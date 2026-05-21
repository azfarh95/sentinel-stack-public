# Sentinel Finance — V2 Behavioural Commitments

The "social contracts" behind every number Sentinel Finance shows. These
are deliberate choices, not implementation details. They affect how YTD,
materiality, and reconciliation are interpreted across the system.

## Basis

**Sentinel Finance is accrual-basis.**

Every P&L slice (year, month, custom range) uses `journals.journal_date`
to determine inclusion. Not `tx_date` (which may be the posting day on
the bank statement vs the actual transaction day). Not `detected_at`
(when our parser saw the row). Not `created_at` (when the journal landed
in the GL).

Implication: backdated journals affect prior periods. A journal entered
today with `journal_date='2026-03-15'` retroactively appears in Q1 2026
reporting, even though the GL row was created in Q2.

Enforced by: inv27 (period-cutoff invariant on /income_statement).

## Period definitions

* **Fiscal year:** calendar year. Sentinel's reports use Jan 1 – Dec 31
  of the year argument.
* **Month bounds:** inclusive on both ends. April 2026 = 2026-04-01 to
  2026-04-30 inclusive.
* **YTD:** Jan 1 of the current year through today, inclusive.

Enforced by: `income_statement._month_bounds()` + inv27.

## Materiality

| Surface | Threshold | Action below threshold |
| --- | --- | --- |
| Reconciliation drift (Gate 4) | $0.01 | Auto-reconciled; no queue row. |
| Income-statement aggregation (inv25-28) | $0.50 | Within tolerance for renderer-vs-direct comparison. |
| Closing identity (inv26) | $0.05 | net_income may differ from inc-exp by ≤ 5 cents. |
| P&L sign sanity (inv22) | $1.00 | Rounding dust below this is ignored. |
| Recurring obligation match (inv20) | `amount_tolerance` field, default $0.50 | Below tolerance = treated as exact match. |
| FX rounding (Wise multi-currency) | $0.01 | Per-currency conversion rounding tolerance. |

## Drift queue triage policy

Per Perplexity audit-7 Q1: T2 drifts (unresolvable) are **user-pinned
confirmed noise**, not auto-aged.

* **No auto-write to 5990.** The Reconciliation Adjustment CoA only
  receives journals from explicit user action (a future `bulk_resolve_drift`
  invocation).
* **Categories:** `peer_paynow`, `cash_withdrawal`, `legacy_gap`, `other`.
  Each implies "consciously accepted as noise" but preserves the row in
  the queue for audit trail.
* **Cadence:** monthly Telegram nudge (1st @ 09:00 SGT) when any
  PERIOD_DRIFT row remains in `status='pending'`. Counts + oldest age.

## Anchor authority

Per audit-6 SoT decisions:

* **Class A (statement-anchored):** `bank_statement_registry.CF` is the
  authoritative balance. If the latest BSR is > 90 days old, the resolver
  returns `source='stale_statement'` with a UI badge but still reports
  the last-known CF as the value.
* **Class B (snapshot-anchored):** `account_snapshot` is the authoritative
  balance. No GL fallback. If no snapshot exists, returns `source='no_snapshot'`
  with `sgd=0`.
* **Class C (snapshot in GL):** GL sum since last re-anchor journal. Used
  for CPF / ILP funds where the user re-anchors when receiving a
  statement.

If a CoA row has no anchor_class set, `resolve(strict=True)` raises
`NoResolverError`. `resolve_debug()` is the explicit, opt-in permissive
variant.

## External_id contract

Every journal posted on/after **2026-05-16** must use the canonical
format: `<source>:v<n>:<stable_key>`.

* `<source>` is from the `EXTERNAL_ID_SOURCES` allowlist (32 sources
  documented).
* `<n>` is an integer ≥ 1.
* `<stable_key>` must be deterministic over immutable business keys.
  Never include a hash of mutable content.

Enforced by: partial UNIQUE index on `journals(external_id) WHERE status
!= 'voided'`, plus inv19f shape validation.

Older journals are grandfathered.

## Voided journals

* `status='voided'` keeps the row in `journals` for audit but excludes it
  from all balance calculations (inv25-28).
* Voided rows may share their external_id with the replacement (amends
  pattern). The partial UNIQUE only applies to non-voided rows.
* `narration` prefix `[VOIDED <date>: <reason>]` is the convention for
  marking voided journals so the audit trail is human-readable.

## Alerts severity

* **High:** worthy of an immediate Telegram push. E.g. Class A statement
  > 180 days old; Class B snapshot dropped > 20% week-over-week.
* **Medium:** appears in `/alerts` for review, no push. E.g. missing
  recurring this month, statement 90-180 days old.
* **Low:** informational, low priority.

A dismissed alert stays dismissed across re-scans (the scan job will not
re-create it).

## Snapshot freshness

* **Class B (Coinbase, Wise):** snapshot < 24 hours = fresh; > 24 hours
  = `source='stale_snapshot'` with UI badge.
* **Class A:** statement < 90 days = fresh; > 90 days = `source='stale_statement'`.

## What V2 does NOT promise

Per V3-SCOPE.md, the following are explicitly deferred and should NOT
be relied on as of V2:

* Realised vs unrealised FX P&L treatment.
* Insurance cash-value split between asset accretion and expense.
* Reclass-stability across historical YTD reports.
* Multi-currency analytics in /income_statement.
* Spend-spike anomaly detection (only 3 of N planned detectors are live).
