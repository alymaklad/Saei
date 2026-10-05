"""
Three scoring fixes (2026-10-05):

1. Soft skills have their own small bucket, and are judged against what the
   whole CV's work shows (agents/soft_skills.py) -- "Led the technical team"
   demonstrates team management without naming it.
2. A requirement's words match in any order, ignoring filler: "Computer
   Science degree" is "Bachelor Degree Computer Science".
3. "AI tools" asks for named tools: it is not reduced to the bare word "ai",
   so an "AI Intern" job title can't satisfy it.
"""
from unittest.mock import Mock, patch

import pytest

import config
from agents import ats_agent, skill_matching, soft_skills

SPANS = [
    {"text": "Full-Stack Developer & Technical Lead Notopia", "source": "experience", "demonstrated": True},
    {"text": "Led the technical team and managed delivery of a mobile e-commerce app, coordinating tasks and technical decisions.",
     "source": "experience", "demonstrated": True},
    {"text": "AI Intern Digital Hub (D-Hub) & Orange Digital Center", "source": "experience", "demonstrated": True},
    {"text": "Built tool-calling agents with LangChain and Flowise.", "source": "experience", "demonstrated": True},
    {"text": "Dual Bachelor Degree Computer Science Ain Shams University", "source": "education", "demonstrated": True},
]


def _req(name, category="technical_skill", importance="required", rtype="skill"):
    return {"name": name, "canonical": skill_matching.canonical(name), "category": category,
            "importance": importance, "requirement_type": rtype}


# ---- 1. soft skills -----------------------------------------------------------------

def test_soft_skills_have_their_own_bucket_whatever_their_importance():
    assert ats_agent._bucket_for(_req("Attention to detail", "soft_skill", "required")) == "soft_skills"
    assert ats_agent._bucket_for(_req("Communication", "soft_skill", "preferred")) == "soft_skills"
    assert ats_agent._bucket_for(_req("Python")) == "required_skills"


def test_a_posting_without_soft_skills_keeps_the_old_weights():
    weights = ats_agent._redistribute_weights(["required_skills", "experience", "preferred_skills", "education"])
    assert round(weights["required_skills"], 4) == 0.5
    assert round(weights["experience"], 4) == 0.2222


def test_soft_skills_are_a_small_share_when_present():
    weights = ats_agent._redistribute_weights(["required_skills", "soft_skills"])
    assert weights["soft_skills"] < 0.11


@pytest.fixture()
def cache_file(tmp_path, monkeypatch):
    path = tmp_path / "soft.json"
    monkeypatch.setattr(soft_skills, "CACHE_PATH", str(path))
    return path


def _llm_answer(text):
    fake = Mock()
    fake.invoke.return_value = Mock(content=text)
    return fake


def test_soft_skills_are_read_with_verbatim_evidence_and_cached(cache_file):
    answer = ('[{"skill": "team management", "evidence": "Led the technical team and managed delivery of a mobile e-commerce app, coordinating tasks and technical decisions."},'
              ' {"skill": "leadership", "evidence": "Led the technical team and managed delivery"},'
              ' {"skill": "charisma", "evidence": "Gave keynote speeches to thousands"}]')
    with patch("agents.soft_skills.get_llm", return_value=_llm_answer(answer)) as llm:
        found = soft_skills.demonstrated_soft_skills(SPANS)
        again = soft_skills.demonstrated_soft_skills(SPANS)
    assert [f["skill"] for f in found] == ["team management", "leadership"]  # invented line dropped
    assert found[1]["evidence"].startswith("Led the technical team") and found[1]["evidence"].endswith("decisions.")
    assert again == found and llm.call_count == 1  # second call served from the cache


def test_soft_skill_read_failure_returns_nothing_and_caches_nothing(cache_file):
    with patch("agents.soft_skills.get_llm", side_effect=RuntimeError("rate limit")):
        assert soft_skills.demonstrated_soft_skills(SPANS) == []
    assert not cache_file.exists()


def test_soft_skill_requirements_are_judged_against_the_marked_lines():
    soft_lines = {SPANS[1]["text"]: ["team management", "leadership"]}
    payload = ats_agent._entailment_payload(
        [_req("Team management", "soft_skill", rtype="capability"), _req("Kubernetes")],
        SPANS, decisions=None, soft_lines=soft_lines)
    assert "Soft skills (each judged separately):\n  - Team management" in payload
    assert "coordinating tasks and technical decisions.  <- shows team management, leadership" in payload
    # the non-soft requirement is still asked the usual way
    assert "Requirements to judge:\n- Kubernetes" in payload


def test_a_soft_skill_verdict_quoting_the_profile_counts_even_for_a_rewritten_cv():
    """A tailored CV's text may word the line differently; the profile line
    was verified when it was read, and a rewrite can't change the work."""
    rewritten_spans = [{"text": "Directed a team of engineers on a mobile app.", "demonstrated": True, "source": "experience"}]
    line = SPANS[1]["text"]
    answer = ('[{"requirement": "Team management", "satisfied": true, '
              f'"evidence": "{line}  <- shows team management", "confidence": 0.9}}]')
    with patch("agents.ats_agent.get_llm", return_value=_llm_answer(answer)):
        verdicts = ats_agent._entailment_pass([_req("Team management", "soft_skill", rtype="capability")],
                                              rewritten_spans, soft_lines={line: ["team management"]})
    assert verdicts["team management"]["evidence"] == line  # the "<- shows" note is stripped
    assert verdicts["team management"]["relation"] == "semantic_support"


# ---- 2. any word order ------------------------------------------------------------------

@pytest.mark.parametrize("requirement, line, credential, expected", [
    ("Computer Science degree", "Dual Bachelor Degree Computer Science Ain Shams", True, "computer science"),
    ("Bachelor's degree", "Dual Bachelor Degree Computer Science", True, "bachelor"),
    ("Bachelor's in Computer Science", "BSc (Hons) in Computer Science, Cairo University", True, "bachelor hons computer science"),
    ("Degree in CS or related field", "BSc Computer Science", True, "computer science"),
    ("Data pipeline design", "Owned the design of the data pipeline", False, "design data pipeline"),
])
def test_words_match_in_any_order(requirement, line, credential, expected):
    assert skill_matching.find_words_any_order(requirement, line, allow_single=credential) == expected


@pytest.mark.parametrize("requirement, line, credential", [
    ("Master's degree in Computer Science", "Dual Bachelor Degree Computer Science", True),  # wrong level
    ("Machine learning engineering", "learning to cook machine parts for engineering class", False),  # scattered
    ("Bachelor's degree", "Dual Bachelor Degree Computer Science", False),  # one word, not a credential
])
def test_any_order_does_not_match_the_wrong_thing(requirement, line, credential):
    assert skill_matching.find_words_any_order(requirement, line, allow_single=credential) is None


def test_degree_requirement_scores_exact_not_semantic():
    outcome = ats_agent.match_requirement(_req("Computer Science degree", "education", rtype="credential"), SPANS)
    assert outcome["relation"] == "exact" and outcome["credit"] == 1.0
    assert "different order" in outcome["via"]


# ---- 3. broad fields need named instances ------------------------------------------------

def test_ai_tools_is_not_reduced_to_ai():
    assert skill_matching.canonical("Familiarity with AI tools") == "familiarity with ai tools"
    assert skill_matching.find_term("Familiarity with AI tools", SPANS[2]["text"]) is None


@pytest.mark.parametrize("phrase, core", [
    ("Docker tooling", "docker"),          # a specific tool keeps its reduction
    ("Python programming", "python"),
])
def test_specific_tools_still_reduce(phrase, core):
    assert skill_matching.canonical(phrase) == core


def test_an_ai_job_title_does_not_satisfy_ai_tools():
    outcome = ats_agent.match_requirement(_req("Familiarity with AI tools", "tool", "preferred"), SPANS[2:3])
    assert outcome["credit"] == 0.0
