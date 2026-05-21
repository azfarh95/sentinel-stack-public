"""Cash Forecast — projects POSB balance over the next 90 days from
recurring income/expense schedule in /finance/recurring.yaml.

The schedule is the single source of truth. Mini App `/cash_forecast`
shows a daily projection + an Add Recurring form that appends to the YAML.
"""
import os
import re
import logging
from datetime import date, timedelta, datetime
from pathlib import Path

import httpx
import yaml

logger = logging.getLogger(__name__)

RECURRING_PATH = Path("/finance/recurring.yaml")
FIREFLY_URL = os.environ.get("FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180")


def load_recurring() -> dict:
    try:
        return yaml.safe_load(RECURRING_PATH.read_text())
    except FileNotFoundError:
        return {"income": [], "expense": []}


def save_recurring(data: dict):
    # Round-trip via yaml dump to maintain shape
    RECURRING_PATH.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))


async def _posb_balance() -> float:
    pat = os.environ.get("FIREFLY_PAT", "")
    if not pat:
        return 0.0
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{FIREFLY_URL}/api/v1/accounts/1",
                        headers={"Authorization": f"Bearer {pat}", "Accept": "application/json"})
        return float(r.json()["data"]["attributes"]["current_balance"])


HENDERSON_DEFAULT_PAY_PER_SHIFT = 360.00  # SGD; matches observed YourAgency POSB credits


async def _youragency_deployments_in_range(start: date, end: date) -> list[dict]:
    """Scan Google Calendar for YourAgency Security deployments.

    Treat each as guaranteed income (per user 2026-05-13 — "all youragency
    deployments in my calendar means guaranteed deployment even if pending").
    Pay rate falls back to HENDERSON_DEFAULT_PAY_PER_SHIFT if not in event.
    """
    token_path = Path("/google-workspace-mcp/data/token.json")
    if not token_path.exists():
        logger.info("YourAgency calendar scan skipped: token.json not mounted at %s", token_path)
        return []
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_authorized_user_file(
            str(token_path), ["https://www.googleapis.com/auth/calendar"])
        svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        events = []
        # Search primary calendar for youragency + ntuc deployment keywords
        for kw in ("youragency", "NTUC LLH", "deployment", "CCTC"):
            r = svc.events().list(
                calendarId="primary", q=kw, singleEvents=True,
                timeMin=f"{start.isoformat()}T00:00:00Z",
                timeMax=f"{end.isoformat()}T23:59:59Z",
                maxResults=50,
            ).execute()
            for ev in r.get("items", []):
                title = ev.get("summary", "")
                start_str = (ev.get("start", {}).get("date")
                             or ev.get("start", {}).get("dateTime", "")[:10])
                if not start_str:
                    continue
                # Try to extract amount from title (e.g. "YourAgency shift $360")
                import re as _re
                m = _re.search(r"\$([0-9]+(?:\.[0-9]{2})?)", title)
                amount = float(m.group(1)) if m else HENDERSON_DEFAULT_PAY_PER_SHIFT
                events.append({
                    "date": start_str,
                    "name": f"YourAgency deployment — {title[:40]}",
                    "amount_signed": amount,
                    "category": "Salary (YourAgency)",
                    "_event_id": ev.get("id"),  # for dedup
                })
        # Dedup by event_id (same event might match multiple keywords)
        seen = set()
        uniq = []
        for e in events:
            if e["_event_id"] in seen:
                continue
            seen.add(e["_event_id"])
            uniq.append(e)
        return uniq
    except Exception:
        logger.exception("YourAgency calendar scan failed")
        return []


def _events_in_range(start: date, end: date, items: list, sign: int) -> list[dict]:
    """For each enabled recurring item, emit one event per occurrence in [start, end]."""
    events = []
    for item in items:
        if not item.get("enabled", True):
            continue
        d = max(1, min(28, int(item.get("day", 1))))  # clamp to safe day-of-month
        cursor = date(start.year, start.month, 1)
        while cursor <= end:
            try:
                occur = date(cursor.year, cursor.month, d)
            except ValueError:
                continue
            if start <= occur <= end:
                events.append({
                    "date": occur.isoformat(),
                    "name": item["name"],
                    "amount_signed": sign * float(item["amount"]),
                    "category": item.get("category", ""),
                })
            # advance to next month
            if cursor.month == 12:
                cursor = date(cursor.year + 1, 1, 1)
            else:
                cursor = date(cursor.year, cursor.month + 1, 1)
    return events


async def build_forecast(horizon_days: int = 90) -> dict:
    today = date.today()
    end = today + timedelta(days=horizon_days)
    schedule = load_recurring()

    inflow_events = _events_in_range(today, end, schedule.get("income", []), sign=+1)
    outflow_events = _events_in_range(today, end, schedule.get("expense", []), sign=-1)

    # Pull YourAgency deployments from Google Calendar — treat as guaranteed
    # income even if status is "pending" (per user 2026-05-13).
    youragency_events = await _youragency_deployments_in_range(today, end)
    events = sorted(inflow_events + outflow_events + youragency_events, key=lambda e: e["date"])

    starting_balance = await _posb_balance()
    running = starting_balance
    timeline = []
    min_balance = starting_balance
    min_balance_date = today.isoformat()
    for e in events:
        running += e["amount_signed"]
        timeline.append({
            "date": e["date"],
            "name": e["name"],
            "amount_signed": round(e["amount_signed"], 2),
            "category": e["category"],
            "running_balance": round(running, 2),
        })
        if running < min_balance:
            min_balance = running
            min_balance_date = e["date"]

    monthly_income = sum(float(i["amount"]) for i in schedule.get("income", []) if i.get("enabled", True))
    monthly_expense = sum(float(i["amount"]) for i in schedule.get("expense", []) if i.get("enabled", True))

    return {
        "today": today.isoformat(),
        "horizon_days": horizon_days,
        "horizon_end": end.isoformat(),
        "starting_balance": round(starting_balance, 2),
        "ending_balance": round(running, 2),
        "min_balance": round(min_balance, 2),
        "min_balance_date": min_balance_date,
        "monthly_income": round(monthly_income, 2),
        "monthly_expense": round(monthly_expense, 2),
        "net_monthly": round(monthly_income - monthly_expense, 2),
        "timeline": timeline,
        "income_items": schedule.get("income", []),
        "expense_items": schedule.get("expense", []),
    }


def add_recurring(kind: str, name: str, amount: float, day: int,
                  category: str = "", note: str = "") -> dict:
    """kind: 'income' or 'expense'. Append to YAML."""
    if kind not in ("income", "expense"):
        raise ValueError("kind must be 'income' or 'expense'")
    schedule = load_recurring()
    entry = {
        "name": name.strip(),
        "amount": round(float(amount), 2),
        "day": int(day),
        "enabled": True,
    }
    if category: entry["category"] = category
    if note: entry["note"] = note
    if kind == "income":
        entry["source"] = "POSB"
    schedule.setdefault(kind, []).append(entry)
    save_recurring(schedule)
    return entry


# ── HTML rendering ────────────────────────────────────────────────────────────

_CSS = """
:root { --bg:#1c1c1e; --fg:#f0f0f0; --muted:#8e8e93; --accent:#4cd964; --sep:rgba(255,255,255,0.10); --pos:#4cd964; --neg:#ff3b30; --card:#2c2c2e; }
* { box-sizing: border-box; }
body { margin:0; padding:18px 14px 60px; background:var(--bg); color:var(--fg);
  font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  max-width: 600px; margin-left: auto; margin-right: auto; }
h1 { font-size: 20px; margin: 0 0 4px; display:flex; justify-content:space-between; align-items:center; }
.add-btn { background: var(--accent); color: #000; border: none; padding: 6px 12px;
  border-radius: 8px; font-size: 12px; font-weight: 600; cursor: pointer; text-decoration: none; }
.meta { color: var(--muted); font-size: 11px; margin-bottom: 14px; }
.back { display:inline-block; color:var(--accent); font-size:13px; text-decoration:none; margin-bottom:6px; }
.big { font-size:22px;font-weight:700; margin: 8px 0; }
.summary { background: var(--card); border-radius: 12px; padding: 14px 16px;
  border: 1px solid var(--sep); margin-bottom: 16px; }
.summary-grid { display:grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 8px; }
.summary-cell { font-size: 12px; }
.summary-cell .k { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; }
.summary-cell .v { font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; }
.section-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px;
  color: var(--muted); margin: 14px 4px 6px; font-weight: 600; }
.tl-row { display: grid; grid-template-columns: 60px 1fr 80px 90px; gap: 8px;
  padding: 6px 0; border-bottom: 1px solid var(--sep); font-size: 12px; align-items: baseline; }
.tl-row .d { color: var(--muted); font-variant-numeric: tabular-nums; }
.tl-row .nm { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.tl-row .am { text-align: right; font-variant-numeric: tabular-nums; }
.tl-row .bal { text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); }
.pos { color: var(--pos); }
.neg { color: var(--neg); }
.warn-row { background: rgba(255,59,48,0.06); }
.form-card { background: var(--card); border: 1px solid var(--accent); border-radius: 12px;
  padding: 14px 16px; margin-top: 12px; }
.form-card label { display: block; font-size: 11px; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.5px; margin-top: 8px; margin-bottom: 4px; }
.form-card input, .form-card select { width: 100%; padding: 8px 10px; font-size: 14px;
  background: #1c1c1e; color: var(--fg); border: 1px solid var(--sep); border-radius: 8px;
  letter-spacing: normal; text-align: left; }
.form-card .actions { display:flex; gap: 10px; margin-top: 12px; }
.form-card button { padding: 10px 14px; font-size: 13px; font-weight: 600; border-radius: 8px; border: none; cursor: pointer; }
.flash { background: rgba(76,217,100,0.10); border:1px solid var(--accent); color: var(--accent);
  padding: 10px 12px; border-radius: 8px; margin: 12px 0; font-size: 12px; }
footer { color:var(--muted); font-size:10px; text-align:center; margin-top:24px; }
"""


def _layout(title: str, body: str) -> str:
    return (
        f'<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        f'<title>{title} — Sentinel Finance</title>'
        f'<link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="#1c1c1e">'
        f'<link rel="apple-touch-icon" href="/static/icon-192.png">'
        f'<script src="https://telegram.org/js/telegram-web-app.js"></script>'
        f'<style>{_CSS}</style>'
        f'<script>try{{Telegram.WebApp.ready();Telegram.WebApp.expand();}}catch(e){{}}</script>'
        f'</head><body>{body}</body></html>'
    )


def render_forecast(data: dict, show_form: bool = False, flash: str = "") -> str:
    timeline_html = ""
    min_row_for = data["min_balance_date"]
    for e in data["timeline"]:
        sign_cls = "pos" if e["amount_signed"] > 0 else "neg"
        sign = "+" if e["amount_signed"] > 0 else ""
        warn = " warn-row" if e["running_balance"] < 100 else ""
        timeline_html += (
            f'<div class="tl-row{warn}">'
            f'<span class="d">{e["date"][5:]}</span>'
            f'<span class="nm">{e["name"]}</span>'
            f'<span class="am {sign_cls}">{sign}${abs(e["amount_signed"]):,.2f}</span>'
            f'<span class="bal">${e["running_balance"]:,.2f}</span>'
            '</div>'
        )

    if not data["timeline"]:
        timeline_html = '<p class="meta" style="text-align:center;padding:20px;">No recurring events scheduled</p>'

    net_cls = "pos" if data["net_monthly"] >= 0 else "neg"
    ending_cls = "pos" if data["ending_balance"] >= 0 else "neg"

    form_html = ""
    if show_form:
        form_html = (
            '<div class="form-card">'
            '<form method="post" action="/cash_forecast/add">'
            '<label>Type</label>'
            '<select name="kind"><option value="income">Income</option><option value="expense" selected>Expense</option></select>'
            '<label>Name</label><input type="text" name="name" placeholder="e.g. Netflix subscription" required>'
            '<label>Amount (SGD)</label><input type="text" name="amount" inputmode="decimal" pattern="[0-9.]*" placeholder="e.g. 19.99" required>'
            '<label>Day of month (1-28)</label><input type="number" name="day" min="1" max="28" required>'
            '<label>Category (optional)</label><input type="text" name="category" placeholder="e.g. Subscription, Utilities">'
            '<label>Note (optional)</label><input type="text" name="note" placeholder="Any extra context">'
            '<div class="actions">'
            '<button type="submit" style="background:var(--accent);color:#000;">Add to schedule</button>'
            '<a href="/cash_forecast" style="background:transparent;color:var(--accent);border:1px solid var(--accent);padding:10px 14px;border-radius:8px;font-size:13px;text-decoration:none;">Cancel</a>'
            '</div>'
            '</form></div>'
        )

    flash_html = f'<div class="flash">{flash}</div>' if flash else ""

    body = (
        '<a class="back" href="/">&larr; Home</a>'
        '<h1>Cash Forecast'
        '<a class="add-btn" href="/cash_forecast?add=1">+ Add recurring</a>'
        '</h1>'
        f'<div class="meta">Today {data["today"]} → {data["horizon_end"]} (next {data["horizon_days"]} days)</div>'
        + flash_html
        + form_html
        + '<div class="summary">'
        f'<div class="big">Starting: SGD {data["starting_balance"]:,.2f}</div>'
        '<div class="summary-grid">'
        f'<div class="summary-cell"><div class="k">Monthly income</div><div class="v pos">+${data["monthly_income"]:,.2f}</div></div>'
        f'<div class="summary-cell"><div class="k">Monthly expense</div><div class="v neg">−${data["monthly_expense"]:,.2f}</div></div>'
        f'<div class="summary-cell"><div class="k">Net per month</div><div class="v {net_cls}">${data["net_monthly"]:,.2f}</div></div>'
        f'<div class="summary-cell"><div class="k">Projected ending</div><div class="v {ending_cls}">${data["ending_balance"]:,.2f}</div></div>'
        f'<div class="summary-cell" style="grid-column: span 2;border-top:1px solid var(--sep);padding-top:8px;margin-top:4px;">'
        f'<div class="k">Lowest projected balance</div>'
        f'<div class="v {("neg" if data["min_balance"] < 100 else "")}">${data["min_balance"]:,.2f}</div>'
        f'<div class="k" style="text-transform:none;letter-spacing:0;">on {data["min_balance_date"]}</div></div>'
        '</div></div>'

        '<div class="section-label">Projected timeline</div>'
        f'<div>{timeline_html}</div>'
        '<footer>By Azfar · Powered by Claude · Edit finance/recurring.yaml to bulk-update</footer>'
    )
    return _layout("Cash Forecast", body)
