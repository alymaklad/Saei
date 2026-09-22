"""
CV Rewriter Agent.

Integrity rule: this must reframe real experience for a job description, never
invent skills or history the candidate doesn't have.

How that rule is enforced changed shape here. It used to be a paragraph in a
system prompt -- "do NOT fabricate skills or experience the candidate doesn't
have" -- checked by nobody. The model returned a whole CV as plain text, which
meant it was also retyping the name, the employers, the dates and the degrees
on every run, and any of those could come back subtly wrong with nothing
downstream in a position to notice.

Now the model returns only the parts that are genuinely its job:

    which entries to include, in what order, reworded how, grouped into
    which skill categories, under what summary.

Everything factual -- name, contact details, job titles, employers, locations,
dates, project URLs, degrees, institutions -- is taken from the user's stored
profile after the call, by index. The model refers to an entry as {"ref": 1};
it cannot rename an employer, move a date, or add a job, because those fields
never travel through it. `_reconcile` does that merge, and also:

  * drops any skill that does not appear in the profile or the CV text, and
    any skill on the missing-skills list (the list the ATS pass found the
    candidate does NOT have -- the one thing a tailored CV must never claim),
  * flags any bullet whose numbers do not appear in that entry's source text,
    which is the fabrication that a template with "[Metric + value]" slots
    invites and the one a reader is least able to catch.

The flags are reported, not silently repaired: `document["integrity_warnings"]`
reaches the debug bench so a bad run is visible rather than quietly shipped.

The layout lives in agents/cv_render.py; see that module for why.
"""
import json
import os
import re

from langchain_core.messages import SystemMessage, HumanMessage

import config
from agents import cv_targeting, skill_matching
from agents.llm import (context_for, estimate_tokens, get_llm, no_think_prefix,
                        prepare_system, visible_content, _is_thinking_model)
from agents.cv_render import (  # re-exported: existing callers import these from here
    CV_ACCENT_COLOR, render_cv_text, save_cv_as_pdf, strip_markup,
)

__all__ = ["rewrite_cv", "render_cv_text", "save_cv_as_pdf", "save_cv_as_docx",
           "build_document", "CV_ACCENT_COLOR"]


# Bullet budgets, straight from the blueprint. Enforced here as well as asked
# for in the prompt, because a model that ignores the budget produces a CV
# that silently spills onto a second page.
MAX_EXPERIENCE_BULLETS = 4
MAX_PROJECT_BULLETS = 4
MAX_SKILL_GROUPS = 5

_SYSTEM_PROMPT = """You tailor a candidate's CV to one job description. Respond with ONLY a JSON object, no prose, no code fences.

You are given the candidate's profile as structured JSON, with an index on every
experience entry and every project. You do NOT write the CV; you choose and reword
its content. Names, employers, dates, locations, links and degrees are filled in
from the profile afterwards and must not appear in your response.

Schema:
{
  "summary": "2-4 sentences of prose: the candidate's specialty, the technologies or methods central to their work, and the focus they bring to THIS role. Not a keyword list. Every term used here must also be backed by a bullet below.",
  "experience": [{"ref": 0, "bullets": ["rewritten bullet", "..."]}],
  "projects": [{"ref": 2, "bullets": ["...", "..."]}],
  "skills": [{"category": "e.g. AI/LLM", "items": ["skill", "skill"]}],
  "note": "one optional line pointing at further work, or null"
}

Rules:
- NEVER invent. Every bullet must be a rewording of that same entry's source
  bullets. You may merge two source bullets, drop one, or re-emphasize what a
  bullet already says. You may not add a claim the source does not make.
- Numbers, metrics and benchmarks: use only ones that appear in that entry's
  source bullets, copied exactly. If a bullet has no number, write it without
  one. Never estimate, round, or supply a plausible-looking figure.
- Never mention a technology the source entry does not mention, and never
  mention anything on the missing-skills list -- those are the requirements
  this candidate does NOT meet.
- Mark 1-3 terms per bullet with **double asterisks** for emphasis: the terms
  this job description actually asks for AND the source bullet actually
  contains. Do not bold whole sentences.
- experience: return one object for EVERY entry in the profile, in the order
  given, each with 2-4 bullets. Leaving a job out creates an unexplained gap.
- projects: include only the ones relevant to this job, ordered by relevance
  to the job, NOT by date. Give the most relevant 3-4 bullets, a strong match
  2-3, a minor one 1-2, and omit tutorial or toy projects entirely.
- skills: 3-5 categories, using ONLY skills already listed in the profile.
  Put the categories and the skills this job asks for first.

Target terms:
- The "Target terms" list is the job's own vocabulary for work these entries
  ALREADY describe. It was computed from the candidate's history, not from the
  job description alone: each line names one entry and the words already in it.
- Work each target's wording into a bullet of the entry it names, in the same
  sentence as the evidence already there, and keep that evidence. "Trained a
  ResNet-50" becomes "Trained a ResNet-50 deep-learning encoder", not "Worked
  on deep learning".
- Use a target's wording ONLY in the entry it is listed against. The same term
  in another entry is a claim about work that entry did not do.
- Where a target gives a full form and an acronym, write the full form once and
  the short form everywhere else.
- Do NOT pull any other term out of the job description. A term that is not on
  the target list has not been checked against this candidate's history, and
  writing it is fabrication however well it fits the sentence."""


def _safe_json_object(raw: str) -> dict:
    """LLMs wrap JSON in prose or code fences; pull the outermost object out."""
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    candidate = match.group(0) if match else (raw or "")
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _profile_for_prompt(profile: dict) -> dict:
    """The subset of the profile the model needs, with the indices it answers by.

    Deliberately narrow: contact details, dates and locations are not sent at
    all. They cannot come back wrong if they were never in the conversation,
    and leaving them out is also most of the prompt-size saving that keeps this
    call inside a metered provider's per-minute window.
    """
    return {
        "experience": [
            {"ref": i, "title": e.get("title", ""),
             "organization": e.get("organization", ""),
             "bullets": e.get("bullets") or []}
            for i, e in enumerate(profile.get("experience") or [])
        ],
        "projects": [
            {"ref": i, "name": p.get("name", ""), "bullets": p.get("bullets") or []}
            for i, p in enumerate(profile.get("projects") or [])
        ],
        "skills": profile.get("skills_claimed") or [],
    }


def rewrite_cv(cv_text: str, job_description: str, missing_skills: list[str],
               profile: dict | None = None, requirement_results=None,
               required_skills=None):
    """Tailor the CV to one job.

    `requirement_results` is the scoring pass's own per-requirement verdict.
    Given it, cv_targeting works out which of the job's words this CV has
    already earned but not yet used -- "Deep Learning" for a bullet that says
    "CNN", "SQL" for one that says "PostgreSQL" -- and the model is told to
    write them into the entries that earned them. That is worth the difference
    between subset credit and exact credit on those requirements, and claims
    nothing the CV could not already defend.

    Returns the document dict that cv_render lays out. Falls back to returning
    the model's raw text when the response can't be read as the expected JSON
    -- cv_render accepts a plain string and renders it with the old line-based
    layout, which is a worse CV but still a CV. Throwing away a rewrite that
    has already been paid for is the one outcome worth avoiding here.
    """
    if not isinstance(profile, dict) or not (profile.get("experience")
                                             or profile.get("projects")):
        # No structured profile to reconcile against -- the model would have to
        # supply the facts itself, which is exactly what this design removes.
        # Rather than quietly re-enabling that, fall back to the plain-text
        # path so the caller still gets a tailored CV.
        return _rewrite_cv_as_text(cv_text, job_description, missing_skills)

    # JSON of refs and bullets, not a whole CV: a few hundred tokens of output
    # rather than a few thousand. Groq's free tier bills the RESERVATION
    # (prompt + max_tokens) against 8,000 tokens/minute and rejects the request
    # with a 413 before the model runs, so the smaller ceiling is what keeps
    # this call inside the window on the provider this project defaults to.
    targets = cv_targeting.build_targets(
        profile, requirement_results=requirement_results,
        required_skills=required_skills, missing_skills=missing_skills)

    human = [
        f"Candidate profile:\n{json.dumps(_profile_for_prompt(profile), ensure_ascii=False)}",
        f"Job description:\n{_trim_job_description(job_description)}",
        f"Missing skills -- requirements this candidate does NOT meet, "
        f"never write these into the CV:\n{list(missing_skills or [])}",
    ]
    if targets:
        human.append("Target terms -- the job's wording for what these entries "
                     "already describe:\n" + cv_targeting.format_targets(targets))

    parsed = _ask_for_document("\n\n".join(human))
    return build_document(
        profile, parsed, cv_text=cv_text, missing_skills=missing_skills,
        targets=targets,
        requirement_names=[str(r.get("name") or "") for r in (requirement_results or [])
                           if isinstance(r, dict)] or list(required_skills or []),
        listed_only=cv_targeting.listed_only(profile, requirement_results),
    )


def _trim_job_description(job_description: str) -> str:
    """The one unbounded input in this prompt, cut to a budget.

    A scraped posting can run to tens of thousands of characters, most of it
    boilerplate after the requirements -- benefits, EEO statements, how to
    apply. Trimmed from the END for that reason, and only when it is actually
    long: everything the rewrite needs is in the first few thousand
    characters, and the requirements were extracted from the full text
    already, upstream of this call.
    """
    text = str(job_description or "")
    limit = config.REWRITE_JD_MAX_CHARS
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[... posting truncated for length ...]"


def _ask_for_document(human_content: str) -> dict:
    """The rewrite call, sized to its own prompt, retried once, and REQUIRED to
    return a usable document.

    Ollama counts prompt + completion against num_ctx and truncates silently,
    so this call passes its prompt to get_llm and gets a context sized for it
    rather than a fixed 8,192 that the longest prompt in the app overflows.
    That overflow is what produced "CV rewrite came back empty
    (finish_reason=unknown)" on a local qwen3 -- an empty string, HTTP 200, no
    exception, one wasted run.

    **A non-empty answer is not a usable one.** qwen3:4b returned 13,000
    characters of reasoning -- "We are given the candidate profile... but wait,
    the problem says..." -- with no CV anywhere in it, and the previous version
    of this function handed that straight back. It parsed as no JSON, the
    caller fell through to `return content`, and the reasoning monologue was
    rendered into a tailored-CV PDF. So the contract here is the DOCUMENT, not
    the string: anything that does not parse into entries is a failure, and
    failures are retried and then raised.

    The retry keeps the same budget and adds a blunter instruction. The first
    attempt's failure mode decides what would help, and there is no way to
    know it from here -- but a model that spent its budget thinking needs to be
    told not to, and one that ran out of room needs a shorter answer, and
    "reply with JSON only, no explanation" serves both.
    """
    system = prepare_system(_SYSTEM_PROMPT)

    attempts = (
        (3000, human_content),
        (3000, no_think_prefix() + "Reply with the JSON object only. No "
                                   "explanation, no reasoning, no preamble.\n\n"
         + human_content),
    )
    for attempt, (budget, content_for_call) in enumerate(attempts):
        prompt = system + content_for_call
        llm = get_llm(temperature=0.3, max_tokens=budget, prompt_text=prompt)
        resp = llm.invoke([SystemMessage(content=system),
                           HumanMessage(content=content_for_call)])
        content = visible_content(resp)

        if content:
            parsed = _safe_json_object(content)
            if parsed.get("experience") or parsed.get("projects"):
                return parsed

        if attempt == 0:
            continue
        raise RuntimeError(_unusable_response_message(resp, content, prompt, budget))
    raise RuntimeError("unreachable")  # pragma: no cover


def _unusable_response_message(resp, content: str, prompt: str, budget: int) -> str:
    """Name what came back instead of a CV.

    Two different failures wear the same error otherwise: nothing at all, and
    a wall of text that is not a CV. The second is the one a small thinking
    model produces, and saying "empty" about 13,000 characters of reasoning
    sends the reader looking in the wrong place.
    """
    if content:
        preview = " ".join(content.split())[:160]
        base = (f"The model answered with {len(content)} characters that are "
                f"not a CV document — no JSON object with experience or "
                f"projects in it. It starts: \"{preview}…\"")
        if config.LLM_PROVIDER == "ollama":
            return (base + f" This is what {config.OLLAMA_MODEL} does when it "
                    "reasons instead of answering: a small thinking model can "
                    "spend its whole output budget deliberating. Try a larger "
                    "or non-thinking model (llama3.1:8b, qwen2.5:7b-instruct), "
                    "or raise OLLAMA_NUM_CTX_MAX so there is room for both.")
        return base + " Try again, or switch model in Settings."
    return _empty_response_message(resp, prompt, budget)


def _empty_response_message(resp, prompt: str, budget: int) -> str:
    """Say which of the causes it was, or at least give the numbers.

    The previous message listed possibilities and left the reader to guess.
    These are the values that distinguish them: how big the prompt was against
    the context it was given, and what the provider said about why it stopped.
    """
    metadata = getattr(resp, "response_metadata", {}) or {}
    reason = (metadata.get("finish_reason") or metadata.get("done_reason")
              or "not reported")
    estimated = estimate_tokens(prompt)
    context = context_for(prompt, budget)
    detail = (f"prompt about {estimated} tokens, output budget {budget}, "
              f"context {context}, stop reason {reason}")

    if config.LLM_PROVIDER == "ollama":
        thinking = (_is_thinking_model(config.OLLAMA_MODEL)
                    and config.OLLAMA_THINKING)
        advice = (
            f"The model returned nothing twice ({detail}). "
            f"On a local model this is nearly always room: {config.OLLAMA_MODEL} "
            f"has to fit the prompt AND its answer inside num_ctx. "
            f"Raise OLLAMA_NUM_CTX_MAX (currently {config.OLLAMA_NUM_CTX_MAX}), "
            f"or lower REWRITE_JD_MAX_CHARS (currently "
            f"{config.REWRITE_JD_MAX_CHARS}) to send less of the posting.")
        if thinking:
            advice += (" OLLAMA_THINKING is on: a reasoning model can spend "
                       "its whole output budget thinking and return nothing. "
                       "Turn it off.")
        return advice

    return (f"CV rewrite came back empty ({detail}). The model spent its "
            "output budget before writing anything, or the provider's quota "
            "is exhausted. Try again, or switch provider/model in Settings.")


def _rewrite_cv_as_text(cv_text: str, job_description: str,
                        missing_skills: list[str]) -> str:
    """The pre-blueprint path: the model emits the whole CV as plain text.

    Only reached when there is no stored profile to reconcile against -- i.e.
    the user has never run an extraction on the Profile page.
    """
    system = prepare_system(
            "Rewrite this CV to be ATS-optimized for the given job description. "
            "Naturally incorporate relevant keywords the candidate genuinely has experience "
            "with, restructure bullet points around measurable impact, and do NOT fabricate "
            "skills or experience the candidate doesn't have. If a required skill is missing, "
            "leave it out of the CV rather than inventing it — missing skills are reported "
            "separately.\n\n"
            "Format the output as plain text laid out like a real CV, so it can be rendered "
            "into a PDF directly, matching the structure of the original CV:\n"
            "- First line: the candidate's name, exactly as it appears in the original CV.\n"
            "- Second line: contact info (email, phone, location, LinkedIn -- whatever the "
            "original CV includes), on a single line, exactly as it appears in the original. "
            "Do not skip this line even if it's long.\n"
            "- A blank line, then section headers in ALL CAPS on their own line (e.g. SUMMARY, "
            "EXPERIENCE, EDUCATION, SKILLS) -- use whichever sections the original CV actually "
            "has, in the same order.\n"
            "- Bullet points for individual achievements/responsibilities, each starting "
            "with '- '.\n"
            "- No markdown symbols (no #, no **, no backticks) -- headers and bullets should "
            "read correctly as plain text on their own."
    )
    human = (f"Current CV:\n{cv_text}\n\n"
             f"Job Description:\n{_trim_job_description(job_description)}\n\n"
             f"Known missing skills (do NOT fabricate these into the CV):\n{missing_skills}")
    llm = get_llm(temperature=0.3, max_tokens=3000, prompt_text=system + human)
    resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=human)])
    content = visible_content(resp)
    if not content:
        raise RuntimeError(_empty_response_message(resp, system + human, 3000))
    return content


def _require_content(resp) -> str:
    content = (getattr(resp, "content", "") or "").strip()
    if content:
        return content
    # Seen in practice with Groq's gpt-oss models: on a long prompt they can
    # spend their whole completion-token budget on internal reasoning and
    # return content="" with finish_reason="length" -- HTTP 200, no exception,
    # which silently produced a blank tailored-CV PDF before this check
    # existed. Raising here routes it through the caller's per-job error
    # handling instead of writing an empty file.
    reason = getattr(resp, "response_metadata", {}).get("finish_reason", "unknown")
    raise RuntimeError(
        f"CV rewrite came back empty (finish_reason={reason}). This usually "
        "means the model ran out of its token budget on internal reasoning "
        "before writing the CV, or the provider's daily quota is exhausted. "
        "Try again, lower 'Max results per site' to send fewer requests, or "
        "switch provider/model in Settings."
    )


# ---- reconciliation ----------------------------------------------------------

_BULLET_PREFIX_RE = re.compile(r"^[-*•–—]\s*")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*%?")


def _clean_bullet(value) -> str:
    text = _BULLET_PREFIX_RE.sub("", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    # An odd number of markers means an unclosed **bold** span; the renderer
    # would drop the stray pair and leave the emphasis half-applied.
    if text.count("**") % 2:
        text = text.replace("**", "")
    return text


def _numbers(text: str) -> set[str]:
    return {n.rstrip("%").replace(",", "") for n in _NUMBER_RE.findall(text or "")}


def _unverified_numbers(bullet: str, source: str) -> list[str]:
    """Numbers in a rewritten bullet that appear nowhere in its source.

    A blunt check on purpose. "8 GB" split out of "RTX 4070 8GB" still
    verifies, and a re-worded percentage still verifies, because only the digits
    are compared -- but a metric the model supplied from nothing has nothing to
    match against, and that is the case worth catching.
    """
    return sorted(_numbers(bullet) - _numbers(source))


def _entry_bullets(entry: dict, fallback: list, source: str, limit: int,
                   label: str, warnings: list) -> list[str]:
    bullets = [b for b in (_clean_bullet(b) for b in entry.get("bullets") or []) if b]
    if not bullets:
        # The model returned an entry with nothing in it. Its own source
        # bullets are still true, so use those rather than printing a bare
        # heading with no content under it.
        bullets = [_clean_bullet(b) for b in (fallback or [])][:2]
    bullets = bullets[:limit]

    for bullet in bullets:
        invented = _unverified_numbers(bullet, source)
        if invented:
            warnings.append(
                f"{label}: bullet cites {', '.join(invented)}, which does not "
                f"appear in the source entry — verify before sending."
            )
    return bullets


def _ref(entry, size) -> int | None:
    try:
        index = int(entry.get("ref"))
    except (TypeError, ValueError):
        return None
    return index if 0 <= index < size else None


def _entry_source_text(entry: dict, *fields: str) -> str:
    parts = [str(entry.get(f) or "") for f in fields]
    parts.extend(str(b or "") for b in entry.get("bullets") or [])
    return " \n".join(p for p in parts if p)


def _verified_bullets(bullets: list[str], source: dict, source_text: str, label: str,
                      blocked: set, checked: list, warnings: list) -> list[str]:
    """Bullets with anything the entry cannot support taken back out.

    The targeting pass invites the model to write the job's vocabulary into
    specific entries, which makes this check the other half of that bargain.
    Every job term that appears in a rewritten bullet but not in that entry's
    own source is examined:

      * a term on the missing-skills list is REMOVED -- with the bullet that
        carries it, falling back to the entry's own source bullets if that
        empties it. The scoring pass has already established the candidate
        does not have it, so no wording of that sentence is honest.
      * a term the entailment tables cannot connect to this entry's evidence
        is reported. It may be a fair inference the tables don't know about,
        and deleting a whole bullet over it would lose real content -- but it
        is the reader's call, not this function's.
    """
    introduced = cv_targeting.introduced_terms(bullets, source_text, checked)
    if not introduced:
        return bullets

    kept = list(bullets)
    for term in introduced:
        if skill_matching.canonical(term) in blocked:
            kept = [b for b in kept if not skill_matching.find_term(term, b)]
            warnings.append(
                f"{label}: dropped a bullet claiming “{term}”, which the job "
                f"asks for and this entry has no evidence of."
            )
        elif not cv_targeting.is_supported(term, source_text):
            warnings.append(
                f"{label}: mentions “{term}”, which this entry's own bullets "
                f"don't support — check it before sending."
            )

    if not kept:
        kept = [_clean_bullet(b) for b in (source.get("bullets") or [])][:2]
    return kept


def _verified_summary(summary: str, profile: dict, cv_text: str,
                      blocked: set, checked: list, warnings: list) -> str:
    """The summary, unless it claims something the whole CV cannot support.

    Checked against the profile as a whole rather than one entry, because a
    summary is a claim about the candidate, not about a role. A blocked term
    here is not repairable by editing a clause -- the sentence was written
    around it -- so the profile's own summary is used instead.
    """
    if not summary:
        return profile.get("summary") or ""

    everything = " \n".join(
        [str(cv_text or ""), str(profile.get("summary") or "")]
        + [_entry_source_text(e, "title", "organization")
           for e in profile.get("experience") or [] if isinstance(e, dict)]
        + [_entry_source_text(p, "name")
           for p in profile.get("projects") or [] if isinstance(p, dict)]
        + [str(s) for s in profile.get("skills_claimed") or []]
    )

    for term in cv_targeting.introduced_terms([summary], everything, checked):
        if skill_matching.canonical(term) in blocked:
            warnings.append(
                f"Summary claimed “{term}”, which the job asks for and your CV "
                f"has no evidence of — your own summary was used instead."
            )
            return profile.get("summary") or ""
        warnings.append(f"Summary mentions “{term}”, which appears nowhere else "
                        f"in your CV — check it before sending.")
    return summary


def build_document(profile: dict, response: dict, *, cv_text: str = "",
                   missing_skills: list[str] | None = None,
                   targets: list[dict] | None = None,
                   requirement_names: list[str] | None = None,
                   listed_only: list[str] | None = None) -> dict:
    """Merge the model's choices onto the profile's facts.

    The profile always wins on anything factual. The model's contribution is
    the summary, the bullets, the project selection and order, and the skill
    grouping -- and each of those is filtered before it lands.
    """
    profile = profile if isinstance(profile, dict) else {}
    response = response if isinstance(response, dict) else {}
    warnings: list[str] = []

    contact = profile.get("contact") if isinstance(profile.get("contact"), dict) else {}
    source_experience = profile.get("experience") or []
    source_projects = profile.get("projects") or []

    # Every term the job asked for is worth re-checking against the entry it
    # landed in, not just the ones the targeting pass proposed: the model sees
    # the whole job description and can lift a word from it unprompted.
    # `missing_skills` was computed from the uploaded CV file. The profile is
    # what this document is built from and what the user can correct, so a
    # term the profile itself demonstrates is not blocked -- see
    # cv_targeting.unsupported_by_profile.
    blocked = cv_targeting.unsupported_by_profile(missing_skills, profile)
    overruled = [s for s in (missing_skills or [])
                 if str(s).strip() and skill_matching.canonical(s) not in blocked]
    if overruled:
        warnings.append(
            "Your profile shows " + ", ".join(overruled) + ", which your uploaded "
            "CV doesn't mention. The tailored CV keeps it, but the score above was "
            "computed without it — re-upload your CV to make the two agree."
        )
    checked = list(dict.fromkeys(
        [str(t.get("term")) for t in (targets or []) if t.get("term")]
        + [str(n) for n in (requirement_names or []) if str(n).strip()]
        + [str(s) for s in (missing_skills or []) if str(s).strip()]
    ))

    # ---- experience: every entry, in profile order, bullets from the model ----
    by_ref = {}
    for entry in response.get("experience") or []:
        if isinstance(entry, dict):
            index = _ref(entry, len(source_experience))
            if index is None:
                warnings.append("An experience entry referred to a role that isn't "
                                "in your profile and was dropped.")
                continue
            by_ref.setdefault(index, entry)

    experience = []
    for index, source in enumerate(source_experience):
        entry = by_ref.get(index, {})
        label = source.get("title") or source.get("organization") or f"experience[{index}]"
        source_text = _entry_source_text(source, "title", "organization")
        bullets = _entry_bullets(entry, source.get("bullets"),
                                 " ".join(source.get("bullets") or []),
                                 MAX_EXPERIENCE_BULLETS, label, warnings)
        experience.append({
            "title": source.get("title", ""),
            "organization": source.get("organization", ""),
            "location": source.get("location", ""),
            "start": source.get("start"),
            "end": source.get("end"),
            "bullets": _verified_bullets(bullets, source, source_text, label,
                                         blocked, checked, warnings),
        })

    # ---- projects: the model's selection AND its order ----
    projects = []
    seen = set()
    for entry in response.get("projects") or []:
        if not isinstance(entry, dict):
            continue
        index = _ref(entry, len(source_projects))
        if index is None:
            warnings.append("A project entry referred to a project that isn't in "
                            "your profile and was dropped.")
            continue
        if index in seen:
            continue
        seen.add(index)
        source = source_projects[index]
        label = source.get("name") or f"projects[{index}]"
        source_text = _entry_source_text(source, "name")
        bullets = _entry_bullets(entry, source.get("bullets"),
                                 " ".join(source.get("bullets") or []),
                                 MAX_PROJECT_BULLETS, label, warnings)
        projects.append({
            "name": source.get("name", ""),
            "url": source.get("url", ""),
            "start": source.get("start"),
            "end": source.get("end"),
            "bullets": _verified_bullets(bullets, source, source_text, label,
                                         blocked, checked, warnings),
        })

    # ---- education: no model involvement at all ----
    education = [{
        "degree": e.get("degree", ""), "field": e.get("field", ""),
        "institution": e.get("institution", ""), "location": e.get("location", ""),
        "start": e.get("start"), "end": e.get("end"),
    } for e in profile.get("education") or [] if isinstance(e, dict)]

    skills = _reconcile_skills(response.get("skills"), profile, cv_text,
                               blocked, warnings, required=requirement_names)

    summary = _verified_summary(_clean_bullet(response.get("summary")), profile,
                                cv_text, blocked, checked, warnings)

    document = {
        # The candidate's own headline, never the job's title: echoing the
        # posting's title back is a real ATS trick, but it reads as a claim
        # about seniority the profile may not support.
        "headline": contact.get("title", ""),
        "name": contact.get("full_name", ""),
        "contact": {k: contact.get(k, "") for k in
                    ("email", "phone", "location", "linkedin", "github")},
        "summary": summary,
        "experience": experience,
        "projects": projects,
        "education": education,
        "skills": skills,
        "note": _clean_bullet(response.get("note")),
        "integrity_warnings": warnings,
        # Advice, not an edit: a requirement the CV only names in its skills
        # list can't be moved into a role by a rewrite -- nothing on record
        # says that role used it. Only the candidate knows whether it did.
        "listed_only": list(listed_only or []),
    }
    # Reported after the fact rather than enforced: a target the model declined
    # to use is a judgement about one sentence, but zero of them landing means
    # the tailoring changed nothing the scorer will notice.
    document["target_coverage"] = cv_targeting.coverage(targets or [], document)
    return document


def _reconcile_skills(groups, profile: dict, cv_text: str,
                      blocked: set, warnings: list, required=None) -> list[dict]:
    """Keep the model's grouping, drop anything neither document claims, and
    put back anything the posting asked for that the model quietly lost.

    Two filters. A skill has to exist in the profile or somewhere in the CV
    text -- grouping is the model's job, sourcing is not. And nothing in
    `blocked` survives: those are the requirements the ATS pass found no
    evidence for, already narrowed against the profile by the caller so a
    skill the user added on the Profile page is not vetoed by a stale file.

    Then one restoration. Regrouping a skills section is an invitation to
    shorten it, and a model rewriting for an AI role dropped "Java" and
    "C/C++" -- both claimed by the profile, both named by the posting, 20
    points gone for nothing. Filtering is the model's to influence; deleting
    a claim the candidate makes and the job asks for is not.
    """
    known = {str(s).strip().casefold() for s in profile.get("skills_claimed") or []}
    haystack = (str(cv_text or "") + " \n"
                + cv_targeting.profile_text(profile)).casefold()

    out, used, unsourced, claimed_missing = [], set(), [], []
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        items = []
        for item in group.get("items") or []:
            name = re.sub(r"\s+", " ", str(item or "")).strip()
            key = name.casefold()
            if not name or key in used:
                continue
            if skill_matching.canonical(name) in blocked:
                claimed_missing.append(name)
                continue
            if key not in known and (not haystack or key not in haystack):
                unsourced.append(name)
                continue
            used.add(key)
            items.append(name)
        if items:
            out.append({"category": re.sub(r"\s+", " ", str(group.get("category") or "")).strip(),
                        "items": items})

    if claimed_missing:
        warnings.append("Removed skills from the missing-requirements list: "
                        + ", ".join(claimed_missing[:8]))
    if unsourced:
        warnings.append("Removed skills that aren't in your profile or CV: "
                        + ", ".join(unsourced[:8]))

    restored = _restore_required_skills(out, used, profile, blocked, required)
    if restored:
        warnings.append("Restored skills the posting asks for that the rewrite "
                        "dropped: " + ", ".join(restored[:8]))

    if not out and known:
        # The model returned nothing usable; an uncategorized list beats an
        # empty skills section on a CV that has a skills section.
        out = [{"category": "Skills",
                "items": [str(s) for s in profile.get("skills_claimed") or []]}]
    return out[:MAX_SKILL_GROUPS]


def _restore_required_skills(groups: list[dict], used: set, profile: dict,
                             blocked: set, required) -> list[str]:
    """Put back the profile's own skills that this posting names. Mutates
    `groups`; returns what it restored.

    Only skills the profile already claims, and only ones this posting asked
    for -- the point is not to reinstate the full list, it is that a tailored
    CV must not be MISSING something the untailored one had and the job wants.
    A restored skill joins the group that holds its siblings, so "Java" lands
    among the languages rather than in a bin at the end.
    """
    wanted = [str(r).strip() for r in (required or []) if str(r or "").strip()]
    if not wanted or not groups:
        return []

    restored = []
    for skill in profile.get("skills_claimed") or []:
        name = re.sub(r"\s+", " ", str(skill or "")).strip()
        key = name.casefold()
        if not name or key in used:
            continue
        canon = skill_matching.canonical(name)
        if canon in blocked:
            continue
        if not any(skill_matching.find_term(req, name) for req in wanted):
            continue
        target = _group_for(groups, name) or groups[0]
        target["items"].append(name)
        used.add(key)
        restored.append(name)
    return restored


def _group_for(groups: list[dict], skill: str) -> dict | None:
    """The group already holding a sibling of this skill, if there is one.

    Siblings are read off EXCLUSIVE_GROUPS -- the table that knows Java and
    Python are the same kind of thing precisely because neither substitutes
    for the other.
    """
    for group in groups:
        for item in group.get("items") or []:
            if skill_matching.are_mutually_exclusive(skill, str(item)):
                return group
    return None


# ---- docx --------------------------------------------------------------------

def save_cv_as_docx(document, output_path: str):
    from docx import Document
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    doc = Document()
    for line in render_cv_text(document).split("\n"):
        doc.add_paragraph(strip_markup(line))
    doc.save(output_path)
