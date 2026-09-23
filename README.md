# Job Search Agent

A personal job search automation tool I built while actively looking for work in 2026. It crawls mission-aligned job boards, scores each role against my specific profile using the Claude API, creates Trello cards for strong matches, and scans Gmail to automatically move pipeline cards when rejections or interview invites arrive.

I built this because I was spending 2-3 hours a day manually checking job boards, copying listings into a tracker, and trying to remember which applications had heard back. This runs every morning in about 5-10 minutes and handles all of that automatically.

---

## What it does

**Job crawling**
Scrapes three mission-aligned job boards daily and reads Idealist email alerts:
- [Remote Impact](https://remoteimpact.org) — remote roles at impact-driven orgs
- [Tech Jobs for Good](https://techjobsforgood.com) — tech roles at nonprofits and social enterprises
- [FFWD Jobs](https://jobs.ffwd.org) — Fast Forward nonprofit tech job board
- Idealist via Gmail digests, followed by fetching each actual posting
- LinkedIn and Built In alert integrations are available but disabled

**Intelligent scoring**
Each new job is scored 0-100 against a detailed personal profile using Claude (Anthropic's API). The profile encodes:
- Three resume lanes (IT Ops, Technical PM, AI Workflow)
- Hard disqualifiers (sales roles, onsite requirements, crypto, revenue cycle)
- Mission alignment scoring (edtech, civic tech, climate, nonprofits score higher)
- Ownership language signals ("build from scratch," "first hire," "small team" get a boost)
- Company size flags (under 50 = positive signal, over 500 = bureaucracy risk noted)

**Trello integration**
Roles scoring 60+ with verified job descriptions and supported eligibility evidence get a Trello card in Watching. Incomplete or ambiguous jobs go into the private review/retry report, not Watching. Each card includes the score, verdict, resume lane recommendation, mission fit rating, concerns, cover letter angle, and a specific portfolio project.

**Gmail pipeline tracking**
Scans the inbox daily for emails from active pipeline companies. Uses Claude to classify each email (interview invite, rejection, application confirmation) and moves Trello cards between pipeline stages automatically. Also parses job alert digest emails from Idealist, LinkedIn, and Built In to extract and score new listings.

**Pre-filtering**
A cheap keyword pre-filter runs before any Claude API calls, skipping obvious mismatches (sales titles, engineering roles, etc.) without spending API tokens. Saves roughly 30-40% of API costs on a typical run.

**Deduplication**
Posting URLs identify jobs, with company/title checks against all Trello lists and archived cards. Archived cards represent roles already passed on and are not recreated. Employer identity is checked separately from title similarity.

Existing company/title cache records are migrated lazily without deletion. Failed scoring, failed delivery, blocked descriptions, and budget-deferred jobs retain retryable state. Salesforce pre-filter mistakes are reconsidered on rediscovery. Old failed records that contain only a hash cannot be reconstructed until the job is rediscovered.

---

## Pipeline stages

```
Watching → Applied → Interview → Offer → Closed
                  ↘ Rejected
                  ↘ Reach (long shots)
```

---

## Tech stack

- Python 3.x
- [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python) — Claude API for scoring and classification
- [Requests](https://requests.readthedocs.io) + [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) — job board scraping
- [Google Gmail API](https://developers.google.com/gmail/api) — inbox scanning and email parsing
- [Trello REST API](https://developer.atlassian.com/cloud/trello/rest/) — pipeline card management
- `python-dotenv` — credential management

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/yourusername/job-search-agent.git
cd job-search-agent

pip3 install requests beautifulsoup4 google-auth google-auth-oauthlib \
             google-auth-httplib2 google-api-python-client anthropic \
             python-dotenv lxml
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` and fill in:

```
ANTHROPIC_API_KEY=your_key_here
TRELLO_API_KEY=your_key_here
TRELLO_TOKEN=your_token_here
TRELLO_BOARD_ID=your_board_id_here
```

**Anthropic API key:** [console.anthropic.com](https://console.anthropic.com) → API Keys

**Trello key + token:** [trello.com/power-ups/admin](https://trello.com/power-ups/admin) → select or create a Power-Up → copy API Key → click Token to generate a read+write token

**Trello board ID:** Open your board in the browser. The URL looks like `trello.com/b/XXXXXXXX/board-name` — the 8-character string is the board ID.

### 3. Set up Gmail OAuth (one time)

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project, enable the Gmail API
3. Go to APIs & Services → Credentials → Create OAuth 2.0 Client ID → Desktop App
4. Download the JSON file and save it as `credentials.json` in the project folder
5. Go to APIs & Services → OAuth consent screen → Audience → Add yourself as a test user
6. Run the setup command:

```bash
python3 jeff_job_agent.py --gmail-setup
```

A browser window opens. Sign in and grant access. The token is saved to `gmail_token.json` automatically — you won't need to do this again.

### 4. Configure your Trello board

The script expects these list names on your board (configurable in `jeff_job_agent.py`):
```
Watching | Applied | Reach | Interview | Closed | Rejected
```

### 5. Run it

```bash
# Full run (crawl + Gmail scan)
python3 jeff_job_agent.py

# Crawl only
python3 jeff_job_agent.py --crawl

# Gmail scan only
python3 jeff_job_agent.py --gmail
```

### 6. Daily execution

Production runs in a separate private GitHub runner repository, keeping personal state and logs private. A Claude routine triggers its `workflow_dispatch` each morning. GitHub's `schedule` trigger is disabled. A concurrency guard prevents overlapping runs.

Deploy these together: `jeff_job_agent.py`, `job_quality.py`, `api_budget.py`, `requirements.txt`, and `tests/`. Keep credentials, profile, caches, and Trello evaluation data out of the public repository. The runner runs the offline test suite before executing and commits its private state afterward.


---

## API cost controls

The configured daily Claude API cap is **$0.50**, resetting at midnight America/New_York. It covers scoring, digest extraction, and email classification across repeated runs of this agent. Other tools or subscriptions are outside this ledger.

Before each paid request, the agent uses the unbilled token-count endpoint and reserves a conservative input/full-output charge. It reconciles that reservation with returned usage afterward. Timeouts retain their reservation because the provider may have completed the request. SDK automatic retries are disabled. Pricing is pinned to the documented Sonnet 4.6 rates; a model change requires an explicit pricing update.

- Completed digest sections are extracted once and cached across runs.
- Pending digest input survives the Gmail lookback window.
- Stable profile/portfolio input uses prompt caching.
- Delivery retries reuse the saved score.
- Unchanged ambiguous evidence reuses the prior assessment instead of paying again.
- Jobs beyond the daily cap remain queued. New plausible roles and jobs awaiting their first score take priority over historical rechecks.
- Idealist sections are split into batches of at most five linked jobs. Each completed batch is saved immediately; jobs get a scoring opportunity before the next extraction. A timeout retries only unfinished batches on a later run.
- Board navigation links are removed before collection counts and old navigation retries are retired without scoring.
- Missing optional score metadata (such as a salary suggestion) gets a safe default. Eligibility evidence and the numeric score remain required.

The private Actions summary reports reserved/reconciled API usage, outcomes, and source health. Runs explicitly report `complete`, `partial`, or `failed`. Isolated source or extraction problems and budget deferrals are partial results; fatal phase errors and total source outages still fail the workflow. Its artifact includes `run_report.json` and `review_jobs.json`. A day with no suitable matches is distinct from failed collection or a reached budget cap. Older README per-job cost estimates were not measured and should not be used.


---

## Configuration reference

Key settings at the top of `jeff_job_agent.py`:

| Setting | Default | Description |
|---|---|---|
| `min_score_for_card` | 60 | Minimum score to create a Trello card |
| `gmail_lookback_days` | 7 | How far back to scan Gmail |
| `seen_jobs_file` | `seen_jobs.json` | Local cache of processed jobs |
| `log_file` | `job_agent.log` | Full run log |
| `daily_api_budget_usd` | 0.50 | Agent-wide daily API spending cap |
| `max_retry_jobs_per_run` | 8 | Historical rechecks after fresh candidates |
| `max_description_chars` | 30000 | Oversized descriptions go to review; no silent cut-off |

---

## How the scoring works

Each job is scored out of 100 across seven dimensions:

| Dimension | Weight |
|---|---|
| Role fit (matches one of three resume lanes) | 25 pts |
| Lifestyle fit (remote, travel, schedule) | 20 pts |
| Salary and benefits | 15 pts |
| Mission alignment | 15 pts |
| Growth path | 10 pts |
| Posting quality (verified live link) | 10 pts |
| Application efficiency | 5 pts |

**Verdict bands:**
- 85-100: Apply Now
- 70-84: Apply If Interested
- 55-69: Maybe / Stretch
- Below 55: Skip

Hard disqualifiers override the score. The scorer returns supporting excerpts for remote eligibility, travel, schedule, work, and credentials; unsupported claims become unknown. Remote and role-fit evidence must be supported before a card is created. Titles alone do not establish eligibility. Posted salary evidence is separate from a suggested salary ask.

Idealist often includes a complete `JobPosting` JSON-LD payload in the initial HTML, so JavaScript execution is not required for those listings. The extractor verifies the role and includes separate benefits/location sections. Generic employer careers pages are never substituted for a specific job description. Blocked or expired pages can still occur.

Bonus signals: roles mentioning "build from scratch," "first hire," "small team," or "you'll own" get up to +15 points — these phrases correlate with the environments that actually work for me.

---

## Project structure

```
jeff_job_agent.py    # Crawlers, scoring, Gmail/Trello orchestration
job_quality.py      # Evidence extraction, validation, identity, retry policy
api_budget.py       # Persistent daily API spending guard
tests/              # Offline regression tests
.env.example         # Credential template — copy to .env
.env                 # Your credentials — never committed
.gitignore           # Keeps credentials and runtime files out of git
credentials.json     # Gmail OAuth credentials — never committed
gmail_token.json     # Gmail OAuth token — auto-generated, never committed
seen_jobs.json       # Job deduplication cache — local only
job_agent.log        # Full run history — local only
```

---

## Context

I'm a technical operations and AI workflow professional with 15+ years of experience across IT ops, project management, and SaaS administration. I built this while searching for roles at mission-aligned organizations — edtech, civic tech, climate, nonprofits — where the product actually matters.

The agent reflects the same approach I take to all operational work: find the repeatable process, build something that handles it consistently, and free up human attention for the decisions that actually require it.

The Claude-powered scoring and classification is not theoretical — it's running daily against my actual job search, and it found and correctly scored several roles I ended up applying to.

---

## Notes

- LinkedIn and Built In are sourced via Gmail alert email parsing rather than direct scraping — both platforms block automated crawlers
- Idealist discovery uses email digests; full descriptions are extracted from the specific public listing when available.
- Do not clear the cache to deploy fixes: it preserves prior decisions and retry state. Run `python3 -m unittest discover -s tests -v` to verify the regression suite without spending API credits or touching Trello.
- The script is opinionated about what constitutes a good fit for my specific background; fork and modify the `JEFF_PROFILE` constant and filter lists to adapt it for your own search
