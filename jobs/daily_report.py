"""Evening report: today's applications, sent via free Telegram bot."""
from datetime import datetime, timezone, timedelta

from db import get_session, init_db
from models import Application, Job
from agents.reporter_agent import build_daily_report, send_telegram_report


def run_daily_report():
    init_db()
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    with get_session() as session:
        rows = (
            session.query(Application, Job)
            .join(Job, Application.job_id == Job.id)
            .filter(Application.date_created >= since)
            .all()
        )
        applications_today = [
            {"title": job.title, "company": job.company, "status": application.status}
            for application, job in rows
        ]

    report_text = build_daily_report(applications_today)
    return send_telegram_report(report_text)


if __name__ == "__main__":
    print(run_daily_report())
