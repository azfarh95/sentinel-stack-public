# Sentinel Finance — code

> **Personal-finance Mini App on top of Firefly III. IAS 1 balance sheet, accrual-aware income statement, 90-day cash forecast, classifier-driven reconciliation, multi-chain crypto + ILP/CPF integration. Telegram-authenticated, self-hosted via TWA + Cloudflare Tunnel.**

This directory holds the **runtime code** for Sentinel Finance, deployed as the `portfolio-mcp` container (FastMCP + Starlette + Python 3.12).

**Strategy + roadmap + design docs**: canonical home is the public **[sentinel-finance docs repo](https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance)**. Local copies of those `.md` files in this folder are now stub-redirects.

---

## Quick facts

| | |
|---|---|
| Version | see [`VERSION`](VERSION) |
| Changelog | [`CHANGELOG.md`](CHANGELOG.md) (per-version notes for v1.0.0 → v1.9.17) |
| License | [`LICENSE`](LICENSE) — FSL-1.1-Apache-2.0 (auto-converts to Apache 2.0 after 2 years per version) |
| Container port | 8086 |
| Mini App URL | https://sentinelfinance.your-domain.example.com (→ sentinelfinance.app at v4 launch) |
| Auth | Telegram Login Widget + TOTP |
| Ledger | Firefly III v6.6.2 :8180 (retiring at v2 → SentinelLite SQLite) |
| Integrations | Wise, Moralis, Krystal, WolfSwap RPC, DexScreener, Morningstar SG, Google Calendar, Telegram bots |

## Where docs live

| Topic | Location |
|---|---|
| **Roadmap** (phased v1→v4 plan) | https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance/blob/main/roadmap.md |
| **Architecture** (current diagram) | https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance/blob/main/docs/architecture.md |
| **v1.9.x backlog** | https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance/blob/main/BACKLOG.md |
| **v2 productize plan** | https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance/blob/main/V2-SCOPE.md |
| **v3 AI copilot** | https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance/blob/main/V4-AI-COPILOT.md |
| **v3.5 AI document extractor** | https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance/blob/main/V4-AI-DOCUMENT-EXTRACTOR.md |
| **v4 multi-tenant SaaS** | https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance/blob/main/V3-DISTRIBUTION.md |
| **License policy + deps** | https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance/blob/main/LICENSING.md |
| **Privacy + data inventory** | https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance/blob/main/PRIVACY.md |
| **Pricing tiers** | https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance/blob/main/MONETIZATION.md |
| **Singpass feasibility** | https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance/blob/main/SINGPASS.md |
| **Domain migration** | https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance/blob/main/DOMAIN-MIGRATION.md |
| **Wallet backend (Moralis →composable)** | https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance/blob/main/WALLET-BACKEND.md |
| **Ledger decision (Firefly → SentinelLite)** | https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance/blob/main/LEDGER-DECISION.md |

## Local dev

```powershell
docker compose --env-file .env.local --profile finance up -d portfolio-mcp
docker logs -f portfolio-mcp
docker exec portfolio-mcp pytest
```

Secrets live in Windows Credential Manager. `scripts/sync_env_from_wcm.ps1` regenerates `.env.local` from the template at boot.

## Repo conventions

- **Versioning**: per-product semver in [`VERSION`](VERSION); repo roll-up in `/VERSION` (monorepo root). See [VERSIONS.md](../VERSIONS.md).
- **Archive**: major/minor bumps snapshot the previous tree into `portfolio-mcp-v{X.Y.0}-archive/` so both versions are browsable.
- **Privacy**: every amount-displaying class (`.amt .big .v .usd .bal .subtotal`) is covered by the global blur toggle.
- **Commits**: DCO sign-off (`git commit -s`) — like Linux kernel. No CLA.
