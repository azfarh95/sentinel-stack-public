"""Credit utilization audit — verify credit_limit = current_outstanding + available_balance
for every revolving facility in CreditFacility table.

Handles shared-limit facilities (SC CC + BT share one limit; sub-accounts roll up
to the parent via `shared_limit_with`).

Run:
    docker exec portfolio-mcp python -m app.credit_utilization_audit
"""
from __future__ import annotations

from collections import defaultdict
from sqlalchemy import select

from . import database as db


def main():
    s = db.SessionLocal()
    try:
        facilities = s.execute(
            select(db.CreditFacility).where(db.CreditFacility.status == "active")
        ).scalars().all()

        # Roll up sub-accounts to their parent via shared_limit_with
        children: dict[str, list] = defaultdict(list)
        primary = []
        for f in facilities:
            if f.shared_limit_with:
                children[f.shared_limit_with].append(f)
            else:
                primary.append(f)

        print(f"{'Facility':<34} {'Limit':>9} {'Outstanding':>11} {'Available':>10} "
              f"{'Sum':>9} {'Δ':>7} {'Util%':>6}")
        print("-" * 95)

        broken = 0
        for f in primary:
            lim = f.credit_limit
            out = f.current_outstanding or 0
            avail = f.available_balance
            sub_note = ""
            for sub in children.get(f.id, []):
                out += sub.current_outstanding or 0
                sub_note = f" (+ {len(children[f.id])} sub)"
            if lim is None or avail is None:
                # Term loans / unlinked facilities — utilization metric n/a
                out_disp = f"{out:,.2f}" if out else "—"
                print(f"  {(f.lender_name[:32] + sub_note):<34} "
                      f"{'—':>9} {out_disp:>11} {'—':>10} {'—':>9} {'—':>7} {'—':>6}")
                continue
            s_total = out + avail
            delta = lim - s_total
            flag = "✓" if abs(delta) < 0.50 else "✗"
            if abs(delta) >= 0.50:
                broken += 1
            util = 100 * out / lim if lim > 0 else 0
            name = (f.lender_name[:32] + sub_note)[:33]
            print(f"  {name:<34} {lim:>9,.0f} {out:>11,.2f} {avail:>10,.2f} "
                  f"{s_total:>9,.2f} {delta:>+7,.2f} {util:>5,.1f}%  {flag}")
        print()
        print(f"  Broken ties: {broken}")
    finally:
        s.close()


if __name__ == "__main__":
    main()
