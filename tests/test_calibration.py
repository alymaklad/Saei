"""
The threshold calibrator.

`bench/report.py --calibrate` is what turns SEMANTIC_SIMILARITY_IGNORE and
_STRONG from guesses into measurements — but only if the rule it applies is
right, and that rule has to be tested somewhere the distribution is known.
So these tests drive it with synthetic embedders whose positive/negative
separation is chosen, and check the derived thresholds behave.

The two failure modes worth catching:

  a floor that is too HIGH silently loses a real match. Retrieval is the only
  thing that can surface a capability, so a positive below the floor is never
  offered to the adjudicator and cannot be recovered downstream.

  a claim of clean separation when the classes overlap would justify
  accepting on similarity alone, which is the one thing this design refuses
  to do.
"""
import hashlib
import math
from unittest.mock import patch

import pytest

from bench import report
from services import embedding_cache


# The dataset's positive pairs, as (requirement fragment, span fragment). The
# fake embedder places each pair at its own angle so the pair is close and
# everything else is not -- a controlled distribution, which is the only way
# to test that the FITTING RULE is right rather than testing a model.
_PLANTED_PAIRS = [
    ("stakeholder management", "physicians"),
    ("financial modelling", "three-statement forecasts"),
    ("campaign performance", "roas"),
    ("user research", "usability"),
    ("vendor management", "logistics suppliers"),
    ("experiment design", "a/b tests"),
    ("cross-functional collaboration", "design, backend and clinical"),
]


def _planted(separation: float):
    """An embedder with a known positive/negative split.

    Each planted pair sits at its own angle. Everything else is spread by a
    hash, so unrelated texts are far from each other as well as from the
    requirements -- without that, every negative collapses onto one point and
    scores 1.0 against itself, which measures nothing.

    `separation` moves the unrelated cloud away from the planted angles: 1.0
    is a clean split, 0.0 an overlap the calibrator must refuse to call clean.
    """
    def _random_unit(text: str, dimensions: int = 24) -> list[float]:
        """A deterministic pseudo-random unit vector.

        High-dimensional random vectors are near-orthogonal, which is exactly
        how a real embedding model behaves for unrelated text. An earlier
        version of this fake spread unrelated texts along a single arc, where
        two of them inevitably collided at 0.999 and made the calibrator look
        broken for a reason that had nothing to do with the calibrator.
        """
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [(seed[i % len(seed)] / 127.5) - 1.0 for i in range(dimensions)]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]

    def vector(text: str):
        lowered = (text or "").lower()
        for index, (requirement, span) in enumerate(_PLANTED_PAIRS):
            if requirement in lowered or span in lowered:
                # Both halves of a planted pair get the SAME vector: a
                # perfect positive, so the fit is measured against negatives
                # rather than against how well the fake models paraphrase.
                return _random_unit(f"planted-{index}")
        unrelated = _random_unit(lowered)
        if separation >= 1.0:
            return unrelated
        # Below full separation, pull unrelated text toward a planted pair so
        # the classes start to overlap.
        planted = _random_unit("planted-0")
        mix = 1.0 - separation
        blended = [(1 - mix) * u + mix * p for u, p in zip(unrelated, planted)]
        norm = math.sqrt(sum(x * x for x in blended)) or 1.0
        return [x / norm for x in blended]
    return vector


@pytest.fixture
def fake_provider(monkeypatch):
    def install(separation):
        embedding_cache.reset()
        vector = _planted(separation)
        monkeypatch.setattr(embedding_cache.embeddings, "embed_texts",
                            lambda texts: [vector(t) for t in texts])
        monkeypatch.setattr(embedding_cache.embeddings, "active_model",
                            lambda: "fake-calibration-model")
        monkeypatch.setattr(embedding_cache, "_load", lambda: {})
        monkeypatch.setattr(embedding_cache, "_save", lambda: None)
    yield install
    embedding_cache.reset()


def test_the_pair_set_has_both_classes_and_hard_negatives():
    """A threshold fitted on positives alone is just the lowest positive,
    which accepts everything. The cross-case and sibling negatives are what
    make the fit mean anything."""
    pairs = report.labelled_pairs()
    positives = [p for p in pairs if p[2]]
    negatives = [p for p in pairs if not p[2]]
    kinds = {p[4] for p in pairs}

    assert len(positives) >= 5, "too few positives to fit on"
    assert len(negatives) > 10 * len(positives), "negatives should dominate"
    assert {"own", "cross", "sibling"} <= kinds


def test_the_floor_never_loses_a_positive(fake_provider):
    """The property that matters most: retrieval is the only path a capability
    has, so a positive below the floor is an unrecoverable miss."""
    fake_provider(separation=1.0)
    result = report.calibrate()
    assert "unavailable" not in result, result
    lowest_positive = result["positive_range"][0]
    assert result["suggested_ignore"] <= lowest_positive


def test_a_clean_split_is_reported_as_separating(fake_provider):
    fake_provider(separation=1.0)
    result = report.calibrate()
    assert result["separates"] is True
    assert result["suggested_strong"] > result["suggested_ignore"]
    assert result["spread"] > 0


def test_an_overlapping_distribution_is_reported_honestly(fake_provider):
    """When negatives score as high as positives the calibrator must say so
    rather than emit a confident pair of numbers. Claiming separation here
    would be the justification for accepting on similarity alone."""
    fake_provider(separation=0.0)
    result = report.calibrate()
    assert result["separates"] is False
    assert result["negatives_above_floor"] > 0


def test_calibration_without_a_provider_says_so_instead_of_guessing(monkeypatch):
    embedding_cache.reset()

    def explode(_texts):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(embedding_cache.embeddings, "embed_texts", explode)
    monkeypatch.setattr(embedding_cache, "_load", lambda: {})
    result = report.calibrate()
    assert "unavailable" in result
    assert "connection refused" in result["unavailable"]
    embedding_cache.reset()


def test_the_report_prints_the_env_lines(capsys, fake_provider):
    """The output has to be actionable: two lines to paste, and the current
    values beside them."""
    fake_provider(separation=1.0)
    report.print_calibration(report.calibrate())
    printed = capsys.readouterr().out
    assert "SEMANTIC_SIMILARITY_IGNORE=" in printed
    assert "SEMANTIC_SIMILARITY_STRONG=" in printed
    assert "currently:" in printed


def test_the_unavailable_message_says_what_to_run(capsys):
    report.print_calibration({"unavailable": "no embedding provider reachable"})
    printed = capsys.readouterr().out
    assert "ollama pull nomic-embed-text" in printed
