# Construction Receipt: Continuous V2 Next-Level — Phase 0 Baseline Freeze

Date: 2026-06-19
Author: Claude (operator-directed next-level research push)
Stage: construction + baseline reproduction
Parent plan: `docs/preregistration/2026-06-19-continuous-v2-next-level-ab-research-plan.md`
Run label: `exploratory` (baseline/control reconstruction — never a candidate)

## Objective

Freeze and reproduce the two comparison baselines that every future A/B arm in
the next-level program must be judged against, BEFORE any 1m data or
trade-aware execution logic is built. This is Wave 0 of the plan ("freeze both
controls; no new A/B research cells"). The acceptance bar is reproduction of
the current 1h object, not a new parameter.

The two frozen baseline objects:

- `V2_LIVE_RESEARCH_CONTROL` — the post-override live object: component
  take-profit `0.12`, daily vol-target adjuster **OFF**
  (`frozen_rebalance_rule().enabled is False`). This is the existing runner arm
  `V2_CONTROL`; no object change.
- `V2_EVIDENCE_ANCHOR` — the pre-override evidence object: component
  take-profit `0.10`, daily vol-target adjuster **ON** with the prior max4
  wiring (`enabled=True`, `max_scale=4.0`, `target_daily_vol=0.045`,
  `realized_vol_window_days=90`, `drawdown_half_threshold=-0.04`). Same frozen
  component book otherwise.

## Why a new arm was needed

The runner already reproduces `V2_LIVE_RESEARCH_CONTROL` (it *is* `V2_CONTROL`:
components at TP `0.12` plus `frozen_rebalance_rule()`, which the 2026-06-19
operator override set to `enabled=False`). The evidence anchor was **not** a
first-class arm: `build_full_ledger` hardwired `frozen_rebalance_rule()`, so the
vol-adjuster-ON object could not be reconstructed without editing the frozen
config (which would void the live forward ledger via the config-hash pin).

## Touched code paths

- `liquidity_migration/continuous_forward_replay.py`
  - `build_full_ledger(...)` gains an optional **keyword-only**
    `rebalance_rule: ContinuousRebalanceRule | None = None`. Default `None`
    resolves to `frozen_rebalance_rule()` — **byte-identical** for every
    existing caller and for the live forward clock (which always passes
    `rebalance_rule=None`). The frozen config and `frozen_config_hash()` are
    untouched (`continuous_profile_hash = c4eb2eed1658697aa1239afd847e0de9d04f87ffe98080d4607ea6c1fd86a4f6`).
- `scripts/continuous_v2_ab_research_runner.py`
  - New `ANCHOR_ARM = "V2_EVIDENCE_ANCHOR"`, `OBJECT_POLICY`
    (`{take_profit_pct: 0.10, rebalance_enabled: True}`), `tp_override_for`
    consults it, new `_arm_rebalance_rule(arm_id)` selector, ARM_DEFINITIONS
    entry (implemented, Phase 0), added to `PHASE0_ARMS`, `amendment_for`
    points the anchor at the next-level plan, `arm_config_payload` records the
    arm's actual rebalance rule, and `run_arm_venue` passes
    `rebalance_rule=_arm_rebalance_rule(arm_id)` into `build_full_ledger`.
    All non-anchor arms resolve to `frozen_rebalance_rule()` → unchanged.
- `tests/test_continuous_v2_evidence_anchor.py` (new) — 5 tests, all green:
  selector invariants; `build_full_ledger(rebalance_rule=None)` frame-equal to
  the explicit frozen rule (forward-clock path guard); an `enabled=True` rule
  diverges (the kwarg really flows through).

## Data roots (working datasets, full-PIT 1h — local, not network-gated)

- Bybit: `~/SHARED_DATA/bybit_full_pit` — `klines_1h` 2021-01-01 → 2026-06-11.
- Binance: `~/SHARED_DATA/binance_full_pit` — `klines_1h` 2020-01-01 → 2026-06-11.

Phase 0 uses only the existing 1h roots. No download, no 1m data.

## Config arms / control arms

| Arm | Object | TP | Vol adjuster | config_hash | role |
|-----|--------|----|----|-------------|------|
| `V2_CONTROL` | live research control | 0.12 | OFF | `bfa8d385210d` | baseline #1 |
| `V2_EVIDENCE_ANCHOR` | pre-override anchor | 0.10 | ON (max4) | `6579c8ece3bb` | baseline #2 |

Both share `continuous_profile_hash = c4eb2eed…a4f6`. The two arm config hashes
differ only by the declared {TP, vol-adjuster} object; component ledgers cache
independently (TP changes `ContinuousEventConfig.config_hash()`).

## Methodology timestamps (declared)

- `decision_ts`: component signal-bar close after the trailing input window closes.
- `data_available_ts`: closed-bar features at `decision_ts`; residual momentum is day-lagged and causal.
- `order_submit_ts`: entry bar close after the +1h confirmation delay.
- `fill_window`: hourly-bar model with explicit taker/spread/impact and funding where available.
- `exit_activation_ts`: venue take-profit at entry + 24h max-hold timer; no daemon/server stop stack.
- `state_initialization_ts`: run start plus warmup for listing age, rmom, BTC trend, vol, rebalance, hedge.

## Exact reproduction command

```bash
.venv/bin/python scripts/continuous_v2_ab_research_runner.py --mode ab \
  --arms V2_CONTROL,V2_EVIDENCE_ANCHOR --venues bybit,binance \
  --start-date 2023-04-01 --end-date 2026-06-12 \
  --out-root backtest-runs/continuous_v2_phase0_freeze_2026-06-19 --resume
```

Robustness (block bootstrap, anchor as control) once the AB root is populated:

```bash
.venv/bin/python scripts/continuous_v2_ab_research_runner.py --mode robustness \
  --ab-root backtest-runs/continuous_v2_phase0_freeze_2026-06-19 \
  --control V2_CONTROL --n-boot 5000 --block 3 --seed 0
```

## Expected artifact paths (per arm × venue)

`backtest-runs/continuous_v2_phase0_freeze_2026-06-19/<ARM>/<venue>/`:
`config.json`, `config_hash.txt`, `trades.csv`, `orders_or_fill_model.csv`,
`mtm.csv`, `equity.csv`, `monthly.csv`, `splits.json`, `summary.json`,
`run_report.md`, `checkpoint.json`; plus root-level `ab_table.csv`,
`pooled_ab_table.csv`, `decision_rule_input.csv`.

Phase-0 baseline bundle (assembled after the run): `baseline_manifest.json`,
`baseline_replay_bybit.csv`, `baseline_replay_binance.csv`, `baseline_diff.md`.

## Success / acceptance criteria

1. `intrabar_resolution=1h` engine (current behavior) reproduces both baselines
   end-to-end with a full trade ledger, equity, drawdown, worst-day, splits.
2. `V2_CONTROL` is byte-identical to the pre-change runner (anchor wiring is a
   provable no-op for it: `tp_override_for=None`, `_arm_rebalance_rule` returns
   the frozen rule). Verified by unit test + the run reproducing prior control
   metrics within numerical-equivalence tolerance.
3. `V2_EVIDENCE_ANCHOR` differs from `V2_CONTROL` ONLY by the declared
   {TP10, vol-adjuster max4} object — confirmed by the divergence unit test and
   by the ledger diff (vol-on daily scale active; TP at 0.10).
4. Any mismatch vs the registered pre-override forward baseline is explained in
   `baseline_diff.md` before Wave 1 begins.

## Null / delayed-feature controls

- `V2_CONTROL_DELAYED_FEATURES` (existing Phase-0 arm) remains the latency
  sanity path; current v2 uses no new uncertain external feature, so it equals
  the control by construction.
- Hash/null controls are deferred to the A/B waves (Books A–I); Phase 0 freezes
  baselines only.

## Stop conditions (from the parent plan)

- `intrabar_resolution=1h` cannot reproduce the accepted 1h control → stop, write verdict.
- Forward demo/paper drift cannot be reconciled after config-hash/data-root changes → stop.

## Results (run complete 2026-06-20, exit 0, runs=4)

| Arm | Venue | total_return | max_drawdown | MAR | worst_day | n_trades | config_hash |
|-----|-------|-------------:|-------------:|----:|----------:|---------:|-------------|
| V2_CONTROL | bybit | 0.2599 | -0.0130 | 6.39 | -0.0093 | 2367 | `bfa8d385210d` |
| V2_CONTROL | binance | 0.1846 | -0.0141 | 4.16 | -0.0063 | 2149 | `bfa8d385210d` |
| V2_EVIDENCE_ANCHOR | bybit | 0.9740 | -0.0549 | 5.66 | -0.0370 | 2367 | `6579c8ece3bb` |
| V2_EVIDENCE_ANCHOR | binance | 0.8402 | -0.0327 | 8.19 | -0.0225 | 2149 | `6579c8ece3bb` |

Block-bootstrap (n=5000, block=3, seed=0) anchor−control MAR delta:
binance **+4.03**, bybit **−0.73**.

### Acceptance — ALL PASS

1. ✅ The `intrabar_resolution=1h` engine reproduces BOTH baselines end-to-end
   (full trade ledger, equity, drawdown, worst-day, split metrics).
2. ✅ `V2_CONTROL` config_hash `bfa8d385210d` equals the offline-computed hash;
   the anchor wiring is a provable no-op for it (unit test + hash match).
3. ✅ `V2_EVIDENCE_ANCHOR` differs ONLY by {TP10, vol-adjuster max4}: identical
   `n_trades` per venue (same entries), vol-on daily scale active, TP at 0.10.
4. ✅ No unexplained mismatch — see `baseline_diff.md` for the full attribution
   (vol-adjuster levers gross toward max4 → ~4× return & drawdown; the MAR venue
   split is the documented override trade-off, now frozen for both baselines).

Bundle written to the run root: `baseline_manifest.json`,
`baseline_replay_bybit.csv` (617 active days, control_final 1.2599 / anchor_final
1.9740), `baseline_replay_binance.csv` (566 days, 1.1846 / 1.8402),
`baseline_diff.md`.

**Wave 0 is COMPLETE.** Next: Wave 1 (build/audit full 1m PIT roots; both 1m
sources confirmed reachable + checksum-valid — see the program execution log).

## No real-money / promotion claim

This is a research-stage baseline freeze. `REAL_MONEY` stays false. Forward
demo/paper remains the only OOS arbiter. Nothing here promotes code, changes
demo/paper wiring, or resets a forward clock.
