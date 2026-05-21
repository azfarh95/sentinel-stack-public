"""Gate 5 + LedgerBackend (read surface) — the single resolver for current
balances. Every UI surface and agent endpoint MUST call `resolve()` instead of
summing GL journals directly. "Two numbers for one metric" becomes
structurally impossible once all callers route through this function.

Architecture (anchor-not-sum):
    Class A  statement-anchored   — read bank_statement_registry.CF
    Class B  live-API-anchored    — call wise/moralis/coinbase API (cached)
    Class C  snapshot-anchored    — read latest cpf_statement_registry /
                                    ilp_portfolio_snapshot / cex_snapshot

The GL is for transaction-trail audit only, not for current balance.

LedgerBackend abstract — leaves room for a future SentinelLite/Firefly split
without touching call sites.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import text


@dataclass
class Balance:
    """A resolved current balance with provenance — every read includes
    enough metadata to audit *where* the number came from."""
    account_code: str
    sgd: float
    as_of: str                   # ISO date
    source: str                  # 'statement_cf' | 'live_api' | 'snapshot' | 'gl_projection' | 'opening_anchor' | 'no_data'
    anchor_class: str            # 'A' | 'B' | 'C' | 'unknown'
    notes: str = ""


# ── LedgerBackend abstract ───────────────────────────────────────────────────


class LedgerBackend(ABC):
    """Read-side contract for whichever ledger implementation provides
    transaction history and journal access.

    Today: SqliteLedgerBackend (the only impl, talking to /data/portfolio.db).
    Future: FireflyBackend (read from Firefly v1 API), or a multi-tenant
    PostgresBackend with per-tenant schemas.
    """

    @abstractmethod
    def gl_balance(self, account_code: str, as_of: Optional[date] = None,
                   since: Optional[date] = None) -> float:
        """Return Dr - Cr sum for the account (assets/expenses positive),
        across posted journals.

        If `since` is set, sum only journals where `since < journal_date <= as_of`.
        Used by Class A current-balance projection: anchor at statement CF,
        then add post-statement GL movement.

        If `as_of` only, sum all journals up to and including `as_of`.
        """

    @abstractmethod
    def opening_anchor(self, account_code: str, on_or_before: date) -> Optional[tuple[date, float]]:
        """Return (opening_date, opening_balance) for the latest anchor
        on or before the given date, or None."""

    @abstractmethod
    def latest_statement_cf(self, account_code: str) -> Optional[tuple[date, float, str]]:
        """Return (period_end, CF, source_doc_path) for the most recent
        bank_statement_registry row, or None."""

    @abstractmethod
    def coa_class(self, account_code: str) -> Optional[str]:
        """Return account_class ('ASSET'/'LIABILITY'/'EQUITY'/'REVENUE'/'EXPENSE') or None."""

    @abstractmethod
    def anchor_class(self, account_code: str) -> Optional[str]:
        """Return anchor_class 'A'/'B'/'C' from chart_of_accounts, or None.
        Replaces the hard-coded CLASS_A_BANK / CLASS_B_LIVE / CLASS_C_SNAPSHOT
        Python sets (which are kept only as a fallback / source-of-truth seed)."""


class SqliteLedgerBackend(LedgerBackend):
    def __init__(self, session):
        self.s = session

    def gl_balance(self, account_code, as_of=None, since=None):
        q = """
          SELECT COALESCE(SUM(CASE WHEN j.status='posted' THEN gl.debit_sgd ELSE 0 END),0)
               - COALESCE(SUM(CASE WHEN j.status='posted' THEN gl.credit_sgd ELSE 0 END),0)
          FROM general_ledger gl JOIN journals j ON j.id=gl.journal_id
          WHERE gl.account_id=(SELECT id FROM chart_of_accounts WHERE account_code=:c)
        """
        params = {"c": account_code}
        if as_of:
            q += " AND j.journal_date <= :d"
            params["d"] = as_of
        if since:
            q += " AND j.journal_date > :since"
            params["since"] = since
        return float(self.s.execute(text(q), params).scalar() or 0)

    def opening_anchor(self, account_code, on_or_before):
        row = self.s.execute(text("""
          SELECT a.opening_date, a.opening_balance
          FROM account_opening_anchor a
          JOIN chart_of_accounts coa ON coa.id=a.account_id
          WHERE coa.account_code=:c AND a.opening_date <= :d
          ORDER BY a.opening_date DESC LIMIT 1
        """), {"c": account_code, "d": on_or_before}).fetchone()
        if not row: return None
        d = row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0])[:10])
        return (d, float(row[1]))

    def latest_statement_cf(self, account_code):
        row = self.s.execute(text("""
          SELECT period_end, balance_carried_forward, source_doc_path
          FROM bank_statement_registry
          WHERE account_code=:c ORDER BY period_end DESC LIMIT 1
        """), {"c": account_code}).fetchone()
        if not row: return None
        d = row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0])[:10])
        return (d, float(row[1]), row[2])

    def coa_class(self, account_code):
        row = self.s.execute(text(
            "SELECT account_class FROM chart_of_accounts WHERE account_code=:c"
        ), {"c": account_code}).fetchone()
        return row[0] if row else None

    def anchor_class(self, account_code):
        row = self.s.execute(text(
            "SELECT anchor_class FROM chart_of_accounts WHERE account_code=:c"
        ), {"c": account_code}).fetchone()
        return row[0] if row and row[0] else None


# ── Anchor-class registry ─────────────────────────────────────────────────────
# Defines which class each CoA belongs to. Anything not in this map falls back
# to the GL projection path (opening_anchor + journals).

CLASS_A_BANK = {"1111", "1114", "1115", "1116"}             # statement-anchored
CLASS_B_LIVE = {"1113", "1231", "1232", "1233"}             # snapshot-anchored (audit-6 Q3: Wise→bank_api)
CLASS_C_SNAPSHOT = {
    "1211", "1212", "1213", "12149",                        # CPF (snapshot)
    "12211","12212","12213","12214","12215","12219",        # Tokio Marine ILP (snapshot)
    "12221","12222","12223","12229",                        # Singlife (snapshot)
}


# ── Per-class resolver functions (audit-4 step c) ────────────────────────────
# Each function takes (backend, account_code, as_of) and returns a Balance.
# `resolve()` is now a thin dispatcher that picks the right resolver by
# anchor_class. Coverage is provable via the RESOLVER_REGISTRY map.


CLASS_A_STALENESS_DAYS = 90
# Pass-10 Q1: projection-gate thresholds. When a Class A account has
# post-statement GL activity AND the noise stays below these thresholds,
# the resolver returns CF + delta (statement_cf_plus_gl). Otherwise it
# returns CF only with source='statement_cf_gated' and a loud badge.
CLASS_A_PROJECTION_SUSPENSE_THRESHOLD_SGD = 100.0
CLASS_A_PROJECTION_UNCLASSIFIED_PCT = 10.0
# CoA code for the "Suspense" catch-all — movements here count as
# unclassified for the gate.
SUSPENSE_COA = "1190"


def _suspense_movement_since(backend, account_code: str, period_end: date) -> float:
    """Sum of |amounts| flowing between this account and 1190 Suspense
    since period_end. Used by projection-gate."""
    row = backend.s.execute(text("""
      SELECT COALESCE(SUM(ABS(gl1.debit_sgd - gl1.credit_sgd)), 0)
      FROM general_ledger gl1
      JOIN journals j ON j.id=gl1.journal_id
      WHERE j.status='posted'
        AND gl1.account_id=(SELECT id FROM chart_of_accounts WHERE account_code=:c)
        AND j.journal_date > :pe
        AND EXISTS (
          SELECT 1 FROM general_ledger gl2
          JOIN chart_of_accounts coa ON coa.id=gl2.account_id
          WHERE gl2.journal_id=j.id AND coa.account_code=:susp
        )
    """), {"c": account_code, "pe": period_end, "susp": SUSPENSE_COA}).scalar() or 0
    return float(row)


def _total_movement_since(backend, account_code: str, period_end: date) -> float:
    """Sum of |Dr-Cr| per journal on this account since period_end."""
    row = backend.s.execute(text("""
      SELECT COALESCE(SUM(ABS(gl.debit_sgd - gl.credit_sgd)), 0)
      FROM general_ledger gl
      JOIN journals j ON j.id=gl.journal_id
      WHERE j.status='posted'
        AND gl.account_id=(SELECT id FROM chart_of_accounts WHERE account_code=:c)
        AND j.journal_date > :pe
    """), {"c": account_code, "pe": period_end}).scalar() or 0
    return float(row)


def _resolve_class_a(backend: LedgerBackend, account_code: str, as_of: date) -> Balance:
    """Class A: bank-statement-anchored with projection-gating (pass-10 Q1).

    Resolution order:
      1. No statement registered → fall back to opening anchor + GL projection
      2. Statement exists, no post-stmt activity → return CF (source='statement_cf')
      3. Statement exists, post-stmt activity, noise BELOW threshold →
         return CF + delta (source='statement_cf_plus_gl')
      4. Statement exists, post-stmt activity, noise ABOVE threshold →
         return CF only with loud note (source='statement_cf_gated')
      5. Statement >90 days old (regardless of activity) →
         source='stale_statement', balance follows (3)/(4) gate

    Gate thresholds:
      |Suspense Δ since period_end| < CLASS_A_PROJECTION_SUSPENSE_THRESHOLD_SGD
      AND
      |Suspense Δ| / |total Δ| < CLASS_A_PROJECTION_UNCLASSIFIED_PCT

    Both must hold. Either failure → CF only.
    """
    cf = backend.latest_statement_cf(account_code)
    if not cf:
        bal = _resolve_fallback(backend, account_code, as_of)
        bal.anchor_class = "A"
        bal.notes = f"[Class A, no statement yet] {bal.notes}"
        return bal

    period_end, cf_value, src = cf
    age_days = (as_of - period_end).days
    is_stale = age_days > CLASS_A_STALENESS_DAYS
    stmt_filename = src.rsplit("/", 1)[-1]

    # Post-statement GL delta (signed)
    post_delta = backend.gl_balance(account_code, as_of=as_of, since=period_end)

    # Case 2: no post-statement activity at all
    if abs(post_delta) < 0.005:
        source = "stale_statement" if is_stale else "statement_cf"
        notes = (f"Latest CF from {stmt_filename}"
                 + (f" ({age_days}d STALE)" if is_stale else ""))
        return Balance(
            account_code=account_code, sgd=cf_value,
            as_of=period_end.isoformat(), source=source,
            anchor_class="A", notes=notes,
        )

    # There IS post-statement activity. Check the projection gate.
    susp = _suspense_movement_since(backend, account_code, period_end)
    total = _total_movement_since(backend, account_code, period_end)
    unclassified_pct = (susp / total * 100) if total > 0 else 0.0

    gate_passes = (
        susp < CLASS_A_PROJECTION_SUSPENSE_THRESHOLD_SGD
        and unclassified_pct < CLASS_A_PROJECTION_UNCLASSIFIED_PCT
    )

    if gate_passes:
        # Case 3 (or 5 if stale): show projected current
        projected = cf_value + post_delta
        source = "stale_statement" if is_stale else "statement_cf_plus_gl"
        notes = (
            f"CF {cf_value:,.2f} ({period_end}) + post-stmt {post_delta:+,.2f} "
            f"= {projected:,.2f}. Suspense Δ ${susp:,.2f} ({unclassified_pct:.1f}% of total)"
            + (f" — STALE {age_days}d" if is_stale else "")
        )
        return Balance(
            account_code=account_code, sgd=projected,
            as_of=as_of.isoformat(), source=source,
            anchor_class="A", notes=notes,
        )

    # Case 4: gate fails — CF only with explanation
    reason_bits = []
    if susp >= CLASS_A_PROJECTION_SUSPENSE_THRESHOLD_SGD:
        reason_bits.append(f"Suspense Δ ${susp:,.2f} ≥ ${CLASS_A_PROJECTION_SUSPENSE_THRESHOLD_SGD:.0f}")
    if unclassified_pct >= CLASS_A_PROJECTION_UNCLASSIFIED_PCT:
        reason_bits.append(f"unclassified {unclassified_pct:.1f}% ≥ {CLASS_A_PROJECTION_UNCLASSIFIED_PCT:.0f}%")
    notes = (
        f"CF {cf_value:,.2f} ({period_end}). "
        f"Projection BLOCKED — {', '.join(reason_bits)}. "
        f"Post-stmt {post_delta:+,.2f} hidden until {SUSPENSE_COA} triaged."
    )
    source = "stale_statement" if is_stale else "statement_cf_gated"
    return Balance(
        account_code=account_code, sgd=cf_value,
        as_of=period_end.isoformat(), source=source,
        anchor_class="A", notes=notes,
    )


CLASS_B_STALENESS_HOURS = 24


def _resolve_class_b(backend: LedgerBackend, account_code: str, as_of: date) -> Balance:
    """Class B: snapshot-anchored from account_snapshot table (audit-6 Q3).

    The resolver NEVER calls the exchange/bank API and NEVER reads the GL.
    A periodic job (coinbase.refresh_snapshot, wise.refresh_snapshot, ...)
    writes the snapshot; this function only reads. Three explicit outcomes:

      - Fresh snapshot   (< CLASS_B_STALENESS_HOURS old) → source='snapshot'
      - Stale snapshot   (older)                          → source='stale_snapshot'
      - No snapshot row                                    → source='no_snapshot', sgd=0

    Returning sgd=0 with source='no_snapshot' keeps the dashboard renderable
    while surfacing the gap. GL fallback is banned (inv19d) — that pattern
    made GL "sometimes the SoT, sometimes a shadow".

    source_type on the snapshot row (cex / bank_api / defi_api) is preserved
    in `notes` so the UI can render risk-appropriate badges.
    """
    from datetime import datetime, timezone
    from . import coinbase as _cb
    snap = _cb.get_latest_snapshot(backend.s, account_code)
    if snap is None:
        return Balance(
            account_code=account_code, sgd=0.0,
            as_of=as_of.isoformat(), source="no_snapshot",
            anchor_class="B",
            notes=f"No account_snapshot row for {account_code}; "
                  f"run the appropriate refresh job",
        )
    captured = snap.captured_at
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - captured).total_seconds() / 3600.0
    src = "snapshot" if age_h < CLASS_B_STALENESS_HOURS else "stale_snapshot"
    note = (f"{snap.source_type}:{snap.provider} snapshot from {captured.isoformat()}"
            + (f" ({age_h:.1f}h old — STALE)" if src == "stale_snapshot" else ""))
    return Balance(
        account_code=account_code, sgd=float(snap.sgd_value),
        as_of=captured.date().isoformat(), source=src,
        anchor_class="B", notes=note,
    )


def _resolve_class_c(backend: LedgerBackend, account_code: str, as_of: date) -> Balance:
    """Class C: snapshot-anchored. GL sum captures every re-anchor journal."""
    gl_total = backend.gl_balance(account_code, as_of=as_of)
    anchor = backend.opening_anchor(account_code, as_of)
    return Balance(
        account_code=account_code, sgd=gl_total,
        as_of=as_of.isoformat(), source="snapshot",
        anchor_class="C",
        notes=f"GL sum since opening {anchor[0] if anchor else 'n/a'}",
    )


def _resolve_fallback(backend: LedgerBackend, account_code: str, as_of: date) -> Balance:
    """No anchor_class tag → opening anchor + GL projection.
    Used for everything that isn't a bank/live/snapshot account
    (liabilities, equity, P&L lines, the rare hand-managed asset)."""
    cls = backend.coa_class(account_code)
    anchor = backend.opening_anchor(account_code, as_of)
    gl_total = backend.gl_balance(account_code, as_of=as_of)
    if cls == "LIABILITY":
        gl_total = -gl_total  # liabilities are Cr-normal

    if anchor:
        return Balance(
            account_code=account_code, sgd=gl_total,
            as_of=as_of.isoformat(), source="gl_projection",
            anchor_class="unknown",
            notes=f"Opening anchor {anchor[0]} + journals to {as_of}",
        )
    return Balance(
        account_code=account_code, sgd=gl_total,
        as_of=as_of.isoformat(), source="no_data",
        anchor_class="unknown", notes="No anchor; pure GL sum",
    )


# Public registry — single source of truth for which resolver runs for which
# anchor_class. Inv18 walks this dict to prove every tagged CoA row has a
# matching resolver. Add a key here when adding a new anchor class.
RESOLVER_REGISTRY: dict[str, dict] = {
    "A": {
        "fn": _resolve_class_a,
        "label": "statement_anchored",
        "source": "bank_statement_registry.CF",
        "python_set": CLASS_A_BANK,
    },
    "B": {
        "fn": _resolve_class_b,
        "label": "live_api_anchored",
        "source": "coinbase / wise / moralis (cached)",
        "python_set": CLASS_B_LIVE,
    },
    "C": {
        "fn": _resolve_class_c,
        "label": "snapshot_anchored",
        "source": "GL sum since last re-anchor journal",
        "python_set": CLASS_C_SNAPSHOT,
    },
}


# ── Gate 5: resolve() — dispatcher ───────────────────────────────────────────


class NoResolverError(LookupError):
    """Raised by `resolve(strict=True)` when a code has no anchor_class /
    registry entry. Surfacing the failure beats silently routing through
    `_resolve_fallback` and tagging the result `unknown` — that was the
    audit-5 escape hatch."""


def resolve(
    backend: LedgerBackend,
    account_code: str,
    as_of: Optional[date] = None,
    strict: bool = True,
) -> Balance:
    """Gate 5 — every UI/API call for "current balance" routes here.

    Resolution order:
      1. Look up the DB-stored anchor_class on the CoA row.
      2. If missing, consult the Python sets (which seeded the column and
         remain the doc'd source of truth for the class -> codes mapping).
      3. Dispatch to RESOLVER_REGISTRY[class]['fn'].

    `strict=True` (default, used by every dashboard/agent surface): raise
    NoResolverError when no resolver matches. This is what audit-5 demanded
    — production reads must never silently degrade to "unknown" + GL sum.

    `strict=False` (debug/legacy): route to `_resolve_fallback` and return a
    Balance with anchor_class='unknown'. Used by `resolve_debug()` and
    legacy non-dashboard paths (e.g. period reconciliation that needs a
    GL-only number for arithmetic).
    """
    as_of = as_of or date.today()

    db_ac = backend.anchor_class(account_code)
    if not db_ac:
        if account_code in CLASS_A_BANK: db_ac = "A"
        elif account_code in CLASS_B_LIVE: db_ac = "B"
        elif account_code in CLASS_C_SNAPSHOT: db_ac = "C"

    entry = RESOLVER_REGISTRY.get(db_ac)
    if entry:
        return entry["fn"](backend, account_code, as_of)

    if strict:
        cls = backend.coa_class(account_code)
        raise NoResolverError(
            f"No anchor_class/RESOLVER_REGISTRY entry for {account_code}. "
            f"CoA class={cls!r}. Tag the row with anchor_class A/B/C, "
            f"or use resolve_debug() if this is a legacy/non-dashboard path."
        )
    return _resolve_fallback(backend, account_code, as_of)


def resolve_debug(
    backend: LedgerBackend, account_code: str, as_of: Optional[date] = None
) -> Balance:
    """Permissive variant — used by reconciliation arithmetic and ad-hoc
    diagnostics where a GL-projection is acceptable even without an anchor.
    Never call from dashboard/agent surfaces; inv19a enforces that the
    dashboard CoA set never returns anchor_class='unknown'."""
    return resolve(backend, account_code, as_of, strict=False)


def resolve_many(session, codes: list[str], as_of: Optional[date] = None) -> dict[str, Balance]:
    """Convenience for dashboard rendering — resolves a list of codes in one shot."""
    backend = SqliteLedgerBackend(session)
    return {c: resolve(backend, c, as_of) for c in codes}


# ── Liability resolvers (closes Perplexity audit-3 SSoT ambiguity) ───────────
# Loans + CC totals come from `credit_facilities` (the hand-curated source of
# truth, per task #63). Without these wrappers, multiple call sites (home.py,
# balance_sheet.py) would query the table directly — that's three sources of
# truth for one number. These resolvers are the ONLY path.


def resolve_total_loans(session) -> Balance:
    """Sum current_outstanding for all active non-CC credit facilities."""
    from sqlalchemy import text
    total = float(session.execute(text("""
      SELECT COALESCE(SUM(current_outstanding), 0) FROM credit_facilities
      WHERE status='active' AND COALESCE(facility_type,'') != 'credit_card'
    """)).scalar() or 0)
    return Balance(
        account_code="loans:total", sgd=total,
        as_of=date.today().isoformat(),
        source="credit_facilities", anchor_class="A",
        notes="Sum credit_facilities WHERE facility_type != 'credit_card'",
    )


def resolve_total_cc(session) -> Balance:
    """Sum current_outstanding for active credit cards."""
    from sqlalchemy import text
    total = float(session.execute(text("""
      SELECT COALESCE(SUM(current_outstanding), 0) FROM credit_facilities
      WHERE status='active' AND facility_type='credit_card'
    """)).scalar() or 0)
    return Balance(
        account_code="cc:total", sgd=total,
        as_of=date.today().isoformat(),
        source="credit_facilities", anchor_class="A",
        notes="Sum credit_facilities WHERE facility_type='credit_card'",
    )
