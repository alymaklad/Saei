"""Apply from a link: paste one job URL, and Sa'ei reads the posting, scores
it, tailors the CV, writes a cover letter and drafts/applies -- the same
per-job pipeline a search runs (jobs/daily_run.py::_process_one_job), so the
result shows up on the Applications page like any other job.

Reading the posting is the part that matters. A plain fetch of a job page is
mostly menus, cookie banners and footers, and scoring that as the "job
description" grades the CV against noise. So, in order:

  1. Sites with a public job API (Greenhouse, Lever, Ashby, LinkedIn's
     logged-out posting endpoint) are read through it.
  2. Otherwise the page's schema.org JobPosting data -- most job boards and
     careers sites embed it for Google Jobs -- gives title, company and the
     description itself.
  3. Last resort: the page's main content with navigation, headers, footers
     and scripts stripped.
"""
import json
import re

import requests
from bs4 import BeautifulSoup

from db import get_session, init_db
from models import Application, Job
from cv_parser import parse_cv, find_default_cv
from agents.search_agent import BROWSER_HEADERS, enrich_linkedin

MIN_DESCRIPTION_CHARS = 200
_SIGN_IN_TITLE = re.compile(r"^(sign[ -]?in|log[ -]?in|sign up|join|authwall|security verification|"
                            r"just a moment|access denied|تسجيل الدخول)\b", re.I)

_GREENHOUSE = re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/([\w-]+)/jobs/(\d+)", re.I)
_GREENHOUSE_EMBED = re.compile(r"[?&]gh_jid=(\d+)", re.I)
_LEVER = re.compile(r"jobs\.lever\.co/([\w.-]+)/([0-9a-f-]{36})", re.I)
_ASHBY = re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)/([0-9a-f-]{36})", re.I)
_LINKEDIN_ID = re.compile(r"currentJobId=(\d{6,})|/jobs/view/(?:[^/?]*-)?(\d{6,})", re.I)


def _text(html: str | None) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text("\n", strip=True)


def _from_greenhouse(board: str, job_id: str) -> dict:
    data = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}",
                        params={"content": "true"}, timeout=20).json()
    import html as _html
    return {"title": data.get("title"), "company": data.get("company_name") or board.replace("-", " ").title(),
            "description": _text(_html.unescape(data.get("content") or "")),
            "location": (data.get("location") or {}).get("name"),
            "source": "greenhouse", "board": board}


def _from_lever(company: str, posting_id: str) -> dict:
    data = requests.get(f"https://api.lever.co/v0/postings/{company}/{posting_id}", timeout=20).json()
    lists = "\n".join(f"{l.get('text')}\n{_text(l.get('content'))}" for l in data.get("lists") or [])
    description = "\n\n".join(x for x in (data.get("descriptionPlain"), lists, data.get("additionalPlain")) if x)
    return {"title": data.get("text"), "company": company.replace("-", " ").title(),
            "description": description,
            "location": (data.get("categories") or {}).get("location"),
            "source": "lever", "company_slug": company}


def _from_ashby(org: str, job_id: str) -> dict:
    data = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}", timeout=20).json()
    job = next((j for j in data.get("jobs") or [] if job_id in (j.get("id"), (j.get("jobUrl") or "").rsplit("/", 1)[-1])), None)
    if not job:
        raise ValueError("That Ashby posting is no longer listed.")
    return {"title": job.get("title"), "company": org.replace("-", " ").title(),
            "description": job.get("descriptionPlain") or _text(job.get("descriptionHtml")),
            "location": job.get("location"), "source": "ashby", "board": org}


def _from_linkedin(job_id: str) -> dict:
    """Everything from LinkedIn's logged-out posting endpoint. The regular
    job page isn't opened at all: for many links (the job feed's
    ?currentJobId= ones especially) it serves a sign-in wall, whose
    "Sign in" heading would become the job title."""
    full = enrich_linkedin({"linkedin_id": job_id}) or {}
    return {"title": full.get("title"), "company": full.get("company"), "location": full.get("location"),
            "description": full.get("description", ""),
            "source": "linkedin", "url": f"https://www.linkedin.com/jobs/view/{job_id}/"}


def _json_ld_posting(page: BeautifulSoup) -> dict | None:
    """The page's schema.org JobPosting, if it embeds one."""
    for tag in page.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                kinds = item.get("@type")
                kinds = kinds if isinstance(kinds, list) else [kinds]
                if "JobPosting" in kinds:
                    return item
                stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
            elif isinstance(item, list):
                stack.extend(item)
    return None


def _from_page(url: str, page: BeautifulSoup) -> dict:
    posting = _json_ld_posting(page)
    if posting:
        org = posting.get("hiringOrganization")
        company = org.get("name") if isinstance(org, dict) else (org if isinstance(org, str) else None)
        place = posting.get("jobLocation")
        place = place[0] if isinstance(place, list) and place else place
        address = (place or {}).get("address") if isinstance(place, dict) else None
        location = ", ".join(str(address.get(k)) for k in ("addressLocality", "addressCountry")
                             if isinstance(address, dict) and address.get(k)) or None
        found = {"title": posting.get("title"), "company": company,
                 "description": _text(posting.get("description")), "location": location}
        if len(found["description"]) >= MIN_DESCRIPTION_CHARS:
            return found

    # Not <form>: some sites wrap the whole page in one.
    for tag in page(["script", "style", "noscript", "nav", "header", "footer", "aside", "svg", "iframe"]):
        tag.decompose()
    # The largest block that names itself a description / job details, else
    # the page's main region, else the whole body.
    blocks = page.select("[id*=description i], [class*=description i], [id*=job-detail i], "
                         "[class*=job-detail i], [id*=jobdetail i], [class*=jobdetail i]")
    blocks = [b for b in blocks if len(b.get_text(" ", strip=True)) >= MIN_DESCRIPTION_CHARS]
    main = (max(blocks, key=lambda b: len(b.get_text(" ", strip=True))) if blocks else None) \
        or page.select_one("main, article, [role=main], #content") or page.body or page
    meta = lambda prop: (page.find("meta", property=prop) or page.find("meta", attrs={"name": prop}) or {}).get("content")  # noqa: E731
    h1 = page.select_one("h1")
    # A <title> like "Machine Learning Engineer | Jobs in Cairo | TanQeeb" --
    # the role is the first segment.
    page_title = page.title.string.strip() if page.title and page.title.string else ""
    return {
        "title": (h1.get_text(strip=True) if h1 else None)
                 or re.split(r"\s[|–-]\s", meta("og:title") or page_title)[0] or None,
        "company": meta("og:site_name"),
        "description": main.get_text("\n", strip=True),
    }


def read_posting(url: str) -> dict:
    """title / company / description / source (+ location, board ids) for one
    job URL. Raises ValueError when the page doesn't hold a readable posting."""
    page = None
    match = _GREENHOUSE.search(url)
    if match:
        job = _from_greenhouse(*match.groups())
    elif (m := _LEVER.search(url)):
        job = _from_lever(*m.groups())
    elif (m := _ASHBY.search(url)):
        job = _from_ashby(*m.groups())
    elif "linkedin.com" in url and (m := _LINKEDIN_ID.search(url)):
        job = _from_linkedin(m.group(1) or m.group(2))
    else:
        resp = requests.get(url, timeout=25, headers=BROWSER_HEADERS)
        if resp.status_code in (401, 403, 429):
            raise ValueError("That site blocks automated readers, so Sa'ei can't open the posting. "
                             "Copy the job's text into the CV page's bench instead.")
        resp.raise_for_status()
        page = BeautifulSoup(resp.text, "html.parser")
        gh = _GREENHOUSE_EMBED.search(url)
        if gh and page.find("script", src=re.compile(r"greenhouse\.io/embed")):
            board = re.search(r"for=([\w-]+)", str(page.find("script", src=re.compile(r"greenhouse\.io/embed"))["src"]))
            job = _from_greenhouse(board.group(1), gh.group(1)) if board else _from_page(url, page)
        else:
            job = _from_page(url, page)
            job["source"] = "manual_link"

    job["url"] = job.get("url") or url
    # A login wall instead of the posting: refuse it rather than score the
    # sign-in page as a job titled "Sign in".
    if _SIGN_IN_TITLE.match((job.get("title") or "").strip()):
        raise ValueError("That link opened a sign-in page instead of the job. Open the posting "
                         "in your browser and copy its address from there, or paste the job's "
                         "text into the CV page's bench.")
    job["title"] = (job.get("title") or "").strip() or "Untitled role"
    job["company"] = (job.get("company") or "").strip()
    if len(job.get("description") or "") < MIN_DESCRIPTION_CHARS:
        raise ValueError("Couldn't find the job description on that page. If it only shows "
                         "after signing in, paste the text into the CV page's bench instead.")
    return job


def apply_from_link(url: str, cv_path: str | None = None) -> dict:
    """Reads, scores, tailors and drafts/applies one posting. Returns
    {"status", "application_id", "title", "company"} -- "already_processed"
    when the URL was seen before."""
    from jobs.daily_run import _process_one_job  # the search pipeline's own per-job flow

    init_db()
    url = url.strip()
    with get_session() as session:
        existing = session.query(Job).filter(Job.url == url).first()
        if existing:
            app_row = session.query(Application).filter(Application.job_id == existing.id).first()
            return {"status": "already_processed", "application_id": app_row.id if app_row else None,
                    "title": existing.title, "company": existing.company}

    cv_path = cv_path or find_default_cv("cv")
    if not cv_path:
        raise RuntimeError("No CV found. Upload one on the CV page first.")
    job = read_posting(url)

    # A LinkedIn link may have been normalised to its canonical form.
    if job["url"] != url:
        with get_session() as session:
            existing = session.query(Job).filter(Job.url == job["url"]).first()
            if existing:
                app_row = session.query(Application).filter(Application.job_id == existing.id).first()
                return {"status": "already_processed", "application_id": app_row.id if app_row else None,
                        "title": existing.title, "company": existing.company}

    outcome = _process_one_job(dict(job), parse_cv(cv_path))
    with get_session() as session:
        row = session.query(Job).filter(Job.url == job["url"]).first()
        app_row = session.query(Application).filter(Application.job_id == row.id).first() if row else None
    return {"status": (outcome or {}).get("status", "unknown"),
            "application_id": app_row.id if app_row else None,
            "title": job["title"], "company": job["company"]}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python jobs/apply_from_link.py <job_url> [cv_path]")
    else:
        print(apply_from_link(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
