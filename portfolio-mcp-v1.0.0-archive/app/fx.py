"""FX rate management — reads/writes balance_sheet_config.yaml, optional auto-fetch.

Sources:
  manual  : value comes from the YAML, user-edited
  xe.com  : scrape xe.com USD→SGD page
  oanda   : OANDA free public converter

Returns (rate: float, source: str, last_updated: str). All callers
should use `get_fx()` rather than reading the YAML directly.
"""
import os
import re
import logging
from datetime import date
from pathlib import Path

import httpx
import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("/finance/balance_sheet_config.yaml")
SOURCES = ("manual", "xe.com", "oanda")
DEFAULT_RATE = 1.27


def get_fx() -> dict:
    """Return current FX state from the config YAML."""
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text())
    except Exception:
        cfg = {}
    return {
        "rate": float(cfg.get("usd_to_sgd", DEFAULT_RATE)),
        "source": cfg.get("fx_source", "manual"),
        "last_updated": cfg.get("fx_last_updated", "—"),
    }


def save_fx(rate: float, source: str) -> dict:
    """Write the new FX state back to the YAML, preserving all other keys."""
    text = CONFIG_PATH.read_text()
    today = date.today().isoformat()
    # Surgical regex replacements so we don't reformat the whole YAML
    text = re.sub(r"^usd_to_sgd:.*$", f"usd_to_sgd: {rate}       # set via /config/fx", text, count=1, flags=re.M)
    if "fx_source:" in text:
        text = re.sub(r"^fx_source:.*$", f'fx_source: "{source}"', text, count=1, flags=re.M)
    else:
        text += f'\nfx_source: "{source}"\n'
    if "fx_last_updated:" in text:
        text = re.sub(r"^fx_last_updated:.*$", f'fx_last_updated: "{today}"', text, count=1, flags=re.M)
    else:
        text += f'\nfx_last_updated: "{today}"\n'
    CONFIG_PATH.write_text(text)
    return {"rate": rate, "source": source, "last_updated": today}


async def fetch_rate(source: str) -> tuple[float | None, str | None]:
    """Pull a fresh USD→SGD rate from the named source. Returns (rate, error)."""
    if source == "manual":
        return None, "manual source — no auto-fetch"
    try:
        if source == "xe.com":
            return await _fetch_xe()
        if source == "oanda":
            return await _fetch_oanda()
    except Exception as e:
        logger.exception("FX fetch failed")
        return None, f"{type(e).__name__}: {e}"
    return None, f"unknown source: {source}"


async def _fetch_xe() -> tuple[float | None, str | None]:
    url = "https://www.xe.com/currencyconverter/convert/?Amount=1&From=USD&To=SGD"
    async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0"}) as c:
        r = await c.get(url)
    # Match the rate in the page — xe renders it in <p class="result__BigRate-sc-...">1.2700</p>
    m = re.search(r"([0-9]+\.[0-9]{2,6})\s*Singapore Dollars", r.text)
    if m: return float(m.group(1)), None
    m = re.search(r'"toAmount":\s*([0-9.]+)', r.text)
    if m: return float(m.group(1)), None
    return None, "xe.com parsing failed — markup may have changed"


async def _fetch_oanda() -> tuple[float | None, str | None]:
    # OANDA's free rate API (no key needed)
    url = "https://www.oanda.com/fx-for-business/historical-rates/api/data/update/"
    params = {
        "source": "OANDA", "adjustment": "0",
        "base_currencies": "USD", "quote_currencies": "SGD",
        "data_range": "d1", "period": "daily",
    }
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as c:
        r = await c.get(url, params=params)
        data = r.json()
    rate = (data.get("widget", [{}])[0]
                .get("baseCurrency", {}).get("data", [[None, None]])[-1][1])
    if rate is not None:
        return float(rate), None
    return None, "oanda response shape unexpected"
