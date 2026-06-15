# Pre-registration: W5 Continuous Stage 4c - Sniper × Regime-Hedge Combination

**Date:** 2026-06-15
**Author:** Claude (W5 continuous signal-alpha loop)
**Stage:** derived analysis (component reuse, no engine run)
**Label:** `exploratory`
**Builds on:** Stage 4 liquidity sniper (`…stage4-sniper.md`, k=10% locked, pooled 1× +0.407)
and Stage 8c BTC-vol regime-hedge (`…stage8c-regime-hedge-robustness.md`, λ=0.5 locked,
pooled ~+0.078). Both parameters were locked in their own receipts; this stage introduces NO
new parameters.

## Question

Do the two W5 candidates STACK? The liquidity sniper changes which trades carry notional
(drop the least-liquid decile); the BTC-vol regime-hedge resizes only the BTC hedge leg
(causal mean-1 intensity). They act on disjoint parts of the book, so the combination is a
cheap component rebuild: `build_full_ledger` over the Stage 4 `T1_turnover_drop` component
pieces (which already encode `size_mult=0` on the dropped trades) with the BTC-vol
`hedge_intensity` overlay — no engine backtest.

## Mechanism (no new parameters)

Four arms via `s8._build_ledger(pieces, btc_rets, btc_fund, intensity, hedge_cost_bps)` per
venue, per hedge cost {5, 7.5, 10} bps (1×/1.5×/2× the frozen 5 bps):

- `C0` = T0_control pieces, intensity None (frozen control).
- `S` = T1_turnover_drop pieces, intensity None (sniper alone).
- `H` = T0_control pieces, BTC-vol intensity λ=0.5 (regime-hedge alone).
- `SH` = T1_turnover_drop pieces, BTC-vol intensity λ=0.5 (the combination).

Reproduction gate: C0 = 4.748 / 5.255, S = 4.907 / 5.910 (Stage 4), H = Stage 8c.

## Claim (a priori) / Pass bar

The combination is a robust improvement iff, at 1× hedge cost, `SH` MAR > `C0` AND > `S` AND
> `H` on BOTH venues. Stacking is super-additive iff (SH−C0) > (S−C0)+(H−C0). Cost-robustness:
SH MAR delta > 0 both venues at 1.5× and 2× hedge cost. Tier-3 real-money gate UNCHANGED.

## Post-run results

Run UTC 2026-06-15, git HEAD `5dd4e12`, λ=0.5, reuses `w5_continuous_stage4_sniper_2026-06-15`
pieces (no engine). Reproduction confirmed: C0 4.748/5.255, S 4.907/5.910, H reproduces
Stage 8c (bybit +0.108, binance +0.049). MAR by hedge cost:

| Venue | cost | C0 | S | H | SH | SH−C0 | SH−S | SH−H |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| bybit | 1.0× | 4.748 | 4.907 | 4.856 | 5.088 | +0.340 | +0.181 | +0.232 |
| bybit | 1.5× | 4.728 | 4.890 | 4.834 | 5.078 | +0.350 | +0.188 | +0.244 |
| bybit | 2.0× | 4.709 | 4.873 | 4.822 | 5.058 | +0.348 | +0.185 | +0.236 |
| binance | 1.0× | 5.255 | 5.910 | 5.303 | 6.052 | +0.797 | +0.142 | +0.748 |
| binance | 1.5× | 5.208 | 5.877 | 5.243 | 6.021 | +0.813 | +0.143 | +0.777 |
| binance | 2.0× | 5.161 | 5.845 | 5.195 | 5.990 | +0.828 | +0.145 | +0.795 |

Pooled ΔMAR vs C0: S +0.407/+0.416/+0.424, H +0.078/+0.071/+0.073, **SH +0.569/+0.581/+0.588**
(1×/1.5×/2×). Interaction (super-additivity) at 1×: bybit +0.073, binance +0.093.

## Verdict

> **⚠️ INHERITS THE STAGE 4d DOWNGRADE (2026-06-15).** This combination uses the k=10% sniper
> pieces, which the decile-robustness follow-up showed are NOT robust to the drop fraction on
> bybit (bybit liquidity selection is noise; only binance is real). So the "+0.569 both-venue"
> headline below is overstated: the bybit combo gain is mostly the (real, robust) BTC-vol
> regime-hedge plus fragile sniper noise; only the binance leg has a real sniper contribution.
> The robust both-venue piece of this combination is the **regime-hedge alone (Stage 8c)**; the
> sniper adds genuine value on binance only. Do not cite "+0.569 robust both-venue". Retained
> below for the record.

**[SUPERSEDED by Stage 4d — sniper component not k-robust on bybit] THE COMBINATION STACKS
SUPER-ADDITIVELY.** At 1×,
`SH` beats the control (+0.340 bybit / +0.797 binance), the sniper alone (+0.181 / +0.142),
and the hedge alone (+0.232 / +0.748) on BOTH venues; the interaction is positive on both
(+0.073 / +0.093), so the package exceeds the sum of its parts. It is **cost-robust at all
hedge costs** (both venues positive and GROWING: bybit +0.340→+0.348, binance +0.797→+0.828
across 1×→2×). **Critically, the combination resolves the regime-hedge's lone weakness — its
thin binance cost headroom:** the hedge alone was +0.049 at 1× and went negative at 1.5×
(Stage 8c), but in the combination binance is +0.797 at 1× and +0.828 at 2×, because the
sniper's selection is itself cost-robust (it drops the highest-impact trades, so its pooled
benefit RISES with cost: +0.407→+0.424). Mechanistically complementary: the sniper removes
low-liquidity/low-gross-EV fades (book quality), the regime-hedge protects the squeeze tail in
high vol (tail risk) — non-overlapping levers.

**Label `exploratory`** (derived from two pre-registered candidates via read-only component
reuse; no new engine run, no new parameters). The recommended **demo/paper forward-watch
package is sniper(k=10%) + BTC-vol regime-hedge(λ=0.5)**, pooled ~+0.57 MAR both venues,
cost-robust, keeping ~90% of trades. Tier-3 real-money gate UNCHANGED — forward demo fills
validate the sniper's execution-cost component. Follow-ups: drop-decile robustness (k∈{5,15,20}%)
and a fresh-receipt engine confirmation of the combo if it advances toward forward-watch.
