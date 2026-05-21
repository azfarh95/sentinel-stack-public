"""One-shot audit script — drills the P&L anomalies for 2026 YTD.

Run inside the portfolio-mcp container:
    docker exec portfolio-mcp python -m app._pnl_audit_2026

Outputs grouped findings for:
  A1 — Debt service entries on the INCOME side (deposits)
  A2 — General Expense entries on the INCOME side (deposits)
  A3 — Personal transfer net imbalance (orphan outflows + inflows)
  A4 — Categories used in Firefly but missing from classifier.yaml
  A5 — Uncategorised income deposits
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
    if not PAT:
        print("FIREFLY_PAT missing", file=sys.stderr)
        return []
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


def fmt(amt: float) -> str:
    return f"{amt:>12,.2f}"


def line(tx: dict, max_desc: int = 50) -> str:
    desc = (tx.get("description") or "")[:max_desc]
    src = tx.get("source_name") or "?"
    dst = tx.get("destination_name") or "?"
    return (f"  {tx['date'][:10]}  #{tx['_id']:>5}  "
            f"{fmt(float(tx.get('amount', 0)))}  "
            f"{src[:18]:18} → {dst[:18]:18}  "
            f"{desc}")


async def main():
    print(f"=== P&L audit — period {START} → {END} ===\n")
    deposits = await fetch_tx("deposit")
    withdrawals = await fetch_tx("withdrawal")
    print(f"Total tx: {len(deposits)} deposits + {len(withdrawals)} withdrawals\n")

    # ── A1: Debt service on income side ──────────────────────────────────────
    print("─" * 80)
    print("A1 — 'Debt service' on INCOME side (deposits)")
    print("─" * 80)
    a1 = [t for t in deposits if t.get("category_name") == "Debt service"]
    a1_total = sum(float(t.get("amount", 0)) for t in a1)
    print(f"Count: {len(a1)}  Total: SGD {fmt(a1_total).strip()}\n")
    for t in sorted(a1, key=lambda x: x["date"]):
        print(line(t))

    # ── A2: General Expense on income side ───────────────────────────────────
    print("\n" + "─" * 80)
    print("A2 — 'General Expense' on INCOME side (deposits)")
    print("─" * 80)
    a2 = [t for t in deposits if t.get("category_name") == "General Expense"]
    a2_total = sum(float(t.get("amount", 0)) for t in a2)
    print(f"Count: {len(a2)}  Total: SGD {fmt(a2_total).strip()}\n")
    for t in sorted(a2, key=lambda x: x["date"]):
        print(line(t))

    # ── A3: Personal transfer imbalance ──────────────────────────────────────
    print("\n" + "─" * 80)
    print("A3 — 'Personal transfer' deposits vs withdrawals (imbalance scan)")
    print("─" * 80)
    pt_in = [t for t in deposits if t.get("category_name") == "Personal transfer"]
    pt_out = [t for t in withdrawals if t.get("category_name") == "Personal transfer"]
    in_total = sum(float(t.get("amount", 0)) for t in pt_in)
    out_total = sum(float(t.get("amount", 0)) for t in pt_out)
    print(f"Inflows:  {len(pt_in):>3} tx  total SGD {fmt(in_total).strip()}")
    print(f"Outflows: {len(pt_out):>3} tx  total SGD {fmt(out_total).strip()}")
    print(f"Imbalance: SGD {fmt(out_total - in_total).strip()} (negative = more inflow than outflow)\n")

    print("Top 15 OUTFLOWS by amount (these are the suspect ones — money out without offsetting in):")
    for t in sorted(pt_out, key=lambda x: -float(x.get("amount", 0)))[:15]:
        print(line(t))

    print("\nTop 15 INFLOWS by amount:")
    for t in sorted(pt_in, key=lambda x: -float(x.get("amount", 0)))[:15]:
        print(line(t))

    # Pair attempt: within ±3 days and ±$1, look for offsetting pair
    print("\nLikely unmatched OUTFLOWS (no same-amount inflow within ±3 days):")
    in_amounts = sorted([(t["date"][:10], float(t.get("amount", 0))) for t in pt_in])
    unmatched = []
    from datetime import datetime as _dt, timedelta
    for o in pt_out:
        amt = float(o.get("amount", 0))
        od = _dt.fromisoformat(o["date"][:10]).date()
        found = False
        for id_str, amt_in in in_amounts:
            d_in = _dt.fromisoformat(id_str).date()
            if abs((od - d_in).days) <= 3 and abs(amt - amt_in) <= 1.0:
                found = True
                break
        if not found:
            unmatched.append(o)
    unmatched_total = sum(float(t.get("amount", 0)) for t in unmatched)
    print(f"Count: {len(unmatched)}  Total: SGD {fmt(unmatched_total).strip()}\n")
    for t in sorted(unmatched, key=lambda x: -float(x.get("amount", 0)))[:20]:
        print(line(t))

    # ── A4: Categories not in classifier.yaml ────────────────────────────────
    print("\n" + "─" * 80)
    print("A4 — Firefly categories NOT in classifier.yaml")
    print("─" * 80)
    from . import classifier
    known = {v.get("category") for v in classifier._load()}
    seen_cats = defaultdict(lambda: {"in": 0.0, "out": 0.0, "tx": 0})
    for t in deposits:
        c = t.get("category_name") or "(blank)"
        seen_cats[c]["in"] += float(t.get("amount", 0))
        seen_cats[c]["tx"] += 1
    for t in withdrawals:
        c = t.get("category_name") or "(blank)"
        seen_cats[c]["out"] += float(t.get("amount", 0))
        seen_cats[c]["tx"] += 1
    unknown = {c: v for c, v in seen_cats.items() if c and c not in known and c != "(blank)"}
    for c, v in sorted(unknown.items(), key=lambda kv: -(kv[1]["in"] + kv[1]["out"])):
        print(f"  {c:<40} tx={v['tx']:>3}  in=SGD {fmt(v['in']).strip():>12}  out=SGD {fmt(v['out']).strip():>12}")

    # ── A5: Uncategorised income ─────────────────────────────────────────────
    print("\n" + "─" * 80)
    print("A5 — Uncategorised / blank-category DEPOSITS (need triage)")
    print("─" * 80)
    a5 = [t for t in deposits if not t.get("category_name") or t.get("category_name") == "Uncategorised"]
    a5_total = sum(float(t.get("amount", 0)) for t in a5)
    print(f"Count: {len(a5)}  Total: SGD {fmt(a5_total).strip()}\n")
    for t in sorted(a5, key=lambda x: -float(x.get("amount", 0)))[:15]:
        print(line(t))


if __name__ == "__main__":
    asyncio.run(main())
