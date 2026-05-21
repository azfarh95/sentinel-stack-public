"""Sweep all $120 or $140 POSB inflows in Jun-Nov 2025 that aren't yet in 4140
and reclassify to Ganesan. User confirmed all such payments in this window are
from Ganesan."""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()
aid_4140 = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='4140'")).fetchone()[0]
aid_1111 = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='1111'")).fetchone()[0]

# Find POSB-Dr journals with $120 or $140 in Jun-Nov 2025 where the credit-side
# leg is NOT yet on 4140
rows = s.execute(text("""
  SELECT DISTINCT j.id, j.journal_date, gl_dr.debit
  FROM journals j
  JOIN general_ledger gl_dr ON gl_dr.journal_id = j.id AND gl_dr.account_id = :posb
                              AND ROUND(gl_dr.debit, 2) IN (120.00, 140.00)
  JOIN general_ledger gl_cr ON gl_cr.journal_id = j.id AND gl_cr.credit > 0
                              AND gl_cr.account_id != :aid4140
  WHERE j.status='posted'
    AND j.journal_date BETWEEN '2025-06-01' AND '2025-11-15'
  ORDER BY j.journal_date
"""), {"posb": aid_1111, "aid4140": aid_4140}).all()
print(f"Remaining candidates: {len(rows)} journals")

n = 0
for jid, dt, dr in rows:
    legs = s.execute(text("""
      SELECT gl.id, coa.account_code, gl.credit FROM general_ledger gl
      JOIN chart_of_accounts coa ON coa.id=gl.account_id
      WHERE gl.journal_id=:j AND gl.credit > 0
    """), {"j": jid}).all()
    for gl_id, code, cr in legs:
        if code == '4140': continue
        s.execute(text("""
          UPDATE general_ledger SET account_id=:a,
            narration='Ganesan SO payment (weekend-processed)'
          WHERE id=:gl
        """), {"a": aid_4140, "gl": gl_id})
        n += 1
        print(f"  ↻ jid={jid} {dt} ${float(dr):>7,.2f}  was {code} → 4140")
s.commit()
print(f"\nReclassified {n} additional legs to 4140")

# Final total in 4140
total = s.execute(text("""
  SELECT SUM(gl.credit) FROM journals j
  JOIN general_ledger gl ON gl.journal_id=j.id
  JOIN chart_of_accounts coa ON coa.id=gl.account_id
  WHERE coa.account_code='4140' AND gl.credit > 0 AND j.status='posted'
""")).fetchone()
print(f"\n4140 Salary — Ganesan total income: ${float(total[0] or 0):,.2f}")
s.close()
