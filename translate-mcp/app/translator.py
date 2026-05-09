"""LibreTranslate client — local primary with public fallback."""

import asyncio
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger(__name__)

LT_BASE_URL     = os.environ.get("LT_BASE_URL", "http://libretranslate:5000").rstrip("/")
LT_FALLBACK_URL = os.environ.get("LT_FALLBACK_URL", "https://translate.argosopentech.com").rstrip("/")
LT_API_KEY      = os.environ.get("LT_API_KEY", "")

_TIMEOUT = httpx.Timeout(connect=3.0, read=20.0, write=10.0, pool=5.0)


async def _post(url: str, payload: dict) -> dict:
    if LT_API_KEY:
        payload = dict(payload, api_key=LT_API_KEY)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(url, json=payload)
        r.raise_for_status()
        return r.json()


async def _get(url: str) -> dict | list:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.json()


_RETRY_ATTEMPTS = 3
_RETRY_SLEEP    = 2.0  # seconds between attempts


async def _try_endpoints(path: str, payload: Optional[dict] = None, method: str = "POST"):
    """Try local first, fall back to public on connection/5xx errors.
    Retries each endpoint up to _RETRY_ATTEMPTS times to handle cold-start 503s."""
    last_err: Exception | None = None
    for base in (LT_BASE_URL, LT_FALLBACK_URL):
        if not base:
            continue
        url = f"{base}{path}"
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                if method == "POST":
                    return await _post(url, payload or {}), base
                return await _get(url), base
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                log.warning("LibreTranslate %s unreachable (attempt %d/%d): %s", base, attempt, _RETRY_ATTEMPTS, e)
                last_err = e
            except httpx.HTTPStatusError as e:
                if 500 <= e.response.status_code < 600:
                    log.warning("LibreTranslate %s returned %s (attempt %d/%d)", base, e.response.status_code, attempt, _RETRY_ATTEMPTS)
                    last_err = e
                else:
                    raise
            if attempt < _RETRY_ATTEMPTS:
                await asyncio.sleep(_RETRY_SLEEP)
        log.warning("LibreTranslate %s failed after %d attempts; trying next endpoint", base, _RETRY_ATTEMPTS)
    raise RuntimeError(f"All LibreTranslate endpoints failed after retries: {last_err}")


async def translate(text: str, source: str, target: str, fmt: str = "text") -> dict:
    """Translate text. source can be 'auto' for detection."""
    payload = {"q": text, "source": source, "target": target, "format": fmt}
    data, used = await _try_endpoints("/translate", payload, "POST")
    return {
        "translated": data.get("translatedText", ""),
        "detected_source": (data.get("detectedLanguage") or {}).get("language") if source == "auto" else source,
        "endpoint": used,
    }


async def detect(text: str) -> list[dict]:
    """Detect the language of given text."""
    data, _used = await _try_endpoints("/detect", {"q": text}, "POST")
    return data if isinstance(data, list) else [data]


async def list_languages() -> list[dict]:
    data, _used = await _try_endpoints("/languages", method="GET")
    return data if isinstance(data, list) else []
