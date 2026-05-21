"""Void [suspense cleanup] journal duplicates — they triple-count receipts that
the original POSB direct journal already records."""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

# Identify all SUSPENSE_CLEANUP source_doc journals that have a same-date same-amount
# counterpart in POSB_PDF_DIRECT
dups = s.execute(text("""
  SELECT DISTINCT sc.id FROM journals sc
  JOIN general_ledger gl_sc ON gl_sc.journal_id = sc.id
  JOIN chart_of_accounts coa_sc ON coa_sc.id = gl_sc.account_id AND coa_sc.account_code = '4900'
  JOIN journals pd ON pd.journal_date = sc.journal_date AND pd.id != sc.id
  JOIN general_ledger gl_pd ON gl_pd.journal_id = pd.id
  JOIN chart_of_accounts coa_pd ON coa_pd.id = gl_pd.account_id AND coa_pd.account_code = '4900'
  WHERE sc.source_doc = 'SUSPENSE_CLEANUP' AND sc.status = 'posted'
    AND pd.source_doc = 'POSB_PDF_DIRECT' AND pd.status = 'posted'
    AND ABS(gl_sc.credit - gl_pd.credit) < 0.01
""")).all()
ids = [r[0] for r in dups]
print(f"Voiding {len(ids)} SUSPENSE_CLEANUP duplicate journals")
for jid in ids:
    s.execute(text("""
      UPDATE journals SET status='voided',
        voided_at=CURRENT_TIMESTAMP,
        voided_reason='reconcile: superseded by POSB_PDF_DIRECT (suspense cleanup retired)'
      WHERE id=:jid
    """), {"jid": jid})
s.commit()

# Show what's left in 4900 in 2025
totals = s.execute(text("""
  SELECT SUM(gl.credit), COUNT(DISTINCT j.id) FROM journals j
  JOIN general_ledger gl ON gl.journal_id=j.id
  JOIN chart_of_accounts coa ON coa.id=gl.account_id
  WHERE coa.account_code='4900' AND gl.credit > 0 AND j.status='posted'
    AND j.journal_date BETWEEN '2025-01-01' AND '2025-12-31'
""")).fetchone()
print(f"\n2025 4900 Other Income now: ${float(totals[0] or 0):,.2f} across {totals[1]} journals")
s.close()
