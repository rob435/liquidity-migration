# Prospective Runtime Parity Execution Epoch: Post-21 Amendments

This file extends the frozen base contract and Amendments 1--21 without
changing any earlier file. A runner using an amendment here must bind this
file by exact SHA-256 in addition to the earlier contract identities.

## Amendment 22: repair-aware prefix semantic equivalence

Registered 2026-07-19 after the replacement comparator completed all 29,449
registered hourly cycles but failed the pre-boundary byte-identity guard, and
before implementation, affected tests, or another comparator run. The
create-only failure receipt is
`reports/prospective-runtime-parity-execution-epoch-2026-07-18/runtime-parity/.active-production-comparator.working-8f3fd034d199/termination.json`
with SHA-256
`b4f7e4a383e475eea4ddcf05b2d98de7a15c724cd6321a3c57a8101c6d16f4e7`.
No monetary, return, alpha, cost, or thesis outcome was inspected. The failed
run remains invalid for those conclusions and its observed data is spent.

The original prefix guard requires byte identity to the pre-repair trace. That
was appropriate for the performance-only refactor it was introduced to
protect. It is impossible for the intentional XRP exit repair to satisfy that
guard: removing the formerly deadlocked XRP continuous exposure changes the
shared account state subsequently observed by the LONG funnel. The failed run
showed this only as structural trace differences; it did not change any LONG
acceptance or first-rejection decision.

The replacement guard must compare the new trace against the preserved
pre-repair baseline at
`reports/prospective-runtime-parity-execution-epoch-2026-07-18/runtime-parity/.active-production-comparator.working-d54eb524c208-boundary-flat-xrp`.
Every baseline file must first match the 21 SHA-256 identities already frozen
in the runner. For each registered file, schema, row count, row order, and all
values are exact except for the following narrowly permitted derived-state
changes in LONG rows whose symbol is exactly `XRPUSDT`:

- `gate_existing_exposure` may change only from `fail` in the baseline to
  `pass` in the repaired run;
- `gate_cooldown` may change only from `pass` in the baseline to `fail` in the
  repaired run; and
- `gate_state_sha256` may differ only on a row where at least one of those two
  permitted gate values differs.

All non-XRP rows, all continuous-gate rows, source fields, keys, timestamps,
gate values not named above, rejection keys, `first_rejection`, and
`barebones_accepted` must remain exact. A changed derived hash without an
allowed gate transition, an allowed gate transition without a changed derived
hash, a transition in the opposite direction, or any other difference is a
structural failure. At least one `gate_existing_exposure` transition must be
observed so that the guard cannot silently fall back to the pre-repair
behavior. The receipt must report file-level and transition-level counts and
whether exact acceptance and first-rejection equivalence passed.

This amendment changes only the structural equivalence test needed to assess
an explicitly registered execution-accounting repair. It grants no alpha,
cost, thesis, deployment, or real-money conclusion. The replacement run still
must satisfy every terminal-flat, owner-admission, journal, provenance, PIT,
lifecycle, BTC, and create-only receipt gate registered previously.
