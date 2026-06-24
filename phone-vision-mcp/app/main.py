"""Phone Vision MCP — the box-side shim for Sentinel AI Mobile's `phone.vision`.

Routes an image + prompt to the owner's on-device VLM (Gemma-4-E4B) when the
phone endpoint is configured AND reachable, and falls back to the box's Qwen-VL
(llama.cpp --mmproj on the local OpenAI server :1234) otherwise. The phone is an
OPTIMISATION, never a dependency — the caller always gets an answer.

This is Phase-2 step 1 of Sentinel AI Mobile (see sentinel-ai-mobile/SCOPE.md):
with PHONE_VL_BASE unset it runs entirely on Qwen-VL, which validates the
metamcp -> shim -> model -> Dove contract before the phone app exists. Once the
phone's InferenceService is up, set PHONE_VL_BASE and it prefers the phone.

Mirrors the pdf-mcp FastMCP pattern (streamable_http_app + /health). The Qwen-VL
call path mirrors openclaw/tg_bot/attachment_processor._extract_image_vision.
Image input: a file under /data, an http(s) URL, or a base64 / data-URL string.
"""

import base64
import json
import logging
import os
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DATA_DIR = "/data"
MAX_CHARS = 120_000

# Box fallback: Qwen-VL (llama.cpp --mmproj) on the host OpenAI server.
QWEN_BASE = os.environ.get("QWEN_VL_BASE", "http://host.docker.internal:1234").rstrip("/")
# Phone upstream (Sentinel AI Mobile InferenceService). Empty until the app exists.
PHONE_BASE = os.environ.get("PHONE_VL_BASE", "").rstrip("/")
PHONE_TOKEN = os.environ.get("PHONE_VL_TOKEN", "")
PHONE_MODEL = os.environ.get("PHONE_VL_MODEL", "gemma-4-e4b")
TIMEOUT = int(os.environ.get("PHONE_VL_TIMEOUT", "180"))

DEFAULT_PROMPT = (
    "Read this image. Transcribe ALL visible text verbatim (exact wording, "
    "numbers, times). If any text is not in English, also give a faithful English "
    "translation. Be complete; do not summarise."
)

_CACHED_QWEN_MODEL = None


def _llm_key() -> str:
    return os.environ.get("LLM_API_KEY") or os.environ.get("LM_API_TOKEN") or ""


def _mime_for(name: str) -> str:
    ext = Path(name).suffix.lower().lstrip(".") or "png"
    return "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"


def _resolve_image(image: str):
    """Return (b64, mime) from a /data path, an http(s) URL, or a base64/data-URL."""
    s = (image or "").strip()
    if s.startswith("data:") and ";base64," in s:
        head, b64 = s.split(";base64,", 1)
        mime = head[5:].split(";", 1)[0] or "image/png"
        return b64, mime
    if s.startswith("http://") or s.startswith("https://"):
        with urllib.request.urlopen(s, timeout=30) as r:
            return base64.b64encode(r.read()).decode("ascii"), _mime_for(s)
    p = Path(s)
    if not p.is_absolute():
        cand = Path(DATA_DIR) / s
        if cand.exists():
            p = cand
    if p.exists() and p.is_file():
        return base64.b64encode(p.read_bytes()).decode("ascii"), _mime_for(p.name)
    # last resort: assume the string already IS base64
    return s, "image/png"


def _call_vision(base, model, b64, mime, prompt, key, timeout) -> str:
    """POST an OpenAI-compatible multimodal chat completion; return the text."""
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        "max_tokens": 3072,
        "temperature": 0.1,   # faithful OCR-style reading
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read())
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
    if not text:
        raise RuntimeError("empty completion")
    return text


def _find_qwen_model(key) -> "str | None":
    """Discover the vision-capable model on the host server (mirrors the
    attachment_processor probe: name heuristic, then /props vision capability)."""
    global _CACHED_QWEN_MODEL
    if _CACHED_QWEN_MODEL:
        return _CACHED_QWEN_MODEL
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        req = urllib.request.Request(f"{QWEN_BASE}/v1/models", headers=headers)
        with urllib.request.urlopen(req, timeout=4) as r:
            models = json.loads(r.read()).get("data", [])
        for m in models:
            mid = (m.get("id") or "").lower()
            if "-vl-" in mid or "vision" in mid or mid.startswith("vl-"):
                _CACHED_QWEN_MODEL = m["id"]
                return _CACHED_QWEN_MODEL
        try:
            preq = urllib.request.Request(f"{QWEN_BASE}/props", headers=headers)
            with urllib.request.urlopen(preq, timeout=4) as r:
                props = json.loads(r.read())
            if (props.get("modalities") or {}).get("vision") and models:
                _CACHED_QWEN_MODEL = models[0]["id"]
                return _CACHED_QWEN_MODEL
        except Exception as e:
            logger.debug("qwen /props probe failed: %s", e)
        if models:  # single-model server with no /props — best effort
            _CACHED_QWEN_MODEL = models[0]["id"]
            return _CACHED_QWEN_MODEL
    except Exception as e:
        logger.warning("qwen model probe failed: %s", e)
    return None


def _phone_online() -> bool:
    if not PHONE_BASE:
        return False
    try:
        headers = {"Authorization": f"Bearer {PHONE_TOKEN}"} if PHONE_TOKEN else {}
        req = urllib.request.Request(f"{PHONE_BASE}/health", headers=headers)
        with urllib.request.urlopen(req, timeout=4) as r:
            return 200 <= getattr(r, "status", 200) < 300
    except Exception:
        return False


@asynccontextmanager
async def _lifespan(server: FastMCP):
    logger.info("phone-vision-mcp starting (phone=%s, qwen=%s)",
                PHONE_BASE or "(unset → qwen-vl only)", QWEN_BASE)
    yield
    logger.info("phone-vision-mcp shutting down")


mcp = FastMCP(
    "PhoneVision",
    lifespan=_lifespan,
    instructions=(
        "Sentinel AI Mobile fast-lane vision. Send an image + prompt; it is read by "
        "the owner's on-device VLM (Gemma-4-E4B) when the phone is online, else by "
        "the box's Qwen-VL. Use for: read or translate a screenshot/photo, describe "
        "an image, OCR. `image` = a file under /data, an http(s) URL, or a base64 / "
        "data-URL string. Returns {text, source, model}."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*",
                       "host.docker.internal:*", "phone-vision-mcp:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                         "http://host.docker.internal:*", "http://phone-vision-mcp:*"],
    ),
)


@mcp.tool()
async def phone_vision(image: str, prompt: str = "", prefer: str = "auto") -> dict:
    """
    Read an image with a vision model and answer `prompt` about it. Runs on the
    owner's phone (Gemma-4-E4B) when online, else the box's Qwen-VL.

    image  : a file under /data, an http(s) URL, or a base64 / data-URL string.
    prompt : what to do with it (default: transcribe verbatim + translate to English).
    prefer : "auto" (phone if online, else box), "phone", or "qwen".

    Returns {text, source: "phone"|"qwen-vl", model} or {error}.
    """
    p = (prompt or "").strip() or DEFAULT_PROMPT
    try:
        b64, mime = _resolve_image(image)
    except Exception as e:
        return {"error": f"could not load image: {e}"}

    # 1) phone, if asked/auto and reachable
    if prefer in ("auto", "phone") and _phone_online():
        try:
            text = _call_vision(PHONE_BASE, PHONE_MODEL, b64, mime, p, PHONE_TOKEN, TIMEOUT)
            return {"text": text[:MAX_CHARS], "source": "phone", "model": PHONE_MODEL}
        except Exception as e:
            logger.warning("phone vision failed, falling back to qwen: %s", e)
            if prefer == "phone":
                return {"error": f"phone unreachable: {e}", "source": "phone"}

    # 2) box Qwen-VL fallback
    key = _llm_key()
    model = _find_qwen_model(key)
    if not model:
        return {"error": "no vision model available (phone offline AND no Qwen-VL on the host)"}
    try:
        text = _call_vision(QWEN_BASE, model, b64, mime, p, key, TIMEOUT)
        return {"text": text[:MAX_CHARS], "source": "qwen-vl", "model": model}
    except Exception as e:
        return {"error": f"qwen-vl failed: {e}", "source": "qwen-vl"}


# ── Health ─────────────────────────────────────────────────────────────────────

async def _health(request):
    return JSONResponse({
        "status": "ok",
        "service": "phone-vision-mcp",
        "phone_configured": bool(PHONE_BASE),
        "phone_online": _phone_online(),
        "qwen_base": QWEN_BASE,
    })


# ── Build ASGI app ────────────────────────────────────────────────────────────

app = mcp.streamable_http_app()
app.router.routes.insert(
    0,
    __import__("starlette.routing", fromlist=["Route"]).Route("/health", _health, methods=["GET"]),
)
