"""
Embeddings abstraction — CV <-> job-description semantic matching.

Deliberately a separate provider switch from agents/llm.py rather than
reusing LLM_PROVIDER: Groq (one of the three chat providers) has no
embeddings endpoint at all, so a user on LLM_PROVIDER=groq still needs an
independent choice here. config.EMBEDDING_PROVIDER is that choice.

    EMBEDDING_PROVIDER=ollama  -> local, free forever, no quota.
                                  Needs `ollama serve` running and a one-time
                                  `ollama pull nomic-embed-text`.
    EMBEDDING_PROVIDER=gemini  -> hosted free tier, needs GEMINI_API_KEY.
                                  Model defaults to gemini-embedding-001;
                                  text-embedding-004 and embedding-001 were
                                  both shut down (2026-01-14 / 2025-08-14) and
                                  now 404 from embedContent.

Cosine similarity is computed in plain Python rather than numpy: this app
compares one CV against tens-to-low-hundreds of jobs per run, which is
nowhere near the scale that justifies a vector database (FAISS/Chroma/etc.)
or even a numpy dependency the project doesn't otherwise have.
"""
import hashlib
import json
import math
import os
import re
import time

import config

# Gemini's free tier meters embeddings per MINUTE (quotaId
# EmbedContentRequestsPerMinutePerUserPerProjectPerModel-FreeTier, 100), and
# counts each content in a batch against it -- so a single run over a few
# hundred job descriptions trips it. The API tells us exactly how long to wait
# ("Please retry in 18.049973242s" / retryDelay: '18s'), so honor that rather
# than guessing a backoff curve or giving up on the whole semantic stage.
_RATE_LIMIT_MARKERS = ("RESOURCE_EXHAUSTED", "429", "rate limit", "quota")
_RETRY_DELAY_RE = re.compile(r"retry in ([\d.]+)\s*s|'retryDelay':\s*'(\d+)s'", re.I)
_MAX_RATE_LIMIT_RETRIES = 4
_FALLBACK_RETRY_SECONDS = 20.0

# Where the current CV's embedding is cached, so it isn't recomputed on every
# run (and every job comparison) for a CV that hasn't changed. Keyed by a hash
# of the CV *text*, so replacing the CV file -- or re-uploading a different
# one -- invalidates it automatically without needing an explicit "clear
# cache" step anywhere.
_CV_EMBEDDING_CACHE_PATH = os.path.join("data", "cv_embedding_cache.json")


def active_model() -> str:
    """The embedding model name for the currently selected provider."""
    if config.EMBEDDING_PROVIDER == "gemini":
        # The Gemini REST path is models/<name>:embedContent, and both the
        # bare and prefixed forms are accepted -- normalize so a user who
        # copied either shape out of the docs gets the same behavior (and the
        # same cache key).
        return config.GEMINI_EMBEDDING_MODEL.removeprefix("models/")
    return config.OLLAMA_EMBEDDING_MODEL


def get_embedder():
    """Returns a LangChain embeddings object with .embed_query/.embed_documents.

    Mirrors agents/llm.py::get_llm's provider-branching shape so both are
    read the same way, and so adding a provider means touching one function.
    """
    if config.EMBEDDING_PROVIDER == "gemini":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        if not config.GEMINI_API_KEY:
            raise RuntimeError(
                "EMBEDDING_PROVIDER=gemini but GEMINI_API_KEY is not set in .env"
            )
        return GoogleGenerativeAIEmbeddings(
            model=active_model(),
            google_api_key=config.GEMINI_API_KEY,
        )

    # default: ollama (local, free)
    return _ollama_embeddings_class()(
        model=active_model(),
        base_url=config.OLLAMA_BASE_URL,
    )


def _ollama_embeddings_class():
    """Prefers the dedicated langchain-ollama package, falling back to the
    langchain-community class.

    langchain-community is being sunset, and its OllamaEmbeddings was
    deprecated in LangChain 0.3.1. Unlike ChatOllama -- which community has
    already REMOVED, see agents/llm.py::_chat_ollama_class -- this one still
    exists there, so the fallback is real rather than theoretical and keeps
    an install that hasn't reinstalled requirements working (with a warning)
    instead of failing outright. Identical `model`/`base_url` arguments in
    both, so it's a drop-in either way.
    """
    try:
        from langchain_ollama import OllamaEmbeddings
    except ImportError:
        from langchain_community.embeddings import OllamaEmbeddings
        _warn_fallback_once()
    return OllamaEmbeddings


_fallback_warned = False


def _warn_fallback_once():
    """Says plainly what LangChain's own deprecation warning does not: which
    environment is missing the package, and how to fix it.

    Worth the noise because the failure is silent otherwise -- the fallback
    works, so the only symptom is a third-party warning pointing at a line
    number inside this file, which reads like a bug here rather than a
    missing install. On this project that specifically happens after
    launching with run_conda_quick.bat, which skips dependency sync by design.
    """
    global _fallback_warned
    if _fallback_warned:
        return
    _fallback_warned = True
    import sys
    print(
        "[embeddings] langchain-ollama is not installed in this environment "
        f"({sys.prefix}) -- falling back to the deprecated langchain-community "
        "class. Fix: run run_conda.bat (not run_conda_quick.bat, which skips "
        "dependency sync), or `pip install langchain-ollama` in this env.",
        file=sys.stderr,
    )


def _is_rate_limit(exc: Exception) -> bool:
    message = str(exc)
    return any(marker.lower() in message.lower() for marker in _RATE_LIMIT_MARKERS)


def _retry_delay_seconds(exc: Exception) -> float:
    """The wait the API itself asked for, or a conservative fallback."""
    match = _RETRY_DELAY_RE.search(str(exc))
    if match:
        raw = match.group(1) or match.group(2)
        try:
            # +1s of headroom: retrying at the exact boundary tends to race
            # the server's own window accounting and trip a second 429.
            return float(raw) + 1.0
        except (TypeError, ValueError):
            pass
    return _FALLBACK_RETRY_SECONDS


def _with_rate_limit_retry(call, description: str):
    """Runs `call`, waiting and retrying on a provider rate-limit error.

    Bounded: after _MAX_RATE_LIMIT_RETRIES the exception propagates, so a
    genuinely exhausted DAILY quota surfaces as a real failure (and the
    caller degrades to no semantic path) instead of stalling a search run
    forever on a limit that won't clear.
    """
    for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 -- provider SDKs wrap their own types
            if not _is_rate_limit(exc) or attempt == _MAX_RATE_LIMIT_RETRIES:
                raise
            delay = _retry_delay_seconds(exc)
            print(f"[embeddings] rate limited on {description}; waiting {delay:.0f}s "
                  f"(attempt {attempt + 1}/{_MAX_RATE_LIMIT_RETRIES})")
            time.sleep(delay)
    return None  # unreachable -- the loop either returns or raises


# Query/document prefixing, per model family.
#
# Two families are in use here and they want completely different things:
#
#   qwen3-embedding   an INSTRUCTION on the query side describing the
#                     retrieval task; documents bare. Asymmetric by design.
#                     ("Instruct: {task}" then "Query:{text}")
#
#   nomic-embed-text  a TASK PREFIX on BOTH sides, and they are not optional.
#                     The model card is explicit: "the text prompt *must*
#                     include a task instruction prefix". Queries take
#                     "search_query: " and corpus text "search_document: ";
#                     embedding either one bare is an unsupported use of the
#                     model, and the similarities come back compressed and
#                     less separable as a result.
#
# Getting this wrong is invisible -- no error, no warning, just quietly worse
# retrieval -- which is why it lives in a table rather than in an if at a call
# site.
_PREFIX_SCHEMES = {
    "qwen3-embedding": {"query": "instruction", "document": None},
    "nomic-embed-text": {"query": "search_query: ", "document": "search_document: "},
    "mxbai-embed-large": {"query": "Represent this sentence for searching relevant passages: ",
                          "document": None},
}


def _scheme() -> dict:
    model = active_model().lower()
    for family, scheme in _PREFIX_SCHEMES.items():
        if family in model:
            return scheme
    return {"query": None, "document": None}


def supports_instruction_prefix() -> bool:
    """Whether the active model wants a task INSTRUCTION on the query side.

    Narrower than "has a prefix scheme": this is specifically the Qwen-style
    form, the only one that interpolates config.EMBEDDING_QUERY_INSTRUCTION.
    nomic's prefixes are fixed strings the model was trained on, not wording
    for a user to choose.
    """
    return _scheme().get("query") == "instruction"


def prefixes_documents() -> bool:
    """Whether the corpus side needs a prefix too. True for nomic."""
    return bool(_scheme().get("document"))


def apply_query_instruction(text: str) -> str:
    """Wrap a QUERY the way the active model expects.

    Pure string formatting, which is why this whole feature needs no
    transformers/torch dependency: the prefix is built here and the result
    goes to the provider as ordinary text.
    """
    query = _scheme().get("query")
    if not query:
        return text
    if query == "instruction":
        if not config.EMBEDDING_QUERY_INSTRUCTION:
            return text
        return "Instruct: " + config.EMBEDDING_QUERY_INSTRUCTION + "\nQuery:" + text
    return query + text


def apply_document_instruction(text: str) -> str:
    """Wrap a CORPUS text the way the active model expects.

    Bare for Qwen and for anything with no scheme -- there the asymmetry is
    the point. "search_document: " for nomic, where it is required.
    """
    document = _scheme().get("document")
    return document + text if document else text


def embed_text(text: str) -> list[float]:
    embedder = get_embedder()
    return _with_rate_limit_retry(lambda: embedder.embed_query(text or ""), "embed_query")


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch form -- one provider round-trip per chunk instead of one per
    document, which matters when scoring a whole board's worth of job
    descriptions in a single run.

    Chunked at config.EMBEDDING_BATCH_SIZE so a rate-limit retry only has to
    redo one chunk rather than the entire set, and so a partial result is
    still usable if a later chunk fails outright.
    """
    if not texts:
        return []
    embedder = get_embedder()
    cleaned = [t or "" for t in texts]
    size = max(1, config.EMBEDDING_BATCH_SIZE)

    out: list[list[float]] = []
    for start in range(0, len(cleaned), size):
        chunk = cleaned[start:start + size]
        out.extend(_with_rate_limit_retry(
            lambda c=chunk: embedder.embed_documents(c),
            f"embed_documents[{start}:{start + len(chunk)}]",
        ))
    return out


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Standard cosine similarity, clamped to 0..1.

    Raw cosine ranges -1..1, but the embedding models used here produce
    non-negative similarity for real text in practice; clamping keeps the
    value directly comparable to the other 0..1 scores in this project
    (ATS score, match score) instead of introducing a second scale.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _cache_key(text: str) -> str:
    """Cache identity = the candidate's text + provider + model.

    The provider/model MUST be part of this. Different models emit different
    dimensionalities (nomic-embed-text is 768, gemini-embedding-001 is 3072),
    and cosine_similarity returns 0.0 on a length mismatch rather than
    raising -- so a cache keyed on CV text alone would, after any provider or
    model change, silently score every job 0.0 on the semantic factor and
    quietly rescue nothing, with no error anywhere to explain why.
    """
    # The instruction prefix changes the vector too, so it's part of the key:
    # editing EMBEDDING_QUERY_INSTRUCTION must invalidate a cached CV vector
    # built under the old wording.
    # The prefix changes the vector too, so it is part of the key: editing
    # EMBEDDING_QUERY_INSTRUCTION, or moving to a model with a different
    # scheme, must invalidate a CV vector built under the old one.
    instruction = apply_query_instruction("")
    return f"{_content_hash(text)}:{config.EMBEDDING_PROVIDER}:{active_model()}:{_content_hash(instruction)[:8]}"


def _read_cv_cache() -> dict:
    if not os.path.exists(_CV_EMBEDDING_CACHE_PATH):
        return {}
    try:
        with open(_CV_EMBEDDING_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}  # corrupt/unreadable cache is not fatal -- just recompute


def _write_cv_cache(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_CV_EMBEDDING_CACHE_PATH), exist_ok=True)
        with open(_CV_EMBEDDING_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError:
        pass  # a failed cache write must never break a search run


def embed_candidate(text: str, use_cache: bool = True) -> list[float]:
    """The candidate's embedding, computed once per distinct text and reused.

    Callers pass the user's PROFILE rendered to text, not the uploaded file's
    text: the profile is the record they maintain, and it is free of the
    headers, footers and extraction noise a PDF drags in -- both of which make
    it the better query vector. Keying the cache on that content is what makes
    an edit on the Profile page actually change the vector.

    Only ever stores ONE entry rather than accumulating every text ever
    embedded -- old entries are dead weight the moment the profile changes,
    and this file is rewritten wholesale anyway.
    """
    key = _cache_key(text)
    if use_cache:
        cached = _read_cv_cache()
        if cached.get("hash") == key and isinstance(cached.get("embedding"), list):
            return cached["embedding"]

    # The CV is the QUERY side of this comparison -- we're retrieving job
    # postings that match it -- so it gets the instruction prefix, while job
    # descriptions (the corpus) are embedded bare in embed_texts().
    embedding = embed_text(apply_query_instruction(text))

    if use_cache:
        _write_cv_cache({
            "hash": key,
            "provider": config.EMBEDDING_PROVIDER,
            "model": active_model(),
            "dimensions": len(embedding),
            "embedding": embedding,
        })
    return embedding
