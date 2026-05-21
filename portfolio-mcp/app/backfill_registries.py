"""Backfill statement_registry, payslip_registry, and nav_history from existing
parsed data (PDFs in OneDrive + funds.yaml).

After this runs, the bot can answer queries like:
  - "What are my 12 SC CC statement dates?" → SELECT FROM statement_registry
  - "What was my Dec 2025 gross from AZ United?" → SELECT FROM payslip_registry
  - "What was Tokio Marine ILP value at 2025-12-31?" → SELECT FROM nav_history

Idempotent — UniqueConstraints on (facility_id, statement_date) etc. let re-runs
update existing rows without dup insert.

Run:
    docker exec portfolio-mcp python -m app.backfill_registries
    docker exec portfolio-mcp python -m app.backfill_registries --only statement
    docker exec portfolio-mcp python -m app.backfill_registries --only payslip
    docker exec portfolio-mcp python -m app.backfill_registries --only nav
"""
from __future__ import annotations

import argparse
import logging
from datetime import date as _date
from pathlib import Path

from sqlalchemy import select

from . import cc_statement_parser as ccp
from . import cc_pipeline
from . import payslip_parser
from . import database as db
from . import ledger

logger = logging.getLogger(__name__)

CC_ROOT = Path("/onedrive/Sentinel Finance/02_Credit card statements")
PAYSLIP_ROOT = Path("/onedrive/Sentinel Finance/05_Payslips")
FUNDS_YAML = Path("/finance/funds.yaml")


def backfill_statement_registry(s) -> int:
    """Walk all CC PDFs, parse each, upsert statement_registry row."""
    if not CC_ROOT.exists():
        print(f"CC_Statement folder missing: {CC_ROOT}")
        return 0
    count = 0
    skipped = 0
    for pdf in CC_ROOT.rglob("*.pdf"):
        if "unsorted" in [p.name for p in pdf.parents]:
            continue
        fn = pdf.name.lower()
        if any(x in fn for x in ["application form", "consolidation", "acknowledgement",
                                  "transactionhistory", "_encrypted", "payslip", "noa ",
                                  "credit report", "loan agreement", "ml compairson"]):
            continue
        try:
            stmt = ccp.detect_and_parse(str(pdf))
        except Exception as e:
            logger.warning("backfill: parse failed %s: %s", pdf.name, e)
            skipped += 1
            continue
        if not stmt or not stmt.statement_date:
            skipped += 1
            continue
        try:
            cc_pipeline.upsert_statement_registry(s, stmt)
            s.commit()  # commit per row so SELECT in next iter sees it (avoid same-batch dup INSERTs)
            count += 1
        except Exception as e:
            s.rollback()
            logger.warning("backfill: registry upsert failed %s: %s", pdf.name, e)
            skipped += 1
    print(f"  Statements: {count} upserted, {skipped} skipped")
    return count


def backfill_payslip_registry(s) -> int:
    """Walk Payslips/, parse + upsert each."""
    if not PAYSLIP_ROOT.exists():
        print(f"Payslips folder missing: {PAYSLIP_ROOT}")
        return 0
    count = 0
    skipped = 0
    for pdf in PAYSLIP_ROOT.rglob("*.pdf"):
        fn_l = pdf.name.lower()
        if "payslip" not in fn_l and "youragency" not in fn_l:
            continue
        try:
            p = payslip_parser.detect_and_parse(str(pdf))
        except Exception as e:
            logger.warning("backfill: payslip parse failed %s: %s", pdf.name, e)
            skipped += 1
            continue
        if not p or not p.period_end:
            skipped += 1
            continue
        try:
            payslip_parser._upsert_payslip_registry(s, p, journal_id=None)
            s.commit()
            count += 1
        except Exception as e:
            s.rollback()
            logger.warning("backfill: payslip registry failed %s: %s", pdf.name, e)
            skipped += 1
    print(f"  Payslips:   {count} upserted, {skipped} skipped")
    return count


def backfill_nav_history(s) -> int:
    """Snapshot the current funds.yaml NAVs into nav_history. One row per fund
    at last_nav_date (so we capture today's known state). Subsequent
    morningstar_sg runs will append daily new rows."""
    import yaml
    try:
        cfg = yaml.safe_load(open(FUNDS_YAML))
    except Exception as e:
        print(f"  funds.yaml read failed: {e}")
        return 0
    count = 0
    now = db.now_utc()
    for f in cfg.get("funds", []):
        nav = f.get("last_nav")
        nav_date_s = f.get("last_nav_date")
        if not (nav and nav_date_s):
            continue
        try:
            from datetime import datetime
            nav_date = datetime.strptime(nav_date_s, "%Y-%m-%d").date()
        except Exception:
            continue
        existing = s.execute(
            select(ledger.NavHistory).where(
                ledger.NavHistory.fund_id == f["id"],
                ledger.NavHistory.nav_date == nav_date,
            )
        ).scalar_one_or_none()
        if existing:
            existing.nav_price = float(nav)
            existing.fund_name = f.get("name", "")
        else:
            s.add(ledger.NavHistory(
                fund_id=f["id"], fund_name=f.get("name", ""),
                nav_date=nav_date, nav_price=float(nav),
                currency=f.get("currency", "SGD"),
                source=(f.get("sources") or ["manual"])[0],
                created_at=now,
            ))
            count += 1
    s.commit()
    print(f"  NAV history: {count} new rows from funds.yaml snapshot")
    return count


def show_summary(s) -> None:
    """Quick counts for verification."""
    from sqlalchemy import func
    print("\n=== Registry summary ===")
    for label, model in [("statement_registry", ledger.StatementRegistry),
                          ("payslip_registry", ledger.PayslipRegistry),
                          ("nav_history", ledger.NavHistory)]:
        n = s.execute(select(func.count(model.id))).scalar()
        print(f"  {label:<22} {n:>5} rows")
    print()
    print("=== Statements by facility ===")
    rows = s.execute(
        select(ledger.StatementRegistry.bank,
               func.count(ledger.StatementRegistry.id),
               func.min(ledger.StatementRegistry.statement_date),
               func.max(ledger.StatementRegistry.statement_date))
        .group_by(ledger.StatementRegistry.bank)
        .order_by(ledger.StatementRegistry.bank)
    ).all()
    for bank, n, mn, mx in rows:
        print(f"  {bank:<18} {n:>3} stmts  ({mn} → {mx})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["statement", "payslip", "nav"],
                    help="Backfill only one registry (default: all three)")
    args = ap.parse_args()

    db.init_db()
    s = db.SessionLocal()
    try:
        if args.only in (None, "statement"):
            print("Backfilling statement_registry...")
            backfill_statement_registry(s)
        if args.only in (None, "payslip"):
            print("Backfilling payslip_registry...")
            backfill_payslip_registry(s)
        if args.only in (None, "nav"):
            print("Backfilling nav_history...")
            backfill_nav_history(s)
        show_summary(s)
    finally:
        s.close()


if __name__ == "__main__":
    main()
