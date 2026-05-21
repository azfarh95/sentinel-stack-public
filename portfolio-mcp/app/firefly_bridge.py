"""Firefly → GL bridge.

For every Firefly transaction, posts a balanced journal into our GL.
Idempotent: skips Firefly tx already represented in the GL (matched via
source_ref='firefly_tx:<id>' OR via existing ActualPayment row from
backfill_credit_journals).

Account mapping happens in two places:
  - FIREFLY_ACCT_TO_COA: hardcoded for assets + liabilities (we know the IDs)
  - CATEGORY_TO_COA: hardcoded for revenue + expense category_name values
  - Fallbacks: Suspense (1190) for ambiguity; Other Income (4900) / General Expense (5190)

Run inside the container:
    docker exec portfolio-mcp python -m app.firefly_bridge --start 2026-01-01

For full bridge:
    docker exec portfolio-mcp python -m app.firefly_bridge --start 2024-01-01
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, datetime
from typing import Iterable

import httpx
from sqlalchemy import select, func

from . import database as db
from . import journal_service as js
from . import ledger

logger = logging.getLogger(__name__)

FIREFLY_URL = os.environ.get("FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180")
PAT = os.environ.get("FIREFLY_PAT", "")


# ── Account mapping (Firefly acct_id → CoA code) ──────────────────────────────


FIREFLY_ACCT_TO_COA: dict[int, str] = {
    # ── Assets ─────────────────────────────────────────────────────────────
    1: "1111", 4: "1112",                               # POSB, Cash Wallet
    168: "1113", 173: "1113",                           # Wise (asset + Firefly revenue mirror)
    171: "1114", 172: "1115",                           # Maybank Savings, SC Savings
    141: "1211", 143: "1212", 145: "1213",              # CPF OA/SA/MA (postable leaves)
    147: "12149",                                        # CPF IS → Unallocated leaf (1214 is now header)
    162: "12219", 163: "12229",                          # ILPs → Unallocated leaves (1221/1222 are headers)
    95: "1231", 97: "1231", 98: "1231", 99: "1231", 195: "1231",  # Crypto wallets + Coinbase
    # ── Liabilities ───────────────────────────────────────────────────────
    100: "2121", 103: "2111", 106: "2112", 112: "2113", 115: "2211",
    118: "2122", 121: "2114", 122: "2221", 123: "2222", 129: "2213", 132: "2212",
    # Firefly-side auto-created liability mirrors (revenue/expense accounts that ARE liability flows)
    184: "2121",   # DBS Cashline (revenue source for drawdown)
    187: "2221",   # EZ Loan (expense destination for debt service)
    188: "2211",   # SC Loan/BT
    189: "2112",   # Maybank CC
    43: "2111",    # My Preferred Payment Plan from Credit Card → DBS CC (cash advance liability)
    176: "2115",   # Atome (BNPL) — leaf under 2110 Credit Cards
    202: "2113",   # TRANSFER WITHDRAWAL TO CARD 5498...8810 = SC CC
    # ── Revenue (Firefly auto-created revenue source accounts) ────────────
    42: "4110",    # Salary
    148: "4110",   # AZ United Pte Ltd
    178: "4120", 154: "4120",  # YourAgency Security
    174: "4130",   # SAF Imprest
    45: "4210", 160: "4210",   # Dividends / iFAST Dividends
    155: "4220",   # CPF Interest
    156: "4300",   # EDUSAVE/PSEA Transfer
    47: "4900",    # MEPS Receipt → Other Income temporarily (true liability mapping needs amount lookup)
    # ── Expense (Firefly auto-created expense destination accounts) ───────
    185: "5200", 196: "5200", 192: "5200", 191: "5200", 190: "5200",  # Anthropic, Claude.ai, Microsoft, Telegram Premium, Webshare
    175: "5110", 177: "5110", 186: "5110", 194: "5110",  # F&B vendors
    197: "5130", 198: "5130",                            # Transport
    180: "5160", 193: "5161",                            # Shopping
    150: "5330", 149: "5320", 151: "5320", 152: "5310", 153: "5320",  # Insurance breakouts
    179: "5340",   # Singapore Life (whole-life)
    200: "5700", 85: "5700",   # Bank fees
    88: "5700", 89: "5700",    # Outward Telegraphic Transfer (fees-like)
    161: "5460",   # iFAST Wrap Fees → Processing Fees
    # ── Internal transfers / movements ────────────────────────────────────
    46: "1112",    # Cash Withdrawal → Cash Wallet
    83: "1112",    # Cash Deposit Machine → Cash Wallet (POSB inflow from cash)
    # ── OCI / equity ──────────────────────────────────────────────────────
    96: "3300", 167: "3300",   # <Crypto Market> revenue + expense
    91: "3100", 94: "3100",    # <Historical Net Asset>, <Reconciliation> → Retained Earnings
    # ── Genuine unknowns → Suspense (1190) ────────────────────────────────
    38: "1190", 39: "1190", 40: "1190", 44: "1190",     # "Unknown", "Advice"
    41: "1190", 181: "1190",                             # FAST Collection, PayNow
    48: "1190", 82: "1190", 84: "1190", 87: "1190",     # Standing Instruction, Funds Transfer, Remittance
    83: "1190", 86: "1190", 90: "1190", 199: "1190",   # Cash Deposit Machine, POS Tx, FR039, Loan Drawdown Pool
    # Firefly auto-generates one expense account per "(narration prefix)" — Suspense for all
    135: "1190", 136: "1190", 137: "1190", 138: "1190",
    139: "1190", 140: "1190", 201: "1190",
}


CATEGORY_TO_COA: dict[str, str] = {
    # Revenue (4xxx)
    "Salary": "4110",
    "Salary (YourAgency)": "4120",
    "Reimbursement (SAF)": "4130",
    "Dividend income": "4210",
    "Interest income": "4220",
    "Investment Income": "4210",
    "Government Transfer": "4300",
    # Expense (5xxx) — operating
    "F&B": "5110",
    "F&B (delivery)": "5111",
    "Groceries": "5120",
    "Transport": "5130",
    "Transport (Public)": "5131",
    "Transport (Fuel)": "5132",
    "Subscriptions": "5200",
    "Utilities - Internet": "5141",
    "Utilities - Mobile": "5142",
    "Utilities - Electricity": "5143",
    "Healthcare": "5150",
    "Shopping": "5160",
    "Shopping (online)": "5161",
    "Family expense": "5170",
    "General Expense": "5190",
    "Insurance - Life": "5340",
    "Insurance - Term Life": "5310",
    "Insurance - CI": "5320",
    "Insurance - Health": "5330",
    "Bank fees": "5700",
    "Government fees": "5600",
    "Tax": "5500",
    "Investment Fees": "5460",
}


# Categories where the "other leg" is a liability account, not an expense/revenue
LIABILITY_BEHAVIOUR_CATEGORIES = {
    "Loan drawdown",            # DR Bank, CR Liability
    "Debt service",             # DR Liability, CR Bank (interest portion lost — see v1.10.1 parser)
    "BNPL",                     # similar to debt service
}

# Categories that imply asset movement
ASSET_BEHAVIOUR_CATEGORIES = {
    "Crypto purchase": "1231",
    "Insurance/ILP (asset)": "12219",  # Tokio Unallocated — Singlife (12229) not disambiguable from category alone
    "Portfolio adjustment": "3300",   # Unrealized gains/losses (equity OCI)
}

# Transfers between own accounts
TRANSFER_CATEGORIES_TO_COA = {
    "Transfer (Wise)": "1113",
    "Transfer (Maybank)": "1114",
    "Transfer (SC)": "1115",
}


SUSPENSE = "1190"
OTHER_INCOME = "4900"
GENERAL_EXPENSE = "5190"


# ── Firefly fetch ─────────────────────────────────────────────────────────────


async def fetch_firefly_tx(start: str, end: str, txn_type: str | None = None) -> list[dict]:
    """Fetch all Firefly tx of one type (or all types if None) in a date range."""
    if not PAT:
        raise RuntimeError("FIREFLY_PAT missing")
    out, page = [], 1
    params = {"start": start, "end": end, "limit": 200}
    if txn_type:
        params["type"] = txn_type
    async with httpx.AsyncClient(timeout=60) as c:
        while True:
            r = await c.get(f"{FIREFLY_URL}/api/v1/transactions",
                            headers={"Authorization": f"Bearer {PAT}",
                                     "Accept": "application/json"},
                            params=params | {"page": page})
            d = r.json()
            for t in d.get("data", []):
                attr = t["attributes"]
                tx = attr["transactions"][0]
                tx["_id"] = int(t["id"])
                # Firefly's type is inside the inner tx object, not on outer attributes.
                # When we queried with type=X, we ALSO know the answer — use that as truth.
                tx["_type_outer"] = (txn_type or tx.get("type") or attr.get("type") or "").lower()
                tx["_tags"] = tx.get("tags") or []
                out.append(tx)
            meta = d.get("meta", {}).get("pagination", {})
            if page >= int(meta.get("total_pages", 1) or 1):
                break
            page += 1
    return out


# ── Mapping helpers ───────────────────────────────────────────────────────────


def acct_to_coa(firefly_acct_id: int | str | None, default: str | None = SUSPENSE) -> str | None:
    """Firefly account ID → CoA account code. Falls back to Suspense by default
    (or to the explicit default — None signals 'no mapping found, try next layer')."""
    if firefly_acct_id is None:
        return default
    try:
        fid = int(firefly_acct_id)
    except (TypeError, ValueError):
        return default
    return FIREFLY_ACCT_TO_COA.get(fid, default)


def category_to_coa(category_name: str | None,
                    fallback_revenue: str = OTHER_INCOME,
                    fallback_expense: str = GENERAL_EXPENSE,
                    is_inflow: bool = False) -> str | None:
    """Map category_name → CoA code. Returns None for blank/missing so caller
    can fall back to account_id mapping or classifier.lookup()."""
    if not category_name:
        return None
    if category_name in CATEGORY_TO_COA:
        return CATEGORY_TO_COA[category_name]
    return fallback_revenue if is_inflow else fallback_expense


def description_to_coa_via_classifier(description: str, is_inflow: bool) -> str | None:
    """Last-resort: ask the classifier (78 vendor rules in classifier.yaml).
    Returns CoA code if match found, else None."""
    from . import classifier as _cl
    if not description:
        return None
    m = _cl.lookup(description)
    if m is None:
        return None
    # Map classifier's category to CoA
    return CATEGORY_TO_COA.get(m.category)


# ── Journal classification ────────────────────────────────────────────────────


def classify_tx_to_journal_lines(tx: dict) -> tuple[str, list[dict], str]:
    """Return (journal_type, lines, narration_suffix) for a single Firefly tx.

    Handles deposit / withdrawal / transfer + special category behaviours
    (loan drawdown, debt service, asset purchases, internal transfers).

    Each line dict is suitable for journal_service.post_journal().
    """
    tx_type = (tx.get("_type_outer") or "").lower()
    amount = abs(float(tx.get("amount", 0)))
    if amount == 0:
        return "noop", [], "zero amount"

    cat = tx.get("category_name") or ""
    src_id = tx.get("source_id")
    dst_id = tx.get("destination_id")
    desc = (tx.get("description") or "")[:120]

    # Skip user-created reconciliation mirror journals — these are duplicates of
    # the bank-side movement that already creates a proper double-entry via this bridge.
    if "mirror of TX" in desc:
        return "noop", [], "skipped: mirror journal (duplicate of bank-side tx)"

    # Common fields for each line
    base = {"narration": desc}

    # ── Special: Liability-behaviour categories ─────────────────────────────
    if cat in LIABILITY_BEHAVIOUR_CATEGORIES:
        if cat == "Loan drawdown" or (cat == "Debt service" and tx_type == "deposit"):
            # Drawdown: DR bank/cash (dest), CR liability (source)
            return "general", [
                {"account_code": acct_to_coa(dst_id, "1111"), "debit": amount, **base},
                {"account_code": acct_to_coa(src_id, SUSPENSE), "credit": amount, **base},
            ], "loan drawdown"
        else:
            # Debt service withdrawal: DR liability (dest), CR bank (source)
            return "cash_payment", [
                {"account_code": acct_to_coa(dst_id, SUSPENSE), "debit": amount, **base},
                {"account_code": acct_to_coa(src_id, "1111"), "credit": amount, **base},
            ], "debt service (principal only — interest split deferred to v1.10.1)"

    # ── Special: Asset-behaviour categories ─────────────────────────────────
    if cat in ASSET_BEHAVIOUR_CATEGORIES:
        asset_code = ASSET_BEHAVIOUR_CATEGORIES[cat]
        if tx_type == "withdrawal":
            return "general", [
                {"account_code": asset_code, "debit": amount, **base},
                {"account_code": acct_to_coa(src_id, "1111"), "credit": amount, **base},
            ], f"asset acquisition: {cat}"
        else:  # deposit (e.g. selling)
            return "general", [
                {"account_code": acct_to_coa(dst_id, "1111"), "debit": amount, **base},
                {"account_code": asset_code, "credit": amount, **base},
            ], f"asset disposal: {cat}"

    # ── Special: Inter-own-account transfers ─────────────────────────────────
    if cat in TRANSFER_CATEGORIES_TO_COA or tx_type == "transfer":
        if tx_type == "transfer":
            # Both src and dst should be asset accounts
            return "general", [
                {"account_code": acct_to_coa(dst_id, SUSPENSE), "debit": amount, **base},
                {"account_code": acct_to_coa(src_id, "1111"), "credit": amount, **base},
            ], "internal transfer"
        else:
            # Tagged Transfer (X) but Firefly type is deposit/withdrawal
            target_asset = TRANSFER_CATEGORIES_TO_COA.get(cat, SUSPENSE)
            if tx_type == "deposit":
                return "general", [
                    {"account_code": acct_to_coa(dst_id, "1111"), "debit": amount, **base},
                    {"account_code": target_asset, "credit": amount, **base},
                ], f"transfer in: {cat}"
            else:  # withdrawal
                return "general", [
                    {"account_code": target_asset, "debit": amount, **base},
                    {"account_code": acct_to_coa(src_id, "1111"), "credit": amount, **base},
                ], f"transfer out: {cat}"

    # ── Standard income/expense ─────────────────────────────────────────────
    if tx_type == "deposit":
        # Try category first, then source account map, then classifier, then OTHER_INCOME
        revenue_code = (category_to_coa(cat, is_inflow=True)
                        or acct_to_coa(src_id, None)
                        or description_to_coa_via_classifier(desc, is_inflow=True)
                        or OTHER_INCOME)
        return "cash_receipt", [
            {"account_code": acct_to_coa(dst_id, "1111"), "debit": amount, **base},
            {"account_code": revenue_code, "credit": amount, **base},
        ], f"income: {cat or 'unclassified'}"

    if tx_type == "withdrawal":
        # Try category first, then destination account map, then classifier, then GENERAL_EXPENSE
        expense_code = (category_to_coa(cat, is_inflow=False)
                        or acct_to_coa(dst_id, None)
                        or description_to_coa_via_classifier(desc, is_inflow=False)
                        or GENERAL_EXPENSE)
        return "cash_payment", [
            {"account_code": expense_code, "debit": amount, **base},
            {"account_code": acct_to_coa(src_id, "1111"), "credit": amount, **base},
        ], f"expense: {cat or 'unclassified'}"

    if tx_type == "opening balance":
        # Opening balance: detect direction from which side is the user's account.
        # For ASSETs (account number starts with 1xxx): DR asset, CR Retained Earnings
        # For LIABILITIES (account number starts with 2xxx): DR Retained Earnings, CR liability
        # Firefly's opening-balance tx has dst as the asset/liability and src as a Firefly-internal
        # "(initial balance)" account (e.g. id=91 <Historical Net Asset>).
        dst_coa = acct_to_coa(dst_id, None)
        src_coa = acct_to_coa(src_id, None)
        # Pick whichever side is a real asset/liability; the other is the equity counter-leg.
        real_account_coa = None
        for c in (dst_coa, src_coa):
            if c and (c.startswith("1") or c.startswith("2")) and c not in (SUSPENSE,):
                real_account_coa = c
                break
        if real_account_coa is None:
            real_account_coa = SUSPENSE
        if real_account_coa.startswith("1"):
            # Asset: DR Asset, CR Retained Earnings
            return "opening", [
                {"account_code": real_account_coa, "debit": amount, **base},
                {"account_code": "3100", "credit": amount, **base},
            ], "opening balance (asset)"
        else:
            # Liability: DR Retained Earnings, CR Liability
            return "opening", [
                {"account_code": "3100", "debit": amount, **base},
                {"account_code": real_account_coa, "credit": amount, **base},
            ], "opening balance (liability)"

    # Fallback — route via Suspense, alert in logs
    return "general", [
        {"account_code": SUSPENSE, "debit": amount, **base},
        {"account_code": SUSPENSE, "credit": amount, **base},
    ], f"UNKNOWN tx_type={tx_type} cat={cat}"


# ── Bridge orchestrator ───────────────────────────────────────────────────────


def already_bridged(s, tx_id: int) -> bool:
    """Skip if a journal already references this Firefly tx via source_ref."""
    existing = s.execute(
        select(ledger.Journal).where(ledger.Journal.source_ref == f"firefly_tx:{tx_id}")
    ).scalar_one_or_none()
    if existing is not None:
        return True
    # Also skip if backfill_credit_journals already linked via ActualPayment
    ap = s.execute(
        select(db.ActualPayment).where(db.ActualPayment.firefly_tx_id == tx_id)
    ).scalar_one_or_none()
    if ap is not None:
        # Check whether a journal with the backfill external_id matching this exists
        # The backfill external_id format: hash_id('instalment', facility_id, str(instalment_no))
        # Easier: if there's an ActualPayment, we assume backfill covered it.
        # (false negative is fine: just creates an extra journal we can void later)
        return True
    return False


def stats_default() -> dict:
    return {"created": 0, "skipped": 0, "noop": 0, "errors": 0,
            "by_type": {}, "by_category": {}}


async def bridge(start: str, end: str, types: list[str] | None = None) -> dict:
    types = types or ["withdrawal", "deposit", "transfer", "opening balance"]
    db.init_db()
    all_tx: list[dict] = []
    for t in types:
        try:
            batch = await fetch_firefly_tx(start, end, txn_type=t)
            all_tx.extend(batch)
            print(f"  fetched {len(batch):>4} Firefly tx of type {t}")
        except Exception as e:
            print(f"  ERROR fetching {t}: {e}", file=sys.stderr)
    print(f"  total: {len(all_tx)} Firefly tx in {start} → {end}")

    s = db.SessionLocal()
    stats = stats_default()
    try:
        for tx in all_tx:
            tx_id = int(tx["_id"])
            if already_bridged(s, tx_id):
                stats["skipped"] += 1
                continue
            try:
                jtype, lines, narration_suffix = classify_tx_to_journal_lines(tx)
                if jtype == "noop":
                    stats["noop"] += 1
                    continue
                # Parse tx date
                d_str = tx.get("date", "")[:10]
                jdate = datetime.fromisoformat(d_str).date() if d_str else date.today()
                desc = (tx.get("description") or "")[:200]
                journal_id = js.post_journal(
                    s,
                    journal_date=jdate,
                    narration=f"[bridged] {desc} — {narration_suffix}",
                    journal_type=jtype,
                    lines=lines,
                    source_doc="FIREFLY_BRIDGE",
                    source_ref=f"firefly_tx:{tx_id}",
                    external_id=f"firefly:{tx_id}",
                )
                stats["created"] += 1
                # Track for histogram
                cat = tx.get("category_name") or "(none)"
                stats["by_category"][cat] = stats["by_category"].get(cat, 0) + 1
                stats["by_type"][tx.get("_type_outer", "?")] = \
                    stats["by_type"].get(tx.get("_type_outer", "?"), 0) + 1
                # Commit periodically
                if stats["created"] % 100 == 0:
                    s.commit()
            except Exception as e:
                stats["errors"] += 1
                logger.exception("bridge failed for tx %d: %s", tx_id, e)
                s.rollback()
        s.commit()
    finally:
        s.close()
    return stats


# ── CLI ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--types", default="withdrawal,deposit,transfer")
    args = parser.parse_args()
    types = [t.strip() for t in args.types.split(",") if t.strip()]
    print(f"[firefly_bridge] {args.start} → {args.end}, types: {types}")
    stats = asyncio.run(bridge(args.start, args.end, types=types))
    print("\n=== Bridge stats ===")
    print(f"  created: {stats['created']}")
    print(f"  skipped (already bridged): {stats['skipped']}")
    print(f"  noop (zero amount): {stats['noop']}")
    print(f"  errors: {stats['errors']}")
    print("\n  By Firefly type:")
    for t, n in sorted(stats["by_type"].items(), key=lambda kv: -kv[1]):
        print(f"    {t:<25} {n:>4}")
    print("\n  Top 15 categories:")
    for cat, n in sorted(stats["by_category"].items(), key=lambda kv: -kv[1])[:15]:
        print(f"    {cat:<35} {n:>4}")


if __name__ == "__main__":
    main()
