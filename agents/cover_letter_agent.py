"""
Cover Letter Agent.

Writes a short cover letter for one job from the stored profile -- the same
record the CV is tailored from -- so the letter can only claim what the
profile says. One LLM call; when that call fails (usage limit spent, provider
down) a letter is assembled from the profile's own facts instead, because a
plain, true letter is better than none and the job's scoring and tailoring
shouldn't be thrown away over it.

Returns {"text": str, "source": "llm" | "template"} so the caller can say
which one went out.
"""
import re

from langchain_core.messages import HumanMessage, SystemMessage

import config
from agents.llm import get_llm, no_think_prefix, prepare_system, visible_content

MAX_JOB_CHARS = 3000      # the posting's opening carries the role and the asks
MAX_WORDS = 260

_SYSTEM_PROMPT = """You write cover letters for job applications.

Rules:
- Use ONLY facts present in the candidate profile. Never invent employers,
  titles, dates, numbers, degrees, tools or achievements. If the profile
  doesn't show a requirement, don't claim it -- write around it.
- 170 to 240 words, 3 or 4 short paragraphs, plain text. No markdown, no
  bullet points, no placeholders like [Company] or [Your Name].
- Open with the role and why this candidate fits it; connect 2-3 concrete
  pieces of the candidate's experience or projects to what the job asks for;
  close with a brief, confident call to action.
- Start with "Dear Hiring Team," unless the posting names a person.
- End with "Best regards," on its own line followed by the candidate's name.
- Write in English, in the candidate's voice (first person)."""


def _profile_brief(profile: dict) -> str:
    """The parts of the profile a letter draws on, as compact text."""
    contact = profile.get("contact") or {}
    lines = [f"Name: {contact.get('full_name') or ''}",
             f"Headline: {contact.get('title') or ''}",
             f"Location: {contact.get('location') or ''}",
             f"Summary: {profile.get('summary') or ''}"]
    for e in (profile.get("experience") or [])[:4]:
        lines.append(f"Experience: {e.get('title')} at {e.get('organization')} "
                     f"({e.get('start') or '?'} - {e.get('end') or 'present'})")
        lines.extend(f"  - {b}" for b in (e.get("bullets") or [])[:3])
    for p in (profile.get("projects") or [])[:3]:
        lines.append(f"Project: {p.get('name')}")
        lines.extend(f"  - {b}" for b in (p.get("bullets") or [])[:2])
    for ed in (profile.get("education") or [])[:2]:
        lines.append(f"Education: {ed.get('degree') or ''} {ed.get('field') or ''}, "
                     f"{ed.get('institution') or ''}".strip())
    skills = profile.get("skills_claimed") or []
    if skills:
        lines.append("Skills: " + ", ".join(str(s) for s in skills[:30]))
    return "\n".join(line for line in lines if line.strip())


def _matched(requirement_names: list[str] | None) -> str:
    names = [n for n in (requirement_names or []) if n][:8]
    return ", ".join(names)


def _clean(text: str, name: str) -> str:
    text = re.sub(r"\*\*|__|^#+\s*", "", text.strip(), flags=re.M)
    if re.search(r"\[[^\]]{2,30}\]", text):  # an unfilled placeholder slipped through
        raise ValueError("the letter still contains a placeholder")
    if name and name.split()[0].lower() not in text[-120:].lower():
        text = f"{text.rstrip()}\n\nBest regards,\n{name}"
    return text


def template_letter(profile: dict, job: dict, requirement_names: list[str] | None = None) -> str:
    """A plain letter from the profile's facts, used when the LLM is unavailable."""
    contact = profile.get("contact") or {}
    name = contact.get("full_name") or ""
    headline = contact.get("title") or "professional"
    title = job.get("title") or "this role"
    company = job.get("company") or "your team"
    parts = [f"Dear Hiring Team,",
             f"I am writing to apply for the {title} position at {company}. "
             f"As {_article(headline)} {headline}, I believe my background is a strong match for this role."]
    exp = (profile.get("experience") or [])[:2]
    if exp:
        roles = "; ".join(f"{e.get('title')} at {e.get('organization')}" for e in exp)
        bullet = next((b for e in exp for b in (e.get("bullets") or [])), "")
        parts.append(f"My recent experience includes {roles}." + (f" There, I {_lower_first(bullet)}" if bullet else ""))
    matched = _matched(requirement_names)
    if matched:
        parts.append(f"The role's focus on {matched} lines up closely with the work I have been doing.")
    parts.append("My CV is attached. I would welcome the chance to discuss how I can contribute to your team.")
    parts.append(f"Best regards,\n{name}".rstrip())
    return "\n\n".join(parts)


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _lower_first(text: str) -> str:
    text = text.strip().rstrip(".") + "."
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


def generate_cover_letter(profile: dict | None, job: dict,
                          requirement_names: list[str] | None = None) -> dict:
    """{"text", "source"} for one job. `requirement_names` are the job's
    requirements the profile already evidences (from the ATS result), which
    tell the model what to connect."""
    profile = profile or {}
    name = (profile.get("contact") or {}).get("full_name") or ""
    description = (job.get("description") or "")[:MAX_JOB_CHARS]
    human = (
        f"{no_think_prefix()}"
        f"JOB TITLE: {job.get('title') or ''}\n"
        f"COMPANY: {job.get('company') or ''}\n"
        f"REQUIREMENTS THE CANDIDATE ALREADY MEETS: {_matched(requirement_names) or '(not scored)'}\n\n"
        f"JOB POSTING (start):\n{description}\n\n"
        f"CANDIDATE PROFILE:\n{_profile_brief(profile)}\n\n"
        "Write the cover letter now. Output only the letter."
    )
    try:
        resp = get_llm(temperature=0.4, max_tokens=600, prompt_text=human).invoke([
            SystemMessage(content=prepare_system(_SYSTEM_PROMPT)),
            HumanMessage(content=human),
        ])
        text = _clean(visible_content(resp), name)
        if len(text.split()) < 60:
            raise ValueError("the letter came back too short")
        words = text.split()
        if len(words) > MAX_WORDS + 80:
            raise ValueError("the letter came back too long")
        return {"text": text, "source": "llm"}
    except Exception as exc:  # noqa: BLE001 -- a letter must never sink the job
        print(f"[cover-letter] using the template letter: {exc}")
        return {"text": template_letter(profile, job, requirement_names), "source": "template"}
