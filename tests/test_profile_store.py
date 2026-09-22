"""
Tests for the persisted, user-editable CV profile (profile_store.py) and the
/api/profile endpoints.

Every test here runs against a temporary SQLite file, never the real
data/job_agent.db -- see the conftest-style fixture below, which points
config.DATABASE_URL at a tmp_path and reloads db/profile_store around it.
"""
import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def store(tmp_path, monkeypatch):
    """profile_store bound to a throwaway database."""
    import config
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path/'test.db'}")

    import db
    importlib.reload(db)
    db.Base.metadata.create_all(db.engine)

    import profile_store
    importlib.reload(profile_store)
    return profile_store


# ---- normalisation ----------------------------------------------------------

def test_normalize_fills_every_key_from_junk(store):
    for junk in (None, "a string", 42, [], {"experience": "not a list"}):
        profile = store.normalize(junk)
        assert set(profile) == {
            "contact", "summary", "experience", "projects", "education",
            "certifications", "skills_claimed", "seniority_self_described",
        }
        assert isinstance(profile["experience"], list)
        assert set(profile["contact"]) == set(store.CONTACT_FIELDS)


def test_normalize_drops_wholly_blank_entries(store):
    """A row the user added and never filled in is not data.

    Without this, pressing "Add entry" and saving stores an entry with no
    title, no organization and no bullets -- which the scorer then walks as a
    real span and the CV writer renders as an empty section.
    """
    profile = store.normalize({
        "experience": [
            {"title": "", "organization": "", "bullets": []},
            {"title": "Engineer", "organization": "Acme", "bullets": ["Did a thing"]},
        ],
        "projects": [{"name": "", "bullets": [""]}],
        "education": [{"degree": "", "field": "", "institution": ""}],
    })
    assert len(profile["experience"]) == 1
    assert profile["experience"][0]["title"] == "Engineer"
    assert profile["projects"] == []
    assert profile["education"] == []


def test_is_professional_defaults_to_true(store):
    """Miscounting a course as work is one visible click to fix. Silently
    dropping a real job from the years total is not."""
    profile = store.normalize({"experience": [{"title": "Intern", "bullets": ["x"]}]})
    assert profile["experience"][0]["is_professional"] is True

    explicit = store.normalize({"experience": [
        {"title": "Course", "bullets": ["x"], "is_professional": False},
    ]})
    assert explicit["experience"][0]["is_professional"] is False


def test_empty_and_null_strings_become_none_for_dates(store):
    profile = store.normalize({"experience": [
        {"title": "A", "bullets": ["x"], "start": "", "end": "null"},
    ]})
    entry = profile["experience"][0]
    assert entry["start"] is None and entry["end"] is None


def test_bullets_accept_a_newline_block(store):
    profile = store.normalize({"experience": [
        {"title": "A", "bullets": "- first\n- second\n\n"},
    ]})
    assert profile["experience"][0]["bullets"] == ["first", "second"]


def test_certifications_accept_objects_and_strings(store):
    profile = store.normalize({"certifications": [
        "AWS Certified",
        {"name": "Deep Learning", "issuer": "Coursera", "date": "2024"},
        {"name": "", "issuer": "", "date": ""},
    ]})
    assert profile["certifications"] == [
        "AWS Certified", "Deep Learning — Coursera — 2024",
    ]


def test_contact_carries_a_professional_title(store):
    """The headline printed under the name on a CV ("AI Engineer"), which is
    distinct from any single job title in the experience list."""
    profile = store.normalize({"contact": {
        "full_name": "Aly Maklad", "title": "AI Engineer",
    }})
    assert profile["contact"]["title"] == "AI Engineer"
    assert "title" in store.CONTACT_FIELDS
    # Always present, so the CV writer never has to guard for a missing key.
    assert store.empty_profile()["contact"]["title"] == ""
    assert store.normalize({})["contact"]["title"] == ""


def test_an_edited_title_counts_as_a_contact_edit(store):
    store.save_extraction({"contact": {"full_name": "Aly Maklad", "title": ""}},
                          cv_filename="cv.pdf", cv_text="cv")
    store.save({"contact": {"full_name": "Aly Maklad", "title": "AI Engineer"}})
    assert store.load()["edited_sections"] == ["contact"]


def test_the_description_box_round_trips_through_bullets(store):
    """The Profile page edits descriptions as one textarea and stores
    value.split("\\n"). Blank lines have to be dropped on save (pressing Enter
    twice shouldn't create an empty evidence span) while real lines survive
    verbatim -- they're quoted back as evidence in score explanations."""
    typed = "Led the technical team.\n\nShipped the mobile app.\n"
    stored = store.normalize({"experience": [
        {"title": "Lead", "bullets": typed.split("\n")},
    ]})["experience"][0]["bullets"]

    assert stored == ["Led the technical team.", "Shipped the mobile app."]
    # Re-rendered into the textarea and saved again, unchanged.
    again = store.normalize({"experience": [
        {"title": "Lead", "bullets": "\n".join(stored).split("\n")},
    ]})["experience"][0]["bullets"]
    assert again == stored


def test_the_certification_separator_is_the_one_the_ui_splits_on(store):
    """profile.js edits certifications as three fields and joins them with
    " — ". That separator is a contract between two files: change it in
    _clean_str_list without changing CERT_SEPARATOR in profile.js and an
    extracted certification stops being editable field-by-field."""
    flattened = store.normalize({"certifications": [
        {"name": "AWS Certified", "issuer": "Amazon", "date": "2024"},
    ]})["certifications"]

    assert flattened == ["AWS Certified — Amazon — 2024"]
    assert flattened[0].split(" — ") == ["AWS Certified", "Amazon", "2024"]


def test_a_certification_cleared_to_blank_is_dropped(store):
    """Clearing all three fields of a row is how the UI's remove button's
    effect can also be achieved by hand -- it must not leave an empty string
    behind for the CV writer to print."""
    assert store.normalize({"certifications": ["", "   "]})["certifications"] == []


def test_skills_are_deduplicated_case_insensitively(store):
    profile = store.normalize({"skills_claimed": ["Python", "python", " PYTHON ", "SQL"]})
    assert profile["skills_claimed"] == ["Python", "SQL"]


# ---- section diffing --------------------------------------------------------

def test_changed_sections_names_only_what_moved(store):
    before = store.normalize({"summary": "old", "skills_claimed": ["Python"]})
    after = store.normalize({"summary": "new", "skills_claimed": ["Python"]})
    assert store.changed_sections(before, after) == ["summary"]


def test_changed_sections_is_empty_for_identical_profiles(store):
    profile = store.normalize({"experience": [{"title": "A", "bullets": ["x"]}]})
    assert store.changed_sections(profile, profile) == []


# ---- persistence ------------------------------------------------------------

def test_save_and_load_round_trip(store):
    assert store.load() is None  # nothing extracted yet

    store.save({"contact": {"full_name": "Aly"}, "skills_claimed": ["Python"]})
    loaded = store.load()

    assert loaded["profile"]["contact"]["full_name"] == "Aly"
    assert loaded["profile"]["skills_claimed"] == ["Python"]


def test_save_records_which_sections_were_edited(store):
    store.save_extraction({"summary": "extracted", "skills_claimed": ["Python"]},
                          cv_filename="current_cv.pdf", cv_text="cv text")
    assert store.load()["edited_sections"] == []

    store.save({"summary": "hand written", "skills_claimed": ["Python"]})
    assert store.load()["edited_sections"] == ["summary"]

    # Edits accumulate -- a later edit to a different section doesn't erase the
    # record of the first, or the re-extract dialog would under-report.
    store.save({"summary": "hand written", "skills_claimed": ["Python", "Rust"]})
    assert store.load()["edited_sections"] == ["summary", "skills_claimed"]


def test_extraction_is_not_recorded_as_a_user_edit(store):
    """Otherwise the re-extract dialog warns forever about changes the user
    never made."""
    store.save_extraction({"summary": "one"}, cv_filename="a.pdf", cv_text="a")
    store.save_extraction({"summary": "two"}, cv_filename="a.pdf", cv_text="a")
    assert store.load()["edited_sections"] == []
    assert store.load()["edited_at"] is None


def test_re_extraction_clears_prior_edit_markers(store):
    store.save_extraction({"summary": "extracted"}, cv_filename="a.pdf", cv_text="a")
    store.save({"summary": "edited"})
    assert store.load()["edited_sections"] == ["summary"]

    store.save_extraction({"summary": "fresh"}, cv_filename="a.pdf", cv_text="a")
    after = store.load()
    assert after["edited_sections"] == []
    assert after["profile"]["summary"] == "fresh"


def test_saving_keeps_exactly_one_row(store):
    """One local user, one profile. A second row would make load()'s
    'most recent wins' silently discard the older one instead of updating it."""
    from models import CvProfile
    from db import get_session

    for i in range(4):
        store.save({"summary": f"v{i}"})
    with get_session() as session:
        assert session.query(CvProfile).count() == 1
    assert store.load()["profile"]["summary"] == "v3"


def test_is_stale_detects_a_replaced_cv_file(store):
    store.save_extraction({"summary": "x"}, cv_filename="a.pdf", cv_text="original cv text")
    stored = store.load()

    assert store.is_stale(stored, "original cv text") is False
    assert store.is_stale(stored, "a completely different cv") is True
    # No CV text to compare against is not evidence of staleness.
    assert store.is_stale(stored, None) is False
    assert store.is_stale(None, "anything") is False


# ---- API --------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path/'api.db'}")

    import db
    importlib.reload(db)
    db.Base.metadata.create_all(db.engine)

    import profile_store
    importlib.reload(profile_store)

    import api
    importlib.reload(api)
    monkeypatch.setattr(api, "CV_DIR", tmp_path / "cv")
    (tmp_path / "cv").mkdir(exist_ok=True)
    return TestClient(api.app), api, profile_store


def test_get_profile_is_empty_but_well_formed_before_extraction(client):
    tc, _, ps = client
    body = tc.get("/api/profile").json()

    assert body["has_profile"] is False
    assert body["profile"] == ps.empty_profile()
    assert body["edited_sections"] == []
    # The page renders section headings from this, so it must always be present.
    assert set(body["section_labels"]) == set(ps.SECTIONS)


def test_post_profile_saves_and_returns_the_stored_state(client):
    tc, _, _ = client
    resp = tc.post("/api/profile", json={"profile": {
        "contact": {"full_name": "Aly Maklad", "email": "a@example.com"},
        "experience": [{"title": "AI Research Intern", "organization": "Manipal",
                        "start": "2025-07", "end": "2026-05", "bullets": ["Built a thing"]}],
        "skills_claimed": ["Python", "PyTorch"],
    }})
    assert resp.status_code == 200
    body = resp.json()

    assert body["has_profile"] is True
    assert body["profile"]["contact"]["full_name"] == "Aly Maklad"
    assert body["profile"]["experience"][0]["is_professional"] is True
    assert tc.get("/api/profile").json()["profile"]["skills_claimed"] == ["Python", "PyTorch"]


def test_re_extract_preview_names_the_edited_sections(client):
    tc, _, ps = client
    ps.save_extraction({"summary": "from the file", "skills_claimed": ["Python"]},
                       cv_filename="current_cv.pdf", cv_text="cv")
    tc.post("/api/profile", json={"profile": {
        "summary": "rewritten by hand", "skills_claimed": ["Python"],
    }})

    preview = tc.get("/api/profile/re-extract/preview").json()
    assert preview["has_edits"] is True
    assert preview["edited_sections"] == ["summary"]
    assert preview["edited_section_labels"] == ["Professional summary"]


def test_re_extract_preview_costs_no_llm_call(client, monkeypatch):
    """The dialog opens on every click; paying for an extraction to populate a
    warning would make opening it as expensive as confirming it."""
    tc, _, _ = client
    from agents import cv_profile
    monkeypatch.setattr(cv_profile, "build_profile",
                        lambda *a, **k: pytest.fail("preview must not extract"))
    assert tc.get("/api/profile/re-extract/preview").status_code == 200


def test_re_extract_without_a_cv_is_a_clear_400(client):
    tc, _, _ = client
    resp = tc.post("/api/profile/re-extract", json={})
    assert resp.status_code == 400
    assert "No CV uploaded" in resp.json()["detail"]


def test_re_extract_replaces_the_profile_and_records_provenance(client, monkeypatch):
    tc, api, ps = client

    monkeypatch.setattr(api.cv_parser, "find_default_cv", lambda _d: "cv/current_cv.pdf")
    monkeypatch.setattr(api.cv_parser, "parse_cv", lambda _p: "REAL CV TEXT")

    from agents import cv_profile
    monkeypatch.setattr(cv_profile, "build_profile", lambda text, use_cache=True: {
        "summary": "extracted summary",
        "experience": [{"title": "Engineer", "organization": "Acme",
                        "start": "2024-01", "end": "present", "bullets": ["Shipped it"]}],
        "skills_claimed": ["Python"],
    })

    ps.save({"summary": "stale hand edit"})
    body = tc.post("/api/profile/re-extract", json={}).json()

    assert body["profile"]["summary"] == "extracted summary"
    assert body["source_cv_filename"] == "current_cv.pdf"
    assert body["source_cv_hash"] == ps.cv_hash("REAL CV TEXT")
    assert body["edited_sections"] == []
    assert body["cv_changed_since_extraction"] is False


def test_re_extract_bypasses_the_extraction_cache(client, monkeypatch):
    """Pressing this button usually means the cached parse was wrong. Serving
    the cache back would make the button appear to do nothing."""
    tc, api, _ = client
    monkeypatch.setattr(api.cv_parser, "find_default_cv", lambda _d: "cv/current_cv.pdf")
    monkeypatch.setattr(api.cv_parser, "parse_cv", lambda _p: "cv text")

    seen = {}
    from agents import cv_profile

    def fake_build(text, use_cache=True):
        seen["use_cache"] = use_cache
        return {"summary": "s"}

    monkeypatch.setattr(cv_profile, "build_profile", fake_build)
    tc.post("/api/profile/re-extract", json={})
    assert seen["use_cache"] is False


def test_profile_reports_a_cv_file_that_changed_since_extraction(client, monkeypatch):
    tc, api, ps = client
    ps.save_extraction({"summary": "x"}, cv_filename="current_cv.pdf",
                       cv_text="the ORIGINAL cv text")

    monkeypatch.setattr(api.cv_parser, "find_default_cv", lambda _d: "cv/current_cv.pdf")
    monkeypatch.setattr(api.cv_parser, "parse_cv", lambda _p: "a REPLACED cv")

    assert tc.get("/api/profile").json()["cv_changed_since_extraction"] is True


def test_an_unparseable_cv_does_not_break_the_profile_page(client, monkeypatch):
    tc, api, _ = client
    monkeypatch.setattr(api.cv_parser, "find_default_cv", lambda _d: "cv/current_cv.pdf")

    def boom(_path):
        raise ValueError("corrupt PDF")

    monkeypatch.setattr(api.cv_parser, "parse_cv", boom)
    resp = tc.get("/api/profile")
    assert resp.status_code == 200
    assert resp.json()["cv_changed_since_extraction"] is False
