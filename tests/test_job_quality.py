import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import timedelta

os.environ['JOB_AGENT_PROFILE_FILE'] = str(Path(__file__).resolve().parents[1] / 'profile.example.txt')
os.environ['JOB_AGENT_LOG_FILE'] = os.devnull
import job_quality as q
import jeff_job_agent as agent


def job(**extra):
    return {'company': 'Example Foundation', 'title': 'IT Manager',
            'url': 'https://example.org/jobs/123', 'source': 'Fixture',
            'location': 'Remote in United States', 'salary': 'USD 100000',
            'description': 'Remote in United States. Manage internal SaaS systems. ' * 10,
            'description_verified': True, **extra}


def score():
    return dict(score=80, verdict='Apply If Interested', disqualified=False,
                why_it_fits='Systems ownership', concerns='None identified', lane='Lane 1 IT Ops',
                mission_fit='Strong', next_step='Review', salary_ask='100000',
                salary_source='estimated', environment_flags=[], needs_review=False,
                eligibility={k: {'status': 'pass', 'quote': 'Remote in United States'} if k == 'remote'
                             else {'status': 'unknown', 'quote': None}
                             for k in q.ELIGIBILITY_CRITERIA})


class QualityTests(unittest.TestCase):
    def test_salesforce_is_not_sales(self):
        for title in ['Salesforce Administrator', 'Integration Consultant - Salesforce & Grantmaking Systems']:
            self.assertTrue(agent.pre_filter(job(title=title))[0])
        self.assertFalse(agent.pre_filter(job(title='Sales Manager'))[0])

    def test_it_and_ai_are_whole_words(self):
        self.assertFalse(q.contains_term('benefits specialist', 'it'))
        self.assertFalse(q.contains_term('campaign specialist', 'ai'))
        self.assertTrue(q.contains_term('IT Manager', 'it'))

    def test_preserve_nonprofit_development(self):
        self.assertTrue(agent.pre_filter(job(title='Development Director'))[0])

    def test_protected_technicians(self):
        for title in ['IT Technician', 'Network Technician', 'Help Desk Technician']:
            self.assertTrue(agent.pre_filter(job(title=title))[0])
        self.assertFalse(agent.pre_filter(job(title='Associate Officer, Distribution'))[0])

    def test_distinct_employers_are_not_duplicates(self):
        cards=[{'company':'Apex','title':'Technical Program Manager'}]
        self.assertIsNone(q.find_duplicate(job(company='Acme',title='Technical Program Manager'), cards))

    def test_company_aliases_work(self):
        cards=[{'company':'CodePath Org 2','title':'IT Manager'}]
        self.assertIsNotNone(q.find_duplicate(job(company='CodePath'),cards,agent.clean_company_name))

    def test_unknown_employers_do_not_collide(self):
        a=job(company='Unknown'); b=job(company='Unknown',url='https://example.org/jobs/456')
        self.assertNotEqual(q.posting_key(a),q.posting_key(b))
        self.assertIsNone(q.find_duplicate(a,[{**b}]))

    def test_unknown_employer_still_matches_exact_url(self):
        a=job(company='Unknown'); self.assertIsNotNone(q.find_duplicate(a,[{**a,'closed':True}]))

    def test_query_tracking_removed_but_posting_id_retained(self):
        self.assertEqual(q.canonical_url('http://example.org/jobs/123?utm_source=a'),q.canonical_url(job()['url']))
        self.assertNotEqual(q.canonical_url('https://example.org/apply?id=1'),q.canonical_url('https://example.org/apply?id=2'))
        self.assertEqual('',q.canonical_url('https://example.org/jobs'))

    def test_legacy_migration_preserves_data(self):
        j=job(); old={'verdict':'Skip','score':20}; seen={q.legacy_key(j):old}
        self.assertFalse(q.should_process(seen,j))
        self.assertIn(q.legacy_key(j),seen)
        self.assertTrue(q.should_process(seen,job(url='https://example.org/jobs/456')))

    def test_legacy_repairs_are_selective(self):
        for entry in [{'scored':False},{'verdict':'Score withheld'},{'verdict':'Pre-filtered'}]:
            j=job(title='Salesforce Administrator'); seen={q.legacy_key(j):entry}
            self.assertTrue(q.should_process(seen,j))
        j=job(company='Unknown'); self.assertTrue(q.should_process({q.legacy_key(j):{'verdict':'Skip'}},j))

    def test_retry_backoff_and_persisted_input(self):
        seen={}; j=job()
        q.record_decision(seen,j,'score_failed','temporary')
        self.assertFalse(q.should_process(seen,j))
        self.assertTrue(q.should_process(seen,j,q.utcnow()+timedelta(days=2)))
        self.assertIn('job',seen[q.posting_key(j)])

    def test_jsonld_uses_complete_matching_job(self):
        j=job(); text=j['description']+' Travel up to 20% is required.'
        obj={'@type':'JobPosting','title':j['title'],'description':text,'hiringOrganization':{'name':j['company']}}
        html='<script type="application/ld+json">'+json.dumps({'@graph':[obj]})+'</script>'
        out=q.extract_posting(html,j,j['url'])
        self.assertTrue(out['description'].endswith('Travel up to 20% is required.'))
        self.assertEqual(out['company'],j['company'])
        self.assertIsNone(q.extract_posting(html,job(title='Other Manager'),j['url']))

    def test_careers_directory_and_challenge_rejected(self):
        html='<main><h1>Careers</h1>'+job()['description']+'</main>'
        self.assertIsNone(q.extract_posting(html,job(),job()['url']))
        self.assertFalse(q.sufficient_description('Verify you are human. '*50))

    def test_expired_posting(self):
        obj={'@type':'JobPosting','title':'IT Manager','description':job()['description'],'validThrough':'2020-01-01T00:00:00Z'}
        out=q.extract_posting('<script type="application/ld+json">'+json.dumps(obj)+'</script>',job(),job()['url'])
        self.assertTrue(out['expired'])

    def test_invalid_scores_rejected(self):
        for bad in ['80',True,101,-1]:
            x=score();x['score']=bad
            with self.assertRaises(ValueError):q.validate_score(x,job())
        x=score();x['disqualified']='false'
        with self.assertRaises(ValueError):q.validate_score(x,job())

    def test_evidence_must_exist(self):
        x=score();x['eligibility']['remote']['quote']='A fabricated work arrangement'
        self.assertTrue(q.validate_score(x,job())['needs_review'])
        self.assertEqual(x['eligibility']['remote']['status'],'unknown')

    def test_explicit_failure_overrides_score(self):
        x=score();x['eligibility']['remote']={'status':'fail','quote':'Remote in United States'}
        result=q.validate_score(x,job())
        self.assertTrue(result['disqualified']);self.assertEqual(result['score'],0)

    def test_unknown_remote_needs_review(self):
        x=score();x['eligibility']['remote']={'status':'unknown','quote':None}
        self.assertTrue(q.validate_score(x,job())['needs_review'])

    def test_unsupported_salary_is_estimated(self):
        x=score();x.update(salary_source='confirmed',salary_evidence='$150,000')
        self.assertEqual(q.validate_score(x,job())['salary_source'],'estimated')

    def test_idealist_section_counts(self):
        sections=agent.split_into_search_sections('Here are your new updates for "IT" jobs: Example 2 new results found for this search')
        self.assertEqual(sections[0]['stated_count'],2)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.config=patch.dict(agent.CONFIG,seen_jobs_file=str(Path(self.tmp.name)/'seen.json'),alert_extractions_file=str(Path(self.tmp.name)/'extractions.json'))
        agent._ALERT_CACHE = None
        self.config.start();self.addCleanup(self.config.stop)
        agent._PROCESSED_THIS_RUN.clear()
        self.seen={};self.cards=[]

    def test_description_failure_creates_no_card_and_retries(self):
        with patch.object(agent,'enrich_job_description',return_value=job(description_verified=False)), patch.object(agent,'create_trello_card') as create:
            self.assertEqual(agent.process_job(job(),self.seen,self.cards,'list'),'description_unavailable')
            create.assert_not_called()
        self.assertIn('retry_after',self.seen[q.posting_key(job())])

    def test_score_failure_is_retryable(self):
        with patch.object(agent,'enrich_job_description',return_value=job()), patch.object(agent,'score_job_with_claude',return_value=None):
            self.assertEqual(agent.process_job(job(),self.seen,self.cards,'list'),'score_failed')

    def test_delivery_failure_retries_without_rescoring(self):
        with patch.object(agent,'enrich_job_description',return_value=job()), patch.object(agent,'score_job_with_claude',return_value=score()), patch.object(agent,'create_trello_card',side_effect=RuntimeError('temporary')):
            self.assertEqual(agent.process_job(job(),self.seen,self.cards,'list'),'delivery_failed')
        entry=self.seen[q.posting_key(job())];entry['retry_after']='2020-01-01T00:00:00Z'
        agent._PROCESSED_THIS_RUN.clear()
        with patch.object(agent,'score_job_with_claude') as scorer, patch.object(agent,'create_trello_card',return_value={'id':'new'}):
            self.assertEqual(agent.process_job(entry['job'],self.seen,self.cards,'list'),'created')
            scorer.assert_not_called()

    def test_archived_pass_is_never_recreated(self):
        self.cards=[{**job(),'closed':True,'card_id':'old','list_name':'Watching'}]
        with patch.object(agent,'score_job_with_claude') as scorer,patch.object(agent,'create_trello_card') as create:
            self.assertEqual(agent.process_job(job(),self.seen,self.cards,'list'),'duplicate')
            scorer.assert_not_called();create.assert_not_called()

    def test_same_run_duplicate_processed_once(self):
        with patch.object(agent,'enrich_job_description',return_value=job()),patch.object(agent,'score_job_with_claude',return_value=score()),patch.object(agent,'create_trello_card',return_value={'id':'one'}) as create:
            agent.process_job(job(),self.seen,self.cards,'list')
            agent.process_job(job(),self.seen,self.cards,'list')
            self.assertEqual(create.call_count,1)

    def test_valid_unknown_location_does_not_create_card(self):
        x=score();x['needs_review']=True
        with patch.object(agent,'enrich_job_description',return_value=job()),patch.object(agent,'score_job_with_claude',return_value=x),patch.object(agent,'create_trello_card') as create:
            self.assertEqual(agent.process_job(job(),self.seen,self.cards,'list'),'needs_review')
            create.assert_not_called()

    def test_digest_extraction_is_paid_only_once(self):
        from types import SimpleNamespace
        section={'search_name':'IT','raw_text':'One IT role','stated_count':1}
        response=SimpleNamespace(content=[SimpleNamespace(text='[{"title":"IT Manager","company":"Example","url":"https://example.org/jobs/123"}]')])
        with patch.object(agent,'budgeted_message',return_value=response) as call:
            first=agent.extract_idealist_section_listings(object(),'Digest',section)
            agent._ALERT_CACHE=None
            second=agent.extract_idealist_section_listings(object(),'Digest',section)
            self.assertEqual(first,second);self.assertEqual(call.call_count,1)

    def test_budget_deferred_job_is_kept(self):
        from api_budget import BudgetExceeded
        with patch.object(agent,'enrich_job_description',return_value=job()),patch.object(agent,'score_job_with_claude',side_effect=BudgetExceeded()):
            self.assertEqual(agent.process_job(job(),self.seen,self.cards,'list'),'budget_deferred')
        self.assertIn('job',self.seen[q.posting_key(job())])

    def test_unchanged_review_does_not_pay_again(self):
        q.record_decision(self.seen,job(),'needs_review','uncertain',score())
        self.seen[q.posting_key(job())]['retry_after']='2020-01-01T00:00:00Z'
        with patch.object(agent,'enrich_job_description',return_value=job()),patch.object(agent,'score_job_with_claude') as scorer:
            self.assertEqual(agent.process_job(job(),self.seen,self.cards,'list'),'needs_review')
            scorer.assert_not_called()

    def test_empty_pipeline_still_reads_idealist(self):
        with patch.object(agent,'get_gmail_service',return_value=object()),patch.object(agent,'get_trello_lists',return_value={'Watching':'w'}),patch.object(agent,'get_all_active_cards',return_value=[]),patch.object(agent,'load_seen_emails',return_value={}),patch.object(agent,'save_seen_emails'),patch.object(agent,'load_seen_jobs',return_value={}),patch.object(agent,'get_cards_for_duplicate_check',return_value=[]),patch.object(agent,'run_gmail_scan_idealist',return_value=0) as scan,patch.object(agent,'run_gmail_trello_reconciliation'):
            agent.run_gmail_scan();scan.assert_called_once()

if __name__=='__main__':unittest.main()
