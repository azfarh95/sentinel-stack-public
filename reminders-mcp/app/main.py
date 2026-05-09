"""
reminders-mcp  —  Autonomous reminder scheduler with Telegram delivery.

MCP Tools:
  add_reminder      Schedule a one-shot or recurring reminder.
  list_reminders    Show active reminders (optionally filter by chat_id).
  cancel_reminder   Cancel a reminder by ID.
  update_reminder   Change the message or schedule of an existing reminder.
  snooze_reminder   Delay the next fire by a given duration.
  reminder_info     Get full details of a single reminder.
"""

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import database as db
from . import scheduler as sched
from .parser import parse_when

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SCHEDULER_TIMEZONE = os.environ.get("SCHEDULER_TIMEZONE", "Asia/Kuala_Lumpur")


async def _purge_job():
    deleted = db.purge_old(days=30)
    if deleted:
        logger.info("Purged %d old reminders (>30 days completed/cancelled)", deleted)


@asynccontextmanager
async def _lifespan(server: FastMCP):
    db.init_db()
    started = False
    if not sched.scheduler.running:
        sched.scheduler.start()
        logger.info("APScheduler started (tz=%s)", SCHEDULER_TIMEZONE)
        started = True
        # Register daily cleanup at 04:00 local. id is fixed so re-runs just replace it.
        sched.scheduler.add_job(
            _purge_job, trigger="cron", hour=4, minute=0,
            id="__sentinel_purge_old_reminders__", replace_existing=True,
        )
        # Run once on startup to clean up immediately if the container has been off
        try:
            await _purge_job()
        except Exception as e:
            logger.warning("Initial purge failed: %s", e)
    yield
    if started and sched.scheduler.running:
        sched.scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")


mcp = FastMCP(
    "Reminders",
    lifespan=_lifespan,
    instructions=(
        "Schedule reminders and recurring tasks that fire autonomously and deliver "
        "via Telegram. "
        "IMPORTANT: Always pass the chat_id from the current Telegram message so the "
        "reminder is delivered to the right chat. "
        "Typical flow: call add_reminder(chat_id, message, when) — 'when' accepts "
        "natural language like 'tomorrow 9am', 'every Monday at 8am', 'in 30 minutes', "
        "or cron expressions like '0 9 * * 1'. "
        "Use list_reminders(chat_id) to show the user their active reminders. "
        "Use cancel_reminder(id) to delete one."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*", "localhost:*", "[::1]:*",
            "host.docker.internal:*", "reminders-mcp:*",
        ],
        allowed_origins=[
            "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
            "http://host.docker.internal:*", "http://reminders-mcp:*",
        ],
    ),
)


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def add_reminder(
    chat_id: str,
    message: str,
    when: str,
    label: str = None,
    recipients: list[str] = None,
) -> dict:
    """
    Schedule a reminder that fires automatically and sends a Telegram message.

    chat_id    : Telegram chat ID from the current message (user or group).
    message    : The reminder text to send when it fires.
    when       : When to fire. Accepts natural language:
                   One-shot  : 'tomorrow 9am', 'next Friday at 3pm', 'in 30 minutes',
                               '2026-05-10T09:00:00'
                   Recurring : 'every day at 9am', 'every Monday at 8am',
                               'every weekday at 9am', 'every hour', 'every 30 minutes'
                   Cron      : '0 9 * * 1'  (standard 5-field cron, minute first)
    label      : Optional short name for the reminder (e.g. 'standup', 'meds').
    recipients : Optional list of additional Telegram chat IDs to notify when the
                 reminder fires. Must be numeric chat IDs (e.g. ["123456789"]).
                 The primary chat_id always receives the reminder; entries here are
                 extra recipients (duplicates of chat_id are ignored).
    """
    try:
        parsed = parse_when(when, tz=SCHEDULER_TIMEZONE)
    except ValueError as e:
        return {"error": str(e)}

    reminder_id = str(uuid.uuid4())[:8]
    recipients_json = json.dumps(recipients) if recipients else None

    next_run = sched.add_job(
        reminder_id=reminder_id,
        chat_id=chat_id,
        message=message,
        trigger_type=parsed["trigger_type"],
        trigger_kwargs=parsed["trigger_kwargs"],
        recipients=recipients,
    )

    reminder = db.create_reminder(
        reminder_id=reminder_id,
        chat_id=chat_id,
        message=message,
        label=label,
        trigger_type=parsed["trigger_type"],
        trigger_description=parsed["description"],
        when_raw=when,
        next_run=next_run,
        recipients=recipients_json,
    )

    return {
        "ok": True,
        "reminder_id": reminder_id,
        "schedule": parsed["description"],
        "next_run": next_run,
        "recipients": recipients or [],
        "message": f"Reminder set: '{message}' — {parsed['description']}",
    }


@mcp.tool()
async def list_reminders(chat_id: str = None, include_completed: bool = False) -> list:
    """
    List reminders.

    chat_id           : Filter to a specific Telegram chat. Omit to see all.
    include_completed : Include past one-shot reminders that have already fired.
    """
    active = db.list_reminders(chat_id=chat_id, status="active")

    # Refresh next_run from live scheduler
    for r in active:
        live_next = sched.get_next_run(r["id"])
        if live_next and live_next != r.get("next_run"):
            db.update_reminder(r["id"], next_run=live_next)
            r["next_run"] = live_next

    if include_completed:
        completed = db.list_reminders(chat_id=chat_id, status="completed")
        return active + completed

    return active


@mcp.tool()
async def cancel_reminder(reminder_id: str) -> dict:
    """
    Cancel a reminder. The reminder will no longer fire.

    reminder_id : The ID returned by add_reminder.
    """
    reminder = db.get_reminder(reminder_id)
    if not reminder:
        return {"error": f"No reminder found with id '{reminder_id}'"}

    sched.remove_job(reminder_id)
    db.mark_cancelled(reminder_id)

    return {
        "ok": True,
        "reminder_id": reminder_id,
        "message": f"Reminder '{reminder_id}' cancelled.",
    }


@mcp.tool()
async def update_reminder(
    reminder_id: str,
    message: str = None,
    when: str = None,
) -> dict:
    """
    Update the message or schedule of an existing reminder.
    Only the fields you provide are changed.

    reminder_id : The ID returned by add_reminder.
    message     : New reminder text (optional).
    when        : New schedule in the same format as add_reminder (optional).
    """
    reminder = db.get_reminder(reminder_id)
    if not reminder:
        return {"error": f"No reminder found with id '{reminder_id}'"}
    if reminder["status"] != "active":
        return {"error": f"Reminder '{reminder_id}' is {reminder['status']} — cannot update."}

    updates: dict = {}
    next_run = reminder.get("next_run")

    if message:
        updates["message"] = message

    if when:
        try:
            parsed = parse_when(when, tz=SCHEDULER_TIMEZONE)
        except ValueError as e:
            return {"error": str(e)}

        new_msg = message or reminder["message"]
        existing_recipients = json.loads(reminder["recipients"]) if reminder.get("recipients") else None
        next_run = sched.reschedule_job(
            reminder_id=reminder_id,
            trigger_type=parsed["trigger_type"],
            trigger_kwargs=parsed["trigger_kwargs"],
            chat_id=reminder["chat_id"],
            message=new_msg,
            recipients=existing_recipients,
        )
        updates.update({
            "trigger_type": parsed["trigger_type"],
            "trigger_description": parsed["description"],
            "when_raw": when,
            "next_run": next_run,
        })

    db.update_reminder(reminder_id, **updates)
    updated = db.get_reminder(reminder_id)

    return {
        "ok": True,
        "reminder": updated,
        "next_run": next_run,
    }


@mcp.tool()
async def snooze_reminder(reminder_id: str, duration: str) -> dict:
    """
    Delay the next fire of a reminder by a given duration.
    Only works for active one-shot or recurring reminders.

    reminder_id : The ID returned by add_reminder.
    duration    : How long to snooze, e.g. '1 hour', '30 minutes', '2 hours'.
    """
    reminder = db.get_reminder(reminder_id)
    if not reminder:
        return {"error": f"No reminder found with id '{reminder_id}'"}
    if reminder["status"] != "active":
        return {"error": f"Reminder '{reminder_id}' is {reminder['status']} — cannot snooze."}

    # Parse duration
    dur_lower = duration.lower().strip()
    m_min = __import__("re").search(r"(\d+)\s*(minute|min)", dur_lower)
    m_hr = __import__("re").search(r"(\d+)\s*(hour|hr)", dur_lower)

    delta = timedelta()
    if m_hr:
        delta += timedelta(hours=int(m_hr.group(1)))
    if m_min:
        delta += timedelta(minutes=int(m_min.group(1)))

    if not delta:
        return {"error": f"Cannot parse duration '{duration}'. Try '1 hour' or '30 minutes'."}

    new_time = datetime.now(timezone.utc) + delta

    # Reschedule as one-shot at new_time (overrides the recurring schedule temporarily)
    existing_recipients = json.loads(reminder["recipients"]) if reminder.get("recipients") else None
    sched.remove_job(reminder_id)
    next_run = sched.add_job(
        reminder_id=reminder_id,
        chat_id=reminder["chat_id"],
        message=reminder["message"],
        trigger_type="date",
        trigger_kwargs={"run_date": new_time},
        recipients=existing_recipients,
    )
    db.update_reminder(reminder_id, next_run=next_run)

    return {
        "ok": True,
        "reminder_id": reminder_id,
        "snoozed_until": next_run,
        "message": f"Reminder snoozed until {new_time.strftime('%Y-%m-%d %H:%M UTC')}.",
    }


@mcp.tool()
async def reminder_info(reminder_id: str) -> dict:
    """Get full details of a single reminder by ID."""
    reminder = db.get_reminder(reminder_id)
    if not reminder:
        return {"error": f"No reminder found with id '{reminder_id}'"}

    # Refresh next_run from live scheduler
    live_next = sched.get_next_run(reminder_id)
    if live_next:
        reminder["next_run"] = live_next

    return reminder


# ── REST endpoints ─────────────────────────────────────────────────────────────

async def _health(request: Request) -> JSONResponse:
    job_count = len(sched.scheduler.get_jobs())
    return JSONResponse({
        "status": "ok",
        "service": "reminders-mcp",
        "active_jobs": job_count,
        "timezone": SCHEDULER_TIMEZONE,
    })


app = mcp.streamable_http_app()
app.router.routes.insert(0, Route("/health", _health, methods=["GET"]))
