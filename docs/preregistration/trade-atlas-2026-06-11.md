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

## OPERATOR OVERRIDE (2026-06-11) — gates backtested and wired into the demo

The operator explicitly overrode the forward-only application path ("give me the
backtest with [both leads] and get it wired up into the demo. i know what im doing
here"). Executed as ordered, with the label discipline intact:

- **Implementation**: repeat-name penalty → `cooldown_days=30` (the engine cooldown
  is post-exit, slightly stricter than the atlas's entry-to-entry definition —
  declared). Weekend bonus → `weekend_size_mult=1.5` (new engine knob, both engines,
  Sat/Sun entries; the atlas measured a win-rate delta — the 1.5× size tilt is the
  chosen application form, declared).
- **Backtests (RUN 2026-06-11, all 16 cells clean `full_pit_universe`, untainted)**:
  `short_atlas_gates_2026-06-11` + `long_regularity_2026-06-10/TA4*`, both venues,
  exact deployed profiles ± gates, window 2023-04-01→2026-05-28 (3.16y). MAR =
  CAGR/|maxDD|. These numbers are IN-SAMPLE-DERIVED-GATE descriptives on the spent
  window (the features were selected on these very trades) — recorded for
  transparency, NEVER promotion evidence. The forward-watch graduation bar above
  remains the only evidence path.

  | book | cell | trades | return | maxDD | MAR | dMAR | Sharpe |
  |---|---|---|---|---|---|---|---|
  | SHORT bybit | baseline | 367 | +81.8% | −7.2% | 2.92 | — | 1.75 |
  | SHORT bybit | **TA40 cd30** | 348 | +85.3% | −6.2% | **3.51** | **+0.59** | 1.82 |
  | SHORT bybit | TA41 we1.5 | 367 | +89.6% | −7.7% | 2.93 | +0.02 | 1.64 |
  | SHORT bybit | TA42 both | 348 | +92.3% | −7.5% | 3.06 | +0.14 | 1.69 |
  | SHORT binance | baseline | 198 | +7.2% | −14.4% | 0.15 | — | 0.35 |
  | SHORT binance | TA40 cd30 | 190 | +9.5% | −12.5% | 0.23 | +0.08 | 0.45 |
  | SHORT binance | TA41 we1.5 | 198 | +12.8% | −13.8% | 0.28 | +0.13 | 0.51 |
  | SHORT binance | TA42 both | 190 | +14.3% | −12.4% | 0.35 | +0.19 | 0.57 |
  | LONG bybit | baseline | 192 | +29.0% | −3.5% | 2.40 | — | 1.96 |
  | LONG bybit | TA40 cd30 | 161 | +26.0% | −3.6% | 2.13 | −0.28 | 1.91 |
  | LONG bybit | **TA41 we1.5** | 192 | +33.6% | −3.6% | **2.65** | **+0.25** | 1.97 |
  | LONG bybit | TA42 both | 161 | +29.8% | −3.7% | 2.32 | −0.09 | 1.92 |
  | LONG binance | baseline | 195 | +22.8% | −3.9% | 1.74 | — | 1.45 |
  | LONG binance | TA40 cd30 | 165 | +19.8% | −3.9% | 1.52 | −0.22 | 1.37 |
  | LONG binance | **TA41 we1.5** | 195 | +27.8% | −4.0% | 2.02 | **+0.28** | 1.51 |
  | LONG binance | TA42 both | 165 | +23.7% | −4.0% | 1.74 | +0.00 | 1.41 |

- **Best values (MAR-primary, Sharpe secondary, both venues — the wired choice)**:
  per-book, NOT both-gates-everywhere. **SHORT: `cooldown_days=30` only** (dMAR
  +0.59/+0.08, Sharpe up both venues; the weekend tilt is return-only dilution on
  bybit — Sharpe 1.75→1.64 at dMAR +0.02 — and was rejected for this book,
  consistent with the atlas, where weekend was flat-negative on bybit short).
  **LONG: `weekend_size_mult=1.5` only, cooldown stays 7** (dMAR +0.25/+0.28,
  Sharpe up both venues; the 30d post-exit cooldown HURTS the long book on both
  venues, dMAR −0.28/−0.22 — the all-4-books atlas direction did not survive the
  engine's stricter post-exit form there). Each book ships exactly the gate whose
  atlas evidence was strongest for it.
- **Sweep-base note**: the sweep scripts pin the PRE-gate base
  (`cooldown 5/7, weekend 1.0`) explicitly, because the deployed profiles now carry
  the gates — without the pin, every cell would silently inherit them.
- **Wiring (final)**: `event_demo._demo_event_config("promoted")` carries
  `cooldown_days=30`; `_v11a_long_native_config()` carries `weekend_size_mult=1.5`
  (cooldown 7). Both sleeves are currently OFF on the VPS, so the gates are dormant
  until the operator deploys/enables.
- Standing risk, stated plainly: if the leads are window noise, the demo books
  will trade them anyway when enabled; the forward-watch recompute is the
  detection mechanism either way.
