"""Offline requirements and false-positive regressions from the September review."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_job_quality import agent, job, q
from first_pass import (posted_pay, screen_metadata, extract_listing_metadata,
                        parse_idealist_html, display_pay, display_location,
                        explicit_description_exclusions)


def salary(low='80000.00', high='85000.00', period='YEAR'):
    return json.dumps({'currency':'USD', 'value':{'minValue':low, 'maxValue':high, 'unitText':period}})


def posting(**values):
    obj = {'@type':'JobPosting', 'title':'IT Manager', 'hiringOrganization':{'name':'Actual Employer'}, **values}
    return '<script type="application/ld+json">' + json.dumps(obj) + '</script>'


class FeedbackRules(unittest.TestCase):
    def test_numeric_strings_apply_pay_floor(self):
        self.assertEqual(posted_pay(salary()), (85000, 'year', 'USD', True))
        self.assertEqual(screen_metadata(job(salary=salary()))['decision'], 'skip')
        self.assertEqual(screen_metadata(job(salary=salary('85000', '95000')))['decision'], 'pass')
        result = screen_metadata(job(salary=salary('110000', '115000')))
        self.assertFalse(any('Verify pay' in flag for flag in result['flags']))

    def test_invalid_pay_is_uncertain_not_zero(self):
        for value in ['NaN', 'Infinity', '-1', 'not disclosed', None, True]:
            with self.subTest(value=value):
                self.assertIsNone(posted_pay(salary(high=value))[0])
                self.assertEqual(screen_metadata(job(salary=salary(high=value)))['decision'], 'pass')

    def test_structured_pay_dict_and_minimum_only(self):
        pay={'currency':'usd', 'value':{'minValue':'85000', 'unitText':'YEAR'}}
        self.assertEqual(posted_pay(pay),(85000,'year','USD',False))
        self.assertEqual(screen_metadata(job(salary=pay))['decision'],'pass')

    def test_consultant_url_applies_contract_floor_even_from_old_email_cache(self):
        url='https://www.idealist.org/en/consultant-job/'+'a'*32+'-consultant'
        for pay, expected in [('$64/hour','skip'),('$65/hour','pass')]:
            self.assertEqual(screen_metadata(job(url=url,salary=pay))['decision'],expected)
        jobs,complete=parse_idealist_html(f'<p><a href="{url}">Integration Consultant</a><br>Employer<br>$110/hr<br>United States (Remote)</p>')
        self.assertTrue(complete)
        self.assertEqual(jobs[0]['employment_type'],'contract')

    def test_foreign_job_location_vs_remote_employer_headquarters(self):
        for country in ['IN', {'name':'India'}]:
            location={'jobLocation':{'address':{'addressCountry':country}}}
            self.assertEqual(screen_metadata(job(location=location))['decision'],'skip')
            location['jobLocationType']='TELECOMMUTE'
            self.assertEqual(screen_metadata(job(location=location))['decision'],'pass')
        location={'jobLocation':[{'address':{'addressCountry':'CA'}},{'address':{'addressCountry':'US'}}]}
        self.assertEqual(screen_metadata(job(location=location))['decision'],'pass')

    def test_metadata_absence_is_not_proof_of_office_work(self):
        location={'jobLocation':{'address':{'addressCountry':'US','addressLocality':'Boston'}}}
        result=screen_metadata(job(location=location))
        self.assertEqual(result['decision'],'pass')
        self.assertEqual(result['checks']['remote'],'Needs verification')

    def test_display_keeps_pay_range_and_location_readable(self):
        self.assertEqual(display_pay(salary('110000','115000')),'$110,000–$115,000/yr')
        self.assertEqual(display_pay(salary('110','110','HOUR')),'$110/hr')
        value={'jobLocation':{'address':{'addressLocality':'Sacramento','addressRegion':'CA','addressCountry':'US'}},'jobLocationType':'TELECOMMUTE','applicantLocationRequirements':{'name':'US'}}
        self.assertEqual(display_location(json.dumps(value)),'Sacramento, CA, US · Remote (US applicants)')
        self.assertEqual(display_location('Remote, US'),'Remote, US')

    def test_explicit_exclusions(self):
        for text in [
            'This role requires regular travel to active industrial and construction environments.',
            'Travel: 20% of the time.', 'Travel up to 25%.', 'Travel 5–20%.', '15% travel required.',
            'Our hybrid schedule allows up to two days per week of remote work.',
            'This is a full-time, on-site role.', 'You must work from our office.',
            'Work 3 days per week in the office.',
            'Remote position (California residents only).',
            'We are unable to offer employment to non-California residents.',
            'San Francisco: local candidates only.', 'You own account renewals.',
            'Fully own the relationship for a portfolio of utility accounts.',
        ]:
            with self.subTest(text=text):
                self.assertTrue(explicit_description_exclusions(text))

    def test_adjacent_teams_negations_and_technical_language_are_not_rejections(self):
        for text in [
            'Manage Salesforce integrations for fundraising and customer success teams.',
            'Manage vendor license renewals for our internal IT team.',
            'This fully remote role supports hybrid cloud infrastructure.',
            'Candidates must be based in the US.', 'US residents only.',
            'Local candidates only.',
            'No regular travel required.', 'Regular travel is not required.',
            'No local candidates only restriction.', 'On-site work is optional.',
            'This is not a hybrid role.', 'Travel up to 10%.',
            'Remote or hybrid role.', 'There is no requirement to work from our office.',
            'You report to the VP of Customer Success and manage technical integrations.',
        ]:
            with self.subTest(text=text):
                self.assertEqual(explicit_description_exclusions(text),[])

    def test_location_suffix_dedupe_keeps_distinct_specialties(self):
        self.assertTrue(q.titles_match('Program Manager - Wilmington DE (DE)', 'Program Manager - Philadelphia, PA (Philadelphia, PA)'))
        self.assertFalse(q.titles_match('Program Manager - Data (Analytics)', 'Program Manager - People (Operations)'))
        self.assertFalse(q.titles_match('Senior Program Manager', 'Program Manager'))

    def test_new_title_exclusions_preserve_technical_fundraising_support(self):
        for title in ['Director of Customer Operations','Creative Lead, Growth & Operations','Clinic Operations Director','Director, Development Operations','Director of Advancement','Donor Program Manager']:
            self.assertFalse(agent.pre_filter(job(title=title))[0],title)
        for title in ['Salesforce Administrator, Fundraising Systems','IT Manager, Donor Technology','Integration Consultant - Salesforce & Grantmaking Systems']:
            self.assertTrue(agent.pre_filter(job(title=title))[0],title)

    def test_visible_requirement_sections_are_scoped_to_matching_posting(self):
        html=posting(description='Maintain internal tools.')+'<main><h1>IT Manager</h1><h2>Benefits</h2><p>California residents only.</p></main>'
        meta=extract_listing_metadata(html,'IT Manager','https://www.idealist.org/en/job/123')
        self.assertTrue(explicit_description_exclusions(meta['description']))
        meta=extract_listing_metadata(html.replace('<h1>IT Manager','<h1>Another Role'),'IT Manager','https://www.idealist.org/en/job/123')
        self.assertEqual(explicit_description_exclusions(meta['description']),[])


class FeedbackPipeline(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        for context in [
            patch.dict(agent.CONFIG,seen_jobs_file=str(Path(tmp.name)/'seen.json'),alert_extractions_file=str(Path(tmp.name)/'alerts.json')),
            patch.multiple(agent,_ALERT_CACHE=None,_COLLECT_JOBS=False,_JOB_QUEUE={},_PROCESSED_THIS_RUN=set(),RUN_REPORT={'sources':{},'counts':{},'jobs':[],'warnings':[],'errors':[]}),
            patch('requests.sessions.Session.request',side_effect=AssertionError('No live network in regression test')),
        ]:
            context.start();self.addCleanup(context.stop)

    def test_known_employer_fetches_missing_pay_and_corrects_partner_board(self):
        html=posting(baseSalary=json.loads(salary()),description='Manage internal systems.')
        candidate=job(company='Partner Board',salary='Not listed',location='See posting')
        with patch.object(agent,'safe_get',return_value=SimpleNamespace(text=html)) as get,patch.object(agent,'create_trello_card') as create,patch.object(agent,'budgeted_message') as api:
            self.assertEqual(agent.process_job(candidate,{},[],'w'),'filtered')
            self.assertEqual(candidate['company'],'Actual Employer')
            get.assert_called_once();create.assert_not_called();api.assert_not_called()

    def test_complete_verified_metadata_does_not_refetch(self):
        with patch.object(agent,'safe_get') as get:
            agent.recover_first_pass_metadata(job())
            get.assert_not_called()

    def test_retry_with_verified_flag_but_missing_text_still_recovers_description(self):
        with patch.object(agent,'safe_get',return_value=None) as get:
            agent.recover_first_pass_metadata(job(description=''))
            get.assert_called_once()

    def test_failed_annotation_does_not_fail_the_crawl(self):
        agent.RUN_REPORT['created_cards']=[{'company':'Same','card_id':'1'},{'company':'Same','card_id':'2'}]
        with patch.object(agent,'get_card_description',side_effect=TimeoutError):
            agent.flag_same_run_employers()
        self.assertEqual(len(agent.RUN_REPORT['warnings']),2)
        self.assertEqual(agent.run_status(agent.RUN_REPORT),'partial')

    def test_failed_fetch_is_cached_across_restart_and_uncertainty_preserved(self):
        with patch.object(agent,'safe_get',return_value=None) as get,patch.object(agent,'create_trello_card',return_value={'id':'card'}) as create:
            candidate=job(location='See posting',description='',description_verified=False)
            self.assertEqual(agent.process_job(candidate,{},[],'w'),'created')
            self.assertIn('Needs verification',create.call_args.args[2])
            agent._ALERT_CACHE=None
            agent.recover_first_pass_metadata(dict(candidate))
            get.assert_called_once()

    def test_email_office_evidence_survives_structured_remote_metadata(self):
        html=posting(jobLocationType='TELECOMMUTE')
        candidate=job(location='Hybrid',description_verified=False)
        with patch.object(agent,'safe_get',return_value=SimpleNamespace(text=html)):
            agent.recover_first_pass_metadata(candidate)
        self.assertEqual(screen_metadata(candidate)['decision'],'skip')

    def test_explicit_requirement_blocks_even_when_remote_label_passes(self):
        candidate=job(description='This role requires regular travel to construction sites.')
        with patch.object(agent,'create_trello_card') as create,patch.object(agent,'budgeted_message') as api:
            self.assertEqual(agent.process_job(candidate,{},[],'w'),'filtered')
            create.assert_not_called();api.assert_not_called()

    def test_multiple_new_cards_same_employer_are_flagged_without_touching_other_cards(self):
        agent.RUN_REPORT['created_cards']=[{'company':'Same Co','card_id':'new1'},{'company':'Same Co','card_id':'new2'},{'company':'Different','card_id':'new3'}]
        with patch.object(agent,'get_card_description',return_value='User edit') as get,patch.object(agent,'update_card_description') as update:
            agent.flag_same_run_employers()
            self.assertEqual({c.args[0] for c in get.call_args_list},{'new1','new2'})
            self.assertEqual(update.call_count,2)
            self.assertTrue(all(c.args[1].startswith('User edit') and '2 cards' in c.args[1] for c in update.call_args_list))


if __name__=='__main__':
    unittest.main()
