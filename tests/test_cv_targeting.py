"""
Writing the job's vocabulary into a CV that already earned it.

The premise, from config.py's own credit table: a bullet saying "Trained a
CNN" earns 0.80 of a "Deep Learning" requirement, and the same bullet saying
"Trained a CNN for deep-learning image classification" earns 1.00. Nothing was
invented -- skill_matching.HYPONYMS already asserts a CNN is a kind of deep
learning, which is why the 0.80 was awarded at all. The points were lost to
wording.

So these tests come in two halves, and the second half is the important one:

  * the bridges are found and placed (Deep Learning onto the CNN bullet, SQL
    onto the PostgreSQL one, the spelled-out form onto the entry that only
    ever writes the acronym), and
  * nothing else gets through. A model handed a job description and told to
    match its vocabulary will write "Kubernetes" into a CV that only used
    Docker; the last three tests are the ones standing between that instinct
    and the page.
"""
import pytest

from agents import ats_agent, cv_render, cv_targeting, skill_matching
from agents.cv_rewriter_agent import build_document


PROFILE = {
    "contact": {"full_name": "Aly Maklad", "title": "AI Engineer"},
    "summary": "AI engineer.",
    "experience": [
        {"title": "AI Research Intern", "organization": "Manipal",
         "location": "Manipal, India", "start": "2025-07", "end": "2026-05",
         "is_professional": True,
         "bullets": ["Trained a 3D ResNet-50 CNN encoder with MoCo self-supervised learning.",
                     "Fine-tuned an LLM with 4-bit QLoRA on a single GPU."]},
        {"title": "Backend Developer", "organization": "Notopia",
         "location": "Cairo, Egypt", "start": "2024-10", "end": "2025-01",
         "is_professional": True,
         "bullets": ["Built NestJS services backed by PostgreSQL.",
                     "Containerised the stack with Docker for local runs."]},
    ],
    "projects": [
        {"name": "Course Chatbot", "url": "", "start": "2025-02", "end": "2025-08",
         "bullets": ["Built a RAG platform over university PDFs with Qdrant."]},
    ],
    "education": [], "certifications": [],
    "skills_claimed": ["Python", "PyTorch", "Docker", "SQL"],
}

REQUIREMENTS = {"requirements": [
    {"name": "Deep Learning", "category": "technical_skill", "importance": "required"},
    {"name": "SQL", "category": "technical_skill", "importance": "required"},
    {"name": "Large Language Models", "category": "technical_skill", "importance": "required"},
    {"name": "Retrieval Augmented Generation", "category": "technical_skill",
     "importance": "required"},
    {"name": "PyTorch", "category": "technical_skill", "importance": "required"},
    {"name": "Kubernetes", "category": "technical_skill", "importance": "preferred"},
]}

JOB = ("Senior AI Engineer: Deep Learning, Large Language Models (LLM), "
       "Retrieval Augmented Generation, SQL, PyTorch. Kubernetes a plus.")


@pytest.fixture
def scored(monkeypatch):
    """The scoring pass, with the LLM entailment fallback off so the whole
    fixture is deterministic and free."""
    import config
    monkeypatch.setattr(config, "REQUIREMENT_ENTAILMENT", False)
    return ats_agent.compute_requirements_score(
        "cv text", JOB, extracted=REQUIREMENTS, profile=PROFILE)


@pytest.fixture
def targets(scored):
    return cv_targeting.build_targets(
        PROFILE, requirement_results=scored["requirement_results"],
        missing_skills=scored["missing_skills"])


def _by_term(targets):
    return {t["term"]: t for t in targets}


# ---- the bridges the job's wording opens --------------------------------------

def test_a_broader_requirement_is_bridged_from_the_narrower_evidence(targets):
    """The user's own example: the job says Deep Learning, the CV says CNN."""
    target = _by_term(targets)["Deep Learning"]
    assert target["kind"] == "bridge"
    assert target["evidence_term"] == "cnn"
    assert (target["section"], target["ref"]) == ("experience", 0)


def test_a_parent_technology_is_bridged_from_the_product(targets):
    """And the second: the job says SQL, the CV says PostgreSQL."""
    target = _by_term(targets)["SQL"]
    assert target["evidence_term"] == "postgresql"
    assert (target["section"], target["ref"]) == ("experience", 1)


def test_the_bridge_lands_on_the_entry_that_describes_the_work(targets):
    """A term in an entry's heading is evidence too, but the job's wording
    belongs in the sentence that describes the work, not next to a job title.
    Bullets therefore outrank headings when choosing where to put it."""
    target = _by_term(targets)["Deep Learning"]
    entry = PROFILE[target["section"]][target["ref"]]
    assert skill_matching.find_term(target["evidence_term"],
                                    " ".join(entry["bullets"]))


def test_an_acronym_only_entry_is_asked_for_the_full_form_once(targets):
    """The third example: the CV writes LLM, the job writes it out. Worth
    nothing to this scorer, which treats them as aliases -- and everything to
    an ATS on the other side doing literal string matching."""
    target = _by_term(targets)["Large Language Models"]
    assert target["kind"] == "form"
    assert target["expansion"] == "Large Language Model (LLM)"
    assert "ONCE" in cv_targeting.format_targets([target])


def test_a_requirement_with_no_evidence_is_never_targeted(targets):
    """Kubernetes is in the job and nowhere in this CV. Docker is a SIBLING,
    not evidence -- skill_matching.EXCLUSIVE_GROUPS exists for exactly this."""
    assert "Kubernetes" not in _by_term(targets)


def test_a_skill_only_listed_is_reported_rather_than_placed(scored):
    """PyTorch is in the skills list and in no role. A rewrite cannot honestly
    move it into one, so it becomes advice to the candidate instead."""
    assert "PyTorch" not in {t["term"] for t in cv_targeting.build_targets(
        PROFILE, requirement_results=scored["requirement_results"])}
    assert "PyTorch" in cv_targeting.listed_only(PROFILE, scored["requirement_results"])


def test_expansion_is_offered_only_for_real_acronyms():
    assert cv_targeting.expansion_hint("LLM") == "Large Language Model (LLM)"
    assert cv_targeting.expansion_hint("NLP") == "Natural Language Processing (NLP)"
    # "Postgres" is a synonym, not an initialism; spelling both out is padding.
    assert cv_targeting.expansion_hint("PostgreSQL") is None
    assert cv_targeting.expansion_hint("Python") is None


# ---- the point of the exercise ------------------------------------------------

def test_using_the_jobs_wording_raises_the_score(scored, monkeypatch):
    """End to end, no model: score the CV, apply the targets by hand exactly as
    the prompt asks, re-score. This is the number the whole module exists for."""
    import config
    monkeypatch.setattr(config, "REQUIREMENT_ENTAILMENT", False)

    response = {
        "summary": "AI engineer.",
        "experience": [
            {"ref": 0, "bullets": [
                "Trained a 3D ResNet-50 **CNN** encoder — a **deep learning** model — "
                "with MoCo self-supervised learning.",
                "Fine-tuned a **Large Language Model (LLM)** with 4-bit QLoRA on a single GPU."]},
            {"ref": 1, "bullets": [
                "Built NestJS services backed by **SQL** (**PostgreSQL**).",
                "Containerised the stack with **Docker** for local runs."]},
        ],
        "projects": [{"ref": 0, "bullets": [
            "Built a **Retrieval Augmented Generation (RAG)** platform over university PDFs."]}],
        "skills": [{"category": "AI/LLM", "items": ["Python", "PyTorch", "SQL", "Docker"]}],
    }
    document = build_document(PROFILE, response, cv_text="Python PyTorch Docker SQL",
                              missing_skills=scored["missing_skills"],
                              requirement_names=[r["name"] for r in scored["requirement_results"]])

    after = ats_agent.compute_requirements_score(
        "cv", JOB, extracted=REQUIREMENTS, profile=PROFILE,
        evidence_text=cv_render.render_cv_text(document))

    assert after["score"] > scored["score"]

    credit_before = {r["name"]: r["credit"] for r in scored["requirement_results"]}
    credit_after = {r["name"]: r["credit"] for r in after["requirement_results"]}
    # The two bridges go from subset credit to the requirement's own word.
    assert credit_before["Deep Learning"] < credit_after["Deep Learning"] == 1.0
    assert credit_before["SQL"] < credit_after["SQL"] == 1.0
    # And the one with no evidence stays exactly where it was.
    assert credit_after["Kubernetes"] == 0.0


def test_coverage_reports_which_targets_actually_landed(targets):
    document = build_document(
        PROFILE,
        {"experience": [{"ref": 0, "bullets": ["Trained a **CNN** for **deep learning**."]},
                        {"ref": 1, "bullets": ["Built services on **PostgreSQL**."]}]},
        cv_text="", targets=targets)

    coverage = document["target_coverage"]
    assert "Deep Learning" in coverage["used"]
    assert "SQL" in coverage["missed"]      # the bullet kept only "PostgreSQL"
    assert 0 < coverage["rate"] < 1


# ---- and nothing else gets through --------------------------------------------

def test_a_bullet_claiming_a_missing_requirement_is_dropped():
    """Inviting the model to use the job's vocabulary is exactly what makes
    this necessary: the job asks for Kubernetes, the entry used Docker, and
    the two words sit next to each other in every training corpus."""
    document = build_document(
        PROFILE,
        {"experience": [{"ref": 1, "bullets": [
            "Containerised the stack with **Docker** and orchestrated it on **Kubernetes**.",
            "Built NestJS services backed by PostgreSQL."]}]},
        cv_text="", missing_skills=["Kubernetes"], requirement_names=["Kubernetes"])

    bullets = " ".join(document["experience"][1]["bullets"])
    assert not skill_matching.find_term("Kubernetes", bullets)
    assert "NestJS" in bullets   # the honest bullet survives
    assert any("Kubernetes" in w for w in document["integrity_warnings"])


def test_an_unsupported_term_is_reported_but_not_deleted():
    """Weaker case: the term isn't on the missing list, it just has no support
    in that entry. The bullet around it may be fine, so it is flagged rather
    than cut -- the same policy the invented-metric check uses."""
    document = build_document(
        PROFILE,
        {"experience": [{"ref": 1, "bullets": ["Built services and ran **Terraform**."]}]},
        cv_text="", missing_skills=[], requirement_names=["Terraform"])

    assert "Terraform" in " ".join(document["experience"][1]["bullets"])
    assert any("Terraform" in w for w in document["integrity_warnings"])


def test_a_summary_claiming_a_missing_requirement_falls_back_to_your_own():
    """A summary is a claim about the candidate, not about one role, so it is
    checked against the whole CV -- and a sentence written around a skill they
    don't have can't be repaired by editing a clause."""
    document = build_document(
        PROFILE,
        {"summary": "AI engineer running **Kubernetes** clusters at scale.",
         "experience": [{"ref": 0, "bullets": ["Trained a CNN."]}]},
        cv_text="Python PyTorch Docker SQL", missing_skills=["Kubernetes"],
        requirement_names=["Kubernetes"])

    assert document["summary"] == PROFILE["summary"]
    assert any("Summary" in w for w in document["integrity_warnings"])


# ---- the two entry points must stay the same pipeline -------------------------

def _recorder(store, tag):
    def fake(cv_text, jd, missing, profile=None, requirement_results=None,
             required_skills=None):
        store[tag] = {"cv_text": cv_text, "jd": jd, "missing": list(missing or []),
                      "profile": profile,
                      "requirement_results": requirement_results,
                      "required_skills": list(required_skills or [])}
        return "TAILORED TEXT"
    return fake


def test_the_auto_path_and_the_bench_tailor_from_identical_inputs(scored, monkeypatch):
    """Two entry points reach the rewriter: orchestrator.rewrite_node on a
    low-fit job, and the debug bench on demand. They have drifted apart before,
    and a bench that tailors differently from production is worse than no bench
    -- so this pins that both hand rewrite_cv the same thing.
    """
    from unittest.mock import patch
    import api, config, orchestrator, profile_store

    monkeypatch.setattr(config, "FIT_THRESHOLD", 0.99)
    seen = {}

    state = {"job": {"id": 7, "description": JOB}, "cv_text": "cv text",
             "ats_result": scored}
    with patch.object(profile_store, "load_profile_dict", return_value=PROFILE), \
         patch.object(orchestrator, "rewrite_cv", _recorder(seen, "auto")), \
         patch.object(orchestrator, "save_cv_as_pdf"), \
         patch.object(orchestrator, "compute_ats_score", return_value=scored):
        orchestrator.rewrite_node(dict(state))

    with patch.object(profile_store, "load_profile_dict", return_value=PROFILE), \
         patch("agents.ats_agent.compute_ats_score", return_value=scored), \
         patch("agents.cv_rewriter_agent.rewrite_cv", _recorder(seen, "bench")), \
         patch("agents.cv_rewriter_agent.save_cv_as_pdf"), \
         patch.object(api.cv_parser, "find_default_cv", return_value="cv/current_cv.pdf"), \
         patch.object(api.cv_parser, "parse_cv", return_value="cv text"):
        api.debug_ats(api.AtsDebugRequest(job_description=JOB))

    assert seen["auto"] == seen["bench"]
    # And the targeting inputs are actually there, not identically absent.
    assert seen["auto"]["profile"] is PROFILE
    assert seen["auto"]["requirement_results"] == scored["requirement_results"]


def test_the_live_path_reports_what_the_rewrite_rejected(scored, monkeypatch):
    """The bench renders the checks beside the score. An unattended run has no
    bench, and a guard that silently drops a bullet is worse than no guard, so
    the same findings ride out on the explanation the Application row stores."""
    from unittest.mock import patch
    import orchestrator, profile_store

    document = build_document(
        PROFILE,
        {"experience": [{"ref": 0, "bullets": [
            "Trained a 3D ResNet-50 **CNN** **deep learning** encoder.",
            "Ran the training cluster on **Kubernetes**."]}]},
        cv_text="", missing_skills=["Kubernetes"],
        requirement_names=["Deep Learning", "Kubernetes"], listed_only=["PyTorch"])

    state = {"job": {"id": 9, "description": JOB}, "cv_text": "cv",
             "ats_result": scored}
    with patch.object(profile_store, "load_profile_dict", return_value=PROFILE), \
         patch.object(orchestrator, "rewrite_cv", return_value=document), \
         patch.object(orchestrator, "save_cv_as_pdf"), \
         patch.object(orchestrator, "compute_ats_score", return_value=scored):
        out = orchestrator.rewrite_node(state)

    explanation = out["result"]["tailored_ats_explanation"]
    assert "Kubernetes" in explanation      # the bullet that was dropped
    assert "PyTorch" in explanation         # the listed-only advice
    assert out["result"]["integrity_warnings"]


# ---- the CV file and the profile are two different documents ------------------
#
# By design: the CV page uploads a file and seeds the profile from it; the
# Profile page is where the user corrects and extends that record. Scoring reads
# the FILE ("would my actual CV pass this ATS?"); the tailored CV is built from
# the PROFILE. `missing_skills` therefore comes from the file, and using it
# unchanged as a block list would let a stale upload veto the user's own edits.

PROFILE_AHEAD_OF_CV = {
    "contact": {"full_name": "Aly"}, "summary": "Engineer.",
    "experience": [{"title": "DevOps Engineer", "organization": "X",
                    "start": "2024-01", "end": "2025-01", "is_professional": True,
                    "bullets": ["Containerised the services with Docker.",
                                "Deployed and scaled them on Kubernetes."]}],
    "projects": [], "education": [], "certifications": [],
    "skills_claimed": ["Docker", "Kubernetes", "Python"],
}
UPLOADED_CV = "Aly. DevOps Engineer at X. Containerised with Docker. Skills: Docker, Python."


def test_a_profile_edit_is_not_vetoed_by_the_uploaded_file():
    """The user added Kubernetes to the role that used it. The file on disk
    predates that edit, so scoring calls Kubernetes missing -- but the tailored
    CV is built from the profile, and the profile is the record the user
    maintains."""
    document = build_document(
        PROFILE_AHEAD_OF_CV,
        {"experience": [{"ref": 0, "bullets": ["Deployed and scaled on **Kubernetes**."]}],
         "skills": [{"category": "Infra", "items": ["Docker", "Kubernetes", "Python"]}]},
        cv_text=UPLOADED_CV, missing_skills=["Kubernetes"],
        requirement_names=["Docker", "Kubernetes", "Python"])

    assert "Kubernetes" in [i for g in document["skills"] for i in g["items"]]
    assert skill_matching.find_term(
        "Kubernetes", " ".join(document["experience"][0]["bullets"]))
    # And the user is told the two documents disagree, because the SCORE was
    # computed without it.
    assert any("re-upload" in w for w in document["integrity_warnings"])


def test_a_skill_neither_document_supports_is_still_refused():
    """The narrowing must not become a hole: Terraform is in neither the file
    nor the profile, so it is dropped exactly as before."""
    document = build_document(
        PROFILE_AHEAD_OF_CV,
        {"experience": [{"ref": 0, "bullets": ["Managed the estate with **Terraform**."]}],
         "skills": [{"category": "Infra", "items": ["Docker", "Terraform"]}]},
        cv_text=UPLOADED_CV, missing_skills=["Terraform"], requirement_names=["Terraform"])

    assert "Terraform" not in [i for g in document["skills"] for i in g["items"]]
    assert not skill_matching.find_term(
        "Terraform", " ".join(document["experience"][0]["bullets"]))


def test_a_profile_supported_requirement_can_still_be_targeted():
    """Same narrowing on the targeting side: a requirement the file missed but
    the profile demonstrates is a legitimate bridge, not a fabrication."""
    targets = cv_targeting.build_targets(
        PROFILE_AHEAD_OF_CV,
        required_skills=["Container Orchestration"],
        missing_skills=["Container Orchestration"])
    assert [t["evidence_term"] for t in targets] == ["kubernetes"]
