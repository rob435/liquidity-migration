# Pre-registration: W5 Continuous Stage 8e - Regime-Hedge BTC-vol + Book-DD Blend

**Date:** 2026-06-15
**Author:** Claude (W5 continuous signal-alpha loop)
**Stage:** run-pending
**Plan:** `docs/research_plans/w5_continuous_signal_alpha/09_stage8_regime_response.md`
**Builds on:** Stage 8d
(`docs/preregistration/2026-06-15-w5-continuous-stage8d-regime-hedge-signals.md`) — found
BTC-vol and book-drawdown are COMPLEMENTARY regime signals: BTC-vol is robust on bybit
(binance fragile), book-DD is robust on binance (bybit breaks). Each fixes the other's
binding venue.

## Question

Does a single, both-venue-fixed blend of the two complementary causal signals — BTC
volatility and the book's own drawdown depth — give a regime-hedge that is robustly
positive on BOTH venues across the cost grid (the both-venue robustness neither signal
achieves alone)?

## Mechanism (locked before the run)

Same hedge mechanism (causal, mean-1, percentile hedge-intensity via the additive hook;
reuses Stage 0 components; V0 entries untouched; hedge-only ⇒ all trades kept). Signal:

- `blend_pct(d) = 0.5·btcvol_pct(d) + 0.5·bookdd_pct(d)` — equal, **single locked
  weighting for both venues** (no per-venue tuning). `btcvol_pct` = trailing-30d BTC-vol
  percentile (Stage 8); `bookdd_pct` = the combined book's running drawdown-depth
  percentile (Stage 8d), both causal, trailing-250 percentile.
- `intensity_raw(d) = 1 + λ(2·blend_pct(d) − 1)`, then **prior-calendar-month
  normalization** (causal — divide by the prior month's mean raw intensity; first month
  1.0) to enforce gross-neutrality (the book-DD percentile is skewed by drawdown ties, so
  the blend needs recentering; mean intensity reported and gated [0.95,1.05]).
- λ = 0.50 locked (λ=0.75 reported).

## Arms / grid

- `V0` control (frozen hedge) per venue.
- `BLEND` at λ ∈ {0.50, 0.75}, hedge `cost_bps` ∈ {5.0, 7.5, 10.0} (1.0×/1.5×/2.0×).
- `S_btcvol` baseline (λ=0.5) for reference.
- `R5_hash` negative control per cost.

## Decision rule (a priori)

The blend is a **robust regime-hedge candidate that supersedes BTC-vol** iff, at λ=0.5:

1. pooled MAR delta `> 0` on **both venues** at 1.0× cost;
2. pooled MAR delta `> 0` on **both venues** at **1.5× cost** (the stress BTC-vol fails);
3. beats the R5 hash control at every cost;
4. mean intensity ∈ [0.95, 1.05] (gross-neutral);
5. keeps all trades (true by construction).

If 1–5 hold → the blend is the robust forward-watch candidate (demo/paper; Tier-3
real-money gate unchanged); record the cost headroom. If it fails (e.g. the blend dilutes
both signals and goes negative on a venue), bank that and pursue a cross-sectional
dispersion signal / regime-conditioned sizing instead. No per-venue weighting, no
threshold moved.

## Falsifier

Not a candidate if negative on either venue at 1.0× cost, fails the 1.5×-cost stress on a
venue, is matched by R5, needs mean intensity outside [0.95,1.05], or only one venue
carries it.

## Window / roots / run

Window `2023-04-01 <= signal_ts < 2026-05-01`; reuses Stage 0 components; read-only roots;
writes only to `~/SHARED_DATA/w5_continuous_stage8e_*`. No engine backtests.

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/w5_continuous_stage8e_regime_hedge_blend.py \
  --venues bybit,binance --stage0-tag w5_continuous_stage0_candidate_tape_2026-06-14 \
  --out ~/SHARED_DATA/w5_continuous_stage8e_regime_hedge_blend_2026-06-15
```

## Post-run results

Run UTC 2026-06-15, both venues, reuses Stage 0 components, code hash `…`. V0 MAR bybit
4.748 / binance 5.255. BTC-vol reference reproduces +0.078/+0.038/+0.005; R5 hash
−0.614/−0.697/−0.763. Blend per-venue MAR delta (λ=0.5), cost 1.0×/1.5×/2.0×:

| Venue | 1.0× | 1.5× | 2.0× | mean int (1×) |
|---|---:|---:|---:|---:|
| bybit | +0.116 | +0.005 | −0.018 | 1.032 |
| binance | **−0.061** | −0.115 | −0.168 | 1.025 |

Pooled λ0.5: +0.028 (1×), −0.055 (1.5×). (λ=0.75 mean intensity drifts to ~1.05–1.06,
out of band — the DD skew amplifies at higher λ.)

## Verdict

**NULL — the blend DILUTES rather than combines.** binance goes **−0.061 at 1× cost,
worse than EITHER component alone** (book-DD +0.268, BTC-vol +0.049). Averaging the two
percentiles under-hedges exactly when binance needs protection (book in drawdown but BTC
calm → a moderate blend instead of book-DD's full response), so the 50/50 average destroys
book-DD's binance benefit. bybit stays roughly BTC-vol-like. The blend is strictly worse
than BTC-vol alone.

**Regime-hedge signal exploration conclusion (Stages 8/8b/8c/8d/8e, thorough):** the
**continuous BTC-vol regime-hedge (Stage 8c) is the best and the standing candidate** —
robust to λ, beats the hash control by +0.6–0.8, both-venue-positive at realistic cost
(bybit robust at all costs, binance thin: +0.049 at 1×, breaks ~1.2×). Book-DD is
venue-split (robustly fixes binance at ALL costs but breaks bybit — opposite-sign effect
by venue, unusable alone, no single-venue claims). Book-vol and the BTC+book-vol
multifactor are counterproductive. The complementary BTC-vol/book-DD pair does not combine
cleanly via averaging (max/multiplicative combiners would be combination-mining — not
pursued). No threshold moved. **Next: pivot to a genuinely different lever** (cross-sectional
alt-dispersion regime; regime-conditioned sizing; Stage 2 entry-style; Stage 4 sniper),
keeping the BTC-vol regime-hedge as the established forward-watch candidate.
