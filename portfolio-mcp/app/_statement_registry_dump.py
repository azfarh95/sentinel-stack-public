"""Pull earliest statement per facility from statement_registry — that BF is
the opening balance we need."""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

# Show schema
print("=== statement_registry columns ===")
for c in s.execute(text("PRAGMA table_info(statement_registry)")).all():
    print(f"  {c[1]:<25} {c[2]}")

# Earliest row per facility
print("\n=== Earliest statement per facility (BF = opening anchor candidate) ===")
rows = s.execute(text("""
  SELECT facility_id, bank, MIN(statement_date) AS earliest, previous_balance, closing_balance
  FROM statement_registry
  WHERE previous_balance IS NOT NULL
  GROUP BY facility_id, bank
  ORDER BY earliest
""")).all()
for r in rows:
    print(f"  {(r[1] or '?')[:25]:<25} facility_id={r[0]:<6}  earliest={r[2]}  BF=${float(r[3] or 0):>10,.2f}  CF=${float(r[4] or 0):>10,.2f}")

# Map facility_id → CoA via credit_facilities table
print("\n=== facility_id → CoA mapping (via credit_facilities) ===")
fac_rows = s.execute(text("""
  SELECT cf.id, cf.lender_name, cf.facility_type, cf.firefly_acct_id
  FROM credit_facilities cf
""")).all()
for r in fac_rows:
    print(f"  cf.id={r[0]:<22} ff_id={r[3]}  {r[1]} ({r[2]})")
s.close()
