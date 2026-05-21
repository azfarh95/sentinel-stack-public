"""Combined Sentinel Finance report — bundles P&L for multiple periods + balance
sheet snapshot + cash forecast into a single markdown doc accessible via
http://100.73.83.20:18086/static/reports/sentinel_finance_report.md (on host)
or just /static/reports/ from the phone.

This is the closing deliverable: a single readable view of "what did I earn /
spend / am I cash-flow positive?" — answered from the user's own ledger.
"""
from __future__ import annotations
import argparse
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from sqlalchemy import text

from app import database as db


def _income_statement(s, date_from: str, date_to: str) -> tuple[list, list, float, float, float]:
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
    """), {"df": date_from, "dt": date_to}).all()
    income, expense = [], []
    income_total = expense_total = 0.0
    for r in rows:
        code, name, klass, dr, cr = r[0], r[1], r[2], float(r[3] or 0), float(r[4] or 0)
        net = (cr - dr) if klass == 'REVENUE' else (dr - cr)
        if abs(net) < 0.01: continue
        if klass == 'REVENUE': income.append((code, name, net)); income_total += net
        else: expense.append((code, name, net)); expense_total += net
    return income, expense, income_total, expense_total, income_total - expense_total


def _balance_sheet(s) -> tuple[list, list, list, float, float, float]:
    """Snapshot at today's date — assets, liabilities, equity totals."""
    rows = s.execute(text("""
      SELECT coa.account_code, coa.account_name, coa.account_class,
             SUM(gl.debit) - SUM(gl.credit) AS bal
      FROM general_ledger gl
      JOIN journals j ON j.id = gl.journal_id
      JOIN chart_of_accounts coa ON coa.id = gl.account_id
      WHERE coa.is_postable = 1
        AND j.status = 'posted'
        AND coa.account_class IN ('ASSET', 'LIABILITY', 'EQUITY')
      GROUP BY coa.account_code, coa.account_name, coa.account_class
      HAVING ABS(SUM(gl.debit) - SUM(gl.credit)) > 1
      ORDER BY coa.account_code
    """)).all()
    assets, liabilities, equity = [], [], []
    a = l = e = 0.0
    for r in rows:
        code, name, klass, bal = r[0], r[1], r[2], float(r[3] or 0)
        # Assets: positive Dr-Cr; Liabilities: positive Cr-Dr; Equity: positive Cr-Dr
        if klass == 'ASSET':
            assets.append((code, name, bal)); a += bal
        elif klass == 'LIABILITY':
            liabilities.append((code, name, -bal)); l += -bal
        elif klass == 'EQUITY':
            equity.append((code, name, -bal)); e += -bal
    return assets, liabilities, equity, a, l, e


def _recurring_outflows(s, lookback_days: int = 90) -> list:
    today = date.today()
    start = today - timedelta(days=lookback_days)
    rows = s.execute(text("""
      SELECT ROUND(gl.credit, 2) AS amt,
             SUBSTR(gl.narration, 1, 40) AS narr,
             COUNT(*) AS occurrences
      FROM general_ledger gl
      JOIN journals j ON j.id = gl.journal_id
      JOIN chart_of_accounts coa ON coa.id = gl.account_id
      WHERE coa.account_code = '1111' AND gl.credit > 1
        AND j.status = 'posted'
        AND j.journal_date BETWEEN :df AND :dt
      GROUP BY ROUND(gl.credit, 2), SUBSTR(gl.narration, 1, 30)
      HAVING COUNT(*) >= 2
      ORDER BY 1 DESC
    """), {"df": str(start), "dt": str(today)}).all()
    return rows


def _journal_source_mix(s, date_from: str, date_to: str) -> list:
    return s.execute(text("""
      SELECT j.source_doc, COUNT(*) FROM journals j
      WHERE j.status='posted' AND j.journal_date BETWEEN :df AND :dt
      GROUP BY j.source_doc ORDER BY 2 DESC
    """), {"df": date_from, "dt": date_to}).all()


def generate(out_path: Path):
    s = db.SessionLocal()
    buf = StringIO()
    w = buf.write

    today = date.today()
    w(f"# Sentinel Finance — Report\n\n")
    w(f"_Generated {today.isoformat()} from the Sentinel ledger (Firefly-decoupled, "
       "POSB-direct, OCR-universal pipeline)._\n\n")

    # ── P&L per period ────────────────────────────────────────
    for label, df, dt in [
        ("2024 — Full year",       "2024-01-01", "2024-12-31"),
        ("2025 — Full year",       "2025-01-01", "2025-12-31"),
        ("2026 — Year-to-date",    "2026-01-01", today.isoformat()),
    ]:
        income, expense, it, et, net = _income_statement(s, df, dt)
        w(f"## Income Statement — {label}\n\n")
        w(f"_{df} to {dt}_\n\n")
        w("### Income\n")
        w("| CoA | Account | $ |\n|---|---|---:|\n")
        for code, name, amt in income:
            w(f"| {code} | {name} | ${amt:,.2f} |\n")
        w(f"| | **TOTAL INCOME** | **${it:,.2f}** |\n\n")
        w("### Expenses\n")
        w("| CoA | Account | $ |\n|---|---|---:|\n")
        for code, name, amt in expense:
            w(f"| {code} | {name} | ${amt:,.2f} |\n")
        w(f"| | **TOTAL EXPENSES** | **${et:,.2f}** |\n\n")
        net_label = "NET INCOME" if net >= 0 else "NET LOSS"
        w(f"**{net_label}: ${net:+,.2f}**\n\n")
        w("---\n\n")

    # ── Balance Sheet (today) ─────────────────────────────────
    assets, liabilities, equity, a, l, e = _balance_sheet(s)
    w(f"## Balance Sheet — Snapshot ({today})\n\n")
    w("### Assets\n")
    w("| CoA | Account | $ |\n|---|---|---:|\n")
    for code, name, amt in assets:
        w(f"| {code} | {name} | ${amt:,.2f} |\n")
    w(f"| | **TOTAL ASSETS** | **${a:,.2f}** |\n\n")
    w("### Liabilities\n")
    w("| CoA | Account | $ |\n|---|---|---:|\n")
    for code, name, amt in liabilities:
        w(f"| {code} | {name} | ${amt:,.2f} |\n")
    w(f"| | **TOTAL LIABILITIES** | **${l:,.2f}** |\n\n")
    w(f"**Net Worth: ${a - l:,.2f}**  (Assets − Liabilities)\n\n")
    w("---\n\n")

    # ── Cash forecast ─────────────────────────────────────────
    w("## Cash Forecast — Next 30 days\n\n")
    cash_rows = s.execute(text("""
      SELECT coa.account_code, coa.account_name, SUM(gl.debit) - SUM(gl.credit) bal
      FROM general_ledger gl JOIN journals j ON j.id=gl.journal_id
      JOIN chart_of_accounts coa ON coa.id=gl.account_id
      WHERE coa.account_code BETWEEN '1110' AND '1119' AND coa.is_postable=1
        AND j.status='posted'
      GROUP BY coa.account_code ORDER BY coa.account_code
    """)).all()
    total_cash = 0.0
    w("### Current cash position\n")
    w("| Account | Balance |\n|---|---:|\n")
    for r in cash_rows:
        bal = float(r[2] or 0); total_cash += bal
        w(f"| {r[0]} {r[1]} | ${bal:,.2f} |\n")
    w(f"| **TOTAL CASH** | **${total_cash:,.2f}** |\n\n")

    rec = _recurring_outflows(s)[:20]
    recurring_total = sum(float(r[0]) for r in rec if r[2] >= 2)
    w(f"### Recurring outflows (last 90 days, projected to repeat once in next 30d)\n\n")
    w(f"| Amount | Narration | Occurrences |\n|---:|---|---:|\n")
    for r in rec[:15]:
        amt = float(r[0]); narr = (r[1] or '')[:50]; n = r[2]
        w(f"| ${amt:,.2f} | {narr} | {n} |\n")
    w(f"\n**Projected outflows next 30d: ${recurring_total:,.2f}**\n")
    w(f"**Projected cash end-of-period: ${total_cash - recurring_total:,.2f}**\n\n")
    w("---\n\n")

    # ── Caveats ───────────────────────────────────────────────
    w("## Known imperfections\n\n")
    w("- **4900 Other Income** is a catch-all for unclassified inflows. Most likely composition: cashline drawdowns "
       "(should be liability ↑, not income), friend repayments, transfers from other own banks not yet matched.\n")
    w("- **5190 General Expense** is the lifestyle-lump catch-all for debit card / POS / Bill Payment / FAST+PayNow.\n")
    w("- **4 YourAgency payslips** have phantom POSB-inflow legs — YourAgency actually pays into a different bank "
       "(awaiting user confirmation of which account).\n")
    w("- **Per-month POSB reconciliation drifts** ranging $50-$1500 (mostly date misalignments between POSB tx date "
       "and CC parser POST date). Annual totals remain directionally correct.\n")
    w("- **Opening Balance @ 2026-01-01** uses $2,696.69 for POSB; PDF says $2,353.81. Off by $343.\n")
    w("- **27 FIREFLY_BRIDGE journals** still active in 2025 — some flows still flowing through the bridge path.\n\n")

    # ── Source mix ────────────────────────────────────────────
    w("## Journal sources contributing to 2025 P&L\n\n")
    w("| source_doc | journals |\n|---|---:|\n")
    for r in _journal_source_mix(s, "2025-01-01", "2025-12-31")[:15]:
        w(f"| {r[0]} | {r[1]} |\n")
    w("\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(buf.getvalue())
    print(f"Wrote {out_path}  ({len(buf.getvalue())} chars)")
    s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/app/static/reports/sentinel_finance_report.md")
    args = ap.parse_args()
    db.init_db()
    generate(Path(args.out))


if __name__ == "__main__":
    main()
