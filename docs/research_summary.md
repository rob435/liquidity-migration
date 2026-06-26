# Research Summary

Updated: 2026-06-25.

This is the durable decision log. Detailed receipts, command transcripts, and
one-off append logs are intentionally out of the hot path. See
`docs/preregistration/INDEX.md` for active anchors and use git history for exact
deleted receipt text.

## Active Objects

| Object | Role | Current read |
| --- | --- | --- |
| `continuous_ensemble_v2` | Continuous fade demo/paper book | No forward trade sample yet; local target still needs checked deploy |
| `LongV11aDivWeekendVol` | Long-native v11a demo/paper sleeve | Strongest current internal object; TP-tail dependent |

The old daily short sleeve is not dormant; it is gone from the active system.
Mainnet is outside the current operating mode.

## Evidence Standards

- Forward demo/paper is the arbiter for execution behavior.
- Internal backtests are useful for mechanism and regression checks; they do not
  by themselves settle deployment or mainnet questions.
- PIT membership, causal feature availability, survivorship control, costs,
  funding, ledger identity, and reproducible artifacts are correctness gates.
- Exploratory runs can guide investigation, but cannot accept a parameter.

## Continuous v2

### Current Local Target

- Baseline clock: `2026-06-18T19:54:00Z`.
- Components: p3 `0.3333333333333333`, p4p3 `0.2222222222222222`, p4p5
  `0.4444444444444444`.
- Sizing: inverse vol, `TARGET_VOL_PER_NAME=0.01`, `VOL_WEIGHT_CLAMP=2`.
- Entries: `BTC_TREND_GATE=uptrend`, rmom q25, max 25 active shorts, max 5 new
  entries per cycle.
- Exits: 12% component TP and 24h max hold.
- Disabled: daily rebalance, daemon/server stop, `left_decile`,
  `stop_approach`, `failed_fade`, `breakeven`, re-entry cooldown.
- Hedge: BTC+ETH 2-factor hedge plus BTC-vol regime overlay.
- Add-ons: demo sniper execution watch; dynamic exit no-order shadow.
- Sizing overlay: `CTRL_BTC_RISK_70_90_35`.

Latest VPS observation is older than the local target: commit `e8c8080`, daily
rebalance still enabled, local BTC gate telemetry absent, and BTC-risk sizing not
proven live.

### Closed Continuous Research

| Arc | Result |
| --- | --- |
| Live-exit diagnosis | `stop_approach` and server-stop style exits broke the short-fade lifecycle; TP/24h became the v2 lifecycle. |
| Deep A/B foundation | No accepted parameter. Flow, conviction sizing, entry timing, and exit timing failed controls or data requirements. |
| TP variants | Bybit liked wider TP; Binance drawdown/MAR rejected the two-venue change. TP12 remains a local target, not broad proof. |
| One-minute execution books | No durable two-venue lead after path-aware controls. |
| Upper-wick sizing | Initial apparent gain was duplicate-counting; corrected full-ledger and parity checks retracted it. |
| BTC gate replacement | Replacement gates failed. `CTRL_BTC_RISK_70_90_35` improved MAR/drawdown but cut Binance total return, so it is a narrow sizing experiment. |
| Daily vol rebalance | 2026-06-25 TP12 A/B rejected legacy ON. It mostly saturated at max leverage, worsened drawdown, and failed the MAR/worst-90d rule; keep disabled. |
| Regime-score work | No common robust replacement survived the current-control and anchor checks. |

Recurring conclusion: continuous signals exist, but most transforms either
vanish under execution constraints, split by venue, or act like leverage rather
than durable edge.

Daily-rebalance caveat: the 2026-06-25 A/B isolated the portfolio rebalance
layer on TP12 component ledgers. The current Bybit rebuild was only 77 calendar
days, and the live BTC-risk entry-size overlay was not embedded in those
component ledgers, so the run is rejection evidence, not positive acceptance
evidence for any new risk layer.

## Long v11a

Latest internal cross-venue refresh through 2026-06-23:

| Venue | Trades | Return | Max DD | Sharpe-like |
| --- | ---: | ---: | ---: | ---: |
| Bybit | 188 | +32.87% | -3.46% | 1.98 |
| Binance | 190 | +27.59% | -4.00% | 1.46 |

Positive evidence:

- Both venues stay positive after removing their best month.
- 2x and 3x existing cost stress remain positive.
- Deterministic monthly bootstrap p05 remains positive: +5.76% Bybit, +5.17%
  Binance.
- Worst 12-month windows remain positive: +2.55% Bybit, +2.79% Binance.
- Active monthly sign agreement: 24/26.
- Same-entry paired trades: 146 common trades, 144/146 sign agreement, 0.9679
  return correlation.
- Removing BTCUSDT, top three positive symbols, or best exit day leaves both
  venues positive.
- Matched random-symbol null beaten on both venues.
- PIT OHLC exit-path validation supports all recorded exits under bar-end
  timestamps and stop-before-TP ordering.

Material caveats:

- Take-profit exits drive the result. Removing the TP bucket flips Bybit/Binance
  to -0.92%/-5.99%.
- Absolute return trails BTC buy-and-hold, though monthly MAR, beta, and
  beta-adjusted residual return are better.
- The latest evidence is still internal; no forward trade sample exists yet.

Closed long research:

| Arc | Result |
| --- | --- |
| FC sigma cadence loosening | More trades, worse guard outcomes; no retained arm. |
| Cross-venue stability audit | Positive but not enough to skip forward evidence. |

## Data And Ops

- Bybit and Binance PIT roots are current enough for the latest long refresh and
  continuous replay maintenance.
- Bybit June manifest kline/funding coverage is clean for refreshed work.
- Binance daily Vision kline/manifest coverage is current; June-tail funding is
  sparse for many manifest symbols, but refreshed long trades use modeled
  funding.
- Reconciliation now runs from Windows with UTF-8 Python I/O and SCP fallback
  when `rsync` is absent.
- Latest full three-way reconcile found no unexplained drift and no active
  trade/order rows.
- Continuous current rows still show no entries or exits; the false BTC uptrend
  gate is explained by PIT BTC trend, not an unexplained daemon mismatch.

## Revisit Queue

1. Forward trade sample for both surviving systems.
2. Continuous deploy alignment if the owner wants the local target live.
3. Long v11a paper/demo/fill/funding audit once trades appear.
4. Only targeted continuous research with a specific missing-data or execution
   mechanism; no broad mining replay.
