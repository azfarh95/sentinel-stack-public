"""DexScreener price client. Free, no auth, 300 req/min rate limit.

We use the /latest/dex/tokens/{address} endpoint which returns all pairs for a
token across all chains. Pick the pair with the highest USD liquidity on the
target chain — that's the most stable price reference.
"""
import logging
import httpx

logger = logging.getLogger(__name__)

BASE = "https://api.dexscreener.com/latest"

# Map our chain slugs to DexScreener chainId values
CHAIN_TO_DEXSCREENER = {
    "eth":       "ethereum",
    "bsc":       "bsc",
    "polygon":   "polygon",
    "arbitrum":  "arbitrum",
    "base":      "base",
    "avalanche": "avalanche",
    "cronos":    "cronos",
}


class DexScreenerError(RuntimeError):
    pass


async def token_price(chain: str, token_address: str) -> dict:
    """Return {price_usd, liquidity_usd, dex, pair_address, symbol} for the highest-liquidity
    pair of (chain, token_address). Raises DexScreenerError on no pairs found."""
    ds_chain = CHAIN_TO_DEXSCREENER.get(chain, chain)
    url = f"{BASE}/dex/tokens/{token_address}"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url)
        if r.status_code != 200:
            raise DexScreenerError(f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()

    pairs = data.get("pairs") or []
    if not pairs:
        raise DexScreenerError(f"no pairs found for {token_address} on {chain}")

    # Filter to target chain
    on_chain = [p for p in pairs if (p.get("chainId") or "").lower() == ds_chain]
    if not on_chain:
        raise DexScreenerError(
            f"no pairs on chain {ds_chain} (token has pairs on: "
            f"{sorted({p.get('chainId') for p in pairs})})"
        )

    # Pick pair with highest USD liquidity
    best = max(on_chain, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
    price = float(best.get("priceUsd") or 0)
    if price <= 0:
        raise DexScreenerError(f"best pair has no price: {best.get('pairAddress')}")

    return {
        "price_usd": price,
        "liquidity_usd": float((best.get("liquidity") or {}).get("usd") or 0),
        "dex": best.get("dexId"),
        "pair_address": best.get("pairAddress"),
        "symbol": (best.get("baseToken") or {}).get("symbol"),
    }
