"""Daily search-and-apply run: search all free sources, dedupe, rank, act.

Pipeline shape (see ARCHITECTURE.md "Job matching"):

    query expansion -> retrieval (search_agent) -> drop already-known URLs
    -> semantic retrieval path -> ranking -> match-score gate -> orchestrator

Retrieval is recall-biased and cheap; ranking is the expensive, precise pass,
and it runs only on jobs that are both new and made it through retrieval.
"""
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


def _job_url(raw_job: dict) -> str | None:
    return raw_job.get("url") or raw_job.get("hostedUrl") or raw_job.get("absolute_url")


def _drop_already_known(jobs: list[dict]) -> list[dict]:
    """Removes jobs whose URL is already in the Job table.

    run_search() has no memory across runs, so a daily schedule re-fetches the
    same postings every morning and they're only recognized as duplicates at
    INSERT time -- after the expensive work. Dropping them here instead means
    embeddings, skill extraction and ranking are only ever paid for on jobs
    that are actually new.
    """
    urls = {u for u in (_job_url(j) for j in jobs) if u}
    if not urls:
        return jobs
    with get_session() as session:
        known = {
            row[0] for row in session.query(Job.url).filter(Job.url.in_(urls)).all()
        }
    return [j for j in jobs if _job_url(j) not in known]


def _candidate_profile():
    """The stored profile, or None before a CV has been uploaded and read.

    The uploaded file's only job is to fill this record; everything after that
    -- which roles are searched for, which jobs are semantically recalled, how
    they are ranked, how they are scored, and what the tailored CV says --
    reads the profile, so that a correction on the Profile page changes the
    whole run rather than only the parts that happened to be wired to it.
    """
    try:
        import profile_store
        return profile_store.load_profile_dict()
    except Exception:  # noqa: BLE001 -- a DB hiccup must not stop a search run
        return None


def _rank_candidates(jobs: list[dict], cv_text: str, target_roles: list[str], seniority: str,
                     trace=None, profile: dict | None = None) -> list[dict]:
    """Semantic retrieval path + ranking, with every step degrading gracefully.

    Embeddings are the one part of this pipeline that depends on an external
    service the user may not have set up (a local Ollama with
    nomic-embed-text pulled, or a Gemini key). If that's unavailable, the
    whole matching stage falls back to token-retrieved jobs scored without a
    semantic factor rather than failing the run -- the agent still works, it
    just loses the "different title, right content" recall.
    """
    from agents import ranking_agent
    if trace is None:
        from agents.search_trace import NullTrace
        trace = NullTrace()

    matched, missed = ranking_agent.split_by_token_match(jobs, target_roles)

    cv_embedding = None
    recovered = []
    with trace.stage("semantic retrieval"):
        try:
            from agents import cv_profile
            from agents.embeddings import embed_candidate
            # The query vector is the PROFILE, not the file: same content
            # without the headers, footers and extraction noise a PDF drags
            # in, and it moves when the user corrects something.
            cv_embedding = embed_candidate(
                cv_profile.profile_text(profile) if profile else cv_text)
            # Only the token-path MISSES go through the semantic path:
            # re-embedding a job the title filter already accepted spends a
            # call to confirm a decision that's already made, since the two
            # paths are unioned.
            recovered = ranking_agent.semantic_retrieval_path(missed, cv_embedding)
            recovered_urls = {r.get("url") for r in recovered}
            for job in missed:
                if job.get("url") in recovered_urls:
                    score = next((r.get("semantic_score") for r in recovered
                                  if r.get("url") == job.get("url")), None)
                    trace.record_retrieval(job, "semantic", "kept",
                                           "title didn't match, but the description is close to the CV",
                                           semantic_score=score)
                else:
                    trace.record_retrieval(job, "semantic", "dropped",
                                           "title didn't match and the description isn't close enough to the CV")
        except Exception as exc:  # noqa: BLE001 -- ollama down, model not pulled, no API key
            print(f"[matching] semantic retrieval unavailable, continuing without it: {exc}")
            trace.record_error("embeddings", config.EMBEDDING_PROVIDER, exc)
            for job in missed:
                trace.record_retrieval(job, "semantic", "dropped",
                                       f"semantic path unavailable: {exc}")
            recovered = []

    candidates = matched + recovered
    # Whatever's left of the run's embedding budget after the semantic path
    # spent its share. Discovery came first (it finds jobs nothing else
    # would); the token-matched jobs here only lose their semantic *factor*
    # if the budget is gone, scoring neutral on it instead.
    remaining_budget = max(0, config.EMBEDDING_MAX_PER_RUN - len(missed))
    if len(matched) > remaining_budget:
        print(f"[matching] embedding budget reached: scoring {remaining_budget} of "
              f"{len(matched)} title-matched jobs semantically, rest score neutral")
        trace.record_error(
            "embedding budget", config.EMBEDDING_MAX_PER_RUN,
            f"{len(matched) - remaining_budget} title-matched jobs scored neutral on "
            f"the semantic factor (per-run embedding budget reached)",
        )

    with trace.stage("ranking"):
        try:
            return ranking_agent.rank_jobs(
                candidates, cv_text,
                cv_embedding=cv_embedding,
                target_roles=target_roles,
                seniority=seniority,
                trace=trace,
                max_embeddings=remaining_budget,
            )
        except Exception as exc:  # noqa: BLE001 -- ranking must never sink the run
            print(f"[matching] ranking failed, proceeding unranked: {exc}")
            trace.record_error("ranking", None, exc)
            return candidates


def _finalize_job(
    job_id: int, job_title: str, job_company: str, result: dict,
    match_score: float | None = None, match_breakdown: dict | None = None,
) -> dict:
    """Second short transaction: writes Application (+ SkillGap, if the
    scoring step surfaced missing skills) for a job whose orchestrator run
    already completed successfully.

    ats_result is set by orchestrator.py's score_node for every job
    regardless of which path it takes afterward (rewrite, auto-submit, or
    draft), so ats_score/ats_breakdown/ats_explanation are recorded for
    every application, not just low-fit ones. tailored_ats_result /
    tailored_ats_explanation are only present when rewrite_node ran (fit
    score below the threshold), so those columns stay NULL otherwise.

    match_score/match_breakdown come from the ranking stage that ran BEFORE
    the orchestrator (agents/ranking_agent.py), not from the orchestrator
    itself -- they're passed through here so one Application row carries both
    "is this job right for me" (match) and "would my CV pass this employer's
    ATS" (ats) side by side."""
    ats_result = result.get("ats_result") or {}
    tailored_result = result.get("tailored_ats_result") or {}
    with get_session() as session:
        app_row = Application(
            job_id=job_id,
            ats_score=ats_result.get("score"),
            # Recorded from the RESULT, not from config: the setting says what
            # the next run will do, whereas this row needs to say what produced
            # the breakdown sitting next to it. The two differ the moment
            # someone changes the setting between runs.
            scoring_engine=ats_result.get("engine"),
            ats_breakdown=json.dumps(ats_result["breakdown"]) if ats_result.get("breakdown") else None,
            ats_explanation=ats_result.get("explanation"),
            tailored_ats_score=tailored_result.get("score"),
            tailored_ats_breakdown=json.dumps(tailored_result["breakdown"]) if tailored_result.get("breakdown") else None,
            tailored_ats_explanation=result.get("tailored_ats_explanation"),
            match_score=match_score,
            match_breakdown=json.dumps(match_breakdown) if match_breakdown else None,
            cv_version_path=result.get("cv_path"),
            status=result.get("status", "unknown"),
            date_applied=datetime.now(timezone.utc) if result.get("status") == "auto_submitted" else None,
        )
        session.add(app_row)

        if ats_result.get("missing_skills"):
            session.add(SkillGap(
                job_id=job_id,
                missing_skills=json.dumps(ats_result["missing_skills"]),
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
    # Popped before the row is built so these ranking-stage keys don't leak
    # into the Job/orchestrator payload as if they were source fields.
    match_score = raw_job.pop("match_score", None)
    match_breakdown = raw_job.pop("match_breakdown", None)
    raw_job.pop("match_missing_skills", None)
    raw_job.pop("semantic_score", None)

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

    return _finalize_job(
        job_id, job_fields["title"], job_fields["company"], result,
        match_score=match_score, match_breakdown=match_breakdown,
    )


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
    # Loaded once for the whole run and threaded from here down. cv_text stays
    # only as the fallback for a run before anything has been extracted.
    profile = _candidate_profile()
    from agents import cv_profile as _cv_profile
    candidate_text = _cv_profile.profile_text(profile) if profile else cv_text
    position = position if position is not None else config.SEARCH_POSITION_QUERY
    seniority = seniority if seniority is not None else config.SEARCH_SENIORITY_LEVEL
    max_results_per_site = max_results_per_site if max_results_per_site is not None else config.SEARCH_MAX_RESULTS_PER_SITE
    max_age_days = max_age_days if max_age_days is not None else config.SEARCH_MAX_AGE_DAYS

    # Records every stage for the Excel debug report. A NullTrace when
    # SEARCH_DEBUG_REPORTS is off, so this costs nothing when unused.
    from agents.search_trace import new_trace
    trace = new_trace(position)
    trace.set_meta(
        seniority=seniority,
        max_results_per_site=max_results_per_site,
        max_age_days=max_age_days,
        query_expansion_enabled=config.SEARCH_QUERY_EXPANSION,
        embedding_provider=config.EMBEDDING_PROVIDER,
        similarity_threshold=config.CV_JOB_SIMILARITY_THRESHOLD,
        match_threshold=config.MATCH_SCORE_THRESHOLD,
        serpapi_expansion_limit=config.SERPAPI_EXPANSION_LIMIT,
        search_only=config.SEARCH_ONLY_MODE,
    )

    # ---- Stage 1: query expansion ------------------------------------------
    # Widens one typed Position into the set of role phrases that describe the
    # same kind of job, so a "Software Engineer" search also finds the
    # "Backend Developer" postings it would otherwise never fetch or keep.
    # Cached per position, and degrades to [position] if the LLM is
    # unavailable -- i.e. exactly the pre-expansion behavior.
    with trace.stage("query expansion"):
        try:
            from agents import query_expansion_agent
            target_roles = query_expansion_agent.get_target_roles(
                position, candidate_text=candidate_text)
            # Report where the phrases actually came from rather than assuming
            # the LLM produced them -- a cache hit and a fresh generation look
            # identical in the returned list.
            trace.record_expansion(
                target_roles, cached=query_expansion_agent.LAST_EXPANSION_CACHED,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[matching] query expansion unavailable, using the typed position only: {exc}")
            trace.record_error("query_expansion", position, exc)
            target_roles = [position] if position else []
            trace.record_expansion(target_roles, cached=False, source="fallback")

    # ---- Stage 2: retrieval -------------------------------------------------
    with trace.stage("retrieval (all sources)"):
        found, source_errors = run_search(
            position=position,
            seniority=seniority,
            max_results_per_site=max_results_per_site,
            max_age_days=max_age_days,
            target_roles=target_roles,
            trace=trace,
            # Carry title-mismatched jobs forward so the semantic retriever
            # below can rescue the ones whose description fits the CV. Without
            # this the semantic path has nothing to work on -- run_search's
            # own title filter would already have discarded every job it
            # exists to recover.
            include_title_mismatches=True,
        )

    # ---- Stage 3: drop jobs already in the DB ------------------------------
    # Before any embedding/LLM cost is spent on them. These would be caught by
    # the dedupe check inside _insert_job_row anyway, but only AFTER ranking
    # had already paid for them.
    with trace.stage("drop already-known URLs"):
        new_jobs = _drop_already_known(found)
        known_urls = {_job_url(j) for j in new_jobs}
        for job in found:
            if _job_url(job) not in known_urls:
                trace.record_retrieval(job, "token", "dropped", "already in the Jobs database")

    # ---- Stage 4: semantic retrieval + ranking -----------------------------
    ranked = _rank_candidates(new_jobs, cv_text, target_roles, seniority, trace=trace,
                              profile=profile)

    # ---- Stage 5: match-score gate -----------------------------------------
    # Off by default (MATCH_SCORE_THRESHOLD=0.0). Jobs held back here are
    # reported so a too-high threshold is visible rather than looking like
    # "the search found nothing".
    threshold = config.MATCH_SCORE_THRESHOLD
    if threshold > 0:
        passing = [j for j in ranked if (j.get("match_score") or 0) >= threshold]
        skipped_low_match = len(ranked) - len(passing)
    else:
        passing, skipped_low_match = ranked, 0
    for job in ranked:
        trace.record_gate(job, threshold, passed=job in passing)

    # ---- Search-only mode: stop here ---------------------------------------
    # Everything above is the matching pipeline; everything below is the
    # orchestrator (ATS scoring, CV rewriting, apply decisions) plus the DB
    # writes. Stopping here keeps a tuning loop cheap and repeatable: nothing
    # is persisted, so the next run sees the same jobs and its report is
    # directly comparable to this one.
    if config.SEARCH_ONLY_MODE:
        trace.set_meta(search_only=True)
        for err in source_errors:
            trace.record_error(err.get("source", ""), err.get("identifier"), err.get("error", ""))
        report_path = trace.write()
        print(f"[search-only] {len(found)} found, {len(new_jobs)} new, {len(ranked)} ranked, "
              f"{len(passing)} would be processed ({skipped_low_match} below the match gate). "
              f"Orchestrator skipped; nothing written to the database.")
        if report_path:
            print(f"[search-trace] debug report written to {report_path}")
        return {
            "processed": [],
            "errors": [],
            "found": len(found),
            "source_errors": source_errors,
            "target_roles": target_roles,
            "new_after_dedup": len(new_jobs),
            "ranked": len(ranked),
            "skipped_low_match": skipped_low_match,
            "report_path": report_path,
            "search_only": True,
            "would_process": len(passing),
        }

    processed = []
    errors = []

    with trace.stage("orchestrator (per job)"):
        for raw_job in passing:
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
                trace.record_error("orchestrator", url, exc)

    for err in source_errors:
        trace.record_error(err.get("source", ""), err.get("identifier"), err.get("error", ""))

    report_path = trace.write()
    if report_path:
        print(f"[search-trace] debug report written to {report_path}")

    return {
        "processed": processed,
        "errors": errors,
        "found": len(found),
        "source_errors": source_errors,
        "target_roles": target_roles,
        "new_after_dedup": len(new_jobs),
        "ranked": len(ranked),
        "skipped_low_match": skipped_low_match,
        "report_path": report_path,
    }


if __name__ == "__main__":
    outcome = run_daily_search_and_apply()
    if outcome.get("target_roles"):
        print("Searched as:", ", ".join(outcome["target_roles"]))
    for r in outcome["processed"]:
        print(r)
    for e in outcome["errors"]:
        print("ERROR:", e)
    for e in outcome["source_errors"]:
        print("SOURCE UNREACHABLE:", e)
    print(f"{outcome['found']} found, {outcome['new_after_dedup']} new, "
          f"{outcome['ranked']} ranked, {outcome['skipped_low_match']} below match threshold")
    print(f"{len(outcome['processed'])} processed, {len(outcome['errors'])} failed, "
          f"{len(outcome['source_errors'])} source(s) unreachable")
