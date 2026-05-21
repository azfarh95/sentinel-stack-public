"""Simulation harness: void → replay → reconcile → measure.
One-shot diagnostic script, not part of the app.
"""
import sys
from datetime import date
from app import database as db, journal_service as js
from sqlalchemy import text


def reset_posb_april(s):
    """Void all POSB-related journals dated >= 2026-04-01 EXCEPT opening
    anchors. Preserves Gates 1, 3 state."""
    rows = s.execute(text("""
      SELECT DISTINCT j.id FROM journals j
      JOIN general_ledger gl ON gl.journal_id = j.id
      WHERE j.journal_date >= '2026-04-01'
        AND j.status = 'posted'
        AND j.journal_type != 'opening'
        AND gl.account_id = (SELECT id FROM chart_of_accounts WHERE account_code='1111')
    """)).fetchall()
    ids = [r[0] for r in rows]
    for jid in ids:
        s.execute(text("""
          UPDATE journals SET status='voided', voided_at=CURRENT_TIMESTAMP,
            voided_reason='sim_loop reset' WHERE id=:i
        """), {"i": jid})
    # Drop the Apr 2026 period_drift queue entry (so reconcile can re-create fresh)
    s.execute(text("""
      DELETE FROM unreconciled_queue
      WHERE tx_type='PERIOD_DRIFT' AND source_ref='1111:2026-04-30'
    """))
    s.commit()
    return len(ids)


def gl_at(s, account_code, as_of):
    row = s.execute(text("""
      SELECT COALESCE(SUM(CASE WHEN j.status='posted' THEN gl.debit_sgd ELSE 0 END),0)
           - COALESCE(SUM(CASE WHEN j.status='posted' THEN gl.credit_sgd ELSE 0 END),0)
      FROM general_ledger gl JOIN journals j ON j.id=gl.journal_id
      WHERE gl.account_id=(SELECT id FROM chart_of_accounts WHERE account_code=:c)
        AND j.journal_date <= :d
    """), {"c": account_code, "d": as_of}).fetchone()
    return float(row[0] or 0)


def measure(s, sim_label):
    target = 1510.62
    cf_row = s.execute(text("""
      SELECT balance_carried_forward, balance_brought_forward FROM bank_statement_registry
      WHERE account_code='1111' AND period_end='2026-04-30'
    """)).fetchone()
    cf = float(cf_row[0]) if cf_row else target
    bf = float(cf_row[1]) if cf_row else 0
    gl_apr_end = gl_at(s, "1111", date(2026, 4, 30))
    gl_mar_end = gl_at(s, "1111", date(2026, 3, 31))

    # Anchor contribution (USER_ANCHOR posts)
    anchor = float(s.execute(text("""
      SELECT COALESCE(SUM(CASE WHEN j.status='posted' THEN gl.debit_sgd-gl.credit_sgd ELSE 0 END),0)
      FROM general_ledger gl JOIN journals j ON j.id=gl.journal_id
      WHERE gl.account_id=(SELECT id FROM chart_of_accounts WHERE account_code='1111')
        AND j.source_doc='USER_ANCHOR'
    """)).scalar() or 0)

    drift = round(cf - gl_apr_end, 2)
    weight = abs(anchor / drift) * 100 if abs(drift) > 0.01 else 0
    print(f"\n══ {sim_label} ══")
    print(f"  Apr'26  BF={bf:,.2f}  CF={cf:,.2f}  (target)")
    print(f"  GL @ Mar 31 (before Apr replay): {gl_mar_end:,.2f}")
    print(f"  GL @ Apr 30 (after Apr replay):  {gl_apr_end:,.2f}")
    print(f"  drift (CF - GL):                 {drift:+,.2f}")
    print(f"  anchor contribution:              {anchor:+,.2f}")
    print(f"  anchor weight (% of drift):       {weight:.1f}%")
    return drift


if __name__ == "__main__":
    s = db.SessionLocal()
    try:
        label = sys.argv[1] if len(sys.argv) > 1 else "current"
        if len(sys.argv) > 2 and sys.argv[2] == "reset":
            n = reset_posb_april(s)
            print(f"reset: voided {n} Apr journals")
        measure(s, label)
    finally:
        s.close()
