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
