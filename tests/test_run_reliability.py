"""Offline regressions for partial runs, scheduling, and paid-call reuse."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_job_quality import agent, job, q, score


def digest(count=6):
    urls = [f'https://www.idealist.org/en/nonprofit-job/{i:032x}-it-manager' for i in range(count)]
    text = ' '.join(f'IT Manager {url} Example Foundation Remote' for url in urls)
    return {'search_name': 'IT', 'raw_text': text, 'stated_count': count}, urls


def response(urls):
    return SimpleNamespace(stop_reason='end_turn', content=[SimpleNamespace(text=json.dumps([
        {'title': 'IT Manager', 'company': 'Example Foundation', 'url': url} for url in urls]))])


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        config = patch.dict(agent.CONFIG, **{key: str(Path(tmp.name) / (key + '.json')) for key in (
            'seen_jobs_file', 'alert_extractions_file', 'api_usage_file', 'run_report_file', 'review_jobs_file')})
        config.start(); self.addCleanup(config.stop)
        state = patch.multiple(agent, _ALERT_CACHE=None, _COLLECT_JOBS=False, _JOB_QUEUE={},
            _DEFERRED_EXTRACTIONS={}, _EXTRACTION_ATTEMPTS=set(), _PROCESSED_THIS_RUN=set(),
            RUN_REPORT={'sources': {}, 'counts': {}, 'jobs': [], 'warnings': [], 'errors': []})
        state.start(); self.addCleanup(state.stop)

    def test_partial_warning_does_not_mean_failed(self):
        agent.RUN_REPORT['warnings'] = ['One extraction timed out']
        agent.write_run_report()
        self.assertEqual(json.loads(Path(agent.CONFIG['run_report_file']).read_text())['status'], 'partial')

    def test_fatal_error_and_total_source_outage_still_fail(self):
        report = agent.RUN_REPORT
        self.assertEqual(agent.run_status(report), 'complete')
        report['sources'] = {'crawler': {'healthy': False}}
        self.assertEqual(agent.run_status(report), 'failed')
        report['sources']['Gmail'] = {'healthy': True}
        report['warnings'].append('crawler unavailable')
        self.assertEqual(agent.run_status(report), 'partial')
        report['errors'].append('Cannot read Trello')
        self.assertEqual(agent.run_status(report), 'failed')

    def test_budget_limit_is_partial(self):
        agent.RUN_REPORT['budget_deferred'] = True
        self.assertEqual(agent.run_status(agent.RUN_REPORT), 'partial')

    def test_navigation_routes_are_narrow(self):
        for path in ['/jobs/?q=IT&sort=new', '/jobs/new/', '/jobs/post/', '/jobs/category/climate/', '/jobs/companies/create']:
            self.assertTrue(q.navigation_url('https://techjobsforgood.com' + path))
        for url in ['https://techjobsforgood.com/jobs/12345', 'https://remoteimpact.org/jobs/it-manager',
                    'https://employer.org/jobs/new', 'https://jobs.ffwd.org/companies/acme/jobs/123-manager']:
            self.assertFalse(q.navigation_url(url))

    def test_old_navigation_retry_is_retired_without_fetch_or_score(self):
        seen = {}; candidate = job(url='https://remoteimpact.org/jobs/category/tech/')
        q.record_decision(seen, candidate, 'description_unavailable', 'Old failure')
        seen[q.posting_key(candidate)]['retry_after'] = '2020-01-01T00:00:00Z'
        with patch.object(agent, 'enrich_job_description') as fetch, patch.object(agent, 'score_job_with_claude') as api:
            self.assertEqual(agent.process_job(candidate, seen, [], 'w'), 'not_a_job')
            fetch.assert_not_called(); api.assert_not_called()
        self.assertNotIn('retry_after', seen[q.posting_key(candidate)])

    def test_remote_crawler_filters_navigation_before_source_count(self):
        html = '<a href="/jobs/">All jobs</a><a href="/jobs/post/">Post job</a><a href="/jobs/123/">IT Manager</a>'
        with patch.object(agent, 'safe_get', return_value=SimpleNamespace(text=html)), patch.object(agent.time, 'sleep'):
            jobs = agent.crawl_remote_impact()
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]['url'].endswith('/jobs/123/'))

    def test_optional_missing_fields_do_not_discard_valid_score(self):
        result = score()
        for key in ['salary_ask', 'salary_source', 'lane', 'mission_fit', 'next_step', 'environment_flags']:
            result.pop(key)
        validated = q.validate_score(result, job())
        self.assertEqual(validated['salary_ask'], 'Not provided')
        self.assertTrue(validated['needs_review'])  # missing work evidence still prevents a card
        with self.assertRaises(ValueError):
            q.validate_score({**validated, 'eligibility': None}, job())

    def test_tracking_links_and_all_observed_job_types_split(self):
        kinds = ['nonprofit-job', 'consultant-job', 'business-job', 'government-job', 'job', 'nonprofit-job']
        text = ' '.join(f'Role {i} https://track.pstmrk.it/3ts/www.idealist.org%2Fen%2F{kind}%2F{i:032x}-role%3Futm_source%3Demail/abc Organization {i}' for i, kind in enumerate(kinds))
        batches = q.idealist_batches(text)
        self.assertEqual([len(b['urls']) for b in batches], [5, 1])
        self.assertTrue(all('track.pstmrk' not in b['raw_text'] for b in batches))
        self.assertIn('Role 5', batches[1]['raw_text'])
        self.assertIn('Organization 4', batches[0]['raw_text'])

    def test_timeout_retries_only_missing_batch_after_restart(self):
        section, urls = digest()
        with patch.object(agent, 'budgeted_message', side_effect=[response(urls[:5]), TimeoutError()]) as api:
            first = agent.extract_idealist_section_listings(object(), 'Digest', section)
            self.assertEqual(len(first), 5); self.assertEqual(api.call_count, 2)
        agent._ALERT_CACHE = None; agent._EXTRACTION_ATTEMPTS.clear()
        with patch.object(agent, 'budgeted_message', return_value=response(urls[5:])) as api:
            self.assertEqual(len(agent.extract_idealist_section_listings(object(), 'Digest', section)), 6)
            self.assertEqual(api.call_count, 1)
        with patch.object(agent, 'budgeted_message') as api:
            agent.extract_idealist_section_listings(object(), 'Digest', section)
            api.assert_not_called()

    def test_each_batch_is_available_to_score_before_next_extraction(self):
        section, urls = digest(); events = []
        def extract(*args, **kwargs):
            batch = urls[:5] if not events else urls[5:]
            events.append('extract')
            return response(batch)
        with patch.object(agent, 'budgeted_message', side_effect=extract):
            agent.extract_idealist_section_listings(object(), 'Digest', section, on_batch=lambda _: events.append('score'))
        self.assertEqual(events, ['extract', 'score', 'extract', 'score'])

    def test_pending_digest_waits_for_fresh_candidates(self):
        section, urls = digest()
        key = hashlib.sha256((section['search_name'] + section['raw_text']).encode()).hexdigest()
        agent.queue_extraction(key, section=section, subject='Digest')
        agent._COLLECT_JOBS = True
        with patch.object(agent, 'budgeted_message') as api:
            self.assertEqual(agent.extract_idealist_section_listings(object(), 'Digest', section), [])
            api.assert_not_called()
        self.assertIn(key, agent._DEFERRED_EXTRACTIONS)

    def test_wrong_urls_are_not_cached_or_retried_in_same_run(self):
        section, _ = digest(1)
        with patch.object(agent, 'budgeted_message', return_value=response(['https://example.org/jobs/other'])) as api:
            agent.extract_idealist_section_listings(object(), 'Digest', section)
            agent.extract_idealist_section_listings(object(), 'Digest', section)
            self.assertEqual(api.call_count, 1)
        self.assertFalse(any('listings' in entry for entry in agent.alert_cache().values()))

    def test_large_unrecognized_digest_is_preserved_without_large_paid_call(self):
        section = {'search_name': 'Unknown', 'raw_text': 'x' * 10000, 'stated_count': 40}
        with patch.object(agent, 'budgeted_message') as api:
            agent.extract_idealist_section_listings(object(), 'Digest', section)
            api.assert_not_called()
        self.assertEqual(next(iter(agent.alert_cache().values()))['status'], 'pending')

    def test_fresh_candidates_precede_history_and_budget_deferred_gets_first_look(self):
        fresh = job(url='https://example.org/jobs/fresh')
        old = job(url='https://example.org/jobs/old')
        deferred = job(url='https://example.org/jobs/deferred')
        seen = {}
        for candidate, status in [(old, 'score_failed'), (deferred, 'budget_deferred')]:
            q.record_decision(seen, candidate, status, 'Retry')
            seen[q.posting_key(candidate)]['retry_after'] = '2020-01-01T00:00:00Z'
        agent.save_seen_jobs(seen)
        agent._JOB_QUEUE = {q.posting_key(j): j for j in [old, deferred, fresh]}
        with patch.object(agent, 'get_trello_lists', return_value={'Watching': 'w'}), patch.object(agent, 'get_cards_for_duplicate_check', return_value=[]), patch.object(agent, 'process_job') as process:
            agent.flush_job_queue(fresh_only=True)
            first = [call.args[0]['url'] for call in process.call_args_list]
            self.assertEqual(first, [fresh['url'], deferred['url']])
            self.assertIn(q.posting_key(old), agent._JOB_QUEUE)
            agent.flush_job_queue()
            self.assertEqual(process.call_args_list[-1].args[0]['url'], old['url'])

    def test_collection_mode_restored_after_flush_error(self):
        agent._COLLECT_JOBS = True; agent._JOB_QUEUE = {q.posting_key(job()): job()}
        with patch.object(agent, 'get_trello_lists', return_value={'Watching': 'w'}), patch.object(agent, 'get_cards_for_duplicate_check', return_value=[]), patch.object(agent, 'process_job', side_effect=OSError()):
            with self.assertRaises(OSError):
                agent.flush_job_queue()
        self.assertTrue(agent._COLLECT_JOBS)

    def test_gmail_batch_delivery_survives_outer_state_save(self):
        section, urls = digest(1)
        body = f'Here are your new updates for "IT" jobs: {section["raw_text"]} 1 new result found for this search'
        agent._COLLECT_JOBS = True
        with patch.object(agent, 'get_gmail_service', return_value=object()), patch.object(agent, 'get_trello_lists', return_value={'Watching': 'w'}), patch.object(agent, 'get_all_active_cards', return_value=[]), patch.object(agent, 'load_seen_emails', return_value={}), patch.object(agent, 'save_seen_emails'), patch.object(agent, 'get_cards_for_duplicate_check', return_value=[]), patch.object(agent, 'search_gmail', return_value=[{'id': 'email', 'subject': 'Digest', 'snippet': ''}]), patch.object(agent, 'get_email_body', return_value=body), patch.object(agent.anthropic, 'Anthropic'), patch.object(agent, 'budgeted_message', return_value=response(urls)), patch.object(agent, 'enrich_job_description', side_effect=lambda candidate: job(**candidate, description_verified=True)), patch.object(agent, 'score_job_with_claude', return_value=score()), patch.object(agent, 'create_trello_card', return_value={'id': 'new'}), patch.object(agent, 'run_gmail_trello_reconciliation'):
            agent.run_gmail_scan()
        self.assertEqual(agent.load_seen_jobs()[q.posting_key({'url': urls[0]})]['status'], 'created')

    def test_main_scores_both_fresh_sources_before_historical_retry(self):
        old = job(url='https://example.org/jobs/old'); fresh = job(url='https://example.org/jobs/fresh')
        mail = job(url='https://example.org/jobs/mail'); seen = {}; order = []
        q.record_decision(seen, old, 'score_failed', 'Retry')
        seen[q.posting_key(old)]['retry_after'] = '2020-01-01T00:00:00Z'
        agent.save_seen_jobs(seen)
        def crawl():
            for candidate in [old, fresh]:
                agent.process_job(candidate, seen, [], 'w')
        def scorer(candidate):
            order.append(candidate['url'])
            return {**score(), 'disqualified': True}
        with patch('sys.argv', ['agent']), patch.object(agent, 'run_job_crawl', side_effect=crawl), patch.object(agent, 'run_gmail_scan', side_effect=lambda: agent.collect_idealist_batch([mail])), patch.object(agent, 'get_trello_lists', return_value={'Watching': 'w'}), patch.object(agent, 'get_cards_for_duplicate_check', return_value=[]), patch.object(agent, 'enrich_job_description', side_effect=lambda candidate: {**job(), **candidate, 'description_verified': True}), patch.object(agent, 'score_job_with_claude', side_effect=scorer):
            agent.main()
        self.assertEqual(order, [fresh['url'], mail['url'], old['url']])


if __name__ == '__main__':
    unittest.main()
