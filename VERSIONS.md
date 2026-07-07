# Versioning

This monorepo uses **per-product VERSION files** with a roll-up tag at the repo root.

## Per-product (semver, independent)

| Product | File | Notes |
|---|---|---|
| Sentinel Finance | `portfolio-mcp/VERSION` | Mini App, Firefly mirror, cash forecast, balance sheet |
| Sentinel AI | `sentinel-miniapp-v2/VERSION` | Mini App for AI portal / Tailscale gate |
| Crib Watchdog | `watchdog/VERSION` | Power-spike monitor + gaming/inference classifier |

## Roll-up

| File | Meaning |
|---|---|
| `VERSION` (repo root) | Monorepo meta tag. Bumped when **any** product changes. Acts as commit-batch label only. |

## Bump rules

- **patch (x.y.Z)** — bug fixes, internal refactors, no UX change
- **minor (x.Y.0)** — new features, UI additions, backward-compatible
- **major (X.0.0)** — breaking changes (data migration, removed routes, etc.)

## Archive convention

When bumping a per-product **major or minor** version, snapshot the previous state into a sibling folder so both versions are visible without git commands:

```
portfolio-mcp/                       <-- live (v1.4.1)
portfolio-mcp-v1.0.0-archive/        <-- baseline (pre-2026-05-13)
```

Patch bumps don't get archived (would be too noisy).

Snapshot via:

```bash
mkdir -p portfolio-mcp-v1.0.0-archive
git ls-tree -r HEAD --name-only portfolio-mcp/ | while read f; do
  rel="${f#portfolio-mcp/}"; dst="portfolio-mcp-v1.0.0-archive/$rel"
  mkdir -p "$(dirname "$dst")"
  git show "HEAD:$f" > "$dst"
done
```

The archive folder is checked in so both versions are visible in the repo
without needing git commands.
