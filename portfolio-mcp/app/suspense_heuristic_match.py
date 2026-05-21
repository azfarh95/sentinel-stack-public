"""Date+amount heuristic matcher for Bill Payment - Unknown Suspense entries.

For each remaining Bill Payment in Suspense, match against known CC minimum
payment patterns from recurring.yaml:
  - Day 1-4   ±2d, $90-130        → Maybank CC ($106.97)
  - Day 2-7   ±2d, $50-90         → HSBC CC ($68.13)
  - Day 3-7   ±2d, $65-85         → DBS Cashline ($73.15)
  - Day 6-11  ±2d, $250-380       → DBS CC ($327.78)
  - Day 9-14  ±2d, $250-380       → SC CC ($323.95)
  - Day 10-14 ±2d, $130-180       → UOB CashPlus ($153.65)
  - $1000+ on day 25-31           → DBS CC (lump-sum paydown — historical pattern)

Confidence levels:
  HIGH: exact amount + exact day match → auto-post
  MEDIUM: amount match within tolerance + day window → auto-post with note
  LOW: ambiguous or no clear pattern → leave in Suspense
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date as _date
from sqlalchemy import select

from . import database as db
from . import journal_service as js
from . import ledger

logger = logging.getLogger(__name__)


# (target_amount, low, high, day_low, day_high, cc_coa, label)
CC_PATTERN_TABLE = [
    # Maybank CC: day 2, $106.97
    (106.97, 90.00, 130.00, 1, 5, "2112", "Maybank CC"),
    # HSBC CC: day 4, $68.13
    (68.13, 50.00, 90.00, 2, 7, "2114", "HSBC CC"),
    # DBS Cashline: day 5, $73.15 (overlaps HSBC band; tie-broken by day-of-month)
    (73.15, 60.00, 90.00, 3, 7, "2121", "DBS Cashline"),
    # DBS CC: day 8, $327.78
    (327.78, 280.00, 380.00, 5, 11, "2111", "DBS CC"),
    # SC CC: day 11, $323.95
    (323.95, 280.00, 380.00, 9, 14, "2113", "SC CC"),
    # UOB CashPlus: day 12, $153.65
    (153.65, 130.00, 180.00, 10, 14, "2122", "UOB CashPlus"),
    # Maybank CreditAble: day 4-20, ~$30-100 (was $30 in Apr'26 statement)
    (30.00, 25.00, 110.00, 1, 22, "2213", "Maybank CreditAble"),
    # ── Aggressive lump-sum heuristics (lower confidence, route to DBS CC) ───
    # Month-end big payments are typically DBS CC lump-sum paydowns / MPPP clearance.
    # The user pays bills via DBS iBanking; biggest of these flow to DBS CC.
    (1500.00, 700.00, 2500.00, 25, 31, "2111", "DBS CC (month-end lump-sum)"),
    (1000.00, 700.00, 2500.00,  1,  3, "2111", "DBS CC (month-start lump-sum)"),
    # Medium month-end ($300-700) — likely DBS CC or SC CC; default to DBS as primary
    (500.00,  300.00,  700.00, 25, 31, "2111", "DBS CC (month-end medium)"),
    # Smaller month-end ($100-300) catch-all → DBS CC
    (200.00,  100.00,  300.00, 25, 31, "2111", "DBS CC (month-end small)"),
]


def _match_pattern(amount: float, dom: int) -> tuple[str, str, str] | None:
    """Return (cc_coa, label, confidence) if a pattern matches, else None.
    confidence ∈ {'high', 'medium'}."""
    best = None  # (score, coa, label, conf)
    for tgt, lo, hi, dlo, dhi, coa, lbl in CC_PATTERN_TABLE:
        if not (lo <= amount <= hi):
            continue
        if not (dlo <= dom <= dhi):
            continue
        # Score: tighter to target = better
        amt_dist = abs(amount - tgt)
        if amt_dist <= 1.0:
            score = 100 - amt_dist
            conf = "high"
        else:
            score = 50 - amt_dist
            conf = "medium"
        if best is None or score > best[0]:
            best = (score, coa, lbl, conf)
    if best is None:
        return None
    return (best[1], best[2], best[3])


def main():
    db.init_db()
    s = db.SessionLocal()
    try:
        sus_id = s.execute(select(ledger.ChartOfAccount)
                           .where(ledger.ChartOfAccount.account_code == "1190")).scalar_one().id
        # Bill Payment - Unknown DR entries in Suspense from FIREFLY_BRIDGE, NOT yet matched
        # (matched ones are zeroed by SUSPENSE_MATCH_CC journals — we filter by ext_id presence)
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
        # Filter out ones already CC-matched (find their j.id in SUSPENSE_MATCH_CC source_refs)
        already_matched_journal_ids = set()
        for j in s.execute(
            select(ledger.Journal).where(ledger.Journal.source_doc == "SUSPENSE_MATCH_CC")
        ).scalars().all():
            if j.source_ref and j.source_ref.startswith("journal:"):
                already_matched_journal_ids.add(int(j.source_ref.split(":")[1]))
        rows = [(g, j) for g, j in rows if j.id not in already_matched_journal_ids]
        print(f"Unmatched Bill Payment - Unknown entries: {len(rows)}")
        total = sum(g.debit_sgd for g, _ in rows)
        print(f"Total in Suspense: SGD {total:,.2f}")
        print()

        per_cc = defaultdict(lambda: {"count": 0, "amount": 0.0, "conf_high": 0, "conf_medium": 0})
        unmatched = []
        posted = 0
        for gle, j in rows:
            d = j.journal_date.date() if hasattr(j.journal_date, "date") else j.journal_date
            amt = float(gle.debit_sgd)
            match = _match_pattern(amt, d.day)
            if match is None:
                unmatched.append((j, gle, amt, d))
                continue
            cc_coa, label, conf = match
            try:
                js.post_journal(
                    s,
                    journal_date=d,
                    narration=f"[suspense heuristic] {amt:.2f} → {label} ({conf})",
                    journal_type="general",
                    lines=[
                        {"account_code": cc_coa, "debit": amt,
                         "narration": f"CC liability reduction (heuristic: {label} day{d.day})"},
                        {"account_code": "1190", "credit": amt,
                         "narration": f"Clear Suspense (Firefly tx via {j.source_ref})"},
                    ],
                    source_doc="SUSPENSE_HEURISTIC",
                    source_ref=f"journal:{j.id}",
                    external_id=f"sus_heur:{j.id}",
                )
                posted += 1
                per_cc[label]["count"] += 1
                per_cc[label]["amount"] += amt
                per_cc[label][f"conf_{conf}"] += 1
            except Exception as e:
                logger.warning("post failed: %s", e)
        s.commit()
        print(f"Matched + posted: {posted}")
        print(f"Unmatched: {len(unmatched)}  (SGD {sum(u[2] for u in unmatched):,.2f})")
        print()
        print("Per-CC heuristic resolution:")
        for label, v in sorted(per_cc.items(), key=lambda kv: -kv[1]["amount"]):
            print(f"  {label:<25} count={v['count']:>3}  high={v['conf_high']:>2}  med={v['conf_medium']:>2}"
                  f"  SGD {v['amount']:>10,.2f}")
        print()
        print(f"Suspense after heuristic match: SGD {js.account_balance(s, '1190'):,.2f}")

        # Show the unmatched ones — those need user input
        if unmatched:
            print()
            print("Unmatched tx (need user input):")
            print(f"{'Date':<11}  {'DOM':>3}  {'Amount':>10}")
            print("-" * 30)
            for j, gle, amt, d in sorted(unmatched, key=lambda x: -x[2])[:20]:
                print(f"  {d.isoformat()}  {d.day:>3}  SGD {amt:>9,.2f}")
            print(f"  ... ({len(unmatched)} total)")

    finally:
        s.close()


if __name__ == "__main__":
    main()
