"""
jobs/daily_run.py's matching pipeline wiring: the already-known-URL dedupe
that runs BEFORE any embedding cost, the token/semantic union, and the
match-score gate.

Uses a temp SQLite database swapped into db.engine/db.SessionLocal, the same
isolation pattern tests/test_daily_run_transactions.py already uses -- the
real data/job_agent.db is never touched.
"""
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import db
from models import Base, Job


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    return engine


# ---- _drop_already_known ----------------------------------------------------

def test_drops_jobs_whose_url_is_already_in_the_database(temp_db):
    """This is what keeps the daily schedule from re-embedding and re-ranking
    the same postings every morning -- without it, that cost is paid on every
    run and only recognized as a duplicate at INSERT time, after the fact."""
    from jobs.daily_run import _drop_already_known
    with db.get_session() as session:
        session.add(Job(url="https://x/already-seen", title="Old", company="A", source_site="greenhouse"))

    jobs = [
        {"title": "Old", "url": "https://x/already-seen"},
        {"title": "New", "url": "https://x/brand-new"},
    ]
    remaining = _drop_already_known(jobs)
    assert [j["title"] for j in remaining] == ["New"]


def test_keeps_everything_when_the_database_is_empty(temp_db):
    from jobs.daily_run import _drop_already_known
    jobs = [{"title": "A", "url": "https://x/1"}, {"title": "B", "url": "https://x/2"}]
    assert len(_drop_already_known(jobs)) == 2


def test_handles_jobs_with_no_url(temp_db):
    from jobs.daily_run import _drop_already_known
    assert _drop_already_known([{"title": "No URL"}]) == [{"title": "No URL"}]


def test_recognizes_the_alternate_url_keys(temp_db):
    from jobs.daily_run import _drop_already_known
    with db.get_session() as session:
        session.add(Job(url="https://x/1", title="Old", company="A", source_site="lever"))
    jobs = [{"title": "Old", "hostedUrl": "https://x/1"}]
    assert _drop_already_known(jobs) == []


# ---- _rank_candidates -------------------------------------------------------

def test_semantic_path_only_runs_on_token_path_misses():
    """Re-embedding a job the title filter already accepted would spend a call
    to confirm a decision that's already made -- the two paths are unioned."""
    from jobs.daily_run import _rank_candidates
    jobs = [{"title": "Software Engineer", "url": "https://x/1", "description": "d"},
            {"title": "Backend Developer", "url": "https://x/2", "description": "d"}]

    with patch("agents.embeddings.embed_candidate", return_value=[1.0, 0.0]), \
         patch("agents.ranking_agent.semantic_retrieval_path", return_value=[]) as mock_semantic, \
         patch("agents.ranking_agent.rank_jobs", side_effect=lambda jobs, *a, **k: jobs):
        _rank_candidates(jobs, "cv text", ["Software Engineer"], "")

    passed_to_semantic = mock_semantic.call_args[0][0]
    assert [j["title"] for j in passed_to_semantic] == ["Backend Developer"]


def test_semantic_recoveries_are_unioned_with_token_matches():
    from jobs.daily_run import _rank_candidates
    jobs = [{"title": "Software Engineer", "url": "https://x/1"},
            {"title": "Backend Developer", "url": "https://x/2"}]
    recovered = [{"title": "Backend Developer", "url": "https://x/2", "semantic_score": 0.9}]

    with patch("agents.embeddings.embed_candidate", return_value=[1.0, 0.0]), \
         patch("agents.ranking_agent.semantic_retrieval_path", return_value=recovered), \
         patch("agents.ranking_agent.rank_jobs", side_effect=lambda jobs, *a, **k: jobs):
        out = _rank_candidates(jobs, "cv", ["Software Engineer"], "")

    assert {j["title"] for j in out} == {"Software Engineer", "Backend Developer"}


def test_unavailable_embeddings_degrade_instead_of_failing_the_run():
    """Embeddings depend on a local Ollama (or a Gemini key) the user may not
    have set up. Losing them costs recall, not the whole search."""
    from jobs.daily_run import _rank_candidates
    jobs = [{"title": "Software Engineer", "url": "https://x/1"},
            {"title": "Backend Developer", "url": "https://x/2"}]

    with patch("agents.embeddings.embed_candidate", side_effect=RuntimeError("ollama not running")), \
         patch("agents.ranking_agent.rank_jobs", side_effect=lambda jobs, *a, **k: jobs):
        out = _rank_candidates(jobs, "cv", ["Software Engineer"], "")

    # token-matched job survives; the title-mismatched one is lost (that's the
    # recall the semantic path would have recovered), but nothing raised.
    assert [j["title"] for j in out] == ["Software Engineer"]


def test_ranking_failure_falls_back_to_unranked_candidates():
    from jobs.daily_run import _rank_candidates
    jobs = [{"title": "Software Engineer", "url": "https://x/1"}]
    with patch("agents.embeddings.embed_candidate", return_value=[1.0]), \
         patch("agents.ranking_agent.semantic_retrieval_path", return_value=[]), \
         patch("agents.ranking_agent.rank_jobs", side_effect=RuntimeError("boom")):
        out = _rank_candidates(jobs, "cv", ["Software Engineer"], "")
    assert [j["title"] for j in out] == ["Software Engineer"]


# ---- per-run cost ceilings --------------------------------------------------
# Gemini's free tier meters embeddings at 100 per MINUTE, counted per job
# description. Groq's free tier is 200k tokens per DAY. A 250-job run
# previously spent 250 embeddings AND 250 LLM calls (~197k tokens -- the whole
# daily budget) ranking jobs that mostly never get applied to.

def test_ranking_never_calls_an_llm(monkeypatch):
    from jobs.daily_run import _rank_candidates
    jobs = [{"title": f"Engineer {i}", "description": "Python and Docker", "url": f"u{i}"}
            for i in range(30)]

    def explode(*a, **k):
        raise AssertionError("ranking must not call an LLM")

    with patch("agents.ats_agent.get_llm", side_effect=explode), \
         patch("agents.embeddings.embed_candidate", return_value=[1.0, 0.0]), \
         patch("agents.ranking_agent.embed_texts", side_effect=lambda t: [[1.0, 0.0]] * len(t)):
        ranked = _rank_candidates(jobs, "Skills:\nPython, Docker", ["Engineer"], "")
    assert len(ranked) == 30


def test_embeddings_stay_within_the_per_run_budget(monkeypatch):
    """Both paths draw on one budget: the semantic rescue path AND scoring the
    token-matched jobs. Capping only one of them isn't enough."""
    from jobs.daily_run import _rank_candidates
    monkeypatch.setattr(config, "EMBEDDING_MAX_PER_RUN", 40)
    monkeypatch.setattr(config, "CV_JOB_SIMILARITY_THRESHOLD", 0.5)

    jobs = ([{"title": f"Engineer {i}", "description": "Python", "url": f"u{i}"} for i in range(100)]
            + [{"title": f"Unrelated {i}", "description": "Other", "url": f"v{i}"} for i in range(30)])

    embedded = {"n": 0}

    def counting(texts):
        embedded["n"] += len(texts)
        return [[1.0, 0.0]] * len(texts)

    with patch("agents.embeddings.embed_candidate", return_value=[1.0, 0.0]), \
         patch("agents.ranking_agent.embed_texts", side_effect=counting):
        ranked = _rank_candidates(jobs, "Skills:\nPython", ["Engineer"], "")

    assert embedded["n"] <= config.EMBEDDING_MAX_PER_RUN
    assert len(ranked) == 130  # nothing dropped -- only the semantic factor degrades


def test_budget_skipped_jobs_score_neutral_rather_than_zero(monkeypatch):
    from jobs.daily_run import _rank_candidates
    from agents import ranking_agent
    monkeypatch.setattr(config, "EMBEDDING_MAX_PER_RUN", 2)

    jobs = [{"title": f"Engineer {i}", "description": "Python", "url": f"u{i}"} for i in range(10)]
    with patch("agents.embeddings.embed_candidate", return_value=[1.0, 0.0]), \
         patch("agents.ranking_agent.embed_texts", side_effect=lambda t: [[1.0, 0.0]] * len(t)):
        ranked = _rank_candidates(jobs, "Skills:\nPython", ["Engineer"], "")

    semantic = [j["match_breakdown"]["semantic"]["score"] for j in ranked]
    assert semantic.count(ranking_agent.NEUTRAL) == 8   # skipped -> neutral, not 0
    assert all(s != 0.0 for s in semantic)


# ---- search-only mode -------------------------------------------------------
# Runs the matching pipeline, writes the report, stops before the orchestrator.
# The point is a cheap repeatable tuning loop: no per-job LLM calls, and
# nothing persisted, so consecutive runs see the same jobs.

def _stub_pipeline(monkeypatch, daily_run, ranked):
    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION", False)
    monkeypatch.setattr(daily_run, "find_default_cv", lambda d: "cv/fake.pdf")
    monkeypatch.setattr(daily_run, "parse_cv", lambda p: "cv text")
    monkeypatch.setattr(daily_run, "init_db", lambda: None)
    monkeypatch.setattr(daily_run, "run_search", lambda **kw: (
        [{"title": j["title"], "url": j["url"]} for j in ranked], []
    ))
    monkeypatch.setattr(daily_run, "_rank_candidates", lambda *a, **k: ranked)


def test_search_only_mode_skips_the_orchestrator_entirely(temp_db, monkeypatch):
    import jobs.daily_run as daily_run
    monkeypatch.setattr(config, "SEARCH_ONLY_MODE", True)
    monkeypatch.setattr(config, "MATCH_SCORE_THRESHOLD", 0.0)
    ranked = [{"title": "A", "url": "https://x/1", "match_score": 0.9},
              {"title": "B", "url": "https://x/2", "match_score": 0.5}]
    _stub_pipeline(monkeypatch, daily_run, ranked)

    def explode(*a, **k):
        raise AssertionError("orchestrator must not run in search-only mode")

    monkeypatch.setattr(daily_run, "_process_one_job", explode)

    result = daily_run.run_daily_search_and_apply(position="AI Engineer")
    assert result["search_only"] is True
    assert result["processed"] == []
    assert result["ranked"] == 2
    assert result["would_process"] == 2       # reports what it WOULD have done


def test_search_only_mode_writes_nothing_to_the_database(temp_db, monkeypatch):
    """Repeatability is the point -- the next run must see the same jobs."""
    import jobs.daily_run as daily_run
    from models import Job, Application
    monkeypatch.setattr(config, "SEARCH_ONLY_MODE", True)
    monkeypatch.setattr(config, "MATCH_SCORE_THRESHOLD", 0.0)
    _stub_pipeline(monkeypatch, daily_run, [{"title": "A", "url": "https://x/1", "match_score": 0.9}])

    daily_run.run_daily_search_and_apply(position="AI Engineer")

    with db.get_session() as session:
        assert session.query(Job).count() == 0
        assert session.query(Application).count() == 0


def test_search_only_mode_still_writes_the_debug_report(temp_db, monkeypatch, tmp_path):
    """The report is the whole output of this mode -- if it didn't write one,
    the run would produce nothing at all."""
    import jobs.daily_run as daily_run
    monkeypatch.setattr(config, "SEARCH_ONLY_MODE", True)
    monkeypatch.setattr(config, "SEARCH_DEBUG_REPORTS", True)
    monkeypatch.setattr(config, "SEARCH_REPORT_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MATCH_SCORE_THRESHOLD", 0.0)
    _stub_pipeline(monkeypatch, daily_run, [{"title": "A", "url": "https://x/1", "match_score": 0.9}])

    result = daily_run.run_daily_search_and_apply(position="AI Engineer")
    assert result["report_path"]
    assert list(tmp_path.glob("search_report_*.xlsx"))


def test_search_only_mode_still_applies_the_match_gate(temp_db, monkeypatch):
    """Gate behaviour must match the real pipeline's, or tuning against this
    mode wouldn't predict what the real run does."""
    import jobs.daily_run as daily_run
    monkeypatch.setattr(config, "SEARCH_ONLY_MODE", True)
    monkeypatch.setattr(config, "MATCH_SCORE_THRESHOLD", 0.6)
    ranked = [{"title": "A", "url": "https://x/1", "match_score": 0.9},
              {"title": "B", "url": "https://x/2", "match_score": 0.5}]
    _stub_pipeline(monkeypatch, daily_run, ranked)

    result = daily_run.run_daily_search_and_apply(position="AI Engineer")
    assert result["would_process"] == 1
    assert result["skipped_low_match"] == 1


def test_full_pipeline_runs_when_search_only_is_off(temp_db, monkeypatch):
    import jobs.daily_run as daily_run
    monkeypatch.setattr(config, "SEARCH_ONLY_MODE", False)
    monkeypatch.setattr(config, "MATCH_SCORE_THRESHOLD", 0.0)
    _stub_pipeline(monkeypatch, daily_run, [{"title": "A", "url": "https://x/1", "match_score": 0.9}])

    called = []
    monkeypatch.setattr(daily_run, "_process_one_job", lambda job, cv: called.append(job["title"]) or {
        "title": job["title"], "company": "", "status": "pending_review",
    })

    result = daily_run.run_daily_search_and_apply(position="AI Engineer")
    assert called == ["A"]
    assert "search_only" not in result


# ---- match score gate -------------------------------------------------------

def test_gate_is_off_by_default(monkeypatch):
    """MATCH_SCORE_THRESHOLD defaults to 0.0 deliberately: a non-zero default
    would silently discard jobs against a threshold nobody has calibrated.

    Asserts the DEFAULT the code falls back to with the variable unset -- not
    config.MATCH_SCORE_THRESHOLD itself, which reflects whatever the user has
    since saved from the Settings page and is theirs to change.
    """
    monkeypatch.delenv("MATCH_SCORE_THRESHOLD", raising=False)
    assert config._float("MATCH_SCORE_THRESHOLD", 0.0) == 0.0


def test_gate_filters_and_counts_what_it_held_back(temp_db, monkeypatch):
    """The count is reported rather than silently dropped, so a too-high
    threshold looks like 'held back N' instead of 'found nothing'."""
    import jobs.daily_run as daily_run

    monkeypatch.setattr(config, "MATCH_SCORE_THRESHOLD", 0.6)
    # Pinned, not inherited: this test is about the ORCHESTRATOR being reached,
    # so it must not silently pass just because the developer's .env happens to
    # have search-only mode on.
    monkeypatch.setattr(config, "SEARCH_ONLY_MODE", False)
    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION", False)
    monkeypatch.setattr(daily_run, "find_default_cv", lambda d: "cv/fake.pdf")
    monkeypatch.setattr(daily_run, "parse_cv", lambda p: "cv text")
    monkeypatch.setattr(daily_run, "init_db", lambda: None)
    monkeypatch.setattr(daily_run, "run_search", lambda **kw: (
        [{"title": "Good", "url": "https://x/1"}, {"title": "Poor", "url": "https://x/2"}], []
    ))
    monkeypatch.setattr(daily_run, "_rank_candidates", lambda *a, **k: [
        {"title": "Good", "url": "https://x/1", "match_score": 0.9},
        {"title": "Poor", "url": "https://x/2", "match_score": 0.2},
    ])

    processed = []
    monkeypatch.setattr(daily_run, "_process_one_job", lambda job, cv: processed.append(job["title"]) or {
        "title": job["title"], "company": "", "status": "pending_review",
    })

    result = daily_run.run_daily_search_and_apply(position="Software Engineer")

    assert processed == ["Good"]
    assert result["skipped_low_match"] == 1
    assert result["ranked"] == 2


def test_threshold_of_zero_processes_everything(temp_db, monkeypatch):
    import jobs.daily_run as daily_run

    monkeypatch.setattr(config, "MATCH_SCORE_THRESHOLD", 0.0)
    monkeypatch.setattr(config, "SEARCH_ONLY_MODE", False)   # see note above
    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION", False)
    monkeypatch.setattr(daily_run, "find_default_cv", lambda d: "cv/fake.pdf")
    monkeypatch.setattr(daily_run, "parse_cv", lambda p: "cv text")
    monkeypatch.setattr(daily_run, "init_db", lambda: None)
    monkeypatch.setattr(daily_run, "run_search", lambda **kw: (
        [{"title": "Good", "url": "https://x/1"}, {"title": "Poor", "url": "https://x/2"}], []
    ))
    monkeypatch.setattr(daily_run, "_rank_candidates", lambda *a, **k: [
        {"title": "Good", "url": "https://x/1", "match_score": 0.9},
        {"title": "Poor", "url": "https://x/2", "match_score": 0.01},
    ])

    processed = []
    monkeypatch.setattr(daily_run, "_process_one_job", lambda job, cv: processed.append(job["title"]) or {
        "title": job["title"], "company": "", "status": "pending_review",
    })

    result = daily_run.run_daily_search_and_apply(position="Software Engineer")

    assert processed == ["Good", "Poor"]
    assert result["skipped_low_match"] == 0


def test_expansion_failure_falls_back_to_the_typed_position(temp_db, monkeypatch):
    import jobs.daily_run as daily_run

    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION", True)
    monkeypatch.setattr(daily_run, "find_default_cv", lambda d: "cv/fake.pdf")
    monkeypatch.setattr(daily_run, "parse_cv", lambda p: "cv text")
    monkeypatch.setattr(daily_run, "init_db", lambda: None)
    monkeypatch.setattr(daily_run, "_rank_candidates", lambda jobs, *a, **k: jobs)
    monkeypatch.setattr(daily_run, "_process_one_job", lambda job, cv: None)

    seen = {}
    monkeypatch.setattr(daily_run, "run_search", lambda **kw: seen.update(kw) or ([], []))

    with patch("agents.query_expansion_agent.get_target_roles", side_effect=RuntimeError("llm down")):
        result = daily_run.run_daily_search_and_apply(position="AI Engineer")

    assert seen["target_roles"] == ["AI Engineer"]
    assert result["target_roles"] == ["AI Engineer"]
