# Lookback Robustness Audit

Date: 2026-07-04

Purpose: make time-window choices explicit. A `30d` constant is not automatically
a "month", and replacing every `30d` with `365.25 / 12` would be another bad
assumption. The right unit depends on what the window is doing.

## Rule

Classify every material lookback before tuning it:

- Timestamp windows use `exact_duration_ms()` / `exact_lookback_cutoff_ms()`.
  They must subtract exact elapsed milliseconds from the decision anchor, with
  no hour/day flooring.
- Lifecycle mechanics use exact exchange time: hours or days from entry. Do not
  calendar-normalize them.
- Daily-bar features should use calendar-bounded windows and explicit gap
  behavior. A positional 30-row window is wrong on sparse symbol histories.
- Research knobs need a robustness family, not a single chosen value.
- Risk memory should prefer decay or event-count state over cliff cutoffs where
  practical.
- Operational SLAs are freshness/error-detection thresholds, not alpha
  parameters.

No single lookback value is acceptance evidence. A research lookback advances
only when the result is a plateau across nearby values and survives the normal
PIT, cost, funding, and two-venue gates.

## Unit Policy

| Unit | Use for | Do not use for |
| --- | --- | --- |
| Fixed hours | Entry delay, max hold, sniper deadline, stale WebSocket checks | Calendar-month claims |
| Fixed days | Daily-bar features and reporting windows where each bar is one calendar day | Sparse symbol histories without calendar-aware roll/shift |
| Calendar months | Human calendar policies, monthly reports, month-over-month audits | Intraday execution timers |
| Event count | "After N bad exits" or "N trades in bucket" state | Calendar seasonality |
| Exponential half-life | Toxicity memory, entry-time learned risk, stale signal decay | Hard contractual timers |

## Current High-Signal Inventory

| Lookback | Location | Purpose | Unit | Status | Robustness action |
| --- | --- | --- | --- | --- | --- |
| `entry_confirm_delay_hours=1` | `liquidity_migration/continuous_demo.py`, `liquidity_migration/continuous_events.py` | Confirmed-bar entry lifecycle | fixed hours | active profile | Fixed unless a new prereg reopens entry timing. Prior added-delay replays rejected waiting longer. |
| `max_hold_hours=24` | `apply_continuous_demo_profile()`, continuous dispatcher profiles | Continuous TP12 lifecycle cap | fixed hours | active profile | Treat as lifecycle, not a calendar lookback. No-time-stop blacklist work must leave it unchanged. |
| `btc_trend_lookback_days=30` / `btc_trend_30d` | `ContinuousEventConfig`, `ContinuousDemoCycleConfig`, reports | Continuous BTC regime gate | fixed calendar days | active profile, research-fragile | Do not silently convert to month. Non-30d simple lookbacks were rejected; any revisit needs a preregistered family and plateau. |
| `btc_trend_mode`, `btc_trend_month_days=365.25/12`; long `btc_month_regime_*` | `continuous_events.py`, `continuous_demo.py`, `long_native.py` | Opt-in BTC month-regime research hooks | fixed days / exact elapsed month-equivalent | proposed research | Registered in `docs/preregistration/btc-month-regime-2026-07-04.md`. Defaults preserve current behavior. Continuous hourly modes use confirmed bar-close data; long joins the comparable context to daily rows. |
| `lookback_days=45`, `ws_klines_lookback_days=45` | `ContinuousDemoCycleConfig` | Live kline cache / feature rebuild buffer | fixed days | operational | Must exceed the longest live feature need plus margin. Not alpha. |
| `rv_168h`, `max_ret168`, `turnover_spike_168h` | continuous feature pipeline | Continuous signal and inverse-vol sizing inputs | fixed hours | active components | Signal research only. If reopened, test a band around 168h and hash controls; do not call it a "week" without checking missing bars. |
| `entry_pause_window_minutes=1440` | continuous live/research entry pause | Correlated-squeeze entry brake | fixed minutes | live risk state | Candidate for half-life/event-count robustness if researched. Do not tune from one backtest. |
| `entry_exchange_mismatch_lookback_hours=72` | continuous risk-health gate | Detect exchange-only positions tied to recent continuous entries | fixed hours | operational safety | SLA-style threshold. Keep tied to max hold plus incident-recovery buffer. |
| `daily_rebalance_realized_vol_window_days=90` | `continuous_rebalance.py`, `ContinuousDemoCycleConfig` | Disabled daily rebalance vol estimate | fixed days | rejected ON | Leave disabled. Rebalance research needs a fresh plan; do not retune the window alone. |
| `daily_rebalance_strategy_momentum_window_days=180` | `continuous_rebalance.py`, profile override | Disabled strategy momentum scaling | fixed days | rejected/disabled | Not active. Do not revive without a dated plan. |
| `beta_window_days=90`, `beta_min_obs=60` | `ContinuousHedgeRule`, hedge manager | BTC/ETH hedge beta estimate | fixed days / obs count | active hedge | Already has causal trailing-window logic. Future changes need latency falsifier and two-venue replay. |
| BTC-vol regime `vol_window=30`, `pct_window=250` | frozen hedge regime / continuous refresh | Hedge/regime overlay | fixed days | active overlay | Treat as hedge-regime research surface. Changes require full component+hedge replay. |
| Daily features `7d`/`30d` | `liquidity_migration/daily_feature_panel.py` | OI, ADV, vol, range, distance, turnover features | calendar-bounded daily windows | feature library | Keep using `calendar_roll` / `calendar_shift`; do not replace with row-count windows. If promoted into a strategy, require a family. |
| `min_listing_history_days=30`, `age_days_min=240` | long/continuous profiles | Fresh-listing filter | fixed days since listing | active risk filter | These are lifecycle/universe safety filters, not month claims. Test nearby values only in a preregistered universe-risk study. |
| Long `universe_volume_window_days=90`, live `lookback_days=100` | `long_native.py`, `long_native_event_demo.py` | Long universe liquidity and data buffer | fixed days | active long profile | `100d` exists to populate `90d` turnover after trims. Do not shorten below data dependency. |
| Long `regime_sma_days=30`, `vol_estimate_window_days=30` | `_v11a_long_native_config()` | Long regime and vol-parity inputs | fixed days | active long profile | Treat as profile constants until forward evidence accumulates. Retuning needs full long prereg. |
| Long `fc_sniper_deadline_hours=6`, `fc_max_hold_days=3`, `cooldown_days=7` | `_v11a_long_native_config()` | Long entry/exit lifecycle | fixed hours/days | active long profile | Mechanics from the accepted internal object. Do not calendar-normalize. |
| ATR windows `atr_14d`, `atr_20d` | long-native features/exits | Long volatility and trailing stop inputs | fixed daily bars | active/feature | Keep explicit. Any variation is a volatility-model study, not a calendar cleanup. |
| Metrics `worst_30d_return`, `worst_90d_return` | `trade_lifecycle.py`, reports | Reporting stress windows | fixed days | reporting | Not strategy parameters. Add adjacent reporting windows if needed, but do not tune strategy from them. |
| Forward readiness `tier3_days_gate_30` | `continuous_forward_replay.py` | Minimum forward ledger sample age | fixed calendar days | evidence threshold | Operational evidence gate, not alpha. Do not relax to rescue a run. |
| Cache prune `gzip_after_days=7`, `prune_after_days=60` | collectors/tests | Disk retention | fixed days | operational | Disk/SLA policy. Separate from research lookbacks. |

## Research Lookback Standard

Every new research lookback must declare:

- intent: feature, lifecycle, risk memory, evidence gate, or ops SLA.
- unit: fixed hours, fixed days, calendar month, event count, or half-life.
- causality: exactly what data exists at `decision_ts`.
- neighboring family: the values that would falsify parameter fragility.
- plateau rule: what "stable enough" means before the result can advance.
- negative controls: hash, permutation, delayed state, or venue split as
  appropriate.

Default neighboring families:

| Surface | Default family |
| --- | --- |
| Symbol toxicity memory | `0.5m`, `1m`, `2m`, `3m`, `6m`, plus half-life variants |
| Entry-time blacklist memory | half-life `1m`, `3m`, `6m`; min effective rows `12`, `30`, `40` |
| Daily feature lookbacks | `0.5x`, `1x`, `2x` around the named window, preserving calendar-bounded implementation |
| Execution timers | adjacent fixed-hour values only after a lifecycle preregistration |
| Ops SLAs | derive from expected cadence and incident response, not return metrics |

For "month" language, report both:

- month-equivalent days: `365.25 / 12 = 30.4375`.
- calendar-month arithmetic, if the mechanism is truly calendar-month based.

If those disagree materially, the result is not robust enough to influence a
runtime change.

## Implementation Pass 2026-07-04

Elapsed timestamp windows now use the shared exact-duration primitives across
the active long/continuous research and demo paths:

- `exact_duration_ms()` rejects non-finite, negative, or sub-millisecond
  durations instead of rounding.
- `exact_lookback_cutoff_ms(anchor_ts_ms, ...)` subtracts from the exact anchor
  timestamp. It does not floor to a UTC hour or day.
- Converted live/research cooldown, entry-delay, max-hold, quarantine,
  same-symbol cooldown simulation, alert cooldown, kline retention/bootstrap,
  recent-history, adoption hold, closed-PnL paging, and disabled upper-wick
  audit windows to the shared helpers.
- Added one-millisecond boundary tests for the converted windows.

Remaining raw `MS_PER_DAY` / `MS_PER_HOUR` expressions were audited and left in
place only when their purpose is calendar or bar-grid alignment: UTC day/hour
flooring, daily feature grids, exchange interval maps, report bucketing,
annualization, dataset padding around day-grids, and synthetic data generation.
Those are not "same timestamp X time ago" lookbacks; converting them blindly
would weaken the timestamp semantics rather than improve them.
