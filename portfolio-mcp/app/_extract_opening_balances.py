"""Scan every PDF statement we have, find the EARLIEST one per account, extract
the Balance Brought Forward (BF). That value, anchored at 2024-01-01, becomes
the opening balance for the GL.

For statements where the earliest available is mid-2024 or later, we still use
the BF as if it were 2024-01-01 — the gap (Jan to that statement's start) is
unaccounted activity that ends up in Retained Earnings.
"""
from app.universal_pdf_parser import load_all_schemas, parse_pdf
from pathlib import Path
from collections import defaultdict

ROOTS = [
    Path("/onedrive/Sentinel Finance/01_Bank statements"),
    Path("/onedrive/Sentinel Finance/02_Credit card statements"),
    Path("/onedrive/Sentinel Finance/03_Credit facilities"),
    Path("/onedrive/Sentinel Finance/04_Loan agreements"),
]

# CoA mapping by schema name
SCHEMA_TO_COA = {
    "posb-savings":     "1111",
    "maybank-savings":  "1114",
    "sc-savings":       "1115",
    "dbs-cc":           "2111",
    "maybank-cc":       "2112",
    "sc-creditable":    "2213",  # SC CreditAble
    "hsbc-cc":          "2114",
    "dbs-cashline":     "2121",
    "uob-cashplus":     "2122",
    "gxs-flexiloan":    "2212",
    "singlife-ilp":     "12229",
}

schemas = load_all_schemas()

# Collect all PDFs with their parsed extract
candidates = defaultdict(list)   # schema_name → [(date, bf, path), ...]
for root in ROOTS:
    if not root.exists(): continue
    for pdf in root.rglob("*.pdf"):
        try:
            r = parse_pdf(pdf, schemas)
        except Exception:
            continue
        if not r.statement_date or r.balance_brought_forward is None:
            continue
        sch = (r.schema_name or "").lower()
        bf = float(r.balance_brought_forward or 0)
        candidates[sch].append((r.statement_date, bf, pdf.name))

print("=== Earliest available statement per schema ===")
print(f"{'Schema':<22} {'CoA':<7} {'Earliest date':<14} {'BF':>11}  PDF")
print("-" * 100)
for sch, items in sorted(candidates.items()):
    if not items: continue
    items.sort()   # earliest first
    earliest_date, earliest_bf, earliest_pdf = items[0]
    coa = SCHEMA_TO_COA.get(sch, "?")
    print(f"  {sch:<20} {coa:<7} {earliest_date:<14} ${earliest_bf:>10,.2f}  {earliest_pdf[:60]}")
