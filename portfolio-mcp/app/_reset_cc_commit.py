"""One-shot: reset all cc_statement_commitment statuses + matcher-derived fields
so run_matcher() can re-evaluate from scratch under the new cumulative-payment logic.
"""
from app import database as db
from app.cc_commitment_tracker import ensure_table
from sqlalchemy import text

db.init_db()
s = db.SessionLocal()
ensure_table(s)
n = s.execute(text("SELECT COUNT(*) FROM cc_statement_commitment")).scalar()
s.execute(text("""
    UPDATE cc_statement_commitment
    SET status='pending',
        cumulative_paid=0,
        unpaid_balance=NULL,
        payments_jids=NULL,
        estimated_interest=NULL,
        interest_warning=NULL,
        matched_payment_jid=NULL,
        matched_at=NULL,
        match_amount=NULL,
        days_offset=NULL
"""))
s.commit()
print(f"reset {n} commitments to pending")
s.close()
