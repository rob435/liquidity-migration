# Strategy-Overhaul Migration Audit

Updated 2026-07-16. This records the selective transfer from the retired dirty
overhaul workspace into `codex/demo-operational-cutover`. It is not a claim that
the overhaul completed and it does not make its artifacts current.

## Selection boundary

The source was an uncommitted 108-path workspace rooted at
`4b598d7103fd6fa3960300954c7a09fa9e432cf5`. The operational branch had moved
substantially beyond that point. Files were therefore classified by invariant
and current consumer, not copied by pathname and not merged as a second
architecture.

The source workspace remains untouched. The audit found roughly 113,000 added
lines, including 43 `strategy_overhaul*` production modules and a large cyclic
artifact/receipt graph. Size, test count, and receipt count were not treated as
evidence of usefulness.

## Transferred

| Source lesson or defect | Operational owner after transfer |
| --- | --- |
| Valid Unicode Binance symbols were dropped or unsafe in URLs/partitions | Strict symbol identity, URL quoting, reversible partition encoding, and Unicode integration tests |
| S3 listing pages could lose Unicode or stop on a truncated page with no locally matching key | Pure UTF-8 page parser, mandatory `IsTruncated`, explicit continuation, and repeated/non-advancing marker refusal |
| Bybit instruments-info API errors could be interpreted as an empty universe | Pure page parser that requires `retCode=0`, a valid result/list/cursor, and advancing pagination |
| Bybit archive observation, current-listing inference, and kline coverage were easy to conflate | Manifest evidence-class columns with explicit inference and limitation fields; coverage validation still cannot rewrite independent membership |
| Binance rebuild accumulated the full archive in memory and removed live klines before a replacement manifest pair was ready | Bounded download/staging batches, persisted-pair verification, locked two-dataset publication, rollback, and an incomplete-publication refusal marker |
| Full-PIT shell entry points accepted accidental positional input and hid useful strictness controls | Argument refusal/help plus explicit Binance batch/failure-ratio and manifest symbol/provenance checks |
| COMMON4 could drift between the factor owner and RMOM precompute | One exported factor-column tuple consumed by the precompute |
| RMOM stable/provisional ownership was embedded inside filesystem orchestration | A pure residual-to-signal boundary with explicit provisional state, retained by the operational precompute |
| The long runs contained decision-useful diagnostics and four bounded untested questions | Retired-lessons record with exact outcome limits, estimands, dependence, and multiplicity constraints |

## Already solved more cleanly on the operational branch

These lessons were retained through the newer implementation; importing the old
modules would have regressed ownership:

- same-symbol component and sleeve targets aggregate before venue submission;
- lifecycle and protection clocks derive from attributable confirmed fills;
- account journals, reconciliation, leases, and paper/demo ownership have one
  operational authority instead of parallel overhaul owners;
- consumers distinguish immutable stable RMOM rows from a replaceable
  provisional tail;
- unmeasured LONG MAE/MFE stays missing rather than becoming a favorable zero;
- paper remains an uncalibrated integration simulator;
- Bybit independent PIT membership is validated against coverage rather than
  destructively rebuilt from observed bars.

## Deliberately excluded

- The 43-module `strategy_overhaul*` graph, frozen-inventory APIs, acquisition
  lineage graph, artifact registry, semantic-receipt hierarchy, supervisor
  graph, and checkpoint/recovery framework. Their useful ingestion invariants
  were implemented at existing operational boundaries instead.
- S02 feature tapes, S03 entry anchors, S04 labels, estimates, PnL, and verdicts:
  none completed, so there is no result to migrate.
- Preregistration templates, operator commands, and state/Graphify snapshots
  whose only consumer was the retired graph.
- A source-bound COMMON4 panel builder created for the abandoned artifact
  workflow. It had no operational caller; only the shared factor definition was
  retained.
- Broad parameter atlases, synthetic missing paths, or any rule change inferred
  from partial receipts. They were neither run nor authorized.

## Interpretation

This is an information-and-invariant injection, not an append of the old tree.
An excluded file can still contain thoughtful work; exclusion means its value
was either already present, captured in a smaller current owner, inseparable
from retired machinery, or unsupported by a completed outcome. See
`docs/strategy_overhaul_lessons.md` for the empirical boundary.
