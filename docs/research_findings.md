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

## What the VPS demo is for

The pre-2023 OOS failure means the only forward evidence available is the
**live VPS demo**. That is the actual out-of-sample test of whether the IS
evidence holds outside 2023-26. The demo's accumulating slippage, fill timing,
and regime sensitivity are the strongest available signal of edge durability.

## Methodology

See `docs/backtesting_errors_we_never_repeat.md`. No real-money deployment
claim is made beyond what the evidence supports — the VPS forward test is the
forward evidence, and the strategy is not real-money-validated.
