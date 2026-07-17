# Strategy Overhaul V2 Phase-3 Structural Checkpoint — 2026-07-17

This is a post-run engineering and integrity receipt, not a preregistration,
alpha result, evidence card, or deployment decision. It does not amend the
frozen contract consumed by the artifact.

## Boundaries

- Frozen contract: `docs/preregistration/strategy_overhaul_v2_diagnostic_epoch_2026-07-17.md`,
  artifact-bound SHA-256
  `9b522bb09bc08e36eb8cdddcbc47d915fc580499895879c2d10070b4fe090879`.
- Source window: Bybit `[2026-07-05, 2026-07-06)` UTC; raw read window
  `[2026-03-07, 2026-07-10)` UTC.
- Corrected code identity: `e126ecc30cdeda794593f5d43d2d707127f70480`,
  clean `main` worktree at generation.
- No data/RMOM refresh, active LONG/CONTINUOUS backtest, research refresh, or
  equity-curve command ran. The rejected residual-momentum file was hashed but
  not consumed because it lacks `is_provisional` provenance.
- Native Windows Python 3.13.6 used the exact lock environment. A process-local
  no-op `fcntl` import shim was required only because read-only strategy imports
  transitively load POSIX account modules; the candidate command did not invoke
  their locks, journal, account, venue, or execution paths.

## Immutable baseline preflight

The one baseline artifact check at `2026-07-17T19:24:52.3929778Z` found zero of
23 pinned files locally: 23 missing, zero matching, and zero hash mismatches.
The pinned commit exists and the relevant profile source files were unchanged,
but the absent artifacts disable active-result comparison. No baseline file was
regenerated, copied, or replaced.

## Engineering checks

- Writer-off/on fixtures preserve exact LONG candidate order, target intents,
  and numerical fields, and exact CONTINUOUS panel, entry, trade, and lifecycle
  output. Raising writers also leave active outputs unchanged.
- JSONL source/transition tests cover restart duplicate suppression and reject a
  transition without its source.
- Whole-repository Ruff passed; targeted mypy and compilation passed; nine new
  funnel/candidate tests passed. A focused existing strategy slice had 52
  passes. Six additional selected tests reached pre-existing POSIX durability
  assumptions (`fsync`/stable-file reads) that native Windows cannot validate;
  none failed a funnel assertion.
- The amended cumulative Phase-1--3 production total is 3,772 net lines, below
  the prospective 3,850-line ceiling. This does not retroactively erase the
  original 1,500-line budget breach discovered after Phase 2.

## Failed attempt retained

The first clean attempt at commit `5d3c7e1` stopped after input loading because
raw OHLC contained nulls. It completed no sleeve checkpoint and produced no
candidate or label artifact. Its checkpoint run-identity SHA-256 is
`36ef4e52f97bdc0ddd66f5dd51a76f99dd75727a9dcf6a5be9de9b84e68ad67f`
and remains under the ignored run root as
`.date=2026-07-05.failed-5d3c7e1-invalid-ohlc/`. The repair explicitly counts
and rejects invalid OHLC rows instead of coercing them.

## Successful bounded partition

The corrected run completed at 2026-07-17 20:09 UTC in 42.7 seconds. The output
is the ignored run-scoped directory
`reports/strategy-overhaul-v2/diagnostic-epoch-2026-07-17/bybit/date=2026-07-05/`.

- Run-identity SHA-256:
  `d42d109b2630a39a9028fcca9a9476fb23a7f9b0574ccb947b3a601feba0913d`.
- Manifest payload SHA-256:
  `4a368624ec813e72ade9de62b15a365b8a279221baf6ecf86abde542ed6d2c93`;
  manifest-file SHA-256:
  `b5d1985d636dcad2d161c81c93e00ace6d6b8307f87f3ff08c023c34dd87da38`.
- Input identities: 70,430 kline files / 332,122,305 bytes / aggregate
  `9960af67398381693dcaa68c8e91d7f36757c4cf5146bd9a96f89d9f84c812cb`;
  two PIT-manifest files / 27,227 bytes / aggregate
  `3e9b9bf028a89cec07c4b75b4b6d39adfac01c0ff56ccbad3ff6576737e28dbd`.
- Input quality: 1,690,320 raw hourly rows, 956 explicitly rejected invalid
  OHLC rows, and 1,689,364 usable rows.
- Funnel structure: 11 LONG plus 1,440 CONTINUOUS source rows; 1,451 rows and
  1,451 independently checked unique keys; 211 barebones-admitted keys.
- Label structure: exactly 211 unique keys, matching admitted funnel keys; all
  registered horizons satisfy the at-most-one-hour observation-lag rule.
- `decision_funnel.parquet`: 155,421 bytes, SHA-256
  `53a8f7a572a9251e36158563fe6ef1767c93ab098090dce7ade69d8703a7159a`.
- `path_labels.parquet`: 24,700 bytes, SHA-256
  `d175f41f802d8bc393631cd76a3dbd9fd7f9bfc630e7d41d9e8346f5f3486a2b`.

The manifest records `outcomes_inspected=false`. No return, MAE, MFE, P&L,
effect, distribution, characteristic ranking, or active comparison was opened
or summarized. Phase 3 remains in progress: do not append a partition, build a
portfolio curve, or nominate a thesis until a separately authorized diagnostic
read follows the frozen order.
