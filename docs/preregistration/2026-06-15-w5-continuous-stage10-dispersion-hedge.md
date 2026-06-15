# Pre-registration: W5 Continuous Stage 10 - Cross-Sectional Dispersion Regime Hedge

**Date:** 2026-06-15
**Author:** Claude (W5 continuous signal-alpha loop)
**Stage:** run-pending
**Plan:** `docs/research_plans/w5_continuous_signal_alpha/09_stage8_regime_response.md`
**Builds on:** Stage 8c (BTC-vol regime-hedge candidate, thin binance cost headroom),
Stage 8d/8e (book-DD fixes binance but is venue-split; signals don't blend).

## Question

Does a regime signal built from **cross-sectional alt-return dispersion** — a market-
structure measure of squeeze risk — give a regime-hedge with more MAR margin over the
hedge cost on BOTH venues (firming binance where BTC-vol is thin)? Dispersion is a market
property (alts moving together / spreading), so unlike the book's own drawdown it may be
both-venue-consistent rather than venue-split.

## Mechanism (locked before the run)

Same hedge mechanism (causal mean-1 percentile `hedge_intensity = 1 + λ(2·pct − 1)` via
the additive hook; reuses Stage 0 components; V0 entries untouched; hedge-only ⇒ all
trades kept). Signal:

- daily cross-sectional dispersion `disp(d)` = std across the PIT fade-candidate universe
  of each symbol's day-`d` return, from the per-venue engine panel
  `_continuous_engine_panel_v2_rmom25_feat4a91acf4_616aea03.parquet` (cols symbol/ts_ms/ret1;
  daily return = compounded hourly `ret1`).
- regime signal `S(d)` = trailing-10-day mean of `disp` ending at **d−1** (causal — uses
  only dispersion realized strictly before day d).
- `pct(d)` = trailing-250 percentile of `S`; `intensity(d) = 1 + λ(2·pct − 1)` — hedge
  MORE in high dispersion. λ = 0.50 locked.

## Arms / grid

- `V0` control (frozen hedge) per venue.
- `DISP` regime hedge, hedge `cost_bps` ∈ {5.0, 7.5, 10.0} (1.0×/1.5×/2.0×).
- `S_btcvol` baseline (reference) + `R5_hash` negative control per cost.

## Decision rule (a priori)

The dispersion signal **firms the candidate / supersedes BTC-vol** iff, at λ=0.5:

1. pooled MAR delta `> 0` on **both venues** at 1.0× cost;
2. pooled MAR delta `> 0` on **both venues** at **1.5× cost** (the stress BTC-vol fails on
   binance);
3. beats the R5 hash control at every cost;
4. mean intensity ∈ [0.95, 1.05].

If 1–4 hold → dispersion is the more-robust regime-hedge candidate (record cost headroom);
a BTC-vol+dispersion blend is considered only if dispersion is itself both-venue-robust.
If it fails the binance 1.5× stress like BTC-vol, the **BTC-vol regime-hedge stands as the
final in-sample candidate** and the path is forward-watch (operator-gated); keep generating
distinct mechanisms. No threshold moved; banked honestly. Single-venue is rejected.

## Falsifier

Not an improvement if negative on either venue at 1.0×, fails the binance 1.5× stress,
matched by R5, mean intensity out of band, or one venue carries it.

## Window / roots / run

Window `2023-04-01 <= signal_ts < 2026-05-01`; reuses Stage 0 components + the per-venue
engine panel; read-only roots; writes only to `~/SHARED_DATA/w5_continuous_stage10_*`. No
engine backtests.

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/w5_continuous_stage10_dispersion_hedge.py \
  --venues bybit,binance --stage0-tag w5_continuous_stage0_candidate_tape_2026-06-14 \
  --out ~/SHARED_DATA/w5_continuous_stage10_dispersion_hedge_2026-06-15
```

## Post-run results

Run UTC 2026-06-15, both venues, reuses Stage 0 components + the per-venue v2 rmom25
panel, code hash `…`. V0 MAR bybit 4.748 / binance 5.255. BTC-vol reference reproduces
+0.078/+0.038/+0.008; R5 hash −0.614/−0.697/−0.763. Dispersion per-venue MAR delta vs V0,
cost 1.0×/1.5×/2.0×:

| Venue | 1.0× | 1.5× | 2.0× |
|---|---:|---:|---:|
| bybit | +0.082 | +0.057 | +0.032 |
| binance | **−0.273** | −0.329 | −0.385 |

Pooled: −0.095 / −0.136 / (neg). **Mean dispersion intensity 1.139 (bybit) / 1.167
(binance) — OUT OF BAND:** the dispersion percentile is right-skewed (like book-DD ties),
so without the prior-month normalization the intensity over-hedges ~15% (a gross-neutrality
/ mechanism defect — criterion 4 fails).

## Verdict

> **⚠️ BINANCE SIGN CORRECTED by Stage 10b (2026-06-15).** The binance −0.273 below was a
> GROSS-NEUTRALITY BUG (the prior-month normalization was omitted → ~15% over-hedge). Clean,
> dispersion is binance **+0.293** (a sign flip), and it is a ROBUST binance hedge regime
> (beats hash + constant-level controls at every λ/cost; Stage 10b
> `…stage10b-dispersion-clean.md`). The "venue-split" conclusion below STANDS but its polarity
> reverses: dispersion is **binance-real / bybit-NOISE** (bybit +0.04 is weak and fails the
> hash control at λ0.25 + is negative in 2/3 sub-periods), not bybit-good/binance-bad. So it is
> still not a both-venue candidate; for a binance sleeve it is a real hedge. The
> gross-defect-driven numbers below are superseded by Stage 10b.

**[SUPERSEDED binance sign — see Stage 10b] NULL — dispersion is venue-split (helps bybit, HURTS
binance) and not gross-neutral.**
bybit +0.082 (≈ BTC-vol), binance **−0.273** (the opposite of the goal — dispersion makes
binance worse, not better). The over-hedge (mean intensity ~1.15) inflates the harm, but
even a gross-corrected version cannot flip binance's clear negative sign. So dispersion
does not firm binance.

**Regime-signal search EXHAUSTED — clean conclusion:** every signal helps exactly one
venue except BTC-vol: BTC-vol (bybit-robust, binance-thin-but-positive at 1×), book-DD
(binance-robust, bybit-broken), dispersion (bybit-positive, binance-broken), book-vol /
regime-sizing (both negative). **BTC-vol is the UNIQUE both-venue-positive regime-hedge
signal**, and its thin binance cost headroom is intrinsic (no signal fixes binance without
breaking bybit). The **BTC-vol regime-hedge (Stage 8c) is the FINAL in-sample regime
candidate**, and it **qualifies under the operator's bar** (robust at realistic 1× hedge
cost: both venues +, λ-robust, beats control +0.6–0.8, keeps trades); the 1.5×-cost stress
on binance is the lone caveat. Falsifier: triggered (single-venue, gross defect). Next: one
final cost-headroom attempt (EMA-smoothed intensity to cut hedge turnover, firming the
*stress* headroom); else the candidate stands and pivot to a genuinely different lever
(Stage 2 entry-style / Stage 4 sniper).
