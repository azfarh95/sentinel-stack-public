from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()
r = s.execute(text("SELECT status, COUNT(*) FROM journals WHERE source_doc='CC_PDF_DIRECT:2114' GROUP BY status")).all()
for row in r: print(f"  CC_PDF_DIRECT:2114  {row[0]}: {row[1]}")
r = s.execute(text("""
  SELECT SUM(gl.debit) - SUM(gl.credit) as net_change, COUNT(DISTINCT j.id) n_journals
  FROM journals j JOIN general_ledger gl ON gl.journal_id=j.id
  JOIN chart_of_accounts coa ON coa.id=gl.account_id
  WHERE j.status='posted' AND coa.account_code='2114'
""")).fetchone()
print(f"HSBC (2114) posted net change (ALL sources): ${float(r[0] or 0):,.2f} over {r[1]} journals")
r = s.execute(text("""
  SELECT SUM(gl.debit) - SUM(gl.credit) as net
  FROM journals j JOIN general_ledger gl ON gl.journal_id=j.id
  JOIN chart_of_accounts coa ON coa.id=gl.account_id
  WHERE j.status='posted' AND coa.account_code='2114'
    AND j.source_doc='CC_PDF_DIRECT:2114'
""")).fetchone()
print(f"  HSBC change via CC_PDF_DIRECT:2114 only: ${float(r[0] or 0):,.2f}")
r = s.execute(text("""
  SELECT SUM(gl.debit) - SUM(gl.credit) as net
  FROM journals j JOIN general_ledger gl ON gl.journal_id=j.id
  JOIN chart_of_accounts coa ON coa.id=gl.account_id
  WHERE j.status='posted' AND coa.account_code='2114'
    AND j.source_doc='POSB_PDF_DIRECT'
""")).fetchone()
print(f"  HSBC change via POSB_PDF_DIRECT (payments): ${float(r[0] or 0):,.2f}")
s.close()
