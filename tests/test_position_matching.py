"""
_matches_position used to be a literal substring check (position.lower() in
title.lower()), which silently missed common real-world title phrasing --
e.g. searching "AI Engineer" never matched "AI Software Engineer" or
"AI/ML Software Engineer" because "ai engineer" never appears as one
contiguous phrase in either. Reported directly by the user with four
concrete examples. Replaced with a word-based match: a title matches if it
contains every significant word from the query, in any order.
"""
from agents.search_agent import _matches_position, _position_tokens


# ---- the user's exact reported examples, all searching "AI Engineer" ----

def test_ai_engineer_matches_ai_software_engineer():
    assert _matches_position("AI Software Engineer", "AI Engineer")


def test_ai_engineer_matches_ai_ml_software_engineer():
    assert _matches_position("AI/ML Software Engineer", "AI Engineer")


def test_ai_engineer_matches_gen_ai_engineer():
    assert _matches_position("Gen AI Engineer", "AI Engineer")


def test_ai_engineer_matches_gen_ai_agentic_ai_engineer():
    assert _matches_position("Gen AI/Agentic AI Engineer", "AI Engineer")


# ---- still excludes genuinely unrelated titles ----

def test_ai_engineer_does_not_match_unrelated_title():
    assert not _matches_position("Marketing Manager", "AI Engineer")


def test_ai_engineer_does_not_match_title_missing_engineer():
    """Contains "AI" but not "Engineer" at all -- still correctly excluded,
    since matching requires ALL query words, not just any one of them."""
    assert not _matches_position("AI Product Manager", "AI Engineer")


# ---- order independence + punctuation handling ----

def test_word_order_does_not_matter():
    assert _matches_position("Engineer, AI", "AI Engineer")


def test_punctuation_is_treated_as_a_word_boundary():
    # "/" splits "AI/ML" into separate words "ai" and "ml"
    assert _position_tokens("AI/ML Engineer") == {"ai", "ml", "engineer"}


# ---- edge cases ----

def test_blank_position_matches_everything():
    assert _matches_position("Literally Anything", "")
    assert _matches_position("", "")


def test_blank_title_does_not_match_a_real_query():
    assert not _matches_position("", "AI Engineer")


def test_stopwords_are_ignored_in_the_query():
    # "of" is a stopword -- shouldn't be required in the title
    assert _matches_position("Head Engineer", "Head of Engineer")


def test_original_behavior_still_works_for_exact_phrase_titles():
    """Anything the old literal substring check matched must still match --
    this is a strict superset, not a replacement with different tradeoffs."""
    assert _matches_position("Senior Software Engineer", "Software Engineer")
    assert _matches_position("Software Engineer", "Software Engineer")
