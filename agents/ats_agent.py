"""
ATS Scoring Agent — deterministic keyword overlap + LLM judgment, combined.
Matches how real ATS systems filter (keywords) while still capturing context/seniority fit (LLM).
"""
import json
import re
from langchain_core.messages import SystemMessage, HumanMessage
from agents.llm import get_llm


def extract_required_skills(job_description: str) -> list[str]:
    llm = get_llm(temperature=0.0)
    messages = [
        SystemMessage(content="Extract the required and preferred skills/keywords from this "
                               "job description. Respond with ONLY a JSON array of strings, "
                               "nothing else."),
        HumanMessage(content=job_description),
    ]
    resp = llm.invoke(messages)
    return _safe_json_list(resp.content)


def _safe_json_list(raw: str) -> list[str]:
    """LLMs sometimes wrap JSON in prose or code fences — extract the array robustly."""
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    candidate = match.group(0) if match else raw
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, list):
            return [str(s) for s in parsed]
    except json.JSONDecodeError:
        pass
    return []


def keyword_overlap_score(cv_text: str, required_skills: list[str]) -> float:
    if not required_skills:
        return 0.0
    cv_lower = cv_text.lower()
    matched = sum(1 for s in required_skills if s.lower() in cv_lower)
    return matched / len(required_skills)


def llm_fit_score(cv_text: str, job_description: str) -> float:
    llm = get_llm(temperature=0.0)
    messages = [
        SystemMessage(content="You are an ATS/recruiter evaluator. Given a CV and a job "
                               "description, respond with ONLY a number from 0 to 1 "
                               "representing overall fit (experience level, domain relevance, "
                               "skills). No words, just the number."),
        HumanMessage(content=f"CV:\n{cv_text}\n\nJob Description:\n{job_description}"),
    ]
    resp = llm.invoke(messages)
    match = re.search(r"\d*\.?\d+", resp.content)
    if not match:
        return 0.5
    value = float(match.group(0))
    return max(0.0, min(1.0, value))


def compute_ats_score(cv_text: str, job_description: str) -> dict:
    required_skills = extract_required_skills(job_description)
    kw_score = keyword_overlap_score(cv_text, required_skills)
    llm_score = llm_fit_score(cv_text, job_description)
    final_score = 0.5 * kw_score + 0.5 * llm_score
    missing = [s for s in required_skills if s.lower() not in cv_text.lower()]
    return {
        "score": final_score,
        "keyword_score": kw_score,
        "llm_score": llm_score,
        "missing_skills": missing,
        "required_skills": required_skills,
    }
