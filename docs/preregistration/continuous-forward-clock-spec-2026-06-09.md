# R3 audit + design: the forward Tier-3 clock for the banked continuous object

**Date:** 2026-06-09. **Type:** infrastructure audit + design decision (no strategy run).
**Program:** `docs/research_plan_continuous_live_readiness_2026-06-09.md (deleted in the doc consolidation; recover via git history)` R3.

## Audit: what tracks forward evidence today

- The continuous live stack (`continuous_demo.py` / `continuous_demo_daemon.py`, units
  `liquidity-migration-bybit-continuous-{demo,paper}.service`, datasets
  `continuous_fade_{demo,paper}_{trades,orders,cycles}`) implements the OLD sub-hourly
  **decile state machine** (D9-membership entries with confirm-delay, leave-decile
  hysteresis exits, defaults rmom 0.33 / age_min 30 / btc gate off / 48h cap). Per the
  runbook (2026-06-08): NOT promoted, demo orders OFF, only a no-order evidence
  collector may run. Bybit-only.
- `continuous-forward-readiness` (reconciliation.py) scores that stream's paper
  evidence. `continuous_addon_shadow.py` audits a primary-vs-addon pair of paper streams.

**Addendum 2026-06-10:** this audit snapshot was superseded by the 2026-06-09
operator re-shape. The rebuilt VPS now runs continuous demo orders plus a paper
shadow; the live base book is `continuous_rebalance_v1` (single-component
turn4_pop4 rebalance book), not the old decile machine audited above. Continuous
remains research-stage and not promoted; demo fills are execution evidence only.

## The gap

The BANKED candidate is a different object: 4 trigger-based components
(`turn3_pop3`, `turn4_pop3`, `turn4_pop5`, `age210_tp14`: turnover-trigger entries,
TP10/TP14, fixed 24h holds, age/crowd/rmom-0.25/btc-uptrend gates) combined at FROZEN
weights `{.30,.20,.40,.10}` through the `w90/tv0.045/max4/ddh-0.04` rebalance, plus the
BTC hedge leg (`ContinuousHedgeRule(90,60,2.0,5bps)`). **No forward stream tracks this
object.** Tier-3's 30-day clock is therefore not running for the thing we would deploy.

**Addendum 2026-06-10:** this gap is partially closed. The Road-B replay
collector is built, tested, and seeded on real data with the clock at 0 days
pending a fresh data-root refresh. The live demo book tracks a single-component
turn4_pop4 rebalance object with real orders; full four-component ensemble wiring
and sniper/hedge submission remain separate live-readiness work.

## Design decision: Road B (signal-replay collector) before Road A (live ensemble executor)

- **Road A** — implement the 4 components as a live ticker-driven executor (new
  entry/exit state machines, multi-component bookkeeping, binance leg, hedge orders).
  Heavy build; only needed when ORDER-level execution evidence is wanted (operator
  decision; demo orders are off anyway).
- **Road B (chosen)** — a **no-order signal-replay paper collector**: a scheduled job
  that re-runs the SAME research engine (`run_continuous_event_research` for the 4
  component configs + `_combine_components` + `apply_rebalance_rule` with the frozen
  weights and the hedge inputs) over a trailing window of freshly-ingested data, and
  appends the out-of-sample daily rows (yesterday's ledger day) to a forward ledger.
  Properties: exact same-code path as every banked receipt (strongest signal-level
  same-code guarantee); zero order risk; STATE-compliant ("no-order paper evidence
  collector"); both venues (data permitting). Execution realism stays modeled — the
  strict Tier-3 execution-reconciliation items still require demo orders later (Road A
  or the existing stream re-enabled, operator-gated).
- Keep the existing decile paper stream untouched (infrastructure value), but its
  evidence is NOT the banked object's evidence.

## Build plan (Road B)

1. `liquidity_migration/continuous_forward_replay.py`: `run_continuous_forward_replay(
   research_root, state_dir, end_day)` — runs the 4 component configs on a trailing
   window (engine warm-up ~120d + margin), combines + rebalances + hedges with the
   FROZEN parameters, verifies the overlap days against the previously-stored forward
   ledger (drift check = same-code regression alarm), appends new days, emits a
   readiness summary (days accrued, forward MAR/Sharpe both venues vs the Tier-3 bars).
2. Tests: frozen-config hash pinning (replay must refuse to run if the stored config
   hash differs), overlap-drift detection, append idempotency.
3. Scheduling: on this box (research root must be refreshed — currently stale at
   2026-05-26/27) or on the VPS next to the ingesting roots. Deployment = operator
   decision (push). Until ingestion freshness is solved, the collector can backfill
   2026-05-27..today only after a root refresh.

## Honest framing

Road B accrues SIGNAL forward evidence (modeled fills/costs) — it can satisfy the
30-day forward-MAR clock for Tier-2→Tier-3 *consideration*, but the Tier-3 execution
legs (daily demo reconciliation, capacity/stress on real fills) still need order-level
evidence later. This is the correct sequencing: prove the signal forward first, spend
order-risk second.

## Status

Audit complete. Build items 1+2 were completed on 2026-06-09:
`liquidity_migration/continuous_forward_replay.py` plus tests, seeded on real data
(clock 0 days, awaiting data-root refresh; see
`continuous-capacity-impact-2026-06-09.md`).

## Addendum (2026-06-12, round-4 audit — API correction)

The build-plan name `run_continuous_forward_replay(research_root, state_dir,
end_day)` was never implemented as a single function. The shipped API in
`liquidity_migration/continuous_forward_replay.py` is:
`init_or_check_state(state_dir)` → `build_full_ledger(pieces, hedge_returns,
hedge_funding)` → `update_forward_ledger(state_dir, venue, full_ledger)` →
`forward_readiness_summary(state_dir, venue, forward_start_ms=...)`. The
orchestration layer that produces `pieces` (re-running the 4 frozen component
configs through `run_continuous_event_research` over a refreshed research root)
is NOT yet built — the consolidated research scripts that did this were removed
2026-06-02 (git history is the backstop). Until that runner exists, the forward
clock does not tick automatically; building it is an explicit work item, not a
scheduling detail.
