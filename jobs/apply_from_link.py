"""Manual link application: paste any job URL, agent scores + drafts/applies."""
import requests
from bs4 import BeautifulSoup

from db import get_session, init_db
from models import Job
from orchestrator import app as orchestrator_app
from cv_parser import parse_cv


def apply_from_link(url: str, cv_path: str):
    init_db()
    cv_text = parse_cv(cv_path)

    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    description = soup.get_text(separator="\n")

    if "greenhouse.io" in url:
        source = "greenhouse"
    elif "lever.co" in url:
        source = "lever"
    else:
        source = "manual_link"

    with get_session() as session:
        existing = session.query(Job).filter(Job.url == url).first()
        if existing:
            return {"status": "already_processed", "job_id": existing.id}

        job_row = Job(
            url=url,
            title=(soup.title.string if soup.title else url),
            company="",
            source_site=source,
            description=description,
        )
        session.add(job_row)
        session.flush()
        job_id = job_row.id

    job = {"id": job_id, "url": url, "description": description, "source": source,
           "title": soup.title.string if soup.title else url}
    return orchestrator_app.invoke({"job": job, "cv_text": cv_text})


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python jobs/apply_from_link.py <job_url> <cv_path>")
    else:
        print(apply_from_link(sys.argv[1], sys.argv[2]))
