"""September 30 noise exclusions, including explicit NJ eligibility and counterexamples."""
import json
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from test_job_quality import agent, job
import test_feedback_regressions as fixtures
from test_feedback_regressions import posting
from first_pass import screen_metadata
from geographic_filters import foreign_location, foreign_currency, state_eligibility


class GeographicRules(unittest.TestCase):
    def test_requested_titles_are_excluded_without_partial_word_matches(self):
        for title in ['Senior Power Systems Engineer - EMT Studies', 'Senior Commercial Operations Manager',
                      'Pricing Operations Analyst', 'Compliance Manager, Program Operations',
                      'Senior Ops Manager - Asia', 'IT Manager, India', 'EMEA Technical Program Manager', 'Operations Manager LATAM']:
            self.assertFalse(agent.pre_filter(job(title=title))[0], title)
        for title in ['IT Manager, Indiana', 'Technology Operations Manager', 'IT Systems Engineer']:
            self.assertTrue(agent.pre_filter(job(title=title))[0], title)

    def test_foreign_countries_with_and_without_country_field(self):
        for location in ['Remote, India', 'Dehradun, Uttarakhand, India', 'Nairobi, Kenya',
                         'Remote US or Canada', 'Remote, UK', 'Remote, Europe',
                         {'jobLocationType':'TELECOMMUTE','jobLocation':{'address':{'addressCountry':'IN'}}},
                         {'jobLocation':{'address':{'addressLocality':'Dehradun, Uttarakhand, India'}}},
                         {'applicantLocationRequirements':{'@type':'Country','name':'Georgia'}}]:
            self.assertEqual(screen_metadata(job(location=location))['decision'],'skip',location)

    def test_us_states_are_not_interpreted_as_country_codes(self):
        for location in ['Remote US', 'Atlanta, GA', 'Georgia, United States', 'Portland, ME', 'Indianapolis, IN',
                         'Remote, New Jersey', 'New Mexico, US', 'Jersey City, NJ', 'Lebanon, PA',
                         {'jobLocation':{'address':{'addressCountry':'USA','addressRegion':'CA'}}},
                         {'applicantLocationRequirements':{'@type':'State','name':'CA'}}]:
            self.assertIsNone(foreign_location(location),location)

    def test_foreign_country_still_rejects_alongside_similarly_named_us_state(self):
        for location in ['New Mexico or Mexico', 'New Jersey or Jersey']:
            self.assertIsNotNone(foreign_location(location),location)

    def test_non_usd_pay_is_rejected_even_without_an_amount(self):
        for pay in ['INR 1800000–2400000/year', '€90k', '£110000/year', 'C$100k', '₹2400000',
                    'JPY 12000000', 'CHF 100000', 'eur 95000',
                    {'currency':'INR','value':{'unitText':'YEAR','maxValue':2400000}},
                    {'currency':'CAD','value':{'unitText':'YEAR','maxValue':None}}]:
            self.assertEqual(screen_metadata(job(salary=pay))['decision'],'skip',pay)
        for pay in ['$100k', 'USD 100000', 'US$100k', 'Not listed', '100000/year']:
            self.assertIsNone(foreign_currency(pay),pay)

    def test_missing_salary_and_location_remain_flagged(self):
        result=screen_metadata(job(location='',salary='Not listed'))
        self.assertEqual(result['decision'],'pass')
        self.assertTrue(result['flags'])

    def test_explicit_state_allowlists_exclude_nj(self):
        for text in ['Remote in the following states: FL, GA, IL, KS, MA, MI, NC, NE, SD, TN, TX, WI, WY.',
                     'Applicants must reside in California, Colorado, Arizona, or Oregon.',
                     'This position may be performed fully remotely by candidates located in California, Colorado, Arizona, or Oregon but travel may be necessary.',
                     'Eligible remote states: CA / NY / PA.',
                     'You must be based in one of the following states: California or New York.']:
            reason,flag=state_eligibility(text)
            self.assertTrue(reason,text);self.assertIsNone(flag)

    def test_nj_full_name_and_abbreviation_pass(self):
        for text in ['Remote in the following states: CA, New Jersey, PA.',
                     'Candidates must reside in NJ or New York.',
                     'Eligible states: FL, OR, NJ.', 'Candidates must reside in the United States.']:
            self.assertEqual(state_eligibility(text),(None,None),text)

    def test_denylists_reverse_the_nj_test(self):
        for text in ['We cannot hire candidates in NJ, CA, or NY.', 'This position is not available in New Jersey.', 'Excluded states: NJ, CA.']:
            self.assertTrue(state_eligibility(text)[0],text)
        for text in ['We cannot hire employees in CA or NY.', 'Excluded states: CA, NY.']:
            self.assertEqual(state_eligibility(text),(None,None),text)

    def test_incomplete_lists_are_flagged_not_rejected(self):
        for text in ['Eligible states: California, New York, ...', 'Eligible states: see our careers site.',
                     'Remote in the following states: CA, other states may be added.',
                     'Applicants must reside in one of the following states: including CA, NY.',
                     'Eligible states: CA, NJResponsibilities', 'Eligible states: CA, Atlantis, NJ.']:
            reason,flag=state_eligibility(text)
            self.assertIsNone(reason,text);self.assertTrue(flag,text)

    def test_state_mentions_outside_an_eligibility_clause_are_ignored(self):
        for text in ['Remote US. Our offices are in California, Colorado, and Oregon.',
                     'Candidates from California are encouraged to apply. Remote nationwide.',
                     'Clients must reside in California. You may work remotely throughout the US.',
                     'Candidates are not required to reside in California.',
                     'Work with teams in NY and CA.']:
            self.assertEqual(state_eligibility(text),(None,None),text)
        reason,_=state_eligibility('Candidates must reside in CA or NY. Our clients are based in New Jersey.')
        self.assertTrue(reason)

    def test_structured_state_restrictions(self):
        location={'applicantLocationRequirements':[{'@type':'State','name':'California'},{'@type':'State','name':'NY'}]}
        self.assertTrue(state_eligibility('',location)[0])
        location['applicantLocationRequirements'].append({'@type':'State','name':'NJ'})
        self.assertEqual(state_eligibility('',json.dumps(location)),(None,None))


class GeographicPipeline(unittest.TestCase):
    setUp = fixtures.FeedbackPipeline.setUp
    def test_state_rule_runs_on_recovered_jd_despite_remote_us_label(self):
        html=posting(description='This position is remote in the following states: CA, NY, PA.', jobLocationType='TELECOMMUTE')
        with patch.object(agent,'safe_get',return_value=SimpleNamespace(text=html)),patch.object(agent,'create_trello_card') as create,patch.object(agent,'budgeted_message') as api:
            self.assertEqual(agent.process_job(job(description='',description_verified=False),{},[],'w'),'filtered')
            create.assert_not_called();api.assert_not_called()

    def test_ambiguous_state_list_is_flagged_on_card(self):
        with patch.object(agent,'create_trello_card',return_value={'id':'card'}) as create:
            self.assertEqual(agent.process_job(job(description='Eligible states: see website.'),{},[],'w'),'created')
            self.assertIn('Verify state eligibility',create.call_args.args[2])

    def test_title_exclusion_avoids_all_fetches_and_paid_calls(self):
        with patch.object(agent,'safe_get') as fetch,patch.object(agent,'create_trello_card') as create,patch.object(agent,'budgeted_message') as api:
            self.assertEqual(agent.process_job(job(title='Pricing Operations Analyst'),{},[],'w'),'filtered')
            fetch.assert_not_called();create.assert_not_called();api.assert_not_called()
