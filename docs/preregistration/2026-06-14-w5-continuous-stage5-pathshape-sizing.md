# Pre-registration: W5 Continuous Stage 5 - Path-Shape Sizing (Z2, gross-neutral, causal)

**Date:** 2026-06-14
**Author:** Claude (W5 continuous signal-alpha loop)
**Stage:** run-pending
**Plan:** `docs/research_plans/w5_continuous_signal_alpha/06_stage5_sizing_alpha.md`
**Contract:** `docs/research_plans/w5_continuous_signal_alpha/00_methodology_contract.md`
**Depends on:** Stage 0 PASS + **Stage 7b ADMISSIBLE**
(`docs/preregistration/2026-06-14-w5-continuous-stage7b-within-symbol-pathshape.md`)
— path-shape carries causal, cross-venue, within-symbol information about
per-notional return beyond the composite.

## Question

Does **sizing the SAME trades** by the admissible within-symbol path-shape signal
improve pooled MAR vs the frozen control, **without changing entries, exits, or
breadth, and without adding leverage**? Stage 7b proved the signal exists
(within-symbol IC ~0.05–0.11, p=0.001 both venues); Stage 5 is the engine test of
whether it is *harvestable* as a gross-neutral risk reallocation, net of resize
and funding costs.

## Control

`Z0_control_size`: the frozen `continuous_ensemble_v1` sizing/rebalance/hedge
object (the Stage 0 reconstruction). Must reproduce the Stage 0 ensemble exactly
(size hook absent).

## Mechanism (locked before the run)

A per-entry notional multiplier `m_i`, keyed by `(symbol, signal_ts_ms)`, injected
into the engine via the additive `size_mult_lookup` hook in
`continuous_events._run_trades` (default `None` → byte-identical; 329 continuous
tests pass unchanged). The multiplier is applied **after every selection gate and
after inverse-vol + regime sizing**, so entries, exits, breadth, capacity, cooldown
and the crowding gate are byte-identical to the control — only the *notional* of
each already-selected trade changes. Impact/resize cost is recomputed at the new
size by `_round_trip_bps` (resize cost charged).

Causal, gross-neutral construction of `m_i`:

1. **Feature** `f`: a causal pre-entry path-shape feature on the Stage 0 tape.
   Arms below pick `pre_24h_return` (Stage 7b strongest single) or the
   `Q_combined` equal-z average of the three Stage-7b features.
2. **Frozen-train within-symbol residual (causal).** Per venue, the train fold is
   the earliest 60% of selected entries by `signal_ts` (same split as Stage 7b).
   Freeze each symbol's train-fold mean `μ_sym` and the train-fold std of the
   within-symbol residuals `σ`. For any entry (train or test): `r = f − μ_sym`,
   `z = r / σ`. A symbol with **no** train-fold entry gets `m = 1` (no
   within-symbol baseline exists → no tilt; never a cross-symbol tilt). All
   parameters are frozen from the train fold and applied forward — no look-ahead.
3. **Bounded tilt:** `m_raw = clip(1 + κ·z, 1/C, C)`, **κ = 0.25**, **C = 2.0**
   (locked a priori; a 1-SD within-symbol residual tilts size ±25% before the
   clamp; size ∈ [0.5×, 2.0×]).
4. **Causal gross-neutralization (prior calendar month).** A single frozen-train
   mean normalizer does NOT hold gross-neutral out-of-sample because the
   path-shape feature *trends* (pre-run smoke test: binance test-fold raw-tilt mean
   reached ~1.15 — a 15% gross creep). So each entry's multiplier is divided by the
   **prior calendar month's** mean raw multiplier (fully causal — no within-month
   look-ahead; the first month uses 1.0). This tracks the trend with <=1-month lag,
   leaving ~2–3% residual creep (test-fold mean ~1.02–1.03). **MAR is
   scale-invariant** — uniformly scaling all sizes scales return *and* drawdown
   equally — so a few-percent uniform gross creep is not a MAR artifact; the real
   risk is *timing*-leverage (gross concentrated in favorable periods), which the
   prior-month normalizer removes. The binding gross check (gate #6) is therefore
   the **realized ledger notional ratio** vs Z0, gated at ±5%.

## Arms (locked)

- `Z0_control_size`: control (hook absent).
- `Z2_pre24_within_symbol`: `m` from `pre_24h_return` within-symbol residual.
- `Z2_combined_within_symbol`: `m` from the `Q_combined` within-symbol residual.
- `Z6_symbol_identity` (negative control): `m` from `symbol_hash_bucket`
  **cross-sectionally** standardized (frozen train μ,σ), same κ/C — i.e. sizing by
  arbitrary symbol identity. If `Z2` does not beat `Z6`, the "sizing alpha" is
  symbol mix, not within-symbol path-shape.

(`Z1` score-monotone, `Z3` vol-residual, `Z4` crowding-risk-budget,
`Z5` sniper-size are registered in the plan but deferred to their own runs;
this receipt runs Z0/Z2/Z2c/Z6.)

## Constraints (binding, from the plan)

- same entries; same exits; same max-active; same global gross cap;
- resize costs charged (size-aware impact via `_round_trip_bps`);
- funding preserved (`use_funding` ON, bybit modeled / binance partial-disclosed);
- **no leverage hidden as alpha** — realized gross must not exceed control.

## Metrics (per arm, per venue, pooled)

- total return, MAR, max drawdown, worst day, bps/trade;
- R1-compatible monthly returns (`scripts/r1_robustness.py`);
- realized mean multiplier and **avg / max active gross notional vs Z0**;
- symbol concentration (top-5-symbol share of |gross-weighted PnL|, HHI) vs Z0;
- component contribution; full-window AND test-fold (latest 40%, OOS) MAR;
- chronological-third MAR-delta stability.

## Decision rule (a priori) / Pass bar

`Z2` (either feature arm) is admissible only if, vs `Z0`:

1. positive total return on **both** venues;
2. **full-window pooled MAR delta `> +0.1`**;
3. no venue MAR delta `< -0.5`;
4. **test-fold (OOS) pooled MAR delta `> 0`** (same direction — guards train
   in-sampleness of the frozen sizing params);
5. max drawdown not worse than **+20% relative** on either venue;
6. **gross-neutral:** realized ledger notional ratio vs Z0 ∈ **[0.95, 1.05]** on
   both venues (MAR scale-invariance makes a few-% uniform creep immaterial; this
   band guards against *timing*-leverage; outside it → reject as leverage);
7. the negative control `Z6` pooled MAR delta is **strictly weaker** than `Z2`'s;
8. not carried by one venue or one symbol bucket (report concentration; if a
   single symbol contributes > 50% of the pooled MAR gain, flag as fragile).

Default label **`exploratory`** — historical. A pass nominates a demo/paper
shadow only; promotion is the strict Tier-3 forward-demo bar in `STATE.md`.

## Falsifier

Reject as sizing alpha if the effect is just more leverage (gross up), hidden
trade selection (breadth changed — impossible by construction, asserted), a single
venue or single symbol bucket carrying it, the test-fold (OOS) MAR delta is
negative, or `Z6` (symbol-identity sizing) matches/beats `Z2`.

## Window, roots, universe

- Window `2023-04-01 <= signal_ts < 2026-05-01` (common full-PIT overlap).
- Roots read-only (`~/SHARED_DATA/{bybit,binance}_full_pit`; writes only to
  `reports/<tag>/` and `~/SHARED_DATA/w5_continuous_stage5_*`). Forward
  demo/paper untouched. Full-PIT mandatory; Stage 0 PIT gate re-asserted.

## Run command

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/w5_continuous_stage5_pathshape_sizing.py \
  --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
  --stage0 ~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14 \
  --arms Z0_control_size,Z2_pre24_within_symbol,Z2_combined_within_symbol,Z6_symbol_identity \
  --kappa 0.25 --clamp 2.0 \
  --out ~/SHARED_DATA/w5_continuous_stage5_pathshape_sizing_2026-06-14
```

## Artifacts

Under `~/SHARED_DATA/w5_continuous_stage5_pathshape_sizing_2026-06-14/` and the
per-venue `reports/w5_continuous_stage5_pathshape_sizing_2026-06-14/{arm}/` cells:
per-arm `ensemble_hedged_ledger.csv`, `volume_event_best_monthly.csv`,
`volume_event_research_report.json` (R1-compatible); `size_mult_{arm}_{venue}.csv`
(per-entry multiplier); `stage5_summary.{json,md}`, `stage5_metrics.csv`,
`gross_exposure.csv`, `concentration.csv`, `config_hashes.json`, `code_hash.txt`.

## Post-run results

Run UTC 2026-06-14, both venues, window `2023-04-01 <= signal_ts < 2026-05-01`,
git HEAD `5dd4e12` (engine hook + Stage 5 code uncommitted; code hash `ccb3796c…`),
frozen forward config hash `1fc760f1…`, κ=0.25, clamp=2.0, prior-month causal
gross-normalizer. Artifacts
`~/SHARED_DATA/w5_continuous_stage5_pathshape_sizing_2026-06-14/`
(`stage5_summary.{json,md}`, `stage5_metrics.csv`, `gross_exposure.csv`,
`concentration.csv`; per-arm R1 ledgers under `reports/<tag>/{arm}/`).

**Z0 wiring sanity — PASS.** Z0 (size hook absent) reproduces the local Stage 0
ensemble exactly: bybit ret 0.7707 / MAR 4.748 / DD −5.27%; binance ret 0.6428 /
MAR 5.255 / DD −3.97% (identical to the Stage 0 rebuild on this data vintage; the
Stage 0 *receipt's* 0.7136 / 4.40 was the parallel-session vintage — a data-vintage
difference, not a wiring error).

| Venue | Arm | Return | MAR | MaxDD | Test-fold MAR | Gross ratio |
|---|---|---:|---:|---:|---:|---:|
| bybit | Z0 control | 0.7707 | 4.748 | −5.27% | 8.10 | 1.000 |
| bybit | Z2 pre24 | 0.8526 | 3.993 | −6.93% | 8.72 | 1.038 |
| bybit | Z2 combined | 0.7843 | 3.328 | −7.64% | 8.97 | 1.034 |
| bybit | Z6 symbol-id | 0.8697 | 5.392 | −5.23% | 8.75 | 1.006 |
| binance | Z0 control | 0.6428 | 5.255 | −3.97% | 6.64 | 1.000 |
| binance | Z2 pre24 | 0.7349 | 5.348 | −4.46% | 6.68 | 1.064 |
| binance | Z2 combined | 0.7491 | 5.141 | −4.73% | 6.97 | 1.067 |
| binance | Z6 symbol-id | 0.7043 | 5.284 | −4.32% | 6.45 | 1.012 |

Pooled ΔMAR vs Z0: Z2_pre24 **−0.331** (bybit −0.755, binance +0.093);
Z2_combined **−0.766**; Z6_symbol_identity **+0.337**. Symbol concentration (HHI,
top-5 share) barely moves for Z2 — not a concentration artifact.

## Verdict

**NULL — no arm admissible.** Sizing the same trades by the causal within-symbol
path-shape residual does NOT improve pooled MAR vs the frozen control; the failure
is multiply-determined:

1. **No risk-adjusted gain (DD worsens).** Z2 raises *return* on both venues (the
   tilt moves capital toward higher-expected-return trades, consistent with the
   Stage 7b signal) — bybit 0.771→0.853, binance 0.643→0.735 — but drawdown
   worsens *more* (bybit −5.27%→−6.93%, +31%), so bybit MAR collapses 4.748→3.993.
   Pooled ΔMAR −0.33. Buying return at a worse-than-proportional DD cost is not
   alpha.
2. **Beaten by the negative control.** Z6 (arbitrary symbol-identity sizing) has
   pooled ΔMAR **+0.337**, *stronger* than Z2 (bybit Z6 MAR 5.392 > Z0 4.748). The
   only positive sizing perturbation comes from symbol-level dispersion — a random
   per-symbol tilt catches high-return symbols as well or better than path-shape.
   Z2 carries no marginal sizing alpha over symbol identity. (Confirms the Stage 7
   lesson: per-symbol return dispersion is large enough that any symbol-correlated
   sizing perturbs MAR by ±0.6 — noise, not alpha.)
3. **Residual leverage on binance.** Even the prior-month causal normalizer left
   the realized binance gross ratio at 1.064–1.067 (outside ±5%) — feature uptrend
   plus per-component dup-weighting; the binance "return gain" is partly leverage.

The positive **test-fold (OOS) ΔMAR** (Z2_pre24 +0.33, Z2_combined +0.60) is noted
but non-decisive: it does not hold full-window, does not beat Z6, and rides on the
out-of-band binance gross.

**Conclusion for the path-shape lever:** the within-symbol path-shape signal is
*statistically real* (Stage 7b, IC ~0.10, p=0.001 both venues) but **not
harvestable** as risk-adjusted return through the two obvious engine levers —
entry *priority* (Stage 1 structural NULL: no within-cycle contention) and
*sizing* (this NULL: no MAR gain, beaten by the symbol-identity control). The IC
is too small and too entangled with drawdown to convert. Banked as a clean kill of
path-shape sizing; admissibility (Stage 7b) was necessary, not sufficient.
Falsifier outcome: **triggered** — does not beat the negative control and shows no
gross-neutral pooled-MAR gain. Next track: **Stage 8 regime-response** (independent
of path-shape).
