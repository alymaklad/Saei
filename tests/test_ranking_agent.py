"""
Ranking agent — the semantic retrieval path and the weighted match score.
All embedding calls are mocked; nothing here hits Ollama or Gemini.
"""
from unittest.mock import patch

import config
from agents import ranking_agent as ra


CV = """
Aly Tarek — Machine Learning Engineer
Experience 2019 - 2023
Built multimodal medical image understanding systems using PyTorch and Docker.
Skills: Python, PyTorch, Computer Vision, Docker, Kubernetes
Education: Bachelor of Science in Computer Engineering
"""


def _job(title="Software Engineer", description="We need Python and Docker.", **extra):
    return {"title": title, "description": description, **extra}


# ---- weights ----------------------------------------------------------------

def test_weights_sum_to_one_and_semantic_takes_its_stated_share():
    assert abs(sum(ra.WEIGHTS.values()) - 1.0) < 1e-9
    assert ra.WEIGHTS["semantic"] == ra.SEMANTIC_WEIGHT


def test_rule_factor_proportions_are_preserved_after_rescaling():
    """Rescaling to make room for the semantic factor must not change the
    seven rule-based factors RELATIVE to each other -- skills should still be
    8x education, 2x experience, and so on."""
    assert abs(ra.WEIGHTS["skills"] / ra.WEIGHTS["education"] - 8.0) < 1e-9
    assert abs(ra.WEIGHTS["skills"] / ra.WEIGHTS["experience"] - 2.0) < 1e-9
    assert abs(ra.WEIGHTS["title"] / ra.WEIGHTS["seniority"] - 3.0) < 1e-9


# ---- token/semantic split ---------------------------------------------------

def test_split_by_token_match_partitions_correctly():
    jobs = [_job(title="Software Engineer"), _job(title="Backend Developer")]
    matched, missed = ra.split_by_token_match(jobs, ["Software Engineer"])
    assert [j["title"] for j in matched] == ["Software Engineer"]
    assert [j["title"] for j in missed] == ["Backend Developer"]


def test_split_with_expanded_roles_catches_the_adjacent_title():
    """The whole point of expansion: 'Backend Developer' stops being a miss
    once it's in target_roles."""
    jobs = [_job(title="Backend Developer")]
    matched, missed = ra.split_by_token_match(jobs, ["Software Engineer", "Backend Developer"])
    assert len(matched) == 1 and not missed


# ---- semantic retrieval path ------------------------------------------------

def test_semantic_path_keeps_only_jobs_above_the_threshold(monkeypatch):
    monkeypatch.setattr(config, "CV_JOB_SIMILARITY_THRESHOLD", 0.5)
    jobs = [_job(title="Close"), _job(title="Far")]
    # identical vector -> similarity 1.0; orthogonal -> 0.0
    with patch.object(ra, "embed_texts", return_value=[[1.0, 0.0], [0.0, 1.0]]):
        kept = ra.semantic_retrieval_path(jobs, [1.0, 0.0])
    assert [j["title"] for j in kept] == ["Close"]
    assert kept[0]["semantic_score"] == 1.0


def test_semantic_path_attaches_the_score_for_reuse(monkeypatch):
    """score_job must not have to re-embed a job the retrieval path already
    embedded -- the score rides along on the job dict."""
    monkeypatch.setattr(config, "CV_JOB_SIMILARITY_THRESHOLD", 0.1)
    with patch.object(ra, "embed_texts", return_value=[[1.0, 0.0]]):
        kept = ra.semantic_retrieval_path([_job()], [1.0, 0.0])
    assert "semantic_score" in kept[0]


def test_semantic_path_is_a_noop_without_a_cv_embedding():
    assert ra.semantic_retrieval_path([_job()], []) == []
    assert ra.semantic_retrieval_path([], [1.0, 0.0]) == []


# ---- individual factors -----------------------------------------------------

def test_title_factor_scores_one_on_match_zero_on_miss():
    score, _ = ra.score_title(_job(title="Software Engineer"), ["Software Engineer"])
    assert score == 1.0
    score, _ = ra.score_title(_job(title="Chef"), ["Software Engineer"])
    assert score == 0.0


def test_missing_data_scores_neutral_not_zero():
    """Location and salary are absent from most of this project's sources.
    Scoring 'not stated' as 0 would punish jobs for how their source happens
    to be structured rather than for anything about the job itself."""
    location_score, note = ra.score_location(_job())
    assert location_score == ra.NEUTRAL
    assert "No location information" in note

    salary_score, note = ra.score_salary(_job())
    assert salary_score == ra.NEUTRAL
    assert "No salary information" in note


def test_remote_only_sources_score_location_full():
    score, _ = ra.score_location(_job(source="remoteok"))
    assert score == 1.0
    score, _ = ra.score_location(_job(source="weworkremotely"))
    assert score == 1.0


def test_location_reads_greenhouses_nested_location_object():
    score, note = ra.score_location(_job(location={"name": "Remote - EMEA"}))
    assert score == 1.0
    assert "Remote - EMEA" in note


def test_salary_factor_rewards_disclosure():
    score, _ = ra.score_salary(_job(description="Salary: $120,000 - $150,000 per year"))
    assert score > ra.NEUTRAL


def test_education_meets_requirement():
    score, _ = ra.score_education(_job(description="Requires a Bachelor degree"), CV)
    assert score == 1.0


def test_education_below_requirement_is_penalized_not_zeroed():
    score, _ = ra.score_education(_job(description="PhD required"), CV)
    assert 0 < score < 1.0


def test_education_unstated_is_neutral():
    score, note = ra.score_education(_job(description="Come work with us"), CV)
    assert score == ra.NEUTRAL
    assert "No specific education requirement" in note


def test_experience_reuses_ats_agent_extraction():
    score, note = ra.score_experience(_job(description="5+ years of experience required"), CV)
    # CV spans 2019-2023 = 4 years against a 5-year ask
    assert 0 < score < 1.0
    assert "5+ years" in note


def test_experience_unstated_is_neutral():
    score, _ = ra.score_experience(_job(description="No specific requirement"), CV)
    assert score == ra.NEUTRAL


def test_ranking_imports_no_llm_entry_point_at_all():
    """Ranking runs over every retrieved candidate -- hundreds per run -- so an
    LLM call per job isn't affordable: measured, 250 jobs consumed ~197k of
    Groq's 200k free-tier daily tokens. This asserts the dependency is gone,
    not merely unused, so it can't be reintroduced by accident."""
    assert not hasattr(ra, "extract_required_skills")
    assert not hasattr(ra, "get_llm")


def test_skills_factor_uses_passed_in_skills_when_a_caller_already_has_them():
    """A caller that already paid for LLM extraction (the orchestrator, via
    ats_agent) gets the stronger 'JD requirements vs CV' measure."""
    score, note, missing = ra.score_skills(_job(), CV, required_skills=["Python", "Docker", "Rust"])
    assert missing == ["Rust"]
    assert abs(score - 2 / 3) < 1e-9


def test_skills_factor_is_deterministic_without_passed_in_skills():
    """The default path: which of MY skills does this JD mention. No model."""
    job = _job(description="We need Python, Docker and Kubernetes experience.")
    score, note, _ = ra.score_skills(job, CV)
    assert score > 0
    assert "of your skill terms" in note


def test_deterministic_skills_scores_an_unrelated_posting_low():
    related = ra.score_skills(_job(description="Python, PyTorch, Docker, Kubernetes"), CV)[0]
    unrelated = ra.score_skills(_job(description="Baking, pastry decoration, kitchen hygiene"), CV)[0]
    assert related > unrelated


def test_extract_cv_skill_terms_prefers_an_explicit_skills_section():
    cv = "Aly Tarek\n\nSummary\nEngineer.\n\nSkills:\nPython, PyTorch, Docker\n\nEducation\nBSc"
    terms = ra.extract_cv_skill_terms(cv)
    assert "python" in terms and "pytorch" in terms and "docker" in terms


def test_extract_cv_skill_terms_drops_generic_filler():
    terms = ra.extract_cv_skill_terms("Skills:\nexperience, years, strong, Python")
    assert "python" in terms
    for filler in ("experience", "years", "strong"):
        assert filler not in terms


def test_skills_factor_is_neutral_when_the_posting_has_no_text():
    score, note, _ = ra.score_skills(_job(description=""), CV)
    assert score == ra.NEUTRAL


def test_seniority_factor_neutral_when_no_preference_set():
    score, _ = ra.score_seniority(_job(title="Senior Engineer"), "")
    assert score == ra.NEUTRAL


# ---- overall score ----------------------------------------------------------

def test_score_job_produces_a_full_breakdown_of_every_factor():
    job = dict(_job(title="Software Engineer"), semantic_score=0.9)
    result = ra.score_job(job, CV, target_roles=["Software Engineer"], required_skills=["Python"])
    assert set(result["breakdown"]) == set(ra.WEIGHTS)
    for detail in result["breakdown"].values():
        assert "score" in detail and "weight" in detail and "evidence" in detail
    assert 0.0 <= result["score"] <= 1.0


def test_score_job_reuses_an_existing_semantic_score_without_embedding():
    job = dict(_job(), semantic_score=0.8)
    with patch.object(ra, "embed_texts") as mock_embed:
        result = ra.score_job(job, CV, cv_embedding=[1.0, 0.0], required_skills=["Python"])
    mock_embed.assert_not_called()
    assert result["breakdown"]["semantic"]["score"] == 0.8


def test_score_job_without_embeddings_scores_semantic_neutral():
    """Embeddings depend on an external service the user may not have set up.
    Losing them must degrade the score, not crash the run."""
    result = ra.score_job(_job(), CV, cv_embedding=None, required_skills=["Python"])
    assert result["breakdown"]["semantic"]["score"] == ra.NEUTRAL
    assert "unavailable" in result["breakdown"]["semantic"]["evidence"]


def test_a_strong_job_outscores_a_weak_one():
    strong = dict(
        _job(title="Machine Learning Engineer",
             description="Python, PyTorch, Docker. Salary: $150,000. Bachelor degree.",
             source="remoteok"),
        semantic_score=0.95,
    )
    weak = dict(_job(title="Pastry Chef", description="Baking experience."), semantic_score=0.05)
    roles = ["Machine Learning Engineer"]
    strong_result = ra.score_job(strong, CV, target_roles=roles, required_skills=["Python", "PyTorch", "Docker"])
    weak_result = ra.score_job(weak, CV, target_roles=roles, required_skills=["Baking", "Pastry"])
    assert strong_result["score"] > weak_result["score"]


def test_rank_jobs_sorts_best_first_and_never_drops_anything():
    jobs = [
        dict(_job(title="Pastry Chef"), semantic_score=0.05),
        dict(_job(title="Software Engineer"), semantic_score=0.95),
    ]
    ranked = ra.rank_jobs(jobs, CV, target_roles=["Software Engineer"])
    assert len(ranked) == 2  # gating is the caller's job, not ranking's
    assert ranked[0]["title"] == "Software Engineer"
    assert ranked[0]["match_score"] >= ranked[1]["match_score"]


# ---- batched embedding during ranking ---------------------------------------
# score_job used to embed each job individually -- one API request per job,
# which is what exhausted Gemini's free-tier per-minute quota (100/min,
# counted per content) on a normal-sized run.

def test_rank_jobs_embeds_all_missing_jobs_in_one_batched_call():
    jobs = [_job(title=f"Role {i}") for i in range(25)]
    with patch.object(ra, "embed_texts", side_effect=lambda t: [[1.0, 0.0]] * len(t)) as mock_embed:
        ra.rank_jobs(jobs, CV, cv_embedding=[1.0, 0.0], target_roles=["Role"])
    assert mock_embed.call_count == 1                      # not 25
    assert len(mock_embed.call_args[0][0]) == 25


def test_jobs_already_scored_by_the_semantic_path_are_not_re_embedded():
    already = dict(_job(title="Recovered"), semantic_score=0.87)
    fresh = _job(title="Token Match")
    with patch.object(ra, "embed_texts", side_effect=lambda t: [[1.0, 0.0]] * len(t)) as mock_embed:
        ranked = ra.rank_jobs([already, fresh], CV, cv_embedding=[1.0, 0.0])
    assert len(mock_embed.call_args[0][0]) == 1            # only the un-scored one
    recovered = next(j for j in ranked if j["title"] == "Recovered")
    assert recovered["match_breakdown"]["semantic"]["score"] == 0.87


def test_attach_semantic_scores_is_a_noop_without_a_cv_embedding():
    jobs = [_job()]
    with patch.object(ra, "embed_texts") as mock_embed:
        assert ra.attach_semantic_scores(jobs, None) == jobs
    mock_embed.assert_not_called()


def test_embedding_failure_during_ranking_degrades_to_neutral(capsys):
    """An exhausted quota mid-ranking must cost the semantic factor, not the
    whole run."""
    jobs = [_job(title="Role")]
    with patch.object(ra, "embed_texts", side_effect=RuntimeError("429 RESOURCE_EXHAUSTED")):
        ranked = ra.rank_jobs(jobs, CV, cv_embedding=[1.0, 0.0], target_roles=["Role"])
    assert len(ranked) == 1
    assert ranked[0]["match_breakdown"]["semantic"]["score"] == ra.NEUTRAL


def test_build_match_explanation_mentions_every_factor():
    job = dict(_job(title="Software Engineer"), semantic_score=0.9)
    result = ra.score_job(job, CV, target_roles=["Software Engineer"], required_skills=["Python"])
    text = ra.build_match_explanation(result["score"], result["breakdown"])
    for label in ra.FACTOR_LABELS.values():
        assert label in text
