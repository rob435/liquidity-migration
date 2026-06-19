# Continuous V2 E1 Intrabar Entry-Timing Verdict (Bybit-only exploratory)

Date: 2026-06-19

Construction: `docs/preregistration/2026-06-19-continuous-v2-e1-entry-timing-construction.md`
Scope: `claimed_venue_scope=bybit_only_execution_exploratory`. Run label `exploratory`.
Per-trade diagnostic (no lifecycle re-sim / no rebalance). Not a candidate; not real-money evidence.

## What ran

`scripts/continuous_v2_e1_entry_timing_diag.py` over the Bybit `V2_CONTROL` short trades
(2278 / 2367 had `klines_5m` entry-hour data). Causal sell-into-strength stop at
`entry_price*(1+δ)`, δ ∈ {0.25%, 0.5%, 1%}; missed-fill + adverse-selection + random-bar-null
accounting. Output: `backtest-runs/continuous_v2_e1_entry_timing_2026-06-19/`.

Control total net contribution (5m universe): 0.5019.

| δ | fill rate | net effect (contrib) | % of control | missed foregone | adverse-sel (missed−filled mean net) | random-bar null |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.25% | 88.5% | −0.1021 | −20.3% | +0.1506 | +4.0e−4 | +0.0275 |
| 0.50% | 80.9% | −0.1296 | −25.8% | +0.2174 | +3.4e−4 | +0.0358 |
| 1.00% | 66.8% | −0.1762 | −35.1% | +0.3163 | +3.0e−4 | +0.0223 |

## Verdict — FALSIFIED; entry-timing axis CLOSED

All three pre-registered falsifiers fired:

1. **Net effect ≤ 0** — strongly negative at every δ (−20% to −35% of control). The entry-price
   improvement on filled trades (+0.05 to +0.14) is dwarfed by the PnL foregone on missed trades
   (+0.15 to +0.32).
2. **Adverse selection** — the missed trades (shorts whose price did NOT spike intrabar) had
   *higher* mean net return than the filled trades at every δ. The rule skips the best shorts.
3. **Loses to the random-bar null** — random intrabar timing is mildly positive (+0.02 to +0.04);
   the sell-into-strength rule is far worse (−0.10 to −0.18). The timing "skill" is negative.

**Mechanism (why):** the book shorts names that just ran up. A name that spikes *further* up within
the entry hour (triggering the stop) is one with continuation/squeeze momentum — the worst short to
hold. The names that fall immediately (no spike, missed by the stop) are the best shorts. So
"sell into strength" systematically selects continuation-risk losers and discards the easy winners.

## Implication for the documented next blocker

This **removes entry-timing as a reason to backfill Binance sub-hourly OHLC**. The adverse-selection
mechanism is structural (intrabar strength is bad news for a fade short), not Bybit-specific, so a
Binance E1 would very likely replicate the negative. Other execution sub-axes are NOT closed by this:
`E3` liquidity-aware clip-size (reduce market impact, `binance_usdm_bookdepth_1h` available) and `E2`
maker/post-only (needs forward fill-probability data) target *cost/impact*, not entry timing, and
remain open as separate, lower-urgency lines under a future dated amendment.
