"""Sentinel Finance — Cash forecast.

Forward-project the next 30 days of cash movements based on:
  1. Recurring patterns in the past 90 days (monthly bills, salary, etc.)
  2. Scheduled instalments from credit_facilities table
  3. Known fixed obligations (insurance premiums, etc.)

Approach: simple but useful — extract tx by counterparty over last 3 months,
detect recurring patterns (same-amount-different-month), project forward.
"""
import argparse
from datetime import date, datetime, timedelta
from collections import defaultdict
from sqlalchemy import text
from app import database as db


def run(out_path: str | None = None, days: int = 30):
    today = date.today()
    end = today + timedelta(days=days)
    look_back_start = today - timedelta(days=90)
    s = db.SessionLocal()
    output_lines = []

    def w(line=""):
        output_lines.append(line)
        print(line)

    try:
        # Get current cash position
        cash_rows = s.execute(text("""
          SELECT coa.account_code, coa.account_name,
                 SUM(gl.debit) - SUM(gl.credit) AS bal
          FROM general_ledger gl
          JOIN journals j ON j.id = gl.journal_id
          JOIN chart_of_accounts coa ON coa.id = gl.account_id
          WHERE coa.account_class = 'ASSET'
            AND coa.account_code BETWEEN '1110' AND '1119'
            AND j.status = 'posted'
          GROUP BY coa.account_code, coa.account_name
          ORDER BY coa.account_code
        """)).all()
        total_cash = 0.0
        cash_breakdown = []
        for r in cash_rows:
            bal = float(r[2] or 0)
            total_cash += bal
            cash_breakdown.append((r[0], r[1], bal))

        # Get current liability snapshot
        liab_rows = s.execute(text("""
          SELECT coa.account_code, coa.account_name,
                 SUM(gl.credit) - SUM(gl.debit) AS bal
          FROM general_ledger gl
          JOIN journals j ON j.id = gl.journal_id
          JOIN chart_of_accounts coa ON coa.id = gl.account_id
          WHERE coa.account_class = 'LIABILITY'
            AND coa.is_postable = 1
            AND j.status = 'posted'
          GROUP BY coa.account_code, coa.account_name
          HAVING SUM(gl.credit) - SUM(gl.debit) > 1
          ORDER BY coa.account_code
        """)).all()
        total_liab = 0.0
        liab_breakdown = []
        for r in liab_rows:
            bal = float(r[2] or 0)
            total_liab += bal
            liab_breakdown.append((r[0], r[1], bal))

        # Find recurring outflows from POSB (the main spending account)
        # Group by (amount-rounded, counterparty-narration-stem) over last 90 days
        recurring_rows = s.execute(text("""
          SELECT
            ABS(gl.credit) AS amt,
            SUBSTR(gl.narration, 1, 50) AS narr_stem,
            COUNT(*) AS occurrences,
            MIN(j.journal_date) AS first_seen,
            MAX(j.journal_date) AS last_seen
          FROM general_ledger gl
          JOIN journals j ON j.id = gl.journal_id
          JOIN chart_of_accounts coa ON coa.id = gl.account_id
          WHERE coa.account_code = '1111'
            AND gl.credit > 1
            AND j.status = 'posted'
            AND j.journal_date BETWEEN :df AND :today
          GROUP BY ROUND(gl.credit, 2), SUBSTR(gl.narration, 1, 30)
          HAVING COUNT(*) >= 2
          ORDER BY 1 DESC
        """), {"df": str(look_back_start), "today": str(today)}).all()

        w(f"## Cash forecast — next {days} days ({today} to {end})")
        w()
        w("### Current cash position")
        w(f"| Account | Balance |")
        w(f"|---|---:|")
        for code, name, bal in cash_breakdown:
            w(f"| {code} {name} | ${bal:,.2f} |")
        w(f"| **TOTAL CASH** | **${total_cash:,.2f}** |")
        w()
        w("### Current liabilities (CCs, cashlines, loans)")
        w(f"| Account | Balance |")
        w(f"|---|---:|")
        for code, name, bal in liab_breakdown[:15]:
            w(f"| {code} {name} | ${bal:,.2f} |")
        w(f"| **TOTAL LIABILITIES** | **${total_liab:,.2f}** |")
        w()
        w(f"### Net worth (cash − liabilities): **${total_cash - total_liab:,.2f}**")
        w()
        w("### Recurring outflows from POSB (last 90 days, ≥2 occurrences)")
        w("Projected to recur next month at same cadence.")
        w()
        w(f"| Amount | Narration | Occurrences | First | Last |")
        w(f"|---:|---|---:|---|---|")
        recurring_total = 0.0
        for r in recurring_rows[:25]:
            amt, narr, n, first, last = float(r[0]), r[1], r[2], r[3], r[4]
            # Simple projection: if 2+ occurrences in 90d, assume monthly recurring
            if n >= 2:
                recurring_total += amt    # one expected occurrence in next 30d
            w(f"| ${amt:,.2f} | {narr[:60]} | {n} | {first} | {last} |")
        w()
        w(f"### Projected outflows next 30 days (recurring-pattern based): **${recurring_total:,.2f}**")
        w()
        w(f"### Projected cash position end of period: **${total_cash - recurring_total:,.2f}**")
        w()
        w("---")
        w()
        w("**Caveats:**")
        w("- Projection assumes each recurring outflow happens once in the next 30 days")
        w("- Doesn't include irregular spending (one-off purchases)")
        w("- Doesn't model salary inflows — those continue but vary")
        w("- For higher accuracy: use credit_facilities.payment_schedule (fixed instalments)")
    finally:
        s.close()

    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(output_lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    db.init_db()
    run(args.out, args.days)


if __name__ == "__main__":
    main()
