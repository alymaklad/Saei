"""
Soft skills the CV demonstrates, read once from the whole profile.

A posting's soft-skill requirements ("attention to detail", "self-motivation",
"team management") almost never appear in a CV in those words -- they show in
what the work required. "Led the technical team and managed delivery of a
mobile e-commerce app" demonstrates team management and leadership without
naming either, so matching words finds nothing and retrieving lines one
requirement at a time finds little.

So the model is asked ONCE per profile: which soft skills does this work
demonstrate, and which line shows each? The answer is cached against the
profile's content and reused by every job until the profile changes, which
keeps the per-job cost at zero. The scorer then hands those lines to the
adjudication call it already makes (agents/ats_agent.py), so each soft-skill
requirement is judged against the evidence for it.

Every evidence line has to be a verbatim line of the profile -- checked here,
so a soft skill the model imagined never reaches a score.
"""
import hashlib
import json
import os

from langchain_core.messages import HumanMessage, SystemMessage

from agents import skill_matching
from agents.llm import get_llm, no_think_prefix, prepare_system, visible_content

PROMPT_VERSION = 2
CACHE_PATH = os.path.join("data", "soft_skills_cache.json")
MAX_CACHED_PROFILES = 10
MAX_SKILLS = 40

# The skills postings ask for most. The model goes through them one by one
# rather than listing whatever comes to mind -- a free list caught "curiosity"
# in a research internship and missed "experimentation mindset" in the same
# work. Anything else it finds is still welcome.
CHECKLIST = (
    "communication", "collaboration", "teamwork", "leadership", "team management",
    "mentoring", "coordination", "decision-making", "ownership", "initiative",
    "self-motivation", "independent work", "problem solving", "analytical thinking",
    "critical thinking", "attention to detail", "curiosity", "experimentation mindset",
    "creativity", "adaptability", "fast learning", "time management", "organization",
    "stakeholder communication", "strong interest in the field (name it, e.g. AI)",
)

_PROMPT = """You read the work section of a CV and decide which soft skills that work DEMONSTRATES. Respond with ONLY a JSON array, no prose, no code fences.

Each item: {"skill": "<soft skill>", "evidence": "<one CV line that shows it, copied verbatim>"}

Go through this checklist one skill at a time, and add an item for every skill
a specific line demonstrates (then add any other soft skill you notice):
""" + "\n".join(f"  - {skill}" for skill in CHECKLIST) + """

Rules:
- A soft skill is shown by what the work REQUIRED, not by the CV naming it:
  leading a team shows leadership and team management; coordinating tasks and
  decisions shows coordination and decision-making; defining evaluation
  criteria or reporting precise metrics shows analytical thinking and
  attention to detail; building self-initiated projects end to end shows
  self-motivation, initiative, ownership and independent work; research,
  training and comparing models, or trying new methods shows curiosity and an
  experimentation mindset; learning a new stack to ship something shows fast
  learning and adaptability; working with a team shows collaboration;
  building many systems in one field shows a strong interest in that field.
- Only list a skill a specific line supports. Never invent one, and never list
  technical skills (Python, LangChain, ...).
- Copy the evidence line exactly as given. The same line may support several
  skills; list each skill as its own item, with the single best line for it.
- Use the checklist's names where they fit."""


def _work_lines(spans: list[dict]) -> list[str]:
    """The lines that describe real work: role and project entries and their
    bullets. A skills list says nothing about how someone works."""
    seen, lines = set(), []
    for span in spans:
        if not span.get("demonstrated"):
            continue
        text = str(span.get("text") or "").strip()
        if text and text not in seen:
            seen.add(text)
            lines.append(text)
    return lines


def _cache_key(lines: list[str]) -> str:
    digest = hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()
    return f"v{PROMPT_VERSION}:{digest}"


def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    # Newest last; keep only the most recent profiles.
    if len(cache) > MAX_CACHED_PROFILES:
        for key in list(cache)[: len(cache) - MAX_CACHED_PROFILES]:
            cache.pop(key, None)
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
    except OSError:
        pass  # a cache that can't be written just means asking again


def _verified(items: list, lines: list[str]) -> list[dict]:
    """Keep the items whose evidence really is one of the CV's lines, with the
    line restored to exactly how the CV wrote it."""
    normalized = [(skill_matching.normalize(line), line) for line in lines]
    out, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        skill = str(item.get("skill") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        needle = skill_matching.normalize(evidence)[:60]
        if not skill or not needle:
            continue
        line = next((orig for norm, orig in normalized if needle in norm), None)
        key = (skill.lower(), line)
        if line and key not in seen:
            seen.add(key)
            out.append({"skill": skill, "evidence": line})
    return out[:MAX_SKILLS]


def _ask(lines: list[str]) -> list[dict]:
    system = prepare_system(_PROMPT)
    human = no_think_prefix() + "CV work lines:\n" + "\n".join(f"- {line}" for line in lines)
    resp = get_llm(temperature=0.0, max_tokens=3000, prompt_text=system + human).invoke([
        SystemMessage(content=system),
        HumanMessage(content=human),
    ])
    raw = visible_content(resp)
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        parsed = json.loads(raw[start:end + 1])
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def demonstrated_soft_skills(spans: list[dict]) -> list[dict]:
    """[{"skill", "evidence"}] for the profile these spans come from.

    Cached by the work lines' content, so it's one model call per profile
    version, not per job. Never raises: if the model is unavailable the
    scorer simply judges soft skills the old way, and nothing is cached so the
    next run tries again.
    """
    lines = _work_lines(spans)
    if not lines:
        return []
    key = _cache_key(lines)
    cache = _load_cache()
    if key in cache:
        return cache[key]
    try:
        found = _verified(_ask(lines), lines)
    except Exception as exc:  # noqa: BLE001 -- scoring must not fail over this
        print(f"[soft-skills] couldn't read soft skills from the profile: {exc}")
        return []
    if found:
        cache.pop(key, None)
        cache[key] = found
        _save_cache(cache)
    return found


def evidence_lines(found: list[dict]) -> dict[str, list[str]]:
    """{CV line: [soft skills it shows]}, in first-seen order."""
    by_line: dict[str, list[str]] = {}
    for item in found:
        by_line.setdefault(item["evidence"], []).append(item["skill"])
    return by_line
