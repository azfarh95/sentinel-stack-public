"""PDF statement parsers for Maybank + SC Savings.

Maybank Ar Rihla Regular Savings (account 14030791138 -> Firefly #171)
  Format:
    Account No.: <number>
    Date Transaction Description Withdrawal ($) Deposit ($) Balance ($)
    01 Apr Opening Balance 10.56
    30 Apr Service Charge 2.00 8.56
    30 Apr Closing Balance 8.56

SC Current Savings (account 01-1-783334-7 -> Firefly #172)
  Format:
    SUPERSALARY ACCOUNT 01-1-783334-7 (SGD)
    Date Description Deposit Withdrawal Balance
    31 Mar 2026 BALANCE FROM PREVIOUS STATEMENT 32.48
    01 Apr 2026 INWARD CREDIT FEE 5.00 27.48
    09 Apr 2026 IBFT|... 260.00 287.48
    13 Apr 2026 TRANSFER WITHDRAWAL NTRF 257.77 29.71
                TO CARD 5498341645008810
    30 Apr 2026 CLOSING BALANCE 260.00 262.77 29.71

Both produce the same dict shape as posb_ibanking_importer.parse_csv() so
the rest of the import pipeline (classifier, ledger post, ImportLog,
auto-reconcile) is reusable.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, date
from pathlib import Path

import httpx
import pdfplumber

from . import classifier as _cls

logger = logging.getLogger(__name__)

FIREFLY_URL = os.environ.get("FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180")

# Firefly asset-account IDs per bank (from balance_sheet_config.yaml).
MAYBANK_SAVINGS_ID = 171
SC_SAVINGS_ID = 172

# Month abbreviation -> number, used to parse "01 Apr" and "31 Mar 2026" styles.
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

DATE_RE = re.compile(r"^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?:\s+(\d{4}))?")
AMOUNT_RE = re.compile(r"^[\d,]+\.\d{2}$")


def _detect_bank(text: str) -> str | None:
    if "Ar Rihla" in text or "Maybank Singapore" in text:
        return "maybank"
    if "Standard Chartered Bank" in text or "SUPERSALARY ACCOUNT" in text:
        return "sc"
    return None


def _parse_amount(s: str) -> float:
    return float(s.replace(",", ""))


def _iso(day: int, month_abbr: str, year: int) -> str:
    m = _MONTHS.get(month_abbr)
    if not m:
        raise ValueError(f"unknown month: {month_abbr}")
    return date(year, m, day).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Maybank parser
# ─────────────────────────────────────────────────────────────────────────────

def parse_maybank(path: Path) -> dict:
    """Parse Maybank Ar Rihla Regular Savings PDF.

    Layout per-line:
      Date  Description ...  Withdrawal($)  Deposit($)  Balance($)
    """
    with pdfplumber.open(path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)

    # Account number (e.g. "Account No.: 14030791138")
    acct_match = re.search(r"Account No\.:\s*(\S+)", text)
    account_number = acct_match.group(1) if acct_match else None

    # Statement date (e.g. "As at 30 April 2026")
    sd_match = re.search(r"As at\s+(\d{1,2})\s+(\w+)\s+(\d{4})", text)
    statement_date = None
    year = date.today().year
    if sd_match:
        try:
            statement_date = datetime.strptime(
                f"{sd_match.group(1)} {sd_match.group(2)[:3]} {sd_match.group(3)}",
                "%d %b %Y").date().isoformat()
            year = int(sd_match.group(3))
        except ValueError:
            pass

    transactions: list[dict] = []
    opening_balance = None
    closing_balance = None

    # Walk lines; identify each tx row by date prefix
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = DATE_RE.match(line)
        if not m:
            continue
        # Trailing tokens are amounts: split off all trailing numeric tokens
        tokens = line.split()
        amounts: list[str] = []
        while tokens and AMOUNT_RE.match(tokens[-1]):
            amounts.append(tokens.pop())
        amounts.reverse()
        desc_tokens = tokens[2:]  # skip date day + month
        if "Opening Balance" in " ".join(desc_tokens):
            if amounts:
                opening_balance = _parse_amount(amounts[-1])
            continue
        if "Closing Balance" in " ".join(desc_tokens):
            if amounts:
                closing_balance = _parse_amount(amounts[-1])
            continue
        if "Total" in " ".join(desc_tokens):
            continue

        # Layout: [withdrawal, deposit, balance] OR [withdrawal, balance] OR [deposit, balance]
        # Heuristic: last is balance; first is withdrawal/deposit; middle is the other if present.
        if len(amounts) < 2:
            continue
        balance = _parse_amount(amounts[-1])
        prev_balance = opening_balance if opening_balance is not None else None
        # Try to figure direction by comparing to running balance
        movement = _parse_amount(amounts[0])
        direction = "out"
        if prev_balance is not None:
            inferred = prev_balance + movement
            if abs(inferred - balance) < 0.01:
                direction = "in"
            else:
                direction = "out"
        else:
            # 2-amount form usually means (movement, balance) — guess from desc
            descl = " ".join(desc_tokens).lower()
            if any(k in descl for k in ("credit", "deposit", "inward", "refund")):
                direction = "in"
            else:
                direction = "out"

        amount = movement if direction == "in" else -movement
        date_iso = _iso(int(m.group(1)), m.group(2), year)

        transactions.append({
            "date": date_iso,
            "code": "MB",
            "description": " ".join(desc_tokens),
            "ref1": "", "ref2": "", "ref3": "",
            "debit": 0.0 if direction == "in" else movement,
            "credit": movement if direction == "in" else 0.0,
            "amount": round(amount, 2),
        })
        opening_balance = balance  # update running balance for next inference

    return {
        "bank": "maybank",
        "account_number": account_number,
        "firefly_account_id": MAYBANK_SAVINGS_ID,
        "statement_date": statement_date,
        "opening_balance": opening_balance,
        "ledger_balance": closing_balance,
        "available_balance": closing_balance,
        "transactions": transactions,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Standard Chartered parser
# ─────────────────────────────────────────────────────────────────────────────

SC_DATE_RE = re.compile(r"^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})\b")


def parse_sc(path: Path) -> dict:
    """Parse Standard Chartered Current Savings PDF.

    Layout per-line (after BALANCE FROM PREVIOUS STATEMENT):
      DD MMM YYYY  Description  [Deposit]  [Withdrawal]  Balance
    Multi-line descriptions: continuation lines have NO leading date.
    """
    with pdfplumber.open(path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)

    # Account number (e.g. "SUPERSALARY ACCOUNT 01-1-783334-7 (SGD)")
    acct_match = re.search(r"ACCOUNT\s+(\S+)\s+\(SGD\)", text)
    account_number = acct_match.group(1) if acct_match else None

    sd_match = re.search(r"Statement Date\s*:\s*(\d{1,2})\s+(\w+)\s+(\d{4})", text)
    statement_date = None
    if sd_match:
        try:
            statement_date = datetime.strptime(
                f"{sd_match.group(1)} {sd_match.group(2)[:3]} {sd_match.group(3)}",
                "%d %b %Y").date().isoformat()
        except ValueError:
            pass

    transactions: list[dict] = []
    opening_balance = None
    closing_balance = None
    last_balance = None

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i].strip()
        i += 1
        m = SC_DATE_RE.match(raw)
        if not m:
            continue

        # IMPORTANT: extract amounts from the FIRST line only (the one with
        # the date prefix). Continuation lines are description-only — their
        # trailing tokens (e.g. "TRANSFER") would break amount detection.
        first_tokens = raw.split()
        amounts: list[str] = []
        while first_tokens and AMOUNT_RE.match(first_tokens[-1]):
            amounts.append(first_tokens.pop())
        amounts.reverse()
        first_desc_tokens = first_tokens[3:]  # strip date day, month, year

        # Collect continuation lines (description-only)
        continuation: list[str] = []
        while i < len(lines):
            nxt = lines[i].strip()
            if not nxt:
                i += 1; continue
            if SC_DATE_RE.match(nxt):
                break
            if any(stop in nxt for stop in ("Page ", "Deposit Insurance",
                                              "Singapore dollar deposits",
                                              "Standard Chartered Bank",
                                              "Your Statement",
                                              "If you note",
                                              "This statement serves",
                                              "Cashback Summary",
                                              "Mastercard Spend")):
                break
            continuation.append(nxt)
            i += 1

        date_iso = _iso(int(m.group(1)), m.group(2), int(m.group(3)))
        desc = " ".join(first_desc_tokens + continuation)

        if "BALANCE FROM PREVIOUS STATEMENT" in desc.upper():
            if amounts:
                opening_balance = _parse_amount(amounts[-1])
                last_balance = opening_balance
            continue
        if "CLOSING BALANCE" in desc.upper():
            if amounts:
                closing_balance = _parse_amount(amounts[-1])
            continue

        if len(amounts) < 2 or last_balance is None:
            continue
        balance = _parse_amount(amounts[-1])
        # Movement = (balance - last_balance) — sign carries direction
        movement_signed = round(balance - last_balance, 2)
        direction = "in" if movement_signed > 0 else "out"
        movement = abs(movement_signed)
        last_balance = balance

        transactions.append({
            "date": date_iso,
            "code": "SC",
            "description": desc[:200],
            "ref1": "", "ref2": "", "ref3": "",
            "debit": 0.0 if direction == "in" else movement,
            "credit": movement if direction == "in" else 0.0,
            "amount": movement_signed,
        })

    return {
        "bank": "sc",
        "account_number": account_number,
        "firefly_account_id": SC_SAVINGS_ID,
        "statement_date": statement_date,
        "opening_balance": opening_balance,
        "ledger_balance": closing_balance,
        "available_balance": closing_balance,
        "transactions": transactions,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch + Firefly post
# ─────────────────────────────────────────────────────────────────────────────

def parse_pdf(path: Path) -> dict | None:
    """Auto-detect bank format + parse. Returns None if format unknown."""
    with pdfplumber.open(path) as pdf:
        first = pdf.pages[0].extract_text() or ""
    bank = _detect_bank(first)
    if bank == "maybank":
        return parse_maybank(path)
    if bank == "sc":
        return parse_sc(path)
    return None


def _firefly_headers() -> dict:
    pat = os.environ.get("FIREFLY_PAT", "")
    return {"Authorization": f"Bearer {pat}", "Accept": "application/json",
            "Content-Type": "application/json"}


def _post_transaction(tx: dict, account_id: int, account_name: str) -> tuple[str, str]:
    """Same shape as posb_ibanking_importer._post_transaction."""
    amount = tx["amount"]
    is_outflow = amount < 0
    match = _cls.classify_or_default(tx["description"])
    counterparty = match.canonical
    txn = {
        "date": tx["date"],
        "amount": f"{abs(amount):.2f}",
        "description": (tx["description"] or counterparty)[:255],
        "notes": tx["description"][:1000],
        "external_id": (tx["description"] + "|" + tx["date"])[:255],
        "category_name": match.category[:255],
    }
    if is_outflow:
        txn["type"] = "withdrawal"
        txn["source_name"] = account_name
        txn["destination_name"] = counterparty[:255] or "Unknown"
    else:
        txn["type"] = "deposit"
        txn["source_name"] = counterparty[:255] or "Unknown"
        txn["destination_name"] = account_name
    payload = {
        "error_if_duplicate_hash": True,
        "apply_rules": True,
        "fire_webhooks": False,
        "transactions": [txn],
    }
    try:
        r = httpx.post(f"{FIREFLY_URL}/api/v1/transactions",
                       headers=_firefly_headers(), json=payload, timeout=15)
        if r.status_code in (200, 201):
            return ("created", f"{tx['date']} {amount:+.2f}")
        body = r.text[:300]
        if "Duplicate" in body or "duplicate" in body:
            return ("dup", "duplicate hash")
        return ("error", f"HTTP {r.status_code} · {body[:120]}")
    except Exception as e:
        return ("error", str(e)[:200])


def _firefly_balance(account_id: int) -> float | None:
    try:
        r = httpx.get(f"{FIREFLY_URL}/api/v1/accounts/{account_id}",
                      headers=_firefly_headers(), timeout=8)
        if r.status_code == 200:
            return float(r.json()["data"]["attributes"]["current_balance"])
    except Exception:
        pass
    return None


def import_pdf(path: Path) -> dict:
    """Parse a PDF + POST each row to Firefly. Returns import summary."""
    parsed = parse_pdf(path)
    if not parsed:
        return {"file": path.name, "error": "unknown PDF format"}
    aid = parsed["firefly_account_id"]
    account_name = "Maybank Savings" if parsed["bank"] == "maybank" else "Standard Chartered Savings"
    counts = {"created": 0, "dup": 0, "error": 0}
    errors: list[str] = []
    for tx in parsed["transactions"]:
        status, detail = _post_transaction(tx, aid, account_name)
        counts[status] = counts.get(status, 0) + 1
        if status == "error":
            errors.append(f"{tx['date']} {tx['amount']:+.2f}: {detail}")
    # Post-import reconcile
    ff_bal = _firefly_balance(aid)
    variance = None
    if ff_bal is not None and parsed.get("ledger_balance") is not None:
        variance = round(ff_bal - parsed["ledger_balance"], 2)
    return {
        "file": path.name,
        "bank": parsed["bank"],
        "account_number": parsed["account_number"],
        "account_id": aid,
        "account_name": account_name,
        "statement_date": parsed["statement_date"],
        "opening_balance": parsed["opening_balance"],
        "ledger_balance": parsed["ledger_balance"],
        "firefly_balance": ff_bal,
        "variance": variance,
        "n_rows": len(parsed["transactions"]),
        "created": counts["created"],
        "dup": counts["dup"],
        "errored": counts["error"],
        "errors": errors[:10],
    }
