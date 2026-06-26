# Research Program State

Last updated: 2026-06-25.

Read this first. This page is live state, not a receipt archive. Historical
details are in git history, local artifacts, and
`docs/preregistration/INDEX.md`.

## Current Systems

| System | Current role | Read |
| --- | --- | --- |
| `continuous_ensemble_v2` | Continuous fade demo/paper book | Needs forward trade sample; local target and VPS are not fully aligned yet |
| `LongV11aDivWeekendVol` | Long-native v11a demo/paper sleeve | Best current internal positive object; still needs forward sample |

The daily short sleeve was removed from the active system on 2026-06-11.
Mainnet is not the current operating mode; changing that requires explicit owner
action and fresh evidence.

## What Is Wired

- `deploy/sleeves.env`: long demo/paper ON, continuous demo ON, continuous paper
  ON.
- Continuous local target: TP12, daily rebalance disabled, no daemon/server
  stop, inverse-vol sizing, BTC/ETH hedge, BTC-vol regime, and
  `CTRL_BTC_RISK_70_90_35` sizing overlay.
- Latest read-only VPS observation still showed older continuous code on commit
  `e8c8080`: daily rebalance enabled and missing the local BTC gate telemetry
  and BTC-risk sizing changes.
- Deploying the local continuous target requires owner confirmation, checked
  deploy, state-clock archival/reset, and hedge warmstart regeneration.

## Continuous Read

- Baseline clock: `2026-06-18T19:54:00Z`.
- Core components: p3 weight `0.3333333333333333`, p4p3 weight
  `0.2222222222222222`, p4p5 weight `0.4444444444444444`.
- Active exits: 12% component TP and 24h max hold.
- Disabled exits/risk rules: `left_decile`, `stop_approach`, `failed_fade`,
  `breakeven`, re-entry cooldown, server stop.
- `CTRL_BTC_RISK_70_90_35`: MAR and drawdown improved on both venues; Binance
  total return fell. Treat it as a local demo/paper sizing experiment, not
  broad acceptance proof.
- Daily vol rebalance A/B on 2026-06-25 rejected turning legacy ON back on for
  TP12 components. The ON rule mostly hit max leverage and failed the
  MAR/drawdown/worst-90d rule; keep it disabled.
- The 2026-06-19 and later continuous A/B work produced no accepted candidate.
  Flow, conviction sizing, intrabar entry timing, hold/exit timing, TP variants,
  upper-wick sizing, and gate replacements either failed hash/two-venue controls
  or were not executable with current data.

## Long v11a Read

Latest internal cross-venue refresh through the 2026-06-23 signal day:

| Venue | Trades | Return | Max DD | Sharpe-like |
| --- | ---: | ---: | ---: | ---: |
| Bybit | 188 | +32.87% | -3.46% | 1.98 |
| Binance | 190 | +27.59% | -4.00% | 1.46 |

Durability checks:

- Positive after removing best month and after 2x/3x cost stress.
- Deterministic monthly bootstrap p05 positive on both venues.
- Worst 12-month windows positive.
- Active monthly sign agreement: 24/26.
- Paired same-entry signal agreement: 144/146, return correlation 0.9679.
- Random-symbol null beaten on both venues.

Material caveat: take-profit tail winners carry the sleeve. Removing the TP exit
bucket flips both venues negative. PIT OHLC exit-path validation supports the
recorded TP exits mechanically; it does not remove the concentration caveat.

## Data And Reconciliation

- Full-PIT Bybit and Binance roots are current enough for the latest internal
  long refresh and continuous replay maintenance.
- Binance June-tail funding remains sparse for many manifest symbols, but all
  refreshed long trades used modeled funding.
- Latest full three-way reconcile exited 0, pulled demo/paper telemetry, rebuilt
  PIT context, and found no unexplained live/paper/model drift.
- No active forward trade/order rows yet for the surviving sleeves; current
  evidence is therefore state/reconciliation/readiness, not performance.

## Current Next Work

1. Let forward demo/paper accrue actual trade samples.
2. Keep running `scripts/reconcile.sh` after meaningful VPS/data changes.
3. Before deploying the local continuous target, archive/reset the continuous
   forward clock and regenerate hedge warmstarts.
4. Do not reopen broad continuous mining without a specific falsifiable
   hypothesis and missing-data plan.
5. If long v11a receives forward trades, audit paper/demo/fills/funding before
   making any claim from the sample.

## Canonical Docs

- `docs/research_summary.md` - compact decision log.
- `docs/promoted_trading_logic.md` - active lifecycle and runtime boundary.
- `docs/data_roots.md` - data-root contract.
- `docs/pit_gate.md` - PIT/reconcile contract.
- `docs/preregistration/INDEX.md` - active anchors and closed arcs.
