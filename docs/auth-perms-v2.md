# Sentinel auth-perms v2 — design

**Status**: draft (Phase 0 — design doc, no code yet)
**Owner**: azfar
**Last updated**: 2026-05-27

This is the lock-in document for the v2 authentication and per-scope
permission system across the Sentinel stack. Future implementation
phases (Phase 1, 2, 3) must conform to the contracts defined here.
Any deviation requires editing this document and a comment on the PR
explaining why.

---

## 1. Goals

1. **Beta-user gating** — issue per-user keys that grant access to a
   curated subset of pillars / features, without compromising the
   owner-only gate that protects everything else.
2. **No new infrastructure pieces** — runs inside the existing
   `sentinel-vpn-dashboard` container (the Suite launcher at
   `suite.your-domain.example.com`). No new service to deploy.
3. **Backwards-compatible rollout** — owner cookie v1 keeps working
   throughout the rollout. Each pillar can adopt v2 enforcement on
   its own schedule without coordinated deploys.
4. **Stateless verification at the pillar edge** — HMAC-signed cookie
   carries the full claim set; pillars don't have to call the Suite
   to check permissions per request. Revocation handled out-of-band
   with a small revocation list lookup (Phase 2+).
5. **Audit-ready** — every cookie issuance, redemption, and
   revocation logged with timestamp, actor, scopes (Phase 2+).

## 2. Non-goals

- **Public sign-up / self-service registration** — every user is
  invited by the owner. No registration page.
- **OAuth / SSO** — overkill for the scale (≤50 users projected).
- **Per-pillar credentials** — one cookie covers all pillars; scope
  determines which subsets of each are reachable.
- **Multi-tenant isolation** — single tenant (azfar's stack), per-
  user scoping. Data isolation between users is not a v2 concern.

## 3. Architecture overview

```
┌─ suite.your-domain.example.com (Suite launcher) ────────────────────┐
│  • Mints scoped cookies                                      │
│  • Hosts /admin/users CRUD UI (Phase 2)                      │
│  • Stores users + invites + revocations in SQLite (Phase 2)  │
│  • Logs auth_events (Phase 2)                                │
└──────────────────────────────────────────────────────────────┘
                    │ Domain-wide cookie on .your-domain.example.com
                    ▼
┌─ each pillar (SMDL, Finance, AI, Gaming, Watchdog) ─────────┐
│  • Reads the cookie via existing _verify()                   │
│  • Parses scopes from the v2 payload                         │
│  • Enforces require_scope("…") at the route layer            │
│  • Owner cookie v1 still recognised → all scopes implicit    │
└──────────────────────────────────────────────────────────────┘
```

No cross-service RPC. Each pillar holds the same HMAC secret
(`OWNER_AUTH_TOKEN` from `.env.local`) and can verify cookies
locally. That's the same secret that signs the current v1 cookie.

## 4. Cookie format

### 4.1. v1 (current, owner-only)

```
<unix_ts>.<nonce>.<hmac_hex>

  unix_ts   — issuance time, seconds
  nonce     — 16 bytes of random, url-safe base64
  hmac_hex  — HMAC-SHA256(OWNER_AUTH_TOKEN, "<unix_ts>.<nonce>")
              hex-encoded (64 chars)
```

Validates: signature matches AND `(now - unix_ts) < 90 days`.

### 4.2. v2 (new, scoped)

```
v2.<unix_ts>.<user_id>.<jti>.<scopes_b64>.<hmac_hex>

  v2         — literal string, dot-separated
  unix_ts    — issuance time, seconds
  user_id    — owner-assigned identifier (slug, max 32 ASCII chars,
               [a-z0-9_-]+)
  jti        — random per-cookie id, 16 bytes url-safe base64
               (used for revocation lookup in Phase 2)
  scopes_b64 — url-safe base64 of compact JSON like
               '["smdl.iptv","apps.install"]'.
               Max 512 bytes raw, max ~700 chars b64.
  hmac_hex   — HMAC-SHA256 of "v2.<unix_ts>.<user_id>.<jti>.<scopes_b64>"
               hex-encoded.
```

Validates:
- Starts with literal `v2.` → use v2 parser; else fall through to
  v1 parser.
- Signature matches the same `OWNER_AUTH_TOKEN`.
- `(now - unix_ts) < <expiry_seconds>`. Default expiry **90 days**
  to match v1; per-user override planned for Phase 2 (`exp_seconds`
  field in users table).
- (Phase 2+) `jti` is NOT in the revocation list.

### 4.3. Migration

- Owner cookie stays v1. New beta-user cookies are minted v2.
- Each pillar's `_verify()` tries v2 first (cheap regex check on
  `v2.` prefix), falls back to v1.
- v1 cookies implicitly grant scope `*` (wildcard, all-access). Owner
  retains current behaviour.
- No flag day. Each pillar adopts v2 enforcement at its own pace by
  starting to call `require_scope(...)` on its routes. Until it does,
  v2 cookies behave like v1 (allowed everywhere their HMAC verifies).

### 4.4. Why this format

- **Dot-separated literals**: human-debuggable in browser devtools,
  no library needed to decode (compared to JWT). Performance: ~3μs
  to verify with stdlib `hmac.compare_digest`.
- **No JSON in clear**: scopes are b64'd not because they're secret
  (the HMAC isn't an encryption) but because dots and quotes in
  scope names would break the format. Reverse-able trivially.
- **Same secret as v1**: zero-key-rotation rollout. If the secret ever
  rotates, ALL cookies (v1 + v2) invalidate together — already the
  case today.
- **Versioned prefix**: forward-compatible. v3 (e.g. signed groups,
  device binding) starts with `v3.` and parsers route accordingly.

## 5. Scope catalogue

Source of truth: `metamcp-local/sentinel-vpn-dashboard/data/scopes.yaml`,
committed to the `azfarh95/sentinel-stack-public` repo.

Schema:
```yaml
scopes:
  <scope-id>:
    owner:  <pillar-name>     # which pillar enforces this scope
    label:  <human-readable>
    routes: [<glob>, ...]     # informational — which routes need it
    notes:  <optional>
```

### 5.1. Initial catalogue (Phase 1)

```yaml
# Wildcards — special, not in the table; granted to owner and via
# explicit admin assignment only.
# "*"           — implicit all-scopes (owner default)
# "pillar.*"    — all scopes inside one pillar

scopes:

  smdl.iptv:
    owner:  smdl
    label:  IPTV browser, EPG, recording
    routes: [/iptv, /iptv/*, /api/iptv/*]

  smdl.downloader:
    owner:  smdl
    label:  yt-dlp /dl flow + URL cache
    routes: [/dl, /api/dl/*]

  smdl.streamtracker:
    owner:  smdl
    label:  Watchlist add/remove + live recording
    routes: [/watchlist, /api/watchlist/*, /api/miniapp/watchlist*]

  smdl.stickers:
    owner:  smdl
    label:  Sticker maker — videos → Telegram packs
    routes: [/stickers, /api/sticker_drafts/*]

  smdl.admin:
    owner:  smdl
    label:  SMDL admin tab (site blocklist, user mgmt)
    routes: [/api/miniapp/admin/*]

  finance.view:
    owner:  finance
    label:  Finance dashboards read-only
    routes: [/dashboard, /report/*, /api/*  (GET only)]

  finance.write:
    owner:  finance
    label:  Ledger edits, reconciliation, journals
    routes: [/api/*  (POST/PATCH/DELETE)]

  ai.chat:
    owner:  sentinel-ai
    label:  Sentinel AI conversations
    routes: [/chat, /api/brain/*]

  ai.admin:
    owner:  sentinel-ai
    label:  Model selection, secrets, tool overrides
    routes: [/api/brain/admin/*]

  gaming.play:
    owner:  gaming
    label:  Connect to game servers
    routes: [/api/games/*]

  gaming.host:
    owner:  gaming
    label:  Start/stop game-server containers
    routes: [/api/games/admin/*]

  network.view:
    owner:  suite
    label:  VPN dashboard read
    routes: [/vpn, /api/cf, /api/all, /api/whereami]

  network.admin:
    owner:  suite
    label:  Exit node toggles, peer config
    routes: [/api/vpn/admin/*]

  apps.install:
    owner:  suite
    label:  Sideload Apps store access (read + download)
    routes: [/apps, /api/apps, /apps/*]

  watchdog.view:
    owner:  watchdog
    label:  Service health dashboards
    routes: [/miniapp, /api/v2/health, /api/v2/all]

  watchdog.restart:
    owner:  watchdog
    label:  Trigger restarts on specific services
    routes: [/api/v2/restart/*]
```

### 5.2. Adding a scope (process)

1. Edit `data/scopes.yaml`, append the new entry.
2. Add `require_scope("new.scope")` to the route(s) that need it
   in the owning pillar.
3. Commit + push.
4. Re-issue cookies for any beta users that should have the new scope
   (manual until Phase 2 ships the admin UI).

## 6. Per-pillar verifier API

Each pillar's `auth.py` (or equivalent — SMDL calls it `miniapp._verify`)
imports a small standardised helper. The helper is *copy-pasted*
across pillars (no shared package — each pillar's deployment is
independent). Total: ~70 lines.

```python
# Shared snippet — paste into each pillar's auth module.

import base64
import hmac
import json
import time
from hashlib import sha256
from fastapi import HTTPException

# Set from env in the host module.
OWNER_AUTH_TOKEN: str = ""

V1_MAX_AGE_SEC = 90 * 24 * 3600
V2_MAX_AGE_SEC = 90 * 24 * 3600   # overridable per-user in Phase 2

def parse_session_cookie(raw: str) -> dict:
    """Parse + verify the session cookie. Returns dict with:
        version  — "v1" or "v2"
        user_id  — "owner" for v1, slug for v2
        scopes   — list[str]; ["*"] for v1, parsed array for v2
        jti      — random id (v2 only; "" for v1)
        iat      — issuance unix-ts
        expired  — bool (True if older than max age)
    Raises HTTPException(401) on invalid signature or malformed input.
    """
    if not raw or not OWNER_AUTH_TOKEN:
        raise HTTPException(401, "no session")
    parts = raw.split(".")
    if len(parts) == 6 and parts[0] == "v2":
        _, ts_s, user_id, jti, scopes_b64, sig = parts
        body = ".".join(parts[:5])
        expected = hmac.new(OWNER_AUTH_TOKEN.encode(),
                            body.encode(), sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise HTTPException(401, "bad signature")
        try:
            ts = int(ts_s)
            scopes = json.loads(base64.urlsafe_b64decode(
                scopes_b64 + "==").decode())
            if not isinstance(scopes, list):
                raise ValueError
            scopes = [str(s) for s in scopes]
        except Exception:
            raise HTTPException(401, "malformed")
        return {
            "version": "v2", "user_id": user_id, "scopes": scopes,
            "jti": jti, "iat": ts,
            "expired": (time.time() - ts) >= V2_MAX_AGE_SEC,
        }
    elif len(parts) == 3:
        ts_s, nonce, sig = parts
        body = f"{ts_s}.{nonce}"
        expected = hmac.new(OWNER_AUTH_TOKEN.encode(),
                            body.encode(), sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise HTTPException(401, "bad signature")
        try:
            ts = int(ts_s)
        except Exception:
            raise HTTPException(401, "malformed")
        return {
            "version": "v1", "user_id": "owner", "scopes": ["*"],
            "jti": "", "iat": ts,
            "expired": (time.time() - ts) >= V1_MAX_AGE_SEC,
        }
    else:
        raise HTTPException(401, "unrecognised cookie format")


def has_scope(payload: dict, required: str) -> bool:
    scopes = payload.get("scopes") or []
    if "*" in scopes:
        return True
    if required in scopes:
        return True
    # Pillar-wide wildcard: "smdl.*" grants "smdl.iptv", etc.
    pillar = required.split(".", 1)[0] + ".*"
    return pillar in scopes


def require_scope(payload: dict, required: str) -> None:
    if not has_scope(payload, required):
        raise HTTPException(403, f"missing scope: {required}")
```

### 6.1. Usage in pillar routes

```python
# Existing pattern (SMDL example):
@router.get("/api/iptv/channels")
async def iptv_channels(request: Request, ...):
    payload = await _mini._verify(request)
    require_scope(payload, "smdl.iptv")    # ← single line addition
    ...
```

That's the only per-route change. Existing `_verify` still returns
the payload; new helper enforces.

### 6.2. Revocation (Phase 2+)

When the revocation table is added, `parse_session_cookie` gets
a third argument:

```python
def parse_session_cookie(raw: str, revoked_jtis: set[str] = None) -> dict:
    ...
    if jti and revoked_jtis and jti in revoked_jtis:
        raise HTTPException(401, "session revoked")
```

The pillar fetches `revoked_jtis` once per request from a cached
local file (`/tmp/sentinel_revocation_list.json`) that the Suite
rsync-style writes after each revocation. Cache TTL 60s. Revocation
propagates within ~1 minute.

## 7. Phase 2 — admin UI + persistence

### 7.1. SQLite schema (in suite container)

```sql
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,        -- slug, [a-z0-9_-]+
    handle          TEXT,                    -- human name/email for display
    scopes_json     TEXT NOT NULL,           -- ["smdl.iptv","apps.install"]
    created_at      TEXT NOT NULL,
    expires_at      TEXT,                    -- null = use default 90d
    revoked_at      TEXT,                    -- null = active
    notes           TEXT,                    -- free-form admin notes
    last_active_at  TEXT                     -- updated by validator
);

CREATE TABLE IF NOT EXISTS invites (
    token         TEXT PRIMARY KEY,           -- url-safe 32 bytes
    user_id       TEXT NOT NULL REFERENCES users(id),
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,              -- typically created_at + 24h
    redeemed_at   TEXT,                       -- null = unused
    redeemed_ip   TEXT
);

CREATE TABLE IF NOT EXISTS revocations (
    jti           TEXT PRIMARY KEY,
    user_id       TEXT,                       -- denormalised for the audit
    revoked_at    TEXT NOT NULL,
    reason        TEXT
);

CREATE TABLE IF NOT EXISTS auth_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    event         TEXT NOT NULL,              -- 'user.create' | 'invite.send' | 'invite.redeem' | 'cookie.issue' | 'cookie.revoke' | 'access.denied'
    user_id       TEXT,
    jti           TEXT,
    scopes_json   TEXT,
    ip            TEXT,
    user_agent    TEXT,
    payload_json  TEXT                        -- event-specific extras
);
```

### 7.2. Admin endpoints (suite-internal, owner-only)

```
GET  /admin/users                       → HTML user list + create form
POST /admin/users                       → create user; body: {id, handle, scopes[], expires_in_days?}
GET  /admin/users/<id>                  → user detail + audit log
POST /admin/users/<id>/invite           → mint a redemption URL (24h TTL)
POST /admin/users/<id>/scopes           → edit scopes (creates new JTI, old not auto-revoked)
POST /admin/users/<id>/revoke           → set revoked_at + add active JTI to revocations
GET  /admin/audit                       → recent events (server-paginated)
GET  /api/scopes                        → returns parsed scopes.yaml (for UI dropdowns)
```

### 7.3. Beta-user endpoints (public, no cookie needed for redeem)

```
GET  /auth/redeem?token=<t>             → looks up invite, mints v2 cookie,
                                          303 to suite home. Marks invite redeemed.
GET  /auth/whoami                       → returns own payload (any cookie)
POST /auth/logout                       → clears own cookie
```

### 7.4. Suite UI surface

A 7th admin-only section in the Suite (`/` home page) shows up only
when `payload.user_id == "owner"`:

```
🔑 User management
  Active beta users: 3 · Invites pending: 1
  → /admin/users
```

Page layout (mobile-friendly, same drawer pattern as everything else):
- Top: "+ Invite new user" form (id, handle, scopes multi-select)
- List of users with: handle · scopes count · last-active · status badge
- Tap user → detail page → edit scopes / revoke / view audit log

## 8. Phase 3 — cross-pillar rollout

In order of risk (low → high):

1. **SMDL `/iptv/*`** — well-isolated, low-traffic, owner-tested.
   First pillar to enforce. Decorator added in Phase 1.
2. **Sentinel Suite Apps store (`/apps/*`)** — also self-contained,
   no external dependencies. Probably the SECOND pillar.
3. **SMDL streamtracker (`/watchlist`, `/api/miniapp/*`)** — heavier
   user-facing surface; needs careful decorator pass.
4. **SMDL downloader (`/dl`, `/api/dl/*`)** — touches yt-dlp.
5. **Sentinel Finance** — sensitive data, two scopes
   (`finance.view`, `finance.write`). Requires more thoughtful audit.
6. **Sentinel AI** — `ai.chat` (any beta user) vs `ai.admin` (almost
   never granted).
7. **Watchdog** — view vs restart. Admin scope, rarely granted.
8. **Gaming** — `gaming.play` only; host scope owner-only forever.

Per pillar: ~30 min once the helper is in place.

## 9. Security considerations

- **HMAC key reuse**: same `OWNER_AUTH_TOKEN` signs v1 and v2.
  Cookie validity is tied to that secret across BOTH formats. If
  the secret rotates, all v1 + v2 cookies invalidate together.
  Acceptable — same property as today.
- **Scope inflation**: a user can't grant themselves scopes — only
  the owner can write to `users.scopes_json` via /admin/users.
  Cookies are signed; the user can't edit scopes_b64 without
  breaking the HMAC.
- **JTI uniqueness**: 16 bytes of CSPRNG = 128 bits entropy.
  Collision probability negligible.
- **Replay**: a leaked cookie is usable until its `iat + 90d`
  expires OR it appears in the revocation list. Owner detecting
  a leak and revoking propagates within 60s (cache TTL).
- **Cookie attributes**: `HttpOnly` + `Secure` + `SameSite=Lax`
  + `Domain=.your-domain.example.com`. Same as v1.
- **Invite tokens**: one-time-use (`redeemed_at` set on first use).
  TTL 24h. Sent to user via channel of owner's choosing (TG, email).
- **No web-facing user enumeration**: `/auth/redeem` returns same
  401 response for "unknown token" vs "expired token" vs "already
  used".
- **Audit log retention**: rolled in SQLite indefinitely (volume
  low — bounded by user count × actions/day). Rotated only if it
  ever exceeds ~10 MB.

## 10. Migration path from v1

1. **Pre-Phase 1**: zero changes. Everything works.
2. **Phase 1 lands**: cookie parser accepts both v1 and v2. SMDL
   `/iptv/*` calls `require_scope("smdl.iptv")`. Owner's v1 cookie
   continues to work (implicit `*` scope). No v2 cookies exist yet.
3. **Phase 2 lands**: admin UI ships. Owner can mint v2 cookies for
   beta users. Beta user without `smdl.iptv` scope gets 403 on
   /api/iptv/* — by design. Other pillars still see v2 cookies as
   "owner-equivalent" because they don't call `require_scope` yet.
4. **Phase 3 sequence**: each pillar adopts `require_scope` for its
   own routes one at a time. Beta users without the appropriate
   scope start hitting 403 on those pillars. Existing cookies don't
   need re-issuance — scopes were already baked in at mint time.
5. **End state**: every route is `require_scope`-gated. Owner
   cookie's implicit `*` keeps everything working.

## 11. Open questions

1. **Per-user expiry override** — Phase 2 schema includes
   `users.expires_at` but the verifier currently uses a global
   `V2_MAX_AGE_SEC`. Decision: per-user expiry baked INTO the cookie
   at mint time (cookie's `iat + expires_at` is the effective
   deadline), so the pillar verifier doesn't need to fetch user-
   table data per request.
2. **Refresh tokens?** — Not in scope. Users with expired cookies
   ask the owner for a new invite. Simpler, fewer moving parts.
3. **Multi-device cookies** — same JTI on all devices, or per-device
   JTIs? Decision: per-device. Each invite redemption mints a fresh
   JTI; user can have multiple active cookies, each independently
   revokable.
4. **2FA / TOTP** — out of scope for v2. Possibly a v3 concern if
   user count grows.
5. **Migration to Postgres** — SQLite is fine for the foreseeable
   scale. If we ever exceed ~1000 users or want cross-machine HA,
   migrate to the existing `metamcp_db` Postgres instance. Schema
   is portable verbatim.

## 12. Phases (recap)

| Phase | Effort | Ships when | Includes |
|---|---|---|---|
| 0 | 1 hr | done — this doc | Spec |
| 1 | 3 hr | when ready | Cookie v2 parser + scopes.yaml + verifier helper + SMDL /iptv decorator |
| 2 | 3 hr | when first beta user lined up | SQLite tables + /admin/users UI + /auth/redeem flow + audit log |
| 3 | 30 min × 7 | rolling | Per-pillar `require_scope` decorator pass |

Total to "fully realised v2": ~10 hours of work spread across however
many sessions makes sense.

---

**Sign-off**: this is the contract. Anything code-side that diverges
from this needs an edit here first, with a `# Changed:` callout in
the changelog at the bottom of this section when we revisit.

## Changelog

- 2026-05-27 — initial draft (Phase 0). azfar.
- 2026-05-27 — Phase 2 shipped: SQLite-backed user store + /admin/users
  CRUD UI + /auth/redeem one-time-use flow + /auth/whoami + /api/scopes
  + audit log + revocation cascade. Bonus Phase-3 enforcement on the
  Suite's own /apps/* (apps.install scope). Verified end-to-end:
  owner-cookie unchanged, beta user create→invite→redeem→whoami chain
  green, scope-gated 403 confirmed, revocation propagates immediately
  (in-process, no cache TTL — single container). Suite home renders
  the 🔑 Users tile only when payload.user_id == "owner". azfar.
- 2026-05-27 — Phase 3 SMDL pillar complete (sentinel-smdl commit
  15ffbcd). 44 routes now scope-gated across miniapp.py + sticker_
  routes.py: 8 streamtracker, 7 downloader, 24 admin, 5 stickers.
  Phase 1's /iptv/* gating already in 2dcdf73. SMDL Mini App is the
  first pillar fully gated end-to-end — a v2 cookie with only
  `smdl.iptv` now correctly 403s on every other SMDL surface.
  Remaining Phase 3 pillars (Finance / AI / Watchdog / Gaming) are
  tracked in task #129 but deliberately not started — work paused
  on SMDL by owner direction. azfar.
