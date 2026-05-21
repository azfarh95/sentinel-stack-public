"""Investigate 12229 Singlife Savvy Invest balance — where did $36k come from?"""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

# Get current balance
row = s.execute(text("""
  SELECT COALESCE(SUM(gl.debit),0) - COALESCE(SUM(gl.credit),0) AS bal
  FROM general_ledger gl
  JOIN chart_of_accounts coa ON coa.id = gl.account_id
  JOIN journals j ON j.id = gl.journal_id
  WHERE coa.account_code = '12229' AND j.status = 'posted'
""")).fetchone()
print(f"Net balance of 12229: ${float(row[0] or 0):,.2f}\n")

# Breakdown by source_doc
print("=== 12229 movements by source ===")
rows = s.execute(text("""
  SELECT j.source_doc, COUNT(*), SUM(gl.debit) as dr, SUM(gl.credit) as cr,
         MIN(j.journal_date), MAX(j.journal_date)
  FROM journals j JOIN general_ledger gl ON gl.journal_id = j.id
  JOIN chart_of_accounts coa ON coa.id = gl.account_id
  WHERE coa.account_code = '12229' AND j.status = 'posted'
  GROUP BY j.source_doc ORDER BY 3 DESC
""")).all()
print(f"{'source_doc':<32} {'n':>3} {'sum_Dr':>11} {'sum_Cr':>11} {'first':<11} {'last':<11}")
for r in rows:
    dr, cr = float(r[2] or 0), float(r[3] or 0)
    print(f"  {r[0]:<30} {r[1]:>3}  ${dr:>9,.2f} ${cr:>9,.2f}  {r[4]}  {r[5]}")

# Top 10 biggest individual debits
print("\n=== Top 10 biggest Dr 12229 entries ===")
rows = s.execute(text("""
  SELECT j.id, j.journal_date, j.source_doc, gl.debit, j.narration
  FROM journals j JOIN general_ledger gl ON gl.journal_id = j.id
  JOIN chart_of_accounts coa ON coa.id = gl.account_id
  WHERE coa.account_code = '12229' AND gl.debit > 0 AND j.status='posted'
  ORDER BY gl.debit DESC LIMIT 10
""")).all()
for r in rows:
    print(f"  jid={r[0]:<5} {r[1]} {r[2]:<22} Dr ${float(r[3]):>9,.2f}  {(r[4] or '')[:60]}")

# Count of monthly premium entries
print("\n=== $252.85 monthly premium count ===")
r = s.execute(text("""
  SELECT COUNT(*), SUM(gl.debit)
  FROM general_ledger gl
  JOIN journals j ON j.id = gl.journal_id
  JOIN chart_of_accounts coa ON coa.id = gl.account_id
  WHERE coa.account_code='12229' AND ABS(gl.debit - 252.85) < 0.5 AND j.status='posted'
""")).fetchone()
print(f"  $252.85 × {r[0]} = ${float(r[1] or 0):,.2f}")
s.close()
