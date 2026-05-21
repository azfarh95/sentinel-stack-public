"""Pre-posting verifier — the inline reconciler.

Given a CandidateJournal (proposed by a parser, NOT yet posted), walks every
canonical registry, computes a confidence score, and returns a Verdict:

  • POST_AUTO   confidence ≥ AUTO_POST_THRESHOLD → caller posts the journal
                                                   directly to GL
  • QUEUE       confidence below threshold → write to unreconciled_queue;
                                             user resolves on /reconcile
  • SKIP        a covering journal already exists (cross-doc dedup)
                                             → caller does nothing

The verifier consults (in order of confidence weight):
  1. payslip_registry         → did a payslip already cover this POSB inflow?
  2. insurance_policy_registry → does the amount + carrier match a policy?
  3. ilp_portfolio_snapshot   → cross-ref NAV adjustments (rare path)
  4. credit_facilities        → is this a known facility drawdown / repayment?
  5. payment_schedule         → is this a dated scheduled payment?
  6. subscription_registry    → recurring vendor subscription
  7. account_router rules     → identifier-based routing (tx_type + carriers)
  8. lifestyle-lump rules     → final fallback for debit-card / FAST+PayNow

Confidence levels (top wins):
  100 — exact identifier match (policy_ref, card #, account #, MSL/SCL routing)
   90 — registry row with amount within tolerance + date within window
   75 — amount-pinpoint match against an active obligation (loose)
   60 — tx_type marker fallback (SALARY → 4110, FINANCE CHARGES → 5410, ...)
   50 — lifestyle lump (debit card / POS / bill payment / FAST+PayNow outflow)
    0 — no match

A confidence ≥ AUTO_POST_THRESHOLD (default 75) → POST_AUTO.
Below that → QUEUE for user triage.

This module replaces the post-hoc reconciler model (salary_reconciler,
recurring_reconciler) which voided and re-posted journals after the fact.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Optional, Literal

from sqlalchemy import text


AUTO_POST_THRESHOLD = 75


# ── Data classes ─────────────────────────────────────────────────────────────


@dataclass
class CandidateJournal:
    """A proposed journal entry, not yet posted."""
    source_doc: str                   # 'POSB_PDF_DIRECT' | 'CC_PDF_DIRECT:2114' | 'PAYSLIP' | ...
    source_ref: str                    # PDF path + line index, etc.
    tx_date: str                       # ISO YYYY-MM-DD
    tx_amount: float
    tx_narration: str = ""
    tx_carriers: dict = field(default_factory=dict)
    tx_type: str = ""
    direction: Literal["in", "out", "unknown"] = "unknown"
    # Caller may pre-build the proposed lines (Dr/Cr) if it has strong context;
    # the verifier will respect or override based on match strength.
    proposed_lines: list[dict] = field(default_factory=list)
    # Idempotency hint: if caller knows this tx has been seen, pass external_id.
    external_id: Optional[str] = None


@dataclass
class Match:
    """One candidate match from a registry."""
    registry: str                     # 'payslip' | 'insurance' | 'credit_facility' | ...
    row_id: int                        # registry row id
    row_label: str                     # human-readable
    contra_coa: str                    # the CoA this match would post against
    journal_kind: str                  # 'expense' | 'transfer' | 'cc_pay' | 'loan_pay' | 'ilp_premium' | 'income'
    confidence: int                    # 0-100
    reason: str                        # debug


@dataclass
class Verdict:
    decision: Literal["POST_AUTO", "QUEUE", "SKIP"]
    top_match: Optional[Match]
    all_matches: list[Match]
    confidence: int                    # = top_match.confidence (0 if no matches)
    reason: str                        # one-line summary


# ── Helpers ──────────────────────────────────────────────────────────────────


def _amount_match(a: float, b: float, tol: float = 0.50) -> bool:
    return abs(a - b) <= tol


def _within_days(d1: str, d2: str, days: int) -> bool:
    """Are two ISO dates within ±days of each other?"""
    try:
        a = datetime.fromisoformat(str(d1)[:10]).date()
        b = datetime.fromisoformat(str(d2)[:10]).date()
        return abs((a - b).days) <= days
    except Exception:
        return False


# ── Registry probes ──────────────────────────────────────────────────────────


def _probe_payslip(s, c: CandidateJournal) -> Optional[Match]:
    """Salary inflow already covered by a PAYSLIP journal? If yes, SKIP.

    This is the cross-doc dedup that salary_reconciler.fix_dups used to do
    post-hoc. Now runs inline.
    """
    if c.direction != "in" or c.tx_amount < 100:
        return None
    if "SALARY" not in (c.tx_type or "").upper():
        return None
    r = s.execute(text("""
      SELECT pr.id, pr.employer, pr.net_pay, pr.payment_date
      FROM payslip_registry pr
      WHERE pr.journal_id IS NOT NULL
        AND ABS(pr.net_pay - :amt) < 5.0
        AND pr.payment_date BETWEEN date(:d, '-5 days') AND date(:d, '+7 days')
      ORDER BY ABS(julianday(pr.payment_date) - julianday(:d)) LIMIT 1
    """), {"amt": c.tx_amount, "d": c.tx_date}).fetchone()
    if not r:
        return None
    return Match(
        registry="payslip",
        row_id=r[0],
        row_label=f"{r[1]} payslip net ${r[2]:,.2f} dated {r[3]}",
        contra_coa="__SKIP__",       # signal: don't post — covered by PAYSLIP
        journal_kind="skip_covered",
        confidence=100,
        reason="payslip_journal_covers — POSB inflow already represented by payslip-side journal",
    )


def _probe_insurance(s, c: CandidateJournal) -> list[Match]:
    """Match against insurance_policy_registry (term life / whole life / health / ILP)."""
    out = []
    rows = s.execute(text("""
      SELECT id, policy_ref, insurer, kind, premium_amount, premium_frequency,
             contra_coa, contra_coa_pnl_slice, identifier_patterns
      FROM insurance_policy_registry
      WHERE status = 'active'
    """)).all()
    narr_up = (c.tx_narration or "").upper()
    for r in rows:
        amt_tol = max(0.50, r[4] * 0.02)
        if not _amount_match(c.tx_amount, float(r[4] or 0), amt_tol):
            continue
        # Identifier match? (policy_ref or insurer name in narration)
        ident_hit = False
        try:
            patterns = json.loads(r[8] or "[]")
            for p in patterns:
                if re.search(p, narr_up, re.IGNORECASE):
                    ident_hit = True; break
        except Exception: pass
        if not ident_hit and r[1] and r[1].upper() in narr_up:
            ident_hit = True
        if not ident_hit and r[2] and r[2].upper() in narr_up:
            ident_hit = True
        if ident_hit:
            confidence = 100
            reason = f"insurance policy {r[1]} ({r[2]}) — identifier match"
        else:
            # Amount-only match (recurring marker required: GIRO/Standing/FAST)
            if not any(m in (c.tx_type or "").upper() for m in
                       ("GIRO", "STANDING INSTRUCTION", "FAST", "PAYMENTS / COLLECTIONS")):
                continue
            confidence = 75
            reason = f"insurance policy {r[1]} — amount + recurring marker"
        out.append(Match(
            registry="insurance_policy",
            row_id=r[0],
            row_label=f"{r[2]} — {r[3]} ({r[1]})",
            contra_coa=r[6],
            journal_kind="ilp_premium" if r[3] == "ilp" else "expense",
            confidence=confidence,
            reason=reason,
        ))
    return out


def _probe_subscription(s, c: CandidateJournal) -> list[Match]:
    """Match against subscription_registry."""
    out = []
    rows = s.execute(text("""
      SELECT id, name, vendor, amount, contra_coa, identifier_patterns
      FROM subscription_registry
      WHERE status = 'active'
    """)).all()
    narr_up = (c.tx_narration or "").upper()
    for r in rows:
        if not _amount_match(c.tx_amount, float(r[3] or 0), 0.50):
            continue
        ident_hit = False
        try:
            for p in json.loads(r[5] or "[]"):
                if re.search(p, narr_up, re.IGNORECASE): ident_hit = True; break
        except Exception: pass
        if not ident_hit and r[2] and r[2].upper() in narr_up:
            ident_hit = True
        confidence = 100 if ident_hit else 75
        out.append(Match(
            registry="subscription",
            row_id=r[0],
            row_label=f"{r[1]} ({r[2]})",
            contra_coa=r[4],
            journal_kind="expense",
            confidence=confidence,
            reason=f"subscription {r[1]} {'identifier' if ident_hit else 'amount'} match",
        ))
    return out


def _probe_facility(s, c: CandidateJournal) -> list[Match]:
    """Match against credit_facilities + payment_schedule."""
    out = []
    # Direct facility match by carrier (card # / account #)
    if c.tx_carriers:
        # Card-based CC payment match
        for k in ("dbs_card_routing", "ccc_card_routing", "to_card_routing"):
            card = (c.tx_carriers or {}).get(k, "")
            if not card: continue
            r = s.execute(text("""
              SELECT id, lender_name, facility_type, account_number
              FROM credit_facilities
              WHERE status='active' AND REPLACE(account_number, '-', '') LIKE :p
            """), {"p": f"%{card.replace('-','')[-12:]}%"}).fetchone()
            if r:
                # Map facility to CoA — credit_facilities has no CoA column;
                # use facility_type + lender as a hint. For now stub via lender heuristic.
                coa = _facility_to_coa(r[1], r[2])
                out.append(Match(
                    registry="credit_facility",
                    row_id=r[0],
                    row_label=f"{r[1]} ({r[2]})",
                    contra_coa=coa,
                    journal_kind="cc_pay" if r[2] == "credit_card" else "loan_pay",
                    confidence=100,
                    reason=f"facility {r[1]} — card-number carrier match",
                ))

    # Scheduled payment match by date+amount
    rows = s.execute(text("""
      SELECT ps.id, ps.facility_id, ps.due_date, ps.amount, cf.lender_name, cf.facility_type
      FROM payment_schedule ps
      LEFT JOIN credit_facilities cf ON cf.id = ps.facility_id
      WHERE ABS(ps.amount - :amt) < 1.0
        AND ps.due_date BETWEEN date(:d, '-7 days') AND date(:d, '+7 days')
    """), {"amt": c.tx_amount, "d": c.tx_date}).all()
    for r in rows:
        coa = _facility_to_coa(r[4] or "", r[5] or "")
        out.append(Match(
            registry="payment_schedule",
            row_id=r[0],
            row_label=f"scheduled {r[4] or r[1]} ${r[3]:,.2f} due {r[2]}",
            contra_coa=coa,
            journal_kind="loan_pay" if r[5] != "credit_card" else "cc_pay",
            confidence=90,
            reason=f"payment_schedule match on amount+date (facility {r[1]})",
        ))
    return out


def _facility_to_coa(lender: str, facility_type: str) -> str:
    """Heuristic: map a credit_facilities row to its GL CoA code."""
    lender_up = (lender or "").upper()
    if "DBS LIVE FRESH" in lender_up or "DBS CC" in lender_up: return "2111"
    if "MAYBANK PLATINUM" in lender_up or "MAYBANK CC" in lender_up: return "2112"
    if "STANDARD CHARTERED CC" in lender_up or "SC CASHBACK" in lender_up: return "2113"
    if "HSBC" in lender_up and "CC" in lender_up: return "2114"
    if "DBS CASHLINE" in lender_up: return "2121"
    if "UOB CASHPLUS" in lender_up: return "2122"
    if "SC LOAN" in lender_up or "BT" in lender_up: return "2211"
    if "GXS" in lender_up: return "2212"
    if "MAYBANK" in lender_up and "CREDITABLE" in lender_up: return "2213"
    if "EZ LOAN" in lender_up: return "2221"
    if "LENDING BEE" in lender_up: return "2222"
    if "SANDS" in lender_up: return "2223"
    return "1190"   # fallback to suspense


def _probe_inflow(c: CandidateJournal) -> Optional[Match]:
    """tx_type-based inflow classification (Sim 6 patch — closes the gap
    that left 843 of 2,300 POSB historical tx unrouted).

    Routes:
      SALARY                  → 4110 (Salary — primary employer)
      INTEREST EARNED         → 4220 (Interest income)
      INWARD CREDIT / IBG     → 4900 (Other Income — pending classification)
      Refund / reversal hints → 4900 (Other Income, low conf — queue for triage)
    Confidence 80 so these auto-post.
    """
    if c.direction != "in": return None
    tx = (c.tx_type or "").upper()
    if "SALARY" in tx:
        return Match("inflow_rule", 0, "Salary credit", "4110", "salary", 80,
                      "salary credit (tx_type=SALARY)")
    if "INTEREST EARNED" in tx or "INTEREST" in tx and "CREDIT" in tx:
        return Match("inflow_rule", 0, "Interest earned", "4220", "income", 80,
                      "interest earned on deposit")
    if "INWARD CREDIT" in tx or "INWARD IBG" in tx:
        return Match("inflow_rule", 0, "Inward credit / IBG", "4900", "income", 75,
                      "inward credit — needs payer classification (4900 placeholder)")
    if "REVERSAL" in tx or "REFUND" in tx:
        return Match("inflow_rule", 0, "Refund / reversal", "5190", "expense_reversal", 75,
                      "refund — credit lifestyle expense")
    if "WIRE TRANSFER" in tx:
        return Match("inflow_rule", 0, "Wire transfer in", "4900", "income", 70,
                      "wire transfer inflow — needs payer classification")
    if "MEPS" in tx:
        return Match("inflow_rule", 0, "MEPS receipt", "4900", "income", 80,
                      "MEPS receipt (inter-bank wire)")
    if "DIVIDEND" in tx or "DISTRIBUTION" in tx:
        return Match("inflow_rule", 0, "Dividend / distribution", "4220", "income", 80,
                      "dividend / cash distribution credited")
    if "CASH DEPOSIT" in tx:
        return Match("inflow_rule", 0, "Cash deposit", "1112", "transfer", 80,
                      "cash deposit — transfer from wallet 1112")
    if "ADVICE" in tx:
        return Match("inflow_rule", 0, "Bank advice", "4900", "income", 70,
                      "bank advice / unspecified credit")
    if "BILL PAYMENT" in tx:
        return Match("inflow_rule", 0, "Bill payment reversal", "5190", "expense_reversal", 70,
                      "bill payment inflow — likely a refund/reversal")
    # Catch-all: any inflow that didn't match above → suspense
    # (balanced 2-leg journal Dr 1111 / Cr 1190 means GL stays balanced,
    # user later triages from /reconcile reading suspense balance)
    if "FAST" in tx or "FUNDS TRANSFER" in tx or "GIRO" in tx or "REMITTANCE" in tx \
       or "MY PREFERRED" in tx or "PAYMENT PLAN" in tx:
        return Match("inflow_rule", 0, "Ambiguous inflow → Suspense", "1190", "transfer", 75,
                      f"{tx} — needs user classification from suspense")
    return None


def _probe_lifestyle(c: CandidateJournal) -> Optional[Match]:
    """tx_type-based lifestyle fallback for OUTFLOWS only.
    User-confirmed rule (feedback_lifestyle_expense_lumping.md): lump don't drill.
    Confidence 80 so these auto-post — no per-item review required."""
    if c.direction != "out": return None
    tx = (c.tx_type or "").upper()
    if "DEBIT CARD" in tx or "POINT-OF-SALE" in tx or "POINT OF SALE" in tx:
        return Match("lifestyle_rule", 0, "Debit card / POS", "5190", "expense", 80,
                      "lifestyle-lump (debit-card / POS)")
    if "BILL PAYMENT" in tx and "DBS INTERNET" not in tx:
        return Match("lifestyle_rule", 0, "Bill payment", "5190", "expense", 80,
                      "lifestyle-lump (bill payment)")
    if "FAST" in tx and (c.tx_carriers or {}).get("paynow_recipient"):
        return Match("lifestyle_rule", 0, "FAST/PayNow outflow", "5190", "expense", 80,
                      "lifestyle-lump (FAST+PayNow, no specific entity)")
    if "CASH WITHDRAWAL" in tx or "ATM" in tx:
        return Match("lifestyle_rule", 0, "Cash withdrawal", "5170", "expense", 80,
                      "family expense (cash withdrawal)")
    if "FAST COLLECTION" in tx:
        return Match("lifestyle_rule", 0, "FAST collection", "5190", "expense", 80,
                      "FAST collection (outflow to ext party)")
    if "STANDING INSTRUCTION" in tx:
        return Match("lifestyle_rule", 0, "Standing instruction", "5190", "expense", 78,
                      "standing instruction outflow (no specific match)")
    if "SERVICE CHARGE" in tx:
        return Match("lifestyle_rule", 0, "Service charge", "5410", "expense", 85,
                      "bank service charge")
    if "GIRO" in tx and "PAYMENT" in tx:
        return Match("lifestyle_rule", 0, "GIRO payment", "5190", "expense", 75,
                      "GIRO outflow — fallback lifestyle")
    # Catch-all: any outflow that didn't match → 1190 Suspense for triage
    if "FAST" in tx or "FUNDS TRANSFER" in tx or "GIRO" in tx \
       or "REMITTANCE" in tx or "MY PREFERRED" in tx or "PAYMENT PLAN" in tx:
        return Match("lifestyle_rule", 0, "Ambiguous outflow → Suspense", "1190", "transfer", 75,
                      f"{tx} — needs user classification from suspense")
    return None


# ── Main entry point ─────────────────────────────────────────────────────────


def verify(s, candidate: CandidateJournal) -> Verdict:
    """Walk every registry, compute confidence, return decision.

    The caller (POSB cutover / CC cutover / etc) uses the verdict:
      • POST_AUTO  → caller calls js.post_journal() with verdict.top_match's contra_coa
      • QUEUE      → caller persists candidate into unreconciled_queue
      • SKIP       → caller does nothing (covered by another pipeline)
    """
    matches: list[Match] = []

    # 1. Payslip cross-doc dedup — high-priority SKIP signal
    m = _probe_payslip(s, candidate)
    if m:
        return Verdict(
            decision="SKIP",
            top_match=m, all_matches=[m],
            confidence=m.confidence,
            reason=f"covered by payslip — caller should not post",
        )

    # 2. Insurance / ILP
    matches.extend(_probe_insurance(s, candidate))

    # 3. Subscription
    matches.extend(_probe_subscription(s, candidate))

    # 4. Credit facilities + payment_schedule
    matches.extend(_probe_facility(s, candidate))

    # 5. Inflow rules (Sim 6 patch)
    m = _probe_inflow(candidate)
    if m: matches.append(m)

    # 6. Lifestyle fallback (outflows only)
    m = _probe_lifestyle(candidate)
    if m: matches.append(m)

    # Top-1 wins
    matches.sort(key=lambda m: -m.confidence)
    top = matches[0] if matches else None
    conf = top.confidence if top else 0

    if conf >= AUTO_POST_THRESHOLD:
        decision = "POST_AUTO"
        reason = f"matched {top.registry}: {top.reason}"
    else:
        decision = "QUEUE"
        reason = f"low confidence ({conf}) — user triage needed" if top else "no match — user triage needed"

    return Verdict(
        decision=decision,
        top_match=top,
        all_matches=matches[:5],   # keep top-5 for queue display
        confidence=conf,
        reason=reason,
    )


# ── Queue-persistence helpers (called by posters when decision=QUEUE) ────────


def enqueue(s, candidate: CandidateJournal, verdict: Verdict) -> int:
    """Persist a candidate to unreconciled_queue. Idempotent on external_id."""
    if candidate.external_id:
        existing = s.execute(text("""
          SELECT id FROM unreconciled_queue WHERE external_id = :eid
        """), {"eid": candidate.external_id}).fetchone()
        if existing: return existing[0]

    matches_blob = json.dumps([asdict(m) for m in verdict.all_matches])
    r = s.execute(text("""
      INSERT INTO unreconciled_queue
        (source_doc, source_ref, tx_date, tx_amount, tx_narration, tx_carriers,
         tx_type, direction, candidate_journal, best_guess_matches, confidence,
         status, external_id, created_at)
      VALUES (:src, :ref, :d, :amt, :narr, :car, :tt, :dir, :cj, :bgm, :conf,
              'pending', :eid, CURRENT_TIMESTAMP)
    """), {
        "src": candidate.source_doc, "ref": candidate.source_ref,
        "d": candidate.tx_date, "amt": candidate.tx_amount,
        "narr": candidate.tx_narration[:500] if candidate.tx_narration else "",
        "car": json.dumps(candidate.tx_carriers or {}),
        "tt": candidate.tx_type, "dir": candidate.direction,
        "cj": json.dumps(candidate.proposed_lines),
        "bgm": matches_blob,
        "conf": verdict.confidence,
        "eid": candidate.external_id,
    })
    s.commit()
    return r.lastrowid
