"""Sentinel Finance — Income Statement direct from GL (no Firefly).

Different from app.income_statement (which still pulls from Firefly). This one
queries the SQLite GL directly. After the v2 decouple, this is authoritative.

Sums all postable journals in the period by CoA, organised:
  Revenue / income (class 4)
  Expenses (class 5)
  Net profit/loss

Run:
    docker exec portfolio-mcp python -m app.sf_income_statement --from 2026-01-01 --to 2026-04-30
"""
import argparse
from datetime import date
from sqlalchemy import text
from app import database as db


def run(date_from: str, date_to: str, out_path: str | None = None):
    s = db.SessionLocal()
    output_lines = []

    def w(line=""):
        output_lines.append(line)
        print(line)

    try:
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

        income_lines = []
        expense_lines = []
        income_total = 0.0
        expense_total = 0.0
        for r in rows:
            code, name, klass, dr, cr = r[0], r[1], r[2], float(r[3] or 0), float(r[4] or 0)
            net = (cr - dr) if klass == 'REVENUE' else (dr - cr)
            if abs(net) < 0.01:
                continue
            if klass == 'REVENUE':
                income_lines.append((code, name, net)); income_total += net
            else:
                expense_lines.append((code, name, net)); expense_total += net

        title = f"INCOME STATEMENT  |  {date_from}  to  {date_to}"
        w("=" * 78)
        w(title)
        w("=" * 78)
        w()
        w(f"{'Code':<8} {'Account':<48} {'$':>16}")
        w("-" * 78)
        w("INCOME")
        for code, name, net in income_lines:
            w(f"  {code:<6} {name[:46]:<46} {net:>16,.2f}")
        w("  " + "-" * 70)
        w(f"  {'TOTAL INCOME':<54} {income_total:>16,.2f}")
        w()
        w("EXPENSES")
        # Group expenses by category prefix
        prefix_buckets = {}
        for code, name, net in expense_lines:
            prefix = code[:2]   # 51 F&B / 52 Subscription / 53 Insurance / 57 Bank fees
            prefix_buckets.setdefault(prefix, []).append((code, name, net))
        for prefix in sorted(prefix_buckets.keys()):
            subtotal = sum(net for _, _, net in prefix_buckets[prefix])
            for code, name, net in prefix_buckets[prefix]:
                w(f"  {code:<6} {name[:46]:<46} {net:>16,.2f}")
        w("  " + "-" * 70)
        w(f"  {'TOTAL EXPENSES':<54} {expense_total:>16,.2f}")
        w()
        w("=" * 78)
        net_result = income_total - expense_total
        label = "NET INCOME" if net_result >= 0 else "NET LOSS"
        w(f"  {label:<54} {net_result:>+16,.2f}")
        w("=" * 78)
        w()
        w("Source data (journals contributing to P&L lines this period):")
        rows2 = s.execute(text("""
          SELECT j.source_doc, COUNT(DISTINCT j.id)
          FROM journals j
          JOIN general_ledger gl ON gl.journal_id = j.id
          JOIN chart_of_accounts coa ON coa.id = gl.account_id
          WHERE j.status = 'posted'
            AND j.journal_date BETWEEN :df AND :dt
            AND coa.account_class IN ('REVENUE', 'EXPENSE')
          GROUP BY j.source_doc
          ORDER BY 2 DESC
        """), {"df": date_from, "dt": date_to}).all()
        for r in rows2:
            w(f"  {r[0]:<32} {r[1]:>4} journals")
    finally:
        s.close()

    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(output_lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default="2026-01-01")
    ap.add_argument("--to", dest="date_to", default=str(date.today()))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    db.init_db()
    run(args.date_from, args.date_to, args.out)


if __name__ == "__main__":
    main()
