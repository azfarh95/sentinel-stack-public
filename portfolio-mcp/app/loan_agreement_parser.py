"""Loan agreement parser — extracts contract terms, seeds CreditFacility row.

Auto-populates the CreditFacility + PaymentSchedule tables from a loan
agreement PDF, replacing manual `seed_credit_db.py` entries. Idempotent:
re-running on the same agreement updates fields but doesn't create dupes
(facility_id is the unique key, derived from license + contract number).

Does NOT post journals. The disbursement journal is posted when funds hit
POSB (caught by firefly_bridge.py). This parser just creates the master row.

Run:
    docker exec portfolio-mcp python -m app.loan_agreement_parser <file.pdf>
    docker exec portfolio-mcp python -m app.loan_agreement_parser <folder> --apply
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

logger = logging.getLogger(__name__)


@dataclass
class ParsedLoanAgreement:
    lender_name: str
    lender_license: str | None
    lender_address: str | None
    lender_contact: str | None
    contract_number: str | None
    borrower_name: str | None
    nric: str | None
    principal: float
    disbursed: float                 # principal - admin_fee (cash in hand)
    admin_fee: float
    total_interest: float
    total_amount: float              # principal + interest
    instalment_amount: float
    num_instalments: int
    late_fee: float
    monthly_rate_pct: float
    annual_rate_pct: float
    signing_date: _date | None
    first_repayment_date: _date | None
    maturity_date: _date | None
    facility_id: str = ""           # derived: lender-slug + contract number
    coa_code: str = "2221"           # default moneylender; refined per lender
    source_path: str = ""
    parse_errors: list[str] = field(default_factory=list)


COA_BY_LENDER = {
    "ez loan": "2221",
    "lending bee": "2222",
    "sands credit": "2223",
}


def _money(s: str | None) -> float:
    if not s: return 0.0
    s = s.replace(",", "").replace("$", "").strip()
    try: return float(s)
    except Exception: return 0.0


def _parse_date(s: str) -> _date | None:
    for fmt in ("%d-%b-%Y", "%d/%m/%Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except Exception:
            pass
    return None


def detect_and_parse(pdf_path: str) -> ParsedLoanAgreement | None:
    text = ccp._extract_text_smart(pdf_path)
    is_loan = any(m in text.lower() for m in [
        "loan agreement", "licensed moneylender", "moneylenders act",
        "note of contract", "loan account no",
    ])
    if not is_loan:
        return None

    first_lines = text.split("\n", 6)
    lender_name = first_lines[0].strip() if first_lines else ""
    # Better: search for "<NAME> PTE LTD" or "Business Name <NAME>"
    m = re.search(r"Business Name\s+([A-Z][A-Z\s&,()-]+?)\s+(?:PTE\s+LTD|UEN|LOAN)", text)
    if m:
        lender_name = m.group(1).strip() + " PTE LTD"
    elif "PTE LTD" in text[:500].upper():
        m = re.search(r"^([A-Z][A-Z\s&,()-]+?\s+(?:PTE\s+LTD|PTE\.\s+LTD))", text, re.M | re.I)
        if m:
            lender_name = m.group(1).strip()

    # Detect lender for CoA mapping
    lender_key = ""
    for key in COA_BY_LENDER:
        if key in text[:500].lower():
            lender_key = key
            break
    coa = COA_BY_LENDER.get(lender_key, "2221")

    out = ParsedLoanAgreement(
        lender_name=lender_name, lender_license=None, lender_address=None,
        lender_contact=None, contract_number=None, borrower_name=None, nric=None,
        principal=0, disbursed=0, admin_fee=0, total_interest=0, total_amount=0,
        instalment_amount=0, num_instalments=0, late_fee=0,
        monthly_rate_pct=0, annual_rate_pct=0,
        signing_date=None, first_repayment_date=None, maturity_date=None,
        coa_code=coa, source_path=pdf_path,
    )

    m = re.search(r"Licensed Moneylender[\s:]*\(?(\d+/\d+)\)?", text, re.I)
    if m: out.lender_license = m.group(1)
    m = re.search(r"^(BLOCK\s+[^\n]+)", text, re.M)
    if m: out.lender_address = m.group(1).strip()
    m = re.search(r"Tel[\s:]*(\d{8,})", text)
    if m: out.lender_contact = m.group(1)

    # Borrower / NRIC
    m = re.search(r"I,?\s*([A-Z\s]+),\s*(S\d{7}[A-Z])\b", text)
    if m:
        out.borrower_name = m.group(1).strip()
        out.nric = m.group(2)

    # Money fields — multiple legal phrasings; take the LAST match (numeric summary
    # section usually appears late in the document with the canonical numbers)
    def last(pat: str) -> str | None:
        matches = re.findall(pat, text, re.I)
        return matches[-1] if matches else None

    # Allow both comma and no-comma formats (Sands omits comma in $5000.00)
    out.principal = _money(last(r"(?:Loan Amount|Principal)\s*:?\s*\$?\s*([\d,]*\d+\.\d{2})"))
    out.disbursed = _money(last(r"Disbursed Amount\s*:?\s*\$?\s*([\d,]*\d+\.\d{2})"))
    out.admin_fee = _money(last(r"(?:Administrative|Admin(?:in)?|Processing)\s+Fee\s*\(?\s*[\d.]*%?\)?\s*:?\s*\$?\s*([\d,]*\d+\.\d{2})"))
    out.total_interest = _money(last(r"Total Interest\s*:?\s*\$?\s*([\d,]*\d+\.\d{2})"))
    out.total_amount = _money(last(r"Total Amount\s*:?\s*\$?\s*([\d,]*\d+\.\d{2})"))
    # OCR-tolerant: "Instalment" sometimes mis-OCR'd as "Tnstalment", "lnstalment", etc.
    # "Amount" suffix is optional (EZ Loan: "Installment : $498.72"; Sands: "Tnstalment Amount : $530.19")
    out.instalment_amount = _money(last(r"[A-Za-z]?nstal[lm]?ment(?:\s*Amount)?\s*:?\s*\$?\s*([\d,]*\d+\.\d{2})"))
    out.late_fee = _money(last(r"(?:Late\s+Repayment\s+Fee|Late\s+Payment\s+Fee)[^\d]*\$?\s*([\d,]*\d+\.\d{2})"))

    m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*per\s*month", text, re.I)
    if m: out.monthly_rate_pct = float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*per\s*annum", text, re.I)
    if m: out.annual_rate_pct = float(m.group(1))

    # Number of instalments — prefer derivation (more robust to OCR garble),
    # fall back to regex when total_amount + instalment_amount unavailable.
    if out.instalment_amount > 0 and out.total_amount > 0:
        derived = round(out.total_amount / out.instalment_amount)
        if 6 <= derived <= 60:
            out.num_instalments = derived
    if out.num_instalments == 0:
        # OCR-tolerant: "Number of Instalments ; {2" → 12 ("{" is OCR misread of "1")
        m = re.search(r"Number\s+of\s+Instal[lm]?ments\s*[:;]?\s*[\{\[]?(\d{1,2})", text, re.I)
        if m:
            out.num_instalments = int(m.group(1))
        else:
            m = re.search(r"\b(\d{1,2})\s*(?:monthly|equal)\s*(?:installments?|instalments?)", text, re.I)
            if m:
                out.num_instalments = int(m.group(1))

    # Signing date — first "07-Jan-2026" style date in text
    m = re.search(r"\b(\d{1,2}[\-/\s][A-Za-z]{3}[\-/\s]\d{4})\b", text)
    if m:
        out.signing_date = _parse_date(m.group(1))

    # Contract number — try "Account No: XXX" / "Contract No: XXX" / filename
    m = re.search(r"(?:Loan\s+Account\s+No|Account\s+No|Contract\s+No|Loan\s+No)[\.\s:;]+([A-Z0-9\-/]+)", text, re.I)
    if m:
        out.contract_number = m.group(1).rstrip("/-.")
    else:
        # Fall back to filename stem prefix before " - LOAN AGREEMENT"
        stem = Path(pdf_path).stem
        m = re.match(r"^([A-Z0-9\-]+)", stem)
        if m:
            out.contract_number = m.group(1).rstrip("-")

    # facility_id slug
    if out.contract_number and lender_key:
        out.facility_id = f"{lender_key.replace(' ', '-')}-{out.contract_number}"
    elif lender_key:
        out.facility_id = lender_key.replace(' ', '-')

    # Sanity: disbursed + admin_fee should = principal (when both present)
    if out.principal > 0 and out.disbursed > 0 and out.admin_fee > 0:
        diff = abs(out.principal - out.disbursed - out.admin_fee)
        if diff > 0.50:
            out.parse_errors.append(
                f"principal-disbursed-fee mismatch: {out.principal:.2f} != "
                f"{out.disbursed:.2f} + {out.admin_fee:.2f} ({diff:+.2f})"
            )
    return out


# ── DB seed ────────────────────────────────────────────────────────────────


def upsert_facility(s, parsed: ParsedLoanAgreement) -> str:
    """Upsert into CreditFacility table. Returns facility_id."""
    if not parsed.facility_id:
        raise ValueError("cannot upsert without facility_id")
    fac = s.get(db.CreditFacility, parsed.facility_id)
    now = datetime.now()
    if fac is None:
        fac = db.CreditFacility(id=parsed.facility_id, created_at=now)
        s.add(fac)
    fac.updated_at = now
    fac.lender_name = parsed.lender_name
    fac.lender_license = parsed.lender_license
    fac.lender_address = parsed.lender_address
    fac.lender_contact = parsed.lender_contact
    fac.facility_type = "moneylender_loan"
    fac.account_number = parsed.contract_number
    fac.origination_date = parsed.signing_date and datetime.combine(parsed.signing_date, datetime.min.time())
    fac.principal_amount = parsed.principal
    fac.disbursed_amount = parsed.disbursed
    fac.admin_fee = parsed.admin_fee
    fac.nominal_monthly_pct = parsed.monthly_rate_pct or None
    fac.interest_basis = "reducing_balance"
    fac.late_fee = parsed.late_fee or None
    fac.num_instalments = parsed.num_instalments or None
    fac.instalment_amount = parsed.instalment_amount or None
    fac.status = "active"
    fac.agreement_document_path = parsed.source_path.replace(
        "/onedrive/Sentinel Finance/", "")
    return parsed.facility_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="Loan agreement PDF or folder")
    ap.add_argument("--apply", action="store_true",
                    help="Actually upsert into CreditFacility DB")
    args = ap.parse_args()

    target = Path(args.target)
    files = []
    if target.is_dir():
        files = sorted(target.rglob("*LOAN AGREEMENT*.pdf"))
    else:
        files = [target]

    db.init_db()
    sess = db.SessionLocal() if args.apply else None
    print(f"{'File':<40} {'Lender':<22} {'Principal':>10} {'Disbursed':>10} "
          f"{'Fee':>7} {'Inst':>9} {'#':>3} ID")
    print("-" * 130)
    try:
        for f in files:
            p = detect_and_parse(str(f))
            if not p:
                print(f"  {f.name[:38]:<40} (not loan agreement)")
                continue
            facility_id = ""
            if args.apply and sess:
                try:
                    facility_id = upsert_facility(sess, p)
                    sess.commit()
                except Exception as e:
                    facility_id = f"ERR:{str(e)[:25]}"
                    sess.rollback()
            else:
                facility_id = p.facility_id or "(no id)"
            print(f"  {f.name[:38]:<40} {p.lender_name[:20]:<22} "
                  f"{p.principal:>10,.2f} {p.disbursed:>10,.2f} {p.admin_fee:>7,.2f} "
                  f"{p.instalment_amount:>9,.2f} {p.num_instalments:>3} {facility_id}")
            for e in p.parse_errors:
                print(f"      ⚠ {e}")
    finally:
        if sess:
            sess.close()


if __name__ == "__main__":
    main()
