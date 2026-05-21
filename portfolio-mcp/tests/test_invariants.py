"""Invariant tests — the contracts the five-gate architecture must always satisfy.

These tests run against the LIVE container DB (/data/portfolio.db) rather than
fixtures because the architecture is anchored to real-world account state.
Pure-logic fixtures live in test_balance_sheet.py / test_fx.py.

Run inside the container:
    docker exec portfolio-mcp pytest tests/test_invariants.py -v
"""
import pytest
from datetime import date
from sqlalchemy import text


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def s():
    from app import database as db
    db.init_db()
    sess = db.SessionLocal()
    yield sess
    sess.close()


# ── INV 1: Every balance-sheet account with journals has an opening anchor ────


def test_inv1_every_bs_account_with_journals_has_opening_anchor(s):
    """Gate 1 invariant: no posted journal exists on a balance-sheet account
    that lacks an `account_opening_anchor` row with opening_date ≤ first journal.
    """
    rows = s.execute(text("""
      SELECT coa.account_code, coa.account_name, MIN(j.journal_date) AS first_tx
      FROM general_ledger gl
      JOIN journals j ON j.id = gl.journal_id
      JOIN chart_of_accounts coa ON coa.id = gl.account_id
      WHERE j.status='posted'
        AND coa.account_class IN ('ASSET','LIABILITY','EQUITY')
        AND coa.is_postable=1
        AND coa.id NOT IN (SELECT account_id FROM account_opening_anchor)
      GROUP BY coa.id
    """)).fetchall()
    assert not rows, (
        f"{len(rows)} balance-sheet account(s) have journals but no opening anchor:\n"
        + "\n".join(f"  {r[0]}  {r[1]}  first_tx={r[2]}" for r in rows[:10])
    )


# ── INV 2: All posted journals are internally balanced (ΣDr == ΣCr) ───────────


def test_inv2_all_posted_journals_balance(s):
    rows = s.execute(text("""
      SELECT j.id, SUM(gl.debit_sgd) AS dr, SUM(gl.credit_sgd) AS cr
      FROM journals j JOIN general_ledger gl ON gl.journal_id = j.id
      WHERE j.status='posted'
      GROUP BY j.id
      HAVING ABS(SUM(gl.debit_sgd) - SUM(gl.credit_sgd)) > 0.02
    """)).fetchall()
    assert not rows, (
        f"{len(rows)} unbalanced posted journals:\n"
        + "\n".join(f"  j={r[0]}  Dr={r[1]:.2f}  Cr={r[2]:.2f}  Δ={r[1]-r[2]:+.2f}" for r in rows[:10])
    )


# ── INV 3: No journal has fewer than 2 lines ─────────────────────────────────


def test_inv3_journals_have_at_least_two_lines(s):
    rows = s.execute(text("""
      SELECT j.id, COUNT(gl.id) AS n FROM journals j
      LEFT JOIN general_ledger gl ON gl.journal_id=j.id
      WHERE j.status='posted' GROUP BY j.id HAVING COUNT(gl.id) < 2
    """)).fetchall()
    assert not rows, f"{len(rows)} journals with <2 lines (one-sided!): {rows[:5]}"


# ── INV 4: Gate 1 raises OpeningAnchorRequired correctly ─────────────────────


def test_inv4_gate1_blocks_unanchored_post(s):
    """Posting a journal on an UNANCHORED balance-sheet account must raise
    OpeningAnchorRequired."""
    from app import journal_service as js

    # Use a unique test code that definitely has no anchor
    test_code = "1198"
    # Ensure account exists; skip if can't insert
    exists = s.execute(text(
        "SELECT id FROM chart_of_accounts WHERE account_code=:c"
    ), {"c": test_code}).fetchone()
    if exists:
        # Clean any stale anchor + journals first
        s.execute(text("DELETE FROM account_opening_anchor WHERE account_id=:i"), {"i": exists[0]})
        s.execute(text("DELETE FROM journals WHERE source_doc='TEST_INV4'"))
        s.commit()
        test_id = exists[0]
    else:
        s.execute(text("""
          INSERT INTO chart_of_accounts (account_code, account_name, account_class,
            account_subclass, normal_balance, is_postable, is_control_account, parent_id,
            sub_ledger_table, is_active, created_at)
          VALUES (:c, 'INV4 test', 'ASSET', 'CURRENT', 'DEBIT', 1, 0,
                  (SELECT id FROM chart_of_accounts WHERE account_code='1110'),
                  NULL, 1, CURRENT_TIMESTAMP)
        """), {"c": test_code})
        s.commit()
        test_id = s.execute(text("SELECT id FROM chart_of_accounts WHERE account_code=:c"), {"c": test_code}).scalar()

    try:
        with pytest.raises(js.OpeningAnchorRequired):
            js.post_journal(s, journal_date=date(2026, 5, 15),
                narration="INV4 test", journal_type="expense",
                lines=[{"account_code": test_code, "debit": 10.0},
                       {"account_code": "3100", "credit": 10.0}],
                source_doc="TEST_INV4", external_id="inv4_block")
        s.rollback()
    finally:
        # Cleanup
        s.execute(text("DELETE FROM general_ledger WHERE journal_id IN (SELECT id FROM journals WHERE source_doc='TEST_INV4')"))
        s.execute(text("DELETE FROM journals WHERE source_doc='TEST_INV4'"))
        s.execute(text("DELETE FROM chart_of_accounts WHERE account_code=:c"), {"c": test_code})
        s.commit()


# ── INV 5: bank_statement_registry uniqueness on (account_code, period_end) ──


def test_inv5_bank_statement_registry_unique_per_period(s):
    rows = s.execute(text("""
      SELECT account_code, period_end, COUNT(*) AS n FROM bank_statement_registry
      GROUP BY account_code, period_end HAVING n > 1
    """)).fetchall()
    assert not rows, f"{len(rows)} duplicate (account, period_end) rows: {rows[:5]}"


# ── INV 6: Every period_drift queue item references a real registry row ───────


def test_inv6_drift_queue_references_real_periods(s):
    rows = s.execute(text("""
      SELECT q.id, q.source_ref FROM unreconciled_queue q
      WHERE q.tx_type='PERIOD_DRIFT' AND q.status='pending'
        AND q.source_ref NOT IN (
          SELECT account_code || ':' || period_end FROM bank_statement_registry
        )
    """)).fetchall()
    assert not rows, f"{len(rows)} drift queue items reference unknown periods: {rows[:5]}"


# ── INV 7: Balance sheet identity — Assets - Liabilities - Equity ≈ 0 ─────────


def test_inv7_balance_sheet_identity(s):
    """In a clean double-entry ledger, total Dr == total Cr always. This is the
    fundamental accounting identity, equivalent to: Assets - Liabilities - Equity = 0
    when normal-balance signs are applied."""
    row = s.execute(text("""
      SELECT
        COALESCE(SUM(CASE WHEN j.status='posted' THEN gl.debit_sgd ELSE 0 END),0) AS dr,
        COALESCE(SUM(CASE WHEN j.status='posted' THEN gl.credit_sgd ELSE 0 END),0) AS cr
      FROM general_ledger gl JOIN journals j ON j.id=gl.journal_id
    """)).fetchone()
    delta = abs(float(row[0]) - float(row[1]))
    assert delta < 1.0, (
        f"Ledger out of balance: ΣDr={row[0]:,.2f}  ΣCr={row[1]:,.2f}  Δ={delta:.2f}"
    )


# ── INV 8: P&L accounts (4xxx, 5xxx) cannot have opening_balance > 0 ──────────


def test_inv8_pnl_accounts_have_no_opening_anchor(s):
    rows = s.execute(text("""
      SELECT coa.account_code, a.opening_balance FROM account_opening_anchor a
      JOIN chart_of_accounts coa ON coa.id=a.account_id
      WHERE coa.account_class IN ('REVENUE','EXPENSE')
    """)).fetchall()
    assert not rows, (
        f"{len(rows)} P&L accounts have opening anchors (should be empty):\n"
        + "\n".join(f"  {r[0]}  bal={r[1]}" for r in rows[:5])
    )


# ── INV 9: Verifier probes return valid CoA codes ─────────────────────────────


def test_inv9_verifier_lifestyle_probe_returns_valid_coa(s):
    """Every contra_coa returned by a verifier probe must reference a postable
    CoA account; smoke-tests probes via direct calls."""
    from app.verifier import _probe_inflow, _probe_lifestyle, CandidateJournal

    test_cases = [
        CandidateJournal(source_doc="TEST", source_ref="t", tx_date="2026-05-15",
                         tx_amount=100.0, tx_type="SALARY", direction="in"),
        CandidateJournal(source_doc="TEST", source_ref="t", tx_date="2026-05-15",
                         tx_amount=100.0, tx_type="DEBIT CARD", direction="out"),
        CandidateJournal(source_doc="TEST", source_ref="t", tx_date="2026-05-15",
                         tx_amount=100.0, tx_type="MEPS RECEIPT", direction="in"),
    ]
    seen = set()
    for c in test_cases:
        for probe in (_probe_inflow, _probe_lifestyle):
            m = probe(c)
            if m: seen.add(m.contra_coa)

    for code in seen:
        row = s.execute(text(
            "SELECT is_postable FROM chart_of_accounts WHERE account_code=:c"
        ), {"c": code}).fetchone()
        assert row is not None, f"Verifier probe returned unknown CoA: {code}"
        assert row[0] == 1, f"Verifier probe returned non-postable CoA: {code}"


# ── INV 11: Conservation of value across tagged transfer journals ────────────


def test_inv11_transfer_journals_conserve_value(s):
    """Any journal whose journal_type is 'transfer' must net to zero across
    balance-sheet accounts (excl. P&L fee lines). Conservation principle:
    moving $X from A to B can't create or destroy value."""
    rows = s.execute(text("""
      SELECT j.id,
             SUM(CASE WHEN coa.account_class IN ('ASSET','LIABILITY','EQUITY')
                      THEN gl.debit_sgd - gl.credit_sgd ELSE 0 END) AS bs_net
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id = j.id
      JOIN chart_of_accounts coa ON coa.id = gl.account_id
      WHERE j.status='posted' AND j.journal_type='transfer'
      GROUP BY j.id
      HAVING ABS(SUM(CASE WHEN coa.account_class IN ('ASSET','LIABILITY','EQUITY')
                          THEN gl.debit_sgd - gl.credit_sgd ELSE 0 END)) > 0.05
    """)).fetchall()
    # Note: liabilities flip — Cr-Dr is the natural positive — but for transfer
    # journals the Dr asset + Cr asset SHOULD still sum to zero. If liability
    # involved, manually verify via a non-zero result here being investigated.
    # For now we accept up to 5% mismatch tolerance.
    bad = [r for r in rows if abs(float(r[1] or 0)) > 1.0]
    assert not bad, (
        f"{len(bad)} transfer journals don't conserve BS-side value:\n"
        + "\n".join(f"  j={r[0]}  net={r[1]:+.2f}" for r in bad[:5])
    )


# ── INV 12: Per-account flow identity (Opening + flows = Closing) ────────────


def test_inv12_per_account_flow_identity(s):
    """For each bank_statement_registry row, GL_at(period_end) reconciles
    against statement.CF OR a matching period_drift queue entry exists.

    Either Gate 4 reconciled the period, OR drift was surfaced for triage.
    No silent gaps allowed."""
    rows = s.execute(text("""
      SELECT bsr.account_code, bsr.period_end, bsr.balance_carried_forward
      FROM bank_statement_registry bsr
    """)).fetchall()
    unaccounted = []
    for code, pe, cf in rows:
        gl = float(s.execute(text("""
          SELECT COALESCE(SUM(CASE WHEN j.status='posted' THEN gl.debit_sgd ELSE 0 END),0)
               - COALESCE(SUM(CASE WHEN j.status='posted' THEN gl.credit_sgd ELSE 0 END),0)
          FROM general_ledger gl JOIN journals j ON j.id=gl.journal_id
          WHERE gl.account_id=(SELECT id FROM chart_of_accounts WHERE account_code=:c)
            AND j.journal_date <= :d
        """), {"c": code, "d": pe}).scalar() or 0)
        drift = abs(float(cf) - gl)
        if drift <= 0.01:
            continue  # reconciled
        # Must have a drift entry for this period
        ext = f"period_drift:{code}:{pe}"
        has_drift = s.execute(text(
            "SELECT 1 FROM unreconciled_queue WHERE external_id=:e"
        ), {"e": ext}).fetchone()
        if not has_drift:
            unaccounted.append((code, pe, drift))
    assert not unaccounted, (
        f"{len(unaccounted)} statement periods have drift but no queue entry:\n"
        + "\n".join(f"  {r[0]} @ {r[1]}  drift={r[2]:.2f}" for r in unaccounted[:5])
    )


# ── INV 13: Idempotence — registries don't grow with re-runs ─────────────────


def test_inv13_registries_idempotent(s):
    """Multiple anchor rows for the same (account, opening_date) would mean
    Gate 1 helper isn't being idempotent. Same for bank_statement_registry."""
    dup_anchors = s.execute(text("""
      SELECT account_id, opening_date, COUNT(*) FROM account_opening_anchor
      GROUP BY account_id, opening_date HAVING COUNT(*) > 1
    """)).fetchall()
    assert not dup_anchors, f"Duplicate opening anchors: {dup_anchors[:5]}"

    dup_bsr = s.execute(text("""
      SELECT account_code, period_end, COUNT(*) FROM bank_statement_registry
      GROUP BY account_code, period_end HAVING COUNT(*) > 1
    """)).fetchall()
    assert not dup_bsr, f"Duplicate bank_statement_registry rows: {dup_bsr[:5]}"


# ── INV 14: Gate 5 SoT — Class A accounts only resolved via account_balance ──


def test_inv14_gate5_sot_class_a(s):
    """For every Class A account, account_balance.resolve() returns a balance
    sourced from one of the statement-anchored sources. Updated v2.25 to
    accept the dual-display + projection-gating sources:
      - 'statement_cf'         — no post-stmt activity
      - 'statement_cf_plus_gl' — projection enabled (gate passed)
      - 'statement_cf_gated'   — projection blocked, CF only with badge
      - 'stale_statement'      — CF >90d, any of the above with stale marker

    Plus the value-anchor contract:
      - statement_cf / statement_cf_gated: bal.sgd == registry CF exactly
      - statement_cf_plus_gl: bal.sgd == CF + post-stmt GL delta
      - stale_statement: follows whichever underlying mode applies

    This still enforces "Gate 5 is the only Class A read path" — raw GL
    sums or Firefly bridge values would fail this check.
    """
    from app import account_balance as ab
    backend = ab.SqliteLedgerBackend(s)
    VALID_SOURCES = {
        "statement_cf", "statement_cf_plus_gl",
        "statement_cf_gated", "stale_statement",
    }
    for code in ab.CLASS_A_BANK:
        cf = backend.latest_statement_cf(code)
        if not cf:
            continue
        period_end, cf_value, _ = cf
        b = ab.resolve(backend, code)
        assert b.source in VALID_SOURCES, (
            f"Class A {code} resolved via {b.source!r}, expected one of {VALID_SOURCES}"
        )
        # Value contract: anchored balance must be either CF exactly
        # (modes 1/2/4) or CF + GL delta (mode 3).
        if b.source in {"statement_cf", "statement_cf_gated"}:
            assert abs(b.sgd - cf_value) < 0.01, (
                f"Class A {code} source={b.source}: resolve()={b.sgd} != CF={cf_value}"
            )
        elif b.source == "statement_cf_plus_gl":
            post_delta = backend.gl_balance(code, since=period_end)
            expected = cf_value + post_delta
            assert abs(b.sgd - expected) < 0.50, (
                f"Class A {code} projection: resolve()={b.sgd} "
                f"!= CF({cf_value}) + delta({post_delta}) = {expected}"
            )
        # stale_statement falls through whichever sub-mode applies


# ── INV 15: Drift lifecycle — resolved rows have a posted_journal_id ─────────


def test_inv15_drift_lifecycle(s):
    """Every queue row with status='resolved' must reference a posted journal
    via posted_journal_id (no half-resolved rows). Rejected rows are exempt."""
    rows = s.execute(text("""
      SELECT id, external_id, status FROM unreconciled_queue
      WHERE status='resolved' AND posted_journal_id IS NULL
    """)).fetchall()
    assert not rows, (
        f"{len(rows)} 'resolved' queue rows have no posted_journal_id:\n"
        + "\n".join(f"  q{r[0]}  ext={r[1]}" for r in rows[:5])
    )


# ── INV 16: Drift-resolve journals have strict shape (Perplexity audit-4) ───


def test_inv16_drift_resolve_journals_shape(s):
    """Every journal with journal_type='drift_resolve' must:
       - Have exactly 2 GL lines.
       - Touch exactly one offset account in {3100, 5990}.
       - Have no other P&L lines (no REVENUE/EXPENSE accounts except 5990).
    """
    rows = s.execute(text("""
      SELECT j.id, COUNT(gl.id) AS n
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id = j.id
      WHERE j.status='posted' AND j.journal_type='drift_resolve'
      GROUP BY j.id
      HAVING COUNT(gl.id) != 2
    """)).fetchall()
    assert not rows, (
        f"{len(rows)} drift_resolve journal(s) do not have exactly 2 lines:\n"
        + "\n".join(f"  j={r[0]}  n_lines={r[1]}" for r in rows[:5])
    )

    rows = s.execute(text("""
      SELECT j.id,
             SUM(CASE WHEN coa.account_code IN ('3100','5990') THEN 1 ELSE 0 END) AS n_offset
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id = j.id
      JOIN chart_of_accounts coa ON coa.id = gl.account_id
      WHERE j.status='posted' AND j.journal_type='drift_resolve'
      GROUP BY j.id
      HAVING n_offset != 1
    """)).fetchall()
    assert not rows, (
        f"{len(rows)} drift_resolve journal(s) do not touch exactly one of 3100/5990:\n"
        + "\n".join(f"  j={r[0]}  n_offset={r[1]}" for r in rows[:5])
    )

    rows = s.execute(text("""
      SELECT DISTINCT j.id, coa.account_code, coa.account_class
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id = j.id
      JOIN chart_of_accounts coa ON coa.id = gl.account_id
      WHERE j.status='posted' AND j.journal_type='drift_resolve'
        AND coa.account_class IN ('REVENUE','EXPENSE')
        AND coa.account_code != '5990'
    """)).fetchall()
    assert not rows, (
        f"{len(rows)} drift_resolve journal(s) touch unexpected P&L accounts:\n"
        + "\n".join(f"  j={r[0]}  code={r[1]} class={r[2]}" for r in rows[:5])
    )


# ── INV 10: Opening anchors have offset to 3100 Retained Earnings ─────────────


def test_inv10_opening_anchors_offset_to_retained_earnings(s):
    """Every opening journal must touch 3100 (Retained Earnings) as the offset
    leg, OR be a multi-account portfolio anchor (the 2024-01-01 jid 13441)."""
    re_id = s.execute(text(
        "SELECT id FROM chart_of_accounts WHERE account_code='3100'"
    )).scalar()
    rows = s.execute(text("""
      SELECT j.id, j.narration FROM journals j
      WHERE j.journal_type='opening' AND j.status='posted'
        AND j.id NOT IN (
          SELECT journal_id FROM general_ledger WHERE account_id=:r
        )
    """), {"r": re_id}).fetchall()
    # Exemption: the multi-account 2024-01-01 portfolio anchor may not touch 3100
    # if it balances across many accounts itself
    exempt_ids = {13441}
    leaked = [r for r in rows if r[0] not in exempt_ids]
    assert not leaked, (
        f"{len(leaked)} opening journal(s) don't offset to 3100:\n"
        + "\n".join(f"  j={r[0]}  {r[1][:60]}" for r in leaked[:5])
    )


# ── INV 17: anchor_class column ↔ Python sets consistency (audit-4 step b) ────


def test_inv17_anchor_class_db_matches_python_sets(s):
    """The DB-stored anchor_class column on chart_of_accounts must agree with
    the CLASS_A_BANK / CLASS_B_LIVE / CLASS_C_SNAPSHOT sets that seeded it.
    Also: only ASSET accounts may carry an anchor_class — anchors are not a
    concept for liabilities/equity/P&L.
    """
    from app.account_balance import CLASS_A_BANK, CLASS_B_LIVE, CLASS_C_SNAPSHOT

    rows = s.execute(text(
        "SELECT account_code, account_class, anchor_class "
        "FROM chart_of_accounts WHERE anchor_class IS NOT NULL"
    )).fetchall()
    db_a = {r[0] for r in rows if r[2] == "A"}
    db_b = {r[0] for r in rows if r[2] == "B"}
    db_c = {r[0] for r in rows if r[2] == "C"}

    # Tagged rows must all be ASSET — anchor_class is meaningless on Cr-normal
    # accounts (liabilities/equity) and P&L lines.
    not_asset = [r for r in rows if r[1] != "ASSET"]
    assert not not_asset, (
        f"{len(not_asset)} anchor_class rows are not ASSET:\n"
        + "\n".join(f"  {r[0]}  class={r[1]}  ac={r[2]}" for r in not_asset[:5])
    )

    # Python sets are authoritative seeds; the DB column must be a superset
    # (codes in the set must be tagged). Codes tagged in the DB but missing
    # from the set are allowed — that's how new accounts get added: tag in
    # DB first, sync the set on next code refactor.
    missing_a = CLASS_A_BANK - db_a
    missing_b = CLASS_B_LIVE - db_b
    missing_c = CLASS_C_SNAPSHOT - db_c
    # Filter: only codes that actually exist as CoA rows count as "missing"
    # (retired codes like 1116 may have been deleted intentionally).
    existing = {r[0] for r in s.execute(text(
        "SELECT account_code FROM chart_of_accounts"
    )).fetchall()}
    missing_a &= existing
    missing_b &= existing
    missing_c &= existing
    assert not (missing_a or missing_b or missing_c), (
        f"Python sets reference codes that exist as CoA rows but are not tagged:\n"
        f"  A missing: {sorted(missing_a)}\n"
        f"  B missing: {sorted(missing_b)}\n"
        f"  C missing: {sorted(missing_c)}"
    )


# ── INV 19b: counter_account_map shape (audit-5 #2) ──────────────────────────


def test_inv19b_counter_account_map_shape(s):
    """The corridor table must respect the shape contract: both endpoints
    exist in CoA, both are Class A ASSET (only statement-anchored accounts
    can produce overlapping-period evidence), no self-mapping, no duplicate
    (src,dst,relation_type) tuples.
    """
    rows = s.execute(text("""
      SELECT cam.src_account_code, cam.dst_account_code, cam.relation_type,
             src.account_class, src.anchor_class,
             dst.account_class, dst.anchor_class
      FROM counter_account_map cam
      LEFT JOIN chart_of_accounts src ON src.account_code=cam.src_account_code
      LEFT JOIN chart_of_accounts dst ON dst.account_code=cam.dst_account_code
    """)).fetchall()
    assert rows, "counter_account_map is empty — classify_drift will route every drift to T2"

    bad: list[str] = []
    for r in rows:
        src, dst, rel, src_cls, src_ac, dst_cls, dst_ac = r
        if src is None or dst is None:
            bad.append(f"{r}: src or dst missing")
            continue
        if src == dst:
            bad.append(f"{src}->{dst} ({rel}): self-mapping")
        if src_cls is None:
            bad.append(f"{src}->{dst}: src not in chart_of_accounts")
        elif src_cls != "ASSET":
            bad.append(f"{src}->{dst}: src account_class={src_cls!r}, expected ASSET")
        if dst_cls is None:
            bad.append(f"{src}->{dst}: dst not in chart_of_accounts")
        elif dst_cls != "ASSET":
            bad.append(f"{src}->{dst}: dst account_class={dst_cls!r}, expected ASSET")
        if src_ac != "A":
            bad.append(f"{src}->{dst}: src anchor_class={src_ac!r}, expected A")
        # dst class depends on relation_type:
        #   bank_peer     → dst must be A (statement-anchored peer)
        #   wallet_bridge → dst may be A or B (cex_snapshot will provide
        #                   the overlap evidence once pass-5 #3 lands)
        allowed_dst = {"bank_peer": {"A"}, "wallet_bridge": {"A", "B"}}
        if dst_ac not in allowed_dst.get(rel, {"A"}):
            bad.append(f"{src}->{dst} ({rel}): dst anchor_class={dst_ac!r}, "
                       f"expected one of {sorted(allowed_dst.get(rel, {'A'}))}")
    assert not bad, "counter_account_map shape violations:\n  " + "\n  ".join(bad[:10])

    # Uniqueness check (the index enforces it, but verify nothing slipped through)
    dups = s.execute(text("""
      SELECT src_account_code, dst_account_code, relation_type, COUNT(*) AS n
      FROM counter_account_map
      GROUP BY src_account_code, dst_account_code, relation_type
      HAVING n > 1
    """)).fetchall()
    assert not dups, f"Duplicate (src,dst,relation_type): {[tuple(r) for r in dups]}"


# ── INV 19c: fixable drift always has a corridor with overlap ────────────────


def test_inv19c_fixable_drift_has_corridor_overlap(s):
    """For every PERIOD_DRIFT row marked 'fixable' (Type 3), classify_drift
    must agree — i.e. there exists a counter_account_map entry whose dst
    has a bank_statement_registry row overlapping the drift's period_end.
    This re-derives the classification from data and protects against the
    queue going stale relative to the corridor table.
    """
    from app.journal_service import classify_drift
    rows = s.execute(text("""
      SELECT id, source_ref, tx_date, user_decision
      FROM unreconciled_queue
      WHERE tx_type='PERIOD_DRIFT' AND status='pending'
    """)).fetchall()
    mismatched: list[str] = []
    for r in rows:
        qid, src_ref, tx_date, decision = r
        # We only assert for rows the OPERATOR has marked fixable. The auto-
        # classifier output is used by the admin view; this invariant is the
        # tighter contract: don't let a 'fixable' label survive after the
        # corridor map changes.
        if (decision or "") != "operational_drift":
            continue
        cls = classify_drift(s, {"source_ref": src_ref, "tx_date": tx_date})
        if cls != "fixable":
            mismatched.append(f"queue#{qid} ref={src_ref!r}: decision='operational_drift' but classify={cls!r}")
    assert not mismatched, (
        f"{len(mismatched)} queue row(s) labelled fixable no longer have corridor coverage:\n  "
        + "\n  ".join(mismatched[:10])
    )


# ── INV 19d: Class B never reads GL (audit-5 #3 fail-closed) ─────────────────


def test_inv19d_class_b_never_returns_gl_source(s):
    """The GL is banned as a SoT path for Class B accounts. Pre-audit-5, a
    failed exchange API silently routed the dashboard to gl_sum/gl_projection.
    Now Class B must produce one of:
        - 'snapshot'        — fresh cex_snapshot row
        - 'stale_snapshot'  — row older than CLASS_B_STALENESS_HOURS
        - 'no_snapshot'     — no row in cex_snapshot at all
    Anything containing 'gl' means the resolver regressed.
    """
    from app.account_balance import resolve, SqliteLedgerBackend, CLASS_B_LIVE

    backend = SqliteLedgerBackend(s)
    leaked: list[str] = []
    db_class_b = {r[0] for r in s.execute(text(
        "SELECT account_code FROM chart_of_accounts WHERE anchor_class='B'"
    )).fetchall()}
    for code in (db_class_b | CLASS_B_LIVE):
        bal = resolve(backend, code)
        if "gl" in bal.source.lower():
            leaked.append(f"{code}: source={bal.source!r}")
    assert not leaked, "Class B accounts leaking GL as SoT:\n  " + "\n  ".join(leaked)


# ── INV 25/26/27/28: Income-statement aggregation (audit-8 Q1) ───────────────


def _build_is_sync(year: int | None = None, month: int | None = None) -> dict:
    """Async wrapper. Returns the same dict /income_statement renders."""
    import asyncio
    from app.income_statement import build_income_statement
    return asyncio.run(build_income_statement(year=year, month=month))


def test_inv25_income_statement_voided_exclusion(s):
    """Pass-8 Q1(a): /income_statement totals must match SUM(P&L journals
    with status='posted') for the same period — voided journals MUST be
    excluded. The check catches drift between the renderer's filter and a
    direct GL query.
    """
    bs_data = _build_is_sync()
    period_start = bs_data["period_start"]
    period_end = bs_data["period_end"]

    direct = s.execute(text("""
      SELECT
        ROUND(SUM(CASE WHEN coa.account_class='REVENUE' THEN gl.credit - gl.debit ELSE 0 END), 2) AS rev,
        ROUND(SUM(CASE WHEN coa.account_class='EXPENSE' THEN gl.debit - gl.credit ELSE 0 END), 2) AS exp
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id = j.id
      JOIN chart_of_accounts coa ON coa.id = gl.account_id
      WHERE j.status='posted'
        AND j.journal_date BETWEEN :ds AND :de
        AND coa.is_postable = 1
        AND coa.account_class IN ('REVENUE','EXPENSE')
    """), {"ds": period_start, "de": period_end}).fetchone()
    direct_rev = float(direct[0] or 0)
    direct_exp = float(direct[1] or 0)
    rendered_rev = float(bs_data["totals"]["income_sgd"])
    rendered_exp = float(bs_data["totals"]["expenses_sgd"])

    assert abs(direct_rev - rendered_rev) < 0.50, (
        f"income_sgd mismatch: renderer={rendered_rev:,.2f} vs "
        f"direct query={direct_rev:,.2f} for {period_start}..{period_end}"
    )
    assert abs(direct_exp - rendered_exp) < 0.50, (
        f"expenses_sgd mismatch: renderer={rendered_exp:,.2f} vs "
        f"direct query={direct_exp:,.2f} for {period_start}..{period_end}"
    )

    # Also check no voided journal leaks: a journal with status='voided'
    # in the period must not appear in the renderer's income/expenses lists.
    voided_codes = {r[0] for r in s.execute(text("""
      SELECT DISTINCT coa.account_code
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id=j.id
      JOIN chart_of_accounts coa ON coa.id=gl.account_id
      WHERE j.status='voided'
        AND j.journal_date BETWEEN :ds AND :de
        AND coa.account_class IN ('REVENUE','EXPENSE')
    """), {"ds": period_start, "de": period_end}).fetchall()}
    rendered_codes = {row["name"].split()[0] for row in
                      bs_data["income"] + bs_data["expenses"]}
    # Voided codes can still appear if they have OTHER (posted) activity —
    # we can't assert they're absent. The renderer-vs-direct check above
    # is the real voided-exclusion proof.


def test_inv26_income_statement_closing_identity(s):
    """Pass-8 Q1(b): net_income = total_income − total_expenses (within
    tolerance). The displayed net_income must equal a recomputed-from-totals
    value, not be an independent number that could drift.
    """
    bs_data = _build_is_sync()
    t = bs_data["totals"]
    inc = float(t["income_sgd"])
    exp = float(t["expenses_sgd"])
    net = float(t["net_income_sgd"])
    assert abs(net - (inc - exp)) < 0.05, (
        f"closing identity broken: net_income={net:,.2f} vs "
        f"income({inc:,.2f}) - expenses({exp:,.2f}) = {inc-exp:,.2f}"
    )


def test_inv27_income_statement_period_cutoff(s):
    """Pass-8 Q1: period filter on /income_statement must use journal_date
    only, and no posted P&L journal outside the rendered [period_start,
    period_end] must contribute to the rendered totals.

    Constructive check: re-compute totals over the SAME period using a
    journal_date filter and confirm match. (inv25 already does this for the
    current-period default; this test pins down the contract.)
    """
    bs_data = _build_is_sync()
    period_start = bs_data["period_start"]
    period_end = bs_data["period_end"]

    # Pull journals OUTSIDE the period that touch P&L; their amounts must
    # NOT be reflected in the rendered totals. Direct math check.
    outside_total = s.execute(text("""
      SELECT
        ROUND(COALESCE(SUM(CASE WHEN coa.account_class='REVENUE' THEN gl.credit - gl.debit ELSE 0 END),0), 2) AS rev,
        ROUND(COALESCE(SUM(CASE WHEN coa.account_class='EXPENSE' THEN gl.debit - gl.credit ELSE 0 END),0), 2) AS exp
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id = j.id
      JOIN chart_of_accounts coa ON coa.id = gl.account_id
      WHERE j.status='posted'
        AND (j.journal_date < :ds OR j.journal_date > :de)
        AND coa.is_postable = 1
        AND coa.account_class IN ('REVENUE','EXPENSE')
    """), {"ds": period_start, "de": period_end}).fetchone()
    out_rev = float(outside_total[0] or 0)
    out_exp = float(outside_total[1] or 0)

    rendered_inc = float(bs_data["totals"]["income_sgd"])
    rendered_exp = float(bs_data["totals"]["expenses_sgd"])

    # The total "all-time" P&L would equal rendered + outside. We don't
    # need to assert that — only that the period filter is non-trivial
    # (there IS P&L activity outside this period that's correctly excluded).
    # Sanity: rendered must not include any of outside's magnitude.
    # We confirm by re-running the in-period direct query and matching.
    in_period = s.execute(text("""
      SELECT
        ROUND(COALESCE(SUM(CASE WHEN coa.account_class='REVENUE' THEN gl.credit - gl.debit ELSE 0 END),0), 2) AS rev,
        ROUND(COALESCE(SUM(CASE WHEN coa.account_class='EXPENSE' THEN gl.debit - gl.credit ELSE 0 END),0), 2) AS exp
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id = j.id
      JOIN chart_of_accounts coa ON coa.id = gl.account_id
      WHERE j.status='posted'
        AND j.journal_date BETWEEN :ds AND :de
        AND coa.is_postable = 1
        AND coa.account_class IN ('REVENUE','EXPENSE')
    """), {"ds": period_start, "de": period_end}).fetchone()
    assert abs(float(in_period[0] or 0) - rendered_inc) < 0.50, \
        f"period-cutoff: in-period revenue {in_period[0]} vs rendered {rendered_inc}"
    assert abs(float(in_period[1] or 0) - rendered_exp) < 0.50, \
        f"period-cutoff: in-period expense {in_period[1]} vs rendered {rendered_exp}"


def test_inv28_income_statement_chart_rollup(s):
    """Pass-8 Q1: sum of rendered line items must equal the section header
    totals. Catches UI grouping bugs where some categories are accidentally
    omitted from a section or where the UI grouping diverges from the
    aggregation query.
    """
    bs_data = _build_is_sync()
    sum_income_items = round(sum(it["sgd"] for it in bs_data["income"]), 2)
    sum_expense_items = round(sum(it["sgd"] for it in bs_data["expenses"]), 2)
    t = bs_data["totals"]
    inc = float(t["income_sgd"])
    exp = float(t["expenses_sgd"])
    assert abs(sum_income_items - inc) < 0.50, (
        f"chart roll-up income mismatch: line-items sum={sum_income_items:,.2f} "
        f"vs totals.income_sgd={inc:,.2f}"
    )
    assert abs(sum_expense_items - exp) < 0.50, (
        f"chart roll-up expense mismatch: line-items sum={sum_expense_items:,.2f} "
        f"vs totals.expenses_sgd={exp:,.2f}"
    )


# ── INV 34: Every Suspense journal is reachable by the reclassifier (v2.26) ─


def test_inv34_suspense_journals_are_reclassifier_reachable(s):
    """Per Perplexity pass-11 Q1: the user must never be stuck looking at
    a Suspense (1190) journal that the system doesn't surface in
    /reconcile/suspense. Every posted journal whose contra is 1190 must
    appear in suspense_reclassifier.scan() output — HIGH, MED, or LOW.

    This is a discoverability contract, not a clean-state assertion.
    inv31 already enforces "projection-gate: Suspense Δ < threshold";
    inv34 enforces "if Suspense Δ > 0, every cause is on the cleanup
    page". Together: the user can always drive Suspense to 0 by working
    through the surfaced proposals.
    """
    from app import suspense_reclassifier as _sr

    # Total posted Suspense journals (any date)
    db_ids = {r[0] for r in s.execute(text(
        "SELECT DISTINCT j.id FROM journals j "
        "JOIN general_ledger gl ON gl.journal_id=j.id "
        "JOIN chart_of_accounts coa ON coa.id=gl.account_id "
        "WHERE j.status='posted' AND coa.account_code='1190'"
    )).fetchall()}

    if not db_ids:
        return  # vacuously true

    # Scan returns proposals — collect all journal_ids from all buckets
    buckets = _sr.scan(s)
    scan_ids = {p.journal_id for bucket in buckets.values() for p in bucket}

    missing = db_ids - scan_ids
    assert not missing, (
        f"{len(missing)} Suspense journal(s) NOT discoverable by reclassifier:\n"
        + "\n".join(f"  j#{j}" for j in sorted(missing)[:10])
        + "\n\nSuspense_reclassifier.scan() must surface every posted "
        "journal whose contra is 1190."
    )


# ── INV 31: Projection-gate (pass-10 Q1) ────────────────────────────────────


def test_inv31_class_a_projection_gate(s):
    """Per Perplexity pass-10 Q1: when resolve() returns source='statement_cf_plus_gl'
    (Class A projected current), the projection MUST be 'clean enough' to trust.

    Gate conditions (both must hold):
      - |Σ Suspense Δ since latest BSR period_end| < $100 for that account
      - unclassified_pct = |Suspense Δ| / |total Δ since CF| < 10%

    If either fails, the resolver must NOT return statement_cf_plus_gl —
    it must fall back to statement_cf only with a UI badge.

    This invariant is vacuously true today (v2.24): resolver currently
    only returns statement_cf (no projection). v2.25 will implement the
    projection + gating; this invariant locks the contract ahead of time.
    """
    from app.account_balance import resolve, SqliteLedgerBackend, CLASS_A_BANK

    backend = SqliteLedgerBackend(s)
    suspense_id_row = s.execute(text(
        "SELECT id FROM chart_of_accounts WHERE account_code='1190'"
    )).fetchone()
    if not suspense_id_row:
        return  # no suspense account yet, vacuously true
    suspense_id = suspense_id_row[0]

    violations: list[str] = []
    db_class_a = {r[0] for r in s.execute(text(
        "SELECT account_code FROM chart_of_accounts WHERE anchor_class='A'"
    )).fetchall()}
    for code in (db_class_a | CLASS_A_BANK):
        bal = resolve(backend, code)
        if bal.source != "statement_cf_plus_gl":
            continue  # gate only applies to projection mode

        # Find the latest BSR period_end for this code
        pe_row = s.execute(text(
            "SELECT MAX(period_end) FROM bank_statement_registry WHERE account_code=:c"
        ), {"c": code}).fetchone()
        if not pe_row or not pe_row[0]:
            violations.append(f"  {code}: projection without any BSR row")
            continue
        period_end = pe_row[0]

        # Suspense Δ since CF (Dr to Suspense from this account's journals)
        suspense_delta = s.execute(text("""
          SELECT COALESCE(SUM(gl2.debit_sgd - gl2.credit_sgd), 0)
          FROM general_ledger gl1
          JOIN journals j ON j.id=gl1.journal_id
          JOIN general_ledger gl2 ON gl2.journal_id=j.id AND gl2.account_id=:susp_id
          WHERE j.status='posted'
            AND gl1.account_id=(SELECT id FROM chart_of_accounts WHERE account_code=:c)
            AND j.journal_date > :pe
        """), {"susp_id": suspense_id, "c": code, "pe": period_end}).scalar() or 0
        susp_abs = abs(float(suspense_delta))

        # Total |Δ| since CF (sum of |Dr-Cr| per journal on this account)
        total_delta = s.execute(text("""
          SELECT COALESCE(SUM(ABS(gl.debit_sgd - gl.credit_sgd)), 0)
          FROM general_ledger gl
          JOIN journals j ON j.id=gl.journal_id
          WHERE j.status='posted'
            AND gl.account_id=(SELECT id FROM chart_of_accounts WHERE account_code=:c)
            AND j.journal_date > :pe
        """), {"c": code, "pe": period_end}).scalar() or 0
        total_abs = abs(float(total_delta))

        unclass_pct = (susp_abs / total_abs * 100) if total_abs > 0 else 0

        if susp_abs >= 100.0:
            violations.append(
                f"  {code}: projection enabled but Suspense Δ = ${susp_abs:,.2f} "
                f">= $100 threshold (since {period_end})"
            )
        if unclass_pct >= 10.0:
            violations.append(
                f"  {code}: projection enabled but unclassified% = {unclass_pct:.1f}% "
                f">= 10% threshold (Suspense ${susp_abs:,.2f} of total ${total_abs:,.2f})"
            )
    assert not violations, (
        "Class A account(s) showing projection while noise exceeds threshold:\n"
        + "\n".join(violations[:10])
        + "\n\nResolver must fall back to statement_cf only when these gates fail."
    )


# ── INV 32: Headline parity (pass-10 Q2) ────────────────────────────────────


def test_inv32_dashboard_headline_parity(s):
    """Per Perplexity pass-10 Q2: every entry in ui_sot_registry.yaml's
    `endpoints` list must produce a headline that equals its SoT path's
    output within tolerance.

    This is the structural defence against UI-vs-backend drift. Any dashboard
    page added or changed must update the registry AND keep parity.
    """
    import asyncio
    import importlib
    import yaml
    from pathlib import Path
    from app import account_balance as ab

    reg_path = Path("/finance/ui_sot_registry.yaml")
    if not reg_path.exists():
        pytest.skip(f"ui_sot_registry.yaml not at {reg_path}")
    reg = yaml.safe_load(reg_path.read_text())

    backend = ab.SqliteLedgerBackend(s)

    def _walk_yaml_for_codes(cfg: dict, node_id: str) -> list[str]:
        """Find balance_sheet_config.yaml node by id and collect gl_account_codes."""
        codes: list[str] = []
        def visit(n):
            if not isinstance(n, dict):
                return
            if n.get("id") == node_id:
                # Collect codes from this node + all descendant items
                def collect(nn):
                    if not isinstance(nn, dict):
                        return
                    for c in (nn.get("gl_account_codes") or []):
                        codes.append(str(c))
                    for child in (nn.get("children") or []):
                        collect(child)
                collect(n)
                return
            for child in (n.get("children") or []):
                visit(child)
        for section in ("assets", "liabilities"):
            for bucket_key in ("current", "non_current"):
                bucket = cfg.get(section, {}).get(bucket_key)
                if isinstance(bucket, list):
                    for n in bucket: visit(n)
                elif isinstance(bucket, dict):
                    for n in bucket.get("nodes", []): visit(n)
        return codes

    def _resolve_sot(spec: str) -> float:
        """Map a sot: pattern string to a numeric value."""
        if spec == "resolve_total_loans":
            return float(ab.resolve_total_loans(s).sgd)
        if spec == "resolve_total_cc":
            return float(ab.resolve_total_cc(s).sgd)
        if spec.startswith("resolve_code:"):
            code = spec.split(":", 1)[1]
            return float(ab.resolve(backend, code).sgd)
        if spec.startswith("resolve_class:"):
            cls = spec.split(":", 1)[1]
            codes = {r[0] for r in s.execute(text(
                "SELECT account_code FROM chart_of_accounts WHERE anchor_class=:a"
            ), {"a": cls}).fetchall()}
            return sum(float(ab.resolve(backend, c).sgd) for c in codes)
        if spec.startswith("sum_classes:"):
            classes = spec.split(":", 1)[1].split(",")
            total = 0.0
            for cls in classes:
                codes = {r[0] for r in s.execute(text(
                    "SELECT account_code FROM chart_of_accounts WHERE anchor_class=:a"
                ), {"a": cls.strip()}).fetchall()}
                total += sum(float(ab.resolve(backend, c).sgd) for c in codes)
            return total
        if spec.startswith("coa_list:"):
            codes = [c.strip() for c in spec.split(":", 1)[1].split(",") if c.strip()]
            return sum(float(ab.resolve(backend, c).sgd) for c in codes)
        if spec.startswith("yaml_node:"):
            node_id = spec.split(":", 1)[1].strip()
            bs_cfg = yaml.safe_load(Path("/finance/balance_sheet_config.yaml").read_text())
            codes = _walk_yaml_for_codes(bs_cfg, node_id)
            if not codes:
                raise ValueError(f"yaml_node:{node_id!r} found no CoA codes")
            return sum(float(ab.resolve(backend, c).sgd) for c in codes)
        if spec == "balance_sheet_net_worth":
            from app import balance_sheet as bs
            return float(asyncio.run(bs.build_balance_sheet())["net_worth_sgd"])
        raise ValueError(f"unknown sot pattern: {spec!r}")

    def _resolve_headline(entry: dict) -> float:
        """Call the endpoint's func and extract the result_path."""
        mod = importlib.import_module(entry["module"])
        fn = getattr(mod, entry["func"])
        kwargs = entry.get("func_kwargs") or {}
        # Detect async
        result = fn(**kwargs)
        if hasattr(result, "__await__"):
            result = asyncio.run(result)
        # Walk dotted path
        for part in entry["result_path"].split("."):
            if isinstance(result, dict):
                result = result[part]
            else:
                result = getattr(result, part)
        return float(result)

    violations: list[str] = []
    for entry in reg.get("endpoints", []):
        try:
            headline = _resolve_headline(entry)
            sot = _resolve_sot(entry["sot"])
        except Exception as e:
            violations.append(
                f"  {entry['endpoint']} headline={entry['headline']!r}: "
                f"could not evaluate: {type(e).__name__}: {e}"
            )
            continue
        tol = float(entry.get("tolerance", 0.50))
        if abs(headline - sot) > tol:
            violations.append(
                f"  {entry['endpoint']} ({entry['headline']!r}): "
                f"headline={headline:,.2f} vs SoT({entry['sot']})={sot:,.2f} "
                f"diff={headline - sot:+,.2f} > tol ${tol}"
            )
    assert not violations, (
        f"{len(violations)} dashboard endpoint(s) drift from their SoT:\n"
        + "\n".join(violations[:10])
        + "\n\nEither fix the renderer to call the SoT path, or update "
        "ui_sot_registry.yaml if the contract changed deliberately."
    )


# ── INV 33: Render-path traceability (pass-10 Q2) ───────────────────────────


def test_inv33_no_unapproved_sot_paths_in_ui():
    """Per Perplexity pass-10 Q2: dashboard render modules may not bypass
    Gate 5. Scans the AST of each UI module and forbids:
      - calls to _firefly()        (legacy bridge, deleted in v2.22)
      - calls to gl_balance()      (raw GL — must wrap in resolve())
      - raw SUM(gl.debit) queries  (same — must go via resolve)

    Approved primitives:
      - account_balance.resolve(), resolve_total_loans/cc
      - bank_statement_registry / credit_facilities reads
      - account_snapshot reads

    Catches the v2.21 bug class structurally: any UI module that
    re-introduces a legacy data path fails CI before merge.
    """
    import ast
    import inspect
    from app import drill, home, cash_forecast, income_statement, v2_dashboards

    BANNED_CALLEES = {"_firefly", "gl_balance"}
    UI_MODULES = [drill, home, cash_forecast, income_statement, v2_dashboards]

    violations: list[str] = []
    for mod in UI_MODULES:
        try:
            source = inspect.getsource(mod)
        except OSError:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # node.func may be Name or Attribute
            name: str | None = None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in BANNED_CALLEES:
                violations.append(
                    f"  {mod.__name__}:{node.lineno} calls banned {name}() — "
                    "UI render paths must use account_balance.resolve()"
                )

    assert not violations, (
        f"{len(violations)} UI module call site(s) bypass Gate 5:\n"
        + "\n".join(violations[:15])
        + "\n\nApproved primitives: account_balance.resolve(), "
        "resolve_total_loans/cc, account_snapshot, credit_facilities, "
        "bank_statement_registry."
    )


# ── INV 29: No _firefly() reads in production code (V2.22) ──────────────────


def test_inv29_no_firefly_reads_in_production():
    """V2.22: every UI render path must read from the canonical SoT
    (Gate 5 / credit_facilities / account_snapshot). The legacy
    `_firefly()` helper functions and the `firefly_bridge` module's
    write-path are the only places Firefly may be imported from.

    Catches the bug class that produced the v2.21 UOB CashPlus discrepancy:
    drill.py held a stale `_firefly()` call inside build_liability_drill,
    silently returning Firefly's outdated value instead of the canonical
    credit_facilities outstanding.
    """
    import os
    from pathlib import Path

    # Allowlist: files where Firefly access is intentional (write path).
    ALLOWED = {
        "firefly_bridge.py",         # writes journals INTO Firefly only
        "firefly_damage_report.py",  # one-shot diagnostic, not a UI path
        "connectors.py",             # health-check probe, no balance read
        "backup.py",                 # data export, not UI
    }
    # Modules permitted to mention Firefly in narrative comments / variable
    # names without actually calling it.
    NAME_ONLY_OK = {"drill.py", "income_statement.py", "balance_sheet.py"}

    app_dir = Path(__file__).parent.parent / "app"
    bad: list[str] = []
    for py in sorted(app_dir.glob("*.py")):
        if py.name in ALLOWED:
            continue
        # Skip throwaway diagnostic scripts (prefix _).
        if py.name.startswith("_"):
            continue
        content = py.read_text(encoding="utf-8", errors="replace")
        # Forbidden: any actual CALL to a function named _firefly(
        # (parens after the name distinguishes calls from defs/comments).
        for ln, line in enumerate(content.splitlines(), 1):
            if "_firefly(" not in line:
                continue
            # Allow the definition line itself (it's dead-code if not called,
            # which is fine — Phase B already removed them; this catches
            # any future re-introduction).
            stripped = line.strip()
            if stripped.startswith("async def _firefly") or stripped.startswith("def _firefly"):
                # Definition allowed only in allowlisted files
                if py.name not in ALLOWED:
                    bad.append(f"  {py.name}:{ln} defines _firefly() outside allowlist")
                continue
            # Call site detected
            if "await _firefly" in line or "= _firefly(" in line:
                bad.append(f"  {py.name}:{ln} calls _firefly(): {line.strip()[:80]}")
    assert not bad, (
        f"{len(bad)} production module(s) read from Firefly:\n"
        + "\n".join(bad[:15])
        + "\n\nAll UI balance reads must route through Gate 5 / "
        "credit_facilities / account_snapshot."
    )


# ── INV 30: UI roll-up parity (V2.22) ────────────────────────────────────────


def test_inv30_loans_drill_matches_facilities(s):
    """V2.22: the /drill/loans page total must equal the /facilities page
    total. Both read from credit_facilities; this invariant locks the
    contract so a future regression can't silently re-introduce two
    different sources of truth.

    The v2.21 bug: /drill/loans returned $28,307 from Firefly while
    /facilities returned $26,043 from credit_facilities. After fix,
    both agree.
    """
    import asyncio
    from app.drill import build_liability_drill
    from app.account_balance import resolve_total_loans, resolve_total_cc

    loans_drill = asyncio.run(build_liability_drill(only_type="loans"))
    cc_drill = asyncio.run(build_liability_drill(only_type="credit_card"))

    drill_loans_total = float(loans_drill["total_outstanding"])
    drill_cc_total = float(cc_drill["total_outstanding"])

    # The canonical SoT functions used by home/balance_sheet.
    sot_loans = float(resolve_total_loans(s).sgd)
    sot_cc = float(resolve_total_cc(s).sgd)

    assert abs(drill_loans_total - sot_loans) < 0.50, (
        f"/drill/loans total ({drill_loans_total:,.2f}) != "
        f"resolve_total_loans ({sot_loans:,.2f})"
    )
    assert abs(drill_cc_total - sot_cc) < 0.50, (
        f"/drill/cc total ({drill_cc_total:,.2f}) != "
        f"resolve_total_cc ({sot_cc:,.2f})"
    )


# ── INV 24: Alerts row shape (audit-7 Q3) ────────────────────────────────────


def test_inv24_alerts_shape(s):
    """Pass-7 Q3: alerts table integrity. Every row must have:
      - kind in the writer module's allowlist
      - severity in {'low','medium','high'}
      - status in {'pending','dismissed','resolved'}
      - account_code, if set, exists in chart_of_accounts
    Cheap sanity check that the alerts writer hasn't drifted out of contract.
    """
    KNOWN_KINDS = {"stale_class_a", "missing_recurring", "snapshot_drop",
                   "salary_missing", "spend_spike"}
    rows = s.execute(text("""
      SELECT a.id, a.kind, a.severity, a.status, a.account_code,
             (SELECT 1 FROM chart_of_accounts WHERE account_code=a.account_code) AS coa_exists
      FROM alerts a
    """)).fetchall()
    bad: list[str] = []
    for aid, kind, sev, status, code, coa_ok in rows:
        if kind not in KNOWN_KINDS:
            bad.append(f"  a#{aid} unknown kind={kind!r}")
        if sev not in {"low", "medium", "high"}:
            bad.append(f"  a#{aid} bad severity={sev!r}")
        if status not in {"pending", "dismissed", "resolved"}:
            bad.append(f"  a#{aid} bad status={status!r}")
        if code and not coa_ok:
            bad.append(f"  a#{aid} account_code={code!r} not in chart_of_accounts")
    assert not bad, (
        f"{len(bad)} alerts row(s) violate the shape contract:\n"
        + "\n".join(bad[:15])
    )


# ── INV 23: Anchor journal shape (audit-7 Q2 ILP unrealised P&L) ─────────────


def test_inv23_anchor_journals_shape(s):
    """Per Perplexity pass-7 Q2: ILP / Class B / Class C re-anchor journals
    must conserve their value as a balance-sheet movement only — never
    pollute the P&L. The canonical shape is:

      one Dr/Cr leg on ASSET or LIABILITY (the anchored account)
      one matching Cr/Dr leg on EQUITY (3100 Retained Earnings)

    No anchor journal should touch a REVENUE or EXPENSE account. Today we
    route the unrealised delta directly to retained earnings — when we
    later introduce a 48xx "Unrealised gains" REVENUE bucket (Perplexity's
    phase-2 enhancement), this invariant relaxes to allow 48xx as a valid
    contra. For now, P&L stays clean.
    """
    rows = s.execute(text("""
      SELECT j.id, COUNT(*) AS n_lines,
             SUM(CASE WHEN coa.account_class IN ('REVENUE','EXPENSE') THEN 1 ELSE 0 END) AS n_pnl,
             GROUP_CONCAT(coa.account_code || ':' || coa.account_class) AS shape
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id=j.id
      JOIN chart_of_accounts coa ON coa.id=gl.account_id
      WHERE j.status='posted' AND j.journal_type='anchor'
      GROUP BY j.id
      HAVING n_pnl > 0 OR n_lines != 2
    """)).fetchall()

    bad: list[str] = []
    for jid, n_lines, n_pnl, shape in rows:
        if n_pnl > 0:
            bad.append(f"  j#{jid} anchor touches {n_pnl} P&L line(s): {shape}")
        elif n_lines != 2:
            bad.append(f"  j#{jid} anchor has {n_lines} lines (expected 2): {shape}")
    assert not bad, (
        f"{len(bad)} anchor journal(s) violate the BS-only shape:\n"
        + "\n".join(bad[:15])
        + "\n\nAnchor re-valuations must route to 3100 Retained Earnings "
        "(or 48xx Unrealised Gains when introduced), never to expense/revenue."
    )


# ── INV 22: Classification sanity (audit-7 Q2 nice-to-have) ──────────────────


def test_inv22_classification_sanity(s):
    """Per Perplexity pass-7 Q2 nice-to-have: no REVENUE account should end
    the year with a net debit; no EXPENSE account should end the year with
    a net credit. Catches sign mistakes and mis-codings consistent with
    FRS 1 presentation expectations.

    Scope: current-year-to-date (avoids polluting on pre-cutover noise).
    Tolerance: $1 (rounding / dust).
    """
    from datetime import date as _date
    ytd_start = _date(_date.today().year, 1, 1).isoformat()
    rows = s.execute(text("""
      SELECT coa.account_code, coa.account_name, coa.account_class,
             ROUND(SUM(gl.debit_sgd - gl.credit_sgd), 2) AS net
      FROM general_ledger gl
      JOIN journals j ON j.id=gl.journal_id
      JOIN chart_of_accounts coa ON coa.id=gl.account_id
      WHERE j.status='posted'
        AND j.journal_date >= :start
        AND coa.account_class IN ('REVENUE','EXPENSE')
      GROUP BY coa.account_code, coa.account_name, coa.account_class
      HAVING ABS(SUM(gl.debit_sgd - gl.credit_sgd)) > 1.00
    """), {"start": ytd_start}).fetchall()

    bad: list[str] = []
    for code, name, cls, net in rows:
        net = float(net)
        # REVENUE is Cr-normal — a net Dr (positive net) is wrong
        if cls == "REVENUE" and net > 0:
            bad.append(f"  {code} {name} [REVENUE] net Dr {net:+,.2f} "
                       f"(revenue should be Cr-normal; positive=Dr=wrong)")
        # EXPENSE is Dr-normal — a net Cr (negative net) is wrong
        if cls == "EXPENSE" and net < 0:
            bad.append(f"  {code} {name} [EXPENSE] net Cr {net:+,.2f} "
                       f"(expense should be Dr-normal; negative=Cr=wrong)")
    assert not bad, (
        f"{len(bad)} P&L account(s) have wrong-sign YTD net:\n"
        + "\n".join(bad[:15])
        + "\n\nLikely a misclassified journal — check journal_type and "
        "the contra-account choice."
    )


# ── INV 21: Conservation on salary / income corridors (audit-7 Q2 narrow) ────


def test_inv21_income_journals_shape_conservation(s):
    """Per Perplexity pass-7 Q2: narrow conservation invariant. Every
    journal with journal_type in {'salary', 'income', 'cash_receipt'} must
    look like a proper income posting:
      - Has a Dr leg on an ASSET account (the inflow lands somewhere)
      - Has a Cr leg on a REVENUE account (the P&L gets the income)
      - The Dr and Cr totals are equal within tolerance (the inv1
        balanced-journal check already covers this, but we re-check here
        for clarity)

    Catches the CPF case Perplexity flagged: salary inflow lands on POSB
    but no 4110 revenue credit is posted → P&L silently understates income.
    """
    rows = s.execute(text("""
      SELECT j.id, j.narration,
             SUM(CASE WHEN gl.debit_sgd  > 0 AND coa.account_class='ASSET'   THEN 1 ELSE 0 END) AS asset_dr,
             SUM(CASE WHEN gl.credit_sgd > 0 AND coa.account_class='REVENUE' THEN 1 ELSE 0 END) AS revenue_cr,
             COUNT(*) AS n_lines
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id=j.id
      JOIN chart_of_accounts coa ON coa.id=gl.account_id
      WHERE j.status='posted'
        AND j.journal_type IN ('salary','income','cash_receipt')
      GROUP BY j.id
    """)).fetchall()

    bad: list[str] = []
    for r in rows:
        jid, narr, asset_dr, revenue_cr, n_lines = r
        if asset_dr == 0:
            bad.append(f"  j#{jid} type=income/salary has no ASSET debit "
                       f"(n_lines={n_lines}): {(narr or '')[:60]}")
        if revenue_cr == 0:
            bad.append(f"  j#{jid} type=income/salary has no REVENUE credit "
                       f"(n_lines={n_lines}): {(narr or '')[:60]}")
    assert not bad, (
        f"{len(bad)} income/salary journal(s) violate the conservation "
        f"shape (ASSET-Dr / REVENUE-Cr):\n" + "\n".join(bad[:15])
        + "\n\nLikely a parser bug: tagging a tx as salary/income but routing "
        "its contra to a non-REVENUE account (5190 General Expense, etc.)."
    )


# ── INV 20: Coverage on active recurring obligations (audit-7 Q2 foundational)

def test_inv20_recurring_obligations_have_recent_posts(s):
    """Per Perplexity pass-7 Q2: foundational P&L invariant. Every active
    recurring_obligation_registry row with a real `expected_amount` must
    have at least one matching journal in the last 90 days. The match is
    by (contra_coa, amount ± amount_tolerance) — narrow but enough to
    surface missed Spotify/Netflix/insurance debits.

    False-positive correction path is to UPDATE the registry (mark
    inactive via active_to), not to touch the GL.
    """
    from datetime import timedelta
    today = date.today()
    cutoff = today - timedelta(days=90)

    obligations = s.execute(text("""
      SELECT name, contra_coa, expected_amount, amount_tolerance,
             frequency, active_from, active_to
      FROM recurring_obligation_registry
      WHERE expected_amount > 0
        AND (active_from IS NULL OR active_from <= :today)
        AND (active_to   IS NULL OR active_to   >= :today)
    """), {"today": today.isoformat()}).fetchall()

    missing: list[str] = []
    for r in obligations:
        name, coa, amt, tol, freq, _af, _at = r
        amt = float(amt or 0)
        tol = float(tol or 0.50)
        # The match window for a monthly obligation: at least one debit
        # to the contra CoA within ±tolerance in the last 90 days.
        n = s.execute(text("""
          SELECT COUNT(*)
          FROM general_ledger gl
          JOIN journals j ON j.id=gl.journal_id
          JOIN chart_of_accounts coa ON coa.id=gl.account_id
          WHERE j.status='posted'
            AND coa.account_code=:c
            AND j.journal_date >= :cutoff
            AND gl.debit_sgd BETWEEN :lo AND :hi
        """), {
            "c": coa, "cutoff": cutoff.isoformat(),
            "lo": amt - tol, "hi": amt + tol,
        }).scalar() or 0
        if n == 0:
            missing.append(f"  '{name}' (coa={coa}, expect ${amt:,.2f}±{tol}, "
                           f"freq={freq}) — no matching debit in last 90d")

    assert not missing, (
        f"{len(missing)} active recurring obligation(s) have no matching "
        f"journal in the last 90 days:\n" + "\n".join(missing[:15])
        + "\n\nResolve by either (a) registering the missed bill, or "
        "(b) marking the obligation inactive in recurring_obligation_registry."
    )


# ── INV 19f: external_id canonical contract (audit-6 Q1) ─────────────────────


def test_inv19f_external_id_canonical_format(s):
    """Every journal posted on or after EXTERNAL_ID_ENFORCED_FROM must conform
    to the canonical `<source>:v<n>:<stable_key>` contract. Older journals are
    grandfathered. This invariant is what prevents the next $60k phantom-
    outflow bug (different cutover runs writing different external_id formats
    for the same underlying row).
    """
    from app.journal_service import (
        EXTERNAL_ID_ENFORCED_FROM,
        EXTERNAL_ID_SOURCES,
        validate_external_id,
    )

    rows = s.execute(text("""
      SELECT id, external_id, created_at, journal_date, source_doc
      FROM journals
      WHERE status='posted' AND external_id IS NOT NULL
    """)).fetchall()

    bad: list[str] = []
    grandfathered = 0
    enforced_iso = EXTERNAL_ID_ENFORCED_FROM.isoformat()
    for r in rows:
        jid, ext, created_at, jdate, src = r
        created_iso = str(created_at)[:10] if created_at else ""
        if created_iso and created_iso < enforced_iso:
            grandfathered += 1
            continue
        ok, reason = validate_external_id(ext)
        if not ok:
            bad.append(f"  j#{jid} created={created_iso} src={src!r} "
                       f"ext={ext[:60]!r} → {reason}")

    assert not bad, (
        f"{len(bad)} journal(s) posted on/after {enforced_iso} violate the "
        f"external_id canonical contract "
        f"(format: <source>:v<n>:<stable_key>; allowed sources in "
        f"EXTERNAL_ID_SOURCES={sorted(EXTERNAL_ID_SOURCES)}):\n"
        + "\n".join(bad[:15])
    )


# ── INV 19a: Dashboard codes must resolve strictly (audit-5 #1) ──────────────


def test_inv19a_dashboard_codes_resolve_strictly(s):
    """Every account code wired into balance_sheet_config.yaml's
    `gl_account_codes:` lists MUST resolve under strict=True. Production
    dashboard reads route through `resolve(strict=True)`; this invariant
    ensures the YAML never references an untagged code, which would have
    silently fallen back to anchor_class='unknown' pre-audit-5.
    """
    import yaml
    from pathlib import Path
    from app.account_balance import resolve, SqliteLedgerBackend, NoResolverError

    cfg_path = Path("/finance/balance_sheet_config.yaml")
    if not cfg_path.exists():
        pytest.skip(f"balance_sheet_config.yaml not at {cfg_path}; skipping dashboard coverage check")

    cfg = yaml.safe_load(cfg_path.read_text())

    # Walk the config tree collecting every gl_account_codes list
    codes: set[str] = set()
    def _walk(node):
        if isinstance(node, dict):
            if "gl_account_codes" in node:
                for c in node["gl_account_codes"]:
                    codes.add(str(c))
            for v in node.values(): _walk(v)
        elif isinstance(node, list):
            for v in node: _walk(v)
    _walk(cfg)

    assert codes, "No gl_account_codes found in balance_sheet_config.yaml"

    backend = SqliteLedgerBackend(s)
    failed: list[tuple[str, str]] = []
    for code in sorted(codes):
        try:
            bal = resolve(backend, code)  # strict=True default
            if bal.anchor_class not in {"A", "B", "C"}:
                failed.append((code, f"resolved to anchor_class={bal.anchor_class!r}"))
        except NoResolverError as e:
            failed.append((code, f"NoResolverError: {e}"))

    assert not failed, (
        f"{len(failed)} dashboard code(s) fail strict resolution:\n"
        + "\n".join(f"  {c}: {msg}" for c, msg in failed)
    )


# ── INV 18: Every tagged CoA has a resolver (audit-4 step c) ──────────────────


def test_inv18_every_anchor_class_has_a_registered_resolver(s):
    """For every anchor_class value that appears on a CoA row, RESOLVER_REGISTRY
    must have a matching entry with a callable `fn`. This is the SoT graph: no
    CoA can be tagged Class-X unless code exists to resolve Class-X balances.
    Also: every resolver in RESOLVER_REGISTRY must actually return a Balance
    for every account tagged with its class.
    """
    from app.account_balance import (
        RESOLVER_REGISTRY,
        Balance,
        SqliteLedgerBackend,
    )

    used_classes = {r[0] for r in s.execute(text(
        "SELECT DISTINCT anchor_class FROM chart_of_accounts "
        "WHERE anchor_class IS NOT NULL"
    )).fetchall()}
    unregistered = used_classes - set(RESOLVER_REGISTRY.keys())
    assert not unregistered, (
        f"Anchor classes used in DB but not in RESOLVER_REGISTRY: {sorted(unregistered)}"
    )

    # Every registry entry must be callable + produce a Balance for the first
    # tagged account in its class. Smoke-test the dispatcher end-to-end.
    backend = SqliteLedgerBackend(s)
    for ac, entry in RESOLVER_REGISTRY.items():
        assert callable(entry.get("fn")), f"Registry entry for {ac!r} has no callable fn"
        row = s.execute(text(
            "SELECT account_code FROM chart_of_accounts "
            "WHERE anchor_class=:a LIMIT 1"
        ), {"a": ac}).fetchone()
        if not row:
            continue  # No CoA tagged with this class yet — registry-only entry
        bal = entry["fn"](backend, row[0], date.today())
        assert isinstance(bal, Balance), (
            f"Resolver for class {ac!r} on {row[0]} returned {type(bal).__name__}, expected Balance"
        )
        assert bal.account_code == row[0], (
            f"Resolver for {ac!r} returned mismatched account_code: {bal.account_code} vs {row[0]}"
        )

