# Research plan: continuous-system live-readiness program (2026-06-09)

**Mandate:** operator granted full authority over the continuous system ("make it
live-money ready"). **Unchanged guardrails:** REAL_MONEY stays false; Tier-3 is strict
and is decided ONLY by ≥30d forward demo/paper evidence (STATE.md); both venues; no
commit/push without operator confirmation; pre-register every run touching the
full-PIT roots; `--end` cap 2026-05-27.

"Live-money ready" therefore decomposes into: (a) close every correctness debt,
(b) harden the inference against the search that produced the system, (c) make the
deployable object executable (hedged book, live planner semantics), (d) start the
forward Tier-3 clock on the RIGHT object. In-sample work can reach "paper_ready
candidate, forward clock running" — never further.

**The deployable object (current):** winner_base uptrend ensemble
`{turn3p3:.30, turn4p3:.20, turn4p5:.40, age210tp14:.10}` @ w90/ddh-0.04 rebalance,
max4–6 anchor, + the banked causal BTC-beta hedge leg
(`ContinuousHedgeRule(90,60,2.0,5bps)`). Receipts: continuous-hedge-{overlay,engine},
continuous-winner-robustness, continuous-demote-downtrend-extension (all 2026-06-09).

## Work items (priority order)

- **R0 — funding-interval debt audit. DONE 2026-06-09: CLOSED.** Accrual verified
  end-to-end vs raw datasets (40/40 trades to 5e-20); binance partial trades are 5
  coverage-edge cases worth ~0. Receipt: `continuous-funding-debt-closure-2026-06-09.md`.
- **R1 — walk-forward causal allocator falsifier (the overfitting attack).** The
  winner's weights/params were chosen on the full window (plateau-robust, but chosen).
  Falsifier: a CAUSAL allocator that at each rebalance point re-derives ensemble
  weights (and optionally max_scale) from trailing data only, evaluated strictly
  out-of-window, both venues. If causal-OOS ≈ fixed-winner performance, the
  weight-selection-overfit concern is dead; if causal-OOS is materially worse, the
  honest deployable is the causal allocator's numbers, and forward expectations get
  reset to them. Either way the live system gains a defensible weight policy
  (frozen-from-receipt vs causal re-estimation). Pre-register before the run.
- **R2 — hedge-leg executor plumbing.** `plan_continuous_hedge_resizes` (live planner
  for the BTC long leg: target H from causal beta state × current scale × equity,
  venue qty filters deferred to executor) + tests; mirrors
  `plan_continuous_rebalance_resizes` so the demo daemon can adopt it. No demo orders
  without operator say-so.
- **R3 — forward Tier-3 clock on the right object.** Audit what the continuous
  paper/no-order evidence collector currently tracks (`continuous-forward-readiness`
  CLI); wire/spec the winner+hedge object as the tracked configuration so forward
  evidence accrues for the thing we would deploy. Demo orders remain OFF.
- **R4 — impact calibration.** Reconcile modeled entry-impact (impact_exponent 0.5,
  fixed bps) against OBSERVED daily-demo fills (shared execution path) at deployed
  sizes; restate winner+hedge numbers under calibrated impact if it differs materially.
- **R5 — capacity statement.** Per-symbol participation at target deploy size ×10
  (Tier-3 requires capacity ≥10x deployment); document the binding names.

## Status log

- 2026-06-09: R0 done (closed). R1 next.
- 2026-06-09: R1 done — **weight-overfit concern DEAD** (pre-registered): causal-chooser
  haircut 13.8% (≤15% bar); equal-weight MATCHES the winner OOS (pooled 2.334 vs 2.317)
  so weights are not load-bearing; adaptive re-weighting actively hurts → live policy =
  frozen receipt weights, no re-estimation. Receipt:
  `continuous-walkforward-allocator-2026-06-09.md`. R2 next.
- 2026-06-09: R2 functions DONE — `ContinuousHedgeState` + `compute_continuous_hedge_ratio`
  (live twin of the engine's hedge sizing) + `plan_continuous_hedge_resize` (long-leg
  Buy/Sell-reduce-only planner, floors/guards) in `continuous_rebalance.py`; 11/11 tests
  incl. a backtest<->live PARITY test (planner reproduces the engine's hedge_ratio
  exactly, every day). Demo-daemon adoption of these functions folded into R3 (one
  wiring change set). R3 next: audit what `continuous_demo_daemon` / the no-order paper
  collector tracks vs the winner+hedge object; spec/wire the Tier-3 clock.
- 2026-06-09: R3 audit DONE — the live continuous stack tracks the OLD decile state
  machine (orders off, bybit-only); NO forward stream tracks the banked winner+hedge
  object. Design chosen: Road B no-order SIGNAL-REPLAY collector (same research-engine
  code path, frozen config-hash-pinned, overlap-drift alarm, both venues) before any
  live executor build; receipt
  `docs/preregistration/continuous-forward-clock-spec-2026-06-09.md`. Next: build
  `continuous_forward_replay.py` + tests; root-freshness (ingestion) is the operator
  dependency for starting the clock.
- 2026-06-09: R3 collector BUILT — `liquidity_migration/continuous_forward_replay.py`
  (frozen config + sha256 pin, full-history rebuild + overlap-drift alarm, idempotent
  append, Tier-3-facing readiness summary) + 5 tests; canonical
  `combine_continuous_components` moved into the package (scout delegates). 246
  continuous tests green. Remaining for the clock to tick: refresh data roots, then a
  scheduled `update_forward_ledger` run per venue (component-extension runs the
  research engine on the trailing window) — operator decision on where it runs
  (this box cron vs VPS deploy). R4 (impact calibration) next.
- 2026-06-09: R4 BLOCKED on operator (no rsync/demo ledgers on this box; unblock =
  `bash scripts/reconcile.sh` pull or hand over the order/fill datasets); impact model
  stays "stress-tested, uncalibrated". R5 DONE — capacity statement (pre-stated p95<=5%
  hourly-participation bar at scale 4): bybit ~$0.43-0.6M capacity, binance ~$1.7-2.4M
  -> combined Tier-3-safe deployment ~$200-300k; the system is a SMALL-BOOK strategy at
  current breadth (small-name tail binds); turnover-capped sizing is the documented
  (unrun) capacity lever. R3 collector SEEDED + verified on real data (rebuilds Stage-B
  exactly; idempotent; clock at 0 days awaiting fresh roots). Receipt:
  `continuous-capacity-impact-2026-06-09.md`. **Program state: everything autonomous is
  done; all remaining items need operator decisions (data refresh, ledger pull,
  commit/push, demo).**

## R2-LIVE build spec (2026-06-10, operator-directed): ensemble + hedge + sniper demo executor

Operator decisions now in force: sniper = Tier-2 demo candidate (Amendment 6); VPS is
continuous-only; wire tonight's findings into live demo. Integration map (assembled
2026-06-10, do not re-discover):

1. **Hedge manager** (`liquidity_migration/continuous_hedge_manager.py`, daily systemd
   timer): own ledger root `data/bybit-continuous-hedge-event`, datasets named
   `continuous_fade_demo_{trades,orders}` -> registered with the risk service via
   `continuous_addon_data_root` (ws_risk.py:187; adoption namespace `lm-en-ca-*`,
   `adopt_continuous_addon_strategy_id` set). Trade rows mirror the schema written at
   continuous_demo.py:~2356 (cycle_trade_rows). Orders: `BybitPrivateClient.place_order`
   (REST; demo has no WS-trade), link ids via `_continuous_order_link_id` with the
   `lm-en-ca` prefix. Sizing: beta from `compute_continuous_hedge_ratio` (frozen
   ContinuousHedgeRule(90,60,2.0); parity-tested) on per-unit book returns; live series
   seeded from the winner_base research ledger (ship
   `continuous_forward_state_2026-06-09/{venue}/forward_ledger.csv` unit returns + BTC
   daily series as the warm-start artifact, clearly labeled), appended with live days
   from the primary continuous ledger; H_equity_frac = clip(-beta,0,2) x
   (live_gross_short_frac / 0.5) [the backtest book is 0.5-gross at scale 1].
2. **Ensemble entries** in the demo daemon: the signal pipeline already computes the
   shared decile panel (composite max_ret168, rmom q25 gate, btc uptrend gate, age,
   liq); ADD trigger predicates (turnover_spike_168h >= {3,4} AND ret1 >= {3%,5%} —
   continuous_events.py:319-323) + fixed exits (TP10/TP14 via exchange-native TP, 24h
   max-hold) as a new exit_mode alongside the state machine; 4 components = weight
   fractions {.30,.20,.40,.10} of book notional, crowd cap 2 per component;
   `compute_continuous_rebalance_scale` (live function, exists) for w90/max4 daily
   scale with prior_raw_returns seeded from the component research ledgers.
3. **Sniper add-on**: on each new entry, place a resting limit Sell at entry x 1.08
   for 0.25 x entry notional (lm-en-ca-snipe link id); cancel at trade exit; tranche
   exits with the trade (Amendment-1 exit design; own-TP optional later).
4. **Order-slot swap**: v2 ensemble executor takes CONTINUOUS_SLEEVE order submission;
   the decile daemon drops to paper (single-submitter rule, runbook). Keep both paper
   shadows running.
5. Tests: trigger predicate parity vs continuous_events exprs; hedge state round-trip;
   ledger-row schema accepted by ws_risk reconstruction; link-id round-trips.

Until the v2 executor ships, what is LIVE tonight: decile demo (orders, fills ->
Amendment-5 calibration), paper shadow, risk service, rmom refresh, forward report.
