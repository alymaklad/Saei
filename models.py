"""SQLAlchemy models — SQLite-backed, file-based, no hosted DB required (free)."""
from sqlalchemy import Column, Integer, String, Float, Text, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import declarative_base
from datetime import datetime, timezone

Base = declarative_base()


def _utcnow():
    return datetime.now(timezone.utc)


class Job(Base):
    __tablename__ = "jobs"
    id = Column(Integer, primary_key=True)
    url = Column(String, unique=True, nullable=False)  # dedup key
    title = Column(String)
    company = Column(String)
    source_site = Column(String)          # "greenhouse", "lever", "google_jobs", "manual_link"
    description = Column(Text)
    is_whitelisted_source = Column(Boolean, default=False)
    date_found = Column(DateTime, default=_utcnow)


class Application(Base):
    __tablename__ = "applications"
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"))
    ats_score = Column(Float)
    # Which engine produced ats_score/ats_breakdown ("legacy" | "requirements").
    # NULL on every row written before the cutover, which is meaningful rather
    # than missing: those predate the flag and are all legacy, so readers treat
    # NULL as "legacy" (see api.py's applications listing).
    #
    # Stored per row, not read from config.SCORING_ENGINE, because the setting
    # describes what the NEXT run will do -- reading it to interpret history
    # would relabel every stored legacy breakdown as requirements-shaped the
    # moment the default flipped, and the dashboard would render each one
    # against the wrong set of keys.
    scoring_engine = Column(String, nullable=True)
    # JSON-encoded agents.ats_agent.compute_ats_score()["breakdown"] -- the
    # per-pillar (keyword match / formatting / section completeness /
    # experience alignment) scores and evidence behind ats_score.
    ats_breakdown = Column(Text, nullable=True)
    # Plain-English rendering of ats_breakdown -- what the Dashboard shows
    # under "why this score", built deterministically, no extra LLM call.
    ats_explanation = Column(Text, nullable=True)
    # Only set for jobs that went through the CV-rewrite path: the same CV
    # re-scored against the same job description after tailoring, plus the
    # breakdown/explanation of how the tailored version compares.
    tailored_ats_score = Column(Float, nullable=True)
    tailored_ats_breakdown = Column(Text, nullable=True)
    tailored_ats_explanation = Column(Text, nullable=True)
    # Ranking-agent output (agents/ranking_agent.py) -- "is this job right for
    # ME", as opposed to ats_score's "would my CV survive THIS employer's ATS".
    # Set during the search/ranking stage, before the orchestrator runs, so
    # it's present even for jobs the MATCH_SCORE_THRESHOLD gate held back.
    match_score = Column(Float, nullable=True)
    match_breakdown = Column(Text, nullable=True)   # JSON: per-factor score/weight/evidence
    cv_version_path = Column(String)
    status = Column(String)               # scored_low | drafted | auto_submitted | pending_review | sent
    email_sent = Column(Boolean, default=False)
    date_applied = Column(DateTime, nullable=True)
    date_created = Column(DateTime, default=_utcnow)


class SkillGap(Base):
    __tablename__ = "skill_gaps"
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"))
    missing_skills = Column(Text)         # JSON-encoded list
    date_created = Column(DateTime, default=_utcnow)


class NewsDigest(Base):
    __tablename__ = "news_digests"
    id = Column(Integer, primary_key=True)
    field = Column(String)
    content = Column(Text)
    date_created = Column(DateTime, default=_utcnow)


class EmailLog(Base):
    """One row per attempted send -- what the Email tab lists."""
    __tablename__ = "email_logs"
    id = Column(Integer, primary_key=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=True)
    to_email = Column(String)
    subject = Column(String)
    gmail_message_id = Column(String, nullable=True)
    dry_run = Column(Boolean, default=True)
    status = Column(String)               # "sent" | "dry_run" | "failed"
    error_message = Column(Text, nullable=True)
    sent_at = Column(DateTime, default=_utcnow)


class SearchSite(Base):
    """A website to search for jobs on, beyond the .env-configured
    GREENHOUSE_BOARD_TOKENS/LEVER_COMPANY_SLUGS lists. Managed from the Search
    tab instead of by hand-editing .env. A few verified job boards (currently
    Wuzzuf, Bayt.com -- see agents.search_agent.KNOWN_JOB_BOARD_TEMPLATES)
    are seeded here by default the first time the app runs, so the list isn't
    empty out of the box, but from then on they're ordinary rows the user can
    delete like anything else added by hand."""
    __tablename__ = "search_sites"
    id = Column(Integer, primary_key=True)
    url = Column(String, unique=True, nullable=False)
    site_type = Column(String)            # "greenhouse" | "lever" | "generic" | a KNOWN_JOB_BOARD_TEMPLATES key
    identifier = Column(String, nullable=True)  # board token / company slug, None for generic/templated
    label = Column(String, nullable=True)
    date_added = Column(DateTime, default=_utcnow)


class QueryExpansionCache(Base):
    """One row per distinct Position string the user has searched with.

    Query expansion (agents/query_expansion_agent.py) is an LLM call, and the
    scheduler runs daily -- re-asking for the same expansion of an unchanged
    Position every morning would burn a call a day for an identical answer.
    Cached here instead, and only regenerated when the Position text actually
    changes (or the row is deleted). cv_hash is stored alongside because the
    expansion is CV-aware: uploading a materially different CV should produce
    a differently-calibrated role list, so a changed CV invalidates the entry
    the same way a changed Position does.
    """
    __tablename__ = "query_expansion_cache"
    id = Column(Integer, primary_key=True)
    position_query = Column(String, unique=True, nullable=False)
    cv_hash = Column(String, nullable=True)
    expanded_roles = Column(Text)         # JSON-encoded list of role phrases
    date_created = Column(DateTime, default=_utcnow)


class CvProfile(Base):
    """The user's structured CV data, as an EDITABLE record.

    agents/cv_profile.py already extracted exactly this shape -- dated roles,
    projects, education, the skills list -- but only ever as a disposable
    cache entry keyed by CV hash. That had two consequences worth fixing:

    1. An extraction mistake was unfixable. A misread date, a bullet dropped
       from a two-column layout, an internship classified is_professional=false
       -- each silently distorted every score from then on, and the only way to
       correct it was to edit the PDF and re-upload. Nothing in the app let the
       person whose CV it is say "no, that role ended in March".
    2. The CV rewriter never saw it. rewrite_cv() re-reads the flattened PDF
       text on every tailoring run, so scoring trusts structured data while
       tailoring trusts raw text, and the two can disagree about the same CV.

    This row is the source of truth both of those should read from. The file on
    disk is how it gets seeded, not what it is.

    Deliberately one row for one local user (see profile_store.load(), which
    takes the most recent). Storing `data` as a JSON blob rather than
    normalised child tables is a considered trade: the shape is defined by
    agents/cv_profile.py's extraction schema and changes with it, and a
    five-table join would buy nothing here -- nothing queries INTO a profile,
    it is always read and written whole.
    """
    __tablename__ = "cv_profiles"
    id = Column(Integer, primary_key=True)
    # JSON-encoded profile dict -- same schema agents/cv_profile.py produces,
    # plus the contact/summary block the scorer never needed but a generated
    # CV does.
    data = Column(Text)
    # Provenance: which uploaded file this was extracted from, and that file's
    # content hash. The hash is what lets the UI say "your CV file has changed
    # since this profile was extracted" instead of silently scoring against a
    # profile of a document the user has already replaced -- a mismatch that
    # has actually occurred in this project.
    source_cv_filename = Column(String, nullable=True)
    source_cv_hash = Column(String, nullable=True)
    extracted_at = Column(DateTime, nullable=True)
    edited_at = Column(DateTime, nullable=True)
    # JSON-encoded list of section names the user has hand-edited since the
    # last extraction. Re-extracting overwrites the AI-readable fields, so the
    # confirmation dialog needs to name what specifically is about to be lost
    # -- a generic "this may overwrite your changes" gives the user no basis
    # for deciding.
    edited_sections = Column(Text, nullable=True)
    date_created = Column(DateTime, default=_utcnow)


class ReportLog(Base):
    """One row per daily/weekly report send attempt -- what the Reports tab lists."""
    __tablename__ = "report_logs"
    id = Column(Integer, primary_key=True)
    report_type = Column(String)          # "daily" | "weekly"
    content = Column(Text)
    dry_run = Column(Boolean, default=True)
    status = Column(String)               # "sent" | "dry_run" | "failed"
    error_message = Column(Text, nullable=True)
    sent_at = Column(DateTime, default=_utcnow)
