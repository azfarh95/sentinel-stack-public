"""Telco adapter base.

There are two kinds of carriers in the SG market:
  HttpxCarrier   — server-rendered, plans extractable from raw HTML
                   (eight.com.sg, vivifi.me, gomo.sg, hicard.sg)
  NodriverCarrier — JS-rendered SPA, needs a browser
                    (Simba, Zero1, Circles.Life, MyRepublic, ZYM, ...)

Each subclass implements `fetch_plans()` returning list[TelcoPlan].
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import ClassVar

import httpx

from schema import TelcoPlan

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")


# Common SG-price extractor — matches "$29.90", "$ 29.90", "S$29.90"
PRICE_RE = re.compile(r"S?\$\s?([\d,]+(?:\.\d{1,2})?)")

# Data-allowance extractor — matches "100GB", "Unlimited", "1.5GB"
DATA_RE = re.compile(r"(\d+(?:\.\d+)?)\s*GB", re.IGNORECASE)
UNLIMITED_RE = re.compile(r"unlimited", re.IGNORECASE)

# Speed extractor — matches "1Gbps", "500Mbps", "10G"
SPEED_GBPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Gbps", re.IGNORECASE)
SPEED_MBPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Mbps", re.IGNORECASE)


def parse_price(text: str) -> float | None:
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_data_gb(text: str) -> float | None:
    """Returns float, or float('inf') for unlimited, or None."""
    if not text:
        return None
    if UNLIMITED_RE.search(text):
        return float("inf")
    m = DATA_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_speed_mbps(text: str) -> int | None:
    if not text:
        return None
    m = SPEED_GBPS_RE.search(text)
    if m:
        try:
            return int(float(m.group(1)) * 1000)
        except ValueError:
            pass
    m = SPEED_MBPS_RE.search(text)
    if m:
        try:
            return int(float(m.group(1)))
        except ValueError:
            pass
    return None


class Carrier(ABC):
    name:        ClassVar[str]
    network:     ClassVar[str]                  # "singtel" | "starhub" | "m1"
    category:    ClassVar[str]                  # "mobile" | "broadband"
    home_url:    ClassVar[str]
    plans_url:   ClassVar[str]

    @abstractmethod
    async def fetch_plans(self, *, pool=None) -> list[TelcoPlan]:
        # pool is the shared nodriver BrowserPool, only required by
        # NodriverCarrier subclasses. HttpxCarrier ignores it.
        ...


class HttpxCarrier(Carrier):
    """Carriers whose plan page is server-rendered. fetch() just gets HTML."""

    async def _get(self, url: str) -> str:
        async with httpx.AsyncClient() as c:
            r = await c.get(url, headers={"User-Agent": UA}, timeout=15.0,
                             follow_redirects=True)
            r.raise_for_status()
            return r.text


class NodriverCarrier(Carrier):
    """Carriers whose plan page is a JS-rendered SPA. Need a browser.

    Subclasses implement `extract(soup) -> list[TelcoPlan]` against the
    rendered HTML. The pool argument is passed from telco_plans tool via
    `fetch_plans(pool=...)`; subclasses don't manage it directly.
    """
    wait_seconds: float = 8.0

    async def fetch_plans(self, *, pool=None) -> list[TelcoPlan]:
        if pool is None:
            raise NotImplementedError(
                f"{self.name}: nodriver pool not supplied — call via mcp_server "
                "telco_plans tool which injects the shared BrowserPool")
        import asyncio
        import random
        from bs4 import BeautifulSoup

        browser = await pool.get()
        page = await browser.get(self.plans_url)
        await asyncio.sleep(self.wait_seconds + random.uniform(0, 2))
        html = await page.evaluate("document.documentElement.outerHTML")
        if isinstance(html, dict):
            html = html.get("value", "") or ""
        if not isinstance(html, str):
            html = str(html)
        soup = BeautifulSoup(html, "lxml")
        return self.extract(soup)

    def extract(self, soup) -> list[TelcoPlan]:
        """Hand-tuned per-carrier parsing against the rendered DOM."""
        raise NotImplementedError(f"{self.name}: extract() not implemented")
