"""Coinbase CDP API → cex_snapshot writer (audit-5 #3).

Reads the read-only CDP key from /data/coinbase_cdp_key.json, calls the
brokerage accounts endpoint, computes the SGD-equivalent, and persists a
row in `cex_snapshot`. That table is the SoT for Class B account 1231;
the resolver never calls this API directly.

A periodic job (jobs.poll_coinbase or the scheduler entry that wires it
in) should invoke `refresh_snapshot()` on a cadence — every 15 min today.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

KEY_PATH = Path("/data/coinbase_cdp_key.json")


def _fetch_usd_balance() -> Optional[tuple[float, dict]]:
    """Return (total_usd, raw_response_dict) or None on any error.
    Pure API call — no DB writes here so it stays unit-testable."""
    if not KEY_PATH.exists():
        return None
    try:
        from coinbase.rest import RESTClient
    except ImportError:
        logger.warning("coinbase-advanced-py not installed")
        return None
    try:
        d = json.loads(KEY_PATH.read_text())
        client = RESTClient(api_key=d["name"], api_secret=d["privateKey"])
        accts = client.get_accounts()
        total_usd = 0.0
        accounts_summary = []
        for a in accts.accounts:
            ab = a.available_balance
            val = ab.get("value") if isinstance(ab, dict) else getattr(ab, "value", 0)
            cur = ab.get("currency") if isinstance(ab, dict) else getattr(ab, "currency", "")
            total_usd += float(val or 0)
            accounts_summary.append({"currency": cur, "value": str(val)})
        return total_usd, {"accounts": accounts_summary}
    except Exception:
        logger.exception("coinbase API call failed")
        return None


def refresh_snapshot(
    session, account_code: str = "1231", fx_usd_to_sgd: float = 1.27,
) -> Optional[int]:
    """Call CDP, persist a `cex_snapshot` row, return its id (or None on error).

    Idempotent within a call — does NOT dedupe against prior rows; the time
    series IS the history. Callers should rate-limit (15 min cadence today).
    """
    result = _fetch_usd_balance()
    if result is None:
        return None
    total_usd, raw = result
    sgd_value = round(total_usd * fx_usd_to_sgd, 2)

    from . import ledger
    snap = ledger.AccountSnapshot(
        account_code=account_code,
        source_type="cex",
        provider="coinbase",
        captured_at=datetime.now(timezone.utc).replace(microsecond=0),
        sgd_value=sgd_value,
        usd_value=round(total_usd, 2),
        fx_usd_sgd=fx_usd_to_sgd,
        # Audit-8 Q2: Coinbase reports a single USD total (across all
        # crypto holdings already converted by CDP). raw_currency='USD'
        # makes the future FX-revaluation path clean.
        raw_currency="USD",
        raw_amount=round(total_usd, 2),
        source="coinbase_cdp_api",
        raw_response=json.dumps(raw),
    )
    session.add(snap)
    session.commit()
    logger.info("coinbase: wrote account_snapshot id=%s SGD %.2f (USD %.2f)",
                snap.id, sgd_value, total_usd)
    return snap.id


def get_latest_snapshot(session, account_code: str = "1231"):
    """Return the latest AccountSnapshot row for an account_code, or None.
    Source-type/provider agnostic — the resolver doesn't care if it came from
    coinbase, binance, wise, etc."""
    from . import ledger
    from sqlalchemy import select, desc
    return session.execute(
        select(ledger.AccountSnapshot)
        .where(ledger.AccountSnapshot.account_code == account_code)
        .order_by(desc(ledger.AccountSnapshot.captured_at))
        .limit(1)
    ).scalar_one_or_none()
