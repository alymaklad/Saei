"""
Search Agent.

Free sources, in priority order:
  1. Greenhouse public API  — no key needed
  2. Lever public API       — no key needed
  3. Google Sheets watchlist — free (Google Cloud service account)
  4. Sites added from the dashboard's Search tab — Greenhouse/Lever URLs reuse
     the API-backed paths above; anything else falls back to a best-effort
     generic scrape (see search_generic_site)
  5. SerpAPI (Google Jobs)  — free tier, 100 searches/month, optional
"""
import re
from urllib.parse import urljoin, urlparse

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


def _looks_like_job_link(href: str, text: str) -> bool:
    haystack = f"{href} {text}".lower()
    return any(keyword in haystack for keyword in JOB_LINK_KEYWORDS)


def search_generic_site(url: str, position: str = "") -> list[dict]:
    """
    Best-effort scraper for career-page URLs that aren't Greenhouse/Lever.
    Finds same-domain links that look job-related (by URL/text keywords),
    optionally narrowed to ones matching `position`, capped to a handful of
    candidates to stay fast and polite, then fetches each candidate's page
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
        narrowed = [c for c in candidates if position.lower() in c[1].lower()]
        if narrowed:  # only narrow if it doesn't wipe out every candidate
            candidates = narrowed

    jobs = []
    for job_url, link_text in candidates[:GENERIC_SITE_MAX_CANDIDATES]:
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


def search_from_watchlist() -> list[dict]:
    results = []
    for row in read_watchlist_sheet():
        board = row.get("greenhouse_board_token")
        slug = row.get("lever_company_slug")
        if board:
            results += [{"source": "greenhouse", "watchlist_company": row.get("company"), **j}
                        for j in search_greenhouse(board)]
        elif slug:
            results += [{"source": "lever", "watchlist_company": row.get("company"), **j}
                        for j in search_lever(slug)]
        else:
            query = f"{row.get('role_keyword', '')} {row.get('company', '')}".strip()
            if query:
                results += [{"source": "google_jobs", "watchlist_company": row.get("company"), **j}
                            for j in search_serpapi(query)]
    return results


def get_configured_sites() -> list[dict]:
    """Websites added from the dashboard's Search tab (models.SearchSite)."""
    from db import get_session
    from models import SearchSite
    with get_session() as session:
        rows = session.query(SearchSite).all()
        return [{"url": r.url, "site_type": r.site_type, "identifier": r.identifier} for r in rows]


def run_search(position: str = "") -> list[dict]:
    """
    Pulls from every configured free source and tags each job with its
    source. `position` (a job title/keyword) is used two ways: as the literal
    SerpAPI query, and as a case-insensitive title filter applied to every
    other source (Greenhouse/Lever/watchlist/dashboard-added sites) so one
    field controls relevance everywhere. Leave blank to pull everything
    configured with no title filter.
    """
    broad = []  # everything except SerpAPI -- position-filtered by title below

    for board in config.GREENHOUSE_BOARD_TOKENS:
        broad += [{"source": "greenhouse", "board": board, **j} for j in search_greenhouse(board)]

    for company in config.LEVER_COMPANY_SLUGS:
        broad += [{"source": "lever", "company_slug": company, **j} for j in search_lever(company)]

    broad += search_from_watchlist()

    for site in get_configured_sites():
        if site["site_type"] == "greenhouse":
            broad += [{"source": "greenhouse", "board": site["identifier"], "site_url": site["url"], **j}
                      for j in search_greenhouse(site["identifier"])]
        elif site["site_type"] == "lever":
            broad += [{"source": "lever", "company_slug": site["identifier"], "site_url": site["url"], **j}
                      for j in search_lever(site["identifier"])]
        else:
            broad += search_generic_site(site["url"], position=position)

    if position:
        needle = position.lower()
        broad = [j for j in broad if needle in (j.get("title") or "").lower()]

    serpapi_results = [{"source": "google_jobs", **j} for j in search_serpapi(position)] if position else []

    return broad + serpapi_results
