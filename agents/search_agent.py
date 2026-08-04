"""
Search Agent.

Free sources, in priority order:
  1. Greenhouse public API  — no key needed
  2. Lever public API       — no key needed
  3. Google Sheets watchlist — free (Google Cloud service account)
  4. SerpAPI (Google Jobs)  — free tier, 100 searches/month, optional
"""
import requests
import config

WHITELISTABLE_SOURCES = {"greenhouse", "lever"}  # only these CAN ever be whitelisted for auto-submit


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


def run_search(query: str = "") -> list[dict]:
    """Pulls from every configured free source and tags each job with its source."""
    results = []

    for board in config.GREENHOUSE_BOARD_TOKENS:
        results += [{"source": "greenhouse", "board": board, **j} for j in search_greenhouse(board)]

    for company in config.LEVER_COMPANY_SLUGS:
        results += [{"source": "lever", "company_slug": company, **j} for j in search_lever(company)]

    results += search_from_watchlist()

    if query:
        results += [{"source": "google_jobs", **j} for j in search_serpapi(query)]

    return results
