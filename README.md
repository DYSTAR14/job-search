# LinkedIn job-post scan

Reads LinkedIn's post/content search (NOT the formal Jobs tab) for recent
"hiring" posts matching your query list, filters and scores them, and
writes a CSV + Markdown summary. Read-only: it never applies, messages,
comments, follows or connects.

## Setup (Windows)

```powershell
# 1. Python 3.12 (already installed and confirmed on this machine)
python --version

# 2. Project-local virtual environment
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium   # still needed even though we launch via channel=chrome

# 3. One-time manual login (opens a real browser window you log into yourself)
.venv\Scripts\python scan.py --login
```

The venv, dependencies and Chromium binary are already set up in this
project directory — steps 2 only need to be repeated on a fresh machine.

The login step saves your session into `profile/linkedin/`. This folder
holds live LinkedIn cookies — treat it like a password file: never commit
it, never share it.

## Running

```powershell
.venv\Scripts\python scan.py              # last 24h, all queries in config.yaml
.venv\Scripts\python scan.py --window 12  # override the lookback window
```

Output lands in `output/<DD-MM-YYYY>/scan_<hh-mmAM/PM>.csv` and `.md` (e.g.
`scan_02-29PM.csv`) — one dated folder per day, one file pair per run, named
so the time is readable at a glance. Send both here and I'll rank the
results and draft outreach.

## Scheduling

Windows Task Scheduler, two triggers (e.g. 10:10 AM and 5:00 PM):
- Action: `E:\ai_project\job_search\.venv\Scripts\python.exe` with argument `E:\ai_project\job_search\scan.py`
- **"Run only when user is logged on"** — this must stay checked. The
  browser window needs an active, unlocked desktop session; "run whether
  user is logged on or not" will silently fail to launch it.

Start with one run a day until you've confirmed a few clean runs with no
warnings in the `.md` summary, then add the second trigger.

## If a run stops with a warning

Warnings in the `.md` file mean the script stopped itself rather than
trying to push through. Two different situations show up this way:

- **Checkpoint / CAPTCHA / identity verification:** the account itself is
  restricted. Log in manually in a normal browser, resolve whatever
  LinkedIn is asking for, then re-run `.venv\Scripts\python scan.py --login`
  to refresh the saved session before the next run.
- **"Unusual search traffic" / "unusual activity" / commercial-use-limit:**
  this is a search-specific soft throttle, not a login problem - there's
  nothing to "fix" by logging in. It showed up on 2026-10-07 and again on
  2026-10-08, both times on the 2nd of 12 queries, after the account had
  been running ~12 deep-scrolling searches daily for a week straight
  (external research on LinkedIn's automation detection points to
  cumulative activity volume vs. the account's historical baseline, not
  in-session click speed, as the real trigger). **Do not retry the same
  day** - multiple sources report that retrying soon after this kind of
  block tends to extend it rather than clear it. Skip at least a day,
  ideally two, before the next attempt, and don't resume at full volume -
  run fewer than all 12 queries, or accept a lower scroll cap for the
  first run or two back.

## Tuning

Everything that affects what gets kept is in `config.yaml`:
- `queries` — the search phrases
- `include_keywords` / `exclude_keywords` — scoring and hard drops
- `exclude_role_prefixes` — drop by role title (Angular/React/.NET-first, etc.)
- `location_allow` — keep only posts mentioning one of these
- `experience` — hard cap on minimum years required
- `pacing` — scroll/query delays, scroll step cap

Role, company, experience and location are extracted with regex, not fixed
fields — many recruiter posts are templated ("Role:", "Exp:", "Location:")
and parse cleanly, but freeform posts are best-effort and may come through
with blanks. That's expected; the post URL is always there to check by hand.

## Apply-link analysis

Posts without an email usually apply through a link (mostly `lnkd.in` shorteners). `links.py` extracts every URL from each surviving post, resolves shorteners to their real destination (shortener hosts only, logged-out, cached in `link_cache.json`, capped by `max_link_resolutions_per_run`), and classifies each as application form / company ATS / LinkedIn job / careers page / job board / community channel / aggregator. The `.md` shows the best apply link with its type, lists the other links tagged by kind, and flags posts whose only links are Telegram/WhatsApp/aggregator pages. The CSV gains `apply_url`, `apply_type` and `other_links`. Set `resolve_links: false` in `config.yaml` to skip all network lookups.
