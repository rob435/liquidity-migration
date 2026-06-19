# Continuous V2 A/B Amendment - Binance-Only Flow Branch

Date: 2026-06-19

Parent plan: `docs/preregistration/2026-06-18-continuous-v2-ab-research-plan.md`

Related data receipt:
`docs/preregistration/2026-06-19-continuous-v2-data-topup-flow-blockers.md`

## Operator Decision

Run the flow and microstructure research branch on Binance only. Do not block
the Binance flow work on a Bybit full-market taker-flow archive build.

## Reason

The 2026-06-19 data top-up made Binance OI, taker imbalance, `market_flow`, and
`idiosyncratic_flow` admissible in the feature almanac. Bybit flow is still not
full-market: the current `taker_flow_5m` tape is event-scoped and cannot support
the C-book flow claims.

Blocking all flow work on a large Bybit archive build would stop useful
mechanism learning. Treating Binance-only flow as candidate evidence would be a
methodology error. The amended path is therefore narrow: run Binance-only flow
research, label it exploratory, and keep the two-venue candidate bar intact.

## Plan Change

Problem Book C arms now run with:

- `claimed_venue_scope=binance_only_flow_exploratory`
- venue set: `binance`
- data source: `~/SHARED_DATA/binance_full_pit/binance_usdm_metrics_5m`
- run label: `exploratory`

Renamed amended arms:

- `C0_ORDERFLOW_SCREEN_BINANCE_ONLY`
- `C1_FLOW_RESID_FEATURE_SIZING_BINANCE_ONLY`
- `C2_MARKET_FLOW_HEDGE_INTENSITY_BINANCE_ONLY`
- `C3_FLOW_SQUEEZE_HEDGE_INTENSITY_BINANCE_ONLY`
- `C4_FLOW_DIVERGENCE_ADMISSION_BINANCE_ONLY`
- `C5_FLOW_UNWIND_EXIT_SHADOW_BINANCE_ONLY`
- `C6_NONLINEAR_FLOW_SCORE_BINANCE_ONLY`
- `C7_FLOW_HASH_CONTROL_BINANCE_ONLY`

## Evidence Limits

A positive Binance-only flow result can:

- rank flow mechanisms;
- justify spending compute/storage on a Bybit full-market public-trade backfill;
- motivate a later venue-policy amendment.

It cannot:

- clear the Tier-2 candidate bar;
- support Bybit demo/paper wiring;
- be called cross-venue alpha;
- be cited as real-money evidence.

## Required Falsifiers

Every amended C-book result must report:

- residualized-flow incrementality after lagged returns and current composite;
- flow-shuffle, symbol-hash, and calendar-hash controls;
- time-split and liquidity-bucket stability;
- drawdown versus return tradeoff;
- explicit statement that the result is single-venue exploratory.
