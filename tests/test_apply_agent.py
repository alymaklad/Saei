"""
Whitelist enforcement — the single most safety-critical test in this project.
decide_apply_path must NEVER return auto_submit for a non-whitelisted source
while AUTO_APPLY_MODE is "whitelist" (the default), even for adversarially
crafted job dicts.
"""
import importlib
import config
from agents import apply_agent


def _reload_with_whitelist(whitelist: set):
    config.WHITELISTED_SOURCES = whitelist
    config.AUTO_APPLY_MODE = "whitelist"  # explicit -- don't rely on the .env default
    importlib.reload(apply_agent)
    return apply_agent


def test_non_whitelistable_source_never_auto_submits():
    apply_agent_mod = _reload_with_whitelist({"greenhouse:stripe"})
    job = {"source": "google_jobs", "url": "https://example.com/job/1"}
    assert apply_agent_mod.decide_apply_path(job) == "draft_for_review"


def test_whitelistable_source_not_in_whitelist_drafts():
    apply_agent_mod = _reload_with_whitelist({"greenhouse:stripe"})
    job = {"source": "lever", "company_slug": "netflix", "url": "https://jobs.lever.co/netflix/1"}
    assert apply_agent_mod.decide_apply_path(job) == "draft_for_review"


def test_whitelisted_board_auto_submits():
    apply_agent_mod = _reload_with_whitelist({"greenhouse:stripe"})
    job = {"source": "greenhouse", "board": "stripe", "url": "https://boards.greenhouse.io/stripe/jobs/1"}
    assert apply_agent_mod.decide_apply_path(job) == "auto_submit"


def test_empty_whitelist_never_auto_submits_anything():
    apply_agent_mod = _reload_with_whitelist(set())
    for job in [
        {"source": "greenhouse", "board": "stripe", "url": "https://x/1"},
        {"source": "lever", "company_slug": "netflix", "url": "https://x/2"},
        {"source": "manual_link", "url": "https://x/3"},
    ]:
        assert apply_agent_mod.decide_apply_path(job) == "draft_for_review"


def test_adversarial_source_spoofing_still_drafts():
    """A job dict crafted to look whitelisted by identifier alone must not fool the check."""
    apply_agent_mod = _reload_with_whitelist({"greenhouse:stripe"})
    job = {"source": "manual_link", "board": "stripe", "url": "https://evil.example.com/fake"}
    assert apply_agent_mod.decide_apply_path(job) == "draft_for_review"


# ---- AUTO_APPLY_MODE: off / any / whitelist --------------------------------
# decide_apply_path reads config.* by module attribute access at call time
# (not at import time), so no reload is strictly required after
# monkeypatch.setattr -- unlike _reload_with_whitelist above, which reloads
# to match this file's existing style. Using monkeypatch here (rather than
# assigning straight to config.* like _reload_with_whitelist does) also
# means the value is automatically restored after each test, so these don't
# leak into other test files run in the same session.

def test_mode_off_drafts_even_a_whitelisted_source(monkeypatch):
    monkeypatch.setattr(config, "WHITELISTED_SOURCES", {"greenhouse:stripe"})
    monkeypatch.setattr(config, "AUTO_APPLY_MODE", "off")
    job = {"source": "greenhouse", "board": "stripe", "url": "https://boards.greenhouse.io/stripe/jobs/1"}
    assert apply_agent.decide_apply_path(job) == "draft_for_review"


def test_mode_whitelist_restores_whitelisted_auto_submit(monkeypatch):
    monkeypatch.setattr(config, "WHITELISTED_SOURCES", {"greenhouse:stripe"})
    monkeypatch.setattr(config, "AUTO_APPLY_MODE", "whitelist")
    job = {"source": "greenhouse", "board": "stripe", "url": "https://boards.greenhouse.io/stripe/jobs/1"}
    assert apply_agent.decide_apply_path(job) == "auto_submit"


def test_mode_any_auto_submits_a_non_whitelistable_source(monkeypatch):
    """The whole point of 'any': sources decide_apply_path would otherwise
    always draft (not even in WHITELISTABLE_SOURCES) now auto-submit too."""
    monkeypatch.setattr(config, "WHITELISTED_SOURCES", set())
    monkeypatch.setattr(config, "AUTO_APPLY_MODE", "any")
    job = {"source": "manual_link", "url": "https://example.com/careers/1"}
    assert apply_agent.decide_apply_path(job) == "auto_submit"


def test_mode_any_auto_submits_a_whitelistable_but_unwhitelisted_source(monkeypatch):
    monkeypatch.setattr(config, "WHITELISTED_SOURCES", set())
    monkeypatch.setattr(config, "AUTO_APPLY_MODE", "any")
    job = {"source": "lever", "company_slug": "some-startup", "url": "https://jobs.lever.co/some-startup/1"}
    assert apply_agent.decide_apply_path(job) == "auto_submit"


def test_unrecognized_mode_value_falls_back_to_config_default_of_whitelist(monkeypatch):
    """config.py itself is what guards against a corrupted/unrecognized
    .env value -- config.AUTO_APPLY_MODE is only ever "off"/"any"/"whitelist".
    This test documents that decide_apply_path trusts that guarantee rather
    than re-validating, by simulating what config.py would have produced."""
    monkeypatch.setattr(config, "WHITELISTED_SOURCES", set())
    monkeypatch.setattr(config, "AUTO_APPLY_MODE", "whitelist")  # what config.py falls back to
    job = {"source": "greenhouse", "board": "stripe", "url": "https://boards.greenhouse.io/stripe/jobs/1"}
    assert apply_agent.decide_apply_path(job) == "draft_for_review"  # empty whitelist -> drafts
