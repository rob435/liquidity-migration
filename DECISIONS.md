# Decisions

## Active Decisions

- Treat 22:00-23:00 TWAP daily-close fade as the current research lead.
- Rank at 22:00 only. Ranking at 23:00 while filling from 22:00 is lookahead.
- Use average entry across 1m slices for PnL and the 20% disaster stop.
- Activate the disaster stop from first fill + 15m.
- Activate adaptive profit protection only after final add + 15m.
- Flatten the whole symbol on exit and do not re-enter the same symbol that day.
- Keep the 31-150 prior-liquidity bucket as the core research universe.
- Keep 80% max single-symbol basket weight as a research candidate, not
  real-money approval.
- Block forward/demo TWAP entries until slice-level execution exists.
- Treat current-top-160 backtests as biased benchmarks until PIT archive testing
  is complete.

## Rejected

- Do not promote pre-TWAP results anymore.
- Do not use fixed take-profit as the default; it cut too much right tail in
  prior tests.
- Do not rebuild the old composite aggression/carry/momentum/live stack.
- Do not silently blend volume-alpha and daily-close fade.
- Do not call Bybit demo fills proof of alpha.

## Historical Context

Earlier volume-alpha and pre-TWAP daily-close studies are kept only as background.
The current repo should optimize around the TWAP daily-close proof path.
