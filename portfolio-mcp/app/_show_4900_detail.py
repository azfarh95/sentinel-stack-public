"""Full detail of remaining 4900 'Other Income' for 2025."""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

rows = s.execute(text("""
  SELECT j.journal_date, gl.credit, j.narration, gl.narration as gln
  FROM journals j
  JOIN general_ledger gl ON gl.journal_id=j.id
  JOIN chart_of_accounts coa ON coa.id=gl.account_id
  WHERE coa.account_code='4900' AND gl.credit > 0
    AND j.status='posted'
    AND j.journal_date BETWEEN '2025-01-01' AND '2025-12-31'
  ORDER BY gl.credit DESC
""")).all()

print(f"=== Remaining 4900 Other Income — 2025 ({len(rows)} entries) ===\n")
total = 0
for r in rows:
    amt = float(r[1]); total += amt
    narr = (r[2] or "")
    # Strip the "[direct POSB] " prefix for compactness
    narr = narr.replace("[direct POSB] ", "")
    print(f"  ${amt:>8,.2f}  {r[0]}  {narr[:90]}")
print(f"\n  TOTAL: ${total:,.2f}")
s.close()
