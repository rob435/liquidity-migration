# Research plan: regime-robust / market-neutral continuous short (2026-06-09)

**Status:** proposed program. Research-only, demo/paper only. Nothing here is promoted or
real-money. In-sample work never promotes past Tier-2; forward demo/paper is the only Tier-3
arbiter (STATE.md). Pre-register every run that touches the full-PIT roots (AGENTS.md), both
venues, cost-stressed. Do not commit/push without operator confirmation (pre-push gate =
ruff + pytest).

Read first: `STATE.md`, `docs/research_summary.md`, `docs/backtesting_errors_we_never_repeat.md`,
and the memories `continuous-refinement-campaign-2026-06-09`, `continuous-winner-robustness-2026-06-09`.

---

## 1. Diagnosis (what the 2026-06-09 session established — do NOT re-derive)

The continuous short's **entry-alpha is tapped out**; its binding constraint is **uncompensated
alt-season beta** (it bleeds when alts pump, i.e. BTC-dominance falls / alts outperform BTC).

- The uptrend ensemble winner PASSED a 5/5 in-sample falsification battery — it is a robust
  *plateau*, NOT weight-overfit, but there is no headroom left in re-weighting. `tv`
  (target_daily_vol) is a **dead knob** (daily scale is pinned at the `max_scale` cap;
  `max_scale` is the real leverage lever).
- 8 refinement levers tested, all tapped out or non-robust: parsimony (DoF-only), carry/funding
  (rank-IC +0.04/+0.06), multi-horizon (bybit-only; 24h is the cross-venue-robust horizon),
  conviction-by-score (weak, bybit-tilted), entry circuit-breaker (null on the component pool),
  rmom-gate loosening (0.25 already optimal; looser blows out DD because the extra breadth is
  CORRELATED, not independent).
- **The "current best" downtrend extension is FRAGILE/overfit:** the `premium_24h_ge0` filter
  leaves only 85 trades on ~10 active days (bybit) / 91 on ~9 days (binance) over 3 years,
  ~0% standalone return, and `dt_scale=0.4` sits right at a cliff (push it: binance collapses,
  a=0.7→MAR 3.3/DD-17%, a=1.0→2.5/-22%). Its 7.50/6.84 headline is partly fake. The robust
  object is the uptrend core (~6/6).
- **rmom is load-bearing as a GATE** (no-gate pool MAR 0.12 bybit / -0.22 binance) and strongly
  monotone (Q1 +1.9% fade > … > Q5 -1.9%/-3.5%), but the binary 0.25 gate already captures it.
- **The squeeze is the #1 weakness (= the recent-tilt).** Two attacks both work in-sample:
  - **BTC-beta hedge** (causal 90d rolling beta, long ~10% BTC): Sharpe bybit 3.05→3.32,
    binance 2.62→3.17; DD down; flattens 2023-24. Window-invariant (60-150d) + cost-invariant
    (0-10bps) ⇒ not overfit. BUT most of the headline RETURN gain is sample-specific (long-BTC
    profited as BTC rose ~5x); the regime-agnostic variance-only gain is modest (Sharpe +0.04-0.06).
  - **Relative-strength / BTCDOM gate** (operator's idea, UNTESTED): pause/scale entries when
    alts outperform BTC (BTCDOM↓ = alt-season = the actual squeeze mechanism). Regime-agnostic
    (no bull-beta problem), more precise than the BTC-30d gate (worst days were only 6-8/12
    BTC-up; the common factor is alts ripping), and distinct from the rejected "weak-market skip"
    (that was skip-when-market-FALLING; this is the inverse and RELATIVE).
- The only path to a *large* gain is a genuinely orthogonal alpha (residual-momentum standalone
  cross-sectional L/S, net IC -0.19/-0.35; operator-gated engine build).

**Program thesis: stop refining the entry; make the book regime-robust / market-neutral.**

---

## 2. Benchmark to beat (reproduced bit-exact, commit 5e1c960)

Uptrend ensemble **winner_base** = `{turn3p3:0.30, turn4p3:0.20, turn4p5:0.40, age210tp14:0.10}`
@ rebalance `w90/tv0.045/max10/ddh-0.04` (no strategy-equity momentum):

| venue | return | MAR | DD | Sharpe(ann) | avg scale |
|---|---|---|---|---|---|
| bybit | +226% | 6.18 | -11.7% | 3.05 | ~7.5x |
| binance | +142% | 6.01 | -7.7% | 2.62 | ~8.0x |

A candidate "beats the benchmark" only if it improves DD and/or Sharpe on **BOTH** venues,
survives 2x cost, flattens the 2023-24 sub-period, and is not overfit (perturbation/leave-out
stable). MAR is primary, Sharpe secondary (STATE.md). Re-anchor leverage to max4-6 (the headline
max10 is a recent-regime number; see the robustness receipt).

---

## 3. Work packages (priority order)

### WP1 — Regime-aware continuous short (CENTERPIECE; start here)
**STATUS 2026-06-09: WP1a RAN — NO-GO (pre-registered bar failed unambiguously; ICs
near-zero/positive both venues). Mechanism confirmed contemporaneous (same-day RS vs
book −0.26/−0.30) but alt-RS is a daily martingale (AR1 ~+0.03) → WP1b/1c gate forms
are DEAD; fall back to WP2 (done) + WP3 (active). Receipt:
`docs/preregistration/continuous-rs-squeeze-probe-2026-06-09.md`.**
Hypothesis: the squeeze is driven by alts-outperform-BTC (BTCDOM↓), and a causal relative-strength
signal predicts it.
- **1a (cheap, FIRST): diagnose.** Build a PIT/causal EW-alt-market factor; test whether trailing
  alt-vs-BTC relative strength predicts forward squeezes (negative forward strategy return). The
  proxy is `EW_alt_market_return - BTC_return` (true BTC.D not in the perp roots). Use the winner
  equity ledger's daily returns + BTC daily returns (cheap glob) + the EW-alt factor.
  - *Go/no-go:* if trailing RS predicts squeezes (IC materially negative, both venues), proceed;
    else fall back to WP3 + WP4.
- **1b: test three forms** through the rebalance, both venues, pre-registered:
  (i) hard entry gate (pause when alt-season), (ii) **continuous size-down ∝ alt-season strength**
  (preferred — keeps breadth), (iii) replace the BTC-30d gate with the RS gate.
- **1c: compare & combine with the BTC-beta hedge** — do gate + hedge stack? Which is more
  forward-robust (the gate is regime-agnostic; the hedge's return is sample-specific)?
- *A-priori win (Tier-2 demo-candidate rule):* both venues positive return; pooled MAR Δ > +0.1
  vs the un-gated control; neither venue MAR Δ < -0.5; DD reduced or Sharpe up on both; survives
  2x cost; the size-down version's gated-out trades had negative expectancy (not forgoing good fades).

### WP2 — De-fragilize & re-anchor the canonical (cleanup; do early)
**STATUS 2026-06-09: DONE.** Demotion receipt
`docs/preregistration/continuous-demote-downtrend-extension-2026-06-09.md`; STATE.md +
research_summary updated (canonical = uptrend core @ max4-6).
Formally demote the downtrend extension (document the fragility from §1), re-anchor "the continuous
strategy" to the **parsimonious uptrend core at moderate leverage (max4-6)**. Update STATE.md +
research_summary so the program stops chasing the fake 7.5 MAR. One concise pre-registration
receipt for the fragility verdict.

### WP3 — Bank the market-neutral hedge (modest sure thing)
**STATUS 2026-06-09: DONE — BANKED (both stages pre-registered and PASSED).**
Stage-A (PASS 6/6): BTC instrument selected. Stage-B (PASS 8/8): hedge leg inside
`apply_rebalance_rule` (+8 tests), survives 2x book cost (helps MORE) and 1-day beta
latency. Receipts `docs/preregistration/continuous-hedge-{overlay,engine}-2026-06-09.md`.
Remaining = operator decisions: live hedge-leg executor plumbing, forward-demo push.
Integrate the BTC-beta hedge as an engine leg, pre-register, run the robustness battery, push to
forward demo. Honest framing: regime-robustness, not free return. May be subsumed by WP1c if the
gate dominates.

### WP4 — The big-win swing (OPERATOR-GATED)
Scope the residual-momentum standalone cross-sectional L/S (net IC -0.19/-0.35) — the documented
path to a new orthogonal alpha. Needs an engine build + explicit operator greenlight. Note: the
2026-06-09 ridge combiner (combining features for sizing within the event pool) was REJECTED at
Tier-1 (negative OOF IC), so this must be a standalone selection signal, not a within-pool combine.

### WP0 — Methodology guardrails (gate everything)
- The EW-alt-market factor must be PIT/causal (no future universe, no survivorship; liquid filter
  applied per-day; exclude BTC). A latency-delayed copy should not destroy the result.
- Binance funding-interval debt (4h vs 8h) — treat binance funding-sensitive results cautiously.
- Forward demo/paper is the only Tier-3 arbiter; it is immature for continuous — accumulate it.

---

## 4. Reproduction & tooling (so the next agent doesn't re-discover)

- **Data roots (this Windows box):** `C:\Users\user\SHARED_DATA\bybit_full_pit`,
  `…\binance_full_pit`. Coverage ends bybit 2026-05-26, binance 2026-04-30 → cap `--end 2026-05-27`.
  klines partitioned `date=YYYY-MM-DD/symbol=SYM/`. Run scripts with
  `PYTHONPATH=C:/Users/user/Desktop/liquidity-migration` and `POLARS_MAX_THREADS=8-10`; `.venv/bin/python`.
- **Engine:** `liquidity_migration.continuous_events.run_continuous_event_research(root, config, report_dir)`
  with `ContinuousEventConfig`. Base config = load
  `~/SHARED_DATA/continuous_merged_signal_raw_2026-06-07/bybit/merged_signal/continuous_report.json`["config"]
  (turn3_pop3, age240, TP10, fixed 24h, inverse-vol, rmom_quantile=0.25, btc_trend_gate=uptrend,
  decile 9, feature max_ret168, liq 500k, entry_delay +1h). Writes continuous_{report.json,
  trades.csv, mtm_equity.csv}.
- **Rebalance/ensemble:** `liquidity_migration.continuous_rebalance` (decompose_continuous_components,
  apply_rebalance_rule, ContinuousRebalanceRule) and `scripts/continuous_ensemble_rebalance_scout.py`
  (cheap recombination of precomputed component ledgers; `_combine_components`). Winner rule =
  ContinuousRebalanceRule(realized_vol_window_days=90, target_daily_vol=0.045, max_scale=10.0,
  drawdown_half_threshold=-0.04, resize_cost_bps=10.0, strategy_momentum_window_days=0).
- **Component source ledgers** (the 4 winner components), under `~/SHARED_DATA`:
  turn3p3=`continuous_merged_signal_raw_2026-06-07/{venue}/merged_signal`;
  turn4p3/turn4p5=`independent_continuous_entry_filter_sweep_exploratory_2026-06-07/{venue}/age240_turn4pop{3,5}_crowd2`;
  age210tp14=`independent_continuous_tp_hold_sweep_exploratory_2026-06-07/{venue}/age210_tp14_hold24_invvol10_crowd2`.
- **Winner equity ledger** (for overlay analyses):
  `~/SHARED_DATA/continuous_robustness_2026-06-09/scale_sensitivity/{venue}/winner_base/w90_tv0.045_max10_ddh-0.04/equity.csv`.
- **BTC daily return (cheap):** glob `…/{venue}_full_pit/klines_1h/date=*/symbol=BTCUSDT/*.parquet`,
  daily last close → return.
- **EW-alt-market factor (heavier):** scan klines for date>=2023-01-01, daily last close + turnover
  per symbol, EW mean daily return over `turnover>=500k` non-BTC names; RS = alt_ew - btc.
- **Session drivers already written** (reuse/adapt): `scripts/continuous_{multihorizon,breaker,
  rmom_probe,rmomgate}_driver.py`. Derivatives enrichment: `scripts/continuous_derivatives_filter_scout.py`
  (`_enrich_trades_with_features`, `_apply_filter`, `_load_klines`).
- **Skills:** invoke `backtest-integrity` before any run; `research-phase-runner` for the
  pre-register→run→verdict→STATE-update loop; `run-strategy` for CLI invocations.

## 5. Repo state at handoff (commit 5e1c960 + uncommitted working-tree changes)
- Uncommitted: `liquidity_migration/continuous_demo.py` + test (age gate now reads authoritative v5
  `listing_age_days` instead of the kline-cache first bar — fixes STATE.md debt #2 for continuous;
  90/90 continuous-demo tests pass); filled-in receipts `docs/preregistration/ridge-combiner-2026-06-09.md`
  and `docs/preregistration/continuous-winner-robustness-2026-06-09.md`; this plan; new session drivers.
- Decide with the operator whether to commit these before starting WP1.
