"""
Level B of the three-level matcher: retrieval, guarded, routed.

The three levels in the plan are a division of authority, not a pipeline of
equals:

  A  deterministic rules       decide      — exact, alias, prerequisite, subtype
  B  semantic retrieval        nominates   — this module
  C  batched LLM adjudication  adjudicates — one call, on B's shortlist

This module is B's whole surface. It answers one question — "which lines of
this CV might be about this requirement?" — and returns candidates carrying
their similarity, rank and confidence band. It calculates no credit, and by
design it cannot: nothing here imports the credit table.

**A strong candidate still goes to the LLM.** The band is recorded because
calibrating it needs the distribution, and because a reader deserves to know
whether a verdict came off a near-duplicate line or a distant one. But
accepting on similarity alone would put points on the one signal this project
has repeatedly proved cannot be trusted alone: AWS and GCP are near-identical
vectors precisely BECAUSE they are the same kind of thing.
"""
import config
from agents import evidence_retrieval
from agents.matching_types import Requirement, SemanticCandidate


class SemanticDecision:
    """What retrieval concluded for one requirement.

    `route` is one of:
      none      nothing above the ignore threshold — do not spend a verdict
      adjudicate  candidates worth a directional judgement
    """

    def __init__(self, requirement: Requirement, candidates: list[SemanticCandidate],
                 dropped: int = 0):
        self.requirement = requirement
        self.candidates = candidates
        self.dropped_as_sibling = dropped

    @property
    def route(self) -> str:
        return "adjudicate" if self.candidates else "none"

    @property
    def best(self) -> SemanticCandidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def band(self) -> str:
        return self.best.band if self.best else "ignore"

    def spans(self):
        return [c.span for c in self.candidates if c.span is not None]

    def to_dict(self) -> dict:
        return {
            "requirement_id": self.requirement.id,
            "requirement": self.requirement.raw_text,
            "route": self.route,
            "band": self.band,
            "dropped_as_sibling": self.dropped_as_sibling,
            "candidates": [c.to_dict() for c in self.candidates],
        }


def retrieve_evidence(requirement, evidence_spans, top_k: int | None = None,
                      index=None) -> list[SemanticCandidate]:
    """Top-k spans for one requirement. Builds a throwaway index if needed.

    Callers scoring a whole job should build the index once with
    `evidence_retrieval.build_index()` and pass it in — it is per CV, not per
    requirement, and rebuilding it per requirement would re-read the cache
    dozens of times for no gain.
    """
    if not isinstance(requirement, Requirement):
        requirement = Requirement.from_dict(requirement)
    if index is None:
        index = evidence_retrieval.build_index(evidence_spans)
    return evidence_retrieval.retrieve_top_evidence(requirement, index, top_k=top_k)


def classify_semantic_candidates(requirement, candidates) -> SemanticDecision:
    """Apply the guards and decide what happens to these candidates.

    The sibling drop happens here rather than after adjudication so the model
    is never offered the line that would tempt it. What survives is routed to
    the LLM whatever its band — see the module docstring.
    """
    if not isinstance(requirement, Requirement):
        requirement = Requirement.from_dict(requirement)
    candidates = list(candidates or [])
    safe = evidence_retrieval.drop_unsafe(requirement, candidates)
    return SemanticDecision(requirement, safe, dropped=len(candidates) - len(safe))


def shortlist(requirements, evidence_spans, index=None) -> dict[str, SemanticDecision]:
    """Decisions for every unresolved requirement, keyed by requirement id.

    One index build, one embedding call for the batch of requirement texts
    (the cache collapses repeats), then a local search each.
    """
    requirements = [r if isinstance(r, Requirement) else Requirement.from_dict(r)
                    for r in requirements or []]
    if not requirements or not config.SEMANTIC_RETRIEVAL:
        return {}

    if index is None:
        index = evidence_retrieval.build_index(evidence_spans)
    if index.is_empty:
        return {}

    out = {}
    for requirement in requirements:
        candidates = evidence_retrieval.retrieve_top_evidence(requirement, index)
        out[requirement.id] = classify_semantic_candidates(requirement, candidates)
    return out


def _distribution(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    return {"min": round(ordered[0], 4),
            "median": round(ordered[len(ordered) // 2], 4),
            "max": round(ordered[-1], 4),
            "count": len(ordered)}


def _calibration_warning(decisions: dict, similarities: list[float]) -> str | None:
    """Name a distribution that says the thresholds are wrong.

    Two degenerate shapes, and neither announces itself:

      nothing retrieved at all, for every requirement -- the floor is above
      what this model produces for a real match, so retrieval is silently off
      and every capability requirement reaches the adjudicator with nothing
      attached, or not at all.

      everything in the strong band -- the floor is below what this model
      produces for UNRELATED text, so retrieval is nominating indiscriminately
      and the shortlist is just the first k spans of the CV.

    Reported, never acted on: correcting a threshold from one job's retrieval
    would be fitting to a sample of one. `python -m bench.report --calibrate`
    is where thresholds get set.
    """
    if not decisions:
        return None
    if not similarities:
        return ("retrieval returned nothing for any of "
                f"{len(decisions)} requirements — SEMANTIC_SIMILARITY_IGNORE "
                f"({config.SEMANTIC_SIMILARITY_IGNORE}) may be above what this "
                "model scores a real match; run bench.report --calibrate")
    if all(s >= config.SEMANTIC_SIMILARITY_STRONG for s in similarities):
        return (f"every one of {len(similarities)} candidates scored above the "
                f"strong band ({config.SEMANTIC_SIMILARITY_STRONG}) — the bands "
                "are probably below this model's scale, so retrieval is not "
                "discriminating; run bench.report --calibrate")
    return None


def retrieval_report(decisions: dict[str, SemanticDecision], index=None) -> dict:
    """What retrieval did, for the bench and for calibration.

    Reported rather than logged because the thresholds above are guesses until
    someone looks at this distribution against labelled cases.
    """
    from services import embedding_cache

    bands = {"strong": 0, "ambiguous": 0, "ignore": 0}
    similarities = []
    for decision in decisions.values():
        bands[decision.band] = bands.get(decision.band, 0) + 1
        similarities.extend(c.similarity for c in decision.candidates)

    return {
        # A threshold that is wrong changes what reaches the adjudicator and
        # otherwise says nothing, so the shape of what was actually retrieved
        # is reported beside the score rather than left to be inferred from a
        # missing match.
        "similarity": _distribution(similarities),
        "calibration_warning": _calibration_warning(decisions, similarities),
        "enabled": bool(config.SEMANTIC_RETRIEVAL),
        "available": embedding_cache.available() and bool(index and len(index)),
        "unavailable_reason": embedding_cache.last_error(),
        "indexed_spans": len(index) if index else 0,
        "requirements_retrieved": len(decisions),
        "bands": bands,
        "dropped_as_sibling": sum(d.dropped_as_sibling for d in decisions.values()),
        "thresholds": {"ignore": config.SEMANTIC_SIMILARITY_IGNORE,
                       "strong": config.SEMANTIC_SIMILARITY_STRONG,
                       "top_k": config.SEMANTIC_TOP_K},
    }
