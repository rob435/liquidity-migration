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

Merge note (2026-07-20, Windows box): two parallel chains executed this
program on 2026-07-20 — the consolidation/offense chain (big PC/Mac,
pushed first: T-L v1, this doc's structure) and the chassis chain
(Windows box, pushed second: P0.1–P0.5, P1.1–P1.4, P2.1, then T-L v2 and
T-M). The statuses below merge both. T-L v2 was executed on the Windows
box before fetching the remote and independently replicated v1's
unconditional arms almost exactly (see the T-L cell); its early
"no v1 artifact exists" lineage note was written against unfetched local
history and is corrected in `reports/strategy-research-v3/t-l/2026-07-20/v2/`.

## How work is counted

A status cell flips only together with its artifact: a commit hash, a hashed
`reports/…` directory, or a receipt path written into the cell. A claim
without an artifact did not happen (lesson of 2026-07-20: a session claimed
a day's work and had produced zero commits — and the mirror lesson from the
same day: verify against `origin`, not only local history, before calling a
claim phantom). One completed unit = one local commit after
`scripts/dev.sh check` (on the Windows box: full ruff + the collectible
pytest set; the fcntl/geteuid-bound modules are a known platform gap);
push at natural checkpoints (CI-only; deploys are always a separate
operator action).

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
stability test (it is where T-L v1 broke). Calibration honesty from T-L v2:
a random same-size cell clears the raw +40 bp both-era bar ~26% of the
time — bar-clearing alone is weak; cross-checks (both entry axes,
threshold bands, cross-venue sign) decide.

## Track O — Offense (priority)

| ID | Thesis | Status |
| --- | --- | --- |
| T-L | Young-listing lifecycle (<240d population; untraded by the deployed universe gate) | **closed 2026-07-20 (v1 + v2), no Lane-2 candidate.** v1 (`reports/strategy-research-v3/t-l/2026-07-20/`): no calendar-only arm era-stable. v2 (`…/t-l/2026-07-20/v2/`, Windows chain, independent implementation): replicated v1's arms (short d1→d7 net +251/+269/−180 bp by era vs v1's +275/+273/−175); conditioned on pump, turnover decay, funding state, crowding, BTC trend — one Bybit survivor (turnover-collapse short, era-stable both entry days, threshold-band 0.2–0.4, permutation p=0.015) **failed the Binance same-design cross-venue pass** (negative all three eras at d2; `v2/binance/divergence_note.md`). The mandatory 1m execution-cost read is done (listing week 2.18× mature rel-range; 45 bp RT realistic at demo scale). Only the population-level 2024/2025 flip (funding turning against listing shorts) is cross-venue robust. Reopen only with a new conditioning mechanism or new data |
| T-M | Funding-extreme carry harvest (hedged carry, not momentum) | **closed 2026-07-20, below bar** — `reports/strategy-research-v3/t-m/2026-07-20/` (+ `binance/`). Episode inventory built (Bybit 84,761 / Binance 41,357 episodes; post-2025 negative-funding regime shift quantified: 28.3k/13.6k episodes ≤−0.15%/8h in e2526, concentrated in 30d+ symbols). 12 declared arms × 2 venues × hedged/unhedged, all reported: 0 era-stable hedged arms anywhere (episodes resolve in 2.5–16 h; funding cannot amortize 45 bp/leg; the paying crowd is directionally right at negative extremes); 2 Binance unhedged bar-clearers (n=28/n=3 pre-2025) fail the Bybit cross-check. Entries floored 2021-05-01 both venues to keep G1/G2 unread (the 2019-09→ inventory span deliberately narrowed; recorded). Residual value: the episode tape is R2 state-feature context |
| T-N | Cascade-riding long (C-H1/C-H2 estimands; shares P2.1 features) | todo — next Track-O item |
| T-O | Cross-venue listing lead-lag | blocked: needs incumbent-venue data (P3 acquisition) |
| T-P | Young-listing long continuation | dropped 2026-07-20 — naive d0→d2 negative/flat in every era (T-L card); v2's long mirror also noise post-2025 (s.e. ~400+ bp); revisit only conditioned |

## Chassis — P0 foundations

| ID | Task | Status |
| --- | --- | --- |
| P0.1 | 1m re-simulation harness (T-F exact-reproduction standard; no-lookahead property test; intrabar ambiguity policy, item 14; warm-state honesty, item 15) | done 2026-07-20 — `scripts/research_v3/resim_1m.py` + `tests/test_resim_1m.py` + `docs/resim_1m_harness_2026-07-20.md`. The `tick_ohlc_1m` pointer was stale (no such dataset); the harness runs on `bybit_render_1m` (2023-03-26→2026-07-09). Parity: 0 mismatches over 23,064 recorded trades (both T-A render arms + barebones); 16 surface-divergent listing/delisting paths enumerated & quarantined. Receipt: `reports/tail-risk-program/p01-resim-1m-2026-07-20/` |
| P0.2 | Untouched-slice provenance note (feature-touched vs outcome-unread ranges; frozen grading windows) | done 2026-07-20 — `docs/preregistration/untouched_slice_provenance_2026-07-20.md`. Binance 2020 was outcome-read by the dead momentum-factor gate (2026-05-24); Bybit slice outcome-unread but fully feature-touched; **Binance [2021-01-01, 2021-05-01) pristine**. Frozen windows G1/G2/G3 gate all later grading |
| P0.3 | Forward recorders (Bybit liquidations, L2 depth summaries) — implement + test only; install is an operator deploy | active — code staged 2026-07-20: `liquidity_migration/forward_recorders.py` + `tests/test_forward_recorders.py` (9 tests). **Not installed**; systemd unit + recorded change point remain the separate operator go |
| P0.4 | History backfill toward venue origin | **assigned to the big PC** (operator instruction 2026-07-20) — run the full window there (`BINANCE_START=2019-09-01`, default END); a narrow backward slice is REFUSED by the builder's staging-coverage protection. The Mac attempt was stopped and its staging sibling deleted; canonical root verified intact. Other boxes treat this as delegated: verify the coverage receipt when it lands, run nothing locally. Expected backward yield small — the Windows chain's frontier probe (`reports/tail-risk-program/p04-backfill-frontier-2026-07-20/`) found every dataset already at its upstream origin (Vision klines 2020-01 floor; funding at the 2019-09-10 first settlement; mark/index/premium at REST origins) |
| P0.5 | Re-anchor the pruned 2026-06-20 disaster-stop receipt from git `1fa7045` (labelled reconstruction) | done 2026-07-20 — `docs/disaster_stop_tail_reconstruction_2026-07-20.md` (verbatim recovery; indexed in `docs/preregistration/INDEX.md`) |

## Chassis — P1 first registrations

| ID | Task | Status |
| --- | --- | --- |
| P1.1 | R1 continuous risk intensity: Lane-1 paired renders → config commit + ledger row + kill criteria + daily forward shadow comparison | done 2026-07-20 — Lane-1 card `reports/tail-risk-program/p11-r1-intensity-lane1-2026-07-20/` (~3.8pp/yr net premium for 19–33% era-stable tail relief); Lane-2 `r1_intensity_v1` registered as shadow A/B with kill criteria R1-K1/K2/K3; daily scorer `scripts/research_v3/r1_forward_scorer.py`. No runtime change |
| P1.2 | R3a daily loss budget: Lane-1 insurance replay; shadow governor paper-first; frozen UTC-day-parity A/B; activation = operator decision | done 2026-07-20 — card `reports/tail-risk-program/p12-r3a-loss-budget-lane1-2026-07-20/`; design `docs/preregistration/r3a_loss_budget_experiment_2026-07-20.md`; shadow `liquidity_migration/loss_budget_shadow.py` + staged oneshot — **not installed** |
| P1.3 | R3b correlated-cluster caps: Lane-1 structure → cap design → config commit; hash-parity A/B | done 2026-07-20 — cards `reports/tail-risk-program/p13-r3b-cluster-caps-lane1-2026-07-20*/`; frozen cell ρ≥0.7/K=3 registered `docs/preregistration/r3b_cluster_cap_experiment_2026-07-20.md`; staged `liquidity_migration/cluster_cap.py`. Wiring = separate operator go |
| P1.4 | R3c protection-premium accounting line in the weekly kill-criteria path, with a test | done 2026-07-20 — `protection_premium` in `liquidity_migration/sleeve_kill_criteria.py` + `tests/test_protection_premium.py`; VPS picks it up at the next normal deploy |

## Chassis — P2 squeeze-state governor (holdout spend)

| ID | Task | Status |
| --- | --- | --- |
| P2.1 | Causal squeeze/crash features from fields unused by the 29 families; PIT audit per feature | done 2026-07-20 (build + audit) — `scripts/research_v3/r2_squeeze_features.py` + property tests; built over [2021-05-01, 2024-12-01): OI 4.53M rows/296 sym, premium 6.53M/497, funding 878k/497, breadth 31,440 book-hours, taker 31k symbol-hours (sparse; from 2023-04) — receipt `reports/tail-risk-program/p21-squeeze-features-2026-07-20/`. `positioning_lsr` DATA-GATED (absent from the root). T-M's episode tape (2026-07-20) adds funding-extreme state context. Exploration vs squeeze outcomes = P2.2 |
| P2.2 | Governor design + Lane-1 card (gross multiplier per side, hedge modulation, extreme-state veto; **no per-trade exit changes**) | todo |
| P2.3 | Config commit → **single registered holdout read** of the reserved V2 label tape `[2025-01-01, 2026-07-06)` → rolling forward. Metrics frozen at commit; opening recorded in INDEX + ledger | todo — the holdout stays closed until exactly this step |

## P3 — Extensions

| ID | Task | Status |
| --- | --- | --- |
| P3.1 | S1 cross-venue migration signals (Binance↔Bybit OI/LSR/taker lead-lag) | todo |
| P3.2 | R3d convexity overlay (options data root + carry model) | todo |
| P3.3 | D5 Hyperliquid acquisition (funding/OI/transparent liquidations; robustness + new-field source, not independence) | todo |
| P3.4 | S2 anti-book formalization beyond T-N | todo |

## Queued next actions (updated 2026-07-20, Windows chain merge)

1. **T-N cascade-riding long** — next Track-O offense item (shares P2.1
   features; starts from the frozen C-H1/C-H2 estimands and their frozen
   multiplicity rule).
2. **G1 one-time grade of the committed R1/R3 configs** (configs committed
   2026-07-20; G1 verified pristine). Needs its own registered unit: a
   Binance CONTINUOUS-shape render over G1 (`[2021-01-01, 2021-04-30)`
   entries) incl. an RMOM rebuild into a **separate labelled root — never
   mutate `binance_full_pit` in place**; opening recorded in
   `docs/preregistration/INDEX.md` before the first outcome read.
3. **R1 forward rows** via `scripts/research_v3/r1_forward_scorer.py` once
   the T-A render root refreshes past 2026-07-21; weekly R1-K1/K2/K3 check.
4. **P0.3 install / R3a / R3b activation** — operator decisions; shadow
   tooling staged.
5. **P0.4 coverage receipt** — lands from the big PC; verify only.

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
