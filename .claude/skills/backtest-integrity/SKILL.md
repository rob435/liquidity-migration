---
name: backtest-integrity
description: "Mandatory methodology gate for any backtest, research run, strategy or feature change, or result interpretation in this Bybit quant repo. Use before running a backtest, when designing a research run, when reviewing results, and before calling a result alpha, edge, candidate, or promotion evidence. Enforces the non-negotiable gates and run labels from docs/backtesting_errors_we_never_repeat.md."
---

# Backtest integrity gate

`docs/backtesting_errors_we_never_repeat.md` is a **mandatory standard** for
every serious research run in this repo. A backtest is evidence only when
every decision is reconstructable from data, state, and venue rules that
existed at decision time, and every fill is executable under an explicit cost
and execution model. Anything else is a bug report, not alpha.

## Use this skill when

- About to run or design a `volume-events` / any backtest.
- Interpreting a research report or judging whether a result is trustworthy.
- Tempted to call a result "edge", "candidate", or "promotion evidence".
- Changing strategy gates, features, data ingestion, or fill/cost logic.

## Non-negotiable gates — if any fails, the run is `invalid` or `exploratory`

- [ ] Declares `decision_ts`, `data_available_ts`, `order_submit_ts`,
      `fill_window`, `exit_activation_ts`, `state_initialization_ts`.
- [ ] Every feature is causal at `decision_ts`. Test a latency-delayed copy
      when data availability is uncertain.
- [ ] Universe is full point-in-time, including delisted / renamed / migrated /
      prelisted symbols. `current_universe_biased` is throwaway scouting only —
      never merged into a report or comparison. A live `exchangeInfo` is NOT a
      PIT source.
- [ ] Cost model includes venue fees, aggressive/passive mix, funding/carry,
      spread/slippage, and capacity limits.
- [ ] Every adaptive exit, trailing stop, basket stop, cooldown, and kill
      state starts from the state a live executor would have at activation
      (warm-start bug — the warm-started-state error in the standard).
- [ ] Output has a trade ledger, equity curve, split metrics, drawdown,
      worst-day loss, config/param hash, data-root identity, research-log entry.
- [ ] Expensive grids checkpoint or stage — no multi-hour all-or-nothing runs.
- [ ] Any strange synchronization (e.g. mass same-minute exits) is stop-work
      until explained by code and market data.

## The error taxonomy — fast scan

The recurring failure modes by theme: **look-ahead** (future universe / future
info / non-PIT or revised data / timestamp & resampling leakage / impossible
intrabar path), **cost & capacity** (fees / slippage / market impact / borrow &
funding / capacity), **venue reality** (trading bans / instrument lifecycle /
venue mechanics), **state & lifecycle** (warm-started state / backtest ≠ forward
lifecycle), and **inference** (parameter mining / OOS reuse / multiple-testing /
bad accounting / hidden common risk / pretty-report bias / unreconciled live
drift / all-or-nothing compute). The canonical, **numbered** list is the single
source of truth — `docs/backtesting_errors_we_never_repeat.md`. Read it; do not
reproduce the count or numbering here.

## Run labels — always attach exactly one

- `invalid` — known bug, leakage, impossible fill, missing cost, broken
  accounting.
- `exploratory` — useful sketch, missing one or more proof gates.
- `biased_benchmark` — intentionally biased benchmark kept for comparison,
  never for promotion.
- `candidate` — point-in-time, costed, split-stable, ledger-backed, and NOT
  tuned on the promotion window.
- `paper_ready` — `candidate` plus a demo/paper plan matching the backtest
  lifecycle.

Default to the *lowest* label the evidence supports.

## Before trusting any backtest — answer all 8

1. What exact data existed at `decision_ts`?
2. Which assets were tradable then, and how do we know?
3. What order would have been submitted, when, and at what size?
4. What fill model was used, and how was it costed?
5. What state existed before each exit condition became active?
6. What would make this result disappear?
7. Which untouched window or forward evidence is still clean?
8. Where is the trade ledger and the run record?

If any answer is weak, the backtest is not ready to influence real-money work.

## Repo specifics

- **Progressive system, not a frozen baseline.** These gates are about *methodology and
  evidence* (causality, PIT, costs, OOS, run labels) — NOT about reproducing a prior
  run's output byte-for-byte. A performance/refactor change is held to **numerical
  equivalence** (`np.allclose`, NaN positions matching), not bit-identical output;
  last-bit float-order differences are not an integrity violation. The strict bars that
  remain are the real-money promotion gate and the correctness gates below (look-ahead,
  survivorship, accounting). Do not invoke "preserve the exact old numbers" to block an
  improvement.
- Signal features use only data known at the **decision timestamp**. For the
  deployed daily signal that is the daily signal close, and entry is delayed **+1h**
  to prevent same-bar leakage — the signal close is not an executable fill; this +1h
  guard for daily features is **non-negotiable**. For the continuous rolling-window
  signal under research, the decision timestamp is the rolling bar-close and the entry
  delay may be 0 *only because the feature is already a causal trailing window*
  (research-gated) — never relax the delay for the daily profile.
- Run the `volume-events` backtest via `scripts/volume_events_cell.sh` (it fills
  the ~30 baseline flags) — do not hand-assemble them. The Tier-2 verdict comes
  from `scripts/r1_robustness.py`; the legacy strict Sharpe bar from
  `scripts/apply_decision_rule.py`. `volume-events` requires full PIT by default;
  `--allow-partial-pit` is for explicitly biased diagnostics only, labelled biased.
- Funding is a known gap on roots without a funding dataset — mark such runs
  fee/slippage stressed but funding-missing.
- Legacy fixed-day rebalance-grid benchmarks are retired — do not cite their
  results as evidence for the event-driven system. (The fade-confirmation
  *execution* is the live strategy, a different thing — see STATE.md.)
- Demo/forward execution is execution evidence only, never alpha proof.

The `liqmig-research` MCP server's `audit_run_artifacts` tool checks artifact
completeness against this standard — but artifact presence is necessary, not
sufficient. The PIT, causal, and OOS-hygiene gates still require judgement.
