"""CC statement pipeline: walk OneDrive/Sentinel Finance/CC_Statement, parse, post journals.

Each statement line item → 1 journal entry:
  - charge:        DR Expense (classifier-mapped), CR CC Liability
  - interest:      DR Finance Cost (5410/5430/5440 per facility), CR CC Liability
  - fee / annual:  DR 5450 or 5460, CR CC Liability
  - payment:       SKIP (already in GL via Firefly bridge POSB-side)

Idempotent: each line carries a stable hash; re-runs skip already-posted.

Run:
    docker exec portfolio-mcp python -m app.cc_pipeline
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections import defaultdict
from datetime import date as _date
from pathlib import Path

from sqlalchemy import select

from . import classifier as _cl
from . import cc_statement_parser as p
from . import database as db
from . import journal_service as js
from . import ledger

logger = logging.getLogger(__name__)

CC_STATEMENT_ROOT = Path("/onedrive/Sentinel Finance/02_Credit card statements")


# Map classifier category → expense CoA code (subset for CC charges)
CLASSIFIER_CATEGORY_TO_COA: dict[str, str] = {
    "F&B": "5110", "F&B (delivery)": "5111",
    "Groceries": "5120",
    "Transport": "5130", "Transport (Public)": "5131", "Transport (Fuel)": "5132",
    "Subscriptions": "5200",
    "Utilities - Internet": "5141", "Utilities - Mobile": "5142", "Utilities - Electricity": "5143",
    "Healthcare": "5150",
    "Shopping": "5160", "Shopping (online)": "5161",
    "Family expense": "5170",
    "General Expense": "5190",
    "Insurance - Life": "5340", "Insurance - Term Life": "5310",
    "Insurance - CI": "5320", "Insurance - Health": "5330",
    "Bank fees": "5700",
    "Government fees": "5600",
    "Tax": "5500",
    "Investment Fees": "5460",
    "Crypto purchase": "1231",   # asset, not expense
}


# Finance-cost destination per facility (for interest charges from statements)
INTEREST_COA_PER_FACILITY = {
    "2111": "5410",  # DBS CC → CC Interest
    "2112": "5410",  # Maybank CC
    "2113": "5410",  # SC CC
    "2114": "5410",  # HSBC CC
    "2121": "5440",  # DBS Cashline → OD Interest
    "2122": "5440",  # UOB CashPlus
    "2211": "5420",  # SC Loan/BT → Term Loan Interest
    "2212": "5420",  # GXS FlexiLoan
    "2213": "5420",  # Maybank CreditAble
    "2221": "5430",  # EZ Loan → Moneylender Interest
    "2222": "5430",  # Lending Bee
    "2223": "5430",  # Sands
}


def expense_coa_for_line(line: p.StatementLine) -> str:
    """Map a statement line description → expense CoA via classifier."""
    m = _cl.lookup(line.description)
    if m and m.category in CLASSIFIER_CATEGORY_TO_COA:
        return CLASSIFIER_CATEGORY_TO_COA[m.category]
    # Common keyword fallbacks
    d = line.description.lower()
    if "foodpanda" in d or "grabfood" in d or "deliveroo" in d:
        return "5111"
    if "grab" in d or "tada" in d or "comfort" in d:
        return "5130"
    if "coinbase" in d:
        return "1231"  # asset purchase (crypto)
    if "shopee" in d:
        return "5160"
    if "lazada" in d:
        return "5161"
    if "anthropic" in d or "claude" in d or "microsoft" in d or "telegram" in d:
        return "5200"
    return "5190"  # General Expense fallback


def post_line(s, stmt: p.ParsedStatement, line: p.StatementLine,
              override_facility: str | None = None) -> int | None:
    """Post one journal for one statement line. Returns journal_id or None if skipped."""
    if line.amount == 0:
        return None
    if line.kind == "payment":
        # Already in GL via Firefly bridge (POSB → debt service)
        return None

    facility_coa = override_facility or stmt.facility_coa_code

    # Determine other-leg account
    if line.kind == "charge":
        other_coa = expense_coa_for_line(line)
    elif line.kind == "interest":
        other_coa = INTEREST_COA_PER_FACILITY.get(facility_coa, "5410")
    elif line.kind in ("fee", "annual_fee"):
        other_coa = "5460"  # Processing/Annual Fees
    else:
        other_coa = "5190"  # General fallback

    amount = abs(line.amount)
    if line.amount < 0 and line.kind == "charge":
        # Refund: reverse — DR Liability, CR Expense
        lines = [
            {"account_code": facility_coa, "debit": amount,
             "narration": f"[refund] {line.description}",
             "sub_ledger_table": "credit_facilities",
             "sub_ledger_event": f"refund_line:{line.line_no}"},
            {"account_code": other_coa, "credit": amount,
             "narration": f"[refund] {line.description}"},
        ]
    elif other_coa.startswith("1") and line.kind == "charge":
        # Asset purchase (e.g. crypto on CC): DR Asset, CR Liability
        lines = [
            {"account_code": other_coa, "debit": amount,
             "narration": f"[asset] {line.description}"},
            {"account_code": facility_coa, "credit": amount,
             "narration": f"CC charge: {line.description}",
             "sub_ledger_table": "credit_facilities",
             "sub_ledger_event": f"charge_line:{line.line_no}"},
        ]
    else:
        # Standard: DR Expense, CR Liability (increases debt)
        lines = [
            {"account_code": other_coa, "debit": amount,
             "narration": f"{line.kind}: {line.description}"},
            {"account_code": facility_coa, "credit": amount,
             "narration": f"CC {line.kind}: {line.description}",
             "sub_ledger_table": "credit_facilities",
             "sub_ledger_event": f"{line.kind}_line:{line.line_no}"},
        ]

    jdate = line.posted_date or line.txn_date or stmt.statement_date or _date.today()
    stmt_id = stmt.statement_id()
    ext = f"cc_stmt:{line.hash_id(stmt_id)}"
    try:
        jid = js.post_journal(
            s,
            journal_date=jdate,
            narration=f"[{stmt.bank}] {line.description[:80]}",
            journal_type="general" if line.kind == "charge" else "general",
            lines=lines,
            source_doc=f"CC_STMT:{stmt.bank}",
            source_ref=stmt_id,
            external_id=ext,
        )
        return jid
    except Exception as e:
        logger.warning("Failed to post line %d of %s: %s", line.line_no, stmt_id, e)
        return None


def upsert_statement_registry(s, stmt: p.ParsedStatement) -> None:
    """Write/update statement_registry row with parsed header metadata.
    Idempotent — unique on (facility_id, statement_date). Called before line posting."""
    from . import ledger
    from sqlalchemy import select
    import json
    if not stmt.statement_date:
        return
    facility_id = stmt.bank  # close enough until we resolve to credit_facilities.id properly
    existing = s.execute(
        select(ledger.StatementRegistry).where(
            ledger.StatementRegistry.facility_id == facility_id,
            ledger.StatementRegistry.statement_date == stmt.statement_date,
        )
    ).scalar_one_or_none()
    now = db.now_utc()
    extras_json = json.dumps(stmt.extras) if stmt.extras else None
    src_path = stmt.source_path.replace("/onedrive/Sentinel Finance/", "") if stmt.source_path else None
    fields = dict(
        facility_id=facility_id, bank=stmt.bank,
        statement_date=stmt.statement_date,
        previous_balance=stmt.previous_balance, closing_balance=stmt.closing_balance,
        minimum_due=stmt.minimum_due, payment_due_date=stmt.due_date,
        credit_limit=stmt.credit_limit, available_credit=stmt.available,
        line_count=len(stmt.lines), source_path=src_path,
        parsed_at=now, extras=extras_json, updated_at=now,
    )
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
    else:
        s.add(ledger.StatementRegistry(created_at=now, **fields))


def post_statement(s, stmt: p.ParsedStatement) -> dict:
    """Post all charge/interest/fee lines from one statement. Returns stats."""
    # Write/update statement registry (idempotent)
    try:
        upsert_statement_registry(s, stmt)
    except Exception as e:
        logger.warning("statement_registry upsert failed: %s", e)
    stats = {"charges": 0, "interest": 0, "fees": 0, "payments_skipped": 0,
             "refunds": 0, "errors": 0, "asset_acquisitions": 0}
    for line in stmt.lines:
        # SC statement carries per-line CoA marker (CC vs BT)
        override = None
        if stmt.bank == "sc" and line.raw and line.raw.startswith("[coa:"):
            override = line.raw[5:9]  # "2113" or "2211"
        jid = post_line(s, stmt, line, override_facility=override)
        if jid is None:
            if line.kind == "payment":
                stats["payments_skipped"] += 1
            else:
                stats["errors"] += 1
            continue
        if line.kind == "charge":
            if line.amount < 0:
                stats["refunds"] += 1
            else:
                stats["charges"] += 1
        elif line.kind == "interest":
            stats["interest"] += 1
        elif line.kind in ("fee", "annual_fee"):
            stats["fees"] += 1
    return stats


def is_cc_or_loan_pdf(path: Path) -> bool:
    """Filter out CPF/credit-report/payslip/etc. UOB Personal Loan statements
    are valid (part of UOB CashPlus facility) — explicitly NOT excluded."""
    fn = path.name.lower()
    if fn.endswith(".jpg"):
        return False  # skip images here (HSBC JPEG handled separately)
    excluded_patterns = [
        "payslip", "noa", "credit report", "cbs", "mlcb", "cpf latest",
        "ml compairson", "dc acknowledgement", "dc application", "dc form",
        "dcp", "consolidation", "application form",
        "_temp_", "loan agreement", "_encrypted",
        # NOTE: "personal loan" deliberately NOT excluded —
        # UOB Personal Loan statements are valid CC pipeline inputs.
    ]
    return not any(p in fn for p in excluded_patterns)


def main():
    db.init_db()
    s = db.SessionLocal()
    grand = defaultdict(int)
    parsed_count = 0
    skipped_count = 0
    bank_counts: dict[str, int] = defaultdict(int)
    errors: list[str] = []

    if not CC_STATEMENT_ROOT.exists():
        print(f"ERROR: {CC_STATEMENT_ROOT} not mounted", file=sys.stderr)
        sys.exit(1)

    all_pdfs = list(CC_STATEMENT_ROOT.rglob("*.pdf"))
    print(f"[cc_pipeline] scanning {len(all_pdfs)} PDFs in {CC_STATEMENT_ROOT}")

    try:
        for pdf in all_pdfs:
            if not is_cc_or_loan_pdf(pdf):
                skipped_count += 1
                continue
            try:
                stmt = p.detect_and_parse(str(pdf))
            except Exception as e:
                errors.append(f"{pdf.name}: parse exception {e}")
                continue
            if stmt is None:
                skipped_count += 1
                continue
            if stmt.parse_errors and not stmt.lines:
                skipped_count += 1
                logger.info("skipped %s: %s", pdf.name, stmt.parse_errors[0])
                continue
            parsed_count += 1
            bank_counts[stmt.bank] += 1
            stats = post_statement(s, stmt)
            for k, v in stats.items():
                grand[k] += v
            s.commit()
            print(f"  [{stmt.bank}] {pdf.name[:50]:<50}  charges={stats['charges']:>3}  "
                  f"int={stats['interest']:>2}  fees={stats['fees']:>2}  "
                  f"pay-skipped={stats['payments_skipped']:>2}")
    finally:
        s.close()

    print("\n" + "=" * 80)
    print(f"  Parsed:  {parsed_count} PDFs")
    print(f"  Skipped: {skipped_count} (non-CC or unrecognised format)")
    print(f"  Banks:")
    for b, n in sorted(bank_counts.items(), key=lambda kv: -kv[1]):
        print(f"    {b:<20} {n:>3} statements")
    print(f"\n  Journals posted:")
    for k, v in grand.items():
        print(f"    {k:<20} {v:>5}")
    if errors:
        print(f"\n  Errors: {len(errors)}")
        for e in errors[:10]:
            print(f"    {e}")


if __name__ == "__main__":
    main()
