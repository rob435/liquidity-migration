# Liquidity-Migration Short — Research Plan to Determine Whether the System Is Salvageable

*Self-contained brief. Paste into a fresh session. Goal: decide, with discipline,
whether the liquidity-migration short can be made into a real strategy — and if
so, how. This plan assumes the conclusion is NOT predetermined. It is built to
either save the system or kill it cleanly.*

---

## 0. Orientation (read first)

**The system.** A short-only crypto-perp strategy: fade alt coins that pump hard
on abnormal volume ("liquidity migration" events), betting on 1–5 day mean
reversion. Bybit demo only; the private client hard-refuses `demo=False`.

**Repo:** `~/Desktop/liquidity-migration`. Python + polars. `python -m pytest tests/`
must stay green (329 tests as of this plan).

**PIT data roots** (all point-in-time, built and audited):
- `~/SHARED_DATA/bybit_fullpit_1h` — Bybit IS, 2023‑05‑04 → 2026‑05‑18, 460 symbols.
- `~/SHARED_DATA/bybit_oos_pre2023` — Bybit OOS, 2021‑01 → 2023‑05, 216 symbols.
- `~/SHARED_DATA/binance_oos_pit` — Binance USD‑M OOS, 2020‑01 → 2023‑04, 198 symbols (incl. 25 delisted; built from data.binance.vision).

**Validation windows:**
- IS-train: Bybit 2023‑09‑01 → 2024‑09‑01
- IS-validation: Bybit 2024‑09‑01 → 2026‑05‑18
- OOS-1: Bybit 2022‑04‑01 → 2023‑05‑03
- OOS-2: Binance 2020‑09‑01 → 2023‑05‑01

**Key files:**
- `liquidity_migration/reversion_alpha.py` — clean 3-layer rebuilt harness (alpha / portfolio / execution). **Use this, not v1, for all new research.**
- `tests/test_liquidity_migration_reversion_alpha.py` — 15 simulator tests.
- `liquidity_migration/volume_events.py` — legacy v1 engine (the overfit one).
- `docs/v2_research_report.md`, `docs/reversion_alpha_report.md` — full findings.
- `docs/backtesting_errors_we_never_repeat.md` — the house standard. Obey it.

---

## 1. What was established (do NOT relitigate — build on it)

1. **v1 (the promoted +2022% strategy) is epoch-overfit.** Same code on two
   independent PIT OOS windows produced +5% / +13% (or 0 trades), not +2022%.
2. **v1's ~10 gates are a correlated cluster, not 10 signals.** Leave-one-out
   contributions sum to 1.67× the total return — the fingerprint of redundant
   knobs fitting the same in-sample bumps. No single gate is "the alpha."
3. **`close_location` is dead.** ~0 information coefficient in every window;
   confirmed three independent ways (ablation, LOO, IC). It is noise.
4. **The true edge is weak.** A fit-free cross-sectional rebuild measured the
   honest signal at **IC ≈ 0.05** for the best component (`rank_jump`), ≈0.03
   composite. `residual_return` ≈0.03, `turnover_ratio` ≈0.02.
5. **Cost ≈ edge.** ~0.3%/3‑day gross cross-sectional spread vs 28.8 bps
   round-trip cost. The edge barely clears costs → ~50% win rates.
6. **IS and OOS reward opposite filtering** (a Pareto frontier): event-quality
   filtering helps IS, hurts OOS; regime filtering helps OOS, hurts IS. The
   IS/OOS divide is an *epoch* difference (universe size, exchange, the
   2023‑26 memecoin era), not a tradeable regime signal.
7. **Both v1 and v2 fail adverse-fill stress** (`stop_fill_mode=bar_extreme`):
   v2 goes negative on Binance OOS; v1's drawdown blows past the −25% gate.
8. **The two pre-2023 windows are now lightly contaminated.** They were examined
   many times in research. Treat them henceforth as *validation*, not pristine
   OOS. **The only truly clean OOS left is forward time.**

The honest baseline: the idea has a real but small edge that does not, as
currently built, survive realistic costs. Everything below is about whether a
disciplined rebuild can change the cost/edge ratio enough to matter.

---

## 2. Methodology law (non-negotiable — this is the whole point)

1. **PIT always.** Every feature causal at decision time; every universe
   point-in-time. No current-`exchangeInfo` proxies. See `backtesting_errors`.
2. **IC before P&L.** A signal earns its place by a stable, significant
   information coefficient on **IS-train only**, measured *before* any backtest.
   P&L is the last check, not the first.
3. **Fit-free by default.** Equal-weight composites of standardized signals. If
   weights are fit, fit on IS-train only, report them, and expect OOS decay.
4. **Count degrees of freedom.** Every threshold is a knob. Track the running
   knob count. Target: knobs ≪ independent observations (≈3 regime cycles,
   ≈50–100 independent bets — not 1,100 days).
5. **Pre-register every test.** Before a backtest runs, write the hypothesis and
   the numeric pass/fail threshold. Record it. No post-hoc threshold moves.
6. **Validation/OOS is consumed on use.** You may look at IS-validation once per
   hypothesis and an OOS window once per *finalised* candidate. Re-run after a
   tweak = that window is burned for that line of work.
7. **Honest costs, always three.** Report every backtest at 28.8 bps (3× base),
   ~48 bps (5×), and adverse-fill (`bar_extreme`). A strategy that only works at
   clean 3× fills is not a strategy.
8. **Cross-sectional, never absolute.** Every signal is a same-day rank/z-score.
   No absolute thresholds (v1's `rank_imp≥150` broke on smaller universes).
9. **Kill willingly.** This plan has explicit kill criteria (§6). Reaching them
   and shelving the project is a successful outcome, not a failure.

---

## 3. The core problem, stated precisely

Net edge per trade ≈ (gross cross-sectional spread) − (round-trip cost).
Today: ≈ 0.30% − 0.29% ≈ break-even.

There are exactly four levers that change this, and the plan is one workstream
per lever, hardest-hitting first:

- **A. Cut the cost** (28.8 bps → ?). Highest leverage, most certain.
- **B. Grow the gross edge per trade** via a longer holding horizon.
- **C. Raise the win base-rate** via a hard regime gate.
- **D. Find higher-IC features** (open-ended; biggest potential upside).

---

## 4. Workstreams

### WS-0 — Reproduce and harden the harness  *(do before anything else)*
- Re-run `reversion_alpha.run_reversion_backtest` on all four windows; confirm
  the numbers in `docs/reversion_alpha_report.md` (≈ −28% IS-train, +115%
  IS-valid, +34% Bybit OOS, −77% Binance OOS). If they do not reproduce, stop
  and debug — nothing downstream is valid.
- Fix the IC diagnostic: the composite IC printed +0.176 on Binance, which is
  mathematically impossible for an equal-weight mean (≈4× its best component).
  It is a bug in the throwaway diagnostic, not the harness — but rebuild the IC
  tool properly (cross-sectional Spearman, per day, ≥10 names) and unit-test it
  before WS-1.
- **Gate:** harness reproduces; IC tool is tested. Else: halt.

### WS-1 — Execution-cost study  *(highest leverage)*
- **Hypothesis:** the strategy is break-even only because round-trip cost
  (28.8 bps) ≈ the gross edge. If realistically-achievable cost is materially
  lower (maker/passive fills on Bybit perps), the net edge roughly doubles and
  the strategy becomes viable.
- **Method:** (a) Characterise realisable cost on rank 31–150 Bybit USDT perps —
  spread, depth, realistic maker-fill probability for a patient limit order at
  the signal. Build a defensible cost figure with a stated confidence range.
  (b) Re-run the harness across a cost grid: 10 / 15 / 20 / 28.8 / 48 / 67 bps,
  all four windows. (c) Locate the break-even cost on each window.
- **Deliverable:** cost-vs-net-return curves; the break-even cost; a written,
  evidenced judgement on whether that cost is achievable in live execution.
- **Pre-registered pass:** break-even cost is comfortably above achievable cost
  on BOTH OOS windows.
- **Kills the lever:** if the strategy is OOS-negative even at 10 bps, cost is
  not the answer — proceed to WS-2/WS-4 with that known.

### WS-2 — IC-vs-horizon study
- **Hypothesis:** the 3-day hold is inherited arbitrarily from v1. A longer hold
  (5–10 d) gives a larger gross move per trade against the same fixed round-trip
  cost, improving the cost/edge ratio; reversion may simply decay slower than 3 d.
- **Method:** on IS-train, compute the IC of `reversion_score` and each component
  vs forward returns at 1, 2, 3, 5, 7, 10, 14 days. Identify the horizon that
  maximises IC and, separately, cost-adjusted IC. Backtest the best horizon on
  all four windows (one horizon, chosen on train, not swept on validation).
- **Deliverable:** IC-vs-horizon curve; recommended horizon; 4-window backtest.
- **Pre-registered pass:** the chosen horizon lifts cost-adjusted net edge and
  holds positive on both OOS windows.

### WS-3 — Hard regime gate in the rebuild
- **Hypothesis:** the rebuild's *continuous* regime scaler is too lenient — it
  still traded the 2021 alt-bull and took −77% on Binance OOS. v2's *hard*
  regime gate (trade only when 30d alt-median return ≤ −0.05) cleanly excluded
  that. Port the hard gate into `reversion_alpha`.
- **Method:** replace the continuous scaler with a hard gate (one knob, set a
  priori from the v2 work, NOT swept). Re-run four windows at the three cost
  levels.
- **Deliverable:** 4-window × 3-cost table with the hard gate.
- **Pre-registered pass:** positive on both OOS windows with no IS-train
  collapse; Binance OOS no longer catastrophic.

### WS-4 — Higher-IC feature search  *(biggest upside, highest overfit risk)*
- **Hypothesis:** the current four features cap at IC ≈ 0.05. The economic
  thesis — collecting a liquidity-provision premium from exhausted momentum
  flow — points to features not yet used. Candidate set, each with an a-priori
  economic rationale written down BEFORE any P&L:
  - **funding rate level & 1d/3d change** — funding flipping positive *is* the
    carry mechanism of the edge; arguably the most thesis-aligned missing feature.
  - **open-interest surge** — leverage build-up = fragility into the reversion.
  - **taker buy/sell imbalance** — identifies who the aggressor is.
  - **pump shape** — gap vs grind, overnight gap, intraday path of the pump.
  - **same-hour signal crowding** — how many names fired together (cross-section
    dispersion); v1's `union_pathology` touched this.
- **Method:** for each candidate, compute standalone IC on IS-train FIRST. Only
  features with a stable, significant train IC and a coherent economic story
  advance. Combine survivors equal-weight (or IC-weighted, fit on train only).
  Validate on IS-validation; touch each OOS window exactly once for the final
  composite.
- **Discipline note:** this is the workstream most able to overfit. Pre-register
  each feature. Do not let a feature in on P&L alone. Do not keep swapping the
  feature set and re-scoring on validation — that is the exact error from the
  v1 era.
- **Deliverable:** ranked IC table of candidates; survivor composite; 4-window ×
  3-cost backtest.
- **Pre-registered pass:** composite IC materially above 0.05 (target ≥ 0.08)
  and stable in sign and rough magnitude across all windows.

### WS-5 — Capacity & portfolio bounds  *(run in parallel)*
- Square-root market-impact model on rank 31–150 perp turnover. Find the AUM at
  which slippage erodes the WS-1 net edge. Suspected ceiling ≈ $1–3M.
- **Deliverable:** a capacity ceiling number. This bounds everything — a $1M
  ceiling means this is a small book, not a fund, and that must be stated up front.

### WS-6 — Forward test  *(the only real OOS)*
- Whatever survives WS-1→4 is registered as a shadow challenger in the Bybit
  demo champion/challenger stack alongside v1 and v2. Run 60–90 days.
- **Critical measurement:** live fills vs modeled fills. This is the real test
  of the WS-1 cost assumption and the adverse-fill risk. If live fills are
  materially worse than model, the backtest is void.

---

## 5. Sequencing

1. **WS-0** (reproduce + fix IC tool) — gate everything on this.
2. **WS-1** (cost) and **WS-2** (horizon) — do together; they jointly determine
   whether the cost/edge ratio can be fixed with structure alone.
3. Decision point: if WS-1+WS-2 cannot get OOS net edge clearly positive at
   achievable cost, the only remaining hope is WS-4.
4. **WS-3** (hard regime gate) — cheap, do it alongside WS-1/2.
5. **WS-4** (features) — the upside swing; run after 1–3 so you know how big a
   lift is actually needed.
6. **WS-5** capacity in parallel throughout.
7. **WS-6** forward test — only for a candidate that has cleared 1–4.

---

## 6. Definition of viable / kill criteria

**Viable (proceed to forward test, then real money at 30–40% sizing):**
- Positive net return on BOTH OOS windows AND IS-validation,
- at a cost no better than realistically-achievable execution,
- still positive (even if reduced) under adverse-fill stress,
- composite alpha IC stable and ≥ 0.08 across windows,
- knob count low and every knob pre-registered.

**Kill / shelve (a clean, honest outcome):**
- If after WS-1 + WS-2 + WS-4 the cost-adjusted net edge is still ≤ 0 on either
  OOS window, conclude the idea is not a standalone strategy. Options then:
  run it only as a tiny diversifying overlay, or shelve it. Do not deploy it,
  and do not resume gate-tuning to manufacture a number.

---

## 7. Traps to refuse

- **Do not chase the +2022%.** It is an overfit artifact. The honest forward
  expectation is the OOS / IC level, not the IS level.
- **Do not re-tune v1's gates.** That entire surface is burned. New work uses
  `reversion_alpha.py`.
- **Do not iterate signal subsets on the validation/OOS windows.** Picking the
  best of many variants across those windows is overfitting by hand.
- **Do not trust an in-sample Sharpe.** It was maximised by the fitting; it
  cannot defend the fitting.
- **Do not skip the cost question.** A strategy that needs clean 3× fills to
  work does not work.

---

*Bottom line for the new session: the job is not to rescue a number. It is to
determine, with the discipline above, whether a real but weak edge (IC ≈ 0.05)
can be made net-positive after honest costs — primarily via execution cost
(WS-1), holding horizon (WS-2), and better features (WS-4). If it can, forward-
test it small. If it cannot, say so plainly and stop.*
