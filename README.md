# Job Search Agent

A personal job search automation tool I built while actively looking for work in 2026. It crawls mission-aligned job boards, scores each role against my specific profile using the Claude API, creates Trello cards for strong matches, and scans Gmail to automatically move pipeline cards when rejections or interview invites arrive.

I built this because I was spending 2-3 hours a day manually checking job boards, copying listings into a tracker, and trying to remember which applications had heard back. This runs every morning in about 5-10 minutes and handles all of that automatically.

---

## What it does

**Job crawling**
Scrapes four mission-aligned job boards daily:
- [Remote Impact](https://remoteimpact.org) — remote roles at impact-driven orgs
- [Tech Jobs for Good](https://techjobsforgood.com) — tech roles at nonprofits and social enterprises
- [FFWD Jobs](https://jobs.ffwd.org) — Fast Forward nonprofit tech job board
- Idealist, LinkedIn, and Built In via Gmail alert email parsing

**Intelligent scoring**
Each new job is scored 0-100 against a detailed personal profile using Claude (Anthropic's API). The profile encodes:
- Three resume lanes (IT Ops, Technical PM, AI Workflow)
- Hard disqualifiers (sales roles, onsite requirements, crypto, revenue cycle)
- Mission alignment scoring (edtech, civic tech, climate, nonprofits score higher)
- Ownership language signals ("build from scratch," "first hire," "small team" get a boost)
- Company size flags (under 50 = positive signal, over 500 = bureaucracy risk noted)

**Trello integration**
Roles scoring 60+ automatically get a Trello card in the Watching list. Each card includes the score, verdict, resume lane recommendation, mission fit rating, concerns, cover letter angle, and a portfolio piece placeholder.

**Gmail pipeline tracking**
Scans the inbox daily for emails from active pipeline companies. Uses Claude to classify each email (interview invite, rejection, application confirmation) and moves Trello cards between pipeline stages automatically. Also parses job alert digest emails from Idealist, LinkedIn, and Built In to extract and score new listings.

**Pre-filtering**
A cheap keyword pre-filter runs before any Claude API calls, skipping obvious mismatches (sales titles, engineering roles, etc.) without spending API tokens. Saves roughly 30-40% of API costs on a typical run.

**Deduplication**
A local `seen_jobs.json` cache fingerprints every job by company + title. Jobs are never re-scored or re-added to Trello across runs, even if they appear on multiple boards.

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

### 6. Schedule daily runs (optional)

On Mac, open Terminal and run `crontab -e`, then add:

```
0 8 * * * /usr/bin/python3 /full/path/to/jeff_job_agent.py >> /full/path/to/job_agent.log 2>&1
```

---

## Estimated running costs

The only paid service is the Anthropic API. Gmail API and Trello API are free.

| Scenario | Daily cost | Monthly cost |
|---|---|---|
| Light day (10 new jobs) | ~$0.03 | ~$0.90 |
| Normal day (20-30 new jobs) | ~$0.10 | ~$3.00 |
| First run (full corpus) | ~$0.50-1.00 | one-time |

After the first run, the seen-jobs cache means daily costs stay low — only genuinely new listings get scored.

---

## Configuration reference

Key settings at the top of `jeff_job_agent.py`:

| Setting | Default | Description |
|---|---|---|
| `min_score_for_card` | 60 | Minimum score to create a Trello card |
| `gmail_lookback_days` | 14 | How far back to scan Gmail |
| `seen_jobs_file` | `seen_jobs.json` | Local cache of processed jobs |
| `log_file` | `job_agent.log` | Full run log |

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

Hard disqualifiers (sales, onsite, crypto, revenue cycle, etc.) override the score entirely — a role can score 90 on fit and still be disqualified.

Bonus signals: roles mentioning "build from scratch," "first hire," "small team," or "you'll own" get up to +15 points — these phrases correlate with the environments that actually work for me.

---

## Project structure

```
jeff_job_agent.py    # Main script — all logic lives here
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
- Idealist is sourced the same way; their site is fully JavaScript-rendered and not scrapeable via HTTP
- The `seen_jobs.json` cache is intentionally not cleared between runs — delete it only if you want to reprocess everything from scratch
- The script is opinionated about what constitutes a good fit for my specific background; fork and modify the `JEFF_PROFILE` constant and filter lists to adapt it for your own search
