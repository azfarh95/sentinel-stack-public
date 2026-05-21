"""Auto-sort CC statement files from /onedrive/Sentinel Finance/02_Credit card statements/2025/unsorted
into the right month folder (e.g. May'25, Jun'25).

Detects bank via cc_statement_parser, extracts statement_date, moves to:
    /onedrive/Sentinel Finance/02_Credit card statements/<YYYY>/<Mon>'<YY>/<filename>

Defaults to DRY-RUN. Pass --apply to actually move.

Filename-pattern fallback: when PDF text is image-only (HSBC's _Temp_ files),
try to derive (year, month) from filename or modification date.

Run:
    docker exec portfolio-mcp python -m app.sort_cc_statements              # dry-run
    docker exec portfolio-mcp python -m app.sort_cc_statements --apply      # actually move
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import date as _date, datetime
from pathlib import Path

from . import cc_statement_parser as p

UNSORTED_DIR = Path("/onedrive/Sentinel Finance/02_Credit card statements/2025/unsorted")
ROOT = Path("/onedrive/Sentinel Finance/02_Credit card statements")

MONTH_ABBREV = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
                7: "July", 8: "Aug", 9: "Sept", 10: "Oct", 11: "Nov", 12: "Dec"}

# Canonical filename: <Bank Name> <CC|CA|CL> <Mon>'<YY>.pdf
CANONICAL_PREFIX = {
    "dbs_cc": "DBS CC",
    "dbs_cashline": "DBS CL",
    "hsbc_cc": "HSBC CC",
    "maybank_cc": "Maybank CC",
    "maybank_ca": "Maybank CA",
    "sc": "SC CC",        # SC statement covers SimplyCash CC + BT under one PDF
    "uob": "UOB CL",      # CashPlus = line of credit
    "gxs": "GXS",
}


def canonical_filename(bank: str, stmt_date: _date, suffix: str = ".pdf") -> str:
    """Return canonical filename like 'DBS CC Apr'25.pdf' from bank + date.
    Month uses folder convention (Jan/Feb/Mar/Apr/May/Jun/July/Aug/Sept/Oct/Nov/Dec)."""
    prefix = CANONICAL_PREFIX.get(bank, bank.upper())
    mo_str = MONTH_ABBREV[stmt_date.month]
    yy = stmt_date.strftime("%y")
    return f"{prefix} {mo_str}'{yy}{suffix.lower()}"


def derive_target_folder(stmt_date: _date) -> Path:
    """Compute the OneDrive folder for this statement date.
    Folder convention is the user's: <YYYY>/<Mon>'<YY>/ for 2024+2025,
    or <Mon>'<YY>/ at root for 2026."""
    yr = stmt_date.year
    yy = stmt_date.strftime("%y")
    mo_str = MONTH_ABBREV[stmt_date.month]
    folder_name = f"{mo_str}'{yy}"
    if yr == 2026:
        return ROOT / folder_name
    return ROOT / str(yr) / folder_name


def derive_date_from_filename(filename: str) -> _date | None:
    """Fallback: parse year+month from filename patterns."""
    fn = filename.lower()
    # YYYY_MM_ pattern (HSBC _Temp files)
    m = re.search(r"_(\d{4})_(\d{2})_", fn)
    if m:
        yr, mo = int(m.group(1)), int(m.group(2))
        if 2020 <= yr <= 2030 and 1 <= mo <= 12:
            return _date(yr, mo, 15)
    # -YYYY-MM- pattern (GXS FlexiLoan export: 800XXXXX595-2025-01-statement.pdf)
    m = re.search(r"-(\d{4})-(\d{2})-", fn)
    if m:
        yr, mo = int(m.group(1)), int(m.group(2))
        if 2020 <= yr <= 2030 and 1 <= mo <= 12:
            return _date(yr, mo, 15)
    # MMYY pattern (e.g. "0425")
    m = re.search(r"(\d{2})(\d{2})\.(pdf|jpg|jpeg|png)", fn)
    if m:
        mo, yy = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 20 <= yy <= 30:
            return _date(2000 + yy, mo, 15)
    # DD.MM.YY pattern
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{2})", fn)
    if m:
        d, mo, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 20 <= yy <= 30:
            return _date(2000 + yy, mo, d)
    # DDMMYYYY pattern
    m = re.search(r"(\d{2})(\d{2})(\d{4})", fn)
    if m:
        d, mo, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 2020 <= yr <= 2030:
            return _date(yr, mo, d)
    # MonthName'YY pattern in filename (e.g. "Apr'25")
    m = re.search(r"([a-z]+)['\s]*(\d{2})", fn)
    if m:
        mo_str = m.group(1)[:3]
        months = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                  "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
        if mo_str in months:
            yy = int(m.group(2))
            if 20 <= yy <= 30:
                return _date(2000 + yy, months[mo_str], 15)
    return None


def classify_file(path: Path) -> tuple[str | None, _date | None, str | None]:
    """Return (bank, statement_date, note)."""
    # Hard-exclude non-statement document types (application forms, tx history,
    # credit reports, etc.) — these aren't bank statements even if filename has bank name.
    fn_lower = path.name.lower()
    excluded_patterns = [
        "application form", "consolidation", "acknowledgement", "_encrypted",
        "transactionhistory", "transaction history", "credit report",
        "payslip", "noa ", "cbs ", "mlcb ", "cpf latest", "cpf contribution",
        "dc acknowledgement", "dc application", "dc form", "dcp",
        "15 months", "ml compairson",
    ]
    if any(p in fn_lower for p in excluded_patterns):
        return (None, None, "excluded: non-statement doc")
    # Try parser first (gets bank + exact statement date)
    try:
        stmt = p.detect_and_parse(str(path))
        if stmt and stmt.statement_date and not stmt.parse_errors:
            return (stmt.bank, stmt.statement_date, None)
        if stmt and stmt.statement_date:
            return (stmt.bank, stmt.statement_date, f"parse_errors: {stmt.parse_errors[0][:60]}")
    except Exception:
        pass
    # Filename-pattern fallback
    fn = path.name.lower()
    bank = None
    if "dbs cc" in fn or "credit cards consolidated" in fn or "credit cards statement" in fn:
        bank = "dbs_cc"
    elif "dbs cl" in fn or "dbs cashline" in fn or "cashline statement" in fn:
        bank = "dbs_cashline"
    elif "hsbc" in fn or "_temp_" in fn:
        bank = "hsbc_cc"
    elif "creditable" in fn or "maybank ca" in fn:
        bank = "maybank_ca"
    elif "platinum visa" in fn or ("maybank" in fn and "cc" in fn):
        bank = "maybank_cc"
    elif "maybank" in fn:
        bank = "maybank_cc"
    elif ("sc cc" in fn or "standard chartered" in fn or fn.startswith("sc ")
          or fn.startswith("estatement") or "scbl" in fn):
        # SC's e-statements use timestamp-only filenames; dispatch to SC and let
        # content parser verify
        bank = "sc"
    elif "uob" in fn:
        bank = "uob"
    elif "gxs" in fn or re.match(r"^800[\dxX]{5,}-?", fn):
        bank = "gxs"
    d = derive_date_from_filename(path.name)
    if bank and d:
        return (bank, d, "filename-fallback")
    if bank:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime).date()
            return (bank, mtime, "mtime-fallback")
        except Exception:
            pass
    return (None, None, None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually move files (default: dry-run)")
    parser.add_argument("--source", default=str(UNSORTED_DIR),
                        help="Source folder")
    parser.add_argument("--rename", action="store_true",
                        help="Rename files to canonical convention during move (e.g. 'DBS CC 0425.pdf')")
    parser.add_argument("--rename-existing", action="store_true",
                        help="ALSO rename existing files in the destination tree (does NOT move them)")
    args = parser.parse_args()

    # Mode: rename-existing walks the whole CC_Statement tree and renames any file
    # whose name doesn't match the canonical convention.
    if args.rename_existing:
        return rename_existing_tree(apply=args.apply)

    src = Path(args.source)
    if not src.exists():
        print(f"ERROR: source folder doesn't exist: {src}", file=sys.stderr)
        sys.exit(1)

    files = [f for f in src.iterdir() if f.is_file() and f.suffix.lower() in (".pdf", ".jpg", ".jpeg", ".png")]
    print(f"Scanning {len(files)} files in {src}")
    print()
    print(f"{'File':<55}  {'Bank':<12}  {'Target folder':<22}  {'Note'}")
    print("-" * 120)

    plan: list[tuple[Path, Path]] = []
    unidentified: list[Path] = []
    for f in sorted(files):
        bank, stmt_d, note = classify_file(f)
        if bank is None or stmt_d is None:
            print(f"  {f.name[:53]:<55}  {'?':<12}  {'(UNIDENTIFIED)':<22}  needs manual sort")
            unidentified.append(f)
            continue
        target_folder = derive_target_folder(stmt_d)
        # If --rename, compute canonical name; else keep original.
        target_name = canonical_filename(bank, stmt_d, f.suffix) if args.rename else f.name
        plan.append((f, target_folder / target_name))
        note_disp = note or ""
        rename_disp = f" → {target_name}" if args.rename and target_name != f.name else ""
        print(f"  {f.name[:43]:<45}  {bank:<12}  {target_folder.name:<10}  {target_name:<22}  {note_disp[:30]}")

    print()
    print(f"Plan: {len(plan)} files would be moved, {len(unidentified)} need manual sort")

    if args.apply:
        print()
        print("APPLYING moves...")
        moved = 0
        for src_path, dst_path in plan:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            if dst_path.exists():
                stem, suffix = dst_path.stem, dst_path.suffix
                i = 1
                while dst_path.exists():
                    dst_path = dst_path.parent / f"{stem} ({i}){suffix}"
                    i += 1
            shutil.move(str(src_path), str(dst_path))
            moved += 1
        print(f"Moved {moved} files. Re-run statement_completeness to see updated coverage.")
    else:
        print()
        print("DRY-RUN — no files moved. Pass --apply to execute after review.")


def rename_existing_tree(apply: bool = False):
    """Walk the entire CC_Statement tree and rename files that aren't using
    canonical naming."""
    print(f"Scanning entire {ROOT} tree for files to rename")
    print()
    print(f"{'Old name':<58}  {'Bank':<12}  {'New name':<22}  Folder")
    print("-" * 120)
    plan: list[tuple[Path, Path]] = []
    skipped_canonical = 0
    skipped_unidentified = 0
    for path in ROOT.rglob("*.pdf"):
        # Skip the unsorted dir — that's the sort workflow's domain
        if "unsorted" in [p.name for p in path.parents]:
            continue
        bank, stmt_d, note = classify_file(path)
        if bank is None or stmt_d is None:
            skipped_unidentified += 1
            continue
        canonical = canonical_filename(bank, stmt_d, path.suffix)
        if path.name == canonical:
            skipped_canonical += 1
            continue
        # Don't move across folders — only rename in place
        new_path = path.parent / canonical
        plan.append((path, new_path))
        print(f"  {path.name[:56]:<58}  {bank:<12}  {canonical:<22}  {path.parent.name}")

    print()
    print(f"Plan: {len(plan)} files to rename · {skipped_canonical} already canonical · {skipped_unidentified} unidentified")

    if apply and plan:
        print()
        print("APPLYING renames...")
        renamed = 0
        for src, dst in plan:
            if dst.exists():
                stem, suffix = dst.stem, dst.suffix
                i = 1
                while dst.exists():
                    dst = dst.parent / f"{stem} ({i}){suffix}"
                    i += 1
            shutil.move(str(src), str(dst))
            renamed += 1
        print(f"Renamed {renamed} files.")
    elif not apply:
        print()
        print("DRY-RUN — pass --apply to execute.")


if __name__ == "__main__":
    main()
