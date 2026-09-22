"""
The tailored CV: structured document in, blueprint layout out.

What these tests are really pinning is the integrity boundary. The model no
longer writes the CV -- it chooses and rewords content, and every fact is
merged in afterwards from the user's stored profile. So the interesting cases
are all the ways a model response could try to introduce something that isn't
in the profile, and the guarantee that none of them reach the page.

The layout assertions matter for a different reason: the tailored CV is
re-scored by reading evidence structurally out of render_cv_text()'s output
(see cv_profile.spans_from_text), so the plain-text rendering is not a
convenience -- it is the input to the second half of the pipeline.
"""
import pytest

from agents import cv_profile, cv_render
from agents.cv_rewriter_agent import build_document


PROFILE = {
    "contact": {"full_name": "Aly Maklad", "title": "AI Engineer",
                "email": "aly@example.com", "phone": "+20 100 000 0000",
                "location": "Cairo, Egypt", "linkedin": "linkedin.com/in/alymaklad",
                "github": "github.com/alymaklad"},
    "summary": "Original summary.",
    "experience": [
        {"title": "AI Research Intern", "organization": "Manipal Institute of Technology",
         "location": "Manipal, India", "start": "2025-07", "end": "2026-05",
         "is_professional": True,
         "bullets": ["Trained a 3D ResNet-50 encoder with MoCo self-supervised learning.",
                     "Achieved 0.568 BERTScore-F1 on the CT-RATE dataset."]},
        {"title": "Full-Stack Developer", "organization": "Notopia",
         "location": "Cairo, Egypt", "start": "2024-10", "end": "2025-01",
         "is_professional": True,
         "bullets": ["Led a team delivering a mobile e-commerce application.",
                     "Built Flutter frontend and NestJS backend services."]},
    ],
    "projects": [
        {"name": "Course Material Chatbot", "url": "github.com/alymaklad/chatbot",
         "start": "2025-02", "end": "2025-08",
         "bullets": ["Built a RAG platform over university PDFs.",
                     "Hybrid retrieval with BGE-M3, BM25 and Qdrant."]},
        {"name": "Fall Detection", "url": "",
         "start": "2025-10", "end": "2026-06",
         "bullets": ["Fall detection with YOLO tracking, reaching 91.6% accuracy."]},
    ],
    "education": [{"degree": "B.Sc.", "field": "Computer Science",
                   "institution": "Ain Shams University", "location": "Cairo, Egypt",
                   "start": "2022-09", "end": "2026-07"}],
    "certifications": [],
    "skills_claimed": ["Python", "PyTorch", "LangChain", "FastAPI", "Qdrant"],
}

CV_TEXT = "Aly Maklad. Python, PyTorch, LangChain, FastAPI, Qdrant, Docker."

RESPONSE = {
    "summary": "AI Engineer building **RAG** systems.",
    # Deliberately out of order, and carrying a role that does not exist.
    "experience": [
        {"ref": 1, "bullets": ["Led a team delivering a **Flutter** e-commerce app."]},
        {"ref": 0, "bullets": ["Trained a 3D ResNet-50 encoder with **MoCo** learning.",
                               "Reached 0.568 **BERTScore-F1** on CT-RATE."]},
        {"ref": 7, "bullets": ["Principal Engineer at a company that never employed me."]},
    ],
    # Ranked by relevance: the second project first.
    "projects": [
        {"ref": 1, "bullets": ["Fall detection with **YOLO** tracking, 91.6% accuracy."]},
        {"ref": 0, "bullets": ["Built a **RAG** platform over university PDFs."]},
    ],
    "skills": [{"category": "AI/LLM", "items": ["LangChain", "Qdrant", "Kubernetes", "PyTorch"]},
               {"category": "Programming", "items": ["Python", "FastAPI", "python"]}],
    "note": "More on GitHub.",
}


@pytest.fixture
def document():
    return build_document(PROFILE, RESPONSE, cv_text=CV_TEXT,
                          missing_skills=["Kubernetes"])


# ---- facts come from the profile, never from the model ----------------------

def test_identity_and_contact_come_from_the_profile(document):
    assert document["name"] == "Aly Maklad"
    # The candidate's own headline, not the job's title: echoing the posting's
    # title back is an ATS trick that becomes a seniority claim.
    assert document["headline"] == "AI Engineer"
    assert document["contact"]["email"] == "aly@example.com"


def test_dates_and_locations_are_not_taken_from_the_model(document):
    intern = document["experience"][0]
    assert (intern["start"], intern["end"]) == ("2025-07", "2026-05")
    assert intern["location"] == "Manipal, India"
    assert intern["organization"] == "Manipal Institute of Technology"


def test_project_links_come_from_the_profile(document):
    by_name = {p["name"]: p for p in document["projects"]}
    assert by_name["Course Material Chatbot"]["url"] == "github.com/alymaklad/chatbot"
    # A project with no stored link stays without one rather than acquiring a
    # plausible-looking URL.
    assert by_name["Fall Detection"]["url"] == ""


def test_a_role_that_isnt_in_the_profile_cannot_be_added(document):
    titles = [e["title"] for e in document["experience"]]
    assert titles == ["AI Research Intern", "Full-Stack Developer"]
    assert any("isn't in your profile" in w for w in document["integrity_warnings"])


def test_every_profile_role_survives_in_profile_order(document):
    """Letting the model drop a job would leave an unexplained gap, so the
    entries are always all of them, in the CV's own order -- only the bullets
    are the model's."""
    assert len(document["experience"]) == len(PROFILE["experience"])
    assert [e["organization"] for e in document["experience"]] == \
           [e["organization"] for e in PROFILE["experience"]]


def test_projects_keep_the_models_relevance_order(document):
    """The one place ranking IS the model's call: projects are ordered by fit
    to the job, not by date."""
    assert [p["name"] for p in document["projects"]] == ["Fall Detection",
                                                          "Course Material Chatbot"]


# ---- the two fabrications a template invites --------------------------------

def test_a_skill_from_the_missing_list_is_removed(document):
    """`missing_skills` is the ATS pass's own finding that the candidate does
    NOT have these. A tailored CV claiming one is the exact failure this
    pipeline exists to prevent, so it is filtered rather than requested."""
    assert all("Kubernetes" not in group["items"] for group in document["skills"])
    assert any("Kubernetes" in w for w in document["integrity_warnings"])


def test_a_skill_absent_from_profile_and_cv_is_removed():
    doc = build_document(PROFILE, {"skills": [{"category": "Cloud",
                                               "items": ["Terraform", "Python"]}]},
                         cv_text=CV_TEXT, missing_skills=[])
    assert [i for g in doc["skills"] for i in g["items"]] == ["Python"]
    assert any("Terraform" in w for w in doc["integrity_warnings"])


def test_a_required_skill_the_model_dropped_is_put_back():
    """A rewrite for an AI role returned a skills section without "Java" or
    "C/C++" -- both claimed by the profile, both named by the posting. Twenty
    points vanished from the tailored CV for nothing. Filtering the model's
    list is the guard; deleting the candidate's own claims is not."""
    profile = dict(PROFILE, skills_claimed=PROFILE["skills_claimed"] + ["Java", "C/C++"])
    doc = build_document(profile,
                         {"skills": [{"category": "Languages", "items": ["Python"]}]},
                         cv_text=CV_TEXT, missing_skills=[],
                         requirement_names=["Python", "Java", "C++"])
    items = [i for g in doc["skills"] for i in g["items"]]
    assert "Java" in items and "C/C++" in items
    assert any("Restored" in w for w in doc["integrity_warnings"])


def test_a_restored_skill_joins_its_own_group():
    """Java belongs with the languages, not in a bin at the end of the CV."""
    profile = dict(PROFILE, skills_claimed=PROFILE["skills_claimed"] + ["Java"])
    doc = build_document(profile, {"skills": [
        {"category": "AI/LLM", "items": ["LangChain"]},
        {"category": "Languages", "items": ["Python"]},
    ]}, cv_text=CV_TEXT, missing_skills=[], requirement_names=["Java"])
    group = next(g for g in doc["skills"] if "Java" in g["items"])
    assert group["category"] == "Languages"


def test_restoration_cannot_invent_a_skill_the_profile_never_claimed():
    """The restore reads the PROFILE, never the posting. A job asking for Rust
    does not put Rust on a CV that has never mentioned it."""
    doc = build_document(PROFILE, {"skills": [{"category": "Languages",
                                               "items": ["Python"]}]},
                         cv_text=CV_TEXT, missing_skills=[],
                         requirement_names=["Rust", "Kubernetes"])
    items = [i.casefold() for g in doc["skills"] for i in g["items"]]
    assert "rust" not in items and "kubernetes" not in items


def test_restoration_never_outranks_the_blocked_list():
    """Restoring runs after the no-evidence filter and must not undo it. (Note
    what `blocked` means by the time it gets here: missing_skills narrowed
    against the profile, so a skill the user added on the Profile page is
    never vetoed by a stale CV file -- see unsupported_by_profile.)"""
    from agents.cv_rewriter_agent import _reconcile_skills
    profile = dict(PROFILE, skills_claimed=PROFILE["skills_claimed"] + ["Kubernetes"])
    warnings = []
    groups = _reconcile_skills([{"category": "Cloud", "items": ["Python"]}],
                               profile, CV_TEXT, {"kubernetes"}, warnings,
                               required=["Kubernetes", "Python"])
    items = [i.casefold() for g in groups for i in g["items"]]
    assert "kubernetes" not in items


def test_skills_are_deduplicated_across_categories(document):
    items = [i.casefold() for group in document["skills"] for i in group["items"]]
    assert len(items) == len(set(items))


def test_an_invented_metric_is_flagged():
    """The blueprint's '[Metric + value]' slots are an invitation to fill them.
    A number with no source in the entry it claims to describe is reported --
    not silently stripped, because the bullet around it may be fine and the
    person sending the CV is the one who should decide."""
    doc = build_document(PROFILE, {"experience": [
        {"ref": 0, "bullets": ["Cut inference latency by 42% across the pipeline."]}]},
        cv_text=CV_TEXT, missing_skills=[])
    assert any("42" in w for w in doc["integrity_warnings"])


def test_a_metric_present_in_the_source_is_not_flagged(document):
    assert not any("0.568" in w for w in document["integrity_warnings"])


def test_an_entry_the_model_left_empty_falls_back_to_its_own_bullets():
    doc = build_document(PROFILE, {"experience": [{"ref": 0, "bullets": []}]},
                         cv_text=CV_TEXT, missing_skills=[])
    assert doc["experience"][0]["bullets"][0].startswith("Trained a 3D ResNet-50")


# ---- the text rendering is the rescoring input ------------------------------

def test_text_rendering_is_readable_by_the_structural_span_reader(document):
    """Rescoring a tailored CV reads evidence out of this text instead of
    paying for a second parse, so the section headers have to be ones
    cv_profile._SECTION_PATTERNS recognizes."""
    spans = cv_profile.spans_from_text(cv_render.render_cv_text(document))
    sources = {s["source"] for s in spans}
    assert {"experience", "project", "education", "skills"} <= sources
    assert any(s["demonstrated"] for s in spans if s["source"] == "experience")
    assert all(not s["demonstrated"] for s in spans if s["source"] == "skills")


def test_bold_markers_never_reach_the_text_rendering(document):
    text = cv_render.render_cv_text(document)
    assert "**" not in text
    assert "RAG" in text


def test_dates_are_formatted_for_a_reader():
    assert cv_render.format_date_range("2025-07", "2026-05") == "Jul/2025 - May/2026"
    assert cv_render.format_date_range("2024", None) == "2024"
    assert cv_render.format_date("present") == "Present"
    # An unparseable date is printed as written rather than dropped or guessed.
    assert cv_render.format_date("Summer 2024") == "Summer 2024"


# ---- the PDF ----------------------------------------------------------------

def test_pdf_is_one_page_with_clickable_project_links(document, tmp_path):
    pytest.importorskip("pypdf")
    import pypdf

    out = tmp_path / "cv.pdf"
    cv_render.save_cv_as_pdf(document, str(out))
    reader = pypdf.PdfReader(str(out))
    assert len(reader.pages) == 1

    uris = [(a.get_object().get("/A") or {}).get("/URI")
            for page in reader.pages for a in (page.get("/Annots") or [])]
    assert "https://github.com/alymaklad/chatbot" in uris


def test_a_plain_string_still_renders(tmp_path):
    """The degraded path: if the model's JSON can't be read, rewrite_cv returns
    its raw text and the old line-based layout renders it. A worse CV beats
    discarding a rewrite that has already been paid for."""
    out = tmp_path / "legacy.pdf"
    cv_render.save_cv_as_pdf("Aly Maklad\naly@example.com\n\nSUMMARY\nA summary.\n"
                             "\nEXPERIENCE\n- Did a thing.", str(out))
    assert out.exists() and out.stat().st_size > 0
    assert cv_render.render_cv_text("TAILORED CV") == "TAILORED CV"


# ---- the call site ----------------------------------------------------------

def test_debug_bench_tailors_from_the_stored_profile(monkeypatch):
    """The STORED profile wins over anything the scoring pass built for itself:
    it is the one the user has corrected by hand, and the only one carrying
    per-role locations and project links, which scoring never reads and a CV
    needs."""
    from unittest.mock import patch
    import config, api
    from agents import ats_agent

    requirements = {"requirements": [
        {"name": "Python", "category": "technical_skill", "importance": "required"},
        {"name": "Kubernetes", "category": "technical_skill", "importance": "required"},
    ]}
    monkeypatch.setattr(config, "REQUIREMENT_ENTAILMENT", False)
    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.9)
    scored = ats_agent.compute_ats_score("cv text", "Python role",
                                         required_skills=requirements, profile=PROFILE)
    seen = {}

    def fake_rewrite(cv_text, jd, missing, profile=None, requirement_results=None,
                     **kwargs):
        seen["profile"] = profile
        seen["requirement_results"] = requirement_results
        return build_document(PROFILE, RESPONSE, cv_text=cv_text, missing_skills=missing)

    monkeypatch.setattr(api.cv_parser, "find_default_cv", lambda d: "cv/current_cv.pdf")
    monkeypatch.setattr(api.cv_parser, "parse_cv", lambda p: CV_TEXT)
    monkeypatch.setattr(api.profile_store, "load_profile_dict", lambda: PROFILE)

    with patch("agents.ats_agent.compute_ats_score", return_value=scored), \
         patch("agents.cv_rewriter_agent.rewrite_cv", side_effect=fake_rewrite):
        result = api.debug_ats(api.AtsDebugRequest(job_description="Python role"))

    assert seen["profile"] is PROFILE
    # The scoring pass's verdict travels with it -- that is what the rewrite
    # targets the job's own vocabulary from.
    assert seen["requirement_results"] == scored["requirement_results"]

    rewrite = result["rewrite"]
    # The bench shows text, not the document dict -- and the text is what gets
    # re-scored.
    assert rewrite["tailored_cv"].startswith("Aly Maklad")
    assert "Manipal Institute of Technology" in rewrite["tailored_cv"]
    assert any("Kubernetes" in w for w in rewrite["integrity_warnings"])
