import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

CREDENTIALS_FILE = os.environ.get("CREDENTIALS_FILE", "/data/credentials.json")
TOKEN_FILE = os.environ.get("TOKEN_FILE", "/data/token.json")
REDIRECT_URI = os.environ.get("REDIRECT_URI", "http://localhost:8089/oauth/callback")

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
]


def get_credentials() -> Credentials | None:
    if not Path(TOKEN_FILE).exists():
        return None
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(creds)
        else:
            return None
    return creds


# Single-user server — cache the flow so the code verifier survives the redirect
_pending_flow: Flow | None = None


def start_flow() -> str:
    """Create a flow, cache it, and return the Google authorization URL."""
    global _pending_flow
    _pending_flow = Flow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    url, _ = _pending_flow.authorization_url(prompt="consent", access_type="offline")
    return url


def exchange_code(code: str) -> Credentials:
    global _pending_flow
    if _pending_flow is None:
        raise RuntimeError("No pending OAuth flow. Please visit /oauth first to start authorization.")
    flow = _pending_flow
    _pending_flow = None
    flow.fetch_token(code=code)
    _save_token(flow.credentials)
    return flow.credentials


def _save_token(creds: Credentials):
    Path(TOKEN_FILE).write_text(creds.to_json())
