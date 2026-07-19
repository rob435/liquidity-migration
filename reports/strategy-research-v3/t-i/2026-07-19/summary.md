# T-I — Regime intensity vs the binary BTC gate (exploratory, Lane 1)

**Status: EXPLORATORY.** Ledger-level counterfactual on the spent V2 discovery
surface. No alpha, robustness, candidate, or promotion claim. Family exactly
as declared (three members); the advance rule was frozen in code before any
member's numbers were inspected, and it is applied as registered.

## What ran

- Regime value: the deployed gate's own definition — `_btc_trend_returns`
  (prior-30-day BTC daily return-sum, current day excluded), evaluated at each
  trade's signal day; BTCUSDT closes read from the full-PIT root so no ledger
  trade lacks a value (0 missing). Missing trend fails closed (weight 0) in
  every member, matching deployed behavior.
- Members as per-trade weight scaling on the ungated barebones book:
  binary_gate (weight 1 iff trend > 0), linear (clip(trend/10%, 0, 1)),
  two_sided (1.0 ≥ +10%, 0.25 ≤ −10%, 0.5 otherwise; band declared at the
  family's single 10% anchor).
- Tail arm identical to T-A: the 156 V2 common-loss dates + 2024-08-06.
- Declared advance rule: full-window MAR (net/|maxDD|) above BOTH the ungated
  baseline and the binary gate, with no more negative tail days than the gate.

## Results (full window; baseline −20.23% net, −38.7% DD)

| Member | Net | MaxDD | MAR | Tail-neg days | Tail sum | Worst tail day | 2024-08-06 |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline (ungated) | −20.23% | −38.7% | −0.522 | 156/156 | −51.8% | −3.14% | −3.14% |
| binary_gate | −13.48% | −35.8% | **−0.376** | 82 | −27.6% | −3.09% | −3.09% |
| linear | −13.50% | **−30.8%** | −0.438 | **80** | **−18.9%** | **−1.35%** | **−1.13%** |
| two_sided | −20.40% | −32.8% | −0.622 | 155 | −29.4% | −1.57% | −1.57% |

**Outcome under the registered rule: no member advances; no paired renders.**
linear fails the MAR leg; two_sided fails everything.

**The honest wrinkle, reported as found:** the linear member *Pareto-dominates
the binary gate on every risk dimension* — equal net, 5.0pp less maxDD,
fewer negative tail days, a tail sum two-thirds smaller, and a worst tail day
less than half as deep — yet loses the MAR comparison, because MAR on
negative-net books rewards the LARGER drawdown (equal negative net divided by
a bigger |DD| is closer to zero). The registered rule is applied as written;
per the change discipline, revisions are prospective only. Any future
regime-intensity registration should use a decision metric that is not
ill-posed at negative net (e.g., dominance on {net, maxDD, tail} or MAR gated
on positive net). That metric lesson — not a rule — is T-I's main product.

Secondary findings:

- linear is a strict down-weighting of the gated book (weights ≤ binary's
  everywhere), so it cannot and does not "reclaim the early era" the draft
  hoped for (early net +0.35% vs the gate's +1.57%); what it actually does is
  shave DD and tail at zero net cost — the weak-uptrend entries it trims were
  net-flat but drawdown-bearing.
- two_sided is decisively refuted: keeping 0.25–0.5 weight in downtrends
  preserves essentially the whole tail (155 of 156 tail days negative) while
  earning nothing — worst member of the family.
- The gate's ledger-level value confirms T-A from the other side:
  +6.74pp net vs ungated with the entire tail-day count halved.

## Limitations

- Spent discovery surface; ledger-level weight scaling only — no capacity,
  admission, or hedge interaction; render-level behavior of intensity members
  was not measured (no member reached the render stage).
- MAR comparisons at negative net are ill-posed (see above); the registered
  rule inherited this from the draft's wording.
- Era split for members is curve-based at 2023-02-22; tail dates are V2
  common-loss dates, a spent-surface definition.

## Next action

No prototype and no renders. If regime intensity is pursued, it needs a fresh
registration with a dominance-based decision metric and render-level arms —
citing this closure.

Artifacts: `ti_grid.csv`, `ti_tail.csv`, manifest with member definitions,
tail definition, and the registered advance rule with its outcome.
