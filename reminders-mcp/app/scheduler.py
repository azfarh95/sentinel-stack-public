"""
APScheduler setup and reminder fire function.

Uses AsyncIOScheduler with a SQLAlchemy (SQLite) job store so scheduled
jobs survive container restarts.  The jobs table lives in /data/scheduler.db;
reminder metadata lives in /data/reminders.db (managed by database.py).
"""

import logging
import os
from datetime import datetime, timezone, timedelta

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.asyncio import AsyncIOExecutor

from . import database as db

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SCHEDULER_TIMEZONE = os.environ.get("SCHEDULER_TIMEZONE", "Asia/Kuala_Lumpur")

_jobstores = {
    "default": SQLAlchemyJobStore(url="sqlite:////data/scheduler.db")
}
_executors = {
    "default": AsyncIOExecutor()
}

scheduler = AsyncIOScheduler(
    jobstores=_jobstores,
    executors=_executors,
    timezone=SCHEDULER_TIMEZONE,
    job_defaults={
        "misfire_grace_time": 3600,   # fire up to 1h late if container was down
        "coalesce": True,             # collapse multiple misfires into one
    },
)


# ── Fire function (called by APScheduler) ─────────────────────────────────────

async def fire_reminder(
    reminder_id: str,
    chat_id: str,
    message: str,
    trigger_type: str,
    recipients: list[str] | None = None,
) -> None:
    """Send a Telegram message and update the reminder record."""
    logger.info("Firing reminder %s → chat %s", reminder_id, chat_id)

    text = f"⏰ *Reminder*\n\n{message}"
    sent = await _send_telegram(chat_id, text)
    if not sent:
        logger.warning("Telegram send failed for reminder %s → %s", reminder_id, chat_id)

    for target in (recipients or []):
        if target == chat_id:
            continue
        ok = await _send_telegram(target, text)
        if not ok:
            logger.warning("Telegram send failed for reminder %s → %s", reminder_id, target)

    # Update DB
    db.mark_fired(reminder_id)

    # One-shot reminders are done after firing
    if trigger_type == "date":
        db.mark_completed(reminder_id)
        logger.info("Reminder %s completed (one-shot)", reminder_id)


async def _send_telegram(chat_id: str, text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set — cannot send reminder")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
            })
        data = resp.json()
        if not data.get("ok"):
            logger.error("Telegram API error: %s", data)
            return False
        return True
    except Exception as e:
        logger.exception("Error sending Telegram message: %s", e)
        return False


# ── Scheduler helpers (called from main.py tools) ─────────────────────────────

def add_job(reminder_id: str, chat_id: str, message: str,
            trigger_type: str, trigger_kwargs: dict,
            recipients: list[str] | None = None) -> str | None:
    """
    Schedule a new job.  Returns ISO8601 next_run_time or None for interval/cron.
    """
    job = scheduler.add_job(
        fire_reminder,
        trigger=trigger_type,
        id=reminder_id,
        kwargs={
            "reminder_id": reminder_id,
            "chat_id": chat_id,
            "message": message,
            "trigger_type": trigger_type,
            "recipients": recipients or [],
        },
        replace_existing=True,
        **trigger_kwargs,
    )
    nrt = job.next_run_time
    return nrt.isoformat() if nrt else None


def remove_job(reminder_id: str) -> bool:
    try:
        scheduler.remove_job(reminder_id)
        return True
    except Exception:
        return False


def get_next_run(reminder_id: str) -> str | None:
    job = scheduler.get_job(reminder_id)
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


def reschedule_job(reminder_id: str, trigger_type: str,
                   trigger_kwargs: dict, chat_id: str,
                   message: str, recipients: list[str] | None = None) -> str | None:
    remove_job(reminder_id)
    return add_job(reminder_id, chat_id, message, trigger_type, trigger_kwargs, recipients)
