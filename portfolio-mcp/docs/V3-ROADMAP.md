# V3 Roadmap — Capability

V2 = operability (sealed 2026-05-15).
V3 = capability — "advanced financial management tools." Build user-
visible value on top of V2's solid core.

Sequenced per Perplexity audit pass-9 Q3.

## V3.0 — Base scaffolding (prerequisite to V3.1+)

Not in V2-SCOPE.md's A-K list because it's new infrastructure.

* **Budget / forecast data home.** New tables: `budget_config`
  (per-CoA monthly targets), `forecast_input` (recurring expectations
  + irregular line items). UI deferred to V3.1+, but the data layer
  needs to exist so detectors and analytics can reason about
  plan-vs-actual.

## V3.1 — Signals + hygiene

Immediate user-visible value built on existing infrastructure. No new
deep accounting required.

| # | Source | Item |
| - | --- | --- |
| 1 | G | **Salary-missing alert detector.** Knows the recurring salary corridor (employer + expected day ± window). Fires when the expected credit doesn't land. Protects the primary inflow. |
| 2 | F | **Spend-spike alert detector.** Rolling 6-month median per category. Z-score > threshold (e.g. 2.5σ). Per-category override table for tunable false-positive control. |
| 3 | J | **End-to-end 5-gate test.** PyTest fixture that ingests a golden POSB PDF, runs cutover, asserts BSR + journals + drift + resolver. Regression safety net for V3+. |
| 4 | H | **Maybank Ar Rihla legacy dup cleanup.** 2 of 31 `[direct Ar Rihla]` journals have v2 twins. One-shot void. Last historical hygiene task. |

**Rationale:** thin vertical slice that proves V3 is adding management
value (not just refactoring). Salary + spend detectors immediately give
the user "something's off" notifications. The e2e test locks in
regression safety before deeper changes land.

## V3.2 — Portfolio & FX analytics

Build richer understanding of investment / FX behaviour without
committing to full FRS-21 dual journals yet.

| # | Source | Item |
| - | --- | --- |
| 1 | E | **Multi-currency analytics in /income_statement.** Start from `account_snapshot.raw_currencies`. Surface FX-adjusted returns, per-currency exposure. Can ship without FX P&L journals. |
| 2 | C | **48xx Unrealised Gains REVENUE bucket.** New CoA `4810 Unrealised Gains`. Anchor deltas Cr 4810 instead of Cr 3100. Year-end closing rolls 4810 → 3100. Prepares for V3.3 FX. |
| 3 | I | **SC SuperSalary PDF schema.** 4 currently-unparsed PDFs (Dec 2025 – Mar 2026). New `sc-supersalary.yaml` in `finance/statement_schemas/`. Backfills BSR coverage. |

**Dependencies:** E and C benefit from a clean snapshot-delta → P&L
design. FX P&L (V3.3) can remain "implicit" inside the 4810 bucket
until V3.3 splits it cleanly.

## V3.3 — Accruals & reclass semantics

Heavy accounting upgrades for sophisticated users.

| # | Source | Item |
| - | --- | --- |
| 1 | A | **FX P&L treatment (FRS-21 dual journals).** Every multi-currency tx posts two legs. `4920 FX Gain` / `5920 FX Loss` CoA. Move from "snapshot-only FX" to explicit recognition of realised vs unrealised. |
| 2 | B | **Insurance cash-value accrual.** Split premiums into expense + asset growth. Per-policy cash-value table. Invariant: `Δ cash_value + recognized expense ≈ premiums paid`. |
| 3 | D | **Reclass-stability invariant.** Explicit `replaces_journal_id` column. Invariant: for a reclass-pair (X, Y), the YTD for the period containing X.date is unchanged when Y posts. |

**Mental-model shift:** FX P&L is a breaking change. Doing it after
V3.2 (where users have seen unrealised gains in 4810) gives a smooth
path to understanding the upgrade.

## V3.4 — Infrastructure & tenant prep

| # | Source | Item |
| - | --- | --- |
| 1 | K | **Tenant isolation (V7 prep).** Schema + deployment. Not user-visible in the V4 single-owner MSI/APK story but a prerequisite for V7 multi-tenant SaaS. Can run in parallel with V3.3. |
| 2 | — | Any cleanup that shakes out from real-world FX / insurance feature use. |

## Cadence

V3 is not auditable the same way V2 was. The architecture is locked;
V3 layers capability on top. Audits resume at the V3 → V4 transition
(packaging readiness) or when a new structural concern emerges from
real-world use.

## Out of V3 scope

* V4 packaging (MSI/APK) — separate milestone.
* V7 multi-tenant — design started in V3.4 only.
* AI Copilot deep integration — separate V3 stream once the data tables
  in V3.0 exist.
