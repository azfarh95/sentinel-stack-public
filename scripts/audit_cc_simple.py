"""Simple per-month PDF count with filenames listed.
Excludes obvious non-statements; user judges completeness."""
import io, re, sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(r"C:\Users\azfar\OneDrive\CC_Statement")

# Things definitely NOT a monthly statement
EXCLUDE = re.compile(
    r"(credit\s+report|MLCB|CBS|CPF|loan\s+agreement|reinstatement|"
    r"payslip|payment|suspension\s+form|policy.*change|funds?\s+transfer|"
    r"^NOA\b|TransactionHistory|SGH-PCI|_Temp_2025_)",  # _Temp_2025 = these had different content
    re.I,
)

month_re = re.compile(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'(\d{2})")

months = []
for d in ROOT.rglob("*"):
    if d.is_dir() and month_re.fullmatch(d.name):
        months.append(d)

def sort_key(p):
    m = month_re.fullmatch(p.name)
    mon_idx = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"].index(m.group(1)) + 1
    return (2000 + int(m.group(2)), mon_idx)

months.sort(key=sort_key)

print(f"=== {len(months)} month folders found ===\n")

for folder in months:
    rel = folder.relative_to(ROOT)
    statements = []
    excluded = []
    for f in folder.iterdir():
        if not f.is_file() or not f.suffix.lower() == ".pdf":
            continue
        if EXCLUDE.search(f.name):
            excluded.append(f.name)
        else:
            statements.append(f.name)

    flag = "  ⚠ INCOMPLETE" if len(statements) < 7 else ""
    print(f"{str(rel):<14} {len(statements)} statements{flag}")
    for s in sorted(statements):
        print(f"    {s}")
    if excluded:
        print(f"    (excluded as non-statement: {len(excluded)} — {', '.join(excluded[:3])}{'...' if len(excluded) > 3 else ''})")
    print()

# Summary
print("=" * 60)
print("INCOMPLETE MONTHS (< 7 statements):")
for folder in months:
    rel = folder.relative_to(ROOT)
    n = sum(1 for f in folder.iterdir() if f.is_file() and f.suffix.lower() == ".pdf" and not EXCLUDE.search(f.name))
    if n < 7:
        print(f"  {rel} → {n}")
