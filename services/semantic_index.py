"""
A cosine index over one candidate's evidence spans.

Deliberately not a vector database. The plan calls for retrieving the top few
spans of ONE CV per requirement: that is tens of vectors, searched a handful
of times per job. FAISS, Chroma or pgvector would each add an install, a
process and a schema to maintain in exchange for making a 30-element loop
faster than it already is. The project's existing embeddings module makes the
same call for the same reason and says so.

What this does add over a bare loop is the part that would otherwise be
copy-pasted: vectors are L2-normalised once at build time so the search is a
dot product, missing vectors are dropped rather than scored as zero, and the
index carries the model it was built with so a provider switch cannot serve
stale neighbours.
"""
import math

from agents import embeddings as embeddings_module
from agents.matching_types import EvidenceSpan
from services import embedding_cache


def _unit(vector: list[float]) -> list[float] | None:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else None


class SpanIndex:
    """Built from spans, searched by vector. Empty is a valid state."""

    def __init__(self, model: str | None = None):
        self.model = model or embeddings_module.active_model()
        self._ids: list[str] = []
        self._spans: dict[str, EvidenceSpan] = {}
        self._vectors: list[list[float]] = []

    def __len__(self) -> int:
        return len(self._ids)

    @property
    def is_empty(self) -> bool:
        return not self._ids

    @classmethod
    def build(cls, spans, model: str | None = None) -> "SpanIndex":
        """Embed and index every span that can be embedded.

        A span whose vector is unavailable is simply absent from the index --
        retrieval then returns fewer candidates, which the caller already has
        to handle, rather than a zero-similarity row that looks like a
        considered judgement.
        """
        index = cls(model=model)
        spans = [s if isinstance(s, EvidenceSpan) else EvidenceSpan.from_dict(s)
                 for s in spans or []]
        spans = [s for s in spans if s.text.strip()]
        if not spans:
            return index

        vectors = embedding_cache.embed_many([s.text for s in spans])
        for span, vector in zip(spans, vectors):
            unit = _unit(vector) if vector else None
            if unit is None:
                continue
            span.embedding_id = embedding_cache.cache_key(
                span.text, index.model, role="document")
            index._ids.append(span.id)
            index._spans[span.id] = span
            index._vectors.append(unit)
        return index

    def search(self, query_vector, top_k: int = 5, minimum: float = 0.0):
        """(span, similarity) for the closest spans, best first.

        Similarity is cosine clamped to 0..1, the same scale the rest of this
        project reports scores on, so a threshold in config reads the same way
        as every other number there.
        """
        unit = _unit(list(query_vector or []))
        if unit is None or self.is_empty or len(unit) != len(self._vectors[0]):
            return []

        scored = []
        for span_id, vector in zip(self._ids, self._vectors):
            similarity = max(0.0, min(1.0, sum(a * b for a, b in zip(unit, vector))))
            if similarity >= minimum:
                scored.append((self._spans[span_id], similarity))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:max(1, top_k)]

    def span(self, span_id: str) -> EvidenceSpan | None:
        return self._spans.get(span_id)
