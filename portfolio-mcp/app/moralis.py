"""Moralis Web3 API client — multi-chain wallet snapshots.

Cache policy
------------
wallet_snapshot() reads from a persistent JSON cache at
/data/moralis_snapshot_cache.json keyed by (address, dust_threshold_usd).
Cache TTL defaults to 15 minutes (override via MORALIS_CACHE_TTL_MIN env).

Free tier on Moralis is 40k CU/day. Without this cache, every
/wallet_snapshot Telegram command + every MCP `portfolio_snapshot` tool
call hits 7 chains live (~7 CU per snapshot). With this cache, repeated
calls inside the TTL window return instantly with the same payload and
zero Moralis spend. Callers that need fresh data pass `force=True`.
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List

import httpx

logger = logging.getLogger(__name__)

MORALIS_API_KEY = os.environ.get("MORALIS_API_KEY", "")
BASE = "https://deep-index.moralis.io/api/v2.2"

# Chain slugs Moralis uses. zkSync is sometimes "zksync" — confirm in their docs.
CHAINS = ["eth", "bsc", "polygon", "arbitrum", "base", "avalanche", "cronos"]
# zksync intentionally omitted — Moralis wallet-tokens endpoint rejects it (chain not in enum).

CACHE_PATH = Path(os.environ.get("MORALIS_CACHE_PATH", "/data/moralis_snapshot_cache.json"))
CACHE_TTL_SECONDS = int(os.environ.get("MORALIS_CACHE_TTL_MIN", "15")) * 60


class MoralisError(RuntimeError):
    pass


def _headers() -> Dict[str, str]:
    if not MORALIS_API_KEY:
        raise MoralisError("MORALIS_API_KEY env var is empty — set it via WCM/.env")
    return {"X-API-Key": MORALIS_API_KEY, "Accept": "application/json"}


# ── Persistent cache ─────────────────────────────────────────────────────────


def _cache_key(address: str, dust_threshold_usd: float) -> str:
    return f"{address.lower()}|{dust_threshold_usd:.4f}"


def _read_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("moralis cache read failed (%s) — treating as empty", e)
        return {}


def _write_cache(data: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(CACHE_PATH)  # atomic on POSIX + Windows
    except Exception as e:
        # Cache failure must NEVER break the request — log and continue.
        logger.warning("moralis cache write failed (%s) — ignoring", e)


def _cache_get(address: str, dust_threshold_usd: float) -> dict | None:
    """Return cached snapshot if fresh, else None."""
    store = _read_cache()
    entry = store.get(_cache_key(address, dust_threshold_usd))
    if not entry:
        return None
    age = time.time() - float(entry.get("cached_at", 0))
    if age > CACHE_TTL_SECONDS:
        return None
    snap = entry.get("snapshot")
    if not isinstance(snap, dict):
        return None
    snap = dict(snap)  # copy so we can stamp without mutating the file copy
    snap["cache_age_seconds"] = int(age)
    snap["cache_hit"] = True
    return snap


def _cache_put(address: str, dust_threshold_usd: float, snapshot: dict) -> None:
    store = _read_cache()
    store[_cache_key(address, dust_threshold_usd)] = {
        "cached_at": time.time(),
        "snapshot": snapshot,
    }
    _write_cache(store)


# ── Live API ─────────────────────────────────────────────────────────────────


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


async def wallet_snapshot(address: str, dust_threshold_usd: float = 0.0,
                          force: bool = False) -> Dict:
    """Aggregate snapshot across all configured chains.

    Cached at /data/moralis_snapshot_cache.json (TTL = MORALIS_CACHE_TTL_MIN, default 15min).
    Pass ``force=True`` to bypass the cache and refetch live.

    Cached responses carry two extra fields:
      - cache_hit: True
      - cache_age_seconds: int

    Live responses carry cache_hit=False and cache_age_seconds=0.
    """
    if not force:
        hit = _cache_get(address, dust_threshold_usd)
        if hit is not None:
            return hit

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
    snapshot = {
        "address": address,
        "total_usd": round(total_usd, 2),
        "chain_count": len(chains),
        "token_count": len(all_rows),
        "positions": all_rows,
        "errors": errors,
        "cache_hit": False,
        "cache_age_seconds": 0,
    }
    # Cache policy:
    #   - All chains errored (e.g. daily quota exhausted) → DO NOT cache.
    #     Otherwise we'd serve $0 from cache for 15 minutes after Moralis
    #     recovers. Let the next call retry live.
    #   - Partial success (some chains errored, some returned data) → cache.
    #     Better than nothing, and prevents thrashing the failing chain.
    if errors and len(errors) >= len(CHAINS):
        logger.warning("moralis snapshot fully failed (%d/%d chains) — not caching",
                       len(errors), len(CHAINS))
    else:
        _cache_put(address, dust_threshold_usd, snapshot)
    return snapshot
