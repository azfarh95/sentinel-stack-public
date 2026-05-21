"""Wise API client + Firefly sync.

Pulls multi-currency balances + recent transactions from api.wise.com and
mirrors them onto a Firefly asset account named "Wise" so it appears on
the balance sheet as a current-asset.

Schedule: daily 06:30 (after market data refresh). Triggered manually via
`sync_now()` for testing.

API docs: https://docs.wise.com/api-docs
"""
import os
import logging
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

WISE_API = "https://api.wise.com"
FIREFLY_URL = os.environ.get("FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180")
WISE_FIREFLY_ACCOUNT_NAME = "Wise (Multi-currency)"


def _wise_headers() -> dict:
    token = os.environ.get("WISE_API_TOKEN", "")
    if not token:
        raise RuntimeError("WISE_API_TOKEN not set")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _ff_headers() -> dict:
    pat = os.environ.get("FIREFLY_PAT", "")
    if not pat:
        raise RuntimeError("FIREFLY_PAT not set")
    return {"Authorization": f"Bearer {pat}", "Accept": "application/json", "Content-Type": "application/json"}


async def get_profile_id() -> int:
    async with httpx.AsyncClient(timeout=15, headers=_wise_headers()) as c:
        r = await c.get(f"{WISE_API}/v1/profiles")
        r.raise_for_status()
        for p in r.json():
            if p.get("type") == "personal":
                return int(p["id"])
        # fall back: first profile
        return int(r.json()[0]["id"])


async def get_balances(profile_id: int) -> list:
    async with httpx.AsyncClient(timeout=15, headers=_wise_headers()) as c:
        r = await c.get(f"{WISE_API}/v4/profiles/{profile_id}/balances",
                        params={"types": "STANDARD"})
        r.raise_for_status()
        return r.json()


async def get_statement(profile_id: int, balance_id: int, currency: str,
                        days: int = 30) -> list:
    """Fetch the last N days of transactions for one balance."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    async with httpx.AsyncClient(timeout=20, headers=_wise_headers()) as c:
        r = await c.get(
            f"{WISE_API}/v1/profiles/{profile_id}/balance-statements/{balance_id}/statement.json",
            params={
                "currency": currency,
                "intervalStart": start.strftime("%Y-%m-%dT00:00:00.000Z"),
                "intervalEnd": end.strftime("%Y-%m-%dT23:59:59.999Z"),
                "type": "COMPACT",
            }
        )
        r.raise_for_status()
        return r.json().get("transactions", [])


async def ensure_firefly_account(fx_to_sgd: float) -> int:
    """Get-or-create the Wise asset account in Firefly. Returns its id."""
    async with httpx.AsyncClient(timeout=15, headers=_ff_headers()) as c:
        r = await c.get(f"{FIREFLY_URL}/api/v1/accounts?type=asset&limit=200")
        for a in r.json().get("data", []):
            if a["attributes"]["name"] == WISE_FIREFLY_ACCOUNT_NAME:
                return int(a["id"])
        # Create new
        payload = {
            "name": WISE_FIREFLY_ACCOUNT_NAME,
            "type": "asset",
            "account_role": "savingAsset",
            "opening_balance": "0.00",
            "opening_balance_date": "2026-01-01",
            "currency_code": "SGD",
            "notes": (
                "Multi-currency Wise account. Auto-synced daily from api.wise.com.\n"
                "Balance shown in SGD (sum of all currency balances × FX).\n"
                "Per-currency detail: see /drill/wise."
            ),
        }
        r = await c.post(f"{FIREFLY_URL}/api/v1/accounts", json=payload)
        r.raise_for_status()
        return int(r.json()["data"]["id"])


async def refresh_snapshot(session, account_code: str = "1113") -> int | None:
    """Audit-6 Q3: pull Wise balances + persist one row in `account_snapshot`.

    Source-of-truth writer for Class B Wise account. Returns the snapshot id
    (or None on any error). The resolver reads from account_snapshot and
    NEVER calls this function — keeps reader/writer responsibilities clean.

    Sums all currency balances → SGD via the YAML fx rate + Wise's converter.
    """
    try:
        profile_id = await get_profile_id()
        balances = await get_balances(profile_id)
    except Exception:
        logger.exception("wise refresh_snapshot: API call failed")
        return None

    import yaml
    import json as _json
    cfg = yaml.safe_load(open("/finance/balance_sheet_config.yaml"))
    usd_to_sgd = float(cfg.get("usd_to_sgd", 1.27))
    fx_to_sgd = {"SGD": 1.0, "USD": usd_to_sgd}

    total_sgd = 0.0
    by_cur: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=15, headers=_wise_headers()) as c:
            for b in balances:
                cur = b["currency"]
                amt = float(b["amount"]["value"])
                if cur in fx_to_sgd:
                    rate = fx_to_sgd[cur]
                else:
                    try:
                        r = await c.get(f"{WISE_API}/v1/rates",
                                        params={"source": cur, "target": "SGD"})
                        rate = float(r.json()[0]["rate"]) if isinstance(r.json(), list) and r.json() else 1.0
                    except Exception:
                        rate = 1.0
                    fx_to_sgd[cur] = rate
                total_sgd += amt * rate
                by_cur.append({"currency": cur, "amount": amt, "rate_to_sgd": rate})
    except Exception:
        logger.exception("wise refresh_snapshot: FX lookup failed")
        return None

    from . import ledger
    snap = ledger.AccountSnapshot(
        account_code=account_code,
        source_type="bank_api",
        provider="wise",
        captured_at=datetime.now(timezone.utc).replace(microsecond=0),
        sgd_value=round(total_sgd, 2),
        fx_usd_sgd=usd_to_sgd,
        # Audit-8 Q2: Wise holds positions across multiple currencies.
        # raw_currency stays NULL because there's no single primary cur;
        # raw_currencies (JSON) holds the full per-currency breakdown for
        # future FRS-21 FX P&L treatment in V3.
        raw_currencies=_json.dumps(by_cur),
        source="wise_api_v4",
        external_ref=str(profile_id),
        raw_response=_json.dumps({"balances": by_cur}),
    )
    session.add(snap)
    session.commit()
    logger.info("wise: wrote account_snapshot id=%s SGD %.2f (%d currencies)",
                snap.id, total_sgd, len(by_cur))
    return snap.id


async def sync_now() -> dict:
    """Pull current Wise balances, sum into SGD-equivalent, update Firefly opening_balance.

    Returns a dict summary suitable for logging / Telegram ping.
    """
    profile_id = await get_profile_id()
    balances = await get_balances(profile_id)

    # Simple FX: use Wise's own conversion endpoint? For now, use the YAML rate.
    import yaml
    cfg = yaml.safe_load(open("/finance/balance_sheet_config.yaml"))
    usd_to_sgd = float(cfg.get("usd_to_sgd", 1.27))
    fx_to_sgd = {
        "SGD": 1.0,
        "USD": usd_to_sgd,
    }

    # For other currencies, pull live rates via Wise's converter
    total_sgd = 0.0
    by_currency = []
    async with httpx.AsyncClient(timeout=15, headers=_wise_headers()) as c:
        for b in balances:
            cur = b["currency"]
            amt = float(b["amount"]["value"])
            reserved = float(b.get("reservedAmount", {}).get("value", 0))
            available = amt - reserved

            if cur in fx_to_sgd:
                rate = fx_to_sgd[cur]
            else:
                # Wise public converter
                try:
                    r = await c.get(f"{WISE_API}/v1/rates",
                                    params={"source": cur, "target": "SGD"})
                    rate = float(r.json()[0]["rate"]) if isinstance(r.json(), list) and r.json() else 1.0
                except Exception:
                    rate = 1.0
                fx_to_sgd[cur] = rate

            sgd_value = amt * rate
            total_sgd += sgd_value
            by_currency.append({
                "currency": cur, "amount": amt, "available": available,
                "reserved": reserved, "rate_to_sgd": rate, "sgd": sgd_value,
            })

    # Post a GL anchor journal that brings 1113 (Wise Multi-Currency) to total_sgd.
    # Replaces the legacy Firefly account write.
    from . import database as db, journal_service as js
    from sqlalchemy import text
    from datetime import date as _date

    s = db.SessionLocal()
    try:
        cur = float(s.execute(text("""
          SELECT COALESCE(SUM(CASE WHEN j.status='posted' THEN gl.debit_sgd ELSE 0 END),0)
               - COALESCE(SUM(CASE WHEN j.status='posted' THEN gl.credit_sgd ELSE 0 END),0)
          FROM general_ledger gl JOIN journals j ON j.id=gl.journal_id
          WHERE gl.account_id=(SELECT id FROM chart_of_accounts WHERE account_code='1113')
        """)).scalar() or 0)
        delta = round(total_sgd - cur, 2)
        if abs(delta) >= 0.01:
            today = _date.today()
            lines = ([{"account_code":"1113","debit":delta},{"account_code":"3100","credit":delta}]
                     if delta > 0 else
                     [{"account_code":"3100","debit":-delta},{"account_code":"1113","credit":-delta}])
            js.post_journal(s, journal_date=today,
                narration=f"Wise daily anchor to live API ${total_sgd:,.2f} (delta {delta:+,.2f})",
                journal_type="anchor", lines=lines,
                source_doc="WISE_SYNC", source_ref=f"wise:{today.isoformat()}",
                external_id=f"anchor:1113:{today.isoformat()}",
                created_by="wise_sync_job")
            s.commit()
    finally:
        s.close()

    return {
        "profile_id": profile_id,
        "total_sgd": round(total_sgd, 2),
        "currencies": by_currency,
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
