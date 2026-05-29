# R4 — Risk-factor model construction (Jane-Street-style) — VERDICT (full-PIT)

**Date:** 2026-05-29
**Pre-reg:** [integrated-strategy-program.md](integrated-strategy-program.md) sub-phase R4.
**Run label:** full-PIT factor-model construction (in-sample 2023-04-01→2026-05-28,
1153d; **research infrastructure, not a strategy-P&L claim** — the residual-Sharpe
gate this enables is labelled per-cell at Tier-2/Tier-3).
**Code:** `liquidity_migration/risk_model.py` (`build_factor_panel`, `fit_factor_returns`,
`decompose_strategy_pnl`). Validation runner `scripts/r4_risk_model_validation.py` (4844bf1).
**Artifacts:** `~/SHARED_DATA/r4_risk_model_2026-05-29_{bybit,binance}.json` (tag
`r4_risk_model_2026-05-29`) + the M4-corrected re-validation
`~/SHARED_DATA/r4_revalidate_m4_2026-05-29_{bybit,binance}.json` (runner
`scripts/r4_revalidate_m4.py`). **Numbers below are the M4-corrected values.**

## Headline

**Factor model VALIDATED. 6 stable factors per venue — exactly the plan's 5-6
target.** All three pre-registered validation criteria pass on **both** venues. The
residual machinery (`fit_factor_returns` → `decompose_strategy_pnl` → residual Sharpe)
is confirmed sound: it strips ~47% of cross-sectional forward-return variance with a
mean-zero residual, so the **Tier-3 residual-Sharpe ≥ +0.3 gate now rests on a real
factor model**, not a placeholder.

The construction ran 7 factors → validation **pruned `xs_rank_ret_3d`** (cross-sectional
3-day momentum, factor #2) as the lone criterion-1 failure, leaving 6.

## Final factor set (6) — mapped to the pre-reg's 8 proposed

| # | Factor | Status |
|---|---|---|
| 1 | BTC beta (rolling-60d OLS) | **KEEP** |
| 2 | XS 3d momentum (`xs_rank_ret_3d`) | **DROP** — sign-flip factor return (criterion 1) |
| 3 | XS 30d momentum (`xs_rank_ret_30d`) | **KEEP** (weak but sign-consistent +) |
| 4 | Realized-vol regime (`realized_vol_rank`) | **KEEP** |
| 5 | Funding-rate Z (`funding_rate_z`) | **KEEP** |
| 6 | Liquidity tier (`liquidity_rank`) | **KEEP** |
| 7 | Alt-season | **DEFERRED** (build-time; 6 already meets target) |
| 8 | Mark-index premium (`premium_index_z`) | **KEEP** |

Code now carries the 6-factor `_FACTOR_COLUMNS` / `_REUSED_FACTOR_SPECS` (this PR).

## Results — 6-factor model (full-PIT, both venues)

### Criterion 1 — each factor's daily factor-return Sharpe > 0 (factor is real)

| factor | bybit Sharpe | binance Sharpe |
|---|---|---|
| liquidity_rank | **+1.86** | **+1.80** |
| funding_rate_z | **+1.46** | +0.81 |
| btc_beta | +1.07 | +0.63 |
| premium_index_z | +0.71 | **+1.26** |
| realized_vol_rank | +0.75 | **+1.41** |
| xs_rank_ret_30d | +0.02 | +0.22 |

**PASS: 6/6 positive on both venues.** `liquidity_rank` is the dominant factor
(~1.8 both venues, t≈2.6–3.3) — economically expected for a *liquidity*-migration
universe. `xs_rank_ret_30d` is weak (+0.02 bybit) but sign-consistent, so it clears
the criterion as written; retained (a risk factor's job is to span common variance,
not to be individually profitable).

**Why `xs_rank_ret_3d` was dropped:** in the initial 7-factor run its factor-return
Sharpe was **−0.47 (bybit) / +0.50 (binance)** — sign-inconsistent across venues and
|t|<1 either side, i.e. unpriced noise, not a stable factor. It is the *only* factor
that failed criterion 1. Removing it *raised* the kept factors' Sharpes (btc_beta
0.87→1.07, realized_vol_rank 0.52→0.75 on bybit) — the remaining factors absorb its
variance cleanly. Decision per the criterion as written (Sharpe>0), not a post-hoc
t-stat bar.

### Criterion 2 — factors are not proxies for each other (|corr| < 0.3)

Pre-reg criterion is literally **|corr with realized vol| < 0.3**. `realized_vol_rank`'s
**max** pairwise |corr| is **0.113 (bybit) / 0.173 (binance)** → every factor's
correlation with realized vol is below that → **the literal criterion PASSES with a
large margin on both venues (0 flags).**

The validation runner implements a *stricter* all-pairwise check. Under it, binance is
fully clean (0 redundant; max pair 0.205). On bybit the only flag is
**`funding_rate_z` ↔ `premium_index_z` at 0.335** — a non-vol pair, marginally over
0.30, one venue only. **Both retained**, because: (a) both are strong and
sign-consistent on both venues; (b) they are independent on binance (corr ≤0.07);
(c) residualization needs the factor *span*, not clean individual attribution — mild
collinearity between two real factors does not bias the residual; and (d) they capture
distinct facets of perp basis (realized funding paid vs forward-looking premium).

### Criterion 3 — residual mean ~0 and residual std < raw std (model captures variance)

| venue | raw fwd-ret std | residual std | resid/raw | variance explained | residual mean |
|---|---|---|---|---|---|
| bybit | 0.07126 | 0.05193 | **0.729** | **46.9%** | −3.5e-17 |
| binance | 0.06896 | 0.04996 | **0.724** | **47.5%** | −4.3e-17 |

**PASS both venues.** Residual cross-section mean is ~0 (machine-epsilon) by OLS
construction, and the factor model explains ~47% of cross-sectional forward-return
variance — substantial for a daily 6-factor crypto-perp model. (bybit 1150 / binance
787 factor-return days — binance is shorter because the per-day regression requires all
6 factor columns non-null and binance funding/premium history is shallower; 787d ≈ 2.15y
is ample.)

### M4 re-validation (concurrent commit `9f52819`)

The R4 construction first ran against the pre-`9f52819` `signal_harness`. That commit's
**M4 hardening** replaced the positional row-shift in `_attach_forward_returns` with a
calendar-offset join, so `fwd_ret_1d` — this model's regression target — now nulls a
symbol's gapped day (delist→relist / data hole) instead of returning a misaligned
multi-day move. Because the target changed, the model was **re-validated on the
corrected returns** (`scripts/r4_revalidate_m4.py`, tag `r4_revalidate_m4_2026-05-29`):

- **bybit: byte-identical** pre/post-M4 (no gapped days in its in-window daily grid).
- **binance: shifts <2%** — raw fwd-ret std 0.0704→0.0690 (the fix correctly drops
  inflated gap-straddling returns), resid/raw 0.7238→0.724, factor Sharpes move in the
  2nd decimal. **No conclusion changes:** all 6 factors still Sharpe>0 both venues, the
  `xs_rank_ret_3d` drop re-confirmed (−0.467 bybit / +0.499 binance), all criteria pass.

## R9 / Tier-3 integration spec (the deliverable)

- **Risk model = the 6 factors above.** Loadings are computed at each row's
  end-of-day-close `decision_ts` (rolling windows strictly backward); entry is the
  engine's +1h-delayed fill, so loadings are causal at the executable decision.
- **Residual-Sharpe path (Tier-3 hard gate, ≥ +0.3):** a cell's trade ledger →
  normalize to `(symbol, entry_ts_ms, hold_days, realized_return)` →
  `decompose_strategy_pnl(trades, build_factor_panel(...), fit_factor_returns(...))`.
  `explained = exposure_at_entry · Σ factor_returns_over_hold`; `residual = realized −
  explained`; `residual_sharpe = mean/std` (per-trade, un-annualized — the gate
  annualizes with the cell's actual trade span). A cell that fails (≤ +0.3) is "selling
  vol / buying beta", not carrying alpha.
- **R9 factor caps** use the same loadings (net per-factor exposure ceiling).
- **R7 stress / R10:** residualize each cell on this model before comparison.
- The `risk-model` CLI subcommand (`build-panel`/`fit-returns`/`residualize-trades`)
  remains an optional ergonomics wrapper — the three library functions are the
  integration surface R9/R10 call directly, so the CLI is **not on the R4 critical
  path** and is deferred (build only if a phase needs the shell entrypoint).

## Integrity

- **Full-PIT.** `build_factor_panel` reads the `*_full_pit` roots (delisted-inclusive
  PIT universe) → PIT-clean cross-sections. No `current_universe` shortcut.
- **Causal.** All exposures (rolling-60d beta, XS ranks, Z-scores) look strictly
  backward from `decision_ts`; the forward return `fwd_ret_1d` is the strictly-forward,
  **calendar-correct** (post-M4) regression target. Unit-tested
  (`tests/test_risk_model.py`, 8 tests).
- Not a strategy-P&L claim → no cost/funding model needed here; cost/funding enters at
  the per-cell residual-Sharpe and R6.

## DECISION

- **R4 COMPLETE — model validated, 6 factors finalized.** No further factor iteration.
- Residual machinery confirmed for the Tier-3 gate; `decompose_strategy_pnl` is the gate
  input.
- Alt-season (#7) and the `risk-model` CLI stay deferred (not on the critical path).

## Next

R6 per-name/per-bar cost model → R12 sniper entry → C0–C3 continuous → R9 integrated
assembly (residualize the bullish `drop_all_4` + `ff6_4pct` + dollar-equal + 1 composite
IC factor stack on this model) → R10 demo-candidate gate → R11 pre-2023 OOS.
