"""Reclassify the $120 × 14 Funds Transfer entries (Jun-Sep 2025) from
4900 Other Income → 4140 Salary — Ganesan.

User-confirmed 2026-05-14: these are payments from Ganesan (employer that
never paid CPF — see project_ganesan_unpaid_cpf.md). Real salary income,
not generic 'Other Income'.

Also looks for $120 patterns outside Jun-Sep 2025 in case there are more.
"""
from app import database as db
from sqlalchemy import text
db.init_db()
s = db.SessionLocal()

# Ensure 4140 exists
exists = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='4140'")).fetchone()
if not exists:
    parent = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='4100'")).fetchone()
    pid = parent[0] if parent else None
    s.execute(text("""
      INSERT INTO chart_of_accounts
        (account_code, account_name, parent_id, account_class, account_subclass,
         normal_balance, is_postable, sub_ledger_table, created_at, is_active)
      VALUES ('4140', 'Salary — Ganesan', :p,
              'REVENUE', 'OPERATING_REV', 'CREDIT', 1, NULL,
              CURRENT_TIMESTAMP, 1)
    """), {"p": pid})
    s.commit()
    print("✓ Created CoA 4140 Salary — Ganesan")
else:
    print("ℹ CoA 4140 already exists")

aid_4140 = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code='4140'")).fetchone()[0]

# Find $120 inflows in 4900 — Ganesan pattern (Jun-Sep 2025 + any others)
rows = s.execute(text("""
  SELECT gl.id, j.id, j.journal_date, gl.credit, j.narration
  FROM journals j
  JOIN general_ledger gl ON gl.journal_id = j.id
  JOIN chart_of_accounts coa ON coa.id = gl.account_id
  WHERE coa.account_code='4900' AND ROUND(gl.credit, 2) = 120.00
    AND j.status='posted'
  ORDER BY j.journal_date
""")).all()

print(f"\nFound {len(rows)} matching $120 entries in 4900:")
for r in rows:
    s.execute(text("""
      UPDATE general_ledger SET account_id=:aid,
        narration='Ganesan salary payment (regular $120 from Ganesan employer)'
      WHERE id=:gl_id
    """), {"aid": aid_4140, "gl_id": r[0]})
    print(f"  ↻ jid={r[1]} {r[2]}  $120.00")

s.commit()
total = 120.00 * len(rows)
print(f"\nMoved ${total:,.2f} from 4900 → 4140 Salary — Ganesan")
s.close()
