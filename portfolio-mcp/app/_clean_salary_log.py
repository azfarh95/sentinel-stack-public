"""Clean stale salary_reconcile_log entries pointing to voided journals."""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()
# Delete entries whose posb_journal_id references a voided journal
r = s.execute(text("""
    DELETE FROM salary_reconcile_log
    WHERE posb_journal_id IN (
        SELECT id FROM journals WHERE status='voided' AND source_doc='POSB_PDF_DIRECT'
    )
"""))
print(f"Cleaned {r.rowcount} stale log entries")
s.commit()
r = s.execute(text("""
    SELECT status, COUNT(*), SUM(amount), MIN(period_end), MAX(period_end)
    FROM salary_reconcile_log GROUP BY status
""")).all()
for row in r:
    print(f"  {row[0]:<18}  {row[1]:>3} rows  ${float(row[2] or 0):>10,.2f}  {row[3]}..{row[4]}")
s.close()
