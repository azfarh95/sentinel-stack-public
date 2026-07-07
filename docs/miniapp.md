# Sentinel Mini App

A Telegram Mini App dashboard for the Sentinel AI Stack, secured behind two independent auth factors.

- **Public URL:** `https://your-domain.example.com` (Cloudflare Tunnel)
- **Bridge port:** 8098 (local only)
- **Script:** `sentinel-miniapp-v2/bridge.py`

---

## Authentication flow

1. Telegram identity verified via HMAC-SHA256 on `initData` (or Login Widget in browser)
2. Owner ID checked against configured `OWNER_ID`
3. TOTP code (Google Authenticator) — 30-second rolling window, rate-limited (5 failures / 15 min → 429)
4. 8-hour session token issued, stored in `localStorage`

Session tokens are stored as SHA-256 hashes with per-row HMAC integrity in SQLite. Direct DB edits are detected via MAC mismatch.

**TOTP setup:** on first bridge start, open `sentinel-miniapp-v2/totp_setup.html` locally and scan the QR code with Google Authenticator.

---

## Dashboard screens

| Screen | What it shows / does |
|--------|----------------------|
| Home | Model name, context %, inference state, memory count, Watchdog Monitor cards |
| Watchdog Monitor → Docker | Per-container up/down status with detail |
| Watchdog Monitor → Processes | OpenClaw, LM Studio, Infer Bridge, Sentinel Bridge, Playwright |
| Watchdog Monitor → HTTP | HTTP health endpoint states for all MCP servers |
| Updates | Version tracking for 8 components — current vs latest, Update button |
| Memories | Browse and delete long-term memories |
| Reminders | Browse and delete scheduled reminders |
| Shortcuts | One-tap prompt shortcuts, sent directly to OpenClaw |
| Settings | OpenClaw config, skills, doctor, theme, session management |

<table>
  <tr>
    <td align="center"><b>Home</b><br><img src="../assets/screenshots/01-home.jpg" width="160"/></td>
    <td align="center"><b>Docker</b><br><img src="../assets/screenshots/02-docker.jpg" width="160"/></td>
    <td align="center"><b>Processes</b><br><img src="../assets/screenshots/03-processes.jpg" width="160"/></td>
    <td align="center"><b>HTTP Endpoints</b><br><img src="../assets/screenshots/04-http-endpoints.jpg" width="160"/></td>
  </tr>
  <tr>
    <td align="center"><b>Updates</b><br><img src="../assets/screenshots/05-updates.jpg" width="160"/></td>
    <td align="center"><b>Memories</b><br><img src="../assets/screenshots/06-memories.jpg" width="160"/></td>
    <td align="center"><b>Reminders</b><br><img src="../assets/screenshots/07-reminders.jpg" width="160"/></td>
    <td align="center"><b>Shortcuts</b><br><img src="../assets/screenshots/08-shortcuts.jpg" width="160"/></td>
  </tr>
</table>

---

## Settings

### OpenClaw Config
Adjust OpenClaw without editing `openclaw.json` directly:
- **Reasoning effort** — pill selector (none / minimal / low / medium / high / xhigh)
- **Max tokens** — output token cap for the active model
- **Timeout** — LM Studio request timeout in seconds
- **Web search / Web fetch** — enable/disable toggles
- **Skills** — navigate to Skills subpage

Changes are written to `openclaw.json` and the gateway is sent `SIGUSR1` to hot-reload.

### Skills
Lists all OpenClaw skills grouped by Enabled / Available. Each row has:
- **Toggle** — enable/disable the skill (saved via main Save button)
- **Expand (›)** — tap the skill name to open an inline credential panel

<table>
  <tr>
    <td align="center"><b>Settings</b><br><img src="../assets/screenshots/10-settings.jpg" width="160"/></td>
    <td align="center"><b>OpenClaw Config</b><br><img src="../assets/screenshots/12-openclaw-config.jpg" width="160"/></td>
    <td align="center"><b>Skills</b><br><img src="../assets/screenshots/14-skills.jpg" width="160"/></td>
    <td align="center"><b>Skill Credentials</b><br><img src="../assets/screenshots/15-skills-credentials.jpg" width="160"/></td>
  </tr>
</table>

#### Skill Credential Manager
Stores API tokens/credentials for each skill in **Windows Credential Manager** (via `keyring`), never in `openclaw.json`.

Credential storage key format: `sentinel-skill-{skill-name}`

Actions:
- **+ Add credential** — enter key name (e.g. `api_key`) + value → saved to WCM
- **✎ Edit** — update an existing credential value
- **× Delete** — remove a credential from WCM

Values are never returned to the UI — the panel only shows whether a key is set (masked as `••••••`).

### OpenClaw Doctor
One-tap diagnostic. Checks:
- `systemd` service state
- OpenClaw port `:18789`
- MetaMCP port `:12008`
- Memory MCP port `:8092`
- Memory record count
- Last 8 journal log lines

### Theme *(V2 — in progress)*
Preset color themes + icon library (Lucide). Persisted in `localStorage`.

### Sessions
Lists active sessions with IP, user agent, and expiry. Supports per-session revocation.

---

## Version tracking (Updates screen)

| Component | Current source | Latest source |
|-----------|---------------|---------------|
| yt-dlp | `pip show` inside ytdlp-mcp container | GitHub releases |
| gallery-dl | `pip show` inside ytdlp-mcp container | GitHub releases |
| LibreTranslate | `/app/VERSION` inside container | GitHub releases |
| MetaMCP | OCI image label | GitHub releases |
| GitHub MCP | OCI image label | GitHub releases |
| OpenClaw | `npm list` in WSL Ubuntu-24.04 | npm registry |
| LM Studio | Windows exe VersionInfo | GitHub releases |
| Docker Desktop | Windows exe VersionInfo | docs.docker.com |

---

## Stack version

A single `VERSION` file at repo root is the source of truth for the stack version.

- Read by bridge → served at `GET /api/version` → displayed in Settings footer
- Bumped by `scripts/bump_version.ps1` (manual) or GitHub Actions auto-version workflow (automatic on push)

**Auto-version rules (commit prefix):**

| Prefix | Bump |
|--------|------|
| `fix:` | patch (2.0.0 → 2.0.1) |
| `feat:` | minor (2.0.1 → 2.1.0) |
| `BREAKING CHANGE` | major (2.1.0 → 3.0.0) |
| `chore:`, `docs:`, etc. | no bump |

---

## Bridge API reference

All endpoints require `X-Session-Token` header (except auth routes).

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/auth/telegram` | Step 1 — verify Telegram identity |
| POST | `/api/auth/verify` | Step 2 — verify TOTP, issue session |
| GET | `/api/auth/status` | Check session validity |
| GET | `/api/auth/sessions` | List active sessions |
| DELETE | `/api/auth/sessions/<id>` | Revoke a session |
| GET | `/api/version` | Stack version from `VERSION` file |
| GET | `/api/status` | Home screen data |
| GET | `/api/services` | Service health (Watchdog or direct) |
| GET | `/api/memories` | List / search memories |
| POST | `/api/memories` | Store a memory |
| DELETE | `/api/memories/<id>` | Delete a memory |
| GET | `/api/reminders` | List reminders |
| POST | `/api/reminders` | Add a reminder |
| DELETE | `/api/reminders/<id>` | Cancel a reminder |
| GET | `/api/shortcuts` | List shortcuts |
| GET | `/api/models` | List models + active model |
| POST | `/api/models/active` | Switch active model |
| GET | `/api/openclaw/config` | Get OpenClaw config |
| POST | `/api/openclaw/config` | Update OpenClaw config |
| GET | `/api/openclaw/skills` | List all skills |
| POST | `/api/openclaw/skills` | Toggle skills enabled state |
| GET | `/api/openclaw/skills/<name>/credentials` | List credential keys for a skill |
| POST | `/api/openclaw/skills/<name>/credentials` | Set a skill credential |
| DELETE | `/api/openclaw/skills/<name>/credentials/<key>` | Delete a skill credential |
| GET | `/api/openclaw/doctor` | Run doctor checks |
| GET | `/api/updates` | Component version data |
| POST | `/api/updates/run` | Trigger a component update |
| POST | `/api/service/restart` | Restart a service |
| POST | `/api/stack/<action>` | start / stop / restart full stack |
| POST | `/api/inference/restart` | Restart Infer Bridge |
