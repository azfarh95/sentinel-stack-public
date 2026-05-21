# Sentinel Finance — Invariant Catalogue (V2)

32 invariants. All in `tests/test_invariants.py`. Run inside container:

    docker exec portfolio-mcp pytest tests/test_invariants.py -v

## Balance-sheet contracts (inv1–10)

| # | Name | Enforces |
| - | --- | --- |
| 1 | `every_bs_account_with_journals_has_opening_anchor` | Gate 1: every posted journal on an ASSET/LIABILITY/EQUITY account has a corresponding `account_opening_anchor` row with `opening_date <= journal_date`. |
| 2 | `journal_balanced` | Every posted journal has ΣDr = ΣCr within $0.01 tolerance. |
| 3 | `journal_min_two_lines` | Every posted journal has ≥ 2 GL lines. |
| 4 | `gate1_blocks_unanchored_post` | Attempting to post on an unanchored BS account raises `OpeningAnchorRequired`. |
| 5 | `bsr_uniqueness` | `bank_statement_registry` is unique on (account_code, period_end). |
| 6 | `bsr_period_real` | Every BSR row references a real period (period_start ≤ period_end ≤ today). |
| 7 | `gl_dr_cr_columns_balanced` | Across the whole GL, ΣDr = ΣCr within $0.01. |
| 8 | `every_gl_line_has_account` | No orphan GL lines (every `account_id` resolves in CoA). |
| 9 | `drift_resolve_journal_shape_legacy` | `drift_resolve` journals have ≥ 2 lines. |
| 10 | `opening_anchors_offset_to_retained_earnings` | Every opening journal touches 3100 Retained Earnings (or is the multi-account 2024-01-01 portfolio anchor). |

## Cross-cutting contracts (inv11–16)

| # | Name | Enforces |
| - | --- | --- |
| 11 | `conservation` | For every posted journal, sum of Dr legs by class = sum of Cr legs by class (net = 0). |
| 12 | `flow_identity_per_account` | For each (account, period), Dr−Cr = ending_balance − opening_balance (within tolerance). |
| 13 | `registries_idempotent` | Re-registering the same statement / policy / snapshot is a no-op (no double rows). |
| 14 | `gate5_sot_for_class_a` | Every Class A account resolves via `_resolve_class_a` (statement_cf or gl_projection — never silently from gl_balance). |
| 15 | `drift_lifecycle` | Every PERIOD_DRIFT row has status in {pending, resolved, rejected, triaged}; resolved rows have `posted_journal_id`. |
| 16 | `drift_resolve_journal_shape_strict` | drift_resolve journals are exactly 2 lines, touch one of {3100, 5990}, no other P&L. |

## Resolver / anchor_class contracts (inv17–18, inv19a–f)

| # | Name | Enforces |
| - | --- | --- |
| 17 | `anchor_class_db_matches_python_sets` | `chart_of_accounts.anchor_class` column ↔ `CLASS_A_BANK`/`CLASS_B_LIVE`/`CLASS_C_SNAPSHOT` Python sets agree. Only ASSET rows may carry an anchor_class. |
| 18 | `every_anchor_class_has_a_registered_resolver` | Every anchor_class value present in DB has a callable entry in `RESOLVER_REGISTRY`. |
| 19a | `dashboard_codes_resolve_strictly` | Every code in `balance_sheet_config.yaml`'s `gl_account_codes:` lists resolves under strict=True with anchor_class in {A,B,C}. |
| 19b | `counter_account_map_shape` | corridor table: both endpoints ASSET, src=A, dst=A for bank_peer (dst ∈ {A,B} for wallet_bridge), no self-map. |
| 19c | `fixable_drift_has_corridor_overlap` | Every queue row labelled `operational_drift` still re-classifies `fixable` against the live corridor table. |
| 19d | `class_b_never_returns_gl_source` | Class B resolution never produces a source containing `gl_*`. |
| 19f | `external_id_canonical_format` | Every journal posted on/after `EXTERNAL_ID_ENFORCED_FROM` (2026-05-16) conforms to `<source>:v<n>:<stable_key>`. |

## P&L contracts (inv20–23)

| # | Name | Enforces |
| - | --- | --- |
| 20 | `recurring_obligations_have_recent_posts` | Every active `recurring_obligation_registry` row with `expected_amount > 0` has ≥ 1 matching debit on `contra_coa` in the last 90 days. |
| 21 | `income_journals_shape_conservation` | Every salary/income/cash_receipt journal has Dr on ASSET + Cr on REVENUE. |
| 22 | `classification_sanity` | No REVENUE account has YTD net Dr; no EXPENSE account has YTD net Cr (>$1 tolerance). |
| 23 | `anchor_journals_shape` | Re-anchor journals (`journal_type='anchor'`) are exactly 2 lines, ASSET/LIABILITY + EQUITY, no P&L pollution. |

## Alerts contract (inv24)

| # | Name | Enforces |
| - | --- | --- |
| 24 | `alerts_shape` | Every alert row has known kind, severity in {low,medium,high}, status in {pending,dismissed,resolved}, valid account_code if set. |

## Income-statement aggregation (inv25–28)

| # | Name | Enforces |
| - | --- | --- |
| 25 | `income_statement_voided_exclusion` | /income_statement totals = direct GL query SUM(P&L journals where status='posted') for the same period. |
| 26 | `income_statement_closing_identity` | net_income = total_income − total_expenses within $0.05. |
| 27 | `income_statement_period_cutoff` | Re-computed in-period totals match rendered totals; no journal outside [period_start, period_end] contributes. |
| 28 | `income_statement_chart_rollup` | Sum of rendered line items = section header totals for both income and expense sections. |

## Coverage gaps (deferred to V3 — see [V3-SCOPE.md](./V3-SCOPE.md))

* **inv-29 (deferred):** insurance cash-value accrual — need per-policy cash-value table.
* **inv-30 (deferred):** ILP unrealised P&L identity — need 48xx Unrealised Gains REVENUE bucket.
* **inv-31 (deferred):** reclass stability — needs explicit reclass relationships in the schema.
* **inv-32 (deferred):** FX gain/loss conservation — needs FRS-21 dual-currency journals.
