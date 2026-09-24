# Job Search Agent

This is the inexpensive discovery and first-screen stage of a two-stage job search. It collects listings, checks title/location/remote/pay metadata, and places promising **named-company** leads in Trello. A separate Claude project does the deep vet: full descriptions, employer websites, responsibilities, credentials, and overall suitability.

## What it does

**Discovery**
- [Remote Impact](https://remoteimpact.org)
- [Tech Jobs for Good](https://techjobsforgood.com)
- [FFWD Jobs](https://jobs.ffwd.org)
- Idealist and Wellfound saved-search/recommendation emails in Gmail
- LinkedIn and Built In integrations remain disabled

**Free first-pass screening**
- Title must indicate IT, technical project/program work, AI workflow, or adjacent operations.
- Clear mismatches such as camp directors, legal officers, sales, and HR are skipped.
- Explicit hybrid/on-site listings and clear remote-country restrictions outside the US are skipped. A city by itself does not establish an office requirement.
- Skip annual salary ranges whose maximum is below **$90,000**, except explicitly part-time roles.
- Contract/hourly listings must reach **$65/hour**; explicitly part-time employee roles retain the salary exception.
- Missing salary, unclear remote eligibility, geographic restrictions, or ambiguous pay units are flagged for the Claude project instead of silently discarded.
- Known employers do not require a full-description fetch. Missing names get a free structured-metadata lookup; if the name cannot be recovered, no unnamed card is created.

**Trello handoff**
Cards in Watching say **First-pass lead — awaiting Claude deep vet**. They include employer, source link, listed location/pay, and specific facts to verify. They do not claim to be fully vetted, assign a suitability score, or invent salary recommendations or remote eligibility.

**Gmail pipeline tracking**
Scans the inbox daily for emails from active pipeline companies. Uses Claude to classify each email (interview invite, rejection, application confirmation) and moves Trello cards between pipeline stages automatically. Also reads job alert emails to extract basic listing metadata.

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
- [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python) — Claude API for application-status classification
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

Deploy these together: `jeff_job_agent.py`, `first_pass.py`, `job_quality.py`, `api_budget.py`, `requirements.txt`, and `tests/`. Keep credentials, profile, caches, and Trello evaluation data out of the public repository. The runner runs the offline test suite before executing and commits its private state afterward.


---

## Cost controls and screening rules

The API spending ceiling remains **$0.75/day**, resetting at midnight America/New_York. It is a safety limit, not a spending target. Existing daily usage is preserved across reruns and deployments.

**Job screening and Idealist/Wellfound HTML parsing make no paid model calls.** The application-status classification and reconciliation features can still use the API. They retain their cached decisions and the same spending guard. The older disabled LinkedIn/Built In parsers also use the guard if explicitly re-enabled.

- Parse job-card HTML directly, retaining title, employer, location, salary, and links.
- Keep incomplete input for review; do not silently substitute an empty successful extraction.
- Recover old Idealist backlog from saved metadata, current emails, or at most 20 free structured-data fetches per run.
- Wellfound's public pages may block HTTP clients. First-pass leads can use email metadata; a full description is not required at this stage. Only job-card redirect links are followed, never unsubscribe or preference controls.
- Deduplicate against every Trello list and archived passes before creating cards.
- Reconsider prior deep-score rejections on rediscovery under the new first-pass policy. Existing delivered/archived cards remain protected.
- Preserve failed deliveries and missing-employer cases for retry; new leads are handled before older rechecks.

| Setting | Default | Purpose |
|---|---|---|
| `annual_salary_floor` | 90000 | Reject full-time salary ranges entirely below this floor |
| `contract_hourly_floor` | 65 | Minimum viable contract/hourly rate |
| `enable_wellfound_alerts` | true | Read existing Wellfound emails |
| `gmail_lookback_days` | 7 | Recent discovery/status emails |
| `daily_api_budget_usd` | 0.75 | Cap remaining model-assisted status tracking |
| `max_retry_jobs_per_run` | 8 | Historical job rechecks per run |
| `max_legacy_metadata_per_run` | 20 | Bound free legacy backlog recovery requests |

The private Actions summary distinguishes `complete`, `partial`, and `failed`, with actual outcomes and API usage. An empty candidate list, inaccessible source, and reached API cap are different outcomes. Run artifacts include `run_report.json` and `review_jobs.json`.

The Claude daily trigger is unchanged. Wellfound alerts observed at 9:25 AM Eastern arrive after the observed 7:20 AM run; the next daily run picks those up unless the trigger time is adjusted separately.

---

## Project structure

```
jeff_job_agent.py    # Crawlers, Gmail/Trello orchestration
first_pass.py        # Free metadata screening and HTML email parsers
job_quality.py      # Job identity, duplicate detection, retry policy
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

The automation discovers leads; my separate Claude project evaluates them deeply before I decide whether to apply.

---

## Notes

- LinkedIn and Built In are sourced via Gmail alert email parsing rather than direct scraping — both platforms block automated crawlers
- Idealist and Wellfound discovery uses email metadata; full descriptions are checked in the separate Claude project.
- Do not clear the cache to deploy fixes: it preserves prior decisions and retry state. Run `python3 -m unittest discover -s tests -v` to verify the regression suite without spending API credits or touching Trello.
- Adapt the first-pass CONFIG thresholds and title filters for another search; the detailed Claude-project instructions are managed separately.
