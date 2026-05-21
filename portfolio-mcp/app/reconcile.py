"""Savings ↔ Credit Card reconciliation.

Every POSB withdrawal to a known CC payee should match a CC statement
charge close in date + identical amount. When they don't, either the POSB
tx was misclassified or the CC statement is missing/erroneous.

Approach:
  1. Pull POSB withdrawals in [start, end] where destination_name matches a
     CC counterparty in classifier.yaml (account_type=liability, category=Debt service).
  2. Pull CC charges (deposits TO the liability account in Firefly) in the
     same window.
  3. For each POSB outflow: find the nearest CC charge within ±5 days, amount
     within ±$1. Mark pair matched.
  4. Report unmatched POSB outflows (paid but no CC charge yet booked) and
     unmatched CC charges (we owe but POSB didn't pay yet).

Output feeds /admin/reconcile (Task #42 in v1.9.3 autopilot).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx

from . import classifier as _classifier

logger = logging.getLogger(__name__)

FIREFLY_URL = os.environ.get("FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180")
POSB_SAVINGS_ID = 1

DATE_WINDOW_DAYS = 5
AMOUNT_TOLERANCE = 1.00


@dataclass
class TxLite:
    tx_id: str
    date: str
    amount: float                 # always positive; sign in `direction`
    direction: str                # "out" (POSB outflow) | "in" (CC charge / inflow)
    description: str
    counterparty: str             # canonical via classifier
    account_id: int
    account_name: str
    matched_with: str | None = None  # tx_id of the other side, if matched


def _pat() -> str:
    return os.environ.get("FIREFLY_PAT", "")


async def _fetch_paginated(client: httpx.AsyncClient, params: dict) -> list:
    headers = {"Authorization": f"Bearer {_pat()}", "Accept": "application/json"}
    out: list = []
    page = 1
    while page <= 10:
        r = await client.get(f"{FIREFLY_URL}/api/v1/transactions",
                             headers=headers, params={**params, "page": page})
        if r.status_code != 200:
            return out
        body = r.json()
        out.extend(body.get("data", []))
        meta = body.get("meta", {}).get("pagination", {})
        if page >= int(meta.get("total_pages", 1) or 1):
            break
        page += 1
    return out


async def _liability_account_ids() -> dict[int, str]:
    """Return {firefly_id: name} for every liability account."""
    if not _pat():
        return {}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{FIREFLY_URL}/api/v1/accounts",
                        headers={"Authorization": f"Bearer {_pat()}",
                                 "Accept": "application/json"},
                        params={"type": "liabilities", "limit": 200})
        out: dict[int, str] = {}
        for a in r.json().get("data", []):
            try:
                out[int(a["id"])] = a["attributes"]["name"]
            except Exception:
                pass
        return out


async def collect_window(start: str, end: str) -> tuple[list[TxLite], list[TxLite]]:
    """Returns (posb_outflows_to_cc, cc_charges) for the window."""
    if not _pat():
        return [], []
    liab_accts = await _liability_account_ids()
    posb_out: list[TxLite] = []
    cc_charges: list[TxLite] = []

    async with httpx.AsyncClient(timeout=30) as c:
        # POSB withdrawals (source = account #1)
        for t in await _fetch_paginated(c, {
            "start": start, "end": end, "type": "withdrawal", "limit": 200,
        }):
            tx = t["attributes"]["transactions"][0]
            if str(tx.get("source_id")) != str(POSB_SAVINGS_ID):
                continue
            desc = tx.get("description") or ""
            match = _classifier.lookup(desc)
            # Only retain those classified as Debt service (CC/loan payment)
            if not match or match.account_type != "liability" or match.category != "Debt service":
                continue
            posb_out.append(TxLite(
                tx_id=t["id"], date=tx["date"][:10],
                amount=float(tx["amount"]),
                direction="out",
                description=desc, counterparty=match.canonical,
                account_id=POSB_SAVINGS_ID, account_name="POSB Savings",
            ))

        # CC charges = withdrawals where source is a liability account
        # (Firefly stores card spending as type=withdrawal with the liab account as source)
        for t in await _fetch_paginated(c, {
            "start": start, "end": end, "type": "withdrawal", "limit": 200,
        }):
            tx = t["attributes"]["transactions"][0]
            try:
                src_id = int(tx.get("source_id") or 0)
            except (TypeError, ValueError):
                src_id = 0
            if src_id not in liab_accts:
                continue
            desc = tx.get("description") or ""
            cc_charges.append(TxLite(
                tx_id=t["id"], date=tx["date"][:10],
                amount=float(tx["amount"]),
                direction="in",  # represents amount owed on the CC
                description=desc,
                counterparty=tx.get("destination_name") or "?",
                account_id=src_id, account_name=liab_accts[src_id],
            ))

    return posb_out, cc_charges


def match_pairs(posb_out: list[TxLite], cc_charges: list[TxLite]) -> dict:
    """For each POSB outflow, find the best CC charge within tolerance."""
    matched: list[dict] = []
    used_cc = set()
    for p in posb_out:
        best_idx = None
        best_diff = None
        p_dt = datetime.strptime(p.date, "%Y-%m-%d")
        for i, cc in enumerate(cc_charges):
            if i in used_cc:
                continue
            cc_dt = datetime.strptime(cc.date, "%Y-%m-%d")
            day_diff = abs((p_dt - cc_dt).days)
            if day_diff > DATE_WINDOW_DAYS:
                continue
            amt_diff = abs(p.amount - cc.amount)
            if amt_diff > AMOUNT_TOLERANCE:
                continue
            if best_diff is None or (amt_diff + day_diff * 0.1) < best_diff:
                best_diff = amt_diff + day_diff * 0.1
                best_idx = i
        if best_idx is not None:
            used_cc.add(best_idx)
            cc = cc_charges[best_idx]
            p.matched_with = cc.tx_id
            cc.matched_with = p.tx_id
            matched.append({
                "posb_tx_id": p.tx_id,
                "cc_tx_id": cc.tx_id,
                "posb_date": p.date,
                "cc_date": cc.date,
                "amount_posb": p.amount,
                "amount_cc": cc.amount,
                "amount_diff": round(p.amount - cc.amount, 2),
                "day_diff": (datetime.strptime(p.date, "%Y-%m-%d")
                             - datetime.strptime(cc.date, "%Y-%m-%d")).days,
                "counterparty": p.counterparty,
                "cc_account_name": cc.account_name,
            })

    unmatched_posb = [p for p in posb_out if not p.matched_with]
    unmatched_cc = [cc for cc in cc_charges if not cc.matched_with]
    return {
        "matched": matched,
        "unmatched_posb": [
            {"tx_id": p.tx_id, "date": p.date, "amount": p.amount,
             "counterparty": p.counterparty, "description": p.description}
            for p in unmatched_posb
        ],
        "unmatched_cc": [
            {"tx_id": cc.tx_id, "date": cc.date, "amount": cc.amount,
             "description": cc.description, "cc_account_name": cc.account_name}
            for cc in unmatched_cc
        ],
        "totals": {
            "matched_count": len(matched),
            "matched_sgd": round(sum(m["amount_posb"] for m in matched), 2),
            "unmatched_posb_count": len(unmatched_posb),
            "unmatched_posb_sgd": round(sum(p.amount for p in unmatched_posb), 2),
            "unmatched_cc_count": len(unmatched_cc),
            "unmatched_cc_sgd": round(sum(cc.amount for cc in unmatched_cc), 2),
        },
    }


async def run_reconcile(days: int = 60) -> dict:
    end = date.today()
    start = end - timedelta(days=days)
    posb_out, cc_charges = await collect_window(start.isoformat(), end.isoformat())
    report = match_pairs(posb_out, cc_charges)
    report["window"] = {"days": days, "start": start.isoformat(), "end": end.isoformat()}
    report["ran_at"] = datetime.utcnow().isoformat() + "Z"
    return report


# Descriptions that come from PDF-era POSB statements that lost vendor detail.
# These can't be classified by vendor — they need re-import from iBanking CSV.
GENERIC_PDF_PATTERNS = (
    "Debit Card transaction - Unknown",
    "Point-of-Sale Transaction",
    "FAST Payment / Receipt - Unknown",
    "Bill Payment - Unknown",
    "FAST Collection",
    "Cash Withdrawal",
)


def _is_generic_pdf_desc(desc: str) -> bool:
    return any(desc.startswith(p) for p in GENERIC_PDF_PATTERNS)


async def spend_analysis(days: int = 60) -> dict:
    """Group every POSB withdrawal in the window by classifier category.

    Returns:
      {
        window: {days, start, end},
        by_category: [{category, count, sgd, vendors[]}] sorted by SGD desc,
        generic_pdf_gap: {count, sgd},  # PDF-era descriptions, vendor lost
        uncategorized_real: [{description, count, sgd}],  # specific descs the classifier missed
        totals: {classified_sgd, generic_pdf_sgd, uncategorized_real_sgd, all_sgd},
      }
    """
    if not _pat():
        return {"error": "FIREFLY_PAT not set"}
    end = date.today()
    start = end - timedelta(days=days)

    by_cat: dict[str, dict] = {}
    generic_count = 0
    generic_sgd = 0.0
    uncat_by_desc: dict[str, dict] = {}
    classified_sgd = 0.0

    async with httpx.AsyncClient(timeout=30) as c:
        for t in await _fetch_paginated(c, {
            "start": start.isoformat(), "end": end.isoformat(),
            "type": "withdrawal", "limit": 200,
        }):
            tx = t["attributes"]["transactions"][0]
            if str(tx.get("source_id")) != str(POSB_SAVINGS_ID):
                continue
            desc = tx.get("description") or ""
            amt = float(tx.get("amount") or 0)
            match = _classifier.lookup(desc)
            if match:
                cat = match.category
                slot = by_cat.setdefault(cat, {
                    "category": cat, "account_type": match.account_type,
                    "count": 0, "sgd": 0.0, "vendors": {},
                })
                slot["count"] += 1
                slot["sgd"] += amt
                slot["vendors"][match.canonical] = slot["vendors"].get(match.canonical, 0.0) + amt
                classified_sgd += amt
            elif _is_generic_pdf_desc(desc):
                generic_count += 1
                generic_sgd += amt
            else:
                bucket = uncat_by_desc.setdefault(desc[:80], {
                    "description": desc[:80], "count": 0, "sgd": 0.0,
                })
                bucket["count"] += 1
                bucket["sgd"] += amt

    # Flatten + sort
    by_category = []
    for slot in by_cat.values():
        vendors = sorted(
            [{"name": k, "sgd": round(v, 2)} for k, v in slot["vendors"].items()],
            key=lambda x: -x["sgd"],
        )
        by_category.append({
            "category": slot["category"],
            "account_type": slot["account_type"],
            "count": slot["count"],
            "sgd": round(slot["sgd"], 2),
            "vendors": vendors,
        })
    by_category.sort(key=lambda x: -x["sgd"])

    uncategorized_real = sorted(
        [{"description": v["description"], "count": v["count"], "sgd": round(v["sgd"], 2)}
         for v in uncat_by_desc.values()],
        key=lambda x: -x["sgd"],
    )

    total_all = classified_sgd + generic_sgd + sum(u["sgd"] for u in uncategorized_real)

    return {
        "window": {"days": days, "start": start.isoformat(), "end": end.isoformat()},
        "by_category": by_category,
        "generic_pdf_gap": {
            "count": generic_count,
            "sgd": round(generic_sgd, 2),
            "share_pct": round((generic_sgd / total_all * 100) if total_all else 0, 1),
            "advice": "These are old PDF-era imports that lost vendor detail. "
                      "Re-import the same months from POSB iBanking CSV to recover.",
        },
        "uncategorized_real": uncategorized_real[:30],
        "totals": {
            "classified_sgd": round(classified_sgd, 2),
            "generic_pdf_sgd": round(generic_sgd, 2),
            "uncategorized_real_sgd": round(sum(u["sgd"] for u in uncategorized_real), 2),
            "all_sgd": round(total_all, 2),
            "coverage_pct": round((classified_sgd / total_all * 100) if total_all else 0, 1),
        },
        "ran_at": datetime.utcnow().isoformat() + "Z",
    }
