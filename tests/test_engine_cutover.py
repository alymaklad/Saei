"""
Guards for the 2026-08-25 switch of the live pipeline to the requirements
engine (config.SCORING_ENGINE default "legacy" -> "requirements").

The switch itself is one line. What these cover is the three things that break
QUIETLY when it happens -- each one still produces a score, so none of them
would surface as an error:

  1. orchestrator.rewrite_node reused the legacy reuse payload (a flat list of
     skill names). The requirements engine ignores that shape and silently
     re-extracts, and separately re-parses the tailored CV -- two extra LLM
     calls per rewrite, on the path that already failed against Groq's
     8,000-tokens-per-minute limit AFTER the rewrite had been paid for.
  2. Stored rows written before the switch hold legacy-shaped breakdowns. Read
     back under the new engine's renderer they show as empty, not as an error.
  3. A score from one engine is not comparable with a score from the other, so
     rows have to say which one produced them.
"""
import importlib
import json
from unittest.mock import patch

import pytest

import config
import orchestrator
from agents import ats_agent


# ---- 1. the rescore path reuses everything it should ------------------------

REQUIREMENTS_RESULT = {
    "engine": "requirements",
    "score": 0.42,
    "breakdown": {"required_skills": {"label": "Required skills & tools",
                                      "score": 0.4, "weight": 1.0, "items": []}},
    "explanation": "…",
    "missing_skills": ["Kubernetes"],
    "required_skills": ["Python", "Kubernetes"],
    "requirements": {"requirements": [{"name": "Python", "canonical": "python",
                                       "category": "technical_skill",
                                       "importance": "required"}],
                     "years_experience_required": 3, "seniority": None},
    "requirement_results": [],
    "cv_profile": {"experience": [], "projects": [], "education": [],
                   "certifications": [], "skills_claimed": [], "_cached": True},
    "cv_years": 1.2,
}

LEGACY_RESULT = {
    "engine": "legacy",
    "score": 0.5,
    "breakdown": {"keyword_match": {"score": 0.5, "weight": 0.45,
                                    "matched_skills": [], "missing_skills": [],
                                    "required_skills": []}},
    "explanation": "…",
    "missing_skills": [],
    "required_skills": ["Python", "AWS"],
}


def test_rescore_reuses_the_whole_structured_extraction_not_just_names():
    """A flat list of names fails compute_requirements_score's
    `extracted.get("requirements")` check, so it re-extracts -- paying for a
    second extraction of a job description it already extracted."""
    kwargs = orchestrator._rescore_kwargs(REQUIREMENTS_RESULT, "tailored text")

    assert kwargs["required_skills"] == REQUIREMENTS_RESULT["requirements"]
    assert isinstance(kwargs["required_skills"], dict)
    assert kwargs["required_skills"]["requirements"]  # the shape the engine accepts


def test_rescore_passes_the_profile_and_evidence_text():
    """Together these are what remove the second CV-parsing LLM call."""
    kwargs = orchestrator._rescore_kwargs(REQUIREMENTS_RESULT, "tailored text")
    assert kwargs["evidence_text"] == "tailored text"
    assert kwargs["profile"] is REQUIREMENTS_RESULT["cv_profile"]


def test_rescore_omits_the_profile_when_there_isnt_one():
    """compute_requirements_score builds one itself when profile is absent;
    passing profile=None explicitly would be the same thing, but omitting it
    keeps the "only send what we actually have" contract visible."""
    without = {k: v for k, v in REQUIREMENTS_RESULT.items() if k != "cv_profile"}
    assert "profile" not in orchestrator._rescore_kwargs(without, "t")


def test_a_full_rewrite_run_costs_no_extra_llm_calls(monkeypatch, tmp_path):
    """The end-to-end version of the three tests above: score -> rewrite ->
    rescore must not re-extract requirements or re-parse the CV."""

    calls = {"extract": 0, "profile": 0, "entail": 0}

    def fake_extract(_jd):
        calls["extract"] += 1
        return REQUIREMENTS_RESULT["requirements"]

    def fake_profile(_cv, use_cache=True):
        calls["profile"] += 1
        return REQUIREMENTS_RESULT["cv_profile"]

    def fake_entail(unmatched, spans, decisions=None):
        # `decisions` is the retrieval shortlist (Level B). Accepted here so
        # this double keeps matching the real signature -- the point of the
        # test is the CALL COUNT, and a double that silently stops being
        # callable would pass by failing early instead.
        calls["entail"] += 1
        return {}

    from agents import cv_profile
    monkeypatch.setattr(ats_agent, "extract_requirements", fake_extract)
    monkeypatch.setattr(cv_profile, "build_profile", fake_profile)
    monkeypatch.setattr(ats_agent, "_entailment_pass", fake_entail)
    monkeypatch.setattr(orchestrator, "rewrite_cv", lambda *a, **k: "TAILORED CV TEXT")
    monkeypatch.setattr(orchestrator, "save_cv_as_pdf", lambda *a, **k: None)

    state = {"job": {"id": 1, "description": "Needs Python."},
             "cv_text": "SKILLS\nPython\n", "ats_result": None}
    state = orchestrator.score_node(state)
    state = orchestrator.rewrite_node(state)

    assert calls["extract"] == 1, "the job description must only be extracted once"
    assert calls["profile"] == 1, "the CV must only be parsed once, not again for the rewrite"
    assert state["tailored_ats_result"]["engine"] == "requirements"


# ---- 2 & 3. stored rows say which engine produced them -----------------------

@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path/'cutover.db'}")
    import db
    importlib.reload(db)
    db.Base.metadata.create_all(db.engine)
    return db


def test_the_new_column_is_added_to_an_existing_database(tmp_path, monkeypatch):
    """create_all() only creates missing TABLES -- a new column on a table that
    already exists needs the explicit ADD COLUMN in db._migrate_schema."""
    import db as db_module
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path/'old.db'}")
    db = importlib.reload(db_module)

    # A pre-cutover applications table: no scoring_engine column.
    with db.engine.connect() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE applications (id INTEGER PRIMARY KEY, job_id INTEGER, "
            "ats_score FLOAT, cv_version_path TEXT, status TEXT, "
            "email_sent BOOLEAN, date_applied DATETIME, date_created DATETIME)")
        conn.exec_driver_sql(
            "INSERT INTO applications (id, ats_score, status) VALUES (1, 0.542, 'scored_low')")
        conn.commit()

    db._migrate_schema()

    with db.engine.connect() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(applications)")}
        assert "scoring_engine" in cols
        # The pre-existing row survives, with NULL for the new column.
        row = conn.exec_driver_sql(
            "SELECT ats_score, scoring_engine FROM applications WHERE id=1").first()
        assert row[0] == 0.542 and row[1] is None


def test_the_api_reports_legacy_for_rows_that_predate_the_column(db_env, monkeypatch, tmp_path):
    """NULL is meaningful, not missing: everything written before the flag
    existed was legacy. Defaulted server-side so each frontend doesn't
    reimplement the same fallback."""
    from models import Application, Job
    from db import get_session
    from fastapi.testclient import TestClient

    with get_session() as session:
        session.add(Job(id=1, url="http://x", title="Old", company="Acme"))
        session.add(Application(
            id=1, job_id=1, ats_score=0.542, scoring_engine=None,
            ats_breakdown=json.dumps({"keyword_match": {"score": 0.3, "weight": 0.45}}),
            status="scored_low"))
        session.add(Application(
            id=2, job_id=1, ats_score=0.319, scoring_engine="requirements",
            ats_breakdown=json.dumps({"required_skills": {"label": "Required skills & tools",
                                                          "score": 0.39, "weight": 0.56,
                                                          "items": []}}),
            status="scored_low"))

    import api
    importlib.reload(api)
    rows = TestClient(api.app).get("/api/applications").json()
    by_id = {r["id"]: r for r in rows}

    assert by_id[1]["scoring_engine"] == "legacy"
    assert by_id[2]["scoring_engine"] == "requirements"
    # Both breakdowns come back intact -- neither engine's rows are discarded.
    assert "keyword_match" in by_id[1]["ats_breakdown"]
    assert "required_skills" in by_id[2]["ats_breakdown"]


def test_daily_run_records_the_engine_from_the_result(db_env, monkeypatch):
    """Recorded from the result, not from config: the setting says what the
    NEXT run will do, while the row has to say what produced the breakdown
    stored beside it."""
    from jobs import daily_run
    from models import Application, Job
    from db import get_session

    with get_session() as session:
        session.add(Job(id=1, url="http://y", title="New", company="Acme"))
    daily_run._finalize_job(1, "New", "Acme", {
        "status": "scored_low",
        "ats_result": REQUIREMENTS_RESULT,
    })

    with get_session() as session:
        row = session.query(Application).filter_by(job_id=1).one()
        assert row.scoring_engine == "requirements"
        assert row.ats_score == 0.42
