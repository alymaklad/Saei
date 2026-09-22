"""
Embeddings for short texts, cached on disk by content and model.

Phase 3 of the generic-matching plan, and the reason the semantic layer can
be affordable: a CV's spans change when the CV changes, which is rarely, while
job postings arrive every run. Embedding a span once and reusing the vector
across every job is what keeps the per-job cost at "no extra LLM call".

Two properties this module exists to guarantee:

**It never raises.** No embedding provider configured, Ollama not running,
Gemini out of quota, network down -- every one of those returns None for the
affected text and lets the caller fall back to the deterministic path. The
semantic layer is an improvement to recall, not a dependency of scoring; a
CV must still score when it is unavailable. `last_error()` reports why, so
the bench can show "retrieval unavailable" rather than "nothing matched".

**The key includes the model.** A vector from one model is meaningless
against another's -- different dimensionality at best, silently wrong
neighbours at worst. This project has already been bitten once by a cache key
that omitted the provider.
"""
import hashlib
import json
import os
import threading

import config
from agents import embeddings, skill_matching

_CACHE_PATH = os.path.join("data", "span_embedding_cache.json")
_LOCK = threading.Lock()
_MEMORY: dict[str, list[float]] | None = None
_LAST_ERROR: str | None = None
_DISABLED = False


def cache_key(text: str, model: str | None = None, role: str | None = None) -> str:
    """hash(normalised text) + model, per the plan's caching section.

    Normalised rather than raw so the two spellings a rewrite produces --
    "tool-calling" and "tool‑calling" -- share one vector instead of paying
    twice for the same sentence.

    Pass `role` for a text that has not been prefixed yet; the prefix is part
    of what is hashed, because with nomic-embed-text it is part of what is
    embedded.
    """
    if role:
        text = _with_prefix(text or "", role)
    normalised = skill_matching.normalize(text or "")
    digest = hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:16]
    return f"{model or embeddings.active_model()}::{digest}"


def _load() -> dict:
    global _MEMORY
    if _MEMORY is None:
        try:
            with open(_CACHE_PATH, encoding="utf-8") as handle:
                data = json.load(handle)
            _MEMORY = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _MEMORY = {}
    return _MEMORY


def _save() -> None:
    if _MEMORY is None:
        return
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH) or ".", exist_ok=True)
        with open(_CACHE_PATH, "w", encoding="utf-8") as handle:
            json.dump(_MEMORY, handle)
    except OSError:
        # A cache that cannot be written is a slower session, not a failure.
        pass


def last_error() -> str | None:
    """Why the most recent embedding attempt failed, or None."""
    return _LAST_ERROR


def available() -> bool:
    """Whether embedding is worth attempting at all.

    False once a call has failed in this process: with no provider reachable,
    retrying per requirement turns one clean degradation into a slow one.
    """
    return not _DISABLED


def reset() -> None:
    """Forget the in-process state. For tests and for a provider switch."""
    global _MEMORY, _LAST_ERROR, _DISABLED
    _MEMORY, _LAST_ERROR, _DISABLED = None, None, False


def embed_many(texts: list[str], role: str = "document") -> list[list[float] | None]:
    """Vectors for `texts`, in order, with None where embedding was impossible.

    Only the uncached texts reach the provider, and they go in one batched
    call -- the same rule the plan sets for LLM adjudication, for the same
    reason.

    `role` decides the task prefix. CV spans are the corpus ("document"),
    requirements are the query. With nomic-embed-text this is not decoration:
    the model card requires a task prefix on both sides, and the prefixed
    text is what gets hashed, so a span embedded as a document and the same
    sentence embedded as a query are correctly two different cache entries.
    """
    global _LAST_ERROR, _DISABLED
    texts = [_with_prefix(t or "", role) for t in texts]
    if not texts:
        return []

    cache = _load()
    keys = [cache_key(t) for t in texts]
    out: list[list[float] | None] = [cache.get(k) for k in keys]

    pending = [i for i, vector in enumerate(out)
               if vector is None and texts[i].strip()]
    if not pending or _DISABLED:
        return out

    try:
        vectors = embeddings.embed_texts([texts[i] for i in pending])
    except Exception as exc:  # noqa: BLE001 -- any provider failure degrades
        _LAST_ERROR = f"{type(exc).__name__}: {exc}"
        _DISABLED = True
        return out

    if len(vectors) != len(pending):
        _LAST_ERROR = (f"provider returned {len(vectors)} vectors for "
                       f"{len(pending)} texts")
        return out

    with _LOCK:
        for index, vector in zip(pending, vectors):
            if vector:
                out[index] = list(vector)
                cache[keys[index]] = list(vector)
        _save()
    _LAST_ERROR = None
    return out


def _with_prefix(text: str, role: str) -> str:
    """The text as the active model wants to see it for this role."""
    if role == "query":
        return embeddings.apply_query_instruction(text)
    return embeddings.apply_document_instruction(text)


def embed_one(text: str, role: str = "query") -> list[float] | None:
    """One text, cached the same way.

    Requirement text goes through here. A posting's requirements repeat
    across postings far more than you would expect ("REST APIs", "Python",
    "stakeholder management"), so the same cache serves both sides.
    """
    return embed_many([text or ""], role=role)[0]
