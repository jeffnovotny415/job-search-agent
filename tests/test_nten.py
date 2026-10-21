"""NTEN stays a free first pass; project budgets never become salaries."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import nten_source as n
from test_job_quality import agent, job, q

INDEX_HTML = '''<p>1 jobs found</p><div data-testid="JobListing">
<div><span>RFP</span><span>Contract</span><a href="/jobs/site">Website Redesign RFP</a><div>Example Foundation</div></div>
<div>Salary: $30,000 to $40,000<p>Project-based</p></div>
<div>Location: -Remote United States</div><div>Posted on: 10/01/2026</div></div>'''
DETAIL_HTML = '''<main><div><span>RFP</span><h1>Website Redesign RFP</h1>
<a href="https://docs.example.org/rfp.pdf">Download PDF</a><hr>
<div class="fields"><div><p>Organization:</p><p>Example Foundation</p></div>
<div><p>Salary:</p><p>$30,000 to $40,000</p><div><p>/ Project-based</p></div></div>
<div><p>Type:</p><p>Contract</p></div></div><hr>
<div><p>Website redesign with membership renewals and payment integrations.</p>
<p>Proposal deadline: October 30, 2026, 11:59 PM Pacific.</p>
<p>Submit a portfolio and three client references.</p>
<a href="https://docs.example.org/addendum.pdf">Addendum</a></div></div>
<div><div><p>Job Location</p></div><p>-Remote, United States</p></div></main>'''


def opportunity(**extra):
    return job(source='NTEN', title='Website Redesign RFP', opportunity_type='RFP',
               compensation_unit='project', employment_type='Contract', salary='$30,000–$40,000 / Project-based',
               documents=[], source_fingerprint='original', **extra)


class NtenParsingTests(unittest.TestCase):
    def test_listing_and_detail_preserve_company_type_unit_and_all_documents(self):
        jobs=n.parse_index(INDEX_HTML)
        detail=n.parse_detail(DETAIL_HTML,jobs[0])
        self.assertEqual(detail['company'],'Example Foundation')
        self.assertEqual(detail['opportunity_type'],'RFP')
        self.assertEqual(detail['compensation_unit'],'project')
        self.assertEqual(len(detail['documents']),2)
        self.assertIn('Pacific',detail['description'])
        self.assertEqual(n.parse_index(INDEX_HTML.replace('RFP</span>','Employment</span>'))[0]['opportunity_type'],'Contract')

    def test_incomplete_index_and_detail_raise_instead_of_claiming_success(self):
        with self.assertRaises(ValueError): n.parse_index(INDEX_HTML.replace('1 jobs','2 jobs'))
        with self.assertRaises(ValueError): n.parse_detail('<h1>Another job</h1>',opportunity())

    def test_project_and_unknown_units_never_use_annual_floor(self):
        for unit in ('project','unknown'):
            candidate=opportunity();candidate['compensation_unit']=unit
            result=n.screen(candidate)
            self.assertEqual(result['decision'],'pass')
            self.assertTrue(result['flags'])
        candidate=opportunity();candidate['salary']='EUR 40,000 project-based'
        self.assertEqual(n.screen(candidate)['decision'],'skip')

    def test_hourly_contract_and_annual_employment_keep_existing_floors(self):
        for kind,unit,pay,decision in [('RFP','hour','$64 Hourly','skip'),('Contract','hour','$65 Hourly','pass'),
                                      ('Employment','year','$80,000 Yearly','skip'),('Employment','year','$80,000–115,000 Yearly','pass')]:
            candidate=opportunity();candidate.update(opportunity_type=kind,compensation_unit=unit,salary=pay,employment_type='Full-Time' if kind=='Employment' else 'Contract')
            self.assertEqual(n.screen(candidate)['decision'],decision)

    def test_fundraising_development_is_not_software_and_jd_crm_mentions_dont_rescue_it(self):
        candidate=opportunity();candidate.update(title='Development Director',opportunity_type='Employment',description='Use our website and Salesforce for donor cultivation.')
        self.assertEqual(n.screen(candidate)['decision'],'skip')
        candidate['title']='Web Developer, eCRM'
        self.assertEqual(n.screen(candidate)['decision'],'pass')
        candidate['title']='Database Manager'
        self.assertEqual(n.screen(candidate)['decision'],'pass')

    def test_rfp_website_link_is_not_a_web_development_scope(self):
        candidate=opportunity();candidate.update(title='Fundraising consultant RFP',description='Visit our website for submission requirements. We use Salesforce.')
        self.assertEqual(n.screen(candidate)['decision'],'skip')
        candidate.update(title='Technology services RFP',description='Design a new website and implement Salesforce integrations.')
        self.assertEqual(n.screen(candidate)['decision'],'pass')

    def test_project_renewal_features_are_not_customer_success_ownership(self):
        candidate=opportunity();candidate['description']='The system manages customer renewals and payments.'
        self.assertEqual(n.screen(candidate)['decision'],'pass')
        candidate['location']='Hybrid United States'
        self.assertEqual(n.screen(candidate)['decision'],'skip')
        candidate['location']='Outside the US, Remote'
        self.assertEqual(n.screen(candidate)['decision'],'skip')
        candidate['location']='Remote Canada'
        self.assertEqual(n.screen(candidate)['decision'],'skip')

    def test_linked_documents_supply_state_restrictions_and_requirements(self):
        candidate=opportunity();candidate['documents']=[{'url':'https://example.org/a.pdf','text':'Must reside in one of the following states: CA, NY, PA.'}]
        self.assertEqual(n.screen(candidate)['decision'],'skip')
        evidence=n.proposal_evidence('Proposal deadline\nOctober 30, 2026\nSubmit three client references.\nInclude a portfolio.')
        self.assertIn('October 30',str(evidence['Deadlines / dates']))
        self.assertIn('three client references',str(evidence['Proposal requirements']))

    def test_material_fingerprint_ignores_whitespace_dates_and_read_errors(self):
        a=opportunity();b=copy.deepcopy(a)
        b.update(posted_date='new timestamp',description=a['description']+'\n ',source_flags=['Temporary error'])
        self.assertEqual(n.fingerprint(a),n.fingerprint(b))
        b['salary']='$50,000 / Project-based'
        self.assertNotEqual(n.fingerprint(a),n.fingerprint(b))
        b=copy.deepcopy(a);b['documents']=[{'url':'https://example.org/a.pdf','text':'deadline changed'}]
        self.assertNotEqual(n.fingerprint(a),n.fingerprint(b))

    def test_daily_cache_including_failure_prevents_repeated_requests(self):
        cache={};client=Mock();client.get.side_effect=RuntimeError('offline')
        now=datetime(2026,10,21,12,tzinfo=timezone.utc)
        n.crawl(cache,Mock(),client,now)
        jobs,warnings,cached=n.crawl(cache,Mock(),client,now+timedelta(hours=1))
        self.assertTrue(cached);self.assertTrue(warnings);self.assertEqual(client.get.call_count,1)
        n.crawl(cache,Mock(),client,now+timedelta(days=1))
        self.assertEqual(client.get.call_count,2)

    def test_document_read_failure_retains_prior_evidence_and_fingerprint(self):
        cache={};client=Mock();now=datetime(2026,10,21,12,tzinfo=timezone.utc)
        def fetch(url):
            return INDEX_HTML.encode() if url==n.INDEX else DETAIL_HTML.encode() if url.endswith('/jobs/site') else b'%PDF fixture'
        client.get.side_effect=fetch
        with patch.object(n,'document_text',return_value=('Three references. Deadline October 30.',False)):
            first,_,_=n.crawl(cache,Mock(),client,now)
        def failed_doc(url):
            if url.endswith('.pdf'): raise RuntimeError('blocked')
            return fetch(url)
        client.get.side_effect=failed_doc
        second,warnings,_=n.crawl(cache,Mock(),client,now+timedelta(days=1))
        self.assertEqual(first[0]['source_fingerprint'],second[0]['source_fingerprint'])
        self.assertTrue(warnings);self.assertIn('previous text retained',second[0]['documents'][0]['status'])

    def test_robots_restrictions_and_rate_limits(self):
        for status in (403,429,503):
            client=n.PoliteClient()
            with patch.object(client,'_public',return_value='https://example.org'),patch.object(client,'_request',return_value=(status,{},b'')) as request:
                with self.assertRaises(ValueError): client.get('https://example.org/job')
                self.assertEqual(request.call_count,1)
        client=n.PoliteClient()
        robots=b'User-agent: *\nDisallow: /private\nCrawl-delay: 3\n'
        with patch.object(client,'_public',return_value='https://example.org'),patch.object(client,'_request',return_value=(200,{},robots)) as request:
            with self.assertRaises(ValueError): client.get('https://example.org/private')
            self.assertEqual(request.call_count,1)
            self.assertEqual(client.rules['https://example.org'][1],3)


class NtenDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        settings=patch.dict(agent.CONFIG,seen_jobs_file=str(Path(temp.name)/'seen.json'))
        settings.start();self.addCleanup(settings.stop)
        state=patch.multiple(agent,_COLLECT_JOBS=False,_PROCESSED_THIS_RUN=set(),RUN_REPORT={'sources':{},'counts':{},'jobs':[],'warnings':[],'errors':[]})
        state.start();self.addCleanup(state.stop)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('Offline test must not use network'))
        network.start();self.addCleanup(network.stop)
        self.seen={};self.cards=[]

    def process(self,candidate):
        agent._PROCESSED_THIS_RUN.clear()
        return agent.process_job(candidate,self.seen,self.cards,{'Roles':'watching','Projects':'projects'})

    def test_rfp_creates_named_labeled_card_with_no_ai_or_extra_fetch(self):
        with patch.object(agent,'create_trello_card',return_value={'id':'card'}) as create,patch.object(agent,'budgeted_message') as ai,patch.object(agent,'safe_get') as fetch:
            self.assertEqual(self.process(opportunity()),'created')
            self.assertIn('[RFP]',create.call_args.args[1]);self.assertIn('Total project budget',create.call_args.args[2])
            self.assertEqual(self.process(opportunity()),'cached')
            ai.assert_not_called();fetch.assert_not_called();create.assert_called_once()

    def test_changed_active_card_is_annotated_even_if_now_ineligible(self):
        candidate=opportunity();q.record_decision(self.seen,candidate,'created','delivered',card_id='card')
        self.cards=[{**candidate,'card_id':'card','closed':False,'list_name':'Applied'}]
        candidate.update(source_fingerprint='changed',location='Hybrid United States')
        with patch.object(agent,'get_card_description',return_value='User notes'),patch.object(agent,'update_card_description') as update,patch.object(agent,'create_trello_card') as create:
            self.assertEqual(self.process(candidate),'updated')
            self.assertTrue(update.call_args.args[1].startswith('User notes'))
            self.assertIn('Current screen:** skip',update.call_args.args[1]);create.assert_not_called()
            self.assertEqual(self.process(candidate),'cached')

    def test_archived_card_stays_closed_after_change(self):
        candidate=opportunity();q.record_decision(self.seen,candidate,'created','delivered',card_id='card')
        self.cards=[{**candidate,'card_id':'card','closed':True,'list_name':'Watching'}]
        candidate['source_fingerprint']='changed'
        with patch.object(agent,'create_trello_card') as create,patch.object(agent,'update_card_description') as update:
            self.assertEqual(self.process(candidate),'duplicate');create.assert_not_called();update.assert_not_called()

    def test_failed_change_update_retries_and_is_idempotent(self):
        candidate=opportunity();q.record_decision(self.seen,candidate,'created','delivered',card_id='card')
        self.cards=[{**candidate,'card_id':'card','closed':False,'list_name':'Watching'}]
        candidate['source_fingerprint']='changed'
        with patch.object(agent,'get_card_description',side_effect=RuntimeError('temporary')):
            self.assertEqual(self.process(candidate),'delivery_failed')
        entry=self.seen[q.posting_key(candidate)];entry['retry_after']='2020-01-01T00:00:00+00:00'
        with patch.object(agent,'get_card_description',return_value='Already appended NTEN revision: changed'),patch.object(agent,'update_card_description') as update:
            fresh=copy.deepcopy(candidate);fresh.pop('_update_existing',None)
            self.assertEqual(self.process(fresh),'updated');update.assert_not_called()

    def test_previously_filtered_listing_reconsidered_only_on_material_change(self):
        candidate=opportunity();q.record_decision(self.seen,candidate,'filtered','pay')
        self.assertFalse(q.should_process(self.seen,candidate))
        candidate['source_fingerprint']='new'
        self.assertTrue(q.should_process(self.seen,candidate))
