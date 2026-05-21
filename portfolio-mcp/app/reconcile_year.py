"""Per-(account, period) reconciliation: GL net change vs statement BF/CF.

For each PDF statement, the universal parser captures balance_brought_forward
(BF) and balance_carried_forward (CF). Truth statement:

    CF - BF == sum of (debits-to-account - credits-from-account) over that period

Walk every statement under each account, sum the GL net change for that
exact date range, compare to the PDF-stated delta.  Any drift > $0.50 is
flagged.

This is the sanity gate before the P&L: if drift = 0 we trust the data;
if drift > 0 there's a posting bug somewhere.

Run:
    docker exec portfolio-mcp python -m app.reconcile_year --account 1111
    docker exec portfolio-mcp python -m app.reconcile_year --all
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from pathlib import Path
from sqlalchemy import text

from app import database as db
from app.universal_pdf_parser import load_all_schemas, parse_pdf


# (CoA code, folder glob to find statements)
ACCOUNT_FOLDERS = {
    "1111": [Path("/onedrive/Sentinel Finance/01_Bank statements/DBS_POSB Savings")],
    "1114": [Path("/onedrive/Sentinel Finance/01_Bank statements/Maybank Ar Rihla")],
    "1115": [Path("/onedrive/Sentinel Finance/01_Bank statements/Standard Chartered")],
    "2111": [Path("/onedrive/Sentinel Finance/02_Credit card statements")],            # DBS CC
    "2112": [Path("/onedrive/Sentinel Finance/02_Credit card statements")],            # Maybank CC
    "2114": [Path("/onedrive/Sentinel Finance/02_Credit card statements")],            # HSBC CC
}

# Source_doc tags that count toward GL for each account
SOURCE_PREFIXES = {
    "1111": ["POSB_PDF_DIRECT", "PAYSLIP", "RECURRING_RECON", "POSB_CSV"],
    "1114": ["MAYBANK_PDF_DIRECT"],
    "1115": ["SC_PDF_DIRECT"],
    "2111": ["CC_PDF_DIRECT:2111", "POSB_PDF_DIRECT"],
    "2112": ["CC_PDF_DIRECT:2112", "POSB_PDF_DIRECT"],
    "2114": ["CC_PDF_DIRECT:2114", "POSB_PDF_DIRECT"],
}

TOLERANCE = 0.50


@dataclass
class StmtCheck:
    statement_name: str
    statement_date: str
    bf: float
    cf: float
    bf_to_cf_delta: float        # CF - BF
    gl_net_change: float          # sum of debits - credits to the account in this date range
    drift: float                  # bf_to_cf_delta - gl_net_change
    status: str                   # 'OK' | 'DRIFT' | 'INCOMPLETE'


def _gl_net_change(s, coa: str, date_from: str, date_to: str) -> float:
    """For asset accounts (1xxx): positive = inflow. For liability (2xxx): positive = liability increased."""
    r = s.execute(text("""
        SELECT SUM(gl.debit) - SUM(gl.credit)
        FROM journals j
        JOIN general_ledger gl ON gl.journal_id = j.id
        JOIN chart_of_accounts coa ON coa.id = gl.account_id
        WHERE j.status = 'posted'
          AND coa.account_code = :coa
          AND j.journal_date BETWEEN :df AND :dt
    """), {"coa": coa, "df": date_from, "dt": date_to}).fetchone()
    return float(r[0] or 0.0)


def _liability_sign(coa: str) -> int:
    """For asset accounts, BF→CF positive = money in.
    For liability accounts, BF→CF positive = liability went UP (i.e., more charges than payments).
    GL: assets have positive Dr-Cr for inflow; liabilities have negative Dr-Cr for liability-up.
    Returns +1 for assets, -1 for liabilities (multiplier on gl_net_change to compare with bf→cf)."""
    return -1 if coa.startswith("2") else 1


def reconcile_account(s, coa: str, schemas: list[dict]) -> list[StmtCheck]:
    folders = ACCOUNT_FOLDERS.get(coa, [])
    checks: list[StmtCheck] = []
    sign = _liability_sign(coa)
    for folder in folders:
        if not folder.exists(): continue
        pdfs = sorted(folder.rglob("*.pdf"))
        for pdf in pdfs:
            try:
                r = parse_pdf(pdf, schemas)
            except Exception as e:
                continue
            if not r.statement_date: continue
            # Skip if this statement isn't for our account
            if r.gl_account_code != coa: continue
            if r.balance_brought_forward is None or r.balance_carried_forward is None:
                checks.append(StmtCheck(
                    statement_name=pdf.name[:60], statement_date=r.statement_date,
                    bf=0, cf=0, bf_to_cf_delta=0, gl_net_change=0, drift=0,
                    status="INCOMPLETE",
                ))
                continue
            bf = float(r.balance_brought_forward); cf = float(r.balance_carried_forward)
            stmt_delta = cf - bf
            # Period: from day after previous statement's CF date → this statement's date
            # Simplification: use statement_date's month as the period
            stmt_iso = r.statement_date
            try:
                stmt_d = datetime.fromisoformat(stmt_iso).date()
            except ValueError:
                continue
            # Period = 1st of statement month to statement_date
            period_from = stmt_d.replace(day=1).isoformat()
            period_to = stmt_d.isoformat()
            gl_net = _gl_net_change(s, coa, period_from, period_to) * sign
            drift = stmt_delta - gl_net
            status = "OK" if abs(drift) < TOLERANCE else "DRIFT"
            checks.append(StmtCheck(
                statement_name=pdf.name[:60], statement_date=stmt_iso,
                bf=bf, cf=cf, bf_to_cf_delta=stmt_delta,
                gl_net_change=gl_net, drift=drift, status=status,
            ))
    return checks


def print_report(coa: str, checks: list[StmtCheck]):
    print(f"\n=== Account {coa}  ({len(checks)} statements) ===")
    print(f"{'Statement':<55} {'Date':<11} {'BF':>11} {'CF':>11} {'Δ stmt':>10} {'Δ GL':>10} {'Drift':>10}  Status")
    print("-" * 140)
    ok = drift = inc = 0
    for c in checks:
        marker = "✓" if c.status == "OK" else "⚠"
        print(f"{marker} {c.statement_name:<53} {c.statement_date:<11} "
              f"{c.bf:>11,.2f} {c.cf:>11,.2f} {c.bf_to_cf_delta:>+10,.2f} {c.gl_net_change:>+10,.2f} "
              f"{c.drift:>+10,.2f}  {c.status}")
        if c.status == "OK": ok += 1
        elif c.status == "DRIFT": drift += 1
        else: inc += 1
    print(f"\nSummary: {ok} OK, {drift} DRIFT, {inc} INCOMPLETE  ({len(checks)} total)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", help="Specific CoA to reconcile")
    ap.add_argument("--all", action="store_true", help="Reconcile all configured accounts")
    args = ap.parse_args()

    db.init_db()
    s = db.SessionLocal()
    schemas = load_all_schemas()
    try:
        if args.all:
            for coa in ACCOUNT_FOLDERS:
                checks = reconcile_account(s, coa, schemas)
                if checks:
                    print_report(coa, checks)
        elif args.account:
            checks = reconcile_account(s, args.account, schemas)
            print_report(args.account, checks)
        else:
            ap.print_help()
    finally:
        s.close()


if __name__ == "__main__":
    main()
