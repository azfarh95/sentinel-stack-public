"""Void HSBC direct journals to allow re-replay with new CC-lifestyle routing."""
from app import database as db
from app.cc_cutover import main as cc_main
from sqlalchemy import text
from datetime import date
import sys

db.init_db()
s = db.SessionLocal()
r = s.execute(text("""
    UPDATE journals SET status='voided',
        voided_at=CURRENT_TIMESTAMP,
        voided_reason='Re-replay with CC-lifestyle routing'
    WHERE source_doc='CC_PDF_DIRECT:2114' AND status='posted'
"""))
print(f"Voided {r.rowcount} HSBC direct journals")
s.commit()
s.close()

sys.argv = ['cc_cutover', '--since', '2024-01-01', '--card', 'hsbc-cc', '--post']
cc_main()
