"""Shopify adapter — hits each store's public endpoints with httpx.

Two endpoints we rely on (both unauthenticated, available on every standard
Shopify storefront):

  /search/suggest.json?q=...&resources[type]=product&resources[limit]=N
        Live search — ideal for "cheapest X right now" queries.

  /products.json?limit=250&page=N
        Bulk catalogue dump — for nightly price-history syncs.

If a store ever has these turned off (custom Liquid override), `detect()`
will report False at registration time.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx

from schema import Listing, ShopifyStore

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/javascript, */*; q=0.01"}


async def detect(domain: str, *, client: httpx.AsyncClient | None = None) -> dict:
    """Probe /products.json. Returns:
        {"is_shopify": bool, "store_name": str | None,
         "sample_product": str | None, "currency": str | None, "error": str | None}
    """
    own = client is None
    if own:
        client = httpx.AsyncClient()
    try:
        url = f"https://{domain}/products.json?limit=1"
        r = await client.get(url, headers=HEADERS, timeout=10.0, follow_redirects=True)
        if r.status_code != 200 or not r.text.startswith("{"):
            return {"is_shopify": False, "store_name": None, "sample_product": None,
                    "currency": None, "error": f"status={r.status_code}"}
        data = r.json()
        if "products" not in data or not isinstance(data["products"], list):
            return {"is_shopify": False, "store_name": None, "sample_product": None,
                    "currency": None, "error": "no 'products' array"}
        sample = data["products"][0] if data["products"] else None
        # Best-effort currency detection (via /meta/global.json or guess SGD for .sg)
        currency = "SGD" if domain.endswith(".sg") else None
        return {
            "is_shopify":     True,
            "store_name":     domain.split(".")[0].replace("-", " ").title(),
            "sample_product": (sample or {}).get("title"),
            "currency":       currency,
            "error":          None,
        }
    except Exception as e:
        return {"is_shopify": False, "store_name": None, "sample_product": None,
                "currency": None, "error": f"{type(e).__name__}: {e}"}
    finally:
        if own:
            await client.aclose()


def _coerce_price(p: Any) -> float | None:
    if p is None:
        return None
    try:
        return float(p)
    except (TypeError, ValueError):
        return None


def _extract_image_url(item: dict) -> str | None:
    """Shopify mixes string vs {'src': ...} dict for image fields across endpoints."""
    for key in ("featured_image", "image"):
        v = item.get(key)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, dict):
            src = v.get("src")
            if isinstance(src, str):
                return src
    return None


def _listing_from_suggest(store: ShopifyStore, item: dict) -> Listing:
    price        = _coerce_price(item.get("price"))
    compare_at   = _coerce_price(item.get("compare_at_price"))
    discount_pct = None
    if price is not None and compare_at and compare_at > price:
        discount_pct = round((1 - price / compare_at) * 100, 1)
    url = item.get("url") or ""
    if url and not url.startswith("http"):
        url = store.base_url() + url
    image_url = _extract_image_url(item)
    return Listing(
        marketplace=store.domain,
        title=item.get("title") or "(untitled)",
        url=url,
        price_sgd=price if store.currency == "SGD" else None,
        discount_pct=discount_pct,
        rating=None,
        image_url=image_url,
        in_stock=bool(item.get("available", True)),
        vendor=item.get("vendor"),
    )


async def search_one(store: ShopifyStore, query: str, *,
                      limit: int = 10,
                      client: httpx.AsyncClient | None = None) -> list[Listing]:
    """Query the Shopify /search/suggest.json endpoint on one store."""
    own = client is None
    if own:
        client = httpx.AsyncClient()
    try:
        url = (f"{store.base_url()}/search/suggest.json"
               f"?q={quote(query)}&resources[type]=product&resources[limit]={int(limit)}")
        r = await client.get(url, headers=HEADERS, timeout=12.0, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
        items = (((data.get("resources") or {}).get("results") or {}).get("products")) or []
        return [_listing_from_suggest(store, it) for it in items]
    except Exception as e:
        logger.warning("Shopify search failed on %s: %s", store.domain, e)
        return []
    finally:
        if own:
            await client.aclose()


async def search_all(stores: list[ShopifyStore], query: str, *,
                      limit_per_store: int = 10) -> list[Listing]:
    """Fan out a search across all registered Shopify stores concurrently."""
    if not stores:
        return []
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(search_one(s, query, limit=limit_per_store, client=client) for s in stores),
            return_exceptions=False,
        )
    flat: list[Listing] = []
    for batch in results:
        flat.extend(batch)
    return flat
