"""Moralis Web3 API client — multi-chain wallet snapshots."""
import os
import httpx
from typing import List, Dict

MORALIS_API_KEY = os.environ.get("MORALIS_API_KEY", "")
BASE = "https://deep-index.moralis.io/api/v2.2"

# Chain slugs Moralis uses. zkSync is sometimes "zksync" — confirm in their docs.
CHAINS = ["eth", "bsc", "polygon", "arbitrum", "base", "avalanche", "cronos"]
# zksync intentionally omitted — Moralis wallet-tokens endpoint rejects it (chain not in enum).


class MoralisError(RuntimeError):
    pass


def _headers() -> Dict[str, str]:
    if not MORALIS_API_KEY:
        raise MoralisError("MORALIS_API_KEY env var is empty — set it via WCM/.env")
    return {"X-API-Key": MORALIS_API_KEY, "Accept": "application/json"}


async def wallet_tokens(address: str, chain: str, dust_threshold_usd: float = 0.0) -> List[Dict]:
    """Return ERC20 + native holdings for one chain with USD prices.
    Filters out anything below dust_threshold_usd in the response."""
    url = f"{BASE}/wallets/{address}/tokens"
    params = {"chain": chain, "exclude_spam": "true"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=_headers(), params=params)
        if r.status_code != 200:
            raise MoralisError(f"chain={chain} status={r.status_code} body={r.text[:200]}")
        data = r.json().get("result", [])

    rows = []
    for t in data:
        usd_value = float(t.get("usd_value") or 0)
        if usd_value < dust_threshold_usd:
            continue
        rows.append({
            "chain": chain,
            "token_address": t.get("token_address"),  # null for native
            "symbol": t.get("symbol") or "UNKNOWN",
            "decimals": int(t.get("decimals") or 18),
            "raw_balance": str(t.get("balance") or "0"),
            "usd_price": float(t.get("usd_price") or 0),
            "usd_value": usd_value,
        })
    return rows


async def wallet_history(address: str, chain: str, limit: int = 25) -> List[Dict]:
    """Return most recent transactions on one chain. Includes USD value when Moralis has prices.
    Returns newest-first."""
    url = f"{BASE}/wallets/{address}/history"
    params = {"chain": chain, "limit": str(limit), "order": "DESC"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=_headers(), params=params)
        if r.status_code != 200:
            raise MoralisError(f"history chain={chain} status={r.status_code} body={r.text[:200]}")
        return r.json().get("result", [])


async def wallet_snapshot(address: str, dust_threshold_usd: float = 0.0) -> Dict:
    """Aggregate snapshot across all configured chains."""
    all_rows = []
    errors = []
    for chain in CHAINS:
        try:
            rows = await wallet_tokens(address, chain, dust_threshold_usd)
            all_rows.extend(rows)
        except Exception as e:
            errors.append(f"{chain}: {e}")
    total_usd = sum(r["usd_value"] for r in all_rows)
    chains = sorted({r["chain"] for r in all_rows})
    return {
        "address": address,
        "total_usd": round(total_usd, 2),
        "chain_count": len(chains),
        "token_count": len(all_rows),
        "positions": all_rows,
        "errors": errors,
    }
