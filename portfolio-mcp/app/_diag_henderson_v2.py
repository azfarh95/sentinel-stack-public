"""For each YourAgency payment date, list ALL POSB inflows ±7 days to see what
the actual deposit might be (different amount than payslip net_pay)."""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

for pd_iso, expected_net in [
    ("2025-10-31", 952.00),
    ("2025-12-03", 1271.50),
    ("2025-12-31", 1624.00),
    ("2026-02-03", 1069.00),
]:
    print(f"\n=== Around YourAgency pay_date {pd_iso} (expected net ${expected_net:,.2f}) ===")
    rows = s.execute(text("""
      SELECT j.id, j.journal_date, j.source_doc, gl.debit, j.narration
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id=j.id
      JOIN chart_of_accounts coa ON coa.id=gl.account_id
      WHERE coa.account_code='1111' AND j.status='posted'
        AND gl.debit > 100
        AND j.journal_date BETWEEN date(:pd, '-7 days') AND date(:pd, '+10 days')
      ORDER BY j.journal_date, gl.debit DESC
    """), {"pd": pd_iso}).all()
    for r in rows:
        dr = float(r[3])
        marker = "  ★" if abs(dr - expected_net) < 10 else "   "
        print(f"  {marker} {r[1]} {r[2]:<22} Dr ${dr:>9,.2f}  {(r[4] or '')[:60]}")
s.close()
