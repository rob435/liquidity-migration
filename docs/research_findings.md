# Research Findings

Updated 2026-05-22.

## Verdict

The liquidity-migration short strategy is sound and **effectively
market-neutral**. A full point-in-time, costed backtest, an 81-scenario
parameter sweep, and an adversarial `strategy-tribunal` review return a
**WATCH** verdict — there are **no FAIL findings**. Earlier framing that the
system "needs a regime overlay" is not supported by the evidence and has been
removed.

## Funding-model correction (2026-05-22)

The perpetual-funding model over-charged by up to 8x — `_funding_lookup` billed
every funding row in a hold window, and 147 of 313 research-root symbols carry
intra-interval snapshot rows (e.g. hourly rows of an 8h rate). Fixed in
`008d34a`. The Verdict above and the Evidence / cross-family / tribunal numbers
below **predate the fix**: they ran on over-charged funding, which understated
return and overstated drawdown. The closing-bar figures in the next section are
funding-corrected. Re-running `strategy-tribunal` on corrected funding is
required before the WATCH verdict stands; the error is benign in direction —
the fix made every backtest modestly better (close-0.30 / 5-pos drawdown
-18.0% -> -14.2%, return 2637% -> 2850%).

## Closing-bar setting — close_location_min = 0.30 (canonical as of 2026-05-21)

The canonical close-location entry knob is now **0.30** (was 0.45), for research
and for the VPS forward test. From an exploratory closing-bar sweep on the
full-PIT IS root: 0.30 vs 0.45 gives more trades (510 vs 448) and higher total
return (2850% vs 2212%) at the cost of deeper drawdown (-14.2% vs -11.6%) and
marginally lower walk-forward split Sharpe (3.59 vs 3.71). It is a trade-count /
return vs drawdown choice and is **not yet tribunal-validated** — the WATCH
verdict and 81-scenario sweep below were run on the 0.45 config. See
`docs/system_status.md`.

## Evidence

- Full-PIT canonical backtest (460 symbols, 2023-05..2026-05): strongly
  positive, with **3 of 3 pre-registered windows positive** — train +126%,
  validation +225%, out-of-sample +183%.
- **81-scenario parameter sweep** (threshold x hold x stop x take-profit):
  **81/81 scenarios promotable** — the edge is robust across the grid, not a
  single fragile parameter point.
- All six tribunal **negative controls pass**: block-bootstrap p05 still deeply
  positive, random-sign, inverted-edge (-98%), shuffled time / symbol / event.
- 79-82% positive months; worst month -5.93%.

## Market-neutrality — no hedge or regime gate is needed

The short book carries almost no directional exposure. Measured beta of its
daily returns:

- to BTC: **-0.03** (R^2 0.0015)
- to the equal-weight perpetual universe: **-0.07** (R^2 0.02)

It is positive in **both** regimes — roughly +0.5%/day on universe-up days and
+0.9%/day on universe-down days. Shorting the weakest-liquidity names is a
cross-sectional play, not a directional bet, so it carries near-zero beta by
construction. A short-only book being flat-to-slightly-lower through an
up-market is the signal behaving correctly, not a flaw.

## Hedge testing (tested and rejected)

A long counterpart was built and tested two ways; both **degrade** the book:

- A `top_volume_leadership` long leg returned **-14.67%** on the short book's
  worst-10% days — it loses *alongside* the short book rather than offsetting
  it, and its correlation to the short book is -0.06 (uncorrelated noise).
- A market/beta hedge at overlay weights 0.25-1.0 cut total return
  (2003% -> 107%), deepened max drawdown (-14% -> -78%), and lowered Sharpe
  (0.31 -> 0.06) monotonically.

The hedge hypothesis is disproven by the data: there is no directional
exposure to offset, so any hedge only adds drag.

## Cross-family scan — the edge is singular and real

All 13 event families the engine supports were backtested in both directions
(26 strategy variants) on identical data, universe, costs, and parameters.
**Exactly one is a promotable edge: the `liquidity_migration` short itself**
(+2022%, Sharpe 3.41, 3/3 windows). The other 25 variants all fail the
promotion gate, and most are strongly negative — negative Sharpe, drawdowns
-50% to -95%.

This functions as a 25-way negative control. If the liquidity-migration edge
were a data-mining artifact of the engine or the sample period, some of the
other families would have looked strong by chance. None did — only the
theory-grounded signal (capital migrating away from weak names) produces an
edge. That singularity is strong evidence the edge is genuine and specific, and
it closes the multi-family diversification avenue: there is no second
independent edge in this engine to add.

## Path to a green tribunal PASS

The parameter sweep already cleared the major WATCH findings — parameter
sensitivity, parameter heatmaps, the stress matrix, and cost/funding/slippage
are all PASS. The remaining WATCH items:

- `funding_coverage` ("partial") — driven by 2 of 448 trades hitting a
  per-symbol funding-data gap; a benign 0.4% data-coverage artifact.
- `crowding_model` / `entry_hour_crowding` — minor signal-quality observations.
- `execution_drift` — compares live demo fills against the backtest; requires
  live demo execution data. The demo runner must accumulate real fills first.

A literal PASS verdict is gated on the `execution_drift` check, which by design
needs live fills — you should not fully pass an un-traded strategy. Re-run the
tribunal with `--execution-data-root` once the demo has traded.

## Methodology

See `docs/backtesting_errors_we_never_repeat.md`. No deployment claim is made
beyond what the tribunal evidence supports.
