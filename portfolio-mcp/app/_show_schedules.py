"""Repayment schedules — from facility_plans + payment_schedule tables."""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

for tbl in ("facility_plans", "payment_schedule"):
    print(f"\n=== {tbl} schema ===")
    for c in s.execute(text(f"PRAGMA table_info({tbl})")).all():
        print(f"  {c[1]:<28} {c[2]}")
    n = s.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar()
    print(f"  ({n} rows)")

print("\n=== facility_plans contents ===")
rows = s.execute(text("""
  SELECT * FROM facility_plans ORDER BY facility_id, remaining_months DESC LIMIT 30
""")).all()
cols = [c[1] for c in s.execute(text("PRAGMA table_info(facility_plans)")).all()]
for r in rows:
    pretty = ", ".join(f"{cols[i]}={v}" for i, v in enumerate(r) if v is not None and cols[i] not in ('created_at','updated_at'))
    print(f"  {pretty}")

print("\n=== payment_schedule contents (next 15 due) ===")
rows = s.execute(text("""
  SELECT * FROM payment_schedule
  ORDER BY due_date LIMIT 15
""")).all()
cols = [c[1] for c in s.execute(text("PRAGMA table_info(payment_schedule)")).all()]
for r in rows:
    pretty = ", ".join(f"{cols[i]}={v}" for i, v in enumerate(r) if v is not None and cols[i] not in ('created_at','updated_at'))
    print(f"  {pretty}")
s.close()
