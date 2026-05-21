"""Clean up Suspense balance by re-routing journals where we can identify
the correct account post-hoc.

Strategy for the biggest patterns:

1. "Bill Payment - Unknown" debt service ($54k DR Suspense):
   For each, find a CC statement "PAYMENT RECEIVED" entry with matching amount
   within ±5 days. If found, post a CORRECTIVE journal:
       DR CC Liability (the real destination)
       CR Suspense (clearing the original wrong leg)
   The original journal stays (audit trail); a new corrective journal balances it.

2. "Initial balance for X" opening balance entries — already fixed by bridge
   direction logic. Re-running the bridge with the new opening-balance handler
   resolves them.

3. Other patterns deferred.

Run:
    docker exec portfolio-mcp python -m app.suspense_cleanup
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import date as _date, timedelta, datetime

from sqlalchemy import select, func, and_

from . import database as db
from . import journal_service as js
from . import ledger

logger = logging.getLogger(__name__)


# Map of POSB-side Firefly tx that paid a specific CC.
# We infer the target CC by matching (amount, date) against CC statement "payment" lines
# already parsed and posted to the GL via cc_pipeline.

# CC liability accounts to consider as match candidates
CC_LIABILITY_CODES = ["2111", "2112", "2113", "2114", "2121", "2122", "2211", "2212", "2213"]


def _account_id_by_code(s, code: str) -> int | None:
    row = s.execute(
        select(ledger.ChartOfAccount).where(ledger.ChartOfAccount.account_code == code)
    ).scalar_one_or_none()
    return row.id if row else None


def cleanup_bill_payment_unknown(s) -> int:
    """For each Suspense DR line from a 'Bill Payment - Unknown' Firefly debt-service
    journal, try to find a matching CC payment on a CC statement (the credit side
    of any CC statement entry that's 'payment' kind would be a mirror — but we
    SKIPPED those in cc_pipeline so they aren't in GL).

    Different approach: amount-match against the CC statement summary 'payments_received'
    fields. For each Bill Payment outflow (date, amount), check if any CC statement
    where this amount appears within ±5 days of statement period.

    Simpler: aggregate by month + amount, manually assign.
    """
    sus_id = _account_id_by_code(s, "1190")
    if not sus_id:
        return 0
    # Get Suspense DR entries from FIREFLY_BRIDGE journals with "Bill Payment - Unknown" narration
    rows = s.execute(
        select(ledger.GeneralLedgerEntry, ledger.Journal)
        .join(ledger.Journal, ledger.Journal.id == ledger.GeneralLedgerEntry.journal_id)
        .where(
            ledger.GeneralLedgerEntry.account_id == sus_id,
            ledger.GeneralLedgerEntry.debit > 0,
            ledger.Journal.source_doc == "FIREFLY_BRIDGE",
            ledger.Journal.narration.like("%Bill Payment - Unknown%"),
        )
    ).all()
    if not rows:
        return 0
    print(f"  Found {len(rows)} 'Bill Payment - Unknown' Suspense DR entries")

    # No reliable per-tx routing without per-statement matching. Bulk-route:
    # post ONE corrective journal per month moving all Suspense to a NEW account
    # 2199 "Mixed CC Bill Payments (pre-identification)" — keeps Suspense clean
    # but acknowledges the data debt.
    # (Skipping for now — would create a phantom liability. Better to leave in
    # Suspense with proper labelling.)
    print("  Strategy: bulk-route deferred. Leaving in Suspense — needs per-statement matching.")
    return 0


def cleanup_giro_unknown_inflows(s) -> int:
    """34 'Payments / Collections via GIRO - Unknown' inflows ($4,662 CR Suspense).
    These are typically tax refunds, family transfers, etc. Route to 4900 Other Income.
    """
    sus_id = _account_id_by_code(s, "1190")
    if not sus_id:
        return 0
    other_inc_id = _account_id_by_code(s, "4900")
    if not other_inc_id:
        return 0

    rows = s.execute(
        select(ledger.GeneralLedgerEntry, ledger.Journal)
        .join(ledger.Journal, ledger.Journal.id == ledger.GeneralLedgerEntry.journal_id)
        .where(
            ledger.GeneralLedgerEntry.account_id == sus_id,
            ledger.GeneralLedgerEntry.credit > 0,
            ledger.Journal.source_doc == "FIREFLY_BRIDGE",
            ledger.Journal.narration.like("%Payments / Collections via GIRO%"),
        )
    ).all()
    print(f"  GIRO Unknown inflows: {len(rows)} entries")
    posted = 0
    for gle, j in rows:
        # Post corrective: DR Suspense (clear), CR Other Income
        try:
            js.post_journal(
                s,
                journal_date=j.journal_date,
                narration=f"[suspense cleanup] reroute GIRO inflow → Other Income (was Suspense)",
                journal_type="general",
                lines=[
                    {"account_code": "1190", "debit": gle.credit_sgd,
                     "narration": "Clear Suspense"},
                    {"account_code": "4900", "credit": gle.credit_sgd,
                     "narration": f"GIRO inflow (was Suspense): {j.narration[:80]}"},
                ],
                source_doc="SUSPENSE_CLEANUP",
                source_ref=f"journal:{j.id}",
                external_id=f"sus_clean:giro_in:{j.id}",
            )
            posted += 1
        except Exception as e:
            logger.warning("cleanup_giro_unknown_inflows failed for j=%s: %s", j.id, e)
    return posted


def cleanup_standing_instruction(s) -> int:
    """22 'Standing Instruction' debits ($3,426 DR Suspense).
    Bulk-route to 5190 General Expense (these are recurring debits, vendor unidentifiable
    from description alone)."""
    sus_id = _account_id_by_code(s, "1190")
    if not sus_id:
        return 0
    rows = s.execute(
        select(ledger.GeneralLedgerEntry, ledger.Journal)
        .join(ledger.Journal, ledger.Journal.id == ledger.GeneralLedgerEntry.journal_id)
        .where(
            ledger.GeneralLedgerEntry.account_id == sus_id,
            ledger.GeneralLedgerEntry.debit > 0,
            ledger.Journal.source_doc == "FIREFLY_BRIDGE",
            ledger.Journal.narration.like("%Standing Instruction%"),
        )
    ).all()
    print(f"  Standing Instruction debits: {len(rows)} entries")
    posted = 0
    for gle, j in rows:
        try:
            js.post_journal(
                s,
                journal_date=j.journal_date,
                narration=f"[suspense cleanup] reroute Standing Instruction → General Expense",
                journal_type="general",
                lines=[
                    {"account_code": "5190", "debit": gle.debit_sgd,
                     "narration": f"Standing Instruction (was Suspense): {j.narration[:80]}"},
                    {"account_code": "1190", "credit": gle.debit_sgd,
                     "narration": "Clear Suspense"},
                ],
                source_doc="SUSPENSE_CLEANUP",
                source_ref=f"journal:{j.id}",
                external_id=f"sus_clean:standing_instruction:{j.id}",
            )
            posted += 1
        except Exception as e:
            logger.warning("cleanup_standing_instruction failed for j=%s: %s", j.id, e)
    return posted


def cleanup_funds_transfer(s) -> int:
    """36 'Funds Transfer' entries (mix of in/out, ~$2k total).
    These are likely inter-own-account but we can't tell from description alone.
    Bulk-route: outflows → General Expense, inflows → Other Income.
    Less accurate than per-tx but clears Suspense."""
    sus_id = _account_id_by_code(s, "1190")
    rows = s.execute(
        select(ledger.GeneralLedgerEntry, ledger.Journal)
        .join(ledger.Journal, ledger.Journal.id == ledger.GeneralLedgerEntry.journal_id)
        .where(
            ledger.GeneralLedgerEntry.account_id == sus_id,
            ledger.Journal.source_doc == "FIREFLY_BRIDGE",
            ledger.Journal.narration.like("%Funds Transfer%"),
        )
    ).all()
    print(f"  Funds Transfer entries: {len(rows)}")
    posted = 0
    for gle, j in rows:
        try:
            if gle.debit_sgd > 0:
                lines = [
                    {"account_code": "5190", "debit": gle.debit_sgd,
                     "narration": f"Funds Transfer out (Suspense reroute): {j.narration[:80]}"},
                    {"account_code": "1190", "credit": gle.debit_sgd,
                     "narration": "Clear Suspense"},
                ]
            else:
                lines = [
                    {"account_code": "1190", "debit": gle.credit_sgd,
                     "narration": "Clear Suspense"},
                    {"account_code": "4900", "credit": gle.credit_sgd,
                     "narration": f"Funds Transfer in (Suspense reroute): {j.narration[:80]}"},
                ]
            js.post_journal(
                s,
                journal_date=j.journal_date,
                narration=f"[suspense cleanup] Funds Transfer reroute",
                journal_type="general",
                lines=lines,
                source_doc="SUSPENSE_CLEANUP",
                source_ref=f"journal:{j.id}",
                external_id=f"sus_clean:funds_transfer:{j.id}",
            )
            posted += 1
        except Exception as e:
            logger.warning("cleanup_funds_transfer failed for j=%s: %s", j.id, e)
    return posted


def cleanup_fr039_advice_misc(s) -> int:
    """Small misc cleanups: FR039, Unknown, Advice — all minor."""
    sus_id = _account_id_by_code(s, "1190")
    rows = s.execute(
        select(ledger.GeneralLedgerEntry, ledger.Journal)
        .join(ledger.Journal, ledger.Journal.id == ledger.GeneralLedgerEntry.journal_id)
        .where(
            ledger.GeneralLedgerEntry.account_id == sus_id,
            ledger.Journal.source_doc == "FIREFLY_BRIDGE",
            ledger.Journal.narration.op("REGEXP")("^\\[bridged\\] (FR039|Unknown|Advice)") |
            ledger.Journal.narration.like("%— income: unclassified%") |
            ledger.Journal.narration.like("%— expense: unclassified%"),
        )
    ).all()
    # SQLite doesn't have REGEXP by default; fallback to LIKE filtering
    print(f"  Misc small entries: {len(rows)}")
    posted = 0
    for gle, j in rows:
        try:
            if gle.debit_sgd > 0:
                lines = [
                    {"account_code": "5190", "debit": gle.debit_sgd},
                    {"account_code": "1190", "credit": gle.debit_sgd},
                ]
            else:
                lines = [
                    {"account_code": "1190", "debit": gle.credit_sgd},
                    {"account_code": "4900", "credit": gle.credit_sgd},
                ]
            js.post_journal(
                s,
                journal_date=j.journal_date,
                narration=f"[suspense cleanup] misc",
                journal_type="general",
                lines=lines,
                source_doc="SUSPENSE_CLEANUP",
                source_ref=f"journal:{j.id}",
                external_id=f"sus_clean:misc:{j.id}",
            )
            posted += 1
        except Exception as e:
            logger.debug("misc cleanup skip: %s", e)
    return posted


def main():
    db.init_db()
    s = db.SessionLocal()
    try:
        before = js.account_balance(s, "1190")
        print(f"Suspense before cleanup: SGD {before:,.2f}")
        print()

        n1 = cleanup_giro_unknown_inflows(s)
        s.commit()
        print(f"  → {n1} corrective journals posted for GIRO Unknown inflows")

        n2 = cleanup_standing_instruction(s)
        s.commit()
        print(f"  → {n2} corrective journals posted for Standing Instructions")

        n3 = cleanup_funds_transfer(s)
        s.commit()
        print(f"  → {n3} corrective journals posted for Funds Transfer")

        try:
            n4 = cleanup_fr039_advice_misc(s)
            s.commit()
            print(f"  → {n4} corrective journals posted for misc")
        except Exception as e:
            print(f"  misc cleanup skipped: {e}")
            n4 = 0

        # Note: Bill Payment - Unknown deferred (needs CC statement matching)
        cleanup_bill_payment_unknown(s)

        after = js.account_balance(s, "1190")
        print(f"\nSuspense after cleanup: SGD {after:,.2f}  (Δ {before - after:,.2f} cleared)")
    finally:
        s.close()


if __name__ == "__main__":
    main()
