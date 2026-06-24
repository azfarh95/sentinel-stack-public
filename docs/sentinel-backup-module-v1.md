# Sentinel backup module — design

**Status**: draft (Phase 0 — design doc, no code yet)
**Owner**: azfar
**Last updated**: 2026-05-27
**Related**: `sentinel-watchdog/sentinel_secrets/`, `metamcp-local/docs/auth-perms-v2.md`

Lock-in document for `Backup-SentinelPillar` / `Restore-SentinelPillar` —
a PowerShell module that captures a pillar's complete state into a single
`.bak` file and restores it onto a fresh host. The first concrete step
toward "Sentinel as a portable, redistributable stack" (see
`workspace/proposals/2026-05-09-V6-prep-Hardcoded-Paths.md` for the
broader portability conversation that motivated this).

Future implementation phases (v0.1 → v1.0) must conform to the contracts
defined here. Deviations require editing this doc first, with a
`# Changed:` callout in §14.

---

## 1. Goals

1. **One file per pillar.** `finance.bak` captures everything needed to
   reconstruct Sentinel Finance on another machine. No "and also copy
   these other 14 files."
2. **Layer coverage.** A `.bak` includes:
   - bind-mounted data dirs (anything inside the repo's `data/`)
   - Docker named volumes (the mutable state outside the repo)
   - WCM credentials scoped to the pillar
   - Task Scheduler entries that reference the pillar
   - declarative cloud-state (CF tunnel routes, DNS, Access policies)
   - **not** source code — that's in GitHub
3. **Idempotent restore.** Running `Restore-SentinelPillar` twice in a
   row is a no-op the second time. Detect existing state, skip what's
   already correct.
4. **Cross-host friendly.** Backup on laptop, restore on desktop, both
   Windows. Paths in the backup are relative or template-based; absolute
   paths get rewritten on restore.
5. **Owner-only.** No multi-tenant, no public distribution of `.bak`
   files. Same trust model as the rest of the Sentinel stack.
6. **Encrypted at rest.** The `.bak` is AES-256-GCM-encrypted with a
   user-supplied passphrase. Plain-tarball mode is also supported for
   trusted local backups.
7. **Backup is fast; restore takes whatever it takes.** Capture should
   complete in <2 minutes per pillar for small ones, <10 minutes for the
   heavy ones (Finance has ~28 MB of state, SMDL has GBs of recordings).

## 2. Non-goals

- **Full-host bare-metal restore.** This module restores INDIVIDUAL
  PILLARS onto an existing Sentinel host. Bare-metal install is a
  separate "Sentinel Suite Setup" project (covers Docker Desktop, WSL,
  Cloudflared, Python, all the runtime prereqs).
- **Live / hot backup.** Pillar's containers are stopped during capture
  to avoid mid-write corruption. Brief downtime is expected and
  acceptable for a homelab.
- **Diff / incremental backups.** v1 captures the full state every time.
  Storage is cheap. If `.bak` files grow uncomfortably, v2 can layer
  on `--since-last`.
- **Replication / continuous sync.** This is a scheduled-snapshot tool,
  not a real-time mirror. For real-time, use Tailscale + native db
  replication.
- **Cloud-side state mutation on restore.** Restore can read the CF
  tunnel manifest from the backup but won't auto-recreate the routes
  via CF API (those API tokens are scary). Restore generates a checklist
  for the operator.

## 3. Architecture overview

```
┌─ Backup-SentinelPillar -Name <pillar> ─────────────────────────────┐
│                                                                     │
│  1. Read manifests/<pillar>.yaml                                    │
│  2. Stop pillar's containers (docker compose stop <services>)       │
│  3. For each layer in the manifest:                                 │
│       data    → tar bind-mount dirs                                 │
│       volumes → docker run --rm -v <vol>:/s busybox tar /s          │
│       wcm     → cmdkey /list + secret-fetch via sentinel-secrets    │
│       tasks   → schtasks /Query /XML for matching task names        │
│       cloud   → curl CF API for tunnel routes + Access policies     │
│  4. Write each layer into a temp dir                                │
│  5. Generate manifest.json (versions, hashes, captured-at)          │
│  6. Restart containers                                              │
│  7. Tar the temp dir → AES-256-GCM encrypt → .bak                   │
│  8. Print summary + bak path                                        │
└─────────────────────────────────────────────────────────────────────┘

┌─ Restore-SentinelPillar -InFile <pillar>.bak -DryRun ──────────────┐
│                                                                     │
│  1. Decrypt + extract                                               │
│  2. Read manifest.json                                              │
│  3. Verify host capabilities (Docker running? secrets store?)       │
│  4. For each layer:                                                 │
│       (dry-run mode prints what would happen, doesn't mutate)       │
│       data    → restore tar into bind-mount dir                     │
│       volumes → docker volume create + tar restore                  │
│       wcm     → cmdkey /add (idempotent — overwrite-if-exists)      │
│       tasks   → schtasks /Create /XML (idempotent — replace)        │
│       cloud   → emit checklist for operator to action manually      │
│  5. Print restore summary + checklist                               │
└─────────────────────────────────────────────────────────────────────┘
```

## 4. `.bak` file format

A `.bak` is a single file with one of two shapes:

### 4.1. Plaintext (trusted local)

```
<header bytes>
<tarball body>
```

Header:
```
SENTBAK\0   8 bytes magic
v1\0        3 bytes version  (right-padded to 8)
<json>      256-byte JSON header (UTF-8, NUL-padded)
            {"pillar":"finance","captured_at":"…","encrypted":false,
             "host":"DESKTOP-XYZ","gz":true}
```

Body: gzip'd tar.

### 4.2. Encrypted (default for `.bak`)

```
<header bytes>          (same as above, with "encrypted":true)
<salt — 16 bytes>
<iv — 12 bytes>
<aes-256-gcm ciphertext + tag>
```

Key derivation: PBKDF2-HMAC-SHA256 over passphrase + salt, 600k iterations.

### 4.3. Tar internal layout

```
<pillar>.bak/
├── manifest.json
├── data/
│   └── <subpath>          # mirrors the bind-mount directory structure
├── volumes/
│   └── <volname>.tar      # one tar per Docker named volume
├── wcm/
│   └── credentials.json   # encrypted-at-rest WCM dump (the dump itself
│                           # is base64-encoded ciphertext, on top of
│                           # the outer .bak encryption — defense in depth)
├── tasks/
│   └── <taskname>.xml     # one XML per Task Scheduler entry
└── cloud-state.yaml       # declarative: tunnel routes, DNS, Access policies
```

`manifest.json`:
```json
{
  "version": "v1",
  "pillar": "finance",
  "captured_at": "2026-05-27T22:14:32Z",
  "captured_by": "DESKTOP-XYZ / azfar",
  "manifest_version": 1,
  "layers": {
    "data":     {"size_bytes": 28734592, "sha256": "…"},
    "volumes":  {"items": ["metamcp-local_portfolio_mcp_data"], "size_bytes": 14882304},
    "wcm":      {"items": 11, "size_bytes": 1894},
    "tasks":    {"items": 1, "names": ["Portfolio Daily Snapshot"]},
    "cloud":    {"routes": 1, "dns": 1, "access_policies": 1}
  },
  "smoke_check": {
    "compose_health_endpoint": "/health",
    "expect_status": 200
  }
}
```

## 5. Per-pillar manifest schema

Each pillar that wants to be backup-able ships a manifest at
`<pillar>/sentinel-backup.yaml`:

```yaml
# sentinel-finance/sentinel-backup.yaml
schema_version: 1
pillar: finance
display_name: Sentinel Finance

compose:
  file: ../metamcp-local/docker-compose.yml          # relative to manifest
  services:                                           # services to stop before capture
    - portfolio-mcp
    - firefly
    - firefly-importer
    - firefly-db

data:                                                 # bind-mounted dirs to include
  - path: ../sentinel-finance/finance
    in_bak: finance-source
    notes: Active finance data dir; mutates on every parse run
  - path: ../metamcp-local/google-workspace-mcp/data
    in_bak: gws-data
    optional: true

volumes:                                              # Docker named volumes
  - name: metamcp-local_portfolio_mcp_data
  - name: metamcp-local_firefly_db
  - name: metamcp-local_firefly_upload

wcm:                                                  # credentials scoped to this pillar
  patterns:
    - "wise_api_token@sentinel-miniapp"
    - "endowus_*@sentinel-miniapp"
    - "moralis_api_key@sentinel-miniapp"
    - "etherscan_api_key@sentinel-miniapp"
  notes: Anything tagged @sentinel-miniapp that's finance-relevant

tasks:                                                # TS entries that drive this pillar
  - name: "Portfolio Daily Snapshot"
  - name: "Firefly Auto Import"

cloud:                                                # declarative cloud-side state
  cf_tunnel_routes:
    - hostname: sentinelfinance.your-domain.example.com
      service: http://localhost:8086
    - hostname: firefly.your-domain.example.com
      service: http://localhost:8180
  cf_access_apps: []                                  # finance has no Access wall yet
  dns: []                                             # CF tunnel manages these

smoke_check:                                          # quick post-restore verification
  command: 'curl -fsS http://localhost:8086/health'
  timeout_sec: 30
```

Every pillar's manifest gets committed to its own repo. The backup
module reads it on demand.

## 6. PowerShell module API

Module name: `Sentinel.Backup`. Lives at
`metamcp-local/scripts/sentinel-backup/` and gets imported via
`Import-Module .\sentinel-backup\Sentinel.Backup.psd1`.

### 6.1. `Backup-SentinelPillar`

```powershell
Backup-SentinelPillar
  -Name             <string>            # pillar name (finance, smdl, gaming, …)
  -OutFile          <path>              # destination .bak path
  [-Passphrase      <secure-string>]    # if omitted, prompts; if -Plain, omits
  [-Plain           ]                   # disable encryption (local-only backups)
  [-SkipVolumes     ]                   # skip docker-volume tar pass (faster)
  [-SkipCloud       ]                   # skip CF API queries
  [-StopServices    [bool]]             # default $true; -StopServices:$false = hot capture
```

Returns: PSCustomObject with `OutFile`, `SizeBytes`, `CapturedAt`, `Layers` map.

### 6.2. `Restore-SentinelPillar`

```powershell
Restore-SentinelPillar
  -InFile           <path>              # .bak file to restore
  [-Passphrase      <secure-string>]    # required if file is encrypted
  [-DryRun          ]                   # print what would happen, mutate nothing
  [-Force           ]                   # overwrite existing state without prompting
  [-Layers          <string[]>]         # restore subset: data, volumes, wcm, tasks
                                        # default: all except cloud (cloud is checklist-only)
  [-TargetCompose   <path>]             # override compose-file location
```

Returns: PSCustomObject with `Restored`, `Skipped`, `Failed`, `ManualSteps`.

### 6.3. `Test-SentinelBackup`

```powershell
Test-SentinelBackup
  -InFile           <path>
  [-Passphrase      <secure-string>]
```

Decrypts + extracts to a temp dir, validates manifest, hashes layer
contents, prints a report. Doesn't mutate the host. Useful for "is this
2-week-old backup still readable?"

### 6.4. `Get-SentinelBackupInfo`

```powershell
Get-SentinelBackupInfo -InFile <path>
```

Reads ONLY the file header (no decryption needed). Returns pillar,
captured-at, host, encryption status, manifest-version. Cheap.

## 7. Capture order + safety

For each pillar, the capture proceeds layer-by-layer with this ordering
to minimize live-write risk:

1. **Stop containers** in the pillar's compose `services:` list
2. **Wait 3s** (let in-flight writes flush)
3. **Capture in order**: data → volumes → wcm → tasks → cloud
   - Each layer writes to a temp dir under `%TEMP%\sentinel-backup-<pid>\`
4. **Restart containers** (whether capture succeeded or failed —
   never leave services down)
5. **Tar + encrypt + move to `-OutFile`**
6. **Delete temp dir**

Failure semantics:
- Any layer failure aborts the capture entirely and restarts containers.
- The temp dir is preserved on failure (for forensics) but doesn't
  become a `.bak`.
- Operator can retry; restart is safe.

## 8. Restore safety + idempotency

The restore is intentionally conservative:

| Layer | Idempotency mechanism |
|---|---|
| `data` | Existing files: rename to `.pre-restore-<ts>` then write new. -Force overwrites. |
| `volumes` | If volume exists with content: error unless -Force. -Force = drop volume + recreate. |
| `wcm` | `cmdkey /add` overwrites existing target. Always safe to re-run. |
| `tasks` | `schtasks /Create /XML /F` replaces existing task with same name. Always safe. |
| `cloud` | Read-only — emits checklist. Operator manually verifies / applies. |

Pre-flight checks before mutating:
- Docker daemon running?
- WCM accessible? (Test: `cmdkey /list` returns non-empty)
- Task Scheduler service running?
- Required Docker images present? (Pull if missing — manifest names them.)

## 9. Cloud-state handling (intentionally manual)

The `cloud-state.yaml` in the backup is **declarative**. Restore:

1. Compares the captured state to the target host's current CF tunnel
   routes + DNS + Access apps.
2. Prints a diff: `+ ROUTE ADD sentinelfinance.your-domain.example.com → :8086`
3. **Does NOT** call the CF API to apply.
4. Operator decides: apply the diff manually in CF dashboard, OR run
   a separate command later (`Restore-SentinelCloud -InFile … -Apply`)
   that does the API calls — but that command requires a CF API token
   with `Tunnels:Edit + DNS:Edit + Access:Edit` scopes, and the operator
   passes it explicitly.

Rationale: cloud state is harder to roll back than local state. A bug
in the diff applier could nuke production tunnels. Worth the friction.

## 10. Security considerations

- **Default encryption.** A `.bak` written without `-Plain` is
  AES-256-GCM. Passphrase is prompted; not stored anywhere by the
  module. Forgotten passphrase = unrecoverable backup. This is intentional.
- **Plaintext mode.** `-Plain` is only for backups going straight into
  WCM-protected vault dirs or BitLocker volumes. Module warns when
  invoked without encryption.
- **WCM dump format.** Inside the encrypted backup, the WCM credentials
  themselves are STILL ciphertext (DPAPI-protected by Windows). On
  restore, DPAPI re-protects to the target host's user. Defense in
  depth.
- **No telemetry.** Module emits a local audit row in
  `metamcp-local/scripts/sentinel-backup/audit.log`. No network calls
  on capture (except CF API for cloud-state, which is opt-in).
- **Restore from cross-host.** If a `.bak` was made on Host A and
  restored on Host B, the WCM DPAPI step re-protects under Host B's
  user. Same passphrase works on both hosts for the outer envelope.

## 11. Phased delivery

| Phase | Effort | Deliverable |
|---|---|---|
| v0.1 | 6h | Module skeleton + Backup/Restore for `finance` only. Layers: data + volumes + wcm. No cloud, no encryption, no `-DryRun`. End-to-end test on this host. |
| v0.2 | 4h | Add encryption (AES-256-GCM + PBKDF2). Add `-DryRun` to restore. Add `Test-SentinelBackup`. |
| v0.3 | 3h | Add `tasks` layer. Add per-pillar manifest schema + sample for `finance`. |
| v0.4 | 4h | Add `cloud` layer (CF API queries) + checklist emitter. |
| v0.5 | 6h | Write manifests for all 11 pillars. Smoke-test backup/restore on each. |
| v0.6 | 3h | Cross-host restore test: backup on this machine → restore in a fresh Hyper-V Windows VM. |
| v1.0 | 2h | Documentation, audit-log polish, MSI/NSIS-wrapped install for the module. |

Total: ~28 hours of focused work. Spread across however many sessions.

## 12. Open questions

1. **What's a pillar?** This doc treats "pillar" as a unit corresponding
   to a Sentinel sub-project (finance, smdl, gaming, watchdog, etc.).
   But some pillars have multiple compose services and some share state
   with siblings (e.g., portfolio-mcp's volume is named
   `metamcp-local_portfolio_mcp_data` — owned by the stack-level
   compose, not the finance repo). The manifest schema explicitly lists
   which volumes belong to which pillar; if a volume is referenced by
   two manifests, both backups include it (the restore is idempotent so
   no harm).

2. **What about cross-pillar coupling?** Restoring `finance` without
   first restoring the stack-level `metamcp_local_postgres_data` would
   leave finance pointing at a missing DB. Manifest's `requires:`
   field (future): list other pillar manifests that must be restored
   first.

3. **GitHub state.** The backup intentionally excludes source code
   because GitHub is the source of truth. But if GitHub is down at
   restore time, restore needs source. Future option: `-IncludeSource`
   bundles a `git bundle` per pillar into the .bak.

4. **OpenClaw in WSL.** OpenClaw lives in the Ubuntu-24.04 WSL distro,
   not in any Sentinel pillar. Out of scope for this module — covered
   by `wsl --export` separately. Future: a `Sentinel.Backup.WSL`
   companion module for WSL distros.

5. **Backup of the backup module itself.** The module is in
   `metamcp-local/scripts/sentinel-backup/` and committed to GitHub
   under sentinel-stack. No special bootstrap needed.

## 13. Decision log

| Date | Decision | Rationale |
|---|---|---|
| 2026-05-27 | PowerShell module, not Python script | Native Windows access (WCM, Task Scheduler, Docker Desktop CLI). No interpreter prereq on a fresh box (PS is built-in). |
| 2026-05-27 | One .bak per pillar, not one mega-backup | Encourages per-pillar restore; mega-backup encourages "restore everything" which is rarely what you want. |
| 2026-05-27 | Encryption default, plain opt-in | A .bak likely sits in cloud storage at some point. Default-encrypt protects against the lazy-future-self. |
| 2026-05-27 | Cloud state is checklist-only on restore | CF API mutations are scary; manual review = sleep better. |
| 2026-05-27 | Restart containers in finally{} | If capture crashes, services come back. Never leave the stack down. |

## 14. Changelog

- 2026-05-27 — initial draft (Phase 0). azfar.
- 2026-05-27 — v0.2 shipped. AES-256-GCM encryption default (PBKDF2-HMAC-SHA256,
  600k iterations, salt+iv+tag layout per §4.2). Real `Restore-SentinelPillar`
  (data: rename-aside-then-untar with `.pre-restore-<ts>`; volumes:
  create-or-`-Force`-drop-then-extract; wcm: CredWrite with DPAPI re-wrap).
  `-DryRun` is a real switch. Pre-flight checks (Docker daemon, WCM API).
  Compose services stop/restart in `finally{}` around data/volumes restore.
  `sentinel-shopping/sentinel-backup.json` written as first real-pillar manifest.

  # Changed: WCM inner-DPAPI double-wrap (spec §10) was simplified to plaintext-
  inside-outer-AES. Original spec called for keeping DPAPI ciphertext as defense
  in depth, but DPAPI ciphertext is user+machine-keyed and can't be unwrapped on
  a different host, which breaks the cross-host restore requirement (§1.4).
  v0.2 stores raw credential bytes inside the outer-AES-encrypted .bak; restore
  calls `CredWrite` which DPAPI-wraps under the target host's user. Cross-host
  restore works as designed.

  # Changed: PowerShell version requirement bumped 5.1 → 7.0. AesGcm is .NET
  Core 3.0+ only.

  # Deferred: `Test-SentinelBackup` does not yet decrypt encrypted .bak. Use
  `Get-SentinelBackupInfo` (header-only, no decryption) or `Restore-SentinelPillar
  -DryRun` (full decrypt + extract) for now. Decryption support for Test-* in v0.3.

---

**Sign-off**: this is the contract. Anything code-side that diverges
from this needs an edit here first, with a `# Changed:` callout when
we revisit.
