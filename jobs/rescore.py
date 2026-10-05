"""Re-score one stored application with the current scoring rules.

Scores are written once, when a job is processed, so a change to how scoring
works (weights, matchers, the soft-skill read) leaves every stored
application showing the old number. This recomputes it on demand: the master
CV's score, and the tailored CV's too when one was written.

The job's requirements are reused from the stored breakdown rather than
extracted again -- the posting hasn't changed, and that skips the larger of
the two model calls. Only an application without a usable stored breakdown
(e.g. one scored by the retired four-pillar engine) has them re-extracted.
"""
import json
import os

from db import get_session
from models import Application, Job, SkillGap
from cv_parser import find_default_cv, parse_cv

_REQUIREMENT_KEYS = ("name", "raw_text", "canonical", "category", "importance", "requirement_type",
                     "concept_family", "evidence_expected", "id", "years_required", "seniority")


def stored_requirements(breakdown) -> dict | None:
    """The extracted requirements behind a stored requirements-engine
    breakdown, in the shape compute_ats_score accepts -- or None."""
    if not isinstance(breakdown, dict):
        return None
    requirements = [
        {k: item[k] for k in _REQUIREMENT_KEYS if k in item}
        for bucket in breakdown.values() if isinstance(bucket, dict)
        for item in bucket.get("items") or []
        if isinstance(item, dict) and item.get("name") and item.get("category")
    ]
    if not requirements:
        return None
    experience = (breakdown.get("experience") or {}).get("detail") or {}
    return {"requirements": requirements,
            "years_experience_required": experience.get("years_required"),
            "seniority": experience.get("seniority_required")}


def rescore_application(application_id: int) -> dict:
    """Recomputes and stores the application's scores. Returns the before and
    after numbers. Raises LookupError for an unknown id, RuntimeError when
    there's no CV to score."""
    from agents.ats_agent import build_improvement_explanation, compute_ats_score
    import profile_store

    with get_session() as session:
        row = (session.query(Application, Job).join(Job, Application.job_id == Job.id)
               .filter(Application.id == application_id).first())
        if not row:
            raise LookupError("Application not found")
        application, job = row
        description = job.description or ""
        breakdown = json.loads(application.ats_breakdown) if application.ats_breakdown else None
        tailored_path = application.cv_version_path
        had_tailored = application.tailored_ats_score is not None
        before = {"ats_score": application.ats_score, "tailored_ats_score": application.tailored_ats_score}
        job_id = job.id

    master = find_default_cv("cv")
    if not master:
        raise RuntimeError("No CV found. Upload one on the CV page first.")
    try:
        profile = profile_store.load_profile_dict()
    except Exception:  # noqa: BLE001 -- scoring builds one from the text instead
        profile = None

    original = compute_ats_score(parse_cv(master), description,
                                 required_skills=stored_requirements(breakdown), profile=profile)

    tailored = explanation = None
    if had_tailored and tailored_path and os.path.exists(tailored_path):
        tailored_text = parse_cv(tailored_path)
        tailored = compute_ats_score(tailored_text, description,
                                     required_skills=original.get("requirements"),
                                     profile=original.get("cv_profile"),
                                     evidence_text=tailored_text)
        explanation = build_improvement_explanation(original, tailored)

    with get_session() as session:
        application = session.query(Application).filter(Application.id == application_id).one()
        application.ats_score = original.get("score")
        application.scoring_engine = original.get("engine")
        application.ats_breakdown = json.dumps(original["breakdown"]) if original.get("breakdown") else None
        application.ats_explanation = original.get("explanation")
        if tailored is not None:
            application.tailored_ats_score = tailored.get("score")
            application.tailored_ats_breakdown = json.dumps(tailored["breakdown"]) if tailored.get("breakdown") else None
            application.tailored_ats_explanation = explanation
        # The skill-gap record follows the new result: what's missing may have changed.
        session.query(SkillGap).filter(SkillGap.job_id == job_id).delete()
        if original.get("missing_skills"):
            session.add(SkillGap(job_id=job_id, missing_skills=json.dumps(original["missing_skills"])))

    return {
        "application_id": application_id,
        "before": before,
        "after": {"ats_score": original.get("score"),
                  "tailored_ats_score": tailored.get("score") if tailored is not None else before["tailored_ats_score"]},
        "tailored_rescored": tailored is not None,
        "requirements_reused": stored_requirements(breakdown) is not None,
    }
