"""
What the score means, and which half of it a rewrite can move.

Phase 8. A single scalar cannot answer the question the user actually has,
which is not "how good is this match" but "should I apply, and what do I do
about the gaps". Two candidates scoring 0.62 can be in completely different
situations: one has the experience and describes it in the wrong words, the
other is three years short. The first is a tailoring job. The second is not,
and no rewrite may pretend otherwise.

So the output separates:

    wording gaps    the profile HAS evidence the posting's words missed --
                    a subset or prerequisite match sitting below full credit,
                    or a skill demonstrated in work but only listed in the CV.
                    A tailored CV can close these truthfully.

    genuine gaps    nothing in the profile supports the requirement. A rewrite
                    that closes one of these has fabricated it, which is the
                    single failure this whole pipeline is built to prevent.

    hard failures   a required credential or a years-of-experience bar the
                    candidate does not meet. Not a wording problem, not a
                    coverage problem, and worth saying plainly rather than
                    diluting into a percentage.

Every entry carries the requirement it came from and the evidence behind it,
so the targeting plan (phase 9) can be generated from this deterministically
rather than by asking a model what it thinks the strengths were.
"""
import config


# A recommendation is a threshold call, and the thresholds are named here
# rather than buried in an if-chain so they can be moved with the fit
# threshold they hang off.
def _recommendation(score: float, hard_failures: list) -> str:
    if hard_failures:
        return "review"          # something is genuinely missing; a human decides
    if score >= config.FIT_THRESHOLD:
        return "recommend"
    if score >= config.FIT_THRESHOLD * 0.75:
        return "tailor_first"    # close enough that wording is worth fixing
    return "skip"


RECOMMENDATION_LABELS = {
    "recommend": "Apply",
    "tailor_first": "Tailor, then apply",
    "review": "Worth a look, with caveats",
    "skip": "Probably not worth applying",
}


def _rows(result: dict) -> list[dict]:
    return [r for r in result.get("requirement_results") or [] if isinstance(r, dict)]


def build_recommendation(result: dict, rating: str = "") -> dict:
    """The structured verdict for one scored job.

    Deterministic: every field is derived from rows the scorer already
    produced. No LLM call, nothing to hallucinate, and the same input always
    gives the same advice.
    """
    rows = _rows(result)
    score = float(result.get("score") or 0.0)

    strengths, wording, genuine, hard = [], [], [], []

    for row in rows:
        name = str(row.get("name") or "")
        relation = str(row.get("relation") or "none")
        location = str(row.get("evidence_location") or "none")
        credit = float(row.get("credit") or 0.0)
        importance = str(row.get("importance") or "required")
        entry = {
            "requirement": name,
            "importance": importance,
            "category": row.get("category"),
            "credit": round(credit, 3),
            "relation": relation,
            "evidence": row.get("evidence"),
        }

        if relation == "none":
            # Nothing in the profile supports it. The only honest fix is for
            # the candidate to have the experience, or to add it on the
            # Profile page if they do and never wrote it down.
            entry["why"] = "no line in your profile supports this"
            entry["fixable_by_rewrite"] = False
            (hard if _is_hard_failure(row) else genuine).append(entry)
            continue

        if credit >= 0.999:
            strengths.append(entry)
            continue

        # Partial credit. Which KIND of partial decides who can fix it.
        if location == "claimed":
            entry["why"] = ("listed in your skills section but no role or "
                            "project describes using it")
            entry["fixable_by_rewrite"] = False
            entry["fixable_by_profile_edit"] = True
        else:
            entry["why"] = ("your work shows this in different words "
                            f"({row.get('via') or relation})")
            entry["fixable_by_rewrite"] = True
        wording.append(entry)

    experience = (result.get("breakdown") or {}).get("experience") or {}
    detail = experience.get("detail") or {}
    years_required = detail.get("years_required")
    cv_years = detail.get("cv_years", result.get("cv_years"))
    if years_required and cv_years is not None and cv_years < years_required:
        hard.append({
            "requirement": f"{years_required}+ years of experience",
            "importance": "required",
            "category": "experience",
            "credit": round(float(experience.get("score") or 0.0), 3),
            "relation": "none",
            "evidence": None,
            "why": (f"your dated professional entries total {cv_years} years, "
                    f"about {round(years_required - cv_years, 1)} short"),
            "fixable_by_rewrite": False,
        })

    strengths.sort(key=lambda e: (e["importance"] != "required", e["requirement"]))
    wording.sort(key=lambda e: (e["importance"] != "required", e["credit"]))
    genuine.sort(key=lambda e: (e["importance"] != "required", e["requirement"]))

    recommendation = _recommendation(score, hard)
    return {
        "score": round(score, 4),
        "rating": rating,
        "recommendation": recommendation,
        "recommendation_label": RECOMMENDATION_LABELS[recommendation],
        "strengths": strengths[:8],
        "wording_gaps": wording[:8],
        "gaps": genuine[:8],
        "hard_failures": hard,
        "evidence_summary": _evidence_summary(rows),
        "headline": _headline(score, strengths, wording, genuine, hard),
        # The number a tailoring pass could plausibly reach if every wording
        # gap closed to full credit and nothing else changed. NOT a promise --
        # it is the ceiling of the honest work available, and the distance
        # between it and the score is the whole argument for tailoring.
        "tailoring_ceiling": _ceiling(result, wording),
    }


def _is_hard_failure(row: dict) -> bool:
    """A required credential with no evidence is different in kind from a
    required tool with no evidence: one can be learned before the interview
    and mentioned, the other cannot be acquired at all."""
    return (str(row.get("importance")) == "required"
            and str(row.get("category")) in ("education", "certification"))


def _evidence_summary(rows: list[dict]) -> list[dict]:
    """One line per scored requirement: what was found and where.

    This is the auditable core of the whole engine reduced to something a
    person can read in ten seconds — and the input the targeting plan will
    read in phase 9.
    """
    return [{
        "requirement": r.get("name"),
        "relation": r.get("relation"),
        "location": r.get("evidence_location"),
        "credit": round(float(r.get("credit") or 0.0), 3),
        "evidence": r.get("evidence"),
        "source": r.get("evidence_source"),
        "similarity": r.get("semantic_similarity"),
    } for r in rows]


def _ceiling(result: dict, wording: list[dict]) -> float | None:
    """Re-run the bucket arithmetic with every wording gap at full credit.

    Uses the scorer's own weights and point values rather than a rule of
    thumb, so the number cannot drift away from what a real rewrite would
    actually produce.
    """
    breakdown = result.get("breakdown") or {}
    if not breakdown or not wording:
        return None

    fixable = {e["requirement"] for e in wording if e.get("fixable_by_rewrite")}
    if not fixable:
        return None

    total = 0.0
    for bucket, data in breakdown.items():
        items = data.get("items") or []
        if not items:
            total += float(data.get("score") or 0.0) * float(data.get("weight") or 0.0)
            continue
        earned = sum(float(item["points"]) if item["name"] in fixable
                     else float(item["points_earned"]) for item in items)
        possible = sum(float(item["points"]) for item in items)
        score = earned / possible if possible else 0.0
        total += score * float(data.get("weight") or 0.0)
    return round(total, 4)


def _headline(score, strengths, wording, genuine, hard) -> str:
    """One sentence, and it has to be the true one.

    Written deterministically because a generated headline is where a
    "strong candidate!" creeps in over a 0.41 score.
    """
    parts = [f"{round(score * 100)}% match"]
    if hard:
        parts.append(f"{len(hard)} hard requirement{'s' if len(hard) > 1 else ''} unmet")
    if strengths:
        parts.append(f"{len(strengths)} requirement{'s' if len(strengths) > 1 else ''} "
                     f"fully evidenced")
    if wording:
        parts.append(f"{len(wording)} worth rewording")
    if genuine:
        parts.append(f"{len(genuine)} with no evidence in your profile")
    return "; ".join(parts) + "."
