# Continuous Live v2 Exit Redesign Pre-Registration

Date: 2026-06-18

Run label: exploratory until the full receipt, ledgers, and forward plan are
written. This can support a demo/paper wiring decision only after the registered
cells below are run and documented. It is not real-money evidence.

## Trigger

The 2026-06-18 exit-cause diagnostic in `docs/promoted_trading_logic.md`
showed that the current literal live continuous lifecycle fails because the
daemon `stop_approach` cover exits short fades into interim squeezes. The
strong historical comparison was the shared active book with fixed 24h hold and
10% TP, not the full live daemon lifecycle.

Primary observed damage:

- Fixed 24h/TP shared-max-new baseline: Bybit +70.12%, Binance +88.90%.
- `stop_approach` only: Bybit -11.38%, Binance -11.76%.
- Worst matched transition: `take_profit -> stop_approach` on both venues.
- `left_decile` only remained positive but cut the mean return to +26.63%.

## Hypothesis

The next live system should stop pretending the daemon loss-cut stack is
protective alpha. A live-v2 profile should align the daemon with the lifecycle
that actually survived the full-PIT replay:

- Keep the existing three-component entry book and component weights.
- Keep shared active-book capacity, `MAX_NEW_ENTRIES_PER_CYCLE=5`, and 10% venue
  TP.
- Restore the promoted-object `BTC_TREND_GATE=uptrend`; the `off` window was
  a temporary plumbing test and is not forward evidence.
- Disable `stop_approach`.
- Disable `left_decile` state exits.
- Disable `failed_fade`.
- Disable `breakeven`.
- Disable the 30-minute re-entry cooldown; with `left_decile` disabled, the
  specific cover/reopen boundary churn it protected against is removed.
- Initially test a wide server-side disaster stop at `STOP_LOSS_PCT=0.25` for
  catastrophe containment while the daemon itself follows TP or 24h max hold.
  Amendment A below rejected that stop for the demo/paper v2 candidate.

The adverse-exit breaker is not the primary failure. It may be retained only if
the registered replay shows acceptable cost after server-stop modeling.

## Registered Cells

All cells use full-PIT Bybit and Binance roots, funding/costs, the three
component ensemble after the TP14 drop, flat component-weighted live sizing,
shared max-active 25, shared max-new 5, 10% TP, and start
`2023-04-01` through the run date exclusive.

1. `prefreeze_gate_off`: retired literal live exit lifecycle with
   `BTC_TREND_GATE=off`. Purpose: reproduce the known failure.
2. `v2_gate_off_no_server_stop`: fixed 24h/TP lifecycle, no daemon exits, no
   server-stop simulation. Purpose: reconcile to the high-return baseline.
3. `v2_gate_off_server_stop`: same as cell 2 plus a 25% server disaster stop
   simulation. Purpose: measure the cost of keeping a real live catastrophe cap.
4. `v2_gate_off_server_stop_breaker`: same as cell 3 plus the existing
   adverse-exit entry breaker. Purpose: decide whether retaining the breaker is
   acceptable.
5. `v2_uptrend_server_stop_breaker`: same as cell 4 with
   `BTC_TREND_GATE=uptrend`. Purpose: test the actual proposed demo/paper live-v2
   candidate.
6. `v2_uptrend_server_stop_breaker_hedged`: cell 5 after the existing daily
   rebalance, BTC+ETH hedge, and BTC-vol regime overlay. Purpose: test the full
   proposed live-v2 portfolio path.

## Decision Rules

This is a repair/redesign decision, not a promotion gate.

- Stop immediately if cell 2 fails to reproduce the high-return baseline within
  tight numerical tolerance versus the 2026-06-18 exit-cause artifact.
- Reject live-v2 if cell 3 shows the server stop alone collapses both venues
  near the old live-exit failure.
- Retain the adverse-exit breaker only if cell 4 remains positive on both venues
  and does not create a new cross-venue cliff.
- Candidate live-v2 wiring requires cell 5 positive on both venues before hedge
  and cell 6 not materially worse than the known current live system.
- Any change beyond these cells needs a new preregistration amendment.

## Methodology Timestamps

- `decision_ts`: component signal bar close.
- `data_available_ts`: closed-bar continuous features plus causal residual
  momentum shift used by the current PIT scratch roots.
- `order_submit_ts`: entry bar close after the configured +1h confirmation.
- `fill_window`: historical hourly bar high/low/close model with explicit costs,
  funding, impact, and capacity limits.
- `exit_activation_ts`: TP/server-stop intrabar checks plus 24h max hold;
  registered pre-freeze cell also includes daemon state/protective exits.
- `state_initialization_ts`: 2023-04-01 with PIT listing age and warmup data.

## Expected Artifacts

- Dated runner under `scripts/`.
- Per-venue trades, MTM, rebalanced curves, candidate tapes, and summaries.
- `live_v2_redesign_table.csv` and pooled table.
- Receipt updates in `docs/promoted_trading_logic.md`,
  `docs/research_summary.md`, and `STATE.md`.

## Amendment A: Server Stop Falsifier Fired

Written before running additional no-server-stop cells.

The registered replay showed that the 25% server stop is itself a major
performance destroyer:

- `v2_gate_off_no_server_stop`: mean +79.51%.
- `v2_gate_off_server_stop`: mean -4.89%.
- `v2_uptrend_server_stop_breaker_hedged`: mean +6.81%.

The server stop exits into the same short-squeeze path as `stop_approach`, just
later. Keeping it yields a system that is merely less broken than the
pre-freeze live stack, not a rebuilt live system worth forwarding as alpha.

Additional registered cells:

7. `v2_uptrend_no_server_stop_no_breaker`: promoted BTC gate, no daemon exits,
   no server-stop simulation, no adverse-exit breaker. Purpose: test the clean
   TP/24h lifecycle under the actual gate.
8. `v2_uptrend_no_server_stop_breaker`: same as cell 7 plus the adverse-exit
   entry breaker. Purpose: decide whether the breaker is still useful once the
   damaging stop exits are gone.
9. `v2_uptrend_no_server_stop_breaker_hedged`: cell 8 after daily rebalance,
   BTC+ETH hedge, and BTC-vol regime overlay. Purpose: proposed demo/paper v2
   forward candidate if both venues remain positive.

Decision update:

- If the no-server-stop cells are the only robust positive results, live-v2 may
  be demo/paper only with `STOP_LOSS_PCT=0`. That must be documented as
  **not real-money safe**. A future real-money design would need a different
  risk control, not this no-stop demo/paper profile.

## Verdict Receipt

Artifact root:
`backtest-runs/continuous_live_v2_redesign_2026-06-18`.

| Rung | Bybit | Binance | Mean |
| --- | ---: | ---: | ---: |
| pre-freeze live stack, gate off | -1.11% | -1.76% | -1.43% |
| v2, gate off, no server stop | +70.12% | +88.90% | +79.51% |
| v2, gate off, 25% server stop | -2.14% | -7.64% | -4.89% |
| v2, uptrend, server stop + breaker + hedge | +9.22% | +4.39% | +6.81% |
| v2, uptrend, no server stop, breaker + hedge | +123.97% | +97.33% | +110.65% |

Verdict: wire `continuous_ensemble_v2` for demo/paper with `BTC_TREND_GATE=uptrend`,
no daemon exit stack, no re-entry cooldown, no server stop, adverse-exit breaker
retained, and existing rebalance/hedge/regime overlay retained. This is a
demo/paper repair only. It fails the real-money safety bar because no stop is
attached.
