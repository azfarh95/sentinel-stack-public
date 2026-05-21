"""POSB iBanking CSV → Sentinel GL (direct, no Firefly).

v2.0 ingestion path: POSB CSV exports preserve recipient identifier in the
`Description` + Transaction Ref1/2/3 columns. This module parses the CSV and
posts each transaction DIRECTLY into the Sentinel GL — bypassing Firefly III.

Why this matters (decouple-from-Firefly directive 2026-05-14):
  - POSB PDF source lacks recipient info; PDF→CSV converter inherited that gap
  - Firefly's destination_name="Unknown" for $109k of FAST Payment outflows
  - POSB iBanking CSV has the missing info (e.g. "Wise:3427002", "Singapore Life
    Ltd AVI16172585", "PayNow Transfer To: Kalsum", etc.)

Each posted journal:
  - source_doc = "POSB_CSV"
  - source_ref = tx Reference1 (or composite if blank)
  - external_id = sha256 of date+amount+description (idempotent re-runs)
  - narration preserves the full description

Run:
    docker exec portfolio-mcp python -m app.posb_csv_to_gl <file.csv>             # dry-run
    docker exec portfolio-mcp python -m app.posb_csv_to_gl <file.csv> --post      # post
    docker exec portfolio-mcp python -m app.posb_csv_to_gl <folder>               # batch dry-run
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import database as db
from . import journal_service as js

logger = logging.getLogger(__name__)


POSB_CSV_HEADER_LINE = "Transaction Date"


# ── Pattern → (other_leg_coa, kind, default_narration_prefix) ────────────────
# Order matters: first match wins. Patterns checked against the Description field.
PATTERNS_OUTFLOW = [
    # Internal transfers (asset → asset, no P&L)
    ("Wise:",                       "1113",  "transfer", "Wise top-up"),
    ("AUTO TOP UP FROM CASHLINE",   "2121",  "transfer", "DBS Cashline drawdown"),  # CR Cashline (liab up)
    ("AUTO REPAY FROM CASHLINE",    "2121",  "transfer", "DBS Cashline repay"),
    # Salary inflows
    ("AZ UNITED",                   "4110",  "income",   "Salary — AZ United"),
    ("HENDERSON SECURITY",          "4120",  "income",   "Salary — YourAgency"),
    ("SAF IMPREST",                 "4130",  "income",   "SAF Imprest reimbursement"),
    # SC Balance Transfer disbursement (RTL- prefix with SCBL in ref)
    ("SCBLSG22BRT",                 "2211",  "loan_in",  "SC BT disbursement"),
    # Insurance premium outflows
    ("SINGAPORE LIFE",              "5340",  "expense",  "Singlife — premium"),
    ("TOKIO MARINE",                "5340",  "expense",  "Tokio Marine — premium"),
    # Common SG merchants → categories
    ("FOODPANDA",                   "5111",  "expense",  "Food delivery"),
    ("GRABFOOD",                    "5111",  "expense",  "Food delivery"),
    ("DELIVEROO",                   "5111",  "expense",  "Food delivery"),
    ("ATOME",                       "2115",  "loan_out", "Atome BNPL"),  # DR 2115 (Atome liab down)
    ("RM FOOD MANUFACTURING",       "5110",  "expense",  "F&B"),
    ("ATLASVENDING",                "5110",  "expense",  "F&B (vending)"),
    ("VIEWQWEST",                   "5141",  "expense",  "Internet (Viewqwest)"),
    ("SHOPEE",                      "5160",  "expense",  "Shopee"),
    ("LAZADA",                      "5161",  "expense",  "Lazada"),
    ("GRABRIDES",                   "5130",  "expense",  "Transport"),
    ("TADA",                        "5130",  "expense",  "Transport"),
    ("COMFORT",                     "5130",  "expense",  "Transport"),
    ("ANTHROPIC",                   "5200",  "expense",  "Subscription"),
    ("MICROSOFT",                   "5200",  "expense",  "Subscription"),
    # CC bill payments (POSB → CC liability)
    ("4119-1101-0497-2424",         "2111",  "cc_pay",   "DBS CC payment"),
    ("4966-4309-0492-7004",         "2112",  "cc_pay",   "Maybank CC payment"),
    ("5498-3416-4500-8810",         "2113",  "cc_pay",   "SC CC payment"),
    ("4835",                        "2114",  "cc_pay",   "HSBC CC payment"),
    # PayNow / FAST to known counterparties
    ("To: Qashier-Milah Delights",  "5110",  "expense",  "F&B (Milah)"),
    ("To: Kalsum",                  "5170",  "expense",  "Family transfer"),
]

PATTERNS_INFLOW = [
    ("SAF IMPREST",                 "4130",  "income",   "SAF Imprest"),
    ("AZ UNITED",                   "4110",  "income",   "AZ United salary"),
    ("HENDERSON SECURITY",          "4120",  "income",   "YourAgency salary"),
    ("SCBLSG22BRT",                 "2211",  "loan_in",  "SC BT disbursement"),
    ("AUTO TOP UP FROM CASHLINE",   "2121",  "loan_in",  "DBS Cashline drawdown"),
    ("Wise:",                       "1113",  "transfer", "Wise inflow"),
    ("Interest",                    "4220",  "income",   "Interest earned"),
    # MEPS Receipt disbursements (known events from amount_match_reconciler)
    # Generic — fallback below
]


SUSPENSE = "1190"          # for ambiguous tx
GENERAL_EXPENSE = "5190"   # last-resort fallback for outflows
OTHER_INCOME = "4900"      # last-resort for inflows
POSB = "1111"


@dataclass
class POSBTx:
    date: datetime
    code: str               # ICT | IBG | UMC-S | SAL | ADV | INT | GIRO | ...
    description: str
    ref1: str
    ref2: str
    ref3: str
    debit: float            # outflow from POSB
    credit: float           # inflow to POSB
    status: str

    @property
    def is_inflow(self) -> bool:
        return self.credit > 0

    @property
    def amount(self) -> float:
        return self.credit if self.is_inflow else self.debit

    def external_id(self) -> str:
        """Stable hash for idempotent posting."""
        key = f"{self.date.isoformat()}|{self.code}|{self.description[:80]}|{self.amount:.2f}|{self.ref3 or self.ref2 or self.ref1}"
        return "posbcsv:" + hashlib.sha256(key.encode()).hexdigest()[:24]


def _money(s: str) -> float:
    if not s or s.strip() in ("", '""'):
        return 0.0
    s = s.replace(",", "").strip()
    try:
        return float(s)
    except Exception:
        return 0.0


def parse_csv(path: Path) -> list[POSBTx]:
    """Parse one POSB iBanking CSV. Skips the metadata header rows."""
    txs: list[POSBTx] = []
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        text = f.read()
    idx = text.find(POSB_CSV_HEADER_LINE)
    if idx < 0:
        return txs
    # Back up to the start of the header line (include leading quote so DictReader
    # parses headers correctly).
    line_start = text.rfind("\n", 0, idx) + 1
    body = text[line_start:]
    reader = csv.DictReader(body.splitlines())
    for row in reader:
        try:
            dt = datetime.strptime(row["Transaction Date"], "%d %b %Y")
        except Exception:
            continue
        txs.append(POSBTx(
            date=dt,
            code=row.get("Transaction Code", "").strip(),
            description=row.get("Description", "").strip(),
            ref1=row.get("Transaction Ref1", "").strip(),
            ref2=row.get("Transaction Ref2", "").strip(),
            ref3=row.get("Transaction Ref3", "").strip(),
            debit=_money(row.get("Debit Amount", "")),
            credit=_money(row.get("Credit Amount", "")),
            status=row.get("Status", "").strip(),
        ))
    return txs


def classify(tx: POSBTx) -> tuple[str, str, str]:
    """Match tx → (other_leg_coa, kind, narration_prefix). Falls back to suspense
    when no pattern matches."""
    patterns = PATTERNS_INFLOW if tx.is_inflow else PATTERNS_OUTFLOW
    haystack = (tx.description + " " + tx.ref1 + " " + tx.ref2 + " " + tx.ref3).lower()
    for needle, coa, kind, label in patterns:
        if needle.lower() in haystack:
            return coa, kind, label
    # Fallback: suspense (preferred over generic 5190/4900 which are catch-all garbage buckets)
    return (SUSPENSE, "unclassified", "POSB unclassified")


def build_lines(tx: POSBTx, other_coa: str, kind: str) -> list[dict]:
    """Compose DR/CR pair for one POSB tx.

    - Inflow (CR Amount > 0): DR POSB ↑, CR other_coa ↑ (income) or DR POSB ↑, CR other_coa ↑ (asset/liability)
    - Outflow (DR Amount > 0): CR POSB ↓, DR other_coa ↑ (expense) or DR other_coa ↑ (liability paydown / asset)
    """
    if tx.is_inflow:
        return [
            {"account_code": POSB, "debit": tx.credit, "narration": tx.description[:120]},
            {"account_code": other_coa, "credit": tx.credit, "narration": tx.description[:120]},
        ]
    return [
        {"account_code": other_coa, "debit": tx.debit, "narration": tx.description[:120]},
        {"account_code": POSB, "credit": tx.debit, "narration": tx.description[:120]},
    ]


def post_tx(s, tx: POSBTx) -> int | None:
    """Post one POSB tx as a balanced journal. Idempotent via external_id."""
    if tx.status and tx.status.lower() != "settled":
        return None
    other_coa, kind, label = classify(tx)
    lines = build_lines(tx, other_coa, kind)
    return js.post_journal(
        s,
        journal_date=tx.date.date(),
        narration=f"[POSB] {tx.description[:80]}",
        journal_type=kind,
        lines=lines,
        source_doc="POSB_CSV",
        source_ref=tx.ref1[:60] or tx.code,
        external_id=tx.external_id(),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="POSB CSV file or folder containing CSVs")
    ap.add_argument("--post", action="store_true", help="Actually post journals")
    ap.add_argument("--limit", type=int, default=None, help="Limit to first N tx (testing)")
    args = ap.parse_args()

    target = Path(args.target)
    files = sorted(target.glob("*.csv")) if target.is_dir() else [target]

    all_tx = []
    for f in files:
        txs = parse_csv(f)
        all_tx.extend(txs)
        print(f"  {f.name[:50]:<52}  {len(txs)} tx")
    if args.limit:
        all_tx = all_tx[:args.limit]
    if not all_tx:
        print("No transactions parsed.")
        return

    # Classify all
    classified: dict[str, int] = {}
    by_other_coa: dict[str, list] = {}
    for tx in all_tx:
        coa, kind, label = classify(tx)
        classified[label] = classified.get(label, 0) + 1
        by_other_coa.setdefault(coa, []).append(tx)

    print(f"\n=== Classification summary ({len(all_tx)} tx) ===\n")
    print(f"  {'CoA':<6} {'#':>5} {'Direction':<10} {'Sample narration'}")
    print("  " + "-" * 80)
    for coa, txs in sorted(by_other_coa.items(), key=lambda kv: -len(kv[1])):
        dirs = "in" if any(t.is_inflow for t in txs) else ""
        dirs += "out" if any(not t.is_inflow for t in txs) else ""
        sample = txs[0].description[:50]
        print(f"  {coa:<6} {len(txs):>5} {dirs:<10} {sample}")

    if args.post:
        db.init_db()
        s = db.SessionLocal()
        posted = 0
        skipped = 0
        errors = 0
        try:
            for tx in all_tx:
                try:
                    jid = post_tx(s, tx)
                    if jid is None:
                        skipped += 1
                        continue
                    s.commit()
                    posted += 1
                except Exception as e:
                    s.rollback()
                    errors += 1
                    if errors <= 5:
                        print(f"  ERR: {tx.description[:50]}: {str(e)[:60]}")
        finally:
            s.close()
        print(f"\n  Posted: {posted}  Skipped: {skipped}  Errors: {errors}")
    else:
        print("\n  DRY-RUN — pass --post to write journals.")


if __name__ == "__main__":
    main()
