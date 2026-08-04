"""
Dashboard + control API. Most endpoints are read-only views over the SQLite
DB the agents write to (models.py/db.py); a few (settings, CV upload) let the
frontend write configuration back to disk for a single local user.

Run with: uvicorn api:app --reload --port 8000
"""
import importlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
import cv_parser
import env_store
from db import get_session, init_db
from models import Job, Application, SkillGap, NewsDigest

app = FastAPI(title="Job Application Agent API")

# Tighten allow_origins to your deployed frontend's exact origin before going live.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

init_db()

CV_DIR = Path("cv")
CV_ALLOWED_EXTENSIONS = {".pdf", ".docx"}


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/status")
def status():
    return {
        "dry_run": config.DRY_RUN,
        "whitelisted_sources": sorted(config.WHITELISTED_SOURCES),
        "llm_provider": config.LLM_PROVIDER,
    }


@app.get("/api/stats")
def stats():
    with get_session() as session:
        total_jobs = session.query(Job).count()
        total_apps = session.query(Application).count()
        pending = session.query(Application).filter(Application.status == "pending_review").count()
        auto_submitted = session.query(Application).filter(Application.status == "auto_submitted").count()
        emails_sent = session.query(Application).filter(Application.email_sent.is_(True)).count()
    return {
        "total_jobs": total_jobs,
        "total_applications": total_apps,
        "pending_review": pending,
        "auto_submitted": auto_submitted,
        "emails_sent": emails_sent,
    }


@app.get("/api/applications")
def list_applications(limit: int = 100):
    with get_session() as session:
        rows = (
            session.query(Application, Job)
            .join(Job, Application.job_id == Job.id)
            .order_by(Application.date_created.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": a.id,
                "title": j.title,
                "company": j.company,
                "source": j.source_site,
                "url": j.url,
                "ats_score": a.ats_score,
                "status": a.status,
                "cv_path": a.cv_version_path,
                "email_sent": a.email_sent,
                "date_created": a.date_created.isoformat() if a.date_created else None,
                "date_applied": a.date_applied.isoformat() if a.date_applied else None,
            }
            for a, j in rows
        ]


@app.get("/api/skill-gaps")
def skill_gaps(limit: int = 20):
    with get_session() as session:
        rows = session.query(SkillGap).order_by(SkillGap.date_created.desc()).limit(500).all()
    counts = Counter()
    for row in rows:
        try:
            skills = json.loads(row.missing_skills)
        except (TypeError, json.JSONDecodeError):
            skills = []
        counts.update(skills)
    return [{"skill": skill, "count": count} for skill, count in counts.most_common(limit)]


@app.get("/api/news")
def news(limit: int = 5):
    with get_session() as session:
        rows = session.query(NewsDigest).order_by(NewsDigest.date_created.desc()).limit(limit).all()
    return [
        {
            "field": r.field,
            "content": r.content,
            "date_created": r.date_created.isoformat() if r.date_created else None,
        }
        for r in rows
    ]


# ---- settings: LLM provider + API key -------------------------------------

class SettingsUpdate(BaseModel):
    llm_provider: str
    gemini_api_key: Optional[str] = None
    ollama_model: Optional[str] = None
    ollama_base_url: Optional[str] = None


@app.get("/api/settings")
def get_settings():
    return {
        "llm_provider": config.LLM_PROVIDER,
        "ollama_model": config.OLLAMA_MODEL,
        "ollama_base_url": config.OLLAMA_BASE_URL,
        # Never echo the real key back to the frontend -- just whether one is set.
        "gemini_api_key_set": bool(config.GEMINI_API_KEY),
    }


@app.post("/api/settings")
def update_settings(body: SettingsUpdate):
    if body.llm_provider not in ("ollama", "gemini"):
        raise HTTPException(400, "llm_provider must be 'ollama' or 'gemini'")

    updates = {"LLM_PROVIDER": body.llm_provider}
    if body.ollama_model:
        updates["OLLAMA_MODEL"] = body.ollama_model
    if body.ollama_base_url:
        updates["OLLAMA_BASE_URL"] = body.ollama_base_url
    if body.gemini_api_key:
        updates["GEMINI_API_KEY"] = body.gemini_api_key

    if body.llm_provider == "gemini" and not body.gemini_api_key and not config.GEMINI_API_KEY:
        raise HTTPException(400, "GEMINI_API_KEY is required the first time you switch to gemini")

    env_store.update_env_file(updates)

    # Apply immediately to this running process (so GET /api/settings reflects
    # the change right away). The scheduler runs as a separate OS process and
    # only picks up .env changes on its next restart -- surfaced to the user
    # in the response rather than silently pretending it's already live there.
    for key, value in updates.items():
        os.environ[key] = value
    importlib.reload(config)

    return {
        "saved": True,
        "restart_required_for": ["scheduler"],
        **get_settings(),
    }


# ---- CV upload --------------------------------------------------------------

def _cv_status_payload() -> dict:
    cv_path = cv_parser.find_default_cv(str(CV_DIR))
    if not cv_path:
        return {"has_cv": False}

    path = Path(cv_path)
    stat = path.stat()
    payload = {
        "has_cv": True,
        "filename": path.name,
        "size_bytes": stat.st_size,
        "modified": stat.st_mtime,
    }
    try:
        text = cv_parser.parse_cv(cv_path)
        payload["preview"] = text.strip()[:400]
        payload["char_count"] = len(text)
    except Exception as exc:  # noqa: BLE001 -- surface any parse failure to the UI, don't crash the endpoint
        payload["parse_error"] = str(exc)
    return payload


@app.get("/api/cv/status")
def cv_status():
    return _cv_status_payload()


@app.post("/api/cv/upload")
async def cv_upload(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in CV_ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type '{suffix}' -- upload a .pdf or .docx")

    CV_DIR.mkdir(exist_ok=True)

    # Only one active CV at a time -- clear any previous current_cv.* first
    # so daily_run.py's auto-detection never picks up a stale file.
    for existing in CV_DIR.glob("current_cv.*"):
        existing.unlink()

    dest = CV_DIR / f"current_cv{suffix}"
    contents = await file.read()
    dest.write_bytes(contents)

    return _cv_status_payload()
