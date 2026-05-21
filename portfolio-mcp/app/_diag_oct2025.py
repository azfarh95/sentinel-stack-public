"""Drill into Oct 2025 POSB drift."""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

# All POSB legs in Oct 2025 by source_doc
r = s.execute(text("""
  SELECT j.source_doc, COUNT(*), SUM(gl.debit), SUM(gl.credit),
         SUM(gl.debit) - SUM(gl.credit) as net
  FROM journals j JOIN general_ledger gl ON gl.journal_id=j.id
  JOIN chart_of_accounts coa ON coa.id=gl.account_id
  WHERE j.status='posted' AND coa.account_code='1111'
    AND j.journal_date BETWEEN '2025-10-01' AND '2025-10-31'
  GROUP BY j.source_doc ORDER BY net DESC
""")).all()
print("=== Oct 2025 POSB GL legs ===")
print(f"{'source_doc':<35} {'n':>3}  {'sum_Dr':>12}  {'sum_Cr':>12}  {'net':>12}")
total = 0
for row in r:
    n, dr, cr, net = row[1], float(row[2] or 0), float(row[3] or 0), float(row[4] or 0)
    total += net
    print(f"  {row[0]:<33} {n:>3}  {dr:>12,.2f}  {cr:>12,.2f}  {net:>12,.2f}")
print(f"  TOTAL: {total:.2f}")
print(f"  PDF says +535.76, GL says +{total:.2f}, drift = {535.76 - total:.2f}")

# Top biggest inflows
print("\n=== Top 8 biggest POSB inflows (Dr) Oct 2025 ===")
r = s.execute(text("""
  SELECT j.id, j.journal_date, j.source_doc, gl.debit, j.narration
  FROM journals j JOIN general_ledger gl ON gl.journal_id=j.id
  JOIN chart_of_accounts coa ON coa.id=gl.account_id
  WHERE j.status='posted' AND coa.account_code='1111'
    AND j.journal_date BETWEEN '2025-10-01' AND '2025-10-31'
    AND gl.debit > 0
  ORDER BY gl.debit DESC LIMIT 8
""")).all()
for row in r:
    print(f"  jid={row[0]:<5} {row[1]} {row[2]:<22} Dr ${float(row[3]):>8,.2f}  {(row[4] or '')[:60]}")
s.close()
