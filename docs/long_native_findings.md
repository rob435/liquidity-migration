# Long-Native Creative Sleeve — Findings

**Date:** 2026-05-23
**Status:** Sharpe 3.0 target **not met honestly**. Best result Sharpe 1.48 (daily-aligned) in-sample, OOS negative.
**Code:** `liquidity_migration/long_native.py`

## TL;DR

- Built a creative long-only sleeve with 5 crypto-native event patterns (capitulation rebound, funding squeeze, volume resurrection, oversold bounce, FOMO chase) — explicitly **not** from academic papers, per user direction.
- **FOMO chase** (FC pattern) hits Sharpe 2.85 **as reported by the existing `summarize_trade_backtest` helper** on the canonical Bybit research root.
- **That number is inflated 2.86×.** The helper annualizes with `sqrt(365 / hold_days)` which assumes the strategy trades every hold-day; the FC pattern only fires 14.9 times per year. **Honest daily-aligned Sharpe is 1.48.** The short sleeve doesn't have this issue because it fires 158 trades/year (close to the annualization assumption).
- **OOS fails entirely**: Sharpe −2.81 on Bybit pre-2023, +0.21 on Binance pre-2023. The 2023-2026 alpha is regime-conditional and does not transfer to 2020-2023.
- All other patterns (capitulation rebound, funding squeeze, oversold bounce, volume resurrection) and all existing event types (volume_shelf_reclaim, capitulation_reclaim, top_volume_leadership, etc.) in long-continuation mode **lose money** on the canonical root.

## What I built

### Five custom patterns (zero papers cited)

1. **Capitulation rebound** — violent 5d flush + same-day absorption + volume spike. Trader-intuition "buy the puke" setup.
2. **Funding squeeze** — trailing cumulative funding deeply negative, recent flip positive, green day with volume. Squeeze mechanics.
3. **Volume resurrection** — coin's trailing 30d rank was below median, today's rank in top tier, green day. "Forgotten coin re-emerges."
4. **Oversold bounce** — 14d return < −20%, today's return > +5%, volume confirmation. Classic mean-reversion.
5. **FOMO chase** — today's return > +15%, volume rank in top 5 of universe, closed in upper 70% of day, BTC + ETH both above 50d SMA. Buy the breakout, ride the wave.

Plus optional trailing-ATR stop overlay.

### What worked, what didn't

| Pattern | Best in-sample Sharpe (reported / honest) | Trade count | Promotes? |
|---|---:|---:|---|
| Capitulation rebound (alone) | −36.97 / N/A | 7 | No (too rare) |
| Funding squeeze (alone) | −0.07 / N/A | 4 | No (too rare) |
| Volume resurrection (alone) | −0.62 / N/A | 236 | No (broken signal) |
| Oversold bounce (alone) | −91.6 / N/A | 10 | No (catches knives) |
| **FOMO chase FC_15_5_hold3_tp40** | **2.85 / 1.48** | 38 | True in-sample, fails OOS |
| FC_15_3_hold3_top3 | 5.11 / ~1.6 | 9 | True in-sample but n=9 (suspect) |
| FC_18_5_tp50_hold7 | 2.44 / ~1.3 | 29 | True in-sample, fails OOS |

The FC family is the only signal class with positive results. The other patterns systematically fail — long-side asymmetric setups in crypto's 2023-2026 environment don't have the catch-rate to overcome cost.

### Existing event types in long-continuation mode (volume_events.py infra)

Ran 12 variants over 6 event types via the proven `volume-events` CLI. Best:

| Event | Sharpe | Return | Max DD | Trades |
|---|---:|---:|---:|---:|
| volume_shelf_reclaim_wide | 0.49 | +72.40% | −38.64% | 518 |
| top_volume_leadership_wide | 0.09 | −7.46% | −64.65% | 645 |
| volume_shelf_reclaim_tight | 0.03 | −4.42% | −39.96% | 537 |
| reclaim_breakout_tight | −0.10 | −28.36% | −56.64% | 800 |
| capitulation_reclaim_tight | −0.56 | −67.52% | −77.19% | 855 |
| orderly_leadership_pullback | −0.93 | −82.58% | −84.24% | 755 |

The repo's pre-coded "long-continuation" events lose money systematically. The infrastructure was tuned for the liquidity_migration **short** trade; the long side is genuinely different and these signals don't translate.

## The Sharpe annualization gotcha (HISTORICAL — fixed 2026-05-24)

> **Fix landed:** B.1 of [full_pit_rebuild_and_punchlist.md](full_pit_rebuild_and_punchlist.md).
> `summarize_trade_backtest` and `_split_rows` now report daily-aligned
> Sharpe under `sharpe_like` (annualised off the calendar-day equity curve,
> forward-filled across exit days). The basket-frequency formula is gone
> entirely — there is no `sharpe_basket_frequency_legacy` field anymore.
> The promotion-gate threshold is 0.7 against the honest Sharpe (was 1.0
> against inflated). Historical numbers below this footnote remain useful
> as **directional** evidence; do not compare their magnitudes to runs
> produced after 2026-05-24.

The repo's `summarize_trade_backtest` used to use:
```python
annual_periods = 365.0 / config.rebalance_days   # rebalance_days is the hold horizon
sharpe = mean(basket_returns) / std(basket_returns) × sqrt(annual_periods)
```

This is correct **when the strategy actually trades at frequency 365/rebalance_days**. The short sleeve, with 475 trades over 3 years × hold_days=3, has 158 trades/year vs theoretical max 121 — annualization is approximately right.

The FC long pattern has 14.9 trades/year — far below 121. The `sqrt(365/3) = 11.03` annualization is way too generous; the honest annualization is `sqrt(14.9) = 3.86`. Inflation = 11.03 / 3.86 = **2.86×**.

So the reported Sharpe 2.85 → honest Sharpe ~1.0. Confirmed by computing daily-aligned Sharpe directly from the equity series: **1.48** (mean daily return / std × sqrt(365)).

Either of those is well below the Sharpe 3.0 target.

This is a methodology bug that affects ALL sparse-firing strategies in this codebase. The short sleeve happened to fire frequently enough that the bug didn't bite it, but it bit my low-frequency FC strategy hard. **For honest cross-strategy comparison, daily-aligned Sharpe is the right metric.** That's worth fixing upstream.

## OOS validation

Pre-registered FC_15_5_hold3 and three near-variants, tested once on each OOS root:

| Variant | Canonical (in-sample) | OOS Bybit pre-2023 | OOS Binance pre-2023 |
|---|---:|---:|---:|
| **FC_15_5_hold3** | Sharpe **+2.37** (38 trades) | Sharpe **−2.81** (33 trades) | Sharpe +0.21 (89 trades) |
| FC_15_5_hold3_wider (stop 12%, tp 30%) | +2.47 (38) | −1.91 (33) | +1.02 (88) |
| FC_15_5_hold3_uni50 (top 50) | +1.26 (47) | −2.19 (36) | +0.39 (100) |
| FC_25_5_hold3 (+25% threshold) | +1.74 (12) | −0.16 (14) | +0.96 (43) |

None survive OOS. The FC pattern as discovered is a 2023-2026 phenomenon.

## What gets to honest Sharpe 3.0?

The short sleeve does (Sharpe 3.37 reported, ~3.4 honest, 475 trades). It works because:
- Crypto crashes are violent and predictable (events like liquidity_migration)
- Short squeeze risk is bounded by the asymmetric crowd-trade fade
- High trade frequency (158/yr) provides statistically meaningful samples
- The setup exploits a real microstructure (over-aggressive crowding into top-volume)

For long-only crypto, equivalent honest Sharpe 3.0 requires:
- A signal with similar asymmetric edge (rare in long direction)
- High trade frequency to support Sharpe calculation
- Robustness across regimes

**I did not find such a signal in this work.** Tried:
- Academic factor portfolios (Liu-Tsyvinski-Wu, Asness-Moskowitz-Pedersen, Hurst-Ooi-Pedersen, Daniel-Moskowitz): max honest Sharpe ~1.0
- Custom event patterns (5 different patterns × dozens of parameter combos): max honest Sharpe ~1.5 in-sample, all fail OOS
- Existing event-driven infrastructure (12 variants on 6 event types): all losing

The bar genuinely appears not to be there for single-strategy long-only crypto Sharpe 3 net of conservative costs. Best paths forward:
1. **Multi-sleeve combination** — pair LO_skip0 (~Sharpe 0.9 OOS-robust) with the existing short sleeve (Sharpe ~3.4 IS, presumably OOS-checked already). Combined book correlation may push joint Sharpe above 3.0.
2. **Different asset class or microstructure** — basis trade, funding-rate arbitrage, exchange-listing front-running. Out of scope here.
3. **Forward paper-trading the FC pattern** — if 6+ months forward confirms ~Sharpe 1.5, accept it as a complement to the short sleeve.

## Honest recommendation

Stop iterating on this strategy class. The data is telling us:
- FC pattern is a real signal but regime-conditional (2023-2026 only) and inflated by Sharpe-annualization quirks.
- The OOS validation has now been used on 3 strategies; the dedicated OOS roots are spent.
- Further parameter tuning on canonical is parameter mining.

Sharpe 3.0 long-only by itself is not happening on the data we have without abandoning research integrity.

## Artifacts

```
liquidity_migration/long_native.py
docs/long_native_findings.md (this file)
~/SHARED_DATA/bybit_fullpit_1h/reports/long_native_FC_15_5_hold3_tp40/
~/SHARED_DATA/bybit_fullpit_1h/reports/long_native_FC_15_5_hold3_canonical/
~/SHARED_DATA/bybit_oos_pre2023/reports/long_native_FC_15_5_hold3_OOS_bybit/
~/SHARED_DATA/binance_oos_pit/reports/long_native_FC_15_5_hold3_OOS_binance/
~/SHARED_DATA/bybit_fullpit_1h/reports/long_event_*/ (12 variants)
```
