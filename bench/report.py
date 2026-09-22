"""
Measure the matcher against the golden dataset.

    python -m bench.report

The plan's evaluation section, and the thing that makes threshold calibration
an experiment rather than an opinion. `tests/test_golden_dataset.py` is the
pass/fail gate; this is the measurement, and it reports the two halves
separately because they answer different questions:

  Deterministic (Level A)   precision and recall over cases the tables should
                            settle. Precision here must stay at 1.00 — a
                            false positive is a point awarded for evidence
                            the candidate does not have.

  Semantic (Level B)        retrieval quality on the capability cases, which
                            no table can serve. Reported as "did the right
                            span come back, and at what rank" rather than as
                            a match, because retrieval does not decide
                            anything: recall@k is its ceiling, and the LLM
                            adjudicates what it nominates.

Running this needs whatever embedding provider is configured. Without one the
semantic section says so and the deterministic section still runs — the same
degradation the pipeline itself has.
"""
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from agents import ats_agent, evidence_retrieval, semantic_matching  # noqa: E402
from agents import embeddings as embeddings_module  # noqa: E402
from agents.matching_types import Requirement  # noqa: E402
from services import embedding_cache  # noqa: E402

BENCH = pathlib.Path(__file__).resolve().parent
CASES = json.loads((BENCH / "matching_cases.json").read_text(encoding="utf-8"))["cases"]

POSITIVE_LABELS = {"MATCH", "PARTIAL"}


def _spans(case):
    return [{"text": s["text"], "source": s["source"], "demonstrated": s["demonstrated"],
             "entry": "", "dated": True} for s in case["spans"]]


def deterministic_metrics():
    """Precision/recall over the cases Level A is supposed to settle."""
    tp = fp = fn = tn = 0
    failures = []

    for case in CASES:
        if case.get("semantic_expected") is not None:
            continue  # a capability case: Level A is not expected to fire
        outcome = ats_agent.match_requirement(case["requirement"], _spans(case))
        matched = outcome["credit"] > 0
        should = case["label"] in POSITIVE_LABELS or case["label"] == "INSUFFICIENT_EVIDENCE"

        if matched and should:
            tp += 1
        elif matched and not should:
            fp += 1
            failures.append((case["id"], "FALSE POSITIVE", outcome["relation"], case["why"]))
        elif not matched and should:
            fn += 1
            failures.append((case["id"], "FALSE NEGATIVE", "none", case["why"]))
        else:
            tn += 1

    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": precision,
            "recall": recall, "f1": f1, "failures": failures}


def semantic_metrics(top_k: int | None = None):
    """Recall@k for the capability cases, plus the band distribution.

    The band distribution is the calibration input: if every true positive
    lands in `ambiguous` and every sibling in `strong`, the thresholds are
    wrong regardless of what the accuracy says.
    """
    cases = [c for c in CASES if c.get("semantic_expected") is not None]
    if not cases:
        return None

    hits = misses = 0
    ranks, bands, rows = [], {}, []
    for case in cases:
        spans = _spans(case)
        index = evidence_retrieval.build_index(spans)
        if index.is_empty:
            return {"unavailable": embedding_cache.last_error() or
                    "no embedding provider reachable"}

        requirement = Requirement.from_dict(case["requirement"])
        candidates = evidence_retrieval.retrieve_top_evidence(
            requirement, index, top_k=top_k or config.SEMANTIC_TOP_K)
        decision = semantic_matching.classify_semantic_candidates(requirement, candidates)
        offered = decision.spans()

        wanted = case["semantic_expected"]
        # The first span of a capability case is the demonstrating line.
        target = case["spans"][0]["text"] if wanted else None
        found_rank = next((c.rank for c in decision.candidates
                           if target and c.span and c.span.text == target), None)

        if wanted and found_rank:
            hits += 1
            ranks.append(found_rank)
        elif wanted:
            misses += 1
        elif offered:
            misses += 1  # nothing should have been offered
        else:
            hits += 1

        bands[decision.band] = bands.get(decision.band, 0) + 1
        rows.append((case["id"], case["family"], wanted, found_rank,
                     round(decision.best.similarity, 3) if decision.best else None,
                     decision.band))

    total = hits + misses
    return {"recall_at_k": hits / total if total else 0.0,
            "mean_rank": sum(ranks) / len(ranks) if ranks else None,
            "bands": bands, "rows": rows, "cases": total}


# ---- threshold calibration ---------------------------------------------------

def labelled_pairs():
    """(requirement, span_text, is_positive) over the calibration cases.

    Positives are the demonstrating line of each `semantic_expected: true`
    case. Negatives are everything else the same requirement could be offered:
    the other spans of its own case, every span of every OTHER case, and the
    sibling cases -- "deployed to Google Cloud" against an AWS requirement is
    the hardest negative in the set, and the one a similarity threshold is
    least able to reject on its own.

    Cross-case negatives are what make this worth fitting on: a threshold
    chosen from positives alone is just the lowest positive, which accepts
    everything.
    """
    calibration = [c for c in CASES if c.get("semantic_expected") is not None]
    sibling = [c for c in CASES if c["label"] == "SIBLING"]

    every_span = [(c["id"], c.get("calibration_group"), s["text"])
                  for c in CASES for s in c["spans"]]
    pairs = []

    for case in calibration:
        requirement = Requirement.from_dict(case["requirement"])
        positive_text = case["spans"][0]["text"] if case["semantic_expected"] else None
        group = case.get("calibration_group")
        # A `semantic_expected: false` case asserts something narrow: THESE
        # spans do not support it. It says nothing about the rest of the
        # corpus, and cross-pairing it would be actively wrong -- cap_04 and
        # cap_01 carry the SAME requirement, so cap_04's cross pairs would
        # label cap_01's demonstrating line as a negative for the very
        # requirement it demonstrates.
        spans_to_pair = (every_span if positive_text
                         else [(case["id"], group, s["text"]) for s in case["spans"]])

        for case_id, other_group, text in spans_to_pair:
            own = case_id == case["id"]
            # Cases that mean the same thing are not each other's negatives.
            # "Coordinated with physicians and product stakeholders" really
            # does demonstrate cross-functional collaboration; counting that
            # pair as false would make an accurate model look unable to
            # separate the classes, and would drag the suggested floor up
            # until it started losing real matches.
            if not own and group and other_group == group:
                continue
            # Positive on TEXT, not on which case the text was filed under.
            # cap_02's demonstrating line also appears in cap_09 (where a
            # different requirement makes it a negative); labelling this
            # requirement's own evidence as a negative because it was reached
            # through the other case would be flatly wrong, and it is the kind
            # of wrong that makes a good model look unusable.
            is_positive = bool(positive_text) and text == positive_text
            pairs.append((requirement, text, is_positive, case["id"],
                          "own" if own else "cross"))

    for case in sibling:
        requirement = Requirement.from_dict(case["requirement"])
        for span in case["spans"]:
            pairs.append((requirement, span["text"], False, case["id"], "sibling"))
    return pairs


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def calibrate():
    """Measure the real similarity distribution and derive the two thresholds.

    The rule, stated so the numbers are reproducible rather than chosen:

      classes separate (no negative reaches the lowest positive):
        IGNORE  inside the gap, a quarter of the way up from the highest
                negative. A higher floor buys nothing -- the negatives are
                already under it -- and risks losing a positive that the next
                CV happens to score slightly lower.
        STRONG  the lowest positive: every positive is unambiguous.

      classes overlap:
        IGNORE  just under the lowest positive. The asymmetry is deliberate:
                a positive under the floor is an unrecoverable miss, because
                retrieval is the only path a capability has and nothing
                downstream can retrieve what retrieval never returned. A
                negative above the floor only costs a line in a prompt that
                the adjudicator then rejects.
        STRONG  above every negative, or NONE when a negative sits at the top
                of the scale and no such value exists.

    An overlap is a finding, not a failure: it means the thresholds cannot do
    this work under this model and the adjudication call is carrying it --
    which is exactly why the design keeps the LLM in the loop and never lets
    similarity score anything on its own.
    """
    pairs = labelled_pairs()
    if not pairs:
        return {"unavailable": "no calibration cases in the dataset"}

    texts = sorted({text for _, text, _, _, _ in pairs})
    vectors = embedding_cache.embed_many(texts, role="document")
    if not any(vectors):
        return {"unavailable": embedding_cache.last_error() or
                "no embedding provider reachable"}
    by_text = dict(zip(texts, vectors))

    measured = []
    for requirement, text, is_positive, case_id, kind in pairs:
        query = embedding_cache.embed_one(requirement.raw_text, role="query")
        document = by_text.get(text)
        if not query or not document:
            continue
        similarity = embeddings_module.cosine_similarity(query, document)
        measured.append({"requirement": requirement.raw_text, "text": text,
                         "positive": is_positive, "case": case_id, "kind": kind,
                         "similarity": round(similarity, 4)})

    positives = [m["similarity"] for m in measured if m["positive"]]
    negatives = [m["similarity"] for m in measured if not m["positive"]]
    siblings = [m["similarity"] for m in measured if m["kind"] == "sibling"]
    if not positives or not negatives:
        return {"unavailable": "not enough labelled pairs to fit thresholds"}

    lowest_positive, highest_negative = min(positives), max(negatives)
    separates = highest_negative < lowest_positive

    if separates:
        # There is a gap between the classes. Put the floor INSIDE it, biased
        # toward the low end: an unnecessarily high floor buys nothing (the
        # negatives are already below it) and risks losing a positive the
        # next CV produces slightly lower. The strong band starts where the
        # positives do.
        ignore = round(highest_negative + (lowest_positive - highest_negative) * 0.25, 3)
        strong = round(lowest_positive, 3)
    else:
        # Overlap. Recall is the thing to protect: retrieval is the only path
        # a capability has, so a positive under the floor is an unrecoverable
        # miss, while a negative above it merely costs a line in a prompt the
        # adjudicator then rejects. So the floor goes under the lowest
        # positive and the asymmetry is deliberate.
        ignore = max(0.0, round(lowest_positive - 0.01, 3))
        # "No negative reaches this" may have no answer at all when one sits
        # at the top of the scale. Saying so is the honest result -- a band of
        # 1.0 that nothing can enter is not a calibration, it is a number that
        # looks like one.
        ceiling = round(highest_negative + 0.005, 3)
        strong = ceiling if ceiling <= 1.0 else None

    above_floor = [n for n in negatives if n >= ignore]

    return {
        "pairs": len(measured), "positives": len(positives), "negatives": len(negatives),
        "positive_range": (min(positives), _percentile(positives, 0.5), max(positives)),
        "negative_range": (min(negatives), _percentile(negatives, 0.5), max(negatives)),
        "sibling_max": max(siblings) if siblings else None,
        "sibling_median": _percentile(siblings, 0.5),
        "suggested_ignore": ignore,
        "suggested_strong": strong,
        "separates": separates,
        "negatives_above_floor": len(above_floor),
        "spread": round(_percentile(positives, 0.5) - _percentile(negatives, 0.5), 4),
        "rows": sorted(measured, key=lambda m: -m["similarity"]),
    }


def print_calibration(result: dict) -> None:
    print("\nThreshold calibration")
    if "unavailable" in result:
        print(f"  unavailable: {result['unavailable']}")
        print("  Run this on a machine with the configured embedding provider up:")
        print("    ollama pull nomic-embed-text && python -m bench.report --calibrate")
        return

    low, mid, high = result["positive_range"]
    nlow, nmid, nhigh = result["negative_range"]
    print(f"  {result['pairs']} labelled pairs "
          f"({result['positives']} positive, {result['negatives']} negative)")
    print(f"  positives  min {low:.3f}  median {mid:.3f}  max {high:.3f}")
    print(f"  negatives  min {nlow:.3f}  median {nmid:.3f}  max {nhigh:.3f}")
    if result["sibling_max"] is not None:
        print(f"  siblings   median {result['sibling_median']:.3f}  "
              f"max {result['sibling_max']:.3f}   "
              f"(these are rejected by the exclusive-group guard, not by a threshold)")
    print(f"  median separation {result['spread']:+.3f}")
    print()
    if result["separates"]:
        print("  The classes separate cleanly under this model.")
    else:
        print("  The classes DO NOT separate: some negative scores as high as a")
        print("  positive. That is a finding about the model, not a bug -- it is")
        print("  why nothing here scores on similarity alone and every candidate")
        print("  still goes to the adjudicator.")
    print(f"\n  suggested, from the rule in calibrate():")
    print(f"    SEMANTIC_SIMILARITY_IGNORE={result['suggested_ignore']}")
    if result["suggested_strong"] is None:
        print("    SEMANTIC_SIMILARITY_STRONG=  (no value works: a negative scores")
        print("                                  at the top of the scale, so no band")
        print("                                  excludes them all)")
    else:
        print(f"    SEMANTIC_SIMILARITY_STRONG={result['suggested_strong']}")
    print(f"  currently: ignore {config.SEMANTIC_SIMILARITY_IGNORE} / "
          f"strong {config.SEMANTIC_SIMILARITY_STRONG}")
    print(f"  {result['negatives_above_floor']} negatives sit above the suggested floor "
          f"and would reach the adjudicator")
    print("\n  closest pairs (highest similarity first):")
    print("  sim    label  kind     requirement                    span")
    for row in result["rows"][:12]:
        label = "POS" if row["positive"] else "neg"
        print(f"  {row['similarity']:.3f}  {label:<6} {row['kind']:<8} "
              f"{row['requirement'][:29]:<30} {row['text'][:44]}")


def family_coverage():
    out = {}
    for case in CASES:
        out.setdefault(case["family"], {"cases": 0})["cases"] += 1
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    calibrating = "--calibrate" in argv
    started = time.perf_counter()
    print("=" * 72)
    print("MATCHING BENCH".center(72))
    print("=" * 72)

    det = deterministic_metrics()
    print(f"\nLevel A — deterministic ({det['tp'] + det['fp'] + det['fn'] + det['tn']} cases)")
    print(f"  precision {det['precision']:.3f}   recall {det['recall']:.3f}   f1 {det['f1']:.3f}")
    print(f"  tp {det['tp']}  fp {det['fp']}  fn {det['fn']}  tn {det['tn']}")
    if det["failures"]:
        print("\n  failures:")
        for case_id, kind, relation, why in det["failures"]:
            print(f"    {case_id:<12} {kind:<15} ({relation}) {why}")
    else:
        print("  no false positives, no false negatives")

    print(f"\nLevel B — semantic retrieval "
          f"(provider: {config.EMBEDDING_PROVIDER}, top_k {config.SEMANTIC_TOP_K})")
    sem = semantic_metrics()
    if sem is None:
        print("  no capability cases in the dataset")
    elif "unavailable" in sem:
        print(f"  unavailable: {sem['unavailable']}")
        print("  (the deterministic path above is unaffected — this is the same")
        print("   degradation the pipeline itself has when no provider is reachable)")
    else:
        mean_rank = f"{sem['mean_rank']:.2f}" if sem["mean_rank"] else "n/a"
        print(f"  recall@k {sem['recall_at_k']:.3f} over {sem['cases']} cases, "
              f"mean rank of the right span {mean_rank}")
        print(f"  bands: {sem['bands']}")
        print(f"  thresholds: ignore {config.SEMANTIC_SIMILARITY_IGNORE} / "
              f"strong {config.SEMANTIC_SIMILARITY_STRONG}")
        print("\n  case          family              expected  rank  sim    band")
        for case_id, family, wanted, rank, sim, band in sem["rows"]:
            print(f"  {case_id:<13} {family:<19} {str(wanted):<9} "
                  f"{str(rank or '-'):<5} {str(sim or '-'):<6} {band}")

    if calibrating:
        print_calibration(calibrate())

    print("\nCoverage by job family")
    for family, data in sorted(family_coverage().items()):
        print(f"  {family:<24} {data['cases']} cases")

    if not calibrating:
        print("\n(--calibrate measures the real similarity distribution and derives"
              "\n the two thresholds from it; needs the embedding provider up)")

    print(f"\nRan in {time.perf_counter() - started:.2f}s")
    return 0 if not det["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
