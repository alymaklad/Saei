"""
Orchestrator — LangGraph state machine.

score -> (low fit) -> rewrite_cv -> (tailored score still low, or
                                      config.AUTO_APPLY_ON_TAILORED_SCORE is
                                      off) -> END (notify user)
                                  -> (config.AUTO_APPLY_ON_TAILORED_SCORE is
                                      on AND tailored score clears
                                      config.FIT_THRESHOLD) -> decide_apply_path
       -> (good fit) -> decide_apply_path -> auto_submit | draft_for_review -> END

"Low fit" / "good fit" above is decided against config.FIT_THRESHOLD (0.0-1.0,
default 0.7, user-configurable from the Settings page -- see
route_on_score/route_after_rewrite, both read it fresh at call time).

decide_apply_path (agents/apply_agent.py) is the single choke point both
branches above funnel into before anything can auto-submit -- it checks
config.AUTO_APPLY_MODE ("off" / "any" / "whitelist", Settings page) and, in
"whitelist" mode, config.WHITELISTED_SOURCES too. See that file's docstring
for what each mode actually does.

Respects config.DRY_RUN globally: when true, auto_submit logs intent instead
of calling the real submit function.
"""
from typing import TypedDict, Optional
from langgraph.graph import StateGraph, END

import config
from agents.ats_agent import compute_ats_score, build_improvement_explanation
from agents.cv_rewriter_agent import rewrite_cv, render_cv_text, save_cv_as_pdf
from agents.apply_agent import decide_apply_path, draft_for_review, auto_submit_greenhouse


class State(TypedDict):
    job: dict
    cv_text: str
    ats_result: Optional[dict]
    cv_rewritten: Optional[str]
    cv_path: Optional[str]
    tailored_ats_result: Optional[dict]
    tailored_ats_explanation: Optional[str]
    apply_path: Optional[str]
    status: str
    result: Optional[dict]


def score_node(state: State) -> State:
    """Score the candidate against this job.

    Evidence comes from the STORED PROFILE, not from the CV file's text. The
    profile is the record the user maintains on the Profile page -- seeded
    from their upload, then corrected by hand -- so it is the truthful account
    of what they have done, and it is also what the tailored CV will be built
    from. Scoring the file instead would grade a snapshot the user has since
    fixed, and would report a skill gap they had already closed.

    Falls back to None, which makes compute_ats_score extract a throwaway
    profile from the text, when nothing has been stored yet.
    """
    state["ats_result"] = compute_ats_score(
        state["cv_text"], state["job"].get("description", ""),
        profile=_stored_profile(),
    )
    return state


def _stored_profile():
    import profile_store
    try:
        return profile_store.load_profile_dict()
    except Exception:  # noqa: BLE001 -- a DB hiccup must not stop the run
        return None


def route_on_score(state: State) -> str:
    # tailored_only has no baseline number to gate on by definition, so every
    # job is tailored and the decision is taken on the tailored score instead.
    # That is one LLM rewrite per job -- see config.ATS_SCORE_MODE.
    if config.ATS_SCORE_MODE == "tailored_only":
        return "rewrite_cv"
    return "rewrite_cv" if state["ats_result"]["score"] < config.FIT_THRESHOLD else "decide_apply_path"


def _rescore_kwargs(original: dict, rewritten: str) -> dict:
    """What to hand compute_ats_score() when re-scoring a tailored CV.

    Reuses the whole structured extraction rather than a list of names
    (compute_requirements_score checks `extracted.get("requirements")` and
    silently re-extracts without it), plus the profile and the rewritten text
    as evidence. Without those last two it re-parses the tailored CV with a
    second large LLM call -- which is where this exact path failed on Groq's
    8,000-tokens-per-minute tier, AFTER the expensive rewrite had already
    succeeded and been paid for.
    """
    kwargs = {
        "required_skills": original.get("requirements"),
        # Structural, no-LLM evidence extraction from the rewritten text.
        "evidence_text": rewritten,
    }
    if original.get("cv_profile"):
        # Dates and seniority come from the profile already built. The
        # no-fabrication rule means a rewrite cannot change the work history,
        # so re-deriving it from the tailored text would spend a call to
        # arrive at the same answer.
        kwargs["profile"] = original["cv_profile"]
    return kwargs


def _tailoring_profile(state: State) -> dict | None:
    """Which profile the rewrite is built from.

    The stored profile: it is the record the user maintains, the one this
    job was scored against, and the only one carrying the fields a CV needs
    but scoring never reads (per-role locations, project links). The profile
    the scoring pass built for itself is the fallback for a run before
    anything was stored, and None makes rewrite_cv drop to its plain-text
    path.
    """
    return _stored_profile() or (state.get("ats_result") or {}).get("cv_profile")


def rewrite_node(state: State) -> State:
    missing = state["ats_result"]["missing_skills"]
    job_description = state["job"].get("description", "")
    # A document dict, not text -- the layout is applied by cv_render from the
    # structured result. render_cv_text() flattens it for rescoring and for
    # anything that stores or displays the CV as text, and passes a plain
    # string straight through, which is what keeps the degraded path working.
    document = rewrite_cv(state["cv_text"], job_description, missing,
                          profile=_tailoring_profile(state),
                          requirement_results=state["ats_result"].get("requirement_results"),
                          required_skills=state["ats_result"].get("required_skills"))
    rewritten = render_cv_text(document)
    state["cv_rewritten"] = rewritten

    job_id = state["job"].get("id", "job")
    out_path = f"cv_output/cv_{job_id}.pdf"
    save_cv_as_pdf(document, out_path)
    state["cv_path"] = out_path

    # Re-score the tailored text against the same job description so the user
    # can see the score actually moved, not just trust that a rewrite
    # happened. See _rescore_kwargs for what is reused and why getting it
    # wrong is invisible -- the score still comes out, it just quietly costs
    # an extra large LLM call.
    tailored_ats_result = compute_ats_score(
        rewritten, job_description,
        **_rescore_kwargs(state["ats_result"], rewritten),
    )
    state["tailored_ats_result"] = tailored_ats_result
    explanation = build_improvement_explanation(state["ats_result"], tailored_ats_result)

    # The rewrite's own checks have to reach the user on THIS path too. The
    # debug bench renders them beside the score; an unattended run has no
    # bench, and a guard that silently drops a bullet is worse than no guard --
    # the CV goes out with a hole in it and nothing says why. Appended to the
    # explanation because that is the field already stored on the Application
    # row and already read back into the daily report.
    if config.ATS_SCORE_MODE == "tailored_only":
        # No "up from X" line: the user asked to be shown one number, and a
        # comparison against a score that is never reported reads as noise.
        explanation = tailored_ats_result.get("explanation") or explanation

    notes = _rewrite_notes(document)
    if notes:
        explanation = f"{explanation}\n\n{notes}"
    state["tailored_ats_explanation"] = explanation

    state["status"] = "cv_rewritten_notify_user"
    state["result"] = {
        "status": state["status"],
        "cv_path": out_path,
        "ats_result": state["ats_result"],
        "tailored_ats_result": tailored_ats_result,
        "tailored_ats_explanation": explanation,
        "score_mode": config.ATS_SCORE_MODE,
        "integrity_warnings": _document_field(document, "integrity_warnings", []),
        "target_coverage": _document_field(document, "target_coverage", None),
        "listed_only": _document_field(document, "listed_only", []),
    }
    return state


def _document_field(document, key, default):
    """The degraded path returns a plain string, not a document."""
    return document.get(key, default) if isinstance(document, dict) else default


def _rewrite_notes(document) -> str:
    """The tailoring's own findings, as lines a person can read in a report."""
    lines = []
    warnings = _document_field(document, "integrity_warnings", [])
    if warnings:
        lines.append("Checks that rejected something during tailoring:")
        lines.extend(f"- {w}" for w in warnings)

    coverage = _document_field(document, "target_coverage", None) or {}
    if coverage.get("used"):
        lines.append("Wrote the job's own wording for: " + ", ".join(coverage["used"]) + ".")
    if coverage.get("missed"):
        lines.append("Could not place: " + ", ".join(coverage["missed"]) + ".")

    listed = _document_field(document, "listed_only", [])
    if listed:
        lines.append(
            "Only in your skills list, so earning partial credit: "
            + ", ".join(listed)
            + ". If a role or project actually used one, adding it there on the "
              "Profile page is worth the difference."
        )
    return "\n".join(lines)


def route_after_rewrite(state: State) -> str:
    """
    Opt-in re-entry point (config.AUTO_APPLY_ON_TAILORED_SCORE, off by
    default). If enabled and the tailored CV's re-scored ATS score now clears
    config.FIT_THRESHOLD, hand the job to the same decide_apply_path gate a
    naturally-good-fit job goes through -- so a rewritten CV never skips
    whatever AUTO_APPLY_MODE currently allows just because it took the
    rewrite path first. If disabled, or the tailored score still doesn't
    clear the bar, stop and notify the user, same as before this flag
    existed.
    """
    if not config.AUTO_APPLY_ON_TAILORED_SCORE:
        return "end"
    tailored = state.get("tailored_ats_result") or {}
    if (tailored.get("score") or 0) >= config.FIT_THRESHOLD:
        return "decide_apply_path"
    return "end"


def decide_apply_path_node(state: State) -> State:
    state["apply_path"] = decide_apply_path(state["job"])
    return state


def route_on_apply_path(state: State) -> str:
    return state["apply_path"]  # "auto_submit" or "draft_for_review"


def auto_submit_node(state: State) -> State:
    job = state["job"]
    if config.DRY_RUN:
        state["status"] = "auto_submitted"
        state["result"] = {
            "dry_run": True,
            "status": "auto_submitted",
            "job_url": job.get("url") or job.get("hostedUrl"),
        }
        return state

    # Real submission only reaches here per config.AUTO_APPLY_MODE (see
    # agents/apply_agent.py::decide_apply_path) -- "whitelist" mode requires
    # the source to be in WHITELISTED_SOURCES; "any" mode reaches here for
    # every source, though auto_submit_greenhouse itself is still a
    # Greenhouse-specific stub until hand-verified per board.
    auto_submit_greenhouse(
        job_url=job.get("url") or job.get("hostedUrl"),
        cv_path=state.get("cv_path", ""),
        cover_letter="",
        applicant_info={"name": config.APPLICANT_NAME, "email": config.APPLICANT_EMAIL},
    )
    state["status"] = "auto_submitted"
    return state


def draft_node(state: State) -> State:
    draft = draft_for_review(state["job"], state.get("cv_path", ""), cover_letter="")
    state["status"] = "pending_review"
    state["result"] = draft
    return state


def build_graph():
    graph = StateGraph(State)
    graph.add_node("score", score_node)
    graph.add_node("rewrite_cv", rewrite_node)
    graph.add_node("decide_apply_path", decide_apply_path_node)
    graph.add_node("auto_submit", auto_submit_node)
    graph.add_node("draft_for_review", draft_node)

    graph.set_entry_point("score")
    graph.add_conditional_edges("score", route_on_score, {
        "rewrite_cv": "rewrite_cv", "decide_apply_path": "decide_apply_path",
    })
    graph.add_conditional_edges("rewrite_cv", route_after_rewrite, {
        "decide_apply_path": "decide_apply_path", "end": END,
    })
    graph.add_conditional_edges("decide_apply_path", route_on_apply_path, {
        "auto_submit": "auto_submit", "draft_for_review": "draft_for_review",
    })
    graph.add_edge("auto_submit", END)
    graph.add_edge("draft_for_review", END)
    return graph.compile()


app = build_graph()
