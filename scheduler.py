"""
Automatic triggers. Run this once (`python scheduler.py`) on any always-on
host (a free-tier VM, a small VPS, or a cron-triggered GitHub Action) and it
fires the three daily/weekly jobs forever without manual intervention.
"""
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler

from jobs.daily_run import run_daily_search_and_apply
from jobs.daily_report import run_daily_report
from jobs.weekly_news import run_weekly_news_digest

scheduler = BlockingScheduler()

# Permanent schedule.
scheduler.add_job(run_daily_search_and_apply, "cron", hour=8, id="daily_search_and_apply")
scheduler.add_job(run_daily_report, "cron", hour=20, id="daily_report")
scheduler.add_job(run_weekly_news_digest, "cron", day_of_week="mon", hour=9, id="weekly_news")

# TEMPORARY -- one-time verification run, added on request to prove the
# automation actually works end-to-end without waiting for the next real
# 8am/8pm/Monday-9am slot. `soon` is computed fresh each time this process
# starts (NOT a fixed clock time), so it's always "1 minute after whichever
# moment you (re)start the app" -- fires once, then this whole block is
# inert until the process restarts again. Delete this block once you've
# confirmed things work; leaving it in means every future restart of the
# scheduler also fires an extra unscheduled run 1 minute later (including a
# real Telegram send from the report jobs, since DRY_RUN=false).
soon = datetime.now() + timedelta(minutes=1)
scheduler.add_job(run_daily_search_and_apply, "date", run_date=soon, id="daily_search_and_apply_test_run")
scheduler.add_job(run_daily_report, "date", run_date=soon, id="daily_report_test_run")
scheduler.add_job(run_weekly_news_digest, "date", run_date=soon, id="weekly_news_test_run")

if __name__ == "__main__":
    print("Scheduler started. Jobs: daily search+apply @08:00, daily report @20:00, weekly news Mon @09:00")
    print(f"One-time test run of all three jobs scheduled for {soon.strftime('%H:%M:%S')} (1 min from now).")
    scheduler.start()
