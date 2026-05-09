# Sentinel Mini App v2

A Telegram Mini App dashboard for the Sentinel stack. Provides access to memories, reminders, shortcuts, system status, and OpenClaw controls — secured behind a two-factor authentication flow.

## Authentication Flow

Access requires two independent factors: **Telegram identity** and **TOTP (Google Authenticator)**.

```
Open Mini App
     │
     ▼
[Telegram Identity Check]
  • Inside Telegram app  → initData verified via HMAC-SHA256
  • Browser / web        → Telegram Login Widget OAuth
  • Backend checks: Telegram ID must match owner ID
  • Issues short-lived tg_token (5 min) on success
     │
     ▼
[TOTP Screen]
  • Enter 6-digit code from Google Authenticator
  • Backend verifies against TOTP secret (30s rolling window)
  • tg_token is consumed (one-use only)
  • Issues session token (8 hours) stored in localStorage
     │
     ▼
[Dashboard]
```

## Security Design

| Layer | Mechanism | What it blocks |
|---|---|---|
| Telegram identity | HMAC-SHA256 on `initData` / widget signature | Anyone who isn't you |
| Owner ID check | Telegram user ID compared to `OWNER_ID` in config | Other Telegram users |
| TOTP | Google Authenticator, 30-second rolling code | Someone with your Telegram account |
| Rate limiting | 5 failures per 15-min window → HTTP 429 | TOTP brute force |
| Session tokens | 8-hour expiry, revocable per-device | Stolen/stale sessions |
| SENTINEL_TOKEN | Injected server-side into HTML, never in static files | Token leakage via source |
| TOTP setup | Local-only `totp_setup.html` generated at startup, never served over web | QR code theft |

## Architecture

```
your-domain.example.com  (Cloudflare Tunnel)
          │
          ▼
   bridge.py  :8098  (Flask)
          │
          ├── GET  /              → injects SENTINEL_TOKEN + BOT_USERNAME into HTML
          ├── POST /api/auth/telegram  → verify Telegram identity, issue tg_token
          ├── POST /api/auth/verify    → verify TOTP, issue session token
          ├── GET  /api/auth/status    → validate session liveness
          ├── GET  /api/auth/sessions  → list active sessions
          ├── DEL  /api/auth/sessions  → revoke session(s)
          └── (all other /api/* routes require valid session token)
```

## Running

```bash
cd sentinel-miniapp-v2
py bridge.py
```

Starts on `http://localhost:8098`. Cloudflare Tunnel forwards `your-domain.example.com` to this port.

On startup, `totp_setup.html` is generated locally in this directory with the QR code and TOTP secret. Open it once in a browser to add to Google Authenticator. It is gitignored and never served over the web.

## Configuration

All secrets live in `config.json` at the repo root (gitignored). Required fields:

```json
{
  "telegram_bot_token": "...",
  "telegram_chat_ids": { "dm": "<your_telegram_id>" },
  "mini_app_url": "https://your-domain.example.com",
  "mini_app_secret": "<random hex, injected into page>",
  "totp_secret": "<base32 secret for Google Authenticator>"
}
```

Generate a new TOTP secret:
```python
import pyotp
print(pyotp.random_base32())
```

## vs v1

| | v1 (port 8097) | v2 (port 8098) |
|---|---|---|
| Auth | TOTP only | Telegram identity + TOTP |
| TOTP setup | Web endpoint (removed as security risk) | Local-only HTML file |
| Rate limiting | Partial | Full (both auth endpoints) |
| Session store | In-memory | In-memory |
| Telegram verification | None | HMAC-SHA256 initData + widget |
| Browser access | Anyone with URL + TOTP | Telegram ID gated |
