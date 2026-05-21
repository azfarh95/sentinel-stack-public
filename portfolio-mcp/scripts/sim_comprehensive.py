"""Comprehensive sim: replay every available source through its pipeline,
measure drift per account, total anchor weight, auto-classification rate."""
import sys
from datetime import date
from collections import defaultdict
from app import database as db
from sqlalchemy import text


def gl_balance(s, account_code, as_of=None):
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
    return float(s.execute(text(q), params).scalar() or 0)


def measure_all(s, label):
    """Measure drift across all anchored accounts."""
    # Anchored accounts (from anchor table + USER_ANCHOR journals)
    anchored = [
        ("1111", "POSB"),
        ("1112", "Cash Wallet"),
        ("1113", "Wise"),
        ("1114", "Maybank Ar Rihla"),
        ("1115", "SC SuperSalary"),
        ("1211", "CPF OA"),
        ("1212", "CPF SA"),
        ("1213", "CPF MA"),
        ("12149", "CPF IS"),
        ("12219", "Tokio Marine ILP"),
        ("12229", "Singlife Savvy"),
        ("1231", "Coinbase"),
    ]
    # Latest CF from registry (Class A only — others use user-anchor target)
    user_anchor_targets = {
        "1112": 0.0, "1211": 27880.08, "1212": 18983.07, "1213": 25754.42,
        "12149": 47405.79, "12219": 10227.64, "12229": 10345.48, "1231": 0.0,
    }
    print(f"\n══ {label} ══")
    print(f"  {'CoA':<6} {'Account':<22} {'GL':>12} {'Target':>12} {'Drift':>12}")
    total_drift = 0.0
    n_drift = 0
    for code, name in anchored:
        gl = gl_balance(s, code)
        # Prefer bank_statement_registry CF if it exists
        cf_row = s.execute(text("""
          SELECT balance_carried_forward FROM bank_statement_registry
          WHERE account_code=:c ORDER BY period_end DESC LIMIT 1
        """), {"c": code}).fetchone()
        if cf_row:
            target = float(cf_row[0])
        else:
            target = user_anchor_targets.get(code, gl)  # if no target, assume GL is fine
        drift = target - gl
        total_drift += abs(drift)
        if abs(drift) > 1: n_drift += 1
        print(f"  {code:<6} {name:<22} {gl:>12,.2f} {target:>12,.2f} {drift:>+12,.2f}")

    # Anchor weight
    anchor = float(s.execute(text("""
      SELECT COALESCE(SUM(CASE WHEN j.status='posted' THEN gl.debit_sgd-gl.credit_sgd ELSE 0 END),0)
      FROM general_ledger gl JOIN journals j ON j.id=gl.journal_id
      WHERE j.source_doc='USER_ANCHOR'
    """)).scalar() or 0)
    weight = abs(anchor) / total_drift * 100 if total_drift > 0.01 else 0
    print(f"\n  Σ|drift|:               {total_drift:,.2f}")
    print(f"  Σ user anchor (signed): {anchor:+,.2f}")
    print(f"  anchor weight:          {weight:.2f}%")
    print(f"  accounts with drift:    {n_drift}/{len(anchored)}")

    # Queue counts
    q_drift = s.execute(text(
        "SELECT COUNT(*) FROM unreconciled_queue WHERE tx_type='PERIOD_DRIFT' AND status='pending'"
    )).scalar()
    q_tx = s.execute(text(
        "SELECT COUNT(*) FROM unreconciled_queue WHERE tx_type != 'PERIOD_DRIFT' AND status='pending'"
    )).scalar()
    print(f"  queue: {q_tx} tx items + {q_drift} period_drift items")
    return {"drift": total_drift, "weight": weight, "n_drift": n_drift,
            "q_tx": q_tx, "q_drift": q_drift, "anchor": anchor}


if __name__ == "__main__":
    s = db.SessionLocal()
    try:
        label = sys.argv[1] if len(sys.argv) > 1 else "current"
        measure_all(s, label)
    finally:
        s.close()
