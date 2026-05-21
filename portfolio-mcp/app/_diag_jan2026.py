"""Find what's in POSB GL for Jan 2026 that doesn't match the PDF."""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

# All posted POSB journals in Jan 2026, by source_doc
r = s.execute(text("""
  SELECT j.source_doc, COUNT(*),
         SUM(gl.debit), SUM(gl.credit),
         SUM(gl.debit) - SUM(gl.credit) AS net
  FROM journals j JOIN general_ledger gl ON gl.journal_id=j.id
  JOIN chart_of_accounts coa ON coa.id=gl.account_id
  WHERE j.status='posted' AND coa.account_code='1111'
    AND j.journal_date BETWEEN '2026-01-01' AND '2026-01-31'
  GROUP BY j.source_doc ORDER BY net DESC
""")).all()
print("=== POSB (1111) GL legs in Jan 2026 by source_doc ===")
print(f"{'source_doc':<35} {'n':>3}  {'sum_Dr':>12}  {'sum_Cr':>12}  {'net':>12}")
total = 0
for row in r:
    n, dr, cr, net = row[1], float(row[2] or 0), float(row[3] or 0), float(row[4] or 0)
    total += net
    print(f"  {row[0]:<33} {n:>3}  {dr:>12,.2f}  {cr:>12,.2f}  {net:>12,.2f}")
print(f"  {'TOTAL':<33}        {'':>12}  {'':>12}  {total:>12,.2f}")

# Specifically biggest single-journal contributions
print("\n=== Top 10 biggest individual POSB legs in Jan 2026 ===")
r = s.execute(text("""
  SELECT j.id, j.journal_date, j.source_doc, gl.debit, gl.credit, j.narration
  FROM journals j JOIN general_ledger gl ON gl.journal_id=j.id
  JOIN chart_of_accounts coa ON coa.id=gl.account_id
  WHERE j.status='posted' AND coa.account_code='1111'
    AND j.journal_date BETWEEN '2026-01-01' AND '2026-01-31'
    AND (gl.debit > 200 OR gl.credit > 200)
  ORDER BY (gl.debit + gl.credit) DESC LIMIT 12
""")).all()
for row in r:
    dr = float(row[3] or 0); cr = float(row[4] or 0)
    direction = f"Dr ${dr:,.2f}" if dr > 0 else f"Cr ${cr:,.2f}"
    print(f"  jid={row[0]:<5} {row[1]}  {row[2]:<22} {direction:<15} {(row[5] or '')[:60]}")
s.close()
