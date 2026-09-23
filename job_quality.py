"""Job identity, source evidence, and retry policy (no API calls or credentials)."""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

POLICY_VERSION = "2026-09-evidence-v1"
UNKNOWN_COMPANIES = {"", "unknown", "see posting", "not listed", "n a"}
RETRY_STATES = {"score_failed", "delivery_failed", "description_unavailable", "needs_review", "budget_deferred"}
TRACKING_PARAMETERS = {"gh_src", "source", "ref", "referral", "mc_cid", "mc_eid"}


def normalized(text):
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def known_company(company):
    return normalized(company) not in UNKNOWN_COMPANIES


def contains_term(text, term):
    # 'sales' must never match Salesforce; 'it' must never match benefits.
    return bool(re.search(r"(?<!\w)" + re.escape(term.strip()) + r"(?!\w)", text, re.I))


def canonical_url(url):
    try:
        parts = urlsplit((url or "").strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return ""
        # General job directories are not posting identities.
        if parts.path.rstrip("/") in {"", "/jobs", "/en/jobs", "/en"}:
            return ""
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMETERS]
        return urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/"),
                           urlencode(sorted(query)), ""))
    except ValueError:
        return ""


def posting_key(job):
    url = canonical_url(job.get("url"))
    if url:
        # Idealist slugs can change while the listing ID remains stable.
        match = re.search(r"/en/(?:nonprofit-job|job)/([a-f0-9]{32})", url)
        identity = "idealist:" + match[1] if match else url
    else:
        identity = normalized(job.get("company")) + "|" + normalized(job.get("title"))
    return "v2:" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def legacy_key(job):
    raw = f"{job.get('company', '').lower().strip()}|{job.get('title', '').lower().strip()}"
    return hashlib.md5(raw.encode()).hexdigest()


def titles_match(first, second):
    def clean(title):
        return normalized(re.sub(r"\((?:remote|hybrid)\)|\[.*?\]", "", title or "", flags=re.I))
    a, b = clean(first), clean(second)
    if not a or not b:
        return False
    return a == b or (len(a.split()) == len(b.split()) and
                     SequenceMatcher(None, a, b).ratio() >= .96)


def find_duplicate(job, cards, clean_company=lambda value: value):
    url = canonical_url(job.get("url"))
    company = normalized(clean_company(job.get("company", "")))
    matches = []
    for card in cards:
        if url and url == canonical_url(card.get("url")):
            return card
        if (known_company(company) and company == normalized(clean_company(card.get("company", "")))
                and titles_match(job.get("title"), card.get("title"))):
            matches.append(card)
    # Archived passes take precedence over an accidental duplicate still on the board.
    return next((c for c in matches if c.get("closed")), matches[0] if matches else None)


def utcnow():
    return datetime.now(timezone.utc)


def parse_date(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (ValueError, AttributeError):
        return None


def legacy_retryable(entry, job):
    return (entry.get("scored") is False or entry.get("verdict") == "Score withheld" or
            (entry.get("verdict") == "Pre-filtered" and
             "salesforce" in job.get("title", "").lower()))


def cache_entry(seen, job):
    """Non-destructive lazy migration; preserve every legacy record for audit.

    Never apply an Unknown+title record to a different employer's posting. A
    known legacy role is bound to its first observed URL; subsequent distinct
    postings get their own decisions. Failed/withheld/affected Salesforce
    decisions are eligible for repair on rediscovery.
    """
    key = posting_key(job)
    if key in seen:
        return seen[key]
    old = seen.get(legacy_key(job))
    if not old or not known_company(job.get("company")) or legacy_retryable(old, job):
        return None
    if old.get("migrated_to") not in (None, key):
        return None
    old["migrated_to"] = key
    seen[key] = {**old, "status": "legacy_completed", "legacy_key": legacy_key(job),
                 "url": job.get("url", "")}
    return seen[key]


def should_process(seen, job, now=None):
    entry = cache_entry(seen, job)
    if not entry:
        return True
    if entry.get("status") not in RETRY_STATES:
        return False
    retry = parse_date(entry.get("retry_after"))
    return retry is None or (now or utcnow()) >= retry


def record_decision(seen, job, status, reason, result=None, card_id=None):
    key = posting_key(job)
    previous = seen.get(key, {})
    attempts = previous.get("attempts", 0) + 1
    now = utcnow()
    entry = {
        "company": job.get("company", ""), "title": job.get("title", ""),
        "url": job.get("url", ""), "source": job.get("source", ""),
        "date": now.isoformat(), "status": status, "reason": reason,
        "policy_version": POLICY_VERSION, "attempts": attempts,
        "description_source": job.get("description_source"),
        "description_sha256": hashlib.sha256(job.get("description", "").encode()).hexdigest(),
    }
    if result:
        entry.update(score=result["score"], verdict=result["verdict"], result=result)
    if card_id:
        entry["card_id"] = card_id
    if status in RETRY_STATES:
        days = 1 if status in {"score_failed", "delivery_failed", "budget_deferred"} else min(2 ** (attempts - 1), 14)
        entry["retry_after"] = (now + timedelta(days=days)).isoformat()
        # Persist enough input to retry even when a digest falls outside lookback.
        entry["job"] = {k: v for k, v in job.items() if k != "description"}
        if status == "delivery_failed":
            entry["job"]["description"] = job.get("description", "")
    seen[key] = entry
    return entry


def atomic_json(path, data):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def plain_text(html):
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


def sufficient_description(text):
    bad = ("sign in to view", "you must be signed in", "javascript to run this app",
           "we cannot provide a description", "verify you are human", "access denied")
    return bool(text and len(text.strip()) >= 300 and not any(x in text.lower() for x in bad))


def _json_objects(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            if isinstance(item, (list, dict)):
                yield from _json_objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_objects(item)


def extract_posting(html, job, final_url):
    """Prefer JobPosting JSON-LD. Do not confuse a careers directory with a JD."""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            objects = _json_objects(json.loads(script.string or script.get_text()))
            for obj in objects:
                kind = obj.get("@type", [])
                if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
                    candidates.append(obj)
        except (ValueError, TypeError):
            continue
    for obj in candidates:
        if not titles_match(job.get("title"), obj.get("title")):
            continue
        org = obj.get("hiringOrganization") or {}
        company = org.get("name", "") if isinstance(org, dict) else ""
        text = plain_text(obj.get("description", ""))
        # Idealist puts benefits/location in separate page sections, outside JSON-LD.
        for heading in soup.select("h2"):
            if heading.get_text(strip=True).lower() in {"benefits", "location"}:
                text += "\n" + heading.parent.get_text(" ", strip=True)
        if not sufficient_description(text):
            continue
        expired = parse_date(obj.get("validThrough"))
        location = json.dumps({k: obj[k] for k in ("jobLocation", "jobLocationType", "applicantLocationRequirements") if k in obj}, ensure_ascii=False)
        return {"description": text, "company": company or job.get("company", ""),
                "location": location if location != "{}" else "Not specified",
                "salary": json.dumps(obj["baseSalary"], ensure_ascii=False) if obj.get("baseSalary") else job.get("salary", "Not listed"),
                "description_verified": True, "description_source": final_url,
                "expired": bool(expired and expired < utcnow()), "extraction_method": "JobPosting JSON-LD"}
    # Never extract the entire page if its structured jobs did not match.
    if candidates:
        return None
    heading = soup.select_one("h1")
    if not heading or not titles_match(job.get("title"), heading.get_text(" ", strip=True)):
        return None
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    main = soup.select_one("main, article, [class*='job-description'], [class*='job_description'], #content")
    text = main.get_text(" ", strip=True) if main else ""
    if not sufficient_description(text):
        return None
    closed = bool(re.search(r"(?:this (?:job|position|role) (?:is|has been) (?:now )?(?:closed|filled)|no longer accepting applications)", text, re.I))
    return {"description": text, "description_verified": True, "description_source": final_url,
            "expired": closed, "extraction_method": "matching job heading"}


ELIGIBILITY_CRITERIA = ("remote", "travel", "schedule", "work", "credentials")


def validate_score(result, job):
    if not isinstance(result, dict):
        raise ValueError("Score response must be an object")
    score = result.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
        raise ValueError("Score must be a number between 0 and 100")
    if type(result.get("disqualified")) is not bool:
        raise ValueError("disqualified must be boolean")
    for field in ("why_it_fits", "concerns", "lane", "mission_fit", "next_step", "salary_ask", "salary_source"):
        if not isinstance(result.get(field), str):
            raise ValueError(f"Missing text field: {field}")
    flags = result.get("environment_flags")
    if not isinstance(flags, list) or not all(isinstance(x, str) for x in flags):
        raise ValueError("environment_flags must be a list of strings")
    evidence_text = normalized(job.get("description", "") + " " + job.get("location", "") + " " + job.get("salary", ""))
    eligibility = result.get("eligibility")
    if not isinstance(eligibility, dict):
        raise ValueError("Missing eligibility evidence")
    for criterion in ELIGIBILITY_CRITERIA:
        item = eligibility.get(criterion)
        if not isinstance(item, dict) or item.get("status") not in {"pass", "fail", "unknown"}:
            raise ValueError(f"Invalid eligibility: {criterion}")
        quote = normalized(str(item.get("quote") or ""))
        if item["status"] in {"pass", "fail"} and (len(quote) < 5 or quote not in evidence_text):
            result.setdefault("validation_notes", []).append(f"Unsupported evidence for {criterion}; treated as unknown")
            item.update(status="unknown", quote=None)
    fails = [k for k, v in eligibility.items() if isinstance(v, dict) and v.get("status") == "fail"]
    unsupported_rejection = result["disqualified"] and not fails
    if fails:
        result.update(disqualified=True, score=0, verdict="Skip")
        result["disqualifier_reason"] = "; ".join(fails) or result.get("disqualifier_reason") or "Profile disqualifier"
    else:
        result["disqualified"] = False
        result["verdict"] = "Apply Now" if score >= 85 else "Apply If Interested" if score >= 70 else "Maybe" if score >= 55 else "Skip"
    result["needs_review"] = not result["disqualified"] and (unsupported_rejection or
        eligibility["remote"]["status"] != "pass" or eligibility["work"]["status"] != "pass")
    salary_quote = normalized(str(result.get("salary_evidence") or ""))
    if result["salary_source"] != "confirmed" or not salary_quote or salary_quote not in evidence_text:
        result["salary_source"] = "estimated"
        result["salary_evidence"] = None
    return result
