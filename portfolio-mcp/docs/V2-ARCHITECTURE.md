# Sentinel Finance — V2 Architecture

Status: V2 milestone (32/32 invariants pass). Audit-3 → audit-8 closed.

## What V2 guarantees

1. **Single Source of Truth for every account.** Every balance rendered on
   the dashboard, in the agent API, or in the income statement resolves
   through one function: `account_balance.resolve()`. There is no second
   path. There is no silent fallback.
2. **Structural dedup at write-time.** No write to `journals` can silently
   create a duplicate of an existing non-voided journal. Enforced by a
   partial UNIQUE index on `journals(external_id) WHERE status != 'voided'`
   plus a canonical external_id format validated at post-time.
3. **Drift surfaces, never hides.** Statement vs GL mismatches land in
   `unreconciled_queue` with T1/T2/T3 classification. Never auto-resolved
   to a P&L bucket. User explicitly triages with one of four noise
   categories.
4. **Behavioural alerts are off the hot path.** A separate `alerts` table
   + daily scan job surfaces anomalies (stale statement, missing recurring,
   snapshot value drop). Gates 1–5 stay deterministic.

## The 5 gates

```
                       Sentinel Finance — Gate architecture
                       ════════════════════════════════════

  PARSER                   ┌─────────────────────────────────────┐
  PDF / API → cutover ────►│ Gate 1: Opening anchor required     │
                           │ Gate 2: Verifier-mandatory          │
                           │ Gate 3: bank_statement_registry     │
                           │ Gate 4: Period reconciliation       │
                           └──────────────────┬──────────────────┘
                                              │ posted journal
                                              ▼
                           ┌─────────────────────────────────────┐
                           │ general_ledger (Dr=Cr enforced)     │
                           │ + journals (UNIQUE external_id)     │
                           └──────────────────┬──────────────────┘
                                              │
  RENDER                                      ▼
  Dashboard ◄─────────────  Gate 5: resolve() — single resolver
  Income stmt ◄───────────  by anchor_class (A | B | C)
  Agent API ◄─────────────
                                    A → statement_cf (bank PDFs)
                                    B → account_snapshot (Coinbase/Wise)
                                    C → GL sum (CPF, ILP — snapshot anchored)
```

### Gate 1 — Opening anchor required

No journal may post to an ASSET / LIABILITY / EQUITY account unless an
`account_opening_anchor` row exists with `opening_date <= journal_date`.
Exception: the opening anchor itself (journal_type='opening').

Enforced by: `journal_service.post_journal()` raises `OpeningAnchorRequired`.

### Gate 2 — Verifier mandatory

All cutover paths must go through `verifier.verify()`. The verifier
matches each candidate journal against canonical registries (insurance,
subscription, recurring obligation, account directory) and produces a
confidence score. Low-confidence candidates land in `unreconciled_queue`
instead of `journals`.

### Gate 3 — `bank_statement_registry`

Every parsed bank PDF writes its BF and CF to this table. Unique by
(account_code, period_end). The resolver reads `MAX(period_end).CF` for
Class A accounts.

### Gate 4 — Period reconciliation

`reconcile_period(account_code, period_end)` compares:
* statement CF (Gate 3)
* GL projection from the opening anchor

Mismatch beyond tolerance → drift row in `unreconciled_queue` with
`tx_type='PERIOD_DRIFT'`. Classified into T1 (pre-opening), T2
(unresolvable), or T3 (fixable) by `classify_drift()`.

### Gate 5 — `account_balance.resolve()`

The single SoT resolver. Dispatches by anchor_class via `RESOLVER_REGISTRY`:

| anchor_class | resolver | source |
| --- | --- | --- |
| A | `_resolve_class_a` | `bank_statement_registry.CF` |
| B | `_resolve_class_b` | `account_snapshot` (no GL fallback) |
| C | `_resolve_class_c` | GL sum since last re-anchor journal |
| _none_ | strict=True raises | (debug-only path via `resolve_debug`) |

Sources include the Class-A "stale_statement" and "gl_projection"
secondary outcomes, each rendered with a visible badge.

## Data flow contracts

### external_id

Format: `<source>:v<n>:<stable_key>`

* `<source>` — one of `EXTERNAL_ID_SOURCES` allowlist (posb, sc, cc_stmt, …)
* `<n>` — integer schema version (≥ 1)
* `<stable_key>` — deterministic over immutable business keys (source_doc
  + period_end + line_index + amount + date). Never hash mutable content
  (classification result, narration, router output).

Validation: `validate_external_id()`. Enforced at write time as a warning
(post_journal logs but doesn't block) and as inv19f for journals
created on/after `EXTERNAL_ID_ENFORCED_FROM` (2026-05-16).

### account_snapshot

One row per periodic API sample for Class B accounts.

| column | use |
| --- | --- |
| source_type | `cex` / `bank_api` / `defi_api` |
| provider | `coinbase` / `wise` / `revolut` / ... |
| captured_at | when the API call succeeded |
| sgd_value | the resolver's read |
| raw_currency, raw_amount | single-currency source (e.g. Coinbase USD) |
| raw_currencies (JSON) | multi-currency aggregator (e.g. Wise) |
| raw_response | full provider response, audit blob |

For V2: Wise/Coinbase sum to SGD using a single fx rate per refresh. V3
will introduce FRS-21 dual journals using the raw_currency / raw_currencies
fields. The columns exist now so no data is lost in the meantime.

### counter_account_map

Cross-account corridors that classify_drift uses to decide fixable vs
unresolvable. Schema: `src_account_code, dst_account_code, relation_type,
active_from, active_to, notes`.

Constraint: UNIQUE(src, dst, relation_type); CHECK(src != dst). For
`bank_peer` corridors, dst must be Class A. For `wallet_bridge` corridors,
dst may be A or B (cex_snapshot will provide overlap evidence in V3).

## The 32 invariants

Catalogued in [V2-INVARIANTS.md](./V2-INVARIANTS.md).

## Scheduler jobs

| Job | Cadence | Purpose |
| --- | --- | --- |
| `onchain_poll` | 5 min | Moralis wallet polling, Telegram alerts |
| `manual_price_refresh` | 60 min | WolfSwap + DexScreener price refresh |
| `wise_sync` | daily 06:30 | Legacy wise GL anchor (deprecate when Wise FX work lands) |
| `coinbase_snapshot` | 15 min | Class B writer for 1231 |
| `wise_snapshot` | 60 min | Class B writer for 1113 |
| `daily_backup` | daily 02:00 | YAML + SQLite backup |
| `onedrive_watcher` | 60 min | New PDF auto-classifier |
| `nw_snapshot` | daily 02:30 | networth_history row |
| `morningstar_nav` | daily 06:00 | ILP NAV scraper |
| `firefly_bridge` | daily 07:00 | Legacy bridge (deprecated post-V2) |
| `drift_nudge` | monthly 09:00 SGT day 1 | Telegram nudge on untriaged T2s |
| `alerts_scan` | daily 03:00 SGT | Behavioural anomaly detection |

## Routes

| Route | Purpose |
| --- | --- |
| `/` | Home glance (8 cards, customisable) |
| `/balance_sheet` | IAS 1 nested tree, badges for stale/projection sources |
| `/income_statement` | YTD + per-month, GL-backed |
| `/cash_forecast` | 90-day projection |
| `/reconcile` | Drift queue + verifier queue, T1/T2/T3 badges, triage form |
| `/facilities` | Credit facility registry |
| `/policies` | Insurance + ILP policy registry |
| `/alerts` | Behavioural alerts feed, resolve/dismiss |
| `/admin/users` | Owner/admin user management |
| `/admin/classifier` | Counterparty triage |

## V2 design decisions

Documented in [V3-SCOPE.md](./V3-SCOPE.md):
* FX P&L treatment deferred (V2 = SGD snapshot only)
* Insurance cash-value accrual deferred (V2 = full premium as expense)
* 48xx "Unrealised Gains" REVENUE bucket deferred (V2 = anchor delta → 3100)
* Spend-spike alert detector deferred (V2 = 3 detectors)
* Reclass-stability invariant deferred (V2 = voided-exclusion only)
