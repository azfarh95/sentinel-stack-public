"""Top-level document classifier — multi-category dispatcher.

Given a file (PDF / CSV / image), determines which "pile" it belongs to:
  - cc_statement (delegates sub-bank detection to existing cc_statement_parser)
  - bank_statement (POSB / Maybank / SC / Ar Rihla)
  - loan_agreement (one-time docs that seed CreditFacility rows)
  - ilp_statement (Tokio Marine / Singlife Savvy Invest)
  - cpf_statement (annual contribution / monthly tx history / NOA)
  - payslip (AZ United / YourAgency / Ganesan / HSS)
  - noa_tax (NOA / IRAS docs)
  - insurance_policy (Singlife / Tokio policy schedule — static reference docs)
  - crypto_report (Coinbase CSV / exchange reports)
  - noise (application forms, acknowledgements, marketing — file but no journal)
  - unknown (confidence < threshold → queue for manual review)

Rules are ORDERED — first match wins. Each rule combines filename pattern +
first-page-text markers + anti-markers. Confidence reflects strength of match.

Output: ClassifierResult with target_folder + canonical_filename so caller can
move the file to its pile.

Used by inbox_pipeline.py. Standalone CLI for testing:
    docker exec portfolio-mcp python -m app.doc_classifier <file_or_folder>
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import date as _date, datetime
from pathlib import Path

from . import cc_statement_parser as ccp


CONFIDENCE_THRESHOLD = 0.7  # below this → unknown / queue


MONTH_ABBREV = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
                7: "July", 8: "Aug", 9: "Sept", 10: "Oct", 11: "Nov", 12: "Dec"}
MONTH_FROM_STR = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                  "jul": 7, "july": 7, "aug": 8, "sep": 9, "sept": 9,
                  "oct": 10, "nov": 11, "dec": 12}


@dataclass
class ClassifierResult:
    category: str
    sub_category: str
    confidence: float
    detected_date: _date | None
    target_folder: Path
    target_filename: str
    reason: str
    rule_id: str = ""


# ── Helpers ─────────────────────────────────────────────────────────────────

def _extract_text(path: Path) -> str:
    """Pull text via pdfplumber+OCR fallback. Cap to first ~3KB (header is enough
    for classification; full body unnecessary)."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        try:
            return path.read_text(encoding="utf-8", errors="ignore")[:3000]
        except Exception:
            return ""
    if suffix not in (".pdf", ".jpg", ".jpeg", ".png"):
        return ""
    try:
        text = ccp._extract_text_smart(str(path))
    except Exception:
        text = ""
    return text[:3000]


def _detect_date_from_filename(fn: str) -> _date | None:
    """Common date patterns we've seen in user's filenames."""
    fn_l = fn.lower()
    # Mon'YY (Jan'26)
    m = re.search(r"([a-z]{3,4})['\s]+(\d{2})\b", fn_l)
    if m:
        mo_str = m.group(1)[:4]
        mo = MONTH_FROM_STR.get(mo_str) or MONTH_FROM_STR.get(mo_str[:3])
        if mo:
            try:
                return _date(2000 + int(m.group(2)), mo, 15)
            except Exception:
                pass
    # MonYYYY (Apr2026)
    m = re.search(r"([a-z]{3,4})(\d{4})", fn_l)
    if m:
        mo_str = m.group(1)[:3]
        mo = MONTH_FROM_STR.get(mo_str)
        if mo:
            try:
                return _date(int(m.group(2)), mo, 15)
            except Exception:
                pass
    # _MM_YYYY  (PaySlip_1_2026.pdf)
    m = re.search(r"_(\d{1,2})_(\d{4})", fn_l)
    if m:
        mo, yr = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 2020 <= yr <= 2030:
            return _date(yr, mo, 15)
    # YYYY-MM- (GXS export: 800XXXXX595-2025-01-statement.pdf)
    m = re.search(r"-(\d{4})-(\d{2})-", fn_l)
    if m:
        return _date(int(m.group(1)), int(m.group(2)), 15)
    # DD Month YYYY  (Ar Rihla: "30 April 2026")
    m = re.search(r"_?(\d{1,2})\s+([a-z]{3,9})\s+(\d{4})", fn_l)
    if m:
        mo = MONTH_FROM_STR.get(m.group(2)[:3])
        if mo:
            try:
                return _date(int(m.group(3)), mo, int(m.group(1)))
            except Exception:
                pass
    return None


def _detect_date_from_text(text: str) -> _date | None:
    """Find a credible statement date in the text body."""
    # 'As at 30 April 2026' / 'Statement Date: 14 Apr 2026' / '13April2026' (Singlife)
    patterns = [
        r"(?:As at|Statement Date[:\s]+|Statement Period.*?to|Date\s*[:\s])\s*(\d{1,2}\s*[A-Za-z]{3,9}\s+\d{4})",
        r"(\d{1,2}\s*[A-Z][a-z]+\s+\d{4})",  # 13 April 2026
    ]
    for pat in patterns:
        m = re.search(pat, text[:1500])
        if m:
            for fmt in ("%d %B %Y", "%d %b %Y", "%d%B%Y", "%d%b%Y"):
                try:
                    return datetime.strptime(m.group(1).strip(), fmt).date()
                except Exception:
                    pass
    return None


def _mon_yy(d: _date) -> str:
    return f"{MONTH_ABBREV[d.month]}'{d.strftime('%y')}"


# ── Rule definitions ────────────────────────────────────────────────────────

# Each rule = function (filename_lower, text_lower) → ClassifierResult or None.
# Rules are tried in order; first non-None wins.

def _rule_noise(fn: str, text: str, path: Path) -> ClassifierResult | None:
    """Application forms, marketing, acknowledgements — file but no journal."""
    noise_signals = [
        "application form", "acknowledgement", "dc application", "dc form",
        "dcp", "consolidation app", "credit report",
        "cbs report", "mlcb report", "ml compairson", "15 months",
    ]
    if any(s in fn for s in noise_signals) or any(s in text[:500] for s in ["Application Form", "Acknowledgement Letter"]):
        return ClassifierResult(
            category="noise",
            sub_category="form_or_marketing",
            confidence=0.9,
            detected_date=None,
            target_folder=Path("/onedrive/Sentinel Finance/_ARCHIVE/noise"),
            target_filename=path.name,
            reason="application/acknowledgement/credit-report form",
            rule_id="noise",
        )
    return None


def _rule_loan_agreement(fn: str, text: str, path: Path) -> ClassifierResult | None:
    if "loan agreement" in fn or ("loan agreement" in text[:500].lower() and "licensed moneylender" in text.lower()):
        # Extract lender from first line of text
        first = text.split("\n", 1)[0].strip() if text else ""
        sub = "moneylender"
        if "ez loan" in first.lower():
            sub = "ez_loan"
        elif "lending bee" in first.lower():
            sub = "lending_bee"
        elif "sands credit" in first.lower():
            sub = "sands_credit"
        return ClassifierResult(
            category="loan_agreement",
            sub_category=sub,
            confidence=0.95,
            detected_date=None,
            target_folder=Path("/onedrive/Sentinel Finance/03_Credit facilities/Moneylender"),
            target_filename=path.name,
            reason="LOAN AGREEMENT marker",
            rule_id="loan_agreement",
        )
    return None


def _rule_payslip(fn: str, text: str, path: Path) -> ClassifierResult | None:
    if "payslip" not in fn and "payslip" not in text[:200].lower():
        return None
    # Extract employer from first line(s)
    head = text[:300]
    employer = "Unknown"
    if "az united" in head.lower():
        employer = "AZ United Pte Ltd"
    elif "youragency" in head.lower() or "youragency" in fn:
        employer = "YourAgency"
    elif "ganesan" in head.lower() or "ganesan" in fn:
        employer = "Ganesan"
    elif "hss" in head.lower():
        employer = "HSS"
    d = _detect_date_from_filename(path.name) or _detect_date_from_text(text)
    target_name = f"{employer} Payslip {_mon_yy(d)}.pdf" if d else path.name
    return ClassifierResult(
        category="payslip",
        sub_category=employer.lower().replace(" ", "_").replace(".", ""),
        confidence=0.9,
        detected_date=d,
        target_folder=Path(f"/onedrive/Sentinel Finance/05_Payslips/{employer}"),
        target_filename=target_name,
        reason=f"PAYSLIP marker + employer={employer}",
        rule_id="payslip",
    )


def _rule_noa_tax(fn: str, text: str, path: Path) -> ClassifierResult | None:
    if "noa" in fn or "notice of assessment" in text[:500].lower() or "income tax" in text[:300].lower():
        # Extract YA from filename "NOA YA25.pdf" or text
        ya_match = re.search(r"\bYA\s*(\d{2})\b", path.name + " " + text[:500], re.I)
        ya = ya_match.group(1) if ya_match else "?"
        return ClassifierResult(
            category="noa_tax",
            sub_category=f"noa_ya{ya}",
            confidence=0.9,
            detected_date=None,
            target_folder=Path("/onedrive/Sentinel Finance/09_Tax"),
            target_filename=f"NOA YA{ya}.pdf" if ya != "?" else path.name,
            reason="NOA / Notice of Assessment",
            rule_id="noa_tax",
        )
    return None


def _rule_cpf(fn: str, text: str, path: Path) -> ClassifierResult | None:
    # CPF markers MUST appear early (first 400 chars). Bank statements have
    # "CPF Investment Scheme" boilerplate in their deposit insurance disclaimer
    # at the bottom — that's not a real CPF doc.
    head = (path.name + " " + text[:400]).lower()
    if "cpf" in head and any(m in head for m in [
        "central provident fund", "cpf account number", "transaction history",
        "cpf statement", "cpf contribution", "cpf is", "cpf investment scheme"
    ]):
        sub = "cpf_general"
        if "investment scheme" in head or "cpf is" in head:
            sub = "cpf_is"
        elif "contribution" in head:
            sub = "cpf_contribution"
        elif "transaction history" in head:
            sub = "cpf_tx_history"
        target_folder = "CPF IS" if sub == "cpf_is" else "CPF Statements"
        d = _detect_date_from_filename(path.name) or _detect_date_from_text(text)
        target_name = path.name
        if d and sub != "cpf_is":
            target_name = f"CPF {_mon_yy(d)}.pdf"
        return ClassifierResult(
            category="cpf_statement",
            sub_category=sub,
            confidence=0.85,
            detected_date=d,
            target_folder=Path(f"/onedrive/Sentinel Finance/{target_folder}"),
            target_filename=target_name,
            reason=f"CPF marker ({sub})",
            rule_id="cpf",
        )
    return None


def _rule_ilp(fn: str, text: str, path: Path) -> ClassifierResult | None:
    head = (path.name + " " + text[:500]).lower().replace(" ", "")
    # Anti-marker: policy contract docs (welcome letter, free-look period) are
    # static documents, NOT statements — fall through to insurance rule
    if "free-lookperiod" in head or "welcometotokio" in head or "welcometosinglife" in head:
        return None
    if "policydocumentprovides" in head:
        return None
    if "singlife" in head and ("savvyinvest" in head or "policynumber" in head):
        d = _detect_date_from_filename(path.name) or _detect_date_from_text(text)
        # Statement requires a date — bare policy doc falls through to insurance rule
        if d is None:
            return None
        return ClassifierResult(
            category="ilp_statement",
            sub_category="singlife_savvy_invest",
            confidence=0.95,
            detected_date=d,
            target_folder=Path("/onedrive/Sentinel Finance/07_ILP"),
            target_filename=f"Singlife Savvy Invest {_mon_yy(d)} statement.pdf",
            reason="Singlife Savvy Invest marker + date",
            rule_id="ilp_singlife",
        )
    # Tokio ILP statement: must have statement markers AND a date
    if ("tokiomarine" in head or ("tokio" in head and "marine" in head)):
        d = _detect_date_from_filename(path.name) or _detect_date_from_text(text)
        # Require either statement-y wording OR a parseable date to claim it's a statement
        has_stmt_marker = any(m in text[:800].lower() for m in
                              ["statement date", "statement period", "fund value",
                               "premium history", "unit balance"])
        if not (d or has_stmt_marker):
            return None  # likely a policy doc → fall through
        return ClassifierResult(
            category="ilp_statement",
            sub_category="tokio_marine",
            confidence=0.9 if has_stmt_marker else 0.75,
            detected_date=d,
            target_folder=Path("/onedrive/Sentinel Finance/07_ILP"),
            target_filename=f"Tokio Marine {_mon_yy(d)}.pdf" if d else path.name,
            reason="Tokio Marine statement marker",
            rule_id="ilp_tokio",
        )
    return None


def _rule_insurance_policy(fn: str, text: str, path: Path) -> ClassifierResult | None:
    """Policy schedule / contract docs — static reference, NOT a statement."""
    fn_l = fn
    if "policy document" in fn_l or "policy schedule" in fn_l:
        return _make_insurance_result(path)
    insurance_names = [
        "cancer cover", "health plus", "mindef gtl", "multipay critical illness",
        "multiplay critical illness", "wholelife", "whole life", "wealth pro",
        "careshield", "shield plan",
    ]
    if any(n in fn_l for n in insurance_names):
        return _make_insurance_result(path)
    # Bare "Singlife Savvy Invest.pdf" (no date) = policy doc, not statement
    if "singlife savvy invest" in fn_l and "statement" not in fn_l:
        return _make_insurance_result(path)
    # Body markers: welcome letter + free-look period = policy contract
    head_text = text[:800].lower()
    if "free-look period" in head_text and "policy number" in head_text:
        return _make_insurance_result(path)
    if "policy number" in head_text and "schedule of insurance" in text[:1500].lower():
        return _make_insurance_result(path)
    return None


def _make_insurance_result(path: Path) -> ClassifierResult:
    return ClassifierResult(
        category="insurance_policy",
        sub_category="static_policy_doc",
        confidence=0.85,
        detected_date=None,
        target_folder=Path("/onedrive/Sentinel Finance/08_Insurance/Policy Documents"),
        target_filename=path.name,
        reason="insurance policy document",
        rule_id="insurance",
    )


def _rule_bank_statement(fn: str, text: str, path: Path) -> ClassifierResult | None:
    """POSB / Maybank / SC / Ar Rihla / Wise savings statements."""
    head = (path.name + " " + text[:1500]).lower()
    d = _detect_date_from_filename(path.name) or _detect_date_from_text(text)

    if "deposit account statement" in fn or ("posb" in head and "savings" in head):
        return ClassifierResult(
            category="bank_statement",
            sub_category="posb_savings",
            confidence=0.9,
            detected_date=d,
            target_folder=Path("/onedrive/Sentinel Finance/01_Bank statements/DBS_POSB Savings"),
            target_filename=f"POSB Savings {_mon_yy(d)}.pdf" if d else path.name,
            reason="POSB Deposit Account Statement",
            rule_id="bank_posb",
        )
    if "ar rihla" in fn or "ar rihla" in head:
        return ClassifierResult(
            category="bank_statement",
            sub_category="maybank_ar_rihla",
            confidence=0.9,
            detected_date=d,
            target_folder=Path("/onedrive/Sentinel Finance/01_Bank statements/Maybank Ar Rihla"),
            target_filename=f"Ar Rihla {_mon_yy(d)}.pdf" if d else path.name,
            reason="Ar Rihla (Maybank passthrough)",
            rule_id="bank_ar_rihla",
        )
    if "maybank savings" in fn or "maybank savings" in head:
        return ClassifierResult(
            category="bank_statement",
            sub_category="maybank_savings",
            confidence=0.9,
            detected_date=d,
            target_folder=Path("/onedrive/Sentinel Finance/01_Bank statements/Maybank Savings"),
            target_filename=f"Maybank Savings {_mon_yy(d)}.pdf" if d else path.name,
            reason="Maybank Savings statement",
            rule_id="bank_maybank",
        )
    if "estatement_standard chartered current savings" in fn or "standard chartered savings" in fn:
        return ClassifierResult(
            category="bank_statement",
            sub_category="sc_savings",
            confidence=0.9,
            detected_date=d,
            target_folder=Path("/onedrive/Sentinel Finance/01_Bank statements/Standard Chartered"),
            target_filename=f"SC Savings {_mon_yy(d)}.pdf" if d else path.name,
            reason="Standard Chartered Savings statement",
            rule_id="bank_sc",
        )
    if "wise" in fn and ("statement" in fn or "transaction" in fn):
        return ClassifierResult(
            category="bank_statement",
            sub_category="wise",
            confidence=0.85,
            detected_date=d,
            target_folder=Path("/onedrive/Sentinel Finance/01_Bank statements/Wise"),
            target_filename=path.name,
            reason="Wise statement",
            rule_id="bank_wise",
        )
    return None


def _rule_cc_statement(fn: str, text: str, path: Path) -> ClassifierResult | None:
    """Delegate to existing cc_statement_parser.detect_and_parse()."""
    if path.suffix.lower() not in (".pdf", ".jpg", ".jpeg", ".png"):
        return None
    try:
        stmt = ccp.detect_and_parse(str(path))
    except Exception:
        return None
    if not stmt or not stmt.bank:
        return None
    if stmt.parse_errors and not stmt.statement_date:
        return None
    # Confidence: higher when we got a valid date AND no parse errors
    confidence = 0.9 if stmt.statement_date and not stmt.parse_errors else 0.75
    # Folder routing matches existing convention
    d = stmt.statement_date
    if d is None:
        return None
    yr, mo = d.year, d.month
    folder_name = f"{MONTH_ABBREV[mo]}'{d.strftime('%y')}"
    if yr == 2026:
        target_folder = Path(f"/onedrive/Sentinel Finance/02_Credit card statements/{folder_name}")
    else:
        target_folder = Path(f"/onedrive/Sentinel Finance/02_Credit card statements/{yr}/{folder_name}")
    # Canonical filename
    from .sort_cc_statements import CANONICAL_PREFIX
    prefix = CANONICAL_PREFIX.get(stmt.bank, stmt.bank.upper())
    target_name = f"{prefix} {_mon_yy(d)}{path.suffix.lower()}"
    return ClassifierResult(
        category="cc_statement",
        sub_category=stmt.bank,
        confidence=confidence,
        detected_date=d,
        target_folder=target_folder,
        target_filename=target_name,
        reason=f"cc_statement_parser detected {stmt.bank}",
        rule_id="cc_delegate",
    )


def _rule_crypto_report(fn: str, text: str, path: Path) -> ClassifierResult | None:
    """Coinbase / Crypto.com / exchange reports (CSV or PDF)."""
    head = (path.name + " " + text[:800]).lower()
    # Coinbase CSV — header fingerprint (no "coinbase" branding in tx data,
    # but Quantity Transacted + Price Currency + Price at Transaction is unique)
    if path.suffix.lower() == ".csv":
        if "coinbase" in head:
            sub = "coinbase_csv"; conf = 0.9
        elif "quantity transacted" in text.lower() and "price at transaction" in text.lower():
            sub = "coinbase_csv"; conf = 0.85
        elif "crypto.com" in head:
            sub = "crypto_com_csv"; conf = 0.85
        else:
            return None
        return ClassifierResult(
            category="crypto_report", sub_category=sub, confidence=conf,
            detected_date=None,
            target_folder=Path("/onedrive/Sentinel Finance/10_Crypto/Coinbase exports"),
            target_filename=path.name,
            reason=f"crypto exchange CSV ({sub})", rule_id="crypto_csv",
        )
    # Coinbase PDF — first 500 chars has "Coinbase Global"
    if "coinbase global" in head or "coinbase, inc" in head:
        return ClassifierResult(
            category="crypto_report", sub_category="coinbase_pdf", confidence=0.9,
            detected_date=None,
            target_folder=Path("/onedrive/Sentinel Finance/10_Crypto/Coinbase exports"),
            target_filename=path.name,
            reason="Coinbase Transaction History Report",
            rule_id="crypto_coinbase_pdf",
        )
    if "crypto.com" in head:
        return ClassifierResult(
            category="crypto_report", sub_category="crypto_com", confidence=0.85,
            detected_date=None,
            target_folder=Path("/onedrive/Sentinel Finance/10_Crypto/Coinbase exports"),
            target_filename=path.name,
            reason="Crypto.com export", rule_id="crypto_com",
        )
    return None


RULES = [
    _rule_noise,
    _rule_loan_agreement,
    _rule_payslip,
    _rule_noa_tax,
    _rule_crypto_report,
    _rule_bank_statement,      # BEFORE cc_statement — SC parser is over-eager on "Standard Chartered"
    _rule_cc_statement,        # delegated parser
    _rule_insurance_policy,    # BEFORE ilp — policy docs share Tokio/Singlife brand words
    _rule_ilp,                 # requires date — bare policy doc falls through to insurance
    _rule_cpf,                 # most lenient — runs last
]


# ── Public entry point ──────────────────────────────────────────────────────

def classify(path: Path) -> ClassifierResult:
    """Classify a single file. Always returns a result; unknowns route to _QUEUE."""
    fn_l = path.name.lower()
    text = _extract_text(path)
    text_l = text.lower()
    for rule in RULES:
        try:
            r = rule(fn_l, text, path)
        except Exception as e:
            r = None
        if r is not None:
            return r
    # Fallback — unknown
    return ClassifierResult(
        category="unknown",
        sub_category="",
        confidence=0.0,
        detected_date=None,
        target_folder=Path("/onedrive/Sentinel Finance/_QUEUE"),
        target_filename=path.name,
        reason="no rule matched",
        rule_id="fallback",
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="File or folder to classify (for testing)")
    args = ap.parse_args()
    target = Path(args.target)
    files = []
    if target.is_dir():
        files = [f for f in target.iterdir()
                 if f.is_file() and f.suffix.lower() in (".pdf", ".csv", ".jpg", ".jpeg", ".png")]
    else:
        files = [target]
    print(f"{'Filename':<55} {'Category':<18} {'Sub':<18} {'Conf':>5} {'Date':<11} Reason")
    print("-" * 130)
    for f in sorted(files):
        r = classify(f)
        date_s = r.detected_date.isoformat() if r.detected_date else "—"
        print(f"  {f.name[:53]:<55} {r.category:<18} {r.sub_category[:17]:<18} "
              f"{r.confidence:>5.2f} {date_s:<11} {r.reason[:50]}")


if __name__ == "__main__":
    main()
