"""
Whitelist enforcement — the single most safety-critical test in this project.
decide_apply_path must NEVER return auto_submit for a non-whitelisted source,
even for adversarially crafted job dicts.
"""
import importlib
import config
from agents import apply_agent


def _reload_with_whitelist(whitelist: set):
    config.WHITELISTED_SOURCES = whitelist
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
