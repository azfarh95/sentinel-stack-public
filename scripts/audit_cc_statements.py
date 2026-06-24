"""Scan OneDrive/CC_Statement/ for monthly completeness.
Expected per month: ~8-9 card statements.
Cards: UOB, Maybank CA, Maybank Platinum Visa, SC (Simply Cash + Balance Transfer),
       DBS Cashline, DBS CC, HSBC.
"""
import io
import re
import sys
from pathlib import Path
from collections import defaultdict, Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(r"C:\Users\azfar\OneDrive\CC_Statement")

# Card identification — match filename to a card slot.
# Order matters: more-specific patterns first.
CARD_PATTERNS = [
    ("UOB CashPlus",          re.compile(r"\bUOB\b", re.I)),
    ("Maybank CC",            re.compile(r"Maybank\s+(?:Credit\s*Card|CC)\b", re.I)),
    ("Maybank Platinum Visa", re.compile(r"(Platinum\s+Visa|Maybank\s+Plat)", re.I)),
    ("Maybank CA",            re.compile(r"Maybank\s+CA\b", re.I)),
    ("Maybank Overdraft",     re.compile(r"Maybank\s+Overdraft", re.I)),
    ("Maybank CreditAble",    re.compile(r"Maybank\s+Creditable|^Maybank\s+Credit(?!\s+Card)", re.I)),
    ("Maybank TxnHist",       re.compile(r"Maybank\s+Transaction", re.I)),
    ("DBS Cashline",          re.compile(r"DBS\s+Cashline|^Cashline\b|17370_", re.I)),  # 17370_ prefix = Cashline weekly
    ("DBS CC",                re.compile(r"DBS\s+CC|DBS(?!\s+Cash)", re.I)),
    ("SC Simply Cash CC",     re.compile(r"\bSC\b(?!.*BT|.*Balance)", re.I)),
    ("SC Balance Transfer",   re.compile(r"(SC.*(BT|Balance)|^BT\b)", re.I)),
    ("SC eStatement (auto)",  re.compile(r"_Temp_.*RS9528246F", re.I)),  # SC auto-export pattern
    ("SC CreditAble eStmt",   re.compile(r"^Credit(?:[Aa]ble|sAble).*eStatement", re.I)),
    ("HSBC CC",               re.compile(r"HSBC", re.I)),
    ("GXS Bank",              re.compile(r"\bGXS\b", re.I)),
    ("HL Bank",               re.compile(r"^HL\s+Bank", re.I)),
]

# Non-statement filters (credit reports, forms, payslips, misc)
EXCLUDE = re.compile(
    r"(credit\s+report|MLCB|CBS|CPF|loan\s+agreement|reinstatement|"
    r"payslip|payment|suspension|policy|form|funds?\s+transfer)",
    re.I,
)

def classify(filename: str) -> str | None:
    """Return card slot or None for non-statements."""
    if EXCLUDE.search(filename):
        return None
    for slot, pat in CARD_PATTERNS:
        if pat.search(filename):
            return slot
    return None  # unrecognized — likely not a statement


def scan_month_folder(folder: Path) -> dict:
    """Scan a month folder and return {card_slot: [filenames]} + ungrouped list."""
    found = defaultdict(list)
    excluded = []
    unrecognized = []
    for f in folder.iterdir():
        if not f.is_file() or not f.suffix.lower() == ".pdf":
            continue
        slot = classify(f.name)
        if slot:
            found[slot].append(f.name)
        elif EXCLUDE.search(f.name):
            excluded.append(f.name)
        else:
            unrecognized.append(f.name)
    return {"found": dict(found), "excluded": excluded, "unrecognized": unrecognized}


def main():
    print(f"Scanning: {ROOT}\n")
    if not ROOT.exists():
        print(f"NOT FOUND")
        return

    # Find all month folders (Jan'XX, Feb'XX, ... or YYYY-MM)
    month_folders = []
    for d in ROOT.rglob("*"):
        if not d.is_dir():
            continue
        # Match patterns: Jan'26, Feb'26, ..., or 2024/Jan, etc.
        if re.fullmatch(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'\d{2}", d.name):
            month_folders.append(d)
        elif re.fullmatch(r"\d{4}-\d{2}", d.name):
            month_folders.append(d)

    # Sort chronologically (best-effort)
    def sort_key(p):
        # Try to extract a sortable date
        name = p.name
        m = re.fullmatch(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'(\d{2})", name)
        if m:
            mon = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"].index(m.group(1)) + 1
            return (2000 + int(m.group(2)), mon)
        return (0, 0)

    month_folders.sort(key=sort_key)

    print(f"Found {len(month_folders)} month folders\n")
    print(f"{'Month':<12} {'Found':<6} {'Missing cards':<60}")
    print("-" * 90)

    expected_cards = [c[0] for c in CARD_PATTERNS]
    incomplete_months = []

    for folder in month_folders:
        # Show RELATIVE path so 2024/Jan'24 vs Jan'26 distinguish
        rel = folder.relative_to(ROOT)
        result = scan_month_folder(folder)
        found_cards = set(result["found"].keys())
        missing = [c for c in expected_cards if c not in found_cards]
        n_found = len(found_cards)
        if missing:
            missing_str = ", ".join(missing)
            print(f"{str(rel):<12} {n_found:<6} {missing_str:<60}")
            if n_found < 7:  # incomplete threshold
                incomplete_months.append(str(rel))
        else:
            print(f"{str(rel):<12} {n_found:<6} (all 8 present)")

    print()
    print(f"Months with <7 of 8 expected cards: {len(incomplete_months)}")
    for m in incomplete_months:
        print(f"  - {m}")

    # Also report any unrecognized files anywhere (possibly mis-named statements)
    print(f"\n=== Unrecognized PDFs in month folders (potential mis-classifications) ===")
    for folder in month_folders:
        result = scan_month_folder(folder)
        if result["unrecognized"]:
            print(f"\n  {folder.relative_to(ROOT)}:")
            for fn in result["unrecognized"]:
                print(f"    {fn}")


if __name__ == "__main__":
    main()
