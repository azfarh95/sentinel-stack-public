# V2 — SEALED 2026-05-15

Sentinel Finance V2 ("operability") is sealed at **commit v2.20** per
Perplexity audit pass-9.

## Seal status

* 32/32 invariants pass.
* 8 of 9 pass-8 V2 checklist boxes GREEN. Item 9 (alerts noise/tuning) is
  monitored post-V2 — not a milestone gate.
* Net worth: $98,768 (statement-anchored where possible, GL-projection-
  flagged otherwise).
* Both repos in sync: sentinel-stack (`portfolio-mcp/`), sentinel-finance
  (curated journal + parsers + docs).

## What V2 delivers

| Surface | Contract |
| --- | --- |
| **Balance sheet** | 5-gate architecture. Every code resolves via Gate 5. No silent GL fallback. |
| **Dedup** | Partial UNIQUE on `journals(external_id) WHERE status != 'voided'` + canonical `<source>:v<n>:<key>` format. Cutover re-run is **provably idempotent** (burn-in confirmed Δ 0). |
| **Snapshots** | Generic `account_snapshot` table with `raw_currency` / `raw_amount` / `raw_currencies` columns ready for V3 FX work. |
| **Drift** | T1/T2/T3 classification + user-pinned triage + monthly Telegram nudge. Never auto-writes to 5990. |
| **Alerts** | Separate module, daily scan, Telegram delivery for high-severity. Detectors: `stale_class_a`, `missing_recurring`, `snapshot_drop`. |
| **Invariants** | 32 in `tests/test_invariants.py`. Covers BS + P&L + resolver + aggregation + alerts shape. |
| **Docs** | V2-ARCHITECTURE, V2-INVARIANTS, V2-COMMITMENTS, V3-SCOPE — all in `docs/`. |

## Item 9 closure criterion (monitored post-V2)

Per Perplexity pass-9, item 9 (alerts noise/tuning burn-in) is sealed
when:

1. The daily alerts job has run for **4 consecutive weeks** with stable
   thresholds (no detector-threshold edits during the window).
2. **≤ 3 false-positive alerts per week** on average.
3. **≥ 1 clearly-correct alert** has fired from each of the 3 detector
   types, OR an explicit decision has been recorded to disable / retire
   a detector that never fires usefully.
4. If thresholds are adjusted mid-window, the 4-week clock restarts once.

When all four conditions hold, item 9 closes silently — no V2 re-open,
no milestone bump. Threshold tuning afterwards is V2.x maintenance.

## Acknowledged in V2 (Q2 expectation)

When broadening a cutover to new transaction types or new source
formats, **the first run may emit catch-up journals** from secondary
lanes that the partial UNIQUE doesn't yet cover. Subsequent runs MUST
be Δ 0.

This was observed on pass-8 workstream C: the first POSB cutover re-run
emitted 470 cleanup journals (`posb_direct:` + `xfer:` lanes). The
second consecutive run was exactly Δ 0 across all formats. This is the
expected pattern, not a leak.

If a future cutover broadens coverage and produces non-zero Δ on the
second run, that IS a real dedup gap and warrants a new invariant.

## What V2 does NOT promise

(Documented in V3-SCOPE.md — moved to V3-ROADMAP.md for sequencing.)

* Realised vs unrealised FX P&L.
* Insurance cash-value split.
* Per-currency analytics in /income_statement.
* Spend-spike / salary-missing alert detectors.
* Reclass-stability across historical YTD.
* End-to-end 5-gate integration test.
* Tenant isolation for V7.

## Next session

V3.1 (signals + hygiene). See `docs/V3-ROADMAP.md`.
