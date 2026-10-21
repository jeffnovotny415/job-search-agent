# Job Search Agent

This is the inexpensive discovery and first-screen stage of a two-stage job search. It collects listings, checks title/location/remote/pay metadata, and places promising **named-company** leads in Trello. A separate Claude project does the deep vet: full descriptions, employer websites, responsibilities, credentials, and overall suitability.

## What it does

**Discovery**
- [Remote Impact](https://remoteimpact.org)
- [Tech Jobs for Good](https://techjobsforgood.com)
- [FFWD Jobs](https://jobs.ffwd.org)
- [NTEN](https://www.nten.org/jobs): employment, contracts, and RFPs for Ravensbreath Lab
- [WordPress Jobs](https://jobs.wordpress.net/feed/): official RSS plus employer/type/budget fields from each posting
- [We Work Remotely](https://weworkremotely.com/remote-job-rss-feed): official RSS, restricted to design, product, frontend, full-stack and sysadmin categories; each card attributes and links the source
- [Recidiviz](https://www.recidiviz.org/careers): initial direct-employer watchlist entry, using its official public Greenhouse board
- [Achievement First procurement](https://www.achievementfirst.org/category/partnerships/) and [National Trust RFPs](https://savingplaces.org/requests-for-proposals): small direct procurement watchlist
- Make Hire Help, Airtable Jobs Board and n8n Jobs: **email notifications only**, pending subscriptions and validation against real account alerts
- Idealist and Wellfound saved-search/recommendation emails in Gmail
- LinkedIn and Built In integrations remain disabled

**Free first-pass screening**
- Title must indicate IT, technical project/program work, AI workflow, or adjacent operations.
- Clear mismatches such as camp directors, legal officers, sales, and HR are skipped.
- Explicit hybrid/on-site listings are skipped. Any named non-US country in job-location/eligibility metadata, or explicit non-USD pay, is now excluded even if a remote label is present. Unknown currency/location remains flagged. US state abbreviations are not treated as country codes in free text.
- Power Systems Engineer, Commercial Operations, Pricing, and Compliance Manager titles are excluded, as are titles containing Asia, India, EMEA, or LATAM (whole words).
- Fetched, verified posting text and structured state eligibility are checked against **New Jersey (NJ)**. Explicit eligible-state lists omitting NJ, or exclusions naming NJ, are skipped. Partial lists and unparseable restrictions are flagged; office locations alone do not establish eligibility.
- Skip annual salary ranges whose maximum is below **$90,000**, except explicitly part-time roles.
- Contract/hourly listings must reach **$65/hour**; explicitly part-time employee roles retain the salary exception.
- Missing salary, unclear remote eligibility, geographic restrictions, or ambiguous pay units are flagged for the Claude project instead of silently discarded.
- Promising listings with missing employer, location, pay, or verified description get one free HTTP posting fetch. JSON-LD employer data overrides provisional board labels. Successful metadata is cached for seven days; blocked/empty reads for one day. Missing employers never produce unnamed cards.
- Factual posting text is checked for explicit office requirements, excessive/regular required travel, California-only residency, local-candidate restrictions tied to known incompatible locations, and ownership of customer accounts/renewals. This is a narrow rules check, not a model evaluation of fit. Technical mentions such as hybrid cloud and adjacent customer-success teams do not reject a role.
- Idealist consultant links use the contract floor, including previously cached alerts. String-valued structured salaries are parsed, and location/pay appear as readable text on cards.
- Repeated US location suffixes are ignored in company/title duplicate checks; seniority and specialty distinctions remain. Multiple new cards at one employer receive a comparison flag at the end of the run.

**Trello handoff**
Cards in **Watching — Roles** and **Watching — Projects** say **First-pass lead — awaiting Claude deep vet**. They include employer, source link, listed location/pay, and specific facts to verify. They do not claim to be fully vetted, assign a suitability score, or invent salary recommendations or remote eligibility.

**NTEN employment and client projects**
One tool uses two review queues. **Roles** includes salaried, part-time and contract staff positions. **Projects** includes scoped client builds, fixed-price work and RFPs. A contract label alone does not make something a project. NTEN and the added sources retain `[Employment]`, `[Contract]`, or `[RFP]` in each card title. Website/web-app, integration, automation, internal-tool, database, and nonprofit-operations signals are screened locally; bare “development” is not a software signal. Employment relevance uses the title to avoid treating incidental CRM/website mentions in fundraising jobs as technical work. Project descriptions and attached PDF text can supply concrete scope signals.

Both tracks use free, deterministic screening; the separate Claude project retains the deep vet. Annual employment uses the existing salary floor and part-time exception; hourly contracts use the existing $65 floor. Explicit **project budgets remain total project budgets**, and unknown periods remain unknown. Neither is converted into a salary or an invented hourly rate. Project scope, effort, pricing, references, and suitability remain for the separate deep vet.

NTEN is checked at most once per Eastern calendar day, including failed attempts. Reruns reuse the snapshot stored in the existing private `alert_extractions.json`. Public pages and linked PDFs are fetched without AI, subject to each host's robots rules, crawl delay/request rate, a minimum one-second interval, and rate-limit deferral. Crawl failures appear in the run report instead of masquerading as zero matches.

Cards preserve posting dates, pay units, location notes, deadline/submission/reference excerpts, and links to the RFP and addenda. Extracted excerpts are evidence to verify, not a compliance verdict. Reads are capped at five PDFs per listing, 8 MiB per response, and 30 pages/100,000 characters per PDF. Scanned, encrypted, inaccessible, non-PDF, and truncated documents stay linked with verification flags; no paid OCR or model extraction is added. A failed re-read retains prior document text with a stale-data warning.

Material changes to title, employer, location, compensation, opportunity type, scope text, or document text/links are detected using a fingerprint. Changed active cards receive a dated note (even if the new terms fail screening), without moving them or overwriting user notes. Unchanged listings are skipped, failed updates retry, and archived passes are never reopened. Excerpts are bounded; if an existing card has no room for an update, it stays in the retry/review queue. Posting timestamp/whitespace-only changes do not create updates.

**Added-source limits and health**
Each public adapter is attempted once per Eastern calendar day, with independent health reporting and private cached snapshots. Empty valid feeds count as successful checks; blocked/malformed sources are reported as failures. The same robots-aware, bounded HTTP client is used throughout. We Work Remotely's state field is preserved as a verification flag rather than assumed to be an applicant restriction. Explicit location/remote requirements in posting text still apply. No direct crawling of Make, Airtable or n8n is performed.

The procurement watchlist checks current public indexes and captures proposal deadlines, references, submission excerpts and document links. Explicitly expired proposal dates are skipped; ambiguous dates remain flagged. Public PDF links use the same bounded extraction as NTEN; form-gated documents remain manual links, with no form submissions. This is a focused watchlist, not coverage of every government or nonprofit RFP. NJSTART, RFPDB and other account-based procurement sources are not enabled.

**Subscription setup (user action)**
Use the Gmail account already connected to this agent, sign in, and enable category emails for new topics/posts only, with replies disabled where supported:

- [Make — Hire Help](https://community.make.com/c/hire-a-pro/hire-help/74): category notification control; choose “Watching First Post” if offered and ensure email notifications are enabled in account preferences.
- [Airtable — Jobs Board](https://community.airtable.com/jobs-board-16): subscribe to the category and select first/new posts only where available.
- [n8n — Jobs](https://community.n8n.io/c/jobs/13): category notification control; choose “Watching First Post” if offered and ensure email delivery is enabled.

Exact notification menus depend on the account/site version. No passwords or API keys are needed by the agent for these sources. Once a real alert arrives, verify the template. The initial parser accepts single hiring-topic notifications with explicit company/client fields; replies and “for hire” ads are excluded. Unknown templates/digests retain original input in the private retry cache; missing client identity stays in review and never creates an unnamed card. Until real alerts have been checked, these integrations are provisional, not verified end-to-end.

**Claude deep-vet project update (paste into project instructions)**
> Read both `Watching — Roles` and `Watching — Projects`. Treat the crawler's metadata as preliminary evidence. For Roles, check the full JD, real employer, fully remote eligibility from New Jersey, responsibilities, and compensation ($90k annual floor except part-time employment; $65/hour contract floor). For Projects, assess Ravensbreath Lab's fit for websites/apps, integrations, automation, internal tools and databases; verify the actual client, remote delivery, scope, total budget versus effort, proposal deadline/time zone, references, vendor qualifications, submission requirements and every linked RFP/addendum. Keep annual salary, hourly rates and total project budgets distinct. Contract staff positions stay in Roles. Do not reopen archived passes. Project proposals and client follow-up are manual; job-email automation only manages Roles.

**Gmail pipeline tracking**
Scans the inbox daily for emails from active role pipeline companies. Project cards are excluded from both application-status scanning and reconciliation, including when moved to another list. Uses Claude to classify each email (interview invite, rejection, application confirmation) and moves Trello cards between pipeline stages automatically. Also reads job alert emails to extract basic listing metadata.

**Pre-filtering**
A cheap keyword pre-filter runs before any Claude API calls, skipping obvious mismatches (sales titles, engineering roles, etc.) without spending API tokens. Saves roughly 30-40% of API costs on a typical run.

**Deduplication**
Posting URLs identify jobs, with company/title checks against all Trello lists and archived cards. Archived cards represent roles already passed on and are not recreated. Employer identity is checked separately from title similarity.

Existing company/title cache records are migrated lazily without deletion. Failed scoring, failed delivery, blocked descriptions, and budget-deferred jobs retain retryable state. Salesforce pre-filter mistakes are reconsidered on rediscovery. Old failed records that contain only a hash cannot be reconstructed until the job is rediscovered.

---

## Pipeline stages

```
Watching — Roles → Applied → Interview → Offer → Closed
                  ↘ Rejected
                  ↘ Reach (long shots)
Watching — Projects → manual proposal/client follow-up
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

pip3 install -r requirements.txt
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

Deploy these together: `jeff_job_agent.py`, `first_pass.py`, `geographic_filters.py`, `job_quality.py`, `api_budget.py`, `requirements.txt`, and `tests/`. Keep credentials, profile, caches, and Trello evaluation data out of the public repository. The runner runs the offline test suite before executing and commits its private state afterward.


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
| `home_state` | NJ | Required state eligibility for explicitly restricted remote jobs |
| `enable_wellfound_alerts` | true | Read existing Wellfound emails |
| `enable_nten` | true | Daily NTEN source, with no model calls |
| `enable_additional_sources` | true | Official feeds, Recidiviz and focused procurement watchlist |
| `enable_community_alerts` | true | Email-only intake, pending real notification validation |
| `nten_opportunity_types` | Employment, Contract, RFP | Optional type filter within the same feed |
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
nten_source.py       # Daily NTEN crawl, project units, PDF evidence and change detection
engagement.py        # Role/project classification and queue names
additional_sources.py # Daily public feeds, ATS and procurement watchlists
community_alerts.py  # Email-only hiring topic parsing; no forum requests
geographic_filters.py # Country/currency rules and explicit state eligibility
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
- Idealist and Wellfound discovery uses email metadata plus cached free posting reads where accessible. Deep evaluation of full descriptions remains in the separate Claude project. Missing remote labels or a city alone remain uncertain rather than proof of on-site work.
- Do not clear the cache to deploy fixes: it preserves prior decisions and retry state. Run `python3 -m unittest discover -s tests -v` to verify the regression suite without spending API credits or touching Trello.
- Adapt the first-pass CONFIG thresholds and title filters for another search; the detailed Claude-project instructions are managed separately.
