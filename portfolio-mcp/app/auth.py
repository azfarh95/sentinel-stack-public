"""Telegram Login Widget + TOTP + session middleware for Sentinel Finance.

Auth model:
  Identity = Telegram user_id (HMAC-verified via bot token, no third-party IdP).
  Bootstrap admin = OWNER_CHAT_ID env var (your Telegram user_id).
  Other users → pending → admin approves at /admin/users → TOTP setup → access.

Flow:
  1. /auth/login                  → renders Telegram Login Widget
  2. /auth/telegram/callback      → receives ?id=...&hash=... from widget,
                                    verifies HMAC, finds/creates User row
  3. /auth/totp/setup             → QR + verify (binds secret to user)
  4. /auth/totp/challenge         → 6-digit code per session
  5. /auth/logout                 → drop session

Setup outside this code (one-time):
  In @BotFather chat: /setdomain → YourSentinelBot → sentinelfinance.your-domain.example.com
  Without that, the widget refuses to load.
"""
import os
import io
import hmac
import hashlib
import base64
import secrets
import logging
from datetime import datetime, timedelta, timezone

import pyotp
import qrcode
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from starlette.requests import Request
from starlette.responses import RedirectResponse, HTMLResponse, PlainTextResponse, JSONResponse

from . import database as db

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "YourSentinelBot")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
PUBLIC_URL = os.environ.get("PWA_HOST_OVERRIDE", "https://sentinelfinance.your-domain.example.com").rstrip("/")
OWNER_CHAT_ID = int(os.environ.get("OWNER_CHAT_ID", "0") or "0")
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "30"))
TG_AUTH_MAX_AGE_SECONDS = 86400      # widget auth payload TTL
COOKIE_NAME = "sentinel_session"

_signer = URLSafeTimedSerializer(SESSION_SECRET or "dev-secret-do-not-use", salt="sentinel-session")


class AuthRedirect(Exception):
    def __init__(self, response):
        self.response = response


# ── Telegram HMAC verification ───────────────────────────────────────────────

def verify_telegram_login(data: dict, bot_token: str) -> bool:
    """Verify the HMAC the Telegram Login Widget sends back.

    https://core.telegram.org/widgets/login#checking-authorization
    Algorithm:
      secret_key = SHA256(bot_token)
      data_check_string = "key=value\nkey=value..." sorted by key, hash excluded
      hmac_hex = HMAC-SHA256(secret_key, data_check_string).hexdigest()
      Compare to data['hash'].
    """
    received_hash = data.get("hash", "")
    if not received_hash or not bot_token:
        return False
    pairs = [(k, str(v)) for k, v in data.items() if k != "hash"]
    pairs.sort(key=lambda kv: kv[0])
    check_str = "\n".join(f"{k}={v}" for k, v in pairs)
    secret = hashlib.sha256(bot_token.encode()).digest()
    computed = hmac.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, received_hash)


# ── Session helpers ──────────────────────────────────────────────────────────

def _signed(session_id: str) -> str:
    return _signer.dumps(session_id)


def _unsign(value: str) -> str | None:
    try:
        return _signer.loads(value, max_age=SESSION_TTL_DAYS * 86400)
    except (BadSignature, SignatureExpired):
        return None


def _set_session_cookie(response, session_id: str):
    response.set_cookie(
        COOKIE_NAME, _signed(session_id),
        max_age=SESSION_TTL_DAYS * 86400,
        httponly=True, secure=True, samesite="lax", path="/",
    )


def _clear_session_cookie(response):
    response.delete_cookie(COOKIE_NAME, path="/")


def _new_session_id() -> str:
    return secrets.token_urlsafe(32)


def _get_session_from_request(req: Request):
    raw = req.cookies.get(COOKIE_NAME)
    if not raw:
        return None, None
    sid = _unsign(raw)
    if not sid:
        return None, None
    s = db.SessionLocal()
    try:
        sess = s.get(db.Session, sid)
        if not sess:
            return None, None
        if sess.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
            s.delete(sess); s.commit()
            return None, None
        user = s.get(db.User, sess.user_id)
        return sess, user
    finally:
        s.close()


def require_user(req: Request, need_totp: bool = True):
    sess, user = _get_session_from_request(req)
    if not user:
        raise AuthRedirect(RedirectResponse("/auth/login", status_code=302))
    if user.status == "pending":
        raise AuthRedirect(RedirectResponse("/auth/pending", status_code=302))
    if user.status in ("suspended", "denied"):
        raise AuthRedirect(RedirectResponse("/auth/denied", status_code=302))
    if user.status != "active":
        raise AuthRedirect(RedirectResponse("/auth/login", status_code=302))
    if not user.totp_enabled_at:
        raise AuthRedirect(RedirectResponse("/auth/totp/setup", status_code=302))
    if need_totp and not sess.totp_verified:
        raise AuthRedirect(RedirectResponse("/auth/totp/challenge", status_code=302))
    return user


def require_admin(req: Request):
    user = require_user(req)
    if user.role != "admin":
        raise AuthRedirect(RedirectResponse("/auth/denied", status_code=302))
    return user


# ── Routes ───────────────────────────────────────────────────────────────────

async def login_page(req: Request):
    return HTMLResponse(_render_login_page())


async def telegram_callback(req: Request):
    """Telegram Login Widget redirects here with auth payload in query string."""
    if not BOT_TOKEN:
        return _err("TELEGRAM_BOT_TOKEN not configured.", 500)
    data = dict(req.query_params)
    if not verify_telegram_login(data, BOT_TOKEN):
        return _err("Telegram authentication signature invalid.", 401)

    # Freshness check
    auth_date = int(data.get("auth_date", "0"))
    now = int(datetime.now(timezone.utc).timestamp())
    if now - auth_date > TG_AUTH_MAX_AGE_SECONDS:
        return _err("Login payload expired. Try again.", 401)

    tg_user_id = int(data["id"])
    tg_username = data.get("username") or None
    first_name = data.get("first_name") or ""
    last_name = data.get("last_name") or ""
    full_name = (first_name + " " + last_name).strip() or tg_username or str(tg_user_id)
    photo_url = data.get("photo_url") or None

    now_dt = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)

    s = db.SessionLocal()
    try:
        user = s.query(db.User).filter(db.User.telegram_user_id == tg_user_id).first()
        if user is None:
            is_owner = (OWNER_CHAT_ID and tg_user_id == OWNER_CHAT_ID)
            user = db.User(
                telegram_user_id=tg_user_id,
                telegram_username=tg_username,
                name=full_name,
                picture_url=photo_url,
                role="admin" if is_owner else "member",
                status="active" if is_owner else "pending",
                created_at=now_dt,
                approved_at=now_dt if is_owner else None,
            )
            s.add(user); s.commit(); s.refresh(user)
        else:
            user.telegram_username = tg_username
            user.name = full_name
            user.picture_url = photo_url
            user.last_login_at = now_dt
            s.commit()

        sid = _new_session_id()
        sess = db.Session(
            id=sid, user_id=user.id,
            expires_at=now_dt + timedelta(days=SESSION_TTL_DAYS),
            created_at=now_dt,
            ip=req.client.host if req.client else None,
            user_agent=req.headers.get("user-agent","")[:300],
            totp_verified=0,
        )
        s.add(sess); s.commit()

        # Capture attributes for redirect decision before the session closes
        user_status = user.status
        user_totp_enabled = bool(user.totp_enabled_at)
    finally:
        s.close()

    if user_status == "pending":
        target = "/auth/pending"
    elif user_status in ("suspended", "denied"):
        target = "/auth/denied"
    elif not user_totp_enabled:
        target = "/auth/totp/setup"
    else:
        target = "/auth/totp/challenge"
    resp = RedirectResponse(target, status_code=302)
    _set_session_cookie(resp, sid)
    return resp


async def logout(req: Request):
    sess, _ = _get_session_from_request(req)
    if sess:
        s = db.SessionLocal()
        try:
            row = s.get(db.Session, sess.id)
            if row: s.delete(row); s.commit()
        finally:
            s.close()
    resp = RedirectResponse("/auth/login", status_code=302)
    _clear_session_cookie(resp)
    return resp


async def totp_setup(req: Request):
    sess, user = _get_session_from_request(req)
    if not user or user.status != "active":
        return RedirectResponse("/auth/login", status_code=302)

    if req.method == "GET":
        s = db.SessionLocal()
        try:
            u = s.get(db.User, user.id)
            if not u.totp_secret:
                u.totp_secret = pyotp.random_base32()
                s.commit()
            account = u.telegram_username or f"tg:{u.telegram_user_id}"
            otpauth = pyotp.totp.TOTP(u.totp_secret).provisioning_uri(name=account, issuer_name="Sentinel Finance")
        finally:
            s.close()
        qr = _qr_png(otpauth)
        b64 = base64.b64encode(qr).decode()
        return HTMLResponse(_render_totp_setup_page(b64, account))

    form = await req.form()
    code = (form.get("code") or "").strip()
    s = db.SessionLocal()
    try:
        u = s.get(db.User, user.id)
        if not u.totp_secret:
            return RedirectResponse("/auth/totp/setup", status_code=302)
        if not pyotp.TOTP(u.totp_secret).verify(code, valid_window=1):
            return HTMLResponse(_render_totp_setup_page("", u.telegram_username or str(u.telegram_user_id),
                error="Invalid code. Try again with a fresh number from your authenticator."), status_code=400)
        u.totp_enabled_at = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
        sess_row = s.get(db.Session, sess.id)
        sess_row.totp_verified = 1
        s.commit()
    finally:
        s.close()
    return RedirectResponse("/balance_sheet", status_code=302)


async def totp_challenge(req: Request):
    sess, user = _get_session_from_request(req)
    if not user or user.status != "active" or not user.totp_enabled_at:
        return RedirectResponse("/auth/login", status_code=302)

    if req.method == "GET":
        return HTMLResponse(_render_totp_challenge_page(user.telegram_username or str(user.telegram_user_id)))

    form = await req.form()
    code = (form.get("code") or "").strip()
    if not pyotp.TOTP(user.totp_secret).verify(code, valid_window=1):
        return HTMLResponse(_render_totp_challenge_page(user.telegram_username or str(user.telegram_user_id),
            error="Wrong code. Try the current 6-digit number from your authenticator."), status_code=400)
    s = db.SessionLocal()
    try:
        sess_row = s.get(db.Session, sess.id)
        sess_row.totp_verified = 1
        s.commit()
    finally:
        s.close()
    return RedirectResponse("/balance_sheet", status_code=302)


async def pending_page(req: Request):
    sess, user = _get_session_from_request(req)
    who = user.telegram_username if user and user.telegram_username else (f"@{user.telegram_user_id}" if user else "your account")
    return HTMLResponse(_render_simple_page(
        "Awaiting approval",
        f"<p>{who} has been registered.</p>"
        f"<p>An administrator needs to approve access before you can see the dashboard.</p>"
        f'<p><a href="/auth/logout">Sign out</a></p>'
    ))


async def denied_page(req: Request):
    return HTMLResponse(_render_simple_page(
        "Access denied",
        '<p>Your account is not authorised for this app.</p>'
        '<p><a href="/auth/logout">Sign out</a></p>'
    ), status_code=403)


# ── Admin ────────────────────────────────────────────────────────────────────

async def admin_users(req: Request):
    try:
        admin = require_admin(req)
    except AuthRedirect as e:
        return e.response
    s = db.SessionLocal()
    try:
        users = s.query(db.User).order_by(db.User.created_at.desc()).all()
    finally:
        s.close()
    rows = []
    for u in users:
        who = u.telegram_username or f"id:{u.telegram_user_id}"
        actions = ""
        if u.status == "pending":
            actions = (
                f'<form method="post" action="/admin/users/{u.id}/approve" style="display:inline">'
                f'<button>Approve</button></form> '
                f'<form method="post" action="/admin/users/{u.id}/deny" style="display:inline">'
                f'<button>Deny</button></form>'
            )
        elif u.status == "active" and u.id != admin.id:
            actions = (
                f'<form method="post" action="/admin/users/{u.id}/suspend" style="display:inline">'
                f'<button>Suspend</button></form>'
            )
        elif u.status == "suspended":
            actions = (
                f'<form method="post" action="/admin/users/{u.id}/approve" style="display:inline">'
                f'<button>Re-activate</button></form>'
            )
        totp = "✓" if u.totp_enabled_at else "—"
        rows.append(
            f"<tr><td>{who}</td><td>{u.role}</td><td>{u.status}</td><td>{totp}</td>"
            f"<td>{(u.last_login_at or '—')}</td><td>{actions}</td></tr>"
        )
    body = f"""
    <h1>Sentinel Finance — Users</h1>
    <p>Signed in as <b>{admin.telegram_username or admin.telegram_user_id}</b> (admin) ·
       <a href="/balance_sheet">Dashboard</a> · <a href="/auth/logout">Sign out</a></p>
    <table>
      <thead><tr><th>Telegram</th><th>Role</th><th>Status</th><th>TOTP</th><th>Last login</th><th>Actions</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """
    return HTMLResponse(_render_admin_layout(body))


async def admin_user_action(req: Request, action: str):
    try:
        admin = require_admin(req)
    except AuthRedirect as e:
        return e.response
    uid = int(req.path_params["uid"])
    s = db.SessionLocal()
    try:
        u = s.get(db.User, uid)
        if not u:
            return PlainTextResponse("Not found", status_code=404)
        if u.id == admin.id:
            return PlainTextResponse("Refusing to modify your own admin account.", status_code=400)
        now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
        if action == "approve":
            u.status = "active"; u.approved_at = now; u.approved_by_id = admin.id
        elif action == "deny":
            u.status = "denied"
        elif action == "suspend":
            u.status = "suspended"
        else:
            return PlainTextResponse("Unknown action", status_code=400)
        s.commit()
    finally:
        s.close()
    return RedirectResponse("/admin/users", status_code=303)


# ── Page rendering ───────────────────────────────────────────────────────────

_BASE_CSS = """
:root { --bg:#1c1c1e; --fg:#f0f0f0; --muted:#8e8e93; --accent:#4cd964; --sep:rgba(255,255,255,0.10); --err:#ff3b30; }
* { box-sizing: border-box; }
body { margin:0; padding:32px 20px; background:var(--bg); color:var(--fg);
  font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  max-width: 520px; margin-left:auto; margin-right:auto; }
h1 { margin: 0 0 16px; font-size: 22px; }
p { margin: 0 0 12px; }
.muted { color: var(--muted); font-size: 12px; }
.err { color: var(--err); margin-top: 8px; }
form { margin-top: 16px; }
input[type=text], input[type=password] { width: 100%; padding: 10px 12px; font-size: 16px;
  background: #2c2c2e; color: var(--fg); border: 1px solid var(--sep); border-radius: 8px; margin-bottom: 12px;
  font-variant-numeric: tabular-nums; letter-spacing: 4px; text-align: center; }
button, .btn { display: inline-block; padding: 10px 16px; font-size: 14px; font-weight: 600;
  background: var(--accent); color: #000; border: none; border-radius: 8px; cursor: pointer; text-decoration: none; }
.btn-secondary { background: transparent; color: var(--accent); border: 1px solid var(--accent); }
a { color: var(--accent); }
table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 12px; }
th, td { padding: 6px 8px; text-align: left; border-bottom: 1px solid var(--sep); }
th { color: var(--muted); font-weight: 600; }
.qr { background:#fff; padding:16px; border-radius:12px; display:inline-block; margin:12px 0; }
.qr img { display:block; width: 220px; height: 220px; }
.tg-widget-wrap { margin: 24px 0; display: flex; justify-content: center; }
"""


def _layout(title: str, body: str) -> str:
    return (
        f'<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        f'<title>{title} — Sentinel Finance</title>'
        f'<meta name="theme-color" content="#1c1c1e">'
        f'<style>{_BASE_CSS}</style></head><body>{body}</body></html>'
    )


def _render_simple_page(title: str, html_body: str) -> str:
    return _layout(title, f"<h1>{title}</h1>{html_body}")


def _err(msg: str, status: int = 400):
    return HTMLResponse(_render_simple_page("Error", f"<p class='err'>{msg}</p>"
        f'<p><a href="/auth/login" class="btn-secondary btn">Back to sign in</a></p>'), status_code=status)


def _render_login_page(error: str = ""):
    err = f'<p class="err">{error}</p>' if error else ""
    auth_url = f"{PUBLIC_URL}/auth/telegram/callback"
    widget = (
        f'<script async src="https://telegram.org/js/telegram-widget.js?22" '
        f'data-telegram-login="{BOT_USERNAME}" '
        f'data-size="large" '
        f'data-userpic="false" '
        f'data-auth-url="{auth_url}" '
        f'data-request-access="write"></script>'
    )
    return _layout("Sign in", f"""
        <h1>Sentinel Finance</h1>
        <p class="muted">Sign in with the Telegram account you use for @{BOT_USERNAME}.</p>
        {err}
        <div class="tg-widget-wrap">{widget}</div>
        <p class="muted" style="margin-top:24px;">New users require admin approval before access is granted.</p>
    """)


def _render_totp_setup_page(qr_b64: str, who: str, error: str = ""):
    err = f'<p class="err">{error}</p>' if error else ""
    qr_block = f'<div class="qr"><img src="data:image/png;base64,{qr_b64}"></div>' if qr_b64 else ""
    return _layout("Set up 2FA", f"""
        <h1>Enable two-factor authentication</h1>
        <p class="muted">Signed in as <b>{who}</b>. Scan with Google Authenticator, Authy, or 1Password, then enter the 6-digit code.</p>
        {qr_block}
        {err}
        <form method="post">
          <input type="text" name="code" placeholder="123 456" inputmode="numeric" pattern="[0-9 ]*" autofocus required>
          <button type="submit">Verify & enable</button>
        </form>
        <p class="muted" style="margin-top:24px;"><a href="/auth/logout">Sign out</a></p>
    """)


def _render_totp_challenge_page(who: str, error: str = ""):
    err = f'<p class="err">{error}</p>' if error else ""
    return _layout("2FA", f"""
        <h1>Two-factor code</h1>
        <p class="muted">Signed in as <b>{who}</b>. Open your authenticator app and enter the current 6-digit code.</p>
        {err}
        <form method="post">
          <input type="text" name="code" placeholder="123 456" inputmode="numeric" pattern="[0-9 ]*" autofocus required>
          <button type="submit">Verify</button>
        </form>
        <p class="muted" style="margin-top:24px;"><a href="/auth/logout">Sign out</a></p>
    """)


def _render_admin_layout(body: str) -> str:
    return _layout("Admin", body)


def _qr_png(text: str) -> bytes:
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
    qr.add_data(text); qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO(); img.save(buf, format="PNG"); return buf.getvalue()
