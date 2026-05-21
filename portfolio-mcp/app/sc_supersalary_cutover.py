"""SC SuperSalary → Sentinel GL direct posting (Phase 3 of decouple).

Same pattern as POSB + Maybank cutovers:
- Bank account = SC SuperSalary 01-1-783334-7, CoA 1115
- Schema sc-savings.yaml drives universal_pdf_parser
- account_router resolves the other-leg via account_directory.yaml
- Cross-doc idempotency: transfer-pair ext_id keyed by sorted(coa_a, coa_b)
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

SC_ACCT_ID = 8                   # SC SuperSalary (1115) — verify via chart_of_accounts
SC_COA = "1115"
CUTOVER_DATE = date(2026, 1, 1)
SOURCE_DIR = Path("/onedrive/Sentinel Finance/01_Bank statements/Standard Chartered")
SUSPENSE = "1190"


def stable_transfer_extid(date_iso: str, amount: float, coa_a: str, coa_b: str) -> str:
    legs = sorted([coa_a, coa_b])
    raw = f"transfer:{legs[0]}:{legs[1]}:{date_iso}:{amount:.2f}"
    return "xfer:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def void_firefly_after_cutover(s, dry: bool) -> int:
    rows = s.execute(text("""
      SELECT DISTINCT j.id
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id = j.id
      WHERE j.source_doc LIKE 'FIREFLY_BRIDGE%'
        AND j.status != 'voided'
        AND j.journal_date >= :cutover
        AND gl.account_id = :aid
    """), {"cutover": CUTOVER_DATE, "aid": SC_ACCT_ID}).all()
    ids = [r[0] for r in rows]
    print(f"  {len(ids)} FIREFLY_BRIDGE SC SuperSalary journals to void (>= {CUTOVER_DATE})")
    if not dry:
        for jid in ids:
            s.execute(text("""
              UPDATE journals
              SET status='voided', voided_at=CURRENT_TIMESTAMP,
                  voided_reason='Replaced by direct sc_supersalary_cutover (cutover 2026-01-01)'
              WHERE id = :jid
            """), {"jid": jid})
        s.commit()
        print(f"  ✓ voided {len(ids)} journals")
    return len(ids)


def replay_direct(s, dry: bool):
    schemas = load_all_schemas()
    router = get_router()
    # SC PDFs come in two filename formats
    pdfs = sorted(list(SOURCE_DIR.glob("Standard Chartered Savings*.pdf")) +
                  list(SOURCE_DIR.glob("eStatement_Standard Chartered*.pdf")))
    posted = 0
    skipped = 0
    cross_doc_skipped = 0
    errors = 0
    classification_counts: dict[str, int] = {}

    for pdf in pdfs:
        r = parse_pdf(pdf, schemas)
        if r.schema_name != "sc-savings":
            print(f"  [skip] {pdf.name} matched schema {r.schema_name}")
            continue
        if not r.statement_date or r.statement_date < "2026-01-01":
            continue
        print(f"\n  Replay {pdf.name[:60]}  date={r.statement_date}  tx={len(r.transactions)}  BF=${r.balance_brought_forward or 0:,.2f}  CF=${r.balance_carried_forward or 0:,.2f}")
        if not dry and r.balance_carried_forward is not None:
            from datetime import date as _d
            pe = _d.fromisoformat(r.statement_date)
            try:
                js.register_bank_statement(
                    s, account_code=SC_COA,
                    period_start=pe.replace(day=1), period_end=pe,
                    balance_brought_forward=r.balance_brought_forward,
                    balance_carried_forward=r.balance_carried_forward,
                    source_doc_path=str(pdf),
                )
            except Exception as e:
                print(f"    ⚠ register err: {e}")
        for idx, tx in enumerate(r.transactions):
            if not tx.date_iso:
                skipped += 1
                continue
            is_inflow = (tx.deposit_amount > 0)
            is_outflow = (tx.withdrawal_amount > 0)
            if not (is_inflow or is_outflow):
                skipped += 1
                continue
            amt = tx.deposit_amount if is_inflow else tx.withdrawal_amount

            result = router.route(tx)
            other_coa, kind, reason, confidence = result.as_tuple()
            classification_counts[other_coa] = classification_counts.get(other_coa, 0) + 1

            # Cross-doc idempotency for own-account transfers
            if other_coa in ("1111", "1114", "1115", "1113", "1112", "2111", "2112",
                              "2113", "2114", "2121", "2122", "2211", "2212", "2213"):
                ext_id = stable_transfer_extid(tx.date_iso, amt, SC_COA, other_coa)
            else:
                ext_id = f"sc_direct:{r.statement_date}:{idx}:{amt:.2f}:{tx.date_iso}"

            try:
                if is_inflow:
                    lines = [
                        {"account_code": SC_COA, "debit": amt,
                         "narration": (tx.tx_type or "")[:60]},
                        {"account_code": other_coa, "credit": amt,
                         "narration": f"{reason} [conf={confidence}] | "
                                       f"{', '.join(f'{k}={str(v)[:30]}' for k,v in (tx.carriers or {}).items())[:120]}"},
                    ]
                else:
                    lines = [
                        {"account_code": other_coa, "debit": amt,
                         "narration": f"{reason} [conf={confidence}] | "
                                       f"{', '.join(f'{k}={str(v)[:30]}' for k,v in (tx.carriers or {}).items())[:120]}"},
                        {"account_code": SC_COA, "credit": amt,
                         "narration": (tx.tx_type or "")[:60]},
                    ]
                if dry:
                    posted += 1
                    continue
                jid = js.post_journal(
                    s,
                    journal_date=datetime.fromisoformat(tx.date_iso).date(),
                    narration=f"[direct SC] {tx.tx_type[:50]} — {reason[:50]}",
                    journal_type=kind,
                    lines=lines,
                    source_doc="SC_PDF_DIRECT",
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
                if errors <= 5:
                    print(f"    ERR tx{idx} ${amt}: {str(e)[:120]}")
        if not dry and r.statement_date and r.balance_carried_forward is not None:
            from datetime import date as _d
            pe = _d.fromisoformat(r.statement_date)
            try:
                rec = js.reconcile_period(s, SC_COA, pe)
                if rec.get("action") != "reconciled":
                    print(f"    ⚠ period_drift {rec.get('drift'):+,.2f}")
                else:
                    print(f"    ✓ period_reconciled")
            except Exception as e:
                print(f"    ⚠ reconcile err: {e}")
    return posted, skipped, errors, cross_doc_skipped, classification_counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true")
    args = ap.parse_args()

    db.init_db()
    s = db.SessionLocal()
    try:
        # Verify SC CoA mapping
        r = s.execute(text("SELECT id, account_code, account_name FROM chart_of_accounts WHERE account_code='1115'")).fetchone()
        if r:
            global SC_ACCT_ID
            SC_ACCT_ID = r[0]
            print(f"  SC SuperSalary CoA: id={r[0]}, code={r[1]}, name={r[2]}")

        print(f"\n=== SC SuperSalary cutover at {CUTOVER_DATE} ===\n")
        print("Step 1: Void Firefly bridge SC SuperSalary journals ≥ cutover")
        void_firefly_after_cutover(s, dry=not args.post)

        print("\nStep 2: Replay SC statements 2026 via universal parser + router")
        posted, skipped, errors, xskip, by_coa = replay_direct(s, dry=not args.post)

        print(f"\n  posted={posted}  skipped={skipped}  cross_doc_skipped={xskip}  errors={errors}")
        if by_coa:
            print("\n  Classification breakdown:")
            for coa, n in sorted(by_coa.items(), key=lambda kv: -kv[1]):
                print(f"    {coa:<6}  {n} tx")
        if not args.post:
            print("\nDRY-RUN — pass --post to apply.")
    finally:
        s.close()


if __name__ == "__main__":
    main()
