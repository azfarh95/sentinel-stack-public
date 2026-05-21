"""Phase 3 — Reclassify 24 historical Savvy Invest premiums from 5190 → 1222.

Targets the 24 FIREFLY_BRIDGE journals where:
  - amount = $252.85
  - narration contains "Payments / Collections via GIRO - Unknown"
  - other leg = 5190 General Expense (parked)

The PDF source (universal parser confirmed) reveals these are Singlife Savvy
Invest ILP premium contributions (policy P4064051), which should land in
1222 Singlife ILP (asset increase), NOT 5190 General Expense (P&L charge).

Method: VOID the 5190 leg, POST a replacement journal with the same amount
hitting 1222. Idempotent via external_id pattern `savvy-reclass:firefly_tx:<id>`.

Run:
    docker exec portfolio-mcp python -m app.reclassify_savvy_invest          # dry-run
    docker exec portfolio-mcp python -m app.reclassify_savvy_invest --post   # apply
"""
import argparse
from datetime import date, datetime

from sqlalchemy import text

from app import database as db
from app import journal_service as js

POSB_ACCT_ID = 4
SAVVY_INVEST_COA = "12229"   # leaf account "Singlife Savvy Invest — Unallocated"
                              # (parent 1222 is a header; per-fund split happens
                              #  later when Singlife monthly statements are parsed)
SUSPENSE_COA = "5190"
AMOUNT = 252.85


def find_targets(s):
    """Find the 24 historical FIREFLY_BRIDGE journals matching the Savvy Invest pattern."""
    rows = s.execute(text("""
      SELECT j.id AS jid,
             j.journal_date,
             j.narration,
             j.source_ref,
             j.external_id,
             j.status
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id = j.id
      JOIN chart_of_accounts coa ON coa.id = gl.account_id
      WHERE j.source_doc LIKE 'FIREFLY_BRIDGE%'
        AND j.status != 'voided'
        AND ABS(gl.debit - :amt) < 0.01
        AND coa.account_code = :coa
      ORDER BY j.journal_date
    """), {"amt": AMOUNT, "coa": SUSPENSE_COA}).all()
    return rows


def void_journal(s, jid: int, reason: str):
    """Mark a journal as void."""
    s.execute(text("""
      UPDATE journals
      SET status='voided',
          voided_at = CURRENT_TIMESTAMP,
          voided_reason = :reason
      WHERE id = :jid
    """), {"jid": jid, "reason": reason})


def post_replacement(s, original_date, original_jid, original_narration, original_ref):
    """Post replacement journal: DR 1222 (asset up), CR 1111 (POSB down).
    Idempotent via external_id."""
    ext_id = f"savvy-reclass:firefly_tx:{original_jid}"
    return js.post_journal(
        s,
        journal_date=original_date,
        narration=f"[reclassed Savvy Invest premium → 1222] " + (original_narration or "")[:80],
        journal_type="ilp_premium",
        lines=[
            {"account_code": SAVVY_INVEST_COA, "debit": AMOUNT,
             "narration": "Singlife Savvy Invest P4064051 — premium contribution"},
            {"account_code": "1111", "credit": AMOUNT,
             "narration": "POSB GIRO debit (recovered from Firefly's 'Unknown')"},
        ],
        source_doc="POSB_PDF_RECLASS",
        source_ref=(original_ref or "")[:60],
        external_id=ext_id,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true", help="Actually void + post (default: dry-run)")
    args = ap.parse_args()

    db.init_db()
    s = db.SessionLocal()
    try:
        targets = find_targets(s)
        print(f"Found {len(targets)} historical Savvy Invest journals in 5190 ($252.85)\n")

        total_amt = len(targets) * AMOUNT
        print(f"Total $ to reclassify: {total_amt:,.2f}")
        print()
        print(f"{'date':<12} {'firefly_jid':<12} narration")
        print("-" * 100)
        for r in targets:
            print(f"  {str(r[1]):<12} {r[0]:<12} {(r[2] or '')[:70]}")

        if not args.post:
            print("\nDRY-RUN — pass --post to void + replace.")
            return

        posted = 0
        for r in targets:
            jid = r[0]
            jdate = r[1]
            # Coerce string dates to Python date if needed (SQLite returns strings)
            if isinstance(jdate, str):
                jdate = datetime.strptime(jdate[:10], "%Y-%m-%d").date()
            narration = r[2]
            ref = r[3]
            try:
                new_jid = post_replacement(s, jdate, jid, narration, ref)
                if new_jid is None:
                    # Idempotent skip (already reclassed)
                    continue
                void_journal(s, jid, "Reclassified Savvy Invest premium 5190 → 1222 (Phase 3 of decouple)")
                s.commit()
                posted += 1
                print(f"  ✓ {jdate}  firefly_jid={jid}  → new_jid={new_jid}")
            except Exception as e:
                s.rollback()
                print(f"  ✗ {jdate}  firefly_jid={jid}  ERR: {e}")
        print(f"\nDone. Reclassified {posted} of {len(targets)} historical Savvy Invest premiums.")
    finally:
        s.close()


if __name__ == "__main__":
    main()
