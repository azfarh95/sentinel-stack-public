# Sentinel Finance — Architecture (v2 draft, 2026-05-14)

## Principles

1. **One fact, one owner.** Every business fact has ONE canonical registry table, written by ONE pipeline.
2. **The GL is the only source-of-truth for current state.** Dashboards read the GL; they don't replicate data.
3. **Verify before posting, not after.** Each candidate journal is matched against the canonical registries up-front. High confidence → post. Low confidence → unreconciled queue, user resolves.
4. **No void-recreate loops.** Every journal in the GL was either auto-posted with high confidence OR user-confirmed. Nothing self-corrects later.

## The intended flow

```
                          ┌──────────────────┐
                          │     _INBOX       │   (PDFs / images / HEIC)
                          └────────┬─────────┘
                                   │
                                   ▼
                          ┌──────────────────┐
                          │  ocr_normalize   │   ← universal first step
                          │  → ocr_cache/    │     (table: ocr_normalize_log)
                          └────────┬─────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
       ┌───────────────┐   ┌───────────────┐   ┌───────────────┐
       │statement      │   │payslip        │   │facility /     │
       │parser         │   │parser         │   │policy / loan  │
       │(universal_pdf)│   │               │   │parsers        │
       └───────┬───────┘   └───────┬───────┘   └───────┬───────┘
               │                   │                    │
               │ writes            │ writes             │ writes
               ▼                   ▼                    ▼
       ┌─────────────────┐ ┌─────────────────┐  ┌──────────────────┐
       │statement_       │ │payslip_         │  │credit_facilities │
       │registry         │ │registry         │  │facility_plans    │
       │(BF, CF,         │ │(gross, net,     │  │payment_schedule  │
       │ credit_limit,   │ │ CPF, MBMF,      │  │(facility +       │
       │ statement_date, │ │ employer_key,   │  │ schedule of dated│
       │ facility_id)    │ │ period_end)     │  │ payments)        │
       └────────┬────────┘ └────────┬────────┘  └────────┬─────────┘
                │                   │                     │
                └───────────────────┼─────────────────────┘
                                    │
                                    ▼
                          ┌──────────────────┐
                          │ posting service  │  ← derives journals
                          │ (journal_service)│     from registry rows
                          └────────┬─────────┘
                                   │
                                   ▼
                          ┌──────────────────┐
                          │  journals +      │  ← THE single source
                          │  general_ledger  │     of truth
                          └────────┬─────────┘
                                   │
                  ┌────────────────┼────────────────┐
                  ▼                ▼                ▼
           ┌────────────┐  ┌────────────┐  ┌────────────┐
           │ reports    │  │reconcilers │  │  reports   │
           │ (P&L, BS,  │  │(check that │  │ (cash      │
           │  drill)    │  │ registries │  │  forecast) │
           │            │  │ == GL)     │  │            │
           └────────────┘  └────────────┘  └────────────┘
```

## What's actually built — categorized

### ✅ Foundation (good as-is)
| Component | Purpose | Status |
|---|---|---|
| `chart_of_accounts` (112) | CoA tree | ✅ |
| `journals` + `general_ledger` | The GL | ✅ |
| `ocr_normalize` + `ocr_normalize_log` | Universal first step | ✅ |
| `universal_pdf_parser` | Schema-driven extraction | ✅ |
| `journal_service` | Balanced journal posting | ✅ |

### ✅ Registries (well-formed, source-of-truth for their domain)
| Table | What it holds | Rows | Filled by |
|---|---|---:|---|
| `statement_registry` | Per-statement BF/CF/limit, by facility | 133 | CC/loan parsers |
| `payslip_registry` | Gross/net/CPF/SDL per payslip | 10 | payslip parser |
| `credit_facilities` | Facility metadata (lender/type/limit) | 15 | hand-curated + seed_credit_db |
| `facility_plans` | Repayment plan per facility | 23 | seed |
| `payment_schedule` | Dated payments per facility | 36 | seed |
| `cc_statement_commitment` | CC bill commitments | 34 | auto-matcher (#94) |
| `nav_history` | Fund NAV time series | 12 | morningstar refresh |
| `positions` + `snapshots` | Crypto snapshot | 208 + 5 | Moralis |

### ⚠ Built tonight — DUPLICATES of existing structure
| Tonight's table | Duplicates | What to do |
|---|---|---|
| `recurring_obligation_registry` (17) | `credit_facilities` + `facility_plans` + `payment_schedule` | **Retire**. Reconciler reads from credit_facilities. |
| `recurring_reconcile_log` (75) | OK as audit trail | Keep |
| `salary_reconcile_log` (48) | OK as audit trail | Keep |
| `salary_reconciler.py` cross-doc guard | OK | Keep |

### ❌ Disconnected pipelines (parsers exist but data doesn't flow into registries)
| Source | Parser | Should populate | Currently |
|---|---|---|---|
| POSB / Maybank / SC savings stmts | universal_pdf_parser | `statement_registry` | Only flows to GL, not registry |
| CPF statements | cpf_statement_parser | `statement_registry` or new `cpf_registry` | Doesn't persist |
| ILP statements | ilp_parser | `statement_registry` or new `ilp_registry` | Doesn't persist |
| Wise statements | (no parser yet) | `statement_registry` | Not parsed |

### ❌ Disconnected consumers (data exists but no UI/route surfaces it)
| Question user asks | Should query | Currently |
|---|---|---|
| "What's my aggregated credit limit?" | `credit_facilities.credit_limit` SUM | No route — answered via ad-hoc script |
| "When's the next payment due?" | `payment_schedule WHERE due_date >= today` | No route |
| "Show me the repayment plan for EZ Loan" | `facility_plans WHERE facility_id='ez-loan'` | No route |
| "What was POSB balance Jan 1 2024?" | `statement_registry` earliest BF | No route — needs full PDF scan |
| "How many payslips do I have?" | `payslip_registry COUNT` | No route |

## The two real problems

### Problem 1: I built parallel data stores instead of reusing existing ones
- `recurring_obligation_registry` overlaps `credit_facilities` + `facility_plans`
- I was reading the wrong table for the home glance liability total — should have used `credit_facilities.current_outstanding`, not GL

### Problem 2: The dashboard doesn't surface what's already in the DB
- 6+ routes are missing that would answer questions WITHOUT a CLI script
- Every time you ask a question, I write a `_show_X.py` because there's no `/X` route
- That's why my session looks like 35 throwaway scripts

## Completeness checks (what each consumer should expect)

| Consumer | Must have | Today |
|---|---|---|
| **Reconciler** | Every payment in `payment_schedule` matched to a posted journal in GL within 7d window | Not built |
| **Balance sheet** | Every account in CoA has either a GL entry summing to its current balance, OR a `statement_registry` snapshot within 90d | Partial — gaps in CPF/ILP/savings |
| **Opening balance audit** | One opening journal at the start of each account's tracked period, balancing leg in 3100 | Built today (jid=13441) |
| **Drill pages** | Every line on the income statement is clickable into the journal lines that contributed | Built today (CoA-coded) |
| **Credit utilization** | `credit_facilities.current_outstanding / credit_limit` per facility, surface in home glance | Computed but not surfaced |

## Proposed next steps (ordered by leverage, NOT TONIGHT)

1. **Audit pass**: query EVERY existing table, list every column, identify what feeds each → publish a `data-inventory.md`. Future questions get answered by "which table?" before any new code.

2. **Retire `recurring_obligation_registry`**: rewrite `recurring_reconciler.py` to read from `credit_facilities` + `payment_schedule`. Delete the tonight-built parallel store. Reconcile log stays.

3. **Surface routes** for the questions that have come up:
   - `/facilities` — list of credit_facilities with limits, outstanding, utilization, click into facility_plans + payment_schedule
   - `/statements` — list of statement_registry rows, click into the underlying PDF
   - `/payslips` — payslip_registry
   - `/opening_balance_audit` — show jid=13441 + diff vs current GL balances
   - `/admin/data_inventory` — show all tables, row counts, last_updated

4. **Wire savings/CPF/ILP statement parsers to `statement_registry`** so the BF/CF for those accounts is persisted, not re-derived from raw PDFs each time.

5. **Run completeness checks** as scheduled jobs:
   - "Every facility has at least 1 statement in last 60d"
   - "Every payslip is matched to a POSB inflow"
   - "GL balance vs `credit_facilities.current_outstanding` agrees ± $5"
   These flag drift instead of requiring questions.

## Canonical owners — who writes which fact

Each fact has **exactly one registry as owner** and **exactly one parser as poster**. Other parsers READ the registry to verify, they don't post.

| Fact | Canonical registry | Poster | Other parsers |
|---|---|---|---|
| **Salary received** | `payslip_registry` | `payslip_parser` | POSB cutover verifies (skip if payslip covers) |
| **CC charge** | `statement_registry` (CC) | `cc_cutover` | POSB cutover ignores |
| **CC payment** | (transfer-pair cross-doc) | **POSB cutover** (user initiates from bank) | `cc_cutover` skips via xfer ext_id |
| **Cashline drawdown — bank** (DBS Cashline, UOB CashPlus, GXS) | `credit_facilities.facility_plans` + `payment_schedule` | **POSB cutover** (POSB sees inflow first; no agreement doc available for banks) | facility parser verifies |
| **Cashline drawdown — moneylender** (EZ Loan, Lending Bee, Sands Credit) | loan-agreement registry (subset of `credit_facilities`) | **agreement parser** (loan doc is authoritative — moneylenders provide one) | POSB cutover verifies |
| **Cashline repayment** | `payment_schedule` row | **POSB cutover** (user initiates) | facility parser verifies |
| **ILP premium** (recurring GIRO) | `ilp_policy_registry` (policy doc dictates frequency, due day, amount) | **POSB cutover** (GIRO from POSB — cleanest source) | ILP parser verifies expected matches actual |
| **ILP NAV / units** | `ilp_portfolio_snapshot` | `ilp_statement_parser` (from quarterly statement) | nothing else writes NAV |
| **Insurance premium** (Tokio, Singlife Term Life) | `insurance_policy_registry` | **POSB cutover** (GIRO from POSB) | insurance parser verifies |
| **CPF contribution** | `cpf_statement_registry` (missing — to build) | `cpf_statement_parser` | payslip_parser verifies match |
| **Subscription** (ChatGPT, Apple, etc.) | `subscription_registry` (missing — to build) | **POSB cutover** | none |

## Pre-posting verifier — the inline reconciler

```
TIME →

  Doc lands in _INBOX
       │
       ▼
  ocr_normalize (universal)
       │
       ▼
  parser extracts → writes to its canonical registry (above)
       │
       ▼
  ┌─────────────────────────────────────────────────────────┐
  │  CANDIDATE JOURNAL                                       │
  │  (proposed, NOT yet posted)                              │
  │                                                          │
  │  Bank parser extracts: date, amount, carriers, tx_type,  │
  │  narration. Tags it as 'candidate'.                      │
  └────────────────────┬────────────────────────────────────┘
                       │
                       ▼
  ┌─────────────────────────────────────────────────────────┐
  │  VERIFIER (pre-posting)                                  │
  │                                                          │
  │  Walks every canonical registry. For this tx:            │
  │   1. Identifier exact match? (policy_ref, card #,        │
  │      account #, MSL/SCL routing)   →  confidence = 100   │
  │   2. Registry has expected payment matching amount       │
  │      AND within date window?        →  confidence = 80   │
  │   3. Amount alone matches an active registry row?        │
  │      + tx_type recurring marker     →  confidence = 60   │
  │   4. tx_type-only fallback (Debit Card→Lifestyle,        │
  │      Cash Withdrawal→Family)        →  confidence = 50   │
  │   5. Nothing matched                →  confidence = 0    │
  └─────────────┬───────────────────────────────────────────┘
                │
        ┌───────┴────────┐
        │                │
        ▼                ▼
  confidence ≥ 75   confidence < 75
        │                │
        ▼                ▼
  ┌──────────┐    ┌─────────────────────────────────────┐
  │  post    │    │  unreconciled_queue                  │
  │  journal │    │                                      │
  │  to GL   │    │  Holds: candidate journal data +     │
  │  ✓       │    │  best guesses + status.              │
  └──────────┘    │                                      │
                  │  Surfaces as a list on /reconcile.   │
                  │  User clicks each:                    │
                  │   • approve match X                   │
                  │   • assign to CoA Y                   │
                  │   • mark as new obligation            │
                  │     (writes new registry row)         │
                  │   • delete (spam)                     │
                  │                                      │
                  │  On resolve → journal posts to GL +   │
                  │  unreconciled row marked 'resolved'.  │
                  └──────────────────────────────────────┘
```

## `unreconciled_queue` table

```
unreconciled_queue
├── id
├── source_doc           — POSB stmt, CC stmt, etc.
├── source_ref           — PDF path + line number
├── candidate_journal    — JSON of the legs we'd post if approved
├── tx_date
├── tx_amount
├── tx_narration
├── tx_carriers          — extracted identifiers
├── best_guess_matches   — JSON: top-3 registry rows by confidence
├── confidence           — 0–100
├── status               — pending | resolved | rejected
├── user_decision        — CoA / registry_row_id / 'lifestyle' / etc.
├── resolved_at
├── posted_journal_id    — link to GL if resolved as 'post'
└── created_at
```

## Verifier flow — bidirectional integrity

Both directions run as background jobs, but they're rare-exception alerts now:

**Forward check** (catches missed payments):
"Every active registry row says I expect payment of $X by date Y. Look in GL — is there a journal for it?"
- Singlife Mar 12 expected → GL Mar 12 ✓
- Tokio Marine Apr expected → GL Apr ❓ → flag as missing/late

**Reverse check** (catches surprise activity):
"Scan all journals. Each one should trace back to a registry row OR a user-approved unreconciled item."
- POSB outflow $4,500 Jan 7 → matches payment_schedule (EZ Loan drawdown) ✓
- POSB outflow $1,500 Jan 9 → no registry match, no unreconciled resolution → flag as 'unjustified'

In normal operation, neither check fires. They're the safety net.

## Anti-patterns to stop

- ❌ `_show_X.py`, `_diag_Y.py`, `_test_Z.py` for one-shot data inspection
- ❌ Creating a new table when an existing one answers the question
- ❌ Reading data from one pipeline, writing to another, and not connecting them
- ❌ Band-aiding the home glance to read credit_facilities while leaving the underlying GL broken

## The question to ask before writing code

> "Is there something in Sentinel Finance that is already feeding this data?
>  If yes — connect to it. If no — design where it belongs FIRST,
>  then build the pipeline that fills it."
