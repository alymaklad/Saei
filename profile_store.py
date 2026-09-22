"""
Read/write layer for the user's structured CV profile (models.CvProfile).

Why this sits between agents/cv_profile.py and everything else
--------------------------------------------------------------
agents/cv_profile.py knows how to EXTRACT a profile from CV text -- one LLM
call, cached by content hash. It deliberately knows nothing about the
database, and it produces a value that is thrown away once scoring finishes.

This module owns the profile as a PERSISTED, USER-OWNED record instead:

    extraction (agents/cv_profile.py)  ->  seeds  ->  the stored profile
                                                          |
                                     user edits it in the Profile page
                                                          |
                          scoring and CV tailoring both read from here

The distinction matters because extraction is not reliable enough to be the
final word. A misread end date, a bullet lost to a two-column PDF layout, an
internship marked is_professional=false -- each one quietly distorts every
score from then on, and before this module existed the only way to correct any
of them was to edit the source PDF and re-upload. The person whose CV it is
had no way to say "no, that role ended in March".

The re-extract flow is therefore built around not destroying edits silently.
`changed_sections()` reports exactly which sections a fresh extraction would
alter, so the UI can name them before the user commits, rather than offering a
generic "this may overwrite your changes" that gives them nothing to decide on.
"""
import hashlib
import json
from datetime import datetime, timezone

from db import get_session
from models import CvProfile

# Order matters: this is the order the Profile page renders sections in, and
# the order changed_sections() reports them in.
SECTIONS = ("contact", "summary", "experience", "projects", "education",
            "certifications", "skills_claimed")

SECTION_LABELS = {
    "contact": "Contact",
    "summary": "Professional summary",
    "experience": "Experience",
    "projects": "Projects",
    "education": "Education",
    "certifications": "Certifications",
    "skills_claimed": "Skills",
}

# "title" is the headline a CV carries under the name ("AI Engineer",
# "Senior Backend Developer"). It sits in contact rather than in its own
# section because that is where it is printed -- it is part of the header
# block, not a separate qualification.
CONTACT_FIELDS = ("full_name", "title", "email", "phone", "location",
                  "linkedin", "github")


def empty_profile() -> dict:
    return {
        "contact": {field: "" for field in CONTACT_FIELDS},
        "summary": "",
        "experience": [],
        "projects": [],
        "education": [],
        "certifications": [],
        "skills_claimed": [],
        "seniority_self_described": None,
    }


# ---- normalisation -----------------------------------------------------------
#
# Everything entering this module gets normalised, from BOTH directions: an LLM
# extraction that returned a string where a list belongs, and a JSON body
# posted by the frontend. The scorer indexes into these structures without
# defensive checks (profile["experience"][i]["bullets"]), so a malformed entry
# surfaces as a TypeError mid-scoring-run rather than as a validation error at
# the point it was introduced. Coercing once, here, keeps that impossible.

def _clean_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def _clean_optional(value):
    """Empty string and 'null' both mean absent. The frontend sends "" for a
    cleared input; the LLM sometimes sends the literal string "null"."""
    text = _clean_str(value)
    return None if text.lower() in ("", "null", "none") else text


def _clean_str_list(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out, seen = [], set()
    for item in value:
        # Certifications arrive either as bare strings (what the extraction
        # prompt asks for) or as {name, issuer, date} objects (what the
        # Profile page's certification rows produce). Both are accepted; the
        # object form is flattened to its display string so downstream
        # evidence matching keeps seeing plain text.
        if isinstance(item, dict):
            parts = [_clean_str(item.get(k)) for k in ("name", "issuer", "date")]
            text = " — ".join(p for p in parts if p)
        else:
            text = _clean_str(item)
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
    return out


def _clean_bullets(value) -> list[str]:
    if isinstance(value, str):
        # A bullet block pasted or edited as one textarea, one bullet per line.
        value = value.split("\n")
    if not isinstance(value, list):
        return []
    return [b for b in (_clean_str(v).lstrip("-•* ").strip() for v in value) if b]


def _clean_experience(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = _clean_str(item.get("title"))
        organization = _clean_str(item.get("organization"))
        bullets = _clean_bullets(item.get("bullets"))
        if not (title or organization or bullets):
            continue  # a wholly blank row the user added and never filled in
        out.append({
            "title": title,
            "organization": organization,
            # Where the role was held. A plain string rather than structured
            # city/country: CVs write it a dozen ways ("Cairo, Egypt",
            # "Remote", "Hybrid — Berlin") and none of them is worth parsing
            # when the value is only ever printed back out.
            "location": _clean_str(item.get("location")),
            "start": _clean_optional(item.get("start")),
            "end": _clean_optional(item.get("end")),
            # Defaults to True: the extraction prompt's own rule is that paid
            # employment including internships counts, and an entry the user
            # typed by hand into an experience section is far more likely to
            # be a job than a course. Miscounting a course as work is a
            # visible, correctable one-click error; silently dropping a real
            # job from the years total is neither.
            "is_professional": bool(item.get("is_professional", True)),
            "bullets": bullets,
        })
    return out


def _clean_projects(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = _clean_str(item.get("name"))
        bullets = _clean_bullets(item.get("bullets"))
        if not (name or bullets):
            continue
        out.append({
            "name": name,
            # Repo or demo URL. Stored verbatim -- no scheme is prepended and
            # nothing is validated, because a project link is also written as
            # "github.com/user/repo" or "internal GitLab" and rejecting those
            # would lose real information the user typed.
            "url": _clean_str(item.get("url")),
            "start": _clean_optional(item.get("start")),
            "end": _clean_optional(item.get("end")),
            "bullets": bullets,
        })
    return out


def _clean_education(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if not isinstance(item, dict):
            continue
        fields = {k: _clean_str(item.get(k))
                  for k in ("degree", "field", "institution", "location")}
        # Location alone is not an education entry -- an added row where only
        # the location was filled in is still a blank row.
        if not any(fields[k] for k in ("degree", "field", "institution")):
            continue
        out.append({**fields,
                    "start": _clean_optional(item.get("start")),
                    "end": _clean_optional(item.get("end"))})
    return out


def normalize(data) -> dict:
    """Coerce anything profile-shaped into the exact structure the scorer and
    the CV rewriter index into. Never raises."""
    if not isinstance(data, dict):
        return empty_profile()

    raw_contact = data.get("contact")
    raw_contact = raw_contact if isinstance(raw_contact, dict) else {}
    contact = {field: _clean_str(raw_contact.get(field)) for field in CONTACT_FIELDS}

    seniority = _clean_optional(data.get("seniority_self_described"))

    return {
        "contact": contact,
        "summary": _clean_str(data.get("summary")),
        "experience": _clean_experience(data.get("experience")),
        "projects": _clean_projects(data.get("projects")),
        "education": _clean_education(data.get("education")),
        "certifications": _clean_str_list(data.get("certifications")),
        "skills_claimed": _clean_str_list(data.get("skills_claimed")),
        "seniority_self_described": seniority,
    }


def changed_sections(before: dict, after: dict) -> list[str]:
    """Which sections differ between two profiles.

    Used two ways, and the symmetry is the point: to record what the user
    edited when they save, and to preview what a re-extraction would overwrite
    before they confirm it.
    """
    before, after = normalize(before), normalize(after)
    return [name for name in SECTIONS if before.get(name) != after.get(name)]


# ---- persistence -------------------------------------------------------------

def _utcnow():
    return datetime.now(timezone.utc)


def cv_hash(cv_text: str) -> str:
    return hashlib.sha256((cv_text or "").encode("utf-8")).hexdigest()


def _row_to_payload(row: CvProfile) -> dict:
    try:
        data = json.loads(row.data) if row.data else {}
    except json.JSONDecodeError:
        data = {}
    try:
        edited = json.loads(row.edited_sections) if row.edited_sections else []
    except json.JSONDecodeError:
        edited = []
    return {
        "profile": normalize(data),
        "source_cv_filename": row.source_cv_filename,
        "source_cv_hash": row.source_cv_hash,
        "extracted_at": row.extracted_at.isoformat() if row.extracted_at else None,
        "edited_at": row.edited_at.isoformat() if row.edited_at else None,
        "edited_sections": [s for s in edited if s in SECTIONS],
    }


def load() -> dict | None:
    """The stored profile, or None if the user has never had one extracted."""
    with get_session() as session:
        row = session.query(CvProfile).order_by(CvProfile.id.desc()).first()
        return _row_to_payload(row) if row else None


def load_profile_dict() -> dict | None:
    """Just the profile itself, for callers that score or tailor with it.

    Returns None rather than an empty profile when nothing is stored, so a
    caller can fall back to live extraction instead of scoring a real CV
    against a blank record and reporting zero experience.
    """
    stored = load()
    return stored["profile"] if stored else None


def save(profile: dict, *, mark_edited: bool = True) -> dict:
    """Write the profile, recording which sections changed.

    `mark_edited=False` is for extraction writes: a fresh extraction is not a
    user edit, and recording it as one would make the re-extract dialog warn
    about losing changes the user never made.
    """
    normalized = normalize(profile)
    now = _utcnow()

    with get_session() as session:
        row = session.query(CvProfile).order_by(CvProfile.id.desc()).first()
        if row is None:
            row = CvProfile()
            session.add(row)
            previous, previously_edited = empty_profile(), []
        else:
            try:
                previous = json.loads(row.data) if row.data else {}
            except json.JSONDecodeError:
                previous = {}
            try:
                previously_edited = json.loads(row.edited_sections or "[]")
            except json.JSONDecodeError:
                previously_edited = []

        if mark_edited:
            touched = set(previously_edited) | set(changed_sections(previous, normalized))
            row.edited_sections = json.dumps([s for s in SECTIONS if s in touched])
            if touched:
                row.edited_at = now
        row.data = json.dumps(normalized, ensure_ascii=False)
        session.flush()

    return load()


def save_extraction(profile: dict, *, cv_filename: str | None,
                    cv_text: str | None) -> dict:
    """Record a fresh extraction as the stored profile.

    Clears the edited-section list, because those edits are exactly what has
    just been replaced -- leaving them set would have the UI warn forever about
    changes that no longer exist.
    """
    normalized = normalize(profile)
    now = _utcnow()

    with get_session() as session:
        row = session.query(CvProfile).order_by(CvProfile.id.desc()).first()
        if row is None:
            row = CvProfile()
            session.add(row)
        row.data = json.dumps(normalized, ensure_ascii=False)
        row.source_cv_filename = cv_filename
        row.source_cv_hash = cv_hash(cv_text) if cv_text is not None else None
        row.extracted_at = now
        row.edited_at = None
        row.edited_sections = json.dumps([])
        session.flush()

    return load()


def is_stale(stored: dict | None, cv_text: str | None) -> bool:
    """True when the CV file on disk is not the one the profile came from.

    This project has already hit exactly this state once: a cached profile
    keyed to a 6,287-character CV while the file on disk had become a
    4,475-character rewrite of it. Nothing surfaced the mismatch -- scoring
    simply used a profile of a document that no longer existed. Surfacing it is
    the whole reason source_cv_hash is stored.
    """
    if not stored or cv_text is None:
        return False
    if not stored.get("source_cv_hash"):
        return False
    return stored["source_cv_hash"] != cv_hash(cv_text)
