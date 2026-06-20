# Construction + Verdict: Continuous V2 Next-Level — Problem Book B (Entry Admission / 1m Path Features)

Date: 2026-06-20
Author: Claude (operator-directed next-level research push)
Stage: construction + verdict
Parent plan: `docs/preregistration/2026-06-19-continuous-v2-next-level-ab-research-plan.md`
Run label: `exploratory` (per-trade realized-PnL screen). **Verdict: NO both-venue candidate. Closest lead yet, but venue-split. Book B (admission + 1m-feature sizing) CLOSED.**

## Objective

The E1 closure (Bybit-only) found that selling a fade short INTO intrabar strength
selects continuation losers. With both-venue 1m built (Wave 1), test the entry side
directly: do STRICTLY CAUSAL 1m pre-entry path features predict the trade outcome,
and is that predictive power TRADABLE (admission or sizing) on BOTH venues?

## Method

`scripts/continuous_v2_book_b_admission.py`. For each `V2_CONTROL` short trade,
compute features on the pre-entry window `[entry − 120min, entry)` (only bars with
ts < entry decision — causal):
`ret_last15`, `run_up_120`, `dist_from_hi`, `upper_wick`, `rv_30`.
Per venue: Spearman IC vs the realized control net_return; a **delayed-copy** control
(features from the prior hour) to check the IC uses fresh pre-entry info; an
**admission** screen (admit the favorable half) and a **sizing** screen (mean-1
gross-constant tilt by z(feature)); each vs a **hash null** (random half / permuted
multipliers) and vs the full control book; both venues.

## Results (full run 2026-06-20; control MAR proxy bybit 6.15 / binance 4.33)

| feature | signed IC bybit / binance | lagged IC | ADMIT beats ctrl / hash | SIZE beats ctrl / hash |
|---------|--------------------------:|----------:|:-----------------------:|:----------------------:|
| upper_wick | **+0.104 / +0.118** | +0.084/+0.080 | no / **yes** both | bybit no, **binance yes** / **yes** both |
| rv_30 | +0.157 / +0.125 (signed) | weaker (fresh) | no / no | bybit no, **binance yes** / **yes** both |
| run_up_120 | +0.098 / +0.086 | +0.086/+0.079 | no / yes(bybit) | no / no |
| dist_from_hi | −0.017 / +0.027 | ~ | no / yes(binance) | no / no |
| ret_last15 | −0.052 / −0.024 | ~same (stale) | no / no | no / no |

## Verdict — real signal, but venue-split / not two-venue tradable

- **`upper_wick` is the strongest, most robust signal found in the next-level
  program**: a real both-venue IC (+0.10 / +0.12), it beats its hash null on both
  venues, and its live IC exceeds the delayed (prior-hour) copy — i.e. it uses
  FRESH pre-entry information, not a persistent artifact. The mechanism is sound:
  long upper wicks before entry = sellers rejecting the highs = exhaustion = a
  better fade.
- **But it is not a both-venue tradable edge.** As *admission* it loses to the
  diversified control on both venues (cutting half the book sacrifices more
  diversification than the diffuse IC recovers — beats hash, not control). As
  *sizing* (diversification-preserving) it beats the hash-tilt on both venues and
  beats the control on **Binance** (upper_wick 4.45 vs 4.33; rv_30 4.50 vs 4.33)
  but **not Bybit** (5.96 vs 6.15). A venue split → `both_venue_sizing_winners: []`.
- This is the program's recurring truth, now with the best signal yet: v2's edges
  are real but diffuse, and the tradable residual is venue-split. Notably the split
  direction is OPPOSITE to the exit-side leads — the Bybit-only TP12 gain (F2) vs a
  Binance-only 1m-exhaustion-sizing gain here. Neither is a two-venue candidate.

## Falsifiers applied

- Hash null: sizing beats it on both venues for upper_wick/rv_30 → the signal is real.
- Delayed-copy: live IC > lagged IC → fresh pre-entry info (not stale persistence);
  `ret_last15` lagged ≈ live → that one is stale and weak.
- Both-venue: FAILS (sizing helps Binance, not Bybit).
- Diversification: admission FAILS on both venues (the proper full-book benchmark).

## Honest caveats / scope

- Per-trade realized-PnL SCREEN, no rebalance/hedge re-solve. Sizing z is full-sample
  (fair for the feature-vs-hash RELATIVE test; a candidate needs CAUSAL per-symbol
  expanding-prior z + full-ledger `build_full_ledger` validation — the prior B
  conviction-sizing closure showed loose-rule sizing can be a hash artifact, so the
  hash-tilt control here is the load-bearing falsifier, and upper_wick passes it).
- The Binance-only `upper_wick` / `rv_30` sizing tilt is recorded as an operator-gated
  VENUE-POLICY lead only (symmetric to the Bybit-only TP12 lead), NOT a frozen-object
  change and NOT real-money evidence.

## No real-money / promotion claim

`REAL_MONEY` stays false. Book B closed; the Binance-only 1m-exhaustion sizing tilt
is an operator-gated venue-policy lead for a possible no-order forward shadow.
