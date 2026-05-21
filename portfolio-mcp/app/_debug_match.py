"""Why is recurring_reconciler matching 0?"""
from app import database as db
from sqlalchemy import text
import json
import re

db.init_db()
s = db.SessionLocal()

# Sample a known Tokio Marine candidate ($418.45)
rows = s.execute(text("""
    SELECT j.id, j.journal_date, j.narration, gl.debit, gl.narration as gln
    FROM journals j
    JOIN general_ledger gl ON gl.journal_id = j.id
    JOIN chart_of_accounts coa ON coa.id = gl.account_id
    WHERE j.status='posted' AND j.source_doc='POSB_PDF_DIRECT'
      AND coa.account_code IN ('1190','4900')
      AND ROUND(gl.debit, 2) = 418.45
    LIMIT 3
""")).all()
print("Sample $418.45 candidates:")
for r in rows:
    print(f"  jid={r[0]}  {r[1]}  debit=${float(r[3] or 0):,.2f}")
    print(f"    j.narration: '{r[2]}'")
    print(f"    gl.narration: '{r[4]}'")

# What's in the obligations table?
obs = s.execute(text("SELECT name, expected_amount, identifier_patterns FROM recurring_obligation_registry WHERE name LIKE '%Tokio%'")).fetchone()
print(f"\nTokio registry row:")
print(f"  expected_amount: {obs[1]}")
print(f"  patterns: {obs[2]}")
# Try match
narr_sample = rows[0][2] if rows else ""
print(f"\nNarration for matching: '{narr_sample}'")
print(f"  uppercase: '{narr_sample.upper()}'")
for pat in json.loads(obs[2] or "[]"):
    found = re.search(pat, narr_sample.upper(), re.IGNORECASE) is not None
    print(f"  pattern '{pat}' → match? {found}")
s.close()
