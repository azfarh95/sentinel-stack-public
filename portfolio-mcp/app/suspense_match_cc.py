"""Match the 137 'Bill Payment - Unknown' Suspense DR entries to specific CC liabilities
by cross-referencing CC statement payment-received lines (re-parsed from PDFs).

For each Suspense entry (date, amount), find any CC statement payment-received line
within ±7 days, ±$1.00. If exactly one match → post corrective journal:
    DR CC Liability  (the matched CC)
    CR Suspense      (clearing the wrong leg)

If multiple matches → manual review (logged).
If no match → leave in Suspense (genuine data gap).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date as _date, timedelta
from pathlib import Path

from sqlalchemy import select

from . import cc_statement_parser as p
from . import database as db
from . import journal_service as js
from . import ledger

logger = logging.getLogger(__name__)

CC_STATEMENT_ROOT = Path("/onedrive/Sentinel Finance/02_Credit card statements")
MATCH_DAY_WINDOW = 7
MATCH_AMOUNT_TOL = 1.00


@dataclass
class CCPayment:
    cc_coa: str
    date: _date
    amount: float
    source_pdf: str


def collect_cc_payments() -> list[CCPayment]:
    """Walk all CC statement PDFs, return all 'payment' lines as (cc_coa, date, amount)."""
    out: list[CCPayment] = []
    for pdf in CC_STATEMENT_ROOT.rglob("*.pdf"):
        fn = pdf.name.lower()
        if any(skip in fn for skip in ["payslip", "noa", "credit report", "cbs", "mlcb",
                                        "cpf latest", "loan agreement", "_temp_",
                                        "ml compairson", "dc acknowledgement",
                                        "dc application", "dc form"]):
            continue
        try:
            stmt = p.detect_and_parse(str(pdf))
        except Exception:
            continue
        if not stmt or not stmt.lines:
            continue
        for line in stmt.lines:
            if line.kind != "payment":
                continue
            d = line.posted_date or line.txn_date or stmt.statement_date
            if d is None:
                continue
            # SC has per-line CoA override
            coa = stmt.facility_coa_code
            if stmt.bank == "sc" and line.raw.startswith("[coa:"):
                coa = line.raw[5:9]
            out.append(CCPayment(cc_coa=coa, date=d, amount=abs(line.amount),
                                  source_pdf=pdf.name))
    return out


def main():
    db.init_db()
    s = db.SessionLocal()
    try:
        sus = s.execute(select(ledger.ChartOfAccount)
                        .where(ledger.ChartOfAccount.account_code == "1190")).scalar_one()

        # Get all Suspense DR entries from "Bill Payment - Unknown" bridge journals
        bp_entries = s.execute(
            select(ledger.GeneralLedgerEntry, ledger.Journal)
            .join(ledger.Journal, ledger.Journal.id == ledger.GeneralLedgerEntry.journal_id)
            .where(
                ledger.GeneralLedgerEntry.account_id == sus.id,
                ledger.GeneralLedgerEntry.debit > 0,
                ledger.Journal.source_doc == "FIREFLY_BRIDGE",
                ledger.Journal.narration.like("%Bill Payment - Unknown%"),
            )
        ).all()
        print(f"Found {len(bp_entries)} 'Bill Payment - Unknown' Suspense DR entries")
        total_dr = sum(g.debit_sgd for g, _ in bp_entries)
        print(f"Total: SGD {total_dr:,.2f}")
        print()

        print("Parsing all CC statement PDFs for payment-received lines...")
        cc_pays = collect_cc_payments()
        print(f"Found {len(cc_pays)} CC statement payment-received lines")
        # Index by date for fast lookup
        pays_by_date: dict[_date, list[CCPayment]] = defaultdict(list)
        for pay in cc_pays:
            pays_by_date[pay.date].append(pay)
        print()

        matched = 0
        ambiguous = 0
        unmatched = 0
        posted = 0
        ambig_amount = 0.0
        unm_amount = 0.0
        per_cc_resolved = defaultdict(int)
        per_cc_amount = defaultdict(float)

        for gle, j in bp_entries:
            target_d = j.journal_date.date() if hasattr(j.journal_date, "date") else j.journal_date
            target_a = float(gle.debit_sgd)
            # Search ±MATCH_DAY_WINDOW
            candidates = []
            for delta in range(-MATCH_DAY_WINDOW, MATCH_DAY_WINDOW + 1):
                d = target_d + timedelta(days=delta)
                for pay in pays_by_date.get(d, []):
                    if abs(pay.amount - target_a) <= MATCH_AMOUNT_TOL:
                        candidates.append(pay)
            if len(candidates) == 1:
                pay = candidates[0]
                matched += 1
                per_cc_resolved[pay.cc_coa] += 1
                per_cc_amount[pay.cc_coa] += target_a
                try:
                    js.post_journal(
                        s,
                        journal_date=target_d,
                        narration=f"[suspense match] {target_a:.2f} → {pay.cc_coa} (was Bill Payment Unknown)",
                        journal_type="general",
                        lines=[
                            {"account_code": pay.cc_coa, "debit": target_a,
                             "narration": f"CC liability reduction (matched to {pay.source_pdf})"},
                            {"account_code": "1190", "credit": target_a,
                             "narration": f"Clear Suspense (Firefly tx via {j.source_ref})"},
                        ],
                        source_doc="SUSPENSE_MATCH_CC",
                        source_ref=f"journal:{j.id}",
                        external_id=f"sus_match:{j.id}",
                    )
                    posted += 1
                except Exception as e:
                    logger.warning("post_journal failed: %s", e)
            elif len(candidates) > 1:
                ambiguous += 1
                ambig_amount += target_a
                # Multiple CCs received the same amount around same date — log for manual
                logger.info("AMBIGUOUS j=%s amt=%.2f date=%s candidates=%s",
                            j.id, target_a, target_d,
                            [f"{c.cc_coa}@{c.date}" for c in candidates])
            else:
                unmatched += 1
                unm_amount += target_a

        s.commit()
        print(f"Matched (1:1): {matched}, posted: {posted}")
        print(f"Ambiguous (multi): {ambiguous} (SGD {ambig_amount:,.2f})")
        print(f"Unmatched: {unmatched} (SGD {unm_amount:,.2f})")
        print()
        print("Per-CC resolved breakdown:")
        for coa, n in sorted(per_cc_resolved.items()):
            print(f"  {coa}: {n} tx, SGD {per_cc_amount[coa]:,.2f}")

        after = js.account_balance(s, "1190")
        print(f"\nSuspense after CC matching: SGD {after:,.2f}")

    finally:
        s.close()


if __name__ == "__main__":
    main()
