"""
Query Expansion Agent.

Turns one typed Position ("AI Engineer") into a list of target role phrases
("AI Engineer", "Machine Learning Engineer", "Generative AI Engineer",
"AI Software Engineer", ...) that the search stage then uses two ways:

  1. Role 1 sources (SerpAPI, Wuzzuf, SimplyHired, generic scraper) need a
     literal query string to fetch anything at all -- each phrase becomes its
     own search, widening what gets pulled in the first place.
  2. Role 2 sources (Greenhouse/Lever/RemoteOK/WWR boards, which return
     everything regardless of query) use the list as a broadened title filter
     -- a job matches if its title token-matches ANY phrase, not just the one
     originally typed.

Why an LLM rather than a static taxonomy or synonym dict: the useful
expansions include recent and compound role names ("Agentic AI Engineer",
"Gen AI Engineer") that pre-built title taxonomies and embedding models
trained on older job corpora are structurally bad at surfacing -- which is
exactly the case that matters most here.

The expansion is CV-aware: expanding "AI Engineer" generically produces
generic synonyms, but expanding it in light of a CV that mentions PyTorch and
agent orchestration produces a list actually calibrated to this candidate.

Caching (models.QueryExpansionCache, keyed on the Position string) is
OPTIONAL and OFF by default -- see config.QUERY_EXPANSION_CACHE. It exists
for the case where a daily scheduler would otherwise burn a metered
provider's quota re-asking for an identical answer; with a local model an
expansion costs seconds, and regenerating each run means prompt edits, model
swaps and temperature variation are visible immediately rather than masked
by a stale row.
"""
import json

from langchain_core.messages import SystemMessage, HumanMessage

import config
from agents.llm import get_llm
from agents.embeddings import _content_hash

# How much of the CV reaches the prompt -- see config.QUERY_EXPANSION_CV_CHARS.
# 0 (the default) sends the whole thing. Read from config at call time rather
# than captured here, so changing the setting doesn't need a code edit.

_SYSTEM_PROMPT = (
    "You expand a job-title search query into the set of DIFFERENT job titles "
    "that describe substantially the same kind of role, so a job board search "
    "doesn't miss postings that use different wording.\n"
    "Rules:\n"
    "- Return real job titles a company would actually post, not skills or "
    "descriptions.\n"
    "- Include common abbreviations and spelled-out variants of the same role.\n"
    "- Stay at the SAME seniority as the query: do not add 'Senior'/'Junior'/"
    "'Lead'/'Head of' variants, since seniority is filtered separately.\n"
    "- Do not include unrelated adjacent roles the candidate could not "
    "reasonably apply to.\n"
    "- Respond with ONLY a JSON array of strings, nothing else."
    "For example, if the query is 'AI Engineer', a valid response is:\n"
    '["AI Engineer", "Machine Learning Engineer", "Deep Learning Engineer", "Gen AI Engineer", "Agentic AI Engineer", "AI Software Engineer"]\n'
)


def _safe_json_list(raw: str) -> list[str]:
    """Same tolerant extraction agents/ats_agent.py uses -- models routinely
    wrap a JSON array in prose or a ``` fence, and a parse failure here should
    degrade to 'no expansion' rather than raise."""
    import re
    match = re.search(r"\[.*\]", raw or "", re.DOTALL)
    candidate = match.group(0) if match else (raw or "")
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, list):
            return [str(s).strip() for s in parsed if str(s).strip()]
    except json.JSONDecodeError:
        pass
    return []


def _dedupe_preserving_order(phrases: list[str]) -> list[str]:
    seen = set()
    out = []
    for phrase in phrases:
        key = phrase.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(phrase.strip())
    return out


def generate_target_roles(position: str, candidate_text: str = "", max_roles: int | None = None) -> list[str]:
    """One LLM call. Returns the expanded phrases with the ORIGINAL position
    always first -- the user's own wording is never dropped in favor of the
    model's paraphrases of it, so expansion can only ever widen the search,
    never redirect it somewhere the user didn't ask for."""
    position = (position or "").strip()
    if not position:
        return []

    limit = max_roles if max_roles is not None else config.QUERY_EXPANSION_MAX_ROLES

    human = f"Query job title: {position}\nReturn at most {limit} titles."
    if candidate_text:
        cap = config.QUERY_EXPANSION_CV_CHARS
        excerpt = candidate_text[:cap] if cap and cap > 0 else candidate_text
        label = "(excerpt)" if len(excerpt) < len(candidate_text) else ""
        human += (
            f"\n\nCalibrate the list to this candidate's actual background{label}:\n"
            + excerpt
        )

    try:
        resp = get_llm(temperature=0.7).invoke([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=human),
        ])
        expanded = _safe_json_list(resp.content)
    except Exception:  # noqa: BLE001 -- LLM unreachable/misconfigured/rate-limited
        # Expansion is an enhancement, not a requirement: falling back to just
        # the typed position degrades to exactly the pre-expansion behavior
        # rather than failing the whole search run.
        return [position]

    return _dedupe_preserving_order([position] + expanded)[: limit + 1]


# Whether the LAST get_target_roles() call was served from the cache. Read by
# jobs/daily_run.py so the debug report's Query Expansion sheet says where the
# phrases actually came from, instead of always claiming the LLM produced
# them. Module-level rather than a changed return type so every existing
# caller and test keeps working.
LAST_EXPANSION_CACHED = False


def get_target_roles(position: str, candidate_text: str = "", use_cache: bool | None = None) -> list[str]:
    """Entry point the search pipeline actually calls.

    Caching is controlled by config.QUERY_EXPANSION_CACHE and defaults OFF --
    every run regenerates. That's deliberate: the cache only ever existed to
    stop a daily scheduler burning a hosted provider's quota on an identical
    answer, and on a local model an expansion costs seconds instead. Leaving
    it off also means prompt edits, a model swap, or temperature variation
    show up in the very next run rather than being masked by a stale row.

    `candidate_text` is the user's PROFILE rendered to text, not the uploaded
    file's: the roles are calibrated to what the candidate has actually done,
    and the profile is the record they maintain and correct. Searching is the
    first stage of the run, so reading a stale document here is the most
    expensive place to do it -- it decides which jobs are ever seen.

    When caching IS enabled, the key is the Position string, with a hash of
    that text stored alongside and compared -- so editing the profile (or
    uploading a materially different CV, which refills it) re-expands rather
    than silently reusing a list calibrated to the old one. The column is
    still called cv_hash; what it hashes is the profile.

    `use_cache` overrides the config for a single call (tests use it); leave
    it None to follow the setting.
    """
    global LAST_EXPANSION_CACHED
    LAST_EXPANSION_CACHED = False

    position = (position or "").strip()
    if not position:
        return []
    if not config.SEARCH_QUERY_EXPANSION:
        return [position]  # escape hatch -- literal pre-expansion behavior

    caching = config.QUERY_EXPANSION_CACHE if use_cache is None else use_cache
    cv_hash = _content_hash(candidate_text) if candidate_text else None

    if caching:
        cached = _read_cache(position)
        if cached and cached.get("cv_hash") == cv_hash and cached.get("roles"):
            LAST_EXPANSION_CACHED = True
            return cached["roles"]

    roles = generate_target_roles(position, candidate_text=candidate_text)

    if caching and roles:
        _write_cache(position, cv_hash, roles)
    return roles


def _read_cache(position: str) -> dict | None:
    from db import get_session
    from models import QueryExpansionCache
    with get_session() as session:
        row = (
            session.query(QueryExpansionCache)
            .filter(QueryExpansionCache.position_query == position)
            .first()
        )
        if not row:
            return None
        try:
            roles = json.loads(row.expanded_roles or "[]")
        except (TypeError, json.JSONDecodeError):
            return None
        return {"roles": roles if isinstance(roles, list) else [], "cv_hash": row.cv_hash}


def _write_cache(position: str, cv_hash: str | None, roles: list[str]) -> None:
    from db import get_session
    from models import QueryExpansionCache
    with get_session() as session:
        row = (
            session.query(QueryExpansionCache)
            .filter(QueryExpansionCache.position_query == position)
            .first()
        )
        if row:
            row.cv_hash = cv_hash
            row.expanded_roles = json.dumps(roles)
        else:
            session.add(QueryExpansionCache(
                position_query=position,
                cv_hash=cv_hash,
                expanded_roles=json.dumps(roles),
            ))
