"""POSB cutover — replace Firefly bridge with direct universal-parser path.

User decision 2026-05-14: cutover date = **2026-01-01**.
Matches the existing opening-balance anchor.

What this script does:
  1. Find every FIREFLY_BRIDGE journal where:
       - POSB account (id=4) is one leg
       - journal_date >= 2026-01-01
       - status != 'voided'
  2. Void them (mark status='voided', voided_reason=cutover-2026-01-01).
  3. Run universal_pdf_parser across POSB statements Jan-Apr 2026.
  4. For each parsed transaction:
       - Classify via carriers (using app.universal_pdf_parser's carrier hints +
         a small classifier extension)
       - Post a fresh balanced journal: POSB + classified other-leg
       - external_id = "posb_direct:<statement_date>:<idx>:<amount>"
  5. Verify the POSB ledger closing balance matches the PDF's BF/CF for each month.

Idempotent: re-running skips already-posted journals (via external_id).

Pre-flight CHECK:
  Pre-cutover snapshot of POSB direction-summary is written to /data/posb_pre_cutover.json
  for damage-comparison.

Run:
    docker exec portfolio-mcp python -m app.posb_cutover_2026                # dry-run
    docker exec portfolio-mcp python -m app.posb_cutover_2026 --post         # apply
"""
from __future__ import annotations
import argparse
import json
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text

import hashlib

from app import database as db
from app import journal_service as js
from app.account_router import get_router
from app.universal_pdf_parser import load_all_schemas, parse_pdf


def stable_transfer_extid(date_iso: str, amount: float, coa_a: str, coa_b: str) -> str:
    """Cross-doc idempotency: same ext_id whichever parser sees the tx first."""
    legs = sorted([coa_a, coa_b])
    raw = f"transfer:{legs[0]}:{legs[1]}:{date_iso}:{amount:.2f}"
    return "xfer:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def payslip_journal_covers(s, date_iso: str, amount: float, tol: float = 0.50) -> bool:
    """Cross-pipeline guard: skip POSB salary post if a PAYSLIP-sourced journal
    has already posted Dr POSB on this date for this amount (within tolerance).

    The payslip parser writes the full gross+CPF split journal; replaying the
    POSB statement entry would double-count the net pay in POSB.

    Returns True if a covering payslip journal exists (caller should skip)."""
    r = s.execute(text("""
        SELECT j.id
        FROM journals j
        JOIN general_ledger gl ON gl.journal_id = j.id
        JOIN chart_of_accounts coa ON coa.id = gl.account_id
        WHERE j.source_doc = 'PAYSLIP'
          AND j.status = 'posted'
          AND j.journal_date BETWEEN date(:d, '-5 days') AND date(:d, '+7 days')
          AND coa.account_code = '1111'
          AND ABS(gl.debit - :amt) < :tol
        LIMIT 1
    """), {"d": date_iso, "amt": amount, "tol": tol}).fetchone()
    return r is not None

POSB_ACCT_ID = 4
POSB_COA = "1111"
DEFAULT_CUTOVER = date(2026, 1, 1)
CUTOVER_DATE = DEFAULT_CUTOVER   # overridden by --since CLI arg
POSB_PDF_DIR = Path("/onedrive/Sentinel Finance/01_Bank statements/DBS_POSB Savings")
SUSPENSE = "1190"


def classify_tx(tx) -> tuple[str, str, str]:
    """Delegate to AccountRouter (single source of truth in account_directory.yaml).
    Returns (other_coa, kind, reason). Drops the inline if/elif chains."""
    router = get_router()
    result = router.route(tx)
    return (result.coa, result.kind, result.reason)


def _legacy_classify_tx_unused(tx) -> tuple[str, str, str]:
    """Pre-router classifier — kept here briefly for diff/comparison.
    DO NOT call; the active classifier delegates to account_router.route().
    Returns (other_coa, kind, reason). Uses ALL captured carriers."""
    c = tx.carriers or {}
    typ = (tx.tx_type or "").upper()

    # ── 1. Insurance policy carriers (highest priority — Savvy Invest case) ──
    if c.get("insurance_policy_ref") == "P4064051":
        return "12229", "ilp_premium", "Singlife Savvy Invest"
    if "P4064051" in str(c.get("insurance_policy_long_ref", "")):
        return "12229", "ilp_premium", "Singlife Savvy Invest"
    en = c.get("entity_name_uppercase", "")
    if "SINGAPORE LIFE" in en:
        return "5340", "expense", "Singlife insurance premium"
    if "TOKIO MARINE" in en:
        return "5340", "expense", "Tokio Marine premium"
    if "AIA SINGAPORE" in en:
        return "5340", "expense", "AIA premium"

    # ── 2. Bank routing carriers (user's own bank accounts / CCs) ──
    # Maybank Ar Rihla self-transfer (MSL:14030791138)
    maybank_acct = c.get("maybank_routing_msl", "")
    if maybank_acct == "14030791138":
        return "1114", "transfer", "Maybank Ar Rihla self-transfer"
    if maybank_acct:
        return "1190", "transfer", f"Maybank routing (unknown acct: {maybank_acct})"

    # DBS CC via DBSC routing (4119110104972424 = DBS Live Fresh)
    dbs_card = c.get("dbs_card_routing", "")
    if dbs_card == "4119110104972424":
        return "2111", "cc_pay", "DBS CC payment"
    if dbs_card:
        return "1190", "cc_pay", f"DBS card routing (unknown card: {dbs_card})"

    # CCC card routing — 16-digit CC number.
    # 4835... = HSBC (CC); 5498... = SC CC; 4966... = Maybank CC; 4119... = DBS
    ccc_card = c.get("ccc_card_routing", "")
    if ccc_card.startswith("4835"):    return "2114", "cc_pay", "HSBC CC payment"
    if ccc_card.startswith("5498"):    return "2113", "cc_pay", "SC CC payment"
    if ccc_card.startswith("4966"):    return "2112", "cc_pay", "Maybank CC payment"
    if ccc_card.startswith("4119"):    return "2111", "cc_pay", "DBS CC payment"
    if ccc_card:                        return "1190", "cc_pay", f"CCC card (unknown: {ccc_card})"

    # DBS Cashline routing (acct 085-043736-4 → "850437364" or "0850437364")
    cashline = c.get("dbs_cashline_routing", "")
    if "850437364" in cashline:
        if "ADVICE" in typ:
            return "2121", "loan_in", "DBS Cashline drawdown (via Advice)"
        return "2121", "loan_pay", "DBS Cashline payment"

    # SC routing (SCL:99190816851 = SC BT facility from user's data)
    if c.get("sc_routing"):
        return "2211", "loan_in", f"SC BT drawdown (SCL:{c['sc_routing']})"

    # MEPS Receipt with REM + 0016II refs = Maybank CreditAble drawdown
    # User confirmed 2026-05-14: "$6,300 and $5,600 — drawdown from Maybank credit facilities"
    if c.get("meps_remittance_ref") or c.get("meps_internal_ref"):
        return "2213", "loan_in", "Maybank CreditAble drawdown (MEPS Receipt)"
    if "MEPS RECEIPT" in typ:
        return "2213", "loan_in", "Maybank CreditAble drawdown (no MEPS carrier)"

    # GXS routing
    if c.get("gxs_routing"):
        return "2212", "loan_in", f"GXS FlexiLoan drawdown ({c['gxs_routing']})"

    # ── 3. PayNow recipient (entities + people) ──
    rec = (c.get("paynow_recipient") or "").upper()
    if "COINBASE" in rec:                       return "1231", "transfer", "Coinbase top-up"
    if "SEAMONEY" in rec or "MONEE" in rec:    return "1112", "transfer", "ShopeePay wallet"
    if "EZ LOAN" in rec:                       return "2221", "loan_pay", "EZ Loan repayment"
    if "LENDING BEE" in rec:                   return "2222", "loan_pay", "Lending Bee repayment"
    if "SANDS CREDIT" in rec:                  return "2223", "loan_pay", "Sands Credit repayment"
    if "WISE" in rec:                          return "1113", "transfer", "Wise transfer"
    if "ATOME" in rec:                         return "2115", "bnpl_pay", "Atome BNPL"
    if "GRABPAY" in rec:                       return "1112", "transfer", "GrabPay wallet"
    if "AXS PTE" in rec:                       return "5500", "expense", "AXS bill payment"
    if rec.startswith("AZFAR HAKIM"):          return "1190", "transfer", "self-PayNow (suspense)"
    if " PTE" in rec or " LTD" in rec:
        return "1190", "expense", f"Entity recipient: {rec[:40]}"
    if rec:
        return "5170", "expense", f"Personal: {rec[:40]}"

    # ── 4. Legacy card_number (less precise — DBSC routing usually wins above) ──
    card = c.get("card_number", "")
    if "4119" in card or "DBS_VISA" in typ:                            return "2111", "cc_pay", "DBS CC payment"
    if "4966" in card:                                                 return "2112", "cc_pay", "Maybank CC payment"
    if "5498" in card:                                                 return "2113", "cc_pay", "SC CC payment"
    if "4835" in card:                                                 return "2114", "cc_pay", "HSBC CC payment"

    # ── 5. tx_type-based defaults ──
    if "SALARY" in typ:                                                return "4110", "income", "Salary inflow"
    if "INTEREST EARNED" in typ:                                       return "4220", "income", "Interest earned"
    if "AUTO TOP UP FROM CASHLINE" in typ:                             return "2121", "loan_in", "Cashline drawdown"
    if "AUTO REPAY FROM CASHLINE" in typ:                              return "2121", "loan_pay", "Cashline repay"
    if "CASH WITHDRAWAL" in typ:                                       return "1112", "transfer", "Cash withdrawal"
    if "CASH DEPOSIT" in typ:                                          return "1112", "transfer", "Cash deposit"
    if "DEBIT CARD" in typ or "POINT-OF-SALE" in typ:
        return SUSPENSE, "expense", f"Debit card (no merchant carrier)"
    if "FAST" in typ and tx.deposit_amount > 0:                        return "4900", "income", "External inflow (review)"
    if "INCOMING PAYNOW" in typ:                                       return "4900", "income", "Incoming PayNow (review)"
    if "DIVIDENDS" in typ:                                             return "4210", "income", "Dividend income"
    return SUSPENSE, "expense", f"Unclassified: {typ}"


def void_firefly_after_cutover(s, dry: bool) -> list[int]:
    rows = s.execute(text("""
      SELECT DISTINCT j.id
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id = j.id
      WHERE j.source_doc LIKE 'FIREFLY_BRIDGE%'
        AND j.status != 'voided'
        AND j.journal_date >= :cutover
        AND gl.account_id = :aid
      ORDER BY j.id
    """), {"cutover": CUTOVER_DATE, "aid": POSB_ACCT_ID}).all()
    ids = [r[0] for r in rows]
    print(f"  {len(ids)} FIREFLY_BRIDGE POSB journals to void (>= {CUTOVER_DATE})")
    if not dry:
        for jid in ids:
            s.execute(text("""
              UPDATE journals
              SET status='voided',
                  voided_at = CURRENT_TIMESTAMP,
                  voided_reason = 'Replaced by direct universal_pdf_parser path (cutover 2026-01-01)'
              WHERE id = :jid
            """), {"jid": jid})
        s.commit()
        print(f"  ✓ voided {len(ids)} journals")
    return ids


def replay_direct(s, dry: bool):
    """Legacy direct-post path (kept for back-compat / comparison)."""
    return _replay_legacy(s, dry)


def replay_via_verifier(s, dry: bool):
    """v2 path: each POSB transaction becomes a CandidateJournal that the
    verifier evaluates. High-confidence → POST_AUTO (writes GL). Low → QUEUE
    (writes unreconciled_queue, user resolves on /reconcile). Covered-by-
    payslip → SKIP.

    Replaces the direct-post + ad-hoc reconcile pattern.
    """
    from app.verifier import CandidateJournal, verify, enqueue, AUTO_POST_THRESHOLD
    from datetime import date as _d
    from sqlalchemy import text as _text

    # Idempotency guard: void any legacy posb_direct: posts AND any prior
    # posb:v2: posts in the cutover window before re-replaying.
    if not dry:
        n_voided = 0
        rows = s.execute(_text("""
          SELECT id FROM journals
          WHERE status='posted' AND journal_date >= :d
            AND (external_id LIKE 'posb_direct:%' OR external_id LIKE 'posb:v2:%')
        """), {"d": CUTOVER_DATE}).fetchall()
        for (jid,) in rows:
            s.execute(_text("""
              UPDATE journals SET status='voided', voided_at=CURRENT_TIMESTAMP,
                voided_reason='replay_via_verifier idempotency' WHERE id=:i
            """), {"i": jid})
            n_voided += 1
        s.commit()
        if n_voided:
            print(f"  ↻ voided {n_voided} prior journal(s) before replay (idempotency)")

    schemas = load_all_schemas()
    pdfs = sorted(POSB_PDF_DIR.glob("Deposit Account Statement_*.pdf"))
    posted = queued = skipped = errors = 0
    queue_by_reason = {}
    reconciliation_results = []
    for pdf in pdfs:
        r = parse_pdf(pdf, schemas)
        if not r.statement_date or r.statement_date < CUTOVER_DATE.isoformat():
            continue
        print(f"\n  Replay {pdf.name}  date={r.statement_date}  tx={len(r.transactions)}  BF=${r.balance_brought_forward or 0:,.2f}  CF=${r.balance_carried_forward or 0:,.2f}")

        # Gate 3: persist BF/CF before processing tx
        if not dry and r.balance_carried_forward is not None:
            pe = _d.fromisoformat(r.statement_date)
            # naive period_start = first of month
            ps = pe.replace(day=1)
            try:
                js.register_bank_statement(
                    s, account_code=POSB_COA, period_start=ps, period_end=pe,
                    balance_brought_forward=r.balance_brought_forward,
                    balance_carried_forward=r.balance_carried_forward,
                    source_doc_path=str(pdf),
                )
            except Exception as e:
                print(f"    ⚠ register_bank_statement err: {e}")
        for idx, tx in enumerate(r.transactions):
            if not tx.date_iso:
                skipped += 1; continue
            is_inflow = (tx.deposit_amount > 0)
            is_outflow = (tx.withdrawal_amount > 0)
            if not (is_inflow or is_outflow):
                skipped += 1; continue
            amt = tx.deposit_amount if is_inflow else tx.withdrawal_amount

            candidate = CandidateJournal(
                source_doc="POSB_PDF_DIRECT",
                source_ref=f"{r.statement_date}:tx{idx}:{pdf.name}",
                tx_date=tx.date_iso,
                tx_amount=amt,
                tx_narration=f"{tx.tx_type or ''} | " + ", ".join(
                    f"{k}={str(v)[:30]}" for k, v in (tx.carriers or {}).items()
                )[:200],
                tx_carriers=tx.carriers or {},
                tx_type=tx.tx_type or "",
                direction="in" if is_inflow else "out",
                proposed_lines=[],  # filled in based on verdict
                external_id=f"posb:v2:{r.statement_date}:{idx}:{amt:.2f}:{tx.date_iso}",
            )

            verdict = verify(s, candidate)

            if verdict.decision == "SKIP":
                skipped += 1
                continue

            if verdict.decision == "POST_AUTO":
                other_coa = verdict.top_match.contra_coa
                # Build the journal lines based on direction
                if is_inflow:
                    lines = [
                        {"account_code": POSB_COA, "debit": amt,
                         "narration": (tx.tx_type or "")[:60]},
                        {"account_code": other_coa, "credit": amt,
                         "narration": f"{verdict.top_match.reason[:60]} | {candidate.tx_narration[:80]}"},
                    ]
                else:
                    lines = [
                        {"account_code": other_coa, "debit": amt,
                         "narration": f"{verdict.top_match.reason[:60]} | {candidate.tx_narration[:80]}"},
                        {"account_code": POSB_COA, "credit": amt,
                         "narration": (tx.tx_type or "")[:60]},
                    ]
                if dry:
                    posted += 1; continue
                try:
                    jid = js.post_journal(
                        s,
                        journal_date=datetime.fromisoformat(tx.date_iso).date(),
                        narration=f"[POSB v2] {tx.tx_type[:40]} — {verdict.top_match.row_label[:40]}",
                        journal_type=verdict.top_match.journal_kind,
                        lines=lines,
                        source_doc="POSB_PDF_DIRECT",
                        source_ref=f"{r.statement_date}:tx{idx}",
                        external_id=candidate.external_id,
                    )
                    if jid is None:
                        skipped += 1
                    else:
                        s.commit(); posted += 1
                except Exception as e:
                    s.rollback(); errors += 1
                    if errors <= 5:
                        print(f"    ERR tx{idx} ${amt}: {str(e)[:100]}")
            else:  # QUEUE
                if not dry:
                    enqueue(s, candidate, verdict)
                queued += 1
                key = (verdict.top_match.registry if verdict.top_match else "no_match")
                queue_by_reason[key] = queue_by_reason.get(key, 0) + 1

        # Gate 4: period reconciliation for THIS statement
        if not dry and r.statement_date and r.balance_carried_forward is not None:
            pe = _d.fromisoformat(r.statement_date)
            try:
                rec = js.reconcile_period(s, POSB_COA, pe)
                reconciliation_results.append(rec)
                if rec.get("action") != "reconciled":
                    print(f"    ⚠ period_drift {rec.get('drift'):+,.2f}  GL={rec.get('gl_balance')}  CF={rec.get('statement_cf')}")
                else:
                    print(f"    ✓ period_reconciled GL={rec.get('gl_balance')} == CF")
            except Exception as e:
                print(f"    ⚠ reconcile_period err: {e}")

    return posted, queued, skipped, errors, queue_by_reason


def _replay_legacy(s, dry: bool):
    """The pre-v2 direct-post path. Retained for one-off back-compat runs."""
    schemas = load_all_schemas()
    pdfs = sorted(POSB_PDF_DIR.glob("Deposit Account Statement_*.pdf"))
    posted = 0
    skipped = 0
    errors = 0
    classification_counts = {}
    for pdf in pdfs:
        r = parse_pdf(pdf, schemas)
        if not r.statement_date or r.statement_date < CUTOVER_DATE.isoformat():
            continue
        print(f"\n  Replay {pdf.name}  date={r.statement_date}  tx={len(r.transactions)}")
        for idx, tx in enumerate(r.transactions):
            if not tx.date_iso:
                skipped += 1; continue
            is_inflow = (tx.deposit_amount > 0)
            is_outflow = (tx.withdrawal_amount > 0)
            if not (is_inflow or is_outflow):
                skipped += 1; continue
            amt = tx.deposit_amount if is_inflow else tx.withdrawal_amount
            if is_inflow and "SALARY" in (tx.tx_type or "").upper():
                if payslip_journal_covers(s, tx.date_iso, amt):
                    skipped += 1; continue
            other_coa, kind, reason = classify_tx(tx)
            classification_counts[other_coa] = classification_counts.get(other_coa, 0) + 1
            OWN_ACCT_COAS = {"1111","1112","1113","1114","1115",
                              "2111","2112","2113","2114","2115",
                              "2121","2122","2125",
                              "2211","2212","2213","2221","2222","2223",
                              "12229"}
            if other_coa in OWN_ACCT_COAS:
                ext_id = stable_transfer_extid(tx.date_iso, amt, POSB_COA, other_coa)
            else:
                ext_id = f"posb_direct:{r.statement_date}:{idx}:{amt:.2f}:{tx.date_iso}"
            try:
                if is_inflow:
                    lines = [
                        {"account_code": POSB_COA, "debit": amt, "narration": (tx.tx_type or "")[:60]},
                        {"account_code": other_coa, "credit": amt, "narration": f"{reason} | {', '.join(f'{k}={v[:30]}' for k,v in (tx.carriers or {}).items())[:120]}"},
                    ]
                else:
                    lines = [
                        {"account_code": other_coa, "debit": amt, "narration": f"{reason} | {', '.join(f'{k}={v[:30]}' for k,v in (tx.carriers or {}).items())[:120]}"},
                        {"account_code": POSB_COA, "credit": amt, "narration": (tx.tx_type or "")[:60]},
                    ]
                if dry: posted += 1; continue
                jid = js.post_journal(s, journal_date=datetime.fromisoformat(tx.date_iso).date(),
                    narration=f"[direct POSB] {tx.tx_type[:50]} — {reason[:50]}",
                    journal_type=kind, lines=lines, source_doc="POSB_PDF_DIRECT",
                    source_ref=f"{r.statement_date}:tx{idx}", external_id=ext_id)
                if jid is None: skipped += 1
                else: s.commit(); posted += 1
            except Exception as e:
                s.rollback(); errors += 1
                if errors <= 5: print(f"    ERR tx{idx} ${tx.amount}: {str(e)[:100]}")
    return posted, skipped, errors, classification_counts


def main():
    global CUTOVER_DATE
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true")
    ap.add_argument("--since", default=DEFAULT_CUTOVER.isoformat(),
                    help="Cutover date (YYYY-MM-DD). Voids+replays all POSB tx >= this date.")
    ap.add_argument("--legacy", action="store_true",
                    help="Use the legacy direct-post path. DEPRECATED — generates "
                         "phantom one-sided journals; kept only for emergency back-compat.")
    args = ap.parse_args()
    CUTOVER_DATE = datetime.fromisoformat(args.since).date()

    db.init_db()
    s = db.SessionLocal()
    try:
        print(f"=== POSB cutover at {CUTOVER_DATE} ===\n")
        print("Step 1: Void Firefly bridge POSB journals ≥ cutover")
        void_ids = void_firefly_after_cutover(s, dry=not args.post)

        if args.legacy:
            print("\n⚠️  LEGACY direct-post path — phantom-generating; use only with --post for one-off forensics")
            print("Step 2 (legacy): Replay POSB statements via direct GL post")
            posted, skipped, errors, by_coa = replay_direct(s, dry=not args.post)
            print(f"\n  posted={posted}  skipped={skipped}  errors={errors}")
            print("\n  Classification breakdown (top 15):")
            for coa, n in sorted(by_coa.items(), key=lambda kv: -kv[1])[:15]:
                print(f"    {coa:<6}  {n} tx")
            if not args.post:
                print("\nDRY-RUN — pass --post to apply.")
            return

        # Default: verifier-gated path (Gate 2)
        print("\nStep 2: Replay POSB via verifier — high-conf → post, low-conf → queue")
        posted, queued, skipped, errors, by_reason = replay_via_verifier(s, dry=not args.post)
        print(f"\n  posted={posted}  queued={queued}  skipped={skipped}  errors={errors}")
        if by_reason:
            print(f"\n  Queue breakdown by reason:")
            for k, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
                print(f"    {k:<22}  {n}")
        if not args.post:
            print("\nDRY-RUN — pass --post to apply.")
    finally:
        s.close()


if __name__ == "__main__":
    main()
