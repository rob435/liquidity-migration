# Promoted/Demo Trading Logic Source of Truth

Last verified against code and deploy files: 2026-06-18.

This document is the human-readable source of truth for the full trading
lifecycle of the currently promoted-in-code demo/paper sleeves. "Promoted" in
this repo means available through `liquidity_migration.promoted.PROFILES` for
demo/paper tooling. It does not mean real-money approved. `REAL_MONEY` stays
false unless the owner gives an explicit instruction, and the Tier-3 real-money
gate remains unmet.

The original daily SHORT sleeve was erased on 2026-06-11. Do not treat it as
dormant.

## Source Hierarchy

Use this order when files appear to disagree:

1. `deploy/sleeves.env` says which sleeves run.
2. `liquidity_migration/promoted.py` is the machine registry for promoted
   profile objects.
3. This document explains the full lifecycle: entry, sizing, exits, live/paper
   env overrides, and what can be reconstructed by the official backtest tools.
4. Sleeve factories define default profile logic:
   - Continuous: `liquidity_migration.continuous_demo.apply_continuous_demo_profile`
     and `liquidity_migration.continuous_forward_replay.FROZEN_FORWARD_CONFIG`.
   - Long: `liquidity_migration.long_native_event_demo._v11a_long_native_config`.
5. Systemd unit env vars are live runtime overrides. They matter.
6. One-off `scripts/research_*` files are not source of truth unless a current
   binding receipt explicitly says so.

If any active code, deploy env, or this document diverges, fix them in the same
change. Do not rely on old thread memory or historical helper scripts.

## Current Running State

`deploy/sleeves.env` currently sets:

| Sleeve | Toggle | Runtime |
| --- | --- | --- |
| Long demo + paper | `LONG_SLEEVE=on` | `liquidity-migration-bybit-long-demo.service`, `liquidity-migration-bybit-long-paper.service` |
| Continuous demo | `CONTINUOUS_SLEEVE=on` | `liquidity-migration-bybit-continuous-demo.service` |
| Continuous paper | `CONTINUOUS_PAPER_SLEEVE=on` | `liquidity-migration-bybit-continuous-paper.service` |

The paper services submit no orders. They write paper/dry-run ledgers for
reconciliation and execution-drift measurement.

## Continuous Fade Book

Status: promoted-in-code by operator override on 2026-06-15 for demo/paper only.
This was not a demo-arbiter gate pass and is not a real-money claim.

Registry object: `promoted.continuous_profile()` returns a deep copy of
`FROZEN_FORWARD_CONFIG`, including the explicit `entry_sizing` block
(`inverse_vol`, target `0.01`, clamp `2.0`).

Live daemon profile: `STRATEGY_PROFILE=continuous_ensemble_v2`, resolved through
`apply_continuous_demo_profile`.

Data roots:

- Demo orders/ledger: `data/bybit-continuous-demo-event`.
- Paper shadow: `data/bybit-continuous-paper-event`.

V2-forward reconcile/control baseline:
`docs/preregistration/2026-06-18-continuous-v2-forward-baseline.md`. The
baseline starts at `2026-06-18T19:54:00+00:00` (`1781812440000`) and is the only
control arm for future continuous A/B tests unless a later receipt explicitly
replaces it.

### Continuous Signal And Entry

The live short signal is the top composite fade decile (`decile=9`) after the
rmom-low gate and liquidity filters:

- `rmom_quantile=0.25` for the deployed ensemble profile.
- `feature_set=("max_ret168",)`.
- `liq_turnover_min=500000`.
- Entry timing uses the confirmed-bar path with `entry_confirm_delay_hours=1`.
  Entries are not supposed to be opened from the noisier intra-hour decile.
- `BTC_TREND_GATE` is a runtime env knob. The promoted object is the uptrend
  gate. As of the 2026-06-18 live-v2 redesign, both continuous systemd units pin
  `BTC_TREND_GATE=uptrend`.
- Max active shorts: 25.
- Max new entries per cycle: 5.
- Re-entry cooldown after exit: disabled for v2.
- Same signal-window re-entry is off by default.
- New entries pause when there are at least 8 adverse exits in 1440 minutes.

The deployed ensemble components are:

| Component tag | Trigger | Age floor | Venue TP | Weight |
| --- | --- | ---: | ---: | ---: |
| `p3` | `turn3_pop3` | 240 days | 10% | 0.3333333333333333 |
| `p4p3` | `turn4_pop3` | 240 days | 10% | 0.2222222222222222 |
| `p4p5` | `turn4_pop5` | 240 days | 10% | 0.4444444444444444 |

Live order notional for a component entry is:

`equity * wallet_balance_fraction * (PER_POSITION_NOTIONAL_PCT_EQUITY / 100) * rebalance_scale * component_weight * vol_weight_multiplier`

The current unit env sets `ENTRY_LEVERAGE=2` and
`PER_POSITION_NOTIONAL_PCT_EQUITY=2`, `SIZING_MODE=inverse_vol`,
`TARGET_VOL_PER_NAME=0.01`, and `VOL_WEIGHT_CLAMP=2`. The volatility multiplier
is `target_vol_per_name / rv_168h`, clamped to `[0.5, 2.0]`; missing or invalid
`rv_168h` falls back to `1.0`. Entries are short market orders. Each entry has
a component venue take-profit at 10%. Active v2 sets `STOP_LOSS_PCT=0`, so there
is no venue/server disaster stop. This is demo/paper only and not
real-money-safe.

### Continuous Exit Logic

Active v2 exits:

- `take_profit`: component venue TP at 10%.
- `max_hold`: force cover after 24 hours.
- No daemon `breakeven`.
- No daemon `left_decile`.
- No daemon `stop_approach`.
- No daemon `failed_fade`.
- No server-side disaster stop.

The retired live-exit diagnostic stack was `breakeven`, `left_decile`,
`stop_approach`, `failed_fade`, and `max_hold`, plus a 25% server stop. The
2026-06-18 receipts below show why that lifecycle was removed from the v2
demo/paper system.

### Continuous Rebalance, Hedge, And Add-ons

Daily rebalance is enabled for the ensemble:

- Realized vol window: 90 days.
- Target daily vol: 0.045.
- Max scale: 4.0.
- Drawdown half threshold: -0.04.
- Resize cost: 10 bps.
- Strategy momentum hurdle: off (`strategy_momentum_window_days=0`).

Live rebalance targets preserve the original component weight and stored
`vol_weight_multiplier`, then apply the daily portfolio scale. That prevents the
daily resize from flattening inverse-vol-sized entries back to component-only
notional.

The promoted frozen object includes the BTC+ETH 2-factor hedge:

- Instruments: `BTCUSDT` and `ETHUSDT`.
- Beta window: 90 days.
- Minimum observations: 60.
- Hedge cap: 2.0.
- Hedge cost: 5 bps.
- BTC-vol regime overlay: `FROZEN_BTCVOL_REGIME` with `lam=0.5`,
  `vol_window=30`, `pct_window=250`. The intensity is causal and mean-1.

Sniper is armed in the demo unit with `CONTINUOUS_SNIPER=1`. It places a
quarter-size PostOnly sell limit 8% above the base short entry. It is an
execution add-on under forward watch, not part of the frozen component-ledger
backtest proof.

Dynamic exit remains no-order paper shadow only.

### Continuous Backtest Reconstruction Boundary

The official promoted continuous registry object is `FROZEN_FORWARD_CONFIG`: the
three-component ensemble, daily rebalance, BTC+ETH hedge, and BTC-vol regime.

The source component ledgers used by the frozen portfolio object are fixed-hold
research ledgers: inverse-vol component sizing, 24h hold, component take-profit,
no stop loss, no breakeven, no failed-fade, and no left-decile live daemon exit.
The live daemon adds the execution behavior above plus sniper behavior.

Therefore:

- Use `promoted.continuous_profile()` / `FROZEN_FORWARD_CONFIG` for the promoted
  portfolio object and official equity tooling.
- Do not claim that a frozen component-ledger backtest exactly reproduces the
  daemon-live exit lifecycle.
- A historical run that claims to test literal live exits must explicitly replay
  the daemon state machine, including warm-started exit state, venue stop/TP
  assumptions, and paper/demo execution rules. Otherwise it is exploratory.

### 2026-06-18 Full Live-Config Backtest Receipt

Run label: exploratory. This was a diagnostic replay of the literal current
continuous live config after the TP14 component was removed, not promotion
evidence and not a real-money gate.

Artifact root:
`/Users/jhbvdnsbkvnsd/SHARED_DATA/full_live_system_backtest_2026-06-18`.

Run window: `2023-04-01` through `2026-06-18` exclusive. Config hash:
`695076bb1f3a`. Retired runtime basis under diagnosis: pre-freeze ensemble,
`BTC_TREND_GATE=off`, three deployed components, shared active book, shared
`MAX_NEW_ENTRIES_PER_CYCLE=5`, live-style component-weighted flat sizing,
daily rebalance, BTC+ETH 2-factor hedge, and BTC-vol regime overlay.

The replay was made available by repairing current PIT coverage:

- Bybit `BTCUSDT` hourly klines were topped up through 2026-06-17 so BTC
  factors did not truncate while alt bars continued.
- Binance canonical `klines_1h` was repaired from the Binance USD-M proxy root
  for the active universe from 2026-05-01 through 2026-06-18. A BTC-only repair
  was not enough because the live cross-section needs the full universe.

Results:

| Venue | Trades | Rebalanced + 2f hedge return | Max DD | Sharpe-like | Exit counts |
| --- | ---: | ---: | ---: | ---: | --- |
| Bybit | 4564 | -2.06% | -25.51% | -0.01 | TP 1518, left_decile 1234, max_hold 1098, stop_approach 490, failed_fade 224 |
| Binance | 4230 | -8.71% | -16.04% | -0.24 | TP 1403, max_hold 1117, left_decile 1071, stop_approach 433, failed_fade 206 |

Diagnosis from this run:

- The current literal live config fails both venues after daily rebalance and
  the live 2-factor hedge/regime layer. Bybit is already slightly negative after
  rebalance; Binance flips from positive unhedged to materially negative after
  the hedge/regime layer.
- This is not just "one bad exit." The exit mix shows TP, left-decile,
  max-hold, and stop-approach are all material. Treating the old frozen
  fixed-ledger result as a daemon-live backtest is incorrect.
- `BTC_TREND_GATE=off` is part of the current systemd literal config and was
  honored here. Do not cite this run as evidence for the promoted-object
  `uptrend` gate.
- The old one-component-removed ledger shortcut is stale. Any next diagnostic
  should isolate gate, hedge/regime, and live exits as controlled deltas from
  this receipt.

### 2026-06-18 Live-Feature Ablation Receipt

Run label: exploratory. This was a ruthless attribution ladder from the old
component-ledger object toward the literal live daemon lifecycle.

Artifact root:
`/Users/jhbvdnsbkvnsd/SHARED_DATA/continuous_live_feature_ablation_2026-06-18`.

The ladder intentionally changed one live feature at a time:

1. old component ledger: `BTC_TREND_GATE=uptrend`, inverse-vol component
   sizing, independent component ledgers, fixed 24h/TP exits.
2. gate off.
3. flat sizing.
4. shared active book.
5. shared `MAX_NEW_ENTRIES_PER_CYCLE=5`.
6. live exits/lifecycle: `left_decile`, `stop_approach`, `failed_fade`,
   `breakeven`, 24h max hold, 30-minute re-entry cooldown, and adverse-exit
   pause.
7. BTC+ETH 2-factor hedge plus BTC-vol regime overlay.

Results:

| Rung | Bybit return | Binance return | Mean return | Read |
| --- | ---: | ---: | ---: | --- |
| old component ledger | +81.13% | +68.22% | +74.67% | high-return baseline reproduced |
| gate off | +57.89% | +42.91% | +50.40% | hurts both venues, not fatal |
| flat sizing | +83.20% | +53.99% | +68.59% | not the collapse; raises headline return but worsens DD |
| shared active book | +54.59% | +89.91% | +72.25% | venue-dependent; not the common killer |
| shared max-new | +70.12% | +88.90% | +79.51% | not the killer; often improves selection |
| live exits/lifecycle | -1.11% | -1.76% | -1.43% | collapse point |
| hedge/regime | +0.49% | -5.98% | -2.74% | secondary; helps Bybit slightly, hurts Binance |

Diagnosis from the ablation:

- The live exit/lifecycle layer is the common cross-venue collapse point:
  Bybit drops -71.23 percentage points from the prior rung and Binance drops
  -90.66 percentage points.
- Turning `BTC_TREND_GATE=off` is bad, but it does not explain the failure by
  itself.
- Flat sizing is not the culprit in this ladder. It increases headline return
  while worsening drawdown.
- Shared active book and max-new are not the common cause. They are
  venue-dependent and can improve the candidate mix.
- The hedge/regime layer is not the first break. It is a secondary overlay:
  modestly positive on Bybit after the exit collapse and materially negative on
  Binance.
- Therefore, do not spend the next debugging cycle on component weights first.
  The next serious diagnostic should split the live-exit rung into
  `left_decile`, `stop_approach`, `failed_fade`, `breakeven`, adverse-exit
  breaker, and 30-minute re-entry cooldown.

### 2026-06-18 Exit-Cause Ablation Receipt

Run label: exploratory diagnostic. This run split the live-exit/lifecycle rung
above into atomic switches from the strong shared-max-new baseline.

Script: `scripts/continuous_exit_cause_ablation.py`.

Artifact root:
`backtest-runs/continuous_exit_cause_ablation_2026-06-18`.

Run window: `2023-04-01` through `2026-06-18` exclusive. The baseline is the
same shared active book, flat component-weighted sizing, shared
`MAX_NEW_ENTRIES_PER_CYCLE=5`, fixed 24h hold, and 10% component TP that produced
the near-100% headline result. The diagnostic then turned on individual live
exit switches and replayed both venues from the same PIT scratch roots.

Results:

| Rung | Bybit return | Binance return | Mean return | Read |
| --- | ---: | ---: | ---: | --- |
| fixed 24h/TP baseline | +70.12% | +88.90% | +79.51% | high-return run reproduced |
| `left_decile` only | +29.04% | +24.22% | +26.63% | large secondary drag, not the full failure |
| `stop_approach` only | -11.38% | -11.76% | -11.57% | primary cliff; flips both venues negative |
| `failed_fade` only | +57.16% | +73.68% | +65.42% | modest drag, not fatal |
| `breakeven` only | +70.12% | +88.90% | +79.51% | no effect in this replay |
| 30m exit cooldown only | +84.45% | +79.45% | +81.95% | mixed, not the culprit |
| adverse-exit breaker only | +63.35% | +73.31% | +68.33% | modest drag, not fatal |
| cumulative `left_decile + stop_approach` | -10.98% | -10.13% | -10.55% | stop keeps the book broken |
| full live-exit lifecycle | -1.11% | -1.76% | -1.43% | reproduces the prior live-exit collapse |

Matched-trade attribution:

| Venue | Rung | Matched trade delta | Worst matched transition |
| --- | --- | ---: | --- |
| Bybit | `stop_approach` only | -28.11% net-return-sum | `take_profit -> stop_approach` |
| Binance | `stop_approach` only | -21.61% net-return-sum | `take_profit -> stop_approach` |
| Bybit | full live-exit lifecycle | -24.46% net-return-sum | `take_profit -> stop_approach` |
| Binance | full live-exit lifecycle | -17.94% net-return-sum | `take_profit -> stop_approach` |

Exit-reason PnL for `stop_approach` only:

| Venue | `stop_approach` trades | `stop_approach` net sum | TP trades | TP net sum |
| --- | ---: | ---: | ---: | ---: |
| Bybit | 682 | -116.78% | 1859 | +113.91% |
| Binance | 659 | -112.87% | 1818 | +113.45% |

Diagnosis:

- The earlier "almost 100%" result was the fixed 24h/TP shared-max-new backtest,
  not a literal live-daemon lifecycle backtest.
- The exact primary divergence is `stop_approach`: with `STOP_LOSS_PCT=0.25`,
  the daemon covers near a 20% adverse move (`0.8 * STOP_LOSS_PCT`). That rule
  exits into the squeeze for this short fade book and turns trades that would
  later hit the 10% favorable TP into realized adverse exits.
- `left_decile` is also harmful, mostly by cutting would-be TP winners early,
  but it does not by itself explain the negative live-config result.
- `failed_fade`, 30-minute re-entry cooldown, and the adverse-exit breaker are
  not the root cause. They change selection/capacity and recover a small amount
  in the cumulative path after the stop damage, but they do not restore edge.
- Any parameter change to disable, widen, or rework `stop_approach` must be
  pre-registered before being tested on the full-PIT roots. Do not treat the
  fixed-ledger headline return as evidence for the current live daemon while
  this rule remains active.

### 2026-06-18 Live-v2 Redesign Receipt

Run label: exploratory registered. This run followed
`docs/preregistration/2026-06-18-continuous-live-v2-exit-redesign.md` and its
Amendment A.

Script: `scripts/continuous_live_v2_redesign_runner.py`.

Artifact root:
`backtest-runs/continuous_live_v2_redesign_2026-06-18`.

Run window: `2023-04-01` through `2026-06-18` exclusive.

Results:

| Rung | Bybit return | Binance return | Mean return | Mean DD | Read |
| --- | ---: | ---: | ---: | ---: | --- |
| pre-freeze live stack, gate off | -1.11% | -1.76% | -1.43% | -19.38% | known live-exit failure reproduced |
| v2, gate off, no server stop | +70.12% | +88.90% | +79.51% | -19.49% | high-return baseline reproduced |
| v2, gate off, 25% server stop | -2.14% | -7.64% | -4.89% | -25.13% | server stop fails |
| v2, gate off, server stop + breaker | -3.65% | +3.43% | -0.11% | -21.89% | still not acceptable |
| v2, uptrend, server stop + breaker | +1.02% | +2.29% | +1.66% | -15.41% | weak repair only |
| v2, uptrend, server stop + breaker + hedge | +9.22% | +4.39% | +6.81% | -15.35% | less broken, not a rebuilt edge |
| v2, uptrend, no server stop, no breaker | +78.33% | +97.53% | +87.93% | -8.97% | clean TP/24h lifecycle works |
| v2, uptrend, no server stop, breaker | +81.05% | +67.96% | +74.50% | -9.42% | breaker retained; still strong both venues |
| v2, uptrend, no server stop, breaker + hedge | +123.97% | +97.33% | +110.65% | -9.06% | flat-sizing predecessor; superseded by InvVol + Max4 receipt below |

Diagnosis:

- `stop_approach` was the first identified failure, but the 25% server stop is
  the same structural problem one step later: it exits short fades into interim
  squeezes before the unwind.
- The actual rebuilt demo/paper system is `continuous_ensemble_v2`: same
  three-component entry book, `BTC_TREND_GATE=uptrend`, inverse-vol component
  sizing, max4 daily vol-target rebalance, no `left_decile`, no
  `stop_approach`, no `failed_fade`, no `breakeven`, no re-entry cooldown, no
  server stop, 24h max hold, 10% component TP, adverse-exit breaker retained,
  and the existing BTC/ETH hedge + BTC-vol regime overlay.
- This is **not real-money-safe**. A future real-money design needs a different
  risk control; re-adding a stop recreates the backtest failure.

### 2026-06-18 InvVol + Max4 Promotion Receipt

Run label: registered exploratory replay for demo/paper promotion wiring. This
followed `docs/preregistration/2026-06-18-continuous-v2-invvol-max4-replay.md`.

Script: `scripts/continuous_live_v2_redesign_runner.py`.

Artifact root:
`backtest-runs/continuous_v2_invvol_max4_2026-06-18`.

Official target cell: `10_v2_uptrend_no_server_stop_breaker_invvol_hedged`.
Run window: `2023-04-01` through `2026-06-18` exclusive.

Configuration now promoted into the v2 demo/paper trade system:

- `STRATEGY_PROFILE=continuous_ensemble_v2`.
- `BTC_TREND_GATE=uptrend`.
- `SIZING_MODE=inverse_vol`, `TARGET_VOL_PER_NAME=0.01`,
  `VOL_WEIGHT_CLAMP=2`.
- Daily rebalance `w90/tv0.045/max4/ddh=-0.04`, no strategy-momentum hurdle.
- No daemon `left_decile`, `stop_approach`, `failed_fade`, `breakeven`, no
  re-entry cooldown, and no server stop.
- 24h max hold, 10% component TP, adverse-exit breaker retained, BTC+ETH 2f
  hedge and BTC-vol regime overlay.

Results:

| Venue | Return | Max DD | MAR | Sharpe-like | Trades |
| --- | ---: | ---: | ---: | ---: | ---: |
| Bybit | +87.92% | -5.39% | 5.09 | 3.32 | 2449 |
| Binance | +75.37% | -4.70% | 4.99 | 3.04 | 2273 |
| Pooled mean | +81.65% | -5.05% | n/a | n/a | 4722 |

Interpretation:

- Inverse-vol sizing plus max4 rebalance materially suppresses drawdown and
  improves MAR versus the flat v2 hedged replay.
- Raw return is lower than the flat v2 hedged replay; that is the expected
  tradeoff from suppressing high-vol names and resizing the open book.
- This is still demo/paper only. It does not clear the real-money gate.

## Long-Native v11a Sleeve

Status: promoted-in-code for demo/paper only. No real-money claim.

Registry object: `promoted.long_profile()` returns
`_v11a_long_native_config()`.

Live daemon profile: `STRATEGY_PROFILE=LongV11aDivWeekendVol`.

Data roots:

- Demo orders/ledger: `data/bybit-long-demo-event`.
- Paper shadow: `data/bybit-long-paper-event`.

### Long Signal And Entry

The long sleeve is FC-only:

- Universe size: 50 by trailing 90-day turnover.
- Minimum listing history: 30 days.
- Pattern gates: only `enable_fomo_chase=True`.
- FC day-return gate: `fc_min_day_return=0.15`.
- Volume rank gate: `fc_top_volume_rank_max=10`.
- Close-location gate: `fc_min_close_location=0.7`.
- BTC and ETH regime gates are required.
- Multi-day close-location gate: 0.6.
- ATR cap: `fc_max_atr_pct=0.12`.
- Sigma-relative trigger is on with `fc_sigma_mult=2.5`.
- 3-day and 7-day FC triggers are enabled.

The live cycle only considers fully closed daily bars and drops signals older
than 24 hours. Entry uses v11a sniper retrace:

- `fc_sniper_retrace_pct=0.01`.
- `fc_sniper_deadline_hours=6`.
- Enter when live price retraces 1% below the signal close.
- If no retrace occurs by 6 hours, enter on deadline fall-through.
- `fc_sniper_skip_on_no_retrace=False`.

### Long Sizing And Risk

Current systemd env:

- `NOTIONAL_MULTIPLIER=1`.
- `ENTRY_LEVERAGE=10`.
- `MAX_PROJECTED_INITIAL_MARGIN_PCT_EQUITY=0.5`.
- `MAX_ORDER_NOTIONAL_PCT_EQUITY=0`.
- `MAX_NEW_ENTRIES_PER_CYCLE=5`.
- `UNIVERSE_SIZE=50`.
- `LOOKBACK_DAYS=100`.

The strategy config uses:

- `gross_exposure=1.0`.
- `max_concurrent_positions=10`.
- Base per-order notional at 1x: `1.0 / 10 = 10%` of equity before live
  vol-parity, vol-target, and weekend adjustments.
- Sizing: vol parity, 30-day vol estimate, 30% annualized floor, max position
  weight 0.30.
- Vol target: annual target 0.60, min scale 0.30, max scale 1.25.
- Weekend size multiplier: 1.5.
- Cooldown after exit: 7 days.
- Full-book initial margin guard includes worst-case vol-target scale, weekend
  multiplier, unit position-weight assumption, max concurrent positions, and
  entry leverage.

### Long Exit Logic

Each long entry places venue-managed stop-loss and take-profit:

- ATR stop multiple: 1.5.
- ATR take-profit multiple: 4.0.
- Max hold: 3 days.

The cycle handles only the time-stop fall-through:

- Open positions past `planned_exit_ts_ms` get reduce-only market exits with
  `exit_reason=time_stop`.
- Stop-loss and take-profit are venue-managed fast exits.
- The paper path marks exits to live ticker price when available.

### Long Backtest Boundary

`_v11a_long_native_config()` has `require_full_pit_universe=False`, so runs that
depend on partial PIT inputs must be labelled honestly. Forward demo/paper remains
the arbiter; internal backtests do not approve real money.

## Backtest Integrity Notes

Any backtest or research write-up that touches these profiles must declare its
decision timestamps, data availability, order timing, fill model, exit state,
PIT universe handling, costs/funding, ledger path, and run label.

Forward demo/paper can provide execution evidence and drift evidence. It is not,
by itself, alpha proof or a real-money pass.
