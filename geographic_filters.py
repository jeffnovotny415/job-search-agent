"""Local country/currency lookups and conservative New Jersey eligibility checks."""
import json
import re

import pycountry


US_NAMES = {'US', 'USA', 'UNITED STATES', 'UNITED STATES OF AMERICA', 'U.S.', 'U.S.A.'}
UNKNOWN = {'', 'UNKNOWN', 'NOT LISTED', 'NOT SPECIFIED', 'N/A'}
COUNTRY_NAMES = {name for country in pycountry.countries if country.alpha_2 != 'US'
                 for name in (country.name, getattr(country, 'official_name', ''),
                              getattr(country, 'common_name', '')) if name}
# Georgia is also a US state; only an explicit country field can disambiguate it.
COUNTRY_NAMES.discard('Georgia')
COUNTRY_NAMES.update({'UK', 'U.K.', 'England', 'Scotland', 'Wales', 'South Korea',
                      'North Korea', 'Russia', 'Vietnam', 'Taiwan', 'UAE',
                      'Europe', 'Asia', 'EMEA', 'LATAM', 'APAC'})
COUNTRY_PATTERN = re.compile(r'(?<!\w)(?:' + '|'.join(re.escape(n) for n in sorted(COUNTRY_NAMES, key=len, reverse=True)) + r')(?!\w)', re.I)
CURRENCY_PATTERN = re.compile(r'\b(?:' + '|'.join(c.alpha_3 for c in pycountry.currencies if c.alpha_3 != 'USD') + r')\b')
STATE_NAMES = {s.name.lower(): s.code[3:] for s in pycountry.subdivisions.get(country_code='US')}
STATE_CODES = set(STATE_NAMES.values())
STATE_PATTERN = re.compile(r'(?:(?i:' + '|'.join(re.escape(n) for n in sorted(STATE_NAMES, key=len, reverse=True)) + r')|' + '|'.join(sorted(STATE_CODES)) + r')(?![\w])')


def object_value(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def values(value):
    return value if isinstance(value, list) else [value] if value else []


def foreign_currency(value):
    obj = object_value(value)
    if obj:
        currency = str(obj.get('currency') or '').strip().upper()
        return currency if currency not in UNKNOWN | {'USD', 'US DOLLAR', 'US DOLLARS'} else None
    text = str(value or '')
    match = CURRENCY_PATTERN.search(text)
    if match:
        return match[0]
    match = re.search(r'(?:CA|AU|NZ|HK|SG|C|A)\$|[€£₹¥₩₽₱฿₫]', text)
    if match:
        return match[0]
    # Currency codes may be lower-case when next to an actual monetary amount.
    for code in re.findall(r'(?i)\b([a-z]{3})\s*\$?\s*\d', text):
        if code.upper() != 'USD' and pycountry.currencies.get(alpha_3=code.upper()):
            return code.upper()
    return None


def _country_in_text(text):
    for match in COUNTRY_PATTERN.finditer(text):
        if match[0].lower() in {'jersey', 'mexico'} and re.search(r'\bnew\s+$', text[:match.start()], re.I):
            continue
        if match[0].lower() == 'jersey' and re.match(r'\s+city\b', text[match.end():], re.I):
            continue
        # A country name can also be a US town, e.g. Lebanon, PA.
        if re.match(r',\s*(?:' + '|'.join(sorted(STATE_CODES)) + r')\b', text[match.end():]):
            continue
        return match[0]
    return None


def foreign_location(value):
    """Job location/applicant eligibility only; never scan employer names or JD prose."""
    obj = object_value(value)
    if not obj:
        return _country_in_text(str(value or ''))
    for place in values(obj.get('jobLocation')):
        address = place.get('address', {}) if isinstance(place, dict) else {}
        if isinstance(address, dict):
            country = address.get('addressCountry') or ''
            if isinstance(country, dict):
                country = country.get('name') or country.get('identifier') or ''
            country = str(country).strip()
            if country.upper() not in UNKNOWN | US_NAMES:
                return country
            # Some boards put the whole address in addressLocality and omit country.
            if not country:
                country = _country_in_text(', '.join(str(address.get(k) or '') for k in ('addressLocality', 'addressRegion')))
                if country:
                    return country
        elif isinstance(address, str):
            country = _country_in_text(address)
            if country:
                return country
    for restriction in values(obj.get('applicantLocationRequirements')):
        if not isinstance(restriction, dict):
            continue
        name = str(restriction.get('name') or '').strip()
        if restriction.get('@type') == 'State':
            continue
        if restriction.get('@type') == 'Country' and name.upper() not in US_NAMES | UNKNOWN | {'EARTH', 'WORLDWIDE', 'GLOBAL'}:
            return name
        country = _country_in_text(name)
        if country:
            return country
    return foreign_location(obj.get('listingLocation', ''))


def _state_list(text):
    """Consume only a contiguous list, stopping before unrelated JD content."""
    text = text.lstrip(' \t\r\n:*-•')
    states = set()
    while True:
        match = STATE_PATTERN.match(text)
        if not match:
            return states, text
        states.add(STATE_NAMES.get(match[0].lower(), match[0]))
        text = text[match.end():]
        text = re.sub(r'^[\s,;/&•*-]+', '', text)
        text = re.sub(r'^(?:and|or)\b\s*', '', text)


def state_eligibility(text, location='', home_state='NJ'):
    """Return a rejection reason or verification flag; absence of NJ alone proves nothing."""
    text = ' '.join((text or '').split())
    states = set()
    restrictions = values(object_value(location).get('applicantLocationRequirements'))
    only_states = bool(restrictions) and all(isinstance(r, dict) and r.get('@type') == 'State' for r in restrictions)
    if only_states:
        for restriction in restrictions:
            name = str(restriction.get('name') or '')
            parsed, remainder = _state_list(name)
            if not parsed or remainder.strip():
                return None, 'Verify state eligibility; structured state list could not be fully read'
            states.update(parsed)
        if home_state not in states:
            return 'Eligible states exclude New Jersey: ' + ', '.join(sorted(states)), None

    markers = re.compile(
        r'\b(?:remote(?: work)?(?: is available)?(?: only)? in (?:the following|these) states|'
        r'(?:eligible|approved|excluded) (?:remote )?states|'
        r'(?:performed|work) (?:fully )?remotely by candidates (?:located|residing|based) in|'
        r'(?:must|required to) (?:currently )?(?:reside|live|be based|be located) in|'
        r'(?:cannot|can not|do not|are unable to) (?:currently )?(?:hire|employ)(?: candidates| employees| applicants)? in|'
        r'(?:remote work|this (?:role|position)) is not available in)\b', re.I)
    flag = None
    for match in markers.finditer(text or ''):
        before = text[max(0, match.start() - 90):match.start()]
        if re.search(r'\b(?:not|never)\b[^.;\n]{0,35}$', before, re.I):
            continue
        if re.search(r'\b(?:clients?|customers?|patients?|participants?|beneficiaries)\b[^.;\n]{0,35}$', before, re.I):
            continue
        tail = text[match.end():match.end() + 1600]
        tail = re.sub(r'^\s*(?:(?:one of|any of)\s+)?(?:the )?(?:following )?states?\s*', '', tail, flags=re.I)
        listed, remainder = _state_list(tail)
        if not listed:
            if 'states' in match[0].lower() or re.match(r'\s*(?:one of |the |following )*states\b', text[match.end():], re.I):
                flag = 'Verify state eligibility; the eligible-state list is missing or ambiguous'
            continue
        incomplete = (len(tail) == 1600 and not remainder or
                      re.match(r'\s*(?:\.{2,}|…|including\b|among others\b|other\b|additional\b|be willing\b|except\b)', remainder, re.I) or
                      re.match(r'(?:' + '|'.join(sorted(STATE_CODES)) + r')[A-Z][a-z]', remainder) or
                      re.match(r'[A-Za-z]+,', remainder))
        negative = bool(re.search(r'cannot|can not|do not|unable|not available|excluded', match[0], re.I))
        if incomplete:
            flag = 'Verify state eligibility; the state restriction may be incomplete'
        elif (negative and home_state in listed) or (not negative and home_state not in listed):
            return ('New Jersey is explicitly excluded' if negative else
                    'Eligible states exclude New Jersey: ' + ', '.join(sorted(listed))), None
    return None, flag
