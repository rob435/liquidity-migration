# Pre-registration: Gap-blind → calendar-exact features

**Date:** 2026-06-14
**Author:** Claude (audit flagged-items remediation; operator-approved 2026-06-14)
**Stage:** run-pending
**Findings:** research-methodology-2, pit-signals-3, pit-signals-4
**Template:** `docs/preregistration/_template.md`
**Methodology gate:** `docs/backtesting_errors_we_never_repeat.md` (no look-ahead /
no horizon-mislabel correctness class), `docs/pit_gate.md`.

## What's changing

Route the N-day daily feature builders in `liquidity_migration/signal_harness.py`
and `compute_btc_beta` in `liquidity_migration/risk_model.py` through the
calendar-exact primitives `_common.calendar_shift` / `_common.calendar_roll`
instead of positional `shift(n)` / `rolling_*(window_size=n)` on the sparse daily
`(symbol, ts_ms)` panel, so an "N-day" feature always spans exactly N calendar
days (or nulls/shrinks) rather than reaching the N-th *present* row across a
delist/relist/data-hole gap.

This is a correctness fix, not a parameter sweep: no knob value, window length,
`min_samples`, weight, gate, or threshold changes. Only the *windowing
semantics* of already-frozen features change, and only on gapped `(symbol, day)`
cells. The `calendar_shift` / `calendar_roll` primitives already exist and are
tested in HEAD (`liquidity_migration/_common.py:105-145`); this change is purely
the call-site routing.

## Exact files / knobs touched

Both files are modified in the working tree (uncommitted). Line ranges are from
the current working-tree revision.

`liquidity_migration/signal_harness.py` (import `_common.MS_PER_DAY` →
`MS_PER_DAY, calendar_roll, calendar_shift` at L39):

- `_make_xs_rank_ret_Nd` (L320-321): `close / close.shift(n).over(symbol)` →
  `close / calendar_shift(close, n)` — feeds `xs_rank_ret_Nd`
  (incl. the deployed `xs_rank_ret_30d`).
- `_build_liquidity_rank` (L347-351): `turnover_quote.rolling_mean(window_size=7)`
  → `calendar_roll(turnover_quote, "mean", 7, shifted=False, min_samples=1)`
  (trailing 7d incl. today) — feeds `liquidity_rank` and its delta.
- `_make_liquidity_rank_delta` (L374-379): `liquidity_rank.shift(n)` →
  `calendar_shift(liquidity_rank, n)`.
- `_make_turnover_delta` (L398-404): `turnover_quote.shift(1).rolling_mean(window_size=n)`
  → `calendar_roll(turnover_quote, "mean", n, shifted=True, min_samples=1)`
  (prior N days excl. today) — feeds `turnover_delta_Nd`.
- `_build_funding_rate_delta_7d` (L441-457): trailing-7d
  `funding_rate_1d_sum.rolling_sum(7)` → `calendar_roll(..., "sum", 7,
  shifted=False, ...)`; and the 7-day-prior compare
  `funding_7d_sum.shift(7)` → `calendar_shift(funding_7d_sum, 7)` — feeds
  `funding_rate_delta_7d`.
- `_build_oi_delta_7d` (L467-477): `oi.shift(7)` → `calendar_shift(oi, 7)`; 30d ADV
  `turnover_quote.rolling_mean(window_size=30, min_samples=5)` →
  `calendar_roll(..., "mean", 30, shifted=False, min_samples=5)` — feeds
  `oi_delta_7d`.
- `_build_oi_to_adv` (L499-503): same 30d-ADV `rolling_mean` →
  `calendar_roll(..., "mean", 30, shifted=False, min_samples=5)`.
- `_build_realized_vol_7d` (L536-543): `ret_1d.rolling_std(window_size=7,
  min_samples=3)` → `calendar_roll(ret_1d, "std", 7, shifted=False,
  min_samples=3)` — feeds `realized_vol_7d` (also the inverse-vol sizing
  denominator).
- `_build_vol_of_vol_30d` (L558-563): `ret_1d.abs().rolling_std(window_size=30,
  min_samples=10)` → `calendar_roll(ret_1d.abs(), "std", 30, ...)`.
- `_build_range_extension_30d` (L597-603): `range_1d.shift(1).rolling_mean(
  window_size=30, min_samples=5)` → `calendar_roll(range_1d, "mean", 30,
  shifted=True, min_samples=5)`.
- `_build_dist_from_30d_high` (L622-627): `high.rolling_max(window_size=30,
  min_samples=5)` → `calendar_roll(high, "max", 30, shifted=False,
  min_samples=5)` — feeds `dist_from_30d_high`.
- `_build_dist_from_30d_low` (L646-651): `low.rolling_min(window_size=30,
  min_samples=5)` → `calendar_roll(low, "min", 30, shifted=False,
  min_samples=5)`.

`liquidity_migration/risk_model.py` (import `_common.MS_PER_DAY` →
`MS_PER_DAY, calendar_roll` at L28):

- `compute_btc_beta` (L102-117): the four OLS rolling moments
  `_ex/_ey/_exy/_eyy = {ret_1d, _btc_ret, _xy, _yy}.rolling_mean(window_size=window,
  min_samples=min_periods).over("symbol")` → `calendar_roll(..., "mean", window,
  shifted=False, min_samples=min_periods).over("symbol")`. Note `compute_btc_beta`
  already drops null-`ret_1d` rows before the roll, so a row-based window on the
  surviving rows would stretch the "window-day" beta across >window calendar days
  on a gapped symbol; the calendar window shrinks/nulls instead. This produces
  `btc_beta`, one of the four `COMMON4` factors
  (`scripts/precompute_residual_momentum.py:66`).

Note: the same working-tree change set also touches
`build_combined_signal_portfolio` (ordinal rank / absolute-k decile,
research-methodology-4) lower in `signal_harness.py`. That ranking/deploy-count
change is OUT OF SCOPE for this receipt and is covered separately; it does not
alter any feature value and is on the long-only combined-portfolio path, not the
continuous control's selection path exercised by the decision rule below.

## Hypothesis

Positional `shift(n)` / `rolling(window_size=n)` count present rows, not calendar
days. On a delist→relist or archive-hole `(symbol, day)` gap the N-th present row
is more than N calendar days back, so the feature silently measures the wrong
lookback (e.g. a "7d" liquidity-rank delta computed over 14+ calendar days — the
same gap-blindness that `_attach_daily_returns` / `_attach_forward_returns`
already avoid). `calendar_shift` nulls a row whose exact D-n partner is absent;
`calendar_roll` (`rolling_*_by` over the 00:00-UTC `ts_ms` grid) shrinks the
window to the true span (and nulls under `min_samples`). For a **contiguous**
daily series every partner ts is exactly `n*MS_PER_DAY` back, so both primitives
are numerically identical to the positional form — the change is a no-op except
on gapped symbols.

## Predicted direction + magnitude

- **Contiguous symbols:** byte-identical feature values (`np.allclose`, NaN/null
  positions matching). No change to any non-gapped name's features, OOF IC,
  selection, sizing, or ledger.
- **Gapped symbols only:** the affected feature cells re-base to the true
  calendar horizon (some go null/shrink across the gap). These flow into:
  the ridge OOF rank-IC and the univariate-IC feature-survival rule
  (`ridge_combiner`, fold-IC selection at `ridge_combiner.py:165`), and into
  `residual_momentum` via the `COMMON4` factor set
  (`btc_beta` + `xs_rank_ret_30d`), which drives the deployed continuous **rmom
  q25** gate.
- **Predicted headline effect:** within tolerance of the documented control —
  gapped names are a small minority of the cross-section. A *large* headline
  shift would mean gapped names materially drive selection and must be
  investigated before any promotion claim.
- **Failure mode if hypothesis wrong:** if contiguous symbols are NOT
  byte-identical, the primitive (or its sort/grid assumption) is wrong — that is
  a correctness regression, not a finding, and the change is reverted, not
  re-interpreted.

## Roots that will be touched

- [x] bybit_full_pit (per-venue working dataset) — feature panel recompute
- [x] binance_full_pit (per-venue working dataset) — feature panel recompute
- [x] forward demo/paper (always, by virtue of being live) — once committed the
      live continuous feature build uses the calendar-exact path

## Decision rule (a priori)

Confirmatory **continuous** backtest, **both venues**, over the W5 Stage-0 window
`2023-04-01 <= signal_ts < 2026-05-01` (exclusive), against the documented frozen
ensemble-hedged control:

- bybit ret **0.714** / MAR **4.40**
- binance ret **0.675** / MAR **5.53**

(Control numbers per the W5 Stage-0 candidate-tape receipt,
`docs/preregistration/2026-06-14-w5-continuous-stage0-candidate-tape.md`.)

**Accept** (correctness fix, no behavioral surprise): on both venues the headline
ret and MAR stay within tolerance of the control and the **return sign does not
flip** vs control — consistent with the calendar-exact path being a near no-op on
this universe. This is the expected outcome for a correctness fix and triggers no
Tier reclassification (no MAR-delta claim is made; the fix is not promotion
evidence).

**Investigate / do NOT promote:** a **material** shift in ret or MAR on either
venue (beyond contiguous-equivalence tolerance), or a return sign flip vs control,
means gapped names materially affect selection. That is a substantive change in
behavior, not a transparent fix — it must be diagnosed (which gapped symbols, how
many selection rows changed) before the change is accepted, and it cannot be
cited as alpha or promotion evidence under the STATE.md three-tier gate (forward
demo/paper is the arbiter; Tier-3 real-money stays strict and unmet).

Equivalence on contiguous data is the binding correctness check:
per-symbol `np.allclose` of the recomputed feature panel vs the positional
panel, with NaN/null positions matching, on the contiguous-symbol subset — the
repo standard for performance/refactor numerical equivalence (AGENTS.md), not
bit-identical output.

## Run command

```bash
# 1. Recompute the per-venue feature panels with the calendar-exact builders
#    (both roots), then the COMMON4 / residual_momentum signal.
POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/precompute_residual_momentum.py \
  --root "$BYBIT_FULL_PIT_ROOT" --root "$BINANCE_FULL_PIT_ROOT" \
  --start 2023-04-01 --end 2026-05-01

# 2. Confirmatory continuous control backtest, both venues, W5 Stage-0 window
#    (frozen continuous_ensemble_v1 control; same harness as the Stage-0 receipt).
#    Compare headline ret/MAR to the control numbers above.
.venv/bin/python -m pytest -q     # primitive-equivalence + harness tests gate first
# (continuous control rebuild + per-venue headline compare per the W5 Stage-0
#  reconstruction; window 2023-04-01 <= signal_ts < 2026-05-01 exclusive)
```

## Post-run results

**Stage 1 — residual_momentum re-base (bybit, 2026-06-14, EXPLORATORY confirmatory).**
Rebuilt `residual_momentum.parquet` with the gap-fix over `2023-04-01..2026-06-03`
(POLARS_MAX_THREADS=8 precompute) and diffed against the pre-fix table (2026-06-03,
backed up to `/tmp/rmom_bybit_prefix.parquet`; the deployed table was RESTORED unchanged
afterward — this was a measurement, not a deploy):

- overlap rows 444,195; **changed (>1e-9): 94.6%**, but the change is overwhelmingly
  microscopic — **median |Δ| = 1.05e-05, mean 1.39e-04, p99 2.33e-3**. Only **0.16%**
  of rows (703) have |Δ| > 1e-2 (max 0.89).
- The pervasive 1e-5 shift is the expected cross-sectional cascade: `btc_beta` is a
  COMMON4 factor, so correcting a few gapped symbols' exposures re-fits each day's OLS
  and nudges every residual that day at the last-decimal level (alpha-neutral).
- The MATERIAL changes are concentrated on genuinely gapped names — FHEUSDT (the cited
  54-day archive gap), TNSRUSDT, XCNUSDT, XIONUSDT, 1000BONKUSDT … — i.e. exactly the
  delist/relist symbols the fix is designed to correct. 6,354 rows dropped (gapped-day
  nulls, correct), 18 added.

Interpretation: the deployed `rmom q25` signal is essentially unchanged (1e-5) except on
gapped names it was previously mis-computing. This supports the fix as a correctness
re-base, not a broad alpha shift. STILL PENDING for full ACCEPT: the continuous-backtest
headline (both venues) vs the W5 Stage-0 control, to confirm the q25-gated SELECTIONS and
ret/MAR stay within tolerance (any change should trace to the corrected gapped names).

## Verdict

PENDING the continuous-backtest headline check. Stage-1 rmom diff is consistent with a
correctness re-base (microscopic everywhere except corrected gapped symbols).
