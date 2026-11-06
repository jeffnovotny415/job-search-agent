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

    def test_foreign_job_location_is_excluded_even_with_remote_label(self):
        for country in ['IN', {'name':'India'}]:
            location={'jobLocation':{'address':{'addressCountry':country}}}
            self.assertEqual(screen_metadata(job(location=location))['decision'],'skip')
            location['jobLocationType']='TELECOMMUTE'
            self.assertEqual(screen_metadata(job(location=location))['decision'],'skip')
        location={'jobLocation':[{'address':{'addressCountry':'CA'}},{'address':{'addressCountry':'US'}}]}
        self.assertEqual(screen_metadata(job(location=location))['decision'],'skip')

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


    def test_nov6_reported_titles_never_create_cards(self):
        cases = [
            ('Critical Loop', 'Security Operations Engineer'),
            ('Horizon3', 'Staff Technical Support Engineer'),
            ('SCS Global', 'Technical Specialist I, Seafood COC & Aquaculture - Certification & Sales, LATAM'),
            ('Toptal', 'Cryptography Specialist for AI lab'),
            ('Golden 1', 'Web Applications Programmer II'),
            ('EDF', 'Senior EPC Project Manager'),
        ]
        with patch.object(agent, 'create_trello_card') as create, patch.object(agent, 'budgeted_message') as ai:
            for source in ['Remote Impact', 'We Work Remotely', 'NTEN']:
                for company, title in cases:
                    with self.subTest(source=source, title=title):
                        agent._PROCESSED_THIS_RUN.clear()
                        self.assertEqual(agent.process_job(job(company=company, title=title, source=source), {}, [], 'roles'), 'filtered')
            create.assert_not_called(); ai.assert_not_called()

    def test_datakind_still_passes_at_98_to_109k(self):
        candidate = job(company='DataKind', title='IT Operations & Security Lead', salary='$98–109k per year')
        with patch.object(agent, 'create_trello_card', return_value={'id':'c'}) as create, patch.object(agent, 'budgeted_message') as ai:
            self.assertEqual(agent.process_job(candidate, {}, [], 'roles'), 'created')
            self.assertIn('$98–109k', create.call_args.args[2])
            ai.assert_not_called()
        self.assertEqual(agent.CONFIG['annual_salary_floor'], 90000)

    def test_builder_titles_and_engineer_exception(self):
        import role_screen
        for title in ['Product Builder', 'AI Builder', 'Internal Tools Lead', 'Automation Specialist', 'Low-code Developer', 'Technical Product Owner', 'Solutions Builder']:
            with self.subTest(title=title):
                candidate = job(title=title, description='Build with AI and Retool.')
                self.assertIsNone(role_screen.screen(candidate)[0])
                self.assertEqual(candidate['lane'], 'Builder')
        for title in ['Software Engineer', 'Founding Engineer', 'Product Engineer']:
            self.assertIsNone(role_screen.screen(job(title=title, description='Build with AI using Claude Code.'))[0])
            self.assertTrue(role_screen.screen(job(title=title, description='Maintain the production application.'))[0])
            self.assertTrue(role_screen.screen(job(title=title, description='We use Cursor.', description_verified=False))[0])
            self.assertTrue(role_screen.screen(job(title=title, description='Cursor is prohibited.'))[0])
        self.assertTrue(role_screen.screen(job(title='Security Operations Engineer', description='We use Copilot.'))[0])
        self.assertFalse(agent.title_is_plausible(job(title='Technical Specialist')))
        self.assertFalse(agent.title_is_plausible(job(title='Manager')))
        self.assertTrue(agent.title_is_plausible(job(title='Technical Project Manager')))

    def test_builder_description_warnings_and_ai_ban(self):
        import role_screen
        for text in ['No AI-generated code.', 'Work without AI tools.']:
            self.assertTrue(role_screen.screen(job(title='Product Builder', description=text))[0])
        for text in ['A coding interview is required.', '5+ years production experience with React.', 'CS degree required.', 'We reject vibe-coded work.']:
            reason, flags = role_screen.screen(job(title='Product Builder', description=text))
            self.assertIsNone(reason)
            self.assertTrue(flags, text)

    def test_builder_negated_requirements_do_not_block_engineer_exception(self):
        import role_screen
        for text in [
            'We build with AI. No coding interview.',
            'We build with AI. A coding interview is not required.',
            'We build with AI. We do not ban AI tools.',
            'We build with AI. No AI-generated code restrictions.',
        ]:
            self.assertIsNone(role_screen.screen(job(title='Software Engineer', description=text))[0], text)

    def test_builder_delivery_bypasses_only_engineering_block(self):
        for title, expected in [('Software Engineer', 'created'), ('Software Engineer, Web3', 'filtered')]:
            agent._PROCESSED_THIS_RUN.clear()
            with patch.object(agent, 'create_trello_card', return_value={'id':'c'}) as create, patch.object(agent, 'budgeted_message') as ai:
                candidate=job(title=title, description='Build with AI using Claude Code.')
                self.assertEqual(agent.process_job(candidate, {}, [], 'roles'), expected)
                if expected == 'created':
                    self.assertIn('**Lane:** Builder', create.call_args.args[2])
                ai.assert_not_called()

    def test_remote_state_forms_and_no_headquarters_false_positive(self):
        from geographic_filters import state_eligibility
        for location in ['Remote California', 'Remote CA', 'Remote in CA only', 'Remote CA, US']:
            self.assertTrue(state_eligibility('', location)[0], location)
        for text in ['Remote in CA only.', 'Must reside in CA.', 'You must reside in California.']:
            self.assertTrue(state_eligibility(text, 'Remote US')[0], text)
        for location in ['Remote NJ', 'Remote New Jersey', 'Remote CA, NJ', 'Remote US', 'Remote; headquarters in California']:
            self.assertIsNone(state_eligibility('', location)[0], location)
        self.assertIsNone(state_eligibility('This is not remote in CA only.', 'Remote US')[0])
        self.assertIsNone(state_eligibility('', {'jobLocation': {'address': {'addressRegion':'CA', 'addressCountry':'US'}}, 'jobLocationType':'TELECOMMUTE'})[0])

    def test_edf_field_time_sales_share_and_task_pay(self):
        for text, expected in [
            ('Approximately 20% of the time is spent in the field nationally and internationally.', 'field/site'),
            ('20% site visits.', 'field/site'), ('15–25% visiting projects.', 'field/site'),
            ('50% sales and business development.', 'sales/business'),
            ('25% of your time is spent on business development.', 'sales/business'),
            ('This is a task-based compensation model.', 'Task-based'),
            ('You are paid per task.', 'Task-based'),
            ('Work on AI training tasks.', 'Task-based'),
        ]:
            self.assertTrue(any(expected in reason for reason in explicit_description_exclusions(text)), text)
        for text in ['10% in the field.', '24% sales.', 'No 20% on-site work required.', '20% site visits are optional.', 'Increase sales by 50%.']:
            self.assertEqual(explicit_description_exclusions(text), [], text)

    def test_remoteimpact_header_salary_without_structured_pay(self):
        title='Senior EPC Project Manager [Remote]'
        html=posting(title=title, description='Approximately 20% of the time is spent in the field.') + f'<main><h1>{title}</h1><dl><div><dt>Compensation</dt><dd>$120,800 – $201,300 per year</dd></div></dl></main>'
        metadata=extract_listing_metadata(html, title, 'https://remoteimpact.org/jobs/edf/')
        self.assertEqual(posted_pay(metadata['salary']), (201300, 'year', 'USD', True))
        self.assertIn('field/site', explicit_description_exclusions(metadata['description'])[0])
        without_json=html[html.index('<main>'):]
        self.assertEqual(extract_listing_metadata(without_json, title, 'https://remoteimpact.org/jobs/edf/')['salary'], metadata['salary'])
        self.assertNotIn('salary', extract_listing_metadata(without_json, 'Another title', 'https://remoteimpact.org/jobs/edf/'))

    def test_rsl_body_currency_overrides_generic_sidebar(self):
        import additional_sources
        from geographic_filters import foreign_currency
        fields={'Company':'RSL', 'Job Type':'Project', 'Budget':'$2,000-$2,500', 'Location':'Remote'}
        html=''.join(f'<div class="job-sidebar__detail"><span class="job-sidebar__detail-label">{k}</span><span class="job-sidebar__detail-value">{v}</span></div>' for k,v in fields.items())
        html+='<div class="job-detail__body">Our budget is C$2,000–$2,500 on a fixed-price basis.</div>'
        candidate=job(source='WordPress Jobs', title='WordPress website developer', opportunity_type='Employment')
        additional_sources.wordpress_detail(html, candidate)
        self.assertEqual(foreign_currency(candidate['salary']), 'CAD')
        self.assertEqual(candidate['compensation_unit'], 'project')
        self.assertEqual(agent.process_job(candidate, {}, [], 'projects'), 'filtered')

    def test_upwork_rate_contract_preserves_upper_bound(self):
        # Dot owns Upwork intake. This is the shared contract it must enforce;
        # the test does not imply that this crawler handles Dot's emails.
        for value, expected in [('$12–45/hr', 'skip'), ('$12–65/hr', 'pass'), ('$65/hour', 'pass')]:
            self.assertEqual(screen_metadata(job(source='Upwork', employment_type='contract', salary=value))['decision'], expected)

    def test_project_name_and_type_follow_track(self):
        candidate=job(source='WordPress Jobs', title='WordPress website developer', opportunity_type='Employment', compensation_unit='project', salary='$2500', engagement_track='Projects', description='Build a WordPress website.')
        with patch.object(agent, 'create_trello_card', return_value={'id':'c'}) as create:
            self.assertEqual(agent.process_job(candidate, {}, [], {'Roles':'roles', 'Projects':'projects'}), 'created')
            self.assertEqual(create.call_args.args[1], 'Example Foundation — WordPress website developer')
            self.assertIn('**Opportunity:** Project', create.call_args.args[2])
            self.assertNotIn('[Employment]', create.call_args.args[1])

    def test_employer_link_and_full_title_are_rechecked(self):
        board='https://jobs.ffwd.org/companies/example/jobs/123'
        employer='https://job-boards.greenhouse.io/example/jobs/12345'
        board_html=posting(title='IT Operations Manager', url=employer)
        employer_html=posting(title='IT Operations Manager, LATAM', description='Manage systems.')
        candidate=job(url=board, title='IT Operations Manager', source='FFWD Jobs', salary='Not listed')
        with patch.object(agent, 'safe_get', side_effect=[SimpleNamespace(text=board_html, url=board), SimpleNamespace(text=employer_html, url=employer)]) as fetch, patch.object(agent, 'create_trello_card') as create:
            self.assertEqual(agent.process_job(candidate, {}, [], 'roles'), 'filtered')
            self.assertEqual(candidate['employer_url'], employer)
            self.assertEqual(fetch.call_count, 2)
            create.assert_not_called()
        desc=agent.build_first_pass_card_description(job(employer_url=employer), screen_metadata(job()))
        self.assertIn('**Employer link:** '+employer, desc)

    def test_application_link_scoping_excludes_related_jobs(self):
        employer='https://job-boards.greenhouse.io/datakind/jobs/12345'
        html=posting() + f'<main><h1>IT Manager</h1><a href="{employer}">Apply on company site</a><a href="https://job-boards.greenhouse.io/other/jobs/54321">Other role</a></main>'
        self.assertEqual(extract_listing_metadata(html, 'IT Manager')['employer_url'], employer)

    def test_stage_move_updates_one_field_preserving_user_notes(self):
        current='**Stage:** First-pass lead — awaiting Claude deep vet\n**Track:** Roles\n\nMy notes\nDo not alter these.\n'
        response=SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'id':'c'})
        with patch.object(agent, 'get_card_description', return_value=current), patch.object(agent.requests, 'put', return_value=response) as put:
            agent.move_trello_card('c', 'applied-list', 'Applied')
            changes=put.call_args.kwargs['data']
            self.assertEqual(changes['idList'], 'applied-list')
            self.assertEqual(changes['desc'], current.replace('First-pass lead — awaiting Claude deep vet', 'Applied'))
            self.assertEqual(put.call_count, 1)

    def test_builder_policy_reconsiders_old_filtered_not_created_jobs(self):
        candidate=job(title='Product Builder')
        seen={q.posting_key(candidate): {'status':'filtered', 'policy_version':'2026-09-first-pass-v1'}}
        self.assertTrue(q.should_process(seen, candidate))
        seen[q.posting_key(candidate)]['status']='created'
        self.assertFalse(q.should_process(seen, candidate))


if __name__=='__main__':
    unittest.main()
