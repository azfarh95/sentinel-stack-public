"""Income-statement category drill-down.

When the user clicks a row on /income_statement, /income_statement/category
lists every transaction in that category for the period. Each row has a
form to reassign category (and optionally add a classifier rule).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date
from urllib.parse import quote

import httpx

from . import classifier as _cls

logger = logging.getLogger(__name__)

FIREFLY_URL = os.environ.get("FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180")
PRIOR_YEAR_TAG_PREFIX = "prior-year:"


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "uncategorised"


def _pat() -> str:
    return os.environ.get("FIREFLY_PAT", "")


# What "Pending" means: tx with no category set at all, or the legacy
# "Uncategorised" bucket. "General Expense" is the *intentional* parking
# zone for vendor-lost PDF imports (per user direction 2026-05-13) and is
# NOT pending — it has its own card.
PENDING_BUCKETS = ("", "Uncategorised")
PARKED_BUCKETS = ("General Expense",)

# Only count a tx as "pending" if it touches one of the user's real bank
# accounts (per balance_sheet_config.yaml cash_and_bank node). Synthetic
# bookkeeping entries from portfolio_mcp (Crypto Market / Crypto Portfolio
# adjustments) are NOT actionable triage and get excluded.
REAL_BANK_ACCOUNT_IDS = {1, 4, 168, 171, 172}  # POSB, Cash, Wise, MB Sav, SC Sav


async def list_category_transactions(slug: str, txn_type: str, year: int,
                                      month: int | None = None) -> dict:
    """Return tx in [year-period] matching the given slug.

    Post-decouple (2026-05-14): if slug is a 4-5 digit CoA code, query the
    Sentinel GL directly. Otherwise fall back to legacy Firefly query.

    Special slug `pending` = virtual bucket of 1190+4900 contra-legs.
    """
    # Route to GL backend if slug is a CoA code or 'pending'
    if slug.isdigit() and 4 <= len(slug) <= 5:
        return await _list_by_coa_gl(slug, txn_type, year, month)
    if slug == "pending":
        return await _list_pending_gl(year, month, txn_type=txn_type)
    # Legacy Firefly fallback (kept for backward compat during transition)
    return await _list_category_transactions_firefly_legacy(slug, txn_type, year, month)


async def _list_by_coa_gl(coa_code: str, txn_type: str, year: int,
                           month: int | None) -> dict:
    """GL-backed transaction list for a specific CoA code."""
    from sqlalchemy import text
    from . import database as db

    if month:
        from calendar import monthrange
        last = monthrange(year, month)[1]
        end_d = date(year, month, last)
        if year == date.today().year and month == date.today().month:
            end_d = date.today()
        start = f"{year}-{month:02d}-01"
        end = end_d.isoformat()
        period_label = f"{year}-{month:02d}"
    else:
        start = f"{year}-01-01"
        end = (date.today().isoformat() if year == date.today().year else f"{year}-12-31")
        period_label = f"{year}{' YTD' if year == date.today().year else ''}"

    db.init_db()
    s = db.SessionLocal()
    try:
        # Get CoA name + class
        coa_row = s.execute(text("""
          SELECT account_code, account_name, account_class
          FROM chart_of_accounts WHERE account_code = :c
        """), {"c": coa_code}).fetchone()
        cat = f"{coa_code} {coa_row[1]}" if coa_row else coa_code
        coa_class = coa_row[2] if coa_row else None

        # Each journal touching this CoA in the period
        rows = s.execute(text("""
          SELECT j.id AS journal_id,
                 j.journal_date AS date,
                 j.narration AS description,
                 j.source_doc,
                 gl.debit, gl.credit
          FROM journals j
          JOIN general_ledger gl ON gl.journal_id = j.id
          JOIN chart_of_accounts coa ON coa.id = gl.account_id
          WHERE j.status='posted'
            AND coa.account_code = :c
            AND j.journal_date BETWEEN :df AND :dt
          ORDER BY j.journal_date DESC, j.id DESC
        """), {"c": coa_code, "df": start, "dt": end}).all()

        matched = []
        total = 0.0
        sources: dict[str, int] = {}
        destinations: dict[str, int] = {}
        for r in rows:
            dr, cr = float(r[4] or 0), float(r[5] or 0)
            # For revenue, amount = credit. For expense, amount = debit.
            # For asset/liability, take whichever is non-zero.
            amt = cr if coa_class == "REVENUE" else dr if coa_class == "EXPENSE" else (dr + cr)
            if amt < 0.01: continue

            # Look up the contra-leg(s) of this journal — these become source/destination
            other_legs = s.execute(text("""
              SELECT coa2.account_code, coa2.account_name, gl2.debit, gl2.credit
              FROM general_ledger gl2
              JOIN chart_of_accounts coa2 ON coa2.id = gl2.account_id
              WHERE gl2.journal_id = :j AND coa2.account_code != :c
            """), {"j": r[0], "c": coa_code}).all()
            other_label = ", ".join(f"{ol[0]} {ol[1]}"[:40] for ol in other_legs) or "?"

            src = other_label if cr > 0 else (other_label if coa_class == "REVENUE" else cat)
            dst = cat if coa_class == "REVENUE" else other_label
            sources[src] = sources.get(src, 0) + 1
            destinations[dst] = destinations.get(dst, 0) + 1

            matched.append({
                "tx_id": str(r[0]),
                "journal_id": r[0],
                "date": str(r[1])[:10],
                "amount": amt,
                "description": (r[2] or "")[:120],
                "source_name": src[:60],
                "destination_name": dst[:60],
                "category_name": cat,
                "type": "deposit" if (cr > 0 and coa_class != "EXPENSE") else "withdrawal",
                "tags": [],
                "source_doc": r[3] or "",
            })
            total += amt
    finally:
        s.close()

    return {
        "slug": coa_code,
        "txn_type": txn_type,
        "year": year,
        "month": month,
        "period_label": period_label,
        "period_start": start,
        "period_end": end,
        "transactions": matched,
        "totals": {"count": len(matched), "sgd": round(total, 2)},
        "top_sources": sorted(sources.items(), key=lambda kv: -kv[1])[:5],
        "top_destinations": sorted(destinations.items(), key=lambda kv: -kv[1])[:5],
        "data_source": "sentinel_gl",
    }


async def _list_pending_gl(year: int, month: int | None,
                            txn_type: str = "any") -> dict:
    """GL-backed view of 'pending' suspense — 1190 + 4900 contra-legs.

    v2.28: honour `txn_type` filter. Suspense (1190) is an Asset placeholder:
      - Dr Suspense  → bank went down → outflow ('withdrawal')
      - Cr Suspense  → bank went up   → inflow  ('deposit')
    Previously this function ignored txn_type and returned both, inflating
    the Pending Reconciliation 'withdrawal' bucket with Salary/MEPS Receipt
    deposits ($32k of the $35k headline).
    """
    from sqlalchemy import text
    from datetime import timedelta
    from . import database as db

    today = date.today()
    start = (today - timedelta(days=60)).isoformat()
    end = today.isoformat()
    db.init_db()
    s = db.SessionLocal()
    try:
        rows = s.execute(text("""
          SELECT j.id, j.journal_date, j.narration,
                 coa.account_code, gl.debit, gl.credit, j.source_doc
          FROM journals j
          JOIN general_ledger gl ON gl.journal_id = j.id
          JOIN chart_of_accounts coa ON coa.id = gl.account_id
          WHERE j.status='posted'
            AND coa.account_code IN ('1190', '4900')
            AND j.journal_date BETWEEN :df AND :dt
          ORDER BY j.journal_date DESC, j.id DESC
          LIMIT 500
        """), {"df": start, "dt": end}).all()
        matched = []
        total = 0.0
        want = (txn_type or "any").lower()
        for r in rows:
            dr, cr = float(r[4] or 0), float(r[5] or 0)
            row_type = "withdrawal" if dr > 0 else "deposit"
            if want != "any" and row_type != want:
                continue
            amt = dr + cr
            matched.append({
                "tx_id": str(r[0]), "journal_id": r[0],
                "date": str(r[1])[:10], "amount": amt,
                "description": (r[2] or "")[:120],
                "source_name": "?", "destination_name": f"{r[3]} suspense",
                "category_name": "Pending" if r[3] == "1190" else "Unclassified income",
                "type": row_type, "tags": [],
                "source_doc": r[6] or "",
            })
            total += amt
    finally:
        s.close()
    return {
        "slug": "pending", "txn_type": want, "year": year, "month": month,
        "period_label": "last 60 days", "period_start": start, "period_end": end,
        "transactions": matched,
        "totals": {"count": len(matched), "sgd": round(total, 2)},
        "top_sources": [], "top_destinations": [],
        "data_source": "sentinel_gl",
    }


async def _list_category_transactions_firefly_legacy(slug: str, txn_type: str, year: int,
                                      month: int | None = None) -> dict:
    """LEGACY Firefly-backed transaction list. Retained for non-CoA slugs (back-compat)."""
    if slug == "pending":
        # Match the home glance window exactly: trailing 60 days.
        from datetime import timedelta
        today = date.today()
        end_d = today
        start = (today - timedelta(days=60)).isoformat()
        end = today.isoformat()
        period_label = f"last 60 days"
    elif month:
        from calendar import monthrange
        last = monthrange(year, month)[1]
        today = date.today()
        end_d = date(year, month, last)
        if year == today.year and month == today.month:
            end_d = today
        start = f"{year}-{month:02d}-01"
        end = end_d.isoformat()
        period_label = f"{year}-{month:02d}"
    else:
        start = f"{year}-01-01"
        end = (date.today().isoformat()
               if year == date.today().year else f"{year}-12-31")
        period_label = f"{year}{' YTD' if year == date.today().year else ''}"

    pat = _pat()
    if not pat:
        return {"error": "FIREFLY_PAT not set", "transactions": []}

    matched: list[dict] = []
    total_sgd = 0.0
    sources: dict[str, int] = {}     # source account name -> count
    destinations: dict[str, int] = {}
    headers = {"Authorization": f"Bearer {pat}", "Accept": "application/json"}

    # For the pending bucket, fetch both withdrawals and deposits so
    # uncategorized income shows up alongside uncategorized expense.
    types_to_query = (["withdrawal", "deposit"] if slug == "pending"
                      else [txn_type])

    async with httpx.AsyncClient(timeout=30) as c:
        for query_type in types_to_query:
            for page in range(1, 11):
                r = await c.get(
                    f"{FIREFLY_URL}/api/v1/transactions",
                    headers=headers,
                    params={"start": start, "end": end, "type": query_type,
                            "limit": 200, "page": page},
                )
                body = r.json()
                for t in body.get("data", []):
                    tx = t["attributes"]["transactions"][0]
                    # Skip accrual-tagged tx
                    tags = [(x.get("tag") if isinstance(x, dict) else x)
                            for x in (tx.get("tags") or [])]
                    if any(str(tg).startswith(PRIOR_YEAR_TAG_PREFIX) for tg in tags):
                        continue
                    # For Pending: only show tx touching a real bank account
                    if slug == "pending":
                        try:
                            src_id = int(tx.get("source_id") or 0)
                            dst_id = int(tx.get("destination_id") or 0)
                        except (TypeError, ValueError):
                            src_id = dst_id = 0
                        if src_id not in REAL_BANK_ACCOUNT_IDS and dst_id not in REAL_BANK_ACCOUNT_IDS:
                            continue
                    raw_cat = (tx.get("category_name") or "").strip()
                    cat = raw_cat or "Uncategorised"
                    if slug == "pending":
                        if raw_cat not in PENDING_BUCKETS:
                            continue
                    elif slugify(cat) != slug:
                        continue
                    amt = float(tx.get("amount") or 0)
                    total_sgd += amt
                    src = tx.get("source_name") or "?"
                    dst = tx.get("destination_name") or "?"
                    sources[src] = sources.get(src, 0) + 1
                    destinations[dst] = destinations.get(dst, 0) + 1
                    matched.append({
                        "tx_id": t["id"],
                        "journal_id": tx.get("transaction_journal_id"),
                        "date": tx["date"][:10],
                        "amount": amt,
                        "description": tx.get("description") or "",
                        "source_name": src,
                        "destination_name": dst,
                        "category_name": cat,
                        "type": tx.get("type") or query_type,
                        "tags": tags,
                    })
                meta = body.get("meta", {}).get("pagination", {})
                if page >= int(meta.get("total_pages", 1) or 1):
                    break

    matched.sort(key=lambda x: x["date"], reverse=True)
    return {
        "slug": slug,
        "txn_type": txn_type,
        "year": year,
        "month": month,
        "period_label": period_label,
        "period_start": start,
        "period_end": end,
        "transactions": matched,
        "totals": {
            "count": len(matched),
            "sgd": round(total_sgd, 2),
        },
        "top_sources": sorted(sources.items(), key=lambda kv: -kv[1])[:5],
        "top_destinations": sorted(destinations.items(), key=lambda kv: -kv[1])[:5],
    }


async def recategorise(tx_id: str, journal_id, new_category: str,
                       add_rule_pattern: str | None = None,
                       canonical: str | None = None,
                       account_type: str | None = "expense") -> dict:
    """PATCH a Firefly transaction's category_name. Optionally append a
    classifier rule so future tx with the same pattern auto-classify."""
    pat = _pat()
    if not pat:
        return {"ok": False, "error": "FIREFLY_PAT missing"}
    headers = {"Authorization": f"Bearer {pat}", "Accept": "application/json",
               "Content-Type": "application/json"}
    payload = {
        "apply_rules": False,
        "fire_webhooks": False,
        "transactions": [{
            "transaction_journal_id": journal_id,
            "category_name": new_category,
        }],
    }
    rule_result = None
    if add_rule_pattern and canonical:
        rule_result = _cls.add_rule(
            canonical=canonical,
            match_pattern=add_rule_pattern,
            category=new_category,
            account_type=account_type or "expense",
        )
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.put(f"{FIREFLY_URL}/api/v1/transactions/{tx_id}",
                            headers=headers, json=payload)
        return {
            "ok": r.status_code in (200, 201),
            "status": r.status_code,
            "body": r.text[:200] if r.status_code not in (200, 201) else None,
            "rule_added": rule_result,
        }
    except Exception as e:
        logger.exception("recategorise PATCH failed")
        return {"ok": False, "error": str(e)[:200], "rule_added": rule_result}


async def pending_reconciliation_count(days: int = 60) -> dict:
    """Unified triage surface — reads V2 unreconciled_queue (the canonical
    place where verifier-queued items land), NOT the legacy suspense-GL count.

    Single source: same number on home glance / drill / /reconcile page.
    Includes both verifier-queued tx items AND period_drift entries.

    Returns: {count, sgd, drift_count, drift_accounts, drift_worst,
              data_source}.

    Note on drift metrics: PERIOD_DRIFT rows are monthly diagnostic markers
    (GL vs statement CF), NOT money. A structural offset on one account
    appears as N monthly rows with similar tx_amount values — so
    SUM(tx_amount) double-counts the same underlying gap and produces a
    nonsensical headline (v2.27 fix). Instead we surface:
      - drift_count: total marker rows
      - drift_accounts: distinct accounts with drift
      - drift_worst: largest single-period drift (canonical "how bad")
    """
    from sqlalchemy import text
    from . import database as db
    db.init_db()
    s = db.SessionLocal()
    try:
        tx_row = s.execute(text("""
          SELECT COUNT(*), COALESCE(SUM(tx_amount), 0)
          FROM unreconciled_queue
          WHERE status='pending' AND tx_type != 'PERIOD_DRIFT'
        """)).fetchone()
        drift_row = s.execute(text("""
          SELECT
            COUNT(*),
            COUNT(DISTINCT substr(source_ref,1,instr(source_ref,':')-1)),
            COALESCE(MAX(ABS(tx_amount)), 0)
          FROM unreconciled_queue
          WHERE status='pending' AND tx_type='PERIOD_DRIFT'
        """)).fetchone()
    finally:
        s.close()
    return {
        "count": int(tx_row[0] or 0),
        "sgd": round(float(tx_row[1] or 0), 2),
        "drift_count": int(drift_row[0] or 0),
        "drift_accounts": int(drift_row[1] or 0),
        "drift_worst": round(float(drift_row[2] or 0), 2),
        # Back-compat shims (kept so any out-of-tree caller doesn't KeyError):
        "parked_count": int(drift_row[0] or 0),
        "parked_sgd": 0.0,  # intentionally 0 — old field was misleading
        "data_source": "unreconciled_queue",
    }


async def _pending_reconciliation_count_firefly_legacy(days: int = 60) -> dict:
    """LEGACY Firefly impl, retained for reference. No longer called."""
    from datetime import timedelta
    end = date.today()
    start = end - timedelta(days=days)
    pat = _pat()
    if not pat:
        return {"count": 0, "sgd": 0.0, "parked_count": 0, "parked_sgd": 0.0,
                "error": "FIREFLY_PAT missing"}
    headers = {"Authorization": f"Bearer {pat}", "Accept": "application/json"}
    pending_count = 0
    pending_sgd = 0.0
    parked_count = 0
    parked_sgd = 0.0
    async with httpx.AsyncClient(timeout=20) as c:
        # Only count withdrawal + deposit — transfers between own accounts and
        # opening-balance entries don't need triage (and aren't shown on the
        # drill page either, which would make the counts mismatch).
        for query_type in ("withdrawal", "deposit"):
            for page in range(1, 6):
                r = await c.get(
                    f"{FIREFLY_URL}/api/v1/transactions",
                    headers=headers,
                    params={"start": start.isoformat(), "end": end.isoformat(),
                            "type": query_type, "limit": 200, "page": page},
                )
                body = r.json()
                for t in body.get("data", []):
                    tx = t["attributes"]["transactions"][0]
                    # Skip tx that don't touch a real bank account
                    try:
                        src = int(tx.get("source_id") or 0)
                        dst = int(tx.get("destination_id") or 0)
                    except (TypeError, ValueError):
                        src = dst = 0
                    if src not in REAL_BANK_ACCOUNT_IDS and dst not in REAL_BANK_ACCOUNT_IDS:
                        continue
                    cat = (tx.get("category_name") or "").strip()
                    amt = float(tx.get("amount") or 0)
                    if cat in PENDING_BUCKETS:
                        pending_count += 1
                        pending_sgd += amt
                    elif cat in PARKED_BUCKETS:
                        parked_count += 1
                        parked_sgd += amt
                meta = body.get("meta", {}).get("pagination", {})
                if page >= int(meta.get("total_pages", 1) or 1):
                    break
    return {
        "count": pending_count,
        "sgd": round(pending_sgd, 2),
        "parked_count": parked_count,
        "parked_sgd": round(parked_sgd, 2),
        "window_days": days,
    }
