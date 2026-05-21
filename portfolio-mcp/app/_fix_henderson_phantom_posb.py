"""YourAgency payslip journals double-count POSB inflows.
The PAYSLIP's Dr POSB leg is the phantom — actual POSB receipts are smaller
and recorded by POSB_PDF_DIRECT (as 4900 Other Income).

Fix: replace each YourAgency PAYSLIP's Dr POSB $X leg with Dr 1300 Salary
Receivable $X. The Cr 4120 YourAgency Salary income stays — but on the
Dr side, the money is now sitting as a receivable from YourAgency rather
than as POSB cash. When POSB direct receives actual payments, they're
income in 4900 — leaving 1300 as the unpaid YourAgency balance to chase.

This makes the gap visible as a growing receivable balance (per
feedback_double_entry_forcing_function.md — design accounting to make
hidden problems visible).
"""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

# Ensure 1300 Salary Receivable exists
exists = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='1300'")).fetchone()
if not exists:
    parent = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='1120'")).fetchone()
    pid = parent[0] if parent else None
    s.execute(text("""
      INSERT INTO chart_of_accounts
        (account_code, account_name, parent_id, account_class, account_subclass,
         normal_balance, is_postable, sub_ledger_table, created_at, is_active)
      VALUES ('1300', 'Salary Receivable (employer-owed)', :p,
              'ASSET', 'CURRENT_ASSET', 'DEBIT', 1, NULL,
              CURRENT_TIMESTAMP, 1)
    """), {"p": pid})
    s.commit()
    print("✓ Created CoA 1300 Salary Receivable")
else:
    print("ℹ CoA 1300 already exists")

new_aid = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='1300'")).fetchone()[0]
posb_aid = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='1111'")).fetchone()[0]

# YourAgency payslip journals
rows = s.execute(text("""
  SELECT pr.id, pr.period_end, pr.net_pay, pr.journal_id
  FROM payslip_registry pr
  WHERE pr.employer_key='youragency' AND pr.journal_id IS NOT NULL
""")).all()
for r in rows:
    pid, pe, net, jid = r[0], r[1], float(r[2] or 0), r[3]
    leg = s.execute(text("""
      SELECT gl.id, gl.debit FROM general_ledger gl
      WHERE gl.journal_id=:j AND gl.account_id=:posb AND gl.debit BETWEEN :net-0.5 AND :net+0.5
    """), {"j": jid, "posb": posb_aid, "net": net}).fetchone()
    if not leg:
        print(f"  ⚠ jid={jid} ({pe}) net=${net:,.2f} — no POSB Dr leg found")
        continue
    leg_id, leg_dr = leg
    s.execute(text("""
      UPDATE general_ledger SET account_id=:new_a,
        narration='YourAgency salary receivable (payslip says $X, actual POSB receipt is fragmented and smaller)'
      WHERE id=:lid
    """), {"new_a": new_aid, "lid": leg_id})
    print(f"  ↻ jid={jid} ({pe}) ${float(leg_dr):,.2f} — POSB leg repointed to 1300 Salary Receivable")

s.commit()
s.close()
print("\nResult: YourAgency PAYSLIP Cr 4120 income stays intact.")
print("Phantom POSB inflows removed. The gap now appears as a growing 1300 Salary Receivable balance.")
