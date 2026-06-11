# Pre-registration: TA1 — trade-outcome atlas (winners vs losers, cross-book)

**Date:** 2026-06-11 (registered BEFORE computation). **Label:** `exploratory`
hypothesis-GENERATOR — by construction nothing here is promotion evidence.
**Authority:** standing operator directive ("gather a large dataset, analyse it,
find characteristics of winning/losing trades, apply them").

## Posture (the honest version of "apply them")

Winner/loser characterization on the spent window is how several banked findings
started (age gate, rmom, ff6) — and also how mirages start (cross-listing filter:
helped one venue, hurt the other). The window cannot certify new in-sample claims.
So the application path is FIXED UP FRONT: any surviving characteristic becomes
(a) an ARMED forward/OOS judgment (PE2-style, on post-freeze data), or (b) a
paper-shadow — NEVER a spent-window promotion. The two guards, stronger than any
prior single-book screen:

1. **Cross-book replication**: a trait must move outcomes in the SAME direction in
   the deployed SHORT book and the accepted LONG book (mechanistically signed per
   book where the books are short/long mirrors).
2. **Cross-venue replication** where the data allows (depth features are
   binance-only — flagged; their guard is cross-book only).

## Dataset (fixed)

The four fresh deployed-profile ledgers (window 2023-04-01→2026-05-28, clean
full-PIT): SHORT bybit + binance (`short_promoted_2026-06-10`), LONG bybit +
binance (`long_regularity_2026-06-10/00_baseline`). Outcome = per-trade
`net_return` (primary: win = net_return > 0; secondary: top vs bottom net
quartile).

## Feature menu (FIXED — 11 features; multiple-testing accounting: 11 × 4 ledgers
× 2 outcome cuts; consistency, not p-hacking, is the filter)

Context at ENTRY, all causal:

1. UTC session bucket (Asia 00–08 / EU 08–16 / US 16–24 by entry hour).
2. Weekend entry (Sat/Sun) vs weekday.
3. Concurrent open positions in the same book at entry (ledger overlap count).
4. Book's trailing realized PnL state at entry: sum of the prior 10 closed trades'
   net (entering during a book drawdown vs a hot streak).
5. Repeat-name: same symbol traded by the same book within the prior 30 days.
6. BTC 30d trailing return magnitude at entry (regime depth, not just sign).
7. Entry-day EW alt-market return (breadth context).
8. Symbol's trailing 24h turnover at entry (size/liquidity tier within the book).
9. (binance ledgers only) entry-side depth notional@1% at the entry hour.
10. (binance only) book imbalance at entry: (bid@1% − ask@1%)/(bid+ask).
11. (SHORT ledgers only) `crowding_entry_hour_signal_count` (already a column).

## Protocol

Per ledger × feature: winners-vs-losers delta (continuous features: median diff +
bootstrap 90% CI, 1000 reps, seed 20260611; categorical: win-rate per bucket +
binomial CI). A feature SURVIVES the atlas iff its effect direction is consistent
across ALL ledgers where it is defined AND at least one ledger's CI excludes zero
AND a one-sentence mechanism can be stated. Survivors are RANKED; the top
survivor(s) get an armed forward/OOS receipt (path (a)/(b) above) written in the
same session. No same-window re-backtesting of any derived gate.

## Artifacts

`C:/Users/user/SHARED_DATA/trade_atlas_2026-06-11/` — joined dataset parquet +
report JSON. Script: `scripts/trade_atlas.py`.

## Findings (filled in after the run) — no clean survivor; two forward-watch leads

952 trades across the four ledgers (367/198 short, 192/195 long), atlas parquet
written. Against the pre-registered survival rule:

- **`mkt_day_ret` — the strongest pattern, DISQUALIFIED on causality review.**
  Perfectly mirror-signed across all four books (short winners enter on
  market-down days, W−L −1.97% bybit CI*, −0.14% binance; long winners on
  market-up days +1.05%*/+1.29%*) — but entries execute at ~01:00 UTC and the
  entry-DAY return is ~23h of future information. As constructed it is NOT an
  entry feature; it re-confirms the documented CONTEMPORANEOUS market-beta
  structure (WP1a → the banked hedge treatment). Recorded as a menu-design
  error caught in review, not a finding.
- **`repeat30d` (repeat-name penalty) — direction-consistent in ALL FOUR books**
  (repeat entries win less: −6.2/−13.3/−9.4/−0.8pp) but individually
  insignificant everywhere (9–36 repeats per book; pooled ≈ −7pp at ~1.4σ).
  FAILS the bar (no CI excludes zero). → FORWARD-WATCH lead #1.
- **`weekend` — positive on three books** (+17.2/+13.1/+9.9pp WR) and ~flat
  negative on the largest (bybit short −0.6pp). Fails all-ledger consistency;
  buckets small. → FORWARD-WATCH lead #2.
- `session` is degenerate (all four books enter ~01:00 UTC — zero variance; menu
  error), and `concurrent`, `crowding`, `btc30_mag`, `log_turn`,
  `depth_entry_side`, `depth_imbalance` show no consistent signed effect
  (`trail10_pnl` flips sign on short_binance — fails).

**Application per the fixed path:** no spent-window change. The two leads are
cheap, causal, and trivially computable on FORWARD ledgers; they are hereby ARMED
as forward-watch observables: when ≥100 forward demo/paper trades have accumulated
per book (any venue mix), recompute the repeat30d and weekend win-rate deltas on
forward trades ONLY; a lead graduates to a pre-registered gate proposal iff its
forward direction matches the atlas AND the pooled forward effect is ≥2σ. Until
then neither may be cited as evidence. No other atlas finding may be revisited on
the 2023-04→2026-05 window.
