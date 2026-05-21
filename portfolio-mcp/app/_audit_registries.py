"""Audit what registries actually exist in the DB."""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

print("=== All tables in /data/portfolio.db ===")
rows = s.execute(text("""
  SELECT name FROM sqlite_master WHERE type='table' ORDER BY name
""")).all()
for r in rows:
    cnt = s.execute(text(f"SELECT COUNT(*) FROM {r[0]}")).scalar() or 0
    print(f"  {r[0]:<45} {cnt:>6} rows")

# Look for anything resembling a statement registry
print("\n=== Looking for tables with BF/CF/balance_brought_forward columns ===")
for r in rows:
    cols = s.execute(text(f"PRAGMA table_info({r[0]})")).all()
    col_names = [c[1].lower() for c in cols]
    has_bf = any("brought" in c or "carried" in c or "opening" in c or "closing" in c
                 for c in col_names)
    has_balance = any("balance" in c for c in col_names)
    if has_bf or "balance" in r[0].lower() or "statement" in r[0].lower():
        print(f"  {r[0]}:  cols = {col_names[:8]}{'...' if len(col_names) > 8 else ''}")
s.close()
