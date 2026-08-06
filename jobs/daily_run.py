"""Daily search-and-apply run: search all free sources, dedupe, score, act."""
import json
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

import config
from db import get_session, init_db
from models import Job, Application, SkillGap
from agents.search_agent import run_search
from orchestrator import app as orchestrator_app
from cv_parser import parse_cv, find_default_cv


def _job_exists(session, url: str) -> bool:
    return session.query(Job).filter(Job.url == url).first() is not None


def _insert_job_row(raw_job: dict) -> tuple[int, dict] | None:
    """
    Short, standalone transaction: dedupe-check + insert the Job row,
    commit, done. Deliberately kept to a single INSERT -- see
    run_daily_search_and_apply's docstring for why this must NOT share a
    transaction with the orchestrator call below. Returns None if the URL
    already exists (nothing to do) or lost a race to another process
    inserting the same URL concurrently (Job.url's unique constraint raises,
    caught here the same as any other "someone already has this one").
    """
    url = raw_job.get("url") or raw_job.get("hostedUrl") or raw_job.get("absolute_url")
    if not url:
        return None
    try:
        with get_session() as session:
            if _job_exists(session, url):
                return None
            job_row = Job(
                url=url,
                title=raw_job.get("title", ""),
                company=raw_job.get("company") or raw_job.get("watchlist_company", ""),
                source_site=raw_job.get("source", "unknown"),
                description=raw_job.get("description") or raw_job.get("content", ""),
            )
            session.add(job_row)
            session.flush()  # get job_row.id before returning
            return job_row.id, {
                "title": job_row.title,
                "company": job_row.company,
                "description": job_row.description,
            }
    except IntegrityError:
        return None  # another process inserted this exact URL first


def _delete_job_row(job_id: int) -> None:
    """
    Removes a Job row that _insert_job_row() just committed, when the
    orchestrator subsequently raises -- so the URL is free to be retried on
    the next run instead of permanently skipped by the dedupe check. This is
    what restores the "a failed job leaves no trace" guarantee the old
    single-SAVEPOINT-per-job design gave for free, now that the Job insert
    and the Application insert are two separate short transactions instead
    of one long one wrapping both.
    """
    with get_session() as session:
        session.query(Job).filter(Job.id == job_id).delete()


def _finalize_job(job_id: int, job_title: str, job_company: str, result: dict) -> dict:
    """Second short transaction: writes Application (+ SkillGap, if the
    scoring step surfaced missing skills) for a job whose orchestrator run
    already completed successfully."""
    with get_session() as session:
        app_row = Application(
            job_id=job_id,
            ats_score=result.get("ats_result", {}).get("score") if result.get("ats_result") else None,
            cv_version_path=result.get("cv_path"),
            status=result.get("status", "unknown"),
            date_applied=datetime.now(timezone.utc) if result.get("status") == "auto_submitted" else None,
        )
        session.add(app_row)

        if result.get("ats_result", {}).get("missing_skills"):
            session.add(SkillGap(
                job_id=job_id,
                missing_skills=json.dumps(result["ats_result"]["missing_skills"]),
            ))

        status = app_row.status

    return {"title": job_title, "company": job_company, "status": status}


def _process_one_job(raw_job: dict, cv_text: str) -> dict | None:
    """
    Full per-job flow, split into three phases -- insert Job (short
    transaction), run the orchestrator (no open transaction at all), write
    Application/SkillGap (short transaction) -- instead of one transaction
    spanning all three. See run_daily_search_and_apply's docstring for why.
    Returns None if there was nothing new to process for this job.
    """
    inserted = _insert_job_row(raw_job)
    if inserted is None:
        return None
    job_id, job_fields = inserted

    raw_job["id"] = job_id
    raw_job.setdefault("description", job_fields["description"])

    try:
        result = orchestrator_app.invoke({"job": raw_job, "cv_text": cv_text})
    except Exception:
        _delete_job_row(job_id)  # keep this URL retry-able on the next run
        raise

    return _finalize_job(job_id, job_fields["title"], job_fields["company"], result)


def run_daily_search_and_apply(
    cv_path: str | None = None,
    position: str | None = None,
    seniority: str | None = None,
    max_results_per_site: int | None = None,
    max_age_days: int | None = None,
) -> dict:
    """
    `position`/`seniority`/`max_results_per_site`/`max_age_days` default to
    the matching config.SEARCH_* value (whatever's saved from the Search tab)
    when not explicitly passed, so the 8am scheduler run and any CLI
    invocation automatically stay in sync with whatever's saved -- only pass
    them explicitly to override for a single run.

    Each job is written to the database in short, independent transactions
    (see _process_one_job) rather than one transaction covering the whole
    batch. This matters because SQLite only allows one writer at a time even
    in WAL mode (see db.py): holding a single transaction open across every
    job's orchestrator call -- which is several LLM round-trips, easily
    seconds per job -- meant a batch of dozens of jobs could hold the write
    lock for minutes. Any other process trying to write during that window
    (the scheduler's own automatic run overlapping a manual "Search now"
    click, for example) would blow through even a generous busy_timeout and
    fail outright with "database is locked" -- a real failure seen in
    production. Keeping each transaction down to a single row write means
    the lock is only ever held for milliseconds, so genuine overlap between
    two runs just makes one of them wait briefly instead of erroring.
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
    max_results_per_site = max_results_per_site if max_results_per_site is not None else config.SEARCH_MAX_RESULTS_PER_SITE
    max_age_days = max_age_days if max_age_days is not None else config.SEARCH_MAX_AGE_DAYS
    found, source_errors = run_search(
        position=position,
        seniority=seniority,
        max_results_per_site=max_results_per_site,
        max_age_days=max_age_days,
    )

    processed = []
    errors = []
    for raw_job in found:
        url = raw_job.get("url") or raw_job.get("hostedUrl") or raw_job.get("absolute_url")
        if not url:
            continue
        try:
            outcome = _process_one_job(raw_job, cv_text)
            if outcome is not None:  # None -- already existed, nothing new to record
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
