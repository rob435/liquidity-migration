# Pre-registration: W5 Continuous Stage 9 - Regime-Conditioned Book Sizing

**Date:** 2026-06-15
**Author:** Claude (W5 continuous signal-alpha loop)
**Stage:** run-pending
**Plan:** `docs/research_plans/w5_continuous_signal_alpha/09_stage8_regime_response.md`
(R1 continuous regime→size direction) + `06_stage5_sizing_alpha.md`.
**Contract:** `00_methodology_contract.md`. **Binding:** E2 closed the bounded V1/V2
entry-GATE family; this is NOT a gate change.

## Question

Does sizing the SAME book (V0 entries, constant breadth) **down in high-BTC-volatility
regimes** — a causal, mean-1 regime size tilt — improve pooled MAR vs the frozen control
on both venues, by cutting exposure to the squeeze-heavy high-vol periods? This is the
sizing analogue of the regime-hedge (which hedges more in high vol); it has no hedge
turnover cost, so it may help binance where the hedge is thin.

## Mechanism (locked before the run)

Per-entry notional multiplier via the additive `size_mult_lookup` hook (Stage 5;
applied after all gates → entries/breadth identical to V0; resize/impact cost recomputed
at the new size). The multiplier is the **entry day's BTC-vol regime intensity**, same
for every symbol entering that day:

- `btcvol_pct(day)` = trailing-30d BTC-vol percentile (causal, Stage 8 definition,
  trailing-250 percentile).
- `m_raw(day) = 1 − λ(2·btcvol_pct − 1)` — **size DOWN in high vol** (pct→1 ⇒ 1−λ;
  pct→0 ⇒ 1+λ), the risk-management direction (locked sign). λ = 0.40 (locked; size
  ∈ ~[0.6,1.4]).
- prior-calendar-month normalization (causal) → mean-1 gross-neutral.
- entry at `signal_ts` gets `m_raw_normalized(day_floor(signal_ts))`.

This is distinct from: Stage 5 path-shape sizing (symbol-cross-sectional, beaten by a
symbol-identity control) — here the tilt is a market-wide DAILY regime, identical across
symbols, so it is NOT symbol-correlated; and from E2 V1/V2 — breadth is unchanged (same
entries), only notional is reallocated across regimes at constant average size.

## Arms (locked)

- `Z0_control`: frozen sizing — the Stage 0 ensemble.
- `ZR_btcvol_size`: BTC-vol regime size-down, λ=0.40.
- `ZR_btcvol_size_2xcost`: same + `round_trip_cost_multiplier=2.0`.
- `ZR_hash_size` (negative control): same construction but a hash-week regime (no market
  content), mean-1. If `ZR_btcvol_size` does not beat it, the regime carries nothing.

Full engine re-run per arm (size changes per-trade cost), then frozen ensemble/hedge
rebuild. (Hedge intensity is NOT touched — this isolates the sizing lever; a
hedge+sizing combination is a separate receipt.)

## Constraints

- same ENTRIES as V0 (size hook after all gates — entry count asserted identical);
- mean size multiplier ∈ [0.95,1.05] (gross-neutral — no hidden leverage; reported per venue);
- causal regime (BTC-vol through the prior day); funding ON; resize cost charged.

## Metrics

- total return, MAR, max drawdown, worst day; R1 monthly; realized mean multiplier;
  realized gross (total notional) vs Z0; per-component; chronological thirds.

## Decision rule (a priori) / Pass bar

`ZR_btcvol_size` is a robust candidate iff, vs Z0:

1. positive total return both venues;
2. pooled MAR delta `> 0` both venues (operator bar — robust improvement, not the strict
   +0.1; report whether it also clears +0.1);
3. no venue MAR delta `< -0.5`; drawdown not worse `> +10%` relative;
4. survives the 2×-cost arm (pooled MAR delta still `> 0` both venues);
5. realized gross (total notional) within ±5% of Z0 (no leverage);
6. beats the `ZR_hash_size` control;
7. not carried by one venue or one chronological third.

Default label `exploratory`; a pass nominates a demo/paper forward-watch candidate
(Tier-3 real-money gate unchanged). If robust, it is a candidate alongside / combinable
with the BTC-vol regime-hedge.

## Falsifier

Reject if negative on either venue, fails the 2×-cost arm, matched by the hash control,
changes the entry population, needs mean multiplier outside [0.95,1.05], or one venue/third
carries it. If it fails, regime *book* sizing is closed; the regime-hedge stays the
candidate and the next lever is a hedge+size combination or a different mechanism.

## Window / roots / run

Window `2023-04-01 <= signal_ts < 2026-05-01`, both full-PIT roots; full engine re-run per
arm. Roots read-only; writes only to `reports/<tag>/` and `~/SHARED_DATA/w5_continuous_stage9_*`.

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/w5_continuous_stage9_regime_sizing.py \
  --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
  --out ~/SHARED_DATA/w5_continuous_stage9_regime_sizing_2026-06-15
```

## Post-run results

Run UTC 2026-06-15, both venues, git HEAD `5dd4e12` (code uncommitted; code hash
`e69e9d99…`), λ=0.40. X0 reproduces the Stage 0 ensemble exactly (bybit 0.7707/4.748,
binance 0.6428/5.255); entry counts identical across arms (bybit 3220, binance 2978);
realized gross within ~1% of Z0. Artifacts
`~/SHARED_DATA/w5_continuous_stage9_regime_sizing_2026-06-15/`.

| Venue | Arm | Return | MAR | MaxDD | Mean mult |
|---|---|---:|---:|---:|---:|
| bybit | Z0 | 0.7707 | 4.748 | −5.27% | — |
| bybit | ZR_btcvol_size | 0.7030 | 3.556 | −6.41% | 0.989 |
| bybit | ZR_btcvol_size_2xcost | 0.5248 | 2.353 | −7.23% | 0.989 |
| bybit | ZR_hash_size | 0.7468 | 4.142 | −5.85% | 1.001 |
| binance | Z0 | 0.6428 | 5.255 | −3.97% | — |
| binance | ZR_btcvol_size | 0.5986 | 5.180 | −3.75% | 0.980 |
| binance | ZR_btcvol_size_2xcost | 0.4680 | 3.738 | −4.06% | 0.980 |
| binance | ZR_hash_size | 0.7215 | 5.709 | −4.10% | 1.008 |

Pooled MAR delta vs Z0: ZR_btcvol_size **−0.633** (bybit −1.19, binance −0.075); 2x-cost
−1.956; ZR_hash_size **−0.075**.

## Verdict

**NULL — regime book-sizing (size-down) hurts, and is worse than random.** Sizing the
book down in high-BTC-vol regimes costs return on both venues (bybit 0.77→0.70 with DD
*worse*, binance 0.64→0.60), pooled MAR **−0.633**, and is **beaten by the random
hash-regime size control** (−0.075; the hash got lucky on binance, +0.45, the same
random-tilt variance Stage 5 flagged). So the BTC-vol regime carries no useful *sizing*
information in this direction.

**Mechanistic insight (the key takeaway):** this fade book *profits* in high-vol regimes
(big alt dislocations to fade + funding — consistent with E2's "euphoria is good" and
Stage 3). Sizing *down* there forgoes that profit. The asymmetry with the regime-HEDGE is
the point: in high vol you want to **keep the profitable book AND hedge its squeeze tail**
(what the regime-hedge does) — NOT shrink the book. This is why the hedge works (+0.078)
and sizing-down fails (−0.633). Regime book-sizing is **closed** (size-down harmful;
size-up would add risk against the existing vol-target and is not pursued). A
hedge+size-down combination is unappealing (inherits this harm). The **BTC-vol
regime-hedge (Stage 8c) remains THE candidate.** Falsifier: triggered (negative both
venues, beaten by the random control). Next: the last targeted shot at the hedge's binance
cost headroom is a cross-sectional alt-dispersion HEDGE signal; else consolidate the
regime-hedge candidate and set up its forward-watch.
