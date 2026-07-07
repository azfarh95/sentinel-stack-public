"""Sentinel Home MCP — lets the local LLM / Dove control the smart home.

A thin tool layer over the `home.svc` backend (which holds the HA token + the
owner gate). The MCP mints an owner app-JWT (same HS256 + secret as the app) and
calls home.svc; it only *proposes* typed intents — home.svc/HA *disposes*,
owner-gated. Mirrors the time-mcp FastMCP pattern (streamable_http_app + /health).

So: "Dove, turn on the aircon" → control_device / activate_scene → home.svc → HA.
"""
import logging
import os
import time

import httpx
import jwt
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

HOME_SVC_URL = os.environ.get(
    "HOME_SVC_URL", "http://sentinel-home-backend:8120").rstrip("/")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "your@email.com").strip().lower()
JWT_SECRET = os.environ.get("HOME_APP_JWT_SECRET", "")

_SWITCHABLE = {"toggle", "light", "fan"}


def _token() -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": OWNER_EMAIL, "iat": now, "exp": now + 3600},
        JWT_SECRET, algorithm="HS256")


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


async def _snapshot() -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{HOME_SVC_URL}/home/snapshot", headers=_headers())
        r.raise_for_status()
        return r.json()


async def _service(domain: str, service: str, entity_id: str, data: dict | None = None) -> None:
    body = {"domain": domain, "service": service, "entity_id": entity_id}
    if data:
        body["data"] = data
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{HOME_SVC_URL}/service", headers=_headers(), json=body)
        r.raise_for_status()


def _cap(e: dict) -> str:
    return (e.get("capabilities") or {}).get("type", "toggle")


def _match_device(snap: dict, query: str) -> dict | None:
    q = (query or "").strip().lower()
    devs = list(snap.get("devices", {}).values())
    for d in devs:
        if (d.get("name") or "").lower() == q:
            return d
    hits = [d for d in devs if q and q in (d.get("name") or "").lower()]
    return hits[0] if hits else None


async def _fire_scene(name: str) -> dict:
    snap = await _snapshot()
    ents = snap.get("entities", {})
    q = (name or "").strip().lower()
    scenes = [e for e in ents.values() if _cap(e) == "scene"]
    hit = next((e for e in scenes if (e.get("name") or "").lower() == q), None) \
        or next((e for e in scenes if q and q in (e.get("name") or "").lower()), None)
    if not hit:
        return {"ok": False,
                "error": f"no scene matching {name!r}",
                "available": [e["name"] for e in scenes]}
    await _service("scene", "turn_on", hit["entity_id"])
    return {"ok": True, "scene": hit["name"]}


mcp = FastMCP(
    "Sentinel Home",
    instructions=(
        "Control the owner's smart home (lights, plugs, fans, the aircon, scenes) "
        "via Home Assistant. Call home_status() first to learn what's on and the "
        "exact device/scene names, then control_device / activate_scene / "
        "set_aircon. The aircon currently has no climate entity, so use "
        "activate_scene('On bedroom AC') / ('Off bedroom AC') — or set_aircon "
        "with power='on'/'off'. You only propose actions; the backend enforces "
        "the owner gate."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*",
                       "host.docker.internal:*", "sentinel-home-mcp:*", "metamcp:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                         "http://host.docker.internal:*",
                         "http://sentinel-home-mcp:*", "http://metamcp:*"],
    ),
)


@mcp.tool()
async def home_status() -> dict:
    """
    Snapshot of the home: rooms, what's on right now, available scenes, and
    whether the aircon is a controllable climate entity. Call this FIRST so you
    know the exact device/scene names and current state before acting.
    """
    snap = await _snapshot()
    ents = snap.get("entities", {})
    on = [e["name"] for e in ents.values()
          if _cap(e) not in ("sensor", "scene") and e.get("is_on")]
    scenes = [e["name"] for e in ents.values() if _cap(e) == "scene"]
    has_climate = any(_cap(e) == "climate" for e in ents.values())
    return {
        "rooms": [a["name"] for a in snap.get("areas", [])],
        "device_count": len(snap.get("devices", {})),
        "on_now": on,
        "scenes": scenes,
        "has_aircon_climate_entity": has_climate,
        "aircon_note": ("Use set_aircon for temp/mode/fan." if has_climate else
                        "No climate entity — use activate_scene('On/Off bedroom AC') "
                        "or set_aircon(power='on'|'off')."),
    }


@mcp.tool()
async def list_devices(query: str = "") -> dict:
    """
    List controllable devices (name, room, on/off), optionally filtered by a
    name substring. Use the exact returned 'name' with control_device.
    """
    snap = await _snapshot()
    ents = snap.get("entities", {})
    areas = {a["id"]: a["name"] for a in snap.get("areas", [])}
    q = (query or "").strip().lower()
    out = []
    for d in snap.get("devices", {}).values():
        name = d.get("name", "")
        if q and q not in name.lower():
            continue
        ctrl = [ents[e] for e in d.get("entity_ids", [])
                if e in ents and _cap(ents[e]) in _SWITCHABLE]
        if not ctrl:
            continue
        out.append({"name": name, "room": areas.get(d.get("area_id"), "—"),
                    "on": any(e.get("is_on") for e in ctrl)})
    return {"count": len(out), "devices": out}


@mcp.tool()
async def control_device(name: str, turn_on: bool) -> dict:
    """
    Turn a device on or off by name (e.g. 'Fish tank'). Matches the device whose
    name contains the text and flips all its switches. Call list_devices if
    unsure of the name.
    """
    snap = await _snapshot()
    d = _match_device(snap, name)
    if not d:
        return {"ok": False,
                "error": f"no device matching {name!r}; call list_devices."}
    ents = snap.get("entities", {})
    switches = [ents[e] for e in d.get("entity_ids", [])
                if e in ents and _cap(ents[e]) in _SWITCHABLE]
    if not switches:
        return {"ok": False, "error": f"{d['name']} has no on/off controls."}
    for e in switches:
        dom = e["domain"]
        svc = ("unlock" if turn_on else "lock") if dom == "lock" \
            else ("turn_on" if turn_on else "turn_off")
        await _service(dom, svc, e["entity_id"])
    return {"ok": True, "device": d["name"],
            "set": "on" if turn_on else "off", "switches": len(switches)}


@mcp.tool()
async def activate_scene(name: str) -> dict:
    """
    Run a scene by name (e.g. 'On bedroom AC', 'Movie'). This is the way to turn
    the aircon on/off until it has a climate entity.
    """
    return await _fire_scene(name)


@mcp.tool()
async def set_aircon(temperature: float = 0, mode: str = "", fan: str = "",
                     power: str = "") -> dict:
    """
    Control the aircon. With a climate entity: temperature (°C), mode
    (cool/heat/dry/fan_only/auto/off), fan (low/medium/high). Without one, falls
    back to scenes for on/off via power='on'|'off'. Pass only what you change.
    """
    snap = await _snapshot()
    ents = snap.get("entities", {})
    climate = next((e for e in ents.values() if _cap(e) == "climate"), None)
    if climate is None:
        p = (power or "").strip().lower()
        if p in ("on", "off"):
            return await _fire_scene(f"{p} bedroom ac")
        return {"ok": False,
                "error": "No climate entity. Use power='on'|'off' (scene fallback), "
                         "or activate_scene. Temp/mode/fan need a Broadlink+SmartIR "
                         "climate entity."}
    eid = climate["entity_id"]
    applied = []
    if (power or "").strip().lower() == "off":
        await _service("climate", "set_hvac_mode", eid, {"hvac_mode": "off"})
        applied.append("off")
    if mode:
        await _service("climate", "set_hvac_mode", eid, {"hvac_mode": mode})
        applied.append(f"mode={mode}")
    if temperature:
        await _service("climate", "set_temperature", eid, {"temperature": temperature})
        applied.append(f"{temperature}°")
    if fan:
        await _service("climate", "set_fan_mode", eid, {"fan_mode": fan})
        applied.append(f"fan={fan}")
    return {"ok": True, "aircon": climate["name"], "applied": applied or ["(no change)"]}


@mcp.tool()
async def set_presence(home: bool) -> dict:
    """
    Tell the home you've arrived (home=true) or left (home=false). Triggers HA
    presence + the owner's arrival/leaving automation.
    """
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{HOME_SVC_URL}/presence", headers=_headers(),
                         json={"event": "enter" if home else "exit", "zone": "home"})
        r.raise_for_status()
        return {"ok": True, **r.json()}


async def _health(request):
    return JSONResponse({"status": "ok", "service": "sentinel-home-mcp",
                         "home_svc": HOME_SVC_URL, "jwt_secret_set": bool(JWT_SECRET)})


app = mcp.streamable_http_app()
app.router.routes.insert(
    0,
    __import__("starlette.routing", fromlist=["Route"]).Route(
        "/health", _health, methods=["GET"]),
)
