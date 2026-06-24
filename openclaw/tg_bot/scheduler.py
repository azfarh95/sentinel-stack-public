"""In-process recurring-job scheduler for the shared-brain Telegram bot.

Why this lives here and not in OpenClaw's native cron:
  OpenClaw's gateway can *schedule* and *run* an agent turn fine, but it can
  only *deliver* the result through its own channel outbound adapter. Our
  Telegram channel is intentionally `enabled:false` in openclaw.json (a second
  poller on the same bot token → Conflict 409 against this bot), so the
  gateway's telegram outbound adapter is unavailable and every cron
  "announce" delivery fails with `OutboundDeliveryError`. Routing the cron
  result back to the gateway via a loopback webhook is also a dead end — the
  gateway's webhook delivery runs through an SSRF guard that blocks
  127.0.0.1/private with no opt-in on that code path.

  So the only working server→Telegram path is *this* process's own
  `sendMessage`. This module owns the schedule, runs each job's prompt through
  the same serialised `gateway_turnstile` + `openclaw_one_shot` primitives the
  interactive path uses (so a scheduled run can never race an interactive turn
  and trigger the session-takeover error), and delivers via the bot's
  `_deliver`.

Schedules supported per job:
  - {"every_seconds": N}   — fire roughly every N seconds (anchored; no burst
                             on restart).
  - {"daily_at": "HH:MM"}  — fire once per local day at/after HH:MM.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterable

from openclaw.brain_wrapper import extract_reply, gateway_turnstile, openclaw_one_shot
from openclaw.tg_bot.dispatcher import Outbox
from openclaw.tg_bot.mirror import _split_for_telegram

logger = logging.getLogger("openclaw.tg_bot.scheduler")

# Owner DM — jobs deliver here. Mirrors the OpenClaw cron jobs' target.
_OWNER_CHAT_ID = YOUR_TELEGRAM_CHAT_ID

# Per-job agent-turn timeout. A healthy WARM tool-using turn (tool-search +
# fetch + short report) runs in ~40-90s, so this is generous headroom while
# still failing FAST on a wedge (the R3 mid-stream stall that affects these
# tool-heavy turns) so the single retry can fire promptly. NOTE: the real fix
# for the wedge is OpenClaw `diagnostics.stuckSessionAbortMs` (R3) — until that
# lands, keep this well below the interactive 600s so a stall doesn't dead-air.
_JOB_TIMEOUT_S = 240

# How often the loop wakes to check for due jobs.
_TICK_SECONDS = 60

_STATE_PATH = Path.home() / ".sentinel" / "tg_scheduler_state.json"


# ── job catalogue (ported from ~/.openclaw/cron/jobs.json) ───────────────
JOBS: list[dict] = [
    {
        "id": "mrt-status-daily",
        "name": "Daily MRT Status Update",
        "schedule": {"daily_at": "06:30"},
        "prompt": (
            "Search for current Singapore MRT/train service status for today. "
            "Check for any breakdowns, delays, or service adjustments. If "
            "everything is running normally, say so briefly. Include any planned "
            "works or upcoming disruptions. Be concise — this is a morning "
            "commute update."
        ),
    },
    {
        "id": "wolfies-pack-price",
        "name": "Wolfies (PACK) Price Tracker",
        "schedule": {"every_seconds": 21600},  # 6h
        "prompt": (
            "Fetch this exact URL with your web-fetch tool: "
            "https://api.dexscreener.com/latest/dex/tokens/"
            "0x0d0b4a6fc6e7f5635c2ff38de75af2e96d6d6804 — it returns JSON of "
            "every trading pair for the Wolfies (PACK) token on Cronos. From "
            "the `pairs` array, pick the single pair with the highest "
            "`liquidity.usd`. Report EXACTLY three short lines, no preamble, no "
            "caveats, no ranges:\n"
            "Price: $<priceUsd from that pair>\n"
            "24h: <priceChange.h24>%\n"
            "Source: <dexId> <baseToken.symbol>/<quoteToken.symbol>\n"
            "Use only that one DexScreener response — do not blend in other "
            "aggregators or hedge with 'sources vary'."
        ),
    },
]


# ── state persistence ────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("scheduler: could not persist state: %s", exc)


# ── due-time logic ─────────────────────────────────────────────────────────
def _is_due(job: dict, state: dict, now: datetime) -> bool:
    """Pure predicate: should `job` run at `now` given persisted `state`?
    Mutates nothing — the caller records last_run after a successful fire."""
    sched = job["schedule"]
    jstate = state.get(job["id"], {})

    if "every_seconds" in sched:
        last = jstate.get("last_run_epoch")
        if last is None:
            # First sighting: anchor to now, don't fire immediately. Avoids a
            # burst every time the bot restarts.
            return False
        return (now.timestamp() - float(last)) >= sched["every_seconds"]

    if "daily_at" in sched:
        hh, mm = (int(x) for x in sched["daily_at"].split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < target:
            return False
        # Fire once per local calendar day.
        return jstate.get("last_run_date") != now.date().isoformat()

    return False


def _anchor_unseen_jobs(state: dict, now: datetime) -> dict:
    """Give every-N jobs a starting anchor so they fire one interval from
    first boot rather than never (last_run_epoch stays None forever)."""
    changed = False
    for job in JOBS:
        if "every_seconds" in job["schedule"] and "last_run_epoch" not in state.get(job["id"], {}):
            state.setdefault(job["id"], {})["last_run_epoch"] = now.timestamp()
            changed = True
    if changed:
        _save_state(state)
    return state


# ── execution ───────────────────────────────────────────────────────────────
def _prewarm_model(timeout_s: float = 220.0) -> bool:
    """Fire a 1-token completion at the local LLM so Qwen is RESIDENT before a
    job's real turn. A cold 27B load (~3.5min) otherwise consumes the job's
    budget and the turn aborts — the cron cold-wake failure. Best-effort:
    failure just means the job proceeds (possibly cold) and may retry."""
    import urllib.request
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:1234/v1/chat/completions",
            data=json.dumps({
                "model": "qwen/qwen3.6-27b",
                "messages": [{"role": "user", "content": "warm"}],
                "max_tokens": 1,
            }).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=timeout_s).read()
        logger.info("scheduler: model pre-warmed")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("scheduler: prewarm failed (%s) — proceeding cold", exc)
        return False


def _run_job(job: dict, token: str, deliver_fn: Callable[[str, Iterable[Outbox], bool], None],
             dry_run: bool) -> None:
    """Run one job's prompt through the serialised gateway and deliver the
    reply to the owner. Never raises — errors are logged and surfaced to TG."""
    prompt = job["prompt"]
    if "daily_at" in job["schedule"]:
        prompt = f"Current date: {datetime.now():%A, %d %B %Y}.\n\n{prompt}"

    session_id = f"sched-{job['id']}-{uuid.uuid4().hex[:8]}"
    logger.info("scheduler: running job=%s session=%s", job["id"], session_id)

    # Keep Qwen hot so a cold-load doesn't eat the job budget (the cron cold-wake
    # abort). Best-effort, then run the turn with a single retry: a transient
    # cold-window/eviction abort gets a second, re-warmed attempt before we
    # surface a failure to the owner.
    _prewarm_model()
    reply: dict = {"ok": False, "reply": "", "detail": "no reply"}
    for attempt in (1, 2):
        try:
            with gateway_turnstile():
                turn = openclaw_one_shot(
                    session_id=f"{session_id}-{attempt}", message=prompt,
                    timeout_s=_JOB_TIMEOUT_S,
                )
            reply = extract_reply(turn)
        except Exception as exc:  # noqa: BLE001
            logger.exception("scheduler: job=%s crashed (attempt %d)", job["id"], attempt)
            reply = {"ok": False, "reply": "", "detail": str(exc)}
        if reply.get("ok") and (reply.get("reply") or "").strip():
            break
        if attempt == 1:
            det = reply.get("detail") or reply.get("error") or "no reply"
            logger.warning("scheduler: job=%s attempt 1 failed (%s) — re-warm + retry once",
                           job["id"], str(det)[:120])
            _prewarm_model()

    if reply.get("ok") and (reply.get("reply") or "").strip():
        text = reply["reply"].strip()
    else:
        detail = (reply.get("detail") or reply.get("error") or "no reply").strip()
        text = f"[{job['name']}] failed: {detail[:500]}"
        logger.warning("scheduler: job=%s produced no usable reply (%s)", job["id"], detail[:200])

    outboxes = [Outbox(_OWNER_CHAT_ID, chunk) for chunk in _split_for_telegram(text)]
    try:
        deliver_fn(token, outboxes, dry_run)
    except Exception as exc:
        logger.warning("scheduler: deliver failed for job=%s: %s", job["id"], exc)


def _loop(token: str, deliver_fn: Callable[[str, Iterable[Outbox], bool], None], dry_run: bool) -> None:
    logger.info("scheduler starting (%d jobs, tick=%ss, dry_run=%s)", len(JOBS), _TICK_SECONDS, dry_run)
    state = _anchor_unseen_jobs(_load_state(), datetime.now())
    while True:
        try:
            now = datetime.now()
            for job in JOBS:
                if not _is_due(job, state, now):
                    continue
                _run_job(job, token, deliver_fn, dry_run)
                jstate = state.setdefault(job["id"], {})
                jstate["last_run_epoch"] = now.timestamp()
                jstate["last_run_date"] = now.date().isoformat()
                _save_state(state)
        except Exception:
            logger.exception("scheduler: tick crashed (continuing)")
        time.sleep(_TICK_SECONDS)


def start_scheduler(
    token: str,
    deliver_fn: Callable[[str, Iterable[Outbox], bool], None],
    dry_run: bool = False,
) -> threading.Thread:
    """Spawn the scheduler daemon thread (already started)."""
    t = threading.Thread(
        target=_loop, args=(token, deliver_fn, dry_run), daemon=True, name="tg-scheduler",
    )
    t.start()
    return t


def run_job_now(job_id: str, token: str,
                deliver_fn: Callable[[str, Iterable[Outbox], bool], None],
                dry_run: bool = False) -> bool:
    """Fire a single job immediately by id (for manual smoke tests)."""
    for job in JOBS:
        if job["id"] == job_id:
            _run_job(job, token, deliver_fn, dry_run)
            return True
    return False
