# System Status

Current as of 2026-05-19.

## Active System

- Research default: full-PIT liquidity-migration short, `union_pathology` crowding veto.
- Entry policy: `promoted_quality_squeeze` conservative router.
- Promoted research strategy id: `liqmig_union_q40_h3_tp26_g100_qsqueeze`.
- Live demo entry profile: `demo_relaxed`, strategy id `demo_relaxed_liqmig_q40_h3_tp21_g100_qsqueeze_ff6`.
- Gross exposure: `1.00`, split across `10` max active symbols in demo_relaxed mode.
- Per-entry target: `10.00%` of current Bybit demo USDT equity in demo_relaxed mode.
- Entry service: `model050426-bybit-demo.service`.
- Risk service: `model050426-bybit-risk.service`.
- Venue mode: Bybit demo only. `demo=False` is still refused by the private client.
- Paper shadow was intentionally skipped by user decision; keep the risk contained to demo-only trading.

## Data Roots

- Canonical research root: `~/SHARED_DATA/bybit_fullpit_1h`.
- Current full-PIT manifest/klines coverage: `2023-05-03` through `2026-05-17`
  completed bars, run commands using `--end 2026-05-18`.
- Live demo operational root: `data/bybit-demo-event`.
- Do not use temporary recent-download or current-universe roots as promotion
  evidence.

## Champion / Challenger Stack

The order-submitting champion is the VPS `demo_relaxed` entry service only.
`scripts/run_bybit_demo_event_engine.sh` now refuses `SUBMIT_ORDERS=1` unless
`STRATEGY_PROFILE=demo_relaxed` or the deprecated `observe` alias is used.

Manifest/audit command:

```bash
python -m aggression_carry \
  --data-root data/bybit-demo-event \
  champion-challenger
```

The current shadow challengers are:

```text
shadow_current_promoted
shadow_demo_relaxed_without_crowding
shadow_tiered_execution_sniper
shadow_execution_pullback_guard
shadow_volume_shelf_hedge_overlay
```

These are dry-run or research-only by definition. None contains an order-submit
flag in the manifest. A challenger should not be wired into the live service
unless the manifest is intentionally changed, the Model Court is rerun where
relevant, and this safety audit still passes.

## Promoted Evidence

Promoted report:

```text
/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/entry_signal_cross_strategy_20260517/quality_tier_stress/quality_tier_stress_report.md
```

Full-PIT exact-stop, 3x-cost, no-funding result on `2023-05-03` to `2026-05-03`:

```text
trades: 444
total return: +2285.54%
max drawdown: -11.05%
max no-new-high stretch: 51 days
worst 90d return: -5.02%
worst split return: +118.81%
average split Sharpe-like: 3.78
OOS return: +210.35%
promotion gate: pass
```

Funding stress on the same conservative router produced 444 trades, +1853.99%
total return, -13.72% max drawdown, -6.29% worst 90d, +122.17% worst split,
and +175.32% OOS. Candidate selection, exits, cooldowns, gross exposure, and
crowding decisions are unchanged; only entry timing for promoted-grade squeeze
bars changed.

Canonical TP26 rerun after adding the recent full-PIT data through `2026-05-18`:

```text
report: /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/exit_alpha_20260519/promoted_tp_fine_245_280/volume_event_research_report.md
trades: 448
total return with partial funding data: +2022.17%
max drawdown: -13.72%
max no-new-high stretch: 54 days
worst 90d return: -6.29%
worst split return: +126.03%
average split Sharpe-like: 3.62
OOS return: +183.27%
promotion gate: pass
```

TP26 replaced TP25 because it improved exact-stop return, minimum split, split
Sharpe, and OOS without worsening exact-stop max drawdown or worst-90d. TP26
also beat TP25 under 1x/3x/5x cost stress and adverse hourly stop-fill stress,
although the adverse stop-fill family still fails the formal drawdown gate.

The stricter model court was rerun on 2026-05-18 with the quality-tier stress
matrix filtered to `--comparison-family promoted_funding` and explicit
pre-registered windows:

```text
train:      2023-05-03 to 2024-05-03, +122.17%, -4.86% max drawdown
validation: 2024-05-03 to 2025-05-03, +219.44%, -12.54% max drawdown
oos:        2025-05-03 to 2026-05-03, +175.32%, -13.72% max drawdown
```

The promoted current report passed artifacts, comparison-family filtering,
promotion, recomputed path consistency, pre-registered windows,
block-bootstrap left-tail, random-sign, inverted-edge, shuffled-time,
shuffled-symbol, shuffled-event, parameter-sensitivity, parameter-heatmap,
cost/funding/slippage presence, monthly-regime, and symbol-concentration
checks. It remains `WATCH`, not `PASS`, because funding coverage is still
partial, the filtered stress matrix reaches -30.82% drawdown despite +299.01%
minimum return, live-vs-backtest execution drift evidence was not attached,
and the worst same-hour entry cluster still contains 3 losing trades for
-5.15% additive net return. That is a research warning, not a live-execution
fault.

## Feature Factory Research

Feature-factory tooling was added on 2026-05-18 as a shadow research surface,
not as a new live gate. It adds causal ledger columns for rank-migration speed,
local volatility expansion, event uniqueness, and optional perp basis/premium
aggregates. Live/default trading logic is unchanged.

Latest promoted rerun with the new columns:

```text
report: /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/feature_factory_promoted_20260518
trades: 444
total return: +1853.99%
max drawdown: -13.72%
worst 90d return: -6.29%
worst split return: +122.17%
OOS return: +175.32%
```

Feature report:

```text
/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/feature_factory_promoted_20260518/feature_factory/feature_factory_report.md
```

Coverage is currently 16/27 audited features with non-null data. The missing
surfaces are not code failures: this funded PIT root does not contain
mark/index premium, open-interest, or signed-flow datasets. The strongest
shadow-only feature screens were funding sums, residual return, and
close-to-30d-high. New rank-speed, range-expansion, and event-uniqueness
columns populated correctly, but did not beat their shuffled-feature controls.
Do not promote any feature gate from this report alone.

## Alpha Sweep Status

Alpha work on 2026-05-18 did not produce a replacement for the current promoted
strategy. Funding gates, close-to-high/residual gates, late-day turnover-share
filters, wider strict rank bands, and wider-rank plus late-turnover
interactions were all tested as exact full-PIT lifecycle runs. None beat the
current funded promoted baseline of +1853.99% total return, -13.72% max
drawdown, -6.29% worst 90d, +122.17% minimum split, and +175.32% OOS.

The best-looking rejected variants were:

```text
rank31_220_strict: +1807.79%, -16.85% max DD, -16.21% worst 90d, +209.43% OOS
last6h_share_le090: +1671.00%, -14.00% max DD, -6.62% worst 90d, +140.74% OOS
funding_7d_ge0: +718.21%, -9.07% max DD, -7.43% worst 90d, +90.90% OOS
```

A disabled research-only flag now exists for late-day turnover concentration:
`--liquidity-migration-signal-last6h-turnover-share-max`. It is not deployed.

## Demo-Relaxed Profile Evidence

The active VPS entry service is intentionally configured for a higher-frequency
demo-only observation profile. This profile is not the promoted research
default. It lowers the entry gates while keeping the same short
liquidity-migration premise, fixed exits, stop/loss throttles, and
`union_pathology` same-hour crowding veto. It uses the same conservative
`promoted_quality_squeeze` router for promoted-grade events and normal 1h entry
for lower-tier `demo_relaxed` events.

Full-PIT funded stress report:

```text
/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/entry_signal_cross_strategy_20260517/quality_tier_stress/quality_tier_stress_report.md
```

```text
trades: 1268
candidate events: 1603
total return: +221.29%
max drawdown: -21.32%
worst 90d return: -18.90%
train return: +12.36%
validation return: +17.02%
OOS return: +142.92%
average split Sharpe-like: 1.04
promotion gate: pass
```

The stricter model-court rerun with `--comparison-family observe_funding` fails
`demo_relaxed`. The current order-submitting demo profile is TP21 + FF6:
1,300 trades, +353.46% total return, -16.72% max drawdown, -12.72% worst 90d,
+23.53% worst split, +165.57% OOS, and +1.370 average split Sharpe-like in the
full-PIT exact-stop run. The stricter caveat remains: adverse hourly stop-fill
stress is still the hard failure mode, so treat `demo_relaxed` as a
higher-frequency plumbing and observation profile, not promoted alpha.

## Execution Alpha Research

Execution variants added on 2026-05-18 are research-only and are not deployed.
The live/default entry policy remains `promoted_quality_squeeze`.

Tested variants:

```text
execution_pullback_guard:
  keeps promoted-quality events on the proven squeeze router
  tries to delay lower-tier unresolved-continuation bars for a micro pullback

tiered_execution_sniper:
  keeps promoted-quality events on the proven squeeze router
  tests bounded lower-tier continuation-pop entries

entry_execution_veto_close_location_max:
  optional completed-entry-bar high-close veto
  disabled by default
```

Honest result: none of these clears promotion. The blunt pullback guard cut the
promoted book to +569.77% before composition was fixed, and still hurt
`demo_relaxed`. The first tiered-pop result looked attractive, but the fallback
was lookahead; after fixing it to enter the deadline bar causally, the relaxed
pop150/wait2 run fell to +202.75%, below the current +221.29%. The selective
causal version reproduced the current relaxed result rather than improving it.
The high-close veto looked good in a static ledger slice, but a real backtest at
0.85 cut promoted to +1152.68% and relaxed to +200.44%. These are useful
falsification tools, not live alpha.

## Hedge Research

Portfolio hedge tooling was added on 2026-05-18 through `portfolio-hedge`.
It overlays candidate long basket ledgers on the promoted short book and
reports blended return, drawdown, rolling-window loss, split returns, common-day
correlation, and long PnL on the short book's worst days.

Initial long sweep:

```text
report: /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/hedge_long_sweep_20260518
scenarios: 48
promotable standalone longs: 0
best promotion-ranked long: volume_shelf_reclaim continuation, q20, 3d hold
standalone result: +23.00% total return, -23.96% max drawdown, -7.23% min split
model court: FAIL
```

The only useful hedge candidate so far is `volume_shelf_reclaim` q20/h3 as a
small overlay, not as standalone alpha. At 0.50 overlay weight against the
current promoted short report, it produced +2065.44% combined return and
-12.32% max drawdown versus the short book's +1810.43% and -13.69% in the same
daily overlay method. Its common-day correlation to the short book was -0.347
and it made +4.18% additive return on the short book's worst 10% exit days.
Under bar-extreme stop stress for the long leg, the 0.50 overlay still improved
combined drawdown to -12.46% but reduced return to +1726.53%.

Status: shadow portfolio challenger only. Do not deploy it until the standalone
long leg or the combined portfolio clears stress evidence and model-court
promotion gates.

## Crowding Model Research

The first cross-sectional crowding classifier, `model_v1`, was added on
2026-05-18 as research-only tooling. It classifies signals as:

```text
isolated_idiosyncratic_event
liquidity_migration_idiosyncratic
sector_theme_wave
full_market_impulse
exchange_liquidity_artifact
uncertain_cluster
```

It is available through `--liquidity-migration-crowding-filter model_v1`, but
it is not promoted and not deployed. A full-PIT run that traded only the
idiosyncratic/liquidity-migration classes produced 211 trades, +269.95% total
return, -10.32% max drawdown, -6.94% worst 90d, +50.54% worst split, and
+58.91% OOS. That is profitable, but it discards too much of the promoted edge;
the current TP26 promoted run has 448 trades and +2022.17% total return. The
honest conclusion is that classification is useful diagnostics; `model_v1` is
not a replacement crowding filter.

`demo_relaxed` relaxed gates:

```text
PIT liquidity rank: 11-260
current 24h turnover floor: disabled
rank improvement: >= 80
turnover expansion: >= 3.0
daily return floor: >= -3%
residual return floor: >= +3%
close-location floor: >= 0.25
take profit: 21%
failed-fade exit: after 6 completed hours when MFE < 1% and loss > 4%
max active symbols: 10
symbol cooldown: 2 days
crowding veto: union_pathology kept
```

## Live Demo Proof

The demo execution path has been tested on the VPS with the production entry helper, production reduce-only risk helper, native stop/take-profit attachment, and cleanup back to flat state.

BTCUSDT strategy-sized short proof:

```text
entry qty: 0.025 BTC
notional: 1952.7075 USDT
equity share: 19.53%
native stop loss: 87481.3
native take profit: 58581.2
entry execution history visible after: 181.628 ms
reduce-only exit history visible after: 1925.515 ms
final positions: 0
final open orders: 0
```

DOGEUSDT strategy-sized short proof:

```text
entry qty: 18161 DOGE
notional: 1998.61805 USDT
equity share: 20.00%
native stop loss: 0.12326
native take profit: 0.08253
entry execution history visible after: 609.757 ms
reduce-only exit history visible after: 3201.353 ms
final positions: 0
final open orders: 0
```

Bybit demo WebSocket Trade order entry was attempted and was unavailable or rejected at `wss://stream-demo.bybit.com/v5/trade`. The deployed risk path remains `ORDER_SUBMIT_MODE=ws_then_rest`: WebSocket-first for state and exit decisions, REST fallback for demo order submission.

## Deployment Target / Verification

Target VPS state for the active demo deployment:

```text
path: /opt/MODEL050426
branch: main
services: model050426-bybit-demo.service, model050426-bybit-risk.service
entry strategy id: demo_relaxed_liqmig_q40_h3_tp21_g100_qsqueeze_ff6
entry policy: promoted_quality_squeeze
service state: active / active
```

Current verification status: not proven. Local `main` and `origin/main` are at
`db9ade813720ec784e41b8fb310073f559b644ad`, but the VPS live checkout and
service state are not verified. Local SSH reaches
`204.168.202.167`, the ED25519 host fingerprint is stable at
`SHA256:c4K1qg1rx5kH/706qNTdsHYsCDP/o5GIHW1GAHCjwgY`, but the VPS rejects the
available local key before any deploy or verify command can run:

```text
root@204.168.202.167: Permission denied (publickey,password).
```

The GitHub `VPS Deploy` workflow is configured to deploy on guarded `main`
pushes. The first push-triggered run for `db9ade8` failed in `Configure SSH
key`; after that, repository secret `VPS_SSH_PRIVATE_KEY` was set to a dedicated
GitHub Actions deploy key. The VPS does not accept that new key yet. Run
`scripts/vps_console_recover_and_deploy.sh` from the provider console to restore
both the local public key and the GitHub Actions public key in
`/root/.ssh/authorized_keys`, then run `scripts/verify_vps_live.sh` to prove the
live state.

The VPS entry service intentionally runs at `INTERVAL_SECONDS=60` and
`STRATEGY_PROFILE=demo_relaxed`. Fast exits are still handled by the separate
websocket risk service; the one-minute entry cadence is for quicker stale-order,
report, and candidate-state refresh.

## Known Limits

- This proves demo execution plumbing, not future profitability.
- This does not prove real-money venue behavior.
- Funding is only partially covered in the tribunal evidence. The active demo
  gate is still for Bybit-demo observation, not real-money promotion.
- Adverse hourly stop-fill stress is still the hardest negative result for the older baseline; do not use exact-stop fills as sole promotion evidence for future variants.
