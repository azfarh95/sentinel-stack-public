"""Scope what FIREFLY_BRIDGE journals exist for 2024-2025.
Counts by (year, source_doc, top-level account) — gives us a clear picture of
what we need to void+replay during historical cutover.
"""
from app import database as db
from sqlalchemy import text

db.init_db()
s = db.SessionLocal()

# Total bridge journals 2024-2025
total = s.execute(text("""
    SELECT COUNT(*), SUM(CASE WHEN status='posted' THEN 1 ELSE 0 END) as posted
    FROM journals
    WHERE journal_date BETWEEN '2024-01-01' AND '2025-12-31'
      AND source_doc LIKE 'FIREFLY_BRIDGE%'
""")).fetchone()
print(f"\nFIREFLY_BRIDGE journals 2024-2025: total={total[0]}, posted={total[1]}")

# By source_doc + year
print("\n=== By source_doc + year ===")
rows = s.execute(text("""
    SELECT
        source_doc,
        SUBSTR(CAST(journal_date AS TEXT), 1, 4) as year,
        COUNT(*) as n,
        SUM(CASE WHEN status='posted' THEN 1 ELSE 0 END) as posted_n
    FROM journals
    WHERE journal_date BETWEEN '2024-01-01' AND '2025-12-31'
      AND source_doc LIKE 'FIREFLY_BRIDGE%'
    GROUP BY source_doc, year
    ORDER BY source_doc, year
""")).all()
for r in rows:
    print(f"  {r[0]:<60} {r[1]}  n={r[2]:>5}  posted={r[3]:>5}")

# By account (top-level)
print("\n=== Posted FIREFLY_BRIDGE 2024-2025 by account ===")
rows = s.execute(text("""
    SELECT
        coa.account_code, coa.account_name,
        SUBSTR(CAST(j.journal_date AS TEXT), 1, 4) as year,
        COUNT(DISTINCT j.id) as n_journals,
        SUM(gl.debit) as total_dr,
        SUM(gl.credit) as total_cr
    FROM journals j
    JOIN general_ledger gl ON gl.journal_id = j.id
    JOIN chart_of_accounts coa ON coa.id = gl.account_id
    WHERE j.journal_date BETWEEN '2024-01-01' AND '2025-12-31'
      AND j.source_doc LIKE 'FIREFLY_BRIDGE%'
      AND j.status = 'posted'
      AND coa.account_code IN ('1111','1114','1115','2111','2112','2113','2114','2121')
    GROUP BY coa.account_code, year
    ORDER BY coa.account_code, year
""")).all()
for r in rows:
    print(f"  {r[0]} {r[1][:28]:<28} {r[2]} {r[3]:>5} jids  Dr=${float(r[4] or 0):>12,.2f}  Cr=${float(r[5] or 0):>12,.2f}")

s.close()
