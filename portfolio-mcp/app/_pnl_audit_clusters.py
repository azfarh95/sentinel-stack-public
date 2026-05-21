"""Show the day-1 / day-11 / day-13 clusters in full so the user can identify
recipients by pattern. Also lists big outflows (>$500) that could be Sands
Credit (16125) lump-sum repayments.
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict
from datetime import date

import httpx

FIREFLY_URL = os.environ.get("FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180")
PAT = os.environ.get("FIREFLY_PAT", "")
START = "2026-01-01"
END = date.today().isoformat()


async def fetch_tx(txn_type: str) -> list[dict]:
    out = []
    page = 1
    async with httpx.AsyncClient(timeout=30) as c:
        while True:
            r = await c.get(f"{FIREFLY_URL}/api/v1/transactions",
                            headers={"Authorization": f"Bearer {PAT}",
                                     "Accept": "application/json"},
                            params={"start": START, "end": END, "type": txn_type,
                                    "limit": 200, "page": page})
            data = r.json()
            out.extend(data.get("data", []))
            meta = data.get("meta", {}).get("pagination", {})
            if page >= int(meta.get("total_pages", 1) or 1):
                break
            page += 1
    return [t["attributes"]["transactions"][0] | {"_id": t["id"]} for t in out]


def show(tx_list, label):
    print("\n" + "─" * 90)
    print(f"  {label}")
    print("─" * 90)
    total = sum(float(t.get("amount", 0)) for t in tx_list)
    print(f"  {len(tx_list)} tx, total SGD {total:,.2f}\n")
    for t in sorted(tx_list, key=lambda x: x["date"]):
        d = t["date"][:10]
        amt = float(t.get("amount", 0))
        desc = (t.get("description") or "")[:65]
        print(f"  {d}  #{t['_id']:>5}  SGD {amt:>9,.2f}   {desc}")


async def main():
    print(f"=== Cluster drill — period {START} → {END} ===")
    withdrawals = await fetch_tx("withdrawal")
    pt_out = [t for t in withdrawals if t.get("category_name") == "Personal transfer"]

    by_dom = defaultdict(list)
    for t in pt_out:
        dom = int(t["date"][8:10])
        by_dom[dom].append(t)

    show(by_dom[1],  "DAY 1 cluster — recurring 1st-of-month outflows (Q2)")
    show(by_dom[11], "DAY 11 cluster — mid-month pattern (Q3a)")
    show(by_dom[13], "DAY 13 cluster — mid-month pattern (Q3b)")

    # Big outflows ($500+) — Sands Credit candidates
    print("\n" + "═" * 90)
    print("  Big outflows (>= SGD 500) — candidates for SANDS CREDIT repayments")
    print("═" * 90)
    big = sorted([t for t in pt_out if float(t.get("amount", 0)) >= 500],
                 key=lambda x: x["date"])
    big_total = sum(float(t.get("amount", 0)) for t in big)
    print(f"  {len(big)} tx, total SGD {big_total:,.2f}\n")
    for t in big:
        d = t["date"][:10]
        amt = float(t.get("amount", 0))
        desc = (t.get("description") or "")[:60]
        print(f"  {d}  #{t['_id']:>5}  SGD {amt:>9,.2f}   {desc}")


if __name__ == "__main__":
    asyncio.run(main())
