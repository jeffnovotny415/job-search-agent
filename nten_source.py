"""Free, daily NTEN discovery for employment and Ravensbreath Lab projects.

Source text is evidence, never instructions. PDF extraction is bounded and has
no OCR; inaccessible documents remain linked and explicitly need verification.
"""
import hashlib
import io
import ipaddress
import json
import re
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

from first_pass import screen_metadata, explicit_description_exclusions
from geographic_filters import state_eligibility

INDEX = 'https://www.nten.org/jobs'
CACHE_KEY = 'source:nten:v1'
USER_AGENT = 'JobDiscoveryBot/1.0 (+https://github.com/jeffnovotny415/job-search-agent)'
MAX_BYTES = 8 * 1024 * 1024
SKILLS = {
    'Websites / web apps': r'\b(?:websites?|web (?:development|developer|design|apps?|applications?)|wordpress|drupal|front[- ]?end|full[- ]?stack|ecrm)\b',
    'Integrations / systems': r'\b(?:integrations?|salesforce|crm|business systems|information (?:technology|systems)|IT (?:manager|director|support|operations)|systems (?:and data )?(?:manager|administrator|analyst))\b',
    'Automation / internal tools': r'\b(?:automation|workflows?|internal tools?|business applications?|AI (?:engineer|operations)|artificial intelligence)\b',
    'Databases': r'\b(?:databases?|data (?:architecture|engineer|engineering)|SQL)\b',
    'Nonprofit operations': r'\b(?:operations (?:manager|director)|director of operations|technical (?:project|program) manager)\b',
}


def clean(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def soup_for(html):
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup.select('style, script, noscript'):
        tag.decompose()
    return soup


def opportunity_kind(category, employment):
    if category.lower() == 'rfp':
        return 'RFP'
    return 'Contract' if re.search(r'contract|freelance|consultant', employment, re.I) else 'Employment'


def pay_unit(salary):
    if re.search(r'project[- ]based|total (?:project )?budget|fixed[- ](?:price|fee)', salary, re.I):
        return 'project'
    if re.search(r'hourly|/\s*(?:hr|hour)|per hour', salary, re.I):
        return 'hour'
    if re.search(r'yearly|annual|/year', salary, re.I):
        return 'year'
    return 'unknown'


def parse_index(html):
    soup = soup_for(html)
    jobs = []
    rows = soup.select('[data-testid="JobListing"]')
    for row in rows:
        columns = row.find_all('div', recursive=False)
        link = row.select_one('a[href^="/jobs/"]')
        if len(columns) < 4 or not link:
            raise ValueError('NTEN index layout changed; incomplete listing')
        lines = list(columns[0].stripped_strings)
        title = clean(link.get_text(' ', strip=True))
        title_pos = lines.index(title) if title in lines else -1
        if title_pos < 1 or title_pos + 1 >= len(lines):
            raise ValueError('NTEN employer/type missing')
        category, employment = lines[0], ' '.join(lines[1:title_pos])
        salary = re.sub(r'^Salary:\s*', '', clean(columns[1].get_text(' ', strip=True)))
        location = clean(columns[2].get_text(' ', strip=True)).replace('Location:', '').strip()
        jobs.append({'source': 'NTEN', 'title': title, 'company': clean(lines[title_pos + 1]),
                     'url': urljoin(INDEX, link['href']), 'salary': salary,
                     'compensation_unit': pay_unit(salary), 'location': clean(location),
                     'employment_type': employment, 'opportunity_type': opportunity_kind(category, employment),
                     'posted_date': clean(columns[3].get_text(' ', strip=True)).replace('Posted on:', '').strip(),
                     'description': '', 'metadata_source': 'NTEN listing'})
    expected = re.search(r'(\d+)\s+jobs? found', soup.get_text(' ', strip=True), re.I)
    if not jobs or expected and int(expected[1]) != len(jobs):
        raise ValueError('NTEN listing count mismatch or empty source; check pagination/layout')
    return jobs


def parse_detail(html, job):
    soup = soup_for(html)
    heading = soup.find('h1')
    if not heading or clean(heading.get_text(' ', strip=True)) != job['title']:
        raise ValueError('NTEN posting heading missing or mismatched')
    column = heading.parent
    bodies = [node for node in column.find_all('div', recursive=False) if not node.get('class')]
    if not bodies:
        raise ValueError('NTEN description layout changed')
    body = bodies[-1]
    result = dict(job)
    result['description'] = body.get_text('\n', strip=True)
    result['description_verified'] = True
    for label, key in [('Organization:', 'company'), ('Type:', 'employment_type'), ('Salary:', 'salary')]:
        label_node = column.find('p', string=lambda s: s and s.strip() == label)
        value = label_node.find_next_sibling('p') if label_node else None
        if value:
            result[key] = (clean(label_node.parent.get_text(' ', strip=True)).removeprefix(label).strip()
                           if key == 'salary' else clean(value.get_text(' ', strip=True)))
    for label, key in [('Job Location', 'location'), ('Additional location info', 'location_info'), ('Job Function', 'job_function')]:
        node = soup.find(string=lambda s: s and s.strip() == label)
        if node:
            texts = list(node.parent.parent.stripped_strings)
            result[key] = clean(' '.join(texts[1:]))
    result['compensation_unit'] = pay_unit(result['salary'])
    if result['opportunity_type'] != 'RFP':
        result['opportunity_type'] = opportunity_kind('', result['employment_type'])
    docs = {}
    for link in column.select('a[href]'):
        url = urljoin(job['url'], link['href'])
        label = clean(link.get_text(' ', strip=True)) or 'Linked document'
        if re.search(r'\.pdf(?:$|\?)|/RFPs/', url, re.I) or re.search(r'\brfp\b|addendum|proposal document|scope of work', label, re.I):
            if urlsplit(url).scheme in {'http', 'https'}:
                docs[url] = {'url': url, 'label': label}
    result['documents'] = list(docs.values())
    return result


def document_text(data):
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ValueError('Encrypted PDF')
    text = '\n'.join(page.extract_text() or '' for page in reader.pages[:30])
    if not text.strip():
        raise ValueError('PDF has no extractable text; OCR needed')
    partial = len(reader.pages) > 30 or len(text) > 100000
    return text[:100000], partial


def proposal_evidence(text):
    """Keep source excerpts, not inferred dates or an invented compliance verdict."""
    lines = [clean(line) for line in text.splitlines() if clean(line)]
    patterns = {
        'Deadlines / dates': r'proposal deadline|proposals?.{0,30}(?:due|accepted)|applications?.{0,30}due|submit.{0,60}\bby\b|no (?:new|additional) inquiries',
        'Proposal requirements': r'references?|submit|submission|proposal (?:must|should|requirements)|portfolio|bios?\b|itemized|cost proposal|insurance|eligibility|vendor qualifications',
    }
    evidence = {}
    for label, pattern in patterns.items():
        snippets = []
        for index, line in enumerate(lines):
            if re.search(pattern, line, re.I) and not re.match(r'^[IVX]+\. ', line):
                # PDFs often put a heading/date or a wrapped requirement on the next line.
                snippet = clean(' '.join(lines[index:index + 3]))[:400]
                if snippet not in snippets:
                    snippets.append(snippet)
        if label == 'Proposal requirements':
            snippets.sort(key=lambda s: (not bool(re.search(r'references?|must include|subject line', s, re.I)),
                                         not bool(re.search(r'required|must|submit', s, re.I))))
        evidence[label] = snippets[:8]
    return evidence


def fingerprint(job):
    fields = ('title', 'company', 'salary', 'compensation_unit', 'location', 'location_info',
              'employment_type', 'opportunity_type', 'job_function', 'description')
    material = {key: clean(job.get(key)) for key in fields}
    material['documents'] = sorted((doc['url'], hashlib.sha256(clean(doc.get('text')).encode()).hexdigest())
                                   for doc in job.get('documents', []))
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def relevant_skills(job):
    # Employment uses title only: routine mentions of CRM in a fundraising JD
    # must not turn a Development Director into a software candidate.
    title = job.get('title', '')
    matches = [label for label, pattern in SKILLS.items() if re.search(pattern, title, re.I)]
    if job.get('opportunity_type') in {'Contract', 'RFP'}:
        text = job.get('description', '') + '\n' + '\n'.join(doc.get('text', '') for doc in job.get('documents', []))
        action = r'\b(?:build|design|redesign|develop|migrat|implement|integrat|automat|administ|configur|maintain|optimi|replac|creat|customiz|deploy|upgrad)\w*'
        for label, pattern in SKILLS.items():
            # A link to the organization's website or a mention of its tools is
            # not a deliverable. Require an action near the technical term.
            for sentence in re.split(r'(?<=[.!?;])\s+|\n+', text):
                for match in re.finditer(pattern, sentence, re.I):
                    context = sentence[max(0, match.start()-100):match.end()+100]
                    if re.search(action, context, re.I) and label not in matches:
                        matches.append(label)
    return matches


def screen(job, annual_floor=90000, contract_floor=65, home_state='NJ'):
    project = job.get('compensation_unit') == 'project'
    candidate = dict(job)
    if project or job.get('compensation_unit') == 'unknown':
        # Preserve original money/currency, but never infer an annual period from its size.
        from geographic_filters import foreign_currency
        currency = foreign_currency(job.get('salary', ''))
        candidate['salary'] = f'{currency} (period unknown)' if currency else 'Not listed'
    if job.get('opportunity_type') in {'Contract', 'RFP'}:
        candidate['employment_type'] = 'contract'
    candidate['location'] = clean(job.get('location', '') + ' ' + job.get('location_info', ''))
    result = screen_metadata(candidate, annual_floor, contract_floor)
    if re.search(r'\b(?:outside (?:the )?(?:US|USA|U\.S\.|United States)|non[- ]US)\b', candidate['location'], re.I):
        result.update(decision='skip', reason='Listing explicitly located outside the US')
    result['checks']['salary'] = job.get('salary', 'Not listed')
    skills = relevant_skills(job)
    result['skills'] = skills
    if result['decision'] == 'pass' and not skills:
        result.update(decision='skip', reason='No specific technical or nonprofit operations match; development alone is not software')
    if project:
        result['flags'] = [flag for flag in result['flags'] if flag != 'Verify pay, currency, and pay period']
        result['flags'].append('Total project budget: assess scope, delivery effort, and margin; not an annual salary or an hourly offer')
    text = job.get('description', '') + '\n' + '\n'.join(doc.get('text', '') for doc in job.get('documents', []))
    reason, flag = state_eligibility(text, candidate['location'], home_state)
    exclusions = explicit_description_exclusions(text, candidate['location'])
    if job.get('opportunity_type') != 'Employment':
        exclusions = [item for item in exclusions if item != 'Role owns customer accounts/renewals']
    if reason or exclusions:
        result.update(decision='skip', reason=reason or '; '.join(exclusions))
    if flag:
        result['flags'].append(flag)
    result['flags'].extend(job.get('source_flags', []))
    if job.get('opportunity_type') in {'RFP', 'Contract'}:
        result['flags'].append('Verify proposal deadline/time zone, references, eligibility, scope and all linked documents/addenda before pursuing')
    return result


def card_details(job, result):
    lines = [f"**Opportunity:** {job['opportunity_type']}",
             f"**Compensation unit:** {job.get('compensation_unit', 'unknown')} (as listed)",
             f"**Posted:** {job.get('posted_date', 'Not specified')}",
             f"**Relevant work:** {', '.join(result.get('skills', [])) or 'Needs verification'}"]
    if job.get('location_info'):
        lines.append(f"**Additional location info:** {job['location_info']}")
    for label, snippets in job.get('proposal_evidence', {}).items():
        if snippets:
            lines += ['', f'**{label} — source excerpts; verify current documents:**']
            lines += ['- ' + snippet for snippet in snippets]
    if job.get('documents'):
        lines += ['', '**Linked documents (including addenda):**']
        lines += [f"- [{doc['label']}]({doc['url']}) — {doc.get('status', 'Not read')}" for doc in job['documents']]
    return '\n'.join(lines)


class PoliteClient:
    """Robots checks, host pacing, bounded public HTTP requests, no browser/AI."""
    def __init__(self):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = USER_AGENT
        self.rules, self.last, self.blocked = {}, {}, set()

    def _public(self, url):
        parsed = urlsplit(url)
        if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            raise ValueError('Unsupported document URL')
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError('Non-public document host')
        return f'{parsed.scheme}://{parsed.netloc}'

    def _request(self, url, delay=1):
        origin = self._public(url)
        time.sleep(max(0, delay - (time.monotonic() - self.last.get(origin, 0))))
        self.last[origin] = time.monotonic()
        with self.session.get(url, timeout=(10, 25), stream=True, allow_redirects=False) as response:
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValueError('Response exceeds size limit')
            return response.status_code, response.headers, bytes(data)

    def _rules(self, origin):
        if origin not in self.rules:
            url = origin + '/robots.txt'
            for _ in range(5):
                status, headers, data = self._request(url)
                if 300 <= status < 400 and headers.get('Location'):
                    url = urljoin(url, headers['Location'])
                    continue
                break
            else:
                raise ValueError('Robots redirect limit')
            if status >= 500 or status in {401, 403, 429} or 300 <= status < 400:
                raise ValueError(f'Robots unavailable or restricted ({status}); crawl deferred')
            parser = RobotFileParser()
            parser.parse(data.decode('utf-8', errors='replace').splitlines() if status == 200 else [])
            # Other 4xx responses mean robots unavailable (RFC 9309 2.3.1.3).
            if 400 <= status < 500:
                parser.allow_all = True
            delay = max(1, parser.crawl_delay('JobDiscoveryBot') or 0)
            rate = parser.request_rate('JobDiscoveryBot')
            if rate:
                delay = max(delay, rate.seconds / rate.requests)
            self.rules[origin] = parser, delay
        return self.rules[origin]

    def get(self, url):
        for _ in range(5):
            origin = self._public(url)
            if origin in self.blocked:
                raise ValueError('Host deferred after a robots/rate-limit failure')
            try:
                parser, delay = self._rules(origin)
            except Exception:
                self.blocked.add(origin)
                raise
            if not parser.can_fetch('JobDiscoveryBot', url):
                raise ValueError('Disallowed by robots.txt')
            status, headers, data = self._request(url, delay)
            if 300 <= status < 400 and headers.get('Location'):
                url = urljoin(url, headers['Location'])
                continue
            if status in {429, 503}:
                self.blocked.add(origin)
            if status != 200:
                raise ValueError(f'HTTP {status}')
            return data
        raise ValueError('Redirect limit')


def crawl(cache, save, client=None, now=None):
    """At most one crawl attempt per Eastern calendar day; reuse snapshots on reruns."""
    today = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo('America/New_York')).date().isoformat()
    old = cache.get(CACHE_KEY, {})
    if old.get('day') == today:
        return old.get('jobs', []), old.get('warnings', []), True
    state = {'day': today, 'jobs': [], 'warnings': ['NTEN crawl interrupted before completion']}
    cache[CACHE_KEY] = state
    save()  # Mark the attempt before network activity, including failures/interruption.
    client = client or PoliteClient()
    warnings = []
    try:
        jobs = parse_index(client.get(INDEX).decode('utf-8'))
    except Exception as exc:
        state['warnings'] = [f'NTEN index unavailable: {type(exc).__name__}: {exc}']
        save()
        return [], state['warnings'], False
    previous = {job['url']: job for job in old.get('jobs', [])}
    for listing in jobs:
        job = dict(listing)
        prior = previous.get(job['url'], {})
        flags = []
        try:
            job = parse_detail(client.get(job['url']).decode('utf-8'), job)
        except Exception as exc:
            flags.append(f'NTEN detail unavailable: {type(exc).__name__}; verify original posting')
            # Retain last good details but do not claim to have observed a material change.
            job = json.loads(json.dumps(prior or job))
        prior_docs = {doc['url']: doc for doc in prior.get('documents', [])}
        for index, doc in enumerate(job.get('documents', [])):
            try:
                if index >= 5:
                    raise ValueError('Document count limit')
                data = client.get(doc['url'])
                if not data.startswith(b'%PDF'):
                    raise ValueError('Linked document is not a PDF; inspect link manually')
                text, partial = document_text(data)
                doc.update(text=text, status='Partially extracted' if partial else 'Text extracted')
                if partial:
                    flags.append(f"Document only partly read: {doc['label']}")
            except Exception as exc:
                doc['text'] = prior_docs.get(doc['url'], {}).get('text', '')
                doc['status'] = 'Read failed; previous text retained' if doc['text'] else 'Needs manual review'
                flags.append(f"{doc['label']}: {type(exc).__name__}; verify linked document")
        job['source_flags'] = flags
        job['source_fingerprint'] = fingerprint(job)
        text = job.get('description', '') + '\n' + '\n'.join(doc.get('text', '') for doc in job.get('documents', []))
        job['proposal_evidence'] = proposal_evidence(text)
        state['jobs'].append(job)
        warnings.extend(f"NTEN {job['title']}: {flag}" for flag in flags)
    state['warnings'] = warnings
    save()
    return state['jobs'], warnings, False
