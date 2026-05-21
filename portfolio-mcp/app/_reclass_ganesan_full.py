"""Full Ganesan reclassification — user-confirmed list of 39 dated payments.
Finds each by date+amount across ALL contra-account codes (not just 4900),
and repoints to 4140 Salary — Ganesan.
"""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

# User-confirmed list (Date, Amount)
GANESAN_PAYMENTS = [
    ("2025-06-01", 120.00), ("2025-06-08", 120.00), ("2025-06-14", 120.00),
    ("2025-06-16", 120.00), ("2025-06-21", 120.00), ("2025-06-22", 120.00),
    ("2025-06-29", 120.00),
    ("2025-07-05", 120.00), ("2025-07-06", 120.00), ("2025-07-13", 120.00),
    ("2025-07-20", 120.00), ("2025-07-26", 120.00), ("2025-07-28", 120.00),
    ("2025-08-03", 120.00), ("2025-08-16", 120.00), ("2025-08-17", 120.00),
    ("2025-08-23", 120.00), ("2025-08-24", 120.00), ("2025-08-30", 120.00),
    ("2025-08-31", 120.00),
    ("2025-09-06", 120.00), ("2025-09-07", 120.00), ("2025-09-08", 120.00),
    ("2025-09-12", 120.00), ("2025-09-14", 120.00), ("2025-09-15", 120.00),
    ("2025-09-19", 120.00), ("2025-09-21", 120.00), ("2025-09-22", 120.00),
    ("2025-09-27", 120.00), ("2025-09-28", 120.00),
    ("2025-10-04", 120.00), ("2025-10-05", 120.00), ("2025-10-11", 120.00),
    ("2025-10-12", 120.00), ("2025-10-25", 120.00), ("2025-10-26", 120.00),
    ("2025-11-01", 120.00), ("2025-11-02", 140.00),
]

aid_4140 = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='4140'")).fetchone()[0]
aid_1111 = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='1111'")).fetchone()[0]

found = 0
reclassed = 0
already_4140 = 0
not_found = []

for dt, amt in GANESAN_PAYMENTS:
    # Find the GL leg that contra-posts to POSB inflow of (date, amt)
    # i.e., a journal with Dr POSB amt on that date — find its credit leg
    posb_legs = s.execute(text("""
      SELECT j.id, gl.id, gl.debit
      FROM journals j JOIN general_ledger gl ON gl.journal_id = j.id
      WHERE j.status='posted' AND j.journal_date=:d
        AND gl.account_id=:posb AND ROUND(gl.debit, 2)=:a
    """), {"d": dt, "posb": aid_1111, "a": amt}).all()
    if not posb_legs:
        not_found.append((dt, amt))
        continue
    found += 1
    for jid, _, _ in posb_legs:
        # Get the credit-side leg(s) of this journal — that's what to reclass
        legs = s.execute(text("""
          SELECT gl.id, coa.account_code, gl.credit FROM general_ledger gl
          JOIN chart_of_accounts coa ON coa.id=gl.account_id
          WHERE gl.journal_id=:j AND gl.credit > 0
        """), {"j": jid}).all()
        for gl_id, code, cr in legs:
            if code == '4140':
                already_4140 += 1
                continue
            s.execute(text("""
              UPDATE general_ledger SET account_id=:a,
                narration='Ganesan SO payment (user-confirmed 2026-05-14)'
              WHERE id=:gl
            """), {"a": aid_4140, "gl": gl_id})
            reclassed += 1
            print(f"  ↻ jid={jid} {dt} ${amt:>7,.2f}  was {code} → 4140")

s.commit()
print(f"\nSummary:")
print(f"  payments confirmed in user list:  {len(GANESAN_PAYMENTS)}")
print(f"  POSB inflows found in GL:         {found}")
print(f"  GL legs reclassified to 4140:     {reclassed}")
print(f"  already in 4140 (idempotent skip): {already_4140}")
if not_found:
    print(f"  POSB inflows NOT FOUND ({len(not_found)}):")
    for dt, amt in not_found:
        print(f"    {dt}  ${amt:,.2f}")
s.close()
