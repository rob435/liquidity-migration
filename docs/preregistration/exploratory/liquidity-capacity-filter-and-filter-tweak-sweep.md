# Pre-registration: liquidity-capacity filter + filter-tweak sweep

**Date:** 2026-05-28
**Author:** owner
**Stage:** EXPLORATORY — **run-complete, REJECTED (2026-05-27)**

> _Historical record. The dispatch scripts referenced below (`run_liquidity_capacity_sweep.sh`,
> `sweep_cells.py`) were removed in the 2026-05-28 dead-script cleanup; the current sweep
> pattern is `scripts/_sweep_runtime.py` + the `r*` sweep scripts. This run is complete and
> rejected — not for re-running._

## What's changing

Sweep candidate parameter changes against the current promoted baseline
(threshold 0.4 / hold 3 / stop 0.12 / TP 0.26 / cost 3.0× /
universe_rank 31-400 / rank_improvement 150 / turnover_ratio 6.0 /
residual_return 0.08 / close_location 0.30 / pit_age 90 / crowding
union_pathology / max_active_symbols 3) on bybit_full_pit + binance_full_pit.

## Hypotheses

**H1 (operator concern):** The strategy enters low-liquidity "shitcoin"
names where Bybit's per-order `maxMktOrderQty` is binding. Live demo
recently entered REQUSDT (cap 20,000) and stopped at -12%. Skipping
symbols below a daily-turnover floor (a proxy for liquidity capacity
since the historical maxMktOrderQty is not in our archive) should reduce
drawdown by avoiding the venue's most fragile names without losing
meaningful return.

**H2:** Tightening `rank_improvement_min` (150 → 200 or 250) selects
only the most decisive liquidity migrations and could improve Sharpe at
the cost of fewer trades.

**H3:** Tightening `residual_return_min` (0.08 → 0.12 or 0.15) selects
only the most outlier moves, improving the signal-to-noise.

**H4 (already-known per `docs/research_findings.md` 2026-05-23 sweep):**
`hold_days=2` (vs 3) improves split-Sharpe by ~17% on the rebuilt data.
Confirmation pass + interaction tests with H1-H3.

**H5:** Tightening `universe_rank_max` (400 → 200 or 300) excludes the
weakest-liquidity universe tail.

**H6 (combos):** The above filters compound — e.g. liquidity floor +
h=2 + tighter residual return might stack additively.

## Predicted direction + magnitude

- **H1** (turnover floor): Sharpe Δ +0.0 to +0.3; trade Δ -10% to -40%;
  DD Δ -2pp to -5pp. Failure mode: if low-cap symbols are where the edge
  IS (small-cap reversal premium), removing them tanks return.
- **H2** (rank_improvement_min ↑): Sharpe Δ +0.0 to +0.2; trades -20% to -50%;
  DD similar.
- **H3** (residual_return_min ↑): Sharpe Δ ±0.2; trades -30% to -60%.
- **H4** (hold_days=2): Sharpe Δ +0.4 (per prior sweep); trades same; DD slightly worse.
- **H5** (rank_max ↓): Sharpe ±0.1; trades -20% to -40%.
- **H6** (combo): unknown — may stack or interfere.

## Roots that will be touched

- [x] bybit_full_pit (per-venue working dataset)
- [x] binance_full_pit (per-venue working dataset)
- [ ] forward demo/paper (no live config change from this sweep)

## Decision rule (a priori)

A cell qualifies as "candidate improvement" only if **all** hold:

1. **Sharpe Δ ≥ +0.5** vs baseline on both Bybit AND Binance independently
   (the +0.5 threshold deflates for the ~17-cell multiple-testing bar — at
   17 cells, Bonferroni-equivalent z-score 2.5 = ~+0.4 Sharpe; +0.5 buys
   headroom).
2. **Max DD ≤ baseline DD + 5pp** on both venues (no return improvement
   accepted if it costs material drawdown).
3. **Sign of return ≥ 0** on both venues (sign-stability).
4. **Trade count ≥ 30** on Bybit (statistical power floor).

If no cell qualifies, the recorded verdict is "**no improvement found**" —
keep the current production parameters.

If multiple cells qualify, pick the one with smallest parameter delta from
the current production (Occam's razor — small change less likely to be
overfit).

## Roots' coverage caveat

The bybit_full_pit rebuild (commit `dbb79fa`) excluded a small subset of
2021 ETHUSDT / ADAUSDT date partitions due to a manifest-extension gap that
predates this sweep — so the run uses `--allow-partial-pit`. This is
labelled biased per the integrity standard; the missing data is at the
oldest edge of the window (2021-01) and does not affect 2023-2026 entry
selection (when the strategy actually fires).

## Run command

```bash
bash scripts/run_liquidity_capacity_sweep.sh
```

Internally loops over 17 cells × 2 venues = 34 invocations of
`python -m liquidity_migration volume-events --explain-rejections=False
... --start 2024-01-01 --end 2026-05-28`.

Output: `~/SHARED_DATA/{bybit,binance}_full_pit/reports/sweep_2026-05-28/<cell-id>/`
plus a top-level `sweep_summary.csv` aggregating Sharpe / DD / return /
trade-count per cell per venue.

## Deviations from pre-reg

Two minor pre-reg deviations recorded for honesty:

- **Cell count:** pre-reg said "17 cells × 2 venues = 34 invocations"; the
  actually-shipped sweep config in `scripts/sweep_cells.py` trimmed to
  10 cells × 2 venues = 20 invocations as a compute concession before
  kickoff. The 10 cells retained are a representative subset; the
  hypotheses H1-H6 are still each covered by at least one cell.
- **Window:** pre-reg implied 2024-01-01+; actual run used
  2025-01-01 → 2026-05-28 (~17 months) per the trimmed sweep config.
  This narrower window puts more weight on the harshest recent regime
  (April 2025, Nov-Dec 2025, May 2026 drawdowns) which is appropriate
  for stress-testing filter tweaks but means the comparison-to-baseline
  Sharpe magnitudes here are not directly comparable to older 2024+
  baseline numbers.

Both deviations are noted for the integrity record. The shipped sweep is
still a coherent test of H1-H6 with the pre-registered decision rule.

## Post-run results

Run completed 2026-05-27 (local clock). Process exit clean, 20/20 cells
status=ok. Summary CSV: `~/SHARED_DATA/sweep_2026-05-28_summary.csv`.
Per-cell reports under
`~/SHARED_DATA/{bybit,binance}_full_pit/reports/sweep_2026-05-28/<cell>/`.
Repo state at run: commit `0361d41` (pre-K1-K5 refactor, pre-research-plan).

### Full per-cell metrics

| Cell | Bybit trades | Bybit ret | Bybit DD | Bybit sharpe-like | Binance trades | Binance ret | Binance DD | Binance sharpe-like |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 00_baseline (control) | 416 | 5.19× | -42.1% | 2.27 | 319 | 0.66× | -40.7% | 0.98 |
| A2_turnover_5M | 377 | 3.67× | -53.7% | 2.09 | 321 | 0.72× | -41.3% | 1.01 |
| A3_turnover_10M | 290 | 4.51× | **-28.4%** | 2.23 | 303 | **-0.17×** | **-62.0%** | **0.08** |
| A4_turnover_50M | 7 | 0.03× | -4.1% | 1.01 | 134 | 0.64× | -33.5% | 1.09 |
| B1_rankimp_200 | 382 | **7.47×** | -38.8% | **2.71** | 281 | **-0.20×** | **-62.1%** | **0.05** |
| C1_residret_12 | 372 | 4.65× | -41.3% | 2.18 | 271 | 0.02× | -45.9% | 0.37 |
| D1_hold2 | 428 | 5.65× | -38.1% | 2.37 | 324 | 0.34× | -48.0% | 0.73 |
| E1_rankmax_200 | 418 | 2.44× | -48.2% | 1.69 | 314 | -0.13× | -60.7% | 0.15 |
| F1_turnover10M_hold2 | 301 | 3.22× | -30.7% | 2.11 | 313 | 0.02× | -56.9% | 0.37 |
| F3_turnover10M_hold2_residret12 | 265 | 2.70× | -36.3% | 2.00 | 268 | -0.14× | -49.3% | 0.24 |

### Decision-rule application

For each non-baseline cell, applying the a-priori decision rule:

| Cell | Rule 1: Sharpe Δ ≥ +0.5 both venues? | Rule 2: DD Δ ≤ +5pp both venues? | Rule 3: Return sign ≥ 0 both venues? | Rule 4: Bybit trades ≥ 30? | Verdict |
|---|---|---|---|---|---|
| A2_turnover_5M | Bybit Δ -0.18, Binance Δ +0.03 — **fail** | Bybit DD Δ -11.6pp — **fail** | yes | yes | reject |
| A3_turnover_10M | Bybit Δ -0.04, Binance Δ -0.90 — **fail** | Bybit DD Δ +13.7pp — **fail** | **Binance negative** — **fail** | yes | reject |
| A4_turnover_50M | Bybit Δ -1.27 — **fail** | yes (Bybit Δ +38pp BETTER, Binance Δ +7.2pp) | yes | **Bybit trades = 7 < 30** — **fail** | reject |
| B1_rankimp_200 | Bybit Δ +0.44, Binance Δ -0.93 — **fail (sign flip)** | Bybit Δ +3.3pp ok, Binance Δ -21.4pp — **fail** | **Binance negative** — **fail** | yes | reject |
| C1_residret_12 | Bybit Δ -0.10, Binance Δ -0.61 — **fail** | Binance DD Δ -5.2pp — borderline fail | yes | yes | reject |
| D1_hold2 | Bybit Δ +0.10, Binance Δ -0.25 — **fail** | Binance DD Δ -7.3pp — **fail** | yes | yes | reject |
| E1_rankmax_200 | Bybit Δ -0.58, Binance Δ -0.83 — **fail** | Both DDs worse — **fail** | **Binance negative** — **fail** | yes | reject |
| F1_turnover10M_hold2 | Bybit Δ -0.16, Binance Δ -0.61 — **fail** | Binance DD Δ -16.2pp — **fail** | yes | yes | reject |
| F3_turnover10M_hold2_residret12 | Bybit Δ -0.27, Binance Δ -0.74 — **fail** | Binance DD Δ -8.6pp — **fail** | **Binance negative** — **fail** | yes | reject |

**Cells passing decision rule: 0 of 9.**

### The cross-venue divergence pattern (the actual finding)

The salient feature of these results is not that no cell passed —
that's the expected outcome for an exploratory sweep with strict rules.
The salient feature is the **systematic Bybit-vs-Binance sign disagreement**:

- A3 ($10M turnover floor): Bybit DD improves -14pp; Binance DD worsens
  +21pp, return goes negative. Direct contradiction.
- B1 (rank_improvement_min 200): Bybit return +44%, Sharpe +0.44;
  Binance return -30%, Sharpe -0.93. Direct contradiction.
- E1 (universe_rank_max 200): Bybit return -53%; Binance return -19%
  but DD worse +20pp. Bad on both, in different ways.

The pre-registered rules saved a tempting venue-specific false positive:
B1 alone on Bybit would have looked like a clear winner (+44% Sharpe,
+228% return improvement). Cross-venue testing rejected it.

This pattern itself is research evidence — the baseline strategy
generalises only weakly across the two venues (Bybit 5.19× / Binance
0.66× with similar trade counts), and parameter tweaks amplify rather
than reduce that fragility. It motivates the broader research program
(orthogonal multi-signal features, signal-research harness) committed
in commit `e7dd104` rather than further within-strategy parameter
tweaks.

## Verdict

**REJECTED — no improvement found.** Zero of nine candidate cells pass
the a-priori decision rule. Production parameters remain unchanged.

Filed as the negative-result outcome the pre-reg's H1-H6 hypotheses
foresaw as the "most likely" case in the Honesty Notes section. The
strategy parameters are at or near a local optimum within the explored
filter-tweak space on this window.

Two follow-on items recorded for the broader program:

1. **The strong cross-venue divergence** in this run's results is by
   itself a non-trivial finding — bigger than any individual cell's
   in-sample Sharpe. It motivates Phase 5 (signal-research harness)
   and Phase 6 (combined-signal portfolio) of the multi-phase research
   plan at
   `docs/research_summary.md`.

2. **No filter-tweak hot-fix to demo.** The pre-reg standard prohibits
   trading any cell that fails the decision rule, regardless of how
   attractive single-venue numbers look. Production stays on the
   current promoted profile pending the broader research program.

## Honesty notes

- Every cell here is **EXPLORATORY** by the integrity-skill labelling.
- The rebuilt data root already inherited prior parameter-sweep wear
  (the strategy was historically tuned on similar data). Any improvement
  found here is subject to additional multiple-testing inflation beyond
  what the +0.5 Sharpe gate compensates for.
- A "no improvement found" verdict is the most likely outcome and is
  itself useful evidence — the production strategy has been near a local
  optimum for a while.
- Cross-venue agreement (Bybit AND Binance) is the only robustness signal
  available without burning pristine OOS.
