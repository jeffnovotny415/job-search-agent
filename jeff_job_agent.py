#!/usr/bin/env python3
"""
Jeff's Job Search Agent
=======================
Crawls target job sites, scores each role against Jeff's profile using Claude,
creates Trello cards for strong matches, and scans Gmail to move cards
between pipeline stages automatically.

Run manually:         python jeff_job_agent.py
Run crawl only:       python jeff_job_agent.py --crawl
Run Gmail scan only:  python jeff_job_agent.py --gmail
Schedule with cron:   0 8 * * * /usr/bin/python3 /path/to/jeff_job_agent.py

First-time Gmail setup: python jeff_job_agent.py --gmail-setup
"""

import os
import re
import json
import time
import hashlib
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import anthropic

# Gmail imports
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import base64

# ─────────────────────────────────────────────
# CONFIGURATION
# Credentials are loaded from environment variables.
# Copy .env.example to .env and fill in your values.
# Never commit .env to version control.
# ─────────────────────────────────────────────

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional — env vars can be set directly in shell

CONFIG = {
    # Anthropic — get from console.anthropic.com
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),

    # Trello — get from trello.com/power-ups/admin
    "trello_api_key":    os.getenv("TRELLO_API_KEY", ""),
    "trello_token":      os.getenv("TRELLO_TOKEN", ""),
    "trello_board_id":   os.getenv("TRELLO_BOARD_ID", "Wyqa1R7P"),

    # Trello list names — must match exactly what's on your board
    "trello_lists": {
        "watching":    "Watching",
        "applied":     "Applied",
        "reach":       "Reach",
        "interview":   "Interview",
        "closed":      "Closed",
        "rejected":    "Rejected",
    },

    # Gmail OAuth — download credentials.json from Google Cloud Console
    # See SETUP GUIDE at the bottom of this file
    "gmail_credentials_file": "credentials.json",
    "gmail_token_file":       "gmail_token.json",

    # Score threshold — only create Trello cards for roles at or above this
    "min_score_for_card": 60,

    # How many days back to scan Gmail for status updates
    "gmail_lookback_days": 14,

    # Seen jobs cache — prevents duplicate cards across runs
    "seen_jobs_file": "seen_jobs.json",

    # Log file
    "log_file": "job_agent.log",
}

# ─────────────────────────────────────────────
# SCORING PROFILE — loaded from profile.txt
# Copy profile.example.txt to profile.txt and
# customize for your own search.
# profile.txt is gitignored — never committed.
# ─────────────────────────────────────────────

_profile_path = Path(__file__).parent / "profile.txt"
if _profile_path.exists():
    JEFF_PROFILE = _profile_path.read_text(encoding="utf-8").strip()
else:
    raise FileNotFoundError(
        "profile.txt not found. Copy profile.example.txt to profile.txt "
        "and fill in your personal scoring criteria before running."
    )

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["log_file"]),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# PRE-FILTER
# Cheap keyword check before calling Claude.
# Catches obvious hard disqualifiers and weak
# title matches without spending API tokens.
# Saves ~30-40% of Claude API calls on a
# typical run.
# ─────────────────────────────────────────────

# Titles containing these words are almost
# never a fit — skip Claude entirely
TITLE_BLOCKLIST = [
    "sales", "account executive", "account manager", "business development",
    "revenue", "customer success", "renewals", "marketing", "recruiter",
    "recruiting", "talent acquisition", "finance", "accounting", "payroll",
    "legal counsel", "attorney", "lawyer", "nurse", "physician", "clinical",
    "ux designer", "graphic designer", "data scientist",
    "data engineer", "machine learning engineer", "software engineer",
    "software developer", "frontend engineer", "backend engineer",
    "full stack", "fullstack", "devops engineer", "site reliability",
    "security engineer", "penetration tester", "blockchain", "crypto",
    "web3", "defi", "nft", "content writer", "copywriter", "social media",
    "seo specialist", "paid media", "field technician", "field service",
    "warehouse", "driver", "logistics coordinator",
]

# Titles containing at least one of these suggest
# a plausible fit — worth sending to Claude
TITLE_ALLOWLIST = [
    "it", "information technology", "technical", "technology", "tech",
    "operations", "ops", "project manager", "program manager", "pm",
    "implementation", "systems", "saas", "platform", "product operations",
    "workflow", "automation", "ai", "artificial intelligence", "digital",
    "infrastructure", "network", "sysadmin", "system admin", "helpdesk",
    "help desk", "service desk", "workplace", "internal tools", "devops",
    "release", "delivery", "integration", "enterprise", "business systems",
    "it manager", "it director", "it lead", "data operations",
    # Expanded title variations (June 2026)
    "solutions architect", "program designer", "learning experience",
    "knowledge manager", "systems and tools", "head of operations",
    "director of operations", "technology fellow", "ai implementation",
    "enablement manager", "platform operations", "community operations",
    "workflow automation", "instructional design", "enablement lead",
    "ai implementation lead", "learning design", "program operations",
]

# If the job description contains enough of these,
# it's probably not worth Claude's attention even
# if the title slipped through
DESCRIPTION_BLOCKLIST_THRESHOLD = 3  # how many hits before we skip
DESCRIPTION_BLOCKLIST = [
    "quota", "commission", "ote ", "on-target earnings",
    "cold calling", "cold call", "pipeline generation",
    "revenue growth", "closing deals", "hunting new business",
    "territory management", "upsell", "cross-sell",
    "customer renewals", "churn reduction",
    "blockchain", "cryptocurrency", "web3", "defi", "nft",
    "must be onsite", "required to be in office",
    "5 days a week in office", "four days in office",
    "relocation required",
]


def pre_filter(job):
    """
    Fast keyword pre-filter. Returns (should_score, reason).
    True = send to Claude. False = skip, save the API call.

    Logic:
    1. Block if title matches a hard blocklist term
    2. Pass if title matches an allowlist term
    3. If title is ambiguous, check description for disqualifiers
    4. Default to sending to Claude when uncertain — better to
       spend $0.003 than miss a good role
    """
    title = job.get("title", "").lower()
    description = job.get("description", "").lower()

    # Hard block on title — these are almost never a fit
    for term in TITLE_BLOCKLIST:
        if term in title:
            return False, f"title blocklist: '{term}'"

    # Strong signal in title — send to Claude
    for term in TITLE_ALLOWLIST:
        if term in title:
            # Still do a quick description check for hard disqualifiers
            hits = sum(1 for term in DESCRIPTION_BLOCKLIST if term in description)
            if hits >= DESCRIPTION_BLOCKLIST_THRESHOLD:
                return False, f"description has {hits} disqualifier signals"
            return True, "title allowlist match"

    # Ambiguous title — check description for positive signals
    positive_signals = [
        "remote", "project management", "operations", "saas", "technical",
        "it ", " it,", "systems", "implementation", "workflow", "automation",
        "infrastructure", "platform", "program management",
    ]
    desc_hits = sum(1 for s in positive_signals if s in description)
    if desc_hits >= 2:
        return True, f"ambiguous title but {desc_hits} positive description signals"

    # Default: send to Claude when uncertain
    # A missed good role costs more than a wasted API call
    return True, "uncertain — defaulting to Claude"


# ─────────────────────────────────────────────
# SEEN JOBS CACHE
# Prevents duplicate Trello cards across runs
# ─────────────────────────────────────────────

def load_seen_jobs():
    path = Path(CONFIG["seen_jobs_file"])
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}

def save_seen_jobs(seen):
    with open(CONFIG["seen_jobs_file"], "w") as f:
        json.dump(seen, f, indent=2)

def safe_parse_json_list(raw):
    """
    Robustly parse a JSON array from Claude output.
    Handles markdown fences, special characters, and truncated responses.
    Returns a list or empty list on failure.
    """
    if not raw:
        return []
    # Strip markdown fences
    raw = re.sub(r'^```[a-zA-Z]*\n?', '', raw.strip())
    raw = re.sub(r'\n?```$', '', raw)
    raw = raw.strip()

    # Try direct parse first
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        pass

    # Try to extract just the array portion
    try:
        start = raw.index('[')
        end = raw.rindex(']') + 1
        result = json.loads(raw[start:end])
        return result if isinstance(result, list) else []
    except (ValueError, json.JSONDecodeError):
        pass

    # Try line-by-line object extraction as last resort
    objects = []
    for match in re.finditer(r'\{[^{}]+\}', raw, re.DOTALL):
        try:
            obj = json.loads(match.group())
            if isinstance(obj, dict) and 'title' in obj:
                objects.append(obj)
        except json.JSONDecodeError:
            continue
    return objects


def job_fingerprint(company, title, url=""):
    """Stable hash so the same job isn't added twice even if URL changes slightly."""
    raw = f"{company.lower().strip()}|{title.lower().strip()}"
    return hashlib.md5(raw.encode()).hexdigest()

# ─────────────────────────────────────────────
# TRELLO HELPERS
# ─────────────────────────────────────────────

TRELLO_BASE = "https://api.trello.com/1"

def trello_params(extra=None):
    p = {"key": CONFIG["trello_api_key"], "token": CONFIG["trello_token"]}
    if extra:
        p.update(extra)
    return p

def get_trello_lists():
    """Returns {list_name: list_id} for every list on the board."""
    url = f"{TRELLO_BASE}/boards/{CONFIG['trello_board_id']}/lists"
    r = requests.get(url, params=trello_params())
    r.raise_for_status()
    return {lst["name"]: lst["id"] for lst in r.json()}

def get_trello_cards(list_id):
    """Returns all cards in a given list."""
    url = f"{TRELLO_BASE}/lists/{list_id}/cards"
    r = requests.get(url, params=trello_params())
    r.raise_for_status()
    return r.json()

def get_all_active_cards(list_map):
    """Returns all cards NOT in Closed or Rejected."""
    closed_names = [CONFIG["trello_lists"]["closed"], CONFIG["trello_lists"]["rejected"]]
    active = []
    for name, lid in list_map.items():
        if name not in closed_names:
            active.extend(get_trello_cards(lid))
    return active

def create_trello_card(list_id, name, desc):
    """Creates a card and returns the card object."""
    url = f"{TRELLO_BASE}/cards"
    r = requests.post(url, params=trello_params({
        "idList": list_id,
        "name":   name,
        "desc":   desc,
    }))
    r.raise_for_status()
    return r.json()

def move_trello_card(card_id, list_id):
    """Moves a card to a different list."""
    url = f"{TRELLO_BASE}/cards/{card_id}"
    r = requests.put(url, params=trello_params({"idList": list_id}))
    r.raise_for_status()
    return r.json()

def add_comment_to_card(card_id, text):
    """Adds a comment to a Trello card."""
    url = f"{TRELLO_BASE}/cards/{card_id}/actions/comments"
    r = requests.post(url, params=trello_params({"text": text}))
    r.raise_for_status()

# ─────────────────────────────────────────────
# CLAUDE SCORING
# ─────────────────────────────────────────────

def score_job_with_claude(job):
    """
    Sends a job to Claude for scoring against Jeff's profile.
    Returns a dict with verdict, score, lane, etc.
    """
    client = anthropic.Anthropic(api_key=CONFIG["anthropic_api_key"])

    description = job.get('description', '').strip()
    source = job.get('source', '')
    is_thin = len(description) < 200 or 'Gmail alert' in source

    thin_note = ""
    if is_thin:
        thin_note = """
IMPORTANT: This job came from an email alert with limited description.
Score based on title and company alone. Do NOT disqualify for lack of
description — that is not a hard filter. Give benefit of the doubt on
ambiguous signals. Only disqualify if the title itself contains a hard
disqualifier (sales, crypto, onsite required, etc).
A title like "AI Operations Manager" or "Technical Project Manager" at
any company should score at least 55-70 based on title fit alone.
"""

    prompt = f"""Score this job for Jeff. Respond ONLY in valid JSON, no markdown, no preamble.
{thin_note}
{{
  "verdict": "Apply Now | Apply If Interested | Maybe | Skip",
  "score": 0-100,
  "lane": "Lane 1 IT Ops | Lane 2 TPM | Lane 3 AI Ops",
  "mission_fit": "Strong | Moderate | Thin | None",
  "disqualified": true or false,
  "disqualifier_reason": "reason if disqualified, else null",
  "why_it_fits": "2-3 sentences",
  "concerns": "2-3 sentences — include company size flag and change-management flag if applicable; if description is thin, note that full review needed",
  "cover_letter_angle": "one sentence",
  "salary_ask": "specific number or range",
  "next_step": "one specific action",
  "puzzle_fit": true or false,
  "environment_flags": ["list any: small-org, change-management-heavy, ownership-language, large-org-risk"]
}}

Job details:
Company: {job.get('company', 'Unknown')}
Title: {job.get('title', 'Unknown')}
URL: {job.get('url', 'N/A')}
Location/Remote: {job.get('location', 'Not specified')}
Salary: {job.get('salary', 'Not listed')}
Source: {source}

Description:
{description[:3000] if description else 'No description available — score on title and company only.'}"""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=JEFF_PROFILE,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"Claude returned invalid JSON for {job.get('title')}: {e}")
        return None
    except Exception as e:
        log.error(f"Claude API error for {job.get('title')}: {e}")
        return None

# ─────────────────────────────────────────────
# JOB CRAWLERS
# One function per site. Each returns a list of job dicts:
# { company, title, url, location, salary, description, source }
# ─────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

def safe_get(url, timeout=15):
    """GET with error handling. Returns response or None."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        log.warning(f"Failed to fetch {url}: {e}")
        return None


def crawl_idealist():
    """
    Idealist.org is fully JavaScript-rendered and blocks HTTP scrapers.
    Jobs from Idealist are handled via Gmail job alert emails instead.

    Setup: On idealist.org, run each of your key searches with the
    Remote filter on, then save the search and turn on daily email alerts.
    The Gmail scanner (run_gmail_scan_idealist) will pick up those emails
    automatically on every run and create Trello cards for matches.
    """
    log.info("Crawling Idealist... (via Gmail alerts — see run_gmail_scan_idealist)")
    log.info("  Idealist total: 0 jobs (sourced via Gmail alerts instead)")
    return []
def crawl_remote_impact():
    """
    Crawls RemoteImpact.org — static HTML, scrapeable.
    """
    log.info("Crawling Remote Impact...")
    jobs = []

    search_terms = [
        "technology manager", "IT manager", "technical operations",
        "technical project manager", "technical program manager",
        "AI operations", "AI workflow", "automation manager",
        "SaaS operations", "implementation manager", "program operations",
        "workplace technology", "systems manager", "internal tools",
        "digital workplace", "operations manager",
        # Expanded (June 2026)
        "AI implementation", "enablement manager", "platform operations",
        "community operations", "workflow automation", "knowledge manager",
        "head of operations", "learning experience", "instructional design",
    ]

    seen_urls = set()

    for term in search_terms:
        url = f"https://remoteimpact.org/jobs/?search={requests.utils.quote(term)}"
        r = safe_get(url)
        if not r:
            continue

        soup = BeautifulSoup(r.text, "html.parser")

        # Remote Impact job cards — adjust selectors if site updates
        job_cards = soup.select("div.job-card, article.job, div.listing, li.job-listing")

        if not job_cards:
            # Fallback: look for any links that look like job postings
            job_cards = soup.select("a[href*='/jobs/']")

        for card in job_cards:
            try:
                # Try to find title and company
                title_el = card.select_one("h2, h3, .job-title, .title")
                company_el = card.select_one(".company, .organization, .employer")
                link_el = card.select_one("a") if card.name != "a" else card

                title = title_el.get_text(strip=True) if title_el else card.get_text(strip=True)[:80]
                company = company_el.get_text(strip=True) if company_el else "Unknown"

                href = link_el.get("href", "") if link_el else ""
                if not href:
                    continue
                if not href.startswith("http"):
                    href = "https://remoteimpact.org" + href

                if href in seen_urls or not title:
                    continue
                seen_urls.add(href)

                jobs.append({
                    "company":     company,
                    "title":       title,
                    "url":         href,
                    "location":    "Remote",
                    "salary":      "Not listed",
                    "description": card.get_text(separator=" ", strip=True)[:2000],
                    "source":      "Remote Impact",
                })

            except Exception as e:
                log.warning(f"  Error parsing Remote Impact card: {e}")
                continue

        time.sleep(1)

    log.info(f"  Remote Impact total: {len(jobs)} jobs")
    return jobs


def crawl_tech_jobs_for_good():
    """
    Crawls TechJobsForGood.co using confirmed HTML selectors from page source.
    Selectors verified June 2026:
      Title:   div.header.job-title (title attribute)
      Company: div.meta.company-name span.company_name
      Location: span.location
      Salary:  span.salary
      Link:    a.content[href*='/jobs/']
    """
    log.info("Crawling Tech Jobs for Good...")
    jobs = []
    seen_urls = set()
    base_url = "https://techjobsforgood.com"

    search_queries = [
        "IT+manager",
        "technical+operations",
        "technical+project+manager",
        "technical+program+manager",
        "AI+operations",
        "AI+workflow",
        "automation+manager",
        "SaaS+operations",
        "implementation+manager",
        "program+operations",
        "workplace+technology",
        "systems+manager",
        "operations+manager",
        "digital+workplace",
        "internal+tools",
        # Expanded (June 2026)
        "AI+implementation",
        "enablement+manager",
        "platform+operations",
        "community+operations",
        "workflow+automation",
        "knowledge+manager",
        "head+of+operations",
        "learning+experience",
        "instructional+design",
    ]

    for query in search_queries:
        url = f"{base_url}/jobs/?q={query}"
        r = safe_get(url)
        if not r:
            continue

        soup = BeautifulSoup(r.text, "html.parser")

        # Find all job links using confirmed selector from page source
        job_links = soup.select("a.content[href*='/jobs/']")

        if not job_links:
            # Fallback to any job-looking links
            job_links = soup.select("a[href*='/jobs/']")

        for link in job_links:
            try:
                href = link.get("href", "")
                if not href:
                    continue
                if not href.startswith("http"):
                    href = base_url + href
                if href in seen_urls:
                    continue
                seen_urls.add(href)

                # Title from div.header.job-title title attribute or text
                title_el = link.select_one("div.header.job-title")
                title = ""
                if title_el:
                    title = title_el.get("title", "") or title_el.get_text(strip=True)

                # Company from span.company_name
                company_el = link.select_one("span.company_name")
                company = company_el.get_text(strip=True) if company_el else "See posting"

                # Location
                location_el = link.select_one("span.location")
                location = location_el.get_text(strip=True) if location_el else "See posting"

                # Salary
                salary_el = link.select_one("span.salary")
                salary = salary_el.get_text(strip=True) if salary_el else "Not listed"

                if not title:
                    title = link.get_text(strip=True)[:80]
                if not title:
                    continue

                jobs.append({
                    "company":     company,
                    "title":       title,
                    "url":         href,
                    "location":    location,
                    "salary":      salary,
                    "description": link.get_text(separator=" ", strip=True)[:2000],
                    "source":      "Tech Jobs for Good",
                })

            except Exception as e:
                log.warning(f"  Error parsing Tech Jobs for Good card: {e}")
                continue

        time.sleep(1)

    log.info(f"  Tech Jobs for Good total: {len(jobs)} jobs")
    return jobs


def crawl_ffwd():
    """
    Crawls jobs.ffwd.org (Fast Forward — nonprofit tech jobs)
    """
    log.info("Crawling FFWD Jobs...")
    jobs = []
    seen_urls = set()

    base_url = "https://jobs.ffwd.org"
    search_urls = [
        f"{base_url}/jobs?q=IT+manager",
        f"{base_url}/jobs?q=technical+operations",
        f"{base_url}/jobs?q=technical+project+manager",
        f"{base_url}/jobs?q=technical+program+manager",
        f"{base_url}/jobs?q=AI+operations",
        f"{base_url}/jobs?q=AI+workflow",
        f"{base_url}/jobs?q=automation+manager",
        f"{base_url}/jobs?q=SaaS+operations",
        f"{base_url}/jobs?q=implementation+manager",
        f"{base_url}/jobs?q=operations+manager",
        f"{base_url}/jobs?q=workplace+technology",
        f"{base_url}/jobs?q=systems+manager",
        f"{base_url}/jobs?q=program+operations",
        f"{base_url}/jobs?q=digital+workplace",
        f"{base_url}/jobs?q=internal+tools",
        # Expanded (June 2026)
        f"{base_url}/jobs?q=AI+implementation",
        f"{base_url}/jobs?q=enablement+manager",
        f"{base_url}/jobs?q=platform+operations",
        f"{base_url}/jobs?q=community+operations",
        f"{base_url}/jobs?q=workflow+automation",
        f"{base_url}/jobs?q=knowledge+manager",
        f"{base_url}/jobs?q=head+of+operations",
        f"{base_url}/jobs?q=learning+experience",
        f"{base_url}/jobs?q=instructional+design",
    ]

    for url in search_urls:
        r = safe_get(url)
        if not r:
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("div.job, article, li.job, .job-card, [class*='JobCard']")

        if not cards:
            cards = soup.select("a[href*='/jobs/']")

        for card in cards:
            try:
                title_el = card.select_one("h2, h3, [class*='title'], [class*='Title']")
                company_el = card.select_one("[class*='company'], [class*='Company'], [class*='org']")
                link_el = card.select_one("a") if card.name != "a" else card

                title = title_el.get_text(strip=True) if title_el else ""
                company = company_el.get_text(strip=True) if company_el else "Unknown"
                href = link_el.get("href", "") if link_el else ""

                if not href or not title:
                    continue
                if not href.startswith("http"):
                    href = base_url + href
                if href in seen_urls:
                    continue
                seen_urls.add(href)

                jobs.append({
                    "company":     company,
                    "title":       title,
                    "url":         href,
                    "location":    "See posting",
                    "salary":      "Not listed",
                    "description": card.get_text(separator=" ", strip=True)[:2000],
                    "source":      "FFWD Jobs",
                })

            except Exception as e:
                log.warning(f"  Error parsing FFWD card: {e}")
                continue

        time.sleep(1)

    log.info(f"  FFWD total: {len(jobs)} jobs")
    return jobs


def enrich_job_description(job):
    """
    For jobs where we only have a snippet, fetch the full posting page
    and extract the description. Improves Claude's scoring accuracy.
    """
    if len(job.get("description", "")) > 1500:
        return job  # Already have enough

    r = safe_get(job["url"])
    if not r:
        return job

    soup = BeautifulSoup(r.text, "html.parser")

    # Remove nav, header, footer, scripts, styles
    for tag in soup(["nav", "header", "footer", "script", "style", "aside"]):
        tag.decompose()

    # Look for the main content area
    main = (
        soup.select_one("main") or
        soup.select_one("[class*='description']") or
        soup.select_one("[class*='job-detail']") or
        soup.select_one("[class*='posting']") or
        soup.select_one("article") or
        soup.select_one(".content") or
        soup.body
    )

    if main:
        text = main.get_text(separator=" ", strip=True)
        job["description"] = text[:4000]

    return job


# ─────────────────────────────────────────────
# GMAIL INTEGRATION
# ─────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

def get_gmail_service():
    """
    Authenticates with Gmail API and returns a service object.
    On first run, opens a browser for OAuth consent.
    After that, uses cached token automatically.
    """
    creds = None
    token_file = CONFIG["gmail_token_file"]
    creds_file = CONFIG["gmail_credentials_file"]

    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(creds_file).exists():
                log.error(
                    f"Gmail credentials file '{creds_file}' not found.\n"
                    "See the GMAIL SETUP GUIDE at the bottom of this file."
                )
                return None
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_file, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def search_gmail(service, query, max_results=50):
    """Search Gmail and return a list of message snippets."""
    try:
        result = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
        messages = result.get("messages", [])

        snippets = []
        for msg in messages:
            detail = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"]
            ).execute()

            headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
            snippets.append({
                "id":      msg["id"],
                "from":    headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date":    headers.get("Date", ""),
                "snippet": detail.get("snippet", ""),
            })

        return snippets
    except Exception as e:
        log.error(f"Gmail search error: {e}")
        return []


def get_email_body(service, msg_id):
    """
    Fetches the full plain-text body of an email.
    Falls back to snippet if body can't be extracted.
    Used for Idealist alert emails where the full listing
    content is needed for accurate job extraction.
    """
    try:
        msg = service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()

        payload = msg.get("payload", {})

        def extract_text(part):
            """Recursively extract plain text from email parts."""
            mime = part.get("mimeType", "")
            body = part.get("body", {})
            data = body.get("data", "")

            if mime == "text/plain" and data:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")

            # Recurse into multipart
            for subpart in part.get("parts", []):
                result = extract_text(subpart)
                if result:
                    return result
            return ""

        text = extract_text(payload)

        # Fall back to HTML part if no plain text
        if not text:
            def extract_html(part):
                mime = part.get("mimeType", "")
                body = part.get("body", {})
                data = body.get("data", "")
                if mime == "text/html" and data:
                    raw_html = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
                    soup = BeautifulSoup(raw_html, "html.parser")
                    return soup.get_text(separator=" ", strip=True)
                for subpart in part.get("parts", []):
                    result = extract_html(subpart)
                    if result:
                        return result
                return ""
            text = extract_html(payload)

        return text[:6000] if text else msg.get("snippet", "")

    except Exception as e:
        log.warning(f"Could not fetch email body: {e}")
        return ""


def classify_email_with_claude(service, email, company):
    """
    Uses Claude to classify what a job-related email means
    for pipeline status. Uses full email body for accuracy —
    snippets are often too truncated to classify reliably.
    """
    client = anthropic.Anthropic(api_key=CONFIG["anthropic_api_key"])

    body = get_email_body(service, email['id'])
    content_for_claude = body if body else email['snippet']

    prompt = f"""Classify this job application email for {company}.

From: {email['from']}
Subject: {email['subject']}
Content: {content_for_claude[:3000]}

Respond ONLY in valid JSON:
{{
  "status_change": "interview_scheduled | rejected | offer | info_requested | application_received | no_change",
  "confidence": "high | medium | low",
  "summary": "one sentence about what this email means",
  "suggested_trello_list": "Interview | Rejected | Closed | Applied | null"
}}

Guidance: An invitation to schedule a call, phone screen, chat, or any
conversation about the role counts as interview_scheduled with high
confidence — companies rarely use the word "interview" explicitly even
when that's exactly what it is. A rejection is anything saying they're
moving forward with other candidates, won't be proceeding, or the role
is filled. If the email is just a generic application confirmation
("we received your application") with no further signal, that's
application_received with high confidence, not no_change."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        log.warning(f"Could not classify email for {company}: {e}")
        return None


def run_gmail_scan():
    """
    Scans Gmail for job application status updates and moves
    Trello cards accordingly.
    """
    log.info("=" * 50)
    log.info("GMAIL PIPELINE SCAN")
    log.info("=" * 50)

    service = get_gmail_service()
    if not service:
        log.error("Could not connect to Gmail. Run --gmail-setup first.")
        return

    list_map = get_trello_lists()
    active_cards = get_all_active_cards(list_map)

    if not active_cards:
        log.info("No active Trello cards found.")
        return

    log.info(f"Scanning {len(active_cards)} active pipeline companies...")

    lookback = CONFIG["gmail_lookback_days"]
    moved = 0
    checked = 0

    for card in active_cards:
        # Extract company name from card title (format: "Company — Role")
        card_name = card["name"]
        company = card_name.split(" — ")[0].split(" - ")[0].strip()

        if not company or company == "Unknown":
            continue

        # Search Gmail for emails related to this company.
        # Two searches: one by sender domain (catches direct company emails),
        # one by company name anywhere in subject/body (catches recruiters,
        # ATS platforms like Greenhouse/Lever, and personal emails that
        # don't share the company's domain).
        domain_query = f'from:(*{company.lower().replace(" ", "*")}*) newer_than:{lookback}d'
        content_query = f'"{company}" newer_than:{lookback}d'

        emails = search_gmail(service, domain_query, max_results=8)
        content_emails = search_gmail(service, content_query, max_results=8)

        # Merge and deduplicate by message ID
        seen_ids = {e['id'] for e in emails}
        for e in content_emails:
            if e['id'] not in seen_ids:
                emails.append(e)
                seen_ids.add(e['id'])

        if not emails:
            continue

        checked += 1
        log.info(f"  Found {len(emails)} email(s) for {company}")

        for email in emails:
            classification = classify_email_with_claude(service, email, company)
            if not classification:
                log.warning(f"    [{company}] Classification failed for: {email['subject']}")
                continue

            log.info(
                f"    [{company}] '{email['subject']}' → "
                f"status_change={classification.get('status_change')}, "
                f"confidence={classification.get('confidence')}, "
                f"suggested_list={classification.get('suggested_trello_list')}"
            )

            if classification["confidence"] == "low":
                log.info(f"    [{company}] Skipping — low confidence")
                continue

            suggested_list = classification.get("suggested_trello_list")
            if not suggested_list or str(suggested_list).lower() == "null":
                log.info(f"    [{company}] Skipping — no suggested list (likely no_change)")
                continue

            # Find the target list ID — case-insensitive, whitespace-tolerant match
            target_list_id = None
            matched_list_name = None
            normalized_suggestion = suggested_list.strip().lower()
            for list_name, list_id in list_map.items():
                if list_name.strip().lower() == normalized_suggestion:
                    target_list_id = list_id
                    matched_list_name = list_name
                    break

            # Fuzzy fallback: handle "Interviewing" vs "Interview", etc.
            if not target_list_id:
                for list_name, list_id in list_map.items():
                    if (normalized_suggestion in list_name.strip().lower()
                            or list_name.strip().lower() in normalized_suggestion):
                        target_list_id = list_id
                        matched_list_name = list_name
                        log.info(
                            f"    [{company}] Fuzzy-matched '{suggested_list}' → '{list_name}'"
                        )
                        break

            if not target_list_id:
                log.warning(
                    f"    [{company}] Could not match suggested list '{suggested_list}' "
                    f"to any Trello list. Available lists: {list(list_map.keys())}"
                )
                continue

            # Don't move if already in target list
            current_list_name = next(
                (name for name, lid in list_map.items() if lid == card["idList"]),
                ""
            )
            if current_list_name == matched_list_name:
                log.info(f"    [{company}] Already in '{matched_list_name}' — no move needed")
                continue

            # Move the card
            try:
                move_trello_card(card["id"], target_list_id)
                comment = (
                    f"Auto-moved by job agent on {datetime.now().strftime('%Y-%m-%d')}\n\n"
                    f"Email: {email['subject']}\n"
                    f"From: {email['from']}\n"
                    f"Summary: {classification['summary']}"
                )
                add_comment_to_card(card["id"], comment)
                log.info(
                    f"  ✓ Moved '{card_name}' → {matched_list_name} "
                    f"({classification['summary']})"
                )
                moved += 1
            except Exception as e:
                log.error(f"  Failed to move card for {company}: {e}")

        time.sleep(0.5)  # Rate limiting

    log.info(f"\nPipeline scan complete. Checked {checked} companies, moved {moved} cards.")

    # ── Idealist job alert emails ──
    # Parse any Idealist alert emails and create Trello cards for new matches
    seen = load_seen_jobs()
    watching_list_id = list_map.get(CONFIG["trello_lists"]["watching"])
    if watching_list_id:
        idealist_cards = run_gmail_scan_idealist(service, seen, list_map, watching_list_id)
        save_seen_jobs(seen)
        if idealist_cards:
            log.info(f"  {idealist_cards} new Idealist card(s) added to Watching")
    else:
        log.warning("Could not find Watching list — skipping Idealist Gmail scan")

    # ── LinkedIn job alert emails ──
    if watching_list_id:
        linkedin_cards = run_gmail_scan_linkedin(service, seen, list_map, watching_list_id)
        save_seen_jobs(seen)
        if linkedin_cards:
            log.info(f"  {linkedin_cards} new LinkedIn card(s) added to Watching")

    # ── Built In job alert emails ──
    if watching_list_id:
        builtin_cards = run_gmail_scan_builtin(service, seen, list_map, watching_list_id)
        save_seen_jobs(seen)
        if builtin_cards:
            log.info(f"  {builtin_cards} new Built In card(s) added to Watching")



def run_gmail_scan_idealist(service, seen, list_map, watching_list_id):
    """
    Scans Gmail for Idealist job alert emails, extracts job listings,
    scores them with Claude, and creates Trello cards for strong matches.

    Idealist sends daily digest emails with subject lines like:
    "New jobs matching your search: technology manager"

    Each email contains job titles, organizations, and links.
    We parse those out and run them through the same scoring pipeline
    as the regular crawlers.
    """
    log.info("Scanning Gmail for Idealist job alerts...")

    # Search for Idealist alert emails in the last N days
    lookback = CONFIG["gmail_lookback_days"]
    query = f'from:(idealist.org) subject:(jobs matching) newer_than:{lookback}d'
    emails = search_gmail(service, query, max_results=20)

    if not emails:
        # Try alternate subject patterns
        query = f'from:(idealist.org) newer_than:{lookback}d'
        emails = search_gmail(service, query, max_results=20)

    if not emails:
        log.info("  No Idealist alert emails found in Gmail.")
        log.info("  Make sure you have saved searches with email alerts on idealist.org")
        return 0

    log.info(f"  Found {len(emails)} Idealist alert email(s)")

    client = anthropic.Anthropic(api_key=CONFIG["anthropic_api_key"])
    cards_created = 0

    for email in emails:
        # Use Claude to extract job listings from the email snippet
        # Fetch full email body for accurate job extraction
        body = get_email_body(service, email['id'])
        content_for_claude = body if body else email['snippet']

        prompt = f"""Extract job listings from this Idealist job alert email.

Subject: {email['subject']}
Content: {content_for_claude}

Return ONLY a JSON array of job objects. If no clear jobs found, return [].
Each object should have:
{{"title": "job title", "company": "organization name", "url": "job URL if visible or empty string"}}

Example: [{{"title": "IT Manager", "company": "ACLU", "url": "https://www.idealist.org/en/..."}}]

Return only the JSON array, no other text."""

        try:
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            listings = safe_parse_json_list(raw)
        except Exception as e:
            log.warning(f"  Could not parse Idealist email: {e}")
            continue

        if not listings:
            continue

        log.info(f"  Extracted {len(listings)} listing(s) from alert email")

        for listing in listings:
            title = listing.get("title", "").strip()
            company = listing.get("company", "See posting").strip()
            url = listing.get("url", "").strip()

            if not title:
                continue

            # Check seen cache
            fp = job_fingerprint(company, title)
            if fp in seen:
                continue

            # Build job dict for scoring
            job = {
                "company":     company,
                "title":       title,
                "url":         url or "https://www.idealist.org/en/jobs",
                "location":    "Remote",
                "salary":      "Not listed",
                "description": f"{title} at {company}. Source: Idealist job alert.",
                "source":      "Idealist (Gmail alert)",
            }

            # Try to enrich with full description if we have a URL
            if url and "idealist.org" in url:
                job = enrich_job_description(job)

            # Pre-filter
            should_score, reason = pre_filter(job)
            if not should_score:
                log.info(f"  Pre-filtered: {title} ({reason})")
                seen[fp] = {"verdict": "Pre-filtered", "date": datetime.now().isoformat()}
                continue

            # Score with Claude
            result = score_job_with_claude(job)
            if not result:
                seen[fp] = {"scored": False, "date": datetime.now().isoformat()}
                continue

            score = result.get("score", 0)
            verdict = result.get("verdict", "Skip")
            disqualified = result.get("disqualified", False)

            log.info(f"  {title} at {company}: {verdict} ({score}/100)")

            seen[fp] = {
                "company": company,
                "title":   title,
                "score":   score,
                "verdict": verdict,
                "date":    datetime.now().isoformat(),
            }

            if disqualified or score < CONFIG["min_score_for_card"]:
                continue

            # Create Trello card
            card_name = f"{company} — {title}"
            card_desc = f"""**Source:** Idealist (Gmail alert)
**URL:** {url or 'Search idealist.org'}
**Found:** {datetime.now().strftime('%Y-%m-%d')}

---

**Verdict:** {verdict} | **Score:** {score}/100
**Lane:** {result.get('lane', '?')}
**Mission fit:** {result.get('mission_fit', '?')}
**Salary ask:** {result.get('salary_ask', '?')}

**Why it fits:**
{result.get('why_it_fits', '—')}

**Concerns:**
{result.get('concerns', '—')}

**Cover letter angle:**
{result.get('cover_letter_angle', '—')}

**Next step:**
{result.get('next_step', '—')}

**Puzzle fit:** {'✓ Yes' if result.get('puzzle_fit') else '—'}
**Environment flags:** {', '.join(result.get('environment_flags', [])) or '—'}
**Portfolio piece:** *(add relevant RWA / email triage / job pipeline case study here)*"""

            try:
                create_trello_card(watching_list_id, card_name, card_desc)
                log.info(f"  ✓ Trello card created for {card_name}")
                cards_created += 1
            except Exception as e:
                log.error(f"  Failed to create card for {card_name}: {e}")

            time.sleep(1)

    log.info(f"  Idealist Gmail scan complete. {cards_created} card(s) created.")
    return cards_created


def run_gmail_scan_linkedin(service, seen, list_map, watching_list_id):
    """
    Scans Gmail for LinkedIn job alert emails and creates Trello cards.

    LinkedIn alert emails come from jobs-noreply@linkedin.com with
    subject lines like "7 new jobs for IT Manager" or
    "New jobs matching your preferences".

    Each email contains job title, company, location, and a direct
    apply link to the LinkedIn posting.
    """
    log.info("Scanning Gmail for LinkedIn job alerts...")

    lookback = CONFIG["gmail_lookback_days"]

    # LinkedIn sends from this address
    queries = [
        f'from:(jobs-noreply@linkedin.com) newer_than:{lookback}d',
        f'from:(linkedin.com) subject:(new jobs) newer_than:{lookback}d',
        f'from:(linkedin.com) subject:(jobs for) newer_than:{lookback}d',
    ]

    emails = []
    for query in queries:
        results = search_gmail(service, query, max_results=20)
        emails.extend(results)
        if emails:
            break

    if not emails:
        log.info("  No LinkedIn alert emails found.")
        return 0

    # Deduplicate by message ID
    seen_ids = set()
    unique_emails = []
    for e in emails:
        if e['id'] not in seen_ids:
            seen_ids.add(e['id'])
            unique_emails.append(e)
    emails = unique_emails

    log.info(f"  Found {len(emails)} LinkedIn alert email(s)")

    client = anthropic.Anthropic(api_key=CONFIG["anthropic_api_key"])
    cards_created = 0

    for email in emails:
        body = get_email_body(service, email['id'])
        content_for_claude = body if body else email['snippet']

        prompt = f"""Extract job listings from this LinkedIn job alert email.

Subject: {email['subject']}
Content: {content_for_claude}

Return ONLY a JSON array of job objects. If no clear jobs found, return [].
Each object:
{{"title": "job title", "company": "company name", "location": "location or Remote", "url": "LinkedIn job URL if visible or empty string", "salary": "salary if listed or Not listed"}}

Extract every job listing you can find. Return only the JSON array, no other text."""

        try:
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            listings = safe_parse_json_list(raw)
        except Exception as e:
            log.warning(f"  Could not parse LinkedIn email: {e}")
            continue

        if not listings:
            continue

        log.info(f"  Extracted {len(listings)} listing(s) from LinkedIn alert")

        for listing in listings:
            title = listing.get("title", "").strip()
            company = listing.get("company", "See posting").strip()
            url = listing.get("url", "").strip()
            location = listing.get("location", "See posting").strip()
            salary = listing.get("salary", "Not listed").strip()

            if not title:
                continue

            fp = job_fingerprint(company, title)
            if fp in seen:
                continue

            job = {
                "company":     company,
                "title":       title,
                "url":         url or "https://www.linkedin.com/jobs/",
                "location":    location,
                "salary":      salary,
                "description": f"{title} at {company}. Location: {location}. Source: LinkedIn job alert.",
                "source":      "LinkedIn (Gmail alert)",
            }

            if url and "linkedin.com" in url:
                job = enrich_job_description(job)

            should_score, reason = pre_filter(job)
            if not should_score:
                log.info(f"  Pre-filtered: {title} ({reason})")
                seen[fp] = {"verdict": "Pre-filtered", "date": datetime.now().isoformat()}
                continue

            result = score_job_with_claude(job)
            if not result:
                seen[fp] = {"scored": False, "date": datetime.now().isoformat()}
                continue

            score = result.get("score", 0)
            verdict = result.get("verdict", "Skip")
            disqualified = result.get("disqualified", False)

            log.info(f"  {title} at {company}: {verdict} ({score}/100)")

            seen[fp] = {
                "company": company,
                "title":   title,
                "score":   score,
                "verdict": verdict,
                "date":    datetime.now().isoformat(),
            }

            if disqualified or score < CONFIG["min_score_for_card"]:
                continue

            card_name = f"{company} — {title}"
            card_desc = f"""**Source:** LinkedIn (Gmail alert)
**URL:** {url or 'Search linkedin.com/jobs'}
**Found:** {datetime.now().strftime('%Y-%m-%d')}

---

**Verdict:** {verdict} | **Score:** {score}/100
**Lane:** {result.get('lane', '?')}
**Mission fit:** {result.get('mission_fit', '?')}
**Salary ask:** {result.get('salary_ask', '?')}

**Why it fits:**
{result.get('why_it_fits', '—')}

**Concerns:**
{result.get('concerns', '—')}

**Cover letter angle:**
{result.get('cover_letter_angle', '—')}

**Next step:**
{result.get('next_step', '—')}

**Puzzle fit:** {'✓ Yes' if result.get('puzzle_fit') else '—'}
**Environment flags:** {', '.join(result.get('environment_flags', [])) or '—'}
**Portfolio piece:** *(add relevant RWA / email triage / job pipeline case study here)*"""

            try:
                create_trello_card(watching_list_id, card_name, card_desc)
                log.info(f"  ✓ Trello card created for {card_name}")
                cards_created += 1
            except Exception as e:
                log.error(f"  Failed to create card for {card_name}: {e}")

            time.sleep(1)

    log.info(f"  LinkedIn Gmail scan complete. {cards_created} card(s) created.")
    return cards_created


def run_gmail_scan_builtin(service, seen, list_map, watching_list_id):
    """
    Scans Gmail for Built In job alert emails and creates Trello cards.

    Built In alert emails come from hello@builtin.com or
    notifications@builtin.com with subject lines like
    "New jobs matching your search" or "Jobs you might like".

    Each email contains job title, company, location, salary range,
    and a direct link to the Built In posting.
    """
    log.info("Scanning Gmail for Built In job alerts...")

    lookback = CONFIG["gmail_lookback_days"]

    queries = [
        f'from:(builtin.com) subject:(jobs) newer_than:{lookback}d',
        f'from:(builtin.com) newer_than:{lookback}d',
    ]

    emails = []
    for query in queries:
        results = search_gmail(service, query, max_results=20)
        emails.extend(results)
        if emails:
            break

    if not emails:
        log.info("  No Built In alert emails found.")
        return 0

    # Deduplicate
    seen_ids = set()
    unique_emails = []
    for e in emails:
        if e['id'] not in seen_ids:
            seen_ids.add(e['id'])
            unique_emails.append(e)
    emails = unique_emails

    log.info(f"  Found {len(emails)} Built In alert email(s)")

    client = anthropic.Anthropic(api_key=CONFIG["anthropic_api_key"])
    cards_created = 0

    for email in emails:
        body = get_email_body(service, email['id'])
        content_for_claude = body if body else email['snippet']

        prompt = f"""Extract job listings from this Built In job alert email.

Subject: {email['subject']}
Content: {content_for_claude}

Return ONLY a JSON array of job objects. If no clear jobs found, return [].
Each object:
{{"title": "job title", "company": "company name", "location": "location or Remote", "url": "Built In job URL if visible or empty string", "salary": "salary if listed or Not listed"}}

Extract every job listing you can find. Return only the JSON array, no other text."""

        try:
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            listings = safe_parse_json_list(raw)
        except Exception as e:
            log.warning(f"  Could not parse Built In email: {e}")
            continue

        if not listings:
            continue

        log.info(f"  Extracted {len(listings)} listing(s) from Built In alert")

        for listing in listings:
            title = listing.get("title", "").strip()
            company = listing.get("company", "See posting").strip()
            url = listing.get("url", "").strip()
            location = listing.get("location", "See posting").strip()
            salary = listing.get("salary", "Not listed").strip()

            if not title:
                continue

            fp = job_fingerprint(company, title)
            if fp in seen:
                continue

            job = {
                "company":     company,
                "title":       title,
                "url":         url or "https://builtin.com/jobs",
                "location":    location,
                "salary":      salary,
                "description": f"{title} at {company}. Location: {location}. Source: Built In job alert.",
                "source":      "Built In (Gmail alert)",
            }

            if url and "builtin.com" in url:
                job = enrich_job_description(job)

            should_score, reason = pre_filter(job)
            if not should_score:
                log.info(f"  Pre-filtered: {title} ({reason})")
                seen[fp] = {"verdict": "Pre-filtered", "date": datetime.now().isoformat()}
                continue

            result = score_job_with_claude(job)
            if not result:
                seen[fp] = {"scored": False, "date": datetime.now().isoformat()}
                continue

            score = result.get("score", 0)
            verdict = result.get("verdict", "Skip")
            disqualified = result.get("disqualified", False)

            log.info(f"  {title} at {company}: {verdict} ({score}/100)")

            seen[fp] = {
                "company": company,
                "title":   title,
                "score":   score,
                "verdict": verdict,
                "date":    datetime.now().isoformat(),
            }

            if disqualified or score < CONFIG["min_score_for_card"]:
                continue

            card_name = f"{company} — {title}"
            card_desc = f"""**Source:** Built In (Gmail alert)
**URL:** {url or 'Search builtin.com/jobs'}
**Found:** {datetime.now().strftime('%Y-%m-%d')}

---

**Verdict:** {verdict} | **Score:** {score}/100
**Lane:** {result.get('lane', '?')}
**Mission fit:** {result.get('mission_fit', '?')}
**Salary ask:** {result.get('salary_ask', '?')}

**Why it fits:**
{result.get('why_it_fits', '—')}

**Concerns:**
{result.get('concerns', '—')}

**Cover letter angle:**
{result.get('cover_letter_angle', '—')}

**Next step:**
{result.get('next_step', '—')}

**Puzzle fit:** {'✓ Yes' if result.get('puzzle_fit') else '—'}
**Environment flags:** {', '.join(result.get('environment_flags', [])) or '—'}
**Portfolio piece:** *(add relevant RWA / email triage / job pipeline case study here)*"""

            try:
                create_trello_card(watching_list_id, card_name, card_desc)
                log.info(f"  ✓ Trello card created for {card_name}")
                cards_created += 1
            except Exception as e:
                log.error(f"  Failed to create card for {card_name}: {e}")

            time.sleep(1)

    log.info(f"  Built In Gmail scan complete. {cards_created} card(s) created.")
    return cards_created


# ─────────────────────────────────────────────
# MAIN CRAWL + SCORE + POST PIPELINE
# ─────────────────────────────────────────────

def run_job_crawl():
    """
    Main pipeline:
    1. Crawl all sources
    2. Deduplicate against seen jobs cache
    3. Score each new job with Claude
    4. Create Trello cards for jobs above threshold
    5. Log summary
    """
    log.info("=" * 50)
    log.info("JOB CRAWL STARTING")
    log.info(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info("=" * 50)

    # Load seen jobs to avoid duplicates
    seen = load_seen_jobs()
    log.info(f"Seen jobs cache: {len(seen)} entries")

    # Get Trello list IDs
    try:
        list_map = get_trello_lists()
        log.info(f"Trello connected. Lists: {list(list_map.keys())}")
    except Exception as e:
        log.error(f"Could not connect to Trello: {e}")
        return

    watching_list_id = list_map.get(CONFIG["trello_lists"]["watching"])
    if not watching_list_id:
        log.error(f"Could not find '{CONFIG['trello_lists']['watching']}' list on Trello board.")
        return

    # Run all crawlers
    all_jobs = []
    all_jobs.extend(crawl_remote_impact())
    all_jobs.extend(crawl_tech_jobs_for_good())
    all_jobs.extend(crawl_ffwd())

    log.info(f"\nTotal raw jobs found: {len(all_jobs)}")

    # Deduplicate
    new_jobs = []
    for job in all_jobs:
        fp = job_fingerprint(job["company"], job["title"])
        if fp not in seen:
            new_jobs.append((fp, job))

    log.info(f"New jobs (not yet seen): {len(new_jobs)}")

    if not new_jobs:
        log.info("Nothing new today. Done.")
        return

    # Score and post
    cards_created = 0
    cards_skipped = 0
    pre_filtered = 0
    errors = 0

    for fp, job in new_jobs:
        log.info(f"\nChecking: {job['title']} at {job['company']} ({job['source']})")

        # Pre-filter — cheap keyword check before calling Claude
        should_score, reason = pre_filter(job)
        if not should_score:
            log.info(f"  Pre-filtered ({reason}) — skipping Claude call")
            seen[fp] = {
                "company":       job["company"],
                "title":         job["title"],
                "score":         0,
                "verdict":       "Pre-filtered",
                "filter_reason": reason,
                "date":          datetime.now().isoformat(),
            }
            pre_filtered += 1
            continue

        log.info(f"  Sending to Claude ({reason})...")

        # Enrich description if short
        job = enrich_job_description(job)

        # Score with Claude
        result = score_job_with_claude(job)

        if not result:
            log.warning(f"  Could not score — skipping")
            errors += 1
            seen[fp] = {"scored": False, "date": datetime.now().isoformat()}
            continue

        score = result.get("score", 0)
        verdict = result.get("verdict", "Skip")
        disqualified = result.get("disqualified", False)

        log.info(f"  Verdict: {verdict} | Score: {score} | Mission: {result.get('mission_fit', '?')}")

        # Mark as seen regardless of score
        seen[fp] = {
            "company": job["company"],
            "title":   job["title"],
            "score":   score,
            "verdict": verdict,
            "date":    datetime.now().isoformat(),
        }

        # Skip if below threshold or disqualified
        if disqualified:
            log.info(f"  Disqualified: {result.get('disqualifier_reason', 'hard filter')}")
            cards_skipped += 1
            continue

        if score < CONFIG["min_score_for_card"]:
            log.info(f"  Score {score} below threshold {CONFIG['min_score_for_card']} — skipping")
            cards_skipped += 1
            continue

        # Build Trello card
        card_name = f"{job['company']} — {job['title']}"
        card_desc = f"""**Source:** {job['source']}
**URL:** {job['url']}
**Found:** {datetime.now().strftime('%Y-%m-%d')}

---

**Verdict:** {verdict} | **Score:** {score}/100
**Lane:** {result.get('lane', '?')}
**Mission fit:** {result.get('mission_fit', '?')}
**Salary ask:** {result.get('salary_ask', '?')}

**Why it fits:**
{result.get('why_it_fits', '—')}

**Concerns:**
{result.get('concerns', '—')}

**Cover letter angle:**
{result.get('cover_letter_angle', '—')}

**Next step:**
{result.get('next_step', '—')}

**Puzzle fit:** {'✓ Yes' if result.get('puzzle_fit') else '—'}
**Environment flags:** {', '.join(result.get('environment_flags', [])) or '—'}
**Portfolio piece:** *(add relevant RWA / email triage / job pipeline case study here)*"""

        try:
            card = create_trello_card(watching_list_id, card_name, card_desc)
            log.info(f"  ✓ Trello card created → Watching ({card.get('url', '')})")
            cards_created += 1
        except Exception as e:
            log.error(f"  Failed to create Trello card: {e}")
            errors += 1

        # Save cache periodically
        if cards_created % 5 == 0:
            save_seen_jobs(seen)

        time.sleep(1)  # Rate limit Claude API calls

    # Final save
    save_seen_jobs(seen)

    log.info("\n" + "=" * 50)
    log.info("CRAWL COMPLETE")
    log.info(f"  Jobs found:       {len(all_jobs)}")
    log.info(f"  New this run:     {len(new_jobs)}")
    log.info(f"  Pre-filtered:     {pre_filtered}  (no Claude call)")
    log.info(f"  Sent to Claude:   {len(new_jobs) - pre_filtered}")
    log.info(f"  Cards created:    {cards_created}")
    log.info(f"  Below threshold:  {cards_skipped}")
    log.info(f"  Errors:           {errors}")
    log.info(f"  Est. API cost:    ~${((len(new_jobs) - pre_filtered) * 0.003):.3f}")
    log.info("=" * 50)


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Jeff's Job Search Agent")
    parser.add_argument("--crawl",       action="store_true", help="Run job crawl only")
    parser.add_argument("--gmail",       action="store_true", help="Run Gmail scan only")
    parser.add_argument("--gmail-setup", action="store_true", help="Run Gmail OAuth setup")
    args = parser.parse_args()

    if args.gmail_setup:
        log.info("Running Gmail OAuth setup...")
        service = get_gmail_service()
        if service:
            log.info("✓ Gmail connected successfully. Token saved.")
        return

    if args.crawl:
        run_job_crawl()
    elif args.gmail:
        run_gmail_scan()
    else:
        # Default: run both
        run_job_crawl()
        run_gmail_scan()


if __name__ == "__main__":
    main()


# Setup instructions are in README.md
