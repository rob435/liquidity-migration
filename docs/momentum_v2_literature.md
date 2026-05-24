# Momentum v2 — Literature Synthesis

The v1 event-driven design (Clenow 90-day slope×R² + coil-release + breakout)
produced 5–33 trades over 3 years on the canonical research root, which is too
sparse to even evaluate properly. v2 starts from the academic crypto-momentum
literature directly.

## What the papers actually say

### Liu & Tsyvinski (2021, RFS) — "Risks and Returns of Cryptocurrency"

The canonical crypto momentum paper.

- **Formation periods that work in crypto: 1 week, 2 weeks, 4 weeks.** Not the
  3-12 months of equities.
- **Weekly rebalance.** Long-short top vs bottom quintile.
- **Significant alpha at the 1-week formation period** (their Table 6) — the
  long–short portfolio earns ~3% per week before costs.
- Effect is **strongest in the largest coins** (consistent with equity Asness
  1997 — momentum is a liquid-market phenomenon).
- They use simple cumulative returns, not slope×R². No need for Clenow
  machinery in crypto where the formation window is short.

### Liu, Tsyvinski & Wu (2022, JF) — "Common Risk Factors in Cryptocurrency"

- Three-factor model: market, size, momentum (CMOM).
- CMOM factor: long top quintile by 1-week return, short bottom quintile.
  Weekly rebalance.
- Their CMOM annualized Sharpe ratio in their sample is ~0.7 (after costs).

### Bianchi, Babiak, Dickerson (2023) — "Trading volume and liquidity provision in cryptocurrency markets"

- Confirms cross-sectional momentum at weekly horizon.
- Time-series momentum stronger at monthly horizon.
- Combining the two improves risk-adjusted returns vs either alone.

### Hurst, Ooi & Pedersen (2017, JPM) — "A Century of Evidence on Trend-Following Investing"

- **Time-series momentum signal:** `sign(t-month_return)`. Long if up, short
  if down.
- Lookbacks of 1, 3, 12 months all positive; combining them diversifies.
- Vol-scale each position equally.
- Cross-asset diversification → Sharpe 1.0+ over decades.

### Moskowitz, Ooi & Pedersen (2012, JFE) — "Time series momentum"

- Time-series momentum at 1-month, 3-month, 12-month horizons works in 58
  liquid futures, currencies, equity indices, bonds.
- Combined with cross-sectional momentum, gives further diversification.

### Asness, Moskowitz & Pedersen (2013, JF) — "Value and Momentum Everywhere"

- Momentum works everywhere — equities, currencies, commodities, indices.
- Combining with value (uncorrelated in most markets) raises Sharpe ~30–50%.
- For crypto, the analog of value is carry / funding rate.

### Carhart (1997, JF)

- **Skip the last period** to avoid 1-period reversal contamination. In
  equities, use months t-12 to t-2 for formation, skip t-1.
- In crypto: skip 1 day or 1 week to similar effect, depending on rebalance
  frequency.

### Daniel & Moskowitz (2016, JFE) — "Momentum crashes"

- Momentum suffers severe crashes in bear-to-bull regime transitions.
- In equities, 2009 January–April erased a decade of gains.
- In crypto: late 2022, early 2023 was the equivalent — short-vol momentum
  crash after the FTX implosion.
- Defense: scale exposure down by realized vol of the strategy, or by market
  regime indicators.

## What I'm taking from each

| Source | Idea | Use in v2 |
|---|---|---|
| Liu-Tsyvinski 2021 | Formation = 1/2/4 weeks, weekly rebalance | Primary ranker |
| Liu-Tsyvinski-Wu 2022 | Long top quintile, short bottom quintile (L/S mode) | L/S variant |
| Hurst-Ooi-Pedersen 2017 | Vol-scale all positions | Sizing |
| Moskowitz-Ooi-Pedersen 2012 | Time-series momentum filter (own coin's 30d return > 0) | Filter |
| Asness-Moskowitz-Pedersen 2013 | Combine momentum with carry | Composite signal |
| Carhart 1997 | Skip-1-period | Formation lookback |
| Daniel-Moskowitz 2016 | Regime / vol scaling for crash defense | Vol-target overlay |

## Crypto-specific adaptations

- **Carry signal = trailing 7d cumulative funding rate.** Long perps pay
  funding when it's positive. Low/negative funding = cheap carry. Tilt
  rankings: low funding coin gets a bonus, high funding coin gets a penalty.
- **Universe = top 30–50 by trailing 90-day median USD turnover.** Bigger
  than equities momentum because there are fewer crypto names; smaller than
  "all coins" because the long tail is noise (Liu-Tsyvinski found weaker
  effect in small caps too).
- **Vol target = 15% annualized portfolio vol.** Standard for systematic
  factor portfolios. Higher than equities (5–10%) because crypto can absorb
  it.
- **Regime filter: BTC > 50-day SMA AND BTC realized vol < 80th percentile of
  trailing year.** Tighter than 200-day SMA because crypto regime turns over
  faster. The vol overlay catches the 2022-style crashes.

## What I'm NOT taking from the papers

- **Equity-style long formation periods (3–12 months).** Doesn't work in
  crypto — too noisy at long horizons.
- **Clenow's slope×R² ranker.** It's an elegant idea but for crypto's short
  formation periods, raw cumulative return is the simpler, better signal.
  R² penalty matters more when you have 60+ data points; with 7 data points,
  R² is noisy itself.
- **Carhart's 12-month formation.** Same issue as above.

## Expected performance

Realistic targets, based on academic results adjusted for costs:

| Configuration | Expected gross Sharpe | Expected net Sharpe |
|---|---:|---:|
| Long-only top quintile, weekly | 0.6–0.9 | 0.3–0.6 |
| L/S top vs bottom quintile, weekly | 1.0–1.5 | 0.7–1.0 |
| L/S + carry overlay | 1.3–1.8 | 1.0–1.3 |
| L/S + carry + TS-momentum filter | 1.5–2.0 | 1.2–1.5 |
| All of above + vol-target + regime gate | 1.8–2.2 | 1.4–1.8 |

**Sharpe 2.0 net is institutionally rare** — it's at the upper edge of what
single-asset-class systematic factors achieve. The literature reports it for
unlevered L/S factor portfolios, but those are gross of costs. After realistic
funding + slippage + fees, dropping below 2.0 net is the base case.

I'll target it explicitly via configuration sweeps but report honestly if the
canonical-root number lands at, say, 1.4 instead of 2.0. Forcing Sharpe up by
over-tuning is exactly the parameter-mining trap (integrity gate #17).

## What this means for the v2 design

- Default config: long-only top-20% of top-30 universe, weekly rebalance, 7d
  formation with 1d skip, vol-parity sizing.
- L/S variant: add short bottom-20%, beta-neutralize.
- Carry overlay: blend in funding rank with 0.5 weight.
- TS-momentum filter: require own 30d return > 0 for longs.
- Regime gate: BTC 50d SMA + vol percentile.
- Vol-target: 15% annualized portfolio vol.
- Cost: `cost_multiplier = 3.0` (conservative).
- Funding: modeled.

Configuration sweep order (cheap to expensive):
1. Long-only, no overlays, weekly rebal
2. + carry blend
3. + TS-momentum filter
4. + vol-target overlay
5. + regime gate
6. L/S top vs bottom
7. L/S + all overlays
8. L/S + all + vol-target

Each is its own report. The best gets a `funding-stressed candidate` label and
OOS test next.
