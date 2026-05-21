"""Drill the A3 FAST outflows with full attributes — look for repayment signals.

Output: top 30 outflows with all tags, notes, destination_id, full description,
plus a date-pattern check against known moneylender billing days.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date

import httpx

FIREFLY_URL = os.environ.get("FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180")
PAT = os.environ.get("FIREFLY_PAT", "")
START = "2026-01-01"
END = date.today().isoformat()

# Known moneylender + loan billing days (from recurring.yaml + registry).
KNOWN_BILLING_DAYS = {
    7: "EZ Loan ($498.72)",
    20: "Maybank CreditAble ($105)",
    22: "SC Loan/BT ($93.88)",
    28: "GXS FlexiLoan ($177.41) + Lending Bee ($532)",
}


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


async def main():
    print(f"=== A3 detail drill — period {START} → {END} ===\n")
    withdrawals = await fetch_tx("withdrawal")
    pt_out = [t for t in withdrawals if t.get("category_name") == "Personal transfer"]
    pt_out.sort(key=lambda x: -float(x.get("amount", 0)))

    print(f"Total Personal-transfer outflows: {len(pt_out)} tx, "
          f"SGD {sum(float(t.get('amount', 0)) for t in pt_out):,.2f}\n")

    print("Top 30 outflows — full attributes:")
    print("=" * 100)
    for t in pt_out[:30]:
        d = t["date"][:10]
        amt = float(t.get("amount", 0))
        dom = int(d[8:10])  # day of month
        billing_hint = KNOWN_BILLING_DAYS.get(dom, "")
        if not billing_hint:
            # Check ±2 days from known billing
            for bd, label in KNOWN_BILLING_DAYS.items():
                if abs(dom - bd) <= 2:
                    billing_hint = f"~{label} (day {bd}, ±2)"
                    break
        flag = "🔔" if billing_hint else "  "
        print(f"\n{flag} {d} (day {dom:>2})  #{t['_id']}  SGD {amt:>9,.2f}  {billing_hint}")
        print(f"   src: {t.get('source_name')} → dst: {t.get('destination_name')}")
        print(f"   description: {(t.get('description') or '')[:90]}")
        notes = t.get("notes") or ""
        if notes.strip():
            print(f"   NOTES: {notes[:200]}")
        tags = t.get("tags") or []
        if tags:
            print(f"   TAGS: {tags}")

    # Pattern summary by day-of-month
    print("\n" + "=" * 100)
    print("Pattern summary: FAST outflows by day-of-month\n")
    from collections import defaultdict
    by_dom = defaultdict(lambda: {"count": 0, "total": 0.0})
    for t in pt_out:
        dom = int(t["date"][8:10])
        by_dom[dom]["count"] += 1
        by_dom[dom]["total"] += float(t.get("amount", 0))
    for dom in sorted(by_dom.keys()):
        v = by_dom[dom]
        hint = KNOWN_BILLING_DAYS.get(dom, "")
        flag = "🔔" if hint else "  "
        print(f"  {flag} day {dom:>2}: {v['count']:>3} tx, total SGD {v['total']:>9,.2f}  {hint}")


if __name__ == "__main__":
    asyncio.run(main())
