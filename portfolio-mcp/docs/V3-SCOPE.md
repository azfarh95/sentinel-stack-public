# Sentinel Finance — V3 Scope

Decisions consciously deferred from V2. Each was raised during the
audit-3 → audit-8 review and recommended for deferral by Perplexity.

## A. FX P&L treatment (FRS-21)

**V2 behaviour:** All balances render in SGD. Wise + Coinbase snapshots
sum to SGD via a single fx rate per refresh. FX movements that aren't
actual cash movement (i.e. rate changes on existing foreign-currency
balances) appear silently in retained earnings via the anchor journal
pattern.

**V3 target:** FRS-21-style dual-currency journals. Every multi-currency
position posts two legs (original currency + SGD equivalent), with an
explicit `4920 FX Gain` / `5920 FX Loss` CoA capturing rate-driven deltas.

**Why deferred:** Personal-finance use doesn't need audit-grade FX. For
V7 multi-tenant SaaS, this becomes a tenant opt-in feature. The
`account_snapshot.raw_currency` + `raw_amount` + `raw_currencies` columns
shipped in V2 preserve the data needed to evolve later without back-fill.

**Dependencies for V3:** FX rate history table; new 4920 / 5920 CoAs;
revised inv19d to allow `fx_revaluation` source.

## B. Insurance cash-value accrual

**V2 behaviour:** Whole-life and endowment policy premiums post as full
expense on payment. Cash-value increases (the savings component) are
NOT separated from the protection component.

**V3 target:** Per-policy `cash_value` table. Each premium splits:
* Dr 1290 Cash Value (asset accretion) — for the savings portion
* Dr 5340 Insurance Expense — for the protection / cost-of-insurance portion
* Cr 1111 POSB

Invariant: `Δ cash_value over period + recognized insurance expense ≈
premiums paid`.

**Why deferred:** Requires actuarial assumptions or per-policy split
data from the insurer. The user doesn't have this for V2.

## C. 48xx Unrealised Gains REVENUE bucket

**V2 behaviour:** Class B/C re-anchor deltas go directly to 3100
Retained Earnings via the anchor journal. P&L doesn't see them.

**V3 target:** A new REVENUE account `4810 Unrealised Gains` (or OCI-style
`4820 Other Comprehensive Income`). Anchor deltas Cr 4810 instead of Cr
3100. Year-end closing entry Dr 4810 / Cr 3100 to roll up to retained
earnings.

**Why deferred:** A small accounting change but introduces a new
period-end closing ritual. Worth doing alongside FX P&L (A) so both can
share the period-close mechanics.

**Affected invariants:** inv23 (anchor journal shape) currently
hard-codes EQUITY-only contra. Will relax to allow {EQUITY, REVENUE-4810}
once 4810 exists.

## D. Reclass-stability invariant

**V2 behaviour:** Voiding journal X and reposting Y with the same date
may legitimately change a historical YTD. We don't enforce stability
across the void+repost transition.

**V3 target:** Explicit reclass relationships in the schema (X voided
*because of* Y, via a `replaces_journal_id` column). Then an invariant:
for a reclass-pair (X, Y), the YTD for the period containing X.date is
unchanged when Y posts (X's contribution = Y's contribution).

**Why deferred:** Implementation is straightforward but the operational
cost is non-trivial — every void+repost path must remember to set the
`replaces_journal_id`. Not blocking for V2 personal-use.

## E. Multi-currency analytics in /income_statement

**V2 behaviour:** /income_statement aggregates over `debit` and `credit`
columns (SGD-equivalent baked in). Foreign-currency journals lose their
original-currency identity once they hit the GL.

**V3 target:** `/income_statement?currency=USD` parameter. Per-currency
P&L drill. Requires (a) FX work first, plus (b) revised aggregation that
groups by original_currency.

## F. Spend-spike alert detector

**V2 behaviour:** 3 detectors shipped (stale_class_a, missing_recurring,
snapshot_drop). The spend-spike check ("user normally spends $400/mo on
F&B; this month $1,200") is NOT included.

**V3 target:** Add `_detect_spend_spike` to `app/alerts.py`. Requires:
* Rolling 6-month median per category baseline
* Z-score threshold (>2.5 σ default)
* Per-category override table for tunable false-positive control

**Why deferred:** Needs baseline statistics infrastructure. Not
foundational — can wait until there's enough history (6+ months of
clean post-V2 data).

## G. Salary-missing detector

**V2 behaviour:** Not implemented.

**V3 target:** Add `_detect_salary_missing` that knows the user's
recurring salary corridors (employer name + expected pay date ± window)
and alerts when the expected credit doesn't land.

**Why deferred:** Closely related to F. Both need the recurring-corridor
metadata fleshed out.

## H. Maybank Ar Rihla legacy duplicates

**V2 known issue:** 2 of 31 `[direct Ar Rihla]` journals have v2 twins.
Magnitude is small enough that we didn't void them (vs the 198 POSB and
26 HSBC cases that we DID void).

**V3 target:** Quarterly housekeeping job that runs the same exact-twin
detection across all `[direct *]` legacy patterns and voids dups. Or:
just clean up manually when noticed.

## I. SC SuperSalary PDF parser

**V2 known issue:** 4 of 6 SC PDFs parse as `schema=UNKNOWN`. The 2 that
work give us BSR coverage for Apr 2026 + Jan 2026 only.

**V3 target:** New `sc-supersalary.yaml` schema in `finance/statement_schemas/`
covering the SuperSalary format. Backfill BSR for Dec 2025 – Mar 2026.

**Why deferred:** Schema work, not architecture. Doesn't block V2's
correctness (SC still resolves to statement_cf via the Apr 2026 PDF).

## J. End-to-end gate test (task #131)

**V2 known gap:** No integration test exercising all 5 gates end-to-end
(drop a PDF, observe it flow through ingest → register → reconcile →
resolve).

**V3 target:** A pytest fixture that:
1. Loads a golden POSB PDF
2. Runs the cutover pipeline
3. Asserts the expected journals, BSR rows, drift items, resolver output

**Why deferred:** Lift is moderate (need stable golden fixtures). The
invariant suite + manual cutover burn-in (V2 checklist item 3) covers
the same surface less hermetically.

## K. Tenant isolation (V7 prep)

Out of scope for V3 as well — separate audit when V7 design starts.

---

## Summary

V2 ships with 32 invariants and an explicit list of 11 deferred items
above. Anyone reading this doc can answer "is X done?" for any of A-K
with a clear YES/NO/DEFERRED.

If V2 is the **operability** milestone, V3 is the **capability** one —
budgeting, forecasting, AI copilot, FX, accruals, and the
alerts-baselining work all live in V3.
