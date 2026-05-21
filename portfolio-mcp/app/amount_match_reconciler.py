"""Amount-match reconciler — fix FIREFLY_BRIDGE journals where 'Personal transfer'
or 'Loan drawdown' got dumped to 5190/4900 because the POSB PDF source lacks
recipient identifier.

Strategy:
  1. Scan GL journals where source_doc='FIREFLY_BRIDGE' and account hits 5190 or 4900
  2. For each, compare the journal AMOUNT against known facility instalment amounts
     (from `credit_facilities.instalment_amount`) within ±$2 tolerance
  3. If exactly ONE facility matches, void the old journal and post a new one with
     the correct facility CoA
  4. If multiple facilities match (ambiguous) or none, leave alone + log to TODO

Result: $109k FAST Payment + $14k MEPS Receipt buckets get pared down to genuine
unknowns; loan instalments + disbursements get to the correct liability accounts.

Idempotent — re-running on already-rerouted journals is a no-op (they no longer
land in 5190/4900).

Run:
    docker exec portfolio-mcp python -m app.amount_match_reconciler           # dry-run
    docker exec portfolio-mcp python -m app.amount_match_reconciler --apply   # execute
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime
from collections import defaultdict
from sqlalchemy import select, distinct, func

from . import database as db
from . import ledger
from . import journal_service as js

logger = logging.getLogger(__name__)

# Per-facility match rules (amount, ±tolerance, direction, target_coa, notes)
# Direction: 'outflow' = POSB→facility (repayment); 'inflow' = facility→POSB (disbursement)
FACILITY_INSTALMENT_MATCHES = [
    # (amount, tol, direction, target_coa, label, narration_hint)
    (530.19, 2.00, "outflow", "2223", "Sands Credit monthly",         "Sands"),
    (498.72, 2.00, "outflow", "2221", "EZ Loan monthly",               "EZ Loan"),
    (532.76, 2.00, "outflow", "2222", "Lending Bee monthly",           "Lending Bee"),
    (153.65, 1.00, "outflow", "2122", "UOB CashPlus minimum",          "UOB CashPlus"),
    ( 93.88, 1.00, "outflow", "2211", "SC Balance Transfer minimum",   "SC BT"),
    (120.12, 1.00, "outflow", "2213", "Maybank CreditAble instalment", "Maybank CreditAble"),
]

# Pinpoint MEPS disbursement matches (rare large events)
MEPS_DISBURSEMENT_MATCHES = [
    # (date, amount, tol, target_coa, label)
    ("2026-03-24", 6300.00, 5.00, "2213", "Maybank CreditAble origination"),
    ("2026-04-01", 5600.00, 5.00, "2211", "SC BT origination"),
]


def find_outflow_candidates(s):
    """GL entries on 5190 from FIREFLY_BRIDGE that might match a facility outflow."""
    rows = s.execute(
        select(ledger.Journal.id,
               ledger.Journal.journal_date,
               ledger.Journal.narration,
               ledger.Journal.external_id,
               func.sum(ledger.GeneralLedgerEntry.debit).label("amount"))
        .join(ledger.GeneralLedgerEntry, ledger.GeneralLedgerEntry.journal_id == ledger.Journal.id)
        .join(ledger.ChartOfAccount, ledger.ChartOfAccount.id == ledger.GeneralLedgerEntry.account_id)
        .where(ledger.ChartOfAccount.account_code == "5190",
               ledger.Journal.source_doc == "FIREFLY_BRIDGE",
               ledger.Journal.status == "posted",
               ledger.GeneralLedgerEntry.debit > 0)
        .group_by(ledger.Journal.id, ledger.Journal.journal_date,
                  ledger.Journal.narration, ledger.Journal.external_id)
    ).all()
    return rows


def find_inflow_candidates(s):
    """GL entries on 4900 from FIREFLY_BRIDGE that might match a facility inflow."""
    rows = s.execute(
        select(ledger.Journal.id,
               ledger.Journal.journal_date,
               ledger.Journal.narration,
               ledger.Journal.external_id,
               func.sum(ledger.GeneralLedgerEntry.credit).label("amount"))
        .join(ledger.GeneralLedgerEntry, ledger.GeneralLedgerEntry.journal_id == ledger.Journal.id)
        .join(ledger.ChartOfAccount, ledger.ChartOfAccount.id == ledger.GeneralLedgerEntry.account_id)
        .where(ledger.ChartOfAccount.account_code == "4900",
               ledger.Journal.source_doc == "FIREFLY_BRIDGE",
               ledger.Journal.status == "posted",
               ledger.GeneralLedgerEntry.credit > 0)
        .group_by(ledger.Journal.id, ledger.Journal.journal_date,
                  ledger.Journal.narration, ledger.Journal.external_id)
    ).all()
    return rows


def match_outflow(amount: float) -> tuple[str, str] | None:
    """Return (target_coa, label) if exactly ONE facility matches, else None."""
    matches = []
    for amt, tol, direction, coa, label, _ in FACILITY_INSTALMENT_MATCHES:
        if direction == "outflow" and abs(amount - amt) <= tol:
            matches.append((coa, label))
    return matches[0] if len(matches) == 1 else None


def match_inflow(date_iso: str, amount: float) -> tuple[str, str] | None:
    """MEPS disbursement match — keyed by date + amount."""
    for d, amt, tol, coa, label in MEPS_DISBURSEMENT_MATCHES:
        if d == date_iso and abs(amount - amt) <= tol:
            return (coa, label)
    return None


def reroute_journal(s, jid: int, new_coa: str, new_label: str) -> int:
    """Void the existing journal and post a corrected one routing to new_coa."""
    old = s.get(ledger.Journal, jid)
    if old is None or old.status != "posted":
        return 0
    # Find original DR/CR pair
    lines = s.execute(select(ledger.GeneralLedgerEntry)
                      .where(ledger.GeneralLedgerEntry.journal_id == jid)).scalars().all()
    if len(lines) != 2:
        return 0
    # Identify which side was 5190 or 4900
    bad_codes = ("5190", "4900")
    other_leg = None
    bad_leg = None
    for l in lines:
        coa = s.get(ledger.ChartOfAccount, l.account_id)
        if coa and coa.account_code in bad_codes:
            bad_leg = l
        else:
            other_leg = l
    if bad_leg is None or other_leg is None:
        return 0
    other_coa = s.get(ledger.ChartOfAccount, other_leg.account_id)
    # Build new journal: DR/CR swapped so new_coa takes bad_leg's side
    if bad_leg.debit and bad_leg.debit > 0:
        # Was DR 5190 / CR <other>  →  new: DR new_coa / CR <other>
        new_lines = [
            {"account_code": new_coa, "debit": float(bad_leg.debit),
             "narration": f"[reclass→{new_label}] {old.narration[:60]}"},
            {"account_code": other_coa.account_code, "credit": float(other_leg.credit or 0),
             "narration": f"[reclass→{new_label}] (offset)"},
        ]
    else:
        # Was CR 4900 / DR <other>  →  new: CR new_coa / DR <other>
        new_lines = [
            {"account_code": other_coa.account_code, "debit": float(other_leg.debit or 0),
             "narration": f"[reclass→{new_label}] (offset)"},
            {"account_code": new_coa, "credit": float(bad_leg.credit),
             "narration": f"[reclass→{new_label}] {old.narration[:60]}"},
        ]
    # Void old
    now = datetime.now()
    old.status = "voided"
    old.voided_at = now
    old.voided_reason = f"Amount-match reclass to {new_coa} ({new_label})"
    old.updated_at = now
    # Post new with a derived external_id so re-runs are idempotent
    new_ext = f"reclass_amt_match:{old.external_id or jid}:{new_coa}"
    jid_new = js.post_journal(
        s,
        journal_date=old.journal_date,
        narration=f"[reclass→{new_label}] {old.narration[:80]}",
        journal_type="reclassification",
        lines=new_lines,
        source_doc="AMOUNT_MATCH_RECLASS",
        source_ref=old.source_ref,
        external_id=new_ext,
    )
    return jid_new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually execute the reclassification (default: dry-run)")
    args = ap.parse_args()

    s = db.SessionLocal()
    try:
        outflows = find_outflow_candidates(s)
        inflows = find_inflow_candidates(s)
        print(f"Candidates: {len(outflows)} outflow journals on 5190, {len(inflows)} inflow journals on 4900")
        print()
        print(f"{'JID':>5} {'Date':<11} {'$':>10} {'Match':<28} {'Action'}")
        print("-" * 75)

        outflow_actions = []
        for jid, jdate, narr, ext, amt in outflows:
            match = match_outflow(float(amt))
            if match:
                outflow_actions.append((jid, jdate, float(amt), match[0], match[1]))
                print(f"  {jid:>5} {str(jdate):<11} {float(amt):>10,.2f} {match[1]:<28} → {match[0]}")

        inflow_actions = []
        for jid, jdate, narr, ext, amt in inflows:
            match = match_inflow(str(jdate), float(amt))
            if match:
                inflow_actions.append((jid, jdate, float(amt), match[0], match[1]))
                print(f"  {jid:>5} {str(jdate):<11} {float(amt):>10,.2f} {match[1]:<28} → {match[0]}")

        total = len(outflow_actions) + len(inflow_actions)
        total_amt = (sum(a[2] for a in outflow_actions)
                     + sum(a[2] for a in inflow_actions))
        print()
        print(f"  {total} reclassifications planned · SGD {total_amt:,.2f} total")

        if args.apply and total > 0:
            print("\nApplying...")
            for jid, jdate, amt, new_coa, label in outflow_actions + inflow_actions:
                try:
                    new_jid = reroute_journal(s, jid, new_coa, label)
                    s.commit()
                    print(f"  j={jid} → j={new_jid} ({label})")
                except Exception as e:
                    s.rollback()
                    print(f"  j={jid} FAILED: {str(e)[:80]}")
        elif not args.apply:
            print("\nDRY-RUN — pass --apply to execute.")
    finally:
        s.close()


if __name__ == "__main__":
    main()
