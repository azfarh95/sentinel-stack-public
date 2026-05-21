"""CC + loan statement PDF parsers.

Each parser turns one PDF into a normalised ParsedStatement structure.
The orchestrator then posts journals: DR Expense (mapped via classifier), CR CC Liability.
Payment-received lines are skipped (already captured by Firefly bridge POSB-side).

Bank detection is by filename + content patterns.

Run via: docker exec portfolio-mcp python -m app.cc_pipeline
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date as _date, datetime
from pathlib import Path

import pdfplumber

from . import classifier as _cl

logger = logging.getLogger(__name__)


@dataclass
class StatementLine:
    """One transaction line on a CC statement."""
    line_no: int
    txn_date: _date | None
    posted_date: _date | None
    description: str
    amount: float            # +ve = charge (DR liab), -ve = payment/credit (CR liab)
    kind: str                # charge | payment | interest | fee | annual_fee | balance_transfer | other
    raw: str = ""

    def hash_id(self, statement_id: str) -> str:
        key = f"{statement_id}|{self.line_no}|{self.amount:.2f}|{self.description[:60]}"
        return hashlib.sha256(key.encode()).hexdigest()[:24]


@dataclass
class ParsedStatement:
    """Result of parsing one PDF statement."""
    bank: str                          # 'maybank_cc' | 'dbs_cc' | etc.
    facility_coa_code: str             # e.g. '2112' Maybank CC
    account_number: str | None         # last-4 or full
    statement_date: _date | None
    due_date: _date | None
    previous_balance: float | None
    new_charges: float | None
    payments_received: float | None
    closing_balance: float | None
    minimum_due: float | None
    credit_limit: float | None
    available: float | None
    lines: list[StatementLine] = field(default_factory=list)
    source_path: str = ""
    parse_errors: list[str] = field(default_factory=list)
    extras: dict = field(default_factory=dict)  # bank-specific overflow (e.g. SC per-CoA prev_balance)

    def statement_id(self) -> str:
        d = self.statement_date.isoformat() if self.statement_date else "nodate"
        return f"{self.bank}|{self.account_number or 'noacct'}|{d}"


# ── PDF helpers ──────────────────────────────────────────────────────────────


def _extract_text(pdf_path: str) -> str:
    """Concatenate text across all pages."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as e:
        logger.warning("pdfplumber failed on %s: %s", pdf_path, e)
        return ""


def _extract_text_with_ocr(pdf_path: str, dpi: int = 200) -> str:
    """Last-resort: render each page to image + OCR via tesseract.
    Used when the PDF is image-only (page.extract_text() returns empty).
    """
    try:
        import pytesseract
    except ImportError:
        logger.warning("pytesseract not installed; cannot OCR %s", pdf_path)
        return ""
    out_lines = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages):
                try:
                    im = page.to_image(resolution=dpi).original
                    txt = pytesseract.image_to_string(im, lang="eng")
                    out_lines.append(txt)
                except Exception as e:
                    logger.warning("OCR page %d of %s failed: %s", i, pdf_path, e)
    except Exception as e:
        logger.warning("OCR open failed on %s: %s", pdf_path, e)
    return "\n".join(out_lines)


def _extract_text_smart(pdf_path: str) -> str:
    """Try pdfplumber text first; if empty (image-only PDF), fall back to OCR."""
    txt = _extract_text(pdf_path)
    if len(txt.strip()) >= 100:
        return txt
    logger.info("text-empty PDF, trying OCR: %s", pdf_path)
    return _extract_text_with_ocr(pdf_path)


def _parse_amount(s: str) -> float | None:
    """Parse '1,234.56' or '1234.56CR' or '(123.45)'. Returns float or None."""
    if not s:
        return None
    s = s.strip().replace(",", "")
    is_credit = False
    if s.endswith("CR"):
        is_credit = True
        s = s[:-2].strip()
    if s.startswith("(") and s.endswith(")"):
        is_credit = True
        s = s[1:-1].strip()
    try:
        v = float(s)
        return -v if is_credit else v
    except ValueError:
        return None


# Date parsing for various SG formats
_DATE_RE_SLASH = re.compile(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?")
_DATE_RE_LONG = re.compile(r"(\d{1,2})\s*([A-Z]{3,4})\s*(\d{2,4})?", re.IGNORECASE)
_MONTH_ABBREV = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,
                 "aug":8,"sep":9,"sept":9,"oct":10,"nov":11,"dec":12}


def _parse_date_partial(s: str, stmt_year: int) -> _date | None:
    """Parse '14JAN' / '14 Jan' / '14/01' / '14 Jan 2026'. Uses stmt_year as default."""
    if not s:
        return None
    s = s.strip()
    m = _DATE_RE_LONG.search(s)
    if m:
        d, mo, y = m.group(1), m.group(2).lower()[:3], m.group(3)
        if mo in _MONTH_ABBREV:
            year = int(y) if y else stmt_year
            if year < 100:
                year += 2000
            try:
                return _date(year, _MONTH_ABBREV[mo], int(d))
            except Exception:
                return None
    m = _DATE_RE_SLASH.search(s)
    if m:
        d, mo, y = m.group(1), m.group(2), m.group(3)
        year = int(y) if y else stmt_year
        if year < 100:
            year += 2000
        try:
            return _date(year, int(mo), int(d))
        except Exception:
            return None
    return None


# ── Bank-specific parsers ────────────────────────────────────────────────────


def parse_maybank_cc(pdf_path: str) -> ParsedStatement:
    """Maybank Platinum Visa CC statement.

    Format markers:
      'Product : Platinum Visa'
      'Credit Limit : 7,100'
      'Statement Date : 25/04/2026'
      ...
      'TRANSACTED POSTED DESCRIPTION OF TRANSACTION TRANSACTION AMOUNT (S$)'
      'OUTSTANDING BALANCE BROUGHT FORWARD <amount>'
      Then line items like:
        '06APR 06APR PAYMENT - INTERNET BANKING 137.54CR'
        '26MAR 28MAR COINBASE RTL-J9TUAUPU DUBLIN 29.99'
    """
    text = _extract_text(pdf_path)
    out = ParsedStatement(bank="maybank_cc", facility_coa_code="2112",
                          account_number=None, statement_date=None, due_date=None,
                          previous_balance=None, new_charges=None,
                          payments_received=None, closing_balance=None,
                          minimum_due=None, credit_limit=None, available=None,
                          source_path=pdf_path)
    if "Platinum Visa" not in text:
        out.parse_errors.append("not a Maybank Platinum Visa statement")
        return out

    # Header fields
    m = re.search(r"Statement Date\s*:\s*(\d{2}/\d{2}/\d{4})", text)
    if m:
        try:
            out.statement_date = datetime.strptime(m.group(1), "%d/%m/%Y").date()
        except Exception:
            pass
    m = re.search(r"Due Date\s*:\s*(\d{2}/\d{2}/\d{4})", text)
    if m:
        try:
            out.due_date = datetime.strptime(m.group(1), "%d/%m/%Y").date()
        except Exception:
            pass
    m = re.search(r"Credit Limit\s*:\s*([\d,]+)", text)
    if m: out.credit_limit = _parse_amount(m.group(1))
    m = re.search(r"Previous Balance\s*:\s*([\d,.]+)", text)
    if m: out.previous_balance = _parse_amount(m.group(1))
    m = re.search(r"New Charges this month\s*:\s*([\d,.]+)", text)
    if m: out.new_charges = _parse_amount(m.group(1))
    m = re.search(r"Credit this month\s*:\s*([\d,.]+)", text)
    if m: out.payments_received = _parse_amount(m.group(1))
    m = re.search(r"Total Due\s*:\s*([\d,.]+)", text)
    if m: out.closing_balance = _parse_amount(m.group(1))
    m = re.search(r"Minimum Due\s*:\s*([\d,.]+)", text)
    if m: out.minimum_due = _parse_amount(m.group(1))
    m = re.search(r"(\d{4}-\d{4}-\d{4}-\d{4})", text)
    if m: out.account_number = m.group(1)

    stmt_year = out.statement_date.year if out.statement_date else _date.today().year

    # Line items — match: '06APR 06APR DESCRIPTION AMOUNT[CR]'
    line_re = re.compile(r"^(\d{1,2}[A-Z]{3})\s+(\d{1,2}[A-Z]{3})\s+(.+?)\s+([\d,]+\.\d{2})(CR)?$", re.MULTILINE)
    n = 0
    for m in line_re.finditer(text):
        txn_d_s, post_d_s, desc, amt_s, cr_flag = m.groups()
        amt = _parse_amount(amt_s)
        if amt is None:
            continue
        if cr_flag:
            amt = -amt
        kind = "charge"
        d_lower = desc.lower()
        if "payment" in d_lower and amt < 0:
            kind = "payment"
        elif "finance charge" in d_lower or "interest" in d_lower:
            kind = "interest"
            amt = abs(amt)
        elif "late" in d_lower and ("fee" in d_lower or "charge" in d_lower):
            kind = "fee"
        n += 1
        out.lines.append(StatementLine(
            line_no=n,
            txn_date=_parse_date_partial(txn_d_s, stmt_year),
            posted_date=_parse_date_partial(post_d_s, stmt_year),
            description=desc.strip(),
            amount=amt,
            kind=kind,
            raw=m.group(0),
        ))

    # Capture FINANCE CHARGE if listed separately (no date prefix)
    for m in re.finditer(r"FINANCE CHARGE\s+([\d,]+\.\d{2})", text):
        amt = _parse_amount(m.group(1))
        if amt and not any(l.kind == "interest" for l in out.lines):
            n += 1
            out.lines.append(StatementLine(
                line_no=n, txn_date=out.statement_date, posted_date=out.statement_date,
                description="FINANCE CHARGE", amount=amt, kind="interest", raw=m.group(0),
            ))

    return out


def parse_dbs_cc(pdf_path: str) -> ParsedStatement:
    """DBS Credit Cards statement.

    Format markers:
      'Credit Cards' (header)
      'Statement of Account'
      'STATEMENT DATE CREDIT LIMIT MINIMUM PAYMENT PAYMENT DUE DATE'
      '14 Jan 2026 $9,100.00 $52.51 09 Feb 2026'
      'Total Outstanding Balance Payment Due Date'
      '$1,750.38 09 Feb 2026'
      ...
      Line items:
        '30 DEC BILL PAYMENT - DBS INTERNET/WIRELESS 200.00 CR'
        '14 JAN 014MY PREFERRED PAYMENT PLAN18 (15) 50.76'
    """
    text = _extract_text(pdf_path)
    out = ParsedStatement(bank="dbs_cc", facility_coa_code="2111",
                          account_number=None, statement_date=None, due_date=None,
                          previous_balance=None, new_charges=None,
                          payments_received=None, closing_balance=None,
                          minimum_due=None, credit_limit=None, available=None,
                          source_path=pdf_path)
    if "Credit Cards" not in text and "Statement of Account" not in text:
        out.parse_errors.append("doesn't look like a DBS CC statement")
        return out
    # DBS Cashline ALSO matches the header; differentiate
    if "Cashline" in text and "Credit Cards" not in text[:200]:
        out.parse_errors.append("looks like DBS Cashline, not CC")
        return out

    # Statement date + limit + payment due date — single row
    m = re.search(r"(\d{2}\s+[A-Z][a-z]{2}\s+\d{4})\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})\s+(\d{2}\s+[A-Z][a-z]{2}\s+\d{4})", text)
    if m:
        try:
            out.statement_date = datetime.strptime(m.group(1), "%d %b %Y").date()
        except Exception:
            pass
        out.credit_limit = _parse_amount(m.group(2))
        out.minimum_due = _parse_amount(m.group(3))
        try:
            out.due_date = datetime.strptime(m.group(4), "%d %b %Y").date()
        except Exception:
            pass
    m = re.search(r"Total Outstanding Balance.*?\n\s*\$?([\d,]+\.\d{2})", text, re.DOTALL)
    if m:
        out.closing_balance = _parse_amount(m.group(1))
    m = re.search(r"PREVIOUS BALANCE\s+([\d,]+\.\d{2})", text)
    if m: out.previous_balance = _parse_amount(m.group(1))

    stmt_year = out.statement_date.year if out.statement_date else _date.today().year

    # DBS line item formats:
    #   '30 DEC BILL PAYMENT - DBS INTERNET/WIRELESS 200.00 CR'  ← payment
    #   '14 JAN 014MY PREFERRED PAYMENT PLAN18 (15) 50.76'         ← plan instalment
    #   '15 JAN ANTHROPIC SAN FRANCISCO USA 5.60'                   ← charge
    line_re = re.compile(r"^(\d{1,2}\s+[A-Z]{3})\s+(.+?)\s+([\d,]+\.\d{2})(?:\s+CR)?$", re.MULTILINE)
    n = 0
    for m in line_re.finditer(text):
        date_s, desc, amt_s = m.groups()
        amt = _parse_amount(amt_s)
        if amt is None:
            continue
        is_cr = m.group(0).rstrip().endswith("CR")
        if is_cr:
            amt = -amt
        kind = "charge"
        d_lower = desc.lower()
        if "bill payment" in d_lower and amt < 0:
            kind = "payment"
        elif "finance charge" in d_lower or "interest" in d_lower:
            kind = "interest"
            amt = abs(amt)
        elif "annual fee" in d_lower:
            kind = "annual_fee"
        n += 1
        out.lines.append(StatementLine(
            line_no=n,
            txn_date=_parse_date_partial(date_s, stmt_year),
            posted_date=_parse_date_partial(date_s, stmt_year),
            description=desc.strip(),
            amount=amt,
            kind=kind,
            raw=m.group(0),
        ))
    return out


def parse_dbs_cashline(pdf_path: str) -> ParsedStatement:
    """DBS Cashline statement — distinct from DBS CC.

    Format markers:
      'Cashline' or 'CASHLINE ACCOUNT NO'
      'STATEMENT PRINTED ON DD MMM YYYY'
      'CASHLINE ACCOUNT NO 085-043736-4'
      Line items like DBS CC: '23 Dec 2025 PAYMENT - DBS INTERNET/WIRELESS 53.57 CR'
    """
    text = _extract_text(pdf_path)
    out = ParsedStatement(bank="dbs_cashline", facility_coa_code="2121",
                          account_number=None, statement_date=None, due_date=None,
                          previous_balance=None, new_charges=None,
                          payments_received=None, closing_balance=None,
                          minimum_due=None, credit_limit=None, available=None,
                          source_path=pdf_path)
    if "Cashline" not in text and "CASHLINE" not in text:
        out.parse_errors.append("not a DBS Cashline statement")
        return out

    # Header fields
    m = re.search(r"CASHLINE ACCOUNT NO[^\d]*(\d{3}-\d{6}-\d)", text)
    if m: out.account_number = m.group(1)
    m = re.search(r"STATEMENT PRINTED ON\s+(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})", text)
    if m:
        try:
            out.statement_date = datetime.strptime(m.group(1), "%d %b %Y").date()
        except Exception:
            pass
    # fallback: same row as account number with date alongside
    if out.statement_date is None:
        m = re.search(r"\d{3}-\d{6}-\d\s+(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})", text)
        if m:
            try:
                out.statement_date = datetime.strptime(m.group(1), "%d %b %Y").date()
            except Exception:
                pass
    m = re.search(r"CREDIT LIMIT[^\d]*S\$?([\d,]+\.\d{2})", text, re.I)
    if m: out.credit_limit = _parse_amount(m.group(1))
    m = re.search(r"PAYMENT DUE DATE\s*(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})", text)
    if m:
        try:
            out.due_date = datetime.strptime(m.group(1), "%d %b %Y").date()
        except Exception:
            pass
    m = re.search(r"Total Outstanding Balance.*?S?\$?([\d,]+\.\d{2})", text, re.DOTALL)
    if m: out.closing_balance = _parse_amount(m.group(1))
    m = re.search(r"MINIMUM PAYMENT DUE\s*S?\$?([\d,]+\.\d{2})", text)
    if m: out.minimum_due = _parse_amount(m.group(1))
    m = re.search(r"PREVIOUS BALANCE\s+([\d,]+\.\d{2})", text)
    if m: out.previous_balance = _parse_amount(m.group(1))

    stmt_year = out.statement_date.year if out.statement_date else _date.today().year

    # Line items — Cashline uses "DD MMM YYYY DESCRIPTION AMOUNT CR" format
    line_re = re.compile(r"^(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})\s+(.+?)\s+([\d,]+\.\d{2})(?:\s+CR)?$",
                         re.MULTILINE)
    n = 0
    for m in line_re.finditer(text):
        date_s, desc, amt_s = m.groups()
        amt = _parse_amount(amt_s)
        if amt is None: continue
        d_lower = desc.lower()
        # Skip balance-marker rows — these are not transactions
        if "closing balance" in d_lower or "opening balance" in d_lower or "balance b/f" in d_lower:
            continue
        is_cr = m.group(0).rstrip().endswith("CR")
        if is_cr: amt = -amt
        kind = "charge"
        if "payment" in d_lower and amt < 0:
            kind = "payment"
        elif "funds transfer" in d_lower:
            # Cashline drawdown into POSB / similar — balance-sheet movement,
            # NOT a P&L expense. The opposite leg is bridged from POSB-side via
            # firefly_bridge. Skip via 'payment' kind to avoid double-posting.
            kind = "payment"
            amt = abs(amt)  # force positive then skip
        elif "interest" in d_lower or "finance charge" in d_lower:
            kind = "interest"
            amt = abs(amt)
        elif "annual fee" in d_lower:
            kind = "annual_fee"
        try:
            tdate = datetime.strptime(date_s, "%d %b %Y").date()
        except Exception:
            tdate = None
        n += 1
        out.lines.append(StatementLine(
            line_no=n, txn_date=tdate, posted_date=tdate,
            description=desc.strip(), amount=amt, kind=kind, raw=m.group(0),
        ))
    return out


def parse_hsbc(pdf_path: str) -> ParsedStatement:
    """HSBC Visa Revolution. Format is image-only on most months; some text-extractable.

    'HSBC VISA REVOLUTION'
    'POST DATE TRAN DATE DESCRIPTION AMOUNT(SGD)'
    '31 Dec 30 Dec DBS Visa Direct SG 258.23CR'
    'Previous Statement Balance 6,758.23'
    'Total Due 6,634.78'
    """
    # HSBC PDFs are often image-only — try smart extract (with OCR fallback)
    text = _extract_text_smart(pdf_path)
    out = ParsedStatement(bank="hsbc_cc", facility_coa_code="2114",
                          account_number=None, statement_date=None, due_date=None,
                          previous_balance=None, new_charges=None,
                          payments_received=None, closing_balance=None,
                          minimum_due=None, credit_limit=None, available=None,
                          source_path=pdf_path)
    if "HSBC" not in text and "VISA REVOLUTION" not in text.upper():
        out.parse_errors.append(f"HSBC marker not found in text (even after OCR)")
        return out

    m = re.search(r"Statement From\s+(\d{1,2}\s+[A-Z]{3}\s+\d{4})\s+to\s+(\d{1,2}\s+[A-Z]{3}\s+\d{4})", text)
    if m:
        try:
            out.statement_date = datetime.strptime(m.group(2), "%d %b %Y").date()
        except Exception:
            pass
    m = re.search(r"Previous Statement Balance\s+([\d,]+\.\d{2})", text)
    if m: out.previous_balance = _parse_amount(m.group(1))
    m = re.search(r"Total Due\s+([\d,]+\.\d{2})", text)
    if m: out.closing_balance = _parse_amount(m.group(1))
    m = re.search(r"(4835-X{4}-X{4}-\d{4}|4835-\d{4}-\d{4}-\d{4})", text)
    if m: out.account_number = m.group(1)

    stmt_year = out.statement_date.year if out.statement_date else _date.today().year

    # HSBC line: "31 Dec 30 Dec DBS Visa Direct SG 258.23CR" or "14 Jan 14 Jan FINANCE CHARGE 134.78"
    # OCR often renders day+month with no space ("31Dec 30Dec") — make space optional and allow
    # trailing junk after the amount (OCR sometimes adds stray chars).
    line_re = re.compile(
        r"^(\d{1,2}\s*[A-Z][a-z]{2})\s+(\d{1,2}\s*[A-Z][a-z]{2})\s+=?\s*(.+?)\s+([\d,]+\.\d{2})\s*(CR)?\s*$",
        re.MULTILINE)
    n = 0
    for m in line_re.finditer(text):
        post_d, txn_d, desc, amt_s, cr_flag = m.groups()
        amt = _parse_amount(amt_s)
        if amt is None: continue
        if cr_flag: amt = -amt
        kind = "charge"
        d_lower = desc.lower()
        if amt < 0 and ("payment" in d_lower or "visa direct" in d_lower or "internet banking" in d_lower):
            kind = "payment"
        elif "finance charge" in d_lower or "interest" in d_lower:
            kind = "interest"
            amt = abs(amt)
        n += 1
        out.lines.append(StatementLine(
            line_no=n,
            txn_date=_parse_date_partial(txn_d, stmt_year),
            posted_date=_parse_date_partial(post_d, stmt_year),
            description=desc.strip(),
            amount=amt,
            kind=kind, raw=m.group(0),
        ))
    return out


def parse_sc(pdf_path: str) -> ParsedStatement:
    """Standard Chartered combined CC + BT statement.

    Has TWO product accounts. We focus on the CC (5498-...-8810 → 2113).
    BT (9702-...-6461 → 2211) lines are tagged and ALSO posted (to 2211).
    """
    text = _extract_text(pdf_path)
    out = ParsedStatement(bank="sc", facility_coa_code="2113",
                          account_number=None, statement_date=None, due_date=None,
                          previous_balance=None, new_charges=None,
                          payments_received=None, closing_balance=None,
                          minimum_due=None, credit_limit=None, available=None,
                          source_path=pdf_path)
    if "Standard Chartered" not in text and "SC Mobile" not in text:
        out.parse_errors.append("not a Standard Chartered statement")
        return out

    m = re.search(r"Statement Date\s*:\s*(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})", text)
    if m:
        try:
            out.statement_date = datetime.strptime(m.group(1), "%d %b %Y").date()
        except Exception:
            pass
    m = re.search(r"Approved Credit Limit\s*:\s*([\d,]+)", text)
    if m: out.credit_limit = _parse_amount(m.group(1))
    m = re.search(r"Available Credit Limit\s*:\s*([\d,]+)", text)
    if m: out.available = _parse_amount(m.group(1))
    # SC has TWO products on one statement (CC 2113, BT 2211).
    # First "BALANCE FROM PREVIOUS STATEMENT" = CC, second = BT.
    # Store SUM as previous_balance (overall facility opening); per-CoA
    # breakdown also stored in extras for backfill journal.
    prev_balances = re.findall(r"BALANCE FROM PREVIOUS STATEMENT\s+([\d,]+\.\d{2})", text)
    if prev_balances:
        cc_prev = _parse_amount(prev_balances[0]) or 0.0
        bt_prev = _parse_amount(prev_balances[1]) if len(prev_balances) > 1 else 0.0
        out.previous_balance = cc_prev + bt_prev
        out.extras["previous_balance_by_coa"] = {"2113": cc_prev, "2211": bt_prev}
    # Closing balance — SC has two products on one stmt, each with "NEW BALANCE <amt>"
    new_balances = re.findall(r"NEW BALANCE\s+([\d,]+\.\d{2})", text)
    if new_balances:
        cc_close = _parse_amount(new_balances[0]) or 0.0
        bt_close = _parse_amount(new_balances[1]) if len(new_balances) > 1 else 0.0
        out.closing_balance = cc_close + bt_close
        out.extras["closing_balance_by_coa"] = {"2113": cc_close, "2211": bt_close}

    stmt_year = out.statement_date.year if out.statement_date else _date.today().year

    # SC line: "30 Dec 30 Dec PAYMENT - THANK YOU 256.12CR" or "16 Jan 16 Jan EZBAL INT~~ 8.39"
    # Detect which sub-product (CC vs BT) from header marker before the line
    cur_coa = "2113"  # default CC
    n = 0
    for raw_line in text.split("\n"):
        if "BALANCE TRANSFER A/C" in raw_line or "9702-2221-0463-6461" in raw_line:
            cur_coa = "2211"
        elif "SIMPLY CASH CREDIT CARD" in raw_line or "5498-34" in raw_line:
            cur_coa = "2113"
        m = re.match(r"^\s*(\d{1,2}\s+[A-Z][a-z]{2})\s+(\d{1,2}\s+[A-Z][a-z]{2})\s+(.+?)\s+([\d,]+\.\d{2})(CR)?\s*$", raw_line)
        if not m: continue
        post_d, txn_d, desc, amt_s, cr_flag = m.groups()
        amt = _parse_amount(amt_s)
        if amt is None: continue
        if cr_flag: amt = -amt
        kind = "charge"
        d_lower = desc.lower()
        if amt < 0 and "payment" in d_lower:
            kind = "payment"
        elif "ezbal" in d_lower and "int" in d_lower:
            kind = "interest"
            amt = abs(amt)
        elif "finance charge" in d_lower:
            kind = "interest"
            amt = abs(amt)
        n += 1
        sl = StatementLine(
            line_no=n,
            txn_date=_parse_date_partial(txn_d, stmt_year),
            posted_date=_parse_date_partial(post_d, stmt_year),
            description=desc.strip(),
            amount=amt,
            kind=kind, raw=raw_line.strip(),
        )
        # Override per-line CoA via marker (SC has two products on one statement)
        # Store in raw — orchestrator will dispatch based on text marker
        sl.raw = f"[coa:{cur_coa}] {sl.raw}"
        out.lines.append(sl)
    return out


def parse_uob(pdf_path: str) -> ParsedStatement:
    """UOB CashPlus statement."""
    text = _extract_text(pdf_path)
    out = ParsedStatement(bank="uob", facility_coa_code="2122",
                          account_number=None, statement_date=None, due_date=None,
                          previous_balance=None, new_charges=None,
                          payments_received=None, closing_balance=None,
                          minimum_due=None, credit_limit=None, available=None,
                          source_path=pdf_path)
    if "United Overseas Bank" not in text and "UOB" not in text:
        out.parse_errors.append("not a UOB statement")
        return out

    m = re.search(r"Period:\s*(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})\s+to\s+(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})", text)
    if m:
        try:
            out.statement_date = datetime.strptime(m.group(2), "%d %b %Y").date()
        except Exception:
            pass
    m = re.search(r"Credit Limit.*?([\d,]+\.\d{2})", text, re.DOTALL)
    if m: out.credit_limit = _parse_amount(m.group(1))
    # UOB opening balance line: "10 Dec OPENING BALANCE 153.65OD"
    m = re.search(r"\d{1,2}\s+[A-Z][a-z]{2}\s+OPENING BALANCE\s+([\d,]+\.\d{2})\s*(?:OD)?", text)
    if m: out.previous_balance = _parse_amount(m.group(1))
    # UOB closing line — either "<date> CLOSING BALANCE <amt> OD" or just "CLOSING BALANCE <amt> OD"
    m = re.search(r"(?:\d{1,2}\s+[A-Z][a-z]{2}\s+)?CLOSING BALANCE\s+([\d,]+\.\d{2})\s*(?:OD)?", text)
    if m: out.closing_balance = _parse_amount(m.group(1))
    stmt_year = out.statement_date.year if out.statement_date else _date.today().year

    # UOB lines: "30 Dec Inward Credit-FAST 153.65 0.00"
    #            "05 Jan Annual Fee DR 120.00 120.00OD"
    line_re = re.compile(r"^(\d{1,2}\s+[A-Z][a-z]{2})\s+(.+?)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})(?:OD)?$", re.MULTILINE)
    n = 0
    for m in line_re.finditer(text):
        date_s, desc, amt_s, _bal_s = m.groups()
        amt = _parse_amount(amt_s)
        if amt is None: continue
        d_lower = desc.lower()
        kind = "charge"
        if ("inward credit" in d_lower or "payment" in d_lower
                or "monthly instal" in d_lower):
            # Monthly Instalment is the user's scheduled repayment (P+I) — reduces
            # liability, NOT a new charge. Skip via 'payment' kind (already in GL
            # via Firefly bridge POSB-side).
            kind = "payment"
            amt = -amt
        elif "annual fee" in d_lower:
            kind = "annual_fee"
        elif "interest" in d_lower:
            kind = "interest"
        n += 1
        out.lines.append(StatementLine(
            line_no=n,
            txn_date=_parse_date_partial(date_s, stmt_year),
            posted_date=_parse_date_partial(date_s, stmt_year),
            description=desc.strip(),
            amount=amt,
            kind=kind, raw=m.group(0),
        ))
    return out


def parse_maybank_ca(pdf_path: str) -> ParsedStatement:
    """Maybank CreditAble (term loan + OD)."""
    text = _extract_text(pdf_path)
    out = ParsedStatement(bank="maybank_ca", facility_coa_code="2213",
                          account_number=None, statement_date=None, due_date=None,
                          previous_balance=None, new_charges=None,
                          payments_received=None, closing_balance=None,
                          minimum_due=None, credit_limit=None, available=None,
                          source_path=pdf_path)
    if "CreditAble" not in text and "Creditable" not in text.lower():
        out.parse_errors.append("not a Maybank CreditAble statement")
        return out

    m = re.search(r"Statement Date\s*:\s*(\d{1,2}\s+[A-Z]{3}\s+\d{4})", text)
    if m:
        try:
            out.statement_date = datetime.strptime(m.group(1), "%d %b %Y").date()
        except Exception:
            pass
    m = re.search(r"Drawing Limit\s*:\s*([\d,]+\.\d{2})", text)
    if m: out.available = _parse_amount(m.group(1))
    # Maybank CA: "16/12 Opening Balance 3,667.46-" (trailing '-' = debit = OD)
    m = re.search(r"\d{2}/\d{2}\s+Opening Balance\s+([\d,]+\.\d{2})-?", text)
    if m: out.previous_balance = _parse_amount(m.group(1))
    m = re.search(r"Outstanding Balance\s+([\d,]+\.\d{2})", text)
    if m: out.closing_balance = _parse_amount(m.group(1))
    stmt_year = out.statement_date.year if out.statement_date else _date.today().year

    # Maybank CA line: "16/12 Opening Balance 3,667.46-"
    #                  "15/01 Term Loan Instalment (1/60) 105.00 105.00-"
    #                  "15/01 OD Interest 70.10 3,622.56-"
    line_re = re.compile(r"^(\d{2}/\d{2})\s+(.+?)\s+([\d,]+\.\d{2})(?:\s+[\d,]+\.\d{2}-?)?$", re.MULTILINE)
    n = 0
    for m in line_re.finditer(text):
        date_s, desc, amt_s = m.groups()
        amt = _parse_amount(amt_s)
        if amt is None: continue
        d_lower = desc.lower()
        kind = "charge"
        if "opening balance" in d_lower or "closing balance" in d_lower:
            continue  # skip balance markers
        if "term loan instalment int" in d_lower:
            kind = "interest"
        elif "term loan instalment" in d_lower:
            kind = "charge"  # principal increment (drawing more)
        elif "od interest" in d_lower:
            kind = "interest"
        elif "transfer from" in d_lower or "payment" in d_lower:
            kind = "payment"
            amt = -amt
        elif "interest adjustment" in d_lower:
            kind = "interest"
            amt = -amt
        n += 1
        out.lines.append(StatementLine(
            line_no=n,
            txn_date=_parse_date_partial(date_s, stmt_year),
            posted_date=_parse_date_partial(date_s, stmt_year),
            description=desc.strip(),
            amount=amt,
            kind=kind, raw=m.group(0),
        ))
    return out


def parse_gxs(pdf_path: str) -> ParsedStatement:
    """GXS Bank statement — handles both GXS Savings and GXS FlexiLoan.

    GXS Savings format markers:
      'GXS Savings Account 888-XXXXXX-X'
      'DD Mon YYYY GXS Savings Account ...'
      Most months show $0 activity (savings has no money in it).

    GXS FlexiLoan format markers:
      'FlexiLoan' header
      Account 800-170405-95
      Monthly payment line items.
    """
    text = _extract_text(pdf_path)
    out = ParsedStatement(bank="gxs", facility_coa_code="2212",
                          account_number=None, statement_date=None, due_date=None,
                          previous_balance=None, new_charges=None,
                          payments_received=None, closing_balance=None,
                          minimum_due=None, credit_limit=None, available=None,
                          source_path=pdf_path)
    if "GXS" not in text:
        out.parse_errors.append("not a GXS statement")
        return out

    is_flexiloan = "FlexiLoan" in text or "Flexi-Loan" in text or "FLEXILOAN" in text.upper()

    if is_flexiloan:
        out.facility_coa_code = "2212"  # GXS FlexiLoan
        # Account no
        m = re.search(r"(800-\d{6}-\d{2})", text)
        if m: out.account_number = m.group(1)
        # Statement date — GXS format: "31 December 2025 = GXS FlexiLoan ..."
        # Try full-month-name first, fall back to abbreviated
        m = re.search(r"(\d{1,2}\s+[A-Z][a-z]+\s+\d{4})\s*=\s*GXS FlexiLoan", text)
        if m:
            for fmt in ("%d %B %Y", "%d %b %Y"):
                try:
                    out.statement_date = datetime.strptime(m.group(1), fmt).date()
                    break
                except Exception:
                    pass
        if out.statement_date is None:
            m = re.search(r"Statement[^\n]*?(\d{1,2}\s+[A-Z][a-z]+\s+\d{4})", text)
            if m:
                for fmt in ("%d %B %Y", "%d %b %Y"):
                    try:
                        out.statement_date = datetime.strptime(m.group(1), fmt).date()
                        break
                    except Exception:
                        pass
        # FlexiLoan tx history table: "<date> Opening balance -5,431.76"
        # Negative sign reflects liability convention. Strip sign for opening_balance.
        m = re.search(r"[Oo]pening [Bb]alance\s+-?\$?([\d,]+\.\d{2})", text)
        if m: out.previous_balance = _parse_amount(m.group(1))
        # Closing: last balance line in the table = closing.
        # Match all "<date> <desc> <amounts...> -<balance>" rows; pick the last.
        bal_lines = re.findall(r"-([\d,]+\.\d{2})$", text, re.MULTILINE)
        if bal_lines:
            # Use last numeric balance entry as closing (last row of tx table)
            out.closing_balance = _parse_amount(bal_lines[-1])
        # Also accept explicit "Outstanding/Closing balance" if present
        m = re.search(r"(?:Outstanding|Closing) [Bb]alance\s+-?\$?([\d,]+\.\d{2})", text)
        if m: out.closing_balance = _parse_amount(m.group(1))
        # Line items — GXS FlexiLoan format:
        #   "1 Dec 2025 Opening balance -5,431.76"   (running balance, no amt; SKIP)
        #   "30 Dec 2025 Loan repayment 180.00 -5,251.76"   (amount + running balance)
        # Regex captures amount + optional trailing running-balance column.
        line_re = re.compile(
            r"^(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})\s+(.+?)\s+([\d,]+\.\d{2})"
            r"(?:\s+-?\$?[\d,]+\.\d{2})?\s*$",
            re.MULTILINE)
        n = 0
        for m in line_re.finditer(text):
            date_s, desc, amt_s = m.groups()
            d_lower = desc.lower()
            # Skip balance markers — they're not transactions
            if "opening balance" in d_lower or "closing balance" in d_lower:
                continue
            amt = _parse_amount(amt_s)
            if amt is None or amt == 0:
                continue
            try:
                tdate = datetime.strptime(date_s, "%d %b %Y").date()
            except Exception:
                tdate = None
            # Loan repayment = payment (already bridged via Firefly POSB-side)
            if "loan repayment" in d_lower or "repayment" in d_lower:
                kind = "payment"
                amt = -amt
            elif "interest" in d_lower:
                kind = "interest"
            elif "late" in d_lower and "fee" in d_lower:
                kind = "fee"
            else:
                kind = "charge"
            n += 1
            out.lines.append(StatementLine(
                line_no=n, txn_date=tdate, posted_date=tdate,
                description=desc.strip(), amount=amt, kind=kind, raw=m.group(0),
            ))
        return out

    # GXS Savings — account-statement, not a credit facility.
    # Pull statement_date for folder routing. Don't post journals (savings not in CoA yet).
    out.facility_coa_code = "1116"  # placeholder — would be GXS Savings asset acct if added
    m = re.search(r"(888-\d{6}-\d)", text)
    if m: out.account_number = m.group(1)
    # Statement date in line like "31 Jan 2026 GXS Savings Account 888-..."
    m = re.search(r"(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})\s+GXS Savings Account", text)
    if m:
        try:
            out.statement_date = datetime.strptime(m.group(1), "%d %b %Y").date()
        except Exception:
            pass
    if out.statement_date is None:
        # try "Statement Date" pattern
        m = re.search(r"Statement Date[:\s]+(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})", text)
        if m:
            try:
                out.statement_date = datetime.strptime(m.group(1), "%d %b %Y").date()
            except Exception:
                pass
    # GXS Savings opening + closing balances (asset side — useful for opening-balance journal)
    m = re.search(r"[Oo]pening [Bb]alance\s+\$?([\d,]+\.\d{2})", text)
    if m: out.previous_balance = _parse_amount(m.group(1))
    m = re.search(r"(?:Closing|Ending) [Bb]alance\s+\$?([\d,]+\.\d{2})", text)
    if m: out.closing_balance = _parse_amount(m.group(1))
    out.parse_errors.append("GXS Savings statement — date extracted for folder routing; no journal posting (savings acct not in CoA)")
    return out


# ── Dispatch ──────────────────────────────────────────────────────────────────


def detect_and_parse(pdf_path: str) -> ParsedStatement | None:
    """Auto-detect bank from filename + content and dispatch."""
    fn = Path(pdf_path).name.lower()
    # Filename-based fast path
    if "platinum visa" in fn or ("maybank" in fn and ("cc" in fn or "credit card" in fn)):
        return parse_maybank_cc(pdf_path)
    if "creditable" in fn or "maybank ca" in fn:
        return parse_maybank_ca(pdf_path)
    if ("dbs cl" in fn or "dbs cashline" in fn or fn.startswith("dbs cashline")
            or "cashline statement" in fn):
        return parse_dbs_cashline(pdf_path)
    if ("dbs cc" in fn or "dbs credit" in fn
            or "credit cards consolidated" in fn or "credit cards statement" in fn):
        return parse_dbs_cc(pdf_path)
    if "hsbc" in fn:
        return parse_hsbc(pdf_path)
    if "sc" in fn or "standard chartered" in fn:
        return parse_sc(pdf_path)
    if "uob" in fn:
        return parse_uob(pdf_path)
    if "gxs" in fn:
        return parse_gxs(pdf_path)
    # Content-based fallback — smart extract (OCR for image-only)
    text_head = _extract_text_smart(pdf_path)[:2000]
    if "Platinum Visa" in text_head:
        return parse_maybank_cc(pdf_path)
    if "CreditAble" in text_head:
        return parse_maybank_ca(pdf_path)
    if "HSBC" in text_head:
        return parse_hsbc(pdf_path)
    if ("Standard Chartered" in text_head or "SC Mobile" in text_head
            or "Credit Card and Personal Loan Statement" in text_head
            or "SIMPLY CASH CREDIT CARD" in text_head):
        return parse_sc(pdf_path)
    if "United Overseas Bank" in text_head:
        return parse_uob(pdf_path)
    if "DBS Cards" in text_head:
        return parse_dbs_cc(pdf_path)
    if "Cashline" in text_head:
        return parse_dbs_cashline(pdf_path)
    if "GXS" in text_head:
        return parse_gxs(pdf_path)
    return None
