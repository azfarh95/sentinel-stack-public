"""Audit CC statement completeness — what we have, what's missing, per CC × month."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from pathlib import Path


CC_STATEMENT_ROOT = Path("/onedrive/Sentinel Finance/02_Credit card statements")

# Active CCs that should have monthly statements
CC_FACILITIES = ["DBS CC", "DBS CL", "HSBC", "Maybank CC", "Maybank CA", "SC", "UOB", "GXS"]

# Filename → CC mapping (lowercase substring match)
FILENAME_PATTERNS = {
    "dbs cc": "DBS CC", "dbs credit card": "DBS CC",
    "credit cards consolidated": "DBS CC", "credit cards statement": "DBS CC",
    "dbs cl": "DBS CL", "dbs cashline": "DBS CL", "cashline statement": "DBS CL",
    "hsbc": "HSBC",
    "maybank cc": "Maybank CC", "platinum visa": "Maybank CC",
    "maybank ca": "Maybank CA", "creditable": "Maybank CA", "maybank creditable": "Maybank CA",
    "sc": "SC", "standard chartered": "SC",
    "uob": "UOB",
    "gxs": "GXS",
    "_temp_": "HSBC",
}

EXCLUDE_PATTERNS = ["payslip", "noa", "credit report", "cbs", "mlcb", "cpf",
                    "dc acknowledgement", "dc application", "dc form",
                    "dcp", "consolidation app", "application form",
                    "transactionhistory", "loan agreement", "_encrypted",
                    "15 months", "ml compairson"]
# NOTE: "personal loan" intentionally NOT excluded — UOB Personal Loan
# statements are valid (part of UOB CashPlus facility tracking).


def detect_cc(filename: str) -> str | None:
    fn = filename.lower()
    if any(x in fn for x in EXCLUDE_PATTERNS):
        return None
    # Order matters: more-specific patterns first
    order = ["dbs cc", "dbs credit card", "credit cards consolidated", "credit cards statement",
             "dbs cl", "dbs cashline", "cashline statement",
             "hsbc", "platinum visa", "maybank cc", "maybank creditable",
             "creditable", "maybank ca", "standard chartered", "sc",
             "uob", "gxs", "_temp_"]
    for key in order:
        if key in fn:
            return FILENAME_PATTERNS[key]
    return None


def detect_month(path: Path) -> tuple[int, int] | None:
    """Try to extract (year, month) from path/filename."""
    # Folder name pattern: 'Jan'25', 'Dec'24', 'Apr'26', etc.
    folder_re = re.compile(r"([A-Za-z]+)'(\d{2})")
    for part in path.parts:
        m = folder_re.search(part)
        if m:
            mon_str, yr2 = m.groups()
            months = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                      "jul":7,"july":7,"aug":8,"sep":9,"sept":9,
                      "oct":10,"nov":11,"dec":12}
            mo = months.get(mon_str.lower())
            if mo:
                return (2000 + int(yr2), mo)
    # Filename patterns: '0125' = Jan'25, '04.25', '15042025', etc.
    fn = path.name.lower()
    m = re.search(r"(\d{2})(\d{2})\.pdf", fn)  # e.g. "0425"
    if m:
        mo, yr = int(m.group(1)), 2000 + int(m.group(2))
        if 1 <= mo <= 12 and 2020 <= yr <= 2030:
            return (yr, mo)
    m = re.search(r"_(\d{4})_(\d{2})_", fn)  # _2025_08_
    if m:
        return (int(m.group(1)), int(m.group(2)))
    # "Apr2025", "Sep2025" etc. (DBS new export naming)
    months = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
              "jul":7,"aug":8,"sep":9,"sept":9,"oct":10,"nov":11,"dec":12}
    m = re.search(r"([a-z]{3,4})(\d{4})", fn)
    if m:
        mo_str, yr = m.group(1)[:3], int(m.group(2))
        if mo_str in months and 2020 <= yr <= 2030:
            return (yr, months[mo_str])
    return None


def main():
    have = defaultdict(lambda: defaultdict(list))  # have[(year,month)][cc] = [paths]
    for pdf in CC_STATEMENT_ROOT.rglob("*.pdf"):
        cc = detect_cc(pdf.name)
        if not cc:
            continue
        ym = detect_month(pdf)
        if not ym:
            continue
        have[ym][cc].append(pdf.name)

    # Year-by-year completeness table
    for year in (2024, 2025, 2026):
        months = list(range(1, 13))
        if year == 2024:
            months = [11, 12]
        if year == 2026:
            months = list(range(1, date.today().month + 1))
        print()
        print(f"=== {year} completeness ===")
        header = f"{'Month':<6}  " + "  ".join(f"{c:<10}" for c in CC_FACILITIES)
        print(header)
        print("-" * len(header))
        for mo in months:
            row = [f"{year}-{mo:02d}"]
            for cc in CC_FACILITIES:
                paths = have.get((year, mo), {}).get(cc, [])
                if paths:
                    row.append("✓" + (f" ({len(paths)})" if len(paths) > 1 else ""))
                else:
                    row.append("—")
            print(f"  {row[0]:<6}  " + "  ".join(f"{c:<10}" for c in row[1:]))

    # Gap summary
    total_expected = 0
    total_have = 0
    for year in (2024, 2025, 2026):
        months = list(range(1, 13))
        if year == 2024:
            months = [11, 12]
        if year == 2026:
            months = list(range(1, date.today().month + 1))
        for mo in months:
            for cc in CC_FACILITIES:
                total_expected += 1
                if have.get((year, mo), {}).get(cc):
                    total_have += 1
    print()
    print(f"Coverage: {total_have}/{total_expected} statements ({100*total_have/total_expected:.0f}%)")
    print(f"Missing: {total_expected - total_have}")


if __name__ == "__main__":
    main()
