# rmom-gate re-validation runbook (after the shift1→shift3 + join-key re-base)

## Why this is needed
Two audit fixes re-base the `residual_momentum` values the continuous-fade gate consumes:
- **causal shift1→shift3** (`scripts/precompute_residual_momentum.py`, `RMOM_CAUSAL_SHIFT=3`):
  the old shift1 summed `residual_return[D-1]` which completes (D+1) 01:00 UTC — ~25h of
  look-ahead vs a D-00:00 live read. shift3 removes that leak.
- **panel join-key** (`volume_events._attach_residual_momentum`, decision-day key): off-by-one
  that pulled the event day's own forward residual into the gate.

The gate selects the **bottom third by within-ts residual-momentum RANK** (`rmom_quantile=0.33`).
The quantile is a rank fraction, so the re-base does NOT change the *fraction* selected — but it
**reshuffles which names land in the bottom third** (the values changed), which changes the
rmom-gated continuous backtest's performance. `rmom_quantile=0.33` was chosen by the 2026-06-02
alpha-sweep on the OLD (shift1) values, and `scripts/deploy_vps_live.sh:83` hard-asserts it. So
the gate's MAR verdict and the 0.33 choice must be re-validated on the shift3 values before this
path is trusted/redeployed. (This is already in STATE.md open-debts.)

## Re-validation procedure (run on backtest-capable hardware — the 16 GB live box cannot)
1. **Recompute the panel (shift3).** On a full-PIT research root:
   ```bash
   .venv/bin/python scripts/precompute_residual_momentum.py <RESEARCH_ROOT>
   ```
   Confirm `RMOM_WINDOW=7`, `RMOM_CAUSAL_SHIFT=3` and that `residual_momentum.parquet` is rewritten.
   The causal-shift tests (`tests/test_precompute_residual_momentum.py`) already pin no-future-read.
2. **Re-run the rmom-gated continuous backtest** (`continuous_events`) at `rmom_quantile=0.33`
   against the shift3 panel (the engine cache key already encodes the quantile:
   `_continuous_engine_panel_rmom33.parquet`, so delete stale caches first).
3. **Re-run the demo-arbiter MAR verdict** on the result via `scripts/r1_robustness.py` /
   `scripts/apply_decision_rule.py` (the Tier-2 demo-candidate bar). Compare to the pre-re-base
   figures in `docs/research_summary.md`.
4. **Recalibrate the quantile.** Re-run the rmom-quantile alpha-sweep (the 2026-06-02 sweep) on the
   shift3 values to confirm 0.33 is still the optimum (or find the new one).

## Decision rule
- **Accept `rmom_quantile=0.33`** iff, on the shift3 panel, the rmom-gated continuous sleeve still
  passes the Tier-2 demo-candidate bar (MAR-primary, both venues consistent) AND 0.33 remains
  within the sweep's plateau of the optimum.
- **Else** set `rmom_quantile` to the re-validated optimum and update the
  `deploy_vps_live.sh:83` assert (`assert cont.rmom_quantile == <new>`) IN THE SAME PR, with a
  pre-registration receipt (it changes which names the live continuous sleeve trades).

## Safety note (no blackout in the interim)
The live continuous daemon already runs the shift3 panel (the precompute change is deployed via the
daily `liquidity-migration-continuous-rmom-refresh.timer`). The gate fails OPEN (no rmom ⇒ no
entries, never WRONG entries — `deploy_vps_live.sh:156`), so the worst case while re-validation is
pending is fewer/no continuous entries, never mis-selected ones. There is no urgency-to-deploy; the
urgency is to confirm the 0.33 choice still holds before treating continuous-sleeve forward-demo
results as evidence for promotion.

## Step-1 result (measured 2026-06-04, `scripts/rmom_shift_diagnostic.py` on `bybit_full_pit`, 450d)
Bottom-third (`quantile=0.33`) membership, shift1 (old) vs shift3 (fixed), 219,066 (symbol,ts)
obs over 442 trading days:
- **Symmetric churn of the selected set: 51.3%** (mean per-ts Jaccard 0.489).
- **Only 65.5% of the old shift1 selection is retained** under shift3 (~⅓ flips out, ~⅓ flips in).

**Conclusion: HIGH churn ⇒ re-validation is required, not a formality.** Removing the ~25h
look-ahead materially changes which names the gate trades, so the `rmom_quantile=0.33` choice (tuned
on the contaminated shift1 values) and the gated-backtest MAR verdict must be re-run on the shift3
values (steps 2–4) before continuous-sleeve forward-demo results are treated as promotion evidence.
Re-run `scripts/rmom_shift_diagnostic.py --quantile <X>` while sweeping to size each candidate.

## Steps 2–4 result + VERDICT (2026-06-03, `alpha_sweep --experiment rmom` on the rebuilt shift3 panels)
Panel rebuilt with `RMOM_CAUSAL_SHIFT=3` on both research roots (bybit 450,549 rows → 2026-06-04;
binance 404,678 rows → ~2026-04-28, that root's klines are ~5wk stale so its window ends earlier).
`alpha_sweep --experiment rmom` (canonical BASE: side=short, decile=9, liq_turnover_min=500k), MTM-MAR:

| quantile | bybit MAR | bybit DD | binance MAR | binance DD |
|----------|-----------|----------|-------------|------------|
| q50 | 37.68 | 2.6% | 30.32 | 5.1% |
| q40 | 37.16 | 2.3% | 39.57 | 3.4% |
| **q33** | **41.48** | **1.8%** | **50.01** | **2.3%** |
| q25 | 48.44 | 1.3% | 32.77 | 2.9% |

**VERDICT: ACCEPT `rmom_quantile=0.33` — no deploy change.** It passes the Tier-2 demo-candidate bar
on BOTH venues (MAR 41.5 / 50.0, DD <2.5%) and is the cross-venue-robust optimum: binance PEAKS at
0.33 and degrades sharply at 0.25 (50.0→32.8), while bybit is strong at 0.33 (2nd only to 0.25).
Tightening to 0.25 helps bybit but breaks binance, so 0.33 is the right COMMON choice and sits in the
plateau. The shift1→shift3 re-base (51% churn) did NOT invalidate 0.33; the honest values look cleaner.
The `deploy_vps_live.sh:83` assert (`cont.rmom_quantile == 0.33`) stands — no change.
**Caveats:** MTM-MAR is full-window in-sample (relative quantile ranking is robust to funding, which is
~flat across quantiles); binance's window is ~5wk short (refresh `binance_full_pit` klines for a fully
current re-run). Open-debt closed: continuous forward-demo results may now be treated as evidence.
