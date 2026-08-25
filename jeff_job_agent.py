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
Run reconcile only:   python jeff_job_agent.py --reconcile
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
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
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
        "stale":       "Stale/No Reply",
    },

    # Lists to search for fuzzy duplicates before creating a new card
    "duplicate_check_lists": ["watching", "applied", "interview", "rejected", "stale"],

    # How far back to look when checking for duplicate/reposted listings
    "duplicate_lookback_days": 90,

    # Gmail OAuth — download credentials.json from Google Cloud Console
    # See SETUP GUIDE at the bottom of this file
    "gmail_credentials_file": "credentials.json",
    "gmail_token_file":       "gmail_token.json",

    # Score threshold — only create Trello cards for roles at or above this
    "min_score_for_card": 60,

    # Alert-email sources to scan for job listings. Idealist stays on;
    # LinkedIn and Built In are switched off (2026-08) — their alerts
    # weren't surfacing roles Jeff was interested in. The scan functions
    # themselves (run_gmail_scan_linkedin, run_gmail_scan_builtin) are
    # untouched — flip these back to True to turn a source back on.
    "enable_linkedin_alerts": False,
    "enable_builtin_alerts": False,

    # How many days back to scan Gmail for status updates
    "gmail_lookback_days": 7,

    # Seen jobs cache — prevents duplicate cards across runs
    "seen_jobs_file": "seen_jobs.json",

    # Seen emails cache — prevents re-classifying the same email on every run
    # This is the main cost control for the Gmail scanner
    "seen_emails_file": "seen_emails.json",

    # How many days back the Gmail-Trello reconciliation job searches for
    # application-related threads that the per-company sync might have missed
    "reconciliation_lookback_days": 14,

    # Seen-threads cache for the reconciliation job — separate from
    # seen_emails.json since it covers a broader, unrelated search
    "seen_reconciliation_emails_file": "seen_reconciliation_emails.json",

    # Threads that don't match any existing Trello card, for Jeff to review
    # and confirm before anything gets created — the agent never writes a
    # new card off a reconciliation match on its own
    "orphan_candidates_file": "orphan_candidates.json",

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
# PORTFOLIO PROJECTS
# Named, real projects Claude can cite as a proof point on a card.
# Scoring prompt picks the single best match (or none) — never leave
# an unfilled template placeholder in a card description.
# ─────────────────────────────────────────────

PORTFOLIO_PROJECTS = [
    {
        "name": "RWA Grant Navigator",
        "one_liner": "Claude-designed, Custom-GPT-deployed grant research workflow for a 6-volunteer nonprofit team; 70+ opportunities vetted, 50 surfaced.",
        "best_for": ["Lane 3 AI Ops", "nonprofit", "edtech", "mission-driven ops"],
    },
    {
        "name": "Job Search Automation Pipeline",
        "one_liner": "Python + Gmail + Trello + Claude API pipeline that scores and routes job postings daily, unattended. Public repo.",
        "best_for": ["Lane 3 AI Ops", "AI/agent-building roles"],
    },
    {
        "name": "GrantOps",
        "one_liner": "Productized version of the RWA grant workflow, packaged as a repeatable flat-fee offering for other schools.",
        "best_for": ["Lane 3 AI Ops", "edtech", "systems-as-product roles"],
    },
    {
        "name": "Reforge",
        "one_liner": "Live, deployed fantasy-themed fitness RPG web app, built end-to-end with Claude Code; evolving toward a full lifestyle app.",
        "best_for": ["AI-native builder roles", "hands-on technical depth signal"],
    },
    {
        "name": "Paths of Wonder",
        "one_liner": "Live interactive fiction engine (Python, data-driven story architecture, autosaving state), published and in active development.",
        "best_for": ["AI-native builder roles", "creative tools", "gaming"],
    },
    {
        "name": "The Sorting Room",
        "one_liner": "Scoped email-triage automation (Node.js, Microsoft Graph, Claude API) for a small business client; est. $10-15/mo to run.",
        "best_for": ["Lane 3 AI Ops", "Lane 1 IT Ops", "small-business/nonprofit ops"],
    },
    {
        "name": "The Record Room",
        "one_liner": "Compliance document-architecture system for a nonprofit school's business manager; reusable framework across clients.",
        "best_for": ["Lane 1 IT Ops", "nonprofit/education compliance"],
    },
]

_PORTFOLIO_PROJECTS_TEXT = "\n".join(
    f"- {p['name']}: {p['one_liner']} (best for: {', '.join(p['best_for'])})"
    for p in PORTFOLIO_PROJECTS
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
    "human resources", "hr manager", "hr director",
    "legal counsel", "attorney", "lawyer", "nurse", "physician", "clinical",
    "ux designer", "graphic designer", "data scientist",
    "data engineer", "machine learning engineer", "software engineer",
    "software developer", "frontend engineer", "backend engineer",
    "full stack", "fullstack", "devops engineer", "site reliability",
    "security engineer", "penetration tester", "blockchain", "crypto",
    "web3", "defi", "nft", "content writer", "copywriter", "social media",
    "seo specialist", "paid media", "field technician", "field service",
    "field consultant", "warehouse", "driver", "logistics coordinator",
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
    "chief of staff",  # confirmed hits: Spark MicroGrants (62), NYC Kids RISE (88)
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
    "duty station", "local hire", "locally hired",
]

# ─────────────────────────────────────────────
# OCCUPATION / TITLE MISMATCH FILTER
# Blocks blue-collar, manual-labor, field-operations, and other job titles
# that fall outside Jeff's lanes REGARDLESS of mission-vertical keyword
# hits — a Forklift Operator posting at a food-security nonprofit is still
# a forklift operator role. Runs on the job TITLE only, not description
# text, since mission-aligned orgs will legitimately mention e.g. "food
# distribution" in a program-ops job description without the role itself
# being that job.
# ─────────────────────────────────────────────

BLOCKED_TITLE_PATTERNS = [
    r"\bforklift\b", r"\bdriver\b", r"\bwarehouse\b", r"\bmechanic\b",
    r"\bpipe ?fitter\b", r"\bplumber\b", r"\bfront desk\b", r"\bcashier\b",
    r"\bjanitor\b", r"\bcustodian\b", r"\bsecurity guard\b",
    r"\bfarm ?(hand|worker)\b", r"\bagro\b", r"\bfield (officer|associate|worker|agent)\b",
    r"\bdairy\b", r"\bconstruction\b", r"\bwelder\b", r"\bnurse\b",
]
_BLOCKED_TITLE_RE = re.compile("|".join(BLOCKED_TITLE_PATTERNS), re.IGNORECASE)

# "distribution" + a role word, in EITHER order. The real motivating
# example ("Associate Officer, Distribution") has the role word first,
# comma-separated, with "distribution" last — a fixed "distribution
# <role>" pattern misses it entirely (confirmed by testing).
_DISTRIBUTION_ROLE_WORDS = r"(assistant|associate|officer|in-?charge)"
_DISTRIBUTION_RE = re.compile(
    rf"\bdistribution\b.{{0,30}}\b{_DISTRIBUTION_ROLE_WORDS}\b"
    rf"|\b{_DISTRIBUTION_ROLE_WORDS}\b.{{0,30}}\bdistribution\b",
    re.IGNORECASE,
)

# Trade/technical titles that need a protective exception: block only if
# the base word is present AND none of its protective qualifier words
# appear ANYWHERE in the title. Checked order-independently (not just
# "does the qualifier follow the base word") because standard title
# phrasing usually puts the qualifier BEFORE the base word — "IT
# Technician," not "Technician IT" — and a direction-sensitive lookahead
# would incorrectly block exactly the titles it's meant to protect
# (confirmed by testing: a lookahead-only version blocked "IT Technician,"
# "Network Technician," and "Help Desk Technician" outright).
_PROTECTED_OCCUPATION_TERMS = [
    (r"\belectrician\b", r"\bmanager\b"),
    (r"\btechnician\b", r"\b(it|systems|network|help ?desk)\b"),
    (r"\bteacher\b", r"\b(coach|coordinator)\b"),
]

def is_occupation_mismatch(job_title):
    """
    Returns True if the job title matches a known-mismatch occupation
    category (blue-collar/manual-labor/field-operations titles outside
    Jeff's lanes). Call this BEFORE scoring — if True, skip the card
    entirely rather than scoring it.
    """
    if not job_title:
        return False
    title = job_title.lower()

    if _BLOCKED_TITLE_RE.search(title):
        return True
    if _DISTRIBUTION_RE.search(title):
        return True
    for base_pattern, protective_pattern in _PROTECTED_OCCUPATION_TERMS:
        if re.search(base_pattern, title) and not re.search(protective_pattern, title):
            return True

    return False

# ─────────────────────────────────────────────
# EXPANDED DEFENSE / AEROSPACE CONTRACTOR DETECTION
# Matches on employer name — primary defense primes plus the major
# "Beltway bandit" IT/services contractors that work overwhelmingly with
# DoD/intelligence-community clients. This is separate from the existing
# HARD_DISQUALIFIER_PATTERNS "defense/aerospace" category (JD-text jargon
# like "DoD"/"classified"/"ITAR"), which misses postings whose JD simply
# doesn't happen to use those words even though the employer itself is
# unambiguous — e.g. a GDIT posting that just says "federal cloud
# modernization" without ever saying "classified."
# ─────────────────────────────────────────────

DEFENSE_CONTRACTOR_PATTERNS = [
    r"\bgeneral dynamics\b", r"\bgdit\b", r"\blockheed martin\b",
    r"\bnorthrop grumman\b", r"\braytheon\b", r"\brtx corp(oration)?\b",
    r"\bboeing defense\b", r"\bl3 ?harris\b", r"\bbae systems\b",
    r"\bleidos\b", r"\bsaic\b", r"\bcaci\b", r"\bbooz allen\b",
    r"\bparsons corp(oration)?\b", r"\bperaton\b", r"\bamentum\b",
    r"\bkbr\b", r"\banduril\b",
    r"\bpalantir\b",  # commercial + heavy defense/intel contracts — flagged
                      # as possibly too aggressive; revisit if it costs a
                      # role Jeff would've actually wanted considered
]
_DEFENSE_CONTRACTOR_RE = re.compile("|".join(DEFENSE_CONTRACTOR_PATTERNS), re.IGNORECASE)

def is_defense_contractor(employer_name):
    """
    Returns True if the employer name matches a known defense/aerospace
    contractor or subsidiary. Call this BEFORE scoring, as a hard
    disqualifier alongside the existing NGO/non-US-org company blocklist.
    """
    if not employer_name:
        return False
    return bool(_DEFENSE_CONTRACTOR_RE.search(employer_name.lower()))


def pre_filter(job):
    """
    Fast keyword pre-filter. Returns (should_score, reason).
    True = send to Claude. False = skip, save the API call.

    Logic:
    0. Block if company matches a known non-remote-US organization or
       defense/aerospace contractor
    1. Block if title matches a hard blocklist term or occupation-mismatch
       pattern (blue-collar/manual-labor titles, regardless of company)
    2. Pass if title matches an allowlist term
    3. If title is ambiguous, check description for disqualifiers
    4. Default to sending to Claude when uncertain — better to
       spend $0.003 than miss a good role
    """
    title = job.get("title", "").lower()
    description = job.get("description", "").lower()
    company = job.get("company", "").lower()

    # Hard block on known-non-remote-US organizations, regardless of title
    # or how the description reads. See COMPANY_HARD_BLOCKLIST comment.
    # Word-boundary match, not bare substring — "brac" as a substring also
    # matches inside "Embrace", which would silently skip any unrelated
    # company with "Embrace" in its name.
    for term in COMPANY_HARD_BLOCKLIST:
        if re.search(rf"\b{re.escape(term)}\b", company):
            return False, f"company blocklist: '{term}'"

    # Hard block on defense/aerospace contractors and known subsidiaries,
    # checked on the company field directly — the existing
    # HARD_DISQUALIFIER_PATTERNS "defense/aerospace" category only catches
    # JD-text jargon ("DoD," "classified"), which misses postings that
    # don't happen to use those words even though the employer itself is
    # unambiguous.
    defense_match = _DEFENSE_CONTRACTOR_RE.search(company)
    if defense_match:
        return False, f"defense contractor: '{defense_match.group(0)}'"

    # Hard block on title — these are almost never a fit
    for term in TITLE_BLOCKLIST:
        if term in title:
            return False, f"title blocklist: '{term}'"

    # Hard block on occupation mismatch — blue-collar/manual-labor/field
    # titles outside Jeff's lanes, regardless of how mission-aligned the
    # employer is (a Forklift Operator posting at a food-security
    # nonprofit is still a forklift operator role)
    occupation_match = _BLOCKED_TITLE_RE.search(title) or _DISTRIBUTION_RE.search(title)
    if occupation_match:
        return False, f"occupation mismatch: '{occupation_match.group(0)}'"
    for base_pattern, protective_pattern in _PROTECTED_OCCUPATION_TERMS:
        base_match = re.search(base_pattern, title)
        if base_match and not re.search(protective_pattern, title):
            return False, f"occupation mismatch: '{base_match.group(0)}'"

    # Strong signal in title — send to Claude
    for term in TITLE_ALLOWLIST:
        if term in title:
            # Still do a quick description check for hard disqualifiers
            hits = sum(1 for term in DESCRIPTION_BLOCKLIST if term in description)
            if hits >= DESCRIPTION_BLOCKLIST_THRESHOLD:
                return False, f"description has {hits} disqualifier signals"
            return True, "title allowlist match"

    # Ambiguous title — check description for positive signals
    # NOTE: "remote" was removed from this list. It was meant as a signal
    # for remote-friendly roles, but as a bare substring match against the
    # full description text it fires just as often on phrases like "remote
    # schools," "remote communities," or "remote regions" (common in
    # international development / NGO postings) as it does on actual
    # remote-work language. That made it a false-positive magnet rather
    # than a useful signal — it was making international, non-remote
    # postings (BRAC, GIGA, etc.) MORE likely to pass, not less. Remote
    # eligibility is checked separately via location field + hard
    # disqualifier patterns below, not as a role-fit keyword here.
    positive_signals = [
        "project management", "operations", "saas", "technical",
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


def load_seen_emails():
    """
    Loads the cache of email IDs that have already been classified.
    Prevents re-sending the same email to Claude on every run —
    the main cost control for the Gmail scanner.
    """
    path = Path(CONFIG["seen_emails_file"])
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}

def save_seen_emails(seen):
    with open(CONFIG["seen_emails_file"], "w") as f:
        json.dump(seen, f, indent=2)

def mark_email_seen(seen_emails, email_id, company, classification):
    """Records an email as processed so it's never re-classified."""
    seen_emails[email_id] = {
        "company":       company,
        "subject":       "",
        "status_change": classification.get("status_change", "unknown"),
        "confidence":    classification.get("confidence", "unknown"),
        "date":          datetime.now().isoformat(),
    }


def load_seen_reconciliation_emails():
    """Cache of threads already processed by the reconciliation job."""
    path = Path(CONFIG["seen_reconciliation_emails_file"])
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}

def save_seen_reconciliation_emails(seen):
    with open(CONFIG["seen_reconciliation_emails_file"], "w") as f:
        json.dump(seen, f, indent=2)

def load_orphan_candidates():
    """Threads flagged as having no matching Trello card, pending Jeff's review."""
    path = Path(CONFIG["orphan_candidates_file"])
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}

def save_orphan_candidates(orphans):
    with open(CONFIG["orphan_candidates_file"], "w") as f:
        json.dump(orphans, f, indent=2)


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

def get_card_description(card_id):
    """Fetches just the description field of a card."""
    url = f"{TRELLO_BASE}/cards/{card_id}"
    r = requests.get(url, params=trello_params({"fields": "desc"}))
    r.raise_for_status()
    return r.json().get("desc", "")

def update_card_description(card_id, new_desc):
    """Overwrites a card's description. Also bumps dateLastActivity."""
    url = f"{TRELLO_BASE}/cards/{card_id}"
    r = requests.put(url, params=trello_params({"desc": new_desc}))
    r.raise_for_status()
    return r.json()

# ─────────────────────────────────────────────
# DUPLICATE DETECTION
# Fuzzy-matches a new listing against open pipeline cards before a
# Trello card is created, so reposts get appended to the existing
# card instead of showing up as a brand-new "Watching" entry.
# ─────────────────────────────────────────────

def normalize(text):
    text = text.lower()
    text = re.sub(r"\(remote\)|\(hybrid\)|\[.*?\]", "", text)
    text = re.sub(r"[^a-z0-9 ]", "", text)
    return " ".join(text.split())

def parse_card_name(card_name):
    """Splits a 'Company — Role' (or ' - ') card title into (company, title)."""
    for sep in (" — ", " - "):
        if sep in card_name:
            company, _, title = card_name.partition(sep)
            return company.strip(), title.strip()
    return card_name.strip(), ""

def is_likely_duplicate(new_company, new_title, existing_cards, threshold=0.85):
    """
    existing_cards: [{'company':..., 'title':..., 'card_id':..., 'list_name':...}]
    Returns the matching existing card dict if found, else None.

    Both sides are run through clean_company_name() first — an FFWD card
    stored with a raw disambiguation-suffixed name ("CodePath Org 2") and
    a cleanly-named one ("CodePath") need to resolve to the same company
    here, or duplicate/reconciliation/orphan detection all silently miss
    the match.
    """
    norm_new = normalize(f"{clean_company_name(new_company)} {new_title}")
    for card in existing_cards:
        norm_existing = normalize(f"{clean_company_name(card['company'])} {card['title']}")
        ratio = SequenceMatcher(None, norm_new, norm_existing).ratio()
        if ratio >= threshold:
            return card
    return None

def get_cards_for_duplicate_check(list_map):
    """
    Fetches open cards from the pipeline lists worth checking for reposts
    (Watching, Applied, Interview, Rejected, Stale/No Reply), limited to
    cards active within the configured lookback window.
    """
    lookback_days = CONFIG["duplicate_lookback_days"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    cards = []
    for key in CONFIG["duplicate_check_lists"]:
        list_name = CONFIG["trello_lists"].get(key)
        list_id = list_map.get(list_name)
        if not list_id:
            continue

        for card in get_trello_cards(list_id):
            last_activity = card.get("dateLastActivity")
            if last_activity:
                try:
                    activity_dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
                    if activity_dt < cutoff:
                        continue
                except ValueError:
                    pass

            company, title = parse_card_name(card.get("name", ""))
            if not company:
                continue

            cards.append({
                "company":   company,
                "title":     title,
                "card_id":   card["id"],
                "list_name": list_name,
            })

    return cards

def get_all_board_cards(list_map):
    """
    All cards across every list on the board, with no lookback window —
    used for orphan detection during Gmail-Trello reconciliation, where an
    old card is still a valid match.
    """
    cards = []
    for list_name, list_id in list_map.items():
        for card in get_trello_cards(list_id):
            company, title = parse_card_name(card.get("name", ""))
            if not company:
                continue
            cards.append({
                "company":   company,
                "title":     title,
                "card_id":   card["id"],
                "list_name": list_name,
            })
    return cards

def handle_possible_duplicate(existing_cards, company, title, source):
    """
    Checks a new listing against existing pipeline cards. If a fuzzy match
    is found, appends a repost note to that card (bumping its activity so
    it doesn't look falsely stale) instead of letting a new card get created.
    Returns the matching card dict, or None if no duplicate was found.
    """
    match = is_likely_duplicate(company, title, existing_cards)
    if not match:
        return None

    try:
        current_desc = get_card_description(match["card_id"])
        repost_count = current_desc.lower().count("reposted") + 1
        note = (
            f"\n\n— reposted {datetime.now().strftime('%Y-%m-%d')} "
            f"(seen {repost_count}x, via {source}), no new card created"
        )
        update_card_description(match["card_id"], current_desc + note)
        log.info(
            f"  Duplicate: '{company} — {title}' matches existing card in "
            f"'{match['list_name']}' — appended repost note (seen {repost_count}x) "
            f"instead of creating a new card."
        )
    except Exception as e:
        log.error(f"  Failed to update existing card for duplicate '{company} — {title}': {e}")

    return match

def create_card_or_note_duplicate(existing_cards, watching_list_id, company, title, card_desc, source_label):
    """
    Checks for a duplicate before creating a Trello card. If a duplicate is
    found, appends a repost note to the existing card and returns None.
    Otherwise creates the card, tracks it in existing_cards so later jobs
    in this same run are also checked against it, and returns the card.
    """
    if handle_possible_duplicate(existing_cards, company, title, source_label):
        return None

    card_name = f"{company} — {title}"
    try:
        card = create_trello_card(watching_list_id, card_name, card_desc)
        log.info(f"  ✓ Trello card created for {card_name}")
        existing_cards.append({
            "company":   company,
            "title":     title,
            "card_id":   card["id"],
            "list_name": CONFIG["trello_lists"]["watching"],
        })
        return card
    except Exception as e:
        log.error(f"  Failed to create card for {card_name}: {e}")
        return None

# ─────────────────────────────────────────────
# HARD DISQUALIFIER TRIPWIRE
# The holistic scorer weighs title/salary/company well but has repeatedly
# missed a few sentences buried in the JD that should override everything
# else (e.g. a "Technical Project Manager" title whose actual job is
# managing client accounts and revenue). This is a keyword tripwire that
# runs alongside the narrative score, not a replacement for it — a match
# can be a false positive (adjacent team mentioned, not the role itself),
# so it flags for a second look rather than auto-rejecting.
# ─────────────────────────────────────────────

HARD_DISQUALIFIER_PATTERNS = {
    "sales/RevOps/customer success": [
        r"\bcustomer success\b", r"\baccount expansion\b", r"\bbook of business\b",
        r"\brevenue cycle\b", r"\bquota\b", r"\brenewals?\b", r"\bupsell", r"\bcross-?sell",
        r"\bsales pipeline\b", r"\baccount growth\b", r"\bclient portfolio strategy\b",
        r"\bCRM hygiene\b", r"\bpipeline activity\b",
    ],
    "Paper Tiger pattern": [
        r"\bclient relationship\b.{0,40}\b(manage|own)", r"\bclient relations account\b",
        r"\baccount continuity\b", r"\bprofitability\b.{0,40}\bproject\b",
    ],
    "crypto": [r"\bcrypto\b", r"\bbitcoin\b", r"\bweb3\b", r"\bblockchain\b"],
    "defense/aerospace": [r"\bdefense contractor\b", r"\bDoD\b", r"\bclassified\b", r"\bITAR\b"],
    "hybrid/onsite required": [
        r"\bhybrid\b", r"\bin-?person\b", r"\bon-?site required\b",
        r"\bin.?office\b.{0,20}\b(days?|week)\b",
        r"\b\d+\s*days?\s*(a|per)\s*week\s*in\s*(the\s*)?office\b",
        r"\bmust work from (our|the) office\b",
        # Named-weekday in-office requirements (e.g. "office Tuesdays,
        # Thursdays, and additional days as needed") — added after testing
        # showed the patterns above miss this phrasing, which is exactly
        # how the confirmed United Way of Greater LA JD stated it.
        r"\boffice\b.{0,80}\b(mondays?|tuesdays?|wednesdays?|thursdays?|fridays?)\b",
    ],
    # International development / NGO postings (BRAC, GIGA/UNICEF, USAID
    # contractors, etc.) almost never say "hybrid" or "onsite" — they use
    # duty-station and work-authorization language instead. Added after
    # BRAC (Bangladesh-headquartered, country-office postings) and a
    # UNICEF/ITU Giga role both sailed through the filter untouched.
    "non-US location required": [
        r"\bduty station\b", r"\blocal(?:ly)?\s*hired?\b",
        r"\bmust be (?:based|located|resident)\s+in\b(?!.{0,25}\bunited states\b)",
        r"\bwork permit (?:for|in)\b", r"\bnational of\b.{0,20}\bcountry\b",
        r"\bright to work in\b(?!.{0,25}\bunited states\b)",
        r"\bcandidates? (?:must|should) reside in\b(?!.{0,25}\bunited states\b)",
    ],
}

# Organizations whose open roles are structurally non-remote-US even when
# the aggregator source doesn't say so (regional/country-office hires,
# in-country field roles, etc). Checked against the job's company field.
# Add to this list as new repeat offenders show up.
COMPANY_HARD_BLOCKLIST = [
    "brac", "giga", "unicef", "chemonics", "rti international",
    "fhi 360", "forest service international foundation",
]

def scan_for_hard_disqualifiers(jd_text):
    """Returns list of triggered category names, empty if none."""
    hits = []
    lowered = (jd_text or "").lower()
    for category, patterns in HARD_DISQUALIFIER_PATTERNS.items():
        if any(re.search(p, lowered, re.IGNORECASE) for p in patterns):
            hits.append(category)
    return hits

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
  "salary_source": "confirmed | estimated",
  "next_step": "one specific action",
  "puzzle_fit": true or false,
  "environment_flags": ["list any: small-org, change-management-heavy, ownership-language, large-org-risk"],
  "portfolio_piece": "name of the single best-matching project below, or 'none strongly applicable'"
}}

Jeff's portfolio projects (for the portfolio_piece field):
{_PORTFOLIO_PROJECTS_TEXT}

From this list, choose the single project that would most strengthen an
application for this specific role, based on lane and theme match. If none
are a reasonably strong fit, set portfolio_piece to exactly
"none strongly applicable" rather than picking a weak match. Use the
project's "name" field verbatim — never invent a project or leave this
field as a template placeholder.

For salary_source: set it to "confirmed" ONLY if a specific salary number
or range appears verbatim in the Description text below. Set it to
"estimated" for anything else — including inferring from title, company,
or market norms, or reading a number from alert metadata that wasn't
itself pulled from the real job description. Do not mark something
confirmed just because you feel confident in the estimate.

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


def build_scored_card_description(job, result):
    """Card body for a job that received a real Claude score."""
    disqualifier_hits = scan_for_hard_disqualifiers(job.get("description", ""))
    warning = (
        f"⚠ POSSIBLE HARD DISQUALIFIER DETECTED: {', '.join(disqualifier_hits)} — "
        f"verify before trusting the score below.\n\n"
        if disqualifier_hits else ""
    )

    salary_source = str(result.get("salary_source", "")).strip().lower()
    if salary_source == "confirmed":
        salary_tag = f"[CONFIRMED — {job.get('url', 'source unavailable')}]"
    else:
        salary_tag = "[ESTIMATED — not verified against a real JD, re-check before relying on it]"

    return f"""{warning}**Source:** {job['source']}
**URL:** {job['url']}
**Found:** {datetime.now().strftime('%Y-%m-%d')}

---

**Verdict:** {result.get('verdict', '?')} | **Score:** {result.get('score', '?')}/100
**Lane:** {result.get('lane', '?')}
**Mission fit:** {result.get('mission_fit', '?')}
**Salary ask:** {result.get('salary_ask', '?')}  {salary_tag}

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
**Portfolio piece:** {result.get('portfolio_piece') or 'none strongly applicable'}"""

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
        "SaaS operations", "implementation manager",
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
                location_el = card.select_one(".location, [class*='location'], [class*='Location']")
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

                # Don't just trust the site's own "remote jobs" branding —
                # read the card's actual location text if present. Only
                # fall back to "Remote" (this site's default assumption)
                # when no location element is found at all. This is what
                # would have caught the dcbel listing (Montreal, Canada)
                # that got labeled "Remote" purely because of the source.
                location = location_el.get_text(strip=True) if location_el else "Remote"

                jobs.append({
                    "company":     company,
                    "title":       title,
                    "url":         href,
                    "location":    location,
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


# A recurring subset of FFWD postings (FFWD runs on Greenhouse) come through
# the search-results card scrape with a blank/"Unknown" company name. The
# URL embeds the company slug reliably; the Greenhouse-generated <title> tag
# ("Job Application for {Role} at {Company}") is a fallback/cross-check.
#
# FFWD's own slugs carry an internal multi-board disambiguation suffix
# ("-org-2", trailing "-2", etc.) that has to be stripped before the name is
# ever stored or compared — left in place, it silently defeats fuzzy
# matching against cards that were named cleanly from another source (see
# clean_company_name(), used everywhere company names get compared).
KNOWN_NAME_ALIASES = {
    "codepath": "CodePath",
    "digital nest": "Digital Nest",
    "betanyc": "BetaNYC",
}

def clean_ffwd_company_name(raw_name):
    """Strips FFWD's internal disambiguation suffix ('... Org N' or trailing
    ' N'). NOTE: 'Org' here is very likely a slugified '.org' TLD fragment
    (e.g. codepath.org -> 'codepath-org') combined with FFWD's own
    disambiguation index, not a generic word FFWD inserts. Stripping both
    together still produces the correct bare name in every case observed
    so far, but if a future company's genuine name ends in the literal word
    'Org', this would incorrectly truncate it — worth a spot-check if that
    ever looks wrong."""
    cleaned = re.sub(r"\s+Org\s+\d+$", "", raw_name or "", flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+\d+$", "", cleaned)
    return cleaned.strip()

def apply_known_alias(cleaned_name):
    return KNOWN_NAME_ALIASES.get(cleaned_name.lower(), cleaned_name)

def clean_company_name(name):
    """
    Full cleanup pipeline for a company name: strip FFWD's disambiguation
    suffix, then apply known stylized-capitalization aliases. Safe to run
    on any company name, not just FFWD-sourced ones — a name with no
    matching suffix passes through unchanged.
    """
    return apply_known_alias(clean_ffwd_company_name(name))

def extract_company_from_ffwd_url(url):
    """Pulls the company slug out of an FFWD URL. Handles both slug shapes
    observed in the wild: with a trailing UUID (codepath-org-2-50165d4f-...)
    and without one (betanyc-2, givedirectly-3)."""
    match = re.search(r"/companies/([a-z0-9-]+?)/jobs/", url)
    if not match:
        return None
    slug = match.group(1)
    # Strip a trailing UUID segment if present (8-4-4-4-12 hex, hyphenated)
    slug = re.sub(
        r"-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        "", slug
    )
    humanized = " ".join(word.capitalize() for word in slug.split("-"))
    return clean_company_name(humanized)

def extract_company_from_title(page_title):
    """Tries both known title formats: Greenhouse-hosted pages
    ("Job Application for X at Y") and FFWD's own native template
    ("Y - Tech Nonprofit Job Board")."""
    if not page_title:
        return None

    match = re.search(r"Job Application for .+ at (.+)$", page_title)
    if match:
        return clean_company_name(match.group(1).strip())

    match = re.search(r"^(.+?) - Tech Nonprofit Job Board$", page_title)
    if match:
        return clean_company_name(match.group(1).strip())

    return None

def backfill_ffwd_company(job):
    """
    If the listing-card scrape came back without a usable company name,
    recover it from the URL slug (primary — doesn't depend on page
    metadata, can't fail silently) or the page's <title> tag (fallback).

    Known gap, not fixed here: some FFWD listings (confirmed: BRAC's
    "Deputy Manager, Sub-Grants (SHIFT)") redirect straight to an external
    career site (e.g. careers.brac.net/...) rather than a jobs.ffwd.org
    URL. Neither method below can recover a company name in that case —
    there's no FFWD company-slug structure to parse and no FFWD-hosted
    page to fetch a <title> from. The real fix would need to happen
    upstream in crawl_ffwd(), reading the company name directly off the
    search-results-page listing card before the external link is ever
    followed. Tracked here, not fixed — needs its own look at that page's
    HTML structure first.
    """
    if job.get("company") and job["company"] not in ("", "Unknown", "See posting"):
        return job

    company = extract_company_from_ffwd_url(job.get("url", ""))

    if not company:
        r = safe_get(job["url"], timeout=20)
        if r:
            soup = BeautifulSoup(r.text, "html.parser")
            if soup.title and soup.title.string:
                company = extract_company_from_title(soup.title.string.strip())

    if company:
        log.info(f"  Backfilled FFWD company name: '{company}'")
        job["company"] = company

    return job


# NOTE on FFWD coverage: this crawl only ever sees the first ~20
# server-rendered results per query — FFWD's search page is a Getro-powered
# Next.js app that reports a much larger true total (initialState.jobs.total
# in its embedded page state) but only paginates the rest via a client-side
# "Load more" button; `?page=2` is silently ignored. A posting that doesn't
# rank in that first ~20 for any of our search terms can sit invisible for
# weeks. A headless-browser fallback was tried and reverted — it tripped
# what looks like FFWD's bot detection after a handful of automated
# requests (deterministic "Download is starting" failures, even on fresh
# queries and the bare homepage). Left as a known gap; see chat history
# 2026-07-16 for the investigation.

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
                location_el = card.select_one("[class*='location'], [class*='Location']")
                link_el = card.select_one("a") if card.name != "a" else card

                title = title_el.get_text(strip=True) if title_el else ""
                company = company_el.get_text(strip=True) if company_el else "Unknown"
                location = location_el.get_text(strip=True) if location_el else "See posting"
                href = link_el.get("href", "") if link_el else ""

                if not href or not title:
                    continue
                if not href.startswith("http"):
                    href = base_url + href
                if href in seen_urls:
                    continue
                seen_urls.add(href)

                job = {
                    "company":     company,
                    "title":       title,
                    "url":         href,
                    "location":    location,
                    "salary":      "Not listed",
                    "description": card.get_text(separator=" ", strip=True)[:2000],
                    "source":      "FFWD Jobs",
                }
                job = backfill_ffwd_company(job)
                jobs.append(job)

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
# DESCRIPTION COMPLETENESS GATE
# Gmail-alert jobs (esp. LinkedIn) frequently resolve to a login-walled
# or empty description. Scoring those anyway produces a confident-looking
# number with no real signal behind it. Gate blocks scoring in that case;
# caller creates a "score withheld" card instead.
# ─────────────────────────────────────────────

DESCRIPTION_BLOCKED_MARKERS = [
    "sign in to view", "login wall", "you must be signed in",
    "javascript to run this app", "we cannot provide a description",
]

# Common ATS URL patterns, tried as a single fallback fetch when the
# primary posting URL is blocked/thin and a company name is available.
FALLBACK_ATS_URL_PATTERNS = [
    "https://boards.greenhouse.io/{slug}",
    "https://jobs.ashbyhq.com/{slug}",
    "https://jobs.lever.co/{slug}",
]


def has_sufficient_description(raw_description):
    """Return False if the description is too thin/blocked to score responsibly."""
    if not raw_description or len(raw_description.strip()) < 300:
        return False
    lowered = raw_description.lower()
    return not any(marker in lowered for marker in DESCRIPTION_BLOCKED_MARKERS)


def company_slug(company):
    """Best-effort URL slug guess from a company name, for ATS fallback lookups."""
    return re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")


def attempt_fallback_description(job):
    """
    One fallback attempt when the primary description is blocked/thin:
    try common ATS URL patterns derived from the company name. Returns
    the job dict, updated in place if a usable description was found.
    """
    slug = company_slug(job.get("company", ""))
    if not slug:
        return job

    for pattern in FALLBACK_ATS_URL_PATTERNS:
        url = pattern.format(slug=slug)
        r = safe_get(url, timeout=20)
        if not r:
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["nav", "header", "footer", "script", "style", "aside"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)

        if has_sufficient_description(text):
            job["description"] = text[:4000]
            log.info(f"  Fallback description fetched from {url}")
            return job

    return job


def build_withheld_card_description(job):
    """Card body for a job whose description was too thin/blocked to score."""
    return f"""**Source:** {job['source']}
**URL:** {job['url']}
**Found:** {datetime.now().strftime('%Y-%m-%d')}

---

**Verdict:** Score withheld — description blocked
**Score:** —
**Salary:** {job.get('salary', 'Not listed')}
**Location/Remote:** {job.get('location', 'Not specified')}

**Next step:**
Pull the full JD directly before this can be scored or applied to."""


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


def get_email_body(service, msg_id, max_length=6000):
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

        return text[:max_length] if text else msg.get("snippet", "")

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


def extract_reconciliation_info(service, email):
    """
    Used by the Gmail-Trello reconciliation job, where the company isn't
    already known from a Trello card. One Claude call both identifies the
    company/role this thread is about and classifies any status change,
    so a thread that slipped past the per-company scan can still be
    reconciled against the board.
    """
    client = anthropic.Anthropic(api_key=CONFIG["anthropic_api_key"])

    body = get_email_body(service, email["id"])
    content_for_claude = body if body else email["snippet"]

    prompt = f"""This email may be related to a job application. Identify what it's about.

From: {email['from']}
Subject: {email['subject']}
Content: {content_for_claude[:3000]}

Respond ONLY in valid JSON:
{{
  "is_job_related": true or false,
  "company": "company name, or null if not identifiable",
  "title": "job title, or null if not identifiable",
  "status_change": "interview_scheduled | rejected | offer | info_requested | application_received | no_change",
  "confidence": "high | medium | low",
  "summary": "one sentence about what this email means",
  "suggested_trello_list": "Interview | Rejected | Closed | Applied | null"
}}

If this email is not related to a specific job application (a job-alert
digest, newsletter, spam, etc.), set is_job_related to false and leave
company/title as null."""

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
        log.warning(f"    Could not extract reconciliation info: {e}")
        return None


def match_target_list(suggested_list, list_map):
    """
    Case-insensitive, whitespace-tolerant match of a suggested list name
    against the board's real lists, with a fuzzy substring fallback for
    near-misses (e.g. "Interviewing" vs "Interview").
    Returns (list_id, list_name), or (None, None) if nothing matched.
    """
    if not suggested_list:
        return None, None
    normalized_suggestion = suggested_list.strip().lower()

    for list_name, list_id in list_map.items():
        if list_name.strip().lower() == normalized_suggestion:
            return list_id, list_name

    for list_name, list_id in list_map.items():
        if (normalized_suggestion in list_name.strip().lower()
                or list_name.strip().lower() in normalized_suggestion):
            log.info(f"    Fuzzy-matched '{suggested_list}' → '{list_name}'")
            return list_id, list_name

    return None, None


# ─────────────────────────────────────────────
# DAYS-TO-REJECTION TRACKING
# When a rejection moves a card, look up how long it took from the
# original application-confirmation email — minutes/hours reads as an
# ATS keyword auto-reject, days reads as an actual human pass.
# ─────────────────────────────────────────────

APPLICATION_CONFIRMATION_SUBJECT_MARKERS = [
    "application received", "thank you for applying", "thanks for applying",
    "we received your application", "your application to", "application confirmation",
]

def find_application_date(service, company, lookback_days=180):
    """
    Best-effort search for the original application-confirmation email for
    a company. Returns the earliest matching email's timestamp, or None.
    """
    query = f'"{company}" newer_than:{lookback_days}d'
    emails = search_gmail(service, query, max_results=20)

    candidates = []
    for e in emails:
        subject_lower = e.get("subject", "").lower()
        if any(marker in subject_lower for marker in APPLICATION_CONFIRMATION_SUBJECT_MARKERS):
            try:
                candidates.append(parsedate_to_datetime(e["date"]))
            except (TypeError, ValueError):
                continue

    return min(candidates) if candidates else None

def append_turnaround_note(card_id, applied_date, rejected_date):
    """Appends a rejection-speed note to a card's description."""
    delta = rejected_date - applied_date
    hours = delta.total_seconds() / 3600
    days = delta.days

    if hours < 0:
        return  # rejection predates the application email we found — not trustworthy

    if hours < 24:
        speed_note = f"Rejected in {hours:.1f} hours — likely automated/ATS filter."
    elif days <= 7:
        speed_note = f"Rejected in {days} days — likely a real (if brisk) human review."
    else:
        speed_note = f"Rejected in {days} days — extended review process."

    try:
        current_desc = get_card_description(card_id)
        update_card_description(card_id, f"{current_desc}\n\n**Turnaround:** {speed_note}")
        log.info(f"    Turnaround note added: {speed_note}")
    except Exception as e:
        log.error(f"    Failed to append turnaround note: {e}")


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
    skipped_cached = 0

    # Load seen emails cache — emails already classified won't be re-sent to Claude
    seen_emails = load_seen_emails()
    log.info(f"Seen emails cache: {len(seen_emails)} entries")

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
            # Skip if already classified in a previous run
            if email['id'] in seen_emails:
                log.info(
                    f"    [{company}] Skipping cached email: '{email['subject']}' "
                    f"(previously classified as {seen_emails[email['id']].get('status_change', 'unknown')})"
                )
                skipped_cached += 1
                continue

            classification = classify_email_with_claude(service, email, company)
            if not classification:
                log.warning(f"    [{company}] Classification failed for: {email['subject']}")
                continue

            # Mark email as seen so it's never re-classified
            mark_email_seen(seen_emails, email['id'], company, classification)

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

            # Find the target list ID — case-insensitive, whitespace-tolerant,
            # fuzzy-fallback match (e.g. "Interviewing" vs "Interview")
            target_list_id, matched_list_name = match_target_list(suggested_list, list_map)

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

                # Rejections: look up the original application email and
                # log how fast it came back (ATS-speed vs human review)
                if matched_list_name == CONFIG["trello_lists"]["rejected"]:
                    applied_date = find_application_date(service, company)
                    if applied_date:
                        try:
                            rejected_date = parsedate_to_datetime(email["date"])
                        except (TypeError, ValueError):
                            rejected_date = datetime.now(timezone.utc)
                        append_turnaround_note(card["id"], applied_date, rejected_date)
                    else:
                        log.info(f"    [{company}] Could not find original application email — skipping turnaround note")
            except Exception as e:
                log.error(f"  Failed to move card for {company}: {e}")

        time.sleep(0.5)  # Rate limiting

    # Save seen emails cache
    save_seen_emails(seen_emails)

    log.info(
        f"\nPipeline scan complete. Checked {checked} companies, "
        f"moved {moved} cards, skipped {skipped_cached} cached emails."
    )

    # ── Idealist job alert emails ──
    # Parse any Idealist alert emails and create Trello cards for new matches
    seen = load_seen_jobs()
    watching_list_id = list_map.get(CONFIG["trello_lists"]["watching"])
    if watching_list_id:
        # Fetched once and shared across all three alert sources below so a
        # repost that shows up in, say, both LinkedIn and Built In on the
        # same day is only ever created as one card.
        existing_cards = get_cards_for_duplicate_check(list_map)

        idealist_cards = run_gmail_scan_idealist(service, seen, list_map, watching_list_id, existing_cards)
        save_seen_jobs(seen)
        if idealist_cards:
            log.info(f"  {idealist_cards} new Idealist card(s) added to Watching")

        if CONFIG["enable_linkedin_alerts"]:
            linkedin_cards = run_gmail_scan_linkedin(service, seen, list_map, watching_list_id, existing_cards)
            save_seen_jobs(seen)
            if linkedin_cards:
                log.info(f"  {linkedin_cards} new LinkedIn card(s) added to Watching")
        else:
            log.info("  LinkedIn alert scan disabled (CONFIG['enable_linkedin_alerts'] = False) — skipping")

        if CONFIG["enable_builtin_alerts"]:
            builtin_cards = run_gmail_scan_builtin(service, seen, list_map, watching_list_id, existing_cards)
            save_seen_jobs(seen)
            if builtin_cards:
                log.info(f"  {builtin_cards} new Built In card(s) added to Watching")
        else:
            log.info("  Built In alert scan disabled (CONFIG['enable_builtin_alerts'] = False) — skipping")
    else:
        log.warning("Could not find Watching list — skipping alert-email Gmail scans")

    # ── Gmail-Trello reconciliation ──
    # Catches threads the per-company scan above misses entirely (it only
    # searches from existing card names outward)
    run_gmail_trello_reconciliation(service, list_map)


def run_gmail_trello_reconciliation(service, list_map):
    """
    Searches broadly across known ATS senders/subjects instead of starting
    from existing Trello cards, to catch threads the per-company scan in
    run_gmail_scan() misses entirely. Never auto-creates a card for an
    unmatched thread — it only flags orphan candidates for Jeff to confirm,
    since backfilling wrong details is worse than not backfilling. A
    matched thread whose status doesn't match the card's current list gets
    auto-moved, same as the per-company sync (that part's already reliable
    — the gap is missed threads, not incorrect moves).
    """
    log.info("=" * 50)
    log.info("GMAIL-TRELLO RECONCILIATION")
    log.info("=" * 50)

    lookback = CONFIG["reconciliation_lookback_days"]
    query = (
        "(from:(greenhouse-mail.io OR applytojob.com OR ashbyhq.com OR "
        "myworkday.com OR rippling.com) OR "
        'subject:(application OR interview OR "thank you for applying")) '
        f"newer_than:{lookback}d"
    )
    emails = search_gmail(service, query, max_results=100)

    if not emails:
        log.info("  No matching threads found.")
        return

    log.info(f"  Found {len(emails)} candidate thread(s) in the last {lookback} days")

    seen_reconciliation = load_seen_reconciliation_emails()
    orphan_candidates = load_orphan_candidates()
    all_cards = get_all_board_cards(list_map)

    scanned = 0
    new_orphans = 0
    corrected = 0

    for email in emails:
        if email["id"] in seen_reconciliation:
            continue
        scanned += 1

        info = extract_reconciliation_info(service, email)
        if not info:
            seen_reconciliation[email["id"]] = {"failed": True, "date": datetime.now().isoformat()}
            continue

        if not info.get("is_job_related") or not info.get("company"):
            seen_reconciliation[email["id"]] = {"not_job_related": True, "date": datetime.now().isoformat()}
            continue

        company = info["company"].strip()
        title = (info.get("title") or "").strip()

        match = is_likely_duplicate(company, title, all_cards)

        if not match:
            orphan_candidates[email["id"]] = {
                "company": company,
                "title":   title,
                "subject": email["subject"],
                "from":    email["from"],
                "date":    email["date"],
                "summary": info.get("summary", ""),
                "flagged": datetime.now().isoformat(),
            }
            new_orphans += 1
            log.warning(
                f"    ORPHAN CANDIDATE: '{company} — {title}' "
                f"('{email['subject']}') has no matching Trello card"
            )
            seen_reconciliation[email["id"]] = {"orphan": True, "date": datetime.now().isoformat()}
            continue

        # Matched an existing card — check for a stale-state mismatch
        seen_reconciliation[email["id"]] = {
            "matched_card_id": match["card_id"],
            "date":            datetime.now().isoformat(),
        }

        if info.get("confidence") == "low":
            continue

        suggested_list = info.get("suggested_trello_list")
        if not suggested_list or str(suggested_list).lower() == "null":
            continue

        target_list_id, matched_list_name = match_target_list(suggested_list, list_map)
        if not target_list_id or matched_list_name == match["list_name"]:
            continue

        try:
            move_trello_card(match["card_id"], target_list_id)
            comment = (
                f"Auto-moved by reconciliation sync on {datetime.now().strftime('%Y-%m-%d')}\n\n"
                f"Email: {email['subject']}\n"
                f"From: {email['from']}\n"
                f"Summary: {info.get('summary', '')}"
            )
            add_comment_to_card(match["card_id"], comment)
            log.info(
                f"  ✓ Reconciliation moved '{match['company']} — {match['title']}' → "
                f"{matched_list_name} ({info.get('summary', '')})"
            )
            corrected += 1

            if matched_list_name == CONFIG["trello_lists"]["rejected"]:
                applied_date = find_application_date(service, company)
                if applied_date:
                    try:
                        rejected_date = parsedate_to_datetime(email["date"])
                    except (TypeError, ValueError):
                        rejected_date = datetime.now(timezone.utc)
                    append_turnaround_note(match["card_id"], applied_date, rejected_date)
        except Exception as e:
            log.error(f"  Failed to move card for reconciliation match '{company}': {e}")

    save_seen_reconciliation_emails(seen_reconciliation)
    save_orphan_candidates(orphan_candidates)

    log.info(
        f"\nReconciliation complete. Scanned {scanned} new thread(s), "
        f"{new_orphans} new orphan candidate(s) flagged "
        f"({len(orphan_candidates)} pending review total), "
        f"{corrected} stale-state mismatch(es) corrected."
    )
    if orphan_candidates:
        log.info(
            f"  Review pending orphan candidates in "
            f"{CONFIG['orphan_candidates_file']} before creating any cards."
        )


# ─────────────────────────────────────────────
# IDEALIST DIGEST SECTION SPLITTING
# Idealist digests bundle multiple saved-search sections into one email,
# each fully spelled out inline with its own "N new results found for
# this search" count marker. A single whole-body extraction call was
# silently discarding most of a large digest — both because the fetched
# body itself was truncated (see IDEALIST_EMAIL_MAX_CHARS below) and
# because one Claude call trying to list everything at once could get
# cut off mid-output. Splitting per-section lets each chunk get its own
# small, bounded extraction call instead.
# ─────────────────────────────────────────────

# Observed real digest: ~43,000 chars for 69 listings (~623 chars/listing,
# including a long tracking-redirect URL per listing). 80,000 gives
# headroom for 100+ listings in a single digest.
IDEALIST_EMAIL_MAX_CHARS = 80_000

_IDEALIST_SECTION_PATTERN = re.compile(
    r'Here are your new updates for "(.+?)" jobs:(.*?)(\d+) new results? found for this search',
    re.DOTALL
)

def split_into_search_sections(email_body):
    """
    Splits an Idealist digest into per-saved-search chunks.
    Returns [] if the body doesn't match Idealist's standard digest
    template — caller falls back to whole-body extraction in that case.
    """
    sections = []
    for match in _IDEALIST_SECTION_PATTERN.finditer(email_body):
        search_name, raw_text, stated_count = match.groups()
        sections.append({
            "search_name": search_name.strip(),
            "raw_text": raw_text.strip(),
            "stated_count": int(stated_count),
        })
    return sections

def validate_extraction_completeness(listings, stated_count, search_name):
    """
    Compares extracted count against Idealist's own stated count for the
    section — a free correctness check the digest format hands us.
    Always logs on mismatch so truncation is loud, never silent.
    """
    if len(listings) != stated_count:
        log.warning(
            f"  Idealist section '{search_name}': extracted {len(listings)} "
            f"listing(s) but Idealist states {stated_count} — possible truncation/omission"
        )
        return False
    return True

def extract_idealist_section_listings(client, subject, section):
    """
    Extracts listings from one Idealist digest section. Tells Claude the
    expected count upfront (stronger than only checking after the fact).
    Retries ONCE with an explicit "you missed some" instruction if the
    count doesn't match; if still short after the retry, logs and
    proceeds with whatever was extracted — no unbounded retry loop.
    """
    search_name = section["search_name"]
    raw_text = section["raw_text"]
    stated_count = section["stated_count"]

    if stated_count == 0:
        return []  # nothing to extract, skip the call entirely

    prompt = f"""Extract job listings from this section of an Idealist job alert digest email.

Digest subject: {subject}
Saved search name: "{search_name}"
This section contains exactly {stated_count} job listing(s). Extract ALL {stated_count} of them — do not stop early or summarize.

Section content:
{raw_text}

Return ONLY a JSON array of job objects, with exactly {stated_count} entries.
Each object should have:
{{"title": "job title", "company": "organization name", "url": "job URL if visible or empty string"}}

Return only the JSON array, no other text."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=6000,
            messages=[{"role": "user", "content": prompt}],
        )
        listings = safe_parse_json_list(message.content[0].text.strip())
    except Exception as e:
        log.warning(f"  Could not parse Idealist section '{search_name}': {e}")
        return []

    if validate_extraction_completeness(listings, stated_count, search_name):
        return listings

    retry_prompt = f"""Your previous extraction of the "{search_name}" section returned {len(listings)} listing(s), but this section contains exactly {stated_count} listings — you missed some.

Section content:
{raw_text}

Extract EVERY SINGLE job listing in this section. There should be exactly {stated_count} objects in your output. Double-check you have not skipped or merged any listings.

Return ONLY a JSON array of job objects:
{{"title": "job title", "company": "organization name", "url": "job URL if visible or empty string"}}

Return only the JSON array, no other text."""

    try:
        retry_message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=6000,
            messages=[{"role": "user", "content": retry_prompt}],
        )
        retry_listings = safe_parse_json_list(retry_message.content[0].text.strip())
    except Exception as e:
        log.warning(f"  Retry failed for Idealist section '{search_name}': {e}")
        return listings  # keep the original partial result

    validate_extraction_completeness(retry_listings, stated_count, search_name)  # logs again if still short
    return retry_listings


def run_gmail_scan_idealist(service, seen, list_map, watching_list_id, existing_cards):
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
        # Fetch the full email body — Idealist digests bundle multiple
        # saved-search sections into one email, so a much higher cap than
        # the default is used here (see IDEALIST_EMAIL_MAX_CHARS)
        body = get_email_body(service, email['id'], max_length=IDEALIST_EMAIL_MAX_CHARS)
        content_for_claude = body if body else email['snippet']

        sections = split_into_search_sections(content_for_claude)

        listings = []
        if sections:
            log.info(f"  Digest split into {len(sections)} saved-search section(s)")
            for section in sections:
                listings.extend(extract_idealist_section_listings(client, email['subject'], section))
        else:
            # Fallback: unrecognized digest format — whole-body extraction,
            # with a raised max_tokens as cheap insurance even though this
            # is no longer the primary path
            log.info("  Digest did not match expected section format — using whole-body fallback extraction")
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
                    max_tokens=2000,
                    messages=[{"role": "user", "content": prompt}],
                )
                listings = safe_parse_json_list(message.content[0].text.strip())
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

            # Description completeness gate — don't let a blocked/thin
            # description produce a confident-looking numeric score
            if not has_sufficient_description(job.get("description", "")):
                job = attempt_fallback_description(job)

            if not has_sufficient_description(job.get("description", "")):
                log.info(f"  Description blocked/thin for {title} at {company} — withholding score")
                card_desc = build_withheld_card_description(job)
                if create_card_or_note_duplicate(
                    existing_cards, watching_list_id, company, title, card_desc, job["source"]
                ):
                    cards_created += 1
                seen[fp] = {
                    "company": company,
                    "title":   title,
                    "verdict": "Score withheld",
                    "date":    datetime.now().isoformat(),
                }
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

            # Create Trello card (checks for duplicates/reposts first)
            card_desc = build_scored_card_description(job, result)
            if create_card_or_note_duplicate(
                existing_cards, watching_list_id, company, title, card_desc, job["source"]
            ):
                cards_created += 1

            time.sleep(1)

    log.info(f"  Idealist Gmail scan complete. {cards_created} card(s) created.")
    return cards_created


def run_gmail_scan_linkedin(service, seen, list_map, watching_list_id, existing_cards):
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

            # Description completeness gate — LinkedIn alerts frequently
            # resolve to a login-walled/empty description. Don't let that
            # produce a confident-looking numeric score.
            if not has_sufficient_description(job.get("description", "")):
                job = attempt_fallback_description(job)

            if not has_sufficient_description(job.get("description", "")):
                log.info(f"  Description blocked/thin for {title} at {company} — withholding score")
                card_desc = build_withheld_card_description(job)
                if create_card_or_note_duplicate(
                    existing_cards, watching_list_id, company, title, card_desc, job["source"]
                ):
                    cards_created += 1
                seen[fp] = {
                    "company": company,
                    "title":   title,
                    "verdict": "Score withheld",
                    "date":    datetime.now().isoformat(),
                }
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

            card_desc = build_scored_card_description(job, result)
            if create_card_or_note_duplicate(
                existing_cards, watching_list_id, company, title, card_desc, job["source"]
            ):
                cards_created += 1

            time.sleep(1)

    log.info(f"  LinkedIn Gmail scan complete. {cards_created} card(s) created.")
    return cards_created


def run_gmail_scan_builtin(service, seen, list_map, watching_list_id, existing_cards):
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

            # Description completeness gate — don't let a blocked/thin
            # description produce a confident-looking numeric score
            if not has_sufficient_description(job.get("description", "")):
                job = attempt_fallback_description(job)

            if not has_sufficient_description(job.get("description", "")):
                log.info(f"  Description blocked/thin for {title} at {company} — withholding score")
                card_desc = build_withheld_card_description(job)
                if create_card_or_note_duplicate(
                    existing_cards, watching_list_id, company, title, card_desc, job["source"]
                ):
                    cards_created += 1
                seen[fp] = {
                    "company": company,
                    "title":   title,
                    "verdict": "Score withheld",
                    "date":    datetime.now().isoformat(),
                }
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

            card_desc = build_scored_card_description(job, result)
            if create_card_or_note_duplicate(
                existing_cards, watching_list_id, company, title, card_desc, job["source"]
            ):
                cards_created += 1

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

    # Existing pipeline cards, for fuzzy duplicate/repost detection before
    # any new card gets created
    existing_cards = get_cards_for_duplicate_check(list_map)

    # Run all crawlers
    all_jobs = []
    all_jobs.extend(crawl_idealist())
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
    withheld = 0
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

        # Enrich description if short
        job = enrich_job_description(job)

        # Description completeness gate — don't let a blocked/thin
        # description produce a confident-looking numeric score
        if not has_sufficient_description(job.get("description", "")):
            job = attempt_fallback_description(job)

        if not has_sufficient_description(job.get("description", "")):
            log.info(f"  Description blocked/thin — withholding score")
            card_desc = build_withheld_card_description(job)
            if create_card_or_note_duplicate(
                existing_cards, watching_list_id, job["company"], job["title"], card_desc, job["source"]
            ):
                cards_created += 1
            seen[fp] = {
                "company": job["company"],
                "title":   job["title"],
                "verdict": "Score withheld",
                "date":    datetime.now().isoformat(),
            }
            withheld += 1
            if cards_created % 5 == 0:
                save_seen_jobs(seen)
            time.sleep(1)
            continue

        log.info(f"  Sending to Claude ({reason})...")

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

        # Build Trello card (checks for duplicates/reposts first)
        card_desc = build_scored_card_description(job, result)
        if create_card_or_note_duplicate(
            existing_cards, watching_list_id, job["company"], job["title"], card_desc, job["source"]
        ):
            cards_created += 1

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
    log.info(f"  Score withheld:   {withheld}  (blocked/thin description)")
    log.info(f"  Sent to Claude:   {len(new_jobs) - pre_filtered - withheld}")
    log.info(f"  Cards created:    {cards_created}")
    log.info(f"  Below threshold:  {cards_skipped}")
    log.info(f"  Errors:           {errors}")
    log.info(f"  Est. API cost:    ~${((len(new_jobs) - pre_filtered - withheld) * 0.003):.3f}")
    log.info("=" * 50)


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Jeff's Job Search Agent")
    parser.add_argument("--crawl",       action="store_true", help="Run job crawl only")
    parser.add_argument("--gmail",       action="store_true", help="Run Gmail scan only")
    parser.add_argument("--reconcile",   action="store_true", help="Run Gmail-Trello reconciliation only")
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
    elif args.reconcile:
        service = get_gmail_service()
        if not service:
            log.error("Could not connect to Gmail. Run --gmail-setup first.")
            return
        list_map = get_trello_lists()
        run_gmail_trello_reconciliation(service, list_map)
    else:
        # Default: run both
        run_job_crawl()
        run_gmail_scan()


if __name__ == "__main__":
    main()


# Setup instructions are in README.md
