"""Scope MANUAL_BACKFILL and OPENING_BALANCE journals — find duplicates."""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

print("=== All MANUAL_BACKFILL journals ===")
r = s.execute(text("""
  SELECT j.id, j.journal_date, j.source_doc, j.narration,
         GROUP_CONCAT(coa.account_code || ':' || (gl.debit-gl.credit), '|') as legs
  FROM journals j JOIN general_ledger gl ON gl.journal_id=j.id
  JOIN chart_of_accounts coa ON coa.id=gl.account_id
  WHERE j.source_doc LIKE 'MANUAL_BACKFILL%' AND j.status='posted'
  GROUP BY j.id ORDER BY j.journal_date
""")).all()
print(f"  {len(r)} manual backfill journals posted")
for row in r:
    print(f"  jid={row[0]:<5} {row[1]} {row[2]:<28} {(row[3] or '')[:60]}")
    print(f"      legs: {row[4]}")

print("\n=== All OPENING_BALANCE journals ===")
r = s.execute(text("""
  SELECT j.id, j.journal_date, j.narration,
         GROUP_CONCAT(coa.account_code || ':' || ROUND(gl.debit-gl.credit, 2), '|') as legs
  FROM journals j JOIN general_ledger gl ON gl.journal_id=j.id
  JOIN chart_of_accounts coa ON coa.id=gl.account_id
  WHERE j.source_doc='OPENING_BALANCE' AND j.status='posted'
  GROUP BY j.id ORDER BY j.journal_date
""")).all()
for row in r:
    print(f"  jid={row[0]:<5} {row[1]} {(row[2] or '')[:50]}")
    print(f"      {row[3]}")

# For POSB 1111: check if MANUAL_BACKFILL is creating dups with POSB direct
print("\n=== POSB 1111: MANUAL_BACKFILL vs POSB_PDF_DIRECT same-day same-amount ===")
r = s.execute(text("""
  SELECT m.id as manual_jid, m.journal_date, gl_m.debit, gl_m.credit,
         p.id as posb_jid, gl_p.debit, gl_p.credit
  FROM journals m
  JOIN general_ledger gl_m ON gl_m.journal_id = m.id
  JOIN chart_of_accounts coa_m ON coa_m.id = gl_m.account_id AND coa_m.account_code = '1111'
  JOIN journals p ON p.journal_date = m.journal_date AND p.id != m.id
  JOIN general_ledger gl_p ON gl_p.journal_id = p.id
  JOIN chart_of_accounts coa_p ON coa_p.id = gl_p.account_id AND coa_p.account_code = '1111'
  WHERE m.source_doc LIKE 'MANUAL_BACKFILL%' AND m.status='posted'
    AND p.source_doc = 'POSB_PDF_DIRECT' AND p.status='posted'
    AND ABS((gl_m.debit + gl_m.credit) - (gl_p.debit + gl_p.credit)) < 0.50
""")).all()
print(f"  {len(r)} likely duplicate pairs found")
for row in r:
    md = float(row[2] or 0) + float(row[3] or 0)
    pd = float(row[5] or 0) + float(row[6] or 0)
    print(f"  jid={row[0]:<5} (manual) ${md:,.2f}  ↔  jid={row[4]:<5} (posb direct) ${pd:,.2f}  on {row[1]}")
s.close()
