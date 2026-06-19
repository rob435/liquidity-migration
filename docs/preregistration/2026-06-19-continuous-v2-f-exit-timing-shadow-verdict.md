# Continuous V2 Problem Book F — Exit-Timing Shadow Verdict (both-venue no-order)

Date: 2026-06-19

Construction: `docs/preregistration/2026-06-19-continuous-v2-f-exit-timing-shadow-construction.md`
Scope: both-venue NO-ORDER shadow. Not a candidate; not real-money evidence; frozen v2 forward
ledger untouched. Per-trade path shadow on `klines_1h` (no rebalance / concurrency re-solve).

## What ran

`scripts/continuous_v2_f_exit_timing_shadow.py` over both venues' V2_CONTROL short trades
(bybit 2367, binance 2149; path-reconstructed control matches the recorded ledger exactly,
recon_err = 0.0000). Causal exit rules with the 10% TP honored first; honest TP-winner-cut
accounting; random-exit negative control. Output: `backtest-runs/continuous_v2_f_exit_timing_2026-06-19/`.

## Results — net effect vs control (per-trade contribution), and TP winners cut

| rule | bybit net % | binance net % | TP winners cut (by/bi) | forgone TP cut (by/bi) |
| --- | ---: | ---: | ---: | ---: |
| shorter_hold_12h | −65.6% | −78.9% | 303 / 284 | +0.252 / +0.264 |
| shorter_hold_18h | −46.1% | −41.8% | 156 / 141 | +0.114 / +0.102 |
| time_decay_11h | −70.9% | −87.4% | 330 / 298 | +0.265 / +0.291 |
| mfe_giveback_3%/50 | −67.5% | −79.7% | 424 / 377 | +0.454 / +0.415 |
| mfe_giveback_5%/50 | −35.9% | −53.2% | 277 / 260 | +0.268 / +0.262 |
| random_exit (null) | −73.2% | −83.1% | 337 / 301 | +0.318 / +0.314 |

## Verdict — exit-timing CLOSED; the fixed 24h hold is validated, not a weakness

Every exit rule is **strongly negative on both venues** (−36% to −87% of the control book). The
"smart" rules (MFE-giveback, time-decay) are barely better than — and sometimes worse than — a
random exit. The mechanism is unambiguous from the TP-winners-cut column: every early-exit rule cuts
150–420 trades that the control rode to the +10% take-profit, and the forgone TP PnL plus the
max-hold trades that recover by 24h outweigh the losers it saves.

The exit attribution that motivated this (the 24h `max_hold` bucket being net-negative, ~28% giving
back >3% MFE) is a **selection illusion**: that bucket is net-negative *because* the winners already
left early via the 10% TP, leaving losers behind. The giveback is not causally separable in real time
from the temporary-pullback-then-TP path, so capturing it forgoes the winners. The long 24h hold is
what lets the diffuse winners reach TP — exactly the v2 design rationale.

This reproduces, quantitatively and on both venues, the registered 2026-06-18 finding that daemon /
early exits destroy the fade edge. Falsifiers fired: net ≤ 0 on both venues; gains dominated by
cutting TP winners; rules do not robustly beat the random-exit null.

## Status

- **Keep frozen v2 exits unchanged**: 10% component TP + 24h hold (48h max), no early/daemon exits.
- Exit-timing (shorten / time-decay / MFE-giveback) is closed as a falsifier-backed negative.
- Not tested (would need data/effort beyond this shadow): holding LONGER than 24h (needs path beyond
  the control exit), and a hedge-first drawdown control (F3) that does not cut single-name exits.
  Neither is promising given the TP-winner-cut mechanism; revisit only under a new dated hypothesis.
- Limitation: per-trade path shadow; no daily vol-target rebalance / concurrency re-solve. The
  −36% to −87% magnitude and the TP-cut mechanism are far too large for the rebalance to flip.
