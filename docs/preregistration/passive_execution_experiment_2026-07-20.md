# Passive-execution experiment — registered 2026-07-20

Lane-1 design registered with the first measured cost report; no runtime
change is authorized by this document. The commit that later carries the
paper implementation is that config's registration.

## The measured motivation (first cost report, 2026-07-20)

`scripts/build_execution_cost_report.py` over the demo account journal +
capture root (23 of 33 fills measured, 1,120 USDT total notional — a small
sample, stated as such):

- effective spread paid: **3.76 bps** notional-weighted, against a quoted
  spread of 4.91 bps (taker at ~77% of quoted);
- price impact: **+7.0 bps at 1s**, then **−14.7 bps at 15s** and
  **−22.0 bps at 1m** (the 5m figure, −156 bps, is dominated by a few
  volatile fills and is not load-bearing);
- realized spread (what a passive counterparty to our fills earned):
  **+19.4 bps at 15s, +25.7 bps at 1m**.

Reading: on this sample our taker fills show *no adverse selection* — the
mid reverts through our fill price within seconds. Every entry paid the
spread plus taker fees (5.5 bps) to demand liquidity the market would have
given back moments later. The measurable upper bound for a maker-first
policy on this flow is roughly **25–30 bps per side** (spread + fee delta +
captured reversion). This is an execution-cost observation, not a signal claim;
the current strategy evidence boundary is in `docs/strategy_program.md`.

## Hypothesis

H: For CONTINUOUS entries (fade sells into pumps, one-hour decision
cadence), a post-only limit at the touch with a bounded chase-and-timeout
fallback achieves ≥ 60% passive fill rate and improves all-in per-side cost
by ≥ 10 bps versus the deployed market-IOC policy, without materially
degrading signal capture (fills within the same decision hour).

## Design

- **Arms.** A: deployed market-IOC (control, unchanged). B: post-only limit
  at the touch, re-pegged on touch move, falling back to market-IOC after
  `T = 20s` or on price moving `> 10 bps` through the limit. Parameters
  frozen here; changing them is a new registration.
- **Surface.** Paper account owner first (`integration_only_uncalibrated`
  scope is exactly right for an execution-mechanics experiment); demo only
  after a five-line promotion note.
- **Assignment.** Deterministic alternation by entry `trade_id` hash parity
  — no discretion in which orders get which arm.
- **Metrics.** Per-fill effective spread, 15s/1m price impact and realized
  spread (this report), passive fill rate, fallback rate, time-to-fill, and
  missed-fill opportunity cost (signal P&L of entries whose passive order
  never filled, measured at the decision-hour close).
- **Sample target.** ≥ 100 fills per arm before any read beyond mechanics
  (at current fill rates this is the binding constraint; the breadth
  program raises it).
- **Kill rule.** The experiment stops early if arm B's missed-fill
  opportunity cost exceeds its measured cost saving over any 50-entry
  window.

## Implementation status

Arm B shipped 2026-07-20 in `liquidity_migration/passive_execution.py`
(paper owner only; the implementing commit is the config registration). The
fill model is deliberately conservative: strict opposite-touch crossing with
no queue-position credit, so the measured passive fill rate is a lower
bound. Fills carry `execution_arm`, passive limit/re-peg/fallback metadata,
and the cost report groups by arm. Restart safety: pending passive state is
in-memory and the paper owner terminal-cancels orphaned working orders at
startup before convergence re-plans.

## What this does not show

The 23-fill baseline cannot establish the reversion is stable; it
establishes only that the cost telescope now exists and that the first look
justifies the experiment. The cost report should be re-run
(read-only, any time) as fills accumulate, and its notional-weighted
numbers travel with every future execution claim.

## Auxiliary instrument — standalone demo probe (added 2026-07-25)

The in-flow A/B above is the graded instrument, and §16.3 of
`docs/anomaly_research_2026-07-24.md` showed why it is slow: CONTINUOUS holds
roughly one position on 38% of days, so 100 fills per arm is months of accrual.
`scripts/probe_passive_fill_ab.py` (protocol pre-declared in
`liquidity_migration/passive_fill_probe.py`) is a complementary, operator-run
probe that manufactures a powered sample in hours with its own min-notional
demo orders: hash-allocated taker/post-only arms, intention-to-treat cost vs
the decision mid (an unfilled passive attempt is charged the taker fallback at
the terminal quote), fees from observed executions, adverse selection at +30 s,
and written kill criteria.

Scope boundary, fixed here: probe attempts sample ordinary market states while
CONTINUOUS entries sample pumps, so the probe **bounds the mechanism** (is the
passive floor mechanically reachable at all, and at what base-rate cost) and
cannot conclude H. Its 60 s window strictly contains arm B's 20 s chase window,
so fill-rate-at-20s is derivable from the recorded time-to-fill curve and the
two instruments stay comparable. This section changes nothing about the frozen
arm design above.
