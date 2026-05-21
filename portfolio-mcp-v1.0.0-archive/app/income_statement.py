"""IAS 1-flavoured Income Statement for Sentinel Finance.

Pulls income/expense categories from Firefly III for a given period (YTD by
default). Excludes transactions tagged `prior-year:*` so cash that arrived
in the reporting period but was earned the prior year (e.g. Jan CPF for
Dec wages, per CPF Act §7) doesn't pollute the current-year P&L.

Bridge to net worth: closing net worth = opening equity + net income.
"""
import os
import logging
from datetime import datetime, date, timezone, timedelta
from collections import defaultdict
from pathlib import Path

import httpx
import yaml

from . import balance_sheet as bs

logger = logging.getLogger(__name__)

FIREFLY_URL = os.environ.get("FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180")
PRIOR_YEAR_TAG_PREFIX = "prior-year:"


async def _firefly(path: str, params: dict | None = None) -> list | dict:
    pat = os.environ.get("FIREFLY_PAT", "")
    if not pat:
        return []
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{FIREFLY_URL}/api/v1/{path}",
                        headers={"Authorization": f"Bearer {pat}",
                                 "Accept": "application/json"},
                        params=params or {})
        try:
            return r.json()
        except Exception:
            return []


async def _transactions_in_range(start: str, end: str, txn_type: str) -> list:
    """All transactions of a given type in [start, end]. Paginates."""
    pat = os.environ.get("FIREFLY_PAT", "")
    if not pat:
        return []
    out = []
    page = 1
    async with httpx.AsyncClient(timeout=20) as c:
        while True:
            r = await c.get(f"{FIREFLY_URL}/api/v1/transactions",
                            headers={"Authorization": f"Bearer {pat}",
                                     "Accept": "application/json"},
                            params={"start": start, "end": end, "type": txn_type,
                                    "limit": 200, "page": page})
            data = r.json()
            rows = data.get("data", [])
            out.extend(rows)
            meta = data.get("meta", {}).get("pagination", {})
            if page >= int(meta.get("total_pages", 1) or 1):
                break
            page += 1
    return out


def _has_prior_year_tag(tx: dict) -> bool:
    tags = tx.get("tags") or []
    return any(isinstance(t, str) and t.startswith(PRIOR_YEAR_TAG_PREFIX) for t in tags)


async def build_income_statement(year: int | None = None) -> dict:
    today = date.today()
    if year is None:
        year = today.year
    start = f"{year}-01-01"
    # YTD if current year, full year otherwise
    end = today.isoformat() if year == today.year else f"{year}-12-31"

    deposits = await _transactions_in_range(start, end, "deposit")
    withdrawals = await _transactions_in_range(start, end, "withdrawal")

    # Bucket by category, skipping accrual-flagged items
    income = defaultdict(float)
    expenses = defaultdict(float)
    excluded_income = 0.0
    excluded_expense = 0.0

    for t in deposits:
        tx = t["attributes"]["transactions"][0]
        amt = float(tx.get("amount", 0))
        if _has_prior_year_tag(tx):
            excluded_income += amt
            continue
        cat = tx.get("category_name") or "Uncategorised"
        income[cat] += amt

    for t in withdrawals:
        tx = t["attributes"]["transactions"][0]
        amt = float(tx.get("amount", 0))
        if _has_prior_year_tag(tx):
            excluded_expense += amt
            continue
        cat = tx.get("category_name") or "Uncategorised"
        expenses[cat] += amt

    total_income = sum(income.values())
    total_expenses = sum(expenses.values())
    net_income = total_income - total_expenses

    # Bridge to net worth (uses current balance sheet build)
    try:
        bsheet = await bs.build_balance_sheet()
        fx = float(bsheet.get("usd_to_sgd", 1.27))
        closing_nw_sgd = bsheet["net_worth_sgd"]
        opening_equity_sgd = closing_nw_sgd - net_income
        closing_nw_usd = bsheet["net_worth_usd"]
    except Exception as e:
        logger.exception("balance sheet bridge failed")
        fx = 1.27
        closing_nw_sgd = 0.0
        opening_equity_sgd = 0.0
        closing_nw_usd = 0.0

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "year": year,
        "period_start": start,
        "period_end": end,
        "fx_usd_to_sgd": fx,
        "income": [{"name": k, "sgd": round(v, 2), "usd": round(v / fx, 2)}
                   for k, v in sorted(income.items(), key=lambda kv: -kv[1])],
        "expenses": [{"name": k, "sgd": round(v, 2), "usd": round(v / fx, 2)}
                     for k, v in sorted(expenses.items(), key=lambda kv: -kv[1])],
        "totals": {
            "income_sgd": round(total_income, 2),
            "income_usd": round(total_income / fx, 2),
            "expenses_sgd": round(total_expenses, 2),
            "expenses_usd": round(total_expenses / fx, 2),
            "net_income_sgd": round(net_income, 2),
            "net_income_usd": round(net_income / fx, 2),
        },
        "bridge": {
            "opening_equity_sgd": round(opening_equity_sgd, 2),
            "opening_equity_usd": round(opening_equity_sgd / fx, 2),
            "net_income_sgd": round(net_income, 2),
            "closing_net_worth_sgd": round(closing_nw_sgd, 2),
            "closing_net_worth_usd": round(closing_nw_usd, 2),
        },
        "excluded": {
            "income_sgd": round(excluded_income, 2),
            "expense_sgd": round(excluded_expense, 2),
            "reason": "Transactions tagged prior-year:* (CPF accrual fix per CPF Act §7)",
        },
        "txn_counts": {
            "deposits": len(deposits),
            "withdrawals": len(withdrawals),
        },
    }


async def available_years() -> list[int]:
    """Years with at least one transaction (deposit OR withdrawal)."""
    pat = os.environ.get("FIREFLY_PAT", "")
    if not pat:
        return [date.today().year]
    # Cheap approximation: scan basic summary for last 5 years; include those with non-zero earned or spent
    years = set()
    today = date.today()
    for y in range(today.year, today.year - 6, -1):
        end = today.isoformat() if y == today.year else f"{y}-12-31"
        data = await _firefly("summary/basic", {"start": f"{y}-01-01", "end": end})
        if not isinstance(data, dict):
            continue
        earned = float((data.get("earned-in-sgd") or {}).get("value", 0) or 0)
        spent = float((data.get("spent-in-sgd") or {}).get("value", 0) or 0)
        if earned > 0 or spent < 0:
            years.add(y)
    if not years:
        years.add(today.year)
    return sorted(years, reverse=True)


# ── HTML rendering ────────────────────────────────────────────────────────────

_CSS = """
:root { --bg:#1c1c1e; --fg:#f0f0f0; --muted:#8e8e93; --accent:#4cd964; --sep:rgba(255,255,255,0.10); --pos:#4cd964; --neg:#ff3b30; --card:#2c2c2e; }
* { box-sizing: border-box; }
body { margin:0; padding:18px 14px 60px; background:var(--bg); color:var(--fg);
  font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  max-width: 560px; margin-left: auto; margin-right: auto; }
h1 { font-size: 20px; margin: 0 0 4px; }
.meta { color: var(--muted); font-size: 11px; margin-bottom: 14px; }
.back { display:inline-block; color:var(--accent); font-size:13px; text-decoration:none; margin-bottom:6px; }
.period-form { display:flex; gap:8px; align-items:center; margin-bottom: 18px; }
.period-form select { background:#2c2c2e; color:var(--fg); border:1px solid var(--sep);
  border-radius:8px; padding:8px 10px; font-size:14px; }
.section { margin-bottom: 18px; }
.section h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.6px;
  color: var(--muted); margin: 8px 0 6px; font-weight: 600; }
.colhead { display: grid; grid-template-columns: 1fr 90px 90px; gap: 8px;
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.6px; color: var(--muted);
  padding-bottom: 4px; border-bottom: 1px solid var(--sep); margin-bottom: 4px; }
.colhead .amt { text-align: right; }
.row { display: grid; grid-template-columns: 1fr 90px 90px; gap: 8px; padding: 5px 0;
  font-size: 13px; }
.row .amt { font-variant-numeric: tabular-nums; text-align: right; }
.row .amt-usd { color: var(--muted); font-size: 0.92em; }
.subtotal { display: grid; grid-template-columns: 1fr 90px 90px; gap: 8px;
  padding: 8px 0; border-top: 1px solid var(--sep); font-weight: 700; }
.subtotal .amt { font-variant-numeric: tabular-nums; text-align: right; }
.subtotal .amt-usd { color: var(--muted); font-weight: 500; }
.net { display: grid; grid-template-columns: 1fr 90px 90px; gap: 8px;
  padding: 14px 0; margin-top: 8px; font-size: 17px; font-weight: 700;
  border-top: 2px solid var(--accent); border-bottom: 2px solid var(--accent); }
.net.pos { color: var(--pos); }
.net.neg { color: var(--neg); }
.net .amt { font-variant-numeric: tabular-nums; text-align: right; }
.bridge { margin-top: 22px; padding: 16px; background: var(--card); border-radius: 12px;
  border: 1px solid var(--sep); }
.bridge h3 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.6px;
  color: var(--muted); margin: 0 0 8px; font-weight: 600; }
.bridge .row { font-size: 13px; }
.bridge .row.total { font-weight: 700; border-top: 1px solid var(--sep); padding-top: 8px; }
.excluded-note { background: rgba(255,204,0,0.08); border:1px solid rgba(255,204,0,0.25);
  border-radius:8px; padding:10px 12px; font-size:11px; color:#ffcc00; margin-top:14px; }
footer { color:var(--muted); font-size:10px; text-align:center; margin-top:24px; }
"""


def _layout(title: str, body: str) -> str:
    return (
        f'<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        f'<title>{title} — Sentinel Finance</title>'
        f'<link rel="manifest" href="/manifest.webmanifest">'
        f'<meta name="theme-color" content="#1c1c1e">'
        f'<link rel="apple-touch-icon" href="/static/icon-192.png">'
        f'<script src="https://telegram.org/js/telegram-web-app.js"></script>'
        f'<style>{_CSS}</style>'
        f'<script>try{{Telegram.WebApp.ready();Telegram.WebApp.expand();}}catch(e){{}}</script>'
        f'</head><body>{body}</body></html>'
    )


def render_html(data: dict, years_available: list[int]) -> str:
    year = data["year"]
    t = data["totals"]
    b = data["bridge"]

    options = "".join(
        f'<option value="{y}"{" selected" if y == year else ""}>{y}{" YTD" if y == date.today().year else ""}</option>'
        for y in years_available
    )

    period_form = (
        '<form method="get" action="/income_statement" class="period-form">'
        '<label style="font-size:12px;color:var(--muted);">Period:</label>'
        f'<select name="year" onchange="this.form.submit()">{options}</select>'
        '</form>'
    )

    def rows(items):
        return "".join(
            f'<div class="row"><span>{r["name"]}</span>'
            f'<span class="amt amt-usd">${r["usd"]:,.2f}</span>'
            f'<span class="amt">${r["sgd"]:,.2f}</span></div>'
            for r in items
        )

    excluded_html = ""
    if data["excluded"]["income_sgd"] > 0 or data["excluded"]["expense_sgd"] > 0:
        excluded_html = (
            f'<div class="excluded-note">'
            f'<b>Accrual exclusion:</b> ${data["excluded"]["income_sgd"]:,.2f} income + '
            f'${data["excluded"]["expense_sgd"]:,.2f} expense excluded — '
            f'{data["excluded"]["reason"]}'
            '</div>'
        )

    net_cls = "pos" if t["net_income_sgd"] >= 0 else "neg"

    body = (
        '<a class="back" href="/">&larr; Home</a>'
        f'<h1>Income Statement — {year}{" YTD" if year == date.today().year else ""}</h1>'
        f'<div class="meta">Period {data["period_start"]} → {data["period_end"]} · base SGD · USD@{data["fx_usd_to_sgd"]}</div>'
        + period_form
        + '<div class="colhead"><span>&nbsp;</span><span class="amt">USD</span><span class="amt">SGD</span></div>'

        + '<div class="section"><h2>Income</h2>'
        + (rows(data["income"]) or '<div class="row" style="color:var(--muted);"><span>No income recorded</span><span></span><span></span></div>')
        + f'<div class="subtotal"><span>Total Income</span>'
        f'<span class="amt amt-usd">${t["income_usd"]:,.2f}</span>'
        f'<span class="amt">${t["income_sgd"]:,.2f}</span></div>'
        '</div>'

        + '<div class="section"><h2>Expenses</h2>'
        + (rows(data["expenses"]) or '<div class="row" style="color:var(--muted);"><span>No expenses recorded</span><span></span><span></span></div>')
        + f'<div class="subtotal"><span>Total Expenses</span>'
        f'<span class="amt amt-usd">${t["expenses_usd"]:,.2f}</span>'
        f'<span class="amt">${t["expenses_sgd"]:,.2f}</span></div>'
        '</div>'

        + f'<div class="net {net_cls}"><span>Net Income</span>'
        f'<span class="amt amt-usd">${t["net_income_usd"]:,.2f}</span>'
        f'<span class="amt">${t["net_income_sgd"]:,.2f}</span></div>'

        + '<div class="bridge">'
        '<h3>Bridge to Net Worth</h3>'
        f'<div class="row"><span>Opening Equity ({year}-01-01)</span>'
        f'<span class="amt amt-usd">${b["opening_equity_usd"]:,.2f}</span>'
        f'<span class="amt">${b["opening_equity_sgd"]:,.2f}</span></div>'
        f'<div class="row"><span>+ Net Income (this period)</span>'
        f'<span class="amt amt-usd">${t["net_income_usd"]:,.2f}</span>'
        f'<span class="amt">${b["net_income_sgd"]:,.2f}</span></div>'
        f'<div class="row total"><span>= Closing Net Worth ({data["period_end"]})</span>'
        f'<span class="amt amt-usd">${b["closing_net_worth_usd"]:,.2f}</span>'
        f'<span class="amt">${b["closing_net_worth_sgd"]:,.2f}</span></div>'
        '</div>'

        + excluded_html
        + f'<footer>{data["txn_counts"]["deposits"]} deposits · {data["txn_counts"]["withdrawals"]} withdrawals scanned</footer>'
    )
    return _layout(f"Income Statement {year}", body)
