# SMDL — Sentinel Media Downloader

> Standalone Telegram bot that wraps `yt-dlp` and `gallery-dl`. Handles
> single-video downloads, photo carousels, and livestream recording with
> automatic delivery via Telegram bot API, Telethon user account, tailnet
> HTTP, or HMAC-signed public share URLs.

[![Docker Image](https://img.shields.io/badge/docker-ghcr.io%2FYOUR_GITHUB_USERNAME%2Fsentinel--smdl-blue?logo=docker)](https://github.com/YOUR_GITHUB_USERNAME/sentinel-smdl/pkgs/container/sentinel-smdl)
[![Release](https://img.shields.io/github/v/release/YOUR_GITHUB_USERNAME/sentinel-smdl)](https://github.com/YOUR_GITHUB_USERNAME/sentinel-smdl/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Carved out of the [Project Sentinel](https://github.com/azfarh95/sentinel-stack-public-public) homelab stack as a deployable-anywhere component. Runs on a single Docker host (amd64 or arm64). No AI dependencies — same code can be re-wrapped as an MCP tool for LLM-agent use via the `RecorderBridge` interface.

---

## Features

### Core
- **1700+ sites via `yt-dlp`** — YouTube, Twitch, Kick, TikTok, Instagram, X/Twitter, Facebook, Reddit, Bilibili, Pinterest, and the long tail
- **Photo / carousel fallback** via `gallery-dl` for sites where yt-dlp can't extract images
- **Livestream recording** with native HLS downloader, watchdog-supervised graceful stop, `.part` finalize/remux so player metadata matches actual content
- **Stream monitor** — `/watch <url>` adds a streamer to a watchlist; when they go live, the bot DMs you with Record / Skip / Snooze-1h / Snooze-8h buttons
- **Per-chat preferences** persisted across container restarts — every user gets their own language, timezone, quality preference, transcode mode

### Delivery
File size determines the path automatically:
- **< 50 MB** → inline via Telegram Bot API
- **≤ 2 GB** → Telethon user-account upload (if configured)
- **> 2 GB** → tailnet HTTP link (`http://<host>:8096/m/<file>`, mesh-only, source-IP gated) + HMAC-signed public share URL (24h expiry)

### Resilience
- **TLS impersonation** via `curl_cffi` for Cloudflare-protected sites (opt-in per host via plugin)
- **Graceful bot startup** — invalid `SMDL_BOT_TOKEN` keeps `/health` reachable so the operator can curl, check logs, and fix without container crashloop
- **Adaptive site support** — any URL with a yt-dlp extractor works; 3 consecutive "no extractor" failures triggers a friendly "site not supported" message
- **SQLite URL cache** — repeat downloads served from cache (`/clear_cache` to wipe)
- **Per-platform cookies** in `/cookies/<site>.txt` for sub-only / age-gated content

---

## Quick start

### Option A — Pull from GHCR (fastest)

```bash
mkdir -p smdl-data/{config,downloads,cookies}
cp /dev/stdin smdl-data/config/smdl.json <<< '{ "owner_chat_id": YOUR_CHAT_ID }'

docker run -d \
  --name smdl \
  -p 127.0.0.1:8096:8096 \
  -e SMDL_BOT_TOKEN="<from @BotFather>" \
  -v "$PWD/smdl-data/config:/config" \
  -v "$PWD/smdl-data/downloads:/downloads" \
  -v "$PWD/smdl-data/cookies:/cookies" \
  ghcr.io/YOUR_GITHUB_USERNAME/sentinel-smdl:v1.0.0
```

DM your bot any video URL — TikTok, YouTube, Instagram reel, Twitch clip, etc. — and it'll download + reply with the file.

### Option B — Build from source

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/sentinel-smdl
cd sentinel-smdl
cp config/smdl.example.json config/smdl.json
# Edit smdl.json: set owner_chat_id, allowed_chat_ids if you want a closed bot

export SMDL_BOT_TOKEN="<from @BotFather>"

# Optional: enable >50 MB delivery paths
export SMDL_PUBLIC_BASE_URL=https://media.your-domain.example.com
export SMDL_TAILNET_HOST=your-host.tail-XXXX.ts.net
export SMDL_SHARE_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")

# Optional: Telethon for 50 MB – 2 GB delivery
export TELETHON_API_ID=<from my.telegram.org>
export TELETHON_API_HASH=<from my.telegram.org>
export TELETHON_SESSION=<see scripts/generate_session.py>

docker build -t smdl .
docker run -d --name smdl -p 127.0.0.1:8096:8096 \
  -v ./config:/config -v ./downloads:/downloads -v ./cookies:/cookies \
  --env-file .env smdl
```

---

## Bot commands

| Command | Effect | Scope |
|---|---|---|
| (paste any URL) | Auto-detect platform → identify (live / video / photo / carousel) → download → deliver | Anyone in allowed chats |
| `/watch <url> [label]` | Add a streamer to the live-watch list. Bot prompts you when they go live. | Owner only |
| `/unwatch <url>` | Remove from watchlist | Owner only |
| `/watchlist` | Show watchlist + status badges (🔴 live, ⚫ offline, 💤 snoozed) | Owner only |
| `/live_status` | Show the active livestream recording (if any) | Anyone |
| `/stop_livestream` | Halt the active livestream recording cleanly | Anyone |
| `/default_video_size` | Inline picker: Best / 1080p / 720p / 360p (per-chat) | Anyone |
| `/transcode` | Inline picker for post-recording transcode (Off / 480p / 240p, replace or keep-original) | Anyone |
| `/language` | Switch bot language (English / Русский) | Anyone |
| `/timezone <offset>` | Set chat's UTC offset (e.g. `/timezone 8`, `/timezone -5`, `/timezone 5.5`) | Anyone |
| `/storage_stats` | Disk free, recording counts + sizes, cache stats | Owner only |
| `/clear_cache [url]` | Wipe URL cache (entire cache or one URL) | Owner only |

---

## Configuration

`config/smdl.json` — operational settings. Copy from [`config/smdl.example.json`](config/smdl.example.json) which has inline comments for every field.

| Key | Default | Purpose |
|---|---|---|
| `owner_chat_id` | `null` | Numeric Telegram chat ID of the bot's owner |
| `allowed_chat_ids` | `[]` | If non-empty, bot ignores messages from chats not in this list |
| `default_quality` | `"1080p"` | Default video quality cap for non-live downloads (per-chat overrides via `/default_video_size`) |
| `max_concurrent_downloads` | `2` | Semaphore for parallel non-live jobs |
| `delete_after_send` | `false` | If true, deletes file after successful Telegram send |
| `temp_ttl_hours` | `24` | Cleanup interval for `/downloads/temp/` |
| `live_enabled` | `true` | Master switch for livestream recording |
| `live_max_concurrent` | `1` | Per-host cap on simultaneous live recordings |
| `live_heartbeat_seconds` | `300` | yt-dlp progress hook cadence (independent 60s timer ALSO drives UI updates) |
| `live_min_free_disk_gb` | `10` | Refuse to start a live recording if free disk < this |
| `live_abort_on_session_fail` | `true` | Zero-retry on auth/cookie failures |
| `live_platforms` | `["youtube","twitch","kick"]` | Advisory list for friendly labels (any yt-dlp-supported URL works) |
| `live_max_height` | `720` | Capture resolution cap (0 = source/unlimited). Trades file size for quality. |
| `live_transcode_height` | `0` | Optional post-recording re-encode height (e.g. `480`). `0` = off. |
| `live_transcode_keep_original` | `false` | If true, original is kept + transcoded sibling produced; if false, transcode replaces original |
| `monitor_enabled` | `true` | Watchlist polling loop |
| `monitor_poll_interval_seconds` | `300` | How often watchlist URLs are probed for live state |
| `monitor_probe_timeout_seconds` | `30` | Max wait per yt-dlp probe before treating as offline |

### Environment variables

| Var | Required | Purpose |
|---|---|---|
| `SMDL_BOT_TOKEN` | yes | Telegram bot token from `@BotFather` |
| `DOWNLOADS_DIR` | no | Default `/downloads` |
| `COOKIES_DIR` | no | Default `/cookies`. Per-platform: `youtube.txt`, `twitch.txt`, `instagram.txt`, etc. |
| `CONFIG_FILE` | no | Default `/config/smdl.json` |
| `SMDL_PUBLIC_BASE_URL` | optional | HTTPS domain fronting this service (enables signed-URL public sharing) |
| `SMDL_TAILNET_HOST` | optional | Tailscale MagicDNS hostname for mesh-only delivery |
| `SMDL_SHARE_SECRET` | optional | HMAC key for signed URLs (64 hex chars). Required if `SMDL_PUBLIC_BASE_URL` is set. |
| `TELETHON_API_ID` / `_API_HASH` / `_SESSION` | optional | Enables 2 GB Telethon upload fallback |

---

## Architecture

```
                                 ┌─────────────────┐
   Telegram  ────────────────►   │   bot.py        │
                                 │   (PTB + async  │
                                 │   concurrent_up)│
                                 └────────┬────────┘
                                          │
       ┌──────────────────┬───────────────┼──────────────────┬──────────────────┐
       ▼                  ▼               ▼                  ▼                  ▼
┌────────────┐   ┌────────────────┐ ┌──────────────┐ ┌──────────────────┐ ┌─────────────┐
│interceptor │   │  identify_post │ │  download    │ │ recorder_bridge  │ │stream_monitor│
│ regex URL  │──►│  yt-dlp probe  │►│  yt-dlp /    │ │  (job lifecycle  │ │ poll watch- │
│ detection  │   │  (no fetch)    │ │  gallery-dl  │ │   owner)         │ │ list every  │
└────────────┘   └────────────────┘ └──────┬───────┘ └──────┬───────────┘ │ 5 min       │
                                           │                │              └─────────────┘
                                           ▼                ▼
                                   ┌─────────────────────────────────┐
                                   │ Delivery decision (size-based)  │
                                   │  < 50 MB    → bot inline send   │
                                   │  50 MB–2 GB → Telethon (if cfg) │
                                   │  > 2 GB     → tailnet + signed  │
                                   └─────────────────────────────────┘
```

**`recorder_bridge.py`** owns all live-recording job state — chat-id → handle map, stop flags, status snapshots. `bot.py` is thin; the bridge is the single source of truth. The same interface is what a future MCP-tool adapter would consume.

**`file_serve.py`** exposes `GET /m/<file>` (tailnet-only via source-IP gate) and `GET /share/<token>/<file>` (HMAC-signed public).

---

## Plugin tier

`app/plugins/` is a drop-in extension point. Files in this directory are auto-loaded at startup. A plugin module's top-level code registers extensions via four public functions:

```python
# app/plugins/my_plugin.py
from app.interceptor import register_pattern, register_rewrite
from app.live_downloader import register_cloudflare_host, register_platform_label

# Add URL detection — find_video_url() returns ('myplatform', url)
register_pattern("myplatform", r"https?://(?:www\.)?myplatform\.com/[\w\-]+/?")

# Rewrite a mirror domain before yt-dlp sees it
register_rewrite("mymirror", "realsite", "mymirror.com", "realsite.com")

# Declare Cloudflare-protected hosts (need Chrome TLS impersonation)
register_cloudflare_host("myplatform.com", "another.com")

# Friendly platform label for log lines + status messages
register_platform_label("myplatform", "myplatform.com")
```

See [`app/plugins/README.md`](app/plugins/README.md) for the full contract. Failed plugin imports are logged but don't crash the core.

---

## Multi-arch + GHCR

Images published to [`ghcr.io/YOUR_GITHUB_USERNAME/sentinel-smdl`](https://github.com/YOUR_GITHUB_USERNAME/sentinel-smdl/pkgs/container/sentinel-smdl) on every tag push:

- `linux/amd64` — x86 servers, most VPSes, Intel/AMD desktops
- `linux/arm64` — Pi 4/5, ARM cloud instances, M-series Macs running Docker

Build workflow: [`.github/workflows/build-publish.yml`](.github/workflows/build-publish.yml). Uses `docker/buildx-action` with `type=gha` cache so post-first builds finish in <1 min.

---

## Testing

### Fresh-install verification

```powershell
.\tests\fresh-install\run-wsl2-test.ps1
```

Spins up a clean WSL2 Ubuntu distro, runs the bootstrap (installs Docker + git + ffmpeg), builds the SMDL image from source, starts the container, polls `/health` until 200 OK, then tears the distro down. Runs in <30 s on a warm rootfs cache. Catches doc gaps, missing prereqs, and Windows-line-ending issues that would only surface for an outside user.

See [`tests/fresh-install/TEST_PLAN.md`](tests/fresh-install/TEST_PLAN.md) for the acceptance criteria.

---

## Status

**V1 stable** (released [v1.0.0](https://github.com/YOUR_GITHUB_USERNAME/sentinel-smdl/releases/tag/v1.0.0)). In hardening/soak period — 1-2 months of daily-use validation before V2 work begins.

### Roadmap

| Stage | Scope | State |
|---|---|---|
| **V1 — Discovery + Docker** | Telegram bot + yt-dlp/gallery-dl + livestream recording + stream monitor + dual-path delivery + RecorderBridge + plugin tier + GHCR multi-arch + fresh-install test | 🟢 **Stable** — released v1.0.0 |
| **V2 — UX + mini-app** | Dedicated TOTP-gated web mini-app: recording history, manual /stop, per-platform cookie management, retry-budget reset | ⚪ Scoped, not started — gated on V1 soak |
| **V3 — Native binary** | PyInstaller / Nuitka builds for Windows / Linux / macOS published in GitHub Releases. No Docker required. | ⚪ |
| **V4 — Installer wizard** | Inno Setup (Windows) / pkg (macOS) / deb (Linux) wrapping V3, with service registration + uninstaller | ⚪ |

V1 must stabilise (1-2 months without major regressions) before V2 starts. Each stage is independently shippable; users pick the level of polish that suits them.

---

## Contributing

Bug reports + PRs welcome at [github.com/YOUR_GITHUB_USERNAME/sentinel-smdl/issues](https://github.com/YOUR_GITHUB_USERNAME/sentinel-smdl/issues). Site-specific extractor work is preferred upstream at [yt-dlp](https://github.com/yt-dlp/yt-dlp) — this repo's plugin tier is the right place for SMDL-specific wiring (URL detection, Cloudflare hosts, etc.) only.

---

## License

[MIT](LICENSE). Do not redistribute the maintainer's `smdl.json` (contains personal chat IDs).
