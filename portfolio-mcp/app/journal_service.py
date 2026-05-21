"""Post journals to the General Ledger with double-entry enforcement.

post_journal() is the ONLY way to write GL entries. It refuses to commit
unless ΣDr == ΣCr within tolerance.

Callers:
  - Bootstrap / one-off backfills (`backfill_credit_facilities.py`)
  - Statement parser pipeline (v1.10.1)
  - Firefly→GL bridge (v1.10.2)
  - Manual entries via Mini App admin
"""
from __future__ import annotations

import logging
import re
from datetime import date as _date
from typing import Iterable

from sqlalchemy import select, func

from . import database as db
from . import ledger

logger = logging.getLogger(__name__)


# ── External_id canonical contract (audit-6 Q1) ─────────────────────────────
# Spec: `<source>:v<n>:<stable_key>` where stable_key is deterministic over
# IMMUTABLE business keys (source_doc + period_end + line_index + amount +
# date or equivalent). The stable_key must NEVER hash mutable content
# (classification result, narration, router output, suspense routing).
#
# Why this matters: a re-run of any pipeline must produce the same external_id
# for the same underlying source row. That's how post_journal() short-circuits
# duplicates idempotently. Previously, [direct POSB] and [POSB v2] cutovers
# used different hashing formulas → posted the same tx twice → -$60k phantom
# outflow. inv19f enforces the format on new journals so it can't recur.
#
# Source allowlist — every writer module declares its identifier here. Adding
# a writer requires adding it to this set AND inv19f's accepted list.
EXTERNAL_ID_SOURCES = {
    # Statement-anchored bank cutovers
    "posb", "maybank", "sc", "wise",
    # CC statement cutovers
    "cc_stmt", "cc_direct", "dbs_cc", "hsbc_cc", "maybank_cc", "sc_cc",
    # Snapshot-anchored writers (audit-5 #3)
    "cex", "wise_snap",
    # Internal pipelines
    "anchor", "opening", "xfer", "period_drift",
    # Verifier / queue-resolution paths
    "queue", "verifier",
    # Recurring + reclass utilities
    "recurring_repost", "reclass_amt_match", "sus_clean", "ilp_charges",
    "payslip",
    # Legacy bridges (frozen — no new posts under these)
    "firefly", "firefly_bridge", "posbpdf", "posbcsv",
    # Cutover invocations (each replay batch tags itself)
    "posb_direct", "sc_direct", "maybank_direct", "cc_cutover",
}

EXTERNAL_ID_FORMAT_VERSION = 1
EXTERNAL_ID_PATTERN = re.compile(r"^(?P<source>[a-z_]+):v(?P<version>\d+):.+$")
# Day the canonical format goes live. Journals posted before this are
# grandfathered. Journals posted on or after this MUST conform.
EXTERNAL_ID_ENFORCED_FROM = _date(2026, 5, 16)


def validate_external_id(external_id: str) -> tuple[bool, str]:
    """Return (ok, reason). Validates against the canonical contract.

    ok=True means: matches `<source>:v<n>:<key>` AND <source> is in the
    allowlist AND <version> >= 1. Otherwise the reason names which check
    failed so the caller can fix the writer."""
    if not external_id:
        return False, "external_id is empty/None"
    m = EXTERNAL_ID_PATTERN.match(external_id)
    if not m:
        return False, f"format != '<source>:v<n>:<key>' (got {external_id[:60]!r})"
    source = m.group("source")
    if source not in EXTERNAL_ID_SOURCES:
        return False, f"unknown source {source!r}; add to EXTERNAL_ID_SOURCES"
    try:
        version = int(m.group("version"))
    except ValueError:
        return False, f"version is not an integer (got {m.group('version')!r})"
    if version < 1:
        return False, f"version must be >= 1 (got {version})"
    return True, "ok"


class UnbalancedJournalError(ValueError):
    """Raised when a journal's debits and credits don't equal."""


class InvalidGLLineError(ValueError):
    """Raised when a GL line has bad data (negative amounts, both Dr+Cr, etc.)."""


class OpeningAnchorRequired(ValueError):
    """Gate 1: a balance-sheet account has no opening anchor on or before
    the journal date. The first journal on any asset/liability/equity account
    must be an opening anchor (journal_type='opening'). Use
    post_opening_anchor() to register one."""


def _next_journal_no(s) -> str:
    """Generate next sequential journal_no in format JNL-YYYY-NNNNN.
    Uses MAX(no)+1 (not COUNT+1) so gaps from prior deletes don't cause collisions."""
    year = _date.today().year
    prefix = f"JNL-{year}-"
    max_no = s.execute(
        select(func.max(ledger.Journal.journal_no))
        .where(ledger.Journal.journal_no.like(f"{prefix}%"))
    ).scalar_one()
    if max_no and max_no.startswith(prefix):
        try:
            n = int(max_no[len(prefix):]) + 1
        except ValueError:
            n = 1
    else:
        n = 1
    return f"{prefix}{n:05d}"


def _account_id_by_code(s, code: str) -> int:
    """Resolve a CoA account_code to its id. Raises if not found."""
    row = s.execute(
        select(ledger.ChartOfAccount).where(ledger.ChartOfAccount.account_code == code)
    ).scalar_one_or_none()
    if row is None:
        raise ValueError(f"No CoA account with code '{code}'")
    if not row.is_postable:
        raise ValueError(f"CoA account '{code}' ({row.account_name}) is a header — not postable")
    return row.id


def post_journal(
    s,
    journal_date: _date,
    narration: str,
    journal_type: str,
    lines: Iterable[dict],
    *,
    source_doc: str | None = None,
    source_ref: str | None = None,
    external_id: str | None = None,
    created_by: str = "system",
    tolerance: float = 0.01,
) -> int:
    """Post a balanced journal. Returns the journal_id.

    Each line dict can be:
      {"account_code": "5430", "debit": 100.00, "narration": "..."}
      {"account_code": "1111", "credit": 100.00}

    Or by id:
      {"account_id": 42, "debit": 100.00}

    Optional per-line fields:
      party_id, sub_ledger_table, sub_ledger_id, sub_ledger_event,
      currency, fx_rate (default 1.0 for SGD)
    """
    now = db.now_utc()
    # Idempotency: if external_id provided + already posted (NOT voided), return that journal_id.
    # Voided journals don't block re-posts (allows re-doing after a void).
    if external_id:
        # Audit-6 Q1: validate against the canonical contract. Logs a warning
        # for now (not raise) so existing writers have a window to migrate.
        # inv19f hard-enforces on NEW journals posted on/after the cutover date.
        ok, reason = validate_external_id(external_id)
        if not ok:
            logger.warning("post_journal: external_id %r does not match "
                           "canonical contract — %s", external_id, reason)

        existing = s.execute(
            select(ledger.Journal).where(
                ledger.Journal.external_id == external_id,
                ledger.Journal.status != "voided",
            )
        ).scalar_one_or_none()
        if existing is not None:
            logger.info("post_journal: external_id=%s already posted as journal %s",
                        external_id, existing.id)
            return existing.id

    # Resolve account codes → ids; validate line shape
    resolved: list[dict] = []
    sum_dr = 0.0
    sum_cr = 0.0
    for raw in lines:
        line = dict(raw)
        if "account_id" not in line:
            code = line.pop("account_code", None)
            if not code:
                raise InvalidGLLineError("Each line needs account_code or account_id")
            line["account_id"] = _account_id_by_code(s, code)
        debit = float(line.get("debit", 0) or 0)
        credit = float(line.get("credit", 0) or 0)
        if debit < 0 or credit < 0:
            raise InvalidGLLineError(f"Negative amount in line: {line}")
        if debit > 0 and credit > 0:
            raise InvalidGLLineError(f"Line has both Dr and Cr — must be one-sided: {line}")
        if debit == 0 and credit == 0:
            raise InvalidGLLineError(f"Zero-amount line: {line}")
        line["debit"] = debit
        line["credit"] = credit
        line["currency"] = line.get("currency", "SGD")
        line["fx_rate"] = float(line.get("fx_rate", 1.0))
        line["debit_sgd"] = debit * line["fx_rate"]
        line["credit_sgd"] = credit * line["fx_rate"]
        sum_dr += line["debit_sgd"]
        sum_cr += line["credit_sgd"]
        resolved.append(line)

    if len(resolved) < 2:
        raise InvalidGLLineError("Journal must have at least 2 lines")

    delta = round(sum_dr - sum_cr, 2)
    if abs(delta) > tolerance:
        raise UnbalancedJournalError(
            f"Journal unbalanced: ΣDr={sum_dr:.2f} ≠ ΣCr={sum_cr:.2f} (Δ={delta:.2f}). "
            f"Lines: {resolved}"
        )

    # ── Gate 1: Opening Balance Gate ────────────────────────────────────────
    # Every line.account_id whose account_class is ASSET/LIABILITY/EQUITY must
    # have a registered opening anchor with opening_date ≤ journal_date,
    # UNLESS this journal is itself the opening (journal_type='opening').
    if journal_type != "opening":
        from sqlalchemy import text as _text
        for line in resolved:
            row = s.execute(_text("""
              SELECT coa.account_code, coa.account_name, coa.account_class
              FROM chart_of_accounts coa WHERE coa.id = :aid
            """), {"aid": line["account_id"]}).fetchone()
            if not row:
                continue
            if row[2] not in ("ASSET", "LIABILITY", "EQUITY"):
                continue   # P&L accounts are zero each period by definition
            anchor = s.execute(_text("""
              SELECT id FROM account_opening_anchor
              WHERE account_id = :aid AND opening_date <= :d
              ORDER BY opening_date DESC LIMIT 1
            """), {"aid": line["account_id"], "d": journal_date}).fetchone()
            if not anchor:
                raise OpeningAnchorRequired(
                    f"Account {row[0]} ({row[1]}, {row[2]}) has no opening anchor "
                    f"on or before {journal_date}. Post the opening balance "
                    f"first via post_opening_anchor()."
                )

    # Create journal header
    j = ledger.Journal(
        journal_no=_next_journal_no(s),
        journal_date=journal_date,
        narration=narration,
        journal_type=journal_type,
        source_doc=source_doc,
        source_ref=source_ref,
        external_id=external_id,
        status="posted",
        posted_at=now,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    s.add(j)
    s.flush()  # get j.id

    # Create GL lines
    for i, line in enumerate(resolved, start=1):
        gle = ledger.GeneralLedgerEntry(
            journal_id=j.id,
            line_no=i,
            account_id=line["account_id"],
            party_id=line.get("party_id"),
            debit=line["debit"],
            credit=line["credit"],
            currency=line["currency"],
            fx_rate=line["fx_rate"],
            debit_sgd=line["debit_sgd"],
            credit_sgd=line["credit_sgd"],
            narration=line.get("narration"),
            sub_ledger_table=line.get("sub_ledger_table"),
            sub_ledger_id=line.get("sub_ledger_id"),
            sub_ledger_event=line.get("sub_ledger_event"),
            created_at=now,
        )
        s.add(gle)
    return j.id


def void_journal(s, journal_id: int, reason: str) -> None:
    """Mark a journal as voided. Doesn't delete — preserves audit trail."""
    j = s.get(ledger.Journal, journal_id)
    if not j:
        raise ValueError(f"No journal with id {journal_id}")
    if j.status == "voided":
        return
    j.status = "voided"
    j.voided_at = db.now_utc()
    j.voided_reason = reason
    j.updated_at = db.now_utc()


def account_balance(s, account_code: str, as_of: _date | None = None) -> float:
    """Sum of (Dr - Cr) for postable accounts; (Cr - Dr) for credit-normal.
    Returns SGD."""
    acct = s.execute(
        select(ledger.ChartOfAccount).where(ledger.ChartOfAccount.account_code == account_code)
    ).scalar_one_or_none()
    if not acct:
        return 0.0
    q = select(
        func.coalesce(func.sum(ledger.GeneralLedgerEntry.debit_sgd), 0),
        func.coalesce(func.sum(ledger.GeneralLedgerEntry.credit_sgd), 0),
    ).join(ledger.Journal, ledger.Journal.id == ledger.GeneralLedgerEntry.journal_id
    ).where(
        ledger.GeneralLedgerEntry.account_id == acct.id,
        ledger.Journal.status == "posted",
    )
    if as_of:
        q = q.where(ledger.Journal.journal_date <= as_of)
    dr, cr = s.execute(q).one()
    return float(dr - cr) if acct.normal_balance == "DEBIT" else float(cr - dr)


# ── Gate 3 + Gate 4 helpers ──────────────────────────────────────────────────


def register_bank_statement(
    s,
    account_code: str,
    period_start: _date,
    period_end: _date,
    balance_brought_forward: float | None,
    balance_carried_forward: float,
    source_doc_path: str,
    currency: str = "SGD",
) -> int:
    """Gate 3: persist a parsed bank statement's BF/CF.
    Idempotent on (account_code, period_end).
    """
    from sqlalchemy import text as _text
    ext_id = f"{account_code}:{period_end.isoformat()}"
    existing = s.execute(_text("""
      SELECT id FROM bank_statement_registry WHERE external_id = :e
    """), {"e": ext_id}).fetchone()
    if existing:
        return existing[0]
    s.execute(_text("""
      INSERT INTO bank_statement_registry
        (account_code, period_start, period_end, balance_brought_forward,
         balance_carried_forward, currency, source_doc_path, parsed_at, external_id)
      VALUES (:c, :ps, :pe, :bf, :cf, :cur, :src, CURRENT_TIMESTAMP, :eid)
    """), {
        "c": account_code, "ps": period_start, "pe": period_end,
        "bf": balance_brought_forward, "cf": balance_carried_forward,
        "cur": currency, "src": source_doc_path, "eid": ext_id,
    })
    s.commit()
    return s.execute(_text(
        "SELECT id FROM bank_statement_registry WHERE external_id = :e"
    ), {"e": ext_id}).scalar()


def reconcile_period(
    s,
    account_code: str,
    period_end: _date,
    tolerance: float = 0.01,
) -> dict:
    """Gate 4: validate `GL(account_code, as_of=period_end) == statement CF`.
    Drift outside tolerance → enqueue unreconciled_queue (reason='period_drift').
    Returns {gl_balance, statement_cf, drift, action}.
    """
    from sqlalchemy import text as _text
    if isinstance(period_end, str):
        period_end = _date.fromisoformat(period_end[:10])
    stmt = s.execute(_text("""
      SELECT balance_carried_forward, source_doc_path
      FROM bank_statement_registry
      WHERE account_code = :c AND period_end = :d
    """), {"c": account_code, "d": period_end}).fetchone()
    if not stmt:
        return {"error": f"no bank_statement_registry row for {account_code} @ {period_end}"}

    cf = float(stmt[0])

    coa = s.execute(_text(
        "SELECT id, account_class FROM chart_of_accounts WHERE account_code=:c"
    ), {"c": account_code}).fetchone()
    if not coa:
        return {"error": f"no CoA for {account_code}"}

    row = s.execute(_text("""
      SELECT COALESCE(SUM(CASE WHEN j.status='posted' THEN gl.debit_sgd ELSE 0 END),0)
           - COALESCE(SUM(CASE WHEN j.status='posted' THEN gl.credit_sgd ELSE 0 END),0)
      FROM general_ledger gl JOIN journals j ON j.id=gl.journal_id
      WHERE gl.account_id=:aid AND j.journal_date <= :d
    """), {"aid": coa[0], "d": period_end}).fetchone()
    gl_bal = float(row[0] or 0)
    # Liabilities: flip sign (Cr-Dr is the natural positive)
    if coa[1] == "LIABILITY":
        gl_bal = -gl_bal

    drift = round(cf - gl_bal, 2)
    out = {"account_code": account_code, "period_end": period_end.isoformat(),
           "gl_balance": round(gl_bal, 2), "statement_cf": cf, "drift": drift}

    if abs(drift) <= tolerance:
        out["action"] = "reconciled"
        return out

    # Drift detected: enqueue (or update if exists)
    ext_id = f"period_drift:{account_code}:{period_end.isoformat()}"
    existing = s.execute(_text(
        "SELECT id FROM unreconciled_queue WHERE external_id=:e"
    ), {"e": ext_id}).fetchone()
    payload = {
        "src": "PERIOD_RECONCILE",
        "ref": f"{account_code}:{period_end.isoformat()}",
        "d": period_end,
        "amt": abs(drift),
        "narr": f"Period drift {account_code} @ {period_end}: "
                f"GL={gl_bal:,.2f} vs statement CF={cf:,.2f} (drift {drift:+,.2f})",
        "car": "{}",
        "tt": "PERIOD_DRIFT",
        "dir": "in" if drift > 0 else "out",
        "cj": "[]",
        "bgm": "[]",
        "conf": 0,
        "eid": ext_id,
    }
    if existing:
        # Preserve created_at so the drift_nudge "oldest age" stays accurate
        # across re-reconciliations. Per pass-7 Q1: the nudge surfaces queue
        # age, which is meaningless if every UPDATE resets the clock.
        s.execute(_text("""
          UPDATE unreconciled_queue
          SET tx_date=:d, tx_amount=:amt, tx_narration=:narr, confidence=0,
              status='pending'
          WHERE id=:i
        """), {**payload, "i": existing[0]})
        out["action"] = "drift_updated"
        out["queue_id"] = existing[0]
    else:
        s.execute(_text("""
          INSERT INTO unreconciled_queue
            (source_doc, source_ref, tx_date, tx_amount, tx_narration, tx_carriers,
             tx_type, direction, candidate_journal, best_guess_matches, confidence,
             status, external_id, created_at)
          VALUES (:src, :ref, :d, :amt, :narr, :car, :tt, :dir, :cj, :bgm, :conf,
                  'pending', :eid, CURRENT_TIMESTAMP)
        """), payload)
        out["action"] = "drift_queued"
    s.commit()
    return out


# ── Path-A-with-escape-hatches: drift classifier + bulk resolver ─────────────


def classify_drift(s, queue_row: dict) -> str:
    """Classify a PERIOD_DRIFT queue entry. Per Perplexity audit-3+4 policy
    (audit-4 tightened to period-overlap semantics):

      Type 1 — pre_opening: drift_date < first opening anchor for that account.
               Historical net worth before this system → route to 3100.
      Type 3 — fixable: counter-statement coverage OVERLAPS this drift's
               period_end (not just "any statement ever"). Leave in queue;
               ingest more data and Gate 4 closes it naturally.
      Type 2 — unresolvable: post-cutover, no contemporaneous counter-
               statement coverage (e.g. POSB→peer PayNow). Route to
               5990 Reconciliation Adjustment.

    Returns 'pre_opening', 'fixable', or 'unresolvable'.
    """
    from sqlalchemy import text as _text
    from datetime import date as _date

    src_ref = queue_row.get("source_ref") or ""
    if ":" not in src_ref:
        return "unresolvable"
    code, period_end_str = src_ref.split(":", 1)

    drift_date = queue_row.get("tx_date")
    if not drift_date:
        return "unresolvable"

    # Parse period_end as a date; fall back to drift_date
    try:
        period_end = period_end_str
        if not isinstance(period_end, _date):
            period_end = _date.fromisoformat(str(period_end_str)[:10])
    except Exception:
        period_end = drift_date

    # Type 1 — drift period before this account's first opening anchor?
    first_anchor = s.execute(_text("""
      SELECT MIN(a.opening_date) FROM account_opening_anchor a
      JOIN chart_of_accounts coa ON coa.id=a.account_id
      WHERE coa.account_code=:c
    """), {"c": code}).scalar()
    if first_anchor and str(drift_date) < str(first_anchor):
        return "pre_opening"

    # Type 3 — does a counter-account have statement coverage OVERLAPPING
    # this drift's period_end? Audit-4 tightening: not just "any coverage
    # ever" but "coverage of THIS specific period".
    #
    # Counter-corridors live in counter_account_map (audit-5 #3). The
    # period_end filter is applied here so a corridor can be deactivated
    # mid-life (active_to) without dropping the row.
    counter_codes = [r[0] for r in s.execute(_text("""
      SELECT dst_account_code FROM counter_account_map
      WHERE src_account_code=:c
        AND (active_from IS NULL OR active_from <= :pe)
        AND (active_to   IS NULL OR active_to   >= :pe)
    """), {"c": code, "pe": period_end}).fetchall()]

    if counter_codes and period_end:
        codes_sql = ",".join(repr(c) for c in counter_codes)
        n = s.execute(_text(f"""
          SELECT COUNT(*) FROM bank_statement_registry
          WHERE account_code IN ({codes_sql})
            AND period_start <= :pe AND period_end >= :pe
        """), {"pe": period_end}).scalar() or 0
        if n > 0:
            return "fixable"

    # Type 2 — default
    return "unresolvable"


def bulk_resolve_drift(s, dry: bool = True) -> dict:
    """Resolve pending PERIOD_DRIFT entries per the hybrid policy.

      pre_opening → post Dr/Cr <account> + Cr/Dr 3100 (Retained Earnings)
      unresolvable → post Dr/Cr <account> + Cr/Dr 5990 (Reconciliation Adj)
      fixable     → leave queued (mark user_decision='operational_drift')

    Returns summary {resolved_to_3100, resolved_to_5990, left_fixable, errors}.
    """
    from sqlalchemy import text as _text
    from datetime import date as _date

    rows = s.execute(_text("""
      SELECT id, source_ref, tx_date, tx_amount, tx_narration, direction
      FROM unreconciled_queue
      WHERE tx_type='PERIOD_DRIFT' AND status='pending'
      ORDER BY tx_date
    """)).fetchall()

    stats = {"resolved_to_3100": 0, "resolved_to_5990": 0,
             "left_fixable": 0, "errors": 0,
             "by_class": {"pre_opening": 0, "unresolvable": 0, "fixable": 0}}

    for r in rows:
        qrow = {"id": r[0], "source_ref": r[1], "tx_date": r[2],
                "tx_amount": r[3], "tx_narration": r[4], "direction": r[5]}
        klass = classify_drift(s, qrow)
        stats["by_class"][klass] += 1

        if klass == "fixable":
            if not dry:
                s.execute(_text("""
                  UPDATE unreconciled_queue SET user_decision='operational_drift', notes='Type 3 — counter-statement ingest reachable'
                  WHERE id=:i
                """), {"i": qrow["id"]})
            stats["left_fixable"] += 1
            continue

        # Resolve T1 or T2 by posting a journal that brings GL → CF
        if ":" not in (qrow["source_ref"] or ""):
            stats["errors"] += 1; continue
        code, period_end = qrow["source_ref"].split(":", 1)
        drift = float(qrow["tx_amount"])  # the magnitude
        # Sign: narration says "drift=+X means GL is X below CF" → need to add X to GL
        # Convention: direction='in' means we add to the asset (Dr asset)
        direction = (qrow["direction"] or "").lower()

        offset_coa = "3100" if klass == "pre_opening" else "5990"
        offset_label = "Retained Earnings (pre-system)" if klass == "pre_opening" else "Reconciliation Adjustment"

        # Sign rule: if direction='in' (GL was below CF, drift +X) → Dr asset, Cr offset
        # if direction='out' (GL was above CF, drift -X) → Cr asset, Dr offset
        if direction == "in":
            lines = [
                {"account_code": code,       "debit": drift,  "narration": f"Gate-4 drift resolve → {offset_label}"},
                {"account_code": offset_coa, "credit": drift, "narration": f"Drift {drift:+.2f} for {qrow['source_ref']}"},
            ]
        else:
            lines = [
                {"account_code": offset_coa, "debit": drift,  "narration": f"Drift {drift:+.2f} for {qrow['source_ref']}"},
                {"account_code": code,       "credit": drift, "narration": f"Gate-4 drift resolve → {offset_label}"},
            ]

        if dry:
            if klass == "pre_opening": stats["resolved_to_3100"] += 1
            else: stats["resolved_to_5990"] += 1
            continue

        try:
            pe_d = _date.fromisoformat(period_end) if isinstance(period_end, str) else period_end
            jid = post_journal(
                s, journal_date=pe_d,
                narration=f"Drift resolve {klass} → {code} @ {period_end} (${drift:,.2f})",
                journal_type="drift_resolve",
                lines=lines,
                source_doc="DRIFT_RESOLVE",
                source_ref=qrow["source_ref"],
                external_id=f"drift_resolve:{code}:{period_end}",
                created_by="bulk_resolve_drift",
            )
            s.execute(_text("""
              UPDATE unreconciled_queue
              SET status='resolved', user_decision=:k, posted_journal_id=:j,
                  resolved_at=CURRENT_TIMESTAMP,
                  notes=COALESCE(notes,'') || ' | resolved to ' || :o
              WHERE id=:i
            """), {"i": qrow["id"], "k": klass, "j": jid, "o": offset_coa})
            s.commit()
            if klass == "pre_opening": stats["resolved_to_3100"] += 1
            else: stats["resolved_to_5990"] += 1
        except Exception as e:
            s.rollback()
            stats["errors"] += 1
            logger.exception("bulk_resolve_drift error on queue id %s", qrow["id"])

    return stats


# ── Gate 1 helper ─────────────────────────────────────────────────────────────


def post_opening_anchor(
    s,
    account_code: str,
    opening_date: _date,
    opening_balance: float,
    source_doc: str,
    source_ref: str | None = None,
    notes: str | None = None,
) -> int:
    """Register the Day-0 anchor for a balance-sheet account.

    Posts a balanced journal (journal_type='opening', bypasses Gate 1) where
    the offset lands on Retained Earnings (3100) — historical net worth that
    pre-dates this system.

    For ASSETs (Dr-normal): Dr <account>, Cr 3100 if balance > 0; flipped if < 0.
    For LIABILITIES (Cr-normal): Cr <account>, Dr 3100 if balance > 0; flipped if < 0.

    Returns the posted journal_id.

    Idempotent: re-running with the same (account_code, opening_date) returns
    the existing anchor's journal_id (external_id contract).
    """
    from sqlalchemy import text as _text

    acct = s.execute(_text("""
      SELECT id, account_class, normal_balance, account_name
      FROM chart_of_accounts WHERE account_code = :c
    """), {"c": account_code}).fetchone()
    if not acct:
        raise ValueError(f"No CoA account with code '{account_code}'")
    if acct[1] not in ("ASSET", "LIABILITY", "EQUITY"):
        raise ValueError(f"Account {account_code} is {acct[1]} — opening anchors only apply to balance-sheet accounts")

    # Idempotency: existing anchor for same (account, date)?
    existing = s.execute(_text("""
      SELECT posted_journal_id FROM account_opening_anchor
      WHERE account_id = :aid AND opening_date = :d
    """), {"aid": acct[0], "d": opening_date}).fetchone()
    if existing:
        logger.info("post_opening_anchor: %s @ %s already anchored as journal %s",
                    account_code, opening_date, existing[0])
        return existing[0]

    # Build the journal — offset goes to Retained Earnings 3100
    asset_dr_normal = acct[1] == "ASSET" or (acct[1] == "EQUITY" and acct[2] == "DEBIT")
    amt = abs(float(opening_balance))
    if amt < 0.005:
        # Still record the anchor (with $0) so the gate sees it. Create a
        # journal that nets to zero by debiting + crediting Retained Earnings.
        lines = [
            {"account_code": account_code, "debit": 0.01, "narration": "Zero opening (placeholder cent)"},
            {"account_code": "3100", "credit": 0.01, "narration": "Zero opening offset"},
            {"account_code": "3100", "debit": 0.01, "narration": "Zero opening reversal"},
            {"account_code": account_code, "credit": 0.01, "narration": "Zero opening reversal"},
        ]
    elif (asset_dr_normal and opening_balance >= 0) or (not asset_dr_normal and opening_balance < 0):
        lines = [
            {"account_code": account_code, "debit": amt, "narration": f"Opening balance {opening_balance:,.2f}"},
            {"account_code": "3100", "credit": amt, "narration": "Historical net worth (pre-system)"},
        ]
    else:
        lines = [
            {"account_code": "3100", "debit": amt, "narration": "Historical net worth offset"},
            {"account_code": account_code, "credit": amt, "narration": f"Opening balance {opening_balance:,.2f}"},
        ]

    jid = post_journal(
        s,
        journal_date=opening_date,
        narration=f"Opening anchor — {account_code} @ {opening_date} = {opening_balance:,.2f}",
        journal_type="opening",
        lines=lines,
        source_doc=source_doc,
        source_ref=source_ref,
        external_id=f"opening:{account_code}:{opening_date.isoformat()}",
        created_by="opening_anchor",
    )

    # Register in account_opening_anchor
    s.execute(_text("""
      INSERT INTO account_opening_anchor
        (account_id, opening_date, opening_balance, source_doc, source_ref,
         posted_journal_id, notes, created_at)
      VALUES (:aid, :d, :bal, :src, :ref, :jid, :notes, CURRENT_TIMESTAMP)
    """), {
        "aid": acct[0], "d": opening_date, "bal": float(opening_balance),
        "src": source_doc, "ref": source_ref, "jid": jid, "notes": notes,
    })
    return jid
