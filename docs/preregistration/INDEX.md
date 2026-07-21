# Active evidence contracts

This index lists only contracts that still govern an active evidence stream or
are required inputs to a surviving replay. Current strategy evidence and work
live in `docs/strategy_program.md`; evidence policy lives in
`docs/governance.md`.

| Surface | Contract | Status |
| --- | --- | --- |
| Runtime-parity forward stream | `prospective_runtime_parity_execution_epoch_2026-07-18.md`, `prospective_runtime_parity_execution_epoch_2026-07-18_amendments.md`, and `prospective_runtime_parity_execution_epoch_2026-07-18_post17_amendments.md` through `post22_amendments.md` | Active rolling evidence contract; historical 45/45 read remains on file, but rolling post-commit evidence is authoritative. |
| Sleeve retirement | `sleeve_kill_criteria_2026-07-20.md` | Active weekly demotion/retirement rules for LONG and CONTINUOUS. |
| Passive execution | `passive_execution_experiment_2026-07-20.md` | Active paper A/B; target is 100 fills per arm before an economics conclusion. |

## Compatibility inputs, not active research

The following immutable V2 contracts remain only because
`scripts/build_candidate_tape.py`, `scripts/analyze_strategy_overhaul_v2.py`,
and the prospective full-ledger replay verify their exact hashes:

- `strategy_overhaul_v2_diagnostic_epoch_2026-07-17.md`
- `strategy_overhaul_v2_completion_cycle_2026-07-17.md`
- `strategy_overhaul_v2_phase3_replay_recovery_2026-07-18.md`
- `strategy_overhaul_v2_phase3_buffered_replay_recovery_2026-07-18.md`

They do not define a current roadmap and must not be reopened merely because
the files remain. Historical drafts, queues, receipts, and raw report trees
were removed from the working tree on 2026-07-21 after their useful conclusions
were consolidated. Git history remains the audit trail.
