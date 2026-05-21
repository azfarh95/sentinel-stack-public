"""Add 1116 DBS Account (YourAgency destination) and repoint YourAgency payslip
POSB legs to it.

YourAgency Security pays salary into a DBS account that is NOT POSB Savings
(per user 2026-05-14). Payslip parser had assumed POSB → wrong.
"""
from app import database as db
from sqlalchemy import text
from datetime import datetime
db.init_db()
s = db.SessionLocal()

# 1. Insert new CoA leaf 1116 if not exists
exists = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='1116'")).fetchone()
if not exists:
    parent = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='1110'")).fetchone()
    parent_id = parent[0] if parent else None
    s.execute(text("""
      INSERT INTO chart_of_accounts
        (account_code, account_name, parent_id, account_class, account_subclass,
         normal_balance, is_postable, sub_ledger_table, created_at)
      VALUES ('1116', 'DBS Account (YourAgency destination)', :p,
              'ASSET', 'CURRENT_ASSET', 'DEBIT', 1, NULL, CURRENT_TIMESTAMP)
    """), {"p": parent_id})
    s.commit()
    print("✓ Created CoA 1116 DBS Account (YourAgency destination)")
else:
    print("ℹ CoA 1116 already exists")

# 2. Find YourAgency payslip journals
rows = s.execute(text("""
  SELECT pr.id, pr.period_end, pr.net_pay, pr.journal_id
  FROM payslip_registry pr
  WHERE pr.employer_key='youragency' AND pr.journal_id IS NOT NULL
  ORDER BY pr.period_end
""")).all()
print(f"\nYourAgency payslips: {len(rows)}")
new_aid_row = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='1116'")).fetchone()
posb_aid_row = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='1111'")).fetchone()
new_aid = new_aid_row[0]; posb_aid = posb_aid_row[0]

# 3. For each YourAgency payslip journal, find the POSB Dr leg and repoint to 1116
for r in rows:
    jid = r[3]
    pdate = r[1]
    net = float(r[2] or 0)
    leg = s.execute(text("""
      SELECT gl.id, gl.debit FROM general_ledger gl
      WHERE gl.journal_id=:j AND gl.account_id=:posb AND gl.debit>:net-0.5 AND gl.debit<:net+0.5
    """), {"j": jid, "posb": posb_aid, "net": net}).fetchone()
    if not leg:
        print(f"  ⚠ jid={jid} ({pdate}): no POSB Dr leg matching net={net} — skipping")
        continue
    leg_id, leg_dr = leg
    s.execute(text("""
      UPDATE general_ledger SET account_id=:newa,
        narration = 'Net pay: YourAgency Security → DBS account (was POSB)'
      WHERE id=:lid
    """), {"newa": new_aid, "lid": leg_id})
    print(f"  ↻ jid={jid} ({pdate}) net=${float(leg_dr):>8,.2f} — POSB leg repointed to 1116")

s.commit()
s.close()
