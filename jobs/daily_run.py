"""Daily search-and-apply run: search all free sources, dedupe, score, act."""
import json
from datetime import datetime, timezone

from db import get_session, init_db
from models import Job, Application, SkillGap
from agents.search_agent import run_search
from orchestrator import app as orchestrator_app
from cv_parser import parse_cv, find_default_cv


def _job_exists(session, url: str) -> bool:
    return session.query(Job).filter(Job.url == url).first() is not None


def run_daily_search_and_apply(cv_path: str | None = None, query: str = ""):
    init_db()
    cv_path = cv_path or find_default_cv("cv")
    if not cv_path:
        raise RuntimeError(
            "No CV found. Upload one via the dashboard's CV page, or place a "
            "file at cv/current_cv.pdf or cv/current_cv.docx."
        )
    cv_text = parse_cv(cv_path)
    found = run_search(query=query)

    processed = []
    with get_session() as session:
        for raw_job in found:
            url = raw_job.get("url") or raw_job.get("hostedUrl") or raw_job.get("absolute_url")
            if not url or _job_exists(session, url):
                continue  # idempotent: never process the same job twice

            job_row = Job(
                url=url,
                title=raw_job.get("title", ""),
                company=raw_job.get("company") or raw_job.get("watchlist_company", ""),
                source_site=raw_job.get("source", "unknown"),
                description=raw_job.get("description") or raw_job.get("content", ""),
            )
            session.add(job_row)
            session.flush()  # get job_row.id before commit

            raw_job["id"] = job_row.id
            raw_job["url"] = url
            raw_job.setdefault("description", job_row.description)

            result = orchestrator_app.invoke({"job": raw_job, "cv_text": cv_text})

            app_row = Application(
                job_id=job_row.id,
                ats_score=result.get("ats_result", {}).get("score") if result.get("ats_result") else None,
                cv_version_path=result.get("cv_path"),
                status=result.get("status", "unknown"),
                date_applied=datetime.now(timezone.utc) if result.get("status") == "auto_submitted" else None,
            )
            session.add(app_row)

            if result.get("ats_result", {}).get("missing_skills"):
                session.add(SkillGap(
                    job_id=job_row.id,
                    missing_skills=json.dumps(result["ats_result"]["missing_skills"]),
                ))

            processed.append({
                "title": job_row.title,
                "company": job_row.company,
                "status": app_row.status,
            })

    return processed


if __name__ == "__main__":
    for r in run_daily_search_and_apply():
        print(r)
