# Research Findings

Updated 2026-05-21.

## Verdict

The liquidity-migration short signal is **statistically real but
regime-conditional**. It has genuine cross-sectional rank skill and survives a
full negative-control battery, but its profit is concentrated in crash regimes
and is flat-to-negative through alt-bull markets. It is **not deployable as a
standalone strategy** in its current form. The signal is real enough to be
worth refining — the open research direction is a regime-aware overlay.

## What was tested

- **V1** — the multi-gate `volume-events` strategy: absolute-threshold entry
  gates over the volume / liquidity-migration signal.
- **V3** — the `reversion_alpha` pivot: a continuous latent score plus a
  microstructure entry veto.
- **V1 + V3 hybrids** — entry-veto and continuous-score combinations of the two.

## Findings

### The signal is real

A `strategy-tribunal` run put the signal through block-bootstrap, random-sign,
and inverted-edge negative controls. It passed all of them — the
cross-sectional rank skill is not an artifact of noise, look-ahead, or
selection bias.

### "Zero trades out-of-sample" was an encoding artifact

V1's headline backtests used absolute-threshold entry gates. Out-of-sample, on
a smaller universe, those absolute thresholds are never cleared — so V1 took
zero trades, which initially read as "no edge out-of-sample." Relativizing the
gates (scaling thresholds by universe size) makes V1 trade out-of-sample and be
positive. The zero-trade result was a universe-fit artifact, not evidence
against the signal.

### The edge is regime-conditional

With relativized gates V1 trades and is positive out-of-sample — but the profit
is concentrated in crash regimes. Through alt-bull markets the strategy is
flat-to-negative. The `strategy-tribunal` failed V1 for standalone deployment:
only 2 of 3 pre-registered out-of-sample windows were positive, and funding
costs were not modeled.

### V3 and the hybrids do not survive out-of-sample

The V3 continuous-score / microstructure-veto pivot fails out-of-sample. The
V1+V3 hybrids — the "best of both worlds" idea — were tested and also fail
out-of-sample. The obvious combinations have been ruled out.

## Proven vs. hypothesized

**Proven:**

- The signal has real cross-sectional rank skill (negative controls passed).
- V1 with relativized gates trades and is positive out-of-sample.
- The profit is regime-conditional — concentrated in crash regimes.

**Not proven — open hypotheses:**

- That a regime / crash detector can gate the strategy to its profitable
  regimes well enough to make it deployable. This is the central refinement
  bet and is currently **untested**.
- That the edge survives realistic funding costs. The tribunal flagged funding
  as unmodeled; it must be costed before any promotion claim.

## Roadmap

1. **Regime overlay** — build and validate a regime / crash detector; gate V1
   (relativized gates) by it; measure out-of-sample whether the gated strategy
   clears promotion across *all* pre-registered windows.
2. **Funding-cost modeling** — add realistic funding costs to the backtest and
   re-measure the edge.
3. **Promotion re-test** — only after steps 1 and 2, re-run the
   `strategy-tribunal`.

No deployment or promotion claim is valid until the regime overlay is validated
out-of-sample and funding is costed. See
`docs/backtesting_errors_we_never_repeat.md` for the methodology standard that
governs every step above.
