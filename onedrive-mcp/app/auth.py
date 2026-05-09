import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

CLIENT_ID = "7a5bc9cb-f598-4461-a6c0-4942544582df"
CLIENT_SECRET = os.environ["ONEDRIVE_CLIENT_SECRET"]
TENANT = "consumers"
REDIRECT_URI = "http://localhost:8093/oauth/callback"
SCOPES = "Files.Read Files.Read.All offline_access User.Read"
TOKEN_FILE = Path("/data/token.json")

AUTH_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"

# In-memory state for CSRF protection: {state: True}
_states: set[str] = set()


def get_auth_url() -> str:
    state = secrets.token_urlsafe(16)
    _states.add(state)
    params = urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
    })
    return f"{AUTH_URL}?{params}"


def load_token() -> Optional[dict]:
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text())
        except Exception:
            return None
    return None


def save_token(data: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(data))


def is_authenticated() -> bool:
    return TOKEN_FILE.exists()


async def exchange_code(code: str, state: str) -> dict:
    if state not in _states:
        raise ValueError("Invalid or expired OAuth state — try /auth again")
    _states.discard(state)
    async with httpx.AsyncClient() as client:
        r = await client.post(TOKEN_URL, data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        })
        if r.status_code != 200:
            raise RuntimeError(f"Token exchange failed ({r.status_code}): {r.text}")
        data = r.json()
        data["expires_at"] = time.time() + data.get("expires_in", 3600)
        save_token(data)
        return data


async def _refresh(refresh_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.post(TOKEN_URL, data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": SCOPES,
        })
        if r.status_code != 200:
            raise RuntimeError(f"Token refresh failed ({r.status_code}): {r.text}")
        data = r.json()
        data["expires_at"] = time.time() + data.get("expires_in", 3600)
        save_token(data)
        return data


async def get_access_token() -> str:
    token = load_token()
    if not token:
        raise RuntimeError("OneDrive not authenticated. Visit http://localhost:8093/auth in your browser.")
    if time.time() > token.get("expires_at", 0) - 60:
        token = await _refresh(token["refresh_token"])
    return token["access_token"]
