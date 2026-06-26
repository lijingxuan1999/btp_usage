"""
APScheduler job definitions for BTP Usage Agent.

Two recurring jobs:
  daily_report_job   — runs once per day (SCHEDULER_REPORT_HOUR UTC, default 6am)
                       fetches yesterday's usage and emails an HTML report
  anomaly_check_job  — runs every 4 hours
                       detects AI Core CU anomalies and emails an alert if found

Usage (from main.py):
    from scheduler import build_scheduler
    scheduler = build_scheduler()
    scheduler.start()
"""

import asyncio
import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# Track anomaly dates already alerted today to avoid duplicate emails.
# Resets when the process restarts (daily in practice).
_alerted_anomaly_dates: set[str] = set()


def _run_async(coro) -> None:
    """Run an async coroutine from a sync APScheduler job."""
    try:
        asyncio.get_event_loop().run_until_complete(coro)
    except RuntimeError:
        # No event loop in this thread — create a new one
        asyncio.run(coro)


def _daily_report_job() -> None:
    """Sync wrapper for APScheduler — sends daily HTML usage report."""
    logger.info("Scheduler: running daily_report_job")
    from email_tool import send_daily_report_email
    _run_async(send_daily_report_email())


def _anomaly_check_job() -> None:
    """Sync wrapper for APScheduler — detects anomalies and sends alert if found."""
    import json
    logger.info("Scheduler: running anomaly_check_job")

    from uas_tool import detect_aicore_cu_anomaly
    from email_tool import send_anomaly_alert_email

    async def _check():
        result_raw = await detect_aicore_cu_anomaly.ainvoke(
            {"lookback_days": 30, "sensitivity": "medium"}
        )
        result = json.loads(result_raw)

        total_anomalies = result.get("total_daily_anomalies", [])
        per_model       = result.get("per_model_anomalies", {})

        # Collect all new anomaly dates not yet alerted
        new_dates: set[str] = set()
        for a in total_anomalies:
            if a["date"] not in _alerted_anomaly_dates:
                new_dates.add(a["date"])
        for anoms in per_model.values():
            for a in anoms:
                if a["date"] not in _alerted_anomaly_dates:
                    new_dates.add(a["date"])

        if new_dates:
            logger.info("Anomaly check: found new anomalies on dates %s — sending alert", new_dates)
            await send_anomaly_alert_email(result)
            _alerted_anomaly_dates.update(new_dates)
        else:
            logger.info("Anomaly check: no new anomalies found")

    _run_async(_check())


def build_scheduler() -> BackgroundScheduler:
    """Build and configure the APScheduler BackgroundScheduler."""
    report_hour = int(os.environ.get("SCHEDULER_REPORT_HOUR", "6"))

    scheduler = BackgroundScheduler(timezone="UTC")

    scheduler.add_job(
        _daily_report_job,
        trigger=CronTrigger(hour=report_hour, minute=0, timezone="UTC"),
        id="daily_report",
        name="Daily BTP Usage Report Email",
        replace_existing=True,
        misfire_grace_time=3600,  # run up to 1h late if server was down
    )

    scheduler.add_job(
        _anomaly_check_job,
        trigger=CronTrigger(hour="*/4", minute=15, timezone="UTC"),
        id="anomaly_check",
        name="AI Core CU Anomaly Check",
        replace_existing=True,
        misfire_grace_time=600,
    )

    logger.info(
        "Scheduler configured: daily report at %02d:00 UTC, anomaly check every 4h",
        report_hour,
    )
    return scheduler
