"""Compute current classification hit-rate across all direct (post-cutover) journals."""
from app import database as db
from sqlalchemy import text

db.init_db()
s = db.SessionLocal()

# Hit rate = % of direct (POSB / CC) journals NOT landing in 1190 or 4900
sources = ['POSB_PDF_DIRECT', 'CC_PDF_DIRECT:2111', 'CC_PDF_DIRECT:2112',
           'CC_PDF_DIRECT:2114', 'SC_PDF_DIRECT', 'MAYBANK_PDF_DIRECT']

for src in sources:
    rows = s.execute(text("""
        SELECT
            coa.account_code,
            COUNT(*) as n
        FROM journals j
        JOIN general_ledger gl ON gl.journal_id = j.id
        JOIN chart_of_accounts coa ON coa.id = gl.account_id
        WHERE j.status = 'posted' AND j.source_doc = :src
          AND coa.account_code NOT IN ('1111','1114','1115','2111','2112','2113','2114')
        GROUP BY coa.account_code
    """), {"src": src}).all()
    if not rows: continue
    total = sum(r[1] for r in rows)
    suspense = sum(r[1] for r in rows if r[0] in ('1190', '4900'))
    classified = total - suspense
    hit = (100 * classified / total) if total else 0
    print(f"{src:<28}  n={total:>5}  suspense={suspense:>4} ({100-hit:.1f}%)  classified={classified:>4} ({hit:.1f}%)")

# Cross-cut: ALL direct-path journals combined
print("\n=== Overall direct-path hit rate ===")
row = s.execute(text("""
    SELECT
        SUM(CASE WHEN coa.account_code IN ('1190','4900') THEN 1 ELSE 0 END) as suspense_n,
        SUM(CASE WHEN coa.account_code NOT IN ('1190','4900','1111','1114','1115','2111','2112','2113','2114') THEN 1 ELSE 0 END) as classified_n,
        COUNT(*) as total_n
    FROM journals j
    JOIN general_ledger gl ON gl.journal_id = j.id
    JOIN chart_of_accounts coa ON coa.id = gl.account_id
    WHERE j.status = 'posted'
      AND j.source_doc LIKE '%_DIRECT%'
      AND coa.account_code NOT IN ('1111','1114','1115','2111','2112','2113','2114')
""")).fetchone()
suspense, classified, total = row[0] or 0, row[1] or 0, row[2] or 0
print(f"  suspense (1190/4900):  {suspense:>5}  ({100*suspense/total:.1f}%)")
print(f"  specifically classified: {classified:>5}  ({100*classified/total:.1f}%)")
print(f"  TOTAL direct contra-legs: {total:>5}")

# By year
print("\n=== By year ===")
rows = s.execute(text("""
    SELECT
        SUBSTR(CAST(j.journal_date AS TEXT), 1, 4) as year,
        SUM(CASE WHEN coa.account_code IN ('1190','4900') THEN 1 ELSE 0 END) as suspense_n,
        COUNT(*) as total_n
    FROM journals j
    JOIN general_ledger gl ON gl.journal_id = j.id
    JOIN chart_of_accounts coa ON coa.id = gl.account_id
    WHERE j.status = 'posted'
      AND j.source_doc LIKE '%_DIRECT%'
      AND coa.account_code NOT IN ('1111','1114','1115','2111','2112','2113','2114')
    GROUP BY year ORDER BY year
""")).all()
for r in rows:
    yr, susp, tot = r[0], r[1] or 0, r[2] or 0
    print(f"  {yr}  {tot:>5} contra-legs  suspense={susp:>4} ({100*susp/tot:.1f}%)  classified hit={100-100*susp/tot:.1f}%")

s.close()
