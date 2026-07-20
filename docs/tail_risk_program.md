# Tail-Risk Program — active execution state

**Main research focus by operator instruction, 2026-07-20.** Rationale and
receipts: `docs/tail_risk_overhaul_proposal_2026-07-20.md`. Evidence policy:
`docs/governance.md`. Selection accounting: `docs/hypothesis_ledger.md`.

This file is the one mutable "what to do next" surface for the program. Update
the status tables in place as work completes (with dates); evidence notes go to
`docs/research_summary.md`, provenance receipts to `docs/preregistration/`,
operational facts to `STATE.md`. Everything here is research + demo/paper
lane; real money remains a separate, unopened door.

## Closed lines — do not restart

Per-trade price-exit and stop research on the deployed sleeves is **closed on
the spent surface under both grading styles** (alpha metrics and tail
metrics). This includes fixed/wide/server stops, trailing and breakeven
stops, MFE give-back ladders, funding drain-exits, and signal-invalidation
exits. Receipts: `docs/research_summary.md` (mechanism table, T-F/T-B),
`backtest-runs/continuous_exit_cause_ablation_2026-06-18/`, and the
2026-06-20 disaster-stop study (commit `1fa7045`). The TP12/24h CONTINUOUS
shape and LONG 1.5-ATR/4-ATR/3d shape are frozen. A revisit requires a new
mechanism, new data, or new economics (e.g., a confirmed passive-execution
cost regime), registered prospectively — not a new grid on 1h bars.

## Status legend

`todo` → `active` → `done <date>` (or `blocked: <reason>`, `dropped: <reason>`).
One owner rule: whoever flips a row to `active` finishes or reverts it.

## P0 — Foundations (no evidence risk; start immediately)

| ID | Task | Definition of done | Status |
| --- | --- | --- | --- |
| P0.1 | **1m re-simulation harness** on the already-local Bybit `tick_ohlc_1m` (2023-03→2026-05), extended by the `bybit_render_1m` fetch when it lands | Harness reproduces every recorded exit of the canonical CONTINUOUS ledger exactly (T-F standard) before any variant is expressible; focused tests; short doc note of scope/limits | todo |
| P0.2 | **Untouched-slice verification** for Binance `[2020-01-01, 2021-05-01)` and Bybit `[2021-01-01, 2021-05-01)` | Provenance note in `docs/preregistration/` stating the exact *outcome-unread* boundary. Honest subtlety: V2 features had trailing lookbacks (168h, 90d, RMOM warm-up) that read spring-2021 bars as inputs, so the clean boundary is earlier than 2021-05; the task computes it from the actual feature specs, states feature-touched vs outcome-unread ranges, and freezes the grading window | todo |
| P0.3 | **Forward recorders** for Bybit liquidation stream + live-L2 depth summaries | Recorder units deployed through the normal flow with a recorded change point (additive telemetry; no sizing/decision path). Fields land in a research-readable root with coverage receipts | todo |
| P0.4 | **History backfill** — extend `binance_full_pit` toward venue origin via `scripts/build_full_pit_binance.sh` (earlier `BINANCE_START`), respecting upstream availability | Extended root + coverage/manifest receipt; no interpretation, acquisition only | todo |
| P0.5 | **Re-anchor the pruned 2026-06-20 disaster-stop receipt** from git history (commit `1fa7045`; the receipt and the local `backtest-runs/` artifacts were both pruned — nothing remains on this host) | Reconstruction doc (labelled as such) so the sizing-is-the-disaster-control claim has a citable anchor | todo |

## P1 — First registrations (commit = registration; forward clocks start)

| ID | Task | Definition of done | Status |
| --- | --- | --- | --- |
| P1.1 | **R1 — continuous risk intensity.** One monotone gross multiplier replacing the binary BTC gate + discrete 0.35× overlay (T-I linear member revived under §Grading metrics) | Config + scoring recipe committed; hypothesis-ledger row (descends from T-I, sixth-generation prior stated); kill criteria registered before first forward day | todo |
| P1.2 | **R3a — book-level daily loss budget.** Realized-day-loss trigger, entry-side only, daily reset; X from kill-criteria arithmetic (≈ −1.5%) | Lane-1 replay of trigger behavior on seen data (trigger correctness/false-trip rate, not return); config commit; paper-first implementation like the passive-exec pattern | todo |
| P1.3 | **R3b — correlated-cluster caps** (the 2026-06-20 study's own unbuilt recommendation) | Lane-1 estimate of cluster structure on seen data; cap design + config commit; deploy-when-ready with recorded change point | todo |
| P1.4 | **R3c — protection-layer accounting.** Native-stop realized cost as an explicit insurance-premium line in the forward record | Cost line present in the weekly kill-criteria/forward reporting path | todo |

## P2 — R2 squeeze-state governor (flagship; the holdout spend)

| ID | Task | Definition of done | Status |
| --- | --- | --- | --- |
| P2.1 | Causal squeeze/crash index features from fields unused by the 29 prior families: OI level/acceleration, positioning LSR, taker-flow imbalance, premium spikes, melt-up/crash breadth (+ forward liquidations from P0.3 as they accrue) | Feature build with PIT audit; Lane-1 exploration on the spent window only | todo |
| P2.2 | Governor design: gross multiplier per side + hedge-intensity modulation + extreme-state entry veto; **no per-trade exit changes** | Design note + Lane-1 evidence card (all cells, era-split, §Grading metrics) | todo |
| P2.3 | Config commit, then **single registered holdout read** on `[2025-01-01, 2026-07-06)` | Metrics frozen at commit; holdout opening recorded in `docs/preregistration/INDEX.md` + hypothesis ledger (non-descended family justification stated); one scripted read, no iteration; then rolling forward | todo |

## P3 — Extensions (behind P1/P2)

| ID | Task | Status |
| --- | --- | --- |
| P3.1 | S1 cross-venue migration signals (Binance↔Bybit OI/LSR/taker lead-lag as de-risk trigger and entry context) | todo |
| P3.2 | R3d convexity overlay (options data root + carry model; premium budgeted next to gross) | todo |
| P3.3 | D5 Hyperliquid acquisition (funding/OI/transparent liquidations, 2023→; robustness + new-field source, not independence) | todo |
| P3.4 | S2 anti-book (long-cascade sleeve; starts from frozen C-H1/C-H2 estimands with their frozen multiplicity rule) | todo |

## Grading standards (operative for every arm above)

- **Metrics, frozen at each config commit:** ES95/ES99 of daily book P&L, max
  drawdown, common-loss-tail-day count (definition frozen in the config),
  net including costs and funding. Era-split, all grid cells, forgone upside
  next to avoided cost. **MAR is banned at negative net.**
- **Insurance layers (R3) are additionally graded as insurance:** trigger
  correctness, false-trip rate, realized premium vs budget — not return
  improvement (taxonomy item 27).
- **Ordering discipline:** commit configs *before* opening any new surface
  (P0.2 slices, P0.4 backfill, P2.3 holdout). A surface opened first is
  Lane-1 only.
- **Effective N:** unique decisions → simultaneous waves → 28-day blocks;
  component rows are never independent (taxonomy item 29).
- **Every commit records its hypothesis-ledger descent** and family count;
  new-surface reads are recorded in `docs/preregistration/INDEX.md` when
  opened.
- **Kill criteria** per promoted arm in the `sleeve_kill_criteria` pattern,
  registered before the first forward day, checked by the weekly
  `scripts/ops.sh kill-criteria` cadence.

## Interaction with other active workstreams

- **Breadth redesign** (`docs/breadth_redesign_2026-07-20.md`): statistical
  power remains the binding constraint on grading everything here, but the
  T-K funnel replay (2026-07-20) measured per-bet vol ≈ 1,000 bps and
  ρ̂ ≈ 0.21 — admission knobs alone cut days-to-significance by only ~9%
  and cannot deliver the original power-table promise. The operative power
  levers are the ones this program owns: decorrelated sources (S2), per-bet
  vol shape, and cost reduction (passive exec). Breadth stays a supporting
  workstream, coordinated through the hypothesis ledger, not a substitute
  for them.
- **Passive-execution A/B** (`docs/preregistration/passive_execution_experiment_2026-07-20.md`):
  arm-B's rolling record re-prices the cost hurdle; a confirmed ≥10 bps/side
  improvement is the "new economics" that can reopen specific closed families
  prospectively.
- **Sleeve kill criteria** (`docs/preregistration/sleeve_kill_criteria_2026-07-20.md`):
  unchanged; they carry to successor configs unamended unless explicitly
  replaced before a first forward day.
- **Runtime boundary:** nothing in this program changes demo/paper behavior
  without a five-line promotion note and a recorded change point; recorders
  (P0.3) and paper-first implementations follow the normal deploy flow.
