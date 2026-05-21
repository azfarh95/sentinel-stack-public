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
from . import classifier as _classifier

logger = logging.getLogger(__name__)

FIREFLY_URL = os.environ.get("FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180")
PRIOR_YEAR_TAG_PREFIX = "prior-year:"

# Account types that belong on the P&L (income statement).
# Everything else is a balance-sheet movement (financing/investing/transfer)
# and is reported separately under "Excluded from P&L".
PNL_ACCOUNT_TYPES: set[str] = {"income", "expense"}

# Categories that lack a classifier.yaml rule but are KNOWN balance-sheet
# movements (e.g. Firefly-direct journal entries from MEPs reconciliation).
# Map to the account_type they would be classified as.
EXCLUDED_CATEGORY_OVERRIDES: dict[str, str] = {
    "Loan drawdown": "financing",          # Cr Liability, Dr Cash
    "Portfolio adjustment": "revaluation", # unrealized P&L on assets
    "Investment Fees": "investing",        # asset-cost-basis adjustment
    "Investment Income": "income",         # P&L (treat as income — dividends/interest from positions)
    "Government Transfer": "income",       # GST voucher, MediSave top-up, etc. — P&L
    "Family expense": "expense",           # explicit owner direction 2026-05-13
    "Uncategorised": "expense",            # surfaces in P&L so user notices
}

# Account_type → P&L exclusion bucket label
_EXCLUSION_BUCKETS = {
    "transfer": "Transfers (between own accounts)",
    "liability": "Financing (debt service / drawdowns)",
    "investment": "Investing (asset purchases / sales)",
    "financing": "Financing (loan drawdowns)",
    "revaluation": "Revaluation (unrealized gains/losses)",
    "investing": "Investing (asset cost basis)",
}


def _category_account_type_map() -> dict[str, str]:
    """Build category_name → account_type from classifier.yaml + overrides.

    First entry wins on conflicts (classifier.yaml is the SoT). The overrides
    only fill gaps for categories Firefly carries that classifier doesn't
    have a rule for.
    """
    out: dict[str, str] = {}
    for v in _classifier._load():
        cat = v.get("category")
        at = v.get("account_type")
        if cat and at and cat not in out:
            out[cat] = at
    for cat, at in EXCLUDED_CATEGORY_OVERRIDES.items():
        out.setdefault(cat, at)
    return out


def _is_pnl_category(category: str, cat_map: dict[str, str]) -> tuple[bool, str]:
    """Return (include_in_pnl, account_type_or_bucket).

    Categories not in either classifier.yaml or overrides default to 'expense'
    so users notice them rather than silently losing the entry.
    """
    at = cat_map.get(category)
    if at is None:
        # Unknown category — default to expense bucket so it surfaces for review
        return True, "expense"
    return at in PNL_ACCOUNT_TYPES, at


# V2.22 cleanup: legacy Firefly helpers removed (used only by the deleted
# _build_income_statement_firefly_legacy below). build_income_statement
# routes exclusively to _build_income_statement_gl which queries the
# Sentinel SQLite GL directly. inv29 enforces no _firefly reads remain.


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    """Return ('YYYY-MM-01', 'YYYY-MM-DD') for the given month."""
    from calendar import monthrange
    last_day = monthrange(year, month)[1]
    today = date.today()
    end_d = date(year, month, last_day)
    # Clamp to today if the month is current
    if year == today.year and month == today.month:
        end_d = today
    return f"{year}-{month:02d}-01", end_d.isoformat()


async def build_income_statement(year: int | None = None,
                                  month: int | None = None) -> dict:
    """Build P&L from the Sentinel SQLite GL (Firefly-decoupled).

    Migrated 2026-05-14 from Firefly III API to direct SQLite GL query.
    Returns the same dict shape as the legacy Firefly version so render_html
    is unchanged.
    """
    return await _build_income_statement_gl(year, month)


async def _build_income_statement_gl(year, month):
    """GL-backed P&L. Queries chart_of_accounts + journals + general_ledger."""
    from sqlalchemy import text
    from . import database as db

    today = date.today()
    if year is None:
        year = today.year
    if month is not None:
        start, end = _month_bounds(year, month)
    else:
        start = f"{year}-01-01"
        end = today.isoformat() if year == today.year else f"{year}-12-31"

    db.init_db()
    s = db.SessionLocal()
    try:
        # Aggregate revenue + expense per CoA leaf for the period
        rows = s.execute(text("""
          SELECT coa.account_code, coa.account_name, coa.account_class,
                 SUM(gl.debit) AS dr, SUM(gl.credit) AS cr
          FROM journals j
          JOIN general_ledger gl ON gl.journal_id = j.id
          JOIN chart_of_accounts coa ON coa.id = gl.account_id
          WHERE j.status = 'posted'
            AND j.journal_date BETWEEN :df AND :dt
            AND coa.is_postable = 1
            AND coa.account_class IN ('REVENUE', 'EXPENSE')
          GROUP BY coa.account_code, coa.account_name, coa.account_class
          ORDER BY coa.account_code
        """), {"df": start, "dt": end}).all()

        income: dict[str, float] = {}
        expenses: dict[str, float] = {}
        for r in rows:
            code, name, klass, dr, cr = r[0], r[1], r[2], float(r[3] or 0), float(r[4] or 0)
            net = (cr - dr) if klass == "REVENUE" else (dr - cr)
            if abs(net) < 0.01:
                continue
            label = f"{code} {name}" if name else code
            if klass == "REVENUE":
                income[label] = net
            else:
                expenses[label] = net

        total_income = sum(income.values())
        total_expenses = sum(expenses.values())
        net_income = total_income - total_expenses

        # Transaction counts (informational)
        n_deposits = s.execute(text("""
          SELECT COUNT(DISTINCT j.id) FROM journals j
          JOIN general_ledger gl ON gl.journal_id = j.id
          JOIN chart_of_accounts coa ON coa.id = gl.account_id
          WHERE j.status='posted' AND j.journal_date BETWEEN :df AND :dt
            AND coa.account_class='REVENUE' AND gl.credit > 0
        """), {"df": start, "dt": end}).scalar() or 0
        n_withdrawals = s.execute(text("""
          SELECT COUNT(DISTINCT j.id) FROM journals j
          JOIN general_ledger gl ON gl.journal_id = j.id
          JOIN chart_of_accounts coa ON coa.id = gl.account_id
          WHERE j.status='posted' AND j.journal_date BETWEEN :df AND :dt
            AND coa.account_class='EXPENSE' AND gl.debit > 0
        """), {"df": start, "dt": end}).scalar() or 0
    finally:
        s.close()

    # Balance sheet bridge (still uses legacy build for now — out of scope for this pass)
    try:
        bsheet = await bs.build_balance_sheet()
        fx = float(bsheet.get("usd_to_sgd", 1.27))
        closing_nw_sgd = bsheet["net_worth_sgd"]
        closing_nw_usd = bsheet["net_worth_usd"]
        opening_equity_sgd = closing_nw_sgd - net_income
    except Exception as e:
        logger.warning(f"balance sheet bridge failed: {e}")
        fx = 1.27
        closing_nw_sgd = closing_nw_usd = opening_equity_sgd = 0.0

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "year": year,
        "period_start": start,
        "period_end": end,
        "fx_usd_to_sgd": fx,
        "data_source": "sentinel_gl",   # marker for debugging
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
        # Legacy CPF-accrual exclusion no longer applies in the GL model
        "excluded": {
            "income_sgd": 0.0,
            "expense_sgd": 0.0,
            "reason": "GL model: CPF accrual handled at journal-posting time, not via tag exclusion",
        },
        "excluded_from_pnl": [],
        "txn_counts": {
            "deposits": int(n_deposits),
            "withdrawals": int(n_withdrawals),
        },
    }


async def available_years() -> list[int]:
    """Years that have at least one posted P&L journal in the Sentinel GL."""
    from sqlalchemy import text
    from . import database as db
    db.init_db()
    s = db.SessionLocal()
    try:
        rows = s.execute(text("""
          SELECT DISTINCT SUBSTR(CAST(j.journal_date AS TEXT), 1, 4) AS y
          FROM journals j
          JOIN general_ledger gl ON gl.journal_id = j.id
          JOIN chart_of_accounts coa ON coa.id = gl.account_id
          WHERE j.status='posted'
            AND coa.account_class IN ('REVENUE', 'EXPENSE')
          ORDER BY y DESC
        """)).all()
        years = [int(r[0]) for r in rows if r[0] and r[0].isdigit()]
    finally:
        s.close()
    if not years:
        years = [date.today().year]
    return years


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
        f'<link rel="stylesheet" href="/static/privacy.css">'
        f'<style>{_CSS}</style>'
        f'<script src="/static/privacy.js" defer></script>'
        f'</head><body>{body}</body></html>'
    )


def render_html(data: dict, years_available: list[int], current_month: int | None = None) -> str:
    year = data["year"]
    t = data["totals"]
    b = data["bridge"]

    year_options = "".join(
        f'<option value="{y}"{" selected" if y == year else ""}>{y}{" YTD" if y == date.today().year else ""}</option>'
        for y in years_available
    )

    MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    month_options = '<option value="">Full year / YTD</option>' + "".join(
        f'<option value="{i+1}"{" selected" if (i+1) == current_month else ""}>'
        f'{MONTHS[i]}</option>'
        for i in range(12)
    )

    period_form = (
        '<form method="get" action="/income_statement" class="period-form" '
        'style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">'
        '<label style="font-size:12px;color:var(--muted);">Period:</label>'
        f'<select name="year" onchange="this.form.submit()">{year_options}</select>'
        f'<select name="month" onchange="this.form.submit()">{month_options}</select>'
        '</form>'
    )

    def _slug(name: str) -> str:
        """Extract CoA code from name like '4110 Salary — AZ United' → '4110'.
        Falls back to legacy slugify for Firefly-style names."""
        import re as _re
        m = _re.match(r"^(\d{4,5})", name.strip())
        if m:
            return m.group(1)
        s = _re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return s or "uncategorised"

    def rows(items, txn_type: str):
        out = ""
        for r in items:
            slug = _slug(r["name"])
            qs = f"slug={slug}&type={txn_type}&year={year}"
            if current_month:
                qs += f"&month={current_month}"
            out += (
                f'<a class="row" href="/income_statement/category?{qs}" '
                f'style="display:grid;grid-template-columns:1fr 90px 90px;gap:8px;'
                f'text-decoration:none;color:inherit;padding:6px 4px;border-radius:6px;">'
                f'<span>{r["name"]} ▸</span>'
                f'<span class="amt amt-usd">${r["usd"]:,.2f}</span>'
                f'<span class="amt">${r["sgd"]:,.2f}</span></a>'
            )
        return out

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
        + (rows(data["income"], "deposit") or '<div class="row" style="color:var(--muted);"><span>No income recorded</span><span></span><span></span></div>')
        + f'<div class="subtotal"><span>Total Income</span>'
        f'<span class="amt amt-usd">${t["income_usd"]:,.2f}</span>'
        f'<span class="amt">${t["income_sgd"]:,.2f}</span></div>'
        '</div>'

        + '<div class="section"><h2>Expenses</h2>'
        + (rows(data["expenses"], "withdrawal") or '<div class="row" style="color:var(--muted);"><span>No expenses recorded</span><span></span><span></span></div>')
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
