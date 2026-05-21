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

from . import settings as app_settings

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
    """POSB balance from Gate 5 (the canonical SoT resolver) — NOT raw GL.

    V2.22 fix: prior implementation summed gl.debit - gl.credit on 1111,
    which returned the GL projection (~-$11k due to known drift) instead
    of the statement_cf (~$1,510). The cash forecast was starting from
    the wrong opening, producing nonsense.
    """
    from . import database as db
    from . import account_balance as ab
    db.init_db()
    s = db.SessionLocal()
    try:
        backend = ab.SqliteLedgerBackend(s)
        return float(ab.resolve(backend, "1111").sgd)
    finally:
        s.close()


HENDERSON_KEYWORDS = ("youragency", "ntuc", "llh", "cctc", "deployment", "shift", "duty")
# Per user 2026-05-13: scan ONLY Primary + Bills + Deployments calendars
HENDERSON_CALENDAR_WHITELIST = ("bills", "deployments")


async def _youragency_deployments_in_range(start: date, end: date) -> list[dict]:
    """Scan Primary, Bills, and Deployments Google calendars for YourAgency shifts.

    Per user 2026-05-13:
      * Every event = guaranteed shift (pending or not).
      * Rate per shift from settings.yaml (default $120 net).
      * Events with "pending" in title are scaled by `pending_factor`
        (default 0.5) since the user wants pending counted but acknowledges
        not 100% will materialise.
    """
    rate = app_settings.youragency_rate()
    pending_factor = app_settings.youragency_pending_factor()
    token_path = Path("/google-workspace-mcp/data/token.json")
    if not token_path.exists():
        logger.info("YourAgency calendar scan skipped: token.json not mounted at %s", token_path)
        return []
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        import re as _re
        creds = Credentials.from_authorized_user_file(
            str(token_path), ["https://www.googleapis.com/auth/calendar"])
        svc = build("calendar", "v3", credentials=creds, cache_discovery=False)

        # Whitelist: Primary + Bills + Deployments (per user 2026-05-13)
        targets: list[tuple[str, str]] = []
        try:
            for c in svc.calendarList().list().execute().get("items", []):
                summary = (c.get("summary") or "").lower()
                cid = c["id"]
                if c.get("primary"):
                    targets.append((cid, "primary"))
                elif summary in HENDERSON_CALENDAR_WHITELIST:
                    targets.append((cid, summary))
        except Exception:
            targets = [("primary", "primary")]
        logger.info("YourAgency scan calendars: %s", [t[1] for t in targets])

        seen_ids = set()
        events = []
        for cid, label in targets:
            for kw in HENDERSON_KEYWORDS:
                try:
                    r = svc.events().list(
                        calendarId=cid, q=kw, singleEvents=True,
                        timeMin=f"{start.isoformat()}T00:00:00Z",
                        timeMax=f"{end.isoformat()}T23:59:59Z",
                        maxResults=200,
                    ).execute()
                except Exception:
                    continue
                for ev in r.get("items", []):
                    eid = ev.get("id")
                    if not eid or eid in seen_ids:
                        continue
                    seen_ids.add(eid)
                    title = ev.get("summary", "")
                    start_str = (ev.get("start", {}).get("date")
                                 or ev.get("start", {}).get("dateTime", "")[:10])
                    if not start_str:
                        continue
                    tl = title.lower()
                    # Must mention YourAgency/deployment OR live in the Deployments calendar
                    is_relevant = (
                        "youragency" in tl
                        or "deployment" in tl
                        or label == "deployments"
                    )
                    if not is_relevant:
                        continue
                    m = _re.search(r"\$([0-9]+(?:\.[0-9]{2})?)", title)
                    base = float(m.group(1)) if m else rate
                    pending = "pending" in tl
                    amount = round(base * pending_factor, 2) if pending else base
                    events.append({
                        "date": start_str,
                        "name": f"YourAgency · {title.replace('YourAgency - ', '').replace('YourAgency -', '')[:36]}",
                        "amount_signed": amount,
                        "category": "Salary (YourAgency, pending)" if pending else "Salary (YourAgency)",
                    })
        return events
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

    # Fixed income = enabled YAML entries (excludes YourAgency which is calendar-driven)
    fixed_income_items = [i for i in schedule.get("income", []) if i.get("enabled", True)]
    monthly_fixed_income = sum(float(i["amount"]) for i in fixed_income_items)

    # Variable income = YourAgency events, prorated to monthly over the horizon
    youragency_confirmed = [h for h in youragency_events if "pending" not in h["category"].lower()]
    youragency_pending = [h for h in youragency_events if "pending" in h["category"].lower()]
    youragency_total_90d = sum(h["amount_signed"] for h in youragency_events)
    months = max(1.0, horizon_days / 30.0)
    monthly_variable_income = youragency_total_90d / months

    monthly_income = monthly_fixed_income + monthly_variable_income
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
        "monthly_fixed_income": round(monthly_fixed_income, 2),
        "monthly_variable_income": round(monthly_variable_income, 2),
        "monthly_expense": round(monthly_expense, 2),
        "net_monthly": round(monthly_income - monthly_expense, 2),
        "timeline": timeline,
        "income_items": fixed_income_items,
        "expense_items": [i for i in schedule.get("expense", []) if i.get("enabled", True)],
        "youragency_confirmed": youragency_confirmed,
        "youragency_pending": youragency_pending,
        "youragency_total_90d": round(youragency_total_90d, 2),
        "youragency_rate": app_settings.youragency_rate(),
        "youragency_pending_factor": app_settings.youragency_pending_factor(),
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
.drill-cell { text-decoration: none; color: var(--fg); padding: 4px 6px; margin: -4px -6px; border-radius: 6px; transition: background 0.15s; }
.drill-cell:hover, .drill-cell:active { background: rgba(255,255,255,0.05); }
.breakdown { background: var(--card); border: 1px solid var(--sep); border-radius: 12px; margin-top: 12px; padding: 0; overflow: hidden; }
.breakdown summary { padding: 14px 16px; cursor: pointer; font-size: 13px; user-select: none; }
.breakdown summary:hover { background: rgba(255,255,255,0.03); }
.breakdown[open] summary { border-bottom: 1px solid var(--sep); }
.brk-section-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.6px; color: var(--muted); padding: 10px 16px 4px; font-weight: 600; }
.brk-row { display: grid; grid-template-columns: 1fr auto; gap: 4px 12px; padding: 6px 16px; align-items: baseline; font-size: 12px; border-bottom: 1px solid var(--sep); }
.brk-row > span:first-child { grid-column: 1; }
.brk-row .amt { grid-column: 2; font-variant-numeric: tabular-nums; font-weight: 600; }
.brk-row .brk-sub { grid-column: 1 / span 2; color: var(--muted); font-size: 10px; margin-top: -2px; }
.brk-row.brk-total { font-weight: 700; background: rgba(255,255,255,0.03); }
.brk-row.brk-total .brk-sub { font-weight: normal; }
.brk-row.brk-empty { color: var(--muted); font-style: italic; }
"""


def _layout(title: str, body: str) -> str:
    return (
        f'<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        f'<title>{title} — Sentinel Finance</title>'
        f'<link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="#1c1c1e">'
        f'<link rel="apple-touch-icon" href="/static/icon-192.png">'
        f'<script src="https://telegram.org/js/telegram-web-app.js"></script>'
        f'<link rel="stylesheet" href="/static/privacy.css">'
        f'<style>{_CSS}</style>'
        f'<script src="/static/privacy.js" defer></script>'
        f'</head><body>{body}</body></html>'
    )


def render_forecast(data: dict, show_form: bool = False, flash: str = "") -> str:
    timeline_html = ""
    for e in data["timeline"]:
        sign_cls = "pos" if e["amount_signed"] > 0 else "neg"
        sign = "+" if e["amount_signed"] > 0 else ""
        warn = " warn-row" if e["running_balance"] < 100 else ""
        timeline_html += (
            f'<div class="tl-row{warn}">'
            f'<span class="d">{app_settings.format_date(e["date"])}</span>'
            f'<span class="nm">{e["name"]}</span>'
            f'<span class="am {sign_cls}">{sign}${abs(e["amount_signed"]):,.2f}</span>'
            f'<span class="bal">${e["running_balance"]:,.2f}</span>'
            '</div>'
        )

    if not data["timeline"]:
        timeline_html = '<p class="meta" style="text-align:center;padding:20px;">No recurring events scheduled</p>'

    # ── Income breakdown (Fixed + Variable) ─────────────────────────────────
    fixed_rows = "".join(
        f'<div class="brk-row"><span>{i["name"]}</span>'
        f'<span class="amt pos">+${float(i["amount"]):,.2f}</span>'
        f'<span class="brk-sub">day {int(i.get("day", 1))} · {i.get("category", i.get("source", ""))}</span></div>'
        for i in data.get("income_items", [])
    )
    confirmed = data.get("youragency_confirmed", [])
    pending = data.get("youragency_pending", [])
    rate = data.get("youragency_rate", 120.0)
    factor = data.get("youragency_pending_factor", 0.5)
    confirmed_sum = sum(h["amount_signed"] for h in confirmed)
    pending_sum = sum(h["amount_signed"] for h in pending)
    months = max(1.0, data["horizon_days"] / 30.0)
    var_rows = (
        f'<div class="brk-row"><span>YourAgency · confirmed shifts</span>'
        f'<span class="amt pos">+${confirmed_sum:,.2f}</span>'
        f'<span class="brk-sub">{len(confirmed)} shifts × ${rate:.0f} (90d total)</span></div>'
        f'<div class="brk-row"><span>YourAgency · pending shifts</span>'
        f'<span class="amt pos">+${pending_sum:,.2f}</span>'
        f'<span class="brk-sub">{len(pending)} shifts × ${rate:.0f} × {factor:.0%} factor (90d total)</span></div>'
        f'<div class="brk-row brk-total"><span>YourAgency monthly avg</span>'
        f'<span class="amt pos">+${data.get("monthly_variable_income", 0):,.2f}</span>'
        f'<span class="brk-sub">{confirmed_sum + pending_sum:,.2f} over {months:.1f} months</span></div>'
    )
    income_breakdown_html = (
        '<details id="income-breakdown" class="breakdown">'
        '<summary><b>Income breakdown</b> — fixed + variable</summary>'
        '<div class="brk-section-label">FIXED (recurring.yaml)</div>'
        f'{fixed_rows or "<div class=\"brk-row brk-empty\">— none —</div>"}'
        f'<div class="brk-row brk-total"><span>Total Fixed</span>'
        f'<span class="amt pos">+${data.get("monthly_fixed_income", 0):,.2f}</span>'
        f'<span class="brk-sub">per month</span></div>'
        '<div class="brk-section-label">VARIABLE (calendar-driven)</div>'
        f'{var_rows}'
        '</details>'
    )

    # ── Expense breakdown (grouped by category) ─────────────────────────────
    by_cat: dict = {}
    for i in data.get("expense_items", []):
        cat = i.get("category", "Other")
        by_cat.setdefault(cat, []).append(i)
    exp_sections = ""
    for cat, items in sorted(by_cat.items()):
        subtotal = sum(float(i["amount"]) for i in items)
        rows = "".join(
            f'<div class="brk-row"><span>{i["name"]}</span>'
            f'<span class="amt neg">−${float(i["amount"]):,.2f}</span>'
            f'<span class="brk-sub">day {int(i.get("day", 1))}</span></div>'
            for i in items
        )
        exp_sections += (
            f'<div class="brk-section-label">{cat} — ${subtotal:,.2f}/mo</div>{rows}'
        )
    expense_breakdown_html = (
        '<details id="expense-breakdown" class="breakdown">'
        '<summary><b>Expense breakdown</b> — by category</summary>'
        f'{exp_sections}'
        f'<div class="brk-row brk-total"><span>Total Expenses</span>'
        f'<span class="amt neg">−${data["monthly_expense"]:,.2f}</span>'
        f'<span class="brk-sub">per month</span></div>'
        '</details>'
    )

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
        f'<div class="meta">Today {app_settings.format_date(data["today"])} → {app_settings.format_date(data["horizon_end"])} (next {data["horizon_days"]} days)</div>'
        + flash_html
        + form_html
        + '<div class="summary">'
        f'<div class="big">Starting: SGD {data["starting_balance"]:,.2f}</div>'
        '<div class="summary-grid">'
        f'<a class="summary-cell drill-cell" href="#income-breakdown"><div class="k">Monthly income ▸</div>'
        f'<div class="v pos">+${data["monthly_income"]:,.2f}</div>'
        f'<div class="k" style="text-transform:none;letter-spacing:0;margin-top:2px;">fixed ${data.get("monthly_fixed_income", 0):,.0f} · var ${data.get("monthly_variable_income", 0):,.0f}</div></a>'
        f'<a class="summary-cell drill-cell" href="#expense-breakdown"><div class="k">Monthly expense ▸</div>'
        f'<div class="v neg">−${data["monthly_expense"]:,.2f}</div></a>'
        f'<div class="summary-cell"><div class="k">Net per month</div><div class="v {net_cls}">${data["net_monthly"]:,.2f}</div></div>'
        f'<div class="summary-cell"><div class="k">Projected ending</div><div class="v {ending_cls}">${data["ending_balance"]:,.2f}</div></div>'
        f'<div class="summary-cell" style="grid-column: span 2;border-top:1px solid var(--sep);padding-top:8px;margin-top:4px;">'
        f'<div class="k">Lowest projected balance</div>'
        f'<div class="v {("neg" if data["min_balance"] < 100 else "")}">${data["min_balance"]:,.2f}</div>'
        f'<div class="k" style="text-transform:none;letter-spacing:0;">on {app_settings.format_date(data["min_balance_date"])}</div></div>'
        '</div></div>'

        + income_breakdown_html
        + expense_breakdown_html

        + '<div class="section-label">Projected timeline</div>'
        f'<div>{timeline_html}</div>'
        '<footer>By Azfar · Powered by Claude · Edit finance/recurring.yaml to bulk-update</footer>'
    )
    return _layout("Cash Forecast", body)
