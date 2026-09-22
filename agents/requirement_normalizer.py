"""
Turning an extracted requirement into something the matcher can route on.

Phase 5 of the plan. The extraction call now returns four extra fields —
`canonical_name`, `requirement_type`, `concept_family`, `evidence_expected` —
and this module is what makes them safe to depend on: every one of them is
recomputed or defaulted here, so a model that omits them, invents a value, or
returns them in a different vocabulary produces the same routing as one that
gets them right.

The important one is `requirement_type`. It decides which of the three levels
a requirement is even eligible for:

    skill        a named thing. Term matching can settle it, so it goes
                 through the deterministic tables first.
    capability   something a CV DEMONSTRATES without necessarily naming.
                 "Stakeholder management", "requirements gathering",
                 "campaign performance analysis". No table will ever hold
                 these — there is no finite list — so they go to retrieval.

That distinction is the whole of what makes the matcher generic across job
families: a marketing posting is mostly capabilities and a backend posting is
mostly skills, and neither needs its own code path, only its own mix.

Classification is deterministic and works from what the requirement IS, never
from the job title. The plan is explicit about this: a posting is authoritative
for its own role, and "ML engineer implies PyTorch" is exactly the overfitting
to avoid.
"""
import re

import config
from agents import skill_matching
from agents.matching_types import (EVIDENCE_EXPECTATIONS, REQUIREMENT_TYPES,
                                   Requirement)

# Categories that are capabilities by their nature, whatever words they use.
_CAPABILITY_CATEGORIES = {"responsibility", "soft_skill"}

# A requirement phrased as an action is a capability even in a technical
# category: "Deploy models to production" is not a skill you either have or
# don't, it is work a CV either shows or doesn't.
_ACTION_PREFIX = re.compile(
    r"^(build|building|develop|developing|design|designing|deploy|deploying|"
    r"manage|managing|lead|leading|coordinate|coordinating|maintain|"
    r"maintaining|collaborate|collaborating|communicate|communicating|"
    r"analyse|analyze|analysing|analyzing|drive|driving|own|owning|"
    r"deliver|delivering|support|supporting|write|writing|present|presenting)\b",
    re.IGNORECASE)

# Concept families are for grouping and reporting, not for scoring. Derived
# from the tables the matcher already has so there is one vocabulary rather
# than two drifting ones.
_FAMILY_BY_CONCEPT = {
    "python": "programming_language", "java": "programming_language",
    "javascript": "programming_language", "typescript": "programming_language",
    "c++": "programming_language", "c#": "programming_language",
    "sql": "query_language", "r": "programming_language",
    "fastapi": "web_framework", "django": "web_framework", "flask": "web_framework",
    "nestjs": "web_framework", "react": "frontend_framework",
    "angular": "frontend_framework", "vue": "frontend_framework",
    "pytorch": "ml_framework", "tensorflow": "ml_framework", "keras": "ml_framework",
    "scikit-learn": "ml_framework",
    "postgresql": "database", "mysql": "database", "mongodb": "database",
    "sqlite": "database", "redis": "database",
    "amazon web services": "cloud_platform", "google cloud platform": "cloud_platform",
    "microsoft azure": "cloud_platform",
    "docker": "infrastructure", "kubernetes": "infrastructure", "terraform": "infrastructure",
    "git": "tooling", "github actions": "ci_cd", "jenkins": "ci_cd",
    "machine learning": "ai_ml", "deep learning": "ai_ml", "computer vision": "ai_ml",
    "natural language processing": "ai_ml", "agentic ai": "ai_ml",
    "large language model": "ai_ml",
}


def normalize(requirement) -> Requirement:
    """One extracted requirement, with every routing field settled.

    Model-supplied values are kept when they are in the allowed vocabulary and
    replaced when they are not — the same treatment `category` and
    `importance` already get, and for the same reason: these fields change
    what the matcher DOES, so a hallucinated one would change a score.
    """
    req = (requirement if isinstance(requirement, Requirement)
           else Requirement.from_dict(requirement))

    req.canonical_name = (skill_matching.canonical(req.raw_text)
                          or req.canonical_name or req.raw_text)

    if req.requirement_type not in REQUIREMENT_TYPES:
        req.requirement_type = ""
    req.requirement_type = req.requirement_type or classify_type(req)

    if req.evidence_expected not in EVIDENCE_EXPECTATIONS:
        req.evidence_expected = "demonstrated" if req.is_capability else "any"

    req.concept_family = req.concept_family or concept_family(req.canonical_name)
    return req


def classify_type(requirement: Requirement) -> str:
    """skill / capability / credential / experience, from the requirement alone."""
    category = (requirement.category or "").lower()
    if category in ("education", "certification"):
        return "credential"
    if category == "experience":
        return "experience"
    if category in _CAPABILITY_CATEGORIES:
        return "capability"

    text = (requirement.raw_text or "").strip()
    if _ACTION_PREFIX.match(text):
        return "capability"
    # A phrase too long for term matching cannot be settled deterministically
    # whatever it is called, so it is a capability by consequence: the
    # matcher already refuses to term-match these, and typing them this way
    # is what sends them somewhere useful instead of nowhere.
    if len(text) > config.REQUIREMENT_TOKEN_MATCH_MAX_CHARS:
        return "capability"
    # Multi-word and unknown to every table: no alias, no hyponym, no
    # prerequisite mentions it. "Stakeholder management" lands here.
    if len(text.split()) >= 2 and not skill_matching.is_known(text):
        return "capability"
    return "skill"


def concept_family(canonical_name: str) -> str | None:
    """A grouping label, or None. Never used in scoring."""
    return _FAMILY_BY_CONCEPT.get(skill_matching.canonical(canonical_name))


def normalize_all(requirements) -> list[Requirement]:
    return [normalize(r) for r in requirements or []]


def enrich_rows(rows: list[dict]) -> list[dict]:
    """Add the normalised fields to the scorer's own requirement dicts.

    The scorer works in dicts end to end — they are what the API serialises
    and what the stored Application rows hold — so normalisation is applied
    as extra keys rather than as a type change. `raw_text` is added here
    because the plan requires the posting's own wording to survive next to
    whatever we decided it meant.
    """
    out = []
    for row in rows or []:
        req = normalize(row)
        merged = dict(row)
        merged.update({
            "raw_text": req.raw_text,
            "canonical": req.canonical_name,
            "canonical_name": req.canonical_name,
            "requirement_type": req.requirement_type,
            "concept_family": req.concept_family,
            "evidence_expected": req.evidence_expected,
            "id": req.id,
        })
        out.append(merged)
    return out
