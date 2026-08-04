"""
Read-only dashboard API. Serves data from the same SQLite DB the agents
write to (models.py/db.py) — the frontend is a thin view over it, not a
separate system. Run with: uvicorn api:app --reload --port 8000
"""
import json
from collections import Counter

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import config
from db import get_session, init_db
from models import Job, Application, SkillGap, NewsDigest

app = FastAPI(title="Job Application Agent API")

# Tighten allow_origins to your deployed frontend's exact origin before going live.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

init_db()


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
