"""
One record of the user: the upload fills it, everything else reads it.

Three behaviours are pinned here, and they are all consequences of the same
decision -- that the PROFILE, not the uploaded file's text, is what the app
knows about the candidate:

  * uploading a CV replaces the profile, so the two never drift apart;
  * scoring, and therefore the skill gap and the missing-skills list, is
    computed against that profile;
  * ATS_SCORE_MODE decides whether the user is shown the untailored score at
    all -- and in the live run, whether there is a baseline to gate on.
"""
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import api
import config
import db
import orchestrator
import profile_store
from agents import ats_agent
from models import Base


CV_TEXT = "Aly Maklad. Trained a CNN. Skills: Python, Docker."

PROFILE = {
    "contact": {"full_name": "Aly Maklad", "title": "AI Engineer", "email": "a@b.com",
                "phone": "", "location": "Cairo, Egypt", "linkedin": "", "github": ""},
    "summary": "AI engineer.",
    "experience": [{"title": "AI Research Intern", "organization": "Manipal",
                    "location": "Manipal, India", "start": "2025-07", "end": "2026-05",
                    "is_professional": True,
                    "bullets": ["Trained a 3D ResNet-50 CNN encoder.",
                                "Deployed and scaled the training on Kubernetes."]}],
    "projects": [], "education": [], "certifications": [],
    "skills_claimed": ["Python", "Docker", "Kubernetes"],
}

JOB = "AI Engineer: Deep Learning, Kubernetes, Python."
REQUIREMENTS = {"requirements": [
    {"name": n, "category": "technical_skill", "importance": "required"}
    for n in ["Deep Learning", "Kubernetes", "Python"]]}


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    return engine


@pytest.fixture
def no_llm_entailment(monkeypatch):
    monkeypatch.setattr(config, "REQUIREMENT_ENTAILMENT", False)


# ---- the upload fills the profile ---------------------------------------------

def test_uploading_a_cv_replaces_the_profile(temp_db, monkeypatch, tmp_path):
    """The upload IS the profile-creation step. A second upload replaces the
    record wholesale, because a new file is a new statement of what is true."""
    monkeypatch.setattr(api.cv_parser, "parse_cv", lambda path: CV_TEXT)

    with patch("agents.cv_profile.build_profile", return_value=PROFILE) as build:
        api._extract_and_store_profile(str(tmp_path / "current_cv.pdf"))

    # use_cache=False: the point of re-reading is that THIS file is now the
    # truth, and a cached parse would make the upload appear to do nothing.
    assert build.call_args.kwargs["use_cache"] is False
    stored = profile_store.load_profile_dict()
    assert stored["contact"]["full_name"] == "Aly Maklad"
    assert stored["experience"][0]["location"] == "Manipal, India"
    # Extraction is not a hand edit, so the re-extract dialog has nothing to
    # warn about afterwards.
    assert profile_store.load()["edited_sections"] == []


def test_a_failed_extraction_does_not_fail_the_upload(temp_db, monkeypatch):
    """The file is already on disk. A provider outage must leave the user with
    an uploaded CV and a clear next step, not a failed upload."""
    monkeypatch.setattr(api.cv_parser, "find_default_cv", lambda d: "cv/current_cv.pdf")
    monkeypatch.setattr(api.cv_parser, "parse_cv", lambda path: CV_TEXT)

    with patch("agents.cv_profile.build_profile", side_effect=RuntimeError("provider down")):
        with pytest.raises(RuntimeError):
            api._extract_and_store_profile("cv/current_cv.pdf")
    # Nothing was written, so the previous profile (none here) is intact.
    assert profile_store.load() is None


# ---- the gap is measured against the profile ----------------------------------

def test_the_skill_gap_is_measured_against_the_profile(no_llm_entailment):
    """Kubernetes is in the profile and nowhere in the CV file's text. Scoring
    the profile finds it; scoring the file would report a gap the user has
    already closed on the Profile page."""
    from_profile = ats_agent.compute_ats_score(
        CV_TEXT, JOB, required_skills=REQUIREMENTS, profile=PROFILE)
    assert "Kubernetes" not in from_profile["missing_skills"]

    thin = {"experience": [{"title": "AI Research Intern", "organization": "Manipal",
                            "start": "2025-07", "end": "2026-05", "is_professional": True,
                            "bullets": ["Trained a 3D ResNet-50 CNN encoder."]}],
            "projects": [], "education": [], "certifications": [],
            "skills_claimed": ["Python", "Docker"]}
    from_file = ats_agent.compute_ats_score(
        CV_TEXT, JOB, required_skills=REQUIREMENTS, profile=thin)
    assert "Kubernetes" in from_file["missing_skills"]
    assert from_profile["score"] > from_file["score"]


def test_the_orchestrator_scores_from_the_stored_profile(temp_db, monkeypatch):
    monkeypatch.setattr(profile_store, "load_profile_dict", lambda: PROFILE)
    with patch.object(orchestrator, "compute_ats_score", return_value={"score": 0.5}) as scorer:
        orchestrator.score_node({"job": {"description": JOB}, "cv_text": CV_TEXT})
    assert scorer.call_args.kwargs["profile"] is PROFILE


# ---- one number or two --------------------------------------------------------

def test_both_mode_only_tailors_below_the_threshold(monkeypatch):
    monkeypatch.setattr(config, "ATS_SCORE_MODE", "both")
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.7)
    assert orchestrator.route_on_score({"ats_result": {"score": 0.69}}) == "rewrite_cv"
    assert orchestrator.route_on_score({"ats_result": {"score": 0.71}}) == "decide_apply_path"


def test_tailored_only_mode_tailors_every_job(monkeypatch):
    """There is no baseline number left to gate on, so the gate goes. That is
    one rewrite per job -- the cost the setting's description names."""
    monkeypatch.setattr(config, "ATS_SCORE_MODE", "tailored_only")
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.7)
    assert orchestrator.route_on_score({"ats_result": {"score": 0.99}}) == "rewrite_cv"


def test_the_bench_reports_the_mode_it_ran_in(temp_db, monkeypatch, no_llm_entailment):
    """The bench renders one score or two off this field, so it has to say
    which mode produced the numbers it is returning."""
    monkeypatch.setattr(config, "ATS_SCORE_MODE", "tailored_only")
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.1)     # would skip the rewrite in "both"
    monkeypatch.setattr(api.cv_parser, "find_default_cv", lambda d: "cv/current_cv.pdf")
    monkeypatch.setattr(api.cv_parser, "parse_cv", lambda p: CV_TEXT)
    monkeypatch.setattr(api.profile_store, "load_profile_dict", lambda: PROFILE)

    scored = ats_agent.compute_ats_score(CV_TEXT, JOB, required_skills=REQUIREMENTS,
                                         profile=PROFILE)
    with patch("agents.ats_agent.compute_ats_score", return_value=scored), \
         patch("agents.cv_rewriter_agent.rewrite_cv", return_value="TAILORED"):
        result = api.debug_ats(api.AtsDebugRequest(job_description=JOB))

    assert result["score_mode"] == "tailored_only"
    assert result["profile_source"] == "stored profile"
    # Tailored even though the score is far above the threshold.
    assert result["rewrite"] is not None
    assert "tailored-only" in result["rewrite"]["triggered_by"]


# ---- searching reads the profile too ------------------------------------------
#
# The uploaded file's only job is to fill the profile. Search is the first stage
# of a run and the most expensive place to read a stale document: it decides
# which jobs are ever seen at all.

SEARCH_PROFILE = dict(PROFILE, education=[
    {"degree": "B.Sc.", "field": "Computer Science", "institution": "Ain Shams University",
     "location": "Cairo, Egypt", "start": "2022-09", "end": "2026-07"}])


def test_query_expansion_is_calibrated_to_the_profile(monkeypatch):
    """The roles searched for are expanded against the candidate's background.
    That background is the profile, not the file it was seeded from."""
    from agents import cv_profile, query_expansion_agent

    seen = {}

    def fake_generate(position, candidate_text="", max_roles=None):
        seen["text"] = candidate_text
        return [position]

    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION", True)
    monkeypatch.setattr(config, "QUERY_EXPANSION_CACHE", False)
    monkeypatch.setattr(query_expansion_agent, "generate_target_roles", fake_generate)

    query_expansion_agent.get_target_roles(
        "AI Engineer", candidate_text=cv_profile.profile_text(SEARCH_PROFILE))

    assert "Manipal" in seen["text"]           # from the profile's entries
    assert "Kubernetes" in seen["text"]        # from the profile's skills


def test_the_semantic_query_vector_moves_when_the_profile_changes():
    """The cache is keyed on the text's content, so correcting the profile
    re-embeds instead of silently reusing a vector built from the old one."""
    from agents import cv_profile, embeddings

    before = embeddings._cache_key(cv_profile.profile_text(SEARCH_PROFILE))
    edited = dict(SEARCH_PROFILE, skills_claimed=SEARCH_PROFILE["skills_claimed"] + ["Rust"])
    after = embeddings._cache_key(cv_profile.profile_text(edited))
    assert before != after


def test_profile_text_carries_every_section_a_search_would_want():
    from agents import cv_profile

    text = cv_profile.profile_text(SEARCH_PROFILE)
    assert "AI Research Intern" in text        # experience heading
    assert "Kubernetes" in text                # a bullet
    assert "Computer Science" in text          # education
    assert "Docker" in text                    # skills list


def test_the_education_factor_reads_the_profile_not_the_file():
    """Structured fields beat regexing the whole document: the degree sits in
    education[i]["degree"], with no prose around it to misread."""
    from agents import ranking_agent

    job = {"description": "Requires a Bachelor's degree in a technical field."}
    with_profile = ranking_agent.score_education(job, "", profile=SEARCH_PROFILE)
    assert with_profile[0] == 1.0

    # No profile, no degree recorded: reported rather than guessed at.
    without = ranking_agent.score_education(job, "", profile=None)
    assert without[0] < 1.0
    assert "profile" in without[1]


def test_a_run_resolves_the_profile_once_and_threads_it(monkeypatch):
    """Hundreds of jobs, one profile: loading it per job would be the same
    answer fetched over and over."""
    from jobs import daily_run

    calls = {"n": 0}

    def counting_load():
        calls["n"] += 1
        return SEARCH_PROFILE

    monkeypatch.setattr(profile_store, "load_profile_dict", counting_load)
    assert daily_run._candidate_profile() is SEARCH_PROFILE
    assert calls["n"] == 1
