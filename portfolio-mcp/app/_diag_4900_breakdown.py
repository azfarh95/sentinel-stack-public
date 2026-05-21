"""Anatomy of the $35k 'Other Income (4900)' in 2025 — what's in there.
Find the candidates for cashline drawdown reclassification."""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

# All 4900 credits (inflows) in 2025
rows = s.execute(text("""
  SELECT j.id, j.journal_date, j.narration, gl.credit, j.source_doc
  FROM journals j
  JOIN general_ledger gl ON gl.journal_id=j.id
  JOIN chart_of_accounts coa ON coa.id=gl.account_id
  WHERE coa.account_code='4900' AND gl.credit > 0
    AND j.status='posted'
    AND j.journal_date BETWEEN '2025-01-01' AND '2025-12-31'
  ORDER BY gl.credit DESC
""")).all()
print(f"=== 4900 inflows in 2025: {len(rows)} entries ===\n")

# Group by tx_type prefix from narration
from collections import Counter
patterns = Counter()
amounts = {}
for r in rows:
    narr = (r[2] or "").upper()
    # Extract pattern from narration
    if "FAST PAYMENT" in narr: key = "FAST PAYMENT"
    elif "GIRO" in narr: key = "GIRO"
    elif "SALARY" in narr: key = "Salary"
    elif "ADVICE" in narr: key = "Advice/Cashline"
    elif "MEPS RECEIPT" in narr: key = "MEPS Receipt"
    elif "AUTO TOP UP" in narr: key = "AUTO TOP UP FROM CASHLINE"
    elif "REMITTANCE" in narr: key = "Remittance"
    elif "INTEREST EARNED" in narr: key = "INTEREST EARNED"
    elif "DIVIDEND" in narr: key = "Dividend"
    elif "INWARD" in narr: key = "Inward Credit"
    elif "RECEIPT" in narr: key = "Receipt"
    else: key = "Other"
    patterns[key] += 1
    amounts[key] = amounts.get(key, 0) + float(r[3] or 0)

print("Pattern breakdown:")
for k, v in sorted(patterns.items(), key=lambda x: -amounts[x[0]]):
    print(f"  {k:<35} {v:>3} entries  total=${amounts[k]:>10,.2f}")
print(f"\nTotal 4900 in 2025: ${sum(amounts.values()):,.2f}")

# Top 15 individual inflows
print("\n=== Top 15 biggest individual 4900 inflows in 2025 ===")
for r in rows[:15]:
    print(f"  ${float(r[3]):>9,.2f}  {r[1]}  {(r[2] or '')[:80]}")
s.close()
