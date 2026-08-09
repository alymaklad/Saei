# Architecture

Technical deep-dive into how the Job Application Agent actually works under
the hood — one level below the [README](README.md), which covers setup and
usage. This document exists to explain the code itself: what each module in
`agents/` and `jobs/` does, how they call each other, and how a job posting
gets from "found on the internet" to "sitting in the database as a scored,
drafted, or auto-submitted application."

If you're trying to modify the system rather than just run it, start here.

## The short version

`jobs/` holds the entry points — the scripts that actually get executed,
either by hand, by `scheduler.py`, or by `api.py`. `agents/` holds the
capabilities those entry points compose together — search, scoring, CV
rewriting, applying, emailing, reporting. Neither package knows about the
other's internals beyond function signatures: a `jobs/` script imports the
`agents/` functions it needs and wires them to the database, and
`orchestrator.py` (in the project root, not inside either package) wires the
per-job decision logic — score, then rewrite-or-apply — into a small
LangGraph state machine that both `jobs/daily_run.py` and
`jobs/apply_from_link.py` call for every individual job.

```
jobs/daily_run.py ──┐
                     ├─> agents/search_agent.py (find raw jobs)
                     │
                     └─> for each new job:
                             orchestrator.py (per-job state machine)
                               ├─> agents/ats_agent.py       (score fit)
                               ├─> agents/cv_rewriter_agent.py (if low fit)
                               └─> agents/apply_agent.py      (if good fit)
                         └─> writes Job/Application/SkillGap rows to SQLite

jobs/daily_report.py ──> agents/reporter_agent.py  (reads today's Applications, sends Telegram)
jobs/weekly_news.py  ──> agents/news_agent.py + agents/reporter_agent.py
jobs/apply_from_link.py ──> same orchestrator.py path as daily_run.py, for one URL

scheduler.py  ──> calls all three jobs/*.py entry points on a cron schedule
api.py        ──> reads the same SQLite DB for the dashboard, and can invoke
                   jobs/daily_run.py's search on demand ("Search now" button)
```

## Entry points: `jobs/`

Everything in `jobs/` is a script with a `run_*()` function and an
`if __name__ == "__main__"` block, so each one is independently runnable from
the command line, importable by `scheduler.py`, and (for the search job)
callable from `api.py`. None of them contain business logic themselves —
they fetch data, hand it to an agent or the orchestrator, and persist the
result.

### `jobs/daily_run.py` — search, score, act

The main pipeline. `run_daily_search_and_apply()`:

1. Calls `db.init_db()` (creates tables if missing, seeds the default search
   sites — see `agents/search_agent.py` below).
2. Resolves the active CV via `cv_parser.find_default_cv("cv")` (looks for
   `cv/current_cv.pdf` or `cv/current_cv.docx`) and parses it to plain text
   with `cv_parser.parse_cv()`. Raises immediately if no CV exists.
3. Resolves `position` / `seniority` / `max_results_per_site` / `max_age_days`
   — each defaults to the matching `config.SEARCH_*` value (whatever's saved
   from the dashboard's Search tab) if not passed explicitly. This is what
   keeps a manual `python jobs/daily_run.py` run, the 8am scheduler run, and
   the dashboard's "Search now" button all searching the same thing without
   three separate config paths.
4. Calls `agents.search_agent.run_search(...)`, which returns
   `(found_jobs, source_errors)` — a flat list of raw job dicts from every
   configured source, and a list of any individual sources that failed
   (a dead Lever slug, a network timeout) without aborting the rest.
5. For each raw job with a URL, `_process_one_job()` handles dedupe and
   persistence — see below.
6. Returns `{"processed": [...], "errors": [...], "found": N,
   "source_errors": [...]}` — printed to stdout when run from the CLI,
   returned as JSON when triggered from `api.py`.

**`_process_one_job(raw_job, cv_text)`** is split into three short,
independent transactions rather than one transaction spanning the whole
thing — a deliberate fix, not the original design:

1. **`_insert_job_row()`** — dedupe-check (`_job_exists`, keyed on URL) plus
   insert the `Job` row, in one short `with get_session()` block that
   commits immediately. Returns `None` if the URL already exists, or if it
   lost a race to another process inserting the same URL concurrently
   (`Job.url`'s unique constraint raises `IntegrityError`, caught here).
   Committing early (rather than deferring the insert) is what makes the
   row's id available for the CV-output filename in step 2, without needing
   to hold this transaction open while that happens.
2. **`orchestrator_app.invoke(...)`** (see
   [Orchestrator](#orchestrator-the-per-job-state-machine) below) runs with
   **no open database transaction at all** — this is the important part. If
   it raises, **`_delete_job_row()`** removes the row step 1 just committed,
   so the URL is free to be retried on the next run instead of permanently
   stuck behind the dedupe check with no `Application` row to show for it.
3. **`_finalize_job()`** — a second short transaction inserting the
   `Application` row (plus a `SkillGap` row if the orchestrator surfaced
   missing skills).

This replaced an earlier version where the entire batch loop ran inside one
`with get_session()` block, with each job wrapped in only a `SAVEPOINT`
(`session.begin_nested()`) for isolation — meaning the single underlying
SQLite connection held its write lock from the first job's insert until the
*whole batch* finished, including every job's orchestrator call (multiple
LLM round-trips each). With dozens of jobs per run that could hold the lock
for minutes. SQLite only allows one writer at a time even in WAL mode (see
`db.py` below), so a second process writing during that window — the
scheduler's own automatic run overlapping a manual "Search now" click, for
example — failed outright with `sqlite3.OperationalError: database is
locked`, a failure actually hit in production. Splitting into three short,
independent transactions per job means the write lock is only ever held for
a single row write (milliseconds), so real overlap between two runs just
makes one wait briefly instead of erroring.

### `jobs/apply_from_link.py` — one URL, on demand

`apply_from_link(url, cv_path=None)` is the manual counterpart to the daily
batch job: paste a single job URL (from any site, not just
Greenhouse/Lever), and it runs that one job through the exact same
orchestrator path. It fetches the page with `requests` + `BeautifulSoup`,
guesses the source from the URL (`greenhouse.io` / `lever.co` / otherwise
`manual_link`), checks the `jobs` table for the same URL (returns
`{"status": "already_processed", ...}` if found instead of reprocessing),
inserts a `Job` row, and calls `orchestrator_app.invoke()` directly — no
`Application`/`SkillGap` bookkeeping wrapper like `daily_run.py` has, since
this is a one-off CLI tool, not a batch job the dashboard reports on.
Runnable as `python jobs/apply_from_link.py <url> [cv_path]`.

### `jobs/daily_report.py` — evening summary

`run_daily_report()` queries `Application` joined to `Job` for everything
created in the last 24 hours, builds a plain-text summary via
`agents.reporter_agent.build_daily_report()`, sends it with
`agents.reporter_agent.send_telegram_report()`, and — regardless of whether
the send succeeded, failed, or was a dry run — writes a `ReportLog` row
(`status` is one of `sent` / `dry_run` / `failed`) so the dashboard's Reports
tab has a permanent record of every attempt, not just the last one. A raised
exception from the Telegram call is caught and logged as `status="failed"`
rather than crashing the scheduler process.

### `jobs/weekly_news.py` — field news digest

`run_weekly_news_digest(field="software engineering")` calls
`agents.news_agent.fetch_news()` then `summarize_news()`, stores the summary
as a `NewsDigest` row, sends it over Telegram the same way
`daily_report.py` does, and logs a matching `ReportLog` row with the same
sent/dry_run/failed bookkeeping. `field` is currently a hardcoded default
rather than a dashboard setting — the module comment on `DEFAULT_FIELD` is
the whole configuration surface right now.

### How `scheduler.py` uses these

`scheduler.py` just imports `run_daily_search_and_apply`,
`run_daily_report`, and `run_weekly_news_digest` and registers them on
`APScheduler` cron triggers (8am / 8pm / Monday 9am). It has no logic of its
own beyond that — every actual behavior described above lives in `jobs/`.

It previously also scheduled a one-time "run all three jobs 1 minute after
this process starts" block on every restart, added to verify the automation
worked end-to-end without waiting for a real cron slot. That block is now
commented out (not deleted — see the comment in `scheduler.py` for how to
re-enable it) rather than removed outright, for two reasons: automation is
confirmed working, and it was a direct contributor to the `database is
locked` failures above — this test run firing while a manual "Search now"
click was already mid-batch was exactly the kind of process overlap that
surfaced the bug.

## Orchestrator: the per-job state machine

`orchestrator.py` (project root) is what `daily_run.py` and
`apply_from_link.py` both call once per job. It's a small
[LangGraph](https://langchain-ai.github.io/langgraph/) `StateGraph` — a
directed graph of Python functions sharing a typed state dict — rather than
a chain of nested if/else calls, mainly so the flow is inspectable and
easy to extend with new branches later without restructuring the whole thing.

```
        score
          │
   ats score < 0.7?
     ┌────┴────┐
    yes         no
     │           │
 rewrite_cv   decide_apply_path
     │           │
    END     ┌────┴────┐
        auto_submit  draft_for_review
             │           │
            END         END
```

State (`orchestrator.State`, a `TypedDict`): `job`, `cv_text`, `ats_result`,
`cv_rewritten`, `cv_path`, `apply_path`, `status`, `result`. Each node
function takes the state dict, mutates it, and returns it.

- **`score_node`** calls `agents.ats_agent.compute_ats_score(cv_text,
  job_description)` and stores the result.
- **`route_on_score`** — the conditional edge — sends the job to
  `rewrite_cv` if `ats_result["score"] < FIT_THRESHOLD` (0.7, a module
  constant), otherwise to `decide_apply_path`. This is the one place that
  threshold lives; there's no dashboard control for it currently.
- **`rewrite_node`** (only reached on low fit) calls
  `agents.cv_rewriter_agent.rewrite_cv()` then `save_cv_as_pdf()`, writes
  the output to `cv_output/cv_<job_id>.pdf`, and sets
  `status = "cv_rewritten_notify_user"`. The graph ends here deliberately —
  **a low-fit job is never auto-applied to**, even with the rewritten CV;
  it's surfaced to the user (via the Dashboard/CV tab) instead.
- **`decide_apply_path_node`** (only reached on good fit) calls
  `agents.apply_agent.decide_apply_path(job)`.
- **`route_on_apply_path`** sends the job to `auto_submit` or
  `draft_for_review` based on that decision.
- **`auto_submit_node`** — if `config.DRY_RUN` is true, just records intent
  (`status = "auto_submitted"`, `result["dry_run"] = True`) without calling
  anything real. If `DRY_RUN` is false, it calls
  `agents.apply_agent.auto_submit_greenhouse()` — which is a deliberate
  stub that raises `NotImplementedError` until someone hand-verifies a
  specific board's form fields and implements the real submit payload (see
  [Enabling auto-submit](README.md#enabling-auto-submit) in the README).
  Reaching this node at all already implies the job's source was in
  `WHITELISTED_SOURCES`, since `decide_apply_path` gates on that.
- **`draft_node`** calls `agents.apply_agent.draft_for_review()`, which just
  packages the job URL, CV path, and an empty cover letter into a dict with
  `status = "pending_review"` — this is what shows up in the Dashboard's
  Applications table waiting for the user to send it manually via the Email
  tab.

## Agents: `agents/`

Each file in `agents/` is a stand-alone capability with plain functions (no
classes, no shared base) — the orchestrator and `jobs/` scripts import
exactly the functions they need. None of them touch the database directly
except `search_agent.py` (for `SearchSite` config) — everything else is
pure input-in / result-out, which is what makes the orchestrator's node
functions thin wrappers around them.

### `agents/llm.py` — provider abstraction

`get_llm(temperature=0.0)` is the one function every other agent calls
instead of instantiating a LangChain chat model directly — swapping
providers is a one-line `.env` change (`LLM_PROVIDER`), not a code change
anywhere else. Three providers, selected by `config.LLM_PROVIDER`:

| Provider | Class | Notes |
|---|---|---|
| `ollama` (default) | `ChatOllama` | Local, free, needs `ollama serve` running + the model pulled. |
| `gemini` | `ChatGoogleGenerativeAI` | Hosted, free tier, needs `GEMINI_API_KEY`. Model hardcoded to `gemini-1.5-flash`. |
| `groq` | `ChatGroq` | Hosted, free tier, fastest of the three. Needs `GROQ_API_KEY`. `max_tokens=4096` is set explicitly — without a cap, a long prompt (full CV + job description) can exhaust Groq's default token budget before emitting any content. For `gpt-oss` models specifically, `reasoning_effort="low"` is also set, because those models bill hidden internal reasoning as completion tokens — verified directly that a trivial 2-line rewrite spent ~85% of its budget on invisible reasoning before this flag was added, sometimes returning `content=""` with `finish_reason="length"` and no error. |

All three are switchable live from the dashboard's Settings page as well as
`.env` (see `env_store.py` and `api.py`'s `/api/settings` endpoint).

### `agents/search_agent.py` — pulling jobs from every configured source

The largest file in `agents/` (23KB) because it fans out to seven different
free sources and normalizes/filters everything into one shape. Read
top-to-bottom, its job is: fetch raw job listings from wherever's
configured, filter them by title/seniority/age, cap how many are kept per
source, and tag each with which source it came from (used later by
`apply_agent.py` for whitelist checks).

**Sources, in the order `run_search()` queries them:**

1. **Greenhouse public API** (`search_greenhouse(board_token)`) — no key
   needed, hits `boards-api.greenhouse.io`. Board tokens come from
   `config.GREENHOUSE_BOARD_TOKENS` (`.env`) and from any dashboard-added
   `SearchSite` row whose URL was detected as a Greenhouse board.
2. **Lever public API** (`search_lever(company)`) — same idea, `.env`'s
   `LEVER_COMPANY_SLUGS` plus dashboard-added Lever sites.
3. **RemoteOK** (`search_remoteok(position)`) — RemoteOK's own free public
   JSON feed (`remoteok.com/api`), no key/auth needed. Always queried, no
   per-user config — the response's first element is RemoteOK's
   legal/attribution notice rather than a job, filtered out naturally by a
   `job.get("id") and job.get("position")` guard rather than a hardcoded
   "skip index 0". The endpoint has no real search parameter, so `position`
   narrows results client-side via `_matches_position` (see below).
4. **We Work Remotely** (`search_weworkremotely()`) — WWR publishes each job
   category as a plain RSS 2.0 feed (`weworkremotely.com/categories/
   remote-programming-jobs.rss` by default), parsed with the stdlib
   `xml.etree.ElementTree` rather than scraped as HTML. WWR titles each
   listing `"Company: Job Title"`; that's split on the first `": "` into
   separate `company`/`title` fields to match every other source's shape,
   falling back to the whole string as the title if a listing doesn't
   follow that convention.
5. **Google Sheets watchlist** (`search_from_watchlist()` /
   `read_watchlist_sheet()`) — an optional manually-curated sheet
   (`config.GOOGLE_SHEETS_ID`) with columns `company | role_keyword |
   greenhouse_board_token | lever_company_slug`; each row is searched via
   whichever of Greenhouse/Lever/SerpAPI applies to it. A single bad row
   (missing creds, bad slug) is skipped, not fatal to the rest.
6. **Dashboard-added sites** (`get_configured_sites()`, the `search_sites`
   table via `models.SearchSite`) — each row is dispatched based on
   `site_type`:
   - `greenhouse` / `lever` → reuse the API-backed functions above.
   - a `KNOWN_JOB_BOARD_TEMPLATES` key (`wuzzuf`, `bayt`, `simplyhired`,
     `wellfound`, `gulftalent`) → the per-template `build_url(position)`
     callable rebuilds a real, query-filtered search URL fresh every run
     (see below), then scrapes it with `search_generic_site()`.
   - anything else → `search_generic_site()` directly on the stored URL.
7. **SerpAPI / Google Jobs** (`search_serpapi(query)`) — optional, free tier
   100 searches/month, skipped entirely if `config.SERPAPI_KEY` is unset.
   Query is built from `seniority` + `position` combined.

RemoteOK and We Work Remotely are always-on structured sources like
Greenhouse/Lever rather than `SearchSite` rows: no per-user config, and no
way to remove them from the Search tab short of a code change, since (unlike
the `KNOWN_JOB_BOARD_TEMPLATES` sites) they were never added as rows in the
first place.

**`search_generic_site(url, position, max_candidates)`** is the fallback for
any career-page URL that isn't Greenhouse/Lever: it fetches the page,
collects same-domain `<a>` links whose href/text look job-related
(`JOB_LINK_KEYWORDS`), optionally narrows to ones matching `position`, caps
to `max_candidates` (or `GENERIC_SITE_MAX_CANDIDATES` = 15) since each
candidate costs a real HTTP fetch, then fetches each candidate page's text
as its "description." This is explicitly noisier than the API-backed paths —
treat results as leads, not a guaranteed feed — and the docstring notes to
check a site's Terms of Service before relying on it.

**`KNOWN_JOB_BOARD_TEMPLATES`** — `wuzzuf`, `bayt`, `simplyhired`,
`wellfound`, `gulftalent` — are job boards verified by hand (plain
unauthenticated HTTP GET returns real job links, server-rendered, no JS
needed) to be safely scrapable. Each has a `build_url(position)` lambda
rather than a naive `?q={query}` string, because query filtering behavior
turned out to differ per site:

- Wuzzuf's and SimplyHired's `?q=` genuinely filter server-side.
- Bayt's `?keyword=` silently ignores the query and serves unrelated
  generic listings — confirmed by testing (a search for "Ai Engineer"
  returned "Civil Rights Attorney"). Bayt's real filtering only works
  through its SEO slug pages (`/en/international/jobs/<slug>-jobs/`), so
  that's what `build_url` builds instead.
- GulfTalent's `?keyword=` has the same problem as Bayt originally did —
  confirmed by testing: it silently redirects to the same unfiltered "all
  jobs" listing regardless of the query. Rather than surface irrelevant
  jobs, its `build_url` ignores `position` entirely and always points at
  GulfTalent's own Software category page, leaning on `run_search()`'s
  global position-filter (applied to the combined result set at the end)
  to narrow it down — so unlike every other template here, this one is
  only useful while searching software-adjacent titles.
- Wellfound has no free-text query at all — it's a curated role taxonomy
  (`/role/r/<role-slug>`) — so `build_url` slugifies `position` into that
  URL shape. This matches Wellfound's taxonomy for common titles
  ("Software Engineer" → `software-engineer`, confirmed working, 1,827
  real results) but won't for unusual ones; an unmatched slug just returns
  an empty page rather than wrong results.

LinkedIn, Indeed, and NaukriGulf were tested the same way and all failed —
LinkedIn and Indeed block unauthenticated requests outright (HTTP 999 and
403 respectively), and NaukriGulf serves a JS-only shell with no listings
in the raw HTML — so all three are deliberately excluded; adding any of
them manually via the Search tab wouldn't work either.

**`seed_default_search_sites()`** inserts each `KNOWN_JOB_BOARD_TEMPLATES`
entry as an ordinary, deletable `SearchSite` row the first time it exists
(called from `db.init_db()`), tracked per-template-key against
`config.SEARCH_DEFAULT_SITES_SEEDED` (a set of already-seeded keys) rather
than "is the table empty" — the latter would silently never fire for a user
who'd already added their own sites before this feature existed. Per-key
tracking (rather than one global bool) also means a template added later —
e.g. `simplyhired`/`wellfound`/`gulftalent` landing in this file after an
install already had `wuzzuf`/`bayt` seeded under the old boolean flag —
still gets seeded on the next run instead of the old all-or-nothing flag
permanently blocking it. `config.py::_seeded_template_keys()` migrates a
legacy `SEARCH_DEFAULT_SITES_SEEDED=true` value to exactly `{"wuzzuf",
"bayt"}` (what "true" used to mean, back when those were the only two
templates), rather than treating it as "every template, including ones that
don't exist yet, is already seeded."

**Filtering, applied in this order** (order matters — see the code comment
on `_filter_relevant`):

1. `_filter_relevant()` — position and seniority
   (`SENIORITY_KEYWORDS` heuristics — e.g. "senior" / "sr." for Senior,
   "intern" for Intern; "mid" has no positive keyword list, it matches
   titles that hit *none* of the other levels' keywords), applied to each
   source's **raw, uncapped** results. Position matching (`_matches_position`)
   is word-based rather than a literal phrase match: a title matches if it
   contains every significant word from the query, in any order —
   searching "AI Engineer" (query words `{"ai", "engineer"}`) matches "AI
   Software Engineer", "AI/ML Software Engineer", "Gen AI Engineer", and
   "Gen AI/Agentic AI Engineer" alike, none of which contain "ai engineer"
   as one contiguous phrase. This replaced a literal substring check that
   silently missed exactly these cases.
2. `_cap()` — truncates to `max_results_per_site`, applied **after** step 1.
   This ordering was a deliberately fixed bug: capping before filtering on a
   545-job Greenhouse board (returned roughly alphabetically by title) once
   zeroed out all 38 real "Software Engineer" matches, because none of them
   happened to fall within the first 100 raw results.
3. `_within_max_age()` — applied last, across the whole combined result set.
   Only enforced where a real posted date can be extracted
   (`_extract_posted_at()`: Greenhouse's `first_published` ISO date, Lever's
   epoch-millisecond `createdAt`, or SerpAPI's relative text like "3 days
   ago" via `_parse_relative_date()`). Jobs with no extractable date are
   always kept, never excluded — this matters most for `search_generic_site`
   results, which have no structured date at all.

`WHITELISTABLE_SOURCES = {"greenhouse", "lever"}` is also defined here (not
in `apply_agent.py`) since it's fundamentally about what a *source* can
support, not about apply-path decision logic — `apply_agent.py` imports it.

### `agents/ats_agent.py` — scoring fit

`compute_ats_score(cv_text, job_description)` is the entry point the
orchestrator's `score_node` calls. It's a 50/50 hybrid of two independently
computed scores, on the stated theory that keyword overlap mirrors how real
ATS systems filter, while an LLM call captures context/seniority fit that
keyword matching alone would miss:

1. **`extract_required_skills()`** — one LLM call asking for a JSON array of
   required/preferred skills from the job description. `_safe_json_list()`
   regex-extracts a `[...]` block from the response even if the model wraps
   it in prose or a code fence, and falls back to an empty list on a parse
   failure rather than raising.
2. **`keyword_overlap_score()`** — deterministic: fraction of the extracted
   skills that appear as a substring in the (lowercased) CV text. `0.0` if
   no skills were extracted.
3. **`llm_fit_score()`** — a second LLM call asking for a single 0–1 number
   representing overall fit (given the full CV and job description text
   together). Regex-extracts the first number found in the response;
   defaults to `0.5` if nothing parses.
4. Final score: `0.5 * keyword_score + 0.5 * llm_score`. Returns a dict with
   `score`, `keyword_score`, `llm_score`, `missing_skills` (extracted skills
   not found in the CV — this is what feeds both the rewriter and the
   Skill Gap tracking), and `required_skills`.

Two LLM calls per job scored is the main cost driver of a search run — this
is why "Max results per site" exists on the Search tab, to bound how many
jobs get this treatment in one run.

### `agents/cv_rewriter_agent.py` — tailoring the CV for low-fit jobs

Only reached when `ats_result["score"] < 0.7`. Two responsibilities: get the
LLM to rewrite the CV text, then render that text into a real PDF matching
the look of the user's original CV.

**`rewrite_cv(cv_text, job_description, missing_skills)`** — one LLM call
(`temperature=0.3`, the only agent that doesn't use `0.0`, since some
rephrasing variety is wanted here) with a system prompt that is explicit
about the integrity rule: incorporate keywords the candidate genuinely has
experience with, restructure around measurable impact, and **never fabricate
missing skills into the CV** — they're reported separately instead. The
prompt also pins down an exact plain-text layout (name line, contact line,
blank line, ALL-CAPS section headers, `- ` bulleted items, no markdown
symbols) so the output can be parsed by the PDF renderer below without any
markdown-to-PDF conversion step. If the LLM returns empty content (seen in
practice with Groq's `gpt-oss` models running out of their reasoning token
budget — see `llm.py` above), this raises a `RuntimeError` with an
actionable message instead of silently writing a blank PDF, which is what
used to happen before this check existed.

**`save_cv_as_pdf(cv_text, output_path)`** — renders that plain text into a
formatted PDF with `reportlab`, styled to visually echo the user's uploaded
CV: centered name in a navy accent color (`CV_ACCENT_COLOR = "#1F3A5F"`,
sampled directly from the original CV's own divider-rule color via
`pdfplumber`), a muted contact line beneath it, section headers in the same
accent color each followed by a full-width horizontal rule, and `- `-prefixed
lines rendered as bulleted paragraphs. It walks the text line-by-line,
classifying each line via `_looks_like_header()` (matches a fixed keyword
set like "experience"/"education", or a short all-caps line with no
sentence punctuation) and `_looks_like_bullet()` (starts with `-`, `*`, `•`,
or `–`), falling back to a plain body paragraph otherwise. Uses Helvetica
throughout rather than trying to match the original's exact font, since
embedding non-free fonts (e.g. Calibri) isn't viable — the goal is matching
the dominant visual signature, not pixel-identical reproduction.

`_sanitize_for_pdf_font()` / `_PDF_UNSAFE_PUNCTUATION` exist because
reportlab's base Helvetica font only supports the WinAnsi character set, and
LLMs routinely emit "typographically correct" Unicode punctuation (Unicode
hyphen variants, the minus sign) that silently falls back to a
ZapfDingbats/Symbol glyph instead of erroring — rendering as a solid black
square in the output PDF. This was a real, reproduced bug (confirmed by
building a test PDF and checking which font reportlab actually used
per-glyph), and the fix normalizes those characters to plain ASCII (plus a
few other Unicode punctuation marks that render fine but are normalized
anyway for consistency/defense) before layout.

`save_cv_as_docx()` also exists (renders the same text into a `.docx` via
`python-docx`) but isn't currently wired into the orchestrator — only the
PDF path is.

### `agents/apply_agent.py` — whitelist-only auto-submit gate

This is the file that enforces the project's core safety rule, and it's
deliberately small. `decide_apply_path(job)`:

1. If `job["source"]` isn't in `search_agent.WHITELISTABLE_SOURCES`
   (`{"greenhouse", "lever"}`), the answer is always `"draft_for_review"` —
   no other source type can ever auto-submit, full stop.
2. Otherwise, builds a `"source:identifier"` key via `_job_whitelist_key()`
   (identifier is the job's board token, company slug, or watchlist company,
   whichever is present) and checks whether that exact key, *or* the bare
   source name, is in `config.WHITELISTED_SOURCES` (parsed from `.env`'s
   comma-separated `WHITELISTED_SOURCES`). Match → `"auto_submit"`. No
   match → `"draft_for_review"`.

**`auto_submit_greenhouse()`** is an intentional stub that always raises
`NotImplementedError`. The docstring explains why it can't be generic:
different Greenhouse/Lever boards define different custom application
fields (screening questions, EEO fields), so there's no one payload that
works everywhere — a real implementation has to be hand-verified per board
before that board is added to the whitelist. This is the one piece of the
system the README explicitly calls out as meant to be implemented and
tested per employer, not automated blindly.

**`draft_for_review(job, cv_path, cover_letter)`** is the far more commonly
hit path (every non-whitelisted source, which by default is *everything*
since `WHITELISTED_SOURCES` starts empty) — it just packages the job URL,
CV path, and cover letter into a dict with `status: "pending_review"`. This
becomes an `Application` row the Dashboard shows and the Email tab can send.

### `agents/email_agent.py` — sending drafted applications

Gmail API via OAuth (the user's own Gmail account, no per-email cost,
replacing a paid SendGrid option from the original design). Three concerns:

- **Auth state without side effects.** `credentials_configured()` just
  checks whether `config.GMAIL_CREDENTIALS_JSON` (the OAuth client file
  downloaded from Google Cloud Console) exists on disk.
  `is_authenticated()` checks whether a cached, still-usable token exists at
  `config.GMAIL_TOKEN_JSON` — used by `GET /api/email/status` so the
  frontend can show "Connected" without popping open a browser window just
  to check.
- **`get_gmail_service()`** does the actual OAuth dance: loads a cached
  token if present, refreshes it if expired-but-refreshable, otherwise runs
  `InstalledAppFlow.run_local_server()` which opens a real browser tab for
  consent. This blocks the calling thread until the user finishes (or
  abandons) the flow, bounded by `GMAIL_AUTH_TIMEOUT_SECONDS` (180s) so an
  abandoned tab can't hang a request forever — `api.py` runs this inside
  FastAPI's threadpool for exactly that reason. A successful flow writes the
  token to `GMAIL_TOKEN_JSON` so future calls skip the browser entirely.
- **`send_application_email(to_email, subject, body, cv_path)`** — respects
  the global `config.DRY_RUN` flag: if true, returns a dict describing the
  intended send without touching Gmail at all. If false, builds a
  `MIMEMultipart` message with the CV file attached and sends it via
  `service.users().messages().send()`. Every call site (`api.py`'s email
  endpoint) wraps this in the `EmailLog` bookkeeping regardless of outcome,
  so the Email tab's history is a real log, not just a cache of the last
  session.

### `agents/reporter_agent.py` — Telegram delivery + daily summary text

Telegram Bot API, chosen specifically because it's free and unlimited
forever, unlike Twilio WhatsApp (which only offers a small one-time trial
credit, not a real free tier). Two functions:

- **`send_telegram_report(report_text)`** — same `DRY_RUN` short-circuit
  pattern as the email agent. If not dry-run, POSTs to
  `https://api.telegram.org/bot<token>/sendMessage`. Returns an `{"error":
  ...}` dict (not an exception) if the bot token/chat ID aren't configured,
  which is what `jobs/daily_report.py` and `jobs/weekly_news.py` check to
  decide `status = "failed"` for the `ReportLog` row.
- **`build_daily_report(applications_today)`** — pure text formatting, one
  bullet line per application (`title at company (status)`), or a fixed
  "No applications submitted today" string if the list is empty.

### `agents/news_agent.py` — weekly field news

Also SerpAPI-based (Google News engine this time), also free-tier and
optional. **`fetch_news(field)`** returns `[]` immediately if
`config.SERPAPI_KEY` is unset — no error, just nothing to summarize.
Otherwise fetches up to 15 results. **`summarize_news(field, articles)`**
hands the title+snippet of each article to the LLM with a prompt asking for
a themed, 5–8 bullet-point summary; returns a canned "no news fetched"
string if `articles` is empty rather than making an LLM call with nothing to
summarize.

### `agents/skill_gap_agent.py` — aggregating missing skills

The smallest file in `agents/` (711 bytes) and the only one with zero
external dependencies (no LLM call, no HTTP request) — pure aggregation.
**`summarize_skill_gaps(job_scores)`** takes a list of dicts (each with a
`missing_skills` list, as produced by `ats_agent.compute_ats_score()`),
flattens and counts every missing skill across all of them with
`collections.Counter`, and formats the top 10 as `"- <skill>: missing in N
of M jobs reviewed"`. Note this function isn't currently called from any
`jobs/` script directly — the Dashboard's skill-gap display is built from
the `SkillGap` table rows (one per job, written by `daily_run.py`) rather
than calling this aggregator live; it exists as the shared summarization
logic `api.py` could call, or that a future job could call to feed straight
into a report.

## Supporting modules (referenced above, not the focus of this doc)

These live in the project root, not in `agents/` or `jobs/`, but every
agent and job depends on them:

- **`config.py`** — the only place `os.getenv()` is called; every other
  module imports typed values from here instead of reading `.env` directly.
- **`db.py`** — SQLAlchemy engine/session setup (SQLite,
  `expire_on_commit=False` so rows built inside a `with get_session()` block
  are still readable after it exits). `init_db()` creates tables and calls
  `search_agent.seed_default_search_sites()`. The engine is created with
  `connect_args={"timeout": 30}` and a `connect` event listener sets
  `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=30000` on every
  connection — both layers exist because this app routinely has multiple
  processes open on the same SQLite file at once (`api.py` + `scheduler.py`,
  both started by `run.bat`; a manual CLI run on top of either), and
  SQLite's defaults (`busy_timeout=0`, rollback-journal mode locking the
  whole file per writer) turned real overlap between them into immediate
  `database is locked` failures instead of a brief wait. See the
  `jobs/daily_run.py` section above for the other half of this fix — a
  long-held transaction can still exceed even a generous timeout, which is
  why that file also splits its writes into short transactions rather than
  relying on this alone.
- **`models.py`** — the SQLAlchemy schema: `Job`, `Application`,
  `SkillGap`, `NewsDigest`, `EmailLog`, `SearchSite`, `ReportLog`. `Job.url`
  is the unique dedupe key referenced throughout `jobs/`.
- **`cv_parser.py`** — `parse_cv()` (PDF via `pdfplumber`, `.docx` via
  `python-docx`) and `find_default_cv()` (looks for
  `cv/current_cv.pdf`/`.docx`), used by every entry point in `jobs/` that
  needs CV text.
- **`env_store.py`** — line-preserving `.env` reader/writer used by
  `api.py`'s Settings endpoint, so saving a setting doesn't clobber comments
  or unrelated keys.
- **`orchestrator.py`** — see [Orchestrator](#orchestrator-the-per-job-state-machine)
  above.

## Data model quick reference

| Table | Written by | Purpose |
|---|---|---|
| `jobs` | `jobs/daily_run.py`, `jobs/apply_from_link.py` | One row per job seen, ever. `url` is the dedupe key. |
| `applications` | `jobs/daily_run.py` (`_process_one_job`) | One row per job that made it through the orchestrator — status tracks `scored_low` / `pending_review` / `auto_submitted` / etc. |
| `skill_gaps` | `jobs/daily_run.py` | One row per job with missing skills, JSON-encoded list. |
| `news_digests` | `jobs/weekly_news.py` | One row per weekly digest generated. |
| `email_logs` | `api.py`'s email-send endpoint | One row per send *attempt* (sent/dry_run/failed), not just successes. |
| `search_sites` | dashboard Search tab, seeded by `search_agent.seed_default_search_sites()` | User-managed extra sites to search, beyond `.env`'s Greenhouse/Lever lists. |
| `report_logs` | `jobs/daily_report.py`, `jobs/weekly_news.py` | One row per report send attempt (daily or weekly). |

## Safety mechanisms, and where they live in the code

- **Whitelist-only auto-submit** — enforced in exactly one place,
  `agents/apply_agent.py::decide_apply_path`. Nothing else in the codebase
  can cause a real submission.
- **Dry-run** — checked independently in three places that each perform a
  real external side effect: `orchestrator.py::auto_submit_node`,
  `agents/email_agent.py::send_application_email`, and
  `agents/reporter_agent.py::send_telegram_report`. There's no single
  global "dry-run wrapper" — each side-effecting function checks
  `config.DRY_RUN` itself.
- **Never fabricate CV content** — enforced only by the system prompt in
  `agents/cv_rewriter_agent.py::rewrite_cv`; there's no programmatic check
  that the LLM actually complied, so this is a prompting guarantee, not a
  code-level one.
- **Idempotency** — `jobs/daily_run.py::_job_exists` (and the equivalent
  check in `apply_from_link.py`) — a job's URL is checked against the `jobs`
  table before any processing happens, so re-running the search job never
  double-processes the same posting. This also covers jobs that failed
  mid-processing: `_delete_job_row()` removes a job's row if the
  orchestrator raises after it was inserted, so a failed job is retried on
  the next run instead of being silently and permanently skipped by this
  same check.
- **Partial-failure isolation** — `daily_run.py::_process_one_job` writes
  each job in short, independent transactions (see above) rather than one
  shared transaction or SAVEPOINT, and `search_agent.run_search()` wraps
  each source in its own try/except — one bad job or one dead source
  degrades that one item, not the whole run.
