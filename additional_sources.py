"""Bounded, once-daily public sources. No model calls; independent source health."""
import json
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import nten_source as n
from engagement import track

FEEDS = {
    'WordPress Jobs': 'https://jobs.wordpress.net/feed/',
    'We Work Remotely': 'https://weworkremotely.com/remote-jobs.rss',
    'Recidiviz': 'https://boards-api.greenhouse.io/v1/boards/recidiviz/jobs?content=true',
    'Achievement First RFPs': 'https://www.achievementfirst.org/category/partnerships/',
    'National Trust RFPs': 'https://savingplaces.org/requests-for-proposals',
}
WWR_CATEGORIES = {'Design', 'Product', 'Full-Stack Programming', 'Front-End Programming', 'DevOps and Sysadmin'}


def text(markup):
    return n.soup_for(html.unescape(markup)).get_text('\n', strip=True)


def listed_pay(body):
    # Keep a source sentence with explicit compensation context. Company revenue,
    # loan sizes and funding figures are not an offer of pay.
    for line in re.split(r"\n+|(?<=[.!?])\s+", body):
        money = r'(?:[$€£]\s*|(?:USD|EUR|GBP|INR|CAD)\s*)\d'
        label = r'\b(?:salary|compensation|budget|hourly rate|pay:)'
        if (re.search(money, line) and
                (re.search(label + r'.{0,100}' + money, line, re.I) or
                 re.search(money + r'.{0,60}(?:per hour|/hr|annually|per year)', line, re.I))):
            return n.clean(line)[:500]
    return 'Not listed'


def normalize(job):
    job.setdefault('company', '')
    job.setdefault('salary', listed_pay(job.get('description', '')))
    job.setdefault('location', 'Not specified')
    job.setdefault('employment_type', '')
    job.setdefault('opportunity_type', n.opportunity_kind('', job['employment_type']))
    job.setdefault('compensation_unit', n.pay_unit(job['salary']))
    job['engagement_track'] = track(job)
    job['proposal_evidence'] = n.proposal_evidence(job.get('description', '') + '\n' + '\n'.join(d.get('text', '') for d in job.get('documents', []))) if job['engagement_track'] == 'Projects' else {}
    job['source_fingerprint'] = n.fingerprint(job)
    job['metadata_source'] = 'Official source; deterministic extraction'
    job['description_verified'] = True
    return job


def parse_feed(data, source):
    root = ET.fromstring(data)
    if root.tag != 'rss' or root.find('channel') is None:
        raise ValueError('Unrecognized RSS format')
    jobs = []
    items = root.findall('./channel/item')
    if len(items) > 500:
        raise ValueError('RSS item limit exceeded')
    for item in items:
        title, url = item.findtext('title', '').strip(), item.findtext('link', '').strip()
        expected = 'jobs.wordpress.net' if source == 'WordPress Jobs' else 'weworkremotely.com'
        if not title or urlsplit(url).hostname != expected:
            raise ValueError('Missing title or unexpected posting URL')
        if source == 'We Work Remotely' and item.findtext('category') not in WWR_CATEGORIES:
            continue
        body = text(item.findtext('{http://purl.org/rss/1.0/modules/content/}encoded') or item.findtext('description', ''))
        job = dict(source=source, title=title, url=url, description=body,
                   posted_date=item.findtext('pubDate', ''), expires_at=item.findtext('expires_at', ''))
        if source == 'We Work Remotely':
            company, sep, title = title.partition(': ')
            if not sep:
                raise ValueError('WWR employer/title separator missing')
            job.update(company=company, title=title, employment_type=item.findtext('type', ''),
                       location='Remote ' + ' '.join(filter(None, [item.findtext('region'), item.findtext('country')])) )
            # WWR's state can be employer metadata, not an eligibility restriction.
            if item.findtext('state'):
                job['source_flags'] = ['Verify eligible states in the posting; feed state field: ' + item.findtext('state')]
        jobs.append(normalize(job))
    return jobs


def wordpress_detail(data, job):
    soup = n.soup_for(data)
    fields = {}
    for row in soup.select('.job-sidebar__detail'):
        label, value = row.select_one('.job-sidebar__detail-label'), row.select_one('.job-sidebar__detail-value')
        if label and value:
            fields[n.clean(label.text)] = n.clean(value.text)
    if not fields.get('Company') or not fields.get('Job Type'):
        raise ValueError('WordPress employer/type layout changed')
    job.update(company=fields['Company'], employment_type=fields['Job Type'],
               salary=fields.get('Budget', 'Not listed'), location=fields.get('Location', 'Not specified'))
    for key in ('opportunity_type', 'compensation_unit', 'engagement_track'):
        job.pop(key, None)
    # This board calls all compensation "Budget"; only explicit units are used.
    if re.search(r'project', fields['Job Type'], re.I):
        job['compensation_unit'] = 'project'
    return normalize(job)


def parse_greenhouse(data):
    payload = json.loads(data)
    if not isinstance(payload.get('jobs'), list):
        raise ValueError('Greenhouse jobs missing')
    jobs = []
    for row in payload['jobs']:
        jobs.append(normalize(dict(source='Recidiviz', company='Recidiviz', title=row['title'],
            url=row['absolute_url'], location=row.get('location', {}).get('name', 'Not specified'),
            description=text(row.get('content', '')), posted_date=row.get('first_published', ''),
            expires_at=row.get('application_deadline') or '', employment_type='')))
    return jobs


def parse_trust(data):
    soup = n.soup_for(data)
    rows = soup.select('table tr')
    if not rows:
        raise ValueError('National Trust RFP table missing')
    jobs = []
    for row in rows:
        cells = row.find_all('td')
        if len(cells) < 3 or not cells[1].find('a', href=True):
            continue
        link = cells[1].find('a', href=True)
        title = n.clean(link.get_text(' ', strip=True))
        deadline = n.clean(cells[2].text)
        job = dict(source='National Trust RFPs', company='National Trust for Historic Preservation',
                   title=title, url=urljoin(FEEDS['National Trust RFPs'], link['href']),
                   opportunity_type='RFP', compensation_unit='unknown',
                   description='Proposal deadline: ' + deadline, proposal_deadline=deadline,
                   documents=[{'label': n.clean(a.text), 'url': urljoin(FEEDS['National Trust RFPs'], a['href']),
                               'status': 'Manual access; document not read'} for a in cells[1].select('a[href]')],
                   source_flags=['RFP documents may require a form; verify scope, all addenda and deadline manually'])
        jobs.append(normalize(job))
    return jobs


def parse_achievement(data, url):
    soup = n.soup_for(data)
    body = soup.select_one('.entry-content') or soup.select_one('.post-content')
    if body is None:
        # Current site places the article in this container.
        body = soup.select_one('article')
    if body is None:
        raise ValueError('Achievement First article layout changed')
    heading = soup.select_one('h1.post-title, h1.entry-title') or body.find(['h1', 'h2'])
    if heading is None:
        heading = soup.find('title')
    title = n.clean(heading.text).split(' | ')[0]
    for row in body.select('table tr'):
        paragraph = soup.new_tag('p')
        paragraph.string = row.get_text(' ', strip=True)
        row.replace_with(paragraph)
    docs = [{'url': urljoin(url, a['href']), 'label': n.clean(a.text) or 'RFP document',
             'status': 'Not read; verify linked document'} for a in body.select('a[href]')
            if re.search(r'\.pdf(?:$|\?)', a['href'], re.I)]
    return normalize(dict(source='Achievement First RFPs', company='Achievement First', title=title,
        url=url, opportunity_type='RFP', compensation_unit='unknown', description=body.get_text('\n', strip=True),
        documents=docs, source_flags=['Verify vendor eligibility and remote delivery; client location is not an employee residence rule']))


def expired(job, now=None):
    today = (now or datetime.now(timezone.utc)).date()
    value = job.get('expires_at', '')
    if value:
        try:
            date = datetime.fromisoformat(value.replace('Z', '+00:00')).date() if re.match(r'\d{4}-', value) else parsedate_to_datetime(value).date()
            if date < today:
                return True
        except (ValueError, TypeError):
            pass
    # Only explicit proposal-deadline lines, never questions or project start dates.
    value = job.get('proposal_deadline', '')
    if not value:
        lines = job.get('description', '').splitlines()
        value = ' '.join(' '.join(lines[i:i+2]) for i, line in enumerate(lines)
                         if re.search(r'proposal(?:s| submission)?\s+(?:deadline|due)|proposals? (?:are )?due|proposals? must be submitted.{0,50}no later than', line, re.I))
    match = re.search(r'\b([A-Z][a-z]+ \d{1,2},? \d{4})\b', value)
    if match:
        try:
            return datetime.strptime(match[1].replace(',', ''), '%B %d %Y').date() < today
        except ValueError:
            pass
    return False


def crawl(cache, save, client=None, now=None):
    today = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo('America/New_York')).date().isoformat()
    client = client or n.PoliteClient()
    all_jobs, health = [], {}
    for source, url in FEEDS.items():
        key = 'source:expanded:v1:' + source
        old = cache.get(key, {})
        if old.get('day') == today:
            all_jobs.extend(old.get('jobs', [])); health[source] = old.get('health', {'healthy': False})
            continue
        state = {'day': today, 'jobs': [], 'health': {'healthy': False, 'warning': 'Crawl interrupted'}}
        cache[key] = state; save()
        warnings = []
        try:
            data = client.get(url).decode('utf-8')
            if source in {'WordPress Jobs', 'We Work Remotely'}:
                jobs = parse_feed(data, source)
                if source == 'WordPress Jobs':
                    if len(jobs) > 30:
                        raise ValueError('WordPress daily detail limit exceeded')
                    for job in jobs:
                        try:
                            wordpress_detail(client.get(job['url']).decode('utf-8'), job)
                        except Exception as exc:
                            warnings.append('WordPress detail unavailable: ' + type(exc).__name__)
                            job.setdefault('source_flags', []).append('Posting metadata unavailable; employer needs verification')
            elif source == 'Recidiviz':
                jobs = parse_greenhouse(data)
            elif source == 'National Trust RFPs':
                jobs = parse_trust(data)
            else:
                soup = BeautifulSoup(data, 'html.parser')
                urls = list(dict.fromkeys(urljoin(url, a['href']) for a in soup.select('a[href]')
                           if re.search(r'/request-for-proposals?[^/]*/?$', a['href'])
                           and urlsplit(a['href']).hostname == 'www.achievementfirst.org'))
                if not soup.select('article') and not urls:
                    raise ValueError('Achievement First index layout changed')
                if len(urls) > 5:
                    raise ValueError('RFP detail limit exceeded')
                jobs = [parse_achievement(client.get(link).decode('utf-8'), link) for link in urls]
            if source in {'Achievement First RFPs', 'National Trust RFPs'}:
                prior_jobs = {j['url']: j for j in old.get('jobs', [])}
                for job in jobs:
                    previous_docs = {d['url']: d for d in prior_jobs.get(job['url'], {}).get('documents', [])}
                    for index, doc in enumerate(job.get('documents', [])):
                        if not re.search(r'\.pdf(?:$|\?)', doc['url'], re.I):
                            continue  # Form-gated documents remain manual links.
                        try:
                            if index >= 5:
                                raise ValueError('Document count limit')
                            content = client.get(doc['url'])
                            if not content.startswith(b'%PDF'):
                                raise ValueError('Not a PDF')
                            doc['text'], partial = n.document_text(content)
                            doc['status'] = 'Partially extracted' if partial else 'Text extracted'
                            if partial:
                                job.setdefault('source_flags', []).append('Linked PDF partially read; verify complete document')
                        except Exception as exc:
                            warnings.append('Linked PDF unavailable: ' + type(exc).__name__)
                            prior = previous_docs.get(doc['url'], {})
                            if prior.get('text'):
                                doc['text'] = prior['text']
                            doc['status'] = 'Read failed; previous text retained' if prior.get('text') else 'Read failed; verify manually'
                    normalize(job)
            state['jobs'] = jobs
            state['health'] = {'healthy': not warnings, 'collected': len(jobs), 'warnings': warnings}
        except Exception as exc:
            state['health'] = {'healthy': False, 'warning': f'{type(exc).__name__}: {exc}'}
        save(); all_jobs.extend(state['jobs']); health[source] = state['health']
    return all_jobs, health
