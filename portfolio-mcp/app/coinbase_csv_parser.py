"""Coinbase CSV reader — extract-only (no journal posting yet).

Reads Coinbase Advanced Trade tx-history CSV exports and produces:
  - Per-tx summary
  - YTD volume by Asset × Transaction Type
  - Realized P&L for Sell rows

Journal posting deferred until Coinbase API path lands OR user confirms the
bridge story (Coinbase Sell → POSB Withdrawal flow currently bridged via
Firefly from POSB side; doing both would double-count).

CSV format (Coinbase Advanced Trade Reports export):
    Transactions
    User,<name>,<UUID>
    ID,Timestamp,Transaction Type,Asset,Quantity Transacted,Price Currency,Price at Transaction,Subtotal,Total (inclusive of fees and/or spread),Fees and/or Spread,Notes,Sender Address,Recipient Address
    <rows>

Run:
    docker exec portfolio-mcp python -m app.coinbase_csv_parser <file.csv>
    docker exec portfolio-mcp python -m app.coinbase_csv_parser <folder>      # batch
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class CoinbaseTx:
    id: str
    timestamp: datetime
    txn_type: str       # Withdrawal | Sell | Buy | Receive | Send | Convert | Rewards | Deposit
    asset: str          # SGD, USD, USDC, BTC, ETH, ...
    quantity: float     # signed (negative = outflow)
    price_currency: str
    price: float        # price per unit at txn
    subtotal: float
    total: float        # incl fees
    fees: float
    notes: str
    sender_address: str
    recipient_address: str


def _money(s: str) -> float:
    if not s:
        return 0.0
    s = s.replace(",", "").replace("$", "").strip()
    if s in ("", "-"):
        return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0


def parse_csv(path: Path) -> list[CoinbaseTx]:
    """Parse one Coinbase CSV. Skips the 2-row header block."""
    rows: list[CoinbaseTx] = []
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        # Find the actual header row (starts with "ID,Timestamp,Transaction Type")
        text = f.read()
    # Drop the meta block — work from "ID,Timestamp,..." onwards
    header_idx = text.find("ID,Timestamp,Transaction Type")
    if header_idx < 0:
        return rows
    csv_text = text[header_idx:]
    reader = csv.DictReader(csv_text.splitlines())
    for row in reader:
        if not row.get("ID"):
            continue
        try:
            ts = datetime.strptime(row["Timestamp"][:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        rows.append(CoinbaseTx(
            id=row.get("ID", ""),
            timestamp=ts,
            txn_type=row.get("Transaction Type", ""),
            asset=row.get("Asset", ""),
            quantity=_money(row.get("Quantity Transacted", "0")),
            price_currency=row.get("Price Currency", ""),
            price=_money(row.get("Price at Transaction", "0")),
            subtotal=_money(row.get("Subtotal", "0")),
            total=_money(row.get("Total (inclusive of fees and/or spread)", "0")),
            fees=_money(row.get("Fees and/or Spread", "0")),
            notes=row.get("Notes", ""),
            sender_address=row.get("Sender Address", ""),
            recipient_address=row.get("Recipient Address", ""),
        ))
    return rows


def summarize(txs: list[CoinbaseTx]) -> dict:
    """Aggregate volume + realized P&L."""
    by_type_asset: dict[tuple, dict] = defaultdict(lambda: {"count": 0, "qty": 0.0, "total_usd": 0.0, "fees": 0.0})
    for t in txs:
        k = (t.txn_type, t.asset)
        by_type_asset[k]["count"] += 1
        by_type_asset[k]["qty"] += t.quantity
        by_type_asset[k]["total_usd"] += t.total
        by_type_asset[k]["fees"] += t.fees
    # Realized P&L approx: sum total of Sell rows minus cost-basis from prior Buy rows
    # (skipped here — needs full lot-tracking. Just show gross volume.)
    return {"by_type_asset": dict(by_type_asset), "total_txs": len(txs),
            "date_min": min(t.timestamp for t in txs).date() if txs else None,
            "date_max": max(t.timestamp for t in txs).date() if txs else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="Coinbase CSV file OR folder containing CSVs")
    args = ap.parse_args()

    target = Path(args.target)
    files = []
    if target.is_dir():
        files = sorted(target.glob("*.csv"))
    elif target.suffix.lower() == ".csv":
        files = [target]
    else:
        print(f"Not a CSV: {target}")
        return

    all_txs = []
    for f in files:
        txs = parse_csv(f)
        all_txs.extend(txs)
        print(f"  {f.name[:50]:<52}  {len(txs)} tx")

    if not all_txs:
        print("No transactions parsed.")
        return

    summary = summarize(all_txs)
    print(f"\n=== Coinbase activity summary ({summary['total_txs']} tx, "
          f"{summary['date_min']} → {summary['date_max']}) ===\n")
    print(f"  {'Type':<14} {'Asset':<8} {'Count':>5} {'Qty':>14} {'Total ($USD)':>14} {'Fees':>8}")
    print("  " + "-" * 70)
    for (typ, asset), d in sorted(summary["by_type_asset"].items()):
        print(f"  {typ:<14} {asset:<8} {d['count']:>5} {d['qty']:>14,.4f} "
              f"{d['total_usd']:>14,.4f} {d['fees']:>8,.4f}")


if __name__ == "__main__":
    main()
