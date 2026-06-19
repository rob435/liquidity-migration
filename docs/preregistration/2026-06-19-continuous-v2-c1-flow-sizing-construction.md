# Continuous V2 C1 Idiosyncratic-Flow Sizing Construction (Pre-Registration)

Date: 2026-06-19

Parent plan: `docs/preregistration/2026-06-18-continuous-v2-ab-research-plan.md`
Amendment authorizing the branch:
`docs/preregistration/2026-06-19-continuous-v2-ab-amendment-binance-only-flow.md`
Prior step (gating): `docs/preregistration/2026-06-19-continuous-v2-c-flow-overlay-verdict.md`

Scope: `claimed_venue_scope=binance_only_flow_exploratory`. Run label `exploratory`;
**no Tier-2 candidate pass possible**. No real-money claim.

## Why this arm, and why now

The C0 screen showed flow is a real **trade-level** signal, strongest on
`idiosyncratic_flow` (within-symbol rank-IC vs short `net_return` = **−0.103**, above
the null-max), and the C2/C3 hedge-overlay arms confirmed flow is a poor **hedge-timing**
signal (closed negative). The remaining harvest mode is trade-level entry sizing.

Direction matters. The candidate-track B1P path-shape sizing arm sized **up** the
highest-IC names and **lost** MAR by concentrating tails (W5 lesson reproduced).
`idiosyncratic_flow` has a **negative** IC, so the conviction direction here is the
**de-risk** direction: **size DOWN** names with high idiosyncratic taker buying
(continuation risk) and size UP low-flow names. This is the opposite of B1P's
tail-concentrating size-up, so it is a genuinely different test, not a restack.

We deliberately do **not** use `flow_resid_return` (it failed the C0 null, +0.007
within-symbol): the residualize-against-run-up translation removes the signal. We use
raw `idiosyncratic_flow` (symbol taker imbalance minus market flow), which is value-built
and fully covered on Binance.

## Construction (fixed before running)

- Arm `C1_FLOW_RESID_FEATURE_SIZING_BINANCE_ONLY`: entries unchanged; causal per-trade
  `size_mult_lookup = clip(1 + sign*0.25*z, 0.5, 2.0)` with **sign = −1**, where `z` is
  the per-symbol expanding-prior z-score (strictly prior rows, min 10 obs) of
  `idiosyncratic_flow`. Strictly causal (no rescale); book gross is enforced by the
  frozen daily vol-target rebalance, so this is a relative within-book reweighting.
- Hash control `C1H_FLOW_RESID_SIZING_HASH_CONTROL_BINANCE_ONLY`: same multiplier
  multiset, permuted across (symbol, signal_ts) by hash.
- Venue set: `binance` only. Feature tape:
  `backtest-runs/continuous_v2_feature_almanac_2026-06-19_cflow` (idiosyncratic_flow
  coverage 1.0). Components are re-run with the lookup; full PIT, costed.

## Falsifiers (any one closes idiosyncratic-flow sizing)

- C1H hash control matches or beats C1 (sizing is noise).
- Binance MAR delta ≤ 0, or drawdown worsens faster than return improves.
- The result is carried by one month or one liquidity bucket.

## Evidence limit

Binance-only exploratory. A null-beating, stable Binance harvest would justify building a
resumable **Bybit full-market taker-flow archive** to test the two-venue bar — nothing
more. A negative result **closes the entire C-flow branch** (overlay already closed; the
trade-level harvest mode would then also be exhausted).
