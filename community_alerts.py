"""Native community notifications only; never fetch forum pages or guess clients.

Unknown templates are retained for review. Real account alert validation is still
needed after subscriptions are enabled; a sender name is not a hiring company.
"""
import re
from urllib.parse import urlsplit, urlunsplit
from bs4 import BeautifulSoup
from additional_sources import normalize

SOURCES = {
    'Make community': ('community.make.com', 'https://community.make.com/c/hire-a-pro/hire-help/74'),
    'Airtable community': ('community.airtable.com', 'https://community.airtable.com/jobs-board-16'),
    'n8n community': ('community.n8n.io', 'https://community.n8n.io/c/jobs/13'),
}


def parse_alert(html, source, subject='', posted_date=''):
    if re.match(r'\s*(?:re|fwd?):', subject, re.I):
        return [], True  # Replies do not become fresh opportunities.
    host = SOURCES[source][0]
    soup = BeautifulSoup(html, 'html.parser')
    links = {}
    for link in soup.select('a[href]'):
        parts = urlsplit(link['href'])
        if parts.hostname != host or parts.scheme != 'https':
            continue
        path = parts.path
        if host in {'community.make.com', 'community.n8n.io'}:
            match = re.fullmatch(r'/t/[^/]+/\d+(?:/(\d+))?/?', path)
            if not match:
                continue
            if match[1] and match[1] != '1':
                return [], True
            path = re.sub(r'/1/?$', '', path) if match[1] else path.rstrip('/')
        elif not re.search(r'/jobs-board-16/[^/]+-\d+/?$', path):
            continue
        url = urlunsplit(('https', host, path, '', ''))
        label = link.get_text(' ', strip=True)
        if url not in links or len(label) > len(links[url]):
            links[url] = label
    if len(links) != 1:
        return [], False  # Digests/multiple topics need their actual template.
    url, label = next(iter(links.items()))
    title = re.sub(r'^\s*(?:\[[^]]+\]\s*)+', '', subject).strip() or label
    if re.search(r'\b(?:for hire|available for work|offering (?:my |our )?services)\b', title, re.I):
        return [], True
    body_node = soup.select_one('.post, .cooked, .content, article')
    body = (body_node or soup).get_text('\n', strip=True)
    if not re.search(r'\b(?:hiring|looking for|seeking|need|rfp|help wanted)\b', title, re.I):
        return [], False
    fields = {}
    for key, pattern in {'company': r'(?:company|organization|client)', 'salary': r'(?:budget|pay|rate|compensation)',
                         'location': r'(?:location|eligibility)', 'employment_type': r'(?:engagement|employment|job) type'}.items():
        match = re.search(rf'^\s*{pattern}\s*:\s*(?:\n\s*)?([^\n]+)', body, re.I | re.M)
        if match:
            fields[key] = match[1].strip()[:500]
    job = normalize(dict(source=source, title=title, url=url, description=body[:30000],
                         posted_date=posted_date, **fields))
    job['source_flags'] = ['Email notification only; verify original topic date, client, scope and availability in Claude']
    return [job], True
