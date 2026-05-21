"""Find the 7 missing Ganesan payments with ±2 day window."""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

aid_1111 = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='1111'")).fetchone()[0]

MISSING = [
    ("2025-09-06", 120.00), ("2025-09-12", 120.00), ("2025-09-14", 120.00),
    ("2025-09-19", 120.00), ("2025-09-22", 120.00), ("2025-10-12", 120.00),
    ("2025-11-01", 120.00),
]

for dt, amt in MISSING:
    print(f"\nMissing: {dt}  ${amt:,.2f}")
    rows = s.execute(text("""
      SELECT j.id, j.journal_date, gl.debit, j.narration
      FROM journals j JOIN general_ledger gl ON gl.journal_id=j.id
      WHERE j.status='posted' AND gl.account_id=:posb
        AND gl.debit BETWEEN :amt - 0.01 AND :amt + 0.01
        AND j.journal_date BETWEEN date(:d, '-3 days') AND date(:d, '+3 days')
    """), {"posb": aid_1111, "amt": amt, "d": dt}).all()
    if not rows:
        print(f"  ✗ No $120 POSB Dr in ±3 days of {dt}")
    for r in rows:
        print(f"  candidate: jid={r[0]} {r[1]} ${float(r[2]):.2f}  {(r[3] or '')[:60]}")
s.close()
