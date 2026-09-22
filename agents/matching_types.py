"""
The vocabulary the matching layers speak to each other in.

Phase 1 of the generic-matching plan. Three small dataclasses so the semantic
layer can hand candidates to the matcher without either side importing the
other's internals, and so a candidate carries its provenance -- which span,
which similarity, which rank -- all the way to the scored row.

**These wrap the existing dicts; they do not replace them.** Every current
caller of `evidence_spans()` gets the same list of dicts it always did, and
`match_requirement()` still takes and returns dicts. That is deliberate: the
plan says to introduce modules while preserving current APIs, and the dict
shape is what the API layer serialises, the frontend renders and the stored
Application rows hold. `EvidenceSpan.from_dict` / `.to_dict` are the seam.

(The plan filed these under `models/matching.py`. This project already has a
`models.py` at the root holding the SQLAlchemy tables, and a `models/`
package beside it would shadow it -- so the DTOs live here, next to the code
that uses them.)
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict

from agents import skill_matching


# ---- concept vocabulary ------------------------------------------------------
#
# Phase 2: a small, stable vocabulary for NORMALISATION -- not an ontology of
# every profession. The point of the plan is that generality comes from
# representation plus retrieval, so this list exists to say what KIND of thing
# a term is when we happen to know, never to enumerate what exists.

CONCEPT_TYPES = (
    "programming_language", "framework", "library", "platform", "database",
    "cloud_platform", "tool", "methodology", "technology", "business_skill",
    "domain_knowledge", "capability", "responsibility", "credential",
    "soft_skill",
)

# Directional, and named after what they assert. The scorer's relations map
# onto these one-for-one, which is the point: the credit table and the
# vocabulary should not drift into two different theories of the same thing.
RELATION_TYPES = {
    "same_concept": "the requirement's own word, or a spelling of it",
    "alias": "a different NAME for the same thing (Postgres / PostgreSQL)",
    "implied_by": "cannot have been done without it (FastAPI implies Python)",
    "subtype_of": "a KIND of it (CNN is a kind of deep learning)",
    "capability_supported_by": "evidence a model judged to demonstrate it",
    "sibling": "the same kind of thing, and therefore NOT evidence",
    "exclusive": "explicitly cannot substitute",
}

# scorer relation -> plan relation type. `sibling` and `exclusive` have no
# scorer relation on purpose: they are the two that must never produce one.
RELATION_TYPE_OF = {
    "exact": "same_concept",
    "alias": "alias",
    "implied": "implied_by",
    "subset": "subtype_of",
    "semantic_support": "capability_supported_by",
    "none": None,
}

# Requirement types. `skill` is a named thing you either have or don't;
# `capability` is something a CV demonstrates without necessarily naming it,
# and is the case deterministic matching structurally cannot serve.
REQUIREMENT_TYPES = ("skill", "capability", "credential", "experience")

# What kind of evidence a requirement wants. A capability asked for as
# `demonstrated` cannot be satisfied by a keyword row even if the words match.
EVIDENCE_EXPECTATIONS = ("hands_on", "demonstrated", "familiarity", "any")


def _slug(text: str, prefix: str) -> str:
    digest = hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


@dataclass
class Requirement:
    """One thing a posting asks for, normalised.

    `raw_text` is kept verbatim next to `canonical_name` for the reason the
    plan gives and this project already learned the hard way: a score is only
    auditable if you can see the words the posting actually used, not the
    words we decided it meant.
    """
    raw_text: str
    canonical_name: str
    category: str
    importance: str = "required"
    id: str = ""
    years_required: float | None = None
    seniority: str | None = None
    concept_family: str | None = None
    requirement_type: str = "skill"
    evidence_expected: str = "any"

    def __post_init__(self):
        if not self.id:
            self.id = _slug(f"{self.raw_text}|{self.category}", "req")

    @property
    def is_capability(self) -> bool:
        return self.requirement_type in ("capability", "experience")

    @classmethod
    def from_dict(cls, row: dict) -> "Requirement":
        row = row if isinstance(row, dict) else {}
        name = str(row.get("name") or "").strip()
        return cls(
            raw_text=str(row.get("raw_text") or name),
            canonical_name=str(row.get("canonical_name")
                               or row.get("canonical")
                               or skill_matching.canonical(name) or name),
            category=str(row.get("category") or "technical_skill"),
            importance=str(row.get("importance") or "required"),
            id=str(row.get("id") or ""),
            years_required=row.get("years_required"),
            seniority=row.get("seniority"),
            concept_family=row.get("concept_family"),
            # Empty, not "skill"/"any", when the dict does not say. These
            # two fields decide routing, and a default that looks like an
            # answer would stop requirement_normalizer from ever classifying
            # -- every capability would silently be treated as a named skill.
            requirement_type=str(row.get("requirement_type")
                                 or row.get("type") or ""),
            evidence_expected=str(row.get("evidence_expected") or ""),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvidenceSpan:
    """One quotable line of the CV.

    `concepts` is the normalised reading of the line; `text` is what the
    candidate wrote. Both are kept — the plan is explicit that the normalised
    form must not replace the original, and every guard in this project
    depends on being able to quote the source.
    """
    text: str
    source: str = "cv"
    demonstrated: bool = False
    dated: bool = False
    entry: str = ""
    id: str = ""
    concepts: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    embedding_id: str | None = None

    def __post_init__(self):
        if not self.id:
            self.id = _slug(self.text, "span")

    @property
    def location(self) -> str:
        return "demonstrated" if self.demonstrated else "claimed"

    @classmethod
    def from_dict(cls, span: dict) -> "EvidenceSpan":
        span = span if isinstance(span, dict) else {}
        return cls(
            text=str(span.get("text") or ""),
            source=str(span.get("source") or "cv"),
            demonstrated=bool(span.get("demonstrated")),
            dated=bool(span.get("dated")),
            entry=str(span.get("entry") or ""),
            id=str(span.get("id") or ""),
            concepts=list(span.get("concepts") or []),
            domains=list(span.get("domains") or []),
            embedding_id=span.get("embedding_id"),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SemanticCandidate:
    """A span retrieval thinks might be evidence. Not a match.

    The distinction is the load-bearing one in the whole plan: embeddings
    generate candidates, rules and adjudication decide. A SemanticCandidate
    that never reaches the LLM scores nothing, and one that does carries its
    similarity and rank into the row so a surprising verdict can be traced
    back to what retrieval offered.
    """
    requirement_id: str
    span_id: str
    similarity: float
    rank: int
    span: EvidenceSpan | None = None
    band: str = "ignore"

    def to_dict(self) -> dict:
        out = asdict(self)
        out.pop("span", None)
        return out
