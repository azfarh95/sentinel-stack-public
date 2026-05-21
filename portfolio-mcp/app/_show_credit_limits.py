from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()
rows = s.execute(text("""
  SELECT id, lender_name, facility_type, credit_limit, current_outstanding, available_balance, status
  FROM credit_facilities
  WHERE status = 'active'
  ORDER BY facility_type, credit_limit DESC
""")).all()
print(f"=== {len(rows)} ACTIVE facilities ===\n")
print(f"{'Lender':<40} {'Type':<14} {'Limit':>12} {'Outstanding':>12} {'Available':>12}")
print("-" * 100)
by_type = {}
T = O = A = 0.0
for r in rows:
    name, kind, lim, out, avail = r[1], r[2] or '?', float(r[3] or 0), float(r[4] or 0), float(r[5] or 0)
    by_type.setdefault(kind, {"n": 0, "lim": 0.0, "out": 0.0, "avail": 0.0})
    by_type[kind]["n"] += 1
    by_type[kind]["lim"] += lim
    by_type[kind]["out"] += out
    by_type[kind]["avail"] += avail
    T += lim; O += out; A += avail
    print(f"  {(name or '?')[:38]:<38} {kind[:12]:<14} S${lim:>10,.2f} S${out:>10,.2f} S${avail:>10,.2f}")

print("\n=== By type ===")
for k in sorted(by_type):
    v = by_type[k]
    print(f"  {k:<14}  {v['n']} facilities  limit=S${v['lim']:>10,.2f}  outstanding=S${v['out']:>10,.2f}  avail=S${v['avail']:>10,.2f}")

print("\n=== TOTAL ===")
print(f"  Aggregated credit limit:    S$ {T:>11,.2f}")
print(f"  Total current outstanding:  S$ {O:>11,.2f}")
print(f"  Total available headroom:   S$ {A:>11,.2f}")
print(f"  Utilisation:                {(O/T*100) if T > 0 else 0:>11.1f}%")
s.close()
