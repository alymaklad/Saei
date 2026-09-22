"""
The prerequisite relation: skills the work proves without naming.

A CV that says "built a FastAPI service serving a PyTorch model" has proved
Python whether or not the word appears anywhere in it, and one that says
"queried PostgreSQL" has proved SQL. Before this relation existed, both of
those skills could only earn credit from the skills LIST -- 0.65, the same
score a CV earns by pasting the job description into a keyword row -- while
the dated bullet that actually demonstrates them earned nothing.

What is being tested here is mostly the guard rails. A table that says "X
implies Y" is one bad row away from awarding Kubernetes to anyone who has run
a container, so the direction of every relation, and the sibling exclusions,
matter more than the happy path.
"""
from unittest.mock import patch

import pytest

import config
from agents import ats_agent, cv_profile, cv_targeting, skill_matching


REQS = {
    "requirements": [
        {"name": "Python", "category": "technical_skill", "importance": "required"},
        {"name": "SQL", "category": "technical_skill", "importance": "required"},
        {"name": "Kubernetes", "category": "tool", "importance": "preferred"},
    ],
    "years_experience_required": 2,
    "seniority": "mid",
}

# Python and Kubernetes are LISTED and never used. The bullets describe work
# that cannot have happened without Python, and work that cannot have happened
# without SQL -- and nothing that implies Kubernetes.
PROFILE = {
    "experience": [{
        "title": "ML Intern", "organization": "Acme",
        "start": "2024-07", "end": "2025-03", "is_professional": True,
        "bullets": [
            "Built a FastAPI service serving a PyTorch model",
            "Queried PostgreSQL to assemble the training features",
        ],
    }],
    "projects": [{"name": "Resume agent", "start": "2025-01", "end": "2025-04",
                  "bullets": ["Containerised the app with Docker"]}],
    "education": [], "certifications": [],
    "skills_claimed": ["Python", "Kubernetes", "Tableau"],
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


# ---- the table's direction ---------------------------------------------------

@pytest.mark.parametrize("requirement, evidence", [
    ("Python", "FastAPI"),
    ("Python", "PyTorch"),
    ("Python", "pandas"),
    ("SQL", "PostgreSQL"),
    ("SQL", "BigQuery"),
    ("JavaScript", "React"),
    ("AWS", "S3"),
    ("Deep Learning", "PyTorch"),
])
def test_a_tool_implies_what_it_is_built_on(requirement, evidence):
    assert skill_matching.implied_by(requirement, evidence)


@pytest.mark.parametrize("requirement, evidence, why", [
    ("FastAPI", "Python", "the relation is one-way: Python does not imply every framework"),
    ("Docker", "Kubernetes", "orchestrating containers is not the same skill as building them"),
    ("Kubernetes", "Docker", "and the reverse is the claim this whole feature must never make"),
    ("PyTorch", "TensorFlow", "siblings, not prerequisites"),
    ("Python", "Java", "unrelated languages"),
    ("Machine Learning", "pandas", "a dataframe is not a model"),
    ("REST API", "FastAPI", "would let any web work claim API design"),
])
def test_the_relation_is_not_symmetric_and_not_between_siblings(requirement, evidence, why):
    assert not skill_matching.implied_by(requirement, evidence), why


def test_implying_terms_never_offers_a_mutually_exclusive_sibling():
    for requirement in skill_matching.IMPLIED_BY:
        for term in skill_matching.implying_terms(requirement):
            assert not skill_matching.are_mutually_exclusive(requirement, term), (
                f"{requirement} <- {term}")


# ---- what it is worth --------------------------------------------------------

def test_work_that_implies_a_skill_beats_naming_it_in_a_list():
    """The calibration this feature exists for. Python in a keyword row is an
    assertion; a FastAPI bullet in a dated role is a fact about work that
    happened, and the second must score higher than the first."""
    implied_work = config.match_credit("implied", "demonstrated")
    listed_exact = config.match_credit("exact", "claimed")
    assert implied_work > listed_exact, (
        f"implied+demonstrated ({implied_work}) must beat exact+listed ({listed_exact})")


def test_a_prerequisite_is_worth_less_than_saying_it_and_more_than_a_subset():
    """Placed deliberately between the two: naming the skill outright is still
    better evidence than inferring it, but inferring "Python" from FastAPI is
    surer than inferring "Deep Learning" from CNN, because the inference runs
    on a curated table rather than on category membership."""
    assert (config.RELATION_CREDIT["alias"]
            > config.RELATION_CREDIT["implied"]
            > config.RELATION_CREDIT["subset"])


def test_the_credit_ordering_still_reads_top_to_bottom():
    ordered = [
        ("exact", "demonstrated"),
        ("implied", "demonstrated"),
        ("subset", "demonstrated"),
        ("exact", "claimed"),
        ("implied", "claimed"),
        ("none", "demonstrated"),
    ]
    credits = [config.match_credit(r, l) for r, l in ordered]
    assert credits == sorted(credits, reverse=True), dict(zip(ordered, credits))


# ---- end to end through the scorer -------------------------------------------

def test_a_listed_skill_is_promoted_by_the_work_that_used_it(scored):
    row = _row(scored, "Python")
    assert row["relation"] == "implied"
    assert row["evidence_location"] == "demonstrated"
    assert row["credit"] == config.match_credit("implied", "demonstrated")
    assert "fastapi" in row["evidence"].lower() or "pytorch" in row["evidence"].lower()


def test_a_skill_never_listed_at_all_is_still_earned_by_the_work(scored):
    """SQL is nowhere in this profile -- not in the skills row, not in a
    bullet. "Queried PostgreSQL" is the entire case for it, and it is enough."""
    row = _row(scored, "SQL")
    assert row["relation"] == "implied"
    assert "postgresql" in row["evidence"].lower()
    assert "SQL" not in scored["missing_skills"]


def test_the_inference_does_not_leak_into_unrelated_tools(scored):
    """Docker in a project bullet must not carry Kubernetes with it, however
    often the two appear in the same job posting."""
    row = _row(scored, "Kubernetes")
    assert row["relation"] in ("none", "exact")
    if row["relation"] == "exact":
        assert row["evidence_location"] == "claimed", (
            "Kubernetes is listed and never used; it cannot be demonstrated")


def test_the_relation_is_labelled_for_the_user(scored):
    assert _row(scored, "Python")["relation_label"] == "Prerequisite"


# ---- reading the skills back out of the work ---------------------------------

def test_demonstrated_skills_separates_used_from_named():
    rows = {r["skill"]: r for r in cv_profile.demonstrated_skills(PROFILE)}
    assert rows["python"]["relation"] == "implied"
    assert rows["python"]["via"] == "fastapi"
    assert rows["python"]["source"] == "experience"
    assert rows["sql"]["relation"] == "implied"
    assert rows["docker"]["relation"] == "direct"
    assert "tableau" not in rows, "listed and never used, so nothing demonstrates it"
    assert "kubernetes" not in rows


def test_demonstrated_skills_cites_the_line_it_read():
    for row in cv_profile.demonstrated_skills(PROFILE):
        assert row["text"], f"{row['skill']} claimed without a source line"
        assert skill_matching.find_term(row["via"], row["text"])


# ---- what tailoring does with it ---------------------------------------------

def test_the_rewrite_is_told_to_name_the_language_in_the_bullet():
    rows = [{"name": "Python", "importance": "required",
             "relation": "implied", "credit": config.match_credit("implied", "demonstrated")}]
    targets = cv_targeting.build_targets(PROFILE, requirement_results=rows)
    assert targets, "a bullet that implies Python is a place Python can be written"
    target = targets[0]
    assert target["kind"] == "bridge"
    assert target["relation"] == "prerequisite"
    assert target["section"] == "experience"

    prompt = cv_targeting.format_targets(targets)
    assert "a kind of" not in prompt, (
        "FastAPI is not a kind of Python; the instruction has to say why it counts")
    assert "Python" in prompt and "fastapi" in prompt.lower()


def test_listed_only_stops_nagging_about_a_skill_the_work_implies():
    """The report tells the user which skills have nothing behind them, so
    they can add the role that used one. Python has a role behind it now."""
    advice = cv_targeting.listed_only(PROFILE, [
        {"name": "Python", "evidence_location": "claimed"},
        {"name": "Kubernetes", "evidence_location": "claimed"},
        {"name": "Tableau", "evidence_location": "claimed"},
    ])
    assert "Python" not in advice
    assert {"Kubernetes", "Tableau"} == set(advice)


def test_a_bridge_is_never_proposed_for_a_sibling():
    """supporting_terms feeds the rewrite prompt. Anything it returns will be
    written into a CV as a claim, so it must return only what the scorer would
    accept as evidence for the requirement."""
    for requirement in ("Kubernetes", "Docker", "PyTorch", "AWS"):
        for term in cv_targeting.supporting_terms(requirement):
            assert skill_matching.entails(requirement, term) or \
                   skill_matching.implied_by(requirement, term), (
                f"{requirement} <- {term} is neither a kind of it nor a prerequisite")


# ---- the badge that said "undefined" -----------------------------------------

def test_every_relation_has_a_label_to_render():
    """Shipped `implied` without adding it to the UI's own label map, and the
    requirement table rendered a badge reading "undefined" next to a perfectly
    correct 9/10. The frontend now falls back to `relation_label`, which makes
    THIS the assertion that matters: every relation the scorer can produce
    must arrive with words attached."""
    for relation in config.RELATION_CREDIT:
        assert ats_agent.RELATION_LABELS.get(relation), relation
        flat = ats_agent._flat_match(relation, "demonstrated")
        assert ats_agent.MATCH_LABELS.get(flat), f"{relation} -> {flat}"


def test_a_false_friend_cannot_cascade_into_an_implied_skill():
    """The failure this pair caused together: "LangGraph ReAct agent" matched
    React, and React is a prerequisite for JavaScript, so one homograph became
    two scored rows. The inference is only ever as good as the term it starts
    from."""
    profile = dict(PROFILE, skills_claimed=[], projects=[], experience=[{
        "title": "AI Engineer", "organization": "Acme",
        "start": "2024-07", "end": "2025-03", "is_professional": True,
        "bullets": ["Developed a LangGraph ReAct agent with dynamic tool selection"],
    }])
    reqs = {"requirements": [
        {"name": "React", "category": "technical_skill", "importance": "required"},
        {"name": "JavaScript", "category": "technical_skill", "importance": "required"},
    ], "years_experience_required": 1, "seniority": "entry"}
    with patch.object(ats_agent, "extract_requirements",
                      return_value=ats_agent._normalize_requirements(reqs)), \
         patch.object(ats_agent.cv_profile, "build_profile", return_value=profile), \
         patch.object(ats_agent, "_entailment_pass", return_value={}):
        result = ats_agent.compute_requirements_score("raw cv text", "jd text")

    assert _row(result, "React")["relation"] == "none"
    assert _row(result, "JavaScript")["relation"] == "none"
    assert {"React", "JavaScript"} <= set(result["missing_skills"])
