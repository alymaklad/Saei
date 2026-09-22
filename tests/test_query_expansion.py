"""
Query expansion — turning one typed Position into the set of equivalent role
titles. Every LLM call is mocked; nothing here hits a network.
"""
from unittest.mock import MagicMock, patch

import config
from agents import query_expansion_agent as qe


def _llm_returning(content: str):
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content=content)
    return llm


# ---- tolerant JSON parsing (same failure modes ats_agent guards against) ----

def test_safe_json_list_parses_clean_array():
    assert qe._safe_json_list('["AI Engineer", "ML Engineer"]') == ["AI Engineer", "ML Engineer"]


def test_safe_json_list_extracts_array_wrapped_in_prose_or_a_fence():
    raw = 'Sure! Here you go:\n```json\n["AI Engineer", "ML Engineer"]\n```\nHope that helps.'
    assert qe._safe_json_list(raw) == ["AI Engineer", "ML Engineer"]


def test_safe_json_list_returns_empty_on_garbage():
    assert qe._safe_json_list("I could not do that") == []


def test_safe_json_list_drops_blank_entries():
    assert qe._safe_json_list('["AI Engineer", "", "   "]') == ["AI Engineer"]


# ---- generate_target_roles --------------------------------------------------

def test_original_position_is_always_first_and_never_dropped():
    """The user's own wording must survive expansion -- expansion may only
    widen a search, never redirect it to the model's paraphrases."""
    with patch.object(qe, "get_llm", return_value=_llm_returning('["Machine Learning Engineer", "ML Engineer"]')):
        roles = qe.generate_target_roles("AI Engineer")
    assert roles[0] == "AI Engineer"
    assert "Machine Learning Engineer" in roles


def test_duplicates_are_removed_case_insensitively():
    with patch.object(qe, "get_llm", return_value=_llm_returning('["ai engineer", "AI Engineer", "ML Engineer"]')):
        roles = qe.generate_target_roles("AI Engineer")
    assert roles == ["AI Engineer", "ML Engineer"]


def test_blank_position_expands_to_nothing():
    assert qe.generate_target_roles("") == []
    assert qe.generate_target_roles("   ") == []


def test_llm_failure_degrades_to_the_typed_position():
    """An unreachable/misconfigured/rate-limited LLM must not fail the whole
    search run -- it degrades to exactly the pre-expansion behavior."""
    broken = MagicMock()
    broken.invoke.side_effect = RuntimeError("connection refused")
    with patch.object(qe, "get_llm", return_value=broken):
        assert qe.generate_target_roles("AI Engineer") == ["AI Engineer"]


def test_unparseable_llm_output_degrades_to_the_typed_position():
    with patch.object(qe, "get_llm", return_value=_llm_returning("I'm not sure what you mean.")):
        assert qe.generate_target_roles("AI Engineer") == ["AI Engineer"]


def test_respects_max_roles_limit():
    many = '["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]'
    with patch.object(qe, "get_llm", return_value=_llm_returning(many)):
        roles = qe.generate_target_roles("AI Engineer", max_roles=3)
    # original + at most `max_roles` expansions
    assert len(roles) <= 4
    assert roles[0] == "AI Engineer"


def test_the_candidate_text_is_included_in_the_prompt_when_provided():
    """The expansion is CV-aware by design -- verify the CV actually reaches
    the model rather than the position alone."""
    llm = _llm_returning('["ML Engineer"]')
    with patch.object(qe, "get_llm", return_value=llm):
        qe.generate_target_roles("AI Engineer", candidate_text="PyTorch, LangGraph, agent orchestration")
    human_message = llm.invoke.call_args[0][0][1].content
    assert "PyTorch" in human_message


def test_the_whole_candidate_text_is_sent_by_default(monkeypatch):
    """0 means no cap. A cap was silently dropping the tail of a real CV --
    the project descriptions that say what the candidate actually builds,
    which is exactly what the role list should be calibrated to."""
    monkeypatch.setattr(config, "QUERY_EXPANSION_CV_CHARS", 0)
    cv = "START " + ("x" * 20000) + " END-OF-CV-MARKER"
    llm = _llm_returning('["ML Engineer"]')
    with patch.object(qe, "get_llm", return_value=llm):
        qe.generate_target_roles("AI Engineer", candidate_text=cv)
    human = llm.invoke.call_args[0][0][1].content
    assert "END-OF-CV-MARKER" in human
    assert "(excerpt)" not in human          # nothing was cut, so don't claim it was


def test_a_positive_cap_truncates_and_says_so(monkeypatch):
    monkeypatch.setattr(config, "QUERY_EXPANSION_CV_CHARS", 50)
    cv = "START " + ("x" * 500) + " END-OF-CV-MARKER"
    llm = _llm_returning('["ML Engineer"]')
    with patch.object(qe, "get_llm", return_value=llm):
        qe.generate_target_roles("AI Engineer", candidate_text=cv)
    human = llm.invoke.call_args[0][0][1].content
    assert "END-OF-CV-MARKER" not in human
    assert "(excerpt)" in human              # label is honest about the cut


# ---- caching layer ----------------------------------------------------------

def test_expansion_disabled_returns_the_literal_position(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION", False)
    # No LLM patch needed: if this tried to call one it would fail loudly.
    assert qe.get_target_roles("AI Engineer", candidate_text="whatever") == ["AI Engineer"]


def test_caching_is_off_by_default_so_every_run_regenerates(monkeypatch):
    """The default is no cache: an expansion costs seconds on a local model,
    and regenerating means prompt edits / model swaps / temperature variation
    show up in the next run instead of being masked by a stale row."""
    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION", True)
    monkeypatch.setattr(config, "QUERY_EXPANSION_CACHE", False)
    cached = {"roles": ["STALE"], "cv_hash": qe._content_hash("my cv")}
    with patch.object(qe, "_read_cache", return_value=cached) as mock_read, \
         patch.object(qe, "_write_cache") as mock_write, \
         patch.object(qe, "generate_target_roles", return_value=["AI Engineer", "Fresh"]):
        roles = qe.get_target_roles("AI Engineer", candidate_text="my cv")
    assert roles == ["AI Engineer", "Fresh"]     # not the cached row
    mock_read.assert_not_called()
    mock_write.assert_not_called()
    assert qe.LAST_EXPANSION_CACHED is False


def test_cache_hit_skips_the_llm_entirely(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION", True)
    monkeypatch.setattr(config, "QUERY_EXPANSION_CACHE", True)
    cached = {"roles": ["AI Engineer", "ML Engineer"], "cv_hash": qe._content_hash("my cv")}
    with patch.object(qe, "_read_cache", return_value=cached), \
         patch.object(qe, "generate_target_roles") as mock_generate:
        roles = qe.get_target_roles("AI Engineer", candidate_text="my cv")
    assert roles == ["AI Engineer", "ML Engineer"]
    mock_generate.assert_not_called()
    assert qe.LAST_EXPANSION_CACHED is True      # so the report can say so


def test_use_cache_argument_overrides_the_config(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION", True)
    monkeypatch.setattr(config, "QUERY_EXPANSION_CACHE", False)
    cached = {"roles": ["FROM CACHE"], "cv_hash": None}
    with patch.object(qe, "_read_cache", return_value=cached), \
         patch.object(qe, "generate_target_roles") as mock_generate:
        roles = qe.get_target_roles("AI Engineer", use_cache=True)
    assert roles == ["FROM CACHE"]
    mock_generate.assert_not_called()


def test_changed_candidate_text_invalidates_the_cache(monkeypatch):
    """The expansion is calibrated to the CV, so a materially different CV
    must re-expand rather than silently reuse a list built for the old one."""
    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION", True)
    monkeypatch.setattr(config, "QUERY_EXPANSION_CACHE", True)
    cached = {"roles": ["AI Engineer"], "cv_hash": qe._content_hash("OLD cv")}
    with patch.object(qe, "_read_cache", return_value=cached), \
         patch.object(qe, "_write_cache"), \
         patch.object(qe, "generate_target_roles", return_value=["AI Engineer", "Fresh Role"]) as mock_generate:
        roles = qe.get_target_roles("AI Engineer", candidate_text="NEW cv")
    mock_generate.assert_called_once()
    assert "Fresh Role" in roles


def test_cache_miss_generates_and_writes(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION", True)
    monkeypatch.setattr(config, "QUERY_EXPANSION_CACHE", True)
    with patch.object(qe, "_read_cache", return_value=None), \
         patch.object(qe, "_write_cache") as mock_write, \
         patch.object(qe, "generate_target_roles", return_value=["AI Engineer", "ML Engineer"]):
        roles = qe.get_target_roles("AI Engineer", candidate_text="cv")
    assert roles == ["AI Engineer", "ML Engineer"]
    mock_write.assert_called_once()
