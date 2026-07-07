"""Sentinel Box Link — the phone's authenticated tailnet channel to the box.

Reached by the phone (a tailnet peer) at https://boxlink.svc.your-domain.example.com
(caddy-tailnet -> host.docker.internal:8130). Tailnet-only, so it is NOT behind
Cloudflare Access (which gates the public suite.your-domain.example.com hub) — that's why
the in-app self-update version check rides this instead of /api/apps.

Phase C capabilities, added incrementally:
  - /health                     liveness
  - /update/{app_id}            self-update version check (this slice)
  - /mcp/* (later)              proxy to metamcp for Scout's tools
  - /dove/turn (later)          wrap OpenClaw's turn API (Phase D)
  - /memory/* (later)           brain-store read/write (Phase E)

Auth: a static bearer token (BOX_LINK_TOKEN). Owner-only; the phone carries the
same token. If unset, requests are allowed (dev) but a warning is logged.
"""
import datetime
import json
import logging
import os
import pathlib
import re

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

log = logging.getLogger("box-link")
logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("BOX_LINK_TOKEN", "").strip()
APPS_DIR = pathlib.Path(os.environ.get("SENTINEL_APPS_DIR", "/apps"))
APK_BASE = os.environ.get("APK_BASE_URL", "https://suite.your-domain.example.com/apps").rstrip("/")
# When set, the box link serves the APK ITSELF over the tailnet (reliable; the
# public CF URL is flaky for the phone's DownloadManager when tailscale is on —
# DNS-split / CF edge). Falls back to the public APK_BASE otherwise.
BOX_LINK_BASE = os.environ.get("BOX_LINK_BASE_URL", "").rstrip("/")
# Phase D — the "smart lane": the box's big model (Qwen) reached as Dove. The phone
# escalates harder questions here over the box link.
QWEN_BASE = os.environ.get("QWEN_BASE", "http://host.docker.internal:1234").rstrip("/")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen/qwen3.6-27b")

if not TOKEN:
    log.warning("BOX_LINK_TOKEN is unset — auth is OPEN (dev mode).")

app = FastAPI(title="Sentinel Box Link", version="0.1.0")

_SEMVER = re.compile(r"^\d+(\.\d+)*")


def _require_auth(authorization: str | None) -> None:
    if not TOKEN:
        return
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


def _ver_tuple(v: str) -> tuple[int, ...]:
    m = _SEMVER.match((v or "").strip())
    if not m:
        return (0,)
    return tuple(int(x) for x in m.group(0).split("."))


@app.get("/health")
def health():
    return {"status": "ok", "service": "box-link", "version": app.version}


@app.get("/update/{app_id}")
def update(app_id: str, current: str | None = None, authorization: str | None = Header(default=None)):
    """Latest version of an app from the apps-hub manifest + the (public) APK URL.
    Pass ?current=<installed version> to get update_available computed for you."""
    _require_auth(authorization)
    manifest_path = APPS_DIR / "manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(status_code=503, detail="manifest unavailable")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next((a for a in manifest.get("apps", []) if a.get("id") == app_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="unknown app")
    versions = entry.get("versions", [])
    if not versions:
        raise HTTPException(status_code=404, detail="no versions")
    latest = versions[0]  # manifest is newest-first
    ver = latest["version"]
    update_available = current is not None and _ver_tuple(ver) > _ver_tuple(current)
    return {
        "id": app_id,
        "version": ver,
        "update_available": update_available,
        # apk_url = reliable tailnet (works with older apps too); apk_url_fast = the
        # public CF CDN (much faster — direct over the internet, not via tailscale).
        # Newer apps stream apk_url_fast first and fall back to apk_url.
        "apk_url": (f"{BOX_LINK_BASE}/apk/{app_id}/{latest['file']}" if BOX_LINK_BASE
                    else f"{APK_BASE}/{app_id}/{latest['file']}"),
        "apk_url_fast": f"{APK_BASE}/{app_id}/{latest['file']}",
        "sha256": latest.get("sha256"),
        "size_bytes": latest.get("size_bytes"),
        "changelog": latest.get("changelog"),
    }


@app.get("/apk/{app_id}/{path:path}")
def apk(app_id: str, path: str, authorization: str | None = Header(default=None)):
    """Serve an APK from the mounted apps dir over the tailnet. Path-traversal
    guarded; .apk only."""
    _require_auth(authorization)
    base = (APPS_DIR / app_id).resolve()
    target = (base / path).resolve()
    if base not in target.parents or not target.is_file() or target.suffix.lower() != ".apk":
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(target), media_type="application/vnd.android.package-archive",
                        filename=target.name)


# ── Tools (Phase C MCP slice 1) ──────────────────────────────────────────────
# Native, read-only tools to validate the on-device tool-use loop before adding
# the metamcp proxy. All side-effect-free, so safe to expose open for now.

def _tool_time(_args: dict) -> dict:
    now = datetime.datetime.now().astimezone()
    return {
        "datetime": now.isoformat(timespec="seconds"),
        "date": now.strftime("%A, %d %B %Y"),
        "time": now.strftime("%H:%M %Z"),
    }


def _tool_web_search(args: dict) -> dict:
    q = str(args.get("query") or "").strip()
    if not q:
        return {"error": "query is required"}
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            hits = list(ddgs.text(q, max_results=5))
        return {"query": q, "results": [
            {"title": h.get("title"), "snippet": h.get("body"), "url": h.get("href")} for h in hits
        ]}
    except Exception as e:  # noqa: BLE001 — surface the reason to the model
        return {"error": f"search failed: {e}"}


TOOLS = {
    "time": {
        "description": "Get the current local date and time on the box.",
        "params": {},
        "fn": _tool_time,
    },
    "web_search": {
        "description": "Search the web for current/up-to-date information.",
        "params": {"query": "string — what to search for"},
        "fn": _tool_web_search,
    },
}


class CallReq(BaseModel):
    tool: str
    args: dict = {}


@app.get("/tools")
def list_tools(authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    return {"tools": [
        {"name": k, "description": v["description"], "params": v["params"]} for k, v in TOOLS.items()
    ]}


@app.post("/call")
def call_tool(body: CallReq, authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    tool = TOOLS.get(body.tool)
    if not tool:
        raise HTTPException(status_code=404, detail=f"unknown tool: {body.tool}")
    try:
        return {"tool": body.tool, "result": tool["fn"](body.args or {})}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"tool error: {e}")


# ── The smart lanes (Phase D) ────────────────────────────────────────────────
# Three agents, by deliberate design, form a DEPENDENCY LADDER so each is the
# fallback for the one above (resilience if the hive ever goes unmaintainable):
#   Scout (on the phone) — depends on nothing; the offline floor.
#   Sage  (here)         — raw Qwen-27B; depends on the model server only.
#   Dove  (here)         — the hive: Qwen + OpenClaw (memory + tools).
# Sage and the current Dove both forward to raw Qwen (different personas); the
# Dove endpoint becomes the *real* OpenClaw-backed Dove in the next slice — a
# transparent swap as far as the phone is concerned.

SAGE_SYSTEM = (
    "You are Sage, a large, knowledgeable model on the Sentinel box. You are the "
    "standalone smart lane: no memory of the owner and no access to their systems or "
    "tools — you reason from a clean slate every time. Scout (a small fast model on the "
    "owner's phone) hands you the harder, long-context questions. Be thorough, accurate "
    "and clear, and be honest about what you cannot see. You are not Dove."
)
DOVE_SYSTEM = (
    "You are Dove, the Sentinel mesh's box-side assistant running on a large model. "
    "Scout, a small fast model on the owner's phone, hands you the deep reasoning, "
    "long-context and harder questions. Be thorough, accurate and clear."
)


def _qwen_chat(system: str, messages: list, who: str) -> dict:
    """Forward a conversation to the box's big model (Qwen) with a given persona."""
    import urllib.request
    msgs = [{"role": "system", "content": system}] + list(messages or [])
    payload = json.dumps({"model": QWEN_MODEL, "messages": msgs, "stream": False}).encode()
    req = urllib.request.Request(
        f"{QWEN_BASE}/v1/chat/completions", data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            data = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"{who} unreachable: {e}")
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return {"reply": text or "(no reply)", "model": QWEN_MODEL}


class ChatReq(BaseModel):
    messages: list = []


# Back-compat alias for the original /dove/chat body model name.
DoveReq = ChatReq


@app.post("/sage/chat")
def sage_chat(body: ChatReq, authorization: str | None = Header(default=None)):
    """Sage — raw Qwen-27B, no hive/tools. The standalone smart-lane fallback."""
    _require_auth(authorization)
    return _qwen_chat(SAGE_SYSTEM, body.messages, "sage")


@app.post("/dove/chat")
def dove_chat(body: ChatReq, authorization: str | None = Header(default=None)):
    """Smart-lane chat — forwards to the box's big model (Qwen) with a Dove persona.
    First cut; the full OpenClaw Dove (memory + tools) is a later upgrade that swaps
    this endpoint's body for the OpenClaw turn API — transparent to the phone."""
    _require_auth(authorization)
    return _qwen_chat(DOVE_SYSTEM, body.messages, "dove")
