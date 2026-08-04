"""Daily search-and-apply run: search all free sources, dedupe, score, act."""
import json
from datetime import datetime, timezone

import config
from db import get_session, init_db
from models import Job, Application, SkillGap
from agents.search_agent import run_search
from orchestrator import app as orchestrator_app
from cv_parser import parse_cv, find_default_cv


def _job_exists(session, url: str) -> bool:
    return session.query(Job).filter(Job.url == url).first() is not None


def _process_one_job(session, raw_job: dict, cv_text: str) -> dict:
    """
    Everything for a single job, wrapped in a SAVEPOINT by the caller so that
    one job's failure (a flaky LLM call, a malformed job dict, whatever)
    can't roll back every other job already processed in this batch.
    """
    url = raw_job.get("url") or raw_job.get("hostedUrl") or raw_job.get("absolute_url")

    job_row = Job(
        url=url,
        title=raw_job.get("title", ""),
        company=raw_job.get("company") or raw_job.get("watchlist_company", ""),
        source_site=raw_job.get("source", "unknown"),
        description=raw_job.get("description") or raw_job.get("content", ""),
    )
    session.add(job_row)
    session.flush()  # get job_row.id before the orchestrator call

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

    return {"title": job_row.title, "company": job_row.company, "status": app_row.status}


def run_daily_search_and_apply(
    cv_path: str | None = None,
    position: str | None = None,
    seniority: str | None = None,
) -> dict:
    """
    `position`/`seniority` default to config.SEARCH_POSITION_QUERY/
    SEARCH_SENIORITY_LEVEL (the values saved from the Search tab) when not
    explicitly passed, so the 8am scheduler run and any CLI invocation
    automatically stay in sync with whatever's saved -- only pass them
    explicitly to override for a single run.
    """
    init_db()
    cv_path = cv_path or find_default_cv("cv")
    if not cv_path:
        raise RuntimeError(
            "No CV found. Upload one via the dashboard's CV page, or place a "
            "file at cv/current_cv.pdf or cv/current_cv.docx."
        )
    cv_text = parse_cv(cv_path)
    position = position if position is not None else config.SEARCH_POSITION_QUERY
    seniority = seniority if seniority is not None else config.SEARCH_SENIORITY_LEVEL
    found, source_errors = run_search(position=position, seniority=seniority)

    processed = []
    errors = []
    with get_session() as session:
        for raw_job in found:
            url = raw_job.get("url") or raw_job.get("hostedUrl") or raw_job.get("absolute_url")
            if not url or _job_exists(session, url):
                continue  # idempotent: never process the same job twice

            try:
                with session.begin_nested():  # per-job SAVEPOINT
                    outcome = _process_one_job(session, raw_job, cv_text)
                processed.append(outcome)
            except Exception as exc:  # noqa: BLE001 -- one bad job shouldn't sink the batch
                errors.append({
                    "title": raw_job.get("title", ""),
                    "url": url,
                    "error": str(exc),
                })

    return {"processed": processed, "errors": errors, "found": len(found), "source_errors": source_errors}


if __name__ == "__main__":
    outcome = run_daily_search_and_apply()
    for r in outcome["processed"]:
        print(r)
    for e in outcome["errors"]:
        print("ERROR:", e)
    for e in outcome["source_errors"]:
        print("SOURCE UNREACHABLE:", e)
    print(f"{len(outcome['processed'])} processed, {len(outcome['errors'])} failed, "
          f"{len(outcome['source_errors'])} source(s) unreachable, {outcome['found']} found total")
