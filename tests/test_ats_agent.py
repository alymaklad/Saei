"""ATS scoring should always return a 0..1 range, independent of LLM output quirks."""
from agents.ats_agent import keyword_overlap_score, _safe_json_list


def test_keyword_overlap_score_range():
    cv_text = "Experienced in Python, SQL, and AWS."
    required = ["Python", "SQL", "AWS", "Kubernetes"]
    score = keyword_overlap_score(cv_text, required)
    assert 0.0 <= score <= 1.0
    assert score == 0.75  # 3 of 4 matched


def test_keyword_overlap_score_no_required_skills():
    assert keyword_overlap_score("anything", []) == 0.0


def test_safe_json_list_parses_clean_array():
    assert _safe_json_list('["Python", "SQL"]') == ["Python", "SQL"]


def test_safe_json_list_extracts_array_from_prose():
    raw = 'Sure, here are the skills:\n["Python", "SQL"]\nHope that helps!'
    assert _safe_json_list(raw) == ["Python", "SQL"]


def test_safe_json_list_returns_empty_on_garbage():
    assert _safe_json_list("not json at all") == []
