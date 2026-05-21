"""Krystal API client — surfaces LP / vault positions that Moralis
`/wallets/{addr}/tokens` cannot enumerate (LP receipt tokens, Krystal Earn
vaults, concentrated-liquidity positions, etc.).

Free public endpoint: https://api.krystal.app/all/v1/lp/userPositions

Responses are paginated/listed under `positions`. Each entry has:
  - chainId
  - poolAddress, tokenId, type ("dex" / "vault")
  - currentValue (USD)
  - balance0/balance1 (raw)
  - token0/token1 (symbol + decimals)
  - apr, status ("ACTIVE" / "CLOSED")

Cached for KRYSTAL_TTL seconds in process memory + persistent JSON on
/data so container restarts don't refetch.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.krystal.app/all/v1/lp/userPositions"
KRYSTAL_TTL = 900  # 15 minutes — match Moralis snapshot cadence
CACHE_FILE = Path("/data/krystal_cache.json")

_MEM_CACHE: dict = {"at": 0.0, "positions": None, "address": None}


def _load_disk_cache(address: str) -> dict | None:
    try:
        if not CACHE_FILE.exists():
            return None
        data = json.loads(CACHE_FILE.read_text())
        if data.get("address", "").lower() != address.lower():
            return None
        return data
    except Exception:
        logger.exception("krystal cache read failed")
        return None


def _save_disk_cache(address: str, positions: list, at: float) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps({
            "address": address,
            "at": at,
            "positions": positions,
        }))
    except Exception:
        logger.exception("krystal cache write failed")


async def get_positions(address: str, force: bool = False) -> dict:
    """Return Krystal LP/vault positions for the address.

    Cache hierarchy:
      1. _MEM_CACHE if fresh
      2. CACHE_FILE on disk if fresh
      3. Live API call → populate both caches
    """
    now = time.time()
    if not force:
        if (_MEM_CACHE["positions"] is not None
                and _MEM_CACHE["address"] == address.lower()
                and (now - _MEM_CACHE["at"]) < KRYSTAL_TTL):
            return {"positions": _MEM_CACHE["positions"], "cached": "memory",
                    "age_s": int(now - _MEM_CACHE["at"])}
        disk = _load_disk_cache(address)
        if disk and (now - disk.get("at", 0)) < KRYSTAL_TTL:
            _MEM_CACHE.update({"at": disk["at"], "positions": disk["positions"],
                               "address": address.lower()})
            return {"positions": disk["positions"], "cached": "disk",
                    "age_s": int(now - disk["at"])}

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(API_BASE, params={"addresses": address})
        if r.status_code != 200:
            return {"positions": [], "error": f"HTTP {r.status_code}: {r.text[:120]}"}
        body = r.json()
        positions = body.get("positions") or body.get("data") or []
    except Exception as e:
        logger.exception("krystal API call failed")
        return {"positions": [], "error": str(e)[:200]}

    # Filter to ACTIVE positions only — closed ones have zero value
    active = [p for p in positions
              if str(p.get("status", "ACTIVE")).upper() != "CLOSED"]
    _MEM_CACHE.update({"at": now, "positions": active, "address": address.lower()})
    _save_disk_cache(address, active, now)
    return {"positions": active, "cached": "fresh", "age_s": 0}


def summarise_for_snapshot(positions: list) -> list[dict]:
    """Reshape Krystal positions into the dict format used by
    portfolio_snapshot.manual_positions for balance_sheet integration."""
    out = []
    chain_label = {
        1: "eth", 56: "bsc", 137: "polygon", 42161: "arbitrum",
        8453: "base", 43114: "avalanche", 25: "cronos", 10: "optimism",
    }
    for p in positions:
        chain = chain_label.get(int(p.get("chainId", 0)), str(p.get("chainId", "?")))
        usd = float(p.get("currentValue") or 0)
        if usd < 0.01:
            continue
        protocol = p.get("project", {}).get("name") if isinstance(p.get("project"), dict) else "Krystal"
        t0 = (p.get("token0") or {}).get("symbol", "?")
        t1 = (p.get("token1") or {}).get("symbol")
        pair = f"{t0}/{t1}" if t1 else t0
        out.append({
            "label": f"Krystal · {pair}",
            "chain": chain,
            "protocol": protocol or "Krystal",
            "usd_value": round(usd, 2),
            "source": "krystal",
        })
    return out
