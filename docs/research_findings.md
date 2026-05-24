# Research Findings

Updated 2026-05-23.

## Verdict

The liquidity-migration short strategy has a **statistically real but
regime-narrow** cross-sectional reversion edge. The audit-corrected engine
re-baseline (2026-05-22) is strongly positive in-sample on 2023-2026 — but
the strategy fails every pre-2023 OOS variant tested. The "market-neutral,
no regime gate needed" framing in earlier drafts was not supported by the
conditional-regime evidence and has been retracted: the strategy IS materially
short alt-beta (~-0.45 conditional on universe regime), and that is where its
edge comes from.

The deployed VPS configuration (3-position concentrated `promoted` profile)
has been re-baselined with `strategy-tribunal`. The canonical operating point
clears its own gates (-22.66% DD within the -25% promotion gate, all six
negative controls pass, 3/3 in-sample windows positive). The wide-grid stress
matrix fails on the -40% corner — the operator has explicitly accepted that
concentration risk for small-capital deployment.

## Audit-corrected re-baseline figures (2026-05-22)

Canonical 5-position research config (`promoted` + close-0.30, threshold 0.4 /
hold 3d / stop 0.12 / TP 0.26 / cost 3.0x):

- 510 trades; total return 2750.38%; max drawdown -14.16%; avg split Sharpe 3.59
- 3/3 in-sample pre-registered windows positive (train +139%, validation +257%,
  "OOS" 2025-26 +239%)
- 81-scenario symmetric robustness sweep: **79/81 promotable**; returns
  576%-2922%; drawdowns -29.5% to -12.9%
- `strategy-tribunal`: **WATCH** with no FAIL findings

Live 3-position concentrated config (the actual VPS deployment, 33% per trade):

- 475 trades; total return 14568%; max drawdown -22.66% (within the -25% gate);
  avg split Sharpe 3.53
- 3/3 in-sample pre-registered windows positive (min split +267%)
- 81-scenario sweep at 3 positions: **46/81 promotable** (35 fail the -25%
  drawdown gate at grid edges); returns 1296%-15969%; widest corner -40.24%
- `strategy-tribunal`: **FAIL** on `stress_matrix` (wide-grid -40% corner
  exceeds the -35% stress-fail threshold); the canonical operating point
  itself passes its own gates

See `docs/system_status.md` for the detailed re-baseline record. Earlier
figures in this doc (+126%/+225%/+183%, 81/81, "2850% close-0.30") predated
the audit corrections (funding 8x over-charge in `008d34a`; equity daily-grid
and tribunal consistency in `7fc1c1b`/`d9627a4`); they have been superseded.

## What the IS evidence actually shows

A real cross-sectional reversion edge in the 2023-2026 alt market structure.
The four signal sub-components (z-residual return, z-turnover ratio,
z-close-location, z-rank-jump) are not fully collinear — roughly 2-2.5
effective independent factors — so the equal-weight composite is not just
"short alts after they pumped" dressed four ways. Funding is **not** the
edge: mean funding contribution per trade is -0.041%, total -19% across the
475 trades. The strategy profits despite funding, not because of it.

A 25-family cross-family scan (every event family the engine supports, both
directions) finds that exactly one of the 26 variants is a promotable edge:
the `liquidity_migration` short itself. The other 25 all fail or are strongly
negative — that singularity is strong evidence the edge is theory-grounded
(price-insensitive momentum flow exhausts in the weakest-liquidity names)
rather than a data-mining artifact.

All six tribunal negative controls (block-bootstrap p05 deeply positive;
random-sign; inverted-edge -98%; shuffled time/symbol/event) pass cleanly on
the corrected engine.

## Caveats / open weaknesses (honest)

1. **Pre-2023 OOS fails on every variant.** The dedicated pre-2023 Bybit
   (2021-01..2023-05) and Binance USDM (2020-09..2023-04) roots fail every
   pre-registered-window check (0/3 promotable). Binance OOS drawdowns range
   -46% to -51%+ across variants. The strategy does not generalize backward
   into the 2020-22 alt-mania-and-winter regime.
2. **The "pre-registered" windows are not strictly pre-registered.** They are
   CLI args (`--pre-registered-window`), not code-committed before strategy
   parameters were chosen. The 3/3-positive claim is robust *within-sample*;
   it is not an independent OOS test.
3. **Market-neutrality is regime-specific.** Unconditional universe beta is
   -0.07; conditional on bear-universe days the strategy returns +1.94%/day,
   on bull-universe days -1.16%/day — implied conditional beta ≈ -0.45. The
   2023-26 era was a downtrending or range-bound period for most rank 31-150
   alts; the same exposure was catastrophic in 2021 backtests.
4. **Return concentration is extreme.** Top 50 days = ~91% of cumulative
   log-return; top 10 days ~30%. Fat-tailed.
5. **Capacity is tight.** The 3.0x cost multiplier is honest at the $5-10k
   position-size scale of the demo. At $100k position size the slippage curve
   eats ~38% of the edge; at $1M+ on rank 31-150 alts the edge is gone.
6. **The 3-position canonical sits at the -25% drawdown gate.** -22.66%
   canonical vs -25% gate; median 81-grid drawdown -24.9%. Small parameter
   shifts cross the gate; the wide-grid corner hits -40%.

## Universe size & rank-improvement sweep (2026-05-23)

A 105-cell sweep over `universe_rank_max ∈ {100..460}` and
`liquidity_migration_rank_improvement_min ∈ {50..250}` plus adjacent
dimensions (`hold_days`, `close_location_min`, `residual_return_min`,
`turnover_ratio_min`, `event_rank_fraction_max`, `max_active_symbols`,
`universe_rank_min`, `stop_loss_pct`) establishes the validated production
operating point:

**Validated operating point**: `universe_rank_max=200, hold_days=2,
close_location_min=0.50`. Avg-split Sharpe **4.31 vs 3.59 baseline (+20%)**,
DD -16.7% (within -25% gate), tribunal WATCH, 77/81 robust same-family
variants (most robust seen). Per-window Sharpe 5.56 / 3.34 / 4.03
(train/val/OOS) — all up vs baseline 4.37 / 3.24 / 3.16. The lift is
consistent across all three pre-registered windows (not train-overfit).

Decomposition of the +20% Sharpe lift:
- `hold_days=3 → 2` contributes **+17%** (3.59 → 4.19; universe and
  rank-improvement unchanged); the same lift carries to the live 3-position
  VPS deployment (3.53 → 4.12).
- Adding `close_location_min=0.30 → 0.50` contributes **+3%** (4.19 → 4.32
  at u=150).
- Adding `universe_rank_max=150 → 200` contributes **~0%** (4.32 → 4.31) —
  the wider universe carries zero Sharpe cost at this operating point,
  meaning widening to 200 is "free" if the operator wants the capacity
  headroom.

Trade-off: 47% lower compounded return (1451% vs 2750%) and DD slightly
wider (-16.7% vs -14.2%). The lift is in risk-adjusted returns, not
absolute returns.

**What is NOT actionable on these axes**: in isolation, neither widening
universe nor decreasing rank-improvement-min at the baseline hold/quality
config improves Sharpe. Univariate scans:

- `rank_improvement_min ∈ {60, 80, 100, 120}` at baseline universe: Sharpe
  drops to 2.18-2.99, DD opens to -20% to -25.3%.
- `universe_rank_max ∈ {160, 170, 180, 200, 220, 260}` at baseline ri:
  Sharpe drops to 3.05-3.52, DD slightly worsens.
- Joint relaxation (both wider AND lower ri): worse than either alone.
- Quality-filter rescue (tighter close_loc, residual, turnover_ratio
  combined with widening): fails to recover.

The strategy is **not capacity-bound** (3 of 510 baseline trades are
capacity-skipped). The 31-150 / ri=150 boundary is well-tuned for the
2023-26 sample when hold/quality are held fixed; the universe-widening
benefit only materialises when hold/quality are also moved to the h=2 +
close=0.50 operating point.

Full sweep, decomposition, tribunal reports, and 105-cell evidence:
[`docs/research_universe_rank_sweep.md`](research_universe_rank_sweep.md).
Data persisted at
`~/SHARED_DATA/bybit_fullpit_1h/reports/universe_rank_sweep_20260523/combined_sweep.csv`.

## Variable-exit sweep (2026-05-23)

A 60-cell sweep of the trade-exit ladder (MFE-giveback, failed-fade,
plus three newly-implemented exits: breakeven trailing stop, profit-lock
trailing stop, time-adaptive stop) identifies one stackable improvement:
**`failed_fade(hours=6, loss=3%, min_mfe=1%, close_location_min=0.30)`**.

The winning operating point combines this failed_fade with `hold_days=2`
(already validated): avg-split Sharpe **4.247 vs 3.587 baseline (+18.4%)**,
all 3 pre-registered windows improved (train 4.37→5.37, val 3.24→3.59, OOS
3.16→3.77). Drawdown slightly better (-13.6% vs -14.2%). Trade-off: 25%
lower compounded return (same as h=2 alone). 59/81 robust same-family
variants. Tribunal: WATCH (same level). All 6 negative controls pass
cleanly.

The same +17.6% Sharpe lift carries to the live 3-position VPS deployment
(3.53 → 4.15).

Three other exit reinventions did NOT help:
- MFE-giveback trailing at any tested trigger/retain combination — best
  cell loses 0.06 Sharpe.
- Breakeven trailing stop (newly implemented) — every arm threshold tested
  reduces Sharpe (best loses 0.13).
- Profit-lock trailing stop (newly implemented) — best cell loses 0.37
  Sharpe; cuts gains too early.

The new exit types (breakeven, profit-lock, time-adaptive stop) are now in
the code with defaults of 0.0 (disabled), so canonical behavior is
unchanged. Activate via the new config fields `breakeven_arm_pct`,
`profit_lock_arm_pct`, `profit_lock_floor_pct`, `stop_loose_window_hours`,
`stop_loose_pct`, or via the new CLI flags on `scripts/sweep_universe_rank.py`.

Full evidence: [`docs/research_exit_rules.md`](research_exit_rules.md);
sweep data persisted at
`~/SHARED_DATA/bybit_fullpit_1h/reports/exit_research_20260523/combined_exit_sweep.csv`.

## What the VPS demo is for

The pre-2023 OOS failure means the only forward evidence available is the
**live VPS demo**. That is the actual out-of-sample test of whether the IS
evidence holds outside 2023-26. The demo's accumulating slippage, fill timing,
and regime sensitivity are the strongest available signal of edge durability.

## Methodology

See `docs/backtesting_errors_we_never_repeat.md`. No real-money deployment
claim is made beyond what the evidence supports — the VPS forward test is the
forward evidence, and the strategy is not real-money-validated.
