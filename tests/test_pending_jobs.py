"""
Jobs the LLM cuts short -- its usage limit spent, or an answer that couldn't
be read -- are kept in pending_jobs and processed first on the next run,
instead of being reported as failures and depending on the search to find the
same posting again.
"""
import json
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db
from models import Base, Job, PendingJob
from agents.search_trace import NullTrace
from jobs import daily_run

TPD = ("Rate limit reached for model `m` on tokens per day (TPD): Limit 200000, "
       "Used 197061, Requested 4864. Please try again in 17m13.2s.")
TPM = ("Rate limit reached for model `m` on tokens per minute (TPM): Limit 8000, "
       "Used 6999, Requested 5262. Please try again in 1.5s.")
TOO_BIG = ("Rate limit reached for model `m` on tokens per minute (TPM): Limit 8000, "
           "Requested 15262.")
UNREADABLE = ("Requirement extraction returned no requirements for a 9171-character "
              "posting. The model answered with something that was not the requested JSON")
OK = {"status": "pending_review", "ats_result": {"score": 0.7}, "cv_path": None}


@pytest.fixture()
def temp_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pending.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    original = db.engine, db.SessionLocal
    db.engine, db.SessionLocal = engine, Session
    try:
        yield engine
    finally:
        db.engine, db.SessionLocal = original
        engine.dispose()  # release the file before tmp_path is cleaned up (Windows)


def _job(n, **extra):
    return {"url": f"https://example.com/jobs/{n}", "title": f"ML Engineer {n}",
            "company": "Acme", "source": "linkedin", "description": "Build models.",
            "match_score": 0.8, "match_breakdown": {"skills": 0.9}, **extra}


def _pending_urls():
    with db.get_session() as session:
        return [r.url for r in session.query(PendingJob).order_by(PendingJob.id)]


def _run(jobs):
    return daily_run._process_batch(jobs, "cv text", NullTrace())


def test_daily_limit_sets_aside_the_rest_of_the_batch_without_calling_the_model(temp_db):
    calls = []

    def invoke(state):
        calls.append(state["job"]["url"])
        if len(calls) == 1:
            return OK
        raise RuntimeError(TPD)

    with patch("jobs.daily_run.orchestrator_app.invoke", side_effect=invoke):
        out = _run([_job(1), _job(2), _job(3), _job(4)])

    assert len(out["processed"]) == 1
    assert out["errors"] == []
    assert [q["url"] for q in out["queued"]] == [f"https://example.com/jobs/{n}" for n in (2, 3, 4)]
    assert len(calls) == 2  # jobs 3 and 4 never reached the model
    assert _pending_urls() == [f"https://example.com/jobs/{n}" for n in (2, 3, 4)]
    with db.get_session() as session:  # no half-written Job rows for the waiting jobs
        assert session.query(Job).count() == 1


def test_waiting_jobs_run_first_next_time_and_leave_the_queue(temp_db):
    with patch("jobs.daily_run.orchestrator_app.invoke", side_effect=RuntimeError(TPD)):
        _run([_job(1), _job(2)])
    assert len(_pending_urls()) == 2

    seen = []

    def invoke(state):
        seen.append(state["job"]["url"])
        return OK

    with patch("jobs.daily_run.orchestrator_app.invoke", side_effect=invoke):
        # Today's search returns job 2 again plus a new job 3.
        out = _run([_job(2), _job(3)])

    assert seen == [f"https://example.com/jobs/{n}" for n in (1, 2, 3)]  # waiting first, no repeat
    assert out["resumed"] == 2
    assert len(out["processed"]) == 3
    assert _pending_urls() == []


def test_waiting_job_keeps_its_match_score(temp_db):
    with patch("jobs.daily_run.orchestrator_app.invoke", side_effect=RuntimeError(TPD)):
        _run([_job(1)])
    with db.get_session() as session:
        payload = json.loads(session.query(PendingJob).one().payload)
    assert payload["match_score"] == 0.8 and payload["match_breakdown"] == {"skills": 0.9}


def test_per_minute_limit_is_waited_out_and_retried(temp_db, monkeypatch):
    slept = []
    monkeypatch.setattr(daily_run.time, "sleep", slept.append)
    attempts = []

    def invoke(state):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError(TPM)
        return OK

    with patch("jobs.daily_run.orchestrator_app.invoke", side_effect=invoke):
        out = _run([_job(1)])
    assert slept == [2.5]
    assert len(out["processed"]) == 1 and out["queued"] == []


def test_request_bigger_than_the_window_is_an_ordinary_failure(temp_db):
    with patch("jobs.daily_run.orchestrator_app.invoke", side_effect=RuntimeError(TOO_BIG)):
        out = _run([_job(1), _job(2)])
    assert len(out["errors"]) == 2 and out["queued"] == []
    assert _pending_urls() == []


def test_unreadable_answer_is_retried_a_few_times_then_reported(temp_db):
    with patch("jobs.daily_run.orchestrator_app.invoke", side_effect=RuntimeError(UNREADABLE)):
        for _ in range(daily_run.PENDING_MAX_ATTEMPTS - 1):
            out = _run([_job(1)])
            assert out["queued"] and not out["errors"]
        out = _run([])  # the waiting job alone, last attempt
    assert out["queued"] == [] and len(out["errors"]) == 1
    assert _pending_urls() == []


def test_unreadable_answer_does_not_stop_the_batch(temp_db):
    def invoke(state):
        if state["job"]["url"].endswith("/1"):
            raise RuntimeError(UNREADABLE)
        return OK

    with patch("jobs.daily_run.orchestrator_app.invoke", side_effect=invoke):
        out = _run([_job(1), _job(2)])
    assert len(out["processed"]) == 1 and [q["reason"] for q in out["queued"]] == ["unreadable"]


def test_ordinary_failure_is_reported_not_kept(temp_db):
    with patch("jobs.daily_run.orchestrator_app.invoke", side_effect=RuntimeError("boom")):
        out = _run([_job(1)])
    assert out["errors"][0]["error"] == "boom" and out["queued"] == []
    assert _pending_urls() == []


def test_pending_summary_counts_limit_jobs(temp_db):
    with patch("jobs.daily_run.orchestrator_app.invoke", side_effect=RuntimeError(TPD)):
        _run([_job(1), _job(2)])
    assert daily_run.pending_summary() == {"count": 2, "limit": 2}


# ---- the Stop button -------------------------------------------------------------

def test_stop_saves_the_unprocessed_jobs_and_leaves_waiting_ones_alone(temp_db):
    with patch("jobs.daily_run.orchestrator_app.invoke", side_effect=RuntimeError(TPD)):
        _run([_job(1)])  # job 1 now waits (limit)

    calls = []

    def invoke(state):
        calls.append(state["job"]["url"])
        daily_run._stop_requested.set()  # the user presses Stop while job 1 is scored
        return OK

    try:
        with patch("jobs.daily_run.orchestrator_app.invoke", side_effect=invoke):
            out = _run([_job(2), _job(3)])
    finally:
        daily_run._stop_requested.clear()

    assert calls == ["https://example.com/jobs/1"]  # the job in progress finishes, nothing after it
    assert out["resumed"] == 1
    assert [(q["url"][-1], q["reason"]) for q in out["queued"]] == [("2", "stopped"), ("3", "stopped")]
    assert _pending_urls() == ["https://example.com/jobs/2", "https://example.com/jobs/3"]


def test_stopped_jobs_do_not_count_as_failed_attempts(temp_db):
    daily_run._stop_requested.set()
    try:
        for _ in range(daily_run.PENDING_MAX_ATTEMPTS + 1):
            _run([_job(1)])
    finally:
        daily_run._stop_requested.clear()
    with db.get_session() as session:
        assert session.query(PendingJob).one().attempts == 0


def test_request_stop_only_when_a_run_is_in_progress():
    assert daily_run.request_stop() is False
    daily_run._running.set()
    try:
        assert daily_run.request_stop() is True
        assert daily_run._stop_requested.is_set()
    finally:
        daily_run._running.clear()
        daily_run._stop_requested.clear()


def test_run_search_queries_nothing_once_stopped(monkeypatch):
    from agents import search_agent
    import config
    called = []
    monkeypatch.setattr(config, "GREENHOUSE_BOARD_TOKENS", ["acme"])
    monkeypatch.setattr(search_agent, "search_greenhouse", lambda b: called.append(b) or [])
    monkeypatch.setattr(search_agent, "get_configured_sites", lambda: [{"url": "https://x", "site_type": "generic", "identifier": None}])
    monkeypatch.setattr(search_agent, "search_generic_site", lambda *a, **k: called.append("generic") or [])
    monkeypatch.setattr(search_agent, "search_from_watchlist", lambda *a, **k: [])
    jobs, errors = search_agent.run_search(position="ML Engineer", target_roles=["ML Engineer"],
                                           should_stop=lambda: True)
    assert jobs == [] and errors == [] and called == []
