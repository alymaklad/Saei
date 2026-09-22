"""
Term matching: the false positives and false negatives that made the legacy
keyword pillar untrustworthy, pinned so they cannot come back.

Every case here is drawn from real extracted requirements in this project's
database, not invented.
"""
import collections

import pytest

from agents import skill_matching as sm


CV = (
    "Aly Tarek Maklad\n"
    "Skills: Python, PyTorch, C++, REST API development, Git.\n"
    "Experience: Trained ResNet50 CNN models for CT medical image "
    "classification using PyTorch. Deployed services to Google Cloud.\n"
    "Node.js basics.\n"
)


# ---- false positives the substring rule produced ----------------------------

@pytest.mark.parametrize("term", ["R", "Go", "Vector", "math", "C", "Node", "js"])
def test_short_terms_do_not_match_incidentally(term):
    """`"r" in cv_lower` was true for every CV ever written. "R" is a real
    extracted requirement in this database, so this was live, and it inflated
    scores -- which is worse than under-counting, because it makes the system
    apply to jobs it shouldn't."""
    assert sm.find_term(term, CV) is None


def test_substring_containment_would_have_matched_them():
    """Guards the guard: if this ever fails, the CV fixture drifted and the
    test above stopped proving anything."""
    lower = CV.lower()
    assert all(t.lower() in lower for t in ["r", "go", "c", "node", "js"])


# ---- false negatives ---------------------------------------------------------

def test_alias_matching_finds_differently_worded_skills():
    assert sm.find_term("RESTful APIs", CV) == "rest api development"


def test_term_at_end_of_sentence_still_matches():
    """'.' is term-internal in Node.js but not in 'Git.' -- treating it as a
    boundary character in both directions would trade the old false positives
    for equally damaging false negatives."""
    assert sm.find_term("Git", CV) == "git"


def test_symbol_bearing_names_survive():
    assert sm.find_term("C++", CV) == "c++"


# ---- direction: the AWS/GCP problem -----------------------------------------

def test_specific_evidence_satisfies_general_requirement():
    assert sm.entails("Deep Learning", "CNN") is True


def test_general_evidence_does_not_satisfy_specific_requirement():
    """Asymmetry is the point. Having done deep learning does not demonstrate
    the specific CNN experience a posting asking for CNNs wants."""
    assert sm.entails("CNN", "Deep Learning") is False


def test_sibling_technologies_never_substitute():
    """The case from the design review. Embedding cosine similarity scores
    AWS/GCP HIGH precisely because they are the same category -- comparably
    high to CNN/Deep Learning -- so no similarity threshold separates these
    two situations. Only a directional rule does."""
    assert sm.entails("AWS", "Google Cloud") is False
    assert sm.are_mutually_exclusive("AWS", "GCP") is True
    assert sm.are_mutually_exclusive("PyTorch", "TensorFlow") is True
    assert sm.are_mutually_exclusive("Python", "R") is True


def test_aliases_of_the_same_thing_are_not_exclusive():
    assert sm.are_mutually_exclusive("GCP", "Google Cloud Platform") is False


def test_canonical_collapses_equivalent_spellings():
    assert sm.canonical("RESTful APIs") == sm.canonical("REST API development")
    assert sm.canonical("ML") == sm.canonical("Machine Learning")


# ---- the tables themselves ---------------------------------------------------

def test_hyponym_entries_are_never_self_contradictory():
    """A term listed as evidence for a requirement must not also be its
    sibling -- that would make entails() depend on which check ran first."""
    for requirement, evidences in sm.HYPONYMS.items():
        for evidence in evidences:
            assert not sm.are_mutually_exclusive(requirement, evidence), (
                f"{evidence!r} is listed as evidence for {requirement!r} but "
                "they are in the same exclusive group"
            )


def test_plural_variants_do_not_invent_nonsense_tokens():
    """Blindly stripping a trailing 's' turned "data analysis" into "data
    analysi" and put that in the search set for every requirement mentioning
    analysis. Harmless by luck (word boundaries kept it from matching), but a
    garbage token in a matcher is a bug waiting for the boundary rules to
    change."""
    for word in ("data analysis", "devops", "kubernetes", "statistics", "apis"):
        for form in sm.surface_forms(word):
            assert not form.endswith(("analysi", "statu", "proces")), \
                f"{word!r} produced the nonsense form {form!r}"


def test_alias_matching_wins_over_the_hierarchy_when_the_phrase_contains_the_term():
    """Documents deliberate behaviour rather than guarding against it.

    Alias matching runs first, so "Generative AI" satisfies an "Artificial
    Intelligence" requirement at FULL credit even though generative AI is also
    listed as a narrower kind of AI. That is right: the CV literally contains
    the token "AI", so the broader claim is stated, not inferred.

    Contrast the teamwork case, where the CV never says "teamwork" and the
    match runs through a synonym -- there the hierarchy should win, which is
    why "collaboration" was moved out of ALIASES.
    """
    assert _relation("Artificial Intelligence", "Worked on Generative AI systems") \
        in ("exact", "alias")
    assert _relation("Teamwork", "Drove cross-functional collaboration") == "subset"


def _relation(requirement: str, evidence: str) -> str:
    from agents import ats_agent
    return ats_agent.match_requirement(
        {"name": requirement, "canonical": None, "category": "technical_skill",
         "importance": "required"},
        [{"text": evidence, "source": "experience", "demonstrated": True}],
    )["relation"]


def test_no_term_is_both_an_alias_and_a_hyponym_of_the_same_concept():
    """Ambiguity the code resolves silently in favour of the higher credit."""
    for requirement, evidences in sm.HYPONYMS.items():
        canon = sm.canonical(requirement)
        for evidence in evidences:
            if sm.canonical(evidence) == canon and sm.normalize(evidence) != canon:
                pytest.fail(f"{evidence!r} is aliased to {canon!r} AND listed as its "
                            f"hyponym — pick one")


def test_hyponym_keys_are_unique():
    """A duplicated key in the dict literal is silently dropped by Python,
    losing every entry in the earlier copy."""
    import ast
    import pathlib
    source = pathlib.Path(sm.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and getattr(node.targets[0], "id", "") in ("HYPONYMS", "ALIASES",
                                                           "IMPLIED_BY")):
            keys = [k.value for k in node.value.keys]
            duplicates = [k for k, n in collections.Counter(keys).items() if n > 1]
            assert not duplicates, f"{node.targets[0].id} duplicate keys: {duplicates}"


def test_exclusive_groups_are_disjoint():
    """A term in two groups would make exclusivity order-dependent."""
    seen = set()
    for group in sm.EXCLUSIVE_GROUPS:
        canon = {sm.canonical(m) for m in group}
        assert not (canon & seen), f"overlapping exclusive groups: {canon & seen}"
        seen |= canon


# ---- homographs --------------------------------------------------------------

REACT_AGENT = ("Developed a LangGraph ReAct agent with dynamic tool selection "
               "for course retrieval, web search, and calculation")


def test_a_reasoning_agent_is_not_a_ui_library():
    """Found on a real scored job: this bullet matched a React requirement at
    Exact, and React is evidence of JavaScript, so the row below it invented a
    JavaScript match too — on a CV with no frontend JavaScript anywhere. ReAct
    is Reason+Act; the strings differ by one capital letter."""
    assert sm.find_term("React", REACT_AGENT) is None
    assert sm.find_term_with_context("React", REACT_AGENT) is None


@pytest.mark.parametrize("text", [
    "Implemented the ReAct pattern over LangChain tools",
    "A LangGraph-ReAct loop with tool calling",
    "Used ReAct prompting to structure the agent's steps",
])
def test_the_agent_pattern_is_rejected_however_it_is_written(text):
    assert sm.find_term("React", text) is None


@pytest.mark.parametrize("text", [
    "Built the dashboard in React and Tailwind",
    "React frontend with Redux",
    "Shipped a React Native app",
])
def test_the_library_still_matches(text):
    assert sm.find_term("React", text) == "react"


def test_one_bad_occurrence_does_not_hide_a_good_one():
    """The guard skips the occurrence, not the document. A CV describing both
    a ReAct agent and a React console has done React."""
    both = "Used a ReAct agent for tooling and a React frontend for the console"
    assert sm.find_term("React", both) == "react"
    form, snippet = sm.find_term_with_context("React", both)
    assert "frontend" in snippet


def test_the_evidence_snippet_quotes_the_real_occurrence():
    """A snippet centred on the rejected occurrence would show the user
    "ReAct agent" as the proof of their React experience."""
    text = "Built a ReAct agent. " + "x" * 300 + " Rewrote the admin UI in React."
    _, snippet = sm.find_term_with_context("React", text)
    assert "ReAct agent" not in snippet


def test_go_to_market_is_not_the_go_language():
    assert sm.find_term("Go", "Owned the go-to-market analysis for the launch") is None
    assert sm.find_term("Go", "Wrote the ingestion service in Go") == "go"


def test_false_friend_patterns_compile_and_name_known_terms():
    for term in sm.FALSE_FRIENDS:
        assert sm.normalize(term) in sm._FALSE_FRIENDS_RE
        assert sm._is_known(term), f"{term} is not in the vocabulary at all"


def test_the_fabricated_react_based_agent_does_not_score():
    """A tailored CV actually produced "Created a react-based LangGraph ReAct
    agent". The rewrite guard now refuses to write it; if one ever survives,
    the scorer must not pay for it either."""
    text = "Created a react-based LangGraph ReAct agent with dynamic tool selection"
    assert sm.find_term("React", text) is None
    assert sm.find_term("React", "Built a React-based admin dashboard") == "react"


# ---- vocabulary gaps found on real postings ----------------------------------

def test_agentic_ai_is_one_field_under_several_names():
    """"Built an autonomous multi-agent system of 8 specialized agents" against
    a requirement reading "Agentic AI solutions" fell through the tables to the
    LLM entailment pass, which caps at 0.75 -- a perfect match scored 6.75/9.
    These are naming variants of one field, not degrees of it."""
    for evidence in ("multi-agent system", "AI agents", "agentic systems",
                     "autonomous agents", "agent workflows"):
        assert sm.canonical(evidence) == "agentic ai", evidence
    assert sm.canonical("Agentic AI solutions") == "agentic ai"


def test_an_agent_framework_implies_the_field():
    for tool in ("LangGraph", "AutoGen", "CrewAI"):
        assert sm.implied_by("Agentic AI", tool), tool


@pytest.mark.parametrize("evidence", [
    "vision-language model", "CLIP", "YOLO", "MediaPipe", "OpenCV", "ResNet",
])
def test_a_vision_model_implies_computer_vision(evidence):
    """A graduation project reading "YOLO tracking, MediaPipe pose estimation
    and CTR-GCN motion classification" left a Computer Vision requirement
    earning skills-list credit, because none of those words were in any table."""
    assert sm.implied_by("Computer vision", evidence), evidence


@pytest.mark.parametrize("evidence", [
    "pose estimation", "object tracking", "image captioning", "face recognition",
])
def test_a_vision_subfield_is_a_kind_of_computer_vision(evidence):
    assert sm.entails("Computer vision", evidence), evidence


def test_the_tables_do_not_share_entries_across_relations():
    """The bug this caught in review: a block of prerequisite pairs pasted one
    dict too early made "yolo" an ALIAS of computer vision -- a different name
    for the field, worth full credit, rather than evidence of it. A term is
    evidence of a skill or another name for it, never both."""
    for skill, names in sm.ALIASES.items():
        canon = sm.canonical(skill)
        overlap = {sm.canonical(n) for n in names} & set(sm._IMPLIED_BY_NORM.get(canon, ()))
        assert not overlap, f"{skill}: {overlap} listed as both alias and prerequisite"
