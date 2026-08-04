# Job Application Agent

Autonomous job-search & application agent. Searches for jobs, scores them against
your CV, tailors your CV when the fit is low, auto-applies only on boards you've
explicitly whitelisted, drafts applications everywhere else for your approval,
tracks skill gaps, and sends daily/weekly reports.

Built entirely on free tools — see [Tech stack](#tech-stack) below.

## Design principles

1. **Whitelist-only auto-submit.** The agent only submits automatically on sources
   you explicitly whitelist in `.env` (`WHITELISTED_SOURCES`). Everywhere else it
   prepares a complete draft and waits for your approval.
2. **Everything is logged.** Every job seen, scored, drafted, or applied to is
   stored in SQLite — this powers the daily/weekly reports.
3. **Deterministic + LLM hybrid scoring.** ATS scoring combines keyword/skill
   overlap (matches how real ATS systems filter) with LLM judgment (context,
   seniority fit).
4. **Idempotent runs.** The agent never applies to the same job twice — dedupe
   on job URL before acting.
5. **Dry-run by default.** `DRY_RUN=true` in `.env` logs every send/submit
   action instead of executing it, until you've verified the system works.

## Tech stack (all free)

| Component | Tool |
|---|---|
| Orchestration | LangGraph |
| LLM | Ollama (local, free), Gemini API free tier, or Groq API free tier (fast hosted inference) |
| Job search | Greenhouse + Lever public APIs (free), SerpAPI free tier (100/mo, optional) |
| Watchlist | Google Sheets via `gspread` (free service account) |
| CV parsing | `pdfplumber` / `python-docx` (reads your uploaded .pdf/.docx) |
| Tailored CV output | `reportlab` (renders each rewrite as a real formatted .pdf) |
| Email | Gmail API (OAuth, your own account, free) |
| Reports | Telegram Bot API (free, unlimited) |
| Scheduling | APScheduler |
| Storage | SQLite via SQLAlchemy |

## Setup

Pick one environment manager -- both install the exact same packages from
`requirements.txt`, so it's a matter of preference.

**venv:**

```bash
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

**conda:**

```bash
conda env create -f environment.yml
conda activate job-agent
```

(`conda env update -f environment.yml --prune` to sync after `requirements.txt` changes.)

Either way:

```bash
playwright install chromium   # only needed if you enable JS-rendered scraping
cp .env.example .env          # fill in the values you plan to use
```

Minimum to run in dry-run mode with zero external accounts: nothing else —
Greenhouse/Lever need no key, and `DRY_RUN=true` is the default.

To use free LLM scoring, pick one:
- Install [Ollama](https://ollama.com), run `ollama pull llama3.1`, leave `LLM_PROVIDER=ollama`, or
- Get a free [Gemini API key](https://aistudio.google.com/apikey) and set `LLM_PROVIDER=gemini` + `GEMINI_API_KEY`, or
- Get a free [Groq API key](https://console.groq.com/keys) and set `LLM_PROVIDER=groq` + `GROQ_API_KEY`
  (`GROQ_MODEL` defaults to `openai/gpt-oss-120b`; pick a different one from the dropdown on the
  Settings page — Groq periodically retires models, check
  [console.groq.com/docs/models](https://console.groq.com/docs/models) if a model stops working)
  — hosted, no local install, and generally the fastest of the three since Groq runs on its own
  inference hardware.

All three are switchable from the dashboard's **Settings** page too, not just `.env`.

Add your CV before running the daily job — either upload it through the
dashboard's **CV** page (`frontend/cv.html`), or place a file by hand at
`cv/current_cv.pdf` or `cv/current_cv.docx`.

## Running it

**Windows, one click:** double-click `run.bat` (venv) or `run_conda.bat`
(conda — creates/updates the `job-agent` environment automatically, no
manual `conda env create` needed). First run copies `.env.example` to `.env`
and opens it in Notepad so you can fill in real values — save, close, and
double-click the script again. After that it starts the API, the scheduler,
and opens the dashboard in your browser every time.

`run_conda.bat` needs `conda activate` to work in a plain Command Prompt,
which requires `conda init cmd.exe` to have been run once (the Anaconda/Miniconda
installer usually offers this). If the API/Scheduler windows show a "conda is
not recognized" or "CondaError: Run 'conda init'" error, either run that once
from an Anaconda Prompt and restart your terminal, or just launch the script
from an Anaconda Prompt directly.

To get a proper Desktop icon instead of digging into the project folder each
time, double-click `create_desktop_shortcut.vbs` once — it creates a
"Job Application Agent" shortcut on your Desktop that runs `run.bat`. One-time
setup; the shortcut itself is reusable forever. (Edit the `.vbs` file's
`targetBat` line to point at `run_conda.bat` instead if that's the one you use.)

**Manually / other OS:**

```bash
# one-off: search everything configured, score, draft/apply
python jobs/daily_run.py

# one-off: apply from a single URL you found manually (cv_path optional if
# you've already uploaded a CV via the dashboard's CV page)
python jobs/apply_from_link.py "https://boards.greenhouse.io/acme/jobs/123" [cv_path]

# start the always-on scheduler (daily search 08:00, daily report 20:00, weekly news Mon 09:00)
python scheduler.py

# dashboard API + frontend, in separate terminals
uvicorn api:app --host 127.0.0.1 --port 8000
python -m http.server 5500 --directory frontend
```

This is a local-only setup: the API, scheduler, and dashboard all run on your
own machine, so everything stops when your machine sleeps or shuts down —
there's no cloud host or scheduler keeping it running while you're away. See
[Deployment](#deployment) if you want it running unattended on always-on
infrastructure instead.

## Enabling auto-submit

Every board is draft-only until you add it to `WHITELISTED_SOURCES` in `.env`.
Before whitelisting a board:

1. Inspect that specific board's application form fields (browser dev tools or
   its public API) — every Greenhouse/Lever board can have different custom
   fields (screening questions, EEO fields, etc).
2. Implement the exact submit payload in `agents/apply_agent.py::auto_submit_greenhouse`
   (currently a deliberate stub — `NotImplementedError`).
3. Test end-to-end on one real or throwaway application with `DRY_RUN=true` first,
   then `DRY_RUN=false`.
4. Add `"greenhouse:<board_token>"` or `"lever:<company_slug>"` to `WHITELISTED_SOURCES`.

This is the one part of the system meant to be verified per employer, not automated blindly.

## Frontend + dashboard API

`api.py` is a FastAPI layer over the same SQLite DB the agents write to, plus
endpoints that write local config, upload files, and trigger sends for a
single user. `frontend/` is a static, dependency-free HTML/CSS/JS dashboard
(off-white, minimalist) — no build step required. Seven pages, linked from the
nav bar on every page:

- **Dashboard** (`index.html`) — stats, applications table, skill gaps, and
  latest news digest.
- **Search** (`search.html`) — configures what the search-and-apply pipeline
  looks for, in three parts:
  - **Position** — a job title/keyword, saved to `.env`
    (`SEARCH_POSITION_QUERY`). Used as part of the SerpAPI/Google Jobs query,
    and as a case-insensitive title filter applied to every other source
    (Greenhouse, Lever, watchlist, added sites). Leave blank to pull
    everything configured with no filter.
  - **Seniority** — a dropdown (Intern, Entry Level, Mid Level, Senior, Lead,
    Manager), saved to `.env` (`SEARCH_SENIORITY_LEVEL`). Also folded into
    the SerpAPI query, and matched against every other source's job titles
    via keyword heuristics (`agents/search_agent.py::SENIORITY_KEYWORDS` --
    e.g. "senior"/"sr." for Senior, "intern" for Intern; Mid Level matches
    titles with none of those keywords, since unlabeled titles are usually
    mid-level in practice). It's a heuristic, not an exact classification.
  - **Max time since posted** — a dropdown (Any time / 24 hours / 3 days /
    week / 2 weeks / month), saved to `.env` (`SEARCH_MAX_AGE_DAYS`). Only
    applied where a real posted date exists: Greenhouse's `first_published`,
    Lever's `createdAt`, or SerpAPI's relative `detected_extensions.posted_at`
    text ("3 days ago", etc). Added sites searched by the generic scraper
    have no structured date, so they're never excluded by this filter.
  - **Max results per site** — a dropdown (No limit / 10 / 25 / 50 / 100),
    saved to `.env` (`SEARCH_MAX_RESULTS_PER_SITE`). Caps the raw results
    kept from each individual Greenhouse board, Lever company, watchlist
    row, or added site *before* any filtering -- useful for a large board
    (some return 500+ jobs) or to keep the generic scraper's per-page
    fetching polite. Defaults to no limit, matching the original behavior.
  - **Job boards** — add any website URL. Greenhouse/Lever URLs are detected
    automatically and searched via their public APIs, same as the
    `.env`-configured boards; any other URL falls back to a best-effort
    scraper (`agents/search_agent.py::search_generic_site`) that looks for
    same-site links that look job-related — noisier than the API-backed
    path, and worth checking a site's Terms of Service before adding it.
    Stored in the `search_sites` table, editable (add/remove) from this page.
  - **Search now** — runs the same pipeline the scheduler fires at 8am, on
    demand, using whatever position/sites are currently saved. Blocks while
    running -- can take a few minutes since it's one LLM call per new job
    found.
- **Settings** (`settings.html`) — choose the LLM provider (Ollama or Gemini)
  and enter/replace the Gemini API key. Saved to `.env` on the machine running
  `api.py` (`env_store.py` upserts the specific keys, preserving everything
  else in the file). Takes effect immediately for `api.py` itself; the
  scheduler is a separate process and needs a restart to pick up the change —
  the page says so rather than pretending it's instant everywhere.
- **CV** (`cv.html`) — shows the currently active CV (filename, parsed
  preview) and lets you upload a replacement (.pdf/.docx, drag-and-drop or
  file picker), plus a table of every tailored CV the rewrite step has
  generated for low-fit jobs, with download links. The active CV is saved to
  `cv/current_cv.<ext>`, which `jobs/daily_run.py` and `jobs/apply_from_link.py`
  auto-detect via `cv_parser.find_default_cv()` instead of a hardcoded path.
- **Email** (`email.html`) — connect your Gmail account (OAuth; needs a
  client file at `credentials/gmail_credentials.json` from Google Cloud
  Console first), send an email for any `pending_review` application (picks
  the application, prefills a subject/body you can edit, attaches its CV),
  and a table of every email ever sent — to, subject, job, status, date.
  Every attempt is logged to the `email_logs` table regardless of outcome
  (sent, dry-run, or failed), so the list is a real record, not just a cache
  of the last session.
- **Reports** (`reports.html`) — full history of daily application summaries
  and weekly news digests sent over Telegram, each with its status
  (sent/dry-run/failed) and the exact text that went out.
- **Features** (`features.html`) — what each part of the system does, with a
  couple of lines (current whitelist, dry-run state) pulled live from `/api/status`
  rather than being static marketing copy, and links into the relevant page
  for each capability's actual results.

Search results, ATS scores, and the whitelist/draft split don't get their own
pages — they're already the Dashboard's Applications table (score column +
status column), so a separate tab would just duplicate the same rows with a
narrower view. Skill gap tracking is likewise already on the Dashboard.

```bash
# backend (serves /api/*)
uvicorn api:app --reload --port 8000

# frontend — just open index.html, or serve the folder
python -m http.server 5500 --directory frontend
```

Before deploying, edit `frontend/config.js` and change `API_BASE` to wherever
`api.py` ends up running (see [Deployment](#deployment)).

## Deployment

**Frontend** — it's static files, so any free static host works: Cloudflare
Pages, Netlify, Vercel, or GitHub Pages. Cloudflare Pages is a solid default:
unlimited bandwidth, no build step needed (publish `frontend/` as-is).

**Backend (`api.py` + `scheduler.py`)** — this needs a real, persistent Python
process (SQLite file, LangGraph, APScheduler), which **Cloudflare Workers/Pages
Functions can't run** — their Python runtime is WASM-based (Pyodide) and
doesn't support SQLite's C extension or long-running schedulers. Free options
that do work:
- **Cloudflare Pages (frontend) + Cloudflare Tunnel (backend).** Run `api.py`
  and `scheduler.py` on your own machine or a free VM, and use `cloudflared`
  to give it a free public HTTPS URL — no separate hosting bill, and no code
  changes needed.
- **Render / Fly.io / PythonAnywhere free tier** for `api.py` directly (Render's
  free web service sleeps when idle; Fly.io's free allowance stays warm).
- **Google Cloud Compute Engine (Always Free e2-micro)** — same idea as the
  Cloudflare Tunnel option above, on Google's forever-free VM instead of your
  own machine. Step-by-step guide + deploy scripts: [`deploy/gcp/README.md`](deploy/gcp/README.md).

## Testing

```bash
pytest
```

Covers: ATS score stays in 0–1 range, whitelist enforcement never lets a
non-whitelisted (or spoofed) source auto-submit, and dedup prevents double-applying
to the same job URL.

## Project layout

```
config.py            # all settings, read from .env
models.py / db.py     # SQLite schema + session handling
cv_parser.py          # PDF/docx -> text
agents/                # one module per capability (search, ats, rewrite, apply, email, reporter, news, skill_gap)
orchestrator.py        # LangGraph state machine wiring the agents together
jobs/                  # entry points: daily_run, daily_report, weekly_news, apply_from_link
scheduler.py            # APScheduler cron triggers -> automatic daily/weekly execution
api.py                  # FastAPI layer: dashboard data + settings + CV upload + email/reports
env_store.py             # upserts specific keys in .env, preserving the rest
cv/                      # current_cv.pdf/.docx lives here (gitignored -- personal data)
cv_output/               # tailored CVs generated by the rewrite step, served for download
requirements.txt        # pip package list -- single source of truth for versions
environment.yml         # conda env definition, installs from requirements.txt
run.bat                 # Windows one-click launcher (venv): API + scheduler + dashboard
run_conda.bat           # same, but creates/activates the conda env instead
create_desktop_shortcut.vbs  # one-time: creates a Desktop shortcut to run.bat
frontend/               # off-white dashboard: index/search/settings/cv/email/reports/features.html + config.js, no build step
tests/                  # pytest suite
deploy/gcp/             # Compute Engine (Always Free e2-micro) deploy scripts + guide
```

## Key cautions

- Never fabricate CV content — the rewriter reframes real experience, never invents skills.
- Respect job board Terms of Service — stick to public APIs or explicit permission.
- Rate-limit searches/applications — too many automated requests can get an IP or account flagged.
- Review before scaling auto-submit — start draft-for-review everywhere, whitelist one board at a time.
