"""Drain the 1190 suspense bucket using carrier data already captured.

After the 2026-01-01 cutover, ~386 POSB_PDF_DIRECT journals sit in 1190
suspense because the classifier didn't have a specific rule for the recipient.
But the universal parser preserved the recipient name in the journal narration
("Entity recipient: COINBASE SINGAPORE PTE. LTD.", "Personal: YEO LEE YIN", etc.).

This script mines those narrations and reclassifies to specific CoAs.

Rule strategy (ordered):
  - Each rule = (pattern_in_narration, target_coa, label)
  - Walk current 1190 POSB_PDF_DIRECT journals
  - For each matching rule: void original, post replacement with same amount

Idempotent via external_id 'reclass:<orig_jid>'.
"""
import argparse
import re
from datetime import datetime
from sqlalchemy import text
from app import database as db
from app import journal_service as js

POSB_COA = "1111"
SUSPENSE = "1190"

# Drain rules: each tuple = (regex_pattern, target_coa, kind, label)
# Patterns match against the OTHER-leg narration of the suspense journal.
# Order matters — first match wins.
RULES = [
    (r"COINBASE SINGAPORE",            "1231", "transfer",  "Coinbase top-up"),
    (r"WISE ASIA[\s-]*PACIFIC|WISE\b", "1113", "transfer",  "Wise transfer"),
    (r"SEAMONEY|MONEE\b",              "1112", "transfer",  "ShopeePay wallet"),
    (r"APAYLATER",                     "2115", "bnpl_pay",  "SPayLater repayment"),
    (r"GRABPAY TOPUP",                 "1112", "transfer",  "GrabPay wallet top-up"),

    # Loan repayments
    (r"EZ LOAN PTE",                   "2221", "loan_pay",  "EZ Loan repayment"),
    (r"LENDING BEE",                   "2222", "loan_pay",  "Lending Bee repayment"),
    (r"SANDS CREDIT",                  "2223", "loan_pay",  "Sands Credit repayment"),

    # Card payments (carriers may include card numbers)
    (r"4119[-\s]?\d{4}|DBS VISA DIRECT|DBS_VISA", "2111", "cc_pay", "DBS CC payment"),
    (r"4966[-\s]?\d{4}",               "2112", "cc_pay",    "Maybank CC payment"),
    (r"5498[-\s]?\d{4}",               "2113", "cc_pay",    "SC CC payment"),
    (r"4835[-\s]?\w{4}",               "2114", "cc_pay",    "HSBC CC payment"),

    # Insurance (Singlife not Savvy Invest)
    (r"SINGAPORE LIFE LTD",            "5340", "expense",   "Singlife insurance premium"),
    (r"TOKIO MARINE",                  "5340", "expense",   "Tokio Marine premium"),

    # Food/restaurants (entities)
    (r"QASHIER-",                      "5110", "expense",   "F&B (Qashier merchant)"),
    (r"FOODPANDA|GRABFOOD|DELIVEROO",  "5111", "expense",   "Food delivery"),
    (r"FAIRPRICE|NTUC|COLD STORAGE|GIANT|SHENGSIONG|SHENG\s*SIONG",
                                       "5120", "expense",   "Groceries"),
    (r"TUCKSHOP|KOPITIAM|KOUFU|BELACAN|ENAK ENAK|PETER F AND B",
                                       "5110", "expense",   "F&B"),

    # Subscriptions / digital
    (r"ANTHROPIC|CLAUDE\.AI|MICROSOFT|TWITCH|GOOGLE.*YOUTUBE|NETFLIX|SPOTIFY|DOCKER",
                                       "5200", "expense",   "Subscription"),

    # Transport
    (r"GRAB\*|GO-?JEK|RYDE|TADA|EASYVAN|EZ-?LINK",
                                       "5131", "expense",   "Transport"),
    (r"SHELL|ESSO|CALTEX|SPC PETROL",  "5132", "expense",   "Fuel"),

    # Utilities
    (r"VIEWQWEST|SINGTEL|STARHUB|M1\s|SIMBA TELECOM|CIRCLES\.LIFE",
                                       "5141", "expense",   "Internet/mobile"),
    (r"SP\s+(SERVICES|GROUP|UTILITIES)|TUAS POWER|GENECO",
                                       "5143", "expense",   "Electricity"),

    # Shopping
    (r"SHOPEE\b",                      "5160", "expense",   "Shopee"),
    (r"LAZADA",                        "5161", "expense",   "Lazada"),
    (r"AMAZON|SHEIN|TEMU|EBAY|ALIEXPRESS", "5161", "expense", "Online shopping"),

    # Govt / fees
    (r"AXS PTE",                       "5500", "expense",   "AXS bill payment"),
    (r"IRAS|Inland Revenue",           "5500", "expense",   "IRAS tax"),

    # Self-transfers — these stay in 1190 but with better labelling
    # (won't reclass, just narration-improve in a future pass)
]


def find_suspense_targets(s):
    """Pull both the journal narration AND the line-level narration on the 1190 leg.
    Carrier data (recipient names, policy refs, etc.) lives in the LINE narration."""
    rows = s.execute(text("""
      SELECT j.id, j.journal_date,
             j.narration AS j_narr,
             gl.narration AS gl_narr,
             j.source_ref,
             gl.debit, gl.credit
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id=j.id
      JOIN chart_of_accounts coa ON coa.id=gl.account_id
      WHERE j.source_doc='POSB_PDF_DIRECT'
        AND j.status='posted'
        AND coa.account_code=:susp
      ORDER BY j.journal_date
    """), {"susp": SUSPENSE}).all()
    return rows


def classify(narration: str) -> tuple:
    for pat, coa, kind, label in RULES:
        if re.search(pat, narration, re.IGNORECASE):
            return (coa, kind, label)
    return (None, None, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true")
    args = ap.parse_args()

    db.init_db()
    s = db.SessionLocal()
    try:
        rows = find_suspense_targets(s)
        print(f"Found {len(rows)} POSB_PDF_DIRECT journals in {SUSPENSE} suspense\n")

        # Match each
        matched = []
        unmatched = []
        for r in rows:
            jid = r[0]
            jdate = r[1]
            j_narr = r[2] or ""
            gl_narr = r[3] or ""
            ref = r[4] or ""
            dr = float(r[5] or 0)
            cr = float(r[6] or 0)
            amt = dr if dr > 0 else cr
            is_outflow = (dr > 0)   # 1190 was debited → outflow from POSB perspective
            # Match against BOTH the journal narration and the line narration (carriers)
            combined = f"{j_narr} || {gl_narr}"
            narration = combined
            coa, kind, label = classify(combined)
            if coa:
                matched.append((jid, jdate, narration, ref, amt, is_outflow, coa, kind, label))
            else:
                unmatched.append((jid, jdate, narration, amt))

        print(f"Will reclassify: {len(matched)}")
        print(f"Will leave in suspense: {len(unmatched)}\n")

        # Summary by target
        from collections import Counter
        by_coa = Counter()
        for m in matched:
            by_coa[m[6]] += m[4]
        print("Reclass summary by target CoA:")
        for coa, sum_ in sorted(by_coa.items(), key=lambda kv: -kv[1]):
            print(f"  {coa}: ${sum_:>10,.2f}")
        print()

        if not args.post:
            print("DRY-RUN — pass --post to apply.")
            return

        posted = 0
        for jid, jdate, narr, ref, amt, is_out, coa, kind, label in matched:
            if isinstance(jdate, str):
                jdate = datetime.strptime(jdate[:10], "%Y-%m-%d").date()
            ext_id = f"reclass:posb_direct:{jid}"
            try:
                # Post replacement: same direction, swap 1190 for the specific CoA
                if is_out:
                    lines = [
                        {"account_code": coa, "debit": amt,
                         "narration": f"[reclass→{coa}] {label} | {narr[:60]}"},
                        {"account_code": POSB_COA, "credit": amt,
                         "narration": f"POSB outflow (reclass from suspense)"},
                    ]
                else:
                    lines = [
                        {"account_code": POSB_COA, "debit": amt,
                         "narration": f"POSB inflow (reclass from suspense)"},
                        {"account_code": coa, "credit": amt,
                         "narration": f"[reclass→{coa}] {label} | {narr[:60]}"},
                    ]
                new_jid = js.post_journal(
                    s, journal_date=jdate, narration=f"[reclass→{coa}] {label}",
                    journal_type=kind, lines=lines,
                    source_doc="POSB_PDF_RECLASS",
                    source_ref=f"orig_jid:{jid}",
                    external_id=ext_id,
                )
                if new_jid is None:
                    continue
                # Void the original
                s.execute(text("""
                  UPDATE journals SET status='voided',
                    voided_at=CURRENT_TIMESTAMP,
                    voided_reason='Reclassified out of 1190 suspense by carrier rule'
                  WHERE id=:jid
                """), {"jid": jid})
                s.commit()
                posted += 1
            except Exception as e:
                s.rollback()
                print(f"  ERR jid={jid}: {str(e)[:80]}")
        print(f"\nReclassified {posted} of {len(matched)} matched suspense journals")

        if unmatched:
            print(f"\n{len(unmatched)} still in suspense — top 10 by amount:")
            for jid, jdate, narr, amt in sorted(unmatched, key=lambda x: -x[3])[:10]:
                print(f"  ${amt:>8,.2f}  {jdate}  {narr[:80]}")
    finally:
        s.close()


if __name__ == "__main__":
    main()
