"""Credit Card cutover (Phase 4 of decouple) — unified DBS / Maybank / SC / HSBC CC.

For each CC PDF:
  1. Parse via universal_pdf_parser (uses schema's amount_sign rule for CR suffix)
  2. Route via account_router
  3. Post journals:
     - PAYMENT (CR-suffixed): DR CC liability ↓, CR contra_coa (POSB)
       External_id = transfer-pair sorted ext_id → SAME id as POSB's "DR CC, CR POSB",
       so idempotent skip if POSB cutover already posted it. **PREVENTS DOUBLE COUNT.**
     - CHARGE: DR expense_coa, CR CC liability ↑

Cards (mapped via account_directory.yaml):
  4119...2424 → 2111 DBS Live Fresh Visa  (file: DBS CC *.pdf)
  4966...7004 → 2112 Maybank Platinum     (file: Platinum Visa Card *.pdf)
  5498...8810 → 2113 SC Cashback          (file: ?)
  4835...5159 → 2114 HSBC Visa Revolution (file: HSBC CC *.pdf — image-PDF, OCR)
"""
from __future__ import annotations
import argparse
import hashlib
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text

from app import database as db
from app import journal_service as js
from app.account_router import get_router
from app.universal_pdf_parser import load_all_schemas, parse_pdf

DEFAULT_CUTOVER = date(2026, 1, 1)
CUTOVER_DATE = DEFAULT_CUTOVER
SUSPENSE = "1190"

# Card-to-CoA + filename family for cutover dispatch
CC_FAMILIES = {
    "dbs-cc":     {"coa": "2111", "name": "DBS Live Fresh Visa",      "card": "4119110104972424"},
    "maybank-cc": {"coa": "2112", "name": "Maybank Platinum Visa",    "card": "4966430904927004"},
    # SC CC schema TBD — Apr 2026 SC SuperSalary showed TO CARD 5498... but no SC CC stmt PDF in OneDrive
    "hsbc-cc":    {"coa": "2114", "name": "HSBC Visa Revolution",      "card": "4835850017835159"},
}


def stable_transfer_extid(date_iso: str, amount: float, coa_a: str, coa_b: str) -> str:
    legs = sorted([coa_a, coa_b])
    raw = f"transfer:{legs[0]}:{legs[1]}:{date_iso}:{amount:.2f}"
    return "xfer:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def cc_acct_id(s, coa: str) -> int:
    r = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code=:c"),
                   {"c": coa}).fetchone()
    return r[0] if r else None


def void_firefly_after_cutover(s, cc_coa: str, dry: bool) -> int:
    aid = cc_acct_id(s, cc_coa)
    if not aid: return 0
    rows = s.execute(text("""
      SELECT DISTINCT j.id
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id = j.id
      WHERE j.source_doc LIKE 'FIREFLY_BRIDGE%'
        AND j.status != 'voided'
        AND j.journal_date >= :cutover
        AND gl.account_id = :aid
    """), {"cutover": CUTOVER_DATE, "aid": aid}).all()
    ids = [r[0] for r in rows]
    if not dry:
        for jid in ids:
            s.execute(text("""
              UPDATE journals SET status='voided', voided_at=CURRENT_TIMESTAMP,
                  voided_reason='Replaced by direct cc_cutover (cutover 2026-01-01)'
              WHERE id = :jid
            """), {"jid": jid})
        s.commit()
    return len(ids)


def replay_cc(s, pdfs: list[Path], cc_coa: str, cc_name: str, dry: bool):
    schemas = load_all_schemas()
    router = get_router()
    posted = 0
    skipped = 0
    cross_doc_skipped = 0
    errors = 0
    by_coa: dict[str, int] = {}

    for pdf in pdfs:
        try:
            r = parse_pdf(pdf, schemas)
        except Exception as e:
            print(f"  [parse err] {pdf.name}: {e}")
            continue
        if not r.statement_date or r.statement_date < CUTOVER_DATE.isoformat():
            continue
        print(f"\n  Replay {pdf.name[:55]}  schema={r.schema_name}  date={r.statement_date}  tx={len(r.transactions)}")
        for idx, tx in enumerate(r.transactions):
            if not tx.date_iso:
                skipped += 1; continue
            is_payment = (tx.deposit_amount > 0)   # CR-suffixed
            is_charge = (tx.withdrawal_amount > 0)  # default for CC
            if not (is_payment or is_charge):
                skipped += 1; continue
            amt = tx.deposit_amount if is_payment else tx.withdrawal_amount

            result = router.route(tx)
            other_coa, kind, reason, confidence = result.as_tuple()

            # CC-charge lifestyle lumping: any charge that didn't get a specific
            # match defaults to 5190 (lifestyle expense) rather than 1190 (suspense).
            # Reasoning: CC charges ARE by definition expenses — the only question is
            # which category. Lumping unknowns to lifestyle is more accurate than
            # holding them in counterparty-unknown suspense (which implies asset move).
            # Specific finance-charge handling preserved (5700/5410).
            if is_charge and confidence < 50 and other_coa in ("1190", "4900"):
                # Finance / late fee detection
                tx_type_up = (tx.tx_type or "").upper()
                if "FINANCECHARGE" in tx_type_up or "FINANCE CHARGE" in tx_type_up:
                    other_coa, kind, reason = "5410", "expense", "CC Finance Charges"
                elif "LATE FEE" in tx_type_up or "LATEFEE" in tx_type_up:
                    other_coa, kind, reason = "5450", "expense", "CC Late Payment Fee"
                else:
                    other_coa, kind, reason = "5190", "expense", f"CC lifestyle (unmatched: {tx.tx_type[:40]})"
                confidence = 50   # default-tier confidence

            # CC-payment cross-doc dedup: payments without specific routing default
            # to POSB (1111) as source. This makes HSBC-side and POSB-side parsers
            # generate the SAME transfer-pair ext_id, so re-runs idempotent-skip
            # rather than double-counting the liability reduction.
            if is_payment and confidence < 50 and other_coa in ("1190", "4900"):
                other_coa = "1111"
                kind = "cc_pay"
                reason = "CC payment (assumed POSB — cross-doc dedup'd)"
                confidence = 50

            by_coa[other_coa] = by_coa.get(other_coa, 0) + 1

            # Cross-doc transfer-pair external_id (for payments)
            if other_coa in ("1111", "1114", "1115", "1113", "1112"):
                ext_id = stable_transfer_extid(tx.date_iso, amt, cc_coa, other_coa)
            else:
                ext_id = f"cc_direct:{cc_coa}:{r.statement_date}:{idx}:{amt:.2f}:{tx.date_iso}"

            try:
                if is_payment:
                    # CC liability DOWN, contra (POSB) asset DOWN
                    lines = [
                        {"account_code": cc_coa, "debit": amt,
                         "narration": f"{cc_name} — payment received [conf={confidence}]"},
                        {"account_code": other_coa, "credit": amt,
                         "narration": f"{reason} | {tx.tx_type[:50]}"},
                    ]
                else:
                    # Charge: liability UP, expense UP
                    lines = [
                        {"account_code": other_coa, "debit": amt,
                         "narration": f"{reason} [conf={confidence}] | {tx.tx_type[:60]}"},
                        {"account_code": cc_coa, "credit": amt,
                         "narration": f"{cc_name} — charge"},
                    ]
                if dry:
                    posted += 1; continue
                jid = js.post_journal(
                    s,
                    journal_date=datetime.fromisoformat(tx.date_iso).date(),
                    narration=f"[direct {cc_name[:20]}] {tx.tx_type[:50]}",
                    journal_type=kind,
                    lines=lines,
                    source_doc=f"CC_PDF_DIRECT:{cc_coa}",
                    source_ref=f"{r.statement_date}:tx{idx}",
                    external_id=ext_id,
                )
                if jid is None:
                    skipped += 1
                    if ext_id.startswith("xfer:"):
                        cross_doc_skipped += 1
                else:
                    s.commit()
                    posted += 1
            except Exception as e:
                s.rollback()
                errors += 1
                if errors <= 3:
                    print(f"    ERR tx{idx}: {str(e)[:120]}")
    return posted, skipped, errors, cross_doc_skipped, by_coa


def main():
    global CUTOVER_DATE
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true")
    ap.add_argument("--card", help="Limit to one card: dbs-cc / maybank-cc / hsbc-cc")
    ap.add_argument("--since", default=DEFAULT_CUTOVER.isoformat(),
                    help="Cutover date (YYYY-MM-DD). Voids+replays all CC tx >= this date.")
    args = ap.parse_args()
    CUTOVER_DATE = datetime.fromisoformat(args.since).date()

    db.init_db()
    s = db.SessionLocal()
    try:
        cc_root = Path("/onedrive/Sentinel Finance/02_Credit card statements")
        for fam_key, fam in CC_FAMILIES.items():
            if args.card and args.card != fam_key: continue
            print(f"\n══════════════════════════════════════════════════════")
            print(f"  {fam_key.upper()}  CoA {fam['coa']}  {fam['name']}")
            print(f"══════════════════════════════════════════════════════")
            # Pick PDF files matching this family
            if fam_key == "dbs-cc":
                pdfs = list(cc_root.rglob("DBS CC*.pdf")) + list(cc_root.rglob("Credit Cards Consolidated*.pdf"))
            elif fam_key == "maybank-cc":
                pdfs = list(cc_root.rglob("Platinum Visa Card*.pdf"))
            elif fam_key == "hsbc-cc":
                pdfs = list(cc_root.rglob("HSBC CC*.pdf")) + list(cc_root.rglob("HSBC Jan*.pdf")) + list(cc_root.rglob("HSBC Feb*.pdf")) + list(cc_root.rglob("HSBC Mar*.pdf"))
            else:
                pdfs = []
            pdfs = sorted(set(pdfs))
            print(f"  {len(pdfs)} PDFs found")

            voided = void_firefly_after_cutover(s, fam["coa"], dry=not args.post)
            print(f"  {voided} FIREFLY_BRIDGE journals to void (>= {CUTOVER_DATE})")
            posted, skipped, errors, xskip, by_coa = replay_cc(
                s, pdfs, fam["coa"], fam["name"], dry=not args.post
            )
            print(f"\n  posted={posted}  skipped={skipped}  cross_doc_skipped={xskip}  errors={errors}")
            if by_coa:
                print(f"  Top counterparty CoAs:")
                for coa, n in sorted(by_coa.items(), key=lambda kv: -kv[1])[:8]:
                    print(f"    {coa:<6}  {n} tx")

        if not args.post:
            print("\nDRY-RUN — pass --post to apply.")
    finally:
        s.close()


if __name__ == "__main__":
    main()
