"""
The deterministic helpers that outlived the four-pillar scorer.

Formatting and section completeness are still here, but they no longer score
a job match -- they feed compute_ats_compatibility, which answers "can a
parser read this document at all?" and never sees a job description. That is
precisely why they were wrong as match pillars: they returned the same number
on every job in the database, so 40% of every score carried no ranking signal.

_extract_required_years survives because reading "5+ years" out of a posting
is a property of the posting, and the ranking stage still asks that question.
What is gone with the pillars: keyword_overlap_score (substring containment),
llm_fit_score (an LLM asked for a number), and _estimate_cv_experience_years
(max(year) - min(year) over every number in the CV).
"""
from agents.ats_agent import (
    formatting_score,
    section_completeness_score,
    _extract_required_years,
    compute_ats_compatibility,
)

GOOD_CV = """Jane Doe
jane.doe@example.com | +1 555 123 4567 | New York, NY

SUMMARY
Backend engineer with eight years of experience designing and operating
high-throughput services.

EXPERIENCE
Senior Backend Engineer, Acme Corp (2019 - 2024)
- Built and operated a payments service handling 4,000 requests per second.
- Led the migration from a monolith to eight services.

EDUCATION
B.Sc. Computer Science, State University (2011 - 2015)

SKILLS
Python, Go, PostgreSQL, Kubernetes, AWS
"""

THIN_CV = "Jane Doe. I am a developer. I like computers. Contact me."


def test_formatting_score_rewards_a_clean_structured_cv():
    """A short fixture is penalised for length, so the assertion is relative:
    structure scores better than the same content without it."""
    score, issues = formatting_score(GOOD_CV)
    thin, _ = formatting_score(THIN_CV)
    assert score > thin
    assert isinstance(issues, list)


def test_formatting_score_flags_a_thin_unstructured_cv():
    score, issues = formatting_score(THIN_CV)
    assert score < 0.8
    assert issues


def test_section_completeness_finds_the_expected_sections():
    score, missing = section_completeness_score(GOOD_CV)
    assert score >= 0.9
    assert missing == [] or all(isinstance(m, str) for m in missing)


def test_section_completeness_flags_what_is_absent():
    score, missing = section_completeness_score(THIN_CV)
    assert score < 0.9
    assert missing


def test_required_years_is_read_from_the_posting():
    assert _extract_required_years("We need 5+ years of experience in backend work.") == 5
    assert _extract_required_years("Minimum 3 years experience required.") == 3


def test_required_years_is_none_when_the_posting_is_silent():
    assert _extract_required_years("We are looking for a passionate engineer.") is None


def test_compatibility_is_about_the_document_not_the_job():
    """No job description is involved, by design -- mixing the two is what let
    a tidy CV with weak experience outrank a strong one."""
    result = compute_ats_compatibility(GOOD_CV)
    assert 0.0 <= result["score"] <= 1.0
    assert result["status"] in ("Pass", "Warning", "Fail")
    assert set(result["checks"]) == {"formatting", "sections", "contact", "text_extraction"}
