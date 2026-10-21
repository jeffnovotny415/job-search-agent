import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from test_job_quality import agent, job, q
import additional_sources as sources
import community_alerts as alerts
import engagement
import nten_source as n


class EngagementTests(unittest.TestCase):
    def test_contract_does_not_mean_client_project(self):
        self.assertEqual(engagement.track(job(title='WordPress Developer', opportunity_type='Contract', employment_type='Full-Time contract')), 'Roles')
        self.assertEqual(engagement.track(job(title='Technical Project Manager')), 'Roles')
        self.assertEqual(engagement.track(job(title='Website redesign', opportunity_type='RFP')), 'Projects')
        self.assertEqual(engagement.track(job(title='WooCommerce developer, hourly: new store project')), 'Projects')
        self.assertEqual(engagement.track(job(title='Integration Consultant', opportunity_type='Contract', description='Seeking an agency to build our integration.')), 'Projects')

    def test_project_budget_hourly_offer_and_staff_salary_remain_distinct(self):
        base = job(title='Website redesign', opportunity_type='RFP', compensation_unit='project', salary='$4000 fixed-price')
        self.assertEqual(n.screen(base)['decision'], 'pass')
        base.update(compensation_unit='hour', salary='$50/hour', employment_type='Contract')
        self.assertEqual(n.screen(base)['decision'], 'skip')
        base.update(title='Web Developer', opportunity_type='Employment', compensation_unit='year', salary='$80,000/year', employment_type='Full-Time')
        self.assertEqual(n.screen(base)['decision'], 'skip')

    def test_client_address_not_residency_but_explicit_rule_still_applies(self):
        base=job(title='Website redesign', opportunity_type='RFP', compensation_unit='project', location='New York, NY', description='Build our website remotely.')
        self.assertEqual(n.screen(base)['decision'], 'pass')
        base['description']='Vendors must be located in CA, NY, PA.'
        self.assertEqual(n.screen(base)['decision'], 'skip')

    def test_no_cross_lane_fallback(self):
        candidate=job(opportunity_type='RFP')
        with self.assertRaises(ValueError): agent.review_destination({'Roles':'roles'},candidate)
        with self.assertRaises(ValueError): agent.review_destination('roles',candidate)
        self.assertEqual(agent.review_destination({'Roles':'roles','Projects':'projects'},candidate),'projects')
        self.assertEqual(agent.review_destinations({'Watching':'legacy',engagement.PROJECTS:'projects'})['Roles'],'legacy')

    def test_project_cards_excluded_from_status_and_reconciliation_in_any_list(self):
        role={'id':'r','name':'Acme — IT Manager','desc':'**Track:** Roles'}
        project={'id':'p','name':'Client — Website redesign','desc':'**Track:** Projects'}
        with patch.object(agent,'get_trello_cards',return_value=[role,project]):
            self.assertEqual([c['id'] for c in agent.get_all_active_cards({'Applied':'list'})],['r'])
            self.assertEqual([c['card_id'] for c in agent.get_all_board_cards({'Applied':'list'})],['r'])
        self.assertTrue(engagement.project_card({'name':'Client — [RFP] New website'}))

    def test_delivery_routes_both_tracks_without_model_calls(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(agent.CONFIG,seen_jobs_file=str(Path(directory)/'seen.json')), \
             patch.object(agent,'_COLLECT_JOBS',False), patch.object(agent,'_PROCESSED_THIS_RUN',set()), \
             patch.object(agent,'RUN_REPORT',{'counts':{},'jobs':[],'sources':{},'warnings':[]}), \
             patch.object(agent,'create_trello_card',return_value={'id':'c'}) as create, \
             patch.object(agent,'budgeted_message') as ai, patch.object(agent,'safe_get') as fetch:
            for kind,lane in [('RFP','projects'),('Contract','roles')]:
                candidate=job(source='WordPress Jobs',title='WordPress Developer',opportunity_type=kind,
                    employment_type='Contract',compensation_unit='hour',salary='$100/hour',source_fingerprint=kind)
                agent._PROCESSED_THIS_RUN.clear()
                self.assertEqual(agent.process_job(candidate,{},[],{'Roles':'roles','Projects':'projects'}),'created')
                self.assertEqual(create.call_args.args[0],lane)
                self.assertIn('**Track:** '+('Projects' if lane=='projects' else 'Roles'),create.call_args.args[2])
            ai.assert_not_called();fetch.assert_not_called()

    def test_material_change_retry_and_unnamed_client_keep_evidence(self):
        candidate=job(source='Make community',company='',source_fingerprint='a',engagement_track='Projects')
        seen={};q.record_decision(seen,candidate,'needs_review','Unknown client')
        self.assertEqual(seen[q.posting_key(candidate)]['job']['description'],candidate['description'])
        candidate['source_fingerprint']='b'
        self.assertTrue(q.should_process(seen,candidate))


class SourceTests(unittest.TestCase):
    def test_rss_contract_location_and_compensation_preserved(self):
        data='''<rss><channel><item><title>Acme: WordPress Developer</title><link>https://weworkremotely.com/remote-jobs/acme</link><category>Full-Stack Programming</category><type>Contract</type><region>USA Only</region><state>Delaware</state><description>&lt;p&gt;Pay: USD 85/hour&lt;/p&gt;</description></item></channel></rss>'''
        candidate=sources.parse_feed(data,'We Work Remotely')[0]
        self.assertEqual(candidate['company'],'Acme');self.assertEqual(candidate['compensation_unit'],'hour')
        self.assertEqual(candidate['engagement_track'],'Roles');self.assertNotIn('Delaware',candidate['location'])
        with self.assertRaises(ValueError): sources.parse_feed(data.replace('https://weworkremotely.com','https://bad.example'),'We Work Remotely')
        with self.assertRaises(ValueError): sources.parse_feed('<html>Error</html>','WordPress Jobs')

    def test_wordpress_sidebar_company_not_post_author(self):
        data=''.join(f'<div class="job-sidebar__detail"><span class="job-sidebar__detail-label">{key}</span><span class="job-sidebar__detail-value">{value}</span></div>' for key,value in {'Company':'Real Client','Job Type':'Project','Budget':'$4000','Location':'Remote US'}.items())
        candidate=sources.wordpress_detail(data,job(title='WordPress build'))
        self.assertEqual(candidate['company'],'Real Client');self.assertEqual(candidate['compensation_unit'],'project')
        self.assertEqual(candidate['engagement_track'],'Projects')
        with self.assertRaises(ValueError): sources.wordpress_detail('<html>Missing</html>',job())

    def test_greenhouse_and_empty_valid_feed(self):
        data=json.dumps({'jobs':[{'title':'IT Manager','absolute_url':'https://job-boards.greenhouse.io/recidiviz/jobs/1','content':'<p>Salary: USD 120000 per year</p>','location':{'name':'Remote US'}}]})
        candidate=sources.parse_greenhouse(data)[0]
        self.assertEqual(candidate['company'],'Recidiviz');self.assertEqual(candidate['compensation_unit'],'year')
        self.assertEqual(sources.parse_feed('<rss><channel/></rss>','WordPress Jobs'),[])

    def test_proposal_dates_not_question_deadlines(self):
        now=datetime(2026,10,21,tzinfo=timezone.utc)
        candidate=job(description='Questions due October 19, 2026\nProposal Submission Deadline\nNovember 9, 2026')
        self.assertFalse(sources.expired(candidate,now))
        candidate['description']=candidate['description'].replace('November 9','October 9')
        self.assertTrue(sources.expired(candidate,now))
        self.assertFalse(sources.expired(job(description='Questions due October 9, 2026'),now))

    def test_failed_source_does_not_block_others_and_rerun_is_cached(self):
        client=Mock();client.get.side_effect=[RuntimeError('unavailable'),b'<rss><channel/></rss>']
        cache={};now=datetime(2026,10,21,tzinfo=timezone.utc)
        with patch.dict(sources.FEEDS,{'WordPress Jobs':'https://one.example','We Work Remotely':'https://two.example'},clear=True):
            _,health=sources.crawl(cache,Mock(),client,now)
            self.assertFalse(health['WordPress Jobs']['healthy']);self.assertTrue(health['We Work Remotely']['healthy'])
            sources.crawl(cache,Mock(),client,now+timedelta(hours=1))
            self.assertEqual(client.get.call_count,2)


class CommunityTests(unittest.TestCase):
    html='''<article><a href="https://community.make.com/t/website-project/123">Hiring website developer</a><p>Company: Named Client</p><p>Budget: $5000 fixed-price</p><p>Location: Remote US</p><p>Build a website and integrate Airtable.</p></article>'''

    def test_email_only_client_fields_and_project_budget(self):
        jobs,complete=alerts.parse_alert(self.html,'Make community','[Hire Help] Hiring website developer','Wed, 21 Oct 2026 12:00:00 +0000')
        self.assertTrue(complete);self.assertEqual(jobs[0]['company'],'Named Client')
        self.assertEqual(jobs[0]['engagement_track'],'Projects')

    def test_replies_and_freelancer_ads_do_not_create_new_leads(self):
        for subject,html in [('Re: Hiring website developer',self.html),('For hire: website developer',self.html),('Hiring website developer',self.html.replace('/123"','/123/3"'))]:
            self.assertEqual(alerts.parse_alert(html,'Make community',subject),([],True))

    def test_unknown_template_retained_and_missing_company_never_invented(self):
        self.assertEqual(alerts.parse_alert('<p>New newsletter</p>','Make community'),([],False))
        jobs,complete=alerts.parse_alert(self.html.replace('Company: Named Client','Posted by: Someone'),'Make community','Hiring website developer')
        self.assertTrue(complete);self.assertEqual(jobs[0]['company'],'')
        wrong=self.html.replace('community.make.com','malicious.example')
        self.assertEqual(alerts.parse_alert(wrong,'Make community','Hiring website developer'),([],False))

class LiveDiscoveryRegressions(unittest.TestCase):
    def test_regular_office_presence_and_hybrid_work_header_are_rejected(self):
        from first_pass import explicit_description_exclusions as exclusions
        self.assertTrue(exclusions('Our roles require regular on-site presence to foster teamwork.'))
        self.assertTrue(exclusions('You will regularly work with our team at the office in Taipei.'))
        self.assertTrue(exclusions('Flexible working hours and hybrid work:'))
        self.assertFalse(exclusions('We do not require regular on-site presence.'))
        self.assertFalse(exclusions('Design a hybrid workflow for cloud databases.'))

    def test_pay_is_not_company_funding_and_html_entities_decode(self):
        self.assertEqual(sources.listed_pay('We provide $100 million in loans to help people pay their bills.'),'Not listed')
        self.assertEqual(sources.listed_pay('Salary range: $100,000 to $120,000 per year.'),'Salary range: $100,000 to $120,000 per year.')
        self.assertEqual(sources.text('&lt;p&gt;Salary: USD 100000 per year&lt;/p&gt;'),'Salary: USD 100000 per year')

    def test_staff_contract_body_does_not_turn_incidental_website_into_fit(self):
        candidate=job(title='Senior 3D Designer',opportunity_type='Contract',description='Use our website to find projects. Build your website portfolio.')
        self.assertFalse(n.relevant_skills(candidate))

    def test_web_title_exception_does_not_hide_other_title_blocks(self):
        self.assertFalse(agent.pre_filter(job(title='Fullstack Web3 Engineer'),allowed_title_terms={'fullstack'})[0])
        self.assertFalse(agent.pre_filter(job(title='Software Engineer — Compliance Manager'),allowed_title_terms={'software engineer'})[0])
        from first_pass import explicit_description_exclusions as exclusions
        self.assertTrue(exclusions('Work From Home (WFH) option available one day per week.'))
        self.assertFalse(exclusions('No requirement to work from home one day per week.'))

    def test_structured_pay_and_state_metadata_survive_shared_screen(self):
        candidate=job(title='WordPress Developer',opportunity_type='Contract',salary={'currency':'USD','value':{'maxValue':50,'unitText':'HOUR'}}, employment_type='contract',location={'jobLocationType':'TELECOMMUTE','applicantLocationRequirements':[{'@type':'State','name':'CA'}]})
        candidate['compensation_unit']=n.pay_unit(candidate['salary'])
        self.assertEqual(candidate['compensation_unit'],'hour')
        self.assertEqual(n.screen(candidate)['decision'],'skip')
