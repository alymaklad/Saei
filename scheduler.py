"""
Automatic triggers. Run this once (`python scheduler.py`) on any always-on
host (a free-tier VM, a small VPS, or a cron-triggered GitHub Action) and it
fires the three daily/weekly jobs forever without manual intervention.
"""
from apscheduler.schedulers.blocking import BlockingScheduler

from jobs.daily_run import run_daily_search_and_apply
from jobs.daily_report import run_daily_report
from jobs.weekly_news import run_weekly_news_digest

scheduler = BlockingScheduler()
scheduler.add_job(run_daily_search_and_apply, "cron", hour=8, id="daily_search_and_apply")
scheduler.add_job(run_daily_report, "cron", hour=20, id="daily_report")
scheduler.add_job(run_weekly_news_digest, "cron", day_of_week="mon", hour=9, id="weekly_news")

if __name__ == "__main__":
    print("Scheduler started. Jobs: daily search+apply @08:00, daily report @20:00, weekly news Mon @09:00")
    scheduler.start()
