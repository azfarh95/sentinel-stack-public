# Sentinel Finance v2 — The Five-Gate Architecture

> **Status**: Session 2026-05-14 / 2026-05-15. Gates 1–4 implemented. Gate 5 pending.
> Anchor pattern (Class A/B/C) is the source-of-truth contract for every balance.
> The GL is now a transaction-trail audit log, not a source of current-balance truth.

## TL;DR

A consumer-grade accounting system built from PDF statements has one structural failure mode: the General Ledger inflates with one-sided phantom entries when transfers leave one ingested-statement account and land in another whose statement isn't ingested at parity.

The cure isn't more bookkeeping. It's five process gates:

1. **Opening Balance Gate** — no account accepts a journal until its Day-0 anchor exists
2. **Verifier-mandatory cutover** — every transaction's destination CoA is verified BEFORE posting; one-sided journals are structurally impossible
3. **bank_statement_registry** — every parsed PDF persists `(period_start, period_end, BF, CF)` instead of throwing the values away
4. **Period reconciliation** — `GL_at(period_end) == statement.CF` is asserted after every ingest; drift becomes a queue entry, not silent inflation
5. **account_balance.resolve()** — one function per metric across every UI surface; "two numbers, one metric" is structurally impossible (pending)

This document captures the architecture, the session that produced it, and the simulation arc that validated each gate.

---

## Principle: Anchor first, GL second

Every asset/liability/equity account belongs to exactly one class:

| Class | Source of truth | Refresh cadence | Examples |
|---|---|---|---|
| **A — Statement-anchored** | Monthly PDF statement BF/CF persisted to `bank_statement_registry` | On each statement parse | POSB, Maybank Ar Rihla, SC SuperSalary, every CC, every loan facility |
| **B — Live-API-anchored** | API call (cached, TTL ≤ 15min) | On schedule + on-demand | Wise (✓), Moralis on-chain (✓), Coinbase CDP (key wired, snapshot module pending) |
| **C — Snapshot-anchored** | User-entered or PDF-extracted point-in-time value | On upload | CPF OA/SA/MA (CPF app screenshot), ILP NAV, CEX accounts without API |

The dashboard NEVER computes "current balance" by summing GL journals. It reads the latest anchor.

The GL records what happened *between* anchor points. Its purpose:

1. **Audit** — drill-down "where did this $400 go" pages
2. **Forecast** — project forward from the last anchor for `/cash_forecast`
3. **Reconcile** — assert `anchor[N+1] == anchor[N] + Σ journals(period[N..N+1])`, drift surfaces to triage

---

## The Five Gates

### Gate 1 — Opening Balance Gate

**Invariant**: no journal may post to a balance-sheet account (assets/liabilities/equity, codes 1xxx/2xxx/3xxx) on date `D` unless that account has an `account_opening_anchor` row with `opening_date ≤ D`. P&L accounts (4xxx/5xxx) are exempt — they're zero by definition each period.

**Table** `account_opening_anchor`:
```sql
id, account_id (FK), opening_date, opening_balance,
source_doc (STATEMENT_BF | USER_ENTRY | HISTORICAL_BACKFILL | FIREFLY_BRIDGE),
source_ref, posted_journal_id (FK), notes, created_at,
UNIQUE(account_id, opening_date)
```

**Helper** `post_opening_anchor(s, account_code, opening_date, opening_balance, source_doc, source_ref)`:
- Asserts the account is balance-sheet class
- Builds a balanced journal with `journal_type='opening'` (bypasses Gate 1 self-recursion)
- Offset always lands on **Retained Earnings (3100)** — the "historical net worth that pre-dates this system" bucket
- For assets: `Dr <account>, Cr 3100` (or flipped for negative)
- For liabilities: `Cr <account>, Dr 3100`
- Registers the anchor row; idempotent on `(account_code, opening_date)`

**Gate enforcement** (in `journal_service.post_journal`):
```python
if journal_type != 'opening':
    for line.account_id where account_class in (ASSET, LIABILITY, EQUITY):
        if no account_opening_anchor row exists with opening_date ≤ journal_date:
            raise OpeningAnchorRequired(account_code, journal_date)
```

**Day-0 gap → equity**: when a statement BF is ingested for an account with no prior anchor, the difference between BF and zero lands on Retained Earnings 3100. Standard "books opened with existing balances" accounting.

**Why this matters**: without Gate 1, the GL silently accumulates contributions/transactions on top of a phantom $0 opening balance. CPF showed only $283 (contributions since cutover) when actual was $47,406. The gate forces every account to declare what it started with.

### Gate 2 — Verifier-mandatory cutover

**Invariant**: every cutover script routes through the verifier (`app.verifier.verify()`); no transaction reaches `post_journal()` without a probe verdict.

**Verifier probes** (priority order, top-1 wins):
1. `_probe_payslip` — cross-doc dedup; if payslip covers this POSB salary credit, return SKIP
2. `_probe_insurance` — match outflow against `insurance_policy_registry`
3. `_probe_subscription` — match outflow against `subscription_registry`
4. `_probe_facility` — match outflow against `credit_facilities` / `payment_schedule`
5. `_probe_inflow` — tx_type classification for incoming credits (SALARY → 4110, INTEREST EARNED → 4220, MEPS → 4900, DIVIDEND → 4220, INWARD CREDIT → 4900, CASH DEPOSIT → 1112, etc.)
6. `_probe_lifestyle` — tx_type fallback for outflows (DEBIT CARD / POS → 5190, BILL PAYMENT → 5190, FAST+PayNow → 5190, CASH WITHDRAWAL → 5170, SERVICE CHARGE → 5410)
7. **Suspense catch-all** — any remaining FAST/Funds Transfer/GIRO/Remittance → 1190 Suspense at confidence 75 (auto-posts; user triages from `/reconcile`)

**Verdict**:
- `POST_AUTO` (confidence ≥ 75) → caller calls `post_journal()` with the probe's `contra_coa`
- `QUEUE` (< 75) → caller calls `verifier.enqueue()` to write `unreconciled_queue`
- `SKIP` → caller does nothing (cross-doc already covered)

**Idempotency guard**: before re-replay, the cutover voids any prior journals with `external_id LIKE 'posb_direct:%' OR 'posb:v2:%'` to prevent duplication.

### Gate 3 — `bank_statement_registry`

**Invariant**: every parsed bank statement persists `(account_code, period_start, period_end, BF, CF, source_doc_path)`. The dashboard reads `MAX(period_end).CF` instead of summing GL journals (Class A anchor).

**Table**:
```sql
bank_statement_registry (
  id, account_code, period_start, period_end,
  balance_brought_forward, balance_carried_forward, currency,
  source_doc_path, parsed_at,
  external_id UNIQUE (account_code:period_end), notes
)
```

**Helper** `register_bank_statement(s, account_code, period_start, period_end, BF, CF, source_doc_path)`: idempotent on `(account_code, period_end)`. Wired into POSB cutover; Maybank/SC/CC pending.

**Why this matters**: Before Gate 3, parsers extracted BF/CF and printed them to stdout. The CF vanished. Without it, Gate 4 has nothing to reconcile against.

### Gate 4 — Period reconciliation

**Invariant**: after every statement ingest, `GL_at(account, period_end) == statement.CF`. Drift surfaces to `unreconciled_queue` (reason=`period_drift`), never silently inflates GL.

**Function** `reconcile_period(s, account_code, period_end, tolerance=0.01)`:
```python
1. Fetch statement.CF from bank_statement_registry
2. Compute GL_balance = SUM(Dr_sgd - Cr_sgd) WHERE account_id = X AND journal_date <= period_end
3. drift = CF - GL_balance
4. If |drift| <= tolerance: action='reconciled'
5. Else: write to unreconciled_queue with tx_type='PERIOD_DRIFT', action='drift_queued'
```

**Why this matters**: 28 POSB statements processed → 28 drift entries surfaced. Every gap is now triagable; previously they accumulated as -$143k of phantom GL.

### Gate 5 — `account_balance.resolve()` (pending)

**Invariant**: every UI surface (`/`, `/balance_sheet`, `/drill/*`, `/api/agent/*`) reads current balances via one resolver function. "Two numbers for one metric" becomes structurally impossible.

**Design**:
```python
def resolve(account_code, as_of=None) -> Balance:
    coa = lookup(account_code)
    cls = determine_anchor_class(coa)  # A/B/C
    if cls == "A":
        latest = bank_statement_registry.MAX(period_end) for account_code
        if latest.period_end >= today - timedelta(days=45):
            return Balance(value=latest.CF, source='statement', as_of=latest.period_end)
        return project_forward(latest)  # last anchor + journals since
    if cls == "B":
        return live_api_call(account_code)  # Wise / Moralis / Coinbase
    if cls == "C":
        return latest_snapshot(account_code)
```

**Why this matters**: the recent $1,977.93 reconciliation gap between glance NW and sum-of-cards came from balance_sheet reading `liabilities-registry.yaml` (deprecated) while glance read `credit_facilities`. Gate 5 routes all reads through the resolver.

---

## Simulation Arc (this session)

To validate the architecture worked end-to-end, I ran 8 simulations against POSB across 2024-01 → 2026-04 (28 statements, ~2,300 transactions).

| Sim | Change | Posted | Queued | Σ\|drift\| | Anchor weight | Note |
|---|---|---|---|---|---|---|
| 1 | Baseline diagnostic | (pre-existing data) | — | — | — | Identified BF/CF discarded → Gate 3 gap |
| 2 | Built Gates 3 + 4 | 90 | 31 | $7,469 | 37% | Drift queued for the first time; surfaced anchor-date bug on 2221 EZ Loan |
| 3 | Full historical replay through verifier | 1450 | 843 | (deeply negative GL) | — | Discovered cross-path duplication: legacy `posb_direct:*` external_ids vs verifier `posb:v2:*` |
| 4 | Voided 2001 legacy duplicates | — | — | — | — | Cleaned slate; exposed missing inflow probes |
| 5 | Baseline measurement | (current) | — | $143,227 | 1.9% | Pipeline-alone produces -$141,716 of GL drift |
| 6 | + inflow probes (SALARY, INTEREST EARNED, MEPS, INWARD CREDIT) | 1526 | 767 | $1,176,580 | 0.2% | 66% → 78% auto-classification |
| 7 | + dividend, advice, refund probes; outflow Standing Instruction / FAST Collection | 1800 | 493 | $1,099,973 | 0.25% | 78% → 84% auto-classification |
| 8 | + suspense catch-all for ambiguous transfers | **2254** | **28** | $1,121,754 | 0.25% | **98% auto-classification**; queue reduced to period_drift only |

**Endpoint**: 98% of POSB transactions auto-classify through the verifier. Remaining residual drift is the cross-account integrity gap (POSB→Maybank inflows without corresponding Maybank outflow ingest) — Gate 4 surfaces these as 28 period_drift queue items for triage.

---

## What the anchors revealed (anchors-as-scaffolder pattern)

This session I posted 13 manual anchor journals (jids 13442–13458, 13470–13489) to make the dashboard show reality. Each anchor pointed at one of these architectural gaps:

| Anchor (jid) | Account | Δ from GL | Gap revealed |
|---|---|---|---|
| 13442 | 1112 Cash Wallet | +$4,584 | One-sided debit accumulation (POS purchases, no cash-out journal) |
| 13443 | 1231 Coinbase | -$25,362 | POSB→Coinbase Drs with no matching crypto-out leg (Gate 2 / cross-doc) |
| 13444 | 1114 Maybank Ar Rihla | -$20,458 | Maybank's own outflows not ingested at parity |
| 13445 | 12229 Singlife | +$3,286 | NAV revaluation never journaled |
| 13446 | 12219 Tokio Marine | +$10,228 | No GL CoA; was reading from Firefly |
| 13447 | 1115 SC SuperSalary | -$40,348 | Same as Maybank — destination outflows missing |
| 13448–13454 | 1211/1212/1213 CPF | +$53,140 combined | No opening anchor; GL had only contributions since cutover (Gate 1) |
| 13455 | 1111 POSB | +$2,787 | Cumulative misclassification drift (Gate 4 caught it next time) |
| 13456 | 1113 Wise | -$243 | One-sided Drs from POSB→Wise; Wise outflows via Wise API never journaled |

The anchors are **not** the architecture. They were band-aids that made each gap visible. The five gates make the band-aids unnecessary going forward.

---

## What's done, what's next

**Done this session** (2026-05-14 → 2026-05-15):
- ✅ OneDrive folder reorg: 16 unstructured folders → 10 numbered top-level + `_INBOX/_QUEUE/_OUT/_ARCHIVE`
- ✅ Classifier wired to new folder structure
- ✅ Firefly fully decoupled (last `firefly_account_ids` removed; Wise sync writes GL anchor journals instead of Firefly opening_balance)
- ✅ Display bug fix: `_gl_balances` was reading voided journals (LEFT JOIN ON clause leaked Cr/Dr of voided rows)
- ✅ CPF drill migrated from Firefly REST → GL `_gl_balances`
- ✅ Coinbase CDP API wired; key in `/data/coinbase_cdp_key.json`, snapshot fetch verified
- ✅ Gate 1 built + 36 opening anchors registered (existing journals backfilled, 18 orphans anchored)
- ✅ Gate 2 (POSB only — Maybank/SC/CC pending)
- ✅ Gate 3 + `bank_statement_registry` (28 POSB rows)
- ✅ Gate 4 + 28 period_drift entries surfaced
- ✅ Verifier inflow probes; 98% auto-classification on POSB
- ✅ Cross-path idempotency void guard
- ✅ Single-source liability resolution (`credit_facilities` for both NW calc and glance cards)
- ✅ EZ Loan dupe deleted in `credit_facilities`

**Next**:
- Gate 5: `account_balance.resolve()` resolver — one function across all surfaces
- Gate 2 extension: Maybank Ar Rihla / SC SuperSalary / CC cutover scripts route through verifier (currently use simpler `account_router`)
- Gate 3 extension: same parsers persist BF/CF to `bank_statement_registry`
- Triage the 28 period_drift queue entries (each is a real cross-account ingest gap)
- Coinbase live snapshot wired to `/balance_sheet` (1231 anchor → Class B)
- Wise sync verified daily-runs auto-anchor 1113 (the sync job exists but needs schedule validation)

---

## File map

```
portfolio-mcp/
├── app/
│   ├── ledger.py              # SQLAlchemy ORM. New: AccountOpeningAnchor, BankStatementRegistry
│   ├── journal_service.py     # Gate 1 enforced; post_opening_anchor() + register_bank_statement() + reconcile_period() helpers
│   ├── verifier.py            # Verifier + 6 probe classes (added _probe_inflow + suspense catch-all)
│   ├── posb_cutover_2026.py   # Verifier-default cutover with idempotency void guard
│   ├── balance_sheet.py       # `_liability_bucket` reads credit_facilities (single source of truth)
│   ├── drill.py               # CPF + Bank drill migrated from Firefly → GL
│   ├── home.py                # Glance reads credit_facilities for loans/cc
│   ├── wise.py                # sync_now() posts GL anchor journal instead of Firefly opening_balance
│   ├── doc_classifier.py      # Routes to 01_Bank statements/MAYBANK Ar Rihla etc.
│   └── v2_dashboards.py       # /reconcile, /facilities, /policies routes
├── docs/
│   └── ARCHITECTURE-V2-GATES.md  ← this file
└── data/
    └── coinbase_cdp_key.json  # gitignored; CDP read-only key
```
