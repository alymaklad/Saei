"""
The requirements scoring engine, and the guarantee that turning it on did not
disturb the live pipeline.

The LLM is mocked throughout. What is being tested is the arithmetic and the
matching rules -- which is the whole reason the final number is computed in
Python rather than asked for: an opaque 0.83 cannot be unit-tested at all.
"""
from unittest.mock import patch

import pytest

import config
from agents import ats_agent, cv_profile


REQS = {
    "requirements": [
        {"name": "Python", "category": "technical_skill", "importance": "required"},
        {"name": "PyTorch", "category": "technical_skill", "importance": "required"},
        {"name": "Deep Learning", "category": "domain_knowledge", "importance": "required"},
        {"name": "AWS", "category": "tool", "importance": "required"},
        {"name": "Kubernetes", "category": "tool", "importance": "preferred"},
        {"name": "Docker", "category": "tool", "importance": "preferred"},
        {"name": "Bachelor's degree", "category": "education", "importance": "required"},
    ],
    "years_experience_required": 3,
    "seniority": "mid",
}

PROFILE = {
    "experience": [{
        "title": "AI Research Intern", "organization": "Acme",
        "start": "2024-07", "end": "2025-08", "is_professional": True,
        "bullets": [
            "Trained ResNet50 CNN models for CT medical image classification using PyTorch",
            "Containerized services with Docker and deployed them to Google Cloud",
        ],
    }],
    "projects": [{"name": "RAG chatbot", "start": "2025-01", "end": "2025-03",
                  "bullets": ["Built a retrieval pipeline in Python with LangChain"]}],
    "education": [{"degree": "Bachelor's degree", "field": "Computer Engineering",
                   "institution": "Cairo University", "start": "2020-09", "end": "2025-06"}],
    "certifications": [], "skills_claimed": ["Python", "SQL", "FastAPI"],
    "seniority_self_described": "entry", "_cached": False,
}


@pytest.fixture
def scored():
    with patch.object(ats_agent, "extract_requirements",
                      return_value=ats_agent._normalize_requirements(REQS)), \
         patch.object(ats_agent.cv_profile, "build_profile", return_value=PROFILE), \
         patch.object(ats_agent, "_entailment_pass", return_value={}):
        return ats_agent.compute_requirements_score("raw cv text", "jd text")


def _row(result, name):
    return next(r for r in result["requirement_results"] if r["name"] == name)


# ---- the AWS/GCP case, end to end -------------------------------------------

def test_sibling_cloud_provider_does_not_satisfy_the_requirement(scored):
    """The CV says "deployed them to Google Cloud". The posting requires AWS.
    A similarity-based matcher scores these as near-identical; this one must
    not award the points."""
    assert _row(scored, "AWS")["relation"] == "none"
    assert "AWS" in scored["missing_skills"]


def test_specific_evidence_supports_the_general_requirement():
    """"Trained ResNet50 CNN models" SUPPORTS a Deep Learning requirement
    without being equivalent to it, so it earns partial credit and says so.

    Scored on its own profile because the fixture above ALSO says PyTorch,
    which is a prerequisite for the same requirement and outranks a subset --
    see the test below. Here the CNN bullet is the only evidence there is."""
    reqs = {"requirements": [{"name": "Deep Learning", "category": "domain_knowledge",
                              "importance": "required"}],
            "years_experience_required": 1, "seniority": "entry"}
    profile = dict(PROFILE, projects=[], skills_claimed=[], experience=[{
        "title": "AI Research Intern", "organization": "Acme",
        "start": "2024-07", "end": "2025-08", "is_professional": True,
        "bullets": ["Trained ResNet50 CNN models for CT medical image classification"],
    }])
    with patch.object(ats_agent, "extract_requirements",
                      return_value=ats_agent._normalize_requirements(reqs)), \
         patch.object(ats_agent.cv_profile, "build_profile", return_value=profile), \
         patch.object(ats_agent, "_entailment_pass", return_value={}):
        result = ats_agent.compute_requirements_score("raw cv text", "jd text")

    row = _row(result, "Deep Learning")
    assert row["relation"] == "subset"
    assert row["relation_label"] == "Subset"
    assert row["credit"] == config.match_credit("subset", "demonstrated")
    assert "CNN" in row["evidence"] or "cnn" in row["evidence"].lower()


def test_the_strongest_available_relation_is_the_one_reported(scored):
    """The fixture CV evidences Deep Learning twice over: CNN is a KIND of it
    (0.80) and PyTorch cannot be used without it (0.90). Both are true, so the
    row reports the one worth more -- a requirement is not penalised for the
    order the passes happen to run in."""
    row = _row(scored, "Deep Learning")
    assert row["relation"] == "implied"
    assert row["credit"] == config.match_credit("implied", "demonstrated")
    assert config.RELATION_CREDIT["implied"] > config.RELATION_CREDIT["subset"]


# ---- demonstrated vs merely listed ------------------------------------------

def test_skill_used_in_a_role_outranks_a_skill_only_listed(scored):
    """PyTorch appears in a dated project bullet; SQL appears only in the
    skills list. Scoring them identically -- which a keyword count does --
    rewards a CV that simply lists every term in the posting."""
    row = _row(scored, "PyTorch")
    assert row["relation"] == "exact"
    assert row["evidence_location"] == "demonstrated"
    assert config.EVIDENCE_MULTIPLIER["demonstrated"] > config.EVIDENCE_MULTIPLIER["claimed"]


def test_relation_and_location_are_independent_axes():
    """The reason for two fields rather than one enum: an exact term buried in
    a skills list is weaker than a subset term inside a dated role, and a flat
    scale needs a row per combination to say so."""
    assert config.match_credit("exact", "demonstrated") == 1.0
    assert config.match_credit("alias", "demonstrated") == 1.0, (
        "a true synonym is interchangeable, so it must not be discounted")
    assert config.match_credit("none", "demonstrated") == 0.0


def test_demonstrated_related_work_beats_a_bare_listed_keyword():
    """The ordering the demonstrated/claimed split exists to encode.

    Caught by a real example: "SQL" in a skills list scored 0.75 while
    SQLAlchemy work inside a dated project scored 0.70, so naming a term
    without context beat doing something that is a kind of it. If this
    assertion ever fails, the credit table has drifted back into rewarding
    keyword stuffing over described work."""
    listed_exact = config.match_credit("exact", "claimed")
    demonstrated_subset = config.match_credit("subset", "demonstrated")
    assert demonstrated_subset > listed_exact, (
        f"subset+demonstrated ({demonstrated_subset}) must beat "
        f"exact+listed ({listed_exact})")


def test_the_credit_ordering_is_the_documented_one():
    ordered = [
        ("exact", "demonstrated"),
        ("subset", "demonstrated"),
        ("semantic_support", "demonstrated"),
        ("exact", "claimed"),
        ("subset", "claimed"),
        ("none", "demonstrated"),
    ]
    credits = [config.match_credit(r, l) for r, l in ordered]
    assert credits == sorted(credits, reverse=True), dict(zip(ordered, credits))


def test_evidence_is_attached_to_every_awarded_point(scored):
    for row in scored["requirement_results"]:
        if row["relation"] != "none":
            assert row["evidence"], f"{row['name']} scored without evidence"


# ---- weighting ---------------------------------------------------------------

def test_required_and_preferred_are_not_weighted_the_same(scored):
    required = scored["breakdown"]["required_skills"]["weight"]
    preferred = scored["breakdown"]["preferred_skills"]["weight"]
    assert required > preferred


def test_every_requirement_lands_in_exactly_one_bucket(scored):
    """Weighting 'required requirements' and 'relevant technical skills'
    separately would count every required technical skill twice and make the
    published weights mean something other than what they say."""
    counted = sum(len(b["items"]) for b in scored["breakdown"].values())
    non_experience = [r for r in scored["requirement_results"]
                      if r["category"] != "experience"]
    assert counted == len(non_experience)


def test_weights_of_active_buckets_sum_to_one(scored):
    total = sum(b["weight"] for b in scored["breakdown"].values())
    assert abs(total - 1.0) < 1e-6


def test_absent_bucket_redistributes_instead_of_capping_the_score():
    """A posting that states no education requirement must not cap the
    candidate at 90%. Same objection as 'if the job doesn't ask for a summary,
    why does missing one lower the match' -- generalised so it cannot recur
    bucket by bucket."""
    reqs = {"requirements": [
        {"name": "Python", "category": "technical_skill", "importance": "required"}],
        "years_experience_required": None, "seniority": None}
    with patch.object(ats_agent, "extract_requirements",
                      return_value=ats_agent._normalize_requirements(reqs)), \
         patch.object(ats_agent.cv_profile, "build_profile", return_value=PROFILE), \
         patch.object(ats_agent, "_entailment_pass", return_value={}):
        result = ats_agent.compute_requirements_score("cv", "jd")

    assert set(result["breakdown"]) == {"required_skills"}
    assert result["breakdown"]["required_skills"]["weight"] == 1.0
    assert result["score"] == 1.0
    assert "education" in result["inactive_buckets"]


# ---- experience -------------------------------------------------------------

def test_experience_comes_from_merged_dates_not_the_year_span(scored):
    """The legacy estimator was max(year) - min(year) over every 4-digit
    number in the CV. On this fixture that spans 2020-2025 and would report
    5 years -- clearing the posting's 3-year bar outright -- for a candidate
    whose only professional entry is a 14-month internship."""
    assert scored["cv_years"] == pytest.approx(1.2, abs=0.15)
    assert scored["breakdown"]["experience"]["score"] < 1.0


def test_concurrent_roles_are_not_double_counted():
    profile = {"experience": [
        {"title": "A", "start": "2024-01", "end": "2024-12", "is_professional": True},
        {"title": "B", "start": "2024-06", "end": "2024-12", "is_professional": True},
    ]}
    assert cv_profile.professional_months(profile) == 12


def test_undated_entries_contribute_nothing_rather_than_a_guess():
    profile = {"experience": [{"title": "A", "start": None, "end": None,
                               "is_professional": True}]}
    assert cv_profile.professional_months(profile) == 0


def test_education_only_dates_are_excluded_from_professional_experience():
    profile = {"experience": [
        {"title": "Coursework", "start": "2019-09", "end": "2023-06",
         "is_professional": False},
        {"title": "Intern", "start": "2024-07", "end": "2025-08",
         "is_professional": True},
    ]}
    assert cv_profile.professional_months(profile) == 14


# ---- long phrases ------------------------------------------------------------

def test_sentence_length_requirements_skip_token_matching():
    """The database holds a 105-character "skill" reading "Bachelor's degree in
    Computer Science, Machine Learning, Mathematics, Physics, Statistics or
    related field". Substring matching can never satisfy it, so it was counted
    missing on every job forever and then handed to the rewriter as something
    to add. It has to reach the entailment pass instead."""
    long_name = ("Bachelor's degree in Computer Science, Machine Learning, "
                 "Mathematics, Physics, Statistics or related field")
    req = {"name": long_name, "canonical": long_name.lower(),
           "category": "education", "importance": "required"}
    outcome = ats_agent.match_requirement(req, [{"text": long_name, "source": "education",
                                                 "demonstrated": True}])
    assert outcome["match"] == "missing"
    assert "too long" in (outcome["via"] or "")


# ---- extraction hygiene ------------------------------------------------------

def test_duplicate_requirements_collapse_on_canonical_form():
    """Measured requirement counts run 11 to 66 per posting. Under a coverage
    ratio that alone swings the score: 9 of 26 scores 0.35, the same 9 of 66
    scores 0.14."""
    parsed = {"requirements": [
        {"name": "Machine Learning", "category": "domain_knowledge", "importance": "required"},
        {"name": "ML", "category": "domain_knowledge", "importance": "required"},
        {"name": "machine learning", "category": "domain_knowledge", "importance": "required"},
    ]}
    assert len(ats_agent._normalize_requirements(parsed)["requirements"]) == 1


def test_malformed_extraction_degrades_instead_of_crashing():
    out = ats_agent._normalize_requirements({"requirements": [
        "Python", {"name": ""}, {"nonsense": 1}, None,
        {"name": "Rust", "category": "invented", "importance": "sort-of"},
    ]})
    names = [r["name"] for r in out["requirements"]]
    assert names == ["Python", "Rust"]
    assert out["requirements"][1]["category"] == "technical_skill"
    assert out["requirements"][1]["importance"] == "required"


# ---- entailment guardrails ---------------------------------------------------

def test_entailment_verdict_citing_a_sibling_is_discarded():
    """The prompt forbids it, but the guard runs after the model regardless --
    the AWS/GCP line has to hold no matter how persuasive the model was."""
    assert ats_agent._cites_a_sibling("AWS", "Deployed services to Google Cloud") is True
    assert ats_agent._cites_a_sibling("AWS", "Deployed services to AWS Lambda") is False


def test_entailment_evidence_must_actually_appear_in_the_cv():
    """An invented quote has to be thrown away rather than displayed as proof.
    The entire claim of this engine is that scores are evidence-backed."""
    spans = [{"text": "Trained ResNet50 models using PyTorch"}]
    assert ats_agent._evidence_is_real("Trained ResNet50 models using PyTorch", spans)
    assert not ats_agent._evidence_is_real("Led a team of twelve AWS architects", spans)


def test_entailment_is_one_batched_call_not_one_per_requirement():
    """Measured at ~31 requirements per job; per-requirement calls across 250
    jobs would be ~7,900, and this project has already exhausted Groq's
    200k tokens/day at one call per job."""
    unmatched = [{"name": f"Skill {i}"} for i in range(30)]
    spans = [{"text": "Did some work", "demonstrated": True}]

    class FakeLLM:
        def __init__(self): self.calls = 0
        def invoke(self, _messages):
            self.calls += 1
            return type("R", (), {"content": "[]"})()

    fake = FakeLLM()
    with patch.object(ats_agent, "get_llm", return_value=fake):
        ats_agent._entailment_pass(unmatched, spans)
    assert fake.calls == 1


def test_entailment_is_skipped_entirely_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "REQUIREMENT_ENTAILMENT", False)
    with patch.object(ats_agent, "get_llm") as mock_llm:
        assert ats_agent._entailment_pass([{"name": "X"}], [{"text": "y"}]) == {}
    mock_llm.assert_not_called()


# ---- which engine the live pipeline runs -------------------------------------


# ---- ATS compatibility is job-independent ------------------------------------

def test_compatibility_does_not_depend_on_any_job():
    """The defect that started this: formatting_score(cv_text) and
    section_completeness_score(cv_text) never received the job description, so
    0.356 of every score was a CV-level constant carrying 40% of the weight
    and no ranking signal. It is now reported on its own, where being constant
    per CV is correct rather than a bug."""
    import inspect
    params = inspect.signature(ats_agent.compute_ats_compatibility).parameters
    assert list(params) == ["cv_text"]


def test_compatibility_reports_a_status_not_just_a_number():
    cv = ("Aly Tarek\nalytarek@example.com\n+20 100 000 0000\n\nSummary\n"
          "Engineer.\n\nExperience\n- Did a thing 2024\n- Did another 2025\n"
          "- And a third\n\nEducation\nBSc Computer Engineering, 2025\n\nSkills\n"
          "Python, PyTorch\n") + ("filler word " * 200)
    result = ats_agent.compute_ats_compatibility(cv)
    assert result["status"] in ("Pass", "Warning", "Fail")
    assert set(result["checks"]) == {"formatting", "sections", "contact", "text_extraction"}


def test_compatibility_flags_missing_contact_details():
    result = ats_agent.compute_ats_compatibility("Just some text with no way to reach me.")
    assert "no email address found" in result["issues"]
    assert result["status"] == "Fail"


# ---- a weak match must not block a stronger judgement ------------------------

def test_a_claimed_only_row_is_still_offered_for_judgement():
    """Found on a real tailored CV. Its summary said "full-stack development",
    the deterministic pass matched that at exact x claimed (0.65), the row
    stopped counting as "missing", and the entailment pass -- which had scored
    it 0.75 from a dated role the run before -- was never asked. A keyword in
    a summary outranked the job it describes, and the tailored CV lost points
    against the original for saying MORE."""
    reqs = {"requirements": [
        {"name": "Full-stack development", "category": "technical_skill",
         "importance": "required"}],
        "years_experience_required": 1, "seniority": "entry"}
    profile = {
        "experience": [{"title": "Backend Engineer", "organization": "Acme",
                        "start": "2024-01", "end": "2025-01", "is_professional": True,
                        "bullets": ["Owned the checkout service end to end."]}],
        "projects": [], "education": [], "certifications": [],
        "skills_claimed": ["Full-stack development"], "_cached": True,
    }

    def verdict(unmatched, spans, decisions=None):
        assert any(r["name"] == "Full-stack development" for r in unmatched), (
            "a claimed-only row must reach the adjudication call")
        return {"full-stack development": {
            "match": "semantic_support", "relation": "semantic_support",
            "evidence_location": "demonstrated",
            "relation_label": "Semantic support", "evidence_label": "Demonstrated",
            "credit": config.match_credit("semantic_support", "demonstrated"),
            "matched_form": None, "evidence": "Owned the checkout service end to end.",
            "evidence_source": "llm_entailment", "via": "judged"}}

    with patch.object(ats_agent, "extract_requirements",
                      return_value=ats_agent._normalize_requirements(reqs)), \
         patch.object(ats_agent.cv_profile, "build_profile", return_value=profile), \
         patch.object(ats_agent, "_entailment_pass", side_effect=verdict):
        result = ats_agent.compute_requirements_score("cv", "jd")

    row = _row(result, "Full-stack development")
    assert row["evidence_location"] == "demonstrated"
    assert row["credit"] == config.match_credit("semantic_support", "demonstrated")


def test_a_verdict_never_overwrites_a_stronger_table_match():
    """The other direction, and the reason the upgrade is a credit comparison
    rather than a blanket overwrite: a model opinion must never displace a
    curated relation. semantic_support is capped below every one of them."""
    reqs = {"requirements": [
        {"name": "PyTorch", "category": "technical_skill", "importance": "required"}],
        "years_experience_required": 1, "seniority": "entry"}
    profile = {
        "experience": [{"title": "ML Engineer", "organization": "Acme",
                        "start": "2024-01", "end": "2025-01", "is_professional": True,
                        "bullets": ["Trained the classifier in PyTorch."]}],
        "projects": [], "education": [], "certifications": [],
        "skills_claimed": [], "_cached": True,
    }

    def verdict(unmatched, spans, decisions=None):
        return {"pytorch": {
            "match": "semantic_support", "relation": "semantic_support",
            "evidence_location": "demonstrated", "relation_label": "Semantic support",
            "evidence_label": "Demonstrated",
            "credit": config.match_credit("semantic_support", "demonstrated"),
            "matched_form": None, "evidence": "Trained the classifier in PyTorch.",
            "evidence_source": "llm_entailment", "via": "judged"}}

    with patch.object(ats_agent, "extract_requirements",
                      return_value=ats_agent._normalize_requirements(reqs)), \
         patch.object(ats_agent.cv_profile, "build_profile", return_value=profile), \
         patch.object(ats_agent, "_entailment_pass", side_effect=verdict):
        result = ats_agent.compute_requirements_score("cv", "jd")

    row = _row(result, "PyTorch")
    assert row["relation"] == "exact", "the table's match must survive"
    assert row["credit"] == 1.0
