"""Re-scoring a stored application with the current rules (jobs/rescore.py)."""
import json
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db
from jobs import rescore
from models import Application, Base, Job, SkillGap

BREAKDOWN = {
    "required_skills": {"items": [
        {"name": "Python", "canonical": "python", "category": "technical_skill", "importance": "required",
         "requirement_type": "skill", "relation": "alias", "credit": 1.0, "points": 10}]},
    "experience": {"items": [], "detail": {"years_required": 2, "seniority_required": "mid"}},
    "soft_skills": {"items": [
        {"name": "Attention to detail", "category": "soft_skill", "importance": "required",
         "requirement_type": "capability", "relation": "none", "credit": 0.0, "points": 2}]},
}


def test_stored_requirements_rebuild_the_extraction():
    got = rescore.stored_requirements(BREAKDOWN)
    assert [r["name"] for r in got["requirements"]] == ["Python", "Attention to detail"]
    assert "relation" not in got["requirements"][0]  # match results are not requirements
    assert (got["years_experience_required"], got["seniority"]) == (2, "mid")


@pytest.mark.parametrize("value", [None, {}, {"keyword_match": {"score": 0.4}}])
def test_no_usable_breakdown_means_extract_again(value):
    assert rescore.stored_requirements(value) is None


@pytest.fixture()
def temp_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'r.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    original = db.engine, db.SessionLocal
    db.engine, db.SessionLocal = engine, Session
    try:
        yield tmp_path
    finally:
        db.engine, db.SessionLocal = original
        engine.dispose()


def _seed(tmp_path, tailored_file=True):
    cv = tmp_path / "cv_1.pdf"
    if tailored_file:
        cv.write_text("tailored")
    with db.get_session() as s:
        job = Job(url="https://x/1", title="ML Engineer", company="Acme", description="jd")
        s.add(job); s.flush()
        app = Application(job_id=job.id, ats_score=0.74, ats_breakdown=json.dumps(BREAKDOWN),
                          tailored_ats_score=0.70, cv_version_path=str(cv), status="pending_review")
        s.add(app); s.add(SkillGap(job_id=job.id, missing_skills=json.dumps(["old gap"])))
        s.flush()
        return app.id, job.id


def _result(score, missing=()):
    return {"score": score, "engine": "requirements", "breakdown": {"required_skills": {"score": score}},
            "explanation": f"{score}", "missing_skills": list(missing),
            "requirements": {"requirements": []}, "cv_profile": {"p": 1}}


def test_rescore_updates_both_scores_and_reuses_the_requirements(temp_db):
    app_id, job_id = _seed(temp_db)
    calls = []

    def fake_score(text, jd, required_skills=None, profile=None, evidence_text=None):
        calls.append({"text": text, "reqs": required_skills, "evidence_text": evidence_text})
        return _result(0.95, ["Attention to detail"]) if evidence_text is None else _result(0.97)

    with patch("agents.ats_agent.compute_ats_score", side_effect=fake_score), \
         patch("agents.ats_agent.build_improvement_explanation", return_value="improved"), \
         patch("jobs.rescore.find_default_cv", return_value="cv/master.pdf"), \
         patch("jobs.rescore.parse_cv", side_effect=lambda p: f"text of {p}"), \
         patch("profile_store.load_profile_dict", return_value={"contact": {}}):
        out = rescore.rescore_application(app_id)

    assert out["before"] == {"ats_score": 0.74, "tailored_ats_score": 0.70}
    assert out["after"] == {"ats_score": 0.95, "tailored_ats_score": 0.97}
    assert out["tailored_rescored"] and out["requirements_reused"]
    assert [r["name"] for r in calls[0]["reqs"]["requirements"]] == ["Python", "Attention to detail"]
    assert calls[1]["evidence_text"] == calls[1]["text"]  # the tailored CV's own text is the evidence
    with db.get_session() as s:
        app = s.query(Application).one()
        gaps = [json.loads(g.missing_skills) for g in s.query(SkillGap).filter(SkillGap.job_id == job_id)]
    assert (app.ats_score, app.tailored_ats_score, app.tailored_ats_explanation) == (0.95, 0.97, "improved")
    assert gaps == [["Attention to detail"]]  # the old gap record is replaced


def test_missing_tailored_file_rescores_only_the_master(temp_db):
    app_id, _ = _seed(temp_db, tailored_file=False)
    with patch("agents.ats_agent.compute_ats_score", return_value=_result(0.9)) as score, \
         patch("jobs.rescore.find_default_cv", return_value="cv/master.pdf"), \
         patch("jobs.rescore.parse_cv", return_value="cv text"), \
         patch("profile_store.load_profile_dict", return_value={}):
        out = rescore.rescore_application(app_id)
    assert score.call_count == 1 and not out["tailored_rescored"]
    assert out["after"]["tailored_ats_score"] == 0.70  # kept, not wiped


def test_unknown_application(temp_db):
    with pytest.raises(LookupError):
        rescore.rescore_application(999)
