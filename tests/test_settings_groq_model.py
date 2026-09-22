"""
Groq model selection accepts arbitrary ids, not just the built-in list.

Groq adds and retires models faster than the dropdown gets updated, so the
list can't be the only way in -- but the "Custom…" sentinel that makes the
free-text box work must never survive as far as GROQ_MODEL.
"""
from unittest.mock import patch

import pytest

import api
import config


@pytest.fixture
def no_env_writes(monkeypatch):
    """Never touch the real .env. The file holds live API keys, and a test
    that rewrites it would be editing the user's working configuration."""
    written = {}
    monkeypatch.setattr(api.env_store, "update_env_file", written.update)
    monkeypatch.setattr(config, "GROQ_API_KEY", "present-so-validation-passes")
    return written


def _body(**kw):
    return api.SettingsUpdate(llm_provider="groq", **kw)


def test_an_unlisted_model_id_is_accepted(no_env_writes):
    """The whole point: a model that shipped after this dropdown was written
    still has to be reachable."""
    with patch.object(api.importlib, "reload"):
        api.update_settings(_body(groq_model="llama-3.3-70b-versatile"))
    assert no_env_writes["GROQ_MODEL"] == "llama-3.3-70b-versatile"


def test_the_custom_placeholder_is_rejected(no_env_writes):
    """Saved as GROQ_MODEL this would 404 on every LLM call, naming a model
    that appears nowhere in the UI -- a genuinely awful thing to debug.
    Enforced server-side because the endpoint is reachable without the page."""
    with pytest.raises(Exception) as exc:
        api.update_settings(_body(groq_model="__custom__"))
    detail = str(getattr(exc.value, "detail", exc.value))
    assert "Custom" in detail
    assert "GROQ_MODEL" not in no_env_writes


def test_a_blank_model_leaves_the_saved_one_alone(no_env_writes):
    """Blank means 'keep what's there', consistent with how the API key fields
    behave on this endpoint."""
    with patch.object(api.importlib, "reload"):
        api.update_settings(_body(groq_model=None))
    assert "GROQ_MODEL" not in no_env_writes
