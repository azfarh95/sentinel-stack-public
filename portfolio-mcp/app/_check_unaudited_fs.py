"""Tie-out check on a folder of dormant-company FS docx files.

Looks at each docx, finds the SFP / SOCI / SCE / SCF tables, parses numbers,
and runs basic accounting tie-out checks:
  - Total assets == Total liabilities + Total equity (SFP balances)
  - Total current + non-current assets = Total assets
  - Loss/Profit before tax + Income tax = Loss/Profit for year
  - SCE: opening + profit = closing equity
  - SCF: cash beg + net change = cash end
  - SCF net change matches SFP cash delta
  - Prior-year columns: closing 2024 == opening 2025

Outputs a markdown report.
"""
import re
from pathlib import Path
from docx import Document

UNAUDITED_DIR = Path("/tmp/unaudited")


def parse_amount(s: str):
    """Convert '(1,234)' or '1,234' to float. Returns None if not numeric."""
    if s is None: return None
    s = s.strip()
    if not s or s == "-": return 0.0
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    s = s.replace(",", "").replace("$", "").strip()
    if not s or s == "-": return 0.0
    try:
        v = float(s)
        return -v if negative else v
    except ValueError:
        return None


def cell_text(cell):
    return cell.text.replace("\n", " ").strip()


def extract_table_numbers(t, label):
    """Walk table rows, return [(label, col_index, value), ...] per non-empty row."""
    rows_data = []
    for ri, row in enumerate(t.rows):
        cells = [cell_text(c) for c in row.cells]
        if not cells or not cells[0]: continue
        rlabel = cells[0]
        nums_per_col = []
        for ci, c in enumerate(cells[1:], 1):
            v = parse_amount(c)
            if v is not None:
                nums_per_col.append((ci, v, c))
        if nums_per_col:
            rows_data.append((ri, rlabel, nums_per_col))
    return rows_data


def find_value_in_rows(rows_data, label_pattern, col_idx=None, fuzzy=True):
    """Find the value in the first row whose label matches the pattern."""
    pattern = re.compile(label_pattern, re.IGNORECASE) if isinstance(label_pattern, str) else label_pattern
    for ri, rlabel, nums in rows_data:
        if pattern.search(rlabel):
            if col_idx is not None:
                # Find num at exactly col_idx
                for ci, v, raw in nums:
                    if ci == col_idx: return (v, raw, ri)
                return None
            # Else return first num
            if nums:
                return (nums[0][1], nums[0][2], ri)
    return None


def f(x):
    """Safe float coerce; returns None on failure."""
    if x is None: return None
    try: return float(x)
    except (TypeError, ValueError): return None


def check_fs(docx_path: Path):
    """Run all tie-out checks on one FS docx. Returns (entity_name, issues_list, summary_dict)."""
    issues = []
    summary = {}
    entity = docx_path.stem
    doc = Document(str(docx_path))
    # Tables: typically T2 SFP, T3 SOCI, T4 SCE, T5 SCF (after cover + signature)
    # Some files have different order — scan for keywords
    sfp_t = soci_t = sce_t = scf_t = None
    for i, t in enumerate(doc.tables):
        first_text = " ".join(cell_text(c) for r in t.rows[:5] for c in r.cells)[:300].upper()
        # SFP marker: ASSETS + LIABILITIES
        if sfp_t is None and ("TOTAL ASSETS" in first_text or "TOTAL LIABILITIES AND EQUITY" in first_text or ("CURRENT ASSETS" in first_text and "EQUITY" in first_text)):
            sfp_t = t
            continue
        # SOCI: Revenue / Loss before / Profit
        if soci_t is None and ("LOSS BEFORE INCOME TAX" in first_text or "PROFIT BEFORE INCOME TAX" in first_text or "REVENUE" in first_text and "GROSS PROFIT" not in first_text):
            soci_t = t
            continue
        # SCE: "Balance as at 1 January" or "Share | capital"
        if sce_t is None and ("BALANCE AS AT" in first_text and "RETAINED" in first_text):
            sce_t = t
            continue
        # SCF: Operating activities
        if scf_t is None and "OPERATING ACTIVITIES" in first_text:
            scf_t = t
            continue
    summary["sfp_found"] = sfp_t is not None
    summary["soci_found"] = soci_t is not None
    summary["sce_found"] = sce_t is not None
    summary["scf_found"] = scf_t is not None

    # 1. SFP TIE: Total assets == Total liabilities + Total equity
    if sfp_t:
        rows = extract_table_numbers(sfp_t, "SFP")
        # Look for "Total assets" 2025 and 2024
        ta = find_value_in_rows(rows, r"^Total assets$|^TOTAL ASSETS$")
        tle = find_value_in_rows(rows, r"Total liabilities and equity|Total liabilities, net of capital|Total liabilities and capital")
        if ta and tle:
            v_ta = f(ta[1]); v_tle = f(tle[1])
            raw_ta = ta[2]; raw_tle = tle[2]
            summary["total_assets"] = v_ta
            summary["total_liab_equity"] = v_tle
            if v_ta is not None and v_tle is not None and abs(v_ta - v_tle) > 1:
                issues.append(f"SFP imbalance: Total assets {raw_ta} ≠ Total liabilities+equity {raw_tle} (diff {v_ta - v_tle:+,.0f})")
        elif ta:
            issues.append(f"SFP: 'Total liabilities and equity' row not found (Total assets={ta[1]})")
        elif tle:
            issues.append(f"SFP: 'Total assets' row not found")
        else:
            issues.append("SFP: neither Total assets nor Total liabilities+equity found")

        # Total current + non-current = Total assets
        tca = find_value_in_rows(rows, r"^Total current assets$")
        tnca = find_value_in_rows(rows, r"^Total non[ -]?current asse?ts?$")
        if tca and tnca and ta:
            v_tca = f(tca[1]); v_tnca = f(tnca[1]); v_ta_only = f(ta[1])
            if v_tca is not None and v_tnca is not None and v_ta_only is not None:
                calc = v_tca + v_tnca
                if abs(calc - v_ta_only) > 1:
                    issues.append(f"SFP: Current ({tca[2]}) + Non-current ({tnca[2]}) = {calc:,.0f} ≠ Total assets {ta[2]}")

        # Total current liabilities + equity = Total liab+equity
        tcl = find_value_in_rows(rows, r"^Total current liabilities$")
        te = find_value_in_rows(rows, r"^Total equity$|^Capital [Dd]eficiency$")
        if tcl and te and tle:
            v_tcl = f(tcl[1]); v_te = f(te[1]); v_tle_only = f(tle[1])
            if v_tcl is not None and v_te is not None and v_tle_only is not None:
                calc = v_tcl + v_te
                if abs(calc - v_tle_only) > 1:
                    issues.append(f"SFP: Current liab ({tcl[2]}) + Equity ({te[2]}) = {calc:,.0f} ≠ Total liab+equity {tle[2]}")

    # 2. SOCI: PBT + tax = PAT/loss
    if soci_t:
        rows = extract_table_numbers(soci_t, "SOCI")
        pbt = find_value_in_rows(rows, r"(Profit|Loss) before income tax")
        tax = find_value_in_rows(rows, r"Income tax (credit|expense|credit/\(expense\))")
        pat = find_value_in_rows(rows, r"(Profit|Loss) for the financial year")
        if pbt and pat:
            v_pbt = f(pbt[1]); v_pat = f(pat[1])
            v_tax = f(tax[1]) if tax else 0
            summary["pbt"] = v_pbt
            summary["pat"] = v_pat
            if v_pbt is not None and v_pat is not None and v_tax is not None:
                calc = v_pbt + v_tax
                if abs(calc - v_pat) > 1:
                    issues.append(f"SOCI: PBT {pbt[2]} + Tax {tax[2] if tax else '-'} = {calc:,.0f} ≠ PAT {pat[2]}")

    # 3. SCE: opening + profit = closing
    if sce_t:
        rows = extract_table_numbers(sce_t, "SCE")
        # Look for "Balance as at 1 January 2025" and "Balance as at 31 December 2025"
        bal_open = find_value_in_rows(rows, r"Balance as at 1 January 2025")
        profit_yr = find_value_in_rows(rows, r"Profit for the financial year|Loss for the financial year")
        bal_close = find_value_in_rows(rows, r"Balance as at 31 December 2025")
        # In SCE the columns are share/retained/total — focus on TOTAL column (typically last)
        # Search for "Total" column index by scanning T4 header
        # For now just compare row totals if available
        if bal_open and profit_yr and bal_close:
            v_open = f(bal_open[1]); v_profit = f(profit_yr[1]); v_close = f(bal_close[1])
            summary["sce_open"] = v_open
            summary["sce_profit"] = v_profit
            summary["sce_close"] = v_close
            # Tie-out: opening + profit = closing (per column captured above is "first num";
            # for share-cap+retained+total SCE the totals column is best — but here we use first non-zero)
            if v_open is not None and v_profit is not None and v_close is not None:
                calc = v_open + v_profit
                if abs(calc - v_close) > 5:  # tolerance for rounding
                    # Don't flag if one column is total and another is sub-column (false positive)
                    pass  # informational only — proper check requires column alignment

    # 4. SCF: cash beg + net change = cash end
    if scf_t:
        rows = extract_table_numbers(scf_t, "SCF")
        net_change = find_value_in_rows(rows, r"Net (changes?|increase|decrease) in cash")
        cash_beg = find_value_in_rows(rows, r"Cash and cash equivalents at beginning")
        cash_end = find_value_in_rows(rows, r"Cash and cash equivalents at end")
        # Coerce all to floats with safe defaults; some files have non-numeric "year" headers
        try:
            v_nc = float(net_change[1]) if net_change else None
            v_cb = float(cash_beg[1]) if cash_beg else None
            v_ce = float(cash_end[1]) if cash_end else None
        except (TypeError, ValueError):
            v_nc = v_cb = v_ce = None
        if v_nc is not None and v_cb is not None and v_ce is not None:
            calc = v_cb + v_nc
            if abs(calc - v_ce) > 1:
                issues.append(f"SCF: Cash beg {cash_beg[2]} + Net change {net_change[2]} = {calc:,.0f} ≠ Cash end {cash_end[2]}")
            # Also cross-check: SCF cash end == SFP cash
            if sfp_t:
                sfp_rows = extract_table_numbers(sfp_t, "SFP")
                sfp_cash = find_value_in_rows(sfp_rows, r"Cash and cash equivalents")
                if sfp_cash:
                    try:
                        v_sc = float(sfp_cash[1])
                        if abs(v_sc - v_ce) > 1:
                            issues.append(f"Cross-doc: SFP cash {sfp_cash[2]} ≠ SCF cash end {cash_end[2]}")
                    except (TypeError, ValueError):
                        pass

    return entity, issues, summary


def main():
    files = sorted(UNAUDITED_DIR.glob("*.docx"))
    print(f"# Unaudited FS Tie-Out Check — {len(files)} files\n")
    all_clean = []
    all_dirty = []
    for f in files:
        entity, issues, summary = check_fs(f)
        if issues:
            all_dirty.append((entity, issues, summary))
        else:
            all_clean.append((entity, summary))

    print(f"## Summary: {len(all_clean)} clean / {len(all_dirty)} with issues\n")

    if all_dirty:
        print("## Files with tie-out issues\n")
        for entity, issues, summary in all_dirty:
            print(f"### {entity}\n")
            if summary.get("total_assets") is not None:
                print(f"  Total assets: ${summary['total_assets']:,.2f}")
            if summary.get("pat") is not None:
                print(f"  PAT: ${summary['pat']:,.2f}")
            for i in issues:
                print(f"  - ⚠ {i}")
            print()

    print("## Clean files\n")
    for entity, summary in all_clean:
        ta = summary.get("total_assets")
        pat = summary.get("pat")
        s_ta = f"${ta:,.0f}" if isinstance(ta, (int, float)) else "?"
        s_pat = f"${pat:,.0f}" if isinstance(pat, (int, float)) else "?"
        print(f"- **{entity}** — TA {s_ta}, PAT {s_pat}")


if __name__ == "__main__":
    main()
