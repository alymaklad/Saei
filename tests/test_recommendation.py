"""
The recommendation layer: which half of a low score a rewrite may touch.

The distinction these tests exist to protect is the one in the plan and in
this project's integrity rules: a wording gap is evidence the profile already
has, described in words the posting did not use, and a rewrite may close it.
A genuine gap is a requirement nothing in the profile supports, and a rewrite
that closes one has fabricated experience. Collapsing the two into "gaps"
would put the CV rewriter one prompt away from inventing a skill.
"""
import config
from agents import recommendation


def _row(name, relation="none", location="none", credit=0.0,
         importance="required", category="technical_skill", evidence=None, via=None):
    return {"name": name, "relation": relation, "evidence_location": location,
            "credit": credit, "importance": importance, "category": category,
            "evidence": evidence, "via": via}


def _result(rows, score=0.5, breakdown=None, cv_years=1.2):
    return {"score": score, "requirement_results": rows, "cv_years": cv_years,
            "breakdown": breakdown or {}}


def test_a_full_credit_row_is_a_strength():
    rec = recommendation.build_recommendation(_result([
        _row("Python", "exact", "demonstrated", 1.0, evidence="Built a Python service")]))
    assert [e["requirement"] for e in rec["strengths"]] == ["Python"]
    assert not rec["wording_gaps"] and not rec["gaps"]


def test_partial_credit_from_real_work_is_a_wording_gap():
    """0.90 through a prerequisite means the work is there and the posting's
    word is not. That is a sentence away from full credit, truthfully."""
    rec = recommendation.build_recommendation(_result([
        _row("SQL", "implied", "demonstrated", 0.9,
             evidence="Queried PostgreSQL", via="“postgresql” requires “SQL”")]))
    gap = rec["wording_gaps"][0]
    assert gap["requirement"] == "SQL"
    assert gap["fixable_by_rewrite"] is True
    assert not rec["gaps"]


def test_a_listed_only_skill_is_not_fixable_by_a_rewrite():
    """The rewriter cannot move a skill into a role that never mentioned it.
    Only the candidate knows whether that internship used Kubernetes, so this
    is routed to a profile edit rather than to the model."""
    rec = recommendation.build_recommendation(_result([
        _row("Kubernetes", "exact", "claimed", 0.65, category="tool")]))
    gap = rec["wording_gaps"][0]
    assert gap["fixable_by_rewrite"] is False
    assert gap["fixable_by_profile_edit"] is True


def test_an_unevidenced_requirement_is_a_genuine_gap():
    """The line a rewrite must never cross."""
    rec = recommendation.build_recommendation(_result([_row("Terraform")]))
    assert [e["requirement"] for e in rec["gaps"]] == ["Terraform"]
    assert rec["gaps"][0]["fixable_by_rewrite"] is False
    assert not rec["wording_gaps"]


def test_no_gap_a_rewrite_cannot_fix_is_ever_marked_fixable():
    """Swept across every shape at once, because this is the property the CV
    rewriter will read to decide what it is allowed to write."""
    rows = [_row("Terraform"),
            _row("Kubernetes", "exact", "claimed", 0.65),
            _row("Bachelor's degree", category="education"),
            _row("SQL", "implied", "demonstrated", 0.9)]
    rec = recommendation.build_recommendation(_result(rows))
    for entry in rec["gaps"] + rec["hard_failures"]:
        assert entry["fixable_by_rewrite"] is False, entry["requirement"]
    for entry in rec["wording_gaps"]:
        if entry["fixable_by_rewrite"]:
            assert entry["relation"] != "none", (
                "a rewrite may only reword evidence that exists")


def test_a_missing_required_credential_is_a_hard_failure():
    """Different in kind from a missing tool: it cannot be reworded, and it
    cannot be acquired before the interview either."""
    rec = recommendation.build_recommendation(_result([
        _row("Bachelor's degree", category="education")]))
    assert [e["requirement"] for e in rec["hard_failures"]] == ["Bachelor's degree"]
    assert rec["recommendation"] == "review"


def test_a_years_shortfall_is_reported_as_a_hard_failure_not_a_gap():
    """Seniority stays separate from skill coverage, per the plan: a candidate
    can match every technology and still be three years short, and averaging
    that into a percentage hides the only thing that decides the application."""
    breakdown = {"experience": {"score": 0.4, "weight": 0.2,
                                "detail": {"years_required": 3, "cv_years": 1.2}}}
    rec = recommendation.build_recommendation(
        _result([_row("Python", "exact", "demonstrated", 1.0)], breakdown=breakdown))
    hard = rec["hard_failures"][0]
    assert "3+ years" in hard["requirement"]
    assert "1.2 years" in hard["why"]
    assert hard["fixable_by_rewrite"] is False


def test_the_ceiling_is_computed_from_the_scorer_s_own_arithmetic():
    """What tailoring could honestly reach: every wording gap at full credit,
    nothing else changed. Computed with the real weights and points so it
    cannot drift from what a rewrite would actually produce."""
    breakdown = {
        "required_skills": {
            "score": 0.9, "weight": 1.0,
            "items": [
                {"name": "SQL", "points_earned": 9.0, "points": 10},
                {"name": "Python", "points_earned": 9.0, "points": 10},
            ]},
    }
    rows = [_row("SQL", "implied", "demonstrated", 0.9, evidence="Queried PostgreSQL"),
            _row("Python", "implied", "demonstrated", 0.9, evidence="Built a FastAPI service")]
    rec = recommendation.build_recommendation(_result(rows, score=0.9, breakdown=breakdown))
    assert rec["tailoring_ceiling"] == 1.0


def test_the_ceiling_ignores_gaps_a_rewrite_may_not_close():
    """A ceiling that counted unevidenced requirements would be a promise the
    pipeline is forbidden to keep."""
    breakdown = {
        "required_skills": {
            "score": 0.45, "weight": 1.0,
            "items": [
                {"name": "SQL", "points_earned": 9.0, "points": 10},
                {"name": "Terraform", "points_earned": 0.0, "points": 10},
            ]},
    }
    rows = [_row("SQL", "implied", "demonstrated", 0.9, evidence="Queried PostgreSQL"),
            _row("Terraform")]
    rec = recommendation.build_recommendation(_result(rows, score=0.45, breakdown=breakdown))
    assert rec["tailoring_ceiling"] == 0.5, "SQL to full credit only; Terraform stays at 0"


def test_the_headline_never_flatters_a_weak_score():
    rec = recommendation.build_recommendation(
        _result([_row("Terraform"), _row("AWS", category="tool")], score=0.18))
    assert rec["headline"].startswith("18% match")
    assert "2 with no evidence" in rec["headline"]
    assert rec["recommendation"] == "skip"


def test_recommendation_bands_follow_the_fit_threshold():
    strong = recommendation.build_recommendation(
        _result([_row("Python", "exact", "demonstrated", 1.0)],
                score=config.FIT_THRESHOLD + 0.01))
    assert strong["recommendation"] == "recommend"

    nearly = recommendation.build_recommendation(
        _result([_row("Python", "implied", "demonstrated", 0.9)],
                score=config.FIT_THRESHOLD * 0.8))
    assert nearly["recommendation"] == "tailor_first"


def test_every_row_appears_in_the_evidence_summary():
    """The audit trail the targeting plan will read: one line per requirement,
    with what was found and where."""
    rows = [_row("Python", "exact", "demonstrated", 1.0, evidence="Built a service"),
            _row("Terraform")]
    rec = recommendation.build_recommendation(_result(rows))
    assert len(rec["evidence_summary"]) == 2
    assert {e["requirement"] for e in rec["evidence_summary"]} == {"Python", "Terraform"}
