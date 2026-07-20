# R3b correlated-cluster cap — frozen design and A/B registration

Registered 2026-07-20 (tail-risk program P1.3; the 2026-06-20 disaster-stop
study's own unbuilt recommendation, see
`docs/disaster_stop_tail_reconstruction_2026-07-20.md`). The commit adding
this file and `liquidity_migration/cluster_cap.py` is the registration.
Wiring the cap into the live entry-admission path is a **separate deployment
with an operator go and a recorded change point**; until then the decision
layer is staged and nothing imports it.

## Frozen rule

- **Cluster measure:** candidate's trailing **720 h** hourly log-return
  Pearson correlation against each currently OPEN same-direction position,
  requiring ≥ 240 overlapping bars; pairs with less overlap never count
  (young listings are un-clusterable; their count is reported alongside).
- **Cap:** at most **K = 3** open same-direction positions with
  ρ ≥ **0.70** correlated to the candidate; the would-be 4th is refused.
  Entry-side only; existing positions untouched.
- Cell frozen from the declared Lane-1 grid (ρ∈{0.6,0.7}×K∈{2,3}, receipts
  `reports/tail-risk-program/p13-r3b-cluster-caps-lane1-2026-07-20*/`);
  flankers were sensitivity-only.

## Frozen A/B assignment (paper-first)

- **Unit:** individual candidate entry. **Assignment:** trade-id hash parity
  exactly as the passive-execution experiment
  (`sha256(trade_id)` last-byte parity): **arm A = shadow-veto** (decision
  logged, entry proceeds unchanged), **arm B = veto** (entry refused).
- Per-decision metadata (`cluster_cap_arm`, correlated count,
  un-correlatable count) lands on the entry record so the ledger separates
  arms without reconstruction.

## Lane-1 evidence snapshot (seen data; not forward evidence)

- Deployed-shape render book: the cluster state ~never occurs (mean 1.11
  open at entry; 2/2,300 entries with ≥2 correlated opens) — **the cap is
  dormant and free at today's breadth.**
- Stacked barebones book (the study's shape): binds on 2.66% of entries at
  ≈ zero net premium overall (forgone +0.1441 vs avoided −0.1440 over
  3.6 y) with the vetoed entries' losses concentrated exactly on
  common-loss days (−0.036 on native tail days, −0.021 on the registered
  V2 tail set). The cap matters under stacking — including any future
  breadth expansion — and costs nothing until then.

## Metrics (insurance grading, item 27)

Per arm: veto / shadow-veto counts and rates; correlated-count distribution
at entry; subsequent net of shadow-vetoed (arm A) entries — the uncensored
counterfactual — next to arm-B refusals; tail-day concentration of both;
book ES95/ES99 per arm. Not graded on return improvement.

## Pre-committed kill rules for the experiment

- **Y1 — premium runaway:** after ≥ 30 arm-A shadow-vetoed entries, if
  their cumulative subsequent net exceeds +2% of the capital reference
  (the cap would have been refusing winners), stop the experiment and
  record the negative result.
- **Y2 — dormancy:** if 180 days pass with < 10 cap-binding decisions
  across both arms, close as dormant-at-current-breadth (expected at
  today's funnel); the registration stands and re-arms automatically with
  any registered breadth change, re-entering from day 0.
- **Y3 — integrity:** correlation-input staleness (kline gap > 48 h on a
  clustered symbol) suspends decisions fail-open (pass + logged) until
  data recovers; two consecutive weekly checks in that state pause the
  experiment for operator review.

## What this is not

No runtime change today; no return claim; the Lane-1 zero-premium result is
seen-data context, not forward evidence. Real money unaffected.
