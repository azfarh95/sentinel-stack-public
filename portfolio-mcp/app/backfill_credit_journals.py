"""One-off backfill: create proper double-entry journals for moneylender loans.

Encodes the data discovered during the v1.10.0 audit night (2026-05-13):
  - Sands Credit: origination 17-Apr-2025 + 12 instalments through 30-Apr-2026
  - EZ Loan: origination 07-Jan-2026 + 4 instalments through May 2026
  - Lending Bee: origination 28-Feb-2026 + 2 instalments through 27-Apr-2026

Each loan generates:
  1. Origination journal — DR Bank (disbursed) + DR Finance Costs (admin fee) + CR Liability (principal)
  2. Per-instalment journal — DR Liability (principal portion) + DR Finance Costs (interest portion) + CR Bank

All journals carry external_id = stable hash for idempotent re-runs.

Run inside container:
    docker exec portfolio-mcp python -m app.backfill_credit_journals
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date as _date
from pathlib import Path

import yaml
from sqlalchemy import select

from . import database as db
from . import journal_service as js
from . import ledger

logger = logging.getLogger(__name__)


# Map facility_id → CoA account_code (liability) + interest_expense_code
FACILITY_TO_COA = {
    "sands-credit":  {"liability": "2223", "interest_expense": "5430"},  # Sands → Moneylender Interest
    "ez-loan":       {"liability": "2221", "interest_expense": "5430"},
    "lending-bee":   {"liability": "2222", "interest_expense": "5430"},
}

BANK_POSB = "1111"          # POSB Savings
FINANCE_FEE = "5460"        # Annual Fees / Processing Fees


def _hash_id(*parts: str) -> str:
    """Stable external_id for idempotent posting."""
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


def post_origination(s, facility_id: str) -> int | None:
    """Post the 3-leg origination journal for a moneylender loan.

    DR Bank (disbursed)
    DR Finance Costs (admin fee)
    CR Liability (principal)
    """
    fac = s.execute(
        select(db.CreditFacility).where(db.CreditFacility.id == facility_id)
    ).scalar_one_or_none()
    if not fac or not fac.origination_date or not fac.principal_amount:
        logger.info("skip origination: facility %s has incomplete data", facility_id)
        return None
    coa = FACILITY_TO_COA.get(facility_id)
    if not coa:
        logger.info("skip origination: no CoA map for %s", facility_id)
        return None

    principal = float(fac.principal_amount)
    disbursed = float(fac.disbursed_amount or principal)
    admin_fee = float(fac.admin_fee or 0)
    lines = []
    if disbursed > 0:
        lines.append({"account_code": BANK_POSB, "debit": disbursed,
                      "narration": f"{fac.lender_name} disbursement",
                      "sub_ledger_table": "credit_facilities", "sub_ledger_id": facility_id,
                      "sub_ledger_event": "disbursement"})
    if admin_fee > 0:
        lines.append({"account_code": FINANCE_FEE, "debit": admin_fee,
                      "narration": f"{fac.lender_name} admin fee (10%)",
                      "sub_ledger_table": "credit_facilities", "sub_ledger_id": facility_id,
                      "sub_ledger_event": "origination_fee"})
    lines.append({"account_code": coa["liability"], "credit": principal,
                  "narration": f"{fac.lender_name} principal incurred",
                  "sub_ledger_table": "credit_facilities", "sub_ledger_id": facility_id,
                  "sub_ledger_event": "origination"})
    orig_date = fac.origination_date.date() if hasattr(fac.origination_date, "date") else fac.origination_date
    jid = js.post_journal(
        s,
        journal_date=orig_date,
        narration=f"{fac.lender_name} — loan origination (principal {principal:.2f}, fee {admin_fee:.2f}, disbursed {disbursed:.2f})",
        journal_type="general",
        lines=lines,
        source_doc="MANUAL_BACKFILL_2026-05-13",
        source_ref=f"{facility_id}:origination",
        external_id=_hash_id("origination", facility_id),
    )
    return jid


def post_instalments(s, facility_id: str) -> int:
    """For each paid instalment, post a 3-leg journal:
       DR Liability (principal portion)
       DR Finance Costs (interest portion)
       CR Bank (full instalment amount)
    Returns count posted.
    """
    coa = FACILITY_TO_COA.get(facility_id)
    if not coa:
        return 0

    sched_rows = s.execute(
        select(db.PaymentSchedule).where(db.PaymentSchedule.facility_id == facility_id)
        .order_by(db.PaymentSchedule.instalment_no)
    ).scalars().all()
    ap_by_sched = {p.schedule_id: p for p in s.execute(
        select(db.ActualPayment).where(db.ActualPayment.facility_id == facility_id)
    ).scalars().all()}

    fac = s.get(db.CreditFacility, facility_id)
    posted = 0
    for sch in sched_rows:
        ap = ap_by_sched.get(sch.id)
        if not ap:
            continue  # unpaid instalment — no journal yet
        if sch.principal_portion is None or sch.interest_portion is None:
            continue
        lines = []
        if sch.principal_portion > 0:
            lines.append({"account_code": coa["liability"], "debit": float(sch.principal_portion),
                          "narration": f"Principal reduction (instalment {sch.instalment_no})",
                          "sub_ledger_table": "credit_facilities", "sub_ledger_id": facility_id,
                          "sub_ledger_event": f"instalment_principal:{sch.instalment_no}"})
        if sch.interest_portion > 0:
            lines.append({"account_code": coa["interest_expense"], "debit": float(sch.interest_portion),
                          "narration": f"Interest expense (instalment {sch.instalment_no})",
                          "sub_ledger_table": "credit_facilities", "sub_ledger_id": facility_id,
                          "sub_ledger_event": f"instalment_interest:{sch.instalment_no}"})
        lines.append({"account_code": BANK_POSB, "credit": float(sch.amount),
                      "narration": f"Payment to {fac.lender_name} instalment {sch.instalment_no}",
                      "sub_ledger_table": "credit_facilities", "sub_ledger_id": facility_id,
                      "sub_ledger_event": f"instalment_payment:{sch.instalment_no}"})
        paid_d = ap.paid_date.date() if hasattr(ap.paid_date, "date") else ap.paid_date
        jid = js.post_journal(
            s,
            journal_date=paid_d,
            narration=f"{fac.lender_name} — instalment {sch.instalment_no}/{fac.num_instalments}",
            journal_type="cash_payment",
            lines=lines,
            source_doc="MANUAL_BACKFILL_2026-05-13",
            source_ref=f"firefly_tx:{ap.firefly_tx_id}",
            external_id=_hash_id("instalment", facility_id, str(sch.instalment_no)),
        )
        if jid:
            posted += 1
    return posted


def main():
    db.init_db()
    s = db.SessionLocal()
    try:
        total_orig = 0
        total_inst = 0
        for facility_id in FACILITY_TO_COA.keys():
            jid = post_origination(s, facility_id)
            if jid is not None:
                total_orig += 1
                print(f"  [{facility_id}] origination journal #{jid}")
            n = post_instalments(s, facility_id)
            total_inst += n
            print(f"  [{facility_id}] {n} instalment journals posted")
        s.commit()
        print(f"\n[backfill] {total_orig} origination + {total_inst} instalment journals")

        # Quick verification: show resulting P&L balances on the relevant accounts
        print("\n=== Verification (account balances from posted journals) ===")
        for code, label in [
            ("5430", "Moneylender Interest expense"),
            ("5460", "Processing/Admin Fees expense"),
            ("2221", "EZ Loan liability"),
            ("2222", "Lending Bee liability"),
            ("2223", "Sands Credit liability"),
            ("1111", "POSB Savings"),
        ]:
            bal = js.account_balance(s, code)
            print(f"  {code} {label:<35}  SGD {bal:>11,.2f}")
    finally:
        s.close()


if __name__ == "__main__":
    main()
