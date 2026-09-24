"""Free screening of listing metadata. Full job vetting belongs to stage two."""
import base64
import json
import math
import re
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup


def clean(value):
    return ' '.join(str(value or '').split())


def email_html(payload):
    if payload.get('mimeType') == 'text/html' and payload.get('body', {}).get('data'):
        return base64.urlsafe_b64decode(payload['body']['data'] + '==').decode('utf-8', errors='replace')
    return '\n'.join(filter(None, (email_html(part) for part in payload.get('parts', []))))


def idealist_url(href):
    decoded = unquote(href or '')
    match = re.search(r'(?:www\.)?idealist\.org/en/(?:nonprofit-job|consultant-job|business-job|government-job|job)/[a-f0-9]{32}[^\s?#<>]*', decoded)
    return 'https://www.' + match[0].removeprefix('www.') if match else ''


def parse_idealist_html(html):
    """Job-card paragraph boundaries preserve employer, pay, and location for free."""
    soup = BeautifulSoup(html, 'html.parser')
    jobs = {}
    for link in soup.select('a[href]'):
        url = idealist_url(link['href'])
        if not url:
            continue
        container = link.find_parent('p')
        if not container:
            continue
        urls = {idealist_url(a['href']) for a in container.select('a[href]') if idealist_url(a['href'])}
        if urls != {url}:
            continue
        lines = [clean(s) for s in container.stripped_strings if clean(s)]
        title = clean(link.get_text(' ', strip=True))
        if not title or title not in lines:
            continue
        remaining = lines[lines.index(title) + 1:]
        if not remaining:
            continue
        company = remaining[0]
        pay = next((line for line in remaining[1:] if re.search(r'\$|\b(?:USD|CAD|EUR|GBP)\b', line)), 'Not listed')
        location = clean(' '.join(line for line in remaining[1:] if line != pay)) or 'Not specified'
        jobs[url] = {'title': title, 'company': company, 'url': url, 'salary': pay,
                     'location': location, 'employment_type': 'contract' if '/consultant-job/' in url else 'part-time' if re.search(r'part[- ]time', title, re.I) else '',
                     'description': '', 'source': 'Idealist (Gmail alert)', 'metadata_source': 'Email listing'}
    expected = sum(int(n) for n in re.findall(r'(\d+) new results? found for this search', soup.get_text(' ', strip=True)))
    # A repeated listing in several saved searches is still one job.
    link_count = sum(1 for link in soup.select('a[href]') if idealist_url(link['href']))
    complete = bool(jobs) and (not expected or link_count == expected) and len(jobs) == len({idealist_url(a['href']) for a in soup.select('a[href]') if idealist_url(a['href'])})
    return list(jobs.values()), complete


def parse_wellfound_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    jobs = []
    for link in soup.select('a[href]'):
        if link.get_text(' ', strip=True).lower() != 'learn more':
            continue
        host = urlsplit(link['href']).hostname
        if host not in {'links.wellfound.com', 'wellfound.com', 'www.wellfound.com'}:
            continue
        for table in link.find_parents('table'):
            lines = [clean(s) for s in table.stripped_strings if clean(s)]
            employer_index = next((i for i, s in enumerate(lines) if 'Employees' in s), None)
            more = [a for a in table.select('a[href]') if a.get_text(' ', strip=True).lower() == 'learn more']
            if employer_index is None or len(more) != 1 or not lines:
                continue
            # Templates can put '/ 11-50 Employees' in its own span or after the name.
            company = clean(lines[employer_index].split('/')[0])
            if not company and employer_index > 1:
                company = lines[employer_index - 1]
            if not company or company == lines[0]:
                continue
            details = clean(' '.join(lines[employer_index + 1:]))
            segments = [clean(s) for s in details.split('|') if clean(s)]
            salary = next((s for s in segments if re.search(r'\$|\bUSD\b', s)), 'Not listed')
            # Location lies immediately before the experience field in both sampled templates.
            experience = next((i for i, s in enumerate(segments) if re.search(r'years? of exp', s, re.I)), None)
            location = segments[experience - 1] if experience and segments[experience - 1] != salary else 'Not specified'
            jobs.append({'title': lines[0], 'company': company, 'url': link['href'],
                         'salary': salary, 'location': location,
                         'employment_type': 'part-time' if re.search(r'part[- ]time', details, re.I) else 'contract' if re.search(r'\bcontract\b', details, re.I) else '',
                         'description': '', 'source': 'Wellfound (Gmail alert)', 'metadata_source': 'Email listing'})
            break
    expected = re.search(r'found\s+(\d+)\s+new jobs', soup.get_text(' ', strip=True))
    return jobs, bool(expected and len(jobs) == int(expected[1]))


def posted_pay(value):
    """Return upper offered pay, period, currency, and whether an upper bound exists."""
    text = clean(value)
    try:
        structured = value if isinstance(value, dict) else json.loads(text)
    except (ValueError, TypeError):
        structured = None
    if isinstance(structured, dict):
        amount = structured.get('value', structured)
        if isinstance(amount, dict):
            raw = numeric_amount(amount.get('maxValue', amount.get('value', amount.get('minValue'))))
            period = str(amount.get('unitText', '')).lower()
            period = 'hour' if period in {'hour', 'hourly'} else 'year' if period in {'year', 'annual'} else ''
            if raw is not None:
                return raw, period, clean(structured.get('currency', 'unknown')).upper(), 'maxValue' in amount or 'value' in amount
        return None, '', '', False
    text = re.split(r'[|•]', text)[0]
    foreign = re.search(r'\b(CAD|AUD|EUR|GBP|INR)\b|C\$|A\$|€|£', text, re.I)
    currency = foreign[0].upper() if foreign else 'USD' if re.search(r'\$|\bUSD\b', text, re.I) else 'unknown'
    amounts = re.findall(r'(\d[\d,]*(?:\.\d+)?)\s*([kK]?)', text)
    if not amounts:
        return None, '', currency, False
    values = [float(n.replace(',', '')) * (1000 if k else 1) for n, k in amounts]
    if any(k for _, k in amounts):
        values = [v * 1000 if v < 1000 else v for v in values]
    upper = max(values)
    period = 'hour' if re.search(r'/\s*(?:hr|hour)|\b(?:hourly|per hour)\b', text, re.I) else 'year' if upper >= 10000 or re.search(r'year|annual|annum', text, re.I) else ''
    has_upper = not re.search(r'\bat least\b|\bfrom\b|\+\s*$', text, re.I)
    return upper, period, currency, bool(has_upper)


def numeric_amount(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(',', ''))
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError):
        return None


def structured_value(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def country_name(place):
    address = place.get('address') if isinstance(place, dict) else None
    country = address.get('addressCountry') if isinstance(address, dict) else None
    return clean(country.get('name') if isinstance(country, dict) else country).upper()


def display_location(value):
    obj = structured_value(value)
    if not obj:
        return clean(value) or 'Not specified'
    places = obj.get('jobLocation') or []
    places = places if isinstance(places, list) else [places]
    labels = []
    for place in places:
        address = place.get('address', {}) if isinstance(place, dict) else {}
        if isinstance(address, dict):
            label = ', '.join(filter(None, [clean(address.get('addressLocality')),
                                           clean(address.get('addressRegion')), country_name(place)]))
            if label and label not in labels:
                labels.append(label)
    restrictions = obj.get('applicantLocationRequirements') or []
    restrictions = restrictions if isinstance(restrictions, list) else [restrictions]
    applicants = ', '.join(clean(x.get('name')) for x in restrictions if isinstance(x, dict) and x.get('name'))
    if 'TELECOMMUTE' in str(obj.get('jobLocationType', '')).upper():
        labels.append('Remote' + (f' ({applicants} applicants)' if applicants else ''))
    elif applicants:
        labels.append(f'Applicants: {applicants}')
    if obj.get('listingLocation') and obj['listingLocation'] not in labels:
        labels.append(clean(obj['listingLocation']))
    return ' · '.join(labels) or 'Location needs verification'


def display_pay(value):
    obj = structured_value(value)
    if not obj:
        return clean(value) or 'Not listed'
    amount = obj.get('value', obj)
    if not isinstance(amount, dict):
        return 'Pay needs verification'
    upper, period, currency, bounded = posted_pay(obj)
    if upper is None:
        return 'Pay needs verification'
    lower = numeric_amount(amount.get('minValue'))
    prefix = '$' if currency == 'USD' else currency + ' '
    number = lambda n: f'{n:,.2f}'.rstrip('0').rstrip('.')
    pay = f'{prefix}{number(upper)}'
    if lower is not None and lower < upper:
        pay = f'{prefix}{number(lower)}–{pay}'
    if not bounded:
        pay = 'From ' + pay
    return pay + ({'year':'/yr', 'hour':'/hr'}.get(period, ' (period not specified)'))


def screen_metadata(job, annual_floor=90000, contract_floor=65):
    flags = []
    checks = {'title': 'Plausible first-pass match'}
    location = json.dumps(job['location']) if isinstance(job.get('location'), dict) else clean(job.get('location'))
    lower = location.lower()
    remote = bool(re.search(r'\bremote\b|telecommute|telecommuting|telecommute', lower))
    office = bool(re.search(r'\bhybrid\b|\bon[ -]?site\b|\bin[ -]person\b', lower))
    alternative = bool(re.search(r'(?:onsite|on-site|hybrid)\s+(?:or|/)\s+remote|remote\s+(?:or|/)\s+(?:onsite|on-site|hybrid)', lower))
    negated_office = bool(re.search(r'not (?:hybrid|on[ -]?site)|no (?:hybrid|on[ -]?site) requirement|hybrid optional', lower))
    if (office and not alternative and not negated_office) or re.search(r'\bnot remote\b|\bno remote\b', lower):
        return {'stage': 'first-pass', 'decision': 'skip', 'reason': 'Listing explicitly requires hybrid/on-site work', 'flags': [], 'checks': checks}
    checks['remote'] = 'Listed as remote' if remote and not office else 'Needs verification'
    if checks['remote'] == 'Needs verification':
        flags.append('Verify fully remote eligibility; listing is missing or conflicting')
    # Only explicit country restrictions disqualify. A city alone does not prove on-site work.
    foreign_only = re.search(r'(?:remote\s*(?:only)?\s*[•,–—:-]?\s*|anywhere in\s+)(canada|united kingdom|uk|india|australia|germany|europe)(?:\s*\(remote\))?(?:\s+only)?$', lower)
    try:
        structured_location = json.loads(location)
    except (ValueError, TypeError):
        structured_location = {}
    if isinstance(structured_location, dict):
        restrictions = structured_location.get('applicantLocationRequirements', [])
        if isinstance(restrictions, dict):
            restrictions = [restrictions]
        names = {clean(item.get('name')).lower() for item in restrictions if isinstance(item, dict)}
        foreign_countries = {'canada', 'united kingdom', 'uk', 'india', 'australia', 'germany', 'france', 'europe'}
        foreign_only = foreign_only or bool(names and names.issubset(foreign_countries))
        places = structured_location.get('jobLocation') or []
        places = places if isinstance(places, list) else [places]
        countries = {country_name(place) for place in places} - {''}
        # A foreign job location with no remote signal is incompatible. A foreign
        # employer's HQ with explicit remote eligibility is not a disqualifier.
        if countries and not countries & {'US', 'USA', 'UNITED STATES', 'UNITED STATES OF AMERICA'} and not remote:
            return {'stage': 'first-pass', 'decision': 'skip', 'reason': 'Structured job location is outside the US with no remote eligibility', 'flags': [], 'checks': checks}
    if foreign_only:
        return {'stage': 'first-pass', 'decision': 'skip', 'reason': 'Listing restricts remote work outside the US', 'flags': [], 'checks': checks}
    checks['location'] = location or 'Not specified'
    if not re.search(r'\bunited states\b|\bu\.?s\.?a?\b|\bworldwide\b|\beverywhere\b', lower):
        flags.append('Verify geographic eligibility for a northeastern US applicant')
    pay = json.dumps(job['salary']) if isinstance(job.get('salary'), dict) else clean(job.get('salary')) or 'Not listed'
    upper, period, currency, bounded = posted_pay(pay)
    employment = clean(job.get('employment_type')) + ' ' + job.get('title', '')
    part_time = bool(re.search(r'part[- _]?time', employment, re.I))
    contract = '/consultant-job/' in job.get('url', '') or bool(re.search(r'\bcontract(?:or)?\b|\bfreelance\b|\b1099\b', employment, re.I))
    checks['salary'] = pay
    reason = None
    if upper is None or not period or currency != 'USD':
        flags.append('Verify pay, currency, and pay period')
    elif period == 'hour' and contract:
        if bounded and upper < contract_floor:
            reason = f'Contract rate tops out below ${contract_floor}/hour'
    elif part_time:
        flags.append('Part-time pay exception; verify hours and total compensation')
    elif period == 'year' and not contract:
        if bounded and upper < annual_floor:
            reason = f'Listed salary tops out below ${annual_floor:,.0f}/year'
    elif period == 'hour':
        if bounded and upper < contract_floor:
            reason = f'Hourly rate tops out below ${contract_floor}/hour; no part-time exception stated'
        flags.append(f'Hourly role: verify employment type; contracts require ${contract_floor}/hour')
        if bounded and upper < contract_floor:
            flags.append('Rate below contract floor; may qualify only as part-time employment')
    else:
        flags.append(f'Contract pay needs conversion to verify ${contract_floor}/hour floor')
    if upper is not None and not bounded:
        flags.append('Pay upper bound not stated; verify the attainable offer')
    if not job.get('company') or job['company'].lower() in {'unknown', 'see posting'}:
        flags.append('Employer name needs verification')
    return {'stage': 'first-pass', 'decision': 'skip' if reason else 'pass',
            'reason': reason or 'Title and available listing metadata passed the first screen',
            'flags': flags, 'checks': checks}


def extract_listing_metadata(html, expected_title='', page_url=''):
    """Read factual JSON-LD fields and JD text for free deterministic screening."""
    from job_quality import titles_match
    def objects(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from objects(child)
        elif isinstance(value, list):
            for child in value:
                yield from objects(child)
    candidates = []
    for script in BeautifulSoup(html, 'html.parser').select('script[type="application/ld+json"]'):
        try:
            for obj in objects(json.loads(script.get_text())):
                kind = obj.get('@type')
                if kind == 'JobPosting' or isinstance(kind, list) and 'JobPosting' in kind:
                    candidates.append(obj)
        except (ValueError, TypeError):
            continue
    matches = [obj for obj in candidates if titles_match(expected_title, obj.get('title'))]
    obj = matches[0] if len(matches) == 1 else candidates[0] if len(candidates) == 1 else None
    if not obj:
        return {}
    org = obj.get('hiringOrganization') or {}
    location = {k: obj[k] for k in ('jobLocation', 'jobLocationType', 'applicantLocationRequirements') if k in obj}
    result = {'title': clean(obj.get('title')), 'company': clean(org.get('name')) if isinstance(org, dict) else '',
              'employment_type': clean(obj.get('employmentType')),
              'metadata_source': 'Listing structured data'}
    if location:
        result['location'] = json.dumps(location, ensure_ascii=False)
    if obj.get('baseSalary'):
        result['salary'] = json.dumps(obj['baseSalary'], ensure_ascii=False)
    if isinstance(obj.get('description'), str) and obj['description'].strip():
        result['description'] = BeautifulSoup(obj['description'], 'html.parser').get_text(' ', strip=True)
        result['description_verified'] = True
    # These sources truncate JSON-LD or omit a separate benefits/eligibility section.
    # Use only the matching posting's known container, never related-job page text.
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.select_one('main h1')
    host = urlsplit(page_url).hostname or ''
    if heading and titles_match(obj.get('title'), heading.get_text(' ', strip=True)):
        sections = soup.select('article .prose') if host == 'remoteimpact.org' else []
        if host in {'idealist.org', 'www.idealist.org'}:
            sections = soup.select('main')
        if sections:
            visible = ' '.join(section.get_text(' ', strip=True) for section in sections)
            result['description'] = result.get('description', '') + '\n' + visible
            result['description_verified'] = True
    return {k: v for k, v in result.items() if v}


def explicit_description_exclusions(text, location=''):
    """Narrow requirements, not the legacy scorer's broad warning keywords."""
    hits = []
    # A negation/optional statement never supplies evidence of a requirement.
    for sentence in re.split(r'(?<=[.!?;])\s+|\n+', text or ''):
        sentence = clean(sentence).lower()
        # Split independent clauses so 'no sponsorship, but travel is required'
        # can still provide positive travel evidence.
        for clause in re.split(r'\bbut\b|\bhowever\b', sentence):
            def positive(pattern):
                for match in re.finditer(pattern, clause):
                    before = clause[max(0, match.start() - 60):match.start()]
                    after = clause[match.end():match.end() + 45]
                    if re.search(r'\b(?:no|not|never|without)\b', match[0]):
                        continue
                    if re.search(r'\b(?:no|not|never|without)\b[^,;:.]{0,50}$', before):
                        continue
                    if re.match(r'\s*(?:is |are |will be )?(?:not required|optional|not necessary|not expected)', after):
                        continue
                    return True
                return False
            office = (r'\bhybrid (?:work )?(?:schedule|role|position|arrangement)\b',
                      r'\b(?:fully |full[- ]time[, ]+)?on[- ]?site (?:role|position|work)\b',
                      r'\b(?:role|position) (?:is|will be) (?:fully )?on[- ]?site\b',
                      r'\b(?:must|required to|expected to) (?:work|be|come) (?:from |in |into |at )?(?:our |the )?office\b',
                      r'\b\d+ days? (?:a|per) week (?:in|at) (?:the |our )?office\b',
                      r'\bin[- ]office\b.{0,40}\b(?:days? (?:a|per) week|monday|tuesday|wednesday|thursday|friday)\b',
                      r'\bmonday\s*(?:through|to|[-–])\s*friday\b.{0,40}\b(?:office|on[- ]?site)\b')
            optional_remote = re.search(r'\b(?:onsite|on-site|hybrid)\s+(?:or|/)\s+remote|\bremote\s+(?:or|/)\s+(?:onsite|on-site|hybrid)', clause)
            if not optional_remote and any(positive(p) for p in office):
                hits.append('Explicit hybrid/on-site work requirement')
            if positive(r'\b(?:requires?|required|must|expected to)\b.{0,35}\b(?:regular|extensive|frequent) travel\b') or positive(r'\b(?:regular|extensive|frequent) travel\b.{0,25}\b(?:required|expected)\b'):
                hits.append('Regular/extensive travel required')
            for match in re.finditer(r'\btravel\b[^.;]{0,35}?(\d+(?:\.\d+)?)\s*(?:[-–]\s*(\d+(?:\.\d+)?)\s*)?%', clause):
                if max(float(match[1]), float(match[2] or match[1])) > 10 and positive(re.escape(match[0])):
                    hits.append('Listed travel exceeds 10%')
            for match in re.finditer(r'\b(\d+(?:\.\d+)?)\s*(?:[-–]\s*(\d+(?:\.\d+)?)\s*)?%\s+(?:required\s+)?travel\b', clause):
                if max(float(match[1]), float(match[2] or match[1])) > 10 and positive(re.escape(match[0])):
                    hits.append('Listed travel exceeds 10%')
            if positive(r'\bcalifornia residents only\b') or positive(r'\bunable to (?:offer employment|hire)\b.{0,45}\bnon[- ]california residents\b'):
                hits.append('California residency required')
            if positive(r'\blocal candidates only\b') and re.search(r'\b(?:california|san francisco|bay area|san diego|los angeles)\b', display_location(location).lower() + ' ' + clause):
                hits.append('Local candidates required')
            # Ownership of customer revenue, rather than a neighboring team's name.
            if positive(r'\b(?:manage[sd]?|own[sd]?|responsible for)\b.{0,60}\b(?:(?:account|customer|client) renewals?|upselling|sales quotas?)\b') or positive(r'\baccounts?\b.{0,30}\bthrough (?:to )?renewal\b'):
                hits.append('Role owns customer accounts/renewals')
            elif positive(r'\bown\b.{0,35}\brelationship\b.{0,65}\b(?:client|customer|utility) accounts\b'):
                hits.append('Role owns customer accounts/renewals')
    return list(dict.fromkeys(hits))
