# Sentinel Finance — Changelog

Independent semver for `portfolio-mcp/`. Format: [v.M.P] — date — purpose.
Older history (pre-v1.0.0) is in the repo root git log.

---

## [1.18.0] — 2026-05-14 — OCR-first universal intake + cross-pipeline reconcilers (session: end-to-end audit + decouple)

### Tier 1 — Architectural changes (biggest leverage)

#### 1. Universal OCR-first intake — `app/ocr_normalize.py` (NEW)

Every document dropped in `_INBOX` (PDF / JPG / PNG / TIFF / HEIC) now emerges as canonical word-list JSON cached at `/data/ocr_cache/<sha256>.ocr.json` **before** the classifier or pile parsers run. Previously, image-PDFs (HSBC) returned blank text → classifier got nothing → never routed. The fix puts OCR as Step 0 of the pipeline.

- **Cache**: centralized, hash-keyed (`/data/ocr_cache/`). Survives renames/moves. Idempotent (`source_mtime > cached_mtime` → re-OCR).
- **Engine**: `tesseract 5.5.0` via `pytesseract`. Languages: `eng + chi_sim` (Chinese for merchant names).
- **Page text reconstruction**: `_words_to_lines()` groups words by y-coord so regex parsers (HSBC etc.) see proper line structure.
- **Quality metric**: 25th-percentile confidence (was: min, skewed by logo outliers). HSBC OCR'd at p25=0.85, 96.6% of words ≥0.80 confidence.
- **Output**: pdfplumber-compatible word list `{x0, x1, y0, y1, text, confidence}` per page.
- **Universal parser refactor**: `extract_text()` reads from cache; `pdfplumber.open()` call sites swapped for `_open_normalized()` shim (returns `_NormalizedPdf` exposing same interface as pdfplumber).
- **Bulk-normalized 245 docs** across `_INBOX` / `CC_Statement` / `Statements by bank` / payslips. 230 freshly OCR'd, 0 failed.

**Dockerfile**: `+tesseract-ocr-chi-sim, +libheif1`. **requirements.txt**: `+pdf2image, +pillow-heif, +watchdog`.

**Table**: `ocr_normalize_log` (source_hash unique, file_format, extraction_method, ocr_engine, languages, page_count, word_count, min_confidence, cache_path, status, error_msg).

#### 2. Router rules — lifestyle lumping (user-stated 2026-05-14)

Six new fast-path rules in `app/account_router.py` (resolution step 4b, before existing tx_type markers):

| tx_type | → CoA | Notes |
|---|---|---|
| `Debit Card transaction` / `Point-of-Sale` | 5190 Lifestyle | No per-merchant pattern matching — lump |
| `Bill Payment` (excluding internal `DBS INTERNET`) | 5190 Lifestyle | |
| `FAST Payment` + `paynow_recipient` carrier (no entity match) | 5190 Lifestyle | P2P/merchant payment fallback |
| `Cash Withdrawal` / `ATM` | 5170 Family Expense | Cash → parents, not own cash-on-hand |
| `Cash Deposit Machine` | 1112 Cash on Hand | Cash → POSB |

**Impact** (after voiding + re-replaying POSB cutover 2024-2026 with new rules):
- Hit rate **22.6% → 78.4%** classified (3.5× improvement)
- 2024: 27% → 77%
- 2025: 21% → 80%
- 2026: 20% → 75%

Memory: `feedback_lifestyle_expense_lumping.md` — full rule set incl. Funds Transfer (route by account-number carrier or suspense), Standing Instruction (registry), Wise (carrier extraction), MyPP (credit_facilities).

#### 3. Cross-pipeline salary reconciler — `app/salary_reconciler.py` (NEW)

**Bug fixed**: PAYSLIP parser was posting full gross+CPF split journals (Dr POSB net + Dr 1211/1212/1213 CPF + Dr 5500 Tax / Cr 4110 Salary). POSB cutover was independently posting the same POSB salary inflow as Dr POSB / Cr 1190 suspense. Net: POSB balance overstated by ~$19k across 6 months, suspense overstated by the same.

- **`--scan`**: classifies each payslip + each POSB salary candidate into `matched_dup` / `orphan_payslip` / `missing_payslip` (3 buckets).
- **`--fix-dups`**: voids POSB cutover-side journal where a PAYSLIP journal covers it. Audit row in `salary_reconcile_log`.
- **`--report`**: emits `/data/missing_payslips.csv` chase-list for months without payslip data.
- **Cross-doc guard** in `posb_cutover_2026.py`: new `payslip_journal_covers()` check before posting a Salary tx → skipped 7 on re-replay. **Zero duplicates created** in subsequent runs.

**Result**: $19,001.92 of phantom POSB inflow voided. 48 missing-payslip rows logged ($65,431 of orphan POSB salary inflows awaiting payslip PDFs).

**Table**: `salary_reconcile_log` (status, payslip_id, payslip_journal_id, posb_journal_id, voided_journal_id, period_end, amount, employer_guess, notes).

#### 4. CC commitment matcher — cumulative-payment mode

`app/cc_commitment_tracker.py` rewritten to handle real CC payment patterns:
- **Old logic**: single-match — looked for one POSB→CC outflow exactly equal to total_due. Reported partial pays as `overdue`.
- **New logic**: sums ALL POSB→CC outflows in `[stmt_date, stmt_date+33d]` window. Compares cumulative to total_due AND minimum_due. Computes interest exposure on unpaid balance.

Status taxonomy (4-tier):
- `matched_fully` — cumulative ≥ total_due
- `matched_partial` — cumulative ≥ minimum_due, < total_due → bearing 27.80% p.a. on unpaid
- `underpaid` — paid > $0 but < minimum_due → late fee + interest
- `overdue` — no payment → late fee + interest on full balance

**New columns**: `cumulative_paid, unpaid_balance, payments_jids, annual_interest_rate, estimated_interest, interest_warning`.

**Interest rates seeded**: DBS 27.80%, Maybank 27.90%, SC 27.80%, HSBC 27.80%.

After POSB 2024-2025 historical cutover: 23 `matched_partial`, 1 underpaid, 6 overdue, 2 fully paid.

### Tier 2 — Data corrections

#### 5. Historical POSB cutover extension to 2024-01-01

`posb_cutover_2026.py` parameterized with `--since YYYY-MM-DD` (was hard-coded 2026-01-01).

- Voided 1,522 FIREFLY_BRIDGE POSB journals (2024-2025)
- Replayed 2,293 transactions via universal_pdf_parser + router
- Zero errors
- 7 idempotent-skipped (payslip-covered)

**This is the Firefly decouple for 2024-2025 POSB data** — Firefly bridge no longer the source of truth for any POSB tx ≥ 2024-01-01.

#### 6. Idempotency-counter bug in posb_cutover

`post_journal()` returns existing journal_id on idempotent skip, not None. The cutover's counter was incrementing `posted` on every "skip". On re-runs with the same external_id, the script reported `posted=2293` while actually skipping all 2293 (no new journals). Workaround: `_revoid_replay_posb.py` voids existing direct journals before replay so external_ids clear and new rules apply.

### Tier 3 — Diagnostics + scoping (not yet built)

- **CC cutover dry-run** (`cc_cutover.py --since 2024-01-01`) — DBS CC: 292 tx ready, Maybank CC: 0/8 (parser bug), HSBC CC: 0/0 (was OCR — now resolved by Tier 1 #1, awaits schema-tuning).
- **Hit-rate diagnostic** (`_hit_rate.py`) — per-source + per-year breakdown of suspense vs classified.
- **Suspense overview** (`_suspense_overview.py`) — anatomy of 1,725 suspense legs: tx_type frequencies, $ buckets, carrier signal patterns, top high-value misses.
- **Historical scope** (`_scope_firefly_historical.py`) — 1,522 bridge journals 2024-2025.

### Tier 4 — Architectural decisions (recorded to memory)

- **V4 = Mode A self-hosted only** (no managed-SaaS data path). Trust proofs: reproducible builds, fail-closed HTTP allowlist, privacy.json manifest, --no-egress self-test, client_work/ isolation. (`project_sentinel_finance_v4_privacy.md`)
- **Architecture spine articulated**: `_INBOX → ocr_normalize → classifier → piles (statements / payslips / policy / obligation source / recurring evidence) → registries → reconcilers → reports`.
- **Two new doc-classifier piles identified** (not yet built): `obligation_source` (loan contracts, CC facility letters, insurance policies) and `recurring_evidence` (utility bills, subscription receipts, telco bills). Source docs establish obligations; transactions are evidence.
- **Three missing registries** identified as the next-biggest unlock: `recurring_obligation_registry`, `insurance_policy_registry`, `subscription_registry`. The `salary_reconciler` is the reference implementation pattern for what `recurring_reconciler` will look like generalised.

### Tier 5 — Pipeline closing (built later same session)

#### 7. HSBC parser fully working — `finance/statement_schemas/hsbc-cc.yaml`

- Schema tuned for HSBC's two-date format (post + tran). Regex updated.
- `ocr_text_cleanup` rules added to compensate for tesseract letter-confusions (`oz→02`, `ol→01`, `bgs→DBS`, decimal-vs-comma fixes).
- `statement_date` block added (closing date = the "to" date in the statement period).
- `parse_multiline_transactions` now applies schema's `ocr_text_cleanup` rules before regex match.
- CR-suffix check made space-tolerant (`205.00CR` vs `205.00 CR`).

**Result**: 19 HSBC statements parsed, **103 transactions** extracted across 2024-12 to 2026-04. 83 actually posted (20 idempotent-skipped via cross-doc transfer-pair dedup with POSB parser).

`cc_cutover.py` patches:
- **CC-charge lifestyle lump**: any charge with router confidence <50 lands in 5190 (lifestyle expense) rather than 1190 suspense. Finance charge → 5410. Late fee → 5450.
- **CC-payment cross-doc dedup**: payments without specific routing default to POSB (1111) → cross-doc transfer-pair ext_id matches POSB-side parser.

HSBC contra-leg breakdown after rerun: 64 lifestyle, 22 POSB payments (dedup'd), 12 bank fees, 4 transport, 1 finance charge.

#### 8. Eager filesystem watcher — `app/ocr_watcher.py` (NEW)

`watchdog`-based recursive folder watcher. Auto-normalizes any document dropped into a watched path within ~3 seconds (DEBOUNCE_SECONDS=2.5s after last event). Idempotent — re-events on cached files are no-ops.

**Validated**: copied an HSBC PDF into `_INBOX`, OCR cache file appeared at expected path 4 seconds later (405KB JSON with full word list).

Run: `docker exec -d portfolio-mcp python -m app.ocr_watcher --watch "/onedrive/Sentinel Finance/_INBOX" --watch "/onedrive/Sentinel Finance/Statements by bank"`

Includes startup-reconcile pass to catch files dropped while the watcher was offline.

#### 9. Recurring obligation registry — `app/recurring_reconciler.py` (NEW) + `finance/recurring_obligations.yaml` (NEW)

**Concept**: unified table for known recurring outflows (insurance / ILP / subscription / utility / loan / tax / charity / other). Replaces hard-coded router rules with a queryable, source-doc-backed registry. The `salary_reconciler` is the prototype; this generalises the pattern.

**Tables**:
- `recurring_obligation_registry` — `(name, kind, contra_coa, expected_amount, amount_tolerance, frequency, expected_day_of_month, identifier_patterns JSON, journal_kind, ...)` UNIQUE on name
- `recurring_reconcile_log` — `(status, obligation_id, journal_id, voided_journal_id, ...)` for audit trail

**CLI**:
- `--seed`: read YAML → DB upsert (idempotent on `name`)
- `--scan`: report tx matches without modifying GL
- `--apply`: void original suspense journal + re-post with correct contra_coa, log audit row
- `--orphans`: detect recurring patterns (≥2 occurrences same amount + narration prefix) with NO registry hit → "what is this?" alert list
- `--status`: summary of registry coverage + log

**Seeded**: 13 obligations from YAML — Singlife Term Life, Tokio Marine, AIA, Singlife Savvy Invest (ILP P4064051), ChatGPT, Anthropic Claude, Apple iCloud, Google One, SP Group, Singtel, EZ Loan, Lending Bee, Sands Credit.

**First-run orphan detection** surfaced **32 unknown recurring patterns** awaiting user identification — top hits include `$3.05 × 28 GIRO`, `$59.70 × 14 Standing Instruction`, `$418.45 × 8 GIRO`. Each represents an obligation the user hasn't registered yet.

### Closing state

- **All 3 follow-up tasks built tonight** (HSBC parser, watcher, recurring registry).
- **OCR + classifier loop now closes end-to-end** for any document type, any source format (PDF / JPG / PNG / HEIC).
- **Cross-pipeline integrity** working for: payslip ↔ POSB (salary_reconciler), POSB ↔ CC (transfer-pair ext_id), POSB ↔ recurring obligations (recurring_reconciler).

### Issues NOT yet fixed (carry-forward)

- **Maybank CC parser**: Amount column extraction fails — schema's column-position config doesn't match actual PDF layout. ~8 tx/month invisible.
- **48 missing payslips** ($65,431 orphan POSB salary inflows): YourAgency 2025/2026 + 2024 unknowns. Need PDFs from user.
- **4 YourAgency orphan payslips**: parsed but no POSB date+amount match within window. Probably paid to a different account, or amount tolerance issue.
- **YAML obligation amounts are placeholders**: most need to be filled with real values from actual statements/policy docs to achieve registry → reconciler match coverage.
- **Firefly bridge HSBC journals not voided**: cleanup pass needed to retire bridge journals for accounts that now have direct-path coverage.
- **ASCII diagram co-author tool**: user-requested. Proposed as `portfolio-mcp/static/diagram.html` — single static HTML with textarea + canvas + bidirectional sync. Not yet built.
- **`sf_income_statement` + `sf_cash_forecast` end-to-end validation**: drafted modules exist but not run against full 2024-2026 data yet.

### Memories saved this session

- `project_sentinel_finance_v4_privacy.md` — Mode A decision
- `feedback_lifestyle_expense_lumping.md` — 6 router rules (debit card / POS / bill / FAST+PayNow / Cash / Funds Transfer / Standing Instruction / Wise / MyPP)

---

## [1.17.1] — 2026-05-14 — POSB PDF multi-line parser (recipient identifier preserved)

**Discovery**: POSB monthly PDFs DO contain recipient name + reference — they're just 2-5 continuation lines BELOW the date+type+amount summary line. The old `posb_to_firefly_csv.py` was reading only the first line. The data was there all along.

Example raw PDF text:
```
04 Feb FAST Payment / Receipt 498.72
  PayNow Transfer 5636354
  To: EZ LOAN PTE.LTD.
  EL-14603 2026
  Other
```

**`app/posb_pdf_to_gl.py` (NEW)** — multi-line tx extractor with classifier:
- Detects `^<DD> <MMM> <TYPE> <amount>` lines via regex
- Captures all continuation text until the next start line or "Balance Carried Forward"
- Builds full_description = TYPE + joined continuation lines
- 60+ classification rules (entity names like `EZ LOAN PTE`, `LENDING BEE`, `SANDS CREDIT`, `Wise:`, `AZ UNITED`, `HENDERSON`, `SAF IMPREST`, `SCBLSG22BRT`, `SINGAPORE LIFE`, merchants, etc.)
- Direction (in/out) determined by balance-column delta

**Full historical scan (Jan 2024 → Apr 2026, 28 months):**
- **2,217 transactions parsed** across all monthly PDFs
- 1,307 classified (59%) into 30 distinct CoA codes
- 910 unclassified (41%) → suspense for incremental rule expansion
- Total inflow: SGD 149,914 ; total outflow: SGD 228,026

**Top classified flows:**

| CoA | $ | Top label |
|---|---:|---|
| 4110 | 76,198 | AZ United salary |
| 4900 | 24,308 | Incoming PayNow |
| 2211 | 23,336 | SC BT disbursement |
| 2113 | 18,903 | SC CC payment |
| 2114 | 18,650 | HSBC CC payment |
| 2111 | 14,944 | DBS CC payment |
| 4120 | 9,352 | YourAgency salary |
| 2112 | 8,061 | Maybank CC payment |
| 5340 | 7,438 | Singlife premium |
| 1222 | 7,080 | Singlife ILP premium |
| 2223 | 6,362 | Sands Credit (12 × $530.19 monthly) |

**Output**: `_OUT/posb_full_extract_2024-2026.csv` for user review.

**Posting GATED** behind cutover decision (Task #81). When posted, this becomes the v2.0 POSB data source; existing FIREFLY_BRIDGE POSB journals get voided.

## [1.17.0] — 2026-05-14 — POSB CSV → Sentinel GL direct (Firefly-decouple foundation)

User directive 2026-05-14: decouple Sentinel Finance from Firefly III. POSB iBanking CSV exports preserve all the recipient information (counterparty name, references, transaction codes) that the PDF→CSV→Firefly path was stripping. This commit builds the parallel direct-ingestion pipeline.

**`app/posb_csv_to_gl.py` (NEW)** — reads POSB iBanking CSV exports and posts journals DIRECTLY to Sentinel GL, bypassing Firefly entirely.

Pattern-based classifier (initial 30+ rules):

| Pattern | → CoA | Direction | Example |
|---|---|---|---|
| `Wise:` | 1113 Wise | out/in | Wise top-ups |
| `AUTO TOP UP FROM CASHLINE` | 2121 DBS Cashline | in | Cashline drawdown |
| `AZ UNITED` / `HENDERSON SECURITY` | 4110 / 4120 | in | Salary |
| `SAF IMPREST` | 4130 | in | SAF reimbursement |
| `SCBLSG22BRT` | 2211 SC Loan/BT | in | SC Balance Transfer disbursement |
| `SINGAPORE LIFE` / `TOKIO MARINE` | 5340 | out | Insurance premiums |
| `ATOME` | 2115 Atome | out | BNPL repayment |
| `4119/4966/5498/4835...` | 2111/2/3/4 | out | CC bill payment |
| F&B / Transport / Shopping / Subscriptions / Internet | 51xx | out | Merchant categories |
| `PayNow To: <name>` | 5170 / etc. | out | Personal transfers |
| (no match) | 1190 Suspense | — | Unclassified for manual review |

**Smoke test on May 2026 POSB CSV (46 tx):**
- 30 classified correctly across 12 CoA codes (65% coverage on first pass)
- 16 unclassified → Suspense (mostly merchants not yet in pattern list; classifier expands incrementally)

**Idempotency**: each tx gets `external_id = "posbcsv:" + sha256(date|code|desc|amount|ref)`. Re-runs skip dups.

**NOT YET POSTED**: dry-run only. Existing Firefly-bridged journals would conflict (different external_id namespace). Cutover requires:
1. Dedup strategy: for each POSB CSV row, find + void the corresponding `firefly_tx:N` journal (match on date+amount+POSB account), then post the CSV-derived one
2. OR cut-over by date: void ALL FIREFLY_BRIDGE journals from a chosen date forward, then re-import via CSV
3. OR keep historical Firefly data untouched, only use CSV path for new POSB tx going forward

Recommendation: option 3 (forward-only cutover) — least risky, preserves audit trail.

Task #81 queued for execution. Decouple plan to follow.

## [1.16.1] — 2026-05-14 — Amount-match reclassifier + decouple-from-Firefly plan

Autopilot extension.

**`app/amount_match_reconciler.py` (NEW)** — surfaces and re-routes Firefly-bridged journals that landed in `5190 General Expense (parked)` or `4900 Other Income` because the POSB PDF source lacked recipient identifier. Matches outflow amount against `credit_facilities.instalment_amount` (±$2 tolerance) and inflow amount against known MEPS disbursement events.

**Applied 28 reclassifications** = SGD 16,394.95 moved from catch-all accounts to specific facility liabilities:

| Match | Count | Total SGD |
|---|---:|---:|
| UOB CashPlus minimum ($153.65) | 6 | 921.90 |
| SC Balance Transfer minimum (~$93-94) | 11 | 1,033 |
| Maybank CreditAble instalment ($120) | 4 | 482 |
| EZ Loan monthly ($500-498.72) | 3 | 1,500 |
| Sands Credit ($530.19) | 0 | — |
| Lending Bee ($532.76) | 0 | — |
| MEPS disbursements (BT $5,600 + Maybank CA $6,300) | 2 | 11,900 |
| Other date+amount specifics | 2 | 558 |

**Decouple-from-Firefly directive (user 2026-05-14)**

POSB iBanking statement PDFs DO NOT include the recipient identifier on FAST/MEPS rows. The PDF→CSV converter pipeline can't preserve info that the PDF never had. The architectural fix is to drop Firefly as the data layer and use POSB **iBanking CSV exports** directly (which DO include recipient name + reference per memory `reference_posb_csv_export`).

Task #81 opened. Plan in `journal/2026-05-14-decouple-firefly.md` (to be authored).

## [1.16.0] — 2026-05-14 — Coinbase CSV reader (extract-only)

Autopilot chunk 5 (final).

`app/coinbase_csv_parser.py` (NEW) — reads Coinbase Advanced Trade tx-history CSV exports and produces aggregate summary. **Extract-only**, no journal posting yet — journal posting deferred until either:
- (a) Coinbase API key drops (preferred path; full history, idempotent, no manual exports needed), OR
- (b) User confirms the bridge story (Coinbase Sell → POSB Withdrawal is already bridged via Firefly POSB-side; auto-posting Coinbase rows would double-count)

**Verified output (2 CSVs in `Crypto/`, 33 tx, 2026-04-01 → 2026-05-09):**

| Type | Asset | Count | Qty | Total $USD | Fees |
|---|---|---:|---:|---:|---:|
| Deposit | SGD | 6 | 3,106.50 | 2,424.01 | — |
| Buy | USDC | 5 | 2,423.08 | 2,424.01 | 0.94 |
| Send | USDC | 5 | -2,421.76 | -2,421.76 | — |
| Receive | USDC | 5 | 973.63 | 973.63 | — |
| Sell | USDC | 5 | -973.63 | 972.72 | — |
| Withdrawal | SGD | 5 | -1,238.60 | 972.72 | — |
| Convert | USDC↔USDT | 2 | round-trip | — | — |

Workflow inferred: SGD deposit → USDC buy → send to external (DeFi/wallet) → eventually some return → sell → SGD withdrawal back to bank. Net Coinbase residual: ~$1,868 SGD remained.

## [1.15.5] — 2026-05-14 — Morningstar SG → nav_history persistence

Autopilot chunk 4.

`morningstar_sg._write_nav_history()` (NEW) — after each daily Morningstar NAV refresh, append/update rows in `nav_history` table. Source tagged as `"morningstar"`. Idempotent via UniqueConstraint (fund_id, nav_date).

Wired into `refresh_all()`: invoked after the funds.yaml save when `updated > 0` and not dry-run. Wrapped in try/except so a DB failure doesn't block the YAML update (non-fatal).

**Outcome**: time-series persisted. Bot queries like "what was Tokio Marine ILP value at 2025-12-31?" can now resolve via nav_history × current fund holdings. Currently 12 rows; will grow daily as scheduled 06:00 Morningstar cron fires.

## [1.15.4] — 2026-05-14 — GXS FlexiLoan line regex + balance markers

Autopilot chunk 3.

`parse_gxs()` line regex now handles GXS FlexiLoan's running-balance trailing column:

```
1 Dec 2025 Opening balance -5,431.76         (balance marker — SKIP)
30 Dec 2025 Loan repayment 180.00 -5,251.76  (amount + running bal)
```

Regex updated to optionally consume the trailing `-?[\d,]+\.\d{2}` running-balance column. Added narration-based filters:
- "Opening balance" / "Closing balance" → skip (markers, not transactions)
- "Loan repayment" / "Repayment" → `kind=payment` (already bridged via Firefly POSB-side)
- "Interest" → `kind=interest`
- "Late fee" → `kind=fee`

**Smoke test (GXS Dec'25):**
- Statement date: 2025-12-31
- Previous balance: $5,431.76 ✓
- Closing balance: $5,251.76 ✓
- 1 line: Loan repayment $180 → kind=payment → not posted (correct; POSB-side handles)

**P&L impact**: GXS FlexiLoan posts remain 0 (correct — the monthly $180 repayment is bridged via Firefly). What changed: the FlexiLoan's previous/closing balances now populate `statement_registry`, so bot queries about FlexiLoan history return clean data. Statement_registry now has full 15-month GXS coverage Sep'24 → Mar'26.

## [1.15.3] — 2026-05-14 — YourAgency payslip parser (weekly daily-rated format)

Autopilot chunk 2 (payslip backfill).

`payslip_parser._parse_youragency()` (NEW) — detected when text contains "YourAgency Security" or "Daily Rated Payslip". Aggregates ALL weekly slips in one monthly PDF into a single ParsedPayslip row keyed by latest Payment Date.

**Features:**
- Multi-week regex over `TOTAL BASIC SALARY` / `OT AMOUNT` / `ALLOWANCES` / `MISC. PAYMENTS` / `LESS EMPLOYEE CPF` / `LESS ADV/LOAN` / `TAKE HOME PAY`
- Reconciliation plug: when `gross - ecpf - other_ded ≠ net` (typically ethnic-fund / unparsed deduction), gap goes to `other_deductions` with a parse_errors note
- Date fallback: when "Payment Date" header absent (older format), derives from latest "Site Date" + 7 days

**Smoke test (4 YourAgency PDFs):**

| File | Gross | Net | Period end | Journal |
|---|---:|---:|---|---|
| Dec'25 | 2,030 | 1,624 | 2025-12-31 | j=5467 |
| Jan'26 | 1,350 | 1,069 | 2026-02-03 | j=5468 (+$4.50 plug) |
| Nov'25 | 1,595 | 1,272 | 2025-12-03 | collided with Dec ext_id YYYY-MM |
| Oct'25 | 1,190 | 952 | 2025-10-31 | j=5469 |

**Known: YourAgency external_id format `payslip:youragency:YYYY-MM`** can collide when the last week's payment date falls into next month (Nov'25 PDF's last paydate is 03-Dec-2025). The registry rows are correctly unique on (employer_key, period_end), but the journal collides. Either fix: include period_start in external_id, or fix YourAgency period_end to month-end of work period instead of last-payment-date. Queued.

**P&L impact**: 2025 YourAgency income now properly recognized: $4,815 across Oct+Dec slips (Nov got collided). 2026 picks up $1,350 for January.

## [1.15.2] — 2026-05-14 — P&L cleanup: scrub $46k false expense (balance-sheet movements)

Autopilot chunk 1. Three buckets of false-expense journals in `5190 General Expense (parked)` identified and voided:

| Pattern | Source | Count | $ Removed | Reason |
|---|---|---:|---:|---|
| `charge: CLOSING BALANCE` | CC_STMT:dbs_cashline | 16 | 38,111.78 | Pre-fix parser counted statement-marker row as a transaction |
| `charge: Monthly Instalment` | CC_STMT:uob | 36 | 2,765.70 | Scheduled loan repayment (P+I), reduces liability — not a new expense |
| `charge: FUNDS TRANSFER (SPECIAL INT RATE)` | CC_STMT:dbs_cashline | 2 | 5,600.00 | Cashline drawdown into POSB — balance-sheet movement, not P&L |
| | | **54** | **$46,477** | |

**Parsers patched** (`app/cc_statement_parser.py`):

- `parse_uob()` — added `"monthly instal" in desc` → `kind=payment` (skipped from posting, already bridged via Firefly POSB-side)
- `parse_dbs_cashline()` — added `"funds transfer" in desc` → `kind=payment` (same rationale)

P&L impact: 5190 General Expense drops from $25,189.83 → ~$18,712 (deeper sweep still needed; this is the first 65% of that account scrubbed).

## [1.15.1] — 2026-05-14 — Opening journal posted + GL gap-fill + P&L renders

Closed the loop user flagged earlier: "we haven't updated the income statement yet."

**Three concrete actions:**

1. **Opening journal posted as journal #5223**: `python -m app.opening_balance_extract --cutoff 2026-01-01 --post`
   - DR side: SGD 113,278.09 (POSB + CPF + Crypto + savings)
   - CR side: SGD 36,855.01 (9 liabilities + Sands moneylender)
   - Plug to 3100 Retained Earnings: SGD 76,423.08
   - **Bug fixed in `post_opening_journal`**: floating-point rounding (-1.42e-14 on SC Savings near-zero) failed `InvalidGLLineError`. Now rounds to 2dp + skips lines under SGD 0.005. Equity plug routed to 3100 (was 3210 which doesn't exist).

2. **HSBC + others backfilled**: `python -m app.cc_pipeline` posted **889 new GL journals** (684 charges, 198 interest, 7 fees, 74 refunds). HSBC went from 0 → 31 GL journals; SC from 218 → 384; DBS CC from 231 → 231 (no change — already complete).

3. **Income statement renders cleanly** from GL:
   - **YTD 2026 Revenue**: SGD 30,182.66 (Salary AZ United 8,481 + YourAgency 1,803 + Govt Transfers 7,080 + Other Income 12,564)
   - **YTD 2026 Expense**: SGD 31,221.32 (General Expense parked 25,189 + Finance Costs ~2,629 + Insurance + Misc)
   - **Net P&L**: SGD −1,038.66 (loss)

**Known issues surfaced by the rendered P&L** (queued, not in this commit):
- `Other Income (4900)` $12,564 is too high — catch-all for unclassified CR; needs reclassification sweep
- `General Expense (parked) (5190)` $25,189 too high — same catch-all on DR side; same fix needed
- `5500 Tax` missing — payslip parser writes here but only 1 payslip posted (rest still need invocation)
- **GXS FlexiLoan parser still posts 0 journals** — line regex doesn't handle running-balance trailing column. Separate fix needed.

GL state: **2,997 posted journals** across 14 source documents. Up from 2,758 (+239 new from this commit's pipeline run + opening).

## [1.15.0] — 2026-05-14 — Bank/Product universe + Parser registry (two-tier doc)

Two-tier separation so bot queries about coverage become composable:

**`finance/bank_product_registry.yaml`** (NEW) — universe of banks + their product lines (the "what exists" map). 24 banks × ~50 products covering SG retail banking:
- Local: DBS group (POSB / DBS / Vickers), OCBC, UOB
- Foreign: Maybank, HSBC, Standard Chartered, Citi
- Digital: GXS, Trust, MariBank, ANEXT
- BNPL / Moneylenders: Atome, EZ Loan, Lending Bee, Sands Credit
- ILP: Singlife, Tokio Marine, AIA, Prudential
- E-money: Wise
- Crypto: Coinbase, Crypto.com
- Public: CPF Board, IRAS

Each entry has: slug, legal_name, regulator, products[]. Each product has: slug, display, type, statement_formats, account_number_re, notes.

**`finance/parser_registry.yaml`** (refactored) — what parsers Sentinel has. Each parser declares `handles: { bank: <slug>, product: <slug> }` as a FK to bank_product_registry. Plus `sub_handles` for parsers that cover multiple (e.g. SC parser handles cc + balance_transfer in one PDF).

**`app/parser_registry.py`** (refactored) — reader that joins the two registries × statement_registry × payslip_registry. CLI modes:

```bash
python -m app.parser_registry                # 21 supported parsers + coverage stats
python -m app.parser_registry --banks        # 24 banks × ~50 products, coverage flag
python -m app.parser_registry --gaps         # (bank, product) tuples we don't parse — backlog
python -m app.parser_registry --slug sc      # detail for one parser
python -m app.parser_registry --check ocbc cc  # is (bank, product) supported? yes/no
python -m app.parser_registry --json         # structured dump for bot
```

**Verified output:**
- 21 parsers covering 14 distinct (bank, product) pairs
- Coverage stats joined from statement_registry: 133 samples across 8 banks
- Gap analysis: OCBC, Citi, Trust, MariBank, OCBC, all mortgages, all hire-purchase, several ILPs unparsed
- `--check dbs cc` → ✓ SUPPORTED (1 parser, 18 samples)
- `--check ocbc cc` → ✗ UNSUPPORTED (clear path to add)

**Architectural rationale:** future "shared brain" (v4.0) community contributions submit (bank_product_registry entry, parser fingerprint) tuples. Composable registry lets users see what they're missing BEFORE they pay for a Sentinel subscription.

## [1.14.0] — 2026-05-14 — Registry tables (statement / payslip / nav history)

Three new SQLAlchemy tables for bot-queryable historical metadata. Lets the assistant answer questions like "what are my 12 SC CC statement dates?" with one SELECT instead of re-parsing PDFs.

**New tables** (in `app/ledger.py`):

- `statement_registry` — per parsed CC/loan/bank statement. Fields: facility_id, bank, statement_date, period_start/end, previous/closing balance, minimum_due, payment_due_date, credit_limit/available, line_count, source_path, parsed_at, extras (JSON), created/updated_at. UniqueConstraint on (facility_id, statement_date).
- `payslip_registry` — per parsed payslip. Fields: employer, employer_key, period_start/end, payment_date, basic_pay, allowances, gross_pay, employee/employer_cpf, fund_deductions, other_deductions, sdl, net_pay, journal_id (FK), source_path. UniqueConstraint on (employer_key, period_end).
- `nav_history` — per fund × date NAV from Morningstar / FSMone / manual. Fields: fund_id, fund_name, nav_date, nav_price, currency, source. UniqueConstraint on (fund_id, nav_date).

**Population:**
- `cc_pipeline.upsert_statement_registry()` — called inside `post_statement()` on every parse. Idempotent.
- `payslip_parser._upsert_payslip_registry()` — called inside `post_payslip_journal()`. Idempotent.
- `morningstar_sg.refresh_all()` — TODO: append nav_history row on every NAV update (currently only updates funds.yaml in-place; needs follow-up patch).
- `app/backfill_registries.py` (NEW) — one-shot backfill: walks CC_Statement/, Payslips/, funds.yaml. Commit-per-row to avoid same-batch UniqueConstraint collisions.

**Backfill results:**
- statement_registry: 133 rows across 8 facilities (dbs_cc, dbs_cashline, gxs, hsbc_cc, maybank_ca, maybank_cc, sc, uob). Date range 2024-11 → 2026-04.
- payslip_registry: 6 rows (AZ United Pte Ltd).
- nav_history: 12 rows (one per fund in funds.yaml at last_nav_date).

**Example queries the bot can now run:**
```sql
-- 12 historical SC CC statement dates
SELECT statement_date FROM statement_registry WHERE bank='sc' ORDER BY statement_date;

-- Total gross from AZ United in 2025
SELECT SUM(gross_pay) FROM payslip_registry
WHERE employer_key='az_united' AND payment_date BETWEEN '2025-01-01' AND '2025-12-31';

-- Tokio Marine portfolio MV at 2025-12-31 (via fund × units × NAV)
SELECT fund_id, nav_price FROM nav_history
WHERE nav_date <= '2025-12-31' GROUP BY fund_id;
```

Known TODOs:
- Wire `morningstar_sg` to write nav_history rows daily.
- statement_registry.facility_id currently = bank slug; should be linked to credit_facilities.id when payments are tracked.

## [1.13.0] — 2026-05-14 — Pass A: 5-digit ILP/CPF fund leaves + per-fund journal posting

Per-fund granularity for ILP and CPF Investment Scheme positions. Existing 4-digit codes for cash/CC/loans/expenses stay intact (Pass B full renumbering deferred).

**New 5-digit fund leaves (16 added, CoA 95 → 111):**

```
1214 CPF Investment Scheme   →  HEADER (was leaf)
  12141 FTIF Franklin US Opportunities SGD (CPF)
  12142 Allianz Global High Payout AM SGD (CPF)
  12143 Amova Japan Dividend Equity SGD-H (CPF)
  12144 Amova Singapore Equity SGD (CPF)
  12145 abrdn Singapore Equity SGD (CPF)
  12149 CPF IS — Unallocated   (catch-all)

1221 Tokio Marine ILP        →  HEADER
  12211 Franklin Technology SGD-H
  12212 Guinness Global Innovators USD
  12213 Infinity US 500 SGD (Tokio)
  12214 Canaccord Genuity Opportunity SGD-H
  12215 FSSA Regional India SGD (Tokio)
  12219 Tokio Marine — Unallocated

1222 Singlife Savvy Invest   →  HEADER
  12221 Allianz Inc & Growth AMH2 SGD
  12222 BGF World Healthsci A2 SGD-H
  12223 Infinity US 500 SGD (Singlife)
  12229 Singlife — Unallocated
```

**Changes:**

- `app/ledger_seed.py` — 16 new leaves; 1214/1221/1222 flipped to `is_postable=False`.
- `finance/funds.yaml` — `coa_code` field added to every holding (13 funds). Becomes the single source of truth for fund → leaf mapping.
- `app/ilp_statement_parser.post_ilp_journal()` — multi-line journal posting, one DR/CR pair per fund. Reads `funds.yaml` to resolve fund-name → coa_code; falls back to provider-specific Unallocated leaf when fuzzy match fails.
- `app/firefly_bridge.FIREFLY_ACCT_TO_COA` — 147/162/163 remapped to 12149/12219/12229 (Unallocated leaves) since parent codes are now headers.
- `app/opening_balance_extract.FIREFLY_ASSET_TO_COA` + `CoA_NAMES` — same remapping + per-fund labels.
- `app/cpf_statement_parser.CPF_IS` — points to 12149 (Unallocated). Per-fund CPF IS tx not present in statement.
- `app/journal_service.post_journal()` — voided journals no longer block re-post (allows re-doing after a void).
- `app/migrate_coa_pass_a.py` (NEW, but ended up unused) — kept for reference. GL uses `account_id` FK not `account_code` string, so re-routing wasn't required.

**Smoke-tested:**
- Re-seed: 95 → 111 CoA (43 assets, +16 fund leaves).
- ILP journal #5222 posted with **6 lines, 3 per-fund pairs**: 12221 (Allianz, $6.15), 12229 (BGF fuzzy miss → Unallocated, $5.53), 12223 (Infinity, $8.85).
- `opening_balance_extract` unchanged: SGD 36,855.01 liab / SGD 113,278.09 assets / SGD 76,423.08 net.
- `credit_utilization_audit`: 0 broken ties.

**Known:** Singlife fund-name fuzzy matching only catches 2 of 3 (Allianz, Infinity); BGF's "BGFWrldHealthsci" doesn't match "BGF World Healthsci" because regex extracts wrong text from concatenated Singlife PDF. Cleanup via better Singlife parser is separate work; for now the Unallocated leaf absorbs.

**Pass B (full 4-digit → 5-digit renumber) deferred** — user has the per-fund organization they wanted; full migration is consistency cleanup not blocking anything.

## [1.12.4] — 2026-05-14 — CoA: add 2115 Atome leaf (fix bridge errors)

`firefly_bridge.FIREFLY_ACCT_TO_COA[176]` mapped Atome (BNPL) to header CoA `2110 Credit Cards`, which isn't postable — 4 errors per bridge run.

- New leaf: `2115 Atome (BNPL)` under 2110. Seeded in `ledger_seed.py`.
- `FIREFLY_ACCT_TO_COA[176]: "2110" → "2115"`
- Re-seed brought total CoA from 94 → 95 accounts (LIABILITY: 19 → 20).
- Re-bridge verified: 0 errors (was 4).

## [1.12.3] — 2026-05-14 — Cron job resilience + Firefly→GL bridge auto-schedule

Surfaced bug: the Wise sync was scheduled at 06:30 daily but **never auto-reached the Sentinel Finance GL** because `firefly_bridge.py` had no scheduled trigger. Wise → Firefly worked; Firefly → GL only ran when manually invoked.

Also: every container rebuild past a cron's hour caused that day's job to be silently skipped — APScheduler default behavior is to NOT catch up missed cron triggers. This bit all 4 daily jobs (backup 02:00, NW 02:30, Morningstar 06:00, Wise 06:30) during today's v1.10.x → v1.12.x rebuild cycle.

Fixes in `app/main.py:_lifespan`:

- **New job**: `_firefly_bridge_job()` runs `firefly_bridge.bridge()` for last 7 days. Scheduled 07:00 daily (after Wise 06:30 + Morningstar 06:00). Idempotent via `external_id`.
- **Startup catch-up** added for `wise_sync` and `firefly_bridge` (NW snapshot already had this). Container rebuild past schedule → job runs once on startup.
- **`misfire_grace_time=3600` + `coalesce=True`** added to all 4 daily cron jobs (`daily_backup`, `nw_snapshot`, `morningstar_nav`, `wise_sync`) plus the new `firefly_bridge`. Missed runs within 1-hour grace fire on next scheduler tick; multiple missed runs collapse to one.

Verified after rebuild:
- Wise startup catch-up: SGD 21.73 synced to Firefly acct 168
- Firefly bridge startup catch-up: 33 new GL journals (May 7-14), 14 skipped, 4 errors (pre-existing CoA 2110 header-not-postable issue, separate)

Known: 4 bridge errors per run from Firefly tx mapped to header CoA `2110 Credit Cards` (not postable). Separate fix needed — either re-map to a leaf account or add header → leaf default routing in `firefly_bridge.acct_to_coa()`.

## [1.12.2] — 2026-05-14 — Coinbase report classifier improvements

`_rule_crypto_report` now detects Coinbase exports by content fingerprint, not just brand name in head:

- **Coinbase CSV** without "coinbase" in first 800 chars (no branding in tx data) is detected via header fingerprint: `Quantity Transacted` + `Price at Transaction` columns are unique to Coinbase Advanced Trade exports. Conf 0.85.
- **Coinbase PDF** detected via `Coinbase Global` / `Coinbase, Inc` markers. Conf 0.9.
- Sub-categories disambiguated: `coinbase_csv` vs `coinbase_pdf` vs `crypto_com_csv` vs `crypto_com` so the future Coinbase parser can dispatch on sub_category.

Smoke-tested: 4 Coinbase exports (2 PDFs + 2 CSVs, named with UUIDs from mobile upload) classified correctly via `inbox_pipeline --apply` → routed to `Crypto/` folder.

Canonical rename for Coinbase reports (e.g. `Coinbase Apr'26.pdf` from date-range extraction) deferred until the Coinbase API/CSV parser lands.

## [1.12.1] — 2026-05-14 — Inbox pipeline Phase 2.5 (parser dispatch)

`inbox_pipeline.py` now dispatches each classified file to its pile-specific parser when `--post` is passed. Flag model:

- (none) — dry-run; show classifications only
- `--apply` — move files to pile folders (no journals)
- `--apply --post` — move + parse + journal (full pipeline)
- `--apply --post --watch` — daemon mode (30s poll)

Dispatch table (handled internally by `_dispatch_parser`):

| Category | Parser | Journals posted |
|---|---|---|
| `cc_statement` | `cc_pipeline.post_statement()` | per-line charges/interest/fees |
| `payslip` | `payslip_parser.post_payslip_journal()` | one salary journal |
| `loan_agreement` | `loan_agreement_parser.upsert_facility()` | DB row (no GL journal) |
| `ilp_statement` | `ilp_statement_parser.post_ilp_journal()` | one charges journal |
| `cpf_statement` | `cpf_statement_parser.post_cpf_row_journal()` per row | INV/INT/insurance (CON skipped) |
| `bank_statement`, `noa_tax`, `insurance_policy`, `noise`, `crypto_report` | filed only | 0 |
| `unknown` (conf < 0.7) | routed to `_QUEUE/`, never parsed | 0 |

Errors per file are caught + recorded as `MOVED+ERR`; one bad file doesn't abort the pipeline. Idempotent via each parser's `external_id` convention (`payslip:<employer>:<YYYY-MM>`, `ilp_charges:<policy>:<YYYY-MM>`, `cpf:<nric>:<date>:<code>:<amt>`, `cc_stmt:<hash>`).

Parser bugs fixed during integration:
- Payslip: hardcoded CoA 5510 + 4120 didn't exist. Switched to per-employer salary income (4110/4120 from CoA seed) + 5500 for fund deductions. Employer CPF now folded into total compensation income (gross + employer CPF as single CR), CPF asset = employee + employer combined split by OA/SA/MA allocation %.
- Loan agreement: `CreditFacility.updated_at` is NOT NULL. Now set on every upsert.

Smoke-tested end-to-end: payslip → journal posted, CPF → 19 rows parsed (CON-only, 0 journals as designed), loan agreement → CreditFacility upserted.

Workflow is now: **`docker exec portfolio-mcp python -m app.inbox_pipeline --apply --post`** processes any mix of files dropped in `_INBOX/`.

## [1.12.0] — 2026-05-14 — Inbox pipeline Phase 2 (pile-specific parsers)

Four new parsers, each `python -m`-invocable with `--post` flag for journal posting:

- `app/payslip_parser.py` — AZ United / YourAgency / Ganesan / HSS payslip extraction. Handles the column-confusion problem (PDF text extraction concatenates EARNINGS-column + DEDUCTIONS-column per row) via known-label whitelist + Total Earnings reconciliation. Posts balanced salary journal: DR POSB (net) + DR CPF OA/SA/MA (employee + employer CPF split per CPF Board allocation %) + DR Fund Expense (MBMF/SINDA/CDAC); CR Salary Income (gross) + CR Employer CPF Income (employer side). Idempotent via `external_id=payslip:<employer>:<YYYY-MM>`. 6/6 AZ United payslips parsed cleanly.

- `app/loan_agreement_parser.py` — extracts contract terms from moneylender PDFs (EZ Loan, Lending Bee, Sands Credit, future). Upserts directly into `CreditFacility` table — replaces manual `seed_credit_db.py` entries. OCR-tolerant: handles `Tnstalment` for `Instalment`, `{2` for `12`, accepts `$5000.00` (no thousand-separator). Detects "LOAN AGREEMENT", "Licensed Moneylender", "Moneylenders Act", "Note of Contract" markers. Both sample agreements (EL-14603 + 16125) parse with correct principal / disbursed / admin fee / instalment amount. Sands `# instalments` still needs manual override (OCR reads `{` for `1`).

- `app/ilp_statement_parser.py` — Singlife Savvy Invest monthly statement parsing. Extracts per-fund opening/closing units + price + value, plus premium + admin/supplementary charges per period. Posts charges journal: DR Insurance Expense, CR Singlife asset (unit deduction). Premium IN is NOT posted (captured via Firefly bridge as POSB→Singlife transfer). Mark-to-market is NOT posted (Morningstar daily NAV scraper × funds.yaml units handles that continuously). Includes `check_unit_variance()` helper for funds.yaml reconciliation. Tokio Marine support deferred until first PDF arrives.

- `app/cpf_statement_parser.py` — CPF Transaction History extractor across 10+ codes (CON, INV, INT, DPS, CSL, MSL, PMI, SUP, BAL, etc.). Posts journals selectively: INV (OA→IS transfer), INT (annual interest income credit), insurance-deduction codes (DPS/CSL/MSL/PMI as Insurance Expense, deducting from CPF asset). Skips CON entries — those overlap with payslip parser's CPF legs; reconciliation via summary table instead. 44 rows parsed across user's Mar-2025 to May-2026 history.

Phase 2 deliberately does NOT wire parsers into `inbox_pipeline.py` automatically — each remains user-invoked via `python -m`. Phase 2.5 / Phase 3 will integrate.

Known limitations:
- Sands `# instalments` reads as `2` instead of `12` (OCR misreads `1{` as `{`). Override via DB or fix in seed.
- ILP parser handles Singlife only; Tokio deferred.
- CPF rows with code `CON` are extraction-only (not auto-posted) to avoid double-counting with payslip parser.
- Coinbase / crypto exchange parser still pending API key drop.

## [1.11.0] — 2026-05-14 — Inbox pipeline Phase 1 (classify + auto-route)

User vision: single dump zone → auto-classification → pile-specific archive. Phase 1 covers classify + route; Phase 2 will add per-pile parsers + journal posting; Phase 3 the month-end report bundle.

- `app/doc_classifier.py` (NEW) — top-level rule-based dispatcher across 10 categories: `cc_statement` (delegates to existing parser), `bank_statement` (POSB / Maybank Ar Rihla / SC Savings / Wise), `loan_agreement` (Moneylender contracts), `ilp_statement` (Singlife / Tokio — requires date marker), `cpf_statement` (annual / monthly / tx history / IS), `payslip` (extracts employer: AZ United / YourAgency / Ganesan / HSS), `noa_tax`, `insurance_policy` (static policy schedules — distinguished from ILP statements via Free-Look / Welcome anti-markers), `crypto_report` (Coinbase CSV), `noise` (forms / acknowledgements / credit reports).
- Rule order matters: bank_statement runs BEFORE cpf (savings stmts have "CPF Investment Scheme" boilerplate in deposit insurance disclaimer that triggers false positive). insurance_policy runs BEFORE ilp (Tokio Wealth Pro / Singlife Savvy Invest policy contracts share brand keywords with their monthly statements).
- ClassifierResult carries: category, sub_category, confidence, detected_date, target_folder, target_filename, reason, rule_id. Confidence below 0.70 → routed to `_QUEUE/` for manual review.
- `app/inbox_pipeline.py` (NEW) — walks `/onedrive/Sentinel Finance/_INBOX/`, dispatches each file via classifier, moves to canonical pile folder with auto-rename (e.g. `Maybank Savings Jan'26.pdf` → `Ar Rihla Jan'26.pdf` since the underlying account is Maybank's Ar Rihla product). `--apply` to execute, default dry-run. `--watch` polls every 30s for daemon mode.
- Audit log appended to `/data/inbox_pipeline.log` (tab-separated: timestamp, action, category, sub, conf, src, dst, reason).
- Smoke-tested against 19 diverse samples covering all 10 piles — 19/19 classifications correct.
- Phase 2 (per-pile parsers: `loan_agreement_parser`, `ilp_statement_parser`, `cpf_statement_parser`, `payslip_parser`, `crypto_exchange_parser`) gated on Coinbase API key drop + design alignment.

## [1.10.3] — 2026-05-14 — Reconciliation audit + opening balance extract

- `app/opening_balance_extract.py` (NEW) — produces per-CoA opening-balance manifest for any cutoff date. Reads CC statements (`prev_balance` for cutoff month + `closing_balance` for prior month fallback), Firefly assets (live − sum of tx from cutoff onwards, Y2K38-safe), PaymentSchedule for moneylenders (remaining principal of instalments due after cutoff; pre-origination = 0). Writes CSV. `--post` flag writes balanced opening journal with plug to 3210 Retained Earnings.
- `app/statement_reconcile.py` (NEW) — A+B−C=D within-statement check (Prev + Charges − Payments = Closing) AND Dec-close→Jan-prev chain check per CoA. `--month "Jan'26"` filter + `--chain` / `--within` mode toggles.
- `app/credit_utilization_audit.py` (NEW) — credit_limit = outstanding + available tie check across all active revolving facilities. Walks `shared_limit_with` to roll up SC CC + BT etc.
- Verified Jan-1-2026 opening: Liabilities **SGD 36,855.01**, Assets (Firefly-backed) **SGD 113,278.09**, Net **SGD 76,423.08**. 7/7 Jan'26 within-stmt reconciliations close to ±0.00; 8/8 active revolving facilities tie cleanly.
- ILP + crypto opening balances flagged as needing external data (ILP Dec'25 fund-value stmts; on-chain snapshot at 2025-12-31 block height).

## [1.10.2] — 2026-05-13 — Firefly → GL bridge + Suspense reconciliation

- `app/firefly_bridge.py` (NEW) — bridges every Firefly tx into a balanced GL journal. Idempotent via `external_id=firefly_tx:<id>`. `FIREFLY_ACCT_TO_COA` map expanded from 17 → 80+ accounts incl. revenue/expense mirrors. Classifier fallback via `description_to_coa_via_classifier()`. Skips "mirror of TX" duplicates.
- `app/suspense_match_cc.py` (NEW) — matches "Bill Payment Unknown" entries against CC statement payment-received lines within ±7 days ±$1.
- `app/suspense_cleanup.py` (NEW) — one-off cleaners for legacy malformed journals.
- `app/suspense_heuristic_match.py` (NEW, then VOIDED) — speculative date+amount heuristics rejected after over-attributing $-31k to DBS CC. 79 SUSPENSE_HEURISTIC journals voided. Lesson: accounting is structural, not speculative.
- `app/_pnl_audit_*.py` (NEW) — diagnostic clusters / A3-anomaly / 2026 P&L deep-dives.
- Suspense progression: $121,525 → $62,940 (expanded map) → $41,937 (legitimate cleanups). Residual = pre-Apr Bill Payments + cleanup correctives, slated for Apr'26 consolidation inference.

## [1.10.1] — 2026-05-14 — CC statement parsers (8 banks) + OCR + sort

- `app/cc_statement_parser.py` (NEW) — multi-bank PDF parser (DBS CC, DBS Cashline, Maybank CC, Maybank CreditAble, HSBC, Standard Chartered CC+BT, UOB CashPlus, GXS Savings+FlexiLoan). Each parser extracts header (statement_date, due_date, credit_limit, **previous_balance**, **closing_balance**, minimum_due) + line items (charge/payment/interest/fee, with refund handling). SC stores per-CoA split (CC 2113 + BT 2211) in `extras["previous_balance_by_coa"]`/`closing_balance_by_coa"]`.
- OCR fallback: `pytesseract` + system `tesseract-ocr` package in Dockerfile. `_extract_text_smart()` tries pdfplumber first, falls back to OCR at 200dpi when text is empty. Regex tolerant of OCR space-glitches (`31Dec` vs `31 Dec`).
- `app/sort_cc_statements.py` (NEW) — auto-sorts unsorted PDFs into `<Year>/<Mon>'<YY>/` (or root for 2026), canonical naming `<Bank> <CC|CA|CL> <Mon>'<YY>.pdf`. Excludes application forms, loan agreements, transaction histories, payslips, etc. `--rename-existing` mode renames the whole tree.
- `app/statement_completeness.py` (NEW) — month × facility coverage matrix. Coverage went 42% → 88% across the v1.10.1 sprint.
- `app/cc_pipeline.py` (NEW) — orchestrator that walks `/onedrive/Sentinel Finance/CC_Statement/`, parses every PDF, posts journals for each charge/interest/fee line. Payments skipped (already in GL via Firefly bridge POSB-side). SC dispatches per-line CoA via `[coa:XXXX]` marker.
- Reconciliation results for Jan'26: 7/7 statements close A+B−C=D to ±0.00.

## [1.10.0] — 2026-05-13 — Ledger scaffolding (CoA + GL + parties + sub-ledger)

- `app/ledger.py` (NEW) — SQLAlchemy models for parties, chart_of_accounts, journals, general_ledger, bank_reconciliation, investment_positions, firefly_bridge_map (transient).
- `app/ledger_seed.py` (NEW) — bootstrap script: 94-account IAS-1 CoA hierarchy + 78 parties from classifier.yaml. Idempotent.
- `app/journal_service.py` (NEW) — `post_journal()` with double-entry enforcement (ΣDr = ΣCr); `account_balance()` query helper. Only path to write GL.
- `app/backfill_credit_journals.py` (NEW) — one-off backfill that posted 21 journals (3 origination + 18 instalment) for Sands/EZ Loan/Lending Bee with proper P/I split. Net P&L corrections YTD 2026: +SGD 2,262.44 Moneylender Interest, +SGD 1,500 Processing/Admin Fees.
- `app/coa_view.py` (NEW) + route `/admin/chart_of_accounts` + `/api/agent/chart_of_accounts` — hierarchical CoA browser, colour-coded by class.
- CoA additions for double-entry forcing function: 1190 Suspense Account, 1122/1123/1124 Receivables (Ganesan + general + family loans-out), 5410-5460 Finance Cost split (CC interest / loan interest / moneylender interest / OD interest / late fees / admin fees).
- Foundation for v2.0 Firefly retirement. v1.10.1+ build the parser pipeline + Firefly bridge on top.

## [1.9.22] — 2026-05-13 — Credit utilization + reconciliation alerts

- `app/credit_utilization.py` (NEW) + route `/admin/credit_utilization` — A+B=C+D check per facility with reconciliation alerts panel.
- `database.CreditFacility.shared_limit_with` field + reconciler walks linked children. SC CC + SC BT now correctly share $14,600 limit.
- `database.FacilityPlan` extended with `interest_rate_annual`, `interest_method`, `processing_fee_pct`, `principal_outstanding`, `future_interest_remaining`.
- `app/amortization.py` (NEW) — `compute_principal_split()` helper for flat / reducing_balance / promo_zero methods. Resolves DBS CC plan 003IL `plans_overshoot=$216.54` alert (was future unaccrued interest, now isolated).
- Maybank CreditAble restructured from single entry to 3 sub-accounts sharing $7k limit: maybank-creditable-3837 (active term loan), -8901 (paid off), -3866 (empty).
- Agent endpoint `/api/agent/credit_utilization` for Sentinel AI queries.

## [1.9.21] — 2026-05-13 — Credit Facilities DB (SQLite source-of-truth)

- `database.py` extended with `CreditFacility`, `FacilityPlan`, `PaymentSchedule`, `ActualPayment` tables (full schema).
- `app/seed_credit_db.py` (NEW) — reads `liabilities-registry.yaml`, regenerates SQLite tables, runs matcher against Firefly POSB withdrawals to populate `actual_payments`. Orphan-purge step drops facilities no longer in YAML.
- 12 facilities seeded, 23 plans, 36 scheduled instalments (Sands, EZ Loan, Lending Bee), 18 actual payments matched (Sands 12/12, EZ 4/12, Lending Bee 2/12).
- YAML enrichment: full lender entity (UEN, license, address), agreement document refs, P/I schedule per instalment for Sands (from OCR of agreement page 2).

## [1.9.19] — 2026-05-13 — Persistent Moralis snapshot cache

- `app/moralis.py` — `wallet_snapshot()` now reads/writes a persistent JSON cache at `/data/moralis_snapshot_cache.json`. TTL defaults to 15 min (override via `MORALIS_CACHE_TTL_MIN`).
- Cache key: `(address.lower(), dust_threshold_usd)`. Atomic write via temp+rename. Read failure falls back to live fetch — cache layer never breaks the request.
- Snapshot response now carries `cache_hit` (bool) + `cache_age_seconds` (int) so consumers can reason about freshness.
- `force=True` parameter on `wallet_snapshot()` bypasses cache. Wire to /wallet_snapshot Telegram cmd + MCP tool when a refresh button is needed.
- Why: pre-v1.9.19 every `/wallet_snapshot` Telegram press + every MCP `portfolio_snapshot` tool call hit Moralis fresh = 7 CU per snapshot. Repeated user requests inside 15min now cost zero CU.
- `portfolio_mcp_data:/data` Docker volume already mounted — cache survives container restarts.
- Negative-cache guard: if ALL 7 chains error (e.g. daily quota exhausted with HTTP 401 "free-plan-daily total included usage has been consumed"), the snapshot is NOT cached. Otherwise we'd serve `total_usd=0` from cache for 15 min after Moralis recovers. Partial failures (1-6 chains errored) still cache.
- Diagnostic finding 2026-05-13 evening: today's 40k CU/day cap hit. Per-call quota burn was the cause this cache prevents going forward; today's data won't refresh until UTC midnight (~7am SGT).

## [1.9.18] — 2026-05-13 — Service-token /api/agent/* surface for Sentinel AI

- `app/agent_api.py` (NEW) — bearer-token-gated read-only JSON endpoints. Token: `SENTINEL_FINANCE_AGENT_TOKEN` env, loaded from WCM key `sentinel-miniapp/sentinel_finance_agent_token`. Consumer: Sentinel AI (@YourSentinelBot) via OpenClaw + MetaMCP fetch-mcp.
- Endpoints (all GET, all bearer-gated):
  - `/api/agent/health` — self-describes the agent surface
  - `/api/agent/balance_sheet` — current balance sheet JSON
  - `/api/agent/income_statement?year=<YYYY>` — P&L
  - `/api/agent/pending_count` — Pending Reconciliation glance
  - `/api/agent/cash_forecast?horizon=<days>` — POSB projection
  - `/api/agent/classifier/lookup?description=<text>` — vendor → category
  - `/api/agent/glance` — full home summary
- Read-only by design. No PATCH/POST/DELETE. Mutation endpoints (categorize, create transaction) deferred to v3.0 with separate human-approval flow.
- `.env.local.template`, `sync_env_from_wcm.ps1`, `docker-compose.yml` updated.
- Unlocks Sentinel AI to: (1) answer finance questions natively, (2) run UAT against Sentinel Finance — see `workspace/tools/sentinel-finance.md` + `workspace/tools/uat.md` in the OpenClaw workspace.

## [1.9.17] — 2026-05-13 — Maybank + Standard Chartered savings PDF parsers

- `app/bank_pdf_importer.py` (NEW) — `parse_maybank()` + `parse_sc()` via pdfplumber. Auto-detects bank from header. Bug fix during build: extract amounts from first date-line only (continuation lines were breaking AMOUNT_RE detection).
- `import_pdf()` reuses classifier + posts to Firefly with post-import reconcile.
- `requirements.txt` + `pdfplumber>=0.10`.
- Verified end-to-end: Maybank 1 tx (Service Charge), SC 3 tx (-$5 fee, +$260 IBFT, -$257.77 to Maybank CC).
- Set Firefly opening balances: Maybank #171 = $10.56, SC #172 = $32.48. Variance to bank ledger now zero.

Commit: `0b55b3c`.

## [1.9.16] — 2026-05-13 — Pending Reconciliation cleanup → 0 tx

- Final 4 pending tx classified per user direction:
  - TX 5436 $250 Cash Withdrawal → Family expense (one-off, no rule)
  - TX 5430/5431/5432 SGD 0.01-0.02 → Dividend income, tagged `cdp` + `dividend` (CDP residual)
- New classifier rule: "Dividends/Cash Distribution" → Dividend income.
- Session total: Pending went 251 → 0 tx.

Commit: `d3fa343`.

## [1.9.15] — 2026-05-13 — Pending scoped to real banks + MEPs reconciliation

- Synthetic crypto-sync entries (source=`<Crypto Market>`) excluded from Pending count via `REAL_BANK_ACCOUNT_IDS = {1, 4, 168, 171, 172}` filter.
- MEPs loan-drawdown reconciliation (user-confirmed mapping):
  - TX 5505 $5,600 → Maybank CC Flexicash (#106): recategorised Loan drawdown + mirror withdrawal created on liability side
  - TX 5453 $6,300 → Maybank CreditAble Term Loan (#129): same
  - TX 5411 $2,800 → DBS Cashline (#100): same
- Liability balances now reflect drawdowns; POSB side category = Loan drawdown.

Commit: `421473d`.

## [1.9.14] — 2026-05-13 — Bulk reclassify deposits + new rules

- Earlier bulk reclassify only queried `type=withdrawal`. Re-ran across deposits + withdrawals — deposits got their rules applied (YourAgency, SAF, Interest, FAST).
- 6 new classifier rules: Portfolio sync, SC interbank (SCBLSG22/RTL-DDKABGQZ), Bank rounding adjustment, POSB Advice, Interest Earned, Dividend.
- 72 classifier rules now active.

Commit: `421473d` (same commit as v1.9.15).

## [1.9.13] — 2026-05-13 — Disable home + pending-drill browser cache

- `Cache-Control: no-store` + `Pragma: no-cache` headers on `/` and `/income_statement/category?slug=pending`.
- Service worker bumped `sentinel-v14` → `sentinel-v15`; `/` moved out of `CACHED_PREFIXES`.
- Fixes stale Home glance count after recategorise round-trip.

Commit: `65c0c1d`.

## [1.9.12] — 2026-05-13 — Pending drill matches home glance exactly

- Three causes collapsed:
  - Drill page was hardcoded `type=withdrawal`; home counted everything → drill now queries both withdrawal + deposit when `slug=pending`.
  - Window mismatch (home 60d trailing vs drill YTD) → drill uses 60d trailing for pending.
  - Transfers + opening-balance counted in home but not displayable in drill → home now restricts to withdrawal + deposit.
- Per-tx rendering uses actual `tx.type` for sign/colour (not the URL filter).
- Verified: HOME 24 = DRILL 24 = SGD 27,978.38.

Commit: `fffe876`.

## [1.9.11] — 2026-05-13 — Pending bucket excludes General Expense (parked state)

- `PENDING_BUCKETS = ('', 'Uncategorised')` — General Expense moved to new `PARKED_BUCKETS` (informational only, vendor-lost-to-PDF imports).
- Yellow hint banner on Pending page: "X tx parked in General Expense — re-import via POSB iBanking CSV to recover."
- Pending dropped 236 → 27 tx (parked: 52).

Commit: `a5a5f96`.

## [1.9.10] — 2026-05-13 — Account directory + counterparty display + auto-recon

- `app/account_directory.py` (NEW) — reads `finance/liabilities-registry.yaml` + new `finance/asset_accounts.yaml`. `lookup_by_description()` normalizes alphanumerics + finds longest substring match.
- Pending Reconciliation page: each tx shows counterparty prominently; green chip "✓ Matched account · X" when description contains a registered number.
- `/admin/accounts` page (NEW) — view directory + add asset accounts inline.
- 3 new classifier rules (Bill Payment / Debit Card / FAST Payment generic-PDF buckets) → 157 historical tx bulk-reclassified; Pending: 236 → 79 tx.

Commit: `1248f4f`.

## [1.9.9] — 2026-05-13 — Pending drill aggregates 3 buckets

- Special slug `pending` = virtual bucket matching `('', 'Uncategorised', 'General Expense')`.
- Friendly page title + back-to-Home link when `slug=pending`.

Commit: `b1de4a8`.

## [1.9.8] — 2026-05-13 — Drill totals match home glance (bank + crypto)

- Both drills now derive totals from the same `balance_sheet_config.yaml` nodes as home (`cash_and_bank` for bank; `crypto_wallets + defi + token_holdings + staking_vaults` for crypto).
- Verified exact match: Bank SGD 759.37, Crypto SGD 12,661.50.
- Bonus: classifier rule "Point-of-Sale Transaction" → Shopping. Bulk reclassified 308 historical tx.

Commit: `f012486`.

## [1.9.7] — 2026-05-13 — Hotfix: /drill/pending NameError + /config TypeError

- `from datetime import date, timedelta` (was missing `date`) — `/drill/pending` redirect crashed.
- `home.render_config_page` had stray double `+` from earlier edit → "bad operand type for unary +: 'str'".

Commit: `993beff`.

## [1.9.6] — 2026-05-13 — Income statement click-through + recategorise + pending recon glance

- Every Income Statement row is now a link to `/income_statement/category?slug=...`.
- `app/category_drill.py` (NEW): `list_category_transactions()`, `recategorise()`, `pending_reconciliation_count()`.
- Per-tx form with category dropdown + optional "add classifier rule" checkbox.
- Home glance card "Pending Reconciliation" (key=`pending`) added to `GLANCE_CATALOG`.

Commit: `fb96229`.

## [1.9.4 + 1.9.5] — 2026-05-13 — Spend analysis + classifier editor + General Expense default

- Classifier expanded 35 → 68 vendors (SG groceries, F&B delivery, transport, fuel, telcos, govt, healthcare, fees).
- `reconcile.spend_analysis(days)` — groups POSB outflows by category, surfaces generic-PDF gap, top uncategorized.
- `/admin/reconcile` Spend-by-category section.
- Default category for unknowns → "General Expense" (not "Uncategorised").
- `classifier.add_rule()` + `/admin/classifier` inline editor (each unmatched description → pre-filled rule form).
- "Apply current rules to last 60d" bulk button.

Commit: `2a5abe4`.

## [1.9.3] — 2026-05-13 — Reconcile module (POSB ↔ CC matching)

- `app/reconcile.py` (NEW): `collect_window()` + greedy `match_pairs()` (±5 days, ±$1 tolerance).
- `/admin/reconcile` page with period selector (30/60/90/180/365), matched/unmatched lists.
- Hint banner when only POSB side has data (CC statements not yet imported).
- First run (60d): 46 unmatched POSB outflows, 0 CC charges.
- `reconcile_now()` MCP tool.

Commit: `9f20f52`.

## [1.9.2] — 2026-05-13 — Accounts classifier (counterparty → canonical)

- `finance/classifier.yaml` (NEW): 35 vendors across 30 categories.
- `app/classifier.py` (NEW): `lookup()`, `classify_or_default()`, `unmatched_examples()`, in-process cache + `reload_classifier()`.
- POSB importer refactored to delegate; sets Firefly `category_name` automatically.
- `/admin/classifier` triage page surfaces unmatched descriptions (last 60d).
- `classifier_reload()` MCP tool.

Commit: `6ec0537`.

## [1.9.1] — 2026-05-13 — Collapsible drill cards + full privacy coverage

- Per-account cards on `/drill/{loans,cc,funds,ilp,cpf}` are now `<details>` (collapsed by default). All amounts/durations/credit limits wrapped in `.amt` spans.
- `body.private .collapse-card .sub` added to privacy.css.
- SW bumped `sentinel-v13` → `sentinel-v14`.

Commit: `004b5b8`.

## [1.9.0] — 2026-05-13 — Pre-v3 hardening foundations

- `app/privacy_audit.py` (NEW) + `PRIVACY.md` — automated scanner for hardcoded owner literals + data inventory + stale data + env secrets. `/admin/privacy` page renders findings.
- `V2-SCOPE.md` — productize + beta-tenant plan (5 steps).
- `LEDGER-DECISION.md` — Option C (LedgerBackend adapter, Firefly + SentinelLite).
- `SINGPASS.md` — Tier 0/1/2 feasibility.
- `MONETIZATION.md` — FREE/PRO/CLOUD/ENT tiers.
- `PRE-V2-WORKFLOW.md` — auto-reconcile + classifier + Firefly Rules sync + calendar↔ledger.
- README roadmap renumbered (pre-v2 hardening → v2 → v3).

Commit: `5d0f9fd`.

## [1.8.0] — 2026-05-13 — Portfolio depth

- `app/krystal.py` (NEW) — Krystal LP/vault positions via free public API. Two-tier cache.
- `app/morningstar_sg.py` (NEW) — Fund NAV scraper. APScheduler daily 06:00.
- `app/portfolio_chart.py` (NEW) — matplotlib NW history PNG renderer. Telegram sendPhoto.
- `morningstar_refresh()`, `portfolio_chart()` MCP tools.
- `requirements.txt` + `matplotlib>=3.8`.

Commit: `890090e`.

## [1.7.0] — 2026-05-13 — Mini App UX polish

- `app/networth_history.py` (NEW) + `NetWorthSnapshot` table — daily 02:30 cron captures NW + key totals (idempotent per date).
- Customise Glance Cards page (`/config/glance`) — checkbox + numeric order per card. `GLANCE_CATALOG` static map.
- Chain Dust Threshold editor in `/config/datetime`. `settings.dust_usd()`.
- Per-month income statement view (`?month=4`).

Commit: `70ecfa9`.

## [1.6.0] — 2026-05-13 — Reliability indicators

- Bank drill: statement-vs-live balance indicator per-account (uses ImportLog row).
- ILP + CPF variance probes in `/config/connectors` (Reconciliation group): <0.5% green, <3% yellow, >3% red.
- First run: ILP +1.71% (yellow), CPF IS -5.35% (red).

Commit: `290288e`.

## [1.5.0] — 2026-05-13 — Auto-import & ops infrastructure

- `app/backup.py` (NEW) — daily 02:00 cron tars `finance/*.yaml` + Firefly REST export (accounts/transactions/categories/tags/budgets) to `/data/backups/`. 7-day retention.
- Service worker offline cache extended: `/`, `/balance_sheet`, `/income_statement`, `/cash_forecast`, `/drill/*`, `/static/*`. Bumped `sentinel-v12` → `v13`.
- `posb_ibanking_importer` post-import reconcile (variance vs CSV ledger_balance).
- `ImportLog` + `NetWorthSnapshot` SQLAlchemy tables.
- Hourly OneDrive auto-watcher (APScheduler) + testbot ping on first new tx of the day. `TESTBOT_TOKEN` wired through `.env.local.template` + sync script + WCM.
- `/config/imports` history page.
- `backup_now()`, `backup_list()` MCP tools.

Commit: `48d08ee`.

## [1.4.1] — 2026-05-13 — Moralis cost optimization (patch)

- Persistent snapshot cache at `/data/snapshot_cache.json` (survives container rebuilds — first call after restart hydrates from disk, 0 Moralis CU).
- On-chain poll cadence: 5 min → 30 min. Snapshot TTL: 90s → 900s.
- Expected daily Moralis CU drops from ~22k to ~3–5k.

Commits: `b848d0b`, `d6a5ce7`, `2162aaf`.

## [1.4.0] — 2026-05-13 — Drill: ILP + CPF cards

- `/drill/ilp` — per-policy breakdown (Tokio Marine + Singlife Savvy Invest): Firefly cash value, computed value from `funds.yaml` (units × NAV), variance %, monthly premium, per-fund table.
- `/drill/cpf` — per-account breakdown (OA/SA/MA/IS) with % of total; CPF IS fund holdings when present.
- Home glance cards for ILP and CPF are now clickable.

Commit: `fd9e661`.

## [1.3.0] — 2026-05-13 — YourAgency calibration + Settings page

- YourAgency calendar scan whitelisted to **Primary + Bills + Deployments** (previously scanned every calendar).
- Default YourAgency rate: SGD 240/shift → **SGD 120/shift** (net pay per user feedback).
- "Pending" shifts scaled by 0.5 confidence factor (configurable).
- New `/config/datetime` page: date format dropdown (dd-MM / MM-dd / dd MMM / yyyy-MM-dd), timezone dropdown, YourAgency rate + pending factor knobs.
- New `/finance/settings.yaml` as single source of truth.
- Cash forecast UI: dd-MM dates, clickable Monthly Income / Monthly Expense cards open inline breakdown panels (Fixed vs Variable income; expenses grouped by category).
- Disabled the weekly YourAgency YAML entry (calendar-driven now).

## [1.2.0] — 2026-05-13 — Cloud auto-sync

- **Folder provisioning** — `/config/connectors/provision` creates the canonical `Sentinel Finance/` tree on Google Drive AND OneDrive (Statements by bank, CC_Statement, CPF Statements, CPF IS, ILP, Insurance, Policy Document, Cashflow forecast, Loans, Wise, Crypto, Tax, Auto-import subtree). Idempotent.
- **POSB iBanking CSV auto-import** — drop transaction-history CSVs into `Sentinel Finance/Auto-import/POSB/`, then `/config/connectors/import-csv` parses, dedups via Firefly hash, moves the file to `_processed/<date>-<name>.csv`.
- OneDrive Personal root bind-mounted to `/onedrive` in the container.

## [1.1.0] — 2026-05-13 — UX polish

- **Global privacy toggle** — eye-button in home header (and floating FAB on inner pages) blurs every currency value across home / balance sheet / income statement / cash forecast / drill pages. State persists via `localStorage`. Shared `/static/privacy.js` + `/static/privacy.css`.
- Service worker bumped `sentinel-v10` → `sentinel-v12`.
- Three new POSB asset accounts wired (Maybank Sav, SC Sav) into the Cash & Bank node.

## [1.0.0] — 2026-05-12 — Baseline

State of `portfolio-mcp/` at the time of the per-product versioning split. Includes:

- Mini App scaffolding (Telegram Login + TOTP, PWA + TWA build)
- IAS 1 balance sheet with nested collapsible tree
- Income statement (YTD + prior year, accrual-tag handling for CPF)
- Cash forecast (90-day POSB projection from `recurring.yaml`)
- Home dashboard glance cards: Bank, Crypto, ILP, CPF, Total Loans, Total CC, Monthly Recurring, Net Worth
- Drill pages: `/drill/bank`, `/drill/crypto`, `/drill/loans`, `/drill/cc`, `/drill/recurring`, `/drill/funds`
- Wise API integration (daily 06:30 cron + on-demand `wise_sync` tool)
- Moralis multi-chain wallet snapshot (7 chains)
- WolfSwap PACK staking via Cronos JSON-RPC
- DexScreener hourly price refresh for manual positions
- Fund tracker scaffolding (`funds.yaml`, NAV pluggable sources)
- Connectors hub (`/config/connectors`) with live status of 8 integrations
- FX management page (`/config/fx`, xe.com/oanda)
- 32 pytest unit tests

Archived at `portfolio-mcp-v1.0.0-archive/`.
