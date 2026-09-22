"""
Orchestrator graph routing — covers two things:

1. route_after_rewrite as a pure function (fast, no mocking): given a
   tailored ATS result and the AUTO_APPLY_ON_TAILORED_SCORE flag, does it
   pick the right next node key?
2. A full graph.invoke() run with every LLM-backed call mocked, to prove the
   conditional edge registered in build_graph() actually wires rewrite_cv's
   output to decide_apply_path (not just that the routing function alone
   returns the right string -- the graph wiring itself is what a typo in
   add_conditional_edges would break).

config is read by module attribute access at call time in both
route_after_rewrite and decide_apply_path, so monkeypatch.setattr is enough
-- no importlib.reload needed anywhere in this file.
"""
from unittest.mock import patch

import config
import orchestrator


# ---- route_after_rewrite: pure routing function ----------------------------

def test_route_after_rewrite_end_when_flag_off(monkeypatch):
    monkeypatch.setattr(config, "AUTO_APPLY_ON_TAILORED_SCORE", False)
    state = {"tailored_ats_result": {"score": 0.95}}
    assert orchestrator.route_after_rewrite(state) == "end"


def test_route_after_rewrite_reenters_apply_path_when_flag_on_and_score_clears(monkeypatch):
    monkeypatch.setattr(config, "AUTO_APPLY_ON_TAILORED_SCORE", True)
    state = {"tailored_ats_result": {"score": 0.95}}
    assert orchestrator.route_after_rewrite(state) == "decide_apply_path"


def test_route_after_rewrite_stays_at_end_when_score_still_below_threshold(monkeypatch):
    monkeypatch.setattr(config, "AUTO_APPLY_ON_TAILORED_SCORE", True)
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.7)
    state = {"tailored_ats_result": {"score": 0.69}}
    assert orchestrator.route_after_rewrite(state) == "end"


def test_route_after_rewrite_score_exactly_at_threshold_clears(monkeypatch):
    monkeypatch.setattr(config, "AUTO_APPLY_ON_TAILORED_SCORE", True)
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.7)
    state = {"tailored_ats_result": {"score": 0.7}}
    assert orchestrator.route_after_rewrite(state) == "decide_apply_path"


def test_route_after_rewrite_respects_a_custom_threshold(monkeypatch):
    """FIT_THRESHOLD is user-configurable from the Settings page -- a score
    that would clear the old hardcoded 0.7 default must NOT clear a
    deliberately raised threshold."""
    monkeypatch.setattr(config, "AUTO_APPLY_ON_TAILORED_SCORE", True)
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.9)
    assert orchestrator.route_after_rewrite({"tailored_ats_result": {"score": 0.8}}) == "end"
    assert orchestrator.route_after_rewrite({"tailored_ats_result": {"score": 0.95}}) == "decide_apply_path"


def test_route_after_rewrite_handles_missing_tailored_result(monkeypatch):
    monkeypatch.setattr(config, "AUTO_APPLY_ON_TAILORED_SCORE", True)
    assert orchestrator.route_after_rewrite({"tailored_ats_result": None}) == "end"
    assert orchestrator.route_after_rewrite({}) == "end"


# ---- route_on_score: also reads config.FIT_THRESHOLD fresh at call time ----

def test_route_on_score_uses_default_threshold(monkeypatch):
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.7)
    assert orchestrator.route_on_score({"ats_result": {"score": 0.69}}) == "rewrite_cv"
    assert orchestrator.route_on_score({"ats_result": {"score": 0.7}}) == "decide_apply_path"


def test_route_on_score_respects_a_custom_threshold(monkeypatch):
    """A score that would have been 'good fit' under the old hardcoded 0.7
    must route to rewrite_cv once the threshold is raised from Settings."""
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.85)
    assert orchestrator.route_on_score({"ats_result": {"score": 0.8}}) == "rewrite_cv"
    assert orchestrator.route_on_score({"ats_result": {"score": 0.9}}) == "decide_apply_path"


# ---- Full graph.invoke(), everything LLM-backed mocked ---------------------

def _job(source="greenhouse", board="stripe"):
    return {
        "id": "1",
        "source": source,
        "board": board,
        "url": "https://boards.greenhouse.io/stripe/jobs/1",
        "description": "need python",
    }


@patch("orchestrator.save_cv_as_pdf")
@patch("orchestrator.rewrite_cv", return_value="REWRITTEN CV TEXT")
@patch("orchestrator.build_improvement_explanation", return_value="improved")
@patch("orchestrator.decide_apply_path", return_value="auto_submit")
@patch("orchestrator.compute_ats_score")
def test_flag_on_low_fit_job_that_tailors_well_reaches_auto_submit(
    mock_score, mock_decide, mock_explain, mock_rewrite, mock_save_pdf, monkeypatch
):
    monkeypatch.setattr(config, "AUTO_APPLY_ON_TAILORED_SCORE", True)
    monkeypatch.setattr(config, "DRY_RUN", True)
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.7)
    # First call scores the ORIGINAL cv (low); second scores the TAILORED cv (clears threshold).
    mock_score.side_effect = [
        {"score": 0.3, "missing_skills": ["python"], "required_skills": ["python"]},
        {"score": 0.9, "missing_skills": [], "required_skills": ["python"]},
    ]

    graph_app = orchestrator.build_graph()
    result = graph_app.invoke({"job": _job(), "cv_text": "original cv text"})

    assert result["status"] == "auto_submitted"
    mock_decide.assert_called_once()
    # Tailored score data must survive all the way through, even though the
    # graph kept going past rewrite_cv instead of stopping there.
    assert result["tailored_ats_result"]["score"] == 0.9


@patch("orchestrator.save_cv_as_pdf")
@patch("orchestrator.rewrite_cv", return_value="REWRITTEN CV TEXT")
@patch("orchestrator.build_improvement_explanation", return_value="improved")
@patch("orchestrator.decide_apply_path")
@patch("orchestrator.compute_ats_score")
def test_flag_off_low_fit_job_that_tailors_well_still_stops_at_notify(
    mock_score, mock_decide, mock_explain, mock_rewrite, mock_save_pdf, monkeypatch
):
    monkeypatch.setattr(config, "AUTO_APPLY_ON_TAILORED_SCORE", False)
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.7)
    mock_score.side_effect = [
        {"score": 0.3, "missing_skills": ["python"], "required_skills": ["python"]},
        {"score": 0.9, "missing_skills": [], "required_skills": ["python"]},
    ]

    graph_app = orchestrator.build_graph()
    result = graph_app.invoke({"job": _job(), "cv_text": "original cv text"})

    assert result["status"] == "cv_rewritten_notify_user"
    mock_decide.assert_not_called()


@patch("orchestrator.save_cv_as_pdf")
@patch("orchestrator.rewrite_cv", return_value="REWRITTEN CV TEXT")
@patch("orchestrator.build_improvement_explanation", return_value="unchanged")
@patch("orchestrator.decide_apply_path")
@patch("orchestrator.compute_ats_score")
def test_flag_on_but_tailored_score_still_low_stops_at_notify(
    mock_score, mock_decide, mock_explain, mock_rewrite, mock_save_pdf, monkeypatch
):
    monkeypatch.setattr(config, "AUTO_APPLY_ON_TAILORED_SCORE", True)
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.7)
    mock_score.side_effect = [
        {"score": 0.3, "missing_skills": ["python"], "required_skills": ["python"]},
        {"score": 0.4, "missing_skills": ["python"], "required_skills": ["python"]},
    ]

    graph_app = orchestrator.build_graph()
    result = graph_app.invoke({"job": _job(), "cv_text": "original cv text"})

    assert result["status"] == "cv_rewritten_notify_user"
    mock_decide.assert_not_called()


@patch("orchestrator.compute_ats_score", return_value={"score": 0.9, "missing_skills": [], "required_skills": []})
@patch("orchestrator.decide_apply_path", return_value="draft_for_review")
@patch("orchestrator.draft_for_review", return_value={"status": "pending_review"})
def test_good_fit_job_never_touches_rewrite_path(mock_draft, mock_decide, mock_score, monkeypatch):
    """A job that scores well on the original CV should never call rewrite_cv at all."""
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.7)
    with patch("orchestrator.rewrite_cv") as mock_rewrite:
        graph_app = orchestrator.build_graph()
        result = graph_app.invoke({"job": _job(), "cv_text": "already a great fit"})
        mock_rewrite.assert_not_called()
    assert result["status"] == "pending_review"


@patch("orchestrator.compute_ats_score", return_value={"score": 0.9, "missing_skills": [], "required_skills": []})
def test_good_fit_job_auto_submits_via_real_decide_apply_path_in_any_mode(mock_score, monkeypatch):
    """End-to-end through the REAL agents.apply_agent.decide_apply_path (not
    mocked) -- proves AUTO_APPLY_MODE="any" reaches auto_submit even for a
    source that would never be whitelistable, without going through the
    orchestrator's own mocked routing tests above."""
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.7)
    monkeypatch.setattr(config, "AUTO_APPLY_MODE", "any")
    monkeypatch.setattr(config, "DRY_RUN", True)

    graph_app = orchestrator.build_graph()
    result = graph_app.invoke({
        "job": _job(source="manual_link", board=None),
        "cv_text": "already a great fit",
    })

    assert result["status"] == "auto_submitted"
    assert result["apply_path"] == "auto_submit"
