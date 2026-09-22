"""
search_agent's expansion-aware behavior: OR-across-phrases title matching,
URL dedupe, and the SerpAPI phrase cap that protects its 100/month free tier.
"""
from unittest.mock import patch

import config
from agents import search_agent


# ---- OR-across-phrases title matching ---------------------------------------

def test_matches_any_position_accepts_a_match_on_any_phrase():
    roles = ["Software Engineer", "Backend Developer", "Frontend Developer"]
    assert search_agent._matches_any_position("Senior Backend Developer", roles)
    assert search_agent._matches_any_position("Frontend Developer II", roles)


def test_matches_any_position_rejects_a_title_matching_none_of_them():
    roles = ["Software Engineer", "Backend Developer"]
    assert not search_agent._matches_any_position("Pastry Chef", roles)


def test_expansion_is_what_rescues_the_adjacent_title():
    """The exact case that motivated expansion: a 'Software Engineer' search
    used to discard 'Backend Developer' outright."""
    assert not search_agent._matches_any_position("Backend Developer", ["Software Engineer"])
    assert search_agent._matches_any_position(
        "Backend Developer", ["Software Engineer", "Backend Developer"]
    )


def test_empty_target_roles_means_no_filter():
    assert search_agent._matches_any_position("Anything At All", [])


def test_single_phrase_reproduces_the_original_behavior():
    """Passing one phrase must behave exactly like the old single-position
    filter, so nothing changes for a caller that doesn't use expansion."""
    for title in ["AI Software Engineer", "Gen AI/Agentic AI Engineer", "AI Engineer"]:
        assert search_agent._matches_any_position(title, ["AI Engineer"])
    assert not search_agent._matches_any_position("Data Analyst", ["AI Engineer"])


# ---- _filter_relevant with expanded roles -----------------------------------

def test_filter_relevant_keeps_jobs_matching_any_role():
    jobs = [
        {"title": "Software Engineer"},
        {"title": "Backend Developer"},
        {"title": "Pastry Chef"},
    ]
    kept = search_agent._filter_relevant(jobs, ["Software Engineer", "Backend Developer"], "")
    assert [j["title"] for j in kept] == ["Software Engineer", "Backend Developer"]


def test_seniority_remains_a_hard_filter_on_top_of_expansion():
    """Deliberate: seniority is now also a ranking factor, but a 'senior'
    search still shouldn't surface internships."""
    jobs = [{"title": "Senior Software Engineer"}, {"title": "Software Engineer Intern"}]
    kept = search_agent._filter_relevant(jobs, ["Software Engineer"], "senior")
    assert [j["title"] for j in kept] == ["Senior Software Engineer"]


# ---- URL dedupe -------------------------------------------------------------

def test_dedupe_by_url_keeps_first_occurrence():
    jobs = [
        {"title": "A", "url": "https://x/1"},
        {"title": "B", "url": "https://x/1"},   # same posting via another phrase
        {"title": "C", "url": "https://x/2"},
    ]
    out = search_agent._dedupe_by_url(jobs)
    assert [j["title"] for j in out] == ["A", "C"]


def test_dedupe_recognizes_the_alternate_url_keys():
    jobs = [
        {"title": "A", "hostedUrl": "https://x/1"},
        {"title": "B", "url": "https://x/1"},
    ]
    assert len(search_agent._dedupe_by_url(jobs)) == 1


def test_dedupe_keeps_jobs_that_have_no_url_at_all():
    """A job with no URL can't be compared -- keep it rather than silently
    collapsing several distinct postings into one."""
    jobs = [{"title": "A"}, {"title": "B"}]
    assert len(search_agent._dedupe_by_url(jobs)) == 2


# ---- run_search wiring ------------------------------------------------------

def _no_sources(monkeypatch):
    """Silences every source so a test can isolate one code path."""
    monkeypatch.setattr(config, "GREENHOUSE_BOARD_TOKENS", [])
    monkeypatch.setattr(config, "LEVER_COMPANY_SLUGS", [])
    monkeypatch.setattr(search_agent, "search_remoteok", lambda *a, **k: [])
    monkeypatch.setattr(search_agent, "search_weworkremotely", lambda *a, **k: [])
    monkeypatch.setattr(search_agent, "search_from_watchlist", lambda *a, **k: [])
    monkeypatch.setattr(search_agent, "get_configured_sites", lambda: [])


def test_serpapi_is_capped_to_the_configured_phrase_limit(monkeypatch):
    """SerpAPI's free tier is 100 searches/MONTH -- sending every expanded
    phrase would exhaust it in under two weeks on a daily schedule."""
    _no_sources(monkeypatch)
    monkeypatch.setattr(config, "SERPAPI_EXPANSION_LIMIT", 2)
    calls = []

    def fake_serpapi(query):
        calls.append(query)
        return []

    monkeypatch.setattr(search_agent, "search_serpapi", fake_serpapi)
    search_agent.run_search(
        position="AI Engineer",
        target_roles=["AI Engineer", "ML Engineer", "Gen AI Engineer", "AI Software Engineer"],
    )
    assert len(calls) == 2  # not 4


def test_serpapi_limit_of_zero_skips_it_entirely(monkeypatch):
    _no_sources(monkeypatch)
    monkeypatch.setattr(config, "SERPAPI_EXPANSION_LIMIT", 0)
    with patch.object(search_agent, "search_serpapi") as mock_serp:
        search_agent.run_search(position="AI Engineer", target_roles=["AI Engineer", "ML Engineer"])
    mock_serp.assert_not_called()


def test_serpapi_query_still_includes_the_seniority_label(monkeypatch):
    _no_sources(monkeypatch)
    monkeypatch.setattr(config, "SERPAPI_EXPANSION_LIMIT", 1)
    calls = []
    monkeypatch.setattr(search_agent, "search_serpapi", lambda q: calls.append(q) or [])
    search_agent.run_search(position="AI Engineer", seniority="senior", target_roles=["AI Engineer"])
    assert calls == ["Senior AI Engineer"]


def test_target_roles_defaults_to_the_typed_position(monkeypatch):
    """Every pre-expansion caller must keep working unchanged."""
    _no_sources(monkeypatch)
    monkeypatch.setattr(config, "SERPAPI_EXPANSION_LIMIT", 5)
    calls = []
    monkeypatch.setattr(search_agent, "search_serpapi", lambda q: calls.append(q) or [])
    search_agent.run_search(position="AI Engineer")  # no target_roles passed
    assert calls == ["AI Engineer"]


def test_blank_phrases_are_dropped_so_they_cant_disable_the_filter(monkeypatch):
    """A blank phrase matches everything (a blank query means 'no filter'), so
    one bad LLM output could otherwise silently switch off title filtering."""
    _no_sources(monkeypatch)
    monkeypatch.setattr(config, "GREENHOUSE_BOARD_TOKENS", ["acme"])
    monkeypatch.setattr(search_agent, "search_greenhouse", lambda board: [
        {"title": "Software Engineer", "url": "https://x/1"},
        {"title": "Pastry Chef", "url": "https://x/2"},
    ])
    monkeypatch.setattr(search_agent, "search_serpapi", lambda q: [])
    jobs, _ = search_agent.run_search(
        position="Software Engineer", target_roles=["Software Engineer", "", "   "],
    )
    assert [j["title"] for j in jobs] == ["Software Engineer"]


def test_results_are_deduped_across_sources(monkeypatch):
    """The same posting can legitimately arrive from two different sources --
    a company's careers page and its Greenhouse board."""
    _no_sources(monkeypatch)
    monkeypatch.setattr(config, "GREENHOUSE_BOARD_TOKENS", ["acme"])
    monkeypatch.setattr(search_agent, "search_greenhouse", lambda board: [
        {"title": "Software Engineer", "url": "https://same/1"},
        {"title": "Software Engineer", "url": "https://same/1"},
    ])
    monkeypatch.setattr(search_agent, "search_serpapi", lambda q: [])
    jobs, _ = search_agent.run_search(position="Software Engineer", target_roles=["Software Engineer"])
    assert len(jobs) == 1


def test_a_dead_source_is_reported_not_fatal(monkeypatch):
    """Unchanged guarantee -- expansion must not weaken per-source error
    isolation."""
    import requests
    _no_sources(monkeypatch)
    monkeypatch.setattr(config, "GREENHOUSE_BOARD_TOKENS", ["broken"])

    def boom(board):
        raise requests.RequestException("404")

    monkeypatch.setattr(search_agent, "search_greenhouse", boom)
    monkeypatch.setattr(search_agent, "search_serpapi", lambda q: [])
    jobs, errors = search_agent.run_search(position="AI Engineer", target_roles=["AI Engineer"])
    assert jobs == []
    assert errors and errors[0]["source"] == "greenhouse"
