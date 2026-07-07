"""
Natural-language `when` parser → APScheduler trigger kwargs.

Supports:
  One-shot  : "tomorrow 9am", "next Friday at 3pm", "in 30 minutes",
              "2026-05-10T09:00:00+08:00"
  Recurring : "every day at 9am", "every Monday at 8am",
              "every weekday at 9am", "every weekend at 10am",
              "every hour", "every 30 minutes", "every 2 hours",
              "daily at 6pm", "weekly on Friday at 5pm"
  Raw cron  : "0 9 * * 1"  (minute hour dom month dow)
"""

import re
from datetime import datetime, timezone
from typing import Any

import dateparser
import pytz

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DAY_MAP = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed",
    "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun",
    "mon": "mon", "tue": "tue", "wed": "wed",
    "thu": "thu", "fri": "fri", "sat": "sat", "sun": "sun",
}

_CRON_RE = re.compile(
    r"^([\d\*\/\-\,]+)\s+([\d\*\/\-\,]+)\s+([\d\*\/\-\,]+)\s+([\d\*\/\-\,]+)\s+([\d\*\/\-\,]+)$"
)


def _extract_time(text: str, tz: str) -> tuple[int, int]:
    """Parse a time expression like '9am', '14:30', 'noon' → (hour, minute)."""
    settings = {
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "TIMEZONE": tz,
    }
    dt = dateparser.parse(text, settings=settings)
    if dt:
        local = dt.astimezone(pytz.timezone(tz))
        return local.hour, local.minute
    raise ValueError(f"Cannot parse time from: '{text}'")


def _interval_kwargs(text: str) -> dict[str, Any] | None:
    """'every 30 minutes' → {'minutes': 30}, 'every 2 hours' → {'hours': 2}"""
    m = re.search(r"every\s+(\d+)\s+(minute|minutes|min|hour|hours|hr)", text, re.I)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit in ("minute", "minutes", "min"):
            return {"minutes": n}
        return {"hours": n}
    if re.search(r"every\s+hour\b", text, re.I):
        return {"hours": 1}
    if re.search(r"every\s+minute\b", text, re.I):
        return {"minutes": 1}
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_when(when: str, tz: str = "Asia/Kuala_Lumpur") -> dict[str, Any]:
    """
    Parse a `when` string and return a dict with keys:
        trigger_type   : 'date' | 'cron' | 'interval'
        trigger_kwargs : kwargs passed to scheduler.add_job(trigger=..., **trigger_kwargs)
        description    : human-readable summary
    Raises ValueError if parsing fails.
    """
    raw = when.strip()
    lower = raw.lower()

    # ── 1. Raw cron expression ────────────────────────────────────────────
    m = _CRON_RE.match(raw.strip())
    if m:
        mn, hr, dom, mo, dow = m.groups()
        return {
            "trigger_type": "cron",
            "trigger_kwargs": {
                "minute": mn, "hour": hr,
                "day": dom, "month": mo, "day_of_week": dow,
                "timezone": tz,
            },
            "description": f"cron: {raw}",
        }

    # ── 2. Interval: "every N minutes/hours" ─────────────────────────────
    iv = _interval_kwargs(lower)
    if iv:
        unit, n = next(iter(iv.items()))
        label = f"every {n} {unit}"
        return {
            "trigger_type": "interval",
            "trigger_kwargs": iv,
            "description": label,
        }

    # ── 3. Recurring "every [day] at [time]" or "daily/weekly ..." ───────
    is_recurring = bool(
        re.search(r"\bevery\b", lower) or
        re.search(r"\bdaily\b", lower) or
        re.search(r"\bweekly\b", lower)
    )

    if is_recurring:
        # Extract time component: look for "at X" or a time-like token
        time_match = re.search(
            r"\bat\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d{1,2}:\d{2})\b", lower
        ) or re.search(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", lower)

        hour, minute = 9, 0  # sensible default
        if time_match:
            try:
                hour, minute = _extract_time(time_match.group(1), tz)
            except ValueError:
                pass

        # Which days?
        # weekday / weekdays
        if re.search(r"\bweekday(s)?\b", lower):
            dow = "mon-fri"
            desc_day = "weekdays"
        # weekend / weekends
        elif re.search(r"\bweekend(s)?\b", lower):
            dow = "sat,sun"
            desc_day = "weekends"
        # every day / daily
        elif re.search(r"\b(every day|daily)\b", lower):
            dow = "*"
            desc_day = "daily"
        # named day: "every Monday", "weekly on Friday"
        else:
            day_match = None
            for day_name in _DAY_MAP:
                if re.search(rf"\b{day_name}\b", lower):
                    day_match = day_name
                    break
            if day_match:
                dow = _DAY_MAP[day_match]
                desc_day = day_match.capitalize()
            else:
                # No day found → assume daily
                dow = "*"
                desc_day = "daily"

        time_str = f"{hour:02d}:{minute:02d}"
        am_pm = "am" if hour < 12 else "pm"
        disp_hour = hour % 12 or 12
        disp_time = f"{disp_hour}:{minute:02d}{am_pm}"

        return {
            "trigger_type": "cron",
            "trigger_kwargs": {
                "day_of_week": dow,
                "hour": hour,
                "minute": minute,
                "timezone": tz,
            },
            "description": f"every {desc_day} at {disp_time}",
        }

    # ── 4. One-shot: dateparser ───────────────────────────────────────────
    settings = {
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "TIMEZONE": tz,
    }
    dt = dateparser.parse(raw, settings=settings)
    if dt:
        # Ensure tz-aware in local tz
        local_tz = pytz.timezone(tz)
        if dt.tzinfo is None:
            dt = local_tz.localize(dt)
        else:
            dt = dt.astimezone(local_tz)

        if dt <= datetime.now(timezone.utc).astimezone(local_tz):
            raise ValueError(
                f"Parsed time '{dt.strftime('%Y-%m-%d %H:%M %Z')}' is in the past. "
                "Please specify a future time."
            )

        return {
            "trigger_type": "date",
            "trigger_kwargs": {"run_date": dt},
            "description": dt.strftime("%Y-%m-%d %H:%M %Z"),
        }

    raise ValueError(
        f"Could not parse '{when}'. Try formats like: "
        "'tomorrow 9am', 'next Friday at 3pm', 'in 30 minutes', "
        "'every day at 9am', 'every Monday at 8am', 'every 30 minutes', "
        "or a cron expression like '0 9 * * 1'."
    )
