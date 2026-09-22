"""
Finding the CV lines a requirement might be about.

The narrow job in the plan's Level B: embed the requirement, search the
candidate's spans, return the closest few with their similarity and rank.
This module deliberately cannot decide anything — it has no access to the
credit table, does not know what a relation is, and never returns a match.
Its output is a list of candidates for the guards and the adjudicator.

Why this is worth having at all, given the deterministic tables already
resolve most technology names: the requirements it is FOR are the ones no
table will ever hold. "Stakeholder management", "campaign performance
analysis", "requirements gathering" — every job family has a dozen of them,
they are phrased differently in every posting, and a CV demonstrates them
without ever using the phrase. Before this, those requirements reached the
adjudication call alongside the first 80 spans of the CV and had to be found
in the noise. Now the call is shown the five lines most likely to be the
answer.
"""
import config
from agents import skill_matching
from agents.matching_types import EvidenceSpan, Requirement, SemanticCandidate
from services import embedding_cache
from services.semantic_index import SpanIndex


def band_for(similarity: float) -> str:
    """Which confidence band a similarity falls in. Reporting, not routing."""
    if similarity >= config.SEMANTIC_SIMILARITY_STRONG:
        return "strong"
    if similarity >= config.SEMANTIC_SIMILARITY_IGNORE:
        return "ambiguous"
    return "ignore"


def build_index(spans) -> SpanIndex:
    """One index per CV, reused across every requirement of every job."""
    if not config.SEMANTIC_RETRIEVAL or not embedding_cache.available():
        return SpanIndex()
    return SpanIndex.build(spans)


def retrieve_top_evidence(requirement, index: SpanIndex,
                          top_k: int | None = None) -> list[SemanticCandidate]:
    """The closest spans to one requirement, best first.

    Embeds the requirement's RAW text, not its canonical name: the posting's
    own wording is what carries the meaning a table has no entry for, and
    canonicalising "Build and maintain RESTful services" down to "rest api"
    before embedding throws away the half that retrieval is good at.
    """
    if not isinstance(requirement, Requirement):
        requirement = Requirement.from_dict(requirement)
    if index.is_empty or not config.SEMANTIC_RETRIEVAL:
        return []

    query = embedding_cache.embed_one(requirement.raw_text or requirement.canonical_name)
    if not query:
        return []

    hits = index.search(query,
                        top_k=top_k or config.SEMANTIC_TOP_K,
                        minimum=config.SEMANTIC_SIMILARITY_IGNORE)
    return [
        SemanticCandidate(requirement_id=requirement.id, span_id=span.id,
                          similarity=round(similarity, 4), rank=rank,
                          span=span, band=band_for(similarity))
        for rank, (span, similarity) in enumerate(hits, start=1)
    ]


def drop_unsafe(requirement, candidates: list[SemanticCandidate]) -> list[SemanticCandidate]:
    """Remove candidates whose only connection to the requirement is a sibling.

    The guard that makes retrieval safe to use at all. A span reading
    "deployed to Google Cloud" is among the nearest neighbours of an AWS
    requirement — necessarily, since the two are the same kind of thing — and
    offering it to the adjudicator invites exactly the verdict the exclusive
    groups exist to refuse. Rejected here rather than after the model
    answers, so the model is never given the opportunity.

    A span that mentions a sibling AND something legitimate keeps its place:
    the test is whether the sibling is all it has.
    """
    if not isinstance(requirement, Requirement):
        requirement = Requirement.from_dict(requirement)

    kept = []
    for candidate in candidates:
        span = candidate.span
        if span is None:
            continue
        if _only_evidence_is_a_sibling(requirement.canonical_name, span):
            continue
        kept.append(candidate)
    return kept


def _only_evidence_is_a_sibling(requirement: str, span: EvidenceSpan) -> bool:
    concepts = span.concepts or []
    siblings = [c for c in concepts
                if skill_matching.are_mutually_exclusive(requirement, c)]
    if not siblings:
        return False
    # Something else in the line might legitimately support the requirement,
    # so only drop the span when the sibling is the whole of its content.
    others = [c for c in concepts if c not in siblings]
    return not others
