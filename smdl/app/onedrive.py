"""OneDrive integration.

OAuth (device-code flow + refresh tokens) via MSAL, uploads via Microsoft Graph.
Account scope: personal Microsoft accounts only (matches the Azure app's
'consumers' tenant).

State lives in /data/onedrive_token.json (MSAL's SerializableTokenCache) plus
in-memory `_device_flow_state` for an in-progress device flow. The token
cache survives container restarts; the in-memory flow does not (the user just
re-clicks "Connect" if they bounced the container mid-authorization).

Public surface used by miniapp.py + bot.py:

    start_device_flow()      -> initiates auth, returns {user_code, url, ...}
    get_status()             -> for the admin UI status card
    disconnect()             -> wipe the cached token
    upload_file(local, dest) -> single-file upload, chunked if >4 MB
    auto_upload_files(...)   -> fire-and-forget mirror after a successful send
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
import msal


logger = logging.getLogger(__name__)


# ── Config ──────────────────────────────────────────────────────────────────


# Azure app registration ID. Public client (no secret), authority=consumers.
# Hard-coded as the safe default; can be overridden via env for future tenants.
CLIENT_ID = os.environ.get(
    "ONEDRIVE_CLIENT_ID",
    "d5b80727-5e6f-4096-a71b-b85aeaf19d66",
)
AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Files.ReadWrite", "User.Read"]
# Note: msal adds 'offline_access' automatically when using device flow.

TOKEN_CACHE_FILE = Path(os.environ.get("ONEDRIVE_TOKEN_FILE", "/data/onedrive_token.json"))

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# Microsoft Graph requires resumable upload sessions for files >4 MiB.
SIMPLE_UPLOAD_THRESHOLD = 4 * 1024 * 1024
CHUNK_SIZE              = 10 * 1024 * 1024  # 10 MiB; must be multiple of 320 KiB


# ── In-memory state for in-progress device flow ─────────────────────────────


@dataclass
class _DeviceFlowState:
    """Holds the MSAL `flow` dict between /connect (start) and the background
    polling task that completes acquisition. Single global slot — connecting
    twice replaces the prior flow (the user clicked again)."""
    flow:       dict
    started_at: float
    error:      Optional[str] = None
    done:       bool = False


_device_flow_state: Optional[_DeviceFlowState] = None
_device_flow_lock  = asyncio.Lock()


# ── Token cache helpers ─────────────────────────────────────────────────────


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    try:
        if TOKEN_CACHE_FILE.exists():
            cache.deserialize(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("OneDrive: failed to load token cache (%s); starting fresh", e)
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if not cache.has_state_changed:
        return
    try:
        TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TOKEN_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(cache.serialize(), encoding="utf-8")
        tmp.replace(TOKEN_CACHE_FILE)
        # File holds long-lived refresh tokens; keep it owner-only.
        try: os.chmod(TOKEN_CACHE_FILE, 0o600)
        except Exception: pass
    except Exception as e:
        logger.error("OneDrive: failed to persist token cache: %s", e)


def _build_app(cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    return msal.PublicClientApplication(
        client_id=CLIENT_ID,
        authority=AUTHORITY,
        token_cache=cache,
    )


# ── Token acquisition ───────────────────────────────────────────────────────


def _acquire_token_silent() -> tuple[Optional[str], Optional[dict]]:
    """Try to get a fresh access token from the cache (auto-refresh).
    Returns (token, account) or (None, None) if no cached account / refresh fails."""
    cache = _load_cache()
    app = _build_app(cache)
    accounts = app.get_accounts()
    if not accounts:
        return None, None
    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    _save_cache(cache)
    if result and "access_token" in result:
        return result["access_token"], accounts[0]
    return None, accounts[0]


async def get_valid_access_token() -> Optional[str]:
    """Async-friendly entrypoint. Runs the (blocking) MSAL refresh in a thread."""
    token, _ = await asyncio.to_thread(_acquire_token_silent)
    return token


# ── Device flow lifecycle ───────────────────────────────────────────────────


async def start_device_flow() -> dict:
    """Begin device-code auth. Returns the dict that the UI shows to the user
    (user_code, verification_uri, message, expires_in). Also schedules a
    background task that polls Microsoft until the user finishes auth, then
    persists the resulting token. Repeat calls replace the prior in-flight
    flow."""
    global _device_flow_state

    async with _device_flow_lock:
        cache = _load_cache()
        app = _build_app(cache)
        flow = await asyncio.to_thread(app.initiate_device_flow, scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"OneDrive: device flow init failed: {flow}")
        _device_flow_state = _DeviceFlowState(flow=flow, started_at=time.time())
        # Persist the cache state in case msal mutated it on init.
        _save_cache(cache)

        # Background completion. Wrap in try so a failure marks state.error
        # without crashing the loop.
        async def _complete():
            global _device_flow_state
            try:
                cache_inner = _load_cache()
                app_inner = _build_app(cache_inner)
                result = await asyncio.to_thread(
                    app_inner.acquire_token_by_device_flow, flow
                )
                _save_cache(cache_inner)
                if "access_token" not in result:
                    err = result.get("error_description") or result.get("error") or str(result)
                    logger.warning("OneDrive: device flow ended without token: %s", err)
                    if _device_flow_state:
                        _device_flow_state.error = err
                        _device_flow_state.done = True
                else:
                    logger.info("OneDrive: device flow completed; token persisted")
                    if _device_flow_state:
                        _device_flow_state.done = True
            except Exception as e:
                logger.exception("OneDrive: device flow worker crashed")
                if _device_flow_state:
                    _device_flow_state.error = str(e)
                    _device_flow_state.done = True

        asyncio.create_task(_complete())

    return {
        "user_code":         flow.get("user_code"),
        "verification_uri":  flow.get("verification_uri"),
        "message":           flow.get("message"),
        "expires_in":        flow.get("expires_in"),
    }


def disconnect() -> bool:
    """Wipe the token cache. Returns True if a token existed and was removed."""
    if TOKEN_CACHE_FILE.exists():
        try:
            TOKEN_CACHE_FILE.unlink()
            logger.info("OneDrive: token cache wiped")
            return True
        except Exception as e:
            logger.error("OneDrive: failed to wipe token cache: %s", e)
    return False


# ── Status / quota ──────────────────────────────────────────────────────────


async def get_status() -> dict:
    """One-shot status snapshot for the admin UI. Cheap to call frequently —
    no Graph round-trip unless the user is connected (in which case we hit
    /me/drive for quota + display name, with a 4-second timeout)."""
    cache = _load_cache()
    app = _build_app(cache)
    accounts = app.get_accounts()

    # Surface any in-progress device flow.
    flow_info = None
    if _device_flow_state and not _device_flow_state.done and not accounts:
        flow_info = {
            "user_code":        _device_flow_state.flow.get("user_code"),
            "verification_uri": _device_flow_state.flow.get("verification_uri"),
            "expires_in":       max(0, int(
                _device_flow_state.flow.get("expires_in", 0)
                - (time.time() - _device_flow_state.started_at)
            )),
        }

    if not accounts:
        return {
            "configured":     False,
            "client_id_tail": CLIENT_ID[-12:],
            "device_flow":    flow_info,
            "last_error":     (_device_flow_state.error if _device_flow_state else None),
        }

    # Try to get a fresh token + quota info.
    token, _ = _acquire_token_silent()
    quota = None
    drive_owner = None
    if token:
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                r = await client.get(
                    f"{GRAPH_BASE}/me/drive",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if r.status_code == 200:
                    d = r.json()
                    q = d.get("quota") or {}
                    quota = {
                        "total_gb": round(q.get("total", 0) / 1024**3, 1),
                        "used_gb":  round(q.get("used",  0) / 1024**3, 1),
                        "free_gb":  round(q.get("remaining", 0) / 1024**3, 1),
                        "state":    q.get("state"),
                    }
                    drive_owner = ((d.get("owner") or {}).get("user") or {}).get("displayName")
        except Exception as e:
            logger.warning("OneDrive: status probe failed: %s", e)

    return {
        "configured":     True,
        "account":        accounts[0].get("username"),
        "display_name":   drive_owner,
        "client_id_tail": CLIENT_ID[-12:],
        "quota":          quota,
        "token_valid":    bool(token),
        "device_flow":    flow_info,
        "last_error":     (_device_flow_state.error if _device_flow_state else None),
    }


# ── Uploads ─────────────────────────────────────────────────────────────────


def _normalize_remote_path(p: str) -> str:
    """Microsoft Graph expects '/foo/bar/baz.mp4' under /me/drive/root:
    Strip leading slashes, then collapse repeats."""
    parts = [seg for seg in p.replace("\\", "/").split("/") if seg]
    return "/" + "/".join(parts)


async def _check_quota(token: str, file_size: int) -> None:
    """Refuse uploads when OneDrive's free space is less than 2x the file
    size. Cheap pre-flight that prevents partial uploads (Graph returns
    'insufficient storage' mid-session and the upload session goes stale).

    Quota probe failures (network blip, transient 5xx) are non-fatal — we
    log and proceed, since failing closed on every upload because Graph
    occasionally hiccups would be worse than the occasional partial."""
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(
                f"{GRAPH_BASE}/me/drive",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code != 200:
                logger.warning("OneDrive: quota probe HTTP %s — skipping check", r.status_code)
                return
            q = (r.json().get("quota") or {})
            remaining = int(q.get("remaining") or 0)
            if remaining < 2 * file_size:
                raise RuntimeError(
                    f"OneDrive: insufficient free space ({remaining/1024**3:.1f} GB "
                    f"available, need at least {2*file_size/1024**3:.1f} GB headroom)."
                )
    except RuntimeError:
        raise
    except Exception as e:
        logger.warning("OneDrive: quota probe failed (%s) — skipping check", e)


async def upload_file(local_path: str, remote_path: str) -> dict:
    """Upload a single file to OneDrive at `/{remote_path}` (relative to drive root).
    Picks simple PUT for ≤4 MiB, resumable session for larger. Raises on auth
    failure, insufficient quota, or HTTP errors; returns the Graph item dict
    on success."""
    token = await get_valid_access_token()
    if not token:
        raise RuntimeError("OneDrive: not connected (no valid token).")
    src = Path(local_path)
    if not src.exists():
        raise FileNotFoundError(local_path)
    size = src.stat().st_size
    await _check_quota(token, size)
    dest = _normalize_remote_path(remote_path)

    if size <= SIMPLE_UPLOAD_THRESHOLD:
        url = f"{GRAPH_BASE}/me/drive/root:{dest}:/content"
        async with httpx.AsyncClient(timeout=60.0) as client, src.open("rb") as fh:
            r = await client.put(url, content=fh.read(),
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/octet-stream"})
            r.raise_for_status()
            return r.json()

    # Large file: create resumable upload session.
    session_url = f"{GRAPH_BASE}/me/drive/root:{dest}:/createUploadSession"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            session_url,
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        upload_url = r.json()["uploadUrl"]

    # PUT chunks. Each chunk requires a Content-Range header.
    async with httpx.AsyncClient(timeout=120.0) as client, src.open("rb") as fh:
        offset = 0
        last_resp = None
        while offset < size:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            end = offset + len(chunk) - 1
            r = await client.put(
                upload_url,
                content=chunk,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range":  f"bytes {offset}-{end}/{size}",
                },
            )
            # 202 Accepted = more chunks expected; 201/200 = done.
            if r.status_code not in (200, 201, 202):
                # Best-effort cancel the session before bubbling.
                try:
                    async with httpx.AsyncClient(timeout=5.0) as c2:
                        await c2.delete(upload_url)
                except Exception: pass
                r.raise_for_status()
            last_resp = r
            offset = end + 1
        return last_resp.json() if last_resp is not None else {}


async def test_upload() -> dict:
    """Owner-only smoke test: writes a tiny .txt to /SMDL/_test/healthcheck.txt."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(f"sentinel-smdl healthcheck {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n")
        path = fh.name
    try:
        return await upload_file(path, "/SMDL/_test/healthcheck.txt")
    finally:
        try: os.unlink(path)
        except Exception: pass


# ── Auto-upload after Telegram send ─────────────────────────────────────────


def _safe_segment(s: Optional[str], fallback: str = "unknown") -> str:
    """Sanitize a string for use as a OneDrive path segment."""
    if not s:
        return fallback
    bad = '<>:"|?*\x00/\\'
    out = "".join(c for c in s if c not in bad).strip(". ")
    return out[:80] or fallback


async def auto_upload_files(files: list[str], platform: Optional[str],
                             uploader: Optional[str], base_folder: str = "/SMDL",
                             delete_after_upload: bool = False) -> dict:
    """Mirror a batch of just-sent files into OneDrive. Failures on individual
    files don't abort the batch. Returns a summary dict for logging / telegram."""
    sent = []
    failed = []
    total_bytes = 0
    plat = _safe_segment(platform, "other")
    uplo = _safe_segment(uploader, "unknown")
    for f in files:
        try:
            p = Path(f)
            if not p.exists():
                failed.append((f, "missing on disk"))
                continue
            size = p.stat().st_size
            remote = f"{base_folder.rstrip('/')}/{plat}/{uplo}/{p.name}"
            await upload_file(str(p), remote)
            sent.append(p.name)
            total_bytes += size
            if delete_after_upload:
                try:
                    p.unlink()
                except Exception as e:
                    logger.warning("OneDrive: uploaded %s but couldn't delete local: %s", p, e)
        except Exception as e:
            logger.warning("OneDrive: upload %s failed: %s", f, e)
            failed.append((f, str(e)[:120]))
    return {
        "sent_count":  len(sent),
        "failed_count": len(failed),
        "total_bytes": total_bytes,
        "failed":      failed,
    }
