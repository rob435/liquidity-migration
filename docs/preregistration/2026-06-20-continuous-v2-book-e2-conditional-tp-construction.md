# Construction + Verdict: Continuous V2 — Book E2 Feature-Conditional & Time-Decay TP

Date: 2026-06-20
Author: Claude (operator-directed next-level research push)
Run label: `exploratory` (equal-weight per-trade screen). **Verdict: still no two-venue candidate, BUT a run-up-conditional TP is a real Binance-only refinement of the wide-TP lead (beats flat TP15 AND its hash). E6 time-decay TP fails both venues. The Bybit-tight / Binance-wide split persists.**

## Motivation (from the adverse-trade characterization)

`run_up_120` (size of the pop being faded) has +0.17 IC on realized gross — bigger-pop
fades revert FURTHER. So set the take-profit per trade from the pre-entry signal: a WIDER
TP for trades expected to revert further. New vs the closed flat (F2) / vol-scaled (F2b) /
trailing (E5) work. Equal-weight ("perfect the trade", per operator direction), per venue,
vs flat baselines and a hash null.

## Method

`scripts/continuous_v2_book_e2_conditional_tp.py` re-resolves V2_CONTROL shorts on the 1m
engine, EQUAL WEIGHT: E0_TP12, E1_TP15, E_RUNUP_TP = clip(0.10+0.30·run_up, .08,.20),
E_VOL_TP = clip(0.10+8·rv30, .08,.20), E6_DECAY_TP (TP 0.18→0.08 over the 24h hold), and
hash-permuted nulls for the conditional rules. Metric: mean per-trade short return.

## Results (2026-06-20, equal-weight mean trade return, Δ vs flat TP12)

| arm | bybit Δ | binance Δ |
|-----|--------:|----------:|
| E0_TP12 (control) | 0 | 0 |
| E1_TP15 (flat wide) | −0.00066 | +0.00067 |
| **E_RUNUP_TP** | −0.00013 | **+0.00149** |
| E_VOL_TP | +0.00024 | +0.00058 |
| E6_DECAY_TP | −0.00092 | −0.00004 |
| E_RUNUP_HASH | −0.00047 | +0.00037 |
| E_VOL_HASH | +0.00011 | −0.00016 |

Per-venue verdict (beats both flats AND its hash): bybit → only E_VOL_TP "wins" (+0.00024,
noise-level); binance → E_RUNUP_TP wins (+0.00149, the best arm, clears flat TP15 and its
hash). NO arm wins on both venues.

## Verdict

- **Run-up-conditional TP is a genuine Binance-only refinement.** On Binance, sizing the
  TP to the pop being faded (+0.00149 EW) beats both flat TP12 and flat TP15 (+0.00067)
  and its hash null (+0.00037) — confirming the characterization's mechanism (bigger pop →
  further reversion → a wider target on those names captures more). This sharpens the
  existing Binance wide-TP venue lead from a flat raise into a conditional rule.
- **It does nothing on Bybit** (−0.00013), where the tight TP12 remains best — Bybit fades
  revert fast-and-hard then bounce, so a wider target (flat OR conditional) just exposes
  the bounce. The split is fundamental, now confirmed a FIFTH way (flat / vol-scaled /
  trailing / time-decay / run-up-conditional).
- **E6 time-decay TP fails on both venues** (bybit −0.00092, binance ≈0) — decaying the
  target neither helps the fast Bybit reverters nor the slow Binance ones.
- Magnitudes are small in equal-weight terms (few trades hit the wide tail), but the
  Binance run-up rule is the cleanest dynamic-TP signal found and is a real (beats hash)
  per-venue lead.

## Status of the dynamic-TP axis

Pushed thoroughly: flat (F2), vol-scaled (F2b), MFE-extension trailing (E5), time-decay
(E6), and feature-conditional run-up/vol (E2). The recurring, robust conclusion is the
Bybit-tight / Binance-wide split is fundamental; no single TP rule reconciles it. The two
operator-gated, opposite-venue leads stand and are now sharper: Bybit-only tight TP (≤12%),
Binance-only run-up-CONDITIONAL wide TP.

## No real-money / promotion claim

`REAL_MONEY` stays false. No TP change to the frozen object; Binance run-up-conditional TP
is an operator-gated venue-policy lead for a possible no-order forward shadow.
