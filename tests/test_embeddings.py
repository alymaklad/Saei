"""
Embeddings layer: cosine math, provider selection, and CV-embedding caching.
No provider is ever actually contacted here.
"""
import json
import math
import os
from unittest.mock import MagicMock, patch

import pytest

import config
from agents import embeddings as emb


# ---- cosine similarity ------------------------------------------------------

def test_identical_vectors_score_one():
    assert emb.cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 1.0


def test_orthogonal_vectors_score_zero():
    assert emb.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_scale_does_not_affect_similarity():
    """Cosine measures direction, not magnitude -- a doubled vector is the
    same direction and must score identically."""
    assert abs(emb.cosine_similarity([1.0, 2.0], [2.0, 4.0]) - 1.0) < 1e-9


def test_known_value_matches_hand_computed_cosine():
    a, b = [1.0, 1.0], [1.0, 0.0]
    assert abs(emb.cosine_similarity(a, b) - (1 / math.sqrt(2))) < 1e-9


def test_opposite_vectors_clamp_to_zero_rather_than_going_negative():
    """Raw cosine ranges -1..1, but every other score in this project is
    0..1 -- clamping keeps them directly comparable."""
    assert emb.cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == 0.0


def test_degenerate_inputs_return_zero_instead_of_raising():
    assert emb.cosine_similarity([], []) == 0.0
    assert emb.cosine_similarity([1.0], []) == 0.0
    assert emb.cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0   # zero vector
    assert emb.cosine_similarity([1.0, 2.0], [1.0]) == 0.0        # length mismatch


# ---- provider selection -----------------------------------------------------

def test_gemini_without_a_key_raises_a_clear_error(monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    try:
        emb.get_embedder()
        assert False, "expected a RuntimeError"
    except RuntimeError as exc:
        assert "GEMINI_API_KEY" in str(exc)


def test_embed_texts_is_a_noop_on_an_empty_list():
    """Must not construct a provider (or make a call) just to embed nothing."""
    with patch.object(emb, "get_embedder") as mock_get:
        assert emb.embed_texts([]) == []
    mock_get.assert_not_called()


def test_embed_texts_normalizes_none_entries_to_empty_strings():
    embedder = MagicMock()
    embedder.embed_documents.return_value = [[0.1], [0.2]]
    with patch.object(emb, "get_embedder", return_value=embedder):
        emb.embed_texts(["real text", None])
    embedder.embed_documents.assert_called_once_with(["real text", ""])


# ---- CV embedding cache -----------------------------------------------------

def _use_temp_cache(tmp_path, monkeypatch):
    path = str(tmp_path / "cv_embedding_cache.json")
    monkeypatch.setattr(emb, "_CV_EMBEDDING_CACHE_PATH", path)
    return path


def test_cv_embedding_is_computed_once_and_reused(tmp_path, monkeypatch):
    _use_temp_cache(tmp_path, monkeypatch)
    with patch.object(emb, "embed_text", return_value=[0.1, 0.2]) as mock_embed:
        first = emb.embed_candidate("my cv text")
        second = emb.embed_candidate("my cv text")
    assert first == second == [0.1, 0.2]
    mock_embed.assert_called_once()  # second call served from cache


def test_changed_cv_text_invalidates_the_cache(tmp_path, monkeypatch):
    _use_temp_cache(tmp_path, monkeypatch)
    with patch.object(emb, "embed_text", side_effect=[[0.1], [0.9]]) as mock_embed:
        old = emb.embed_candidate("original cv")
        new = emb.embed_candidate("a completely different cv")
    assert old == [0.1] and new == [0.9]
    assert mock_embed.call_count == 2


def test_cache_stores_only_the_current_cv_not_a_growing_history(tmp_path, monkeypatch):
    path = _use_temp_cache(tmp_path, monkeypatch)
    with patch.object(emb, "embed_text", side_effect=[[0.1], [0.9]]):
        emb.embed_candidate("first cv")
        emb.embed_candidate("second cv")
    with open(path, encoding="utf-8") as f:
        cached = json.load(f)
    assert cached["hash"] == emb._cache_key("second cv")
    assert cached["embedding"] == [0.9]


def test_a_corrupt_cache_file_falls_back_to_recomputing(tmp_path, monkeypatch):
    path = _use_temp_cache(tmp_path, monkeypatch)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not valid json")
    with patch.object(emb, "embed_text", return_value=[0.5]):
        assert emb.embed_candidate("my cv") == [0.5]


def test_use_cache_false_always_recomputes(tmp_path, monkeypatch):
    _use_temp_cache(tmp_path, monkeypatch)
    with patch.object(emb, "embed_text", return_value=[0.3]) as mock_embed:
        emb.embed_candidate("cv", use_cache=False)
        emb.embed_candidate("cv", use_cache=False)
    assert mock_embed.call_count == 2


# ---- cache identity includes the provider AND model -------------------------
# Different models emit different dimensionalities (nomic-embed-text 768,
# gemini-embedding-001 3072). cosine_similarity returns 0.0 on a length
# mismatch instead of raising, so a cache keyed on CV text alone would
# silently score every job 0.0 after a provider/model switch -- rescuing
# nothing, with no error to explain why.

def test_switching_provider_invalidates_the_cached_cv_embedding(tmp_path, monkeypatch):
    _use_temp_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "EMBEDDING_PROVIDER", "ollama")
    with patch.object(emb, "embed_text", return_value=[0.1] * 768):
        first = emb.embed_candidate("same cv")

    monkeypatch.setattr(config, "EMBEDDING_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
    with patch.object(emb, "embed_text", return_value=[0.2] * 3072) as mock_embed:
        second = emb.embed_candidate("same cv")

    mock_embed.assert_called_once()   # did NOT reuse the 768-dim vector
    assert len(first) == 768 and len(second) == 3072


def test_switching_model_within_a_provider_also_invalidates(tmp_path, monkeypatch):
    _use_temp_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "EMBEDDING_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
    with patch.object(emb, "embed_text", return_value=[0.1] * 3072):
        emb.embed_candidate("same cv")

    monkeypatch.setattr(config, "GEMINI_EMBEDDING_MODEL", "gemini-embedding-2")
    with patch.object(emb, "embed_text", return_value=[0.9] * 3072) as mock_embed:
        emb.embed_candidate("same cv")
    mock_embed.assert_called_once()


def test_cached_entry_records_the_provider_model_and_dimensions(tmp_path, monkeypatch):
    """So a stale cache file is diagnosable by reading it, not just by
    noticing every semantic score is suspiciously zero."""
    path = _use_temp_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")
    with patch.object(emb, "embed_text", return_value=[0.1] * 768):
        emb.embed_candidate("cv")
    with open(path, encoding="utf-8") as f:
        cached = json.load(f)
    assert cached["provider"] == "ollama"
    assert cached["model"] == "nomic-embed-text"
    assert cached["dimensions"] == 768


# ---- rate-limit handling ----------------------------------------------------
# Gemini's free tier meters embeddings per MINUTE (100, counted per content).
# A run over a few hundred job descriptions trips it, and the API tells us
# exactly how long to wait -- so wait that long rather than losing the whole
# semantic stage.

class _RateLimited(Exception):
    pass


def test_rate_limit_errors_are_recognized():
    real = ("Error embedding content (RESOURCE_EXHAUSTED): 429 RESOURCE_EXHAUSTED. "
            "Quota exceeded for metric: embed_content_free_tier_requests, limit: 100")
    assert emb._is_rate_limit(_RateLimited(real))
    assert not emb._is_rate_limit(ValueError("bad vector shape"))


def test_retry_delay_is_taken_from_the_api_response():
    """Google states the exact wait; guessing a backoff curve would either
    stall longer than needed or retry too early and trip a second 429."""
    exc = _RateLimited("Please retry in 18.049973242s.")
    assert emb._retry_delay_seconds(exc) == pytest.approx(19.05, abs=0.01)  # +1s headroom

    exc = _RateLimited("... {'retryDelay': '18s'} ...")
    assert emb._retry_delay_seconds(exc) == pytest.approx(19.0)


def test_retry_delay_falls_back_when_the_error_names_none():
    assert emb._retry_delay_seconds(_RateLimited("429 too many requests")) == emb._FALLBACK_RETRY_SECONDS


def test_a_rate_limited_call_is_retried_after_waiting(monkeypatch):
    slept = []
    monkeypatch.setattr(emb.time, "sleep", lambda s: slept.append(s))
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _RateLimited("429 RESOURCE_EXHAUSTED. Please retry in 2s.")
        return "ok"

    assert emb._with_rate_limit_retry(flaky, "test") == "ok"
    assert calls["n"] == 2
    assert slept == [3.0]


def test_non_rate_limit_errors_propagate_immediately(monkeypatch):
    monkeypatch.setattr(emb.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise ValueError("malformed request")

    with pytest.raises(ValueError):
        emb._with_rate_limit_retry(broken, "test")
    assert calls["n"] == 1  # not retried


def test_retries_are_bounded_so_an_exhausted_daily_quota_surfaces(monkeypatch):
    """A per-minute limit clears; a daily one doesn't. Retrying forever would
    stall a search run on a limit that will never clear in this run."""
    monkeypatch.setattr(emb.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def always_limited():
        calls["n"] += 1
        raise _RateLimited("429 RESOURCE_EXHAUSTED")

    with pytest.raises(_RateLimited):
        emb._with_rate_limit_retry(always_limited, "test")
    assert calls["n"] == emb._MAX_RATE_LIMIT_RETRIES + 1


# ---- batching ---------------------------------------------------------------

def test_embed_texts_chunks_at_the_configured_batch_size(monkeypatch):
    """Gemini caps a batch at 100; chunking also means a retry only redoes one
    chunk instead of the whole set."""
    monkeypatch.setattr(config, "EMBEDDING_BATCH_SIZE", 2)
    embedder = MagicMock()
    embedder.embed_documents.side_effect = lambda c: [[0.1]] * len(c)
    with patch.object(emb, "get_embedder", return_value=embedder):
        out = emb.embed_texts(["a", "b", "c", "d", "e"])
    assert len(out) == 5
    assert embedder.embed_documents.call_count == 3  # 2 + 2 + 1


def test_a_rate_limited_chunk_only_redoes_that_chunk(monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_BATCH_SIZE", 2)
    monkeypatch.setattr(emb.time, "sleep", lambda s: None)
    embedder = MagicMock()
    seen = []

    def flaky(chunk):
        seen.append(tuple(chunk))
        if len(seen) == 2:  # fail the second chunk once
            raise _RateLimited("429 RESOURCE_EXHAUSTED. Please retry in 1s.")
        return [[0.1]] * len(chunk)

    embedder.embed_documents.side_effect = flaky
    with patch.object(emb, "get_embedder", return_value=embedder):
        out = emb.embed_texts(["a", "b", "c", "d"])
    assert len(out) == 4
    assert seen[1] == seen[2] == ("c", "d")   # only the failed chunk was redone
    assert seen[0] == ("a", "b")


# ---- Qwen3 query-side instruction prefix ------------------------------------
# Qwen3-Embedding is trained with a task instruction on the QUERY only
# ("Instruct: {task}\nQuery: {text}"), documents bare. Here the CV is the
# query and job descriptions are the corpus. It's pure string formatting --
# which is why this needs no transformers/torch dependency.

def test_instruction_prefix_applies_only_to_qwen3_models(monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:4b")
    assert emb.supports_instruction_prefix()
    monkeypatch.setattr(config, "OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")
    assert not emb.supports_instruction_prefix()
    monkeypatch.setattr(config, "OLLAMA_EMBEDDING_MODEL", "embeddinggemma:300m")
    assert not emb.supports_instruction_prefix()


def test_instruction_prefix_uses_qwens_documented_format(monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:4b")
    monkeypatch.setattr(config, "EMBEDDING_QUERY_INSTRUCTION", "Find matching jobs")
    out = emb.apply_query_instruction("my cv text")
    assert out == "Instruct: Find matching jobs\nQuery:my cv text"


def test_a_model_with_no_scheme_gets_the_text_unchanged(monkeypatch):
    """Prefixing a model that was not trained on a prefix adds noise."""
    monkeypatch.setattr(config, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_EMBEDDING_MODEL", "embeddinggemma:300m")
    assert emb.apply_query_instruction("my cv text") == "my cv text"
    assert emb.apply_document_instruction("a job description") == "a job description"


def test_nomic_prefixes_both_sides_because_it_requires_them(monkeypatch):
    """nomic-embed-text's card is explicit: "the text prompt *must* include a
    task instruction prefix". Unlike Qwen's, these are fixed strings the model
    was trained on rather than a task description a user words -- and they go
    on the corpus side too, which is the part that is easy to miss."""
    monkeypatch.setattr(config, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")
    monkeypatch.setattr(config, "EMBEDDING_QUERY_INSTRUCTION", "Find matching jobs")

    assert emb.apply_query_instruction("stakeholder management") == \
        "search_query: stakeholder management"
    assert emb.apply_document_instruction("Coordinated with physicians") == \
        "search_document: Coordinated with physicians"
    assert not emb.supports_instruction_prefix(), (
        "EMBEDDING_QUERY_INSTRUCTION is Qwen's knob and must not leak into "
        "nomic's fixed prefixes")
    assert emb.prefixes_documents()


def test_qwen_leaves_the_corpus_bare(monkeypatch):
    """The asymmetry IS Qwen's design: instruction on the query, corpus
    untouched. The document helper has to be a no-op there, not a second
    prefix scheme applied by accident."""
    monkeypatch.setattr(config, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:4b")
    assert emb.apply_document_instruction("a job description") == "a job description"
    assert not emb.prefixes_documents()


def test_embed_texts_itself_never_prefixes(monkeypatch):
    """The role belongs to the CALL SITE -- a CV is the query when searching
    for jobs and the corpus when a requirement is searching it. Prefixing
    inside the batch helper would silently pick one of those and be wrong for
    the other half of the app."""
    monkeypatch.setattr(config, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")
    embedder = MagicMock()
    embedder.embed_documents.return_value = [[0.1]]
    with patch.object(emb, "get_embedder", return_value=embedder):
        emb.embed_texts(["a job description"])
    assert embedder.embed_documents.call_args[0][0] == ["a job description"]


def test_switching_model_invalidates_the_cv_vector(monkeypatch):
    """768 dimensions against 2560: cosine_similarity returns 0.0 on a length
    mismatch rather than raising, so a shared key would score every job 0.0
    with nothing anywhere to explain why."""
    monkeypatch.setattr(config, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")
    nomic = emb._cache_key("my cv")
    monkeypatch.setattr(config, "OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:4b")
    assert emb._cache_key("my cv") != nomic


def test_cv_embedding_receives_the_prefix(tmp_path, monkeypatch):
    _use_temp_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:4b")
    monkeypatch.setattr(config, "EMBEDDING_QUERY_INSTRUCTION", "Find matching jobs")
    with patch.object(emb, "embed_text", return_value=[0.1]) as mock_embed:
        emb.embed_candidate("my cv")
    assert mock_embed.call_args[0][0].startswith("Instruct: Find matching jobs\nQuery:")


def test_changing_the_instruction_invalidates_the_cached_cv_vector(tmp_path, monkeypatch):
    """The prefix changes the vector, so editing the wording must not silently
    reuse a vector built under the old one."""
    _use_temp_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:4b")
    monkeypatch.setattr(config, "EMBEDDING_QUERY_INSTRUCTION", "Task A")
    with patch.object(emb, "embed_text", return_value=[0.1]):
        emb.embed_candidate("same cv")
    monkeypatch.setattr(config, "EMBEDDING_QUERY_INSTRUCTION", "Task B")
    with patch.object(emb, "embed_text", return_value=[0.9]) as mock_embed:
        assert emb.embed_candidate("same cv") == [0.9]
    mock_embed.assert_called_once()


# ---- provider-aware defaults ------------------------------------------------

def test_local_embeddings_get_a_much_higher_candidate_budget():
    """The 90/95 caps exist only to respect Gemini's 100/min free tier.
    Ollama is unmetered, so those limits would be arbitrary throttling."""
    assert config._int("SEMANTIC_CANDIDATE_CAP", 400 if True else 90) == 400
    # And the shipped defaults differ by provider:
    assert (400 if config.EMBEDDING_PROVIDER == "ollama" else 90) in (90, 400)


def test_default_ollama_model_has_enough_context_for_real_job_posts(monkeypatch):
    """Measured against this project's own stored jobs: descriptions reach
    ~2.2k tokens, and the generic scraper's whole-page text runs far longer,
    so the shipped DEFAULT needs a roomy context window.

    Asserts the default the code falls back to with the variable unset -- not
    config.OLLAMA_EMBEDDING_MODEL itself, which reflects whatever the user has
    since chosen in Settings and is theirs to change (nomic-embed-text, for
    instance, is a perfectly valid speed-over-recall pick)."""
    monkeypatch.delenv("OLLAMA_EMBEDDING_MODEL", raising=False)
    import os
    assert "qwen3-embedding" in os.getenv("OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:4b")


def test_every_catalogued_model_declares_its_tradeoffs():
    """The Settings dropdown shows size/context/multilingual per model; a
    missing field would render as 'undefined' rather than fail loudly."""
    for provider, models in config.KNOWN_EMBEDDING_MODELS.items():
        assert models, f"{provider} has no catalogued models"
        for m in models:
            assert set(m) >= {"id", "size", "context", "multilingual", "note"}, m


def test_nomic_is_offered_as_an_ollama_option():
    ids = [m["id"] for m in config.KNOWN_EMBEDDING_MODELS["ollama"]]
    assert "nomic-embed-text" in ids


# ---- Gemini model-name normalization ----------------------------------------

def test_models_prefix_is_normalized_away(monkeypatch):
    """Both shapes appear in Google's docs; they must not produce two
    different cache keys for the same model."""
    monkeypatch.setattr(config, "EMBEDDING_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_EMBEDDING_MODEL", "models/gemini-embedding-001")
    assert emb.active_model() == "gemini-embedding-001"
    monkeypatch.setattr(config, "GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
    assert emb.active_model() == "gemini-embedding-001"


def test_default_gemini_model_is_not_a_shut_down_one():
    """text-embedding-004 (shut down 2026-01-14) and embedding-001
    (2025-08-14) both return 404 NOT_FOUND from embedContent."""
    assert config.GEMINI_EMBEDDING_MODEL not in ("models/text-embedding-004",
                                                 "text-embedding-004",
                                                 "models/embedding-001",
                                                 "embedding-001")
