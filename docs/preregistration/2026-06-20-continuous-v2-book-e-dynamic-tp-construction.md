# Construction + Verdict: Continuous V2 Next-Level — Problem Book E (Dynamic / MFE-Extension TP)

Date: 2026-06-20
Author: Claude (operator-directed next-level research push)
Stage: construction + verdict
Parent plan: `docs/preregistration/2026-06-19-continuous-v2-next-level-ab-research-plan.md`
Engine: the validated Wave-2 1m intrabar engine (`resolve_dynamic_tp_1m`).
Run label: `exploratory` (per-trade realized-PnL screen). **Verdict: the exit-TP Bybit/Binance venue split is FUNDAMENTAL. MFE-extension trailing TP does not reconcile it — closing the exit-TP question (after flat F2 and vol-scaled F2b).**

## Objective

The prior F2/F2b work (1h + per-trade proxy) closed the FLAT TP raise as a fundamental
venue split (Bybit-positive / Binance-negative) and showed vol-scaled TP is DOMINATED
by the flat raise. This book does NOT re-run that closed ground. It tests the ONE axis
the 1h bar could not measure: PATH-CONDITIONAL winner management — a hard TP15 ceiling
plus a 1m-path-armed trailing giveback (E5 MFE-extension) that protects the control's
12% gain while letting strong reverters run. Does the trailing protection reconcile the
Binance drawdown that flat TP15 caused?

## Method

`scripts/continuous_v2_book_e_dynamic_tp.py` re-resolves every `V2_CONTROL` short trade
on the 1m cache (entries fixed) under:
- `E0_TP12` control (reproduces frozen exit) · `E1_TP15_FLAT` (known venue split) ·
- `E5{a,b,c}` TP15 ceiling + trailing armed at 12% MFE, giveback 1.5 / 2.5 / 4.0%
  (`resolve_dynamic_tp_1m`, close-based giveback matching the deployed `mfe_giveback`) ·
- `E8_HASH_TRAIL` null: base TP hash-permuted in [12,15]%, no trail (matches "sometimes
  exit wider" frequency, destroys the path mechanism).

Metric: realized-PnL MAR proxy + drawdown + worst-day, both venues. Candidate = improve
MAR vs E0 AND not worsen drawdown, on BOTH venues, beating the hash null.

## Results (full run 2026-06-20)

| arm | bybit MAR / Δ / Δdd | binance MAR / Δ / Δdd |
|-----|---------------------|------------------------|
| E0_TP12 (control) | 6.152 / — | 4.316 / — |
| E1_TP15_FLAT | 4.667 / **−1.49** / −0.010 | 4.692 / **+0.38** / +0.002 |
| E5a (give 1.5%) | 5.581 / −0.57 / −0.002 | 4.249 / −0.07 / −0.000 |
| E5b (give 2.5%) | 5.490 / −0.66 / −0.003 | 4.252 / −0.06 / −0.001 |
| E5c (give 4.0%) | 5.658 / −0.49 / −0.001 | 4.270 / −0.05 / −0.001 |
| E8_HASH_TRAIL | 5.292 | 4.385 |

**Both-venue winners: NONE.**

## Verdict — the exit-TP venue split is fundamental; trailing does not reconcile it

- **The MFE-extension trailing is a compromise that pleases neither venue.** On
  **Bybit** it recovers ~2/3 of what flat TP15 lost (5.58 vs 4.67) but stays WORSE than
  the tight TP12 control (−0.5 to −0.66) — Bybit fundamentally prefers the tight exit.
  On **Binance** it is slightly worse than control AND worse than flat TP15, and it
  **loses to its own hash null** (E5 ≈ 4.25 < E8 4.39) — the trailing giveback CUTS the
  very runners that give Binance its wide-TP benefit.
- **Mechanism (the explanation the split demands):** Bybit fade names revert fast and
  hard to ~12% then bounce — a tight TP captures the reversion before the bounce, and
  any widening (flat, vol-scaled, or trailing) just exposes the position to the bounce.
  Binance fade names revert slower and further — they keep running past 12%, so a wide
  TP captures more and a trailing giveback exits too early on the first pullback during
  a continued run. Opposite microstructure → opposite optimal exit → irreconcilable with
  ANY single TP rule.
- **This closes the exit-TP question end to end:** flat (F2), vol-scaled (F2b), and now
  path-conditional MFE-extension (E5) all fail to reconcile the Bybit-tight /
  Binance-wide split. The split is a real property of the two venues' fade-reversion
  dynamics, not an artifact of a crude flat TP. The Bybit-only TP12 gain remains an
  operator-gated venue-policy lead; there is no both-venue exit-TP candidate.

## Falsifiers applied

- Hash null (E8): on Binance the hash beats every E5 trailing variant → the path
  mechanism adds nothing there.
- Both-venue: none.
- Re-confirmed the prior flat-TP15 split at 1m fidelity (E1) before testing the new axis.

## Honest caveats / scope

- Per-trade realized-PnL screen, no rebalance/hedge re-solve (a clean NEGATIVE, which a
  screen can establish; no survivor needs full-ledger validation).
- The trailing resolver `resolve_dynamic_tp_1m` is separate from the validated
  `resolve_exit_1m` core (control-reproduction guarantee untouched); unit-tested (3 new
  tests: ceiling hit, trailing exit after arm, disabled-trail == flat TP).

## No real-money / promotion claim

`REAL_MONEY` stays false. Book E closed; no exit-TP change to the frozen object.
