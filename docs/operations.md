# Operations

Use `scripts/ops.sh` as the single human-facing entry point for routine
demo/paper operations. It is a thin router: the existing canonical scripts
still own strategy, research-integrity, reset, and deploy logic.

Running it with no arguments, `help`, `-h`, or `--help` only prints help.

```bash
scripts/ops.sh
scripts/ops.sh status
scripts/ops.sh equity --sleeves long,continuous
scripts/ops.sh data-audit --venue both
scripts/ops.sh overhaul-plan
scripts/ops.sh overhaul-phase0
scripts/ops.sh account-parity --help
scripts/ops.sh clock-offset --execute --output /absolute/vps/path/clock-offset.json
scripts/ops.sh demo-calibration --execute --help
scripts/ops.sh venue-accounting --help
scripts/ops.sh cutover-authority --help
scripts/ops.sh test -q
```

## Safety boundary

- This interface never sets `REAL_MONEY` and never enables mainnet trading.
- `status` calls the read-only VPS verifier.
- A ledger `reset` is an explicit remote dry-run unless `--execute` is passed.
  The reset script still independently refuses mainnet credentials, concurrent
  resets, non-flat positions, open orders, and mismatched systemd credential
  files before it removes anything.
- `clock-offset` and `demo-calibration` run on the VPS and require `--execute`.
  The first writes a public-time receipt; the second publishes demo targets but
  receives no API credentials and cannot bypass the account owner.
- `deploy` refuses unless the first argument after the command is exactly
  `--execute`. The checked deploy script retains its own commit, test, config,
  credential, and service gates.
- Research commands produce research artifacts only. No command here promotes a
  strategy, changes a decision rule, or authorizes real money.
- `data-build` refuses unless its first argument is `--execute`; the underlying
  tool also requires explicit datasets, window, symbol authority, and a new
  immutable receipt outside both data roots.

## Commands

| Command | Canonical route | Operational effect |
|---|---|---|
| `status` | `scripts/verify_vps_live.sh` | Read-only VPS checkout, config, credential, service, and liveness checks. |
| `account-parity` | `python -m liquidity_migration.kernel_parity` | Structural historical/paper/demo account-journal comparison with non-empty and hash checks; not full captured-tape acceptance. |
| `clock-offset --execute` | VPS `scripts/capture_bybit_clock_offset.py` | Writes a self-hashed, NTP-gated VPS-vs-Bybit public clock receipt. |
| `demo-calibration --execute` | VPS `scripts/run_demo_execution_calibration.py` | Emits the preregistered tiny target-only demo sequence through the account inbox; never direct venue execution. |
| `twin-calibrate` | `scripts/calibrate_execution_twin.py` | Self-hashed market-order twin calibration from verified demo account/L2 tapes; exits nonzero until registered sample gates pass. |
| `venue-accounting` | `scripts/reconcile_bybit_demo_accounting.py` | Owner-serialized, venue-read-only demo TRADE/closed-PnL/SETTLEMENT and flatness reconciliation against the stopped canonical journal. |
| `cutover-authority` | `scripts/account_execution_cutover_authority.py` | Creates reviewed-evidence wrappers, an open assessment template, or the short-lived host/commit/evidence-bound deploy authorization. It never decides a non-machine-verifiable gate by itself. |
| `equity` | `scripts/equity_curves.sh` | Official LONG/CONTINUOUS equity runner; forwards every option unchanged. |
| `reset` | VPS `scripts/reset_demo_paper_ledgers.sh` | Dry-run preview by default; `--execute` is the only mutation opt-in. |
| `data-audit` | `scripts/granular_data_surface.py` | Read-only PIT-manifest-anchored granular/alternative-data coverage and schema audit. |
| `data-build --execute` | `scripts/granular_data_surface.py --execute` | Explicit resume-safe granular backfill; refused without the handshake and immutable receipt path. |
| `tail-plan` | `continuous_tail_survival_2026_07_10.py --plan` | Checks the frozen preregistration, worktree, roots, partitions, and both-venue readiness without running cells. |
| `tail-run` | `continuous_tail_survival_2026_07_10.py` | Runs only the preregistered cells and preserves the dispatcher's integrity/refusal gates. |
| `overhaul-plan` | `strategy_overhaul_scout_2026_07_10.py --plan` | Shallow outcome-blind partition/source/config preflight; does not write the real S00 inventory. |
| `overhaul-phase0` | `strategy_overhaul_scout_2026_07_10.py --phase0-inventory` | Writes a content-addressed, outcome-blind schema/key/provenance/resource diagnostic bundle. It neither establishes S01 readiness nor creates a canonical root/stage receipt. Exit 2 can preserve a useful `PARTIAL` bundle. |
| `test` | `python -m pytest` | Runs all tests, or only the forwarded pytest selection. |
| `deploy --execute` | `scripts/deploy_vps_live.sh` | Checked demo/paper VPS deploy. Refused without the explicit handshake. |

Every argument after the command is forwarded as its own argument. Do not put a
Python command plus flags into `PYTHON`; it must name one executable or
executable path.

## VPS status and account-journal parity

```bash
# Read-only production verification.
scripts/ops.sh status

# Structural account-journal comparison; supply all three roots.
scripts/ops.sh account-parity \
  --environment historical=/path/to/historical-account-root \
  --environment paper=/path/to/paper-account-root \
  --environment demo=/path/to/demo-account-root \
  --output /path/to/account-kernel-parity.json
```

The old `reconcile quick` and `reconcile full` routes were removed on
2026-07-13. They compared sleeve-local idealized-fill projections that the
target-only paper/demo architecture no longer treats as authoritative. Keeping
them would make a green report easier to produce without validating the account
owner.

`account-parity` refuses empty journals and compares decision keys, rejection
keys, target quantities, event-type sequence, and replayed account-state hashes.
It proves only those structural claims for the supplied journal bytes. It does
not establish captured market-tape provenance, full strategy parity, fresh
venue rules, credentialed demo execution, fill/P&L agreement, alpha, or
deployment readiness. Those acceptance gates remain open in
`docs/account_execution_cutover.md`; the cutover-authority command binds an
explicit operator assessment after review, but is intentionally not a
one-command automatic green verdict.

Calibrate only from a fresh demo epoch after actual target/order/ack/fill/P&L
and raw L2 capture exist:

```bash
scripts/ops.sh clock-offset --execute \
  --output /absolute/vps/path/clock-offset.json

scripts/ops.sh twin-calibrate \
  --account-root /path/to/demo-account-root \
  --market-capture-root /path/to/demo-capture-root \
  --account-id bybit-demo-unified \
  --clock-offset-receipt /path/to/clock-offset.json \
  --output /path/to/execution-twin-calibration.json
```

Paper startup consumes only a self-hashed receipt whose registered sample gate
passed. The receipt does not authorize deployment.

The optional bounded sample generator is separately preregistered in
`docs/preregistration/account_execution_calibration_v2_2026_07_13.md`. It supplies
execution-twin observations efficiently but does not replace actual
LONG/CONTINUOUS strategy-tape comparison.

After the demo target set and venue are flat, stop producers, let the owner
complete its final strict funding/position pass, write fresh health, and stop
the owner. Then capture the exact fresh-ledger epoch (at most seven days):

```bash
scripts/ops.sh venue-accounting \
  --account-root /path/to/demo-account-root \
  --account-id bybit-demo-unified \
  --start-time-ms FRESH_EPOCH_START_MS \
  --output /path/to/venue-accounting.json
```

The default floors—registered before the venue result is viewed—are two trade
rows, one closed-PnL row, and one funding settlement. The command acquires the
owner lease, submits no orders, binds raw Bybit rows and pre/post position/open
order snapshots, replays the journal, and exits nonzero on identity, fee, P&L,
funding, lineage, or flatness disagreement. It does not allocate account-netted
P&L to components or authorize deployment.

## Evidence-bound cutover authorization

`account-execution-deploy-ready` is a JSON authorization receipt, not an empty
marker. Start from an intentionally open assessment on the staged, clean VPS
checkout:

```bash
COMMIT="$(git rev-parse HEAD)"
scripts/ops.sh cutover-authority template \
  --authorized-commit "$COMMIT" \
  --authorized-by OWNER_REVIEW_ID \
  --output /etc/liquidity-migration/account-execution-cutover-assessment.json
```

The demo-rule, fresh demo/paper owner-health, execution-twin calibration,
kernel-parity, venue-accounting, and final-flatness roles are parsed and
semantically checked by the issuer. The accounting and flatness roles may point
to the same self-hashed venue receipt. Owner health must be no older than five
minutes and bound to the current journal head at issuance; prepare the open
assessment before stopping the owners. For the remaining claim-scoped gates,
snapshot the exact reviewed source files into self-hashed evidence wrappers;
this records human judgment and source hashes without pretending the judgment
was automated:

```bash
scripts/ops.sh cutover-authority review-evidence \
  --role demo_owner_start_sequence \
  --claim 'OWNER_ACTIVE_AND_HEALTHY_BEFORE_ANY_DEMO_PRODUCER_START' \
  --reviewed-by OWNER_REVIEW_ID \
  --source /absolute/path/to/demo-systemd-start-order.log \
  --output /absolute/path/to/demo-owner-start-evidence.json
```

Populate every template path and decision, and change a gate from `open` to
`passed` only after its registered decision rule actually passes. Then issue
the receipt on the same host and exact clean commit:

```bash
scripts/ops.sh cutover-authority issue \
  --assessment /etc/liquidity-migration/account-execution-cutover-assessment.json \
  --repo-root /opt/liquidity-migration \
  --output /etc/liquidity-migration/account-execution-deploy-ready

scripts/ops.sh cutover-authority verify \
  --receipt /etc/liquidity-migration/account-execution-deploy-ready \
  --expected-commit "$COMMIT" \
  --repo-root /opt/liquidity-migration
```

The mode-`0600` receipt expires within 24 hours and is bound to the host's
machine-id fingerprint, full commit, assessment bytes, and every evidence
artifact hash. Its self-hash is corruption/tamper evidence, not a signature;
root can still replace it, so operator identity and substantive review remain
real responsibilities.

## Safe ledger reset

Preview exactly what would be archived and removed on the VPS:

```bash
scripts/ops.sh reset --sleeves all --label new-forward-window
```

Only after reviewing that preview, request the guarded mutation explicitly:

```bash
scripts/ops.sh reset --execute --sleeves all --label new-forward-window
```

The remote checkout defaults to `/opt/liquidity-migration`. Reset archives and
verifies the selected sleeve projections plus the demo/paper account roots,
inboxes and raw captures before creating fresh account epochs. Prior canonical
account journals remain in the durable archive; they are not rewritten to make
the system look flat. The demo strategy boundary records the independently
proven Bybit flat state. The old deterministic-paper epoch is instead labelled
archived/not carried forward; demo venue truth is never borrowed as paper
evidence. The reset preserves the continuous high-water state and writes fresh
sleeve cycle heartbeats for liveness. It does not cancel orders or close
positions: execution refuses until Bybit demo is already flat with no open
orders.

## Granular data

Audit first; this performs no network writes:

```bash
scripts/ops.sh data-audit --venue both \
  --start 2023-04-01 --end 2026-07-10 \
  --output research/granular_adverse_risk/readiness.json
```

Backfill only a declared surface with explicit authority and a new receipt:

```bash
scripts/ops.sh data-build --execute \
  --venue both \
  --datasets funding,open_interest,premium_index_1h \
  --start 2026-07-01 --end 2026-07-10 \
  --all-pit-symbols \
  --output research/granular_adverse_risk/download-2026-07-10.json
```

Receipts are immutable and must live outside the data roots. Equal, nested,
symlink-aliased, or shared-child Bybit/Binance roots are refused. A readiness
receipt is data evidence only, never an alpha or promotion result.

## Tail-survival research

Run the plan on the larger research machine first:

```bash
scripts/ops.sh tail-plan
scripts/ops.sh tail-run
```

The dispatcher itself remains authoritative. It requires the fixed windows,
both Bybit and Binance, stable residual momentum, complete PIT/funding inputs,
registered cells, matching configuration hashes, byte-level root identity, a
matching full-PIT verification receipt, and a clean relevant worktree. Missing
or stale root receipts force diagnostic-only output; diagnostic overrides
cannot become a positive registered verdict.

## Strategy-overhaul Phase 0

Use the shallow route for a quick partition and source preflight:

```bash
scripts/ops.sh overhaul-plan
```

Use the inventory route for the outcome-blind S00 diagnostic bundle:

```bash
scripts/ops.sh overhaul-phase0
```

The inventory decodes Parquet schemas/footers and selected identity/provenance
columns. It does not calculate or inspect features, ranks, gates, entries,
returns, MFE/MAE, or PnL. Outcome-blind does not mean that every opaque byte used
for source or file identity is outcome-free: a byte snapshot can hash a file
without decoding its numeric contents. Exit 2 is not necessarily lost work;
inspect the printed content-addressed bundle, whose `PARTIAL` or `NOT_READY`
state identifies the exact venue, map, schema, or prospective S01 blocker.

The Phase-0 bundle re-executes its internal derivations under the current
checkout and selected environment manifest; it does not authenticate Git
objects, import hooks/`sys.path`, persisted source labels, unsigned root
receipts, or external-map review claims, and it does not prove full registered
semantics, scope, earliest-history coverage, or transitive provenance. A
`BYTE_SNAPSHOT_ONLY` root snapshot is a diagnostic precursor, and the generic
stage-receipt utility binds bytes and declared metadata only. Neither is a
canonical semantic receipt or a `READY` result. Neither route authorizes S01, a
population stage, or an outcome run.

## Deploy

Deploy is intentionally awkward enough to avoid an accidental keystroke:

```bash
# Refused:
scripts/ops.sh deploy

# Explicit checked deploy (still refused without a valid cutover receipt):
scripts/ops.sh deploy --execute
```

Use environment variables rather than positional deploy arguments when
selecting a target or pinning a commit:

```bash
SSH_TARGET=root@host \
REPO_DIR=/opt/liquidity-migration \
EXPECTED_COMMIT="$(git rev-parse HEAD)" \
scripts/ops.sh deploy --execute
```

Full deploy requires `EXPECTED_COMMIT`; it may be a unique 7-40 character
hexadecimal prefix of the full commit recorded in the authorization. The
already-staged checkout must itself be clean and at that full commit before the
deploy is allowed to fetch or check out anything. For a
private GitHub HTTPS remote, local deploys automatically reuse the credential
from `gh auth` when `GITHUB_TOKEN` is unset. An explicit `GITHUB_TOKEN` still
takes precedence. The credential is passed to the VPS over SSH stdin for the
fetch only and is neither logged nor persisted.

## Overrides

```bash
SSH_TARGET=root@host scripts/ops.sh status
REPO_DIR=/opt/liquidity-migration scripts/ops.sh reset --sleeves continuous
PYTHON=/path/to/python scripts/ops.sh test -q tests/test_runtime_scripts.py
```

- `SSH_TARGET` defaults to `root@116.202.15.128`.
- `REPO_DIR` defaults to `/opt/liquidity-migration`.
- `PYTHON` applies to data, tail, overhaul, and test commands. When unset, the
  wrapper prefers the repository virtual environment, then `python3`.
