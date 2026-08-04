"""
Orchestrator — LangGraph state machine.

score -> (low fit) -> rewrite_cv -> END (notify user, never auto-apply with a low-fit CV)
       -> (good fit) -> decide_apply_path -> auto_submit | draft_for_review -> END

Respects config.DRY_RUN globally: when true, auto_submit logs intent instead
of calling the real submit function.
"""
from typing import TypedDict, Optional
from langgraph.graph import StateGraph, END

import config
from agents.ats_agent import compute_ats_score
from agents.cv_rewriter_agent import rewrite_cv, save_cv_as_pdf
from agents.apply_agent import decide_apply_path, draft_for_review, auto_submit_greenhouse

FIT_THRESHOLD = 0.7


class State(TypedDict):
    job: dict
    cv_text: str
    ats_result: Optional[dict]
    cv_rewritten: Optional[str]
    cv_path: Optional[str]
    apply_path: Optional[str]
    status: str
    result: Optional[dict]


def score_node(state: State) -> State:
    state["ats_result"] = compute_ats_score(state["cv_text"], state["job"].get("description", ""))
    return state


def route_on_score(state: State) -> str:
    return "rewrite_cv" if state["ats_result"]["score"] < FIT_THRESHOLD else "decide_apply_path"


def rewrite_node(state: State) -> State:
    missing = state["ats_result"]["missing_skills"]
    rewritten = rewrite_cv(state["cv_text"], state["job"].get("description", ""), missing)
    state["cv_rewritten"] = rewritten

    job_id = state["job"].get("id", "job")
    out_path = f"cv_output/cv_{job_id}.pdf"
    save_cv_as_pdf(rewritten, out_path)
    state["cv_path"] = out_path
    state["status"] = "cv_rewritten_notify_user"
    state["result"] = {"status": state["status"], "cv_path": out_path, "ats_result": state["ats_result"]}
    return state


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

    # Real submission only reaches here for sources explicitly whitelisted in
    # .env AND after the per-board form has been hand-verified (see apply_agent.py).
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
    graph.add_edge("rewrite_cv", END)
    graph.add_conditional_edges("decide_apply_path", route_on_apply_path, {
        "auto_submit": "auto_submit", "draft_for_review": "draft_for_review",
    })
    graph.add_edge("auto_submit", END)
    graph.add_edge("draft_for_review", END)
    return graph.compile()


app = build_graph()
