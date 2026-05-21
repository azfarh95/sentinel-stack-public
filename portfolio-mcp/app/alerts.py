"""Behavioural-alerts module (audit-7 Q3).

Separate from invariants. Invariants prove the system is internally
consistent; alerts surface behavioural surprises that need owner attention.

Layout:
    scan(session)               — driver, runs all detectors, upserts alerts
    _detect_stale_class_a()     — Class A account with BSR > 90 days old
    _detect_missing_recurring() — active obligation with no debit this month
    _detect_snapshot_drop()     — Class B snapshot dropped >20% week-over-week

Dedupe key: (kind, account_code, period). Re-running scan updates the
existing row instead of multiplying. Severity is per-detector; high-severity
new alerts can be picked up by jobs.alerts_push for Telegram delivery.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import text

from . import database as db
from . import ledger

logger = logging.getLogger(__name__)


@dataclass
class AlertCandidate:
    kind: str
    severity: str            # 'low' | 'medium' | 'high'
    account_code: str | None
    period: str | None
    message: str
    details: dict


# ── Detectors ───────────────────────────────────────────────────────────────
# Each returns a list[AlertCandidate]. Pure functions, no DB writes.


def _detect_stale_class_a(s) -> list[AlertCandidate]:
    """Class A accounts whose latest bank_statement_registry row is > 90 days
    old. Mirrors the dashboard's 'stale stmt' badge but persists the finding."""
    today = date.today()
    threshold = today - timedelta(days=90)
    rows = s.execute(text("""
      SELECT coa.account_code, coa.account_name,
             MAX(bsr.period_end) AS latest_pe
      FROM chart_of_accounts coa
      LEFT JOIN bank_statement_registry bsr ON bsr.account_code=coa.account_code
      WHERE coa.anchor_class='A'
      GROUP BY coa.account_code, coa.account_name
    """)).fetchall()
    out: list[AlertCandidate] = []
    for code, name, latest in rows:
        if latest is None:
            out.append(AlertCandidate(
                kind="stale_class_a", severity="high",
                account_code=code, period=None,
                message=f"{name} has no bank statement registered — "
                        f"dashboard reads GL projection",
                details={"latest_period_end": None},
            ))
            continue
        latest_date = latest if isinstance(latest, date) else date.fromisoformat(str(latest)[:10])
        age = (today - latest_date).days
        if age > 90:
            out.append(AlertCandidate(
                kind="stale_class_a",
                severity="high" if age > 180 else "medium",
                account_code=code,
                period=latest_date.isoformat(),
                message=f"{name} statement is {age} days old "
                        f"(latest period_end {latest_date}).",
                details={"latest_period_end": latest_date.isoformat(), "age_days": age},
            ))
    return out


def _detect_missing_recurring(s) -> list[AlertCandidate]:
    """Active recurring_obligation_registry rows with expected_amount > 0 that
    have no matching debit in the CURRENT calendar month. (Coarser cadence
    than inv20's 90-day window — designed to alert *as the month progresses*.)"""
    today = date.today()
    # Only alert after day 5 of the month (give bills time to land)
    if today.day < 5:
        return []
    month_start = today.replace(day=1)
    obligations = s.execute(text("""
      SELECT name, contra_coa, expected_amount, amount_tolerance, frequency
      FROM recurring_obligation_registry
      WHERE expected_amount > 0
        AND frequency='monthly'
        AND (active_from IS NULL OR active_from <= :today)
        AND (active_to   IS NULL OR active_to   >= :today)
    """), {"today": today.isoformat()}).fetchall()
    out: list[AlertCandidate] = []
    for name, coa, amt, tol, _freq in obligations:
        amt = float(amt or 0)
        tol = float(tol or 0.50)
        n = s.execute(text("""
          SELECT COUNT(*) FROM general_ledger gl
          JOIN journals j ON j.id=gl.journal_id
          JOIN chart_of_accounts coa ON coa.id=gl.account_id
          WHERE j.status='posted'
            AND coa.account_code=:c
            AND j.journal_date >= :start
            AND gl.debit_sgd BETWEEN :lo AND :hi
        """), {
            "c": coa, "start": month_start.isoformat(),
            "lo": amt - tol, "hi": amt + tol,
        }).scalar() or 0
        if n == 0:
            out.append(AlertCandidate(
                kind="missing_recurring",
                severity="medium",
                account_code=coa,
                period=month_start.strftime("%Y-%m"),
                message=f"'{name}' (${amt:,.2f}) — no matching debit "
                        f"in {month_start.strftime('%Y-%m')} yet",
                details={"expected_amount": amt, "tolerance": tol},
            ))
    return out


def _detect_snapshot_drop(s, drop_pct: float = 20.0) -> list[AlertCandidate]:
    """Class B account whose latest snapshot dropped >drop_pct% vs the
    snapshot ~7 days prior. Catches sudden Coinbase/Wise value cliffs
    (withdrawal, hack, or rate-shock)."""
    out: list[AlertCandidate] = []
    classes_b = [r[0] for r in s.execute(text(
        "SELECT account_code FROM chart_of_accounts WHERE anchor_class='B'"
    )).fetchall()]
    for code in classes_b:
        rows = s.execute(text("""
          SELECT sgd_value, captured_at FROM account_snapshot
          WHERE account_code=:c
          ORDER BY captured_at DESC LIMIT 50
        """), {"c": code}).fetchall()
        if len(rows) < 2:
            continue
        latest = rows[0]
        latest_val = float(latest[0])
        if latest_val < 1:    # ignore tiny / zero-balance accounts
            continue
        # Find a row roughly 7 days prior
        latest_at = latest[1] if isinstance(latest[1], datetime) else datetime.fromisoformat(str(latest[1]))
        cutoff = latest_at - timedelta(days=7)
        prior = None
        for r in rows[1:]:
            ts = r[1] if isinstance(r[1], datetime) else datetime.fromisoformat(str(r[1]))
            if ts <= cutoff:
                prior = r
                break
        if not prior:
            continue
        prior_val = float(prior[0])
        if prior_val < 1:
            continue
        drop = (prior_val - latest_val) / prior_val * 100
        if drop >= drop_pct:
            out.append(AlertCandidate(
                kind="snapshot_drop",
                severity="high",
                account_code=code,
                period=latest_at.date().isoformat(),
                message=f"{code} dropped {drop:.1f}% in last 7d "
                        f"(SGD {prior_val:,.2f} → {latest_val:,.2f})",
                details={"prior": prior_val, "latest": latest_val,
                         "drop_pct": round(drop, 2)},
            ))
    return out


# ── Driver ──────────────────────────────────────────────────────────────────


def _upsert(s, c: AlertCandidate) -> tuple[int, bool]:
    """Insert or update an alert by (kind, account_code, period).
    Returns (id, is_new). Existing 'dismissed' alerts stay dismissed."""
    existing = s.execute(text("""
      SELECT id, status FROM alerts
      WHERE kind=:k AND COALESCE(account_code,'')=:a AND COALESCE(period,'')=:p
    """), {"k": c.kind, "a": c.account_code or "", "p": c.period or ""}).fetchone()
    payload = {
        "k": c.kind, "sev": c.severity,
        "a": c.account_code, "p": c.period,
        "msg": c.message,
        "det": json.dumps(c.details, default=str),
    }
    if existing:
        # Don't resurrect dismissed alerts.
        if existing[1] == "dismissed":
            return existing[0], False
        s.execute(text("""
          UPDATE alerts SET severity=:sev, message=:msg, details_json=:det
          WHERE id=:i
        """), {**payload, "i": existing[0]})
        return existing[0], False
    s.execute(text("""
      INSERT INTO alerts
        (kind, severity, account_code, period, message, details_json,
         status, detected_at)
      VALUES (:k, :sev, :a, :p, :msg, :det, 'pending', CURRENT_TIMESTAMP)
    """), payload)
    s.commit()
    new_id = s.execute(text("SELECT last_insert_rowid()")).scalar()
    return new_id, True


def scan(s) -> dict:
    """Run all detectors. Returns a summary dict for logging.
    Idempotent — safe to call repeatedly. Re-running updates message/severity
    of existing alerts but doesn't re-notify."""
    detectors = (
        _detect_stale_class_a,
        _detect_missing_recurring,
        _detect_snapshot_drop,
    )
    new_ids: list[int] = []
    updated = 0
    for fn in detectors:
        try:
            candidates = fn(s)
        except Exception:
            logger.exception("alerts: detector %s failed", fn.__name__)
            continue
        for c in candidates:
            aid, is_new = _upsert(s, c)
            if is_new:
                new_ids.append(aid)
            else:
                updated += 1
    s.commit()
    return {"new": len(new_ids), "updated": updated, "new_ids": new_ids}
