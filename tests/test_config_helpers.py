"""
config.py's small env-parsing helpers, tested directly rather than by
reloading the whole config module (which would mutate the shared config
object every other test file imports) -- same approach
tests/test_search_agent_sources.py uses for _seeded_template_keys.
"""
import config


# ---- _float -----------------------------------------------------------------

def test_float_parses_a_valid_value(monkeypatch):
    monkeypatch.setenv("SOME_FLOAT", "0.85")
    assert config._float("SOME_FLOAT", 0.7) == 0.85


def test_float_falls_back_to_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_FLOAT", raising=False)
    assert config._float("SOME_FLOAT", 0.7) == 0.7


def test_float_falls_back_to_default_on_garbage(monkeypatch):
    monkeypatch.setenv("SOME_FLOAT", "not-a-number")
    assert config._float("SOME_FLOAT", 0.7) == 0.7


def test_float_falls_back_on_blank_string(monkeypatch):
    monkeypatch.setenv("SOME_FLOAT", "   ")
    assert config._float("SOME_FLOAT", 0.7) == 0.7


# ---- _choice ------------------------------------------------------------

def test_choice_parses_a_valid_value(monkeypatch):
    monkeypatch.setenv("SOME_MODE", "any")
    assert config._choice("SOME_MODE", {"off", "any", "whitelist"}, "whitelist") == "any"


def test_choice_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("SOME_MODE", "ANY")
    assert config._choice("SOME_MODE", {"off", "any", "whitelist"}, "whitelist") == "any"


def test_choice_falls_back_to_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_MODE", raising=False)
    assert config._choice("SOME_MODE", {"off", "any", "whitelist"}, "whitelist") == "whitelist"


def test_choice_falls_back_to_default_on_unrecognized_value(monkeypatch):
    """A hand-edited or corrupted .env value (e.g. a typo) must not put the
    app into an undefined auto-apply state -- it silently reverts to the
    safe default instead of raising or passing the raw value through."""
    monkeypatch.setenv("SOME_MODE", "everything")
    assert config._choice("SOME_MODE", {"off", "any", "whitelist"}, "whitelist") == "whitelist"


# ---- Module-level constants built from these helpers ------------------------

def test_auto_apply_mode_is_one_of_the_three_known_values():
    assert config.AUTO_APPLY_MODE in config.AUTO_APPLY_MODES
    assert config.AUTO_APPLY_MODES == {"off", "any", "whitelist"}


def test_fit_threshold_is_a_sane_default_ratio():
    assert 0 < config.FIT_THRESHOLD <= 1
