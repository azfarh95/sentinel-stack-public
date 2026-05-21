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

    # Ensure the Firefly account exists, then set opening_balance so current = total_sgd
    acct_id = await ensure_firefly_account(usd_to_sgd)
    async with httpx.AsyncClient(timeout=15, headers=_ff_headers()) as c:
        # Get current account state
        r = await c.get(f"{FIREFLY_URL}/api/v1/accounts/{acct_id}")
        attrs = r.json()["data"]["attributes"]
        current_balance = float(attrs["current_balance"])
        opening_balance = float(attrs.get("opening_balance", 0) or 0)
        # Net of all transactions (transfer in/out from this account) = current - opening
        # New opening = target - net
        net = current_balance - opening_balance
        new_opening = round(total_sgd - net, 2)

        notes = attrs.get("notes", "") or ""
        # Append latest sync line
        sync_line = f"\n[sync {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}] total SGD {total_sgd:,.2f} from {len(by_currency)} currency balance(s)"
        if "[sync " in notes:
            notes = notes.split("\n[sync ", 1)[0]   # keep only header
        notes = notes + sync_line

        r = await c.put(f"{FIREFLY_URL}/api/v1/accounts/{acct_id}", json={
            "opening_balance": str(new_opening),
            "opening_balance_date": "2026-01-01",
            "notes": notes,
        })
        r.raise_for_status()

    return {
        "profile_id": profile_id,
        "firefly_account_id": acct_id,
        "total_sgd": round(total_sgd, 2),
        "currencies": by_currency,
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
