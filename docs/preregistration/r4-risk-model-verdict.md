# R4 — Risk-factor model (Tier-3 residual-Sharpe foundation) — VERDICT (full-PIT)

> **Re-homed 2026-05-29.** This is the validation record for the risk model that lives on
> `main` (`liquidity_migration/risk_model.py` + `tests/test_risk_model.py`). It was
> produced under the retired Round-2 "integrated-strategy-program" framing; the **model
> itself is current and live**, and it is the **foundation of the Tier-3 residual-Sharpe
> gate** (STATE.md Tier-3: "Residual Sharpe ≥ +0.3 (factor-model residual)"). R4 is
> orthogonal to the forward research plan
> ([research_plan_selection_execution.md](../research_plan_selection_execution.md), E1→E2→E3)
> — it sits *under* the gate those experiments must eventually pass.
>
> **Old-label → current-plan map:** R9 integrated assembly / R10 demo-candidate / R11 OOS
> → the **Tier-2 / Tier-3 gates** in STATE.md; R12 sniper → **E3**; C0–C3 continuous → **E2**;
> R6 cost model → honest costing inside every experiment. The R4 runner
> (`scripts/r4_risk_model_validation.py`) was retired in the script cleanup — the **three
> library functions** (`build_factor_panel`, `fit_factor_returns`, `decompose_strategy_pnl`),
> not a CLI, are the integration surface; recover the runner from git history only if a
> re-validation is needed.

Date: 2026-05-29
Pre-reg: retired (was `integrated-strategy-program.md` sub-phase R4; consolidated into
[research_summary.md](../research_summary.md)).
Run label: full-PIT factor-model construction (in-sample 2023-04-01→2026-05-28,
1153d; research infrastructure, not a strategy-P&L claim — the residual-Sharpe gate this
enables is labelled per-cell at Tier-2/Tier-3).
Code: `liquidity_migration/risk_model.py` (`build_factor_panel`, `fit_factor_returns`,
`residual_variance_capture`, `decompose_strategy_pnl`); unit-tested in
`tests/test_risk_model.py`.
Artifacts: `~/SHARED_DATA/r4_risk_model_2026-05-29_{bybit,binance}.json` (tag
`r4_risk_model_2026-05-29`). Numbers below are from the canonical re-validation
(2026-05-29 13:44) under the `9f52819` + `b1a3368` hardening (both ancestors of `main`) —
calendar-exact returns, permutation-null variance capture, decompose-snap fix.

## Headline

Factor model **VALIDATED**. 6 stable factors per venue. All three pre-registered
validation criteria pass on both venues. Critically, the variance-capture criterion is the
honest permutation-null test (not the in-sample `residual_std<raw_std` R²≥0 tautology): the
model's residual std beats the within-day target-shuffle null on both venues at p=0.0
(200 perms) — it reduces forward-return variance by MORE than chance, with a mean-zero
residual. So the Tier-3 residual-Sharpe ≥ +0.3 gate rests on a real factor model, not a
placeholder.

Construction ran 7 factors → validation pruned `xs_rank_ret_3d` (cross-sectional 3-day
momentum) as the lone criterion-1 failure, leaving 6.

## Final factor set (6)

| # | Factor | Status |
|---|---|---|
| 1 | BTC beta (rolling-60d OLS) | KEEP |
| 2 | XS 3d momentum (`xs_rank_ret_3d`) | DROP — sign-flip factor return (criterion 1) |
| 3 | XS 30d momentum (`xs_rank_ret_30d`) | KEEP (weak but sign-consistent +) |
| 4 | Realized-vol regime (`realized_vol_rank`) | KEEP |
| 5 | Funding-rate Z (`funding_rate_z`) | KEEP |
| 6 | Liquidity tier (`liquidity_rank`) | KEEP |
| 7 | Alt-season | DEFERRED (6 already meets target) |
| 8 | Mark-index premium (`premium_index_z`) | KEEP |

`main`'s `_FACTOR_COLUMNS` carries exactly the 6 kept factors (`xs_rank_ret_3d` dropped).

## Results — 6-factor model (full-PIT, both venues)

### Criterion 1 — each factor's daily factor-return Sharpe > 0 (factor is real)

| factor | bybit Sharpe | binance Sharpe |
|---|---|---|
| `liquidity_rank` | +1.86 | +1.77 |
| `funding_rate_z` | +1.46 | +0.79 |
| `btc_beta` | +1.07 | +0.63 |
| `premium_index_z` | +0.71 | +1.26 |
| `realized_vol_rank` | +0.75 | +1.40 |
| `xs_rank_ret_30d` | +0.02 | +0.19 |

PASS: 6/6 positive on both venues. `liquidity_rank` is the dominant factor (~1.8 both
venues, t≈2.6–3.3) — economically expected for a *liquidity*-migration universe.
`xs_rank_ret_30d` is weak (+0.02 bybit) but sign-consistent, so it clears the criterion as
written; retained (a risk factor's job is to span common variance, not to be individually
profitable).

Why `xs_rank_ret_3d` was dropped: in the 7-factor run its factor-return Sharpe was −0.47
(bybit) / +0.50 (binance) — sign-inconsistent across venues, |t|<1 either side, i.e.
unpriced noise. Removing it *raised* the kept factors' Sharpes (btc_beta 0.87→1.07,
realized_vol_rank 0.52→0.75 on bybit) — the remaining factors absorb its variance cleanly.

### Criterion 2 — factors are not proxies for each other (|corr| < 0.3)

Pre-reg criterion is literally |corr with realized vol| < 0.3. `realized_vol_rank`'s max
pairwise |corr| is 0.113 (bybit) / 0.173 (binance) → the literal criterion PASSES with
margin on both venues (0 flags).

The validation runner implements a *stricter* all-pairwise check. Under it, binance is
fully clean (0 redundant; max pair 0.205). On bybit the only flag is `funding_rate_z` ↔
`premium_index_z` at 0.335 — a non-vol pair, marginally over 0.30, one venue only. Both
retained, because: (a) both are strong and sign-consistent on both venues; (b) they are
independent on binance (corr ≤0.07); (c) residualization needs the factor *span*, not clean
individual attribution — mild collinearity between two real factors does not bias the
residual; (d) they capture distinct facets of perp basis (realized funding paid vs
forward-looking premium).

### Criterion 3 — variance capture vs a permutation null (NOT the in-sample tautology)

The pre-reg's literal "residual std < raw std" is an in-sample tautology: per-day OLS with
an intercept guarantees R²≥0, so residual std is mechanically ≤ target std even for a
pure-noise model. The honest test (`residual_variance_capture`) compares the real residual
std against a within-day target-shuffle permutation null (200 perms; shuffling the target
across symbols destroys any factor→return relation while preserving each day's return
distribution). raw and residual std are over the identical surviving rows.

| venue | raw std (same-pop) | residual std | resid/raw | null p05 ratio | p-value | captures real variance |
|---|---|---|---|---|---|---|
| bybit | 0.06688 | 0.05193 | 0.776 | 0.806 | 0.0 | YES |
| binance | 0.06591 | 0.04996 | 0.758 | 0.804 | 0.0 | YES |

PASS both venues. The real residual ratio (0.776 / 0.758) is below the null's 5th
percentile (0.806 / 0.804) and below all 200 shuffles (p=0.0) — the model reduces
forward-return variance by significantly MORE than fitting 6 parameters to noise does.
Residual mean ≈ 0 (−3.5e-17 / −8.0e-17). (bybit 1150 / binance 787 factor-return days;
binance shorter because the per-day regression needs all 6 factor columns non-null and its
funding/premium history is shallower — 787d ≈ 2.15y is ample.)

Forward-survivorship: only 1508 bybit / 1900 binance panel rows (0.33% / 0.45%) have a null
forward return (delist/gap terminal days) and are necessarily dropped from the
cross-section — a negligible exposure, surfaced for transparency.

### Methodology-hardening re-validation (`9f52819` + `b1a3368`)

R4 was first built against the pre-hardening engine; two methodology commits then changed
its inputs, so it was re-validated under the hardened code (the numbers above are that run):

- **Calendar-exact returns** — `_attach_forward_returns` / `_attach_daily_returns` moved
  from a positional row-shift to a calendar-exact ts-join (gapped returns nulled, not
  misaligned). Affects `fwd_ret_1d` (the target) and `btc_beta`. bybit: byte-identical
  (no in-window gaps); binance: factor Sharpes shift in the 2nd decimal (still 6/6 positive;
  `xs_rank_ret_3d` drop holds, sign-flip −0.47/+0.50).
- **Permutation null** — the variance-capture criterion is now the permutation null above.
  This verdict originally cited the in-sample tautology (~47% "explained") as evidence; that
  was wrong and is corrected — the honest test (p=0.0) still PASSES.
- **Decompose-snap fix** — `decompose_strategy_pnl` snaps `entry_ts_ms` to the 00:00-UTC
  panel grid (the engine ledger is +1h/bar-end; the prior join was all-null → residual
  inflated) and returns null (not 0.0) when un-decomposable, with resolved-fraction
  counters. This fixes the Tier-3 residual-Sharpe input — without it the gate would read
  inflated alpha.

No conclusion changes: 6/6 factors Sharpe>0, the `xs_rank_ret_3d` drop holds, criteria 1–3
pass. The hardening only made the evidence honest.

## Tier-3 integration spec (the deliverable)

- Risk model = the 6 factors above. Loadings are computed at each row's end-of-day-close
  `decision_ts` (rolling windows strictly backward); entry is the engine's +1h-delayed fill,
  so loadings are causal at the executable decision.
- **Residual-Sharpe path (Tier-3 hard gate, ≥ +0.3):** a cell's trade ledger → normalize to
  `(symbol, entry_ts_ms, hold_days, realized_return)` →
  `decompose_strategy_pnl(trades, build_factor_panel(...), fit_factor_returns(...))`.
  `explained = exposure_at_entry · Σ factor_returns_over_hold`; `residual = realized −
  explained`; `residual_sharpe = mean/std` (per-trade, un-annualized — the gate annualizes
  with the cell's actual trade span). `decompose` snaps `entry_ts_ms` to the 00:00-UTC panel
  grid so the engine's +1h/bar-end entries match factor-loading rows; un-decomposable trades
  are null (not 0.0), reported via resolved-fraction. A cell that fails (≤ +0.3) is "selling
  vol / buying beta", not carrying alpha.
- The same loadings serve net per-factor exposure ceilings, and any stress / comparison step
  should residualize each cell on this model before comparison.
- The `risk-model` CLI subcommand (build-panel/fit-returns/residualize-trades) remains an
  optional ergonomics wrapper — the three library functions are the integration surface, so
  the CLI is deferred (build only if a phase needs the shell entrypoint).

## Integrity

- **Full-PIT.** `build_factor_panel` reads the `*_full_pit` roots (delisted-inclusive PIT
  universe) → PIT-clean cross-sections. No current-universe shortcut.
- **Causal.** All exposures (rolling-60d beta, XS ranks, Z-scores) look strictly backward
  from `decision_ts`; both `fwd_ret_1d` (target) and the daily returns feeding `btc_beta`
  are calendar-exact. Unit-tested (`tests/test_risk_model.py`).
- **Not a strategy-P&L claim** → no cost/funding model needed here; cost/funding enters at
  the per-cell residual-Sharpe.

## DECISION

- **R4 COMPLETE** — model validated, 6 factors finalized. No further factor iteration.
- Residual machinery confirmed for the Tier-3 gate; `decompose_strategy_pnl` is the gate
  input.
- Alt-season (#7) and the risk-model CLI stay deferred (not on the critical path).

## Next (current plan)

R4 is done and frozen as the Tier-3 residual-Sharpe foundation. Forward research is the
narrow plan in [research_plan_selection_execution.md](../research_plan_selection_execution.md):
**E1** (execution premium — `fixed_delay` vs `promoted_quality_squeeze`) → **E2** (apply the
fade-confirmation execution to the continuous candidate pool) → **E3** (sniper, sub-1h). At
the Tier-3 gate, residualize each demo-candidate cell on this model (`decompose_strategy_pnl`)
and require residual Sharpe ≥ +0.3. The risk model needs no further work to support that —
it is the foundation, not an open phase.
