# Pre-registration: W5 Continuous Stage 8d - Regime-Hedge Signal Comparison

**Date:** 2026-06-15
**Author:** Claude (W5 continuous signal-alpha loop)
**Stage:** run-pending
**Plan:** `docs/research_plans/w5_continuous_signal_alpha/09_stage8_regime_response.md`
**Builds on:** Stage 8c
(`docs/preregistration/2026-06-15-w5-continuous-stage8c-regime-hedge-robustness.md`) —
the BTC-vol regime-hedge is a candidate but binance cost headroom is thin (breaks even
~1.2×; −0.011 at 1.5× cost).

## Question

Does a regime signal tied to the **book's own risk** (its trailing volatility / drawdown,
or a multifactor blend) predict the fade book's drawdowns better than BTC volatility,
giving more MAR margin over the hedge turnover cost — specifically, getting **binance
positive at 1.5× cost** (where BTC-vol fails)?

## Mechanism

Identical hedge mechanism to Stage 8 (causal, mean-1, percentile-ranked
`hedge_intensity = 1 + λ(2·pct − 1)`, applied via the additive hook; reuses Stage 0
components; V0 entries untouched; hedge-only so all trades kept). Only the **signal**
feeding the percentile changes. All signals causal (use only data strictly before the
sized day), percentile-ranked over a trailing 250-day window (30-day measurement window),
matching Stage 8.

Signals (each → a mean-1 intensity; locked):

- `S_btcvol` (baseline = Stage 8): trailing 30d std of BTC daily hedge returns.
- `S_bookvol`: trailing 30d std of the **combined pre-hedge book daily returns**
  (`combine_continuous_components(pieces, frozen weights).raw_by_day`), causal.
- `S_bookdd`: the combined book's running **drawdown depth** (peak − equity of the
  cumulative raw book return), evaluated as-of the prior day (causal).
- `S_multifactor`: mean of the `S_btcvol` and `S_bookvol` percentile series, then intensity.

## Arms / grid (locked)

- `V0` control (frozen hedge) per venue.
- each signal at λ ∈ {0.50, 0.75}, hedge `cost_bps` ∈ {5.0, 7.5, 10.0} (1.0×/1.5×/2.0×).
- `R5_hash` negative control (hash-week regime, λ=0.5) per cost.

## Metrics

- pooled + per-venue MAR delta vs V0 for each (signal, λ, cost); mean intensity (∈[0.95,1.05]);
- `S_btcvol` must reproduce Stage 8c at 1× (sanity: pooled ~+0.078 at λ=0.5) — else stop.

## Decision rule (a priori)

A signal is a **strictly more robust regime-hedge candidate than BTC-vol** iff, at λ=0.5:

1. pooled MAR delta `> 0` on **both venues** at 1.0× cost;
2. pooled MAR delta `> 0` on **both venues** at **1.5× cost** (the binance-binding stress
   BTC-vol fails);
3. beats the R5 hash control at every cost;
4. mean intensity ∈ [0.95, 1.05].

If any signal satisfies 1–4, it **supersedes BTC-vol as THE forward-watch candidate**;
record the robust λ range + cost headroom. If none does, BTC-vol stays the (thin-headroom)
candidate and the next step is a cross-sectional alt-dispersion signal or a
regime-conditioned sizing/hedge combination. Report all honestly; do not move thresholds.

## Falsifier

A signal is not an improvement if it is negative on either venue at 1.0× cost, fails the
1.5×-cost stress on binance, is matched by R5, or needs mean intensity outside [0.95,1.05].
A signal that only helps one venue is rejected (no single-venue claims).

## Window / roots / run

Window `2023-04-01 <= signal_ts < 2026-05-01`; reuses Stage 0 components; read-only roots;
writes only to `~/SHARED_DATA/w5_continuous_stage8d_*`. No engine backtests.

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/w5_continuous_stage8d_regime_hedge_signals.py \
  --venues bybit,binance --stage0-tag w5_continuous_stage0_candidate_tape_2026-06-14 \
  --out ~/SHARED_DATA/w5_continuous_stage8d_regime_hedge_signals_2026-06-15
```

## Post-run results

Run UTC 2026-06-15, both venues, reuses Stage 0 components, code hash `4d16454e…`. V0
MAR bybit 4.748 / binance 5.255. **Sanity: S_btcvol (λ0.5, 1×) pooled = +0.078** — exactly
reproduces Stage 8c, so the signal replication is correct. Artifacts
`~/SHARED_DATA/w5_continuous_stage8d_regime_hedge_signals_2026-06-15/`.

Per-venue MAR delta vs V0 (λ=0.5), cost 1.0×/1.5×/2.0×:

| Signal | bybit | binance |
|---|---|---|
| S_btcvol | +0.108 / +0.087 / +0.075 | +0.049 / −0.011 / −0.060 |
| S_bookvol | −0.392 / −0.106 / −0.130 | −0.509 / −0.565 / −0.621 |
| S_bookdd | −0.241 / +0.099 / −0.058 | **+0.268 / +0.210 / +0.153** |
| S_multifactor (BTC+bookvol) | −0.102 / −0.122 / −0.144 | −0.277 / −0.328 / −0.379 |

R5 hash control pooled: −0.614 / −0.697 / −0.763.

## Verdict

**None of the four signals is robust through 1.5× cost on both venues — but a clear,
useful mechanistic finding emerges.** `S_bookvol` (book trailing vol) is decisively
counterproductive (−0.4 to −0.6 both venues — book vol is high when the book is fine, so
it mis-times the hedge), and the BTC+book-vol multifactor inherits that. BUT
**`S_bookdd` (book drawdown depth) is robustly positive on BINANCE at every cost**
(+0.268/+0.210/+0.153 — fixing exactly the venue where BTC-vol is fragile) while it
*breaks bybit* (−0.241 at 1×). So **BTC-vol and book-DD are COMPLEMENTARY**: BTC-vol is
robust on bybit, book-DD is robust on binance; each is strong on the other's binding
venue. (Caveat: `S_bookdd` mean intensity drifted to 1.055 — the DD percentile is skewed,
needs mean-1 recentering for a clean gross-neutral blend.)

**Next (the lead):** a **BTC-vol + book-DD blend** (the two complementary signals,
mean-corrected for the DD skew, single locked weighting for both venues — no per-venue
tuning). If the blend is positive both venues across the cost grid, it supersedes BTC-vol
as the robust forward-watch candidate. Registered as Stage 8e. `S_bookvol` /
`S_multifactor(BTC+bookvol)` are closed (counterproductive). No threshold moved; banked
honestly.
