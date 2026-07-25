# Active evidence contracts

This index lists only contracts that still govern an active evidence stream.
Current strategy evidence and work live in `docs/strategy_program.md`; evidence
policy lives in `docs/governance.md`.

| Surface | Contract | Status |
| --- | --- | --- |
| Sleeve retirement | `sleeve_kill_criteria_2026-07-20.md` | Active weekly demotion/retirement rules for LONG and CONTINUOUS, executable as `liquidity_migration/sleeve_kill_criteria.py` and checkable via `scripts/check_kill_criteria.py`. |
| Passive execution | `passive_execution_experiment_2026-07-20.md` | Active paper A/B, implemented in `liquidity_migration/passive_execution.py`; target is 100 fills per arm before an economics conclusion. Auxiliary standalone demo probe (2026-07-25): `scripts/probe_passive_fill_ab.py`, protocol in `liquidity_migration/passive_fill_probe.py` — bounds the mechanism fast; does not conclude H. |

Both remaining contracts have a live executable form. A contract with no verifier
does not belong here — it is history, and history lives in Git.

## Withdrawn on 2026-07-25

**`basket_short_tail_experiment_2026-07-25.md` was withdrawn before start** —
no arms implemented, no forward data consumed, no peek. The in-file note
records the measured reasons; the Phase 1 re-screen has since priced the
basket-short structure negative on both venues (anomaly research §17.2).
Prospective re-registration stays open per `docs/governance.md` if a
short-book substrate ever survives at measured cost.

## Removed on 2026-07-24

**The prospective runtime-parity execution epoch is fully retired**: the
comparator, the `forward_epoch_start` collector, `venue_lifecycle`, and all eight
`prospective_runtime_parity_execution_epoch_2026-07-18*` contracts. Published
start and verification receipts still exist on disk and on the VPS, but nothing
in this checkout reads or validates them, and the parity comparison cannot be
reproduced without recovering the tooling from Git history.

The epoch's calibration/validation window survives only as a plain date range —
2026-07-19 14:00 UTC through 2026-10-17 14:00 UTC — because
`sleeve_kill_criteria_2026-07-20.md` measures against it.

**Strategy Overhaul V2 is fully retired**: the aggregate analyser, the
prospective full-ledger replay runner, and every V2 contract, baseline, and
diagnostic epoch. `scripts/build_candidate_tape.py` no longer hashes a contract;
its `--contract` / `--base-contract` arguments were removed with the file they
defaulted to.

Git history remains the audit trail for every removed file.
