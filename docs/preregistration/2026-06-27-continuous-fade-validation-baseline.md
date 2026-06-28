# Continuous Fade Validation Baseline - 2026-06-27

## Experiment

Execute the first validation layer from
`docs/preregistration/continuous_fade_research_plan.md`: freeze the current
`continuous_ensemble_v2` local target and produce baseline/path diagnostics
under `research/continuous_fade/`.

This is offline research only. No live orders, production credentials, mainnet
mode, or live guard changes were used.

## Frozen Object

- Run: `continuous_ensemble_v2_baseline_current`
- Git commit: `9644fec`
- Profile hash:
  `c4eb2eed1658697aa1239afd847e0de9d04f87ffe98080d4607ea6c1fd86a4f6`
- Run config hash:
  `46134bbbab4d80f73976987e2b94abb7648d13719e0afad30064e39f007c52fb`
- Components: turn3p3 `0.3333333333333333`, turn4p3
  `0.2222222222222222`, turn4p5 `0.4444444444444444`
- Current local target: TP12, inverse-vol sizing, BTC-risk sizing
  `CTRL_BTC_RISK_70_90_35`, daily rebalance disabled, BTC+ETH hedge with
  BTC-vol hedge regime.

## Data Roots

| Venue | Root | 1h kline range | 5m kline range | Manifest range | End boundary |
| --- | --- | --- | --- | --- | --- |
| Bybit | `~/SHARED_DATA/bybit_full_pit` | 2021-01-01..2026-06-25 | 2023-04-01..2026-06-25 | 2021-01-01..2026-06-25 | 2026-06-26 |
| Binance | `~/SHARED_DATA/binance_full_pit` | 2020-01-01..2026-06-24 | 2023-04-01..2026-06-24 | 2020-01-01..2026-06-24 | 2026-06-25 |

## Baseline Result

| Venue | Signals | Selected | Trades | Full return | Component net | MAR | Max DD | Active Sharpe | Cluster Sharpe |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Bybit | 12,690 | 2,367 | 2,367 | +26.64% | +20.89% | 7.33 | -1.13% | 3.419 | 3.411 |
| Binance | 13,155 | 2,152 | 2,152 | +18.84% | +14.69% | 5.72 | -1.02% | 2.662 | 2.472 |

Fees/funding were negative on both venues: Bybit fees -2.37%, funding -2.78%;
Binance fees -1.89%, funding -1.90%.

## Diagnostics

- MAE >=20% remains dangerous: only 19.76% of Bybit and 15.47% of Binance
  trades that reached that pain level still finished profitable.
- A 20% diagnostic stop materially reduces component net: Bybit +20.89% to
  +8.51%; Binance +14.69% to +6.60%. Post-stop original-TP hit rates were
  14.13% and 11.97%, respectively.
- 1h timing delay reduced signal-level unit PnL/signal on both venues: Bybit
  2.07% to 1.64% with 897 immediate winners missed/ruined; Binance 1.58% to
  1.19% with 909 immediate winners missed/ruined.
- Full component+BTC-risk+hedge portfolio replays rejected adding extra delay:
  Bybit baseline +26.64%/MAR 7.33 fell to +23.58%/5.23 with +1h and
  +20.54%/6.35 with +2h; Binance baseline +18.84%/5.72 fell to +17.41%/4.75
  and +16.80%/5.32. A +4h arm timed out with partial Bybit artifacts and is
  not counted as evidence.
- 5m timing variants also reduced signal-level unit PnL/signal versus
  immediate: Bybit 15m/30m/next-red 15m = 1.52% / 1.57% / 1.56% versus 2.07%;
  Binance = 1.02% / 1.02% / 0.97% versus 1.58%. Complete-path exclusions were
  0 Bybit and 33 Binance.
- A +1% adverse-limit diagnostic improved signal-level unit PnL/signal with
  high fill rates: Bybit 98.87% fill and 2.83% PnL/signal; Binance 98.84% fill
  and 2.39% PnL/signal. The full portfolio replay rejected it: Bybit baseline
  +26.64%/MAR 7.33 fell to +24.39%/5.94 with 2,131 component trades vs 2,367;
  Binance +18.84%/5.72 fell to +16.59%/3.75 with 1,900 trades vs 2,152.
- Staged fixed 20%/40%/80% adverse price stops were also rejected by full
  portfolio replay. Bybit baseline +26.64%/MAR 7.33 fell to +9.50%/0.94,
  +21.03%/3.38, and +23.96%/4.68; Binance baseline +18.84%/5.72 fell to
  +7.55%/1.48, +12.70%/2.16, and +13.97%/2.50.
- BTC-regime replays rejected both gate-off and simple-return lookback retunes.
  Gate-off changed Bybit from +26.64%/MAR 7.33 to +26.53%/2.33 and Binance
  from +18.84%/5.72 to +12.99%/0.86. Across the nine non-30d lookbacks, best
  Bybit was 25d at +20.72%/MAR 4.29 and best Binance was 60d at +20.25%/MAR
  4.84; both trailed the 30d baseline on MAR. This rejects the retune and does
  not promote the 30d gate beyond demo/paper research.
- BTC-risk 35% tail skip replay rejected replacing the current downsize with a
  hard skip. Bybit changed from +26.64%/MAR 7.33/DD -1.13% to +26.68%/MAR
  7.08/DD -1.17%; Binance improved from +18.84%/MAR 5.72/DD -1.02% to
  +20.49%/MAR 7.15/DD -0.89%. The two-venue rule rejects the arm because Bybit
  lost MAR and worsened drawdown.
- Synthetic active-book squeeze survival diagnostics were survivable at current
  sampled size but did not close tail risk. Worst active one-coin +100% shocks
  lost 3.12% Bybit / 3.15% Binance; worst three-coin +50% shocks lost 3.28% /
  3.49%; adding a one-hour exchange outage and 10% exit damage lifted one-coin
  +100% losses to 3.43% / 3.46%. Drawdowns reached about -3% to -4% and
  recovery can take 259 days.
- +10 bps extra execution cost leaves component net positive but lower: Bybit
  +19.90%, Binance +13.76%.
- Static largest sampled one-coin +100% shock loss was about 1.39%-1.40% of
  equity at current sampled size. This is heat math, not a liquidation/margin
  simulation.
- Forward 5m path tables cover 25,845 original signals. All-signal 24h average
  short return was +2.20% Bybit / +1.51% Binance; selected-trade 24h average was
  +2.63% / +1.92%.
- Path labels marked 659 Bybit / 644 Binance `FAILED_FADE` trades and 13 / 19
  `DISASTER` trades. The labels are heuristic taxonomy for investigation.
- Worst-tail static shocks remained positive in this sample: doubling the worst
  5 trade returns cut component net to +19.28% Bybit / +13.22% Binance;
  replacing the worst 3 trades with +100% adverse squeeze losses left +20.22% /
  +14.25%. This does not model liquidation, margin, outages, or correlated
  timing.
- Current component weights and equal weights were nearly indistinguishable in
  ledger recombination: +20.89% vs +20.96% Bybit and +14.69% vs +14.78%
  Binance. This is not an optimized-weight selection.
- Conditional scale-in full overlay replay lifted return but failed the
  MAR/drawdown rule. Best MAR arms were Bybit `mae05_add25`
  (+31.17%/MAR 6.75/DD -1.43% vs baseline +26.64%/7.33/-1.13%) and Binance
  `mae10_add50` (+23.54%/MAR 5.36/DD -1.36% vs +18.84%/5.72/-1.02%). Do not
  add live/paper scale-in behavior from this evidence.
- Sparse candidate-tape signal-invalidation exits were negative or zero-hit. The
  best active arm, score>=99 after 3h while the short was losing, cut component
  net from +20.89% to +17.85% Bybit and +14.69% to +12.87% Binance; the
  BTC-trend rejection arm had zero in-window hits.
- Hedge attribution added about +2.49% Bybit / +2.47% Binance against short
  gross +26.03% / +18.48%.

## Integrity Read

Candidate-tape checks passed for:

- `entry_eligible_ts >= signal_ts_ms`
- selected `exit_ts_ms >= entry_eligible_ts`
- PIT archive manifest present on both venues
- residual momentum present across the run window
- direct `feature_ts_ms`, `data_available_ts_ms`, `rmom_data_available_ts_ms`,
  `decision_ts_ms`, order-submit, and fill-window assertions persisted in the
  candidate tape and passed

The run remains `exploratory`, not `candidate`: it is internal research on a
spent window, most variants are still diagnostics, and forward demo/paper is
the OOS arbiter.

## Artifacts

Primary report:

- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/reports/final_research_report.md`

Key tables:

- `tables/baseline_metrics.json`
- `tables/signal_table.parquet`
- `tables/trades_enriched.parquet`
- `tables/forward_path_by_signal.parquet`
- `tables/forward_path_curves.csv`
- `tables/trade_path_labels.csv`
- `tables/mae_conditional_recovery.csv`
- `tables/timing_by_original_signal.csv`
- `tables/timing_portfolio_replay.csv`
- `tables/stop_portfolio_replay.csv`
- `tables/regime_portfolio_replay.csv`
- `tables/skip_portfolio_replay.csv`
- `tables/stop_frontier.csv`
- `tables/component_ablation_ledger_recombination.csv`
- `tables/worst_trade_dependency.csv`
- `tables/conditional_scale_in_by_trade.csv`
- `tables/conditional_scale_in_summary.csv`
- `tables/scale_in_portfolio_replay.csv`
- `tables/signal_invalidation_by_trade.csv`
- `tables/signal_invalidation_summary.csv`
- `tables/disaster_loss_by_trade.csv`
- `tables/disaster_sizing_by_trade.csv`
- `tables/disaster_sizing_summary.csv`
- `tables/portfolio_heat_by_entry_cluster.csv`
- `tables/skip_logic_feature_buckets.csv`
- `tables/hedge_attribution.csv`
- `tables/cost_funding_scenarios.csv`
- `tables/synthetic_squeeze_static_heat.csv`
- `tables/synthetic_squeeze_survival.csv`
- `tables/dynamic_liquidation_outage.csv`
- `tables/leakage_audit.json`

## Verdict

Baseline freeze and first diagnostics are complete, but the full validation plan
is not complete. The strategy is still not live-ready for increased size:
direct feature/data timestamps now pass, and added-delay timing is rejected by
completed +1h/+2h portfolio replays. The +1% adverse-limit entry is also
rejected by full portfolio replay. Staged fixed 20%/40%/80% stops are rejected
by full portfolio replay. Gate-off and non-30d BTC-lookback regime variants are
rejected by full portfolio replay, and the BTC-risk 35% tail-skip arm is also
rejected by full portfolio replay. Synthetic active-book squeeze diagnostics
and cluster-bootstrap risk-of-ruin diagnostics support current small sizing.
The dynamic 5m outage overlay also had no maintenance-proxy liquidations, with
worst peak net losses of 3.33% Bybit / 3.56% Binance. Disaster-loss sizing is
stricter: fixed +100% adverse moves with a 0.10% equity per-trade loss budget
flag 97.34% Bybit / 97.44% Binance component trades over budget. These are
still diagnostics, not a liquidation-engine closeout or live-size approval.
