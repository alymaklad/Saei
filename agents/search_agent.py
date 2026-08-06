"""
Search Agent.

Free sources, in priority order:
  1. Greenhouse public API  — no key needed
  2. Lever public API       — no key needed
  3. RemoteOK JSON feed     — no key needed, official public API (see search_remoteok)
  4. We Work Remotely RSS   — no key needed, official public per-category feed (see search_weworkremotely)
  5. Google Sheets watchlist — free (Google Cloud service account)
  6. Sites added from the dashboard's Search tab — Greenhouse/Lever URLs reuse
     the API-backed paths above; verified job-board templates (Wuzzuf,
     Bayt.com, GulfTalent, SimplyHired, Wellfound -- see
     KNOWN_JOB_BOARD_TEMPLATES) rebuild their URL from the `position` field
     each run; anything else falls back to a best-effort generic scrape (see
     search_generic_site). All five templates are pre-added as default rows
     the first time the app runs (seed_default_search_sites) but are just
     regular rows after that -- removable from the Search tab like any other
     site.
  7. SerpAPI (Google Jobs)  — free tier, 100 searches/month, optional

RemoteOK and We Work Remotely are always-on structured sources (like
Greenhouse/Lever) rather than SearchSite rows -- they need no per-user
config and aren't Greenhouse/Lever, so they can never be whitelisted for
auto-submit (see WHITELISTABLE_SOURCES), same as google_jobs/generic.
"""
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urljoin, urlparse

import requests

import config

WHITELISTABLE_SOURCES = {"greenhouse", "lever"}  # only these CAN ever be whitelisted for auto-submit

GREENHOUSE_URL_RE = re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([a-zA-Z0-9_-]+)", re.I)
LEVER_URL_RE = re.compile(r"jobs\.lever\.co/([a-zA-Z0-9_-]+)", re.I)

# Heuristics for the generic-site scraper -- arbitrary career pages have no
# consistent structure, so this is a best-effort signal, not a guarantee.
JOB_LINK_KEYWORDS = ("job", "career", "position", "opening", "vacan", "apply", "req")
GENERIC_SITE_MAX_CANDIDATES = 15
GENERIC_SITE_TIMEOUT = 10
GENERIC_SITE_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JobApplicationAgent/1.0)"}

# Seniority filter: job titles are matched against these keyword lists.
# "mid" has no reliable keyword of its own -- most unlabeled titles ("Software
# Engineer" with no qualifier) are mid-level in practice, so it's treated as
# "doesn't match any other level's keywords" rather than its own positive list.
SENIORITY_KEYWORDS = {
    "intern": ["intern", "internship", "co-op", "coop"],
    "entry": ["entry level", "entry-level", "junior", "jr.", "new grad", "new-grad", "graduate"],
    "senior": ["senior", "sr."],
    "lead": ["lead", "staff", "principal", "architect"],
    "manager": ["manager", "director", "head of", "vice president", " vp ", "chief"],
}
SENIORITY_LABELS = {
    "intern": "Intern",
    "entry": "Entry Level",
    "mid": "Mid Level",
    "senior": "Senior",
    "lead": "Lead",
    "manager": "Manager",
}


def _matches_seniority(title: str, level: str) -> bool:
    haystack = (title or "").lower()
    if level == "mid":
        return not any(kw in haystack for kws in SENIORITY_KEYWORDS.values() for kw in kws)
    return any(kw in haystack for kw in SENIORITY_KEYWORDS.get(level, []))


def _cap(jobs: list[dict], limit: int | None) -> list[dict]:
    """Keeps only the first `limit` raw results from one source. None/0/negative
    means no cap -- the original "pull everything" behavior."""
    if limit and limit > 0:
        return jobs[:limit]
    return jobs


# Position matching is token-based rather than a literal substring check,
# because job titles routinely phrase the same role differently than the
# query without ever containing it as one contiguous phrase -- e.g.
# searching "AI Engineer" should match "AI Software Engineer", "AI/ML
# Software Engineer", "Gen AI Engineer", and "Gen AI/Agentic AI Engineer"
# alike, none of which contain the literal substring "ai engineer" (the
# words "AI" and "Engineer" are separated by other words, or in a different
# order). A title matches if it contains every significant word from the
# query, in any order and not necessarily adjacent -- a small, deliberately
# simple heuristic (same spirit as SENIORITY_KEYWORDS below) rather than a
# synonym dictionary, since decomposing into words already covers the
# common "role phrased differently" cases above for free.
_POSITION_STOPWORDS = {"a", "an", "and", "or", "the", "of", "in", "for", "with", "to"}
_WORD_RE = re.compile(r"[a-z0-9]+")


def _position_tokens(text: str) -> set[str]:
    words = _WORD_RE.findall((text or "").lower())
    return {w for w in words if w not in _POSITION_STOPWORDS}


def _matches_position(title: str, position: str) -> bool:
    query_tokens = _position_tokens(position)
    if not query_tokens:
        return True  # blank position, or pure punctuation -- no filter
    return query_tokens.issubset(_position_tokens(title))


def _filter_relevant(jobs: list[dict], position: str, seniority: str) -> list[dict]:
    """Position/seniority title filters, applied to a source's raw job list
    BEFORE _cap() truncates it -- capping first can silently discard every
    real match on a large board. Confirmed in practice: Greenhouse returns a
    company's jobs in a roughly alphabetical-by-title order, so on a
    545-job board, capping to the first 100 (a completely normal "Max
    results per site" setting) produced zero "Software Engineer" matches
    out of 38 real ones, because every single match sat past index 100 --
    that's what was actually behind a real "search returns nothing" report."""
    if position:
        jobs = [j for j in jobs if _matches_position(j.get("title"), position)]
    if seniority:
        jobs = [j for j in jobs if _matches_seniority(j.get("title"), seniority)]
    return jobs


# Posted-date extraction: each source exposes "when was this posted"
# differently, so max-age filtering only works where we can actually find a
# date. Jobs with no recognizable date are always kept (never excluded) since
# their age genuinely can't be verified -- this is most relevant for the
# generic scraper, which has no structured date at all.
RELATIVE_DATE_RE = re.compile(r"(\d+)\+?\s*(hour|day|week|month)s?\s+ago", re.I)


def _parse_relative_date(text: str) -> datetime | None:
    """Parses SerpAPI/Google Jobs' detected_extensions.posted_at strings,
    e.g. "3 days ago", "Today", "30+ days ago"."""
    if not text:
        return None
    t = text.strip().lower()
    if t in ("today", "just posted", "just now"):
        return datetime.now(timezone.utc)
    match = RELATIVE_DATE_RE.search(t)
    if not match:
        return None
    n, unit = int(match.group(1)), match.group(2)
    delta = {
        "hour": timedelta(hours=n),
        "day": timedelta(days=n),
        "week": timedelta(weeks=n),
        "month": timedelta(days=n * 30),
    }[unit]
    return datetime.now(timezone.utc) - delta


def _extract_posted_at(job: dict) -> datetime | None:
    # Greenhouse: ISO 8601 with offset. first_published is the original post
    # date; updated_at changes whenever the listing is edited, so it's only
    # used as a fallback.
    for key in ("first_published", "updated_at"):
        value = job.get(key)
        if value:
            try:
                return datetime.fromisoformat(value)
            except (ValueError, TypeError):
                pass

    # Lever: createdAt is epoch milliseconds.
    created_at = job.get("createdAt")
    if created_at:
        try:
            return datetime.fromtimestamp(int(created_at) / 1000, tz=timezone.utc)
        except (ValueError, TypeError, OSError, OverflowError):
            pass

    # RemoteOK (ISO 8601, e.g. "2024-06-01T12:00:00+00:00") and We Work
    # Remotely (RFC 822 pubDate, e.g. "Mon, 01 Jan 2024 00:00:00 +0000") both
    # normalize their post date into this same "date" key -- see
    # search_remoteok()/search_weworkremotely() -- so both formats are tried
    # here rather than needing a source-specific branch above.
    date_value = job.get("date")
    if date_value:
        try:
            return datetime.fromisoformat(date_value)
        except (ValueError, TypeError):
            pass
        try:
            return parsedate_to_datetime(date_value)
        except (ValueError, TypeError):
            pass

    # SerpAPI / Google Jobs: relative text, e.g. "3 days ago".
    posted_at = (job.get("detected_extensions") or {}).get("posted_at")
    if posted_at:
        parsed = _parse_relative_date(posted_at)
        if parsed:
            return parsed

    return None


def _job_age_days(job: dict) -> float | None:
    posted_at = _extract_posted_at(job)
    if not posted_at:
        return None
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - posted_at).total_seconds() / 86400


def _within_max_age(job: dict, max_age_days: int | None) -> bool:
    if not max_age_days or max_age_days <= 0:
        return True
    age = _job_age_days(job)
    return age is None or age <= max_age_days  # unknown age -- don't exclude it


def parse_site_url(url: str) -> tuple[str, str | None]:
    """
    Detects a pasted Greenhouse/Lever board URL and extracts its board
    token/company slug, so it can reuse the reliable API-backed search
    functions instead of falling back to generic scraping. Anything else is
    tagged "generic".
    """
    match = GREENHOUSE_URL_RE.search(url)
    if match:
        return "greenhouse", match.group(1)
    match = LEVER_URL_RE.search(url)
    if match:
        return "lever", match.group(1)
    return "generic", None


def search_greenhouse(board_token: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    return resp.json().get("jobs", [])


def search_lever(company: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    return resp.json()


def search_remoteok(position: str = "") -> list[dict]:
    """
    RemoteOK's free public JSON feed (https://remoteok.com/api) -- no key,
    no auth required, and explicitly published for this kind of use (linked
    from RemoteOK's own nav as "JSON feed"). Returns RemoteOK's current
    developer-jobs feed; `position` narrows it via `_matches_position`
    (word-based, not a literal phrase match), since this endpoint doesn't
    expose a real query/search parameter (only tag filtering, and there's
    no reliable way to map arbitrary free-text position to its tag
    taxonomy).

    The response's first element is RemoteOK's own legal/attribution
    notice, not a job -- it has no "id"/"position" field, so the
    `job.get("id") and job.get("position")` guard below excludes it
    naturally rather than needing a hardcoded "skip index 0".
    """
    resp = requests.get(
        "https://remoteok.com/api",
        timeout=20,
        headers={**GENERIC_SITE_HEADERS, "Accept": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []

    jobs = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        title = entry.get("position")
        if not entry.get("id") or not title:
            continue  # the leading legal-notice object, or a malformed row
        if not _matches_position(title, position):
            continue
        jobs.append({
            "title": title,
            "company": entry.get("company", ""),
            "url": entry.get("url") or f"https://remoteok.com/remote-jobs/{entry['id']}",
            "description": entry.get("description", ""),
            "date": entry.get("date"),  # ISO 8601 -- see _extract_posted_at
        })
    return jobs


def search_weworkremotely(category: str = "remote-programming-jobs") -> list[dict]:
    """
    We Work Remotely publishes each job category as a plain RSS 2.0 feed
    (e.g. /categories/remote-programming-jobs.rss) -- no key, no auth,
    structured XML rather than an HTML page to scrape. `category` defaults
    to programming jobs since that's the relevant category for this app;
    WWR's other categories (design, devops, marketing, etc.) follow the
    same URL pattern if ever needed.

    WWR titles its listings "Company: Job Title" -- split on the first ": "
    below so title/company come back as separate fields like every other
    source here, falling back to the whole string as the title (blank
    company) if a listing doesn't follow that convention.
    """
    url = f"https://weworkremotely.com/categories/{category}.rss"
    resp = requests.get(url, timeout=20, headers=GENERIC_SITE_HEADERS)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    jobs = []
    for item in root.iter("item"):
        raw_title = (item.findtext("title") or "").strip()
        if not raw_title:
            continue
        if ": " in raw_title:
            company, _, title = raw_title.partition(": ")
        else:
            company, title = "", raw_title
        jobs.append({
            "title": title,
            "company": company,
            "url": (item.findtext("link") or "").strip(),
            "description": (item.findtext("description") or "").strip(),
            "date": (item.findtext("pubDate") or "").strip() or None,  # RFC 822 -- see _extract_posted_at
        })
    return jobs


def _looks_like_job_link(href: str, text: str) -> bool:
    haystack = f"{href} {text}".lower()
    return any(keyword in haystack for keyword in JOB_LINK_KEYWORDS)


def search_generic_site(url: str, position: str = "", max_candidates: int | None = None) -> list[dict]:
    """
    Best-effort scraper for career-page URLs that aren't Greenhouse/Lever.
    Finds same-domain links that look job-related (by URL/text keywords),
    optionally narrowed to ones matching `position`, capped to a handful of
    candidates to stay fast and polite (`max_candidates`, falling back to
    GENERIC_SITE_MAX_CANDIDATES if unset -- always capped, since each
    candidate costs a real HTTP fetch), then fetches each candidate's page
    text as its description. Noisier than the Greenhouse/Lever path -- treat
    results as leads to review, not a guaranteed feed. Respect each site's
    Terms of Service before relying on this for a site that prohibits scraping.
    """
    try:
        resp = requests.get(url, timeout=GENERIC_SITE_TIMEOUT, headers=GENERIC_SITE_HEADERS)
        resp.raise_for_status()
    except requests.RequestException:
        return []

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")
    domain = urlparse(url).netloc

    candidates = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        absolute = urljoin(url, href)
        if urlparse(absolute).netloc != domain:
            continue  # stay on-site
        if absolute in seen or not _looks_like_job_link(href, text):
            continue
        seen.add(absolute)
        candidates.append((absolute, text))

    if position:
        narrowed = [c for c in candidates if _matches_position(c[1], position)]
        if narrowed:  # only narrow if it doesn't wipe out every candidate
            candidates = narrowed

    limit = max_candidates if max_candidates and max_candidates > 0 else GENERIC_SITE_MAX_CANDIDATES
    jobs = []
    for job_url, link_text in candidates[:limit]:
        try:
            r = requests.get(job_url, timeout=GENERIC_SITE_TIMEOUT, headers=GENERIC_SITE_HEADERS)
            r.raise_for_status()
        except requests.RequestException:
            continue
        page = BeautifulSoup(r.text, "html.parser")
        title = link_text or (page.title.string if page.title else job_url)
        jobs.append({
            "source": "generic",
            "site_url": url,
            "url": job_url,
            "title": title,
            "description": page.get_text(separator="\n"),
        })
    return jobs


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower())
    return slug.strip("-")


# Known job boards that (a) are server-rendered -- a plain HTTP GET returns
# real job links, no JavaScript needed -- and (b) don't block unauthenticated
# requests, verified by directly fetching each one's raw HTML and checking for
# real job links before adding it here. Both currently listed are MENA-focused
# (Wuzzuf: Egypt, Bayt: Gulf/MENA); LinkedIn and Indeed were tested too and
# both block unauthenticated requests outright (HTTP 999 / 403), so they're
# deliberately not included -- adding them here would just silently return
# nothing.
#
# These double as the seed data for the Search tab's default site list (see
# seed_default_search_sites() below) -- shown as regular, removable rows
# rather than a hardcoded always-on injection, so a user who doesn't want
# Wuzzuf/Bayt searched can just delete them like any other site. A site row
# whose site_type is one of these keys gets its real per-search URL rebuilt
# from `position` at search time (see run_search()) instead of using the
# stored URL directly, since the useful search URL depends on the query.
#
# Each entry is a build_url(position) -> str|None callable rather than a
# plain "?param={query}" template, because sites vary in how (or whether) a
# query string actually filters results for an unauthenticated request --
# verified per site, not assumed:
#   - Wuzzuf's ?q= query string genuinely filters server-side.
#   - Bayt's ?keyword= query string does NOT filter for a plain request (it
#     silently serves generic, unrelated listings -- confirmed by testing:
#     searching "Ai Engineer" that way returned "Civil Rights Attorney" and
#     similar unrelated jobs). Bayt's real search only works through its
#     canonical SEO slug pages (e.g. /en/international/jobs/ai-engineer-jobs/),
#     confirmed working across several different job titles.
KNOWN_JOB_BOARD_TEMPLATES = {
    "wuzzuf": {
        "label": "Wuzzuf",
        "default_url": "https://wuzzuf.net/search/jobs/",
        # Guard against a blank position explicitly (unlike a plain f-string
        # template, this must return None so the dispatcher in run_search()
        # skips it) -- otherwise ?q= with nothing after it would scrape
        # Wuzzuf's entire unfiltered listing instead of being skipped, same
        # as Bayt already does below.
        "build_url": lambda position: (
            f"https://wuzzuf.net/search/jobs/?q={quote_plus(position)}" if position.strip() else None
        ),
    },
    "bayt": {
        "label": "Bayt.com",
        "default_url": "https://www.bayt.com/en/international/jobs/",
        "build_url": lambda position: (
            f"https://www.bayt.com/en/international/jobs/{_slugify(position)}-jobs/"
            if _slugify(position) else None
        ),
    },
    "simplyhired": {
        "label": "SimplyHired",
        "default_url": "https://www.simplyhired.com/",
        # Confirmed working: ?q= genuinely filters server-side (a "software
        # engineer" query returned real, correctly-matching listings with no
        # JS needed).
        "build_url": lambda position: (
            f"https://www.simplyhired.com/search?q={quote_plus(position)}" if position.strip() else None
        ),
    },
    "wellfound": {
        "label": "Wellfound",
        "default_url": "https://wellfound.com/jobs",
        # Wellfound's search is a curated role taxonomy, not a free-text
        # query -- confirmed working via its role-based URLs (fetching
        # /role/r/software-engineer returned 1,827 real, correctly-filtered
        # remote listings, no JS needed). Slugifying the position is a
        # reasonable mapping for common titles ("Software Engineer" ->
        # "software-engineer") but won't match Wellfound's taxonomy for
        # unusual/oddly-phrased titles -- if the slug doesn't exist,
        # Wellfound just returns an empty/generic page, and the position
        # filter in run_search() then correctly narrows that down to
        # nothing rather than surfacing irrelevant jobs.
        "build_url": lambda position: (
            f"https://wellfound.com/role/r/{_slugify(position)}" if _slugify(position) else None
        ),
    },
    "gulftalent": {
        "label": "GulfTalent",
        "default_url": "https://www.gulftalent.com/jobs/category/software",
        # GulfTalent's own ?keyword= query param does NOT filter for a plain
        # unauthenticated request -- confirmed by testing: searching
        # "software engineer" that way silently redirected to the same
        # unfiltered "all jobs" listing (canonical ?pos_ref=all) as no query
        # at all, the same failure mode Bayt's ?keyword= originally had (see
        # its build_url above). Rather than scrape irrelevant jobs, this
        # always points at GulfTalent's own Software category page (1,975+
        # real listings, confirmed server-rendered) regardless of
        # `position`, and leans on run_search()'s global position-filter
        # safety net to narrow it down -- so unlike the other templates
        # here, this source is only useful while searching for
        # software-adjacent titles.
        "build_url": lambda position: "https://www.gulftalent.com/jobs/category/software",
    },
}


def seed_default_search_sites() -> None:
    """Populates models.SearchSite with KNOWN_JOB_BOARD_TEMPLATES the first
    time each template exists, so the Search tab's site list isn't empty by
    default and the user can see/remove them like any other site instead of
    them being an invisible hardcoded behavior.

    Tracked per-template-key against config.SEARCH_DEFAULT_SITES_SEEDED (a
    set of keys already seeded), not on "is the table empty" -- a user who'd
    already added their own site(s) before this feature existed (or just
    from using the app for a while) would make an empty-table check false
    immediately, so the defaults would silently never get added. Per-key
    tracking (rather than one global flag) also means adding a brand new
    template later -- e.g. this file gaining a "gulftalent" entry after a
    user's install already had "wuzzuf"/"bayt" seeded -- still gets that new
    one seeded on the next run, without re-adding "wuzzuf"/"bayt" if the
    user had deliberately removed either.
    """
    import config
    already_seeded = config.SEARCH_DEFAULT_SITES_SEEDED
    to_seed = [key for key in KNOWN_JOB_BOARD_TEMPLATES if key not in already_seeded]
    if not to_seed:
        return

    from db import get_session
    from models import SearchSite
    import env_store
    with get_session() as session:
        existing_types = {r[0] for r in session.query(SearchSite.site_type).all()}
        for key in to_seed:
            if key in existing_types:
                continue  # already present (e.g. re-added by hand) -- don't duplicate
            meta = KNOWN_JOB_BOARD_TEMPLATES[key]
            session.add(SearchSite(
                url=meta["default_url"],
                site_type=key,
                identifier=None,
                label=meta["label"],
            ))

    updated = already_seeded | set(to_seed)
    env_store.update_env_file({"SEARCH_DEFAULT_SITES_SEEDED": ",".join(sorted(updated))})
    config.SEARCH_DEFAULT_SITES_SEEDED = updated  # keep this process's config in sync too


def search_serpapi(query: str) -> list[dict]:
    if not config.SERPAPI_KEY:
        return []  # free-tier key not configured — skip rather than error
    resp = requests.get(
        "https://serpapi.com/search",
        params={"engine": "google_jobs", "q": query, "api_key": config.SERPAPI_KEY},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json().get("jobs_results", [])


def read_watchlist_sheet() -> list[dict]:
    """
    Reads a Google Sheet where you manually list companies/roles of interest.
    Expected columns: company | role_keyword | greenhouse_board_token | lever_company_slug
    """
    if not config.GOOGLE_SHEETS_ID:
        return []
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        config.GOOGLE_SERVICE_ACCOUNT_JSON,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    client = gspread.authorize(creds)
    sheet = client.open_by_key(config.GOOGLE_SHEETS_ID).sheet1
    return sheet.get_all_records()


def search_from_watchlist(
    max_results_per_site: int | None = None, position: str = "", seniority: str = "",
) -> list[dict]:
    results = []
    for row in read_watchlist_sheet():
        try:
            board = row.get("greenhouse_board_token")
            slug = row.get("lever_company_slug")
            if board:
                jobs = _cap(_filter_relevant(search_greenhouse(board), position, seniority), max_results_per_site)
                results += [{"source": "greenhouse", "watchlist_company": row.get("company"), **j} for j in jobs]
            elif slug:
                jobs = _cap(_filter_relevant(search_lever(slug), position, seniority), max_results_per_site)
                results += [{"source": "lever", "watchlist_company": row.get("company"), **j} for j in jobs]
            else:
                query = f"{row.get('role_keyword', '')} {row.get('company', '')}".strip()
                if query:
                    jobs = _cap(search_serpapi(query), max_results_per_site)
                    results += [{"source": "google_jobs", "watchlist_company": row.get("company"), **j} for j in jobs]
        except requests.RequestException:
            continue  # one bad watchlist row shouldn't sink the rest
    return results


def get_configured_sites() -> list[dict]:
    """Websites added from the dashboard's Search tab (models.SearchSite)."""
    from db import get_session
    from models import SearchSite
    with get_session() as session:
        rows = session.query(SearchSite).all()
        return [{"url": r.url, "site_type": r.site_type, "identifier": r.identifier} for r in rows]


def run_search(
    position: str = "",
    seniority: str = "",
    max_results_per_site: int | None = None,
    max_age_days: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Pulls from every configured free source and tags each job with its
    source. `position` (a job title/keyword) is used three ways: as part of
    the SerpAPI query, to build the real search URL for any dashboard-added
    site whose site_type is a KNOWN_JOB_BOARD_TEMPLATES key (Wuzzuf,
    Bayt.com -- skipped for this run if position is blank, since there's no
    query to search with), and as a word-based title filter (see
    _matches_position) applied to every other source (Greenhouse/Lever/
    watchlist/dashboard-added sites) so one field controls relevance
    everywhere. `seniority` (one of "intern",
    "entry", "mid", "senior", "lead", "manager") works the same way -- also
    folded into the SerpAPI query, and matched against titles via keyword
    heuristics (see SENIORITY_KEYWORDS) for every other source. Leave both
    blank to pull everything configured with no filtering.

    `max_results_per_site` caps the results kept from each individual source
    (each Greenhouse board, Lever company, watchlist row, added site) --
    None/0 means no cap. Applied AFTER the position/seniority filters above
    for Greenhouse/Lever/watchlist sources, not before: capping first can
    silently discard every real match on a large board (confirmed in
    practice -- Greenhouse returns a company's jobs in a roughly
    alphabetical-by-title order, so a 100-job cap on a 545-job board zeroed
    out all 38 real "Software Engineer" matches, none of which happened to
    fall in the first 100 raw results). `max_age_days` drops jobs older than
    that (Greenhouse/Lever/SerpAPI expose a real posted date; generic
    scraped sites don't, so they're never excluded by this filter).

    Returns (jobs, source_errors). A single dead/misconfigured board (e.g. a
    Lever slug that 404s) is skipped and reported in source_errors instead of
    aborting the whole run -- one bad source shouldn't block every other one.
    """
    broad = []  # everything except SerpAPI -- position-filtered by title below
    source_errors = []

    for board in config.GREENHOUSE_BOARD_TOKENS:
        try:
            jobs = _cap(_filter_relevant(search_greenhouse(board), position, seniority), max_results_per_site)
            broad += [{"source": "greenhouse", "board": board, **j} for j in jobs]
        except requests.RequestException as exc:
            source_errors.append({"source": "greenhouse", "identifier": board, "error": str(exc)})

    for company in config.LEVER_COMPANY_SLUGS:
        try:
            jobs = _cap(_filter_relevant(search_lever(company), position, seniority), max_results_per_site)
            broad += [{"source": "lever", "company_slug": company, **j} for j in jobs]
        except requests.RequestException as exc:
            source_errors.append({"source": "lever", "identifier": company, "error": str(exc)})

    # Always-on structured sources -- no per-user config needed, unlike the
    # Greenhouse/Lever loops above which depend on .env board tokens/slugs.
    try:
        jobs = _cap(_filter_relevant(search_remoteok(position), position, seniority), max_results_per_site)
        broad += [{"source": "remoteok", **j} for j in jobs]
    except (requests.RequestException, ValueError) as exc:  # ValueError -- unexpected JSON shape
        source_errors.append({"source": "remoteok", "identifier": None, "error": str(exc)})

    try:
        jobs = _cap(_filter_relevant(search_weworkremotely(), position, seniority), max_results_per_site)
        broad += [{"source": "weworkremotely", **j} for j in jobs]
    except (requests.RequestException, ET.ParseError) as exc:
        source_errors.append({"source": "weworkremotely", "identifier": None, "error": str(exc)})

    try:
        broad += search_from_watchlist(max_results_per_site=max_results_per_site, position=position, seniority=seniority)
    except Exception as exc:  # noqa: BLE001 -- e.g. bad service account creds
        source_errors.append({"source": "watchlist", "identifier": None, "error": str(exc)})

    for site in get_configured_sites():
        try:
            if site["site_type"] == "greenhouse":
                jobs = _cap(_filter_relevant(search_greenhouse(site["identifier"]), position, seniority), max_results_per_site)
                broad += [{"source": "greenhouse", "board": site["identifier"], "site_url": site["url"], **j}
                          for j in jobs]
            elif site["site_type"] == "lever":
                jobs = _cap(_filter_relevant(search_lever(site["identifier"]), position, seniority), max_results_per_site)
                broad += [{"source": "lever", "company_slug": site["identifier"], "site_url": site["url"], **j}
                          for j in jobs]
            elif site["site_type"] in KNOWN_JOB_BOARD_TEMPLATES:
                # Wuzzuf/Bayt (or any future template site): the stored URL is
                # just a display/landing link -- the real, query-filtered
                # search URL depends on `position` and is rebuilt fresh here.
                # No position set means no query to build a useful URL from,
                # so this site is skipped for this run rather than scraping
                # its unfiltered landing page.
                built_url = KNOWN_JOB_BOARD_TEMPLATES[site["site_type"]]["build_url"](position)
                if built_url:
                    broad += search_generic_site(built_url, position=position, max_candidates=max_results_per_site)
            else:
                broad += search_generic_site(site["url"], position=position, max_candidates=max_results_per_site)
        except requests.RequestException as exc:
            source_errors.append({"source": site["site_type"], "identifier": site["url"], "error": str(exc)})

    if position:
        broad = [j for j in broad if _matches_position(j.get("title"), position)]

    if seniority:
        broad = [j for j in broad if _matches_seniority(j.get("title"), seniority)]

    serp_query = " ".join(part for part in (SENIORITY_LABELS.get(seniority, ""), position) if part).strip()
    serpapi_results = []
    if serp_query:
        try:
            jobs = _cap(search_serpapi(serp_query), max_results_per_site)
            serpapi_results = [{"source": "google_jobs", **j} for j in jobs]
        except requests.RequestException as exc:
            source_errors.append({"source": "google_jobs", "identifier": serp_query, "error": str(exc)})

    if max_age_days:
        broad = [j for j in broad if _within_max_age(j, max_age_days)]
        serpapi_results = [j for j in serpapi_results if _within_max_age(j, max_age_days)]

    return broad + serpapi_results, source_errors
