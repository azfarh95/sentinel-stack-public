# Sentinel — Cache Registry

Every cache layer in the stack, with its refresh mechanism. Goal: any
config rotation should propagate to all caches automatically OR have a
documented procedure for forcible refresh.

Last verified: 2026-05-11 (sanity check after Phases A-E)

---

## Tier definitions

| Tier | Lifetime | Refresh | Risk if stale |
|---|---|---|---|
| **L1 — in-memory** | Process lifetime | Restart process OR per-call re-read | High — silent stale-value bugs |
| **L2 — on-disk** | Filesystem | Rewrite the file | Medium — process re-reads on next boot |
| **L3 — docker volume / DB** | Container/volume lifetime | UPDATE + container restart | High — DB rows survive container recreate |
| **L4 — external** | Provider-defined | Provider-controlled (API key, OAuth refresh) | Bounded — rotation required |

---

## L1: In-memory caches (every long-running service)

| Service | Cache slot | Refresh mechanism | Rotation impact |
|---|---|---|---|
| **watchdog.py** | `lm_api_key` (LM Studio probe) | **Per-probe via WCM** (Phase B ✓) | Auto-picks up next cycle (~30s) |
| watchdog.py | `bot_token`, `github_pat`, others | Boot-cached via `setup_cfg()` | **Restart required** (Phase B follow-up) |
| **infer_bridge.py** | `_lm_api_key()` | **Per-call via WCM** ✓ | Auto-picks up next request |
| infer_bridge.py | `_loaded_models` | 15s TTL via `_get_loaded_models()` | Self-refreshing |
| infer_bridge.py | `_current_model` (status display) | Set on each /completions request | Self-refreshing |
| **sentinel-miniapp-v2/bridge.py** | `TOTP_SECRET` | **Per-verify via _secret()** (Phase E ✓) | Auto-picks up next auth |
| bridge.py | `TELEGRAM_TOKEN`, `MINI_APP_SECRET`, Telethon | Boot-cached at module load | **Restart required** (Phase E follow-up) |
| **openclaw-gateway** (node) | `auth-profiles.json`, `openclaw.json`, `models.json` | Boot-cached | **`systemctl restart openclaw-gateway`** |
| **MetaMCP** container | Container env (POSTGRES_*, BETTER_AUTH_SECRET) | Container env vars (immutable) | **Recreate container** |
| **MetaMCP** Postgres | `mcp_servers.env` rows (Tavily key) | Read per MCP session | New session picks up change after MetaMCP restart |

---

## L2: On-disk caches (canonical state)

### Windows side

| Path | Type | Refreshed by | Read by |
|---|---|---|---|
| `metamcp-local\.env.local` | Generated env file | `sync_env_from_wcm.ps1` | Docker compose at `up` |
| `google-workspace-mcp\data\token.json` | OAuth refresh token | Google python lib (in-place rewrite) | google-workspace-mcp container |
| `google-workspace-mcp\data\credentials.json` | OAuth client cfg | Rotation scripts | google-workspace-mcp container |
| `onedrive-mcp\data\token.json` | Microsoft refresh token | onedrive-mcp Flask app (re-consent flow) | onedrive-mcp container |
| `logs\infer_bridge.jsonl` | Append-only audit log | infer_bridge.py per request | Manual analysis |
| `metamcp-local\watchdog\contacts.json` | Per-user Telegram contacts | watchdog.py | watchdog.py |

### WSL side (`/home/azfar/.openclaw/`)

| Path | Type | Refreshed by | Read by |
|---|---|---|---|
| `openclaw.json` | Main OpenClaw config | rotate scripts + OpenClaw itself | openclaw-gateway at boot |
| `agents/main/agent/auth-profiles.json` | Provider auth profiles | rotation scripts | openclaw-gateway at boot |
| `agents/main/agent/models.json` | Model list + apiKey copies | rotation scripts | openclaw-gateway at boot |
| `credentials/telegram-default-allowFrom.json` | Per-user allowlist | OpenClaw runtime | openclaw-gateway |
| `memory/store.db` | Long-term agent memory | OpenClaw runtime | openclaw-gateway |
| `openclaw.json.last-good` | Last-known-good baseline | OpenClaw itself (on successful boot) | watchdog drift baseline |
| `openclaw.json.bak` | Most-recent backup | OpenClaw on every config write | Manual rollback |

### Legacy paths (purged 2026-05-11)

| Path | Status | Action taken |
|---|---|---|
| `C:\Users\azfar\.openclaw\` | Stub from old Windows-native install | **Deleted (1.3 MB)** |
| `~/.openclaw/openclaw.json.clobbered.*` (13 files) | Auto-rotated by OpenClaw on config-clobber events | **Deleted** |
| `~/.openclaw/openclaw.json.bak.{1,2,3,4}` | Sequential historical backups | **Deleted (kept `.bak`)** |
| `~/.openclaw/openclaw.json.broken` | Failed-parse snapshot from 2026-04-28 | **Deleted** |
| `~/.openclaw/openclaw.json.new` | Empty 0-byte orphan | **Deleted** |
| `~/.openclaw/credentials/*.bak-*` | Today's allowFrom backup | **Deleted** |
| `metamcp-local\docker-compose.dev.yml` | Pre-consolidation dev variant | **Archived → `backups/legacy-archive/`** |
| `metamcp-local\Maintenance\openclaw-gateway.service.bak` | Pre-edit service unit | **Archived → `backups/legacy-archive/`** |

---

## L3: Docker volumes + Postgres rows

| Volume | Container | Holds | Refresh |
|---|---|---|---|
| `metamcp-local_metamcp_local_postgres_data` | metamcp-pg | MetaMCP DB (mcp_servers, namespaces, tools, secrets) | UPDATE + MetaMCP container restart |
| `metamcp-local_memory_mcp_data` | memory-mcp | Long-term memory store | Container restart not required (writes through) |
| `metamcp-local_reminders_mcp_data` | reminders-mcp | Scheduled reminders DB | Container restart not required |
| `metamcp-local_smdl_data` | smdl | Job state + queue | Container restart not required |
| `metamcp-local_ytdlp_mcp_data` | ytdlp-mcp | yt-dlp cache | Container restart not required |
| `metamcp-local_onedrive_mcp_data` | onedrive-mcp | OneDrive metadata | Container restart not required |
| `metamcp-local_libretranslate_models` | libretranslate | Translation models | Static |
| `metamcp-local_firefly_db` / `_upload` | firefly | Finance app (profile: finance) | App-managed |
| `metamcp-local_forgejo_data` | forgejo | Git server (profile: journal) | App-managed |
| `metamcp-local_pia_exit_data` / `_tailscale_pia_state` | pia-exit / tailscale-pia | VPN state (profile: vpn, parked) | App-managed |

---

## L4: External APIs (provider-side)

| Provider | Endpoint | Auth | Rotation cost |
|---|---|---|---|
| Tavily | `api.tavily.com/search` | Bearer token | App-side regen |
| GitHub | `api.github.com/*` | PAT (Bearer) | App-side regen |
| Google OAuth | `oauth2.googleapis.com/token` | client_id + secret + refresh_token | Console regen + token cache |
| Microsoft OAuth | `login.microsoftonline.com/*/oauth2/v2.0/token` | client_id + secret + refresh_token | Console regen + token cache |
| Azure Doc Intel | `*.cognitiveservices.azure.com` | Subscription key | Console regen |
| Cloudflare Tunnel | `*.cfargotunnel.com` | Connector token | Console (no UI for rotation yet) |
| LM Studio | `localhost:1234` | Bearer (local key) | LM Studio app |
| Telegram Bot API | `api.telegram.org/bot*/getMe` | Bot token | BotFather `/revoke` + new |
| Telethon | MTProto | API ID + hash + session | my.telegram.org + interactive flow |

---

## Forcible refresh procedures

### When a secret rotates, who needs to be told?

Use `scripts/secrets.yaml` as the source of truth + `scripts/restart_consumers.ps1 -Secret <name>` to bounce consumers automatically. Consumers marked `hot-reload: true` skip the restart (Phase B/E refactors).

### When `.env.local` changes (any WCM secret edit):

```powershell
.\scripts\sync_env_from_wcm.ps1     # regenerate .env.local from WCM
docker compose --env-file .env.local up -d                  # recreate ALL containers using new env
# OR for one service:
docker compose --env-file .env.local up -d --no-deps --force-recreate <service>
```

⚠ `--force-recreate` is NOT sufficient on Docker Desktop Windows for some bindings — use full `docker stop <svc> && docker rm <svc> && docker compose up -d <svc>` if you hit the binding-leak bug (see `feedback_docker_desktop_windows_bindings.md`).

### When openclaw.json or auth-profiles.json changes:

```bash
wsl -d Ubuntu-24.04 -u root --exec bash -c 'systemctl restart openclaw-gateway'
```

OpenClaw caches config at boot; no hot-reload. After restart, the watchdog drift meta-check (`_check_secret_drift`) confirms within one cycle that the file matches WCM.

### When LM Studio rotates (in-app):

```powershell
.\scripts\rotate_lmstudio_api.ps1
```

Updates: WCM × 2 namespaces, auth-profiles.json, openclaw.json, models.json, restarts openclaw-gateway + infer-bridge + watchdog.

### When TOTP rotates:

```powershell
.\scripts\rotate_totp_secret.ps1
```

bridge.py's TOTP verify path now re-reads per-call (Phase E), so this only requires WCM update + scan new QR. No bridge restart needed.

---

## Stale-cache fingerprint (debugging)

If a service is behaving as if a rotated secret never happened:

1. `git log --oneline -1` the relevant rotation script to confirm what it touches.
2. Compare `keyring.get_password()` value with what's in the consumer's config file (via `_check_secret_drift` if watchdog) or with what the process is sending (via service logs).
3. If they differ: the service hasn't been restarted since the rotation. Bounce it via `restart_consumers.ps1` or directly.
4. If they match but the service is still failing: check the upstream API itself (probe with the WCM key value via direct curl).

The diagnosis-before-fix pattern (see `feedback_diagnose_before_fix.md`) applies here.

---

## Inventory checksum

To run a full handshake + cache freshness check at any time:

```powershell
.\scripts\sanity_check.ps1   # TODO: not yet built — Phase F candidate
```

Until that exists, use the inline commands in this doc.
