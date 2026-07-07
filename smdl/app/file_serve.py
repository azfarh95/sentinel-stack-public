"""HTTP file delivery for SMDL.

Two paths to retrieve recorded files, neither using the bot API's 50 MB cap:

  Path 2 — tailnet-only:
    GET /m/<filename>
    Source IP must be in 100.64.0.0/10 (Tailscale CGNAT range).
    Use case: own devices already on the mesh.

  Path 1 — public signed URL:
    GET /share/<token>/<filename>
    token = base64url(<expiry-epoch>:HMAC-SHA256(filename + ':' + expiry, share_secret))
    Default validity 24h. Token revocable by rotating SMDL_SHARE_SECRET.
    Use case: sharing with anyone, behind Cloudflare Tunnel.

Bot uses helper sign_share_url() to build the share URL after a recording.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)
router = APIRouter()

# Roots SMDL is allowed to serve from. Path-traversal-safe: every request
# must resolve to a file UNDER one of these roots, else 404.
DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", "/downloads")).resolve()
ALLOWED_ROOTS = [
    DOWNLOADS_DIR,
    DOWNLOADS_DIR / "live",
]

SHARE_SECRET = os.environ.get("SMDL_SHARE_SECRET", "")
SHARE_DEFAULT_TTL_SEC = 24 * 3600  # 24h

# Public hostname used for /share URLs in bot replies. Phase-2 dependent.
PUBLIC_BASE_URL = os.environ.get("SMDL_PUBLIC_BASE_URL", "")  # e.g. https://media.your-domain.example.com

# Tailscale CGNAT range — devices on a tailnet always get IPs in this block.
_TAILNET_NET = ipaddress.ip_network("100.64.0.0/10")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_safe(filename: str) -> Path | None:
    """Return resolved path if it lives under one of ALLOWED_ROOTS, else None.

    Defends against path-traversal (../) and symlinks pointing outside roots.
    `filename` may include subpath like 'live/twitch:stream/foo/bar.mp4'.
    """
    candidate = (DOWNLOADS_DIR / filename).resolve()
    for root in ALLOWED_ROOTS:
        try:
            candidate.relative_to(root)
            return candidate if candidate.is_file() else None
        except ValueError:
            continue
    return None


def _is_tailnet_source(client_ip: str) -> bool:
    try:
        return ipaddress.ip_address(client_ip) in _TAILNET_NET
    except ValueError:
        return False


def _is_tailscale_serve_proxy(client_ip: str, headers) -> bool:
    """When `tailscale serve --https=PORT TARGET` proxies a tailnet peer to
    our backend, the source IP at our app is 127.0.0.1 (same-host loopback)
    BUT Tailscale injects identity headers like `Tailscale-User-Login`,
    `Tailscale-User-Display-Name`, `Tailscale-User-Profile-Pic`. CF Tunnel
    and host-loopback access don't set those headers — clean discriminator.
    """
    if client_ip not in ("127.0.0.1", "::1"):
        return False
    # Any of these headers means tailscale serve forwarded a tailnet request
    for h in ("tailscale-user-login", "tailscale-user-display-name",
              "tailscale-user-profile-pic"):
        if headers.get(h):
            return True
    return False


def sign_share_url(filename: str, ttl_sec: int = SHARE_DEFAULT_TTL_SEC) -> str | None:
    """Generate a public share URL for a file. Returns full URL or None if not configured."""
    if not SHARE_SECRET or not PUBLIC_BASE_URL:
        return None
    expiry = int(time.time()) + ttl_sec
    rel = str(Path(filename))  # normalize separators
    sig = hmac.new(
        SHARE_SECRET.encode("utf-8"),
        f"{rel}:{expiry}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    token_raw = f"{expiry}:".encode("utf-8") + sig
    token = base64.urlsafe_b64encode(token_raw).decode("ascii").rstrip("=")
    encoded_path = quote(rel)
    return f"{PUBLIC_BASE_URL.rstrip('/')}/share/{token}/{encoded_path}"


def _verify_share_token(token: str, filename: str) -> bool:
    if not SHARE_SECRET:
        return False
    try:
        # restore base64 padding
        pad = "=" * ((4 - len(token) % 4) % 4)
        decoded = base64.urlsafe_b64decode(token + pad)
        # split: <expiry-ascii> ':' <32-byte-sig>
        sep_idx = decoded.find(b":")
        if sep_idx < 0:
            return False
        expiry_str = decoded[:sep_idx].decode("ascii")
        sig = decoded[sep_idx + 1:]
        expiry = int(expiry_str)
        if time.time() > expiry:
            return False
        rel = str(Path(filename))
        expected = hmac.new(
            SHARE_SECRET.encode("utf-8"),
            f"{rel}:{expiry}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return hmac.compare_digest(expected, sig)
    except Exception:
        return False


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/m/{filepath:path}")
async def serve_tailnet(filepath: str, request: Request):
    """Tailnet-only file serve. Accept either:
       (a) source IP in 100.64.0.0/10 (direct tailnet), or
       (b) source 127.0.0.1 + Tailscale-User-* header (request came through
           `tailscale serve` which loopback-forwards from a tailnet peer).
    """
    client_ip = request.client.host if request.client else ""
    direct_tailnet = _is_tailnet_source(client_ip)
    via_serve     = _is_tailscale_serve_proxy(client_ip, request.headers)
    if not (direct_tailnet or via_serve):
        logger.info("/m/ refused: client_ip=%s, no Tailscale-User header", client_ip)
        raise HTTPException(status_code=403, detail="Tailnet-only endpoint")

    resolved = _resolve_safe(filepath)
    if not resolved:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(resolved, filename=resolved.name)


@router.get("/share/{token}/{filepath:path}")
async def serve_share(token: str, filepath: str):
    """Public signed-URL file serve. Token = base64url(expiry:hmac)."""
    if not _verify_share_token(token, filepath):
        raise HTTPException(status_code=403, detail="Invalid or expired share token")
    resolved = _resolve_safe(filepath)
    if not resolved:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(resolved, filename=resolved.name)
