"""
Provider class resolution for the Ollama paths.

Background: langchain-community is being sunset upstream. Its ChatOllama and
OllamaEmbeddings were both deprecated in LangChain 0.3.1, but they are NOT in
the same state today -- community has already *removed* ChatOllama (verified
gone in langchain-community 0.4.2) while OllamaEmbeddings still exists there.
That asymmetry is why the two helpers behave differently, and these tests pin
it so a future dependency bump can't quietly reintroduce a broken import path.
"""
import builtins
import sys

import pytest

from agents import embeddings as emb
from agents import llm


def _hide(module_prefix):
    """Context-manager-ish helper: makes `import <module_prefix>...` fail,
    simulating an environment where that package isn't installed."""
    real_import = builtins.__import__
    removed = {name: sys.modules.pop(name) for name in list(sys.modules)
               if name.startswith(module_prefix)}

    def fake_import(name, *args, **kwargs):
        if name.startswith(module_prefix):
            raise ImportError(f"simulated: {module_prefix} not installed")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = fake_import
    return real_import, removed


def _restore(real_import, removed):
    builtins.__import__ = real_import
    sys.modules.update(removed)


# ---- happy path: both resolve to the modern package --------------------------

def test_embeddings_prefer_langchain_ollama():
    cls = emb._ollama_embeddings_class()
    assert cls.__module__.startswith("langchain_ollama")


def test_chat_prefers_langchain_ollama():
    cls = llm._chat_ollama_class()
    assert cls.__module__.startswith("langchain_ollama")


def test_ollama_chat_sets_an_explicit_context_window(monkeypatch):
    """Left unset, Ollama uses the model's own default (commonly 4096) and
    silently truncates longer prompts -- the CV-rewrite prompt carries a whole
    CV plus a whole job description, so a silent cut would be invisible
    corruption rather than an error."""
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(llm.config, "OLLAMA_NUM_CTX", 8192)
    client = llm.get_llm()
    assert client.num_ctx == 8192


def test_both_expose_the_same_constructor_arguments():
    """The swap is only a drop-in if the argument names match -- if a future
    version renames base_url, get_embedder/get_llm break at runtime."""
    assert {"model", "base_url"} <= set(emb._ollama_embeddings_class().model_fields)
    assert {"model", "base_url", "temperature"} <= set(llm._chat_ollama_class().model_fields)


# ---- degraded environments ---------------------------------------------------

def test_embeddings_fall_back_to_community_when_langchain_ollama_is_absent():
    """community still ships OllamaEmbeddings, so this fallback is real: an
    install that hasn't reinstalled requirements keeps working."""
    real_import, removed = _hide("langchain_ollama")
    try:
        cls = emb._ollama_embeddings_class()
        assert cls.__module__.startswith("langchain_community")
    finally:
        _restore(real_import, removed)


def test_chat_raises_an_actionable_error_when_langchain_ollama_is_absent():
    """There is NO fallback here -- community removed ChatOllama entirely, so
    the only useful thing to do is say so and name the fix. A bare ImportError
    traceback wouldn't tell the user to install anything."""
    real_import, removed = _hide("langchain_ollama")
    try:
        with pytest.raises(RuntimeError) as excinfo:
            llm._chat_ollama_class()
        message = str(excinfo.value)
        assert "langchain-ollama" in message
        assert "pip install" in message
    finally:
        _restore(real_import, removed)


# ---- OpenRouter --------------------------------------------------------------

def test_openrouter_requires_an_api_key(monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "openrouter")
    monkeypatch.setattr(llm.config, "OPENROUTER_API_KEY", "")
    with pytest.raises(RuntimeError) as excinfo:
        llm.get_llm()
    assert "OPENROUTER_API_KEY" in str(excinfo.value)


def test_openrouter_builds_with_the_configured_model(monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "openrouter")
    monkeypatch.setattr(llm.config, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(llm.config, "OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    client = llm.get_llm(temperature=0.0)
    assert client.model_name == "meta-llama/llama-3.3-70b-instruct:free"
    # Same uncapped-response guard as the Groq branch: a full CV plus a full
    # job description is a long prompt.
    assert client.max_tokens == 4096


def test_openrouter_caps_reasoning_for_gpt_oss_models(monkeypatch):
    """gpt-oss bills hidden reasoning as completion tokens whichever provider
    serves it -- measured on Groq, and the model is the same model."""
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "openrouter")
    monkeypatch.setattr(llm.config, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(llm.config, "OPENROUTER_MODEL", "openai/gpt-oss-120b:free")
    assert llm.get_llm().model_kwargs == {"reasoning_effort": "low"}


def test_openrouter_leaves_reasoning_alone_for_other_models(monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "openrouter")
    monkeypatch.setattr(llm.config, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(llm.config, "OPENROUTER_MODEL", "google/gemma-3-27b-it:free")
    assert not llm.get_llm().model_kwargs


def test_openrouter_missing_package_names_the_fix(monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_PROVIDER", "openrouter")
    monkeypatch.setattr(llm.config, "OPENROUTER_API_KEY", "test-key")
    real_import, removed = _hide("langchain_openrouter")
    try:
        with pytest.raises(RuntimeError) as excinfo:
            llm.get_llm()
        assert "pip install" in str(excinfo.value)
    finally:
        _restore(real_import, removed)


def test_community_really_has_dropped_chat_ollama():
    """Documents the upstream state this whole module works around. If a
    future langchain-community reinstates ChatOllama this test fails, which is
    the right prompt to revisit the RuntimeError above."""
    with pytest.raises(ImportError):
        from langchain_community.chat_models import ChatOllama  # noqa: F401
