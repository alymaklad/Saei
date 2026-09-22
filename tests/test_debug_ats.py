"""
The ATS/tailoring debug bench (POST /api/debug/ats).

Its whole value is being a fast, side-effect-free loop: run the scoring and
rewriting stages against a pasted job description, see the result, leave no
trace. These tests pin that promise -- a bench that quietly wrote rows would
pollute the very history you're trying to reason about.
"""
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import db
from models import Base, Job, Application

import api


CV = """
Aly Maklad
Skills: Python, PyTorch, Docker
Experience 2019 - 2023
Education: Bachelor of Science in Computer Engineering
"""

FAKE_ATS = {
    "score": 0.42,
    "keyword_score": 0.5,
    "llm_score": 0.4,
    "missing_skills": ["Kubernetes"],
    "required_skills": ["Python", "Kubernetes"],
    "breakdown": {"keyword_match": {"score": 0.5, "weight": 0.45}},
    "explanation": "because reasons",
}
FAKE_TAILORED = dict(FAKE_ATS, score=0.61)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    return engine


@pytest.fixture
def stub_cv(monkeypatch):
    monkeypatch.setattr(api.cv_parser, "find_default_cv", lambda d: "cv/current_cv.pdf")
    monkeypatch.setattr(api.cv_parser, "parse_cv", lambda p: CV)


def test_requires_a_job_description(stub_cv):
    with pytest.raises(Exception) as exc:
        api.debug_ats(api.AtsDebugRequest(job_description="   "))
    assert "job_description" in str(getattr(exc.value, "detail", exc.value))


def test_missing_cv_is_reported_clearly(monkeypatch):
    monkeypatch.setattr(api.cv_parser, "find_default_cv", lambda d: None)
    with pytest.raises(Exception) as exc:
        api.debug_ats(api.AtsDebugRequest(job_description="Python role"))
    assert "CV" in str(getattr(exc.value, "detail", exc.value))


def test_scores_and_rewrites_when_below_threshold(stub_cv, monkeypatch):
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.7)
    with patch("agents.ats_agent.compute_ats_score", side_effect=[FAKE_ATS, FAKE_TAILORED]), \
         patch("agents.cv_rewriter_agent.rewrite_cv", return_value="TAILORED CV"), \
         patch("agents.ats_agent.build_improvement_explanation", return_value="improved"):
        r = api.debug_ats(api.AtsDebugRequest(job_description="Need Python and Kubernetes"))

    assert r["ats"]["score"] == 0.42
    assert r["rewrite"]["tailored_cv"] == "TAILORED CV"
    assert r["rewrite"]["tailored_score"] == 0.61
    assert r["rewrite"]["delta"] == pytest.approx(0.19)
    assert r["rewrite"]["triggered_by"] == "score below threshold"


def test_rewrite_is_skipped_above_threshold_and_says_why(stub_cv, monkeypatch):
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.3)
    with patch("agents.ats_agent.compute_ats_score", return_value=FAKE_ATS), \
         patch("agents.cv_rewriter_agent.rewrite_cv") as mock_rewrite:
        r = api.debug_ats(api.AtsDebugRequest(job_description="Need Python"))
    mock_rewrite.assert_not_called()
    assert r["rewrite"] is None
    assert "fit threshold" in r["rewrite_skipped_because"]


def test_force_rewrite_runs_it_anyway(stub_cv, monkeypatch):
    """Lets you iterate on the rewrite prompt without hunting for a
    low-scoring posting."""
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.3)
    with patch("agents.ats_agent.compute_ats_score", side_effect=[FAKE_ATS, FAKE_TAILORED]), \
         patch("agents.cv_rewriter_agent.rewrite_cv", return_value="TAILORED"), \
         patch("agents.ats_agent.build_improvement_explanation", return_value="x"):
        r = api.debug_ats(api.AtsDebugRequest(job_description="Need Python", force_rewrite=True))
    assert r["rewrite"]["triggered_by"] == "forced"


def test_rescoring_reuses_the_extracted_skills(stub_cv, monkeypatch):
    """Same optimisation the real orchestrator uses -- otherwise the bench
    would cost an extra LLM call the production path doesn't, and its timings
    would mislead."""
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.7)
    with patch("agents.ats_agent.compute_ats_score", side_effect=[FAKE_ATS, FAKE_TAILORED]) as mock_score, \
         patch("agents.cv_rewriter_agent.rewrite_cv", return_value="TAILORED"), \
         patch("agents.ats_agent.build_improvement_explanation", return_value="x"):
        api.debug_ats(api.AtsDebugRequest(job_description="Need Python"))
    assert mock_score.call_args_list[1].kwargs["required_skills"] == FAKE_ATS.get("requirements")


def test_rescoring_reuses_the_whole_structured_extraction(stub_cv, monkeypatch):
    """The requirements engine reuses categories and importance too, not just
    a list of names -- otherwise the tailored CV would be judged against a
    freshly extracted, subtly different rubric and the before/after comparison
    would be measuring two things at once."""
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.7)
    reqs = {"requirements": [{"name": "Python", "category": "technical_skill",
                              "importance": "required"}],
            "years_experience_required": 3, "seniority": "mid"}
    original = dict(FAKE_ATS, engine="requirements", requirements=reqs)
    tailored = dict(FAKE_TAILORED, engine="requirements", requirements=reqs)

    with patch("agents.ats_agent.compute_ats_score", side_effect=[original, tailored]) as mock_score, \
         patch("agents.cv_rewriter_agent.rewrite_cv", return_value="TAILORED"), \
         patch("agents.ats_agent.build_improvement_explanation", return_value="x"), \
         patch("agents.ats_agent.compute_ats_compatibility", return_value={"score": 1.0, "status": "Pass", "checks": {}, "issues": []}):
        api.debug_ats(api.AtsDebugRequest(job_description="Need Python"))

    assert mock_score.call_args_list[1].kwargs["required_skills"] == reqs


def test_compatibility_is_always_reported_and_costs_no_llm_call(stub_cv, monkeypatch):
    """Parse-readiness depends only on the CV, so it is free, deterministic,
    and reported separately rather than folded into a per-job number."""
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.3)
    with patch("agents.ats_agent.compute_ats_score", return_value=FAKE_ATS):
        r = api.debug_ats(api.AtsDebugRequest(job_description="Need Python"))
    assert r["compatibility"]["status"] in ("Pass", "Warning", "Fail")


def test_writes_nothing_to_the_database(temp_db, stub_cv, monkeypatch):
    """The core promise of the bench."""
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.7)
    with patch("agents.ats_agent.compute_ats_score", side_effect=[FAKE_ATS, FAKE_TAILORED]), \
         patch("agents.cv_rewriter_agent.rewrite_cv", return_value="TAILORED"), \
         patch("agents.ats_agent.build_improvement_explanation", return_value="x"):
        api.debug_ats(api.AtsDebugRequest(job_description="Need Python"))

    with db.get_session() as s:
        assert s.query(Job).count() == 0
        assert s.query(Application).count() == 0


def test_no_pdf_is_written_unless_asked(stub_cv, monkeypatch):
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.7)
    with patch("agents.ats_agent.compute_ats_score", side_effect=[FAKE_ATS, FAKE_TAILORED]), \
         patch("agents.cv_rewriter_agent.rewrite_cv", return_value="TAILORED"), \
         patch("agents.ats_agent.build_improvement_explanation", return_value="x"), \
         patch("agents.cv_rewriter_agent.save_cv_as_pdf") as mock_pdf:
        r = api.debug_ats(api.AtsDebugRequest(job_description="Need Python"))
    mock_pdf.assert_not_called()
    assert r["rewrite"]["pdf_url"] is None


def test_pdf_failure_does_not_lose_the_tailored_text(stub_cv, monkeypatch):
    """The text is the expensive part -- a rendering bug shouldn't discard it."""
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.7)
    with patch("agents.ats_agent.compute_ats_score", side_effect=[FAKE_ATS, FAKE_TAILORED]), \
         patch("agents.cv_rewriter_agent.rewrite_cv", return_value="TAILORED"), \
         patch("agents.ats_agent.build_improvement_explanation", return_value="x"), \
         patch("agents.cv_rewriter_agent.save_cv_as_pdf", side_effect=RuntimeError("font blew up")):
        r = api.debug_ats(api.AtsDebugRequest(job_description="Need Python", render_pdf=True))
    assert r["rewrite"]["tailored_cv"] == "TAILORED"
    assert "font blew up" in r["pdf_error"]


def test_provider_failure_surfaces_as_a_useful_error(stub_cv):
    with patch("agents.ats_agent.compute_ats_score", side_effect=RuntimeError("ollama down")):
        with pytest.raises(Exception) as exc:
            api.debug_ats(api.AtsDebugRequest(job_description="Need Python"))
    assert "ollama down" in str(getattr(exc.value, "detail", exc.value))
