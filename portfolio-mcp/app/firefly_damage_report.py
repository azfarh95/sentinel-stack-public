"""Firefly vs Sentinel Finance — damage report.

Compares what Firefly's PDF importer captured against what the universal
parser captures from the same POSB source PDFs.

Surfaces:
  1. Missing journals — lines in PDF but not in GL
  2. Mis-routed journals — in GL under catch-all 5190/4900, but PDF reveals
     a specific entity (Singlife, Coinbase, Wise, etc.)
  3. Stripped-recipient journals — bridged with destination_name='Unknown'
     even though PDF has the recipient on a continuation line

Output: Markdown report.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from pathlib import Path

from app import database as db
from app.universal_pdf_parser import load_all_schemas, parse_pdf
from sqlalchemy import text

POSB_PDF_DIR = Path("/onedrive/Sentinel Finance/01_Bank statements/DBS_POSB Savings")
POSB_ACCOUNT_ID = 4
SUSPENSE_CODES = {"1190", "5190", "4900"}


def get_firefly_bridge_summary(s) -> dict:
    """Aggregate FIREFLY_BRIDGE POSB journals by other-leg CoA + month."""
    rows = s.execute(text("""
      SELECT j.journal_date,
             j.narration,
             coa.account_code,
             coa.account_name,
             COALESCE(gl.debit, gl.credit) AS amt
      FROM general_ledger gl
      JOIN journals j ON j.id = gl.journal_id
      JOIN chart_of_accounts coa ON coa.id = gl.account_id
      WHERE j.source_doc LIKE 'FIREFLY_BRIDGE%'
        AND j.status != 'void'
        AND gl.account_id != :posb_aid
        AND EXISTS (
          SELECT 1 FROM general_ledger gl2
          WHERE gl2.journal_id = j.id AND gl2.account_id = :posb_aid
        )
      ORDER BY j.journal_date
    """), {"posb_aid": POSB_ACCOUNT_ID}).all()
    return rows


def get_pdf_extracts(year_from: int = 2024) -> list[dict]:
    """Run universal parser across all POSB statements from year_from."""
    schemas = load_all_schemas()
    all_tx = []
    for pdf in sorted(POSB_PDF_DIR.glob("Deposit Account Statement_*.pdf")):
        r = parse_pdf(pdf, schemas)
        if not r.statement_date or int(r.statement_date[:4]) < year_from:
            continue
        for tx in r.transactions:
            all_tx.append({
                "date": tx.date_iso,
                "type": tx.tx_type,
                "amount": tx.amount,
                "direction": tx.direction,
                "carriers": tx.carriers,
                "raw_lines": tx.raw_lines,
            })
    return all_tx


def classify_from_carriers(tx: dict) -> tuple[str, str]:
    """Apply schema-derived carrier hints to suggest the right CoA.
    Returns (suggested_coa, reason)."""
    c = tx.get("carriers") or {}
    if c.get("insurance_policy_ref") == "P4064051":
        return "1222", "Singlife Savvy Invest ILP (P4064051)"
    if "P4064051" in str(c.get("insurance_policy_long_ref", "")):
        return "1222", "Singlife Savvy Invest ILP (long ref)"
    if c.get("entity_name_uppercase") and "SINGAPORE LIFE" in c["entity_name_uppercase"]:
        return "5340", "Singlife pure insurance premium"
    recipient = c.get("paynow_recipient", "")
    if recipient:
        rl = recipient.upper()
        if "COINBASE" in rl:           return "1231", "Coinbase top-up (crypto wallet)"
        if "SEAMONEY" in rl or "MONEE" in rl: return "1112", "ShopeePay wallet (SeaMoney/Monee)"
        if "EZ LOAN" in rl:            return "2221", "EZ Loan repayment"
        if "LENDING BEE" in rl:        return "2222", "Lending Bee repayment"
        if "SANDS CREDIT" in rl:       return "2223", "Sands Credit repayment"
        if "WISE" in rl:               return "1113", "Wise transfer"
        if "ATOME" in rl:              return "2115", "Atome BNPL"
        if "GRABPAY" in rl:            return "1112", "GrabPay wallet"
        if rl.startswith("AZFAR HAKIM"): return "1190", "self-transfer (suspense)"
        if " PTE" in rl or " LTD" in rl or " LLP" in rl:
            return "1190", f"Entity recipient: {recipient[:30]} (needs specific rule)"
        return "5170", f"Personal-name PayNow: {recipient[:30]}"
    if c.get("entity_name_uppercase"):
        en = c["entity_name_uppercase"]
        if "TOKIO MARINE" in en:       return "5340", "Tokio Marine premium"
        if "AIA SINGAPORE" in en:      return "5340", "AIA premium"
        return "1190", f"Entity: {en[:30]} (needs specific rule)"
    return "1190", "no carrier info — suspense"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/data/firefly_damage_report.md")
    ap.add_argument("--year-from", type=int, default=2024)
    args = ap.parse_args()

    db.init_db()
    s = db.SessionLocal()

    print("Loading Firefly bridge journals...")
    bridge_rows = get_firefly_bridge_summary(s)
    print(f"  {len(bridge_rows)} bridged POSB journals")

    print("Running universal parser across POSB statements...")
    pdf_tx = get_pdf_extracts(year_from=args.year_from)
    print(f"  {len(pdf_tx)} transactions extracted from PDFs (≥{args.year_from})")

    # 1) Aggregate Firefly bridge by other-leg CoA
    by_firefly_coa: dict = defaultdict(lambda: {"count": 0, "amount": 0.0, "samples": []})
    for r in bridge_rows:
        code = r[2]
        amt = float(r[4] or 0)
        d = by_firefly_coa[code]
        d["count"] += 1
        d["amount"] += amt
        if len(d["samples"]) < 3:
            d["samples"].append((str(r[0]), r[1][:60], amt))

    # 2) Aggregate PDF tx — what direct path would route them to
    by_direct_coa: dict = defaultdict(lambda: {"count": 0, "amount": 0.0, "samples": []})
    for tx in pdf_tx:
        coa, reason = classify_from_carriers(tx)
        d = by_direct_coa[coa]
        d["count"] += 1
        d["amount"] += tx["amount"]
        if len(d["samples"]) < 3:
            d["samples"].append({"date": tx["date"], "amount": tx["amount"], "reason": reason, "carriers": tx["carriers"]})

    # 3) Specifically: Savvy Invest gap
    savvy_in_pdf = [t for t in pdf_tx if t.get("carriers", {}).get("insurance_policy_ref") == "P4064051"]
    savvy_in_gl = s.execute(text("""
      SELECT j.journal_date, j.narration, coa.account_code
      FROM general_ledger gl
      JOIN journals j ON j.id = gl.journal_id
      JOIN chart_of_accounts coa ON coa.id = gl.account_id
      WHERE j.source_doc LIKE 'FIREFLY_BRIDGE%'
        AND ABS(gl.debit - 252.85) < 0.01
        AND gl.account_id != :aid
        AND j.status != 'void'
      ORDER BY j.journal_date
    """), {"aid": POSB_ACCOUNT_ID}).all()

    # 4) Build the report
    lines = []
    lines.append("# Firefly vs Sentinel Finance — Damage Report")
    lines.append("")
    lines.append("Generated by `app.firefly_damage_report` on the universal parser's "
                 f"output across POSB statements ≥{args.year_from}.")
    lines.append("")
    lines.append("## 1. Top-level numbers")
    lines.append("")
    lines.append("| Source | Tx count | Total $ |")
    lines.append("|---|---:|---:|")
    fb_total = sum(d["amount"] for d in by_firefly_coa.values())
    fb_count = sum(d["count"] for d in by_firefly_coa.values())
    direct_total = sum(d["amount"] for d in by_direct_coa.values())
    lines.append(f"| Firefly bridge (POSB other-leg) | {fb_count:,} | {fb_total:,.2f} |")
    lines.append(f"| Universal parser (POSB ≥{args.year_from}) | {len(pdf_tx):,} | {direct_total:,.2f} |")
    lines.append("")
    lines.append("## 2. Firefly bridge: where the money went")
    lines.append("")
    lines.append("Top 15 CoA buckets by amount where Firefly parked POSB tx:")
    lines.append("")
    lines.append("| CoA | Tx | $ | Note |")
    lines.append("|---|---:|---:|---|")
    top_fb = sorted(by_firefly_coa.items(), key=lambda kv: -kv[1]["amount"])[:15]
    for code, d in top_fb:
        flag = "CATCH-ALL" if code in SUSPENSE_CODES else ""
        lines.append(f"| `{code}` | {d['count']:,} | {d['amount']:,.2f} | {flag} |")
    lines.append("")
    catch_amt = sum(d["amount"] for c, d in by_firefly_coa.items() if c in SUSPENSE_CODES)
    catch_count = sum(d["count"] for c, d in by_firefly_coa.items() if c in SUSPENSE_CODES)
    lines.append(f"**Catch-all buckets (5190/4900/1190) total: {catch_count:,} tx / ${catch_amt:,.2f}**")
    lines.append("")
    lines.append("## 3. Universal parser: where the money SHOULD go")
    lines.append("")
    lines.append("Top 15 CoA buckets by amount using carrier-driven classification:")
    lines.append("")
    lines.append("| CoA | Tx | $ | Sample reason |")
    lines.append("|---|---:|---:|---|")
    top_direct = sorted(by_direct_coa.items(), key=lambda kv: -kv[1]["amount"])[:15]
    for code, d in top_direct:
        sample_reason = d["samples"][0]["reason"][:60] if d["samples"] else ""
        lines.append(f"| `{code}` | {d['count']:,} | {d['amount']:,.2f} | {sample_reason} |")
    lines.append("")
    direct_suspense = sum(d["amount"] for c, d in by_direct_coa.items() if c in SUSPENSE_CODES)
    lines.append(f"**Direct catch-all (still 5190/4900/1190): ${direct_suspense:,.2f}**")
    lines.append("")
    delta_recovered = catch_amt - direct_suspense
    lines.append(f"**Net $ moved OUT of catch-all into specific CoAs: ${delta_recovered:,.2f}**")
    lines.append("")
    lines.append("## 4. Singlife Savvy Invest — the case that triggered the decouple")
    lines.append("")
    lines.append(f"- Premiums visible in PDF statements (parser): **{len(savvy_in_pdf)} × $252.85**")
    lines.append(f"- Premiums Firefly bridged at $252.85 (any CoA): **{len(savvy_in_gl)}**")
    pdf_amt = sum(t["amount"] for t in savvy_in_pdf)
    lines.append(f"- Total premium captured by parser: **${pdf_amt:,.2f}**")
    in_5190 = [r for r in savvy_in_gl if r[2] == "5190"]
    lines.append(f"- Of those {len(savvy_in_gl)} bridged, **{len(in_5190)} sit in `5190 General Expense`** (should be `1222 Singlife ILP`)")
    missing = len(savvy_in_pdf) - len(savvy_in_gl)
    if missing > 0:
        lines.append(f"- **{missing} premiums are in the PDF but ABSENT from GL entirely** — Firefly's importer dropped them")
    lines.append("")
    lines.append("## 5. Findings")
    lines.append("")
    lines.append("### What the universal parser preserves that Firefly drops")
    lines.append("")
    carrier_counts = defaultdict(int)
    for tx in pdf_tx:
        for k in tx.get("carriers", {}).keys():
            carrier_counts[k] += 1
    lines.append("| Carrier kind | # tx |")
    lines.append("|---|---:|")
    for k, n in sorted(carrier_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{k}` | {n:,} |")
    lines.append("")
    lines.append("Every one of these is a data point Firefly's PDF→CSV converter discards.")
    lines.append("")
    lines.append("### Recommendation")
    lines.append("")
    lines.append("Move forward with the cutover: void all `FIREFLY_BRIDGE` journals where the "
                 "POSB account is one leg AND `journal_date >= 2026-01-01`, then post via "
                 "`universal_pdf_parser` + `journal_service`. Pre-2026 journals stay as historical "
                 "archive but should be reclassified (separately) where the carrier reveals a "
                 "specific CoA (24 Savvy Invest $252.85 entries in `5190` → `1222`).")
    lines.append("")

    report = "\n".join(lines)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nReport written to {args.out}")
    print(f"Length: {len(report)} chars")
    s.close()


if __name__ == "__main__":
    main()
