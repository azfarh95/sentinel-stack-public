"""Nodriver-based adapter for marketplaces that need a real browser.

Architecture:
  - One long-lived `BrowserPool` holds a single nodriver Browser instance,
    started lazily on first use. Reused across queries.
  - Each marketplace subclass declares its `name`, `search_url(query)`, and
    `selectors_priority` plus a `parse_tile(text, anchor)` for extracting
    title/price from the tile's text content.
  - The MCP runs queries serially through one browser to avoid spawning
    multiple Chromes per query (memory + detection cost).

Skeletons here — selector specifics for Shopee/Lazada/Amazon/GainCity will be
finalised against the sweep results + the earlier verified probes.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar
from urllib.parse import quote

import nodriver as uc

from schema import Listing

logger = logging.getLogger(__name__)

def _profiles_root() -> Path:
    """Pick a writable location: /data inside the container (where the compose
    volume mounts), otherwise %LOCALAPPDATA% on the dev host."""
    if Path("/.dockerenv").exists() and Path("/data").exists():
        return Path("/data/profiles")
    return Path.home() / "AppData" / "Local" / "sentinel-shopping-mcp-profiles"


PROFILES_ROOT = _profiles_root()
PROFILES_ROOT.mkdir(parents=True, exist_ok=True)


class BrowserPool:
    """Single shared nodriver browser. Lazy-start, explicit close.

    Detects container deployment via the `/.dockerenv` sentinel + DISPLAY env.
    Inside the container Xvfb provides the display so headful still works, but
    we pass no_sandbox=True (no namespaces in Docker) and a couple of
    container-only flags.
    """

    def __init__(self, profile_subdir: str = "main"):
        self._profile = PROFILES_ROOT / profile_subdir
        self._profile.mkdir(parents=True, exist_ok=True)
        self._browser: uc.Browser | None = None
        self._lock = asyncio.Lock()

    async def _spawn(self) -> uc.Browser:
        in_docker = Path("/.dockerenv").exists()
        args = ["--lang=en-SG"]
        if in_docker:
            args.extend([
                "--disable-dev-shm-usage",  # /dev/shm too small in containers
                "--no-zygote",
            ])
        logger.info("Starting nodriver browser, profile=%s (docker=%s)",
                    self._profile, in_docker)
        return await uc.start(
            user_data_dir=str(self._profile),
            headless=False,
            no_sandbox=in_docker,
            browser_args=args,
        )

    async def _is_alive(self) -> bool:
        """Cheap liveness check on the CDP WS. If anything throws, declare dead."""
        if self._browser is None:
            return False
        try:
            # `browser.connection.target.info` requires an active WS but doesn't
            # navigate. Falls back to checking the underlying process is up.
            tabs = self._browser.tabs
            if not tabs:
                return False
            # Try a no-op evaluate on the main tab — round-trips the WS once.
            await tabs[0].evaluate("1+1")
            return True
        except Exception as e:
            logger.warning("BrowserPool liveness probe failed: %s", e)
            return False

    async def get(self) -> uc.Browser:
        async with self._lock:
            if self._browser is not None and not await self._is_alive():
                logger.info("BrowserPool detected dead browser — discarding")
                try:
                    self._browser.stop()
                except Exception:
                    pass
                self._browser = None
            if self._browser is None:
                self._browser = await self._spawn()
            return self._browser

    async def reset(self) -> None:
        """Caller can force a fresh browser on the next get()."""
        async with self._lock:
            if self._browser is not None:
                try:
                    self._browser.stop()
                except Exception:
                    pass
                self._browser = None

    async def close(self) -> None:
        await self.reset()


# ── Marketplace base + price parser ───────────────────────────────────────────

PRICE_RE = re.compile(r"S?\$\s?([\d,]+(?:\.\d{1,2})?)")


def parse_price_sgd(text: str) -> float | None:
    """Pull the first S$/$ number we see — good enough for tile-level text."""
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _unwrap_value(v):
    """nodriver wraps primitives as {'type': 'string', 'value': ...}. Recur."""
    if isinstance(v, dict) and "value" in v and "type" in v:
        return _unwrap_value(v["value"])
    return v


def _row_to_dict(row) -> dict:
    """nodriver returns JS objects as either {key: value} (already-unwrapped) or
    [['key', {type, value}], ...] (CDP-pair shape). Normalise to dict."""
    if isinstance(row, dict):
        # Already a dict — but values might be wrapped
        if "value" in row and "type" in row and len(row) <= 3:
            inner = row.get("value")
            if isinstance(inner, (dict, list)):
                return _row_to_dict(inner)
            return {"_value": inner}
        return {k: _unwrap_value(v) for k, v in row.items()}
    if isinstance(row, list):
        out = {}
        for pair in row:
            if isinstance(pair, list) and len(pair) >= 2:
                out[pair[0]] = _unwrap_value(pair[1])
        return out
    return {}


class MarketplaceBlocked(Exception):
    """Raised when a search page returns a bot-bounce instead of real listings.
    The orchestrator surfaces this to the LLM so it doesn't paraphrase the
    blockage as 'no results'."""
    def __init__(self, marketplace: str, reason: str, url: str = "", title: str = ""):
        super().__init__(f"{marketplace} blocked: {reason}")
        self.marketplace = marketplace
        self.reason = reason
        self.url = url
        self.title = title


# Phrases / URL fragments that indicate a bot-bounce page. Compiled once.
# Lowercased before matching; spans Akamai (Shopee), Cloudflare interstitial
# (Lazada/GainCity historical), and Amazon "Robot Check".
_BOT_BOUNCE_URL_FRAGMENTS = (
    "/verify/traffic/error",
    "/captcha",
    "/errors/validatecaptcha",
)
_BOT_BOUNCE_BODY_PHRASES = (
    "page unavailable",
    "looks like you’re not logged in",
    "looks like you're not logged in",
    "just a moment",
    "attention required",
    "checking your browser",
    "robot check",
    "sorry, we just need to make sure",
    "loading issue",
)


def detect_bot_bounce(url: str, title: str, body_text: str) -> str | None:
    """Return a short reason string if the page looks like a bot-bounce,
    otherwise None. Order matters — URL is most reliable, then body text,
    then title (titles are often misleading because the homepage title leaks
    onto the bounce page)."""
    u = (url or "").lower()
    for frag in _BOT_BOUNCE_URL_FRAGMENTS:
        if frag in u:
            return f"redirected to {frag}"
    b = (body_text or "").lower()
    for phrase in _BOT_BOUNCE_BODY_PHRASES:
        if phrase in b:
            return f"page text matches {phrase!r}"
    t = (title or "").lower()
    if any(p in t for p in ("page unavailable", "robot check", "captcha")):
        return f"title matches bounce pattern"
    return None


class Marketplace(ABC):
    name:                ClassVar[str]
    search_url_template: ClassVar[str]              # use {q} placeholder
    tile_selectors:      ClassVar[list[str]]        # tried in order, first non-empty wins
    wait_seconds:        ClassVar[float] = 6.0

    @classmethod
    def search_url(cls, query: str) -> str:
        return cls.search_url_template.format(q=quote(query))

    @classmethod
    async def search(cls, pool: BrowserPool, query: str, *, top_n: int = 10) -> list[Listing]:
        browser = await pool.get()
        page = await browser.get(cls.search_url(query))
        await asyncio.sleep(cls.wait_seconds + random.uniform(0, 2))

        # Bot-bounce detection runs BEFORE selectors so we don't misreport a
        # blocked site as 'no results found'. Pull current URL + visible text
        # snippet — that's enough to identify every bounce family we've seen.
        try:
            live_url    = await page.evaluate("location.href") or ""
            live_title  = await page.evaluate("document.title") or ""
            snippet     = await page.evaluate("document.body.innerText.slice(0, 500)") or ""
        except Exception:
            live_url = live_title = snippet = ""

        bounce_reason = detect_bot_bounce(live_url, live_title, snippet)
        if bounce_reason:
            logger.warning("%s: bot-bounce detected (%s) at url=%s title=%r",
                           cls.name, bounce_reason, live_url, live_title[:80])
            raise MarketplaceBlocked(cls.name, bounce_reason,
                                      url=live_url, title=live_title)

        # First non-empty selector wins
        best_sel, best_count = None, 0
        for sel in cls.tile_selectors:
            try:
                n = await page.evaluate(f"document.querySelectorAll({sel!r}).length")
                if n and n > best_count:
                    best_sel, best_count = sel, n
            except Exception:
                continue
        if not best_sel or best_count == 0:
            logger.warning("%s: no tiles found for query=%r", cls.name, query)
            return []

        # Pull innerText + href for each tile, up to top_n
        rows = await page.evaluate(f"""
            Array.from(document.querySelectorAll({best_sel!r}))
                .slice(0, {int(top_n)})
                .map(el => {{
                    const a = el.querySelector('a[href]') || (el.tagName === 'A' ? el : null);
                    const img = el.querySelector('img');
                    return {{
                        text: (el.innerText || '').replace(/\\s+/g, ' ').trim(),
                        href: a ? a.href : '',
                        img:  img ? (img.src || img.getAttribute('data-src') || '') : '',
                    }};
                }})
        """)

        listings: list[Listing] = []
        for raw_row in rows or []:
            row  = _row_to_dict(raw_row)
            text = row.get("text") or ""
            href = row.get("href") or ""
            img  = row.get("img")  or ""
            if not text:
                continue
            price = parse_price_sgd(text)
            listings.append(Listing(
                marketplace=cls.name,
                title=text[:160],
                url=href,
                price_sgd=price,
                image_url=img or None,
            ))
        return listings


# ── Concrete marketplaces ────────────────────────────────────────────────────
# Selectors verified live in the probe scripts + sweep run on 2026-05-20.


class Shopee(Marketplace):
    name = "shopee.sg"
    search_url_template = "https://shopee.sg/search?keyword={q}"
    tile_selectors = ["li.shopee-search-item-result__item"]
    # Hydration profiled 2026-05-21: 5s -> 29KB body, 0 tiles. 10s -> 194KB
    # body, 39 tiles. 11s gives a small buffer over the observed boundary.
    wait_seconds = 11.0


class Lazada(Marketplace):
    name = "lazada.sg"
    search_url_template = "https://www.lazada.sg/catalog/?q={q}"
    tile_selectors = ["[data-qa-locator='product-item']", "div.Bm3ON"]
    wait_seconds = 9.0


class AmazonSG(Marketplace):
    name = "amazon.sg"
    search_url_template = "https://www.amazon.sg/s?k={q}"
    tile_selectors = ['div[data-component-type="s-search-result"]']
    wait_seconds = 8.0


# ── Magento family ───────────────────────────────────────────────────────────
# Many SG retailers run Magento with identical product-card markup. Gain City,
# Courts, Best Denki all use `li.product-item` (sweep-confirmed 2026-05-20).


class MagentoMarketplace(Marketplace):
    """Magento /catalogsearch/result/?q= pattern. Override only `name`,
    `_domain` (where the search lives), and optional `wait_seconds`."""
    _domain: str = ""           # e.g. "https://www.courts.com.sg"
    tile_selectors = ["li.product-item", "div.product-item-info"]
    wait_seconds = 6.0

    @classmethod
    def search_url(cls, query: str) -> str:
        from urllib.parse import quote
        return f"{cls._domain}/catalogsearch/result/?q={quote(query)}"


class GainCity(MagentoMarketplace):
    name = "gaincity.com"
    _domain = "https://www.gaincity.com"
    search_url_template = "https://www.gaincity.com/catalogsearch/result/?q={q}"  # unused but required by base
    wait_seconds = 10.0   # CF Managed Challenge takes a beat


class Courts(MagentoMarketplace):
    name = "courts.com.sg"
    _domain = "https://www.courts.com.sg"
    search_url_template = "https://www.courts.com.sg/catalogsearch/result/?q={q}"
    wait_seconds = 11.0   # Magento + heavy theme — needs hydration time


class BestDenki(MagentoMarketplace):
    name = "bestdenki.com.sg"
    _domain = "https://www.bestdenki.com.sg"
    search_url_template = "https://www.bestdenki.com.sg/catalogsearch/result/?q={q}"
    wait_seconds = 11.0


# ── One-off marketplaces (own selectors) ─────────────────────────────────────


class FairPrice(Marketplace):
    name = "fairprice.com.sg"
    search_url_template = "https://www.fairprice.com.sg/search?query={q}"
    tile_selectors = ["a[href*='/product/']", "div[class*='product-tile']"]
    wait_seconds = 7.0


class ZaloraSG(Marketplace):
    name = "zalora.sg"
    search_url_template = "https://www.zalora.sg/search?q={q}"
    tile_selectors = ["a[data-test-id='productLink']", "div.b-catalogList__itemImage"]
    wait_seconds = 6.0


# Registry of nodriver-driven marketplaces. Add to this as sweep results
# clear more sites.
MARKETPLACES: dict[str, type[Marketplace]] = {
    Shopee.name:    Shopee,
    Lazada.name:    Lazada,
    AmazonSG.name:  AmazonSG,
    GainCity.name:  GainCity,
    Courts.name:    Courts,
    BestDenki.name: BestDenki,
    FairPrice.name: FairPrice,
    ZaloraSG.name:  ZaloraSG,
}


async def search_marketplaces(pool: BrowserPool, query: str, *,
                               marketplaces: list[str] | None = None,
                               top_n: int = 10) -> tuple[list[Listing], list[dict]]:
    """Run a search across selected marketplaces SERIALLY.

    Serial (not concurrent) because:
      - Each query uses the same single Chrome instance
      - Browsing two tabs at once is its own detection signal

    Returns (listings, blocked_or_errored). The second tuple element lets the
    caller tell the LLM 'Shopee was bot-blocked' instead of pretending it
    returned no results.
    """
    targets = marketplaces or list(MARKETPLACES.keys())
    out: list[Listing] = []
    issues: list[dict] = []
    for name in targets:
        cls = MARKETPLACES.get(name)
        if cls is None:
            logger.warning("Unknown marketplace: %r", name)
            issues.append({"marketplace": name, "status": "unknown_marketplace"})
            continue

        attempted_retry = False
        while True:
            try:
                out.extend(await cls.search(pool, query, top_n=top_n))
                break
            except MarketplaceBlocked as e:
                issues.append({"marketplace": name, "status": "bot_blocked",
                               "reason": e.reason, "url": e.url, "title": e.title})
                break
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                # Dead-WS / dead-browser symptoms — reset the pool and retry once.
                ws_dead = (
                    "no close frame" in str(e).lower()
                    or "connection closed" in str(e).lower()
                    or "websocket" in str(e).lower()
                )
                if ws_dead and not attempted_retry:
                    logger.warning("%s: browser appears dead — resetting pool and retrying once",
                                   name)
                    await pool.reset()
                    attempted_retry = True
                    continue
                logger.error("%s search failed: %s", name, msg)
                issues.append({"marketplace": name, "status": "error",
                               "error": msg[:200]})
                break

        # Inter-marketplace polite pause
        await asyncio.sleep(random.uniform(2.0, 4.0))
    return out, issues
