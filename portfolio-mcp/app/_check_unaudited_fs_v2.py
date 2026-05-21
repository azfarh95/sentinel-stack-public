"""Dormant-FS tie-out check v2 — handles the abbreviated dormant-company structure.

Each docx typically has:
  T0 — director signature
  T1 — SFP (LIABILITIES AND CAPITAL DEFICIENCY only; assets section may be on prior table or absent)
  T2 — SOCI (1-2 lines, loss = trivial expense)
  T3 — SCE (3-col: Share / Accumulated losses / Net capital deficiency)
  T4 — SCF (4-5 rows)
  T5+ — Notes

Checks:
  A. SCE math: opening + profit = closing (per column AND total)
  B. SFP math: accumulated losses 2025 = accumulated losses 2024 + loss for year
  C. SFP math: Net capital deficiency = Share capital + Accumulated losses
  D. SCF cash beg + net change = cash end
  E. SOCI: PBT + tax = PAT
  F. Year-over-year continuity: opening 2025 = closing 2024 (in SCE)
"""
import re
from pathlib import Path
from docx import Document

UNAUDITED_DIR = Path("/tmp/unaudited")


def f(x):
    if x is None: return None
    if isinstance(x, (int, float)): return float(x)
    s = str(x).strip()
    if not s or s == "-" or s == "–": return 0.0
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True; s = s[1:-1]
    s = s.replace(",", "").replace("$", "").strip()
    if not s or s == "-": return 0.0
    try:
        v = float(s)
        return -v if negative else v
    except (ValueError, TypeError):
        return None


def cell(c):
    return c.text.replace("\n", " ").strip()


def row_text(row):
    return " | ".join(cell(c) for c in row.cells)


def find_table_by_first_text(doc, *patterns):
    """Scan entire table text (not just first 8 rows) for any of the patterns."""
    for t in doc.tables:
        full = " ".join(cell(c) for r in t.rows for c in r.cells).upper()
        if any(re.search(p, full, re.IGNORECASE) for p in patterns):
            return t
    return None


def find_row_in_table(t, *label_patterns):
    """Return (row_idx, row_obj) for first row whose first cell matches any pattern."""
    for ri, row in enumerate(t.rows):
        lbl = cell(row.cells[0]) if row.cells else ""
        for p in label_patterns:
            if re.search(p, lbl, re.IGNORECASE):
                return ri, row
    return None, None


def cell_values(row):
    """Return [(col_idx, float_or_None, raw_str), ...]"""
    return [(ci, f(cell(c)), cell(c)) for ci, c in enumerate(row.cells)]


def year_columns(t):
    """Inspect table's first 3 rows to find which columns are 2025 and 2024.
    Returns (col_2025, col_2024) — both may be None if not found."""
    col_2025 = col_2024 = None
    for ri in range(min(3, len(t.rows))):
        for ci, c in enumerate(t.rows[ri].cells):
            txt = cell(c)
            if "2025" in txt and col_2025 is None:
                col_2025 = ci
            elif "2024" in txt and col_2024 is None:
                col_2024 = ci
    return col_2025, col_2024


def value_at(row, ci):
    """Get the float value at column ci of a row. None if out of range / non-numeric."""
    if row is None or ci is None or ci >= len(row.cells): return None, ""
    raw = cell(row.cells[ci])
    return f(raw), raw


def check_fs(docx_path: Path):
    issues = []
    facts = {}
    entity = docx_path.stem
    doc = Document(str(docx_path))

    # Find SFP — broad patterns to cover all dormant + active layouts
    sfp = find_table_by_first_text(
        doc,
        r"LIABILITIES AND CAPITAL", r"TOTAL LIABILITIES AND EQUITY",
        r"TOTAL ASSETS",
        r"NET CAPITAL DEFICIENCY",            # dormant fallback
        r"CURRENT LIABILITIES.*SHARE CAPITAL", # dormant fallback (both keywords)
    )
    # Find SOCI — has "Loss before income tax" OR "Profit before income tax"
    soci = find_table_by_first_text(doc, r"LOSS BEFORE INCOME TAX", r"PROFIT BEFORE INCOME TAX",
                                     r"PROFIT FOR THE FINANCIAL YEAR")
    # Find SCE — has "Balance as at 1 January"
    sce = find_table_by_first_text(doc, r"BALANCE AS AT 1 JANUARY")
    # Find SCF — has "Operating activities" + "Cash and cash equivalents"
    scf = find_table_by_first_text(doc, r"OPERATING ACTIVITIES")

    facts["has_sfp"] = sfp is not None
    facts["has_soci"] = soci is not None
    facts["has_sce"] = sce is not None
    facts["has_scf"] = scf is not None

    # ── A. SCE math: opening + profit/loss = closing (per column) ──
    # SCE has Share/Accum-losses/Total columns. Each is a $ amount column.
    if sce:
        ri_open25, row_open25 = find_row_in_table(sce, r"Balance as at 1 January 2025")
        ri_loss25, row_loss25 = find_row_in_table(sce, r"(Profit|Loss) for the financial year")
        ri_close25, row_close25 = find_row_in_table(sce, r"Balance as at 31 December 2025")
        ri_open24, row_open24 = find_row_in_table(sce, r"Balance as at 1 January 2024")
        ri_close24, row_close24 = find_row_in_table(sce, r"Balance as at 31 December 2024")
        if row_open25 and row_loss25 and row_close25:
            # SCE columns are ALL $ amounts (no Note col). Use col_count from header.
            # Skip col 0 (label).
            ncols = len(row_open25.cells)
            for ci in range(1, ncols):
                vo, raw_o = value_at(row_open25, ci)
                vl, raw_l = value_at(row_loss25, ci)
                vc, raw_c = value_at(row_close25, ci)
                if vo is not None and vl is not None and vc is not None:
                    calc = vo + vl
                    if abs(calc - vc) > 1:
                        col_label = cell(sce.rows[0].cells[ci])[:30] if ci < len(sce.rows[0].cells) else f"col{ci}"
                        issues.append(
                            f"SCE column '{col_label}': opening {raw_o} + movement {raw_l} = {calc:,.0f} ≠ closing {raw_c}"
                        )
        # YoY continuity: closing 2024 == opening 2025
        if row_close24 and row_open25:
            ncols = max(len(row_close24.cells), len(row_open25.cells))
            for ci in range(1, ncols):
                v24, raw24 = value_at(row_close24, ci)
                v25o, raw25 = value_at(row_open25, ci)
                if v24 is not None and v25o is not None and abs(v24 - v25o) > 1:
                    col_label = cell(sce.rows[0].cells[ci])[:30] if ci < len(sce.rows[0].cells) else f"col{ci}"
                    issues.append(
                        f"SCE YoY column '{col_label}': closing 2024 ({raw24}) ≠ opening 2025 ({raw25})"
                    )

    # Get year columns for each statement (skip Note column properly)
    sfp_c25, sfp_c24 = year_columns(sfp) if sfp else (None, None)
    soci_c25, soci_c24 = year_columns(soci) if soci else (None, None)
    scf_c25, scf_c24 = year_columns(scf) if scf else (None, None)

    # ── B. SFP/SOCI tie: Accumulated losses delta = Loss for year ──
    if sfp and soci and sfp_c25 is not None and sfp_c24 is not None and soci_c25 is not None:
        _, row_al = find_row_in_table(sfp, r"^Accumulated losses$|^Retained (earnings|profits)$")
        _, row_pat = find_row_in_table(soci, r"(Profit|Loss) for the financial year")
        if row_al and row_pat:
            al_2025, raw_al25 = value_at(row_al, sfp_c25)
            al_2024, raw_al24 = value_at(row_al, sfp_c24)
            pat_2025, raw_pat = value_at(row_pat, soci_c25)
            if al_2025 is not None and al_2024 is not None and pat_2025 is not None:
                delta = al_2025 - al_2024
                if abs(delta - pat_2025) > 1:
                    issues.append(
                        f"SFP/SOCI tie: Accum losses delta ({raw_al25} - {raw_al24} = {delta:,.0f}) "
                        f"≠ Loss for year from SOCI ({raw_pat})"
                    )
                facts["pat"] = pat_2025
                facts["accum_losses_delta"] = delta

    # ── C. SFP: Net capital deficiency = Share capital + Capital reserves + Accumulated losses ──
    if sfp and sfp_c25 is not None and sfp_c24 is not None:
        _, row_sc = find_row_in_table(sfp, r"^Share capital$")
        _, row_cr = find_row_in_table(sfp, r"^Capital reserve")   # may be absent (None)
        _, row_fvr = find_row_in_table(sfp, r"^Fair value reserve")  # may be absent
        _, row_al = find_row_in_table(sfp, r"^Accumulated losses$|^Retained")
        _, row_ncd = find_row_in_table(sfp, r"^Net capital deficiency$|^Total equity$|^Capital deficiency$")
        if row_sc and row_al and row_ncd:
            for yi, year, c in [(0, 2025, sfp_c25), (1, 2024, sfp_c24)]:
                sc_v, sc_raw = value_at(row_sc, c)
                cr_v, cr_raw = value_at(row_cr, c) if row_cr else (0, "-")
                fvr_v, fvr_raw = value_at(row_fvr, c) if row_fvr else (0, "-")
                al_v, al_raw = value_at(row_al, c)
                ncd_v, ncd_raw = value_at(row_ncd, c)
                cr_v = cr_v or 0
                fvr_v = fvr_v or 0
                if sc_v is not None and al_v is not None and ncd_v is not None:
                    calc = sc_v + cr_v + fvr_v + al_v
                    if abs(calc - ncd_v) > 1:
                        parts = [f"Share cap ({sc_raw})"]
                        if row_cr: parts.append(f"Capital reserves ({cr_raw})")
                        if row_fvr: parts.append(f"FV reserve ({fvr_raw})")
                        parts.append(f"Accum losses ({al_raw})")
                        issues.append(
                            f"SFP {year}: " + " + ".join(parts) + f" = {calc:,.0f} ≠ Equity total ({ncd_raw})"
                        )

    # ── D. SCF: cash beg + net change = cash end ──
    if scf and scf_c25 is not None:
        _, row_nc = find_row_in_table(scf, r"Net changes? in cash|Net (increase|decrease) in cash")
        _, row_cb = find_row_in_table(scf, r"Cash and cash equivalents at beginning|as at 1 January")
        _, row_ce = find_row_in_table(scf, r"Cash and cash equivalents at end|as at 31 December")
        if row_nc and row_cb and row_ce:
            for yi, year, c in [(0, 2025, scf_c25), (1, 2024, scf_c24)]:
                if c is None: continue
                nc_v, nc_raw = value_at(row_nc, c)
                cb_v, cb_raw = value_at(row_cb, c)
                ce_v, ce_raw = value_at(row_ce, c)
                if nc_v is not None and cb_v is not None and ce_v is not None:
                    calc = cb_v + nc_v
                    if abs(calc - ce_v) > 1:
                        issues.append(
                            f"SCF {year}: Cash beg ({cb_raw}) + Net change ({nc_raw}) = {calc:,.0f} "
                            f"≠ Cash end ({ce_raw})"
                        )

    # ── E. SOCI: PBT + tax = PAT ──
    if soci and soci_c25 is not None:
        _, row_pbt = find_row_in_table(soci, r"(Profit|Loss) before income tax")
        # Anchored — don't match "Loss before income tax" which CONTAINS "income tax"
        _, row_tax = find_row_in_table(soci, r"^Income tax (expense|credit|expense/\(credit\)|credit/\(expense\))")
        _, row_pat = find_row_in_table(soci, r"(Profit|Loss) for the financial year")
        if row_pbt and row_pat:
            for yi, year, c in [(0, 2025, soci_c25), (1, 2024, soci_c24)]:
                if c is None: continue
                pbt_v, pbt_raw = value_at(row_pbt, c)
                tax_v, tax_raw = value_at(row_tax, c) if row_tax else (0, "-")
                pat_v, pat_raw = value_at(row_pat, c)
                if pbt_v is not None and pat_v is not None:
                    tx = tax_v if tax_v is not None else 0
                    calc = pbt_v + tx
                    if abs(calc - pat_v) > 1:
                        issues.append(
                            f"SOCI {year}: PBT ({pbt_raw}) + Tax ({tax_raw}) = {calc:,.0f} ≠ PAT ({pat_raw})"
                        )

    return entity, issues, facts


def main():
    files = sorted(UNAUDITED_DIR.glob("*.docx"))
    print(f"# Unaudited FS Tie-Out Check v2 — {len(files)} files\n")

    clean = []
    dirty = []
    for f_path in files:
        try:
            entity, issues, facts = check_fs(f_path)
        except Exception as e:
            entity = f_path.stem
            issues = [f"❌ PARSE ERROR: {type(e).__name__}: {str(e)[:120]}"]
            facts = {}
        missing = [k for k in ("has_sfp", "has_soci", "has_sce", "has_scf") if not facts.get(k)]
        if missing:
            issues.insert(0, f"Missing statements: {', '.join(s.replace('has_','') for s in missing)}")
        if issues:
            dirty.append((entity, issues, facts))
        else:
            clean.append((entity, facts))

    print(f"## Summary: {len(clean)} clean / {len(dirty)} with issues\n")

    if dirty:
        print("## Files with issues\n")
        for entity, issues, facts in dirty:
            print(f"### {entity}\n")
            if facts.get("pat") is not None:
                print(f"  Loss for year (2025): ${facts['pat']:,.2f}")
            for i in issues:
                print(f"  - ⚠ {i}")
            print()

    print("## Clean files\n")
    for entity, facts in clean:
        pat = facts.get("pat")
        s = f"loss ${pat:,.0f}" if isinstance(pat, (int, float)) else "(no PAT)"
        print(f"- **{entity}** — {s}")


main()
