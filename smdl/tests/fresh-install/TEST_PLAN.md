# SMDL Fresh-Install Test

Goal: verify that a stranger with **only Docker + git** can clone the repo,
follow `smdl/README.md`, and reach a working bot — no hidden dependencies,
no undocumented env vars, no Windows-host quirks.

Test environment: a **fresh WSL2 Ubuntu** distro (not your daily-driver
`Ubuntu-24.04`). Spun up via `wsl --import`, torn down with `wsl --unregister`.

## What this catches

| Failure mode | How |
|---|---|
| Missing prereq in README | bootstrap script fails on `command not found` |
| Hidden env var | container starts but bot doesn't respond / health check fails |
| Bad volume mount path | downloads/cookies dir doesn't appear correctly inside container |
| ARM-vs-x86 incompatibility | (caught only on arm64 — see Hyper-V test plan if you want arm coverage) |
| Stale-image issue | docker pull from GHCR fails / wrong tag |
| Windows-line-ending breakage | bootstrap.sh fails with `bad interpreter: /bin/bash^M` |

## Test runs in two modes

1. **build-from-source mode** — the WSL clones the repo (or copies it from the
   Windows side) and runs `docker compose build`. Tests the Dockerfile.
2. **pull-from-GHCR mode** — after the build-publish workflow has pushed an
   image, the WSL pulls `ghcr.io/YOUR_GITHUB_USERNAME/smdl:latest`. Tests the publish path.

For V1 carve-out readiness, **mode 1 is the gate**. Mode 2 becomes possible
once the GitHub Actions workflow runs successfully on the public repo.

## Steps (orchestrated by run-wsl2-test.ps1)

1. Download Ubuntu rootfs tarball (cached after first run, ~70 MB)
2. `wsl --import smdl-test ...` — create the clean distro
3. Run `bootstrap.sh` inside: install docker.io, git, ffmpeg
4. Copy or clone the SMDL source into `/tmp/smdl-test/smdl/`
5. Drop a minimal `.env.test` with the test bot token (or skip env, just verify build)
6. `docker compose -f docker-compose.test.yml up --build -d`
7. Poll `http://127.0.0.1:8096/health` until 200 OK (or 60s timeout)
8. Optional: send a test URL to the test bot, verify it responds
9. `docker compose down -v` to clean up
10. `wsl --unregister smdl-test`

## Acceptance criteria

- Steps 1-7 complete with no manual intervention
- Health endpoint returns `{"status":"ok","service":"sm-dl"}`
- No errors in `docker logs smdl`
- Bootstrap script doesn't fail on missing prereqs documented in README

## What's NOT tested

- Long-lived recording (live streams) — that's a soak-test concern, not install
- Telethon upload path — requires real API_ID/API_HASH
- Cloudflare-tunnel ingress — host-specific
- The private plugins (cam_sites.py, stripchat_extractor.py) — by design,
  fresh-install test ships the PUBLIC scope only

## Running the test

```powershell
.\smdl\tests\fresh-install\run-wsl2-test.ps1
```

Or step-through (for debugging):
```powershell
.\smdl\tests\fresh-install\run-wsl2-test.ps1 -KeepDistro -Verbose
# … investigate inside the distro …
wsl -d smdl-test
# … then clean up manually:
wsl --unregister smdl-test
```
