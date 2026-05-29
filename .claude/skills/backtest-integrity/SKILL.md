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

- About to run or design a `volume-events` / `strategy-tribunal` / any backtest.
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
      (warm-start bug = error #15).
- [ ] Output has a trade ledger, equity curve, split metrics, drawdown,
      worst-day loss, config/param hash, data-root identity, research-log entry.
- [ ] Expensive grids checkpoint or stage — no multi-hour all-or-nothing runs.
- [ ] Any strange synchronization (e.g. mass same-minute exits) is stop-work
      until explained by code and market data.

## The 25 errors — fast scan

1 future universe selection · 2 future info in signals · 3 instantaneous
trading · 4 revised / non-PIT data · 5 ignored capacity · 6 trading fees ·
7 slippage · 8 market impact · 9 borrow availability · 10 borrow/funding fees ·
11 trading bans & venue restrictions · 12 instrument lifecycle (delist /
rename / prelist / tick & lot size) · 13 timestamp & resampling leakage ·
14 impossible OHLC intrabar path · 15 warm-started state · 16 same-code
illusion (backtest ≠ forward lifecycle) · 17 parameter mining · 18 OOS reuse ·
19 multiple-testing denial · 20 bad accounting · 21 hidden common risk (one
basket = one bet) · 22 venue mechanics fantasy · 23 pretty-report bias (no
artifacts = a screenshot) · 24 unreconciled live drift · 25 all-or-nothing
compute. Full detail: `docs/backtesting_errors_we_never_repeat.md`.

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

- Signal features use only data known at the **decision timestamp**. For the
  deployed daily signal (Architecture A) that is the daily signal close, and entry
  is delayed **+1h** to prevent same-bar leakage — the signal close is not an
  executable fill; this +1h guard for daily features is **non-negotiable**. For the
  continuous rolling-window signal under research (Architecture B / C-phases), the
  decision timestamp is the rolling bar-close and the entry delay may be 0 *only
  because the feature is already a causal trailing window* (research-gated, R12e/C-
  phase) — never relax the delay for the daily profile.
- `volume-events` requires full PIT by default; `--allow-partial-pit` is for
  explicitly biased diagnostics only, and that run must then be labelled biased.
- Funding is a known gap on roots without a funding dataset — mark such runs
  fee/slippage stressed but funding-missing.
- The fixed daily-close short-fade path is retired; do not revive it or cite
  its old profit-protection results as evidence for the event-driven system.
- Demo/forward execution is execution evidence only, never alpha proof.

The `liqmig-research` MCP server's `audit_run_artifacts` tool checks artifact
completeness against this standard — but artifact presence is necessary, not
sufficient. The PIT, causal, and OOS-hygiene gates still require judgement.
