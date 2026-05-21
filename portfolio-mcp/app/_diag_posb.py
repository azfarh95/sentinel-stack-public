"""Diagnose POSB_PDF_DIRECT journal state after re-replay."""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()
r = s.execute(text("SELECT status, COUNT(*) FROM journals WHERE source_doc='POSB_PDF_DIRECT' GROUP BY status")).all()
for row in r: print(f"  POSB_PDF_DIRECT  {row[0]}: {row[1]}")
print()
r = s.execute(text("""
  SELECT coa.account_code, COUNT(*)
  FROM journals j JOIN general_ledger gl ON gl.journal_id=j.id
  JOIN chart_of_accounts coa ON coa.id=gl.account_id
  WHERE j.source_doc='POSB_PDF_DIRECT' AND j.status='posted'
    AND coa.account_code NOT IN ('1111','1114','1115','2111','2112','2113','2114')
  GROUP BY coa.account_code ORDER BY 2 DESC LIMIT 15
""")).all()
print("Posted POSB_PDF_DIRECT contra-legs by CoA:")
for row in r: print(f"  {row[0]:<6}  {row[1]}")
s.close()
