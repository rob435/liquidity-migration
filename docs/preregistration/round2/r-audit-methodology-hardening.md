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
