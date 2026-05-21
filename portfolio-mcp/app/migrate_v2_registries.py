"""Idempotent migration — create the v2 canonical-registry tables in /data/portfolio.db.

Tables created (if missing):
  - insurance_policy_registry
  - ilp_portfolio_snapshot
  - subscription_registry
  - cpf_statement_registry
  - unreconciled_queue

Run:
    docker exec portfolio-mcp python -m app.migrate_v2_registries
"""
from app import database as db, ledger
from sqlalchemy import inspect

db.init_db()
insp = inspect(db.engine)
existing = set(insp.get_table_names())

# All models we want to ensure exist
WANT = [
    ledger.InsurancePolicyRegistry,
    ledger.IlpPortfolioSnapshot,
    ledger.SubscriptionRegistry,
    ledger.CpfStatementRegistry,
    ledger.UnreconciledQueue,
]

created = 0
for model in WANT:
    name = model.__tablename__
    if name in existing:
        print(f"  ✓ {name:<35}  (exists)")
        continue
    model.__table__.create(db.engine, checkfirst=True)
    print(f"  + {name:<35}  (created)")
    created += 1

print(f"\nDone — {created} new tables created, {len(WANT) - created} already present.")
