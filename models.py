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
