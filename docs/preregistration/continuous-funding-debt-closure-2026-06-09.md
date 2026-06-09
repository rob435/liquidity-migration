# Audit receipt: binance funding-interval debt — CLOSED for the continuous path

**Date:** 2026-06-09. **Type:** correctness audit (no new strategy run; evidence
verification against raw datasets). **Program:** live-readiness R0
(`docs/research_plan_continuous_live_readiness_2026-06-09.md`).

## The debt (STATE.md "Methodology Debts": binance funding interval handling)

Binance settles funding every 4h on many alts vs bybit's 8h; a 2026-06-03 audit found
and fixed an 8h-bucketing undercount in `trade_lifecycle._funding_lookup`. The open
question was whether the continuous component ledgers (built 2026-06-07, post-fix)
carry correct funding — the winner robustness receipt listed "funding debt" as a
residual risk.

## What was verified (2026-06-09)

1. **Accrual code review:** `_funding_lookup` dedups by exact settlement stamp
   (counts every settlement — interval-agnostic) and `_perp_funding_return` sums
   per-event rates over `(entry_ts, exit_ts]` via bisect, signed `+` for shorts;
   trades extending past per-symbol dataset coverage are charged the covered part and
   flagged `partial` (not zeroed).
2. **Component-ledger census (all 4 winner components × both venues):** bybit 100%
   `modeled` (3,184 trades). Binance 99.6–99.7% `modeled`; only 2–3 trades per
   component are `partial` (5 unique: entries 2023-12-17..2026-03-24, coverage-edge
   symbols), with funding_return at stake ≈ +0.0000. Totals are same-order across
   venues (binance −2.3..−2.8% vs bybit −3.4..−4.0% per component over ~3y).
3. **Independent recomputation:** 20 random `modeled` trades per venue re-priced
   directly from `bybit_full_pit/funding` and `binance_full_pit/binance_usdm_funding`
   raw partitions (exact-stamp dedup, per-event sum over the hold window, ×
   `notional_weight`, short sign). **40/40 match the ledger `funding_return` with max
   abs error 5.4e-20** — accrual, scaling, and sign verified end-to-end.

## Verdict

The continuous engine's funding accounting is CORRECT against the raw venue datasets;
the 4h/8h interval question is moot under per-event summation. The "funding debt"
caveat on the continuous winner + hedge results is lifted. Residual (minor, documented):
5 coverage-edge partial trades (~0 impact); funding data ends 2026-05-27 with the roots.
The same `trade_lifecycle` accrual serves the daily engine (`_simulate_indexed_trade`),
so the accounting-layer verification carries over; daily-path-specific wiring was not
re-audited here.
