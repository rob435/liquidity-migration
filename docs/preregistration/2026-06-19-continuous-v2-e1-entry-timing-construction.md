# Continuous V2 E1 Intrabar Entry-Timing Diagnostic — Construction (Pre-Registration)

Date: 2026-06-19

Parent plan: `docs/preregistration/2026-06-18-continuous-v2-ab-research-plan.md` (Problem Book E).
Scope: **Bybit-only exploratory** execution diagnostic. `claimed_venue_scope=bybit_only_execution_exploratory`
(single-venue: Binance USD-M has no sub-hourly OHLC in the full-PIT root). Run label `exploratory`;
cannot be a both-venue candidate. No real-money claim.

## Why

Execution/cost is the one axis that does not fight the diffuse v2 signal (the plan's high-priority
axis), and "E1 intrabar entry timing if sub-hourly coverage passes" is on the recommended first
wave. Bybit `klines_5m` covers 2023-04-01 → 2026-05-01 (611 symbols). A feasibility probe on a
time-spread sample showed real room: median intrabar upside above the entry = 1.40% (p90 6.2%);
a +0.5% sell-into-strength stop fills ~80% of shorts. The question this answers: **does selling
the short into intrabar strength net-improve outcomes after accounting for the trades you miss
and adverse selection?**

## Construction (fixed before running)

Per Bybit `V2_CONTROL` short trade with 5m data in its entry hour:

- Reference = realized `entry_price` (P0). Predeclared **causal stop-short** at `L = P0·(1+δ)`,
  δ ∈ {0.25%, 0.5%, 1%}. The entry hour is `[entry_ts_ms, entry_ts_ms+1h)`.
- **Filled** if any entry-hour 5m bar `high ≥ L` (a resting stop-short fills at L when price trades
  up through it — causal, no look-ahead). New short entry = L (a higher/better entry).
- **Missed** if no 5m high reaches L in the hour → the trade is NOT taken (foregone PnL).
- Outcome recompute (first-order, per-trade diagnostic — not a full lifecycle re-sim):
  - `take_profit` exits: new gross = take_profit_pct (TP is entry-relative, so a higher entry does
    not change the % gain). Entry timing does **not** help TP trades.
  - time exits (24h / max_hold): new gross = `(L − exit_price)/L` (higher entry → higher short gross),
    exit_price held fixed. Cost/funding held at control values (first-order).
  - Limitation (stated): does not re-trigger TP/timer on the new entry path; a full lifecycle E1
    needs the engine. This diagnostic is the gate that decides whether that build is worth it.
- **Missed-fill accounting:** missed trades contribute 0 vs the control's `net_return` (foregone).
  Net effect = Σ notional-weighted [filled Δnet] + Σ notional-weighted [−control net for missed].
- **Adverse-selection check:** compare control `net_return` of filled vs missed trades. If missed
  trades (no intrabar spike) were the *better* shorts, E1 systematically skips winners.
- **Null control:** enter at a uniformly random entry-hour 5m bar close (no "into strength" skill);
  the stop rule must beat this null to claim timing skill.

## Falsifiers (any one closes E1)

- Net effect ≤ 0 after missed-fill accounting (entry improvement eaten by foregone winners).
- Adverse selection: missed trades have materially higher control net_return.
- The stop rule does not beat the random-5m-bar null.
- The improvement is concentrated in TP trades (where entry timing is mechanically irrelevant).

## Evidence limit

Bybit-only exploratory. A positive, null-beating, adverse-selection-clean result would justify a
full lifecycle E1 build (engine re-sim) and a Binance ≤5m OHLC backfill to test the two-venue bar —
nothing more. Negative closes the execution-timing axis on the venue where sub-hourly data exists.
