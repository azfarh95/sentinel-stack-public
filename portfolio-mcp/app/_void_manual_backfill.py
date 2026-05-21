"""Void MANUAL_BACKFILL_* journals that have an exact same-day same-amount
duplicate in POSB_PDF_DIRECT. POSB cutover is the source of truth now.

The manual entries originally split principal vs interest (e.g., Sands $530.19
= $490.98 principal + $39.21 interest). That split detail is lost when we void
them; the POSB_PDF_DIRECT versions treat the full $530.19 as principal-to-2223.
To recover the split later: add an enrichment pass to recurring_reconciler
that recognizes loan_pay journal_kind + splits POSB→2222/2223 into principal +
5430 interest by registry-known interest rates.
"""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

# Identify manual backfill journals with a POSB direct same-day same-amount peer
dups = s.execute(text("""
  SELECT DISTINCT m.id
  FROM journals m
  JOIN general_ledger gl_m ON gl_m.journal_id = m.id
  JOIN chart_of_accounts coa_m ON coa_m.id = gl_m.account_id AND coa_m.account_code='1111'
  JOIN journals p ON p.journal_date = m.journal_date AND p.id != m.id
  JOIN general_ledger gl_p ON gl_p.journal_id = p.id
  JOIN chart_of_accounts coa_p ON coa_p.id = gl_p.account_id AND coa_p.account_code='1111'
  WHERE m.source_doc LIKE 'MANUAL_BACKFILL%' AND m.status='posted'
    AND p.source_doc = 'POSB_PDF_DIRECT' AND p.status='posted'
    AND ABS((gl_m.debit + gl_m.credit) - (gl_p.debit + gl_p.credit)) < 0.50
""")).all()
jids = [r[0] for r in dups]
print(f"Will void {len(jids)} MANUAL_BACKFILL journals (POSB direct counterpart exists)")
for jid in jids:
    s.execute(text("""
      UPDATE journals SET status='voided',
        voided_at=CURRENT_TIMESTAMP,
        voided_reason='reconcile_year: superseded by POSB_PDF_DIRECT (manual stop-gap retired 2026-05-14)'
      WHERE id=:jid
    """), {"jid": jid})
s.commit()

# Show remaining MANUAL_BACKFILL not auto-voided (might still need review)
remaining = s.execute(text("""
  SELECT j.id, j.journal_date, j.narration
  FROM journals j WHERE j.source_doc LIKE 'MANUAL_BACKFILL%' AND j.status='posted'
""")).all()
print(f"\nRemaining MANUAL_BACKFILL still posted: {len(remaining)}")
for r in remaining:
    print(f"  jid={r[0]:<5} {r[1]} {(r[2] or '')[:70]}")
s.close()
