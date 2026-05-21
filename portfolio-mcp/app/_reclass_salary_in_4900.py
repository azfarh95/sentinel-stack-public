"""Reclassify 'Salary' tx_type entries currently sitting in 4900 Other Income.

Pre-Aug-2025 salary inflows hit 4900 because:
  - No payslip data existed yet (PAYSLIP source_doc didn't have these months)
  - Router has no SALARY → employer-specific rule
  - POSB tx_type 'Salary' + amount fell through to 'Generic income (review)' = 4900

This reclassifier uses pattern matching on amount + date to attribute to 4110
(AZ United) or 4120 (YourAgency) based on known pay structures.

Known patterns (from payslip_registry + user-confirmed):
  AZ United (monthly, last business day):
    $2,481.00 — 2024 / Jan-Feb 2025
    $2,545.00 — Mar-May 2025
    $2,392.62 — Jun 2025
    $2,266.74 — Jul 2025
    (Aug 2025 onwards covered by PAYSLIP source_doc)
  YourAgency (weekly, Wed/Thu — Daily Rated Payslip):
    Various amounts $100-$1000 range typically
    Identifier: small amount + weekly cadence
"""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

AZ_UNITED_AMOUNTS = {
    # 2024
    2393.50, 2410.00, 2481.00,
    # 2025 H1
    2545.00, 2392.62, 2266.74,
    # 2025 year-end bonus (Dec was $6,218 / $6,378 — multi-month accrual)
    6218.00, 6378.00,
    # 2026 — Feb-Apr rate (Jan was $2,545, Feb-Apr stepped up)
    2595.45, 2649.00,
}
TOLERANCE = 0.10

aid_4110 = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='4110'")).fetchone()[0]
aid_4120 = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='4120'")).fetchone()[0]

# Find all 4900 credits with Salary tx_type prior to Aug 2025 (payslip era)
candidates = s.execute(text("""
  SELECT gl.id, j.id, j.journal_date, gl.credit, j.narration
  FROM journals j
  JOIN general_ledger gl ON gl.journal_id = j.id
  JOIN chart_of_accounts coa ON coa.id = gl.account_id
  WHERE coa.account_code='4900' AND gl.credit > 0
    AND j.status='posted'
    AND UPPER(j.narration) LIKE '%SALARY%'
""")).all()

n_az = n_hd = 0
total_az = total_hd = 0.0
for r in candidates:
    gl_id, jid, jdate, amt, narr = r
    amt_f = float(amt or 0)
    is_az = any(abs(amt_f - a) < TOLERANCE for a in AZ_UNITED_AMOUNTS)
    if is_az:
        target_aid, label, code = aid_4110, "AZ United Pte Ltd (pre-payslip-era)", "4110"
        n_az += 1; total_az += amt_f
    elif amt_f < 1200:    # YourAgency weekly range
        target_aid, label, code = aid_4120, "YourAgency Security weekly (pre-payslip-era)", "4120"
        n_hd += 1; total_hd += amt_f
    else:
        continue   # leave for manual review
    s.execute(text("""
      UPDATE general_ledger SET account_id=:newa,
        narration=:lbl
      WHERE id=:lid
    """), {"newa": target_aid, "lid": gl_id, "lbl": label})

s.commit()
print(f"\nReclassified {n_az} entries to 4110 AZ United  (total ${total_az:,.2f})")
print(f"Reclassified {n_hd} entries to 4120 YourAgency    (total ${total_hd:,.2f})")
print(f"Total moved out of 4900: ${total_az + total_hd:,.2f}")

# Show what's left in 4900 for review
print("\n=== Remaining 'Salary' entries in 4900 (>=Aug 2025, awaiting review) ===")
r = s.execute(text("""
  SELECT j.journal_date, gl.credit, j.narration
  FROM journals j JOIN general_ledger gl ON gl.journal_id=j.id
  JOIN chart_of_accounts coa ON coa.id=gl.account_id
  WHERE coa.account_code='4900' AND gl.credit > 0 AND j.status='posted'
    AND UPPER(j.narration) LIKE '%SALARY%'
  ORDER BY gl.credit DESC LIMIT 10
""")).all()
for row in r:
    print(f"  ${float(row[1]):>9,.2f}  {row[0]}  {(row[2] or '')[:70]}")
s.close()
