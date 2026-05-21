"""Re-cut everything for 2026 with the fixed transfer-pair ext_id convention.

Voids the duplicate-laden journals from earlier runs:
  - POSB_PDF_DIRECT (had wrong ext_id format for own-account flows)
  - CC_PDF_DIRECT:2111 (some duplicate the POSB→CC payment)
  - SC_PDF_DIRECT (only 3 entries, void to re-run cleanly)
  - MAYBANK_PDF_DIRECT (only 7 entries, same)
  - SUSPENSE_MATCH_CC (old reclassifier — obsolete now)

Then the user runs the cutovers in order:
  1. POSB    (authoritative for POSB tx)
  2. Maybank Ar Rihla
  3. SC SuperSalary
  4. CC (DBS / Maybank / HSBC) — idempotent-skips POSB-side payments
"""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()
sources_to_void = [
    "POSB_PDF_DIRECT",
    "CC_PDF_DIRECT:2111",
    "CC_PDF_DIRECT:2112",
    "CC_PDF_DIRECT:2113",
    "CC_PDF_DIRECT:2114",
    "SC_PDF_DIRECT",
    "MAYBANK_PDF_DIRECT",
    "SUSPENSE_MATCH_CC",
]
for src in sources_to_void:
    r = s.execute(text("""
      UPDATE journals
      SET status='voided', voided_at=CURRENT_TIMESTAMP,
          voided_reason='Re-cut with fixed transfer-pair ext_id convention'
      WHERE source_doc=:src AND status='posted'
    """), {"src": src})
    print(f"  voided {r.rowcount} {src}")
s.commit()
s.close()
