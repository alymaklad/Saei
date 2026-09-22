"""
The semantic layer: retrieval nominates, guards filter, the LLM decides.

The embedding provider is faked throughout with a deterministic bag-of-words
vectoriser. That is not a shortcut — it is the only way to test routing at
all. A real model would make these tests a measurement of that model's
quality on six sentences, passing or failing for reasons that have nothing to
do with the code under test, and requiring a running Ollama to run at all.
What is being tested here is the plumbing and the guards: that retrieval
never scores, that a sibling never reaches the adjudicator, that the shortlist
is what the LLM is shown, and that every one of these paths degrades to the
previous behaviour when no provider is reachable.

Model quality is a separate question, answered by bench/report.py against the
golden dataset with whatever model is actually configured.
"""
import math
import re
from unittest.mock import patch

import pytest

import config
from agents import ats_agent, evidence_retrieval, semantic_matching
from agents.matching_types import EvidenceSpan, Requirement, SemanticCandidate
from services import embedding_cache
from services.semantic_index import SpanIndex


VOCAB = ["stakeholder", "coordinate", "physician", "product", "release", "scope",
         "aws", "cloud", "google", "deploy", "python", "fastapi", "model",
         "train", "latency", "billing", "refactor", "campaign", "roas", "spend"]


# The one property of a real embedding model these tests depend on: sibling
# technologies land near each other. AWS and GCP share the "cloud" dimension
# here for the same reason they are near-neighbours in a real vector space --
# they are the same KIND of thing. Without this the sibling guard would have
# nothing to guard against and its test would pass vacuously.
EXPANSIONS = {"aws": ["aws", "cloud"], "gcp": ["google", "cloud"],
              "google": ["google", "cloud"], "azure": ["azure", "cloud"]}


def _vector(text: str) -> list[float]:
    """Bag of words over a fixed vocabulary, L2-normalised.

    Crude on purpose: it gives "coordinated with physicians and product
    stakeholders" a real similarity to "stakeholder management" and near-zero
    to "refactored the billing service", which is the only property these
    tests need from an embedder.
    """
    words = set()
    for word in re.findall(r"[a-z]+", (text or "").lower()):
        words.update(EXPANSIONS.get(word, [word]))
    raw = [1.0 if any(w.startswith(term) or term.startswith(w) for w in words) else 0.0
           for term in VOCAB]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw] if norm else raw


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch):
    # The thresholds are pinned to the FAKE vectoriser's scale, not left at
    # the configured defaults. A bag-of-words cosine over a 20-word
    # vocabulary produces much lower absolute similarities than a real
    # embedding model does for the same pair of sentences, so asserting
    # against production thresholds here would test the fake. What these
    # tests pin is the routing: which band a hit falls in relative to the
    # thresholds, and what happens to it. The defaults themselves are
    # asserted separately below and calibrated by bench/report.py.
    monkeypatch.setattr(config, "SEMANTIC_SIMILARITY_IGNORE", 0.20)
    monkeypatch.setattr(config, "SEMANTIC_SIMILARITY_STRONG", 0.50)
    embedding_cache.reset()
    monkeypatch.setattr(embedding_cache.embeddings, "embed_texts",
                        lambda texts: [_vector(t) for t in texts])
    monkeypatch.setattr(embedding_cache.embeddings, "active_model", lambda: "fake-test-model")
    monkeypatch.setattr(embedding_cache.embeddings, "supports_instruction_prefix", lambda: False)
    monkeypatch.setattr(embedding_cache, "_save", lambda: None)
    monkeypatch.setattr(embedding_cache, "_load", lambda: {})
    yield
    embedding_cache.reset()


SPANS = [
    {"text": "Coordinated with physicians, engineers and product stakeholders to agree the release scope.",
     "source": "experience", "demonstrated": True, "concepts": []},
    {"text": "Refactored the billing service and cut p99 latency by 40%.",
     "source": "experience", "demonstrated": True, "concepts": []},
    {"text": "Deployed the training jobs to Google Cloud.",
     "source": "experience", "demonstrated": True, "concepts": ["google cloud platform"]},
    {"text": "Built a FastAPI service serving a PyTorch model.",
     "source": "project", "demonstrated": True, "concepts": ["fastapi", "pytorch", "python"]},
]

STAKEHOLDERS = Requirement(raw_text="Stakeholder management",
                           canonical_name="stakeholder management",
                           category="soft_skill", requirement_type="capability")


# ---- retrieval ---------------------------------------------------------------

def test_retrieval_finds_the_line_that_demonstrates_a_capability():
    """The case deterministic matching structurally cannot serve: the CV never
    writes the phrase, and no table will ever contain it."""
    index = evidence_retrieval.build_index(SPANS)
    candidates = evidence_retrieval.retrieve_top_evidence(STAKEHOLDERS, index)
    assert candidates
    assert "physicians" in candidates[0].span.text
    assert candidates[0].rank == 1


def test_candidates_are_ordered_and_carry_their_provenance():
    index = evidence_retrieval.build_index(SPANS)
    candidates = evidence_retrieval.retrieve_top_evidence(STAKEHOLDERS, index)
    similarities = [c.similarity for c in candidates]
    assert similarities == sorted(similarities, reverse=True)
    for expected_rank, candidate in enumerate(candidates, start=1):
        assert candidate.rank == expected_rank
        assert candidate.requirement_id == STAKEHOLDERS.id
        assert candidate.span.id == candidate.span_id
        assert candidate.band in ("strong", "ambiguous")


def test_lines_below_the_ignore_threshold_are_not_offered():
    """The bottom band exists so the adjudicator is never asked to rule on a
    line that has nothing to do with the requirement."""
    index = evidence_retrieval.build_index(SPANS)
    candidates = evidence_retrieval.retrieve_top_evidence(STAKEHOLDERS, index)
    offered = {c.span.text for c in candidates}
    assert not any("billing service" in text for text in offered)
    assert all(c.similarity >= config.SEMANTIC_SIMILARITY_IGNORE for c in candidates)


def test_retrieval_returns_candidates_not_matches():
    """The architectural rule, asserted rather than trusted: nothing retrieval
    returns has a relation, a credit or a score on it."""
    index = evidence_retrieval.build_index(SPANS)
    for candidate in evidence_retrieval.retrieve_top_evidence(STAKEHOLDERS, index):
        assert not hasattr(candidate, "credit")
        assert not hasattr(candidate, "relation")
        assert set(candidate.to_dict()) == {
            "requirement_id", "span_id", "similarity", "rank", "band"}


# ---- guards ------------------------------------------------------------------

def test_a_sibling_span_is_dropped_before_the_model_sees_it():
    """A "deployed to Google Cloud" line is necessarily among the nearest
    neighbours of an AWS requirement -- they are near-identical vectors
    BECAUSE they are the same kind of thing. Offering it to the adjudicator is
    inviting the one verdict the exclusive groups exist to refuse."""
    aws = Requirement(raw_text="AWS", canonical_name="amazon web services",
                      category="tool")
    index = evidence_retrieval.build_index(SPANS)
    candidates = evidence_retrieval.retrieve_top_evidence(aws, index)
    assert any("Google Cloud" in c.span.text for c in candidates), (
        "precondition: the sibling line should be retrieved")

    decision = semantic_matching.classify_semantic_candidates(aws, candidates)
    assert not any("Google Cloud" in span.text for span in decision.spans())
    assert decision.dropped_as_sibling >= 1


def test_a_span_with_other_content_survives_the_sibling_guard():
    """The test is whether the sibling is ALL the line has. A bullet that
    mentions GCP and Python is still legitimate evidence for Python."""
    mixed = [{"text": "Deployed Python services to Google Cloud.", "source": "experience",
              "demonstrated": True, "concepts": ["google cloud platform", "python"]}]
    aws = Requirement(raw_text="AWS", canonical_name="amazon web services", category="tool")
    candidates = [SemanticCandidate(requirement_id=aws.id, span_id="s1", similarity=0.9,
                                    rank=1, span=EvidenceSpan.from_dict(mixed[0]))]
    decision = semantic_matching.classify_semantic_candidates(aws, candidates)
    assert decision.candidates, "a line with other content is not only a sibling"


def test_a_strong_candidate_is_still_routed_to_the_llm():
    """The deliberate cost. Accepting on similarity alone would put points on
    the one signal that cannot tell AWS from GCP, so the band is recorded and
    the model still has to say yes."""
    index = evidence_retrieval.build_index(SPANS)
    candidates = evidence_retrieval.retrieve_top_evidence(STAKEHOLDERS, index)
    decision = semantic_matching.classify_semantic_candidates(STAKEHOLDERS, candidates)
    assert decision.route == "adjudicate"
    assert decision.best.similarity >= config.SEMANTIC_SIMILARITY_IGNORE


# ---- what the adjudicator is shown -------------------------------------------

def test_the_adjudication_prompt_quotes_only_the_shortlist():
    unmatched = [{"name": "Stakeholder management", "category": "soft_skill",
                  "importance": "required"}]
    spans = [dict(s) for s in SPANS]
    index = evidence_retrieval.build_index(spans)
    decisions = semantic_matching.shortlist(unmatched, spans, index=index)

    payload = ats_agent._entailment_payload(unmatched, spans, decisions)
    assert "physicians" in payload
    assert "billing service" not in payload, (
        "an irrelevant line must not be in the prompt at all")
    assert "Requirement: Stakeholder management" in payload


def test_without_retrieval_the_prompt_is_the_old_whole_cv_payload():
    """Degradation, not failure: no embedding provider means the same call
    with the same content as before the semantic layer existed."""
    unmatched = [{"name": "Stakeholder management", "category": "soft_skill",
                  "importance": "required"}]
    payload = ats_agent._entailment_payload(unmatched, [dict(s) for s in SPANS], None)
    assert "CV lines:" in payload
    assert "billing service" in payload


def test_a_requirement_retrieval_missed_is_still_asked_about(monkeypatch):
    """Retrieval NARROWS the question; it does not delete it.

    With an uncalibrated floor, dropping a requirement is indistinguishable
    from the CV not having the evidence: the row returns "Not found" either
    way. That cost a real tailored CV three requirements it had scored the
    run before, so the default is to fall back to the old whole-CV question
    for anything retrieval could not shortlist."""
    monkeypatch.setattr(config, "SEMANTIC_RETRIEVAL_STRICT", False)
    unmatched = [{"name": "Kubernetes cluster administration", "category": "tool",
                  "importance": "required"}]
    spans = [dict(s) for s in SPANS]
    decisions = semantic_matching.shortlist(unmatched, spans)

    payload = ats_agent._entailment_payload(unmatched, spans, decisions)
    assert payload is not None
    assert "Kubernetes cluster administration" in payload
    assert "billing service" in payload, "falls back to the general span list"


def test_strict_mode_does_drop_it(monkeypatch):
    """Once the floor is calibrated against the model in use, the plan's
    "low similarity -> no match" band is the cheaper and better behaviour --
    it is opt-in, not the default, because it is only safe after measurement."""
    monkeypatch.setattr(config, "SEMANTIC_RETRIEVAL_STRICT", True)
    unmatched = [{"name": "Kubernetes cluster administration", "category": "tool",
                  "importance": "required"}]
    spans = [dict(s) for s in SPANS]
    decisions = semantic_matching.shortlist(unmatched, spans)
    assert ats_agent._entailment_payload(unmatched, spans, decisions) is None


def test_a_shortlisted_requirement_is_still_asked_narrowly(monkeypatch):
    """The fallback must not undo the gain: a requirement retrieval DID
    resolve is still shown only its own lines."""
    monkeypatch.setattr(config, "SEMANTIC_RETRIEVAL_STRICT", False)
    unmatched = [{"name": "Stakeholder management", "category": "soft_skill",
                  "importance": "required"}]
    spans = [dict(s) for s in SPANS]
    decisions = semantic_matching.shortlist(unmatched, spans)
    payload = ats_agent._entailment_payload(unmatched, spans, decisions)
    assert "physicians" in payload
    assert "billing service" not in payload


# ---- degradation -------------------------------------------------------------

def test_scoring_survives_an_embedding_provider_that_is_down(monkeypatch):
    """The whole layer is an improvement to recall, never a dependency of
    scoring. A CV must still score with Ollama stopped."""
    def explode(_texts):
        raise ConnectionError("connection refused")

    embedding_cache.reset()
    monkeypatch.setattr(embedding_cache.embeddings, "embed_texts", explode)
    index = evidence_retrieval.build_index(SPANS)
    assert index.is_empty
    assert semantic_matching.shortlist(
        [{"name": "Stakeholder management", "category": "soft_skill"}], SPANS) == {}
    assert embedding_cache.available() is False
    assert "connection refused" in (embedding_cache.last_error() or "")


def test_a_failed_provider_is_not_retried_per_requirement(monkeypatch):
    """One clean degradation, not a slow one."""
    calls = {"n": 0}

    def explode(_texts):
        calls["n"] += 1
        raise ConnectionError("down")

    embedding_cache.reset()
    monkeypatch.setattr(embedding_cache.embeddings, "embed_texts", explode)
    for _ in range(5):
        embedding_cache.embed_many(["something"])
    assert calls["n"] == 1


def test_retrieval_can_be_switched_off_entirely(monkeypatch):
    monkeypatch.setattr(config, "SEMANTIC_RETRIEVAL", False)
    assert evidence_retrieval.build_index(SPANS).is_empty
    assert semantic_matching.shortlist(
        [{"name": "Stakeholder management", "category": "soft_skill"}], SPANS) == {}


# ---- the cache ---------------------------------------------------------------

def test_a_span_is_embedded_once_across_requirements(monkeypatch):
    """The property that makes the layer affordable: CV spans change when the
    CV changes, job postings arrive every run."""
    seen = {"texts": []}

    def counting(texts):
        seen["texts"].extend(texts)
        return [_vector(t) for t in texts]

    embedding_cache.reset()
    store = {}
    monkeypatch.setattr(embedding_cache, "_load", lambda: store)
    monkeypatch.setattr(embedding_cache, "_save", lambda: None)
    monkeypatch.setattr(embedding_cache.embeddings, "embed_texts", counting)

    evidence_retrieval.build_index(SPANS)
    first = len(seen["texts"])
    evidence_retrieval.build_index(SPANS)
    assert len(seen["texts"]) == first, "the second build should be all cache hits"


def test_the_cache_key_includes_the_model():
    """A vector from one model is meaningless against another's. This project
    has already shipped one cache key that omitted the provider."""
    a = embedding_cache.cache_key("Python developer", "model-a")
    b = embedding_cache.cache_key("Python developer", "model-b")
    assert a != b


def test_typography_variants_share_one_vector():
    """The rewrite's U+2011 hyphen would otherwise pay for the same sentence
    twice and index it as two different lines."""
    assert (embedding_cache.cache_key("tool-calling agents", "m")
            == embedding_cache.cache_key("tool‑calling agents", "m"))


# ---- the index ---------------------------------------------------------------

def test_an_index_over_nothing_is_a_valid_empty_index():
    assert SpanIndex.build([]).is_empty
    assert SpanIndex.build([{"text": "   ", "source": "cv"}]).is_empty


def test_a_query_of_the_wrong_dimension_returns_nothing():
    """Cheap insurance against a provider or model switch mid-cache."""
    index = evidence_retrieval.build_index(SPANS)
    assert index.search([0.1, 0.2, 0.3], top_k=3) == []


def test_the_configured_thresholds_are_ordered_and_in_range():
    """The fixture above overrides these, so they are checked directly. A
    strong band below the ignore band would silently route everything."""
    assert 0.0 <= config.SEMANTIC_SIMILARITY_IGNORE < config.SEMANTIC_SIMILARITY_STRONG <= 1.0
    assert config.SEMANTIC_TOP_K >= 1


# ---- task prefixes -----------------------------------------------------------

def test_spans_are_embedded_as_documents_and_requirements_as_queries(monkeypatch):
    """With nomic-embed-text the prefixes are required, and they differ by
    side. Getting this wrong produces no error and no warning -- just quietly
    worse retrieval -- so it is pinned by what actually reaches the provider."""
    seen = []
    embedding_cache.reset()
    monkeypatch.setattr(config, "OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")
    monkeypatch.setattr(embedding_cache.embeddings, "active_model",
                        lambda: "nomic-embed-text")
    monkeypatch.setattr(embedding_cache.embeddings, "embed_texts",
                        lambda texts: (seen.extend(texts), [_vector(t) for t in texts])[1])
    monkeypatch.setattr(embedding_cache, "_load", lambda: {})
    monkeypatch.setattr(embedding_cache, "_save", lambda: None)

    index = evidence_retrieval.build_index(SPANS)
    evidence_retrieval.retrieve_top_evidence(STAKEHOLDERS, index)

    documents = [t for t in seen if t.startswith("search_document: ")]
    queries = [t for t in seen if t.startswith("search_query: ")]
    assert len(documents) == len(SPANS), "every span is corpus text"
    assert queries == ["search_query: Stakeholder management"]
    assert not [t for t in seen if not t.startswith("search_")], (
        "nothing reaches nomic unprefixed")


def test_the_same_sentence_is_cached_twice_under_the_two_roles(monkeypatch):
    """A line embedded as a document and the same words embedded as a query
    are different vectors under this model, so they must not share a key."""
    monkeypatch.setattr(embedding_cache.embeddings, "active_model",
                        lambda: "nomic-embed-text")
    assert (embedding_cache.cache_key("Python", role="document")
            != embedding_cache.cache_key("Python", role="query"))


# ---- miscalibration is visible, not silent -----------------------------------

def test_the_report_flags_a_floor_nothing_can_clear(monkeypatch):
    """A floor above what the model scores a real match turns retrieval off
    without saying so: every capability requirement then reaches the
    adjudicator with nothing attached, and the score just looks low."""
    monkeypatch.setattr(config, "SEMANTIC_SIMILARITY_IGNORE", 0.999)
    spans = [dict(s) for s in SPANS]
    index = evidence_retrieval.build_index(spans)
    decisions = semantic_matching.shortlist(
        [{"name": "Stakeholder management", "category": "soft_skill"}], spans, index=index)

    report = semantic_matching.retrieval_report(decisions, index)
    assert report["calibration_warning"]
    assert "may be above" in report["calibration_warning"]


def test_the_report_flags_bands_below_the_models_scale(monkeypatch):
    """The opposite failure: a floor under what the model scores UNRELATED
    text. Retrieval then nominates indiscriminately and the shortlist is just
    the first k spans of the CV -- which looks like it is working."""
    monkeypatch.setattr(config, "SEMANTIC_SIMILARITY_IGNORE", 0.0)
    monkeypatch.setattr(config, "SEMANTIC_SIMILARITY_STRONG", 0.0)
    spans = [dict(s) for s in SPANS]
    index = evidence_retrieval.build_index(spans)
    decisions = semantic_matching.shortlist(
        [{"name": "Stakeholder management", "category": "soft_skill"}], spans, index=index)

    report = semantic_matching.retrieval_report(decisions, index)
    assert report["calibration_warning"]
    assert "discriminating" in report["calibration_warning"]


def test_a_healthy_distribution_raises_no_warning():
    spans = [dict(s) for s in SPANS]
    index = evidence_retrieval.build_index(spans)
    decisions = semantic_matching.shortlist(
        [{"name": "Stakeholder management", "category": "soft_skill"}], spans, index=index)

    report = semantic_matching.retrieval_report(decisions, index)
    assert report["calibration_warning"] is None
    assert report["similarity"]["count"] >= 1
