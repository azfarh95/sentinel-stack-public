"""Confirm YourAgency POSB inflows DO exist in the GL — the salary reconciler
just missed them because its narration filter is 'SALARY' only.
"""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

# YourAgency payslips
print("=== YourAgency payslips ===")
rows = s.execute(text("""
  SELECT pr.id, pr.period_end, pr.payment_date, pr.net_pay, pr.journal_id
  FROM payslip_registry pr WHERE pr.employer_key='youragency'
  ORDER BY pr.period_end
""")).all()
for r in rows:
    pid, pe, pd, net, jid = r[0], r[1], r[2], float(r[3] or 0), r[4]
    # Look for matching POSB Dr leg in [pd-5d, pd+7d] with amount ±0.50
    matches = s.execute(text("""
      SELECT j.id, j.journal_date, gl.debit, j.narration
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id=j.id
      JOIN chart_of_accounts coa ON coa.id=gl.account_id
      WHERE coa.account_code='1111'
        AND j.source_doc='POSB_PDF_DIRECT' AND j.status='posted'
        AND gl.debit BETWEEN :amt-0.5 AND :amt+0.5
        AND j.journal_date BETWEEN date(:pd, '-5 days') AND date(:pd, '+7 days')
    """), {"amt": net, "pd": str(pd)[:10]}).all()
    print(f"\n  payslip {pid} period={pe} pay_date={pd} net=${net:,.2f}  (payslip jid={jid})")
    if not matches:
        print("    no POSB match found in window")
    for m in matches:
        print(f"    ↪ jid={m[0]} {m[1]}  Dr=${float(m[2]):,.2f}  {(m[3] or '')[:70]}")
s.close()
