"""sentinel-shopping-mcp — search across SG marketplaces from one MCP server.

Tools:
  shopping_search        : query across Shopify + nodriver marketplaces, return sorted listings
  shopify_detect         : check whether a domain is Shopify-compatible
  shopify_add            : register a Shopify store in the local registry
  shopify_list           : show registered Shopify stores
  shopify_remove         : remove a Shopify store
  marketplaces_list      : show all nodriver marketplaces (built-in)
  price_history_for_url  : recent capture history for a specific listing URL

Transport: streamable HTTP on :8100 (next free port after sentinel-miniapp-v2 :8098 — fits
[[feedback-port-conventions]] convention: 8xxx live services).

Seed the registry on first start from the known-good Shopify stores we verified
during the probe phase.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import storage
from adapters import shopify as shopify_adapter
from adapters import nodriver_browser as browser_adapter
from adapters.telco import registry as telco_registry
from schema import Listing, ShopifyStore, TelcoPlan


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("sentinel-shopping")


# ── Seed the registry on first boot ──────────────────────────────────────────
SEED_SHOPIFY = [
    ShopifyStore(domain="challenger.sg",       display_name="Challenger",
                 notes="Electronics — verified 2026-05-20"),
    ShopifyStore(domain="thetechyard.com",     display_name="TheTechyard",
                 notes="Electronics — verified 2026-05-20"),
    ShopifyStore(domain="compasia.sg",         display_name="CompAsia",
                 notes="Refurbished electronics — verified 2026-05-20"),
    ShopifyStore(domain="secretlab.sg",        display_name="Secretlab",
                 notes="Chairs — verified 2026-05-20"),
    ShopifyStore(domain="shopmustafa.sg",      display_name="Mustafa (Shopify)",
                 notes="Groceries/etc — verified 2026-05-20"),
]


def _seed_registry() -> None:
    storage.init_db()
    current = {s.domain for s in storage.list_shopify_stores(enabled_only=False)}
    added = 0
    for s in SEED_SHOPIFY:
        if s.domain in current:
            continue
        storage.add_shopify_store(s)
        added += 1
    if added:
        logger.info("Seeded %d Shopify stores into registry", added)


# ── Browser pool (lazy) ──────────────────────────────────────────────────────
_pool = browser_adapter.BrowserPool()


# ── MCP setup ────────────────────────────────────────────────────────────────
# DNS-rebinding protection allows MetaMCP to call us via host.docker.internal.
# Default FastMCP only allows 127.0.0.1 / localhost when binding to loopback.
_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        "127.0.0.1:*", "localhost:*", "[::1]:*",
        "host.docker.internal:*",
        "shopping-mcp:*",     # MetaMCP -> docker-network hostname
    ],
    allowed_origins=[
        "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
        "http://host.docker.internal:*",
        "http://shopping-mcp:*",
    ],
)
mcp = FastMCP(
    "sentinel-shopping",
    transport_security=_security,
)


@mcp.tool()
async def shopping_search(query: str,
                           marketplaces: str = "all",
                           top_n: int = 10,
                           max_price_sgd: float | None = None) -> dict:
    """Search SG marketplaces and return sorted listings.

    Args:
      query          The product to search for, e.g. "16gb ddr4 ram".
      marketplaces   "all" | comma-separated list. Use marketplace_id values from
                     marketplaces_list (e.g. "shopee.sg,lazada.sg") or the
                     special tokens "shopify" / "browser" to scope by adapter.
      top_n          Listings per source (default 10). Total can be larger.
      max_price_sgd  Optional ceiling — listings above this are dropped.

    Returns: {"count": int, "listings": [Listing,...], "sources_queried": [...]}.
    Listings are sorted by price_sgd ascending (None last).
    """
    selected = [m.strip() for m in marketplaces.split(",")] if marketplaces != "all" else None

    sources_queried: list[str] = []
    all_listings: list[Listing] = []

    # Shopify side
    if selected is None or "shopify" in selected or any(s in selected for s in [st.domain for st in storage.list_shopify_stores()]):
        stores = storage.list_shopify_stores(enabled_only=True)
        if selected is not None and "shopify" not in selected:
            stores = [s for s in stores if s.domain in selected]
        if stores:
            shopify_results = await shopify_adapter.search_all(stores, query, limit_per_store=top_n)
            all_listings.extend(shopify_results)
            sources_queried.extend(s.domain for s in stores)

    # Nodriver side
    browser_issues: list[dict] = []
    if selected is None or "browser" in selected or any(s in selected for s in browser_adapter.MARKETPLACES.keys()):
        targets = list(browser_adapter.MARKETPLACES.keys())
        if selected is not None and "browser" not in selected:
            targets = [m for m in targets if m in selected]
        if targets:
            browser_results, browser_issues = await browser_adapter.search_marketplaces(
                _pool, query, marketplaces=targets, top_n=top_n,
            )
            all_listings.extend(browser_results)
            sources_queried.extend(targets)

    # Filter + sort
    if max_price_sgd is not None:
        all_listings = [l for l in all_listings
                         if l.price_sgd is not None and l.price_sgd <= max_price_sgd]
    all_listings.sort(key=lambda l: (l.price_sgd is None, l.price_sgd if l.price_sgd is not None else 0))

    # Persist for price-history
    try:
        storage.record_listings(query, all_listings)
    except Exception as e:
        logger.warning("record_listings failed: %s", e)

    # Tear down the shared browser between queries. Profile persists on disk
    # so cookies / cf_clearance carry over; only the Chrome process is fresh.
    # ~3-5s extra cold-start per query, but eliminates the dead-WS class of
    # bugs (where a Chrome killed externally leaves the pool holding a stale
    # CDP handle). Disable via env var if you'd rather keep Chrome warm.
    if os.environ.get("SHOPPING_KEEP_BROWSER", "0") != "1":
        try:
            await _pool.reset()
        except Exception as e:
            logger.warning("browser pool teardown failed: %s", e)

    return {
        "count":            len(all_listings),
        "sources_queried":  sources_queried,
        "listings":         [l.to_dict() for l in all_listings],
        # Empty list when everything worked; otherwise per-marketplace reason
        # (bot_blocked / error / unknown). The LLM should mention blockages
        # so the user knows results aren't necessarily exhaustive.
        "issues":           browser_issues,
    }


@mcp.tool()
async def shopify_detect(domain: str) -> dict:
    """Test whether a domain is Shopify-compatible (exposes /products.json)."""
    domain = domain.lower().strip().removeprefix("https://").removeprefix("http://").strip("/")
    return {"domain": domain, **(await shopify_adapter.detect(domain))}


@mcp.tool()
async def shopify_add(domain: str, display_name: str = "") -> dict:
    """Register a Shopify store after verifying detect() succeeds.

    If display_name is empty, it's derived from the domain.
    """
    domain = domain.lower().strip().removeprefix("https://").removeprefix("http://").strip("/")
    info = await shopify_adapter.detect(domain)
    if not info.get("is_shopify"):
        return {"ok": False, "domain": domain, "reason": info.get("error") or "not Shopify"}

    store = ShopifyStore(
        domain=domain,
        display_name=display_name or info.get("store_name") or domain,
        currency=info.get("currency") or "SGD",
        notes=f"Sample: {info.get('sample_product')}"[:200],
    )
    storage.add_shopify_store(store)
    return {"ok": True, "domain": domain, "display_name": store.display_name, "sample": info.get("sample_product")}


@mcp.tool()
async def shopify_list() -> dict:
    """List all registered Shopify stores."""
    stores = storage.list_shopify_stores(enabled_only=False)
    return {"count": len(stores), "stores": [s.to_dict() for s in stores]}


@mcp.tool()
async def shopify_remove(domain: str) -> dict:
    """Remove a Shopify store from the registry."""
    domain = domain.lower().strip()
    ok = storage.remove_shopify_store(domain)
    return {"ok": ok, "domain": domain}


@mcp.tool()
async def marketplaces_list() -> dict:
    """Show all available sources — Shopify stores + nodriver marketplaces."""
    return {
        "shopify_stores": [s.to_dict() for s in storage.list_shopify_stores(enabled_only=False)],
        "nodriver_marketplaces": [
            {"name": cls.name, "search_url_template": cls.search_url_template}
            for cls in browser_adapter.MARKETPLACES.values()
        ],
    }


@mcp.tool()
async def price_history_for_url(url: str, days: int = 30) -> dict:
    """Recent captured price points for a specific listing URL."""
    return {"url": url, "days": days, "points": storage.history_for_url(url, days)}


@mcp.tool()
async def telco_plans(category: str = "mobile",
                       carrier: str | None = None,
                       network: str | None = None,
                       min_data_gb: float | None = None,
                       max_price_sgd: float | None = None) -> dict:
    """SG telco/MVNO plan comparison.

    Args:
      category       "mobile" or "broadband".
      carrier        Optional — limit to one carrier ("eight", "circles", ...).
      network        Optional — limit by underlying network ("singtel", "starhub", "m1").
      min_data_gb    Mobile-side filter — only show plans with >= this much data.
      max_price_sgd  Hide plans above this monthly price.

    Returns: {"count", "plans": [...] sorted by $/GB ascending for mobile,
              $/Mbps for broadband. Plus a "skipped" list for stub carriers
              we surface but haven't extracted yet.}

    Note: the $0.30/mo platform fee (mandatory since Apr 2025) is usually NOT
    included in headline prices. Plans where it's already baked in are marked
    `platform_fee_included: true`.
    """
    carriers = telco_registry.by_filter(category=category, carrier=carrier, network=network)
    all_plans: list[TelcoPlan] = []
    skipped: list[dict] = []

    for c in carriers:
        try:
            plans = await c.fetch_plans(pool=_pool)
            all_plans.extend(plans)
        except NotImplementedError as e:
            skipped.append({"carrier": c.name, "category": c.category,
                             "reason": "needs nodriver — not yet extracted",
                             "url": c.plans_url})
        except Exception as e:
            skipped.append({"carrier": c.name, "category": c.category,
                             "reason": f"{type(e).__name__}: {e}",
                             "url": c.plans_url})

    if min_data_gb is not None:
        all_plans = [p for p in all_plans
                      if p.data_gb is not None and p.data_gb >= min_data_gb]
    if max_price_sgd is not None:
        all_plans = [p for p in all_plans
                      if p.monthly_sgd is not None and p.monthly_sgd <= max_price_sgd]

    if category == "mobile":
        all_plans.sort(key=lambda p: (p.per_gb_sgd() is None, p.per_gb_sgd() or 0))
    else:
        all_plans.sort(key=lambda p: (p.per_mbps_sgd() is None, p.per_mbps_sgd() or 0))

    try:
        storage.record_telco_plans(all_plans)
    except Exception as e:
        logger.warning("record_telco_plans failed: %s", e)

    return {
        "count":   len(all_plans),
        "plans":   [p.to_dict() for p in all_plans],
        "skipped": skipped,
    }


# ── Entry point ──────────────────────────────────────────────────────────────
# Pattern matches portfolio-mcp: build streamable_http_app() then uvicorn it.
# This is what FastMCP servers behind MetaMCP use in this stack and it carries
# the session lifecycle properly across MetaMCP's reconnection probe.
_seed_registry()
app = mcp.streamable_http_app()

if __name__ == "__main__":
    import uvicorn
    logger.info("sentinel-shopping-mcp starting on :8100")
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="info")
