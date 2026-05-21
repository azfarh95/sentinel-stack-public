"""One-shot CoA migration (Pass A) — re-route journals from converted-to-header
parents onto their new 5-digit `Unallocated` children.

Pass A converted these previously-postable codes into headers:
  1214 CPF Investment Scheme   →  per-fund leaves 12141-12145 + 12149 Unallocated
  1221 Tokio Marine ILP        →  per-fund leaves 12211-12215 + 12219 Unallocated
  1222 Singlife Savvy Invest   →  per-fund leaves 12221-12223 + 12229 Unallocated

Any historical journal that posted DR/CR to 1214/1221/1222 directly would now
fail validation (header not postable). This migration rewrites those entries
to the corresponding `Unallocated` leaf so they remain valid and preserve
audit history. Future posts from `ilp_statement_parser` go to specific funds.

Idempotent — safe to re-run; UPDATEs are scoped by `account_code IN (parent_codes)`.

Run:
    docker exec portfolio-mcp python -m app.migrate_coa_pass_a              # dry-run
    docker exec portfolio-mcp python -m app.migrate_coa_pass_a --apply      # execute
"""
from __future__ import annotations

import argparse
import logging
from sqlalchemy import select, update, func

from . import database as db
from . import ledger

logger = logging.getLogger(__name__)


# Parent (now header) → Unallocated leaf code
MIGRATIONS = {
    "1214": "12149",   # CPF IS → CPF IS Unallocated
    "1221": "12219",   # Tokio → Tokio Unallocated
    "1222": "12229",   # Singlife → Singlife Unallocated
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually execute UPDATEs (default: dry-run preview)")
    args = ap.parse_args()

    s = db.SessionLocal()
    try:
        # Verify destination leaves exist
        for new in set(MIGRATIONS.values()):
            row = s.execute(select(ledger.ChartOfAccount)
                            .where(ledger.ChartOfAccount.account_code == new)).scalar_one_or_none()
            if row is None:
                print(f"ERROR: target leaf {new} not in CoA — run ledger_seed first")
                return
            if not row.is_postable:
                print(f"ERROR: target leaf {new} is not postable")
                return

        print(f"{'old code':<10} {'new code':<10} {'GL entries':>10} {'Action'}")
        print("-" * 60)
        total = 0
        for old, new in MIGRATIONS.items():
            n = s.execute(select(func.count(ledger.GeneralLedgerEntry.id))
                          .where(ledger.GeneralLedgerEntry.account_code == old)).scalar() or 0
            old_name = s.execute(select(ledger.ChartOfAccount.account_name)
                                 .where(ledger.ChartOfAccount.account_code == old)).scalar() or "?"
            new_name = s.execute(select(ledger.ChartOfAccount.account_name)
                                 .where(ledger.ChartOfAccount.account_code == new)).scalar() or "?"
            action = "DRY-RUN" if not args.apply else f"REROUTED"
            print(f"  {old:<10} → {new:<10} {n:>10}  {action}  ({old_name} → {new_name})")
            if args.apply and n > 0:
                s.execute(update(ledger.GeneralLedgerEntry)
                          .where(ledger.GeneralLedgerEntry.account_code == old)
                          .values(account_code=new))
            total += n

        if args.apply:
            s.commit()
            print(f"\n  ✓ {total} GL entries rerouted to Unallocated leaves")
        else:
            print(f"\n  DRY-RUN — would reroute {total} entries. Pass --apply to execute.")
    finally:
        s.close()


if __name__ == "__main__":
    main()
