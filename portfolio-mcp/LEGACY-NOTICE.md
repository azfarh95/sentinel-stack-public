# LEGACY NOTICE — read before editing

**This directory is a stale local copy of Sentinel Finance code.**
**The canonical source of truth is:**

> https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance
> (local: `C:\Users\azfar\sentinel-finance\`)

Migrated 2026-05-15. Docker build cutover **completed same day**:
`docker-compose.yml` now points `build.context` at `../sentinel-finance/`
and bind-mounts `../sentinel-finance/finance/` as `/finance`. This
directory is no longer read or executed by anything. Preserved for
git history / browsability per user instruction. Safe to delete when
ready (after confirming 1-2 weeks of stable operation).

## Where to go now

- **Code edits** → `C:\Users\azfar\sentinel-finance\app\`
- **YAML registries** → `C:\Users\azfar\sentinel-finance\finance\`
- **Audit journals / pass-N prompts** → `C:\Users\azfar\sentinel-finance\journal\`
- **Issue tracker** → https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance/issues

## Recovery / archive references

- Pre-migration tarball: `C:\Users\azfar\_backups\metamcp-local-finance-aspects-2026-05-15-1559.tar.gz`
- Git tag (sentinel-stack): `legacy/finance-pre-migration-2026-05-15`
- Pre-migration commit: `e24c69c` on sentinel-stack master

## What's still here vs in sentinel-finance

| Path | Live? | Notes |
|------|-------|-------|
| `app/*.py` | ⚠️ frozen-with-edits | Edit at sentinel-finance/app/ instead. Docker container reads from here until cutover. |
| `static/sw.js` | ⚠️ frozen-with-edits | Same; v2.27 SW v16 bump is here AND in sentinel-finance. |
| `finance/*.yaml` | ⚠️ frozen | Edit at sentinel-finance/finance/ instead. |
| `docs/` | frozen | Newer docs (V2-SEAL, V3-ROADMAP) only in sentinel-finance. |

## Cutover history (all complete)

- ✅ 2026-05-15: code synced to `YOUR_GITHUB_USERNAME/sentinel-finance` (commit `532e3fe`)
- ✅ 2026-05-15: scripts + v1.0.0 archive synced (commit `06f7633`)
- ✅ 2026-05-15: LEGACY-NOTICE.md markers placed in metamcp-local copies (sentinel-stack `c581ce9`)
- ✅ 2026-05-15: docker-compose.yml build context cutover; container recreated from sentinel-finance image; v2.28 verified live

This directory is now archive-only. Safe to remove when convenient.
