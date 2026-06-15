# Pre-registration: W5 Continuous Stage 8g - Aggregate-Funding Hedge Regime

**Date:** 2026-06-15
**Author:** Claude (W5 continuous signal-alpha loop)
**Label:** `exploratory` (component reuse, no engine)
**Builds on:** Stage 8c (BTC-vol regime-hedge candidate) + 8d/8e (hedge-signal search:
BTC-vol unique among book-vol/book-DD/dispersion/multifactor). This adds the last untested
hedge-regime signal.

## Question

Is market-wide **aggregate funding** a better/complementary squeeze-risk hedge regime than
BTC-vol? The fade shorts high-funding pumps; when the WHOLE market's funding is extreme
(crowded longs everywhere), squeeze risk should peak → hedge more.

## Mechanism (locked)

Causal daily market-wide aggregate funding = mean `funding_rate_8h_equiv` across all symbols
that day (binance 8h-equiv derived from rate×480/interval); trailing-30d mean → trailing-250
percentile → mean-1 intensity `1+λ(2pct−1)`, λ=0.5 (hedge more when high). Component reuse via
`build_full_ledger`; compared vs C0 control, the BTC-vol regime (Stage 8c), and a hash-regime
control, both venues, hedge cost {5,7.5,10} bps. Better iff beats C0 + hash on both venues @1×
AND beats/firms BTC-vol.

## Post-run results

| Venue | cost | C0 | H_funding | H_btcvol | H_hash | fund−C0 | fund−btcvol |
|---|---:|---:|---:|---:|---:|---:|---:|
| bybit | 1× | 4.748 | 4.686 | 4.856 | 4.443 | −0.061 | −0.169 |
| bybit | 2× | 4.709 | 4.653 | 4.822 | 4.309 | −0.057 | −0.170 |
| binance | 1× | 5.255 | 5.285 | 5.303 | 4.332 | +0.030 | −0.019 |
| binance | 2× | 5.161 | 5.209 | 5.195 | 4.167 | +0.048 | +0.014 |

Pooled ΔMAR vs C0: funding **−0.016/−0.010/−0.004** (negative all costs) vs BTC-vol
+0.078/+0.071/+0.073. Mean funding intensity 0.936/0.933 (NOT mean-1 — a gross defect: the
trailing-mean→percentile of trending funding averages below 0.5).

## Verdict

**NULL — hedge-signal space CLOSED.** The aggregate-funding regime fails to beat the frozen
control on bybit (4.686 < 4.748), is worse than BTC-vol on both venues (−0.169 / −0.019 @1×),
and is pooled-negative; only marginally positive on binance. (The mean-intensity defect
inflates the harm slightly, but bybit's clear negative sign is not a gross artifact.) Funding
is real crowding info but the BTC-vol regime already captures squeeze-timing better. **BTC-vol
is the unique both-venue hedge regime across all six signals tested (BTC-vol, book-vol,
book-DD, dispersion, multifactor, aggregate-funding).** The BTC-vol regime-hedge (Stage 8c)
is the converged deliverable — a modest sub-period-variable tail-insurance overlay (Stage 8f).
