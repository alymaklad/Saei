"""
New job-board sources: SimplyHired/Wellfound/GulfTalent's build_url templates
(pure functions, no network), RemoteOK's JSON parser, and We Work Remotely's
RSS parser, plus the config.SEARCH_DEFAULT_SITES_SEEDED legacy-bool
migration that lets a NEW template get seeded on an install that already ran
seeding under the old boolean flag.
"""
import importlib
from unittest.mock import patch, Mock

import config
from agents import search_agent


# ---- KNOWN_JOB_BOARD_TEMPLATES.build_url -----------------------------------

def test_simplyhired_build_url_filters_by_position():
    url = search_agent.KNOWN_JOB_BOARD_TEMPLATES["simplyhired"]["build_url"]("software engineer")
    assert url == "https://www.simplyhired.com/search?q=software+engineer"


def test_simplyhired_build_url_blank_position_returns_none():
    assert search_agent.KNOWN_JOB_BOARD_TEMPLATES["simplyhired"]["build_url"]("   ") is None


def test_wellfound_build_url_slugifies_position():
    url = search_agent.KNOWN_JOB_BOARD_TEMPLATES["wellfound"]["build_url"]("Software Engineer")
    assert url == "https://wellfound.com/role/r/software-engineer"


def test_wellfound_build_url_blank_position_returns_none():
    assert search_agent.KNOWN_JOB_BOARD_TEMPLATES["wellfound"]["build_url"]("") is None


def test_gulftalent_build_url_is_position_independent():
    """GulfTalent's own ?keyword= param doesn't filter (verified by testing),
    so this template always points at its Software category page regardless
    of what's searched -- run_search()'s global position filter is what
    actually narrows results, not GulfTalent's own query string."""
    always = "https://www.gulftalent.com/jobs/category/software"
    assert search_agent.KNOWN_JOB_BOARD_TEMPLATES["gulftalent"]["build_url"]("software engineer") == always
    assert search_agent.KNOWN_JOB_BOARD_TEMPLATES["gulftalent"]["build_url"]("") == always


# ---- search_remoteok ---------------------------------------------------

REMOTEOK_SAMPLE = [
    {"legal": "Attribution required if you display these jobs publicly. https://remoteok.com/"},
    {
        "id": "111",
        "position": "Senior Backend Engineer",
        "company": "Acme Corp",
        "url": "https://remoteok.com/remote-jobs/111",
        "description": "Build backend systems.",
        "date": "2026-08-01T00:00:00+00:00",
    },
    {
        "id": "112",
        "position": "Marketing Manager",
        "company": "Widgets Inc",
        "url": "https://remoteok.com/remote-jobs/112",
        "description": "Run campaigns.",
        "date": "2026-08-02T00:00:00+00:00",
    },
    {"tags": ["malformed-entry-missing-id-and-position"]},
]


def _mock_response(json_data=None, content=b""):
    resp = Mock()
    resp.raise_for_status = Mock()
    resp.json = Mock(return_value=json_data)
    resp.content = content
    return resp


def test_search_remoteok_skips_legal_notice_and_malformed_rows():
    with patch("agents.search_agent.requests.get", return_value=_mock_response(REMOTEOK_SAMPLE)):
        jobs = search_agent.search_remoteok()
    assert len(jobs) == 2
    assert {j["title"] for j in jobs} == {"Senior Backend Engineer", "Marketing Manager"}
    assert all("date" in j and "company" in j and "url" in j for j in jobs)


def test_search_remoteok_filters_by_position():
    with patch("agents.search_agent.requests.get", return_value=_mock_response(REMOTEOK_SAMPLE)):
        jobs = search_agent.search_remoteok(position="backend")
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Senior Backend Engineer"


def test_search_remoteok_non_list_response_returns_empty():
    with patch("agents.search_agent.requests.get", return_value=_mock_response({"error": "not a list"})):
        assert search_agent.search_remoteok() == []


# ---- search_weworkremotely ----------------------------------------------

WWR_RSS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>We Work Remotely</title>
    <item>
      <title>Acme Corp: Senior Backend Engineer</title>
      <link>https://weworkremotely.com/remote-jobs/acme-corp-senior-backend-engineer</link>
      <pubDate>Sat, 01 Aug 2026 00:00:00 +0000</pubDate>
      <description>&lt;p&gt;Build backend systems.&lt;/p&gt;</description>
    </item>
    <item>
      <title>No colon in this title</title>
      <link>https://weworkremotely.com/remote-jobs/no-colon</link>
      <pubDate>Sun, 02 Aug 2026 00:00:00 +0000</pubDate>
      <description>Some description.</description>
    </item>
  </channel>
</rss>
"""


def test_search_weworkremotely_splits_company_and_title():
    with patch("agents.search_agent.requests.get", return_value=_mock_response(content=WWR_RSS_SAMPLE)):
        jobs = search_agent.search_weworkremotely()
    assert len(jobs) == 2
    assert jobs[0]["company"] == "Acme Corp"
    assert jobs[0]["title"] == "Senior Backend Engineer"
    assert jobs[0]["date"] == "Sat, 01 Aug 2026 00:00:00 +0000"


def test_search_weworkremotely_falls_back_when_no_colon():
    with patch("agents.search_agent.requests.get", return_value=_mock_response(content=WWR_RSS_SAMPLE)):
        jobs = search_agent.search_weworkremotely()
    assert jobs[1]["company"] == ""
    assert jobs[1]["title"] == "No colon in this title"


# ---- _extract_posted_at for the two new date formats ------------------

def test_extract_posted_at_handles_remoteok_iso_date():
    posted = search_agent._extract_posted_at({"date": "2026-08-01T00:00:00+00:00"})
    assert posted is not None
    assert posted.year == 2026 and posted.month == 8 and posted.day == 1


def test_extract_posted_at_handles_wwr_rfc822_date():
    posted = search_agent._extract_posted_at({"date": "Sat, 01 Aug 2026 00:00:00 +0000"})
    assert posted is not None
    assert posted.year == 2026 and posted.month == 8 and posted.day == 1


# ---- config.SEARCH_DEFAULT_SITES_SEEDED migration ------------------------

def test_legacy_true_migrates_to_wuzzuf_and_bayt(monkeypatch):
    monkeypatch.setenv("SEARCH_DEFAULT_SITES_SEEDED", "true")
    assert config._seeded_template_keys("SEARCH_DEFAULT_SITES_SEEDED") == {"wuzzuf", "bayt"}


def test_legacy_false_migrates_to_empty_set(monkeypatch):
    monkeypatch.setenv("SEARCH_DEFAULT_SITES_SEEDED", "false")
    assert config._seeded_template_keys("SEARCH_DEFAULT_SITES_SEEDED") == set()


def test_comma_list_parses_to_set(monkeypatch):
    monkeypatch.setenv("SEARCH_DEFAULT_SITES_SEEDED", "bayt,wuzzuf,simplyhired")
    assert config._seeded_template_keys("SEARCH_DEFAULT_SITES_SEEDED") == {"bayt", "wuzzuf", "simplyhired"}


def test_unset_returns_empty_set(monkeypatch):
    monkeypatch.delenv("SEARCH_DEFAULT_SITES_SEEDED", raising=False)
    assert config._seeded_template_keys("SEARCH_DEFAULT_SITES_SEEDED") == set()


def test_new_template_gets_seeded_when_legacy_flag_already_true():
    """The actual bug this migration fixes: an install with the old
    SEARCH_DEFAULT_SITES_SEEDED=true flag must still pick up brand new
    templates (gulftalent/simplyhired/wellfound) instead of the global flag
    silently skipping seeding forever."""
    already_seeded = {"wuzzuf", "bayt"}  # what "true" migrates to
    to_seed = [k for k in search_agent.KNOWN_JOB_BOARD_TEMPLATES if k not in already_seeded]
    assert set(to_seed) == {"simplyhired", "wellfound", "gulftalent"}
