# Active Receipt: Continuous Walk-Forward Allocator

**Status:** binding no-adaptive-reweighting policy.

## Decision

The frozen `winner_base` weights stay fixed:

- p3: 0.30
- p4p3: 0.20
- p4p5: 0.40
- tp14: 0.10

The walk-forward allocator did not justify adaptive re-estimation. Frozen weights
are the live/research baseline unless a new pre-registered forward or new-data
program replaces them.

Historical per-quarter choices and haircut calculations are in git history.
