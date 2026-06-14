# Stage 8 - Regime Response (beat the binary BTC-uptrend gate)

## Question

Can a **materially different** regime-response mechanism beat the current binary
BTC-30d-uptrend entry gate on pooled MAR across both venues?

The live gate trades iff BTC is above its 30-day SMA (`trend > 0`) and otherwise
sits flat. The open research question is not "should we remove it" — it is
"is there a smarter regime response that earns more risk-adjusted return than
this hard on/off switch."

## Objective discipline (binding)

- The success metric is **pooled MAR vs the V0 binary gate on both venues**,
  net of funding and costs. Never trade count, never "uptime," never "always on."
- A mechanism that keeps the book engaged in more regimes is an *allowed
  outcome only if it improves MAR*. If it trades more and earns less
  risk-adjusted return, it loses.

## What E2 already closed (do not repeat)

Receipt: `docs/preregistration/2026-06-12-e2-regime-response-family.md`. NULL.

- V1 (trade iff `0 < trend <= +20%`, cap euphoria): pooled MAR delta **-1.96**.
- V2 (`>+20%` off; `0..+20%` full size; `<=0` quarter-size top-quintile only):
  pooled MAR delta **-2.52**.
- Both lost ~20pp return and roughly halved MAR on both venues. The naive
  exploratory bucket finding (euphoria mean negative) reversed once selection,
  funding (a fade book collects funding in euphoria), and exit/rebalance
  mechanics were modeled through the real engine.

Hard rule: **no bounded-threshold variant of the V1/V2 family may be re-run.**
A regime arm in this stage is admissible only if it is mechanistically distinct
from "turn the gate off / cap a trend band / quarter-size the tail."

## Admissible directions (each needs its own predeclared form)

These are *directions*, not pre-approved arms. Each arm must be fully specified
with thresholds locked before the run, both venues, funding ON.

- `R1_continuous_regime_size`: replace the binary gate with a continuous,
  causal regime-score → size map (monotone, locked shape), so the book scales
  down rather than switching fully off. Must beat V0, not just V1/V2.
- `R2_multifactor_regime`: regime defined by more than BTC SMA sign — e.g.
  BTC realized-vol state, cross-sectional breadth, funding regime, or
  dispersion — combined by a predeclared rule. Each added factor needs a
  same-sign two-venue rationale.
- `R3_regime_conditioned_selection`: keep the gate, but in otherwise-blocked
  regimes admit only entries that clear a *higher* causal composite / path-shape
  bar (from Stage 1 / Stage 7), at reduced size. Distinct from V2 because the
  admission rule is the proven score, not a fixed top-quintile.
- `R4_regime_conditioned_hedge`: hold the entry gate fixed but let the BTC+ETH
  hedge intensity respond to regime, so down/euphoria risk is managed by the
  hedge rather than by gating trades. Judged on MAR of the hedged book.
- `R5_negative_control_regime`: regime label drawn from a hash/calendar bucket
  with no market content.

## Required artifacts

- causal regime-feature tape with `data_available_ts` for every regime input
  (no using a daily regime value before its bar closed — the rmom latency
  lesson applies);
- full-engine per-venue ledgers and monthly returns (R1-compatible);
- funding ON (bybit modeled / binance partial, disclosed), plus a 2x-cost arm;
- episode counts per regime bucket reported as fragility, never used to rescue;
- effect sizes, chronological-third stability, negative-control results.

## Pass Bar

A regime arm advances only if, vs the V0 binary gate:

- positive total return both venues;
- pooled MAR delta `> +0.1`;
- no venue MAR delta `< -0.5`;
- drawdown / worst-day does not worsen beyond preregistered tolerance;
- survives the 2x-cost arm;
- the regime negative control (`R5`) is weaker;
- the result is not carried by one venue, one month, or one regime bucket.

## Falsifier

Reject if the mechanism only "works" by trading more in regimes that the engine
shows are net-negative once funding/exits are modeled (the E2 failure mode), if
it needs a threshold moved after seeing output, or if it collapses to a
relabeled V1/V2.

## Forward gate

A historical winner here can nominate a demo/paper shadow only. Whether the
smarter regime response is real is decided forward, by the Stage 6 forward gate
and the Tier-3 bar in `STATE.md`. The binary uptrend gate remains the live
control until a forward verdict says otherwise.
