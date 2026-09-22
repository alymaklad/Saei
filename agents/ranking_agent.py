"""
Ranking Agent — "is this job right for ME?"

This is the second half of the two-stage retrieve-then-rank matching
pipeline. Retrieval (agents/search_agent.py + semantic_retrieval_path below)
is recall-biased: cheap signals, don't drop anything plausible. Ranking is
precision-biased: deeper per-job analysis on the already-narrowed candidate
pool, producing one continuous 0..1 match score so jobs can be ordered rather
than just marked "relevant / not relevant".

Deliberately a DIFFERENT question from agents/ats_agent.py's score. That one
asks "would my CV, as written, survive THIS employer's ATS parser" (keyword
match, formatting, section completeness, experience alignment). This one asks
"is this job worth my time applying to" -- and looks at signals the ATS score
never considers at all (location, salary, education, seniority). A job can
score well on one and badly on the other: a perfectly ATS-parseable CV can
still be aimed at a job in the wrong city at the wrong seniority.

Factor weights: the seven rule-based factors keep their relative proportions
from the original design (skills 40 / experience 20 / title 15 / location 10 /
education 5 / salary 5 / seniority 5, summing to 100), scaled down to fill the
80% left over after semantic similarity takes 20% as its own factor.

Missing data is scored as NEUTRAL (0.5), never as zero. Location and salary
in particular are absent from most sources -- the generic scraper and job
board templates usually yield only a title and raw page text, with no
structured salary field at all -- so scoring "not stated" as 0 would
systematically punish jobs for how their source happens to be structured
rather than for anything about the job. Each factor reports whether its data
was actually found, so the breakdown can say so explicitly.

COST: ranking is deliberately LLM-FREE. It runs over every retrieved
candidate -- hundreds per run -- so an LLM call per job simply isn't
affordable: measured, 250 jobs consumed ~197k of Groq's 200k free-tier daily
token budget, all of it spent ranking jobs that mostly never get applied to.
Embeddings are the only external call here, they're batched, and they're
optional (the semantic factor degrades to neutral without them). Real
LLM-based skill extraction still happens once per job in
agents/ats_agent.py -- for the far smaller set that reaches the orchestrator.
"""
import re

import config
from agents.embeddings import (apply_document_instruction, cosine_similarity,
                              embed_texts)
from agents import cv_profile, skill_matching
from agents.ats_agent import _extract_required_years
from agents.search_agent import SENIORITY_KEYWORDS, _matches_any_position, _matches_seniority

# ---- factor weights ---------------------------------------------------------
#
# Semantic similarity is its own factor at 20%; the other seven are the
# originally-specified 40/20/15/10/5/5/5 rubric scaled by 0.8 so everything
# still sums to 1.0. Their proportions relative to each other are unchanged
# (skills is still 8x education, still 2x experience, and so on).

SEMANTIC_WEIGHT = 0.20

_RULE_WEIGHTS = {
    "skills": 0.40,
    "experience": 0.20,
    "title": 0.15,
    "location": 0.10,
    "education": 0.05,
    "salary": 0.05,
    "seniority": 0.05,
}

WEIGHTS = {"semantic": SEMANTIC_WEIGHT}
WEIGHTS.update({k: v * (1 - SEMANTIC_WEIGHT) for k, v in _RULE_WEIGHTS.items()})
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

FACTOR_LABELS = {
    "semantic": "Semantic CV/JD similarity",
    "skills": "Skills match",
    "experience": "Experience match",
    "title": "Job title match",
    "location": "Location",
    "education": "Education",
    "salary": "Salary",
    "seniority": "Seniority",
}

# Scored when a factor's underlying data simply isn't present in the job
# posting. Neutral rather than 0 -- see the module docstring.
NEUTRAL = 0.5


def job_text(job: dict) -> str:
    """The text used for embedding/keyword analysis. Title is included because
    some sources (notably the generic scraper) return a thin description where
    the title carries most of the signal."""
    return f"{job.get('title') or ''}\n\n{job.get('description') or ''}".strip()


# ---- retrieval: the semantic path -------------------------------------------

def semantic_retrieval_path(
    jobs: list[dict], cv_embedding: list[float], threshold: float | None = None,
) -> list[dict]:
    """Jobs whose DESCRIPTION is semantically close to the CV, regardless of
    title. This is what catches "Backend Developer" for a "Software Engineer"
    search when the JD content genuinely fits -- the title-token path
    (search_agent._matches_any_position) structurally cannot.

    Callers pass only the jobs the token path already REJECTED: re-embedding a
    job the token filter already accepted spends an embedding call to confirm
    a decision that's already made, since the two paths are unioned anyway.

    Each returned job gets a `semantic_score` key so the ranking stage doesn't
    have to recompute the same similarity.
    """
    if not jobs or not cv_embedding:
        return []
    cutoff = threshold if threshold is not None else config.CV_JOB_SIMILARITY_THRESHOLD

    embeddings = embed_texts([apply_document_instruction(job_text(j)) for j in jobs])
    kept = []
    for job, embedding in zip(jobs, embeddings):
        score = cosine_similarity(cv_embedding, embedding)
        if score >= cutoff:
            kept.append({**job, "semantic_score": score})
    return kept


def attach_semantic_scores(jobs: list[dict], cv_embedding: list[float] | None,
                           max_embeddings: int | None = None) -> list[dict]:
    """Fills in `semantic_score` for every job that doesn't already have one,
    using a single batched embedding pass.

    Jobs recovered by semantic_retrieval_path already carry their score and
    are skipped -- only the token-path matches need embedding here. Returns
    the jobs unchanged if there's no CV embedding to compare against, or if
    embedding fails (the semantic factor then scores neutral rather than
    failing the run).

    `max_embeddings` bounds how many jobs get embedded here, so a metered
    provider's per-run budget can't be blown by a large token-matched set.
    Jobs beyond the limit keep `semantic_score = None` and score neutral on
    that one factor -- they're already known relevant by title, so this is
    the cheaper loss than skipping semantic discovery entirely.
    """
    if not jobs or not cv_embedding:
        return jobs

    pending = [j for j in jobs if j.get("semantic_score") is None]
    if max_embeddings is not None:
        pending = pending[: max(0, max_embeddings)]
    if not pending:
        return jobs

    try:
        embeddings = embed_texts([apply_document_instruction(job_text(j)) for j in pending])
    except Exception as exc:  # noqa: BLE001 -- quota exhausted, provider down
        print(f"[matching] could not embed job descriptions for ranking: {exc}")
        return jobs

    scored = {
        id(job): cosine_similarity(cv_embedding, embedding)
        for job, embedding in zip(pending, embeddings)
    }
    return [
        {**job, "semantic_score": scored[id(job)]} if id(job) in scored else job
        for job in jobs
    ]


def split_by_token_match(jobs: list[dict], target_roles: list[str]) -> tuple[list[dict], list[dict]]:
    """Partitions a job list into (title matched, title did not match), so the
    caller can send only the misses through the (paid) semantic path."""
    matched, missed = [], []
    for job in jobs:
        (matched if _matches_any_position(job.get("title"), target_roles) else missed).append(job)
    return matched, missed


# ---- ranking factors --------------------------------------------------------

def score_title(job: dict, target_roles: list[str]) -> tuple[float, str]:
    title = job.get("title") or ""
    if not title:
        return NEUTRAL, "No job title available to compare."
    if not target_roles:
        return NEUTRAL, "No target roles to compare the title against."
    if _matches_any_position(title, target_roles):
        return 1.0, f"Title matches a target role: \"{title}\"."
    return 0.0, f"Title \"{title}\" doesn't match any target role (kept via semantic similarity instead)."


def score_seniority(job: dict, seniority: str) -> tuple[float, str]:
    title = job.get("title") or ""
    if not seniority:
        return NEUTRAL, "No seniority preference set."
    if not title:
        return NEUTRAL, "No job title available to infer seniority from."
    if _matches_seniority(title, seniority):
        return 1.0, f"Title is consistent with the '{seniority}' level you're targeting."
    return 0.0, f"Title suggests a different level than '{seniority}'."


_REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|distributed|anywhere)\b", re.I)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
_ONSITE_RE = re.compile(r"\b(on[- ]?site|in[- ]?office|in[- ]?person)\b", re.I)


def score_location(job: dict, prefer_remote: bool = True) -> tuple[float, str]:
    """Location data is wildly inconsistent across this project's seven
    sources -- RemoteOK/WWR are remote by definition, Greenhouse/Lever
    sometimes expose a structured location, and the generic scraper exposes
    nothing at all. So this reads whatever text IS available and falls back to
    neutral rather than guessing."""
    source = (job.get("source") or "").lower()
    if source in ("remoteok", "weworkremotely"):
        return 1.0, "Source is a remote-only job board."

    location_field = str(job.get("location") or "")
    if isinstance(job.get("location"), dict):  # Greenhouse nests it
        location_field = str(job["location"].get("name") or "")
    haystack = f"{location_field} {job.get('title') or ''}"

    if _REMOTE_RE.search(haystack):
        return (1.0 if prefer_remote else 0.5), f"Listed as remote{f' ({location_field})' if location_field else ''}."
    if _HYBRID_RE.search(haystack):
        return 0.7, f"Listed as hybrid{f' ({location_field})' if location_field else ''}."
    if _ONSITE_RE.search(haystack) or location_field:
        return 0.4, f"Appears to be on-site{f' ({location_field})' if location_field else ''}."
    return NEUTRAL, "No location information available in this posting."


_SALARY_RE = re.compile(
    r"(?:[$€£]\s?\d[\d,.]*\s*(?:k|000)?|\b\d{2,3},\d{3}\b|\bsalary\b|\bcompensation\b)", re.I
)


def score_salary(job: dict) -> tuple[float, str]:
    """Most sources don't publish salary at all. Without a user-configured
    target figure to compare against, the only honest signal available is
    whether the posting DISCLOSES compensation -- transparency, not fit.
    Scored mildly positive when present, neutral when absent."""
    text = job_text(job)
    if _SALARY_RE.search(text):
        return 0.75, "Posting discloses salary/compensation information."
    return NEUTRAL, "No salary information disclosed in this posting."


_DEGREE_LEVELS = [
    (("phd", "ph.d", "doctorate", "doctoral"), 3),
    (("master", "msc", "m.sc", "mba", "ms in", "m.s."), 2),
    (("bachelor", "bsc", "b.sc", "b.s.", "undergraduate", "degree"), 1),
]


def _highest_degree_level(text: str) -> int:
    lower = (text or "").lower()
    for keywords, level in _DEGREE_LEVELS:
        if any(k in lower for k in keywords):
            return level
    return 0


def _stored_profile() -> dict | None:
    """The user's profile, or None if nothing has been extracted yet.

    Imported inside the function: ranking is used by tests and tools that have
    no database, and a module-level import would make it a hard dependency of
    a stage that is otherwise pure.
    """
    try:
        import profile_store
        return profile_store.load_profile_dict()
    except Exception:  # noqa: BLE001 -- ranking must not fail on a DB hiccup
        return None


def _candidate_text(cv_text: str, profile: dict | None) -> str:
    """What to search when asking whether the candidate has something.

    The profile when there is one -- it is the record the user maintains and
    the same document scoring and tailoring read -- and the uploaded file's
    text only as a fallback for a run before any profile was stored.
    """
    return cv_profile.profile_text(profile) if profile else (cv_text or "")


def _profile_degrees(profile: dict | None) -> str:
    """The degree lines the profile holds, as text for level detection.

    Structured fields rather than the whole document: "B.Sc." sits in
    education[i]["degree"], so there is no need to hunt for it in prose that
    also contains a company called "Bachelor's Coffee".
    """
    if not profile:
        return ""
    return " \n".join(
        " ".join(str(entry.get(k) or "") for k in ("degree", "field", "institution"))
        for entry in profile.get("education") or [] if isinstance(entry, dict)
    )


def score_education(job: dict, cv_text: str, profile: dict | None = None) -> tuple[float, str]:
    required = _highest_degree_level(job.get("description") or "")
    held = _highest_degree_level(_profile_degrees(profile) if profile else cv_text)
    if required == 0:
        return NEUTRAL, "No specific education requirement stated in the posting."
    if held == 0:
        return 0.3, ("Posting states an education requirement, but no degree is "
                     "recorded in your profile.")
    if held >= required:
        return 1.0, "Your education meets or exceeds the stated requirement."
    return 0.5, "Your education is below the stated requirement."


def score_experience(job: dict, cv_text: str, profile: dict | None = None) -> tuple[float, str]:
    """Years of professional experience, counted from the profile's dated entries.

    This used to call ats_agent._estimate_cv_experience_years, which computed
    max(year) - min(year) over every number in the CV. That is not a rough
    proxy but a wrong answer -- a graduation year and a copyright notice both
    count, and the error is unbounded and always flattering (see the docstring
    of agents/cv_profile.py, which was written to replace it). The profile
    holds dated entries flagged professional or not, so the honest number is
    available for free: overlapping roles merged, courses excluded.
    """
    required = _extract_required_years(job.get("description") or "")
    if required is None:
        return NEUTRAL, "Posting doesn't state a years-of-experience requirement."
    if not profile:
        return NEUTRAL, (f"Posting asks for {required}+ years. Upload your CV so your "
                         f"profile can be filled in, and this can be counted.")

    held = cv_profile.professional_years(profile)
    if not held:
        return NEUTRAL, (f"Posting asks for {required}+ years, but no dated professional "
                         f"entry in your profile carries a start date.")
    if held >= required:
        return 1.0, f"Posting asks for {required}+ years; your profile shows roughly {held}."
    ratio = max(0.0, min(1.0, held / required))
    return ratio, f"Posting asks for {required}+ years; your profile shows roughly {held}."


# Ranking is deliberately LLM-FREE. It runs over every retrieved candidate --
# hundreds per run -- and an LLM call per job costs roughly a whole day of
# Groq's free-tier token budget (measured: 250 jobs ≈ 197k of 200k tokens),
# spent ranking jobs that mostly never get applied to. The real LLM skill
# extraction still happens, once, in agents/ats_agent.py, for the far smaller
# set of jobs that actually reach the orchestrator.
#
# The deterministic stand-in below inverts the question: instead of "what does
# this JD require, and do I have it" (needs an LLM to read the JD), it asks
# "which of MY skills does this JD mention" -- computable by intersecting the
# CV's own vocabulary with the JD text, no model required. For "is this job
# right for me", that framing is arguably the more direct one anyway.

_CV_SKILLS_HEADING_RE = re.compile(
    r"^\s*(?:technical\s+|core\s+|key\s+)?skills?\s*:?\s*$", re.IGNORECASE | re.MULTILINE
)
_TERM_SPLIT_RE = re.compile(r"[,;|/•\n\t]+")
# Words too generic to signal anything about fit if they appear in a JD.
_SKILL_STOPWORDS = {
    "and", "or", "the", "with", "for", "of", "in", "to", "a", "an", "experience",
    "years", "strong", "good", "excellent", "knowledge", "ability", "skills",
    "skill", "work", "working", "team", "teams", "using", "use", "including",
    "etc", "plus", "familiarity", "understanding", "proficiency", "proficient",
}


def extract_cv_skill_terms(cv_text: str, limit: int = 60) -> list[str]:
    """The candidate's own skill vocabulary, pulled from the CV deterministically.

    Prefers an explicit "Skills:" section when the CV has one (that's where a
    CV states its own vocabulary most cleanly); otherwise falls back to
    distinctive multi-character tokens from the whole document. Never calls a
    model.
    """
    text = cv_text or ""
    section = ""
    match = _CV_SKILLS_HEADING_RE.search(text)
    if match:
        # Take the lines after the heading up to the next blank-line break --
        # enough to capture a listed skills block without swallowing the rest.
        after = text[match.end():]
        section = after.split("\n\n")[0]

    source = section or text
    terms = []
    for raw in _TERM_SPLIT_RE.split(source):
        term = raw.strip().strip(".:()[]").lower()
        if not term or len(term) < 2 or term in _SKILL_STOPWORDS:
            continue
        # Multi-word phrases longer than 4 words are prose, not a skill name.
        if len(term.split()) > 4:
            continue
        terms.append(term)

    seen, unique = set(), []
    for term in terms:
        if term not in seen:
            seen.add(term)
            unique.append(term)
    return unique[:limit]


def score_skills(job: dict, cv_text: str, required_skills: list[str] | None = None,
                 profile: dict | None = None) -> tuple[float, str, list[str]]:
    """How much of the CV's own skill vocabulary this posting actually mentions.

    Deterministic by default -- see the note above. `required_skills` may still
    be passed in by a caller that already has an LLM-extracted list (the
    orchestrator does, via ats_agent), in which case the original
    "how many of the JD's required skills does the CV cover" measure is used
    instead, since that's strictly better information when it's already paid for.
    """
    description = job.get("description") or ""

    if required_skills is not None:
        if not required_skills:
            return NEUTRAL, "No specific required skills were extracted from this posting.", []
        # Word-boundary matching through skill_matching, not substring
        # containment: `"r" in cv_lower` is true of every CV ever written, and
        # "Git" matches "legitimate". That false-positive class is exactly what
        # that module was written to kill, and ranking decides which jobs are
        # scored at all, so an inflated number here is expensive.
        haystack = _candidate_text(cv_text, profile)
        missing = [s for s in required_skills if not skill_matching.find_term(s, haystack)]
        matched = len(required_skills) - len(missing)
        score = matched / len(required_skills)
        return score, f"You cover {matched} of {len(required_skills)} skills named in the posting.", missing

    cv_terms = extract_cv_skill_terms(_candidate_text(cv_text, profile))
    if not cv_terms:
        return NEUTRAL, "No skill terms could be read from your CV.", []

    jd_lower = description.lower()
    if not jd_lower.strip():
        return NEUTRAL, "This posting has no description text to compare against.", []

    matched_terms = [t for t in cv_terms if skill_matching.find_term(t, description)]
    score = len(matched_terms) / len(cv_terms)
    # A JD naturally mentions only a slice of any CV, so raw coverage runs low.
    # Rescale so "a quarter of my skills appear here" reads as a strong match
    # rather than a 25% one -- clamped, so it stays a 0..1 factor.
    scaled = min(1.0, score / 0.25)
    preview = ", ".join(matched_terms[:6]) or "none"
    return (
        scaled,
        f"This posting mentions {len(matched_terms)} of {len(cv_terms)} of your skill terms ({preview}).",
        [t for t in cv_terms if t not in matched_terms][:20],
    )


# ---- overall match score ----------------------------------------------------

def score_job(
    job: dict,
    cv_text: str,
    cv_embedding: list[float] | None = None,
    target_roles: list[str] | None = None,
    seniority: str = "",
    required_skills: list[str] | None = None,
    profile: dict | None = None,
) -> dict:
    """One job's full match score. Returns
    {score, breakdown{factor: {score, weight, evidence}}, missing_skills}.

    `job["semantic_score"]` is reused when present (set by
    semantic_retrieval_path), so a job that came in through the semantic path
    isn't re-embedded here. Jobs that came in through the token path have no
    such key -- they're embedded on demand if a cv_embedding was supplied, and
    scored neutral on that factor if it wasn't (e.g. embeddings unavailable).
    """
    target_roles = target_roles or []

    semantic = job.get("semantic_score")
    if semantic is None and cv_embedding:
        # Normally already filled in by attach_semantic_scores' batched pass.
        # This per-job fallback only fires when score_job is called directly
        # (tests, future callers) -- and must swallow provider failures, since
        # by the time ranking reaches here the batch may have already failed
        # on an exhausted quota and retrying one job at a time would just
        # raise again, per job.
        try:
            embedded = embed_texts([apply_document_instruction(job_text(job))])
            semantic = cosine_similarity(cv_embedding, embedded[0]) if embedded else None
        except Exception:  # noqa: BLE001 -- degrade to a neutral factor
            semantic = None
    if semantic is None:
        semantic_score, semantic_note = NEUTRAL, "Semantic similarity unavailable (embeddings not computed)."
    else:
        semantic_score = semantic
        semantic_note = f"CV/description cosine similarity {semantic:.2f}."

    skills_score, skills_note, missing_skills = score_skills(job, cv_text, required_skills,
                                                             profile=profile)
    experience_score, experience_note = score_experience(job, cv_text, profile=profile)
    title_score, title_note = score_title(job, target_roles)
    location_score, location_note = score_location(job)
    education_score, education_note = score_education(job, cv_text, profile=profile)
    salary_score, salary_note = score_salary(job)
    seniority_score, seniority_note = score_seniority(job, seniority)

    factors = {
        "semantic": (semantic_score, semantic_note),
        "skills": (skills_score, skills_note),
        "experience": (experience_score, experience_note),
        "title": (title_score, title_note),
        "location": (location_score, location_note),
        "education": (education_score, education_note),
        "salary": (salary_score, salary_note),
        "seniority": (seniority_score, seniority_note),
    }

    breakdown = {
        name: {
            "label": FACTOR_LABELS[name],
            "score": round(value, 4),
            "weight": WEIGHTS[name],
            "evidence": note,
        }
        for name, (value, note) in factors.items()
    }
    total = sum(value * WEIGHTS[name] for name, (value, _) in factors.items())

    return {
        "score": round(max(0.0, min(1.0, total)), 4),
        "breakdown": breakdown,
        "missing_skills": missing_skills,
    }


def rank_jobs(
    jobs: list[dict],
    cv_text: str,
    cv_embedding: list[float] | None = None,
    target_roles: list[str] | None = None,
    seniority: str = "",
    trace=None,
    max_embeddings: int | None = None,
    profile: dict | None = None,
) -> list[dict]:
    """Scores every job and returns them sorted best-first, each with
    `match_score` and `match_breakdown` attached. Never drops anything --
    gating on config.MATCH_SCORE_THRESHOLD is the caller's decision (see
    jobs/daily_run.py), kept separate so ranking stays a pure transformation.

    `trace` is an optional agents.search_trace.SearchTrace; each job's full
    factor breakdown is recorded to it for the Excel debug report. The hook
    lives here rather than in the caller so there's exactly ONE scoring loop
    -- a second, traced copy in daily_run.py would be one more thing to keep
    in sync every time this changes.
    """
    # Pre-compute every missing job embedding in ONE batched pass. score_job
    # would otherwise embed each job individually -- a separate API request
    # per job, which is what exhausted Gemini's free-tier per-minute quota
    # (100/min, counted per content) on a normal-sized run.
    jobs = attach_semantic_scores(jobs, cv_embedding, max_embeddings=max_embeddings)

    # Loaded once for the whole run, not per job: ranking sees hundreds of
    # jobs and the profile is the same document for all of them.
    if profile is None:
        profile = _stored_profile()

    ranked = []
    for job in jobs:
        result = score_job(
            job, cv_text, profile=profile,
            # Deliberately NOT passing cv_embedding: attach_semantic_scores
            # above has already done every embedding this run is allowed to
            # do, within max_embeddings. Passing it would re-enable
            # score_job's per-job fallback, which embeds one job at a time
            # and would sail straight past the budget -- measured, that
            # turned a 95-embedding budget into 250 actual embeddings.
            # Jobs the budget skipped keep semantic_score=None and score
            # neutral on that factor, which is the intended degradation.
            cv_embedding=None,
            target_roles=target_roles,
            seniority=seniority,
        )
        if trace is not None:
            trace.record_ranking(job, result)
        ranked.append({
            **job,
            "match_score": result["score"],
            "match_breakdown": result["breakdown"],
            "match_missing_skills": result["missing_skills"],
        })
    ranked.sort(key=lambda j: j.get("match_score") or 0, reverse=True)
    return ranked


def build_match_explanation(score: float, breakdown: dict) -> str:
    """Plain-English rendering of a match score -- same deterministic,
    no-extra-LLM-call approach as ats_agent.build_score_explanation."""
    lines = [f"Overall match score: {score * 100:.0f}%.", ""]
    for name, detail in sorted(breakdown.items(), key=lambda kv: kv[1]["weight"], reverse=True):
        lines.append(
            f"- {detail['label']} ({detail['weight'] * 100:.0f}% of the score): "
            f"{detail['score'] * 100:.0f}% — {detail['evidence']}"
        )
    return "\n".join(lines)
