"""Post the proper 2024-01-01 opening anchor journal.

Run AFTER _extract_opening_balances.py — that gives you each account's
earliest-available BF. Hand-edit `OPENING_BALANCES` below with verified
numbers, then run --post.

The opening journal:
  Dr each asset at its Jan 2024 opening
  Cr each liability at its Jan 2024 opening
  Cr (or Dr) Retained Earnings (3100) as balancing leg

Run:
    docker exec portfolio-mcp python -m app.post_opening_balance_2024 --post
"""
from __future__ import annotations
import argparse
from datetime import date
from sqlalchemy import text
from app import database as db
from app import journal_service as js


# CoA → opening balance at Jan 1, 2024 (positive value = Dr for assets, Cr for
# liabilities; script flips sign). Sourced from statement_registry.previous_balance
# (the earliest-available BF for each facility). For facilities where the earliest
# statement we have is mid-2024 onwards, the BF approximates Jan-1-2024 — the
# pre-statement-period activity (Jan to that statement date) ends up in Retained
# Earnings via the balancing leg. Acceptable approximation given data we have.
OPENING_BALANCES = {
    # ── Assets (Dr) ────────────────────────────────────────────────────
    "1111": ("POSB Savings",            2338.06),     # Confirmed: Jan'24 BF from PDF
    "1112": ("Cash on Hand",            0.00),
    "1113": ("Wise",                    0.00),        # TODO
    "1114": ("Maybank Ar Rihla",        0.00),        # TODO
    "1115": ("SC SuperSalary",          0.00),        # TODO
    "1116": ("DBS Account (YourAgency)", 0.00),        # TODO
    "1211": ("CPF OA",                  0.00),        # TODO: Dec'23 CPF stmt
    "1212": ("CPF SA",                  0.00),        # TODO
    "1213": ("CPF MA",                  0.00),        # TODO
    "1214": ("CPF IS",                  0.00),        # TODO
    "12229": ("Singlife Savvy Invest",  0.00),        # Premium accrual; started 2024+, anchor at 0

    # ── Liabilities (Cr — enter as POSITIVE) ─────────────────────────────
    # Values from statement_registry.previous_balance (earliest available).
    "2111": ("DBS Live Fresh Visa",      874.99),     # Nov 14, 2024 BF
    "2112": ("Maybank Platinum Visa",    177.60),     # Nov 25, 2024 BF
    "2113": ("SC Cashback Visa",        3613.45),     # Nov 17, 2024 BF
    "2114": ("HSBC Visa Revolution",    3792.10),     # Nov 14, 2024 BF
    "2121": ("DBS Cashline",               0.00),     # Nov 10, 2024 BF (= 0)
    "2122": ("UOB CashPlus",             138.97),     # Nov 9, 2024 BF
    "2211": ("SC BT (Balance Transfer)",   0.00),     # Not in registry — likely started later
    "2212": ("GXS FlexiLoan",           6943.99),     # Oct 31, 2024 BF
    "2213": ("Maybank CreditAble",      4055.21),     # Mar 15, 2025 BF
    "2221": ("EZ Loan",                    0.00),     # Instalment-based; origination after Jan 2024
    "2222": ("Lending Bee",                0.00),     # Same
    "2223": ("Sands Credit",               0.00),     # Same
}

# Which CoAs are liabilities (sign = -1 in net Dr-Cr terms when posting)
LIABILITY_PREFIXES = ("2",)


def void_existing_2026_anchor(s) -> int:
    """The misplaced 2026-01-01 opening anchor — void it to avoid double posting."""
    r = s.execute(text("""
        UPDATE journals SET status='voided',
            voided_at=CURRENT_TIMESTAMP,
            voided_reason='Superseded by 2024-01-01 opening anchor (accounting-correct placement)'
        WHERE source_doc='OPENING_BALANCE' AND status='posted'
          AND journal_date >= '2026-01-01' AND journal_date < '2026-02-01'
    """))
    return r.rowcount


def build_opening_journal(s, dry: bool):
    lines = []
    retained_earnings_balance = 0.0
    for coa, (name, amt) in OPENING_BALANCES.items():
        if abs(amt) < 0.01: continue
        if any(coa.startswith(p) for p in LIABILITY_PREFIXES):
            lines.append({"account_code": coa, "credit": amt,
                          "narration": f"{name} — opening liability balance @ 2024-01-01"})
            retained_earnings_balance -= amt
        else:
            lines.append({"account_code": coa, "debit": amt,
                          "narration": f"{name} — opening asset balance @ 2024-01-01"})
            retained_earnings_balance += amt

    # Retained Earnings balancing leg
    if retained_earnings_balance > 0:
        # More assets than liabilities → positive net worth historical → Cr 3100
        lines.append({"account_code": "3100", "credit": retained_earnings_balance,
                      "narration": "Net opening equity (historical net worth prior to 2024)"})
    elif retained_earnings_balance < 0:
        lines.append({"account_code": "3100", "debit": -retained_earnings_balance,
                      "narration": "Net opening equity (historical deficit prior to 2024)"})

    print(f"\n=== Opening journal — 2024-01-01 ({len(lines)} legs) ===")
    total_dr = total_cr = 0
    for l in lines:
        dr = l.get("debit", 0); cr = l.get("credit", 0)
        total_dr += dr; total_cr += cr
        marker = "Dr" if dr else "Cr"
        amt = dr or cr
        print(f"  {marker} {l['account_code']:<6}  ${amt:>11,.2f}  {l['narration'][:60]}")
    print(f"\n  Total Dr: ${total_dr:,.2f}")
    print(f"  Total Cr: ${total_cr:,.2f}")
    print(f"  Balance:  ${(total_dr - total_cr):+,.2f}  (must be 0.00 for the journal to post)")

    if dry: return None
    if not lines:
        print("\nNo non-zero balances — skipping post")
        return None
    jid = js.post_journal(
        s, journal_date=date(2024, 1, 1),
        narration="Opening Balance @ 2024-01-01 — historical net worth via Retained Earnings",
        journal_type="opening",
        lines=lines,
        source_doc="OPENING_BALANCE_2024",
        source_ref="hand-curated from statement BFs",
        external_id="opening_balance:2024-01-01:v1",
    )
    return jid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true")
    ap.add_argument("--void-2026", action="store_true",
                    help="Also void the 2026-01-01 anchor")
    args = ap.parse_args()
    db.init_db()
    s = db.SessionLocal()
    try:
        if args.void_2026:
            n = void_existing_2026_anchor(s) if args.post else 0
            print(f"\n[void-2026] Voided {n} journals (dated 2026-01-01 / OPENING_BALANCE)")
            if args.post: s.commit()
        jid = build_opening_journal(s, dry=not args.post)
        if jid:
            s.commit()
            print(f"\n✓ Posted opening journal as jid={jid}")
        elif args.post:
            print("\n(nothing posted)")
        else:
            print("\nDRY-RUN — re-run with --post to apply.")
    finally:
        s.close()


if __name__ == "__main__":
    main()
