"""Void all POSB_PDF_DIRECT journals (so re-replay actually re-classifies)
then run the cutover. After: re-apply salary_reconciler --fix-dups.
"""
from app import database as db
from app.posb_cutover_2026 import main as cutover_main, void_firefly_after_cutover, replay_direct
from app.salary_reconciler import fix_dups, _ensure_log_table
from sqlalchemy import text
import sys
from datetime import date, datetime

db.init_db()
s = db.SessionLocal()

# Step 1: void all POSB_PDF_DIRECT that are still posted (their ext_ids will
# clear the idempotent guard so a fresh replay can post new versions with the
# updated router rules).
r = s.execute(text("""
    UPDATE journals SET status='voided',
        voided_at=CURRENT_TIMESTAMP,
        voided_reason='Router rule update — re-classifying'
    WHERE source_doc='POSB_PDF_DIRECT' AND status='posted'
"""))
print(f"Voided {r.rowcount} POSB_PDF_DIRECT journals")
s.commit()
s.close()

# Step 2: run cutover (it'll re-classify each tx with the new router)
sys.argv = ['posb_cutover_2026', '--since', '2024-01-01', '--post']
cutover_main()

# Step 3: re-fix salary dups (POSB journals have new IDs now)
s = db.SessionLocal()
_ensure_log_table(s)
# Clear old matched_dup log entries that point to now-voided journals
s.execute(text("DELETE FROM salary_reconcile_log WHERE status='matched_dup'"))
s.commit()
n = fix_dups(s, dry=False)
print(f"\nRe-applied salary fix-dups: {n} new duplicates voided")
s.close()
