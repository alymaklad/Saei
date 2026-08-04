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
| LLM | Ollama (local, free) or Gemini API free tier |
| Job search | Greenhouse + Lever public APIs (free), SerpAPI free tier (100/mo, optional) |
| Watchlist | Google Sheets via `gspread` (free service account) |
| CV parsing | `pdfplumber` / `python-docx` |
| Email | Gmail API (OAuth, your own account, free) |
| Reports | Telegram Bot API (free, unlimited) |
| Scheduling | APScheduler |
| Storage | SQLite via SQLAlchemy |

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
playwright install chromium   # only needed if you enable JS-rendered scraping

cp .env.example .env          # fill in the values you plan to use
```

Minimum to run in dry-run mode with zero external accounts: nothing else —
Greenhouse/Lever need no key, and `DRY_RUN=true` is the default.

To use free LLM scoring, either:
- Install [Ollama](https://ollama.com), run `ollama pull llama3.1`, leave `LLM_PROVIDER=ollama`, or
- Get a free [Gemini API key](https://aistudio.google.com/apikey) and set `LLM_PROVIDER=gemini` + `GEMINI_API_KEY`.

Put your CV at `cv/current_cv.pdf` (or `.docx`) before running the daily job.

## Running it

```bash
# one-off: search everything configured, score, draft/apply
python jobs/daily_run.py

# one-off: apply from a single URL you found manually
python jobs/apply_from_link.py "https://boards.greenhouse.io/acme/jobs/123" cv/current_cv.pdf

# start the always-on scheduler (daily search 08:00, daily report 20:00, weekly news Mon 09:00)
python scheduler.py
```

Deploy `scheduler.py` on any always-on host (small VPS, Fly.io, a cron-triggered
GitHub Action) to make the three triggers run automatically without you present.

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

`api.py` is a thin read-only FastAPI layer over the same SQLite DB the agents
write to. `frontend/` is a static, dependency-free HTML/CSS/JS dashboard
(off-white, minimalist) that reads from it — no build step required.

```bash
# backend (serves /api/*)
uvicorn api:app --reload --port 8000

# frontend — just open the file, or serve it
python -m http.server 5500 --directory frontend
```

Before deploying, edit `frontend/app.js` and change `API_BASE` to wherever
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
api.py                  # read-only FastAPI layer over the SQLite DB, for the frontend
frontend/               # static off-white dashboard (index.html/style.css/app.js), no build step
tests/                  # pytest suite
deploy/gcp/             # Compute Engine (Always Free e2-micro) deploy scripts + guide
```

## Key cautions

- Never fabricate CV content — the rewriter reframes real experience, never invents skills.
- Respect job board Terms of Service — stick to public APIs or explicit permission.
- Rate-limit searches/applications — too many automated requests can get an IP or account flagged.
- Review before scaling auto-submit — start draft-for-review everywhere, whitelist one board at a time.
