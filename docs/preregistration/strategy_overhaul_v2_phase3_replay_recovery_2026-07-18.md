# Strategy Overhaul V2 Phase-3 Replay Recovery

Registered on 2026-07-18 before the recovery run. This is an engineering-only
addendum to
`strategy_overhaul_v2_completion_cycle_2026-07-17.md` (SHA-256
`702ab2e84e0c6acdc5c14acd251a60a63f8fdca68928b0109b2d440999876cc8`).
It does not create a new discovery sample or independent validation.

## Preserved failure and spent-data boundary

The committed evaluator at
`419163a68b94b5a076f2eeb930909a8007461101` began at 2026-07-17 23:22:32
Europe/London. All candidate-manifest, contract, structural, raw-kline, RMOM,
and funding identities passed before registered discovery outcomes were opened.
The discovery surface is therefore spent.

The run was terminated at approximately 74 wall minutes because its observed
CONTINUOUS transaction throughput projected completion beyond the cumulative
two-hour stage stop. The tool wrapper initially stopped waiting without killing
the child; both exact analyzer PIDs and command lines were verified before the
remaining process tree was killed. The partial root was renamed, not deleted:

`reports/strategy-overhaul-v2/diagnostic-epoch-2026-07-17/.phase3-analysis.failed-fsync-2026-07-18`

It contains 15,047 files and 228,727,861 bytes: 9,606 completed LONG
transaction segments through the allowed 2024-12-03 lifecycle tail, and 5,439
partial CONTINUOUS segments through 2022-04-15. Starting CONTINUOUS implies the
LONG journal, fill count, event chain, and final-flat checks had passed. Only
process/file counts and account event types/timestamps were inspected for
progress; no return, path, characteristic, portfolio, or candidate-score value
was inspected. The partial root is a failure receipt and is not an input to the
retry.

## Frozen recovery change

The suspected bottleneck is Windows flush latency from two research-adapter
`os.fsync` calls on every account transaction: one after atomic segment writes
and one after projection appends. The recovery commit may remove exactly those
two calls. It must retain the process-local mutex, write-complete loops, atomic
segment replacement, append-only projection, per-transaction files, canonical
event bytes, event/state hash chains, and end-of-run journal verification.

This deliberately abandons crash-durability evidence for the ignored Windows
research root. It does not replace or change the production account kernel,
reducer, execution twin, strategy events, decisions, prices, lifecycle, costs,
funding, arithmetic, discovery estimators, selection rule, or four final
payloads. It grants no Linux, concurrency, runtime, demo, paper, mainnet, or
real-money claim.

The retry starts from the same 43 immutable discovery partitions, original
contracts, evaluator logic, and bootstrap seed. It must record this addendum's
path and SHA-256 in preflight/final identity, and must use clean committed code.
The preserved failure directory remains untouched.

## Recovery stop and interpretation

The recovery run has a 45-minute wall-clock stop from process start. Together
with the approximately 74-minute failed attempt, this keeps cumulative Phase-3
analysis compute below two hours. If it does not atomically publish and verify
the original four-payload analysis by then, stop and preserve it; do not make a
third same-surface attempt without another prospective record.

A successful retry is the completion of the original exploratory analysis, not
a replication. Phase-4 selection remains mechanical under the original rule,
and the reserved holdout remains untouched until a thesis-specific contract is
committed.
