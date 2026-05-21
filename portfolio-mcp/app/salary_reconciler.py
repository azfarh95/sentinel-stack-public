"""Salary reconciler — cross-pipeline integrity for payslip ↔ POSB salary inflows.

Problem this fixes:
    - PayslipRegistry parser posts a full split journal (Dr POSB net + Dr CPF + Cr 4110 gross).
    - POSB cutover sees the same POSB salary inflow in the statement PDF and posts an
      independent journal (Dr POSB net + Cr 1190 suspense).
    - Net effect: POSB inflated by net_pay × N payslips, and an equal amount sits
      in suspense. ~$20k corruption as of 2026-05-14.

What this module does:
    --scan     : Report duplicates, orphans, missing payslips. Read-only.
    --fix-dups : Void the POSB cutover-side journal where a PAYSLIP journal covers it.
                 Logs every action to salary_reconcile_log.
    --report   : Emit /data/missing_payslips.csv — months where POSB shows a salary
                 inflow but no payslip has been parsed yet (chase-list for user).
    --status   : Summary of current reconcile state.

Matching rule (date + amount window):
    posb.journal_date  BETWEEN  payslip.payment_date - 5d  AND  payslip.payment_date + 7d
    abs(posb_debit - payslip.net_pay) < 0.50
"""
from __future__ import annotations
import argparse
import csv
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from sqlalchemy import text, select, and_
from app import database as db, ledger


WINDOW_BEFORE = 5
WINDOW_AFTER = 7
AMOUNT_TOL = 0.50


def _ensure_log_table(s):
    """Create salary_reconcile_log if missing (covers fresh installs)."""
    s.execute(text("""
      CREATE TABLE IF NOT EXISTS salary_reconcile_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        status VARCHAR NOT NULL,
        payslip_id INTEGER,
        payslip_journal_id INTEGER,
        posb_journal_id INTEGER,
        voided_journal_id INTEGER,
        period_end DATE,
        amount FLOAT,
        employer_guess VARCHAR,
        notes VARCHAR,
        created_at DATETIME NOT NULL
      )
    """))
    s.execute(text("CREATE INDEX IF NOT EXISTS ix_salrecon_status_period ON salary_reconcile_log(status, period_end)"))
    s.commit()


def _posb_salary_suspense_rows(s):
    """Every POSB_PDF_DIRECT journal with a 1190/4900 leg AND narration matching salary."""
    return s.execute(text("""
        SELECT
          j.id, j.journal_date, j.narration,
          gl.debit as posb_debit, gl.credit as posb_credit,
          gl2.account_id as contra_aid, coa2.account_code as contra_coa
        FROM journals j
        JOIN general_ledger gl ON gl.journal_id = j.id
        JOIN chart_of_accounts coa ON coa.id = gl.account_id
        JOIN general_ledger gl2 ON gl2.journal_id = j.id AND gl2.id != gl.id
        JOIN chart_of_accounts coa2 ON coa2.id = gl2.account_id
        WHERE j.source_doc = 'POSB_PDF_DIRECT'
          AND j.status = 'posted'
          AND coa.account_code = '1111'
          AND coa2.account_code IN ('1190', '4900')
          AND UPPER(j.narration) LIKE '%SALARY%'
        ORDER BY j.journal_date
    """)).all()


def _payslip_journals(s):
    """Every PAYSLIP-sourced journal, with the linked payslip_registry row."""
    return s.execute(text("""
        SELECT
          pr.id as payslip_id, pr.employer, pr.employer_key,
          pr.period_end, pr.payment_date, pr.net_pay, pr.gross_pay,
          pr.journal_id, j.journal_date, j.status
        FROM payslip_registry pr
        LEFT JOIN journals j ON j.id = pr.journal_id
        WHERE pr.journal_id IS NOT NULL
          AND (j.status IS NULL OR j.status = 'posted')
        ORDER BY pr.payment_date
    """)).all()


def _find_posb_match(posb_rows, payment_date, net_pay):
    """Return the FIRST POSB row matching this payment within the window."""
    if not payment_date:
        return None
    window_start = payment_date - timedelta(days=WINDOW_BEFORE)
    window_end = payment_date + timedelta(days=WINDOW_AFTER)
    for r in posb_rows:
        jdate = r[1]
        if isinstance(jdate, str):
            jdate = datetime.fromisoformat(jdate).date()
        if not (window_start <= jdate <= window_end):
            continue
        amt = float(r[3] or 0)
        if abs(amt - float(net_pay or 0)) <= AMOUNT_TOL:
            return r
    return None


_EMPLOYER_HINTS = [
    (r"AZ UNITED", "AZ United Pte Ltd"),
    (r"AZ\b", "AZ United Pte Ltd"),
    (r"HENDERSON", "YourAgency Security"),
    (r"GANESAN", "Ganesan"),
    (r"SAF\b|SINGAPORE ARMED", "SAF (reimbursement)"),
]


def _guess_employer(narration: str) -> str | None:
    n = (narration or "").upper()
    for pat, label in _EMPLOYER_HINTS:
        if re.search(pat, n):
            return label
    return None


def scan(s) -> dict:
    """Read-only: classify each payslip + each POSB salary row."""
    payslips = _payslip_journals(s)
    posb_rows = _posb_salary_suspense_rows(s)
    posb_by_id = {r[0]: r for r in posb_rows}
    posb_matched = set()

    dups = []           # (payslip_row, posb_row)
    orphan_payslips = []   # payslip without a POSB salary match
    for p in payslips:
        net = float(p[5] or 0)
        pd = p[4]
        if isinstance(pd, str): pd = datetime.fromisoformat(pd).date()
        m = _find_posb_match(posb_rows, pd, net)
        if m:
            dups.append((p, m))
            posb_matched.add(m[0])
        else:
            orphan_payslips.append(p)

    missing = [r for r in posb_rows if r[0] not in posb_matched]

    return {
        "payslips_total": len(payslips),
        "posb_salary_total": len(posb_rows),
        "duplicates": dups,
        "orphan_payslips": orphan_payslips,
        "missing_payslips": missing,
    }


def print_scan(result: dict):
    print(f"=== Salary Reconcile — Scan ===\n")
    print(f"  PAYSLIP journals (already posted):         {result['payslips_total']}")
    print(f"  POSB salary suspense candidates:           {result['posb_salary_total']}")
    print(f"  → Duplicates (payslip + POSB both posted): {len(result['duplicates'])}")
    print(f"  → Orphan payslips (no POSB match):         {len(result['orphan_payslips'])}")
    print(f"  → Missing payslips (POSB has no payslip):  {len(result['missing_payslips'])}")
    print()

    if result["duplicates"]:
        print("--- Duplicates (POSB cutover side will be voided by --fix-dups) ---")
        for p, m in result["duplicates"]:
            net = float(p[5] or 0)
            print(f"  payslip jid={p[7]:<5}  emp={p[2]:<12}  payment={p[4]}  net=${net:>9,.2f}"
                  f"  ─dup→  POSB jid={m[0]:<5}  date={m[1]}  amt=${float(m[3] or 0):>9,.2f}")
    if result["missing_payslips"]:
        print("\n--- Missing payslips (POSB salary inflows without a parsed payslip) ---")
        for r in result["missing_payslips"][:25]:
            guess = _guess_employer(r[2]) or "?"
            print(f"  POSB jid={r[0]:<5}  {r[1]}  ${float(r[3] or 0):>9,.2f}  employer_guess={guess}")
        if len(result["missing_payslips"]) > 25:
            print(f"  ... and {len(result['missing_payslips']) - 25} more")
    if result["orphan_payslips"]:
        print("\n--- Orphan payslips (parsed but no POSB inflow in window) ---")
        for p in result["orphan_payslips"]:
            print(f"  payslip_id={p[0]}  {p[2]}  payment={p[4]}  net=${float(p[5] or 0):>9,.2f}")


def fix_dups(s, dry: bool = False) -> int:
    """Void POSB-cutover journals that duplicate a PAYSLIP journal.
    Returns the number of dups fixed."""
    result = scan(s)
    n = 0
    for p, m in result["duplicates"]:
        payslip_jid = p[7]
        posb_jid = m[0]
        # Idempotent — skip if already logged
        existing = s.execute(text("""
            SELECT id FROM salary_reconcile_log
            WHERE status='matched_dup' AND posb_journal_id=:pj
        """), {"pj": posb_jid}).fetchone()
        if existing:
            continue
        if dry:
            n += 1
            continue
        # Void the POSB cutover journal
        s.execute(text("""
            UPDATE journals SET status='voided',
                voided_at = CURRENT_TIMESTAMP,
                voided_reason = :reason
            WHERE id = :jid AND status = 'posted'
        """), {"jid": posb_jid, "reason": f"salary-reconciler: superseded by PAYSLIP jid={payslip_jid}"})
        s.execute(text("""
            INSERT INTO salary_reconcile_log
              (status, payslip_id, payslip_journal_id, posb_journal_id, voided_journal_id,
               period_end, amount, employer_guess, notes, created_at)
            VALUES ('matched_dup', :pid, :pjid, :posb, :posb, :pend, :amt, :emp,
                    :notes, CURRENT_TIMESTAMP)
        """), {
            "pid": p[0], "pjid": payslip_jid, "posb": posb_jid,
            "pend": p[3], "amt": float(p[5] or 0), "emp": p[1],
            "notes": f"POSB jid={posb_jid} voided; PAYSLIP jid={payslip_jid} is authoritative",
        })
        n += 1
    if not dry:
        s.commit()
    return n


def report_missing(s, csv_path: Path | None = None) -> int:
    """Emit chase-list CSV for POSB salary inflows without payslips.
    Returns count."""
    result = scan(s)
    rows_out = []
    for r in result["missing_payslips"]:
        amt = float(r[3] or 0)
        rows_out.append({
            "posb_journal_id": r[0],
            "journal_date": str(r[1])[:10],
            "amount_net": f"{amt:.2f}",
            "employer_guess": _guess_employer(r[2]) or "",
            "narration": (r[2] or "")[:120],
        })
        # Idempotent log row
        existing = s.execute(text("""
            SELECT id FROM salary_reconcile_log
            WHERE status='missing_payslip' AND posb_journal_id=:pj
        """), {"pj": r[0]}).fetchone()
        if not existing:
            s.execute(text("""
                INSERT INTO salary_reconcile_log
                  (status, posb_journal_id, period_end, amount, employer_guess, notes, created_at)
                VALUES ('missing_payslip', :pj, :pe, :amt, :emp, :notes, CURRENT_TIMESTAMP)
            """), {
                "pj": r[0],
                "pe": str(r[1])[:10],
                "amt": amt,
                "emp": _guess_employer(r[2]),
                "notes": "Need payslip PDF to post gross+CPF split journal",
            })
    s.commit()
    if csv_path:
        csv_path = Path(csv_path)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["posb_journal_id", "journal_date",
                                                "amount_net", "employer_guess", "narration"])
            w.writeheader()
            for row in rows_out:
                w.writerow(row)
        print(f"Wrote {len(rows_out)} missing-payslip rows to {csv_path}")
    return len(rows_out)


def status(s):
    """Show current reconcile state from the log table."""
    rows = s.execute(text("""
        SELECT status, COUNT(*), SUM(amount), MIN(period_end), MAX(period_end)
        FROM salary_reconcile_log
        GROUP BY status ORDER BY status
    """)).all()
    print("\n=== salary_reconcile_log ===\n")
    if not rows:
        print("  (empty — run --fix-dups and --report first)")
        return
    for r in rows:
        amt = float(r[2] or 0)
        print(f"  {r[0]:<18}  {r[1]:>3} rows  ${amt:>10,.2f}  {r[3]}..{r[4]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="Read-only diagnostic")
    ap.add_argument("--fix-dups", action="store_true", help="Void duplicate POSB cutover journals")
    ap.add_argument("--report", action="store_true", help="Emit missing-payslip chase-list CSV")
    ap.add_argument("--status", action="store_true", help="Show current reconcile state")
    ap.add_argument("--csv", default="/data/missing_payslips.csv",
                    help="Output path for --report")
    ap.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = ap.parse_args()

    db.init_db()
    s = db.SessionLocal()
    _ensure_log_table(s)
    try:
        if args.scan:
            print_scan(scan(s))
        if args.fix_dups:
            n = fix_dups(s, dry=args.dry_run)
            verb = "would void" if args.dry_run else "voided"
            print(f"\n[fix-dups] {verb} {n} duplicate POSB cutover journals")
        if args.report:
            report_missing(s, csv_path=Path(args.csv))
        if args.status:
            status(s)
        if not any([args.scan, args.fix_dups, args.report, args.status]):
            print_scan(scan(s))   # default
    finally:
        s.close()


if __name__ == "__main__":
    main()
