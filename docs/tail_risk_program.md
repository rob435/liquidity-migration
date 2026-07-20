# Research Program — single source of execution state

**Main research focus by operator instruction, 2026-07-20**, redirected the
same day to offense-first: new-edge theses large enough to change the
strategy and be verifiable. This file is the ONE mutable "what to do next"
surface. The canonical mission prompt for an execution session is
`docs/next_agent_prompt.md` — hand that file to any new agent verbatim.

Provenance receipts (immutable, keep): rationale
`docs/tail_risk_overhaul_proposal_2026-07-20.md`; offensive-slate
registration `docs/preregistration/DRAFT_strategy_research_v7_2026-07-20.md`;
evidence policy `docs/governance.md`; selection accounting
`docs/hypothesis_ledger.md`.

## How work is counted

A status cell flips only together with its artifact: a commit hash, a hashed
`reports/…` directory, or a receipt path written into the cell. A claim
without an artifact did not happen (lesson of 2026-07-20: a session claimed
a day's work and had produced zero commits). One completed unit = one local
commit after `scripts/dev.sh check`; push at natural checkpoints (CI-only;
deploys are always a separate operator action).

## Closed lines — do not restart

Per-trade price-exit and stop research on the deployed sleeves is closed
under both alpha and tail grading (fixed/wide/server stops, trailing and
breakeven stops, MFE give-back ladders, funding drain-exits,
signal-invalidation exits). Receipts: `docs/research_summary.md` mechanism
table, `backtest-runs/continuous_exit_cause_ablation_2026-06-18/`, the
2026-06-20 disaster-stop study (git `1fa7045`). TP12/24h and LONG
1.5-ATR/4-ATR/3d shapes are frozen. Reopening requires new mechanism, new
data, or new economics (e.g. a confirmed passive-execution cost regime),
registered prospectively.

## Admission bar (operative for every thesis)

A thesis advances to a Lane-2 config commit only on era-stable Lane-1
evidence of **net ≥ +40 bp/trade** after the frozen 45 bp round trip and
funding, or **≥ 5 independent bets/day** at deployable gross (from T-K's
measured power arithmetic: per-bet vol ≈ 1,000 bps, ρ̂ ≈ 0.21 — smaller
edges are unverifiable here, not merely unprofitable). Below bar → drop and
record in the hypothesis ledger. The 2024/2025 era boundary is the primary
stability test (it is where T-L v1 broke).

## Track O — Offense (priority)

| ID | Thesis | Status |
| --- | --- | --- |
| T-L | Young-listing lifecycle (<240d population; untraded by the deployed universe gate) | **v2 next (top priority)** — v1 closed 2026-07-20: no calendar-only arm is era-stable (2021-24 bleed +270–310 bp net inverts to −175…−830 bp in 2025-26; d0 chase negative). Panel with turnover/funding paths built. v2 = condition on pump size, turnover-decay slope, funding state, wave crowding, BTC trend; plus a listing-week execution-cost read from `tick_ohlc_1m`. Card: `reports/strategy-research-v3/t-l/2026-07-20/` |
| T-M | Funding-extreme carry harvest (hedged carry, not momentum) | todo — episode inventory + carry-capture P&L from local funding roots (Bybit 2021→, Binance 2019-09→) |
| T-N | Cascade-riding long (C-H1/C-H2 estimands; shares P2.1 features) | todo (after T-L v2 / T-M) |
| T-O | Cross-venue listing lead-lag | blocked: needs incumbent-venue data (P3 acquisition) |
| T-P | Young-listing long continuation | dropped 2026-07-20 — naive d0→d2 negative/flat in every era (T-L card); revisit only conditioned |

## Chassis — P0 foundations

| ID | Task | Status |
| --- | --- | --- |
| P0.1 | 1m re-simulation harness on local `tick_ohlc_1m` (T-F exact-reproduction standard; no-lookahead property test; intrabar ambiguity policy, item 14; warm-state honesty, item 15) | todo |
| P0.2 | Untouched-slice provenance note: derive every trailing lookback from code; state feature-touched vs outcome-unread ranges for Binance `[2020-01-01, 2021-05-01)` / Bybit `[2021-01-01, 2021-05-01)`; freeze the grading window in `docs/preregistration/` | todo |
| P0.3 | Forward recorders (Bybit liquidations, L2 depth summaries) — implement + test only; install is an operator deploy | todo |
| P0.4 | History backfill toward venue origin | active 2026-07-20 — **narrow backward slice is REFUSED by the builder's staging-coverage check** (verified: `[2019-09-01, 2020-01-01)` refused, root unharmed, 812 persisted symbols protected). Correct invocation = full window (`BINANCE_START=2019-09-01`, default END); relaunched detached same day. Acceptance: coverage receipt, acquisition only. Expected backward yield is small (2019 archives are mostly 404) |
| P0.5 | Re-anchor the pruned 2026-06-20 disaster-stop receipt from git `1fa7045` (labelled reconstruction) | todo |

## Chassis — P1 first registrations

| ID | Task | Status |
| --- | --- | --- |
| P1.1 | R1 continuous risk intensity: Lane-1 paired renders (binary gate vs monotone multiplier, T-I ancestor) under program metrics → config commit + ledger row + kill criteria + daily forward shadow comparison | todo |
| P1.2 | R3a daily loss budget: Lane-1 trigger replay graded as insurance (trigger correctness, false-trip rate, premium); shadow governor paper-first; frozen UTC-day-parity A/B design; activation = operator decision | todo |
| P1.3 | R3b correlated-cluster caps (the 2026-06-20 study's own unbuilt recommendation): Lane-1 cluster structure → cap design → config commit; per-entry hash-parity A/B in the passive-exec pattern | todo |
| P1.4 | R3c protection-premium accounting line in the weekly kill-criteria path, with a test | todo |

## Chassis — P2 squeeze-state governor (holdout spend)

| ID | Task | Status |
| --- | --- | --- |
| P2.1 | Causal squeeze/crash features from fields unused by the 29 families (OI accel, LSR, taker imbalance, premium spikes, breadth; forward liquidations as P0.3 accrues); PIT audit per feature | todo |
| P2.2 | Governor design + Lane-1 card (gross multiplier per side, hedge modulation, extreme-state veto; **no per-trade exit changes**) | todo |
| P2.3 | Config commit → **single registered holdout read** of the reserved V2 label tape `[2025-01-01, 2026-07-06)` → rolling forward. Metrics frozen at commit; opening recorded in INDEX + ledger | todo — the holdout stays closed until exactly this step |

## P3 — Extensions

| ID | Task | Status |
| --- | --- | --- |
| P3.1 | S1 cross-venue migration signals (Binance↔Bybit OI/LSR/taker lead-lag) | todo |
| P3.2 | R3d convexity overlay (options data root + carry model) | todo |
| P3.3 | D5 Hyperliquid acquisition (funding/OI/transparent liquidations; robustness + new-field source, not independence) | todo |
| P3.4 | S2 anti-book formalization beyond T-N | todo |

## Grading standards (every arm, frozen at each config commit)

ES95/ES99 of daily book P&L, max drawdown, common-loss-tail-day count, net
including costs and funding; era-split; all grid cells; forgone upside next
to avoided cost. **MAR banned at negative net.** Insurance layers (R3)
additionally graded on trigger correctness, false-trip rate, premium vs
budget (item 27). Effective N = unique decisions → waves → 28-day blocks
(item 29). Missing data excluded and counted, never zero-filled (item 30).
Commit configs before opening any new surface; openings recorded in
`docs/preregistration/INDEX.md`. Kill criteria per promoted arm in the
`sleeve_kill_criteria` pattern before its first forward day.

## Interactions with other active workstreams

- **Breadth** (`docs/breadth_redesign_2026-07-20.md`): T-K measured per-bet
  vol ≈ 1,000 bps and ρ̂ ≈ 0.21 — admission knobs alone cut
  days-to-significance ~9% and cannot deliver the original promise. The
  operative power levers are decorrelated sources (Track O), per-bet vol
  shape, and cost reduction (passive exec).
- **Passive-execution A/B**
  (`docs/preregistration/passive_execution_experiment_2026-07-20.md`): its
  rolling record re-prices the 45 bp hurdle; a confirmed ≥10 bps/side
  improvement is "new economics" that can reopen specific closed families
  prospectively.
- **Sleeve kill criteria**
  (`docs/preregistration/sleeve_kill_criteria_2026-07-20.md`): unchanged;
  carry to successor configs unless explicitly replaced before a first
  forward day.
- **Runtime boundary**: nothing here changes demo/paper behavior without a
  five-line promotion note and recorded change point; real money is a
  separate, unopened door.
