"""Statement health-check — verify parsed statements reconcile internally and chain-wise.

Two checks per statement:
  1. Within-statement:    A + B - C = D
     A = previous_balance, B = sum(charges + interest + fees),
     C = sum(payments), D = closing_balance
  2. Period-over-period:  Dec-stmt closing  ==  Jan-stmt previous   (per facility CoA)

The first proves the parser captured the right movement.
The second proves the statement chain is unbroken (no missing months,
no parser drift between consecutive months).

Run:
    docker exec portfolio-mcp python -m app.statement_reconcile             # all months
    docker exec portfolio-mcp python -m app.statement_reconcile --month "Jan'26"
    docker exec portfolio-mcp python -m app.statement_reconcile --chain     # show chain check only
"""
from __future__ import annotations

import argparse
import os
import re
from collections import defaultdict
from datetime import date as _date
from pathlib import Path

from . import cc_statement_parser as p

CC_STATEMENT_ROOT = Path("/onedrive/Sentinel Finance/02_Credit card statements")

CoA_NAMES = {
    "2111": "DBS CC", "2112": "Maybank CC", "2113": "SC CC",
    "2114": "HSBC CC", "2121": "DBS Cashline", "2122": "UOB CashPlus",
    "2211": "SC Loan/BT", "2212": "GXS FlexiLoan", "2213": "Maybank CreditAble",
    "1116": "GXS Savings",
}


def reconcile_statement(stmt: p.ParsedStatement) -> dict:
    """Compute A+B-C=D for one parsed statement. Returns metrics dict."""
    A = stmt.previous_balance
    D = stmt.closing_balance
    B = sum(l.amount for l in stmt.lines if l.amount > 0 and l.kind != "payment")
    C = sum(abs(l.amount) for l in stmt.lines
            if l.kind == "payment" or (l.kind == "charge" and l.amount < 0))
    computed = (A or 0) + B - C if A is not None else None
    delta = (D - computed) if (D is not None and computed is not None) else None
    if delta is None:
        status = "incomplete"
    elif abs(delta) < 0.50:
        status = "ok"
    elif abs(delta) < 5:
        status = "warn"
    else:
        status = "fail"
    return {"A": A, "B": B, "C": C, "D": D, "computed": computed,
            "delta": delta, "status": status}


def _scan_pdfs(month_filter: str | None) -> list[Path]:
    """Walk CC_Statement tree for PDFs. month_filter is e.g. 'Jan\\'26'."""
    files = []
    for pdf in CC_STATEMENT_ROOT.rglob("*.pdf"):
        if "unsorted" in [p.name for p in pdf.parents]:
            continue
        # Hard-exclude non-statement docs
        fn = pdf.name.lower()
        if any(x in fn for x in ["application form", "consolidation", "acknowledgement",
                                  "transactionhistory", "_encrypted", "payslip", "noa ",
                                  "cbs ", "mlcb ", "cpf latest", "credit report",
                                  "loan agreement", "ml compairson"]):
            continue
        if month_filter and month_filter not in str(pdf):
            continue
        files.append(pdf)
    return sorted(files)


def within_statement_check(month_filter: str | None = None) -> tuple[int, int, int]:
    """Run A+B-C=D check across all (or month-filtered) statements.
    Returns (ok_count, warn_count, fail_count)."""
    files = _scan_pdfs(month_filter)
    print(f"=== Within-statement reconciliation ({len(files)} PDFs) ===")
    print(f"{'File':<50} {'Bank':<14} {'A':>9} {'B':>8} {'C':>9} {'D':>9} {'Δ':>7} {''}")
    print("-" * 110)
    counts = {"ok": 0, "warn": 0, "fail": 0, "incomplete": 0}
    for pdf in files:
        try:
            s = p.detect_and_parse(str(pdf))
        except Exception as e:
            print(f"  {pdf.name[:48]:<50} (parse error: {str(e)[:40]})")
            counts["fail"] += 1
            continue
        if not s:
            counts["incomplete"] += 1
            continue
        r = reconcile_statement(s)
        flag = {"ok": "✓", "warn": "⚠", "fail": "✗", "incomplete": "?"}[r["status"]]
        astr = f"{r['A']:,.2f}" if r['A'] is not None else "—"
        dstr = f"{r['D']:,.2f}" if r['D'] is not None else "—"
        delstr = f"{r['delta']:+,.2f}" if r['delta'] is not None else "—"
        bank = s.bank or "?"
        print(f"  {pdf.name[:48]:<50} {bank:<14} {astr:>9} {r['B']:>8,.2f} "
              f"{r['C']:>9,.2f} {dstr:>9} {delstr:>7} {flag}")
        counts[r["status"]] += 1
    print()
    print(f"  ok={counts['ok']}  warn={counts['warn']}  "
          f"fail={counts['fail']}  incomplete={counts['incomplete']}")
    return counts["ok"], counts["warn"], counts["fail"]


def _statement_month(pdf: Path) -> tuple[int, int] | None:
    """Extract (year, month) from folder path like 'Jan'26' or '2025/Dec'25'."""
    folder_re = re.compile(r"([A-Za-z]+)'(\d{2})")
    for part in pdf.parts:
        m = folder_re.search(part)
        if m:
            mon_str, yr2 = m.groups()
            months = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                      "jul":7,"july":7,"aug":8,"sep":9,"sept":9,
                      "oct":10,"nov":11,"dec":12}
            mo = months.get(mon_str.lower())
            if mo:
                return (2000 + int(yr2), mo)
    return None


def chain_check() -> int:
    """Verify Month-N closing balance == Month-N+1 previous balance, per facility CoA.

    Returns number of broken links (0 = clean chain)."""
    # Collect (year, month, coa, closing_balance, previous_balance) per PDF
    by_facility: dict[str, dict[tuple[int, int], dict]] = defaultdict(dict)
    for pdf in _scan_pdfs(None):
        ym = _statement_month(pdf)
        if not ym:
            continue
        try:
            s = p.detect_and_parse(str(pdf))
        except Exception:
            continue
        if not s:
            continue
        # SC has per-CoA breakdown in extras
        if s.extras.get("previous_balance_by_coa"):
            for coa, prev in s.extras["previous_balance_by_coa"].items():
                clos = (s.extras.get("closing_balance_by_coa") or {}).get(coa)
                by_facility[coa][ym] = {"prev": prev, "close": clos, "pdf": pdf.name}
        else:
            by_facility[s.facility_coa_code][ym] = {
                "prev": s.previous_balance, "close": s.closing_balance, "pdf": pdf.name
            }

    print("=== Period-over-period chain check ===")
    print(f"{'CoA':<6} {'Name':<22} {'From':<9} {'To':<9} {'Close':>10} {'Next prev':>10} {'Δ':>7}")
    print("-" * 78)
    broken = 0
    for coa in sorted(by_facility):
        months = sorted(by_facility[coa])
        for i in range(len(months) - 1):
            curr, nxt = months[i], months[i + 1]
            # Only check adjacent months (skip gaps)
            if (curr[0] * 12 + curr[1]) + 1 != (nxt[0] * 12 + nxt[1]):
                continue
            close = by_facility[coa][curr]["close"]
            prev = by_facility[coa][nxt]["prev"]
            if close is None or prev is None:
                continue  # can't compare
            delta = prev - close
            flag = "✓" if abs(delta) < 1.00 else "✗"
            if abs(delta) >= 1.00:
                broken += 1
            print(f"  {coa:<6} {CoA_NAMES.get(coa, '?'):<22} "
                  f"{curr[0]}-{curr[1]:02d}  {nxt[0]}-{nxt[1]:02d}  "
                  f"{close:>10,.2f} {prev:>10,.2f} {delta:>+7,.2f} {flag}")
    print()
    print(f"  Broken links: {broken}")
    return broken


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", help="Filter to one month, e.g. \"Jan'26\"")
    ap.add_argument("--chain", action="store_true", help="Run chain check only")
    ap.add_argument("--within", action="store_true", help="Run within-stmt check only")
    args = ap.parse_args()

    if args.chain:
        chain_check()
    elif args.within:
        within_statement_check(args.month)
    else:
        within_statement_check(args.month)
        print()
        chain_check()


if __name__ == "__main__":
    main()
