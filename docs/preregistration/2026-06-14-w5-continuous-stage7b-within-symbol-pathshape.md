# Pre-registration: W5 Continuous Stage 7b - Within-Symbol Path-Shape (FE rank-IC, permutation null)

**Date:** 2026-06-14
**Author:** Claude (W5 continuous signal-alpha loop)
**Stage:** run-pending
**Plan:** `docs/research_plans/w5_continuous_signal_alpha/08_stage7_path_shape_neutralized.md`
**Contract:** `docs/research_plans/w5_continuous_signal_alpha/00_methodology_contract.md`
**Supersedes the broken metric in:** Stage 7
(`docs/preregistration/2026-06-14-w5-continuous-stage7-path-shape-neutralized.md`,
registered-NULL on a noise-dominated tercile-spread gate).

## Why this stage exists

Stage 7 produced a **NULL on a methodology error**: its registered decision
statistic (cross-venue pooled tercile spread) is noise-dominated for this
heavy-tailed per-notional return cross-section — a 400-draw characterization on
the same test fold gave per-symbol-random spread SD = 128 bps (bybit) / 175 bps
(binance), 95% band ±240 / ±339 bps, so every measured spread sat inside the
null band. The robust rank-IC was clean (per-symbol-random IC SD 0.047/0.051):
path-shape residual IC = 0.22/0.21 cleared the per-symbol null by ~4–5 SD on
both venues and roughly doubled the symbol-hash control IC (0.13/0.09). That
signal is real **cross-sectionally**, but Stage 7 cannot distinguish "path-shape
predicts which *symbol* is profitable" (symbol selection, fragile/non-harvestable
forward, survivorship-adjacent) from "within a given symbol, higher-path-shape
entries earn more" (genuine, harvestable entry/sizing timing).

Stage 7b decides exactly that, with the correct statistic.

## Mechanism (locked before the run)

**Within-symbol fixed-effects rank-IC with a within-symbol permutation null.**
Within-symbol demeaning removes *all* symbol mix by construction (a per-symbol
constant — e.g. `symbol_hash_bucket` — has zero within-symbol variation, so its
within-symbol IC is identically zero). What survives is only the part usable for
timing/sizing a symbol you have already chosen to trade.

Population: identical to Stage 7 — executed/selected entries of the frozen
control, both venues, window `2023-04-01 <= signal_ts < 2026-05-01`, from the
Stage 0 candidate tape selected rows joined to the rebuilt component ledgers for
`return_per_notional = net_return / max(|notional_weight|, 1e-12)`. Same
chronological 60/40 walk-forward split (earliest 60% train, most recent 40%
test); all decisions on the **test fold**.

Procedure per feature `F` and venue:

1. **Composite removal (frozen, no leakage):** fit `F ~ 1 + composite` by OLS on
   the **train** fold, freeze `β̂`, set `F_resid = F − β̂·composite` on the test
   fold. (`composite` is the production score; this keeps Stage 7b strictly
   *marginal over the composite*.)
2. **Restrict to within-symbol-estimable trades:** keep test-fold trades whose
   `symbol` has **>= 2 test-fold trades** (a singleton has no within-symbol
   ordering). Report kept-event coverage.
3. **Within-symbol demean (centering):** subtract each symbol's test-fold mean
   from `F_resid` and from `return_per_notional`, giving `F_wd`, `R_wd`. (Centering
   defines "within-symbol"; the permutation null below makes the inference exact
   regardless of the centering source, so no return information leaks into the
   decision.)
4. **Observed statistic:** `IC_obs = Spearman(F_wd, R_wd)` pooled over kept
   test-fold trades, per venue.
5. **Within-symbol permutation null:** 1000× permute `F_wd` **within each symbol
   block** (shuffle a symbol's feature values among that symbol's own trades),
   recompute the pooled Spearman each draw → null distribution. One-sided
   p-value = fraction of null draws with `IC >= IC_obs` (for an expected-positive
   feature; symmetric for an expected-negative one, declared per feature below).

Expected sign (locked, from W4 Stage 3 + Stage 7, all positive cross-venue):
`pre_6h_return` +, `pre_24h_return` +, `pre_24h_realized_vol` +. The test is
one-sided in the declared direction; a same-magnitude effect in the *opposite*
direction is a falsification, not a pass.

## Arms (locked)

- `Q1_pre_6h_return`, `Q2_pre_24h_return`, `Q3_pre_24h_realized_vol`: each feature
  individually, within-symbol, marginal over composite.
- `Q_pair`: equal-weight average of the train-z-scored `F_resid` of
  `pre_6h_return` and `pre_24h_return`, then within-symbol-demeaned.
- `Q_combined`: equal-weight average of the train-z-scored `F_resid` of all three.
- `Q_symbol_hash` (degenerate control, reported): `symbol_hash_bucket` —
  identically zero within-symbol; its appearance as ~0 IC confirms the test
  removes symbol mix.

## Metrics (test fold, per venue)

- `IC_obs` (within-symbol residual rank-IC), one-sided permutation p-value,
  null mean/SD/97.5-pct;
- kept-event coverage and number of symbols with >= 2 trades;
- chronological-third sign stability of `IC_obs` within the test fold;
- tercile spread reported for continuity only — **explicitly not a decision
  statistic** (Stage 7 proved it is noise-dominated here);
- raw within-symbol IC (without composite removal) for context.

## Decision rule (a priori) / Pass bar

A feature/arm is **admissible** (path-shape is a usable within-symbol entry/sizing
feature) only if, on the within-symbol residual rank-IC, test fold:

1. both venues have **>= 500** kept events (symbols with >= 2 test trades);
2. `IC_obs` has the **same sign** on both venues, in the declared direction;
3. one-sided within-symbol permutation **p < 0.025 on BOTH venues** (observed
   clears the 97.5-pct of the within-symbol null);
4. **>= 2 of 3** chronological thirds of the test fold agree in `IC_obs` sign.

The combined/pair arms additionally must not be weaker than their best single
constituent (no free lunch from averaging noise).

Default label **`exploratory`** — admissibility only. An admissible feature then
feeds a downstream engine stage (Stage 1 A2/A3/A4 priority and/or Stage 5
`Z2_path_shape_size`) that must still beat the frozen control on **pooled MAR,
both venues**, net of funding and costs, before anything is a demo/paper
candidate. Admissibility here is necessary, not sufficient.

## Falsifier

Reject path-shape as a within-symbol entry/sizing feature if its within-symbol
residual IC is not significant (permutation p >= 0.025) on either venue, flips
sign across venues, reverses the declared direction, or lives in a single
chronological third. A significant *cross-sectional* IC (Stage 7) cannot rescue
an insignificant *within-symbol* IC — that would mean the effect is symbol
selection, which is not harvestable per-symbol and is survivorship-adjacent.

## Window, roots, universe

- Window `2023-04-01 <= signal_ts < 2026-05-01` (common full-PIT overlap).
- Roots read-only (`~/SHARED_DATA/{bybit,binance}_full_pit`); writes only to
  `~/SHARED_DATA/w5_continuous_stage7b_*`. Forward demo/paper untouched. Full-PIT
  universe mandatory; Stage 0 PIT gate re-asserted.

## Run command

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/w5_continuous_stage7b_within_symbol.py \
  --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
  --stage0 ~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14 \
  --permutations 1000 --seed 0 \
  --out ~/SHARED_DATA/w5_continuous_stage7b_within_symbol_2026-06-14
```

## Artifacts

Under `~/SHARED_DATA/w5_continuous_stage7b_within_symbol_2026-06-14/`:
`stage7b_within_symbol_ic.csv` (per venue/arm: IC_obs, p, null mean/SD/97.5,
coverage, n_symbols), `stage7b_thirds.csv`, `stage7b_events_{venue}.csv`,
`stage7b_summary.{json,md}` (root identity, code hash, frozen config hash, PIT,
per-arm admissibility, run label, falsifier outcome).

## Post-run results

Run UTC 2026-06-14, both venues, window `2023-04-01 <= signal_ts < 2026-05-01`,
git HEAD `5dd4e12` (code uncommitted; code hash `c5271543…`), frozen forward
config hash `1fc760f1…`, 1000 within-symbol permutations, seed 0. Coverage
(symbols with >= 2 test trades): bybit 1254 events / 201 symbols, binance 1155 /
190 (both >= the 500 floor). Artifacts
`~/SHARED_DATA/w5_continuous_stage7b_within_symbol_2026-06-14/`
(`stage7b_summary.{json,md}`, `stage7b_within_symbol_ic.csv`, `stage7b_thirds.csv`).

**Methodology correction (applied before interpreting, transparently).** The
locked mechanism pre-removed the composite by a *global* OLS, then within-symbol
demeaned. That contaminated the predeclared degeneracy control: because
`symbol_hash` is constant within a symbol, the global residual left only a
`−β̂·composite` term whose within-symbol part re-injected the composite, giving the
control a false IC (0.16 on binance, p=0.001) — a failed sanity check
(errors-we-never-repeat: a strange result is stop-work until explained). The fix
is to control for the composite *within-symbol* at the IC stage (partial Spearman
of within-symbol path-shape vs within-symbol return, controlling for within-symbol
composite). It is strictly *stricter*, leaves the pass bar unchanged, and restores
the control to degenerate (zero within-symbol variation → no IC). Path-shape
passed under BOTH the contaminated and the corrected statistic, so the fix removed
an inflation, it did not manufacture the result. The binding statistic is the
corrected within-symbol partial rank-IC.

Degeneracy control `Q_symbol_hash`: **degenerate on both venues** (zero
within-symbol variation) — the test correctly removes all symbol mix.

Within-symbol partial rank-IC over composite (test fold); per-venue permutation p
(1000 within-symbol shuffles); per-symbol IC null SD ≈ 0.033:

| Arm | bybit IC (p) | binance IC (p) | Decision |
|---|---|---|---|
| `pre_6h_return` | 0.082 (0.006) | 0.054 (0.020) | admissible |
| `pre_24h_return` | 0.110 (0.001) | 0.105 (0.001) | admissible (cleanest) |
| `pre_24h_realized_vol` | 0.097 (0.003) | 0.066 (0.009) | admissible |
| `Q_pair` (6h+24h ret) | 0.125 (0.001) | 0.093 (0.002) | admissible |
| `Q_combined` (3) | 0.115 (0.001) | 0.114 (0.001) | admissible (most balanced) |

All arms: same positive sign both venues, permutation p < 0.025 both, >= 2/3
chronological thirds same sign. Composite's own within-symbol IC (context): 0.074
(bybit) / 0.160 (binance) — the production score carries within-symbol information
and path-shape adds to it.

## Verdict

**ADMISSIBLE.** After removing ALL symbol mix (within-symbol fixed effects) AND
the production composite (within-symbol partial), causal path-shape still predicts
per-notional net return on both venues, with a properly-degenerate negative
control. This is the "Stage 3b" W4 promised and resolves the W4 symbol-mix
confound (the 97 bps `symbol_hash` spread that blocked W4): the effect is genuine
within-symbol timing, not symbol selection. Strongest single feature
`pre_24h_return` (IC 0.110/0.105, p=0.001 both); most cross-venue-balanced
`Q_combined` (0.115/0.114, p=0.001 both).

Run label stays `exploratory`. Admissibility is necessary, **not sufficient**: a
within-symbol IC of ~0.05–0.11 is modest and must convert to a **pooled-MAR**
improvement through the full engine before it is anything. The admissible residual
now feeds, under their own dated preregistrations:

- **Stage 5 `Z2_path_shape_size`** — score-weighted *sizing* at constant breadth
  (same entries, notional tilted by within-symbol path-shape). This is the
  stronger downstream test: the Stage 1 structural NULL showed entry *priority*
  has no room under the crowding gate, but sizing holds breadth fixed and uses the
  score.
- **Stage 1 A2/A3/A4** — path-shape candidate-priority arms (now unlocked), though
  the Stage 1 NULL means priority has limited room.

Falsifier outcome: **not triggered** — within-symbol IC significant on both
venues, same sign, control degenerate.
