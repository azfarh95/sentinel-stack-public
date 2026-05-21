# LEGACY NOTICE — read before editing

**This directory is a stale local copy of Sentinel Finance YAML
configs.** The canonical source of truth is:

> https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance/tree/main/finance
> (local: `C:\Users\azfar\sentinel-finance\finance\`)

Migrated 2026-05-15. Docker bind-mount cutover **completed same day**:
the running `portfolio-mcp` container now mounts
`../sentinel-finance/finance/` as `/finance`. Edits made HERE are no
longer visible to the container. Preserved for git history per user
instruction. Safe to delete when ready.

## Where to edit now

- Only canonical path: `C:\Users\azfar\sentinel-finance\finance\`
- Edits there land in the live container's `/finance` mount on next
  reload — no copy-back needed.

## Files governed by both copies

`account_directory.yaml`, `balance_sheet_config.yaml`,
`bank_product_registry.yaml`, `classifier.yaml`, `funds.yaml`,
`liabilities-registry.yaml`, `parser_registry.yaml`, `recurring.yaml`,
`recurring_obligations.yaml`, `settings.yaml`, `ui_sot_registry.yaml`,
and `statement_schemas/`.

## Recovery references

- Pre-migration tarball: `C:\Users\azfar\_backups\metamcp-local-finance-aspects-2026-05-15-1559.tar.gz`
- Git tag (sentinel-stack): `legacy/finance-pre-migration-2026-05-15`
