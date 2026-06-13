# Active Receipt: Continuous Sniper Staged Entries

**Status:** Tier-2 demo candidate, wired/armed in demo, no placements yet.

## Final Decision

The retained sniper form is a simple fixed staged-entry add-on:

- base continuous winner entry remains unchanged;
- add a quarter-size PostOnly sell limit at entry * 1.08 for fresh base entries;
- attach disaster stop;
- reconcile fills into first-class trade rows;
- cancel/exit with the base lifecycle.

Adaptive/fitted variants were worse out-of-window. The fixed form was kept by
operator decision as a Tier-2 demo candidate, not as promotion evidence.

## Current Live State

`CONTINUOUS_SNIPER=1` is armed in the demo unit; code default remains off. Since
the continuous base book has had zero entries since the 2026-06-09 rebuild, there
have been no sniper placements yet. That is signal-side until the BTC gate is
open and base entries exist.

## Binding Bar

Forward demo evidence decides whether the pessimistic stress modeling was too
harsh. No spent-window re-optimization, no adaptive sniper refits, and no
manual rescue.

Historical amendments and intermediate cells are in git history.
