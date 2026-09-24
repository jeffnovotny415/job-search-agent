"""First-pass rules: no paid scoring, named employers, honest metadata flags."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_job_quality import agent, job, q
from first_pass import screen_metadata, posted_pay, parse_idealist_html, parse_wellfound_html, extract_listing_metadata


class MetadataTests(unittest.TestCase):
    def test_annual_floor_uses_upper_offer_and_preserves_overlap(self):
        for pay, decision in [('USD $80,000 - $89,000 / year','skip'),('$85–100k','pass'),('$90,000','pass'),('$100k–120k • 0.2% equity','pass')]:
            with self.subTest(pay=pay):
                self.assertEqual(screen_metadata(job(salary=pay))['decision'],decision)

    def test_part_time_exception_and_contract_floor(self):
        self.assertEqual(screen_metadata(job(title='IT Manager (Part-Time)',salary='$50k'))['decision'],'pass')
        for rate,decision in [(64,'skip'),(65,'pass'),(80,'pass')]:
            self.assertEqual(screen_metadata(job(employment_type='Contract',salary=f'${rate}/hr'))['decision'],decision)
        self.assertEqual(screen_metadata(job(employment_type='Part-time',salary='$40/hour'))['decision'],'pass')
        self.assertEqual(screen_metadata(job(employment_type='Part-time contract',salary='$40/hour'))['decision'],'skip')

    def test_unknown_pay_or_remote_is_flagged_not_invented(self):
        result=screen_metadata(job(location='New York City',salary='Not listed'))
        self.assertEqual(result['decision'],'pass')
        self.assertEqual(result['checks']['remote'],'Needs verification')
        self.assertTrue(any('pay' in f for f in result['flags']))

    def test_explicit_hybrid_and_nonremote_rejected(self):
        for location in ['Hybrid','On Site','New York (On-site)','Hybrid, remote 2 days/week','Not remote']:
            with self.subTest(location=location):
                self.assertEqual(screen_metadata(job(location=location))['decision'],'skip')
        for location in ['Remote only, United States','Onsite or remote, United States','Remote or hybrid','Not hybrid; remote']:
            with self.subTest(location=location):
                self.assertEqual(screen_metadata(job(location=location))['decision'],'pass')

    def test_explicit_foreign_only_remote_is_excluded(self):
        for location in ['Remote only, Canada','Anywhere in India (Remote)','Remote — UK only']:
            with self.subTest(location=location):
                self.assertEqual(screen_metadata(job(location=location))['decision'],'skip')
        self.assertEqual(screen_metadata(job(location='Remote, United States or Canada'))['decision'],'pass')
        location=json.dumps({'jobLocationType':'TELECOMMUTE','applicantLocationRequirements':{'name':'Canada'}})
        self.assertEqual(screen_metadata(job(location=location))['decision'],'skip')
        location=json.dumps({'jobLocationType':'TELECOMMUTE','jobLocation':{'address':{'addressCountry':'Canada'}}})
        self.assertEqual(screen_metadata(job(location=location))['decision'],'pass')

    def test_unknown_currency_and_unbounded_pay_do_not_false_reject(self):
        for pay in ['EUR 80000/year','CAD $85000/year','At least USD $80000/year']:
            result=screen_metadata(job(salary=pay))
            self.assertEqual(result['decision'],'pass');self.assertTrue(result['flags'])

    def test_structured_salary_preserves_period(self):
        pay=json.dumps({'currency':'USD','value':{'minValue':60,'maxValue':65,'unitText':'HOUR'}})
        self.assertEqual(posted_pay(pay),(65,'hour','USD',True))
        self.assertEqual(screen_metadata(job(salary=pay,employment_type='CONTRACTOR'))['decision'],'pass')

    def test_idealist_cards_preserve_metadata_and_spacing(self):
        url='https://track.pstmrk.it/3ts/www.idealist.org%2Fen%2Fnonprofit-job%2F'+'a'*32+'-it-manager%3Futm_source%3Demail/x'
        html=f'<p><a href="{url}">IT  Manager</a><br>Example Foundation<br>USD $100,000 - $120,000 / year<br>Anywhere in United States (Remote)</p><p>1 new result found for this search</p>'
        jobs,complete=parse_idealist_html(html)
        self.assertTrue(complete);self.assertEqual(jobs[0]['company'],'Example Foundation')
        self.assertEqual(jobs[0]['title'],'IT Manager')
        self.assertEqual(jobs[0]['location'],'Anywhere in United States (Remote)')
        self.assertNotIn('track.',jobs[0]['url'])
        jobs,complete=parse_idealist_html(html.replace('1 new result','2 new results'))
        self.assertFalse(complete)

    def test_wellfound_metadata_without_description(self):
        html='''<p>I've found 1 new jobs matching your preferences</p><table><tr><td><b>IT Manager</b><br>Example Foundation<br>/ 11-50 Employees<br>$110–160k | Remote only, United States | 5 years of exp | Full-time</td></tr><tr><td><a href="https://links.wellfound.com/s/c/fixture">Learn More</a></td></tr></table>'''
        jobs,complete=parse_wellfound_html(html)
        self.assertTrue(complete);self.assertEqual(jobs[0]['company'],'Example Foundation')
        self.assertEqual(jobs[0]['salary'],'$110–160k')
        self.assertEqual(jobs[0]['location'],'Remote only, United States')
        self.assertEqual(jobs[0]['description'],'')

    def test_metadata_recovery_does_not_require_description(self):
        obj={'@type':'JobPosting','title':'IT Manager','hiringOrganization':{'name':'Example Foundation'},'jobLocationType':'TELECOMMUTE','baseSalary':{'currency':'USD','value':{'value':95000,'unitText':'YEAR'}}}
        html='<script type="application/ld+json">'+json.dumps(obj)+'</script>'
        result=extract_listing_metadata(html,'IT Manager')
        self.assertEqual(result['company'],'Example Foundation')
        self.assertNotIn('description',result)

    def test_unrelated_multiple_jsonld_jobs_are_not_guessed(self):
        html='<script type="application/ld+json">'+json.dumps([{'@type':'JobPosting','title':'Developer'},{'@type':'JobPosting','title':'Designer'}])+'</script>'
        self.assertEqual(extract_listing_metadata(html,'IT Manager'),{})


class FirstPassPipelineTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        settings=patch.dict(agent.CONFIG,seen_jobs_file=str(Path(tmp.name)/'seen.json'),alert_extractions_file=str(Path(tmp.name)/'alerts.json'))
        settings.start();self.addCleanup(settings.stop)
        state=patch.multiple(agent,_ALERT_CACHE=None,_COLLECT_JOBS=False,_JOB_QUEUE={},_PROCESSED_THIS_RUN=set(),RUN_REPORT={'sources':{},'counts':{},'jobs':[],'warnings':[],'errors':[]})
        state.start();self.addCleanup(state.stop)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('Offline test must not use network'))
        network.start();self.addCleanup(network.stop)
        self.seen={}

    def test_unknown_employer_never_makes_unnamed_card(self):
        with patch.object(agent,'safe_get',return_value=None),patch.object(agent,'create_trello_card') as create,patch.object(agent,'budgeted_message') as api:
            self.assertEqual(agent.process_job(job(company='Unknown'),self.seen,[],'w'),'needs_review')
            create.assert_not_called();api.assert_not_called()

    def test_employer_recovery_creates_named_first_pass_card(self):
        html='<script type="application/ld+json">'+json.dumps({'@type':'JobPosting','title':'IT Manager','hiringOrganization':{'name':'Recovered Foundation'}})+'</script>'
        with patch.object(agent,'safe_get',return_value=SimpleNamespace(text=html)),patch.object(agent,'create_trello_card',return_value={'id':'card'}) as create,patch.object(agent,'budgeted_message') as api:
            self.assertEqual(agent.process_job(job(company='Unknown'),self.seen,[],'w'),'created')
            self.assertEqual(create.call_args.args[1],'Recovered Foundation — IT Manager')
            self.assertIn('awaiting Claude deep vet',create.call_args.args[2])
            self.assertNotIn('**Score:**',create.call_args.args[2]);api.assert_not_called()

    def test_recovered_employer_is_checked_against_archived_cards(self):
        html='<script type="application/ld+json">'+json.dumps({'@type':'JobPosting','title':'IT Manager','hiringOrganization':{'name':'Recovered Foundation'}})+'</script>'
        cards=[{'company':'Recovered Foundation','title':'IT Manager','url':'','closed':True,'card_id':'old','list_name':'Watching'}]
        with patch.object(agent,'safe_get',return_value=SimpleNamespace(text=html)),patch.object(agent,'create_trello_card') as create:
            self.assertEqual(agent.process_job(job(company='Unknown'),self.seen,cards,'w'),'duplicate')
            create.assert_not_called()

    def test_no_full_description_fetch_or_scoring_for_known_employer(self):
        with patch.object(agent,'safe_get') as get,patch.object(agent,'create_trello_card',return_value={'id':'card'}),patch.object(agent,'budgeted_message') as api:
            self.assertEqual(agent.process_job(job(description='',description_verified=False),self.seen,[],'w'),'created')
            get.assert_not_called();api.assert_not_called()

    def test_underpaid_and_nonremote_roles_never_create_cards(self):
        for candidate in [job(salary='$80k'),job(location='Hybrid'),job(employment_type='Contract',salary='$60/hr')]:
            agent._PROCESSED_THIS_RUN.clear()
            with patch.object(agent,'create_trello_card') as create,patch.object(agent,'budgeted_message') as api:
                self.assertEqual(agent.process_job(candidate,{},[],'w'),'filtered')
                create.assert_not_called();api.assert_not_called()

    def test_old_deep_rejection_can_be_reconsidered_but_created_is_preserved(self):
        key=q.posting_key(job())
        self.seen[key]={'status':'rejected','policy_version':'2026-09-evidence-v1'}
        self.assertTrue(q.should_process(self.seen,job()))
        self.seen[key]['status']='created'
        self.assertFalse(q.should_process(self.seen,job()))
        legacy={q.legacy_key(job()):{'scored':True,'verdict':'Skip','score':30}}
        self.assertTrue(q.should_process(legacy,job()))

    def test_wellfound_alerts_are_cached_and_free(self):
        html='''<p>found 1 new jobs</p><table><tr><td>IT Manager<br>Example Foundation / 11-50 Employees<br>$100k | Remote, United States | 5 years of exp</td></tr><tr><td><a href="https://wellfound.com/jobs/123-it-manager">Learn More</a></td></tr></table>'''
        service=Mock();service.users.return_value.messages.return_value.get.return_value.execute.return_value={'payload':{'mimeType':'text/html','body':{'data':base64.urlsafe_b64encode(html.encode()).decode()}}}
        with patch.object(agent,'search_gmail',return_value=[{'id':'mail'}]),patch.object(agent,'create_trello_card',return_value={'id':'card'}) as create,patch.object(agent,'budgeted_message') as api:
            agent.run_metadata_alerts(service,'Wellfound',self.seen,[],'w')
            agent._ALERT_CACHE=None
            agent.run_metadata_alerts(service,'Wellfound',self.seen,[],'w')
            self.assertEqual(create.call_count,1)
            self.assertEqual(service.users.return_value.messages.return_value.get.call_count,1)
            api.assert_not_called()

    def test_incomplete_old_email_is_replayed_from_saved_html(self):
        html='''<p>found 1 new jobs</p><table><tr><td>IT Manager<br>Example Foundation / 11-50 Employees<br>$100k | Remote, United States | 5 years of exp</td></tr><tr><td><a href="https://wellfound.com/jobs/123-it-manager">Learn More</a></td></tr></table>'''
        agent.queue_extraction('old',html=html,alert_source='Wellfound')
        with patch.object(agent,'search_gmail',return_value=[]),patch.object(agent,'create_trello_card',return_value={'id':'card'}) as create,patch.object(agent,'budgeted_message') as api:
            agent.run_metadata_alerts(object(),'Wellfound',self.seen,[],'w')
            create.assert_called_once();api.assert_not_called()
        self.assertIn('listings',agent.alert_cache()['old'])

    def test_wellfound_resolver_never_follows_into_blocked_jd(self):
        candidate=job(url='https://links.wellfound.com/s/c/job')
        with patch.object(agent.requests,'get',return_value=SimpleNamespace(headers={'Location':'https://wellfound.com/jobs/123-it-manager'})) as get:
            result=agent.resolve_wellfound_job_link(candidate)
            self.assertEqual(result['url'],'https://wellfound.com/jobs/123-it-manager')
            get.assert_called_once();self.assertFalse(get.call_args.kwargs['allow_redirects'])


if __name__=='__main__':unittest.main()
