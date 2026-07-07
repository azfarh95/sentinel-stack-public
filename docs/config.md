# Config & Secrets

---

## Config files

| File | Contents | Committed |
|------|----------|-----------|
| `config.json` | Mini App bot token, chat IDs, TOTP secret, mini app secret | No |
| `config.example.json` | Template with placeholder values | Yes |
| `.env.local` | Docker service secrets (DB password, auth secret, API keys) | No |
| `watchdog/config.json` | Watchdog owner chat ID, WSL distro, compose paths, alert settings | No |
| `watchdog/config.example.json` | Template with placeholder values | Yes |
| `shortcuts.json` | Prompt shortcuts shown in the Mini App | Yes |
| `workspace/SOUL.md` | OpenClaw system prompt | Yes |
| `workspace/TOOLS.md` | Tool routing rules (authoritative copy lives in WSL) | Yes |
| `VERSION` | Single source of truth for stack version | Yes |

---

## Secrets management

Secrets are stored in **Windows Credential Manager**, never in plaintext files.

### Initial setup / rotate a secret

```powershell
.\scripts\setup_secrets.ps1          # first run — prompts, saves to WCM, pushes PAT to GitHub
.\scripts\setup_secrets.ps1 -Force   # re-enter and rotate the stored token
```

The script stores credentials under the `sentinel-miniapp` service in Windows Credential Manager and uses `gh secret set` to push the GitHub Actions `PAT` secret.

### Credential lookup order (bridge.py)

For each secret the bridge tries in order:
1. Windows Credential Manager (`keyring.get_password`)
2. Environment variable
3. `config.json` field

### Skill credentials

Per-skill API tokens are stored separately under `sentinel-skill-{skill-name}` in Windows Credential Manager. Managed via the Mini App Settings → Skills → expand a skill row → credential panel.

Values are never returned to the browser — the UI only confirms whether a key is set.

---

## Version system

A single `VERSION` file at the repo root is the source of truth.

```
VERSION         ← e.g. "2.1.0"
```

### Manual bump

```powershell
.\scripts\bump_version.ps1 patch    # 2.1.0 → 2.1.1  (hotfix)
.\scripts\bump_version.ps1 minor    # 2.1.1 → 2.2.0  (feature release)
.\scripts\bump_version.ps1 major    # 2.2.0 → 3.0.0  (new generation)
```

The script reads `VERSION`, increments, writes back, then commits + tags + pushes.

### Auto-bump (GitHub Actions)

`.github/workflows/auto-version.yml` runs on every push to `master` and bumps automatically based on commit prefix:

| Prefix | Bump |
|--------|------|
| `fix:` | patch |
| `feat:` | minor |
| `BREAKING CHANGE` (in body) | major |
| `chore:`, `docs:`, `style:`, etc. | skip |

The bump commit itself (`chore: bump to vX.Y.Z`) is skipped to prevent infinite loops.

If a `PAT` secret is set in GitHub Actions, the tag push also triggers `docker-publish.yml`. Otherwise `GITHUB_TOKEN` is used (tag won't chain into docker-publish).

---

## OpenClaw config (`openclaw.json`)

Located at `\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\openclaw.json`.

Key paths used by the Mini App bridge:

| Setting | JSON path |
|---------|-----------|
| Active model | `agents.defaults.model.primary` |
| Reasoning effort | `agents.defaults.models.<primary>.reasoningEffort` |
| Max tokens | `models.providers.lmstudio.models[0].maxTokens` |
| Context tokens | `models.providers.lmstudio.models[0].contextTokens` |
| LM Studio timeout | `models.providers.lmstudio.timeoutSeconds` |
| Web search | `tools.web.search.enabled` |
| Web fetch | `tools.web.fetch.enabled` |
| Skills | `skills.entries.<name>.enabled` |

After writing changes, the bridge sends `SIGUSR1` to `openclaw-gateway.service` to hot-reload without a full restart.

### Reasoning effort levels

Valid values (model-dependent, confirmed for Qwen3):
`none` / `minimal` / `low` / `medium` / `high` / `xhigh`

---

## Docker compose files

| File | Services |
|------|----------|
| `docker-compose.local.yml` | MetaMCP, PostgreSQL, all MCP servers, LibreTranslate |

### `.env.local` fields

```
POSTGRES_PASSWORD=<choose a password>
BETTER_AUTH_SECRET=<random hex>
GITHUB_PAT=<your GitHub PAT>
```

---

## OpenClaw systemd (WSL2)

```bash
# Status
wsl -d Ubuntu-24.04 -u root -- systemctl status openclaw-gateway.service

# Restart
wsl -d Ubuntu-24.04 -u root -- systemctl restart openclaw-gateway.service

# Logs (last 50 lines)
wsl -d Ubuntu-24.04 -u root -- journalctl -u openclaw-gateway.service -n 50 --no-pager

# Hot-reload config (no restart)
wsl -d Ubuntu-24.04 -u root -- systemctl kill -s SIGUSR1 openclaw-gateway.service
```
