# Pre-registration: audit-driven methodology hardening (2026-05-29)

**Date:** 2026-05-29
**Author:** assistant (deep-audit remediation), pending owner ratification
**Stage:** proposed — code applied to the working tree, NOT yet committed/deployed.
**Integrity standard:** `docs/backtesting_errors_we_never_repeat.md` is binding.
**Optimization objective:** unchanged — (Return / Drawdown) MAR-primary, Sharpe secondary.

## Why this receipt exists

The 2026-05-29 full-system deep audit found correctness and methodology issues.
Several fixes change backtest outputs, so per `AGENTS.md` they are pre-registered
here before any run is cited as evidence. **Every change below moves results in
the CONSERVATIVE / more-honest direction (higher cost, worse drawdown, corrected
horizons) — none can inflate an edge.** A methodology change means prior runs are
not directly comparable; the first run under these defaults is a **re-baseline**
(label `exploratory` until the control is re-run under identical settings).

## Applied (working tree, test-gated, ruff-clean)

| Tag | Change | Effect on results | Error # |
|---|---|---|---|
| H3 | `stop_fill_mode` default `stop` → `bar_extreme` (`volume_events.py`) | Stops fill at the bar's adverse extreme (gap-through-to-worst cover) instead of the exact trigger. Drawdown/MAR on stop-heavy short cells worsen — now honest. | #14 |
| M4 | `signal_harness._attach_forward_returns` positional → calendar-exact shift | Gapped-symbol forward returns now calendar-anchored (or null), not horizon-misaligned. Round-1 Phase-5 IC would need recomputation to re-validate the 5 survivors. | #13 |
| M1 | `volume_events._promotion_fields` whole-period branch now enforces max-DD + Sharpe (was unconditional pass) | `promotion_gate_pass` is a real quality gate again, not a PIT-coverage flag. Fewer rows pass. | — |
| M2 | `maker_fill_probability` default `0.60` → `0.0` in `configs/volume_alpha.default.yaml` (+ `--maker-fill-probability` CLI override) | Backtest now costs 100% taker, matching the deployed runner. Base round-trip 15 bps (was a 0.60-maker blend). Net returns/MAR drop — honest. | #6/#24 |
| H2 (diagnostics) | `summarize_trade_backtest` now reports `worst_trade_mae`, `mean_trade_mae`, `worst_weighted_intrahold_loss` (`trade_lifecycle.py`) | Surfaces per-position intra-hold adverse excursion that realised-at-exit DD hides. A LOWER BOUND on portfolio intra-hold DD. (Full portfolio-MTM-DD as a GATE remains the sub-phase below — it needs the daily price path AND recalibration of the pre-registered DD thresholds.) | #20 |
| M3 (observability) | `summarize_trade_backtest` now reports `realized_gross_mean`/`_max` per cell (`trade_lifecycle.py`) | Makes the risk_equal floating-gross confound auditable. Renormalisation was deliberately NOT done — it would destroy risk_equal's documented DD-shrinking mechanism. | — |
| H1 (capability) | `LongNativeConfig.notional_multiplier` (default 1.0) applied to the backtest's `_finalize_trade` (`long_native.py`) | Default 1.0 = no change to historical results. Set to the deployed value (10) to validate the long sleeve at the gross it actually trades. | #16 |
| M5 | `binance_vision.build_binance_oos` fails closed above 0.5% download-failure ratio + persists failed-jobs artifact | Refuses to write a survivorship-biased OOS universe. | #1/#12 |
| M6 + PIT flag | `apply_decision_rule` surfaces crashed cells as falsifiers + flags non-full-PIT cells; `_sweep_runtime` emits `run_label`/`full_pit_universe_pass` | A failed/partial-PIT cell can no longer silently vanish from a verdict. | #1/#19 |

(Non-result-changing correctness fixes from the same audit — C1/H5 cache
orphan-close, H4 funding pagination, H6 kline ts bound, M7 funding sleeve
scoping, M8 multi-leg orphan PnL, ws_risk telemetry-leak — are execution/ledger
fixes, documented in the audit memo, not backtest-methodology and not gated here.)

## Residual OWNER DECISIONS (the code/capability is shipped; the call is yours)

These are no longer code blockers — the supporting capability/observability is
applied above. What remains is a judgement only the owner can make:

| Tag | Decision required |
|---|---|
| **H2 gate threshold** | Adopting a full portfolio mark-to-market DD (compounding CONCURRENT open positions) for the GATES means re-setting the pre-registered DD thresholds (e.g. Tier-3 "DD < 50%") against the new, deeper DD definition — the threshold NUMBERS are calibrated to the realised-DD definition, so swapping the metric without recalibrating would make the gate meaningless. Decide the new thresholds, then implement the daily-MTM curve as its own sub-phase. The shipped `worst_*_mae` diagnostics already quantify the gap in the meantime. |
| **H1 long-sleeve gross** | The live long sleeve trades `notional_multiplier=10` (documented owner pick, 2× the ~5× research-Sharpe peak). The backtest can now be run at any multiplier (default 1.0). Decide: validate the long sleeve at the deployed 10× (`LongNativeConfig(notional_multiplier=10)`) before any real-money step, or reduce the live sleeve to the validated gross. Do NOT promote the long sleeve to real money at an un-validated gross. |
| **M3 matched-gross comparison** | Realised gross is now reported per cell. When judging risk_equal vs the equal-weight control, compare at matched `realized_gross_mean` so a MAR delta reflects risk-adjustment, not leverage. (Renormalisation is intentionally NOT applied — it would remove risk_equal's DD-shrinking mechanism.) |

## Decision rule for these changes

Treat the first sweep under the applied defaults as a **re-baseline** (`exploratory`):
re-run the control cell under identical settings before any cell-vs-control MAR
delta is read as Tier-1/Tier-2 evidence. The applied changes only tighten realism,
so a cell that survives the harder bar is stronger evidence, not weaker.

---

## Audit #2 (2026-05-29) — second deep audit, methodology fixes

A second full-system deep audit (focused on the parallel R4 risk-model code, the
decision/sweep tooling, and the WS-event-driven conversion) surfaced six further
methodology issues. **Every change below again moves results in the conservative /
more-honest direction — none can inflate an edge.** Same re-baseline rule applies.
All test-gated (`tests/test_risk_model.py`, `tests/test_r1_robustness.py`,
`tests/test_scripts_apply_decision_rule.py`, `tests/test_runtime_scripts.py`,
`tests/test_liquidity_migration_signal_harness.py`) and ruff-clean.

| Tag | Change | Effect on results | Error # |
|---|---|---|---|
| A1 | R4 validation criterion (3) "variance capture": in-sample `residual_std < raw_std` (always true — OLS R²≥0 tautology, passes a pure-noise model) → permutation-null test (`risk_model.residual_variance_capture`; `r4_risk_model_validation.py`). | The factor-model variance-capture gate now FAILS a zero-signal model and PASSES only when the real residual std beats a within-day target-shuffle null (p<0.05). A core R4 gate that produced a false PASS is now a real test. No committed R4 verdict existed yet, so nothing prior is contaminated. | #19 (multiple-testing/illusory-evidence class) |
| B1 | `risk_model.decompose_strategy_pnl` matched factor loadings/returns on the **raw** `entry_ts_ms`; the engine ledger's +1h/bar-end entry missed the 00:00-UTC panel grid → all-null → realized return mis-booked as residual alpha. Now snaps entry to the daily grid; emits null (not 0.0) when un-decomposable; reports `n_unresolved`/`resolved_fraction`. | Latent today (function is tests-only, no live caller). Once wired, the Tier-3 residual Sharpe is computed from correctly-decomposed trades instead of a silently-inflated residual. | #13 (timestamp/grid misalignment) |
| B3 | `signal_harness._attach_daily_returns` positional `shift(1)` → calendar-exact `ts_ms − 1 day` join (mirrors the M4 forward-return fix for the BACKWARD daily return). | A gapped symbol's post-gap `ret_1d` is now null, not a multi-calendar-day move mislabeled 1-day. Changes the BTC-beta factor and realized-vol/momentum features for symbols with calendar gaps (delist→relist, data holes). R4 factor returns under this fix supersede any pre-fix run. | #13 |
| B4 | `risk_model.fit_factor_returns` forward-survivorship made visible: a symbol's terminal (null-forward-return) day is necessarily dropped from each cross-sectional regression; the R4 report now emits `fwd_survivorship_null_target_rows`/`_frac`. | No estimator change (you cannot regress on a non-existent forward return). Surfaces the survivorship exposure rather than leaving it silent. | #1/#12 |
| B5 | `apply_decision_rule` now HARD-EXCLUDES non-full-PIT cells (verdict `non_full_pit`) instead of only warning — a survivorship-tainted `--allow-partial-pit` cell can no longer receive `investigation_positive`/`candidate`. | A partial-PIT cell is removed from the promotion-positive bucket regardless of how good its (biased) numbers look. Strictly fewer cells can be promoted. | #1/#19 |
| B6 | `r1_robustness` MAR: zero-drawdown cells returned `+inf` (spuriously cleared the pooled-MAR demo-eligibility gate) → `nan`, and `_tier2_verdict` treats a non-finite MAR Δ as non-eligible. Also per-cell try/except on report load (an OOM-killed cell's truncated JSON no longer crashes the whole run). | A degenerate / too-few-trades zero-DD cell can no longer auto-pass the Tier-2 demo gate on a divide-by-≈0. Strictly fewer cells pass. | #20 (bad accounting) / #25 (all-or-nothing compute) |

(Non-result-changing fixes from audit #2 — the kline cycle-wake future-bar clamp
[`kline_stream_manager`], the long-sleeve `mae=mfe=NaN`-not-`0.0` diagnostic
honesty [`long_native`/`trade_lifecycle`], the corrected ws_risk orphan-close
grace-window comment [`event_demo_exits`], and the memory-aware sweep-worker
default [`_sweep_runtime`] — are execution/diagnostic/ops fixes, not
backtest-methodology, and are not gated here.)

The author's WS-event-loop / trade-client-retry regressions surfaced by the same
audit (bootstrap-blocking WS-trade build, orphaned retry thread) are deferred per
owner instruction (low-severity, restart-only, demo-only).
