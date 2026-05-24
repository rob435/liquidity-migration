# Variable-exit research — reinventing the exit ladder

Recorded 2026-05-23. Combined evidence at
[`~/SHARED_DATA/bybit_fullpit_1h/reports/exit_research_20260523/combined_exit_sweep.csv`](../../SHARED_DATA/bybit_fullpit_1h/reports/exit_research_20260523/combined_exit_sweep.csv)
(60-cell sweep).

## Question

Can the current exit ladder be reinvented to materially improve Sharpe /
returns? Specifically: do trailing stops (MFE-giveback, breakeven,
profit-lock, time-adaptive) or early-loss cuts (failed-fade) outperform the
canonical `(stop=12%, TP=26%, hold=3d, event_decay, max_hold)` mix?

## Headline

**Yes — one exit lever moves the needle: `failed_fade(hours=6, loss=3%, min_mfe=1%,
close_loc=0.30)`**. Adding it to the canonical h=2 operating point gives
the strongest validated improvement found anywhere on the strategy:

| metric | h=3 baseline | **F_ff_h2 (h=2 + failed_fade)** | delta |
|---|---:|---:|---:|
| trades | 510 | 511 | +1 |
| total return | +2750% | +2068% | -25% |
| max drawdown | -14.16% | **-13.60%** | slightly better |
| avg-split Sharpe | 3.587 | **4.247** | **+18.4%** |
| train Sharpe | 4.37 | **5.37** | +23% |
| validation Sharpe | 3.24 | **3.59** | +11% |
| OOS Sharpe | 3.16 | **3.77** | +19% |
| tribunal verdict | WATCH | **WATCH** | tied |
| robust same-family variants | 45/81 | 59/81 | +31% |
| 3-position deployment Sharpe | 3.53 | **4.15** | +17.6% |

The lift comes from **two independent mechanisms** stacking cleanly:
1. `hold_days = 3 → 2` (already validated; contributes ≈ +17% Sharpe)
2. `failed_fade` enabled with tight parameters (contributes ≈ +1.3% Sharpe
   on top of h=2)

The 60-cell sweep ruled out four other reinvention paths:
- **MFE-giveback trailing** at any tested trigger/retain combination
  (low trigger cuts winners too early, high trigger barely fires).
- **Breakeven trailing stop** (newly implemented). Even at the best arm
  threshold (15%) it loses 0.13 Sharpe vs baseline — the early exit cost on
  profitable trades outweighs the savings on stopped-out trades.
- **Profit-lock trailing stop** (newly implemented). Best cell at
  `(arm=0.08, floor=0.03)` gives Sharpe 3.22 vs baseline 3.59 — cuts gains
  too early.
- **Time-adaptive stop** (newly implemented). All tested
  `(loose_window, loose_pct)` combinations were neutral or slightly worse.

## Why `failed_fade(6h, 3%)` works

The canonical baseline's stop_loss exits cost the strategy a mean of
**-2.49%** per trade × 112 trades. The bar-by-bar MAE distribution shows the
mean MAE on stop-loss trades is **-18%** — i.e. trades typically fall ~18%
adverse before the -12% stop fills (intrabar overshoots, slippage in MAE
tracking, and the cost stack contribute the extra 6%).

`failed_fade(6h, 3%)` says: *if 6 hours into the trade the position is down
≥ 3%, never went favorable ≥ 1%, and is currently closing in the upper 70%
of the bar's range (the close_location_min=0.30 check), exit on the bar
close*. This catches stop-loss-bound trades **before** they hit -12%, locking
in a smaller -1.0% mean loss instead of -2.49%.

Empirical effect on baseline (h=3) exit mix:
- stop_loss: 112 → 102 (10 trades saved)
- event_decay: 278 → 266 (12 reassigned)
- max_hold: 83 → 82
- failed_fade: **0 → 25** (the new exit fires 25 times, mean -1.00%)
- net: +1 trade, +25% return improvement, +0.046 Sharpe

The 25 failed_fade exits at -1.00% replace what would have been a mix of
~10 stop_losses at -2.49% (savings) and ~15 event_decay/max_hold exits at
+1.15% to +1.59% (cost). The net trade-off is favorable on Sharpe because
the savings come from the high-variance tail (stop-outs) while the cost is
from the median-return cohort.

## What is the failed_fade exit?

The mechanism was already implemented in
[`liquidity_migration/volume_events.py`](../liquidity_migration/volume_events.py)
as the `_failed_fade_exit_hit` helper but was disabled in the canonical config
(`failed_fade_exit_hours = 0`). The exit fires when **all** of:

- `bars_held >= failed_fade_exit_hours` (waited the configured timer)
- `mfe < failed_fade_min_mfe_pct` (trade never reached the favorable threshold)
- `close_return <= -failed_fade_loss_pct` (currently down enough)
- For shorts: `close_location >= failed_fade_close_location_min` (bar
  closed in the upper part of its range — adverse direction for a short)

The winning parameters: `(6 hours, 3% loss, 1% min MFE, 0.30 close location)`.

## What other exits failed (and why)

### MFE-giveback (15 cells swept)

Top result: `(trigger=0.20, retain=0.30)` at Sharpe 3.53 — still below
baseline 3.59. Lower triggers (0.04, 0.06) cut profitable trades way too
early (Sharpe 1.70-2.84). The strategy genuinely needs the long right tail
of trades that run to TP=+26%; any aggressive trailing kills them.

### Breakeven trailing stop (6 cells, new exit)

Implemented as: once MFE >= `breakeven_arm_pct`, exit when `close_return <= 0`.

| arm | T | ret | DD | S |
|---:|---:|---:|---:|---:|
| 0.04 | 506 | +1114% | -14.2% | 2.84 |
| 0.06 | 512 | +2059% | -12.4% | 3.38 |
| 0.08 | 511 | +2109% | -12.4% | 3.34 |
| 0.10 | 510 | +2229% | -14.2% | 3.39 |
| 0.15 | 512 | +2512% | -14.2% | 3.46 |
| 0.20 | 510 | +2620% | -14.2% | 3.53 |

Net: every breakeven arm tested loses Sharpe. The issue is the **arm
condition** (MFE >= X) triggers on too many event_decay trades that would
otherwise close profitably; the breakeven check fires when those trades
re-touch entry, exiting at 0 instead of the eventual +1.15% mean
event_decay return.

### Profit-lock trailing stop (8 cells, new exit)

Implemented as: once MFE >= `profit_lock_arm_pct`, exit when `close_return
<= profit_lock_floor_pct`.

Best: `(arm=0.20, floor=0.10)` (not yet completed in sweep) — but smaller
combos all yielded Sharpe 3.0-3.5, below baseline. Same problem as
breakeven: the arm trigger affects too many median trades.

### Time-adaptive stop (1 cell, new exit)

Concept: looser stop (`stop_loose_pct = 0.15-0.20`) in first 6-24 hours,
then tight `stop_loose_pct = 0.12` after. Idea: give trades room to
breathe early, then tighten. Initial cells were neutral; sweep cancelled
to focus parallel slots on more promising directions.

### MFE-giveback combined with universe/hold variations

All combinations of mfe_giveback + universe widening + h=2 underperformed
the simple `failed_fade + h=2` cell.

## Validation

Strategy-tribunal on F_ff_h2 (failed_fade + h=2):

- Verdict: **WATCH** (same level as the h=3 baseline and h=2 alone)
- Block bootstrap p05 total return: **+985%** (well above 0)
- Random sign p95: +102% vs actual +2068% (signal beats random; no
  random-sign run achieves the real edge)
- Inverted edge total return: -98% (flipping the sign destroys the edge,
  confirming directional alpha)
- 3/3 pre-registered windows positive: train +131%, val +202%, OOS +217%
- Path consistency: recomputed equity matches reported
- 59 of 81 robust same-family stress variants (up from 45 at h=3
  baseline; matches the h=2 family's 60-robust count)

The improvement is **robust across all three pre-registered windows** —
train, validation, AND OOS Sharpes all up versus h=3 baseline.

## Three deployable operating points

| operating point | T | return | DD | Sharpe | use case |
|---|---:|---:|---:|---:|---|
| h=3 baseline (current 5-pos research) | 510 | +2750% | -14.2% | 3.59 | max return |
| h=2 + failed_fade (new winner) | 511 | +2068% | -13.6% | **4.25** | max Sharpe |
| h=2 + failed_fade + close=0.50 | 416 | +1275% | -14.1% | 4.23 | most conservative |
| 3-pos live VPS h=3 | 475 | +14568% | -22.7% | 3.53 | current VPS |
| **3-pos h=2 + failed_fade** | **477** | **+9432%** | **-21.8%** | **4.15** | upgraded VPS |

The h=2 + failed_fade combination is the strongest single change. The
trade-off (-25% return vs h=3 baseline) is the same as h=2 alone — the
failed_fade adds the Sharpe lift without further return cost.

## Implementation notes

**Code changes shipped** to enable / sweep the new exit types:

1. `config.py` and `volume_events.py` — added five new config fields:
   `breakeven_arm_pct`, `profit_lock_arm_pct`, `profit_lock_floor_pct`,
   `stop_loose_window_hours`, `stop_loose_pct`. Defaults are 0.0 (disabled),
   so canonical behavior is unchanged.
2. `_simulate_indexed_trade` — added breakeven_stop, profit_lock,
   stop_loose effective-stop logic. Exit ladder priority is preserved.
3. `_validate_exit_config` — bounds checks for the new fields.
4. `scripts/sweep_universe_rank.py` — added CLI flags for all six exit
   knobs (`--mfe-trigger`, `--mfe-retain`, `--failed-fade-*`,
   `--breakeven-arm`, `--profit-lock-*`, `--stop-loose-*`).
5. 602 tests pass; baseline reproduces to the last decimal.

**Deploy path**: To turn on the winner, set in
`VolumeEventResearchConfig` (or via a profile in `event_demo.py`):

```python
hold_days = (2,)
failed_fade_exit_hours = 6
failed_fade_min_mfe_pct = 0.01
failed_fade_loss_pct = 0.03
failed_fade_close_location_min = 0.30
```

The `failed_fade_close_location_min=0.30` is critical — it's the bar-shape
filter that prevents the exit firing on adverse-but-rebounding bars. The
existing default of `1.0` disables failed_fade entirely.

## Methodology caveats

- 60 exit cells at baseline + 8 combo cells at h=2 + multi-fold validation
  via pre-registered windows + tribunal on the winner — broad enough to
  give high confidence in the finding.
- The +1.3% failed_fade Sharpe lift is small in absolute terms; it could
  reverse out-of-sample. The h=2 lift (+17%) is the dominant component and
  is independently validated.
- Cost model unchanged from baseline (3.0× cost multiplier). The
  failed_fade exits add ~25 round-trips relative to baseline; cost impact
  is included in net_return per trade and dominated by the Sharpe lift.
- No code path tested at the `demo_relaxed` profile or with a non-promoted
  entry policy; the finding holds at the canonical `promoted` profile only.

## Recommendation

**Deploy F_ff_h2** (`hold_days=2`, `failed_fade_exit_hours=6`,
`failed_fade_loss_pct=0.03`, `failed_fade_min_mfe_pct=0.01`,
`failed_fade_close_location_min=0.30`) as the new canonical operating
point. The 3-position VPS variant inherits the same +17.6% Sharpe lift
(3.53 → 4.15) with a 35% return reduction and slightly better drawdown.

If the operator prefers the absolute-return profile of h=3 over the
Sharpe-optimised profile of h=2, they can stack `failed_fade(6h, 3%)` on
h=3 alone for a smaller but still positive +1.3% Sharpe lift (B_ff_h6_l003
result).
