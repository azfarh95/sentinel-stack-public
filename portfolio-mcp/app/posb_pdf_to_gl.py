"""POSB PDF → Sentinel GL with FULL multi-line description extraction.

Discovery 2026-05-14: POSB monthly statements include recipient name + reference
across 2-5 continuation lines BELOW the date+type+amount summary line. The
previous `posb_to_firefly_csv.py` converter was reading only the summary line
and discarding the detail.

Example multi-line tx in POSB PDF:
    04 Feb FAST Payment / Receipt 498.72
      PayNow Transfer 5636354
      To: EZ LOAN PTE.LTD.
      EL-14603 2026
      Other

This parser:
  1. Splits PDF text into transactions by detecting `^<DD> <MMM> <TYPE> <amount>` start lines
  2. Captures all continuation text up to the next start line OR "Balance Carried Forward"
  3. Classifies each tx via pattern matching against the full description
  4. Determines direction (in/out) using balance-column change between tx
  5. Outputs CSV for review (no GL posting until cutover decision made)

Run:
    docker exec portfolio-mcp python -m app.posb_pdf_to_gl                       # scan all PDFs, write CSV
    docker exec portfolio-mcp python -m app.posb_pdf_to_gl --file <one.pdf>      # one file
    docker exec portfolio-mcp python -m app.posb_pdf_to_gl --post                # post journals (gated)
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date as _date, datetime
from pathlib import Path

from . import cc_statement_parser as ccp
from . import database as db
from . import journal_service as js

logger = logging.getLogger(__name__)

POSB_PDF_ROOT = Path("/onedrive/Sentinel Finance/01_Bank statements/DBS_POSB Savings")
OUTPUT_CSV = Path("/data/posb_full_extract.csv")

# Transaction type keywords that can appear in a tx start line.
TX_TYPES = [
    "FAST Payment / Receipt",
    "Debit Card transaction",
    "Payments / Collections via GIRO",
    "Bill Payment",
    "Salary",
    "Interest Earned",
    "My Preferred Payment Plan from Credit Card",
    "Point-of-Sale Transaction",
    "Cash Withdrawal",
    "Cash Deposit",
    "Standing Instruction",
    "Funds Transfer",
    "Inward Credit",
    "Inward IBG",
    "Wire Transfer",
    "ATM Cash Withdrawal",
    "Cheque",
]


# ── Classification rules — each is (regex_on_description, coa, kind, label) ──
# Order matters: first match wins. Patterns are case-insensitive.
# Build this by mining the suspense bucket, NOT from imagination — see
# `journal/posb-classifier-coverage.md` for the source data behind each rule.
RULES = [
    # ── HIGH-CONFIDENCE: internal transfers (asset/liability) ──────────────
    (r"To:\s*EZ LOAN PTE",                       "2221", "loan_pay",  "EZ Loan repayment"),
    (r"To:\s*LENDING BEE",                       "2222", "loan_pay",  "Lending Bee repayment"),
    (r"To:\s*SANDS CREDIT",                      "2223", "loan_pay",  "Sands Credit repayment"),
    (r"Wise:\d+|WISE ASIA-PACIFIC",              "1113", "transfer",  "Wise transfer"),
    (r"AUTO TOP UP FROM CASHLINE",               "2121", "loan_in",   "DBS Cashline drawdown"),
    (r"AUTO REPAY FROM CASHLINE",                "2121", "loan_pay",  "DBS Cashline repay"),
    (r"SCBLSG\d+BRT",                            "2211", "loan_in",   "SC BT disbursement"),
    (r"To:\s*Singlife Savvy Invest|P4064051",    "1222", "transfer",  "Singlife ILP premium"),
    (r"To:\s*COINBASE SINGAPORE",                "1231", "transfer",  "Coinbase top-up"),
    (r"To:\s*SEAMONEY|To:\s*MONEE",              "1112", "transfer",  "ShopeePay wallet (SeaMoney/Monee)"),
    (r"To:\s*APAYLATER",                         "2115", "bnpl_pay",  "SPayLater repayment"),
    # CC bill payments — match by card number OR DBS Visa Direct prefix
    (r"4119[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}|DBS\s+VISA\s+DIRECT|DBS_VISA",  "2111", "cc_pay", "DBS CC payment"),
    (r"4966[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}|MBB\s+CC",                       "2112", "cc_pay", "Maybank CC payment"),
    (r"5498[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}",                                "2113", "cc_pay", "SC CC payment"),
    (r"4835[-\s]?\w{4}[-\s]?\w{4}[-\s]?\d{4}",                                "2114", "cc_pay", "HSBC CC payment"),
    # My Preferred Payment Plan (DBS internal CC instalment) — moves CC plan principal
    (r"My Preferred Payment Plan",               "2111", "cc_plan",   "DBS MyPreferredPaymentPlan"),
    # ── Income (inbound) ────────────────────────────────────────────────────
    (r"AZ UNITED",                               "4110", "income",    "AZ United salary"),
    (r"HENDERSON SECURITY",                      "4120", "income",    "YourAgency salary"),
    (r"SAF IMPREST",                             "4130", "income",    "SAF Imprest reimbursement"),
    (r"Interest Earned",                         "4220", "income",    "Interest earned"),
    (r"EDUSAVE|PSEA",                            "4300", "income",    "Government transfer"),
    (r"CASHBACK|LENDINGPOT",                     "4900", "income",    "Cashback"),
    # ── Singlife Savvy Invest (ILP) — premium goes to ASSET, not expense ───
    # MUST come before generic SINGAPORE LIFE LTD rule so it wins.
    # Policy ref: P4064051 / P4064051170373766 ($252.85/mo GIRO).
    (r"P4064051|Savvy\s*Invest",                "1222", "ilp_premium", "Singlife Savvy Invest premium"),
    # ── Pure-insurance Singlife products → expense ─────────────────────────
    (r"SGLF-\d+|SINGAPORE LIFE LTD|Singapore Life Ltd|AVI\d+", "5340", "expense", "Singlife premium"),
    (r"TOKIO MARINE",                            "5340", "expense",   "Tokio Marine premium"),
    (r"AIA Singapore",                           "5340", "expense",   "AIA premium"),
    (r"PRUDENTIAL",                              "5340", "expense",   "Prudential premium"),
    (r"NTUC INCOME|INCOME INSUR",                "5340", "expense",   "NTUC Income premium"),
    # ── BNPL (Atome) ───────────────────────────────────────────────────────
    (r"ATOME",                                   "2115", "bnpl_pay",  "Atome BNPL payment"),
    # ── F&B (delivery) ──────────────────────────────────────────────────────
    (r"FoodPanda|fp\*Food\s*Panda|GrabFood|Deliveroo|deliveroo",
                                                 "5111", "expense", "Food delivery"),
    # ── F&B (dine-in / vendor list mined from suspense) ────────────────────
    (r"FAIRPRICE|NTUC|COLD STORAGE|GIANT|SHENGSIONG|SHENG\s*SIONG",
                                                 "5120", "expense", "Groceries"),
    (r"RM FOOD MANUFACTURING|ATLASVENDING|ATLAS VENDING|MAKAN|EATING HOUSE|FOOD HALL|"
     r"KOUFU|KOPITIAM|TUCKSHOP|BELACAN|FIRST CUISINE|DELIGHTS|RESTAURANT|CAFE|"
     r"O'MY DARLING|PERIYAKARUPPAN|ZACKARIA|"
     r"ENAK ENAK|PETER F AND B|AL AFROSE|BUY FISH|KITTY SHOP|MALIM TRADERS|"
     r"TAMPINES BI|CHEERS|7-ELEVEN|SEVEN-ELEVEN|SEVENELEVEN|"
     r"SUBWAY|MCDONALD|KFC|BURGER KING|STARBUCKS|COFFEE BEAN|"
     r"PIZZA HUT|TOAST BOX|YA KUN|OLD CHANG KEE|KOPITAM|JOLLIBEE|"
     r"TONG\s+AIK|BREAD\s+TALK",
                                                 "5110", "expense", "F&B"),
    # ── Transport ───────────────────────────────────────────────────────────
    (r"Grab\*?|grab\s*gpc|gojek|TADA|comfort|BUS/MRT|BUS\s*MRT|SBS\s+TRANSIT|SMRT|"
     r"GO-?JEK|RYDE|EASYVAN|EZ-LINK|EZLINK",
                                                 "5131", "expense", "Transport"),
    (r"SHELL|ESSO|CALTEX|SPC\s+PETROL|SINOPEC",  "5132", "expense", "Fuel"),
    # ── Utilities ───────────────────────────────────────────────────────────
    (r"Viewqwest|SINGTEL|STARHUB|M1\s|MyRepublic|SIMBA TELECOM|CIRCLES\.LIFE|"
     r"WHIZCOMMS",
                                                 "5141", "expense", "Internet/mobile"),
    (r"SP\s+SERVICES|SP\s+GROUP|SP\s+UTILITIES|TUAS\s+POWER|GENECO|"
     r"SENOKO\s+ENERGY|UNION\s+POWER|BEST\s+ELECTRICITY",
                                                 "5143", "expense", "Electricity/utility"),
    (r"GOOGLE\*GOOGLE\s+ONE|ICLOUD|DROPBOX|ONEDRIVE",
                                                 "5142", "expense", "Cloud storage"),
    # ── Subscriptions / Tools / Digital services ───────────────────────────
    (r"ANTHROPIC|Anthropic|CLAUDE|MICROSOFT\s*\*?|TELEGRAM|WEBSHARE|Onlyfans|"
     r"MMBILL|MRCR\s|TWITCH|GOOGLE\*YOUTUBE|GOOGLE\s*ONE|NETFLIX|SPOTIFY|"
     r"DOCKER,\s*INC|GITHUB|OPENAI|CURSOR|FIGMA|NOTION|EVERNOTE|"
     r"PATREON|SUBSTACK|MEDIUM|LINKEDIN",
                                                 "5200", "expense", "Subscription"),
    # ── Government / Tax / AXS ─────────────────────────────────────────────
    (r"AXS PTE",                                 "5500", "expense", "AXS bill payment (tax/fines/utility)"),
    (r"IRAS|Inland Revenue",                     "5500", "expense", "IRAS tax"),
    (r"NET\*CREDIT BUREAU",                      "5600", "expense", "Credit bureau report fee"),
    (r"ICA\s|Immigration",                       "5600", "expense", "Government fee"),
    # ── Shopping ───────────────────────────────────────────────────────────
    (r"SHOPEE",                                  "5160", "expense", "Shopee"),
    (r"LAZADA",                                  "5161", "expense", "Lazada"),
    (r"AMAZON|EBAY|ALIEXPRESS|TEMU|SHEIN",       "5161", "expense", "Online shopping"),
    (r"OZON|Kinguin",                            "5160", "expense", "Shopping"),
    # ── Healthcare ─────────────────────────────────────────────────────────
    (r"INTEMEDICAL|CLINIC|HOSPITAL|GUARDIAN|WATSONS|UNITY PHARMACY|"
     r"POLYCLINIC|DENTAL|GP\s+CLINIC",
                                                 "5150", "expense", "Healthcare"),
    # ── Self-PayNow inflows (TX between user's own accounts) ───────────────
    (r"From:\s*[Aa]zfar\s+[Hh]akim",             "1190", "transfer", "Self-PayNow inflow (suspense)"),
    (r"From:\s*AZFAR\s+HAKIM",                   "1190", "transfer", "Self-PayNow inflow (suspense)"),
    # ── Family transfers (named recipients per CSV mining) ─────────────────
    (r"To:\s*(Kalsum|Shahrom|Umiyatun|Aisyah|"
     r"CHEAH HSIAN LING|PEK LEE PENG|ABDUL SALAM ABDULLAH|DESMONDLIM)",
                                                 "5170", "expense", "Family/personal transfer"),
    # ── Generic PayNow to lowercase first-name only → likely personal ──────
    (r"To:\s*[A-Z][a-z]+\s*$",                   "5170", "expense", "Personal-name PayNow"),
    # ── PayNow / FAST to incorporated entities (UEN-style) — keep suspense ─
    (r"To:\s*[A-Z][A-Z\s&.,'\-]+(PTE|PVT|LTD|LLP|LLC|INC|CORP|COY)\.?",
                                                 "1190", "expense", "Entity payment (specific rule needed)"),
    # ── Incoming PayNow / IBG (external) ────────────────────────────────────
    (r"Incoming PayNow|Incoming IBG",            "4900", "income",   "External inflow (review)"),
    # ── Standing Instruction / Bill Payment recurring ──────────────────────
    (r"Standing Instruction.*?AZ|TO\s*:?AZ",     "1190", "transfer", "SI to own account (suspense)"),
    (r"Bill Payment.*?CITIBANK",                 "2120", "expense", "Citibank bill"),
    # ── Cash ───────────────────────────────────────────────────────────────
    (r"Cash Withdrawal",                         "1112", "transfer", "Cash withdrawal to wallet"),
    (r"Cash Deposit",                            "1112", "transfer", "Cash deposit from wallet"),
    # ── ATM / fees ─────────────────────────────────────────────────────────
    (r"NETS QR PAYMENT",                         "5110", "expense", "NETS QR (likely F&B)"),
    (r"NETS CONTACTLESS",                        "5110", "expense", "NETS contactless (likely F&B)"),
    (r"GIRO.*?REJECTED|GIRO\s+RETURNED",         "5700", "expense", "GIRO rejection fee"),
    (r"Service Charge|MONTHLY FEE|MONTHLY CHARGE", "5700", "expense", "Bank service charge"),
]


SUSPENSE = "1190"
POSB = "1111"
GENERAL_EXPENSE = "5190"
OTHER_INCOME = "4900"


@dataclass
class POSBTx:
    date: _date
    txn_type: str
    amount: float
    balance: float | None
    description: str         # full multi-line concat
    is_inflow: bool
    page: int
    source_file: str = ""

    def external_id(self) -> str:
        key = f"{self.date.isoformat()}|{self.txn_type}|{self.amount:.2f}|{self.description[:120]}"
        return "posbpdf:" + hashlib.sha256(key.encode()).hexdigest()[:24]


# ── PDF parsing ─────────────────────────────────────────────────────────────


_DATE_RE = re.compile(r"^(\d{1,2})\s+([A-Z][a-z]{2})$")
_MONTH = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,
          "Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}


def _types_re() -> re.Pattern:
    # Sort by length descending so longest types match first
    types_sorted = sorted(TX_TYPES, key=lambda t: -len(t))
    alt = "|".join(re.escape(t) for t in types_sorted)
    # Pattern: "DD Mon <TYPE> <amount>[ <balance>]"
    return re.compile(
        r"^(?P<day>\d{1,2})\s+(?P<mon>[A-Z][a-z]{2})\s+"
        r"(?P<type>" + alt + r")\s+"
        r"(?P<amount>[\d,]+\.\d{2})"
        r"(?:\s+(?P<balance>[\d,]+\.\d{2}))?\s*$",
        re.MULTILINE)


def _parse_amount(s: str) -> float:
    if not s:
        return 0.0
    return float(s.replace(",", ""))


def parse_pdf(pdf_path: Path, stmt_year: int | None = None) -> list[POSBTx]:
    """Parse one POSB monthly statement PDF → list of POSBTx."""
    text = ccp._extract_text_smart(str(pdf_path))
    if not text:
        return []

    # Determine year from header "As at <DD Mon YYYY>"
    if stmt_year is None:
        m = re.search(r"As at\s+\d{1,2}\s+[A-Z][a-z]{2}\s+(\d{4})", text)
        stmt_year = int(m.group(1)) if m else _date.today().year

    types_re = _types_re()
    matches = list(types_re.finditer(text))
    txs: list[POSBTx] = []

    prev_balance: float | None = None
    for i, m in enumerate(matches):
        day = int(m.group("day"))
        mon = _MONTH.get(m.group("mon"))
        if not mon:
            continue
        # Year crosses Dec→Jan; if month > stmt_year's last month, use prior year
        # POSB stmts are single-month so this isn't an issue; just use stmt_year.
        try:
            d = _date(stmt_year, mon, day)
        except ValueError:
            continue
        txn_type = m.group("type")
        amount = _parse_amount(m.group("amount"))
        balance = _parse_amount(m.group("balance")) if m.group("balance") else None

        # Continuation text = everything between this match's end and next match's start
        # (or end of text). Strip pagination/balance-carry-forward markers.
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        continuation = text[start:end]
        # Drop balance markers and page footers
        continuation = re.sub(
            r"Balance (?:Brought|Carried) Forward[^\n]*",
            "", continuation)
        continuation = re.sub(
            r"PDS_POSBPCMTHE[^\n]*", "", continuation)
        continuation = re.sub(
            r"Page \d+ of \d+", "", continuation)
        continuation = re.sub(
            r"Details of Your[^\n]*", "", continuation)
        continuation = re.sub(
            r"DATE DETAILS OF TRANSACTIONS[^\n]*", "", continuation)
        continuation = re.sub(
            r"Account No\.:[^\n]*", "", continuation)
        # Trim to first ~5 lines (continuation typically 1-4 lines)
        cont_lines = [ln.strip() for ln in continuation.split("\n") if ln.strip()][:6]
        full_desc = txn_type + " | " + " | ".join(cont_lines)

        # Determine direction (in/out)
        # Rule: if balance went DOWN, it's outflow; UP = inflow.
        # When balance column blank on the type line, fall back on the txn type:
        #   Salary, Inward Credit, Interest Earned, Incoming PayNow → inflow
        #   Everything else → outflow
        if balance is not None and prev_balance is not None:
            is_inflow = balance > prev_balance
        else:
            is_inflow = any(s in txn_type.lower() for s in ["salary", "inward", "interest earned"]) \
                       or "incoming paynow" in continuation.lower()
        if balance is not None:
            prev_balance = balance

        txs.append(POSBTx(
            date=d, txn_type=txn_type, amount=amount, balance=balance,
            description=full_desc, is_inflow=is_inflow,
            page=0, source_file=str(pdf_path.name),
        ))
    return txs


# ── Classification ──────────────────────────────────────────────────────────


def classify(tx: POSBTx) -> tuple[str, str, str]:
    """Return (other_leg_coa, kind, label)."""
    desc = tx.description
    for pat, coa, kind, label in RULES:
        if re.search(pat, desc, re.IGNORECASE):
            return coa, kind, label
    return (SUSPENSE, "unclassified", "POSB unclassified")


# ── Output ──────────────────────────────────────────────────────────────────


def write_csv(txs: list[POSBTx], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source_file", "date", "type", "amount", "direction",
                    "balance", "classified_coa", "kind", "label", "external_id",
                    "description"])
        for tx in txs:
            coa, kind, label = classify(tx)
            w.writerow([tx.source_file, tx.date.isoformat(), tx.txn_type,
                        f"{tx.amount:.2f}",
                        "IN" if tx.is_inflow else "OUT",
                        f"{tx.balance:.2f}" if tx.balance is not None else "",
                        coa, kind, label,
                        tx.external_id(),
                        tx.description[:300]])


def render_summary(txs: list[POSBTx]) -> None:
    print(f"\n=== POSB extraction summary ({len(txs)} tx) ===\n")
    if not txs:
        return
    dates = [t.date for t in txs]
    print(f"  Date range: {min(dates)} → {max(dates)}")
    in_n = sum(1 for t in txs if t.is_inflow)
    out_n = len(txs) - in_n
    in_amt = sum(t.amount for t in txs if t.is_inflow)
    out_amt = sum(t.amount for t in txs if not t.is_inflow)
    print(f"  Inflows:  {in_n:>4}  SGD {in_amt:>12,.2f}")
    print(f"  Outflows: {out_n:>4}  SGD {out_amt:>12,.2f}")
    print()
    by_coa: dict[str, dict] = {}
    by_label: Counter = Counter()
    for t in txs:
        coa, kind, label = classify(t)
        by_coa.setdefault(coa, {"count": 0, "amt": 0})
        by_coa[coa]["count"] += 1
        by_coa[coa]["amt"] += t.amount
        by_label[label] += 1
    print(f"  {'CoA':<6} {'#':>5} {'Total $':>12}  Top label")
    print("  " + "-" * 60)
    for coa, d in sorted(by_coa.items(), key=lambda kv: -kv[1]["amt"]):
        # Find dominant label for this coa
        labels = Counter(classify(t)[2] for t in txs if classify(t)[0] == coa)
        top = labels.most_common(1)[0][0]
        print(f"  {coa:<6} {d['count']:>5} {d['amt']:>12,.2f}  {top}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="Parse one PDF (else scan whole Bank Statements folder)")
    ap.add_argument("--post", action="store_true",
                    help="Actually post journals to GL (cutover decision required)")
    ap.add_argument("--out", default=str(OUTPUT_CSV),
                    help=f"Output CSV path (default: {OUTPUT_CSV})")
    args = ap.parse_args()

    files = []
    if args.file:
        files = [Path(args.file)]
    else:
        if not POSB_PDF_ROOT.exists():
            print(f"ERROR: {POSB_PDF_ROOT} not found")
            return
        files = sorted(POSB_PDF_ROOT.glob("*.pdf"))

    all_txs: list[POSBTx] = []
    for pdf in files:
        # Skip non-monthly-statement PDFs in same folder
        if "Deposit Account Statement_" not in pdf.name:
            continue
        # Year from filename (Apr2024 → 2024)
        m = re.search(r"_(\w+)(\d{4})\.pdf$", pdf.name)
        year = int(m.group(2)) if m else None
        txs = parse_pdf(pdf, stmt_year=year)
        all_txs.extend(txs)
        print(f"  {pdf.name[:55]:<58}  {len(txs)} tx")

    render_summary(all_txs)
    write_csv(all_txs, Path(args.out))
    print(f"\n  CSV written: {args.out}")

    if args.post:
        print("\n  --post requested but cutover decision not made. Skipping write.")
        # When user signs off, the post block goes here.


if __name__ == "__main__":
    main()
