"""Repair: deposit transactions that were incorrectly routed to 5190 Lifestyle.

Two passes:
  1. POSB Dr $120 or $140 in Jun-Nov 2025 → 4140 Ganesan (user-confirmed all of
     these in this window are Ganesan)
  2. Any remaining POSB-Dr journals whose CREDIT-side is on 5190 → reroute to
     4900 Other Income (unknown deposit; not lifestyle)
"""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()
aid_4140 = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='4140'")).fetchone()[0]
aid_4900 = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='4900'")).fetchone()[0]
aid_5190 = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='5190'")).fetchone()[0]
aid_1111 = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='1111'")).fetchone()[0]

# Pass 1: Ganesan window
print("=== Pass 1: Ganesan deposits (Jun-Nov 2025, $120/$140) ===")
journals = s.execute(text("""
  SELECT DISTINCT j.id, j.journal_date, gl_dr.debit
  FROM journals j
  JOIN general_ledger gl_dr ON gl_dr.journal_id=j.id
                AND gl_dr.account_id=:posb
                AND ROUND(gl_dr.debit, 2) IN (120.00, 140.00)
  WHERE j.status='posted'
    AND j.journal_date BETWEEN '2025-06-01' AND '2025-11-15'
""" ), {"posb": aid_1111}).all()
n1 = 0
for jid, jd, dr in journals:
    legs = s.execute(text("""
      SELECT gl.id, gl.account_id FROM general_ledger gl
      WHERE gl.journal_id=:j AND gl.credit > 0
    """), {"j": jid}).all()
    for gl_id, aid in legs:
        if aid == aid_4140: continue
        s.execute(text("""
          UPDATE general_ledger SET account_id=:a,
            narration='Ganesan SO payment'
          WHERE id=:gl
        """), {"a": aid_4140, "gl": gl_id})
        n1 += 1
s.commit()
print(f"  Reclassified {n1} legs to 4140 Salary — Ganesan")

# Pass 2: any remaining POSB-inflow journals whose contra is 5190 → 4900
print("\n=== Pass 2: deposits sitting in 5190 → repoint to 4900 Other Income ===")
journals = s.execute(text("""
  SELECT DISTINCT j.id, gl_cr.id, gl_dr.debit
  FROM journals j
  JOIN general_ledger gl_dr ON gl_dr.journal_id=j.id
                              AND gl_dr.account_id=:posb AND gl_dr.debit > 0
  JOIN general_ledger gl_cr ON gl_cr.journal_id=j.id
                              AND gl_cr.account_id=:l5190 AND gl_cr.credit > 0
  WHERE j.status='posted'
"""), {"posb": aid_1111, "l5190": aid_5190}).all()
n2 = total2 = 0
for jid, gl_cr_id, dr in journals:
    s.execute(text("""
      UPDATE general_ledger SET account_id=:a,
        narration='Other income (was wrongly routed to 5190 Lifestyle — deposit not expense)'
      WHERE id=:gl
    """), {"a": aid_4900, "gl": gl_cr_id})
    n2 += 1; total2 += float(dr)
s.commit()
print(f"  Repointed {n2} legs (${total2:,.2f}) from 5190 → 4900 Other Income")
s.close()
