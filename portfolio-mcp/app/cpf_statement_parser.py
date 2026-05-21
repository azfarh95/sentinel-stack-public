"""CPF statement parser — Transaction history / Monthly contribution / NOA.

CPF Board PDFs are the AUTHORITY for CPF asset movements. This parser:

  1. Extracts every transaction row (CON / INV / INT / DPS / CSL / MSL / PMI / SUP / BAL)
  2. Posts journals for non-contribution movements (INV, INT, insurance deductions)
  3. Provides reconciliation helpers for CON entries (cross-check vs payslip-derived
     expectations)

Contribution rows (CON) are deliberately NOT auto-posted — they overlap with the
payslip parser's CPF asset legs. Reconciliation logic surfaces variances instead.

Codes (subset; full list in CPF Board appendix):
  CON  Contributions / Government Cash Grant / Government Top-up
  INV  CPF Investment Sch transfer (negative = OA → IS)
  INT  Annual interest credit (Dec 31)
  DPS  Dependants' Protection Scheme premium (deduction from OA)
  CSL  CareShield Life premium (deduction from MA)
  MSL  Medishield Life premium (deduction from MA)
  PMI  Private Medisave Insurance premium (deduction from MA)
  SUP  Supplementary Retirement Scheme / other supplementary movement
  BAL  Balance marker — period bookend, NOT a transaction

Run:
    docker exec portfolio-mcp python -m app.cpf_statement_parser <file.pdf>
    docker exec portfolio-mcp python -m app.cpf_statement_parser <file> --post
"""
from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass, field
from datetime import date as _date, datetime
from pathlib import Path

from . import cc_statement_parser as ccp
from . import database as db
from . import journal_service as js

logger = logging.getLogger(__name__)


@dataclass
class CPFRow:
    txn_date: _date
    code: str                # CON | INV | INT | DPS | CSL | MSL | PMI | SUP | BAL | ...
    mth_year: str            # "NOV 2024" / "" (for INT/BAL)
    ref: str                 # A/B/C (cross-referenced to employer below) / ""
    oa: float                # signed (positive = inflow to OA)
    sa: float
    ma: float
    raw: str = ""

    @property
    def total(self) -> float:
        return self.oa + self.sa + self.ma


@dataclass
class ParsedCPFStatement:
    member_name: str
    member_nric: str
    period_start: _date | None
    period_end: _date | None
    statement_date: _date | None
    rows: list[CPFRow] = field(default_factory=list)
    ref_to_employer: dict[str, str] = field(default_factory=dict)
    source_path: str = ""
    parse_errors: list[str] = field(default_factory=list)

    def summarize(self) -> dict[str, dict[str, float]]:
        """Sum per (code, account) totals."""
        out: dict[str, dict[str, float]] = {}
        for r in self.rows:
            d = out.setdefault(r.code, {"OA": 0, "SA": 0, "MA": 0})
            d["OA"] += r.oa
            d["SA"] += r.sa
            d["MA"] += r.ma
        return out

    def statement_id(self) -> str:
        s = self.period_start.isoformat() if self.period_start else "nodate"
        e = self.period_end.isoformat() if self.period_end else "nodate"
        return f"cpf|{self.member_nric}|{s}|{e}"


def _money(s: str | None) -> float:
    if not s: return 0.0
    s = s.replace(",", "").replace("$", "").strip()
    is_neg = s.startswith("-")
    s = s.lstrip("-")
    try:
        v = float(s)
        return -v if is_neg else v
    except Exception:
        return 0.0


def _parse_date(s: str) -> _date | None:
    s = s.strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None


CODES = ["BAL", "CON", "INV", "INT", "DPS", "CSL", "MSL", "PMI", "SUP",
         "ADJ", "GOV", "HSE", "HPR", "HPS", "HPC", "EDN", "CLA", "CLI", "CLR",
         "CSA", "ESH", "ESB", "AMP", "INS", "MED"]


def detect_and_parse(pdf_path: str) -> ParsedCPFStatement | None:
    text = ccp._extract_text_smart(pdf_path)
    if "cpf" not in text[:400].lower():
        return None
    if "central provident fund" not in text.lower() and "cpf account number" not in text.lower():
        return None

    out = ParsedCPFStatement(
        member_name="", member_nric="", period_start=None, period_end=None,
        statement_date=None, source_path=pdf_path,
    )
    m = re.search(r"^([A-Z][A-Z\s]+?)\n\(CPF Account Number\s*:\s*(S\d{7}[A-Z])\)", text, re.M)
    if m:
        out.member_name = m.group(1).strip()
        out.member_nric = m.group(2)
    m = re.search(r"(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})\s+\d{1,2}:\d{2}\s*(?:AM|PM)", text)
    if m:
        out.statement_date = _parse_date(m.group(1))
    m = re.search(r"\(From\s+(\d{1,2}\s+\w+\s+\d{4})\s+to\s+(\d{1,2}\s+\w+\s+\d{4})\)", text)
    if m:
        out.period_start = _parse_date(m.group(1))
        out.period_end = _parse_date(m.group(2))

    # Row pattern: "13 Dec 2024 CON NOV 2024 A 717.45 187.06 249.49"
    # OR:          "31 Dec 2025 INT 504.93 945.93 1,065.35"
    # OR:          "05 Apr 2025 INV -2.18 0.00 0.00"
    # OR:          "01 Mar 2025 BAL 20,066.95 14,265.85 20,915.97"
    code_alt = "|".join(CODES)
    row_re = re.compile(
        rf"^(\d{{1,2}}\s+[A-Z][a-z]{{2}}\s+\d{{4}})\s+({code_alt})\s+"
        rf"(?:(\w{{3}}\s+\d{{4}})\s+)?"        # optional MTH YYYY (CON-only)
        rf"(?:([A-Z])\s+)?"                     # optional ref letter (CON-only)
        rf"(-?[\d,]+\.\d{{2}})\s+(-?[\d,]+\.\d{{2}})\s+(-?[\d,]+\.\d{{2}})",
        re.M,
    )
    for m in row_re.finditer(text):
        date_s, code, mth_year, ref, oa, sa, ma = m.groups()
        d = _parse_date(date_s)
        if d is None:
            continue
        out.rows.append(CPFRow(
            txn_date=d, code=code, mth_year=mth_year or "", ref=ref or "",
            oa=_money(oa), sa=_money(sa), ma=_money(ma),
            raw=m.group(0).strip(),
        ))

    # Ref → employer mapping at the bottom: "REF A : AZ UNITED PTE. LTD."
    for m in re.finditer(r"REF\s+([A-Z])\s*:\s*([A-Z][A-Z\s\.&,()-]+?)(?=\n|$)", text):
        out.ref_to_employer[m.group(1)] = m.group(2).strip()

    if not out.rows:
        out.parse_errors.append("no rows matched — CPF format may have changed")
    return out


# ── Journal posting (selective: only non-CON entries) ──────────────────────


CPF_OA = "1211"; CPF_SA = "1212"; CPF_MA = "1213"
CPF_IS = "12149"   # Unallocated CPF IS leaf — INV-type movements go here when fund not identified
INVESTMENT_INCOME = "4500"        # CPF interest = investment income
INSURANCE_EXPENSE = "5320"        # MediShield / CareShield / DPS premiums
SRS_ASSET = "1219"                # placeholder if user has SRS


def post_cpf_row_journal(s, parsed: ParsedCPFStatement, row: CPFRow) -> int | None:
    """Post one CPF row as a journal. Skips CON (handled by payslip), BAL (marker)."""
    if row.code in ("CON", "BAL"):
        return None
    # Compose journal date + idempotency ext
    ext = f"cpf:{parsed.member_nric}:{row.txn_date.isoformat()}:{row.code}:{row.total:.2f}"
    narration_base = f"CPF {row.code} {row.txn_date.isoformat()}"

    lines = []
    if row.code == "INV":
        # OA → IS transfer (oa is negative)
        amt = abs(row.oa)
        if amt == 0:
            return None
        lines = [
            {"account_code": CPF_IS, "debit": amt,
             "narration": f"{narration_base} (OA→IS transfer)"},
            {"account_code": CPF_OA, "credit": amt,
             "narration": f"{narration_base} (OA outflow to IS)"},
        ]
    elif row.code == "INT":
        # Annual interest credit
        if row.oa > 0:
            lines.append({"account_code": CPF_OA, "debit": row.oa,
                          "narration": f"{narration_base} OA interest"})
        if row.sa > 0:
            lines.append({"account_code": CPF_SA, "debit": row.sa,
                          "narration": f"{narration_base} SA interest"})
        if row.ma > 0:
            lines.append({"account_code": CPF_MA, "debit": row.ma,
                          "narration": f"{narration_base} MA interest"})
        total = row.oa + row.sa + row.ma
        if total > 0:
            lines.append({"account_code": INVESTMENT_INCOME, "credit": total,
                          "narration": f"{narration_base} (CPF interest income)"})
    elif row.code in ("DPS", "CSL", "MSL", "PMI"):
        # Insurance premiums — deduction from CPF
        total_deduct = abs(row.oa) + abs(row.sa) + abs(row.ma)
        if total_deduct == 0:
            return None
        if row.oa < 0:
            lines.append({"account_code": CPF_OA, "credit": abs(row.oa),
                          "narration": f"{narration_base} OA deduction"})
        if row.sa < 0:
            lines.append({"account_code": CPF_SA, "credit": abs(row.sa),
                          "narration": f"{narration_base} SA deduction"})
        if row.ma < 0:
            lines.append({"account_code": CPF_MA, "credit": abs(row.ma),
                          "narration": f"{narration_base} MA deduction"})
        lines.append({"account_code": INSURANCE_EXPENSE, "debit": total_deduct,
                      "narration": f"{narration_base} ({row.code} premium)"})
    else:
        # Other codes — defer (no journal posted)
        return None

    if not lines:
        return None
    return js.post_journal(
        s,
        journal_date=row.txn_date,
        narration=narration_base,
        journal_type="cpf_movement",
        lines=lines,
        source_doc="CPF_STMT",
        source_ref=parsed.statement_id(),
        external_id=ext,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="CPF statement PDF or folder")
    ap.add_argument("--post", action="store_true",
                    help="Post non-CON journals (INV, INT, insurance deductions)")
    args = ap.parse_args()

    target = Path(args.target)
    files = sorted(target.glob("*.pdf")) if target.is_dir() else [target]

    db.init_db()
    sess = db.SessionLocal() if args.post else None
    posted = 0
    try:
        for f in files:
            p = detect_and_parse(str(f))
            if not p:
                continue
            print(f"\n=== {f.name} ===")
            print(f"  Member: {p.member_name} ({p.member_nric})")
            print(f"  Period: {p.period_start} → {p.period_end}")
            print(f"  Refs: {p.ref_to_employer}")
            print(f"  Rows: {len(p.rows)}")
            summary = p.summarize()
            print(f"\n  {'Code':<5} {'OA':>12} {'SA':>10} {'MA':>10} {'count':>6}")
            print("  " + "-" * 50)
            counts: dict[str, int] = {}
            for r in p.rows:
                counts[r.code] = counts.get(r.code, 0) + 1
            for code, totals in sorted(summary.items()):
                print(f"  {code:<5} {totals['OA']:>12,.2f} {totals['SA']:>10,.2f} "
                      f"{totals['MA']:>10,.2f} {counts[code]:>6}")
            if args.post and sess:
                for r in p.rows:
                    try:
                        jid = post_cpf_row_journal(sess, p, r)
                        if jid:
                            posted += 1
                    except Exception as e:
                        logger.warning("CPF row post failed %s: %s", r.raw, e)
                        sess.rollback()
                sess.commit()
            for e in p.parse_errors:
                print(f"  ⚠ {e}")
        if args.post:
            print(f"\nTotal journals posted: {posted}")
    finally:
        if sess:
            sess.close()


if __name__ == "__main__":
    main()
