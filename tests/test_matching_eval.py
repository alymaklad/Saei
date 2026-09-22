"""
Labelled evaluation set for requirement/evidence matching.

The point of this file is to stop the ontology being tuned by anecdote. Every
change to ALIASES, HYPONYMS or EXCLUSIVE_GROUPS should be judged here, not
against whichever example happened to be on screen.

Run it as a script for the full report:

    python tests/test_matching_eval.py

Expected labels:

    exact   the requirement's own word appears
    alias   a true synonym appears -- interchangeable, full credit
    subset  the CV term is a KIND OF the requirement -- partial credit
    none    not satisfied

The metric that matters most is FALSE EQUIVALENCE: awarding exact/alias credit
where the truth is subset or none. That is the dangerous direction, because it
inflates the score and makes the agent apply to jobs the candidate doesn't
match. A false negative costs an opportunity; a false equivalence costs
credibility with an employer. They are graded separately below and the false
equivalence budget is zero.

Cases are matched through the real match_requirement() against a single
demonstrated span, so this exercises the shipping code path rather than a
reimplementation of it.
"""
import collections

import pytest

from agents import ats_agent


# (requirement, CV evidence, expected relation, note)
CASES: list[tuple[str, str, str, str]] = [
    # ---- exact -------------------------------------------------------------
    ("Python", "Developed Python applications for data pipelines", "exact", ""),
    ("PyTorch", "Trained ResNet50 models using PyTorch", "exact", ""),
    ("Docker", "Containerised the service with Docker", "exact", ""),
    ("C++", "Wrote performance-critical C++ modules", "exact", "symbols survive"),
    ("AWS", "Deployed the inference API on AWS", "exact", ""),
    ("FastAPI", "Built a FastAPI backend", "exact", ""),

    # ---- alias: true synonyms, full credit ---------------------------------
    ("REST API", "Led REST API development for the platform", "alias", ""),
    ("RESTful APIs", "Built REST APIs consumed by mobile clients", "alias", ""),
    ("PostgreSQL", "Tuned Postgres queries under load", "alias", ""),
    ("Kubernetes", "Operated k8s clusters in production", "alias", ""),
    ("JavaScript", "Shipped features in JS across the stack", "alias", ""),
    ("TypeScript", "Migrated the TS codebase to strict mode", "alias", ""),
    ("Machine Learning", "Owned ML pipelines end to end", "alias", ""),
    ("Deep Learning", "Deployed DL models to production", "alias", ""),
    ("Natural Language Processing", "Research in natural language processing", "exact",
     "same words, different case"),
    ("NLP", "Research in natural language processing", "alias", ""),
    ("MongoDB", "Designed Mongo aggregation pipelines", "alias", ""),
    ("Retrieval Augmented Generation", "Built a retrieval-augmented generation stack",
     "alias", ""),
    ("PEFT", "Applied parameter-efficient fine-tuning", "alias", "abbreviation only"),
    ("VLM", "Trained a vision-language model on CT scans", "alias", ""),
    ("Continuous Integration", "Maintained the CI pipeline", "alias", ""),
    ("Computer Vision", "MSc thesis in computer vision", "exact",
     "same words, different case"),
    ("CV", "MSc thesis in computer vision", "alias", "abbreviation"),

    # ---- generic packaging words around a real skill ------------------------
    # Postings write "X systems", "X pipelines", "experience with X". The
    # requirement is X. Left unreduced these miss the tables entirely, cost an
    # LLM call, and land at semantic support (0.75) instead of a direct hit --
    # marking the CV down for how the POSTING was phrased.
    ("RAG systems", "Built a production RAG platform over university PDFs and slides",
     "alias", "the case that surfaced this"),
    ("Python programming", "Five years of Python across data and backend", "alias", ""),
    ("Experience with PyTorch", "Trained segmentation models in PyTorch", "alias", ""),
    ("Kubernetes experience", "Ran production workloads on Kubernetes", "alias", ""),
    ("Strong SQL skills", "Wrote reporting queries in SQL daily", "alias", ""),
    ("CI/CD pipelines", "Owned the CI/CD setup for three services", "alias", ""),
    ("Docker tooling", "Published Docker images from CI", "alias",
     "remainder is not in any table, but is still the real skill"),
    ("AWS services", "Ran the platform on AWS", "alias", ""),
    # ...but the packaging must NOT be stripped when the phrase is itself a
    # meaningful multi-word term.
    ("Deep learning framework", "Everything was written in PyTorch", "subset",
     "must not collapse to 'deep learning'"),
    ("Programming language", "Five years of Python", "subset",
     "must not collapse to 'programming'"),
    ("Software development", "Managed the AWS billing account", "none",
     "'software' alone is too generic to match on"),

    # ---- subset: a kind of the requirement, partial credit -----------------
    ("Deep Learning", "Trained a CNN for tumour segmentation", "subset",
     "the canonical example"),
    ("Machine Learning", "Trained a CNN for tumour segmentation", "subset",
     "transitively narrower"),
    ("PEFT", "Fine-tuned Llama with QLoRA", "subset", "QLoRA is one PEFT method"),
    ("Fine tuning", "Trained LoRA adapters for the domain model", "subset", ""),
    ("Version Control", "Used Git daily across three teams", "subset",
     "Git is one VCS, not the concept"),
    ("Cloud Platform", "Deployed the service on AWS", "subset", ""),
    ("Multimodal model", "Built a vision-language model", "subset",
     "VLM is one kind of multimodal"),
    ("Foundation model", "Served large language models in production", "subset", ""),
    ("Data structures and algorithms", "Strong algorithms background", "subset",
     "half of the compound"),
    ("CI/CD", "Owned continuous integration for the monorepo", "subset",
     "CI is half of CI/CD"),
    ("Relational database", "Modelled the schema in PostgreSQL", "subset", ""),
    ("SQL", "Wrote complex joins and window functions", "subset",
     "demonstrated by activity, not named"),
    ("SQL", "Built the data layer with SQLAlchemy", "subset",
     "an ORM is real database work but can abstract the SQL away"),
    ("Object relational mapping", "Built the data layer with SQLAlchemy", "subset", ""),
    ("SQL", "Optimised MySQL queries under load", "subset", ""),
    ("NoSQL", "Stored session data in MongoDB", "subset", ""),
    ("Vector database", "Indexed embeddings with FAISS", "subset", ""),
    ("Web framework", "Built the API in FastAPI", "subset", ""),
    ("Frontend framework", "Wrote the dashboard in React", "subset", ""),
    ("Programming language", "Five years of Python", "subset", ""),
    ("Computer Vision", "Worked on object detection for retail", "subset", ""),
    ("Natural Language Processing", "Fine-tuned BERT for classification", "subset", ""),
    ("MLOps", "Tracked experiments with MLflow", "subset", ""),
    ("Linux", "Administered Ubuntu servers", "subset", ""),
    ("Agile", "Ran Scrum ceremonies for the team", "subset", ""),
    ("Containerization", "Published Docker images to the registry", "subset", ""),
    ("Container orchestration", "Ran workloads on Kubernetes", "subset", ""),
    ("Deep learning framework", "Everything was built in PyTorch", "subset", ""),
    ("Generative AI", "Heavy prompt engineering for the assistant", "subset", ""),
    ("Data analysis", "Cleaned the dataset with pandas", "subset", ""),
    ("Data visualization", "Charted results with matplotlib", "subset", ""),
    ("Agent framework", "Orchestrated tools with LangGraph", "subset", ""),
    ("Problem solving", "Recognised for analytical thinking", "subset",
     "overlapping, not identical"),
    ("Teamwork", "Drove cross-functional collaboration with design", "subset", ""),

    # ---- none: exclusive siblings ------------------------------------------
    ("AWS", "Deployed everything to Google Cloud Platform", "none",
     "THE case: high embedding similarity, zero credit"),
    ("AWS", "Ran the platform on Azure", "none", ""),
    ("PyTorch", "All models were written in TensorFlow", "none", ""),
    ("Python", "Ten years of Java backend work", "none", ""),
    ("R", "Statistical modelling in Python", "none", ""),
    ("React", "Built the frontend in Angular", "none", ""),
    ("PostgreSQL", "Data lived in MySQL", "none", ""),
    ("MongoDB", "Used Cassandra for the write path", "none", ""),
    ("Django", "The service is a Flask app", "none", ""),
    ("Jenkins", "CI ran on GitHub Actions", "none", ""),
    ("Kubernetes", "Scheduled containers with Docker Swarm", "none", ""),
    ("C#", "Systems programming in C++", "none", ""),

    # ---- none: wrong direction (general evidence, specific requirement) ----
    ("CNN", "Broad deep learning experience", "none",
     "deep learning does not evidence CNNs specifically"),
    ("QLoRA", "Familiar with PEFT methods", "none", ""),
    ("VLM", "Worked with multimodal models", "none", ""),
    ("Git", "Comfortable with version control", "none", ""),
    ("LLM", "Research on foundation models", "none", ""),
    ("PostgreSQL", "Experience with relational databases", "none", ""),
    ("Kubernetes", "Understands container orchestration", "none", ""),

    # ---- none: the substring false positives from the live database --------
    ("Scala", "Built scalable distributed systems", "none",
     "matched under the old substring rule"),
    ("R", "Aly Tarek Maklad, Cairo", "none", "matched under the old rule"),
    ("Go", "Django services behind Google Cloud", "none", ""),
    ("Vector", "Directed the team through a rewrite", "none", ""),
    ("Node", "Built with Node.js and Express", "none",
     "conservative: Node.js should not satisfy a bare 'Node' token match"),

    # ---- none: simply unrelated --------------------------------------------
    ("AWS", "Trained CNN models for medical imaging", "none", ""),
    ("PyTorch", "Managed a team of four engineers", "none", ""),
]


# Cases the curated tables do NOT resolve, and are not expected to. They fall
# through to the LLM entailment pass at runtime, so a "none" here means "no
# deterministic answer", not "no match".
#
# This list exists because CASES above scores 100%, and a saturated evaluation
# set measures nothing. These are the queue for the next ontology pass: each
# one is either a table entry worth adding, or a genuine judgement call that
# should stay with the model. They are REPORTED, never asserted -- turning them
# into failures would just pressure the tables to grow without evidence that
# growing them helps.
KNOWN_GAPS: list[tuple[str, str, str]] = [
    ("Medical imaging", "Developed CT-RATE based 3D CT classification models",
     "domain knowledge; the link is real but not lexical"),
    ("Time series forecasting", "Built ARIMA and Prophet models",
     "candidate table entry"),
    ("Distributed systems", "Sharded the service across three regions",
     "described by behaviour, not by name"),
    ("A/B testing", "Ran controlled experiments on the checkout flow",
     "candidate table entry"),
    ("Terraform", "Managed infrastructure as code with Pulumi",
     "siblings, but not yet in an exclusive group — currently just unmatched"),
    ("Microservices", "Split the monolith into independently deployed services",
     "paraphrase"),
    ("Docker", "Wrote a multi-stage Dockerfile",
     "morphological, not covered by aliasing"),
    ("Team leadership", "Mentored three junior engineers and ran hiring",
     "judgement call, better left to the model"),
    ("Stakeholder management", "Presented quarterly results to the exec team",
     "judgement call, better left to the model"),
]


def _classify(requirement: str, evidence: str) -> str:
    outcome = ats_agent.match_requirement(
        {"name": requirement, "canonical": None,
         "category": "technical_skill", "importance": "required"},
        [{"text": evidence, "source": "experience", "demonstrated": True}],
    )
    return outcome["relation"]


def evaluate() -> dict:
    """Run every case and bucket the outcomes."""
    rows, confusion = [], collections.Counter()
    for requirement, evidence, expected, note in CASES:
        actual = _classify(requirement, evidence)
        rows.append((requirement, evidence, expected, actual, note))
        confusion[(expected, actual)] += 1

    correct = sum(n for (e, a), n in confusion.items() if e == a)
    full_credit = {"exact", "alias"}

    # Awarding full credit where the truth is partial or nothing. The
    # dangerous direction: it inflates the score and sends the agent after jobs
    # the candidate does not match.
    false_equivalence = [r for r in rows
                         if r[3] in full_credit and r[2] not in full_credit]
    # Awarding anything where the truth is nothing.
    false_positive = [r for r in rows if r[2] == "none" and r[3] != "none"]
    # Awarding nothing where there was real evidence. Costs an opportunity.
    false_negative = [r for r in rows if r[2] != "none" and r[3] == "none"]
    # Right that it matched, wrong about how strongly.
    misgraded = [r for r in rows
                 if r[2] != r[3] and r[2] != "none" and r[3] != "none"]

    return {
        "rows": rows, "confusion": confusion,
        "total": len(rows), "correct": correct,
        "accuracy": correct / len(rows),
        "false_equivalence": false_equivalence,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "misgraded": misgraded,
    }


# ---- the assertions ---------------------------------------------------------

@pytest.fixture(scope="module")
def report():
    return evaluate()


def test_no_false_equivalences(report):
    """Budget is zero. Full credit for something that is merely related is how
    a matcher quietly becomes the keyword filter it replaced."""
    offenders = [f"{r[0]!r} vs {r[1]!r}: expected {r[2]}, got {r[3]}"
                 for r in report["false_equivalence"]]
    assert not offenders, "false equivalences:\n  " + "\n  ".join(offenders)


def test_no_credit_for_unsatisfied_requirements(report):
    offenders = [f"{r[0]!r} vs {r[1]!r}: expected none, got {r[3]}"
                 for r in report["false_positive"]]
    assert not offenders, "credit awarded with no support:\n  " + "\n  ".join(offenders)


def test_accuracy_meets_the_agreed_floor(report):
    """A floor, not a target. Raise it as the ontology improves; never lower it
    to make a change pass."""
    assert report["accuracy"] >= 0.90, (
        f"accuracy {report['accuracy']:.0%}\n" + _format_failures(report))


def test_false_negatives_stay_bounded(report):
    """Missing real evidence is the acceptable failure direction, but it is not
    free -- every one is a job the candidate is under-scored for."""
    assert len(report["false_negative"]) <= 4, _format_failures(report)


def test_every_case_is_labelled_with_a_known_relation():
    valid = {"exact", "alias", "subset", "none"}
    for requirement, _evidence, expected, _note in CASES:
        assert expected in valid, f"{requirement}: bad expected label {expected!r}"


def test_known_gaps_really_are_unresolved_by_the_tables():
    """If one of these starts resolving, the tables grew to cover it and it
    should be promoted into CASES with a proper expected label -- otherwise the
    gap list slowly turns into a list of things that silently already work."""
    resolved = [(r, e) for r, e, _why in KNOWN_GAPS if _classify(r, e) != "none"]
    assert not resolved, (
        "these now match deterministically — move them into CASES:\n  "
        + "\n  ".join(f"{r!r} vs {e!r}" for r, e in resolved))


def test_the_set_covers_every_relation_and_is_big_enough():
    """A set weighted towards one outcome measures very little."""
    counts = collections.Counter(c[2] for c in CASES)
    assert len(CASES) >= 60
    for relation in ("exact", "alias", "subset", "none"):
        assert counts[relation] >= 5, f"only {counts[relation]} {relation} cases"


def _format_failures(report) -> str:
    lines = []
    for requirement, evidence, expected, actual, note in report["rows"]:
        if expected != actual:
            lines.append(f"  {requirement!r} vs {evidence!r}\n"
                         f"      expected {expected}, got {actual}"
                         + (f"  ({note})" if note else ""))
    return "\n".join(lines)


# ---- runnable report --------------------------------------------------------

if __name__ == "__main__":
    r = evaluate()
    print(f"cases              {r['total']}")
    print(f"accuracy           {r['accuracy']:.1%}  ({r['correct']}/{r['total']})")
    print(f"false equivalence  {len(r['false_equivalence'])}   <- must stay 0")
    print(f"false positive     {len(r['false_positive'])}")
    print(f"false negative     {len(r['false_negative'])}")
    print(f"misgraded          {len(r['misgraded'])}")
    print()
    print("confusion (expected -> actual):")
    for (expected, actual), n in sorted(r["confusion"].items()):
        flag = "" if expected == actual else "   <-"
        print(f"  {expected:8} -> {actual:8}  {n:3}{flag}")
    if r["accuracy"] < 1.0:
        print("\nmismatches:")
        print(_format_failures(r))

    print(f"\nknown gaps ({len(KNOWN_GAPS)}) — fall through to the LLM, by design:")
    for requirement, evidence, why in KNOWN_GAPS:
        status = _classify(requirement, evidence)
        mark = "  " if status == "none" else "->"
        print(f"  {mark} {requirement:26} {why}")
    print("\n  '->' means the tables now resolve it: promote it into CASES.")
