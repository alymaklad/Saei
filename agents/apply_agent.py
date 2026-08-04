"""
Apply Agent — whitelist-only auto-submit logic.

Design rule (non-negotiable): the agent only auto-submits on sources you've
explicitly whitelisted in .env (WHITELISTED_SOURCES). Everywhere else it
prepares a complete draft and waits for your approval. See
agents/search_agent.py:WHITELISTABLE_SOURCES for which source types can ever
be whitelisted at all (only Greenhouse/Lever, since they're the only ones
with a documented public API to submit against).
"""
import config
from agents.search_agent import WHITELISTABLE_SOURCES


def _job_whitelist_key(job: dict) -> str:
    """Builds the 'source:identifier' key checked against WHITELISTED_SOURCES."""
    source = job.get("source", "")
    identifier = job.get("board") or job.get("company_slug") or job.get("watchlist_company") or ""
    return f"{source}:{identifier}" if identifier else source


def decide_apply_path(job: dict) -> str:
    source = job.get("source", "")
    if source not in WHITELISTABLE_SOURCES:
        return "draft_for_review"
    key = _job_whitelist_key(job)
    if key in config.WHITELISTED_SOURCES or source in config.WHITELISTED_SOURCES:
        return "auto_submit"
    return "draft_for_review"


def auto_submit_greenhouse(job_url: str, cv_path: str, cover_letter: str, applicant_info: dict):
    """
    STUB — intentionally not implemented.

    Every Greenhouse/Lever job board can define different custom application
    fields (screening questions, EEO fields, etc). Before enabling true
    auto-submit for a board:
      1. Inspect that specific board's application form schema (browser dev
         tools, or the board's public API) to see its exact required fields.
      2. Hand-build the submit payload for that board.
      3. Test end-to-end on one real (or throwaway) application.
      4. Only then add "greenhouse:<board_token>" to WHITELISTED_SOURCES in .env.
    This is the one part of the system meant to be verified per employer,
    not automated blindly.
    """
    raise NotImplementedError(
        "auto_submit_greenhouse is a stub — verify this board's form schema "
        "and implement the payload before enabling auto-submit for it."
    )


def draft_for_review(job: dict, cv_path: str, cover_letter: str) -> dict:
    return {
        "job_url": job.get("url") or job.get("hostedUrl"),
        "cv_path": cv_path,
        "cover_letter": cover_letter,
        "status": "pending_review",
    }
