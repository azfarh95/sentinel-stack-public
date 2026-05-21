"""Payslip parser — extracts earnings + deductions, posts salary journal.

Supports formats:
  - AZ United Pte Ltd (current employer 2025+)
  - YourAgency (prior employer)
  - Ganesan (prior employer — important: NEVER paid CPF, see Ganesan-unpaid-CPF memory)
  - HSS (deployment-based payroll)

Journal model:
  DR POSB Savings           net pay
  DR CPF OA + SA + MA       employee CPF (per CPF Board allocation %)
  CR Salary Income          gross pay
  +
  DR CPF OA + SA + MA       employer CPF
  CR Employer CPF Income    employer CPF (revenue side)

Idempotent: external_id = `payslip:<employer>:<YYYY-MM>`.

Run:
    docker exec portfolio-mcp python -m app.payslip_parser <file.pdf>     # parse-only
    docker exec portfolio-mcp python -m app.payslip_parser <folder>       # batch parse
    docker exec portfolio-mcp python -m app.payslip_parser <file> --post  # parse + journal
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


# CPF allocation table for age 30 (user's current bracket; CoA 1211/1212/1213
# = OA/SA/MA). Refine per age band if needed.
# Source: CPF Board contribution allocation rates (2026, age ≤35).
CPF_ALLOC_PCT = {"OA": 0.6217, "SA": 0.1621, "MA": 0.2162}
# Note: sum = 1.0 (allocations of TOTAL CPF — employee + employer combined)

EMPLOYER_NAME_TO_KEY = {
    "AZ United Pte Ltd": "az_united",
    "YourAgency": "youragency",
    "Ganesan": "ganesan",
    "HSS Engineers": "hss",
    "HSS Engineering": "hss",
}


@dataclass
class ParsedPayslip:
    employer: str                 # canonical employer name
    employer_key: str             # lower-case slug
    employee_name: str
    period_start: _date | None
    period_end: _date | None
    payment_date: _date | None
    basic_pay: float
    allowances: float             # OT, bonuses, etc. (sum of non-basic earnings)
    employee_cpf: float           # deducted from gross
    fund_deductions: float        # MBMF, SINDA, etc.
    other_deductions: float
    employer_cpf: float
    sdl: float                    # Skills Development Levy (employer-side)
    net_pay: float
    gross_pay: float              # basic + allowances
    source_path: str = ""
    parse_errors: list[str] = field(default_factory=list)

    def statement_id(self) -> str:
        d = self.period_end.isoformat() if self.period_end else "nodate"
        return f"payslip|{self.employer_key}|{d}"


def _money(s: str | None) -> float:
    if not s:
        return 0.0
    s = s.replace(",", "").replace("$", "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0


def _parse_date(s: str, fmts=("%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y")) -> _date | None:
    s = s.strip()
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None


def _parse_youragency(text: str, pdf_path: str) -> ParsedPayslip:
    """YourAgency daily-rated payslip — multiple weekly slips per PDF.

    Aggregates ALL weekly slips in the PDF into one monthly-equivalent row
    keyed by period_end = latest "Payment Date" in PDF. Each slip has:
      TOTAL BASIC SALARY : <amt>
      OT AMOUNT : <amt>
      ALLOWANCES : <amt>
      MISC. PAYMENTS : <amt>
      LESS EMPLOYEE CPF : <amt>
      LESS ADV / LOAN : <amt>
      TAKE HOME PAY : <amt>
    """
    # Extract each weekly slip via "TOTAL BASIC SALARY" anchor
    weeks = []
    for m in re.finditer(
        r"TOTAL BASIC SALARY\s*:\s*(?P<basic>[\d,]+\.\d{2})"
        r".*?OT AMOUNT\s*:\s*(?P<ot>[\d,]+\.\d{2})"
        r".*?ALLOWANCES\s*:\s*(?P<allow>[\d,]+\.\d{2})"
        r".*?MISC\.\s*PAYMENTS?\s*:\s*(?P<misc>[\d,]+\.\d{2})"
        r".*?LESS EMPLOYEE CPF\s*:\s*(?P<ecpf>[\d,]+\.\d{2})"
        r".*?LESS ADV\s*/\s*LOAN\s*:\s*(?P<loan>[\d,]+\.\d{2})"
        r".*?TAKE HOME PAY\s*:\s*(?P<net>[\d,]+\.\d{2})"
        r".*?Payment Date\s*:\s*(?P<paydate>\d{1,2}/\d{1,2}/\d{4})?",
        text, re.S | re.I):
        weeks.append({
            "basic": _money(m.group("basic")),
            "ot": _money(m.group("ot")),
            "allow": _money(m.group("allow")),
            "misc": _money(m.group("misc")),
            "ecpf": _money(m.group("ecpf")),
            "loan": _money(m.group("loan")),
            "net": _money(m.group("net")),
            "paydate": _parse_date(m.group("paydate")) if m.group("paydate") else None,
        })

    if not weeks:
        # parse failed
        out = ParsedPayslip(
            employer="YourAgency", employer_key="youragency",
            employee_name="", period_start=None, period_end=None, payment_date=None,
            basic_pay=0, allowances=0, employee_cpf=0, fund_deductions=0,
            other_deductions=0, employer_cpf=0, sdl=0, net_pay=0, gross_pay=0,
            source_path=pdf_path,
        )
        out.parse_errors.append("YourAgency regex matched no weeks")
        return out

    # Fallback when "Payment Date" header is absent (older PDFs): use the
    # last "Site Date" entry in the document body as a proxy for the period end.
    paydates = [w["paydate"] for w in weeks if w["paydate"]]
    if not paydates:
        sd_matches = re.findall(r"\d{4}A?\s+(\d{1,2}/\d{1,2}/\d{4})\s+\d{2}:\d{2}", text)
        if sd_matches:
            try:
                last_work_date = max(_parse_date(d) for d in sd_matches if _parse_date(d))
                # Payment is typically the Wednesday/Thursday after — bump by 7d
                if last_work_date:
                    from datetime import timedelta as _td
                    paydates = [last_work_date + _td(days=7)]
            except Exception:
                pass
    out = ParsedPayslip(
        employer="YourAgency", employer_key="youragency",
        employee_name="",
        period_start=min(paydates) if paydates else None,
        period_end=max(paydates) if paydates else None,
        payment_date=max(paydates) if paydates else None,
        basic_pay=sum(w["basic"] for w in weeks),
        allowances=sum(w["ot"] + w["allow"] + w["misc"] for w in weeks),
        employee_cpf=sum(w["ecpf"] for w in weeks),
        fund_deductions=0.0,
        other_deductions=sum(w["loan"] for w in weeks),
        employer_cpf=0.0,        # YourAgency payslip doesn't show employer CPF
        sdl=0.0,
        net_pay=sum(w["net"] for w in weeks),
        gross_pay=0.0,
        source_path=pdf_path,
    )
    out.gross_pay = out.basic_pay + out.allowances
    # YourAgency reconciliation: gross - ecpf - other_ded should equal net.
    # Any gap (typically ethnic-fund / unparsed deductions) → plug other_deductions.
    expected_net = out.gross_pay - out.employee_cpf - out.other_deductions
    gap = round(expected_net - out.net_pay, 2)
    if abs(gap) > 0.01:
        out.other_deductions = round(out.other_deductions + gap, 2)
        out.parse_errors.append(
            f"reconciliation plug: +{gap:.2f} to other_deductions (gross/ecpf/loan/net mismatch)")
    return out


def detect_and_parse(pdf_path: str) -> ParsedPayslip | None:
    text = ccp._extract_text_smart(pdf_path)
    if not text or "payslip" not in text.lower():
        return None

    # YourAgency uses a different format: DAILY RATED PAYSLIP with multiple weekly
    # slips concatenated. Dispatch to dedicated parser when detected.
    if "youragency security" in text[:500].lower() or "daily rated payslip" in text[:500].lower():
        return _parse_youragency(text, pdf_path)

    employer_name = "Unknown"
    employer_key = "unknown"
    head = text[:300]
    for canonical, key in EMPLOYER_NAME_TO_KEY.items():
        if canonical.lower() in head.lower():
            employer_name, employer_key = canonical, key
            break

    out = ParsedPayslip(
        employer=employer_name, employer_key=employer_key,
        employee_name="", period_start=None, period_end=None, payment_date=None,
        basic_pay=0, allowances=0, employee_cpf=0, fund_deductions=0,
        other_deductions=0, employer_cpf=0, sdl=0, net_pay=0, gross_pay=0,
        source_path=pdf_path,
    )

    m = re.search(r"Employee Name\s*:\s*([A-Za-z\s]+?)\s*(?:Payslip Period|Designation|$)",
                  text, re.M)
    if m:
        out.employee_name = m.group(1).strip()

    m = re.search(r"Payslip Period\s*:\s*(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})", text)
    if m:
        out.period_start = _parse_date(m.group(1))
        out.period_end = _parse_date(m.group(2))
    m = re.search(r"Payment Date\s*:\s*(\d{2}/\d{2}/\d{4})", text)
    if m:
        out.payment_date = _parse_date(m.group(1))

    # AZ United format: each row is "Basic Pay $ 3,200.00" (earnings on left) or
    # "Employee CPF $ 640.00" (deductions on right).
    # Most reliable extraction: search by label.
    def find(pattern: str) -> float:
        m = re.search(pattern, text, re.I)
        return _money(m.group(1)) if m else 0.0

    # Use Total Earnings + Total Deductions as the canonical totals.
    # AZ United layout puts EARNINGS column on left, DEDUCTIONS column on right.
    # PDF text extraction concatenates rows L→R so we can't reliably split by
    # spatial column — instead trust the explicit totals and back-fill components.
    total_earnings = find(r"Total Earnings\s*\$?\s*([\d,]+\.\d{2})")
    total_deductions = find(r"Total Deductions\s*\$?\s*([\d,]+\.\d{2})")

    out.basic_pay = find(r"Basic Pay\s*\$?\s*([\d,]+\.\d{2})")

    # Known EARNINGS labels (whitelist). Sum these for explicit allowances.
    earnings_labels = [
        r"Annual Wage\s*\n?\s*\$?\s*([\d,]+\.\d{2})\s*(?:Supplement|Bonus)",
        r"Annual Wage Supplement[^\$]*\$?\s*([\d,]+\.\d{2})",
        r"Overtime\s*\$?\s*([\d,]+\.\d{2})",
        r"Medical Claim\s*\$?\s*([\d,]+\.\d{2})",
        r"Transport\s*(?:Allowance)?\s*\$?\s*([\d,]+\.\d{2})",
        r"\bAllowance\b\s*\$?\s*([\d,]+\.\d{2})",
        r"Bonus\s*\$?\s*([\d,]+\.\d{2})",
        r"Commission\s*\$?\s*([\d,]+\.\d{2})",
    ]
    for pat in earnings_labels:
        for m in re.finditer(pat, text, re.I):
            out.allowances += _money(m.group(1))

    # Reconcile: if Total Earnings explicitly given, trust it
    if total_earnings > 0:
        explicit = out.basic_pay + out.allowances
        if abs(explicit - total_earnings) > 0.50:
            # Adjust allowances to reconcile to total
            out.allowances = max(0.0, total_earnings - out.basic_pay)

    out.employee_cpf = find(r"Employee CPF\s*\$?\s*([\d,]+\.\d{2})")
    # MBMF / SINDA / CDAC / ECF / Mosque Build Fund
    for m in re.finditer(r"(?:Fund\s*\([A-Z]+\)|MBMF|SINDA|CDAC|ECF|Mosque)\s*\$?\s*([\d,]+\.\d{2})", text):
        out.fund_deductions += _money(m.group(1))
    # Other known deductions (NS, Medical, Salary Advance, Loan Repayment)
    for pat in [
        r"National Service\s*\$?\s*([\d,]+\.\d{2})",
        r"Salary Advance\s*\$?\s*([\d,]+\.\d{2})",
        r"Loan Repayment\s*\$?\s*([\d,]+\.\d{2})",
    ]:
        for m in re.finditer(pat, text, re.I):
            out.other_deductions += _money(m.group(1))

    out.employer_cpf = find(r"Employer CPF\s*\$?\s*([\d,]+\.\d{2})")
    out.sdl = find(r"\bSDL\b\s*\$?\s*([\d,]+\.\d{2})")
    out.net_pay = find(r"NET PAY\s*\$?\s*([\d,]+\.\d{2})")
    out.gross_pay = out.basic_pay + out.allowances

    # Sanity: net + employee_cpf + fund + other = gross? Often the case for AZ United.
    if out.net_pay and out.gross_pay:
        expected_net = out.gross_pay - out.employee_cpf - out.fund_deductions - out.other_deductions
        if abs(expected_net - out.net_pay) > 0.50:
            out.parse_errors.append(
                f"net pay variance: expected {expected_net:.2f}, got {out.net_pay:.2f} "
                f"(Δ={out.net_pay - expected_net:+.2f}) — likely missing deduction line"
            )

    return out


# ── Journal posting ────────────────────────────────────────────────────────


CPF_OA = "1211"
CPF_SA = "1212"
CPF_MA = "1213"
POSB = "1111"
# Per-employer salary income accounts (already seeded in CoA)
SALARY_INCOME_BY_EMPLOYER = {
    "az_united":  "4110",  # "Salary — AZ United"
    "youragency":  "4120",  # "Salary — YourAgency Security"
    "ganesan":    "4110",  # fallback to AZ United bucket until 4130 seeded
    "hss":        "4110",  # fallback
    "unknown":    "4110",
}
FUND_EXPENSE = "5500"         # Tax/statutory category (closest existing) — TODO add 5510 Statutory funds in CoA seed


def post_payslip_journal(s, parsed: ParsedPayslip) -> int | None:
    """Post the salary journal. Idempotent via external_id."""
    if parsed.payment_date is None or parsed.gross_pay == 0:
        logger.warning("payslip lacks payment_date or gross_pay; skipping post")
        return None

    salary_coa = SALARY_INCOME_BY_EMPLOYER.get(parsed.employer_key, "4110")
    lines = []
    # Asset side
    lines.append({"account_code": POSB, "debit": parsed.net_pay,
                  "narration": f"Net pay: {parsed.employer}"})
    # CPF (employee + employer combined) → split by allocation %
    total_cpf = parsed.employee_cpf + parsed.employer_cpf
    if total_cpf > 0:
        for sub, pct, coa in [("OA", CPF_ALLOC_PCT["OA"], CPF_OA),
                              ("SA", CPF_ALLOC_PCT["SA"], CPF_SA),
                              ("MA", CPF_ALLOC_PCT["MA"], CPF_MA)]:
            lines.append({
                "account_code": coa,
                "debit": round(total_cpf * pct, 2),
                "narration": f"CPF→{sub} (employee + employer)",
            })
    # Fund deductions (MBMF/SINDA/CDAC) — statutory expense
    if parsed.fund_deductions > 0:
        lines.append({"account_code": FUND_EXPENSE, "debit": parsed.fund_deductions,
                      "narration": "Statutory fund deduction (MBMF/SINDA/CDAC)"})
    # Other deductions (NS, salary advance, loan repayment) — expense bucket
    if parsed.other_deductions > 0:
        lines.append({"account_code": "5190", "debit": parsed.other_deductions,
                      "narration": "Other payroll deduction"})
    # Total compensation income (gross + employer CPF benefit) → revenue
    total_income = parsed.gross_pay + parsed.employer_cpf
    lines.append({"account_code": salary_coa, "credit": total_income,
                  "narration": f"Gross salary + employer CPF: {parsed.employer} "
                               f"({parsed.period_start} to {parsed.period_end})"})

    # Rounding tolerance for split allocation (allocation % don't sum to exact 100)
    drs = sum(l.get("debit", 0) for l in lines)
    crs = sum(l.get("credit", 0) for l in lines)
    diff = round(drs - crs, 2)
    if 0 < abs(diff) <= 0.05:
        # Plug rounding into the CPF OA leg
        for l in lines:
            if l.get("account_code") == CPF_OA and l.get("debit", 0) > 0:
                l["debit"] = round(l["debit"] - diff, 2)
                break

    ext = f"payslip:{parsed.employer_key}:{parsed.payment_date.strftime('%Y-%m')}"
    jid = js.post_journal(
        s,
        journal_date=parsed.payment_date,
        narration=f"Salary {parsed.employer} {parsed.period_end.strftime('%b %Y') if parsed.period_end else ''}",
        journal_type="salary",
        lines=lines,
        source_doc="PAYSLIP",
        source_ref=parsed.statement_id(),
        external_id=ext,
    )
    # Upsert payslip_registry (idempotent — unique on employer_key + period_end)
    try:
        _upsert_payslip_registry(s, parsed, jid)
    except Exception as e:
        logger.warning("payslip_registry upsert failed: %s", e)
    return jid


def _upsert_payslip_registry(s, parsed: ParsedPayslip, journal_id: int | None) -> None:
    from . import ledger
    from sqlalchemy import select
    if not parsed.period_end:
        return
    existing = s.execute(
        select(ledger.PayslipRegistry).where(
            ledger.PayslipRegistry.employer_key == parsed.employer_key,
            ledger.PayslipRegistry.period_end == parsed.period_end,
        )
    ).scalar_one_or_none()
    now = db.now_utc()
    src = parsed.source_path.replace("/onedrive/Sentinel Finance/", "") if parsed.source_path else None
    fields = dict(
        employer=parsed.employer, employer_key=parsed.employer_key,
        period_start=parsed.period_start, period_end=parsed.period_end,
        payment_date=parsed.payment_date,
        basic_pay=parsed.basic_pay, allowances=parsed.allowances,
        gross_pay=parsed.gross_pay, employee_cpf=parsed.employee_cpf,
        employer_cpf=parsed.employer_cpf, fund_deductions=parsed.fund_deductions,
        other_deductions=parsed.other_deductions, sdl=parsed.sdl, net_pay=parsed.net_pay,
        journal_id=journal_id, source_path=src,
        parsed_at=now, updated_at=now,
    )
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
    else:
        s.add(ledger.PayslipRegistry(created_at=now, **fields))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="Payslip PDF or folder of PDFs")
    ap.add_argument("--post", action="store_true", help="Post journal to GL")
    args = ap.parse_args()

    target = Path(args.target)
    files = []
    if target.is_dir():
        files = sorted(f for f in target.rglob("*.pdf") if "payslip" in f.name.lower())
    else:
        files = [target]

    db.init_db()
    s = db.SessionLocal() if args.post else None
    print(f"{'File':<48} {'Employer':<22} {'Period':<22} {'Gross':>9} {'EmpCPF':>8} {'ErCPF':>8} {'Net':>9} JID")
    print("-" * 130)
    try:
        for f in files:
            p = detect_and_parse(str(f))
            if not p:
                print(f"  {f.name[:46]:<48} (not a payslip)")
                continue
            period = f"{p.period_start} → {p.period_end}" if p.period_start else "?"
            jid = ""
            if args.post and s:
                try:
                    jid = post_payslip_journal(s, p) or "skipped"
                    s.commit()
                except Exception as e:
                    jid = f"ERR:{str(e)[:30]}"
                    s.rollback()
            print(f"  {f.name[:46]:<48} {p.employer[:20]:<22} {period:<22} "
                  f"{p.gross_pay:>9,.2f} {p.employee_cpf:>8,.2f} {p.employer_cpf:>8,.2f} "
                  f"{p.net_pay:>9,.2f} {jid}")
            if p.parse_errors:
                for e in p.parse_errors:
                    print(f"      ⚠ {e}")
    finally:
        if s:
            s.close()


if __name__ == "__main__":
    main()
