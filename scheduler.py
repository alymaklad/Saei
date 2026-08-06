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

# Permanent schedule.
scheduler.add_job(run_daily_search_and_apply, "cron", hour=8, id="daily_search_and_apply")
scheduler.add_job(run_daily_report, "cron", hour=20, id="daily_report")
scheduler.add_job(run_weekly_news_digest, "cron", day_of_week="mon", hour=9, id="weekly_news")

# The one-time "run everything 1 minute after startup" verification block
# that used to live here is commented out below rather than deleted --
# automation is confirmed working now, and it was also a direct contributor
# to the "database is locked" failures (this test run firing while a manual
# "Search now" click was already mid-batch is exactly the kind of overlap
# that surfaced the bug). Uncomment if you need to re-verify end-to-end
# after a change, but remember it also fires a REAL Telegram send from the
# report jobs (since DRY_RUN=false), not a dry run.
#
# from datetime import datetime, timedelta
# soon = datetime.now() + timedelta(minutes=1)
# scheduler.add_job(run_daily_search_and_apply, "date", run_date=soon, id="daily_search_and_apply_test_run")
# scheduler.add_job(run_daily_report, "date", run_date=soon, id="daily_report_test_run")
# scheduler.add_job(run_weekly_news_digest, "date", run_date=soon, id="weekly_news_test_run")

if __name__ == "__main__":
    print("Scheduler started. Jobs: daily search+apply @08:00, daily report @20:00, weekly news Mon @09:00")
    scheduler.start()
