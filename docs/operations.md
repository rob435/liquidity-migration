# Operations

Use `scripts/ops.sh` as the single human-facing entry point for routine
demo/paper operations. It is a thin router: the existing canonical scripts
still own strategy, research-integrity, reset, and deploy logic.

Running it with no arguments, `help`, `-h`, or `--help` only prints help.

Long-running agent handoffs are documented separately:

- `docs/account_execution_completion_handoff.md` contains the bounded
  completion prompt and progressive validation cadence for the current cutover;
- `docs/repository_cleanup_handoff.md` contains the later evidence-driven
  deletion campaign.

Finish or explicitly close the cutover before implementing the cleanup. Do not
run both prompts concurrently in the same worktree. Neither prompt grants
mainnet authority, and the completion prompt deliberately stops before the
separately authorized `main` push/deployment boundary.

V7 is closed and spent. The active bounded calibration is the prospectively
registered V8 corrected-defect epoch. The CLI/schema names `v7-archive`,
`v7_training`, and `--v7-archive-map` remain compatibility labels for the one
passing training calibration and must bind V8; they cannot consume the failed
V7 receipt.

```bash
scripts/ops.sh
scripts/ops.sh status
scripts/ops.sh equity --sleeves long,continuous
scripts/ops.sh data-audit --venue both
scripts/ops.sh overhaul-plan
scripts/ops.sh overhaul-phase0
scripts/ops.sh natural-freeze --help
scripts/ops.sh natural-run-config --help
scripts/ops.sh natural-effective-config --help
scripts/ops.sh account-replay --help
scripts/ops.sh account-parity --help
scripts/ops.sh account-parity-scope --help
scripts/ops.sh natural-sufficiency --help
scripts/ops.sh clock-offset --execute --output /absolute/vps/path/clock-offset.json
scripts/ops.sh clock-series --help
scripts/ops.sh demo-calibration --execute --help
scripts/ops.sh natural-safety-flatten --execute --help
scripts/ops.sh twin-drift --help
scripts/ops.sh v7-archive --help
scripts/ops.sh stopped-epoch --help
scripts/ops.sh fresh-deploy-epoch --help
scripts/ops.sh fresh-deploy-env --help
scripts/ops.sh authorized-deploy-epoch --help
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
  files before it removes anything. Decision-grade epoch creation additionally
  requires `--leave-stopped --receipt /absolute/new/path.json`.
- `clock-offset` and `demo-calibration` run on the VPS and require `--execute`.
  The first writes a public-time receipt; the second publishes demo targets but
  receives no API credentials and cannot bypass the account owner. The V8
  launcher has no resume mode and refuses an existing event tape or run
  receipt; any emitted target spends that registered attempt.
- `clock-series` only re-opens local evidence. It never contacts the venue,
  starts a timer, deploys, or grants execution authority.
- `natural-freeze linux-ci` accepts only the exact candidate's manual
  `candidate-ci` workflow dispatch. That path must record every SSH/VPS,
  install, verify, recovery, and deploy step as skipped.
- The stopped-natural-epoch seal and fresh-deploy epoch are integrity/root
  boundaries, not authority. Use only their explicit `create`/`verify` routes;
  do not replace either machine gate with an operator note or tarball.
- `fresh-deploy-env` only materializes or verifies exact per-unit environment
  files from an already-created fresh-deploy manifest. It does not create that
  manifest, start systemd, or grant deployment authority.
- `authorized-deploy-epoch` first re-opens cutover authority and the bound
  stopped/fresh manifests. Before that activation exists,
  `prepare-evidence-runtime` explicitly binds the evidence window to one clean
  commit and machine; a missing marker fails closed. `prepare` is the stronger
  prestart empty/stopped check and consumes that marker. `verify`,
  `verify-processes`, and each same-process workload wrapper are read-only
  poststart checks.
- `natural-safety-flatten` also runs on the VPS and requires `--execute`. It is
  credential-free and can only publish RISK-authored zero targets for exact
  canonical LONG/CONTINUOUS component desires after the registered T1. Its
  mode-`0600` capture and create-only manifest prove target provenance only, not acceptance, fills,
  convergence, or final flatness.
- `deploy` refuses unless the first argument after the command is exactly
  `--execute`. The checked deploy script retains its own commit, test, config,
  credential, and service gates.
- Research commands produce research artifacts only. No command here promotes a
  strategy, changes a decision rule, or authorizes real money.
- `data-build` refuses unless its first argument is `--execute`; the underlying
  tool also requires explicit datasets, window, symbol authority, and a new
  immutable receipt outside both data roots.

Unless a command explicitly documents mutable runtime state, every cutover
receipt, manifest, archive map, replay output, assessment, and evidence wrapper
must use a new absent path under a new mode-`0700` attempt namespace. Writers
fail on existing outputs. Preserve failed or partial attempts; never delete,
replace, or rename them into a passing attempt. The fixed deploy-ready
authorization path is the sole final well-known output, and its prior existence
is a blocking activation-state question for the owner, not permission to
overwrite it.

## Commands

| Command | Canonical route | Operational effect |
|---|---|---|
| `status` | `scripts/verify_vps_live.sh` | Read-only VPS checkout, config, credential, service, and liveness checks. |
| `natural-freeze` | `python -m liquidity_migration.natural_cutover_freeze_manifest` | Produces/reopens exact local-suite, non-contacting exact-candidate Linux-CI, and pre-window natural freeze receipts. |
| `natural-run-config` | `python -m liquidity_migration.natural_run_config` | Derives the canonical half-open 120-hour LONG/CONT demo tape paths from the freeze; grants no authority. |
| `natural-effective-config` | `python -m liquidity_migration.natural_effective_config` | Reopens producer-written resolved-config receipts and builds/verifies their stopped post-window bundle. |
| `target-replay` | `python -m liquidity_migration.strategy_target_replay` | Replays one frozen natural target capture into isolated historical/paper/demo scheduling tapes and publishes a source-reopening schema-v2 replay manifest; never opens an account route. |
| `event-parity` | `python -m liquidity_migration.strategy_event_parity` | Schema-v3 exact scheduling/outcome comparison after declared environment/source normalization. The CLI requires the schema-v2 replay manifest, and deployment-valid provenance requires its canonical source capture to be demo. |
| `account-replay` | `python -m liquidity_migration.captured_account_replay` | Builds the mandatory natural input manifest and writes a source-reopening, deterministically rerunnable schema-v3 replay of exact post-reset demo requests/books/risk through isolated historical/paper account roots. Requires the freeze, effective-config bundle, and separate post-T1 safety capture/manifest. |
| `account-parity` | `python -m liquidity_migration.kernel_parity` | Schema-v4, source-reopening comparison of an explicit non-empty strategy-to-order-plan batch scope across historical/paper/actual demo journals. Demo fills are classified, not equated. |
| `account-parity-scope` | `scripts/build_kernel_parity_scope.py` | Builds the schema-v3 exact non-empty natural comparison scope and re-opens both captured-account-replay and event-parity receipts, including their replay provenance. |
| `natural-sufficiency` | `python -m liquidity_migration.natural_tape_sufficiency` | Writes a schema-v3 receipt for the registered 120-hour hourly/lifecycle floors from source tapes, replay, and authenticated accounting. |
| `clock-offset --execute` | VPS `scripts/capture_bybit_clock_offset.py` | Writes a self-hashed, NTP-gated VPS-vs-Bybit public clock receipt. |
| `clock-series` | `python -m liquidity_migration.clock_offset_series` | Builds or verifies the freeze-bound, ordered periodic clock series used only for natural feed-latency correction. |
| `demo-calibration --execute` | VPS `scripts/run_demo_execution_calibration.py` | Emits the preregistered tiny target-only demo sequence through the account inbox; never direct venue execution. |
| `natural-safety-flatten --execute` | VPS `scripts/publish_natural_safety_flatten.py` | After T1, publishes one captured RISK zero target per still-active natural component and creates a source-bound mode-`0600` capture and manifest; refuses stale/blocked owner health, unresolved inbox work, working orders, malformed desires, or any publication error. |
| `twin-calibrate` | `scripts/calibrate_execution_twin.py` | Self-hashed market-order twin calibration from verified demo account/L2 tapes; exits nonzero until registered sample gates pass. |
| `twin-drift` | `python -m liquidity_migration.execution_twin_drift` | Freezes registered training configs and source-recomputes archived-training versus natural-holdout drift using the periodic clock series. |
| `v7-archive` | `python -m liquidity_migration.v7_archive_materialization` | Compatibility route that materializes stopped registered-training sources before reset, or recovers them from a verified reset archive, and writes the archive-source map. |
| `stopped-epoch` | `python -m liquidity_migration.stopped_natural_epoch` | Creates/verifies the five-input, 11-root stopped natural-source seal; creation checks the exact 12-unit fleet before and after hashing. |
| `fresh-deploy-epoch` | `python -m liquidity_migration.fresh_deploy_epoch` | Creates/verifies ten empty deployment roots, deriving candidate, freeze, and exact old-root identities from the stopped seal. |
| `fresh-deploy-env` | `python -m liquidity_migration.fresh_deploy_environment` | Materializes/verifies nine exact late systemd environment files from a fresh-deploy manifest; it does not create roots or start services. |
| `authorized-deploy-epoch` | `python -m liquidity_migration.authorized_deploy_epoch` | Authority-aware prestart preparation, poststart root/environment verification, and active-process environment verification. |
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

`natural-freeze create` requires exactly `demo` and `paper` route and risk
sources. It parses both route EnvironmentFiles and cross-checks their account,
inbox, capture, candidate, rule, risk, calibration, freshness, and paper `p50`
values against the named sources; the risk policies must be semantically equal
and the queue-head market warmup timeout cannot exceed 30 seconds. `REAL_MONEY`
must be unset or explicitly false. Its distinct seed input is the mode-`0600`
BTCUSDT/ETHUSDT/BUSDT V8 symbol file, not the candidate-universe artifact.

## Exact-candidate Linux CI

The tracked pre-push hook runs its full pytest gate with a basetemp outside the
repository and refuses an override that resolves below the checkout. This is a
source-snapshot integrity requirement: `.git/tmp` is still repository-local and
must not be used for a complete candidate run.

Dispatch the checked candidate branch with workflow mode `candidate-ci`; do not
use a pull-request merge SHA:

```bash
gh workflow run vps-deploy.yml --ref "$CANDIDATE_BRANCH" -f mode=candidate-ci

scripts/ops.sh natural-freeze linux-ci \
  --repository-root /absolute/path/to/liquidity-migration \
  --candidate-commit "$EXPECTED_COMMIT" \
  --run-id "$RUN_ID" \
  --provenance /absolute/new/path/github-actions-provenance.json \
  --output /absolute/new/path/exact-candidate-linux-ci.json
```

The loader requires a completed successful `workflow_dispatch` at that exact
40-character head, full Ruff and pytest success, the CI-only confirmation step,
and skipped results for every SSH key/host-key, VPS verify, install-preflight,
recovery, and deploy step. Any VPS contact or mutating step makes the receipt
ineligible even when CI itself passed.

Every argument after the command is forwarded as its own argument. Do not put a
Python command plus flags into `PYTHON`; it must name one executable or
executable path.

The router has an explicit locality boundary. `status`, `reset`,
`clock-offset`, `demo-calibration`, `natural-safety-flatten`, and `deploy` cross
SSH from the control checkout. The other routes execute in the checkout where
`scripts/ops.sh` is invoked. Therefore any local route whose arguments name
live `/opt/liquidity-migration` or `/var/lib/liquidity-migration` sources must
be run from the staged VPS checkout (or only after those sources have been
materialized into a verified immutable local archive and every path argument is
changed accordingly). A local macOS path that happens to share a spelling with
a VPS path is not evidence. Record the execution host for every gate.

## VPS status and account-journal parity

The blocks below are command references, not permission to follow their page
order as an operational sequence. The registered order is:

1. archive/reset for V8, run V8, and materialize its immutable archive before
   the second natural-holdout reset of all registered account, capture, event,
   outcome, target, telemetry, and natural-runtime outputs;
2. start the paper owner alone and stop it cleanly, then start the demo owner
   alone before the registered LONG/CONT producers and fixed 120-hour capture;
3. after T1, converge flat, stop every managed unit, and build the stopped
   effective-config bundle and clock series;
4. capture venue accounting/funding and final flatness, then create the
   stopped-natural-epoch seal from the five pre-seal boundary inputs;
5. write replay, parity, sufficiency, and drift artifacts under one dedicated
   derived-evidence root outside all 11 sealed roots; and
6. only after every analysis gate passes, create the ten-root fresh-deploy
   epoch outside both earlier namespaces and assess cutover authority.

Build the natural run config before T0. No branch promotion or deployment is a
step in this sequence.

Candidate-universe schema v2 retains and hashes the complete raw instrument and
ticker snapshots. A noncanonical ticker label absent from the complete
instrument snapshot is recorded under `rejected_ticker_rows` and excluded from
candidate evaluation; it is never normalized into a strategy symbol. Missing
labels, duplicates, noncanonical instrument rows, and any row that could map to
a validated instrument remain fail-closed. The loader recomputes this partition
and the full decision table from the raw snapshot.

```bash
# Read-only production verification.
scripts/ops.sh status

# After passing and archiving V8, freeze the natural candidate population and
# probe exactly that set. These three outputs must be new and distinct from the
# three-symbol V8 rules receipt.
.venv/bin/python scripts/freeze_account_candidate_universe.py \
  --output /absolute/new/path/candidate-universe-natural.json

.venv/bin/python scripts/probe_bybit_demo_rules.py \
  --symbols-file /absolute/new/path/candidate-universe-natural.json \
  --max-probe-notional-usdt 200 --probe-distance-bps 100 \
  --max-private-requests-per-second 5 --leverage 10 \
  --output /absolute/new/path/demo-rules-natural.json \
  --confirm-demo-probe

.venv/bin/python scripts/verify_candidate_rule_coverage.py \
  --candidate-universe /absolute/new/path/candidate-universe-natural.json \
  --demo-rules /absolute/new/path/demo-rules-natural.json \
  --max-rule-age-hours 168 \
  --output /absolute/new/path/candidate-rule-coverage-natural.json

# The probe and every receipt consumer enforce exactly 100 bps distance,
# <=200 USDT notional, <=5 authenticated requests/second, and <=10x leverage.
# Invalid parameters fail before credentials or venue access; persisted attempts
# must reproduce the receipt cap, venue quantity steps/bounds, and rule minimum.

# Canonical pre-window paths derived from the immutable freeze.
scripts/ops.sh natural-run-config build \
  --freeze-manifest /absolute/path/natural-cutover-freeze.json \
  --output /opt/liquidity-migration/data/bybit-natural-account-cutover/natural-run-config.json

# Atomically activate natural evidence mode for both demo producers. This
# source-reopens the mode-0600 run config and writes exactly two non-secret
# assignments to the owner-only systemd EnvironmentFile; it does not start,
# restart, deploy, or contact the venue.
scripts/ops.sh natural-run-config materialize-env \
  --config /opt/liquidity-migration/data/bybit-natural-account-cutover/natural-run-config.json

# Reopen the config and prove the installed file's exact bytes, owner, mode,
# final-component non-symlink identity, and stable descriptor read.
scripts/ops.sh natural-run-config verify-env \
  --config /opt/liquidity-migration/data/bybit-natural-account-cutover/natural-run-config.json

# Run only after both natural producers have stopped. The per-sleeve receipts
# were written before those producers constructed public-market resources.
scripts/ops.sh natural-effective-config bundle \
  --receipt long=/opt/liquidity-migration/data/bybit-long-demo-event/natural-effective-runtime-config.json \
  --receipt continuous=/opt/liquidity-migration/data/bybit-continuous-demo-event/natural-effective-runtime-config.json \
  --output /opt/liquidity-migration/data/bybit-natural-account-cutover/effective-runtime-config-bundle.json

# Replay one frozen natural target/scheduling capture into a brand-new isolated
# root. This creates historical/paper/demo event, decision, and scheduled-target
# tapes; it never opens an account route or venue adapter.
scripts/ops.sh target-replay \
  --capture /absolute/path/frozen-target-scheduling-capture.jsonl \
  --output-root /absolute/new/path/target-scheduling-replay

# Freeze exact post-reset natural account inputs. The safety capture/manifest
# is a separate, registered post-T1 zero-target path; it is not replayed or
# counted toward natural parity/lifecycle floors.
scripts/ops.sh account-replay \
  --target-capture /absolute/path/frozen-target-scheduling-capture.jsonl \
  --demo-account-root /path/to/stopped-demo-account-root \
  --market-capture-root /path/to/frozen-demo-market-capture \
  --demo-rules-file /path/to/demo-rules.json \
  --risk-policy-file /path/to/risk-policy.json \
  --calibration-file /path/to/frozen-v8-calibration.json \
  --freeze-manifest /path/to/natural-cutover-freeze.json \
  --effective-runtime-config-bundle /path/to/effective-runtime-config-bundle.json \
  --safety-target-capture /path/to/post-window-safety-target-capture.jsonl \
  --safety-manifest /path/to/post-window-safety-manifest.json \
  --expected-account-id bybit-demo-unified \
  --t0-ns "$T0_NS" --t1-ns "$T1_NS" \
  --max-decision-age-ms 250 \
  --max-market-age-ms 5000 \
  --max-snapshot-age-ms 5000 \
  --latency-quantile p50 --slippage-quantile p50 \
  --build-input-manifest /absolute/path/natural-account-replay-input.json

scripts/ops.sh account-replay \
  --target-capture /absolute/path/frozen-target-scheduling-capture.jsonl \
  --demo-account-root /path/to/stopped-demo-account-root \
  --market-capture-root /path/to/frozen-demo-market-capture \
  --demo-rules-file /path/to/demo-rules.json \
  --risk-policy-file /path/to/risk-policy.json \
  --calibration-file /path/to/frozen-v8-calibration.json \
  --freeze-manifest /path/to/natural-cutover-freeze.json \
  --effective-runtime-config-bundle /path/to/effective-runtime-config-bundle.json \
  --safety-target-capture /path/to/post-window-safety-target-capture.jsonl \
  --safety-manifest /path/to/post-window-safety-manifest.json \
  --input-manifest /absolute/path/natural-account-replay-input.json \
  --expected-account-id bybit-demo-unified \
  --max-decision-age-ms 250 \
  --max-market-age-ms 5000 \
  --max-snapshot-age-ms 5000 \
  --latency-quantile p50 --slippage-quantile p50 \
  --output-root /absolute/new/path/natural-account-replay

# Exact offline strategy-event comparison. Repeat --source-map for every raw
# source present in each tape.
scripts/ops.sh event-parity \
  --environment historical=/absolute/new/path/target-scheduling-replay/historical/strategy_event_tape.jsonl \
  --environment paper=/absolute/new/path/target-scheduling-replay/paper/strategy_event_tape.jsonl \
  --environment demo=/absolute/new/path/target-scheduling-replay/demo/strategy_event_tape.jsonl \
  --decision-tape historical=/absolute/new/path/target-scheduling-replay/historical/strategy_event_decision_tape.jsonl \
  --decision-tape paper=/absolute/new/path/target-scheduling-replay/paper/strategy_event_decision_tape.jsonl \
  --decision-tape demo=/absolute/new/path/target-scheduling-replay/demo/strategy_event_decision_tape.jsonl \
  --replay-input historical=/absolute/new/path/target-scheduling-replay/historical/replay_input.jsonl \
  --replay-input paper=/absolute/new/path/target-scheduling-replay/paper/replay_input.jsonl \
  --replay-input demo=/absolute/new/path/target-scheduling-replay/demo/replay_input.jsonl \
  --source-map historical=long:historical=long:replay \
  --source-map paper=long:paper=long:replay \
  --source-map demo=long:demo=long:replay \
  --source-map historical=continuous:historical=continuous:replay \
  --source-map paper=continuous:paper=continuous:replay \
  --source-map demo=continuous:demo=continuous:replay \
  --replay-manifest /absolute/new/path/target-scheduling-replay/replay_manifest.json \
  --output /absolute/path/strategy-event-parity.json

# Deployment-grade normalized account-plan comparison. It reopens every source
# and evidence file; the scope file must name a non-empty ordered natural batch
# set and be bound to the scheduling receipt.
scripts/ops.sh account-parity-scope \
  --captured-account-replay-receipt /absolute/new/path/natural-account-replay/captured_account_replay_receipt.json \
  --event-parity-receipt /path/to/strategy-event-parity.json \
  --output /path/to/natural-batch-scope.json

scripts/ops.sh account-parity \
  --environment historical=/path/to/historical-account-root \
  --environment paper=/path/to/paper-account-root \
  --environment demo=/path/to/demo-account-root \
  --comparison-scope-file /path/to/natural-batch-scope.json \
  --event-parity-receipt /path/to/strategy-event-parity.json \
  --fresh-epoch-reset-receipt /path/to/natural-holdout-reset.json \
  --risk-policy-file /path/to/risk-policy.json \
  --rules-file /path/to/demo-rules.json \
  --effective-runtime-config-bundle /path/to/effective-runtime-config-bundle.json \
  --twin-calibration-receipt /path/to/frozen-v8-calibration.json \
  --repo-root /absolute/path/to/liquidity-migration \
  --expected-commit "$EXPECTED_COMMIT" \
  --quantity-tolerance 1e-12 \
  --output /path/to/account-kernel-parity.json

# Source-recomputed archived-V8 versus natural-holdout drift. The flag retains
# its compatibility name, but the bound receipt and archive must be V8.
scripts/ops.sh twin-drift verify \
  --calibration-file /path/to/frozen-v8-calibration.json \
  --v7-archive-map /path/to/v8-archive-source-map.json \
  --natural-account-root /path/to/stopped-demo-account-root \
  --natural-market-capture-root /path/to/frozen-demo-market-capture \
  --freeze-manifest /path/to/natural-cutover-freeze.json \
  --natural-target-capture /path/to/frozen-target-scheduling-capture.jsonl \
  --safety-target-capture /path/to/post-window-safety-target-capture.jsonl \
  --safety-manifest /path/to/post-window-safety-manifest.json \
  --demo-rules-file /path/to/demo-rules.json \
  --clock-offset-series /path/to/clock-offset-series.json \
  --baseline-config /path/to/v8-baseline-p50.json \
  --stress-config /path/to/v8-stress-p95.json \
  --account-id bybit-demo-unified \
  --t0-ns "$T0_NS" --t1-ns "$T1_NS" \
  --output /path/to/execution-twin-drift.json

# Registered 120-hour event/lifecycle floor. This consumes authenticated
# accounting; the accounting command therefore precedes replay and this gate.
scripts/ops.sh natural-sufficiency \
  --long-event-tape /path/to/long/strategy_event_tape.jsonl \
  --long-outcome-tape /path/to/long/strategy_event_decision_tape.jsonl \
  --continuous-event-tape /path/to/continuous/strategy_event_tape.jsonl \
  --continuous-outcome-tape /path/to/continuous/strategy_event_decision_tape.jsonl \
  --target-capture /path/to/frozen-target-scheduling-capture.jsonl \
  --demo-account-root /path/to/stopped-demo-account-root \
  --safety-target-capture /path/to/post-window-safety-target-capture.jsonl \
  --safety-manifest /path/to/post-window-safety-manifest.json \
  --account-replay-receipt /absolute/new/path/natural-account-replay/captured_account_replay_receipt.json \
  --venue-accounting-receipt /path/to/venue-accounting.json \
  --freeze-manifest /path/to/natural-cutover-freeze.json \
  --effective-runtime-config-bundle /path/to/effective-runtime-config-bundle.json \
  --expected-account-id bybit-demo-unified \
  --t0-ns "$T0_NS" --t1-ns "$T1_NS" \
  --output /path/to/natural-tape-sufficiency.json
```

`event-parity` schema v3 is offline and fail-closed. Each non-empty tape must pass its
native event-id/hash chain, duplicate, and ordering checks. Every event must
carry `replay_input_sha256` matching the supplied immutable input bytes; the
three input files must be byte-identical (using the same immutable path three
times is allowed). Each post-callback companion decision tape must also pass
its hash chain, contain exactly one outcome per raw event in event order, and
record a sorted `decision_keys` list, including an explicit empty list for a
no-decision callback. Source labels are normalized only through the exact maps
on the command line, and only `execution_environment` payload values are
replaced. Event time, phases/kinds, source sequences, all other payload fields,
and decisions remain exact. `ingest_ts_ns` is arrival telemetry: its raw bytes
remain bound in each source tape, but it is not part of normalized scheduling
parity. The required schema-v2 target-replay manifest source-reopens the
canonical capture and all 12 canonical replay outputs, rederives the event,
decision, and scheduled-target semantics, and rejects aliases or changed bytes.
Event parity binds that manifest and its canonical source capture; only a
manifest whose source capture is `demo` is deployment-valid. The self-hashed
receipt is reproduced from the bound files whenever cutover authorization loads
it.

The natural producer and parity artifacts have different causal roles. A LONG
or CONT daemon first writes its input event, runs the production callback, then
re-reads the actual `PublishedTargetRequest` files under the canonical inbox
lock. Only a callback with no publication errors and verifiable durable files
can append the natural hash-chained target capture and companion outcome; a
successful no-target callback appends an explicit empty cycle. Set
`STRATEGY_TARGET_CAPTURE_PATH` to the same new evidence path on LONG and CONT
to create one interprocess-locked artifact for both sleeves. A callback,
publication, or durability failure leaves the outcome missing.

Freeze those natural capture bytes before invoking `target-replay`. The offline
adapter consumes that exact artifact, rejects mutation/duplicates/corruption,
and creates a new isolated root containing three scheduling replays plus exact
input copies. Its schema-v2 `replay_manifest.json` is written only after the
replay artifacts exist and the source has been rechecked, so its declared
completion time is post-replay in the internal construction order. That field
is not an authenticated wall clock. The directory named `demo` is an offline
demo-labelled scheduling replay. It does not contact Bybit, write the demo
account inbox/journal, or fabricate demo orders/fills. Actual demo
target/order/fill/P&L evidence remains the separate account-execution and
venue-accounting gates.

`account-replay` schema v3 is also offline, but consumes the actual stopped
natural demo journal and raw decision books. Its loader reopens every bound
source and output, reruns the replay in a private temporary root, and compares
the complete historical and paper output trees. V8 is a distinct pre-reset
training epoch: the
child `natural_account_replay_input_manifest_v2` binds V8's receipt/config, the
source-reopened effective-runtime-config bundle, and
the exact post-reset holdout sources, and rejects reused journal chains or raw
segments. Every live-source argument must belong to the stopped-natural-epoch
seal; the later authorization aggregate check binds the replay back to that
same sealed source set. Its registered replay configuration is exact: 250-ms
maximum decision age, 5,000-ms market/snapshot age, `p50` latency/slippage, and
the canonical kernel/twin seeds. Alternate self-bound values are rejected. The
mandatory separate post-T1 safety capture may contain only exact zero targets
under `natural-safety-flatten/<freeze-id>/...` with the registered metadata. Its
exact batch set is classified and excluded from natural replay,
plan parity, and lifecycle floors; it remains part of final venue accounting
and flatness. Live paper producers stay stopped—the directory named `paper` is
an isolated deterministic replay root, not a retimed service run.

`event-parity` therefore proves normalized captured-target scheduling and
decision-tape equality for those frozen bytes—not market-data authenticity,
signal selection, strategy/config identity omitted from payloads,
account-kernel parity, venue fills/P&L/funding, alpha, deployment readiness, or
authority. Pre-cutover natural tapes and any event lacking its post-callback
capture/outcome remain ineligible; do not repair them with an operator wrapper
or by adding a replay hash after the fact. This registered deterministic scope
is LONG/CONT only; it does not include hedge, RMOM/liveness timers, every
historical signal-selection loop, or a live paper-producer run.

The old `reconcile quick` and `reconcile full` routes were removed on
2026-07-13. They compared sleeve-local idealized-fill projections that the
target-only paper/demo architecture no longer treats as authoritative. Keeping
them would make a green report easier to produce without validating the account
owner.

`account-parity` refuses an empty or implicit scope. Its schema-v3 comparison
scope reopens and binds the captured-account-replay and event-parity receipts,
their modeled output identities, and the canonical target-replay provenance.
The schema-v4 kernel receipt then reopens the three journals and every bound
config/evidence file, validates the
second reset and V8/natural epoch separation, and compares decision/target
keys, discrete target fields, risk acceptance/rejection and target presence,
semantic commands, and finite quantities at the fixed absolute tolerance
`1e-12`. Raw command IDs are mapped one-to-one by semantic command key. Actual
demo ACK/fill/status/close/P&L families are classified but deliberately not
required to equal the deterministic twin; historical-versus-paper modeled
execution is reported separately. This still does not establish raw-market
authenticity, lifecycle sufficiency, venue accounting, alpha, deployment
readiness, or authority.

The account-replay receipt exposes source-bound hourly, batch/sleeve,
command/fill, conservative round-trip, and P&L mappings, but always leaves its
embedded `sufficiency_gate_passed=false`. A separate machine receipt must bind
the companion outcome tape and authenticated venue rows for the registered
120-hour/30-command/10-per-sleeve/3-symbol/3-round-trip/10-P&L floors. The
venue-accounting registered minimums of 2 trade rows, 1 closed-P&L row, and 1
settlement row are lower integrity checks and cannot satisfy that natural
contract.

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

The clock and calibration outputs are create-only. Stop the owner after V8's
verified-flat final boundary before calibration so the source-reopening
constructor observes one immutable journal/capture generation. A changed source
or non-passing receipt closes the attempt; it is not retried as a test.

Paper startup consumes only a self-hashed receipt whose registered sample gate
passed. The receipt does not authorize deployment.

For the 120-hour natural holdout, that single startup point is insufficient.
Use an external operator-controlled timer at a six-hour target cadence to run
the same public-only capture into a private directory:

```bash
scripts/ops.sh clock-offset --execute \
  --output-directory /var/lib/liquidity-migration/cutover-evidence/clock-series
```

The directory must be verifier-owned mode `0700`; each timestamp-named receipt
is create-only mode `0600`. The capture refuses all Bybit private credential
environment variables. This repository intentionally does not install, enable,
or deploy a timer. A valid series starts with the receipt named by the natural
freeze, targets six-hour sampling, permits no observed gap above eight hours,
and includes a sample at or after T1 (within six hours):

```bash
scripts/ops.sh clock-series build \
  --freeze-manifest /path/to/natural-freeze.json \
  --clock-offset-receipt /path/to/clock-offset-initial.json \
  --clock-offset-receipt /path/to/clock-offset-periodic-01.json \
  --clock-offset-receipt /path/to/clock-offset-periodic-N.json \
  --output /path/to/clock-offset-series.json
```

Pass that artifact to natural drift as `--clock-offset-series`. Feed rows use
timestamp-specific interpolation and report a non-hard uncertainty sensitivity;
request/ack RTT and exchange-timestamp fill spacing receive no clock correction.

The optional bounded sample generator is separately preregistered in
`docs/preregistration/account_execution_calibration_v8_2026_07_15.md`. V4--V7
are retained failed pilots and must not be resumed or merged. V8 may supply
execution-twin observations efficiently but does not replace actual
LONG/CONTINUOUS strategy-tape comparison.

After V8 passes and the demo account is flat, stop every managed unit and
materialize V8 through the compatibility `v7-archive` route before reusing any
live path:

```bash
scripts/ops.sh v7-archive from-stopped-roots \
  --repository-root /opt/liquidity-migration \
  --expected-candidate-commit "$EXPECTED_COMMIT" \
  --calibration-file /path/to/frozen-v8-calibration.json \
  --destination-root /absolute/new/path/v8-immutable-sources \
  --archive-map-output /absolute/new/path/v8-archive-source-map.json

scripts/ops.sh twin-drift freeze-configs \
  --calibration-file /path/to/frozen-v8-calibration.json \
  --max-decision-age-ms 250 \
  --baseline-output /absolute/new/path/v8-baseline-p50.json \
  --stress-output /absolute/new/path/v8-stress-p95.json
```

`from-reset-archive` is a recovery path when the primary stopped-root snapshot
could not be completed. It is not permission to reset first by default.

After the demo target set and venue are flat, stop producers, let the owner
complete its final strict funding/position pass, write fresh health, and stop
the owner. Then capture the exact fresh-ledger epoch (at most seven days).
This command must pass before the stopped-source seal and every offline replay:

```bash
scripts/ops.sh venue-accounting \
  --account-root /path/to/demo-account-root \
  --account-id bybit-demo-unified \
  --start-time-ms FRESH_EPOCH_START_MS \
  --output /path/to/venue-accounting.json
```

The accounting receipt is create-only and belongs outside all stopped source
roots. A failed reconciliation is preserved at that path and blocks the seal;
do not rerun into the same or a replacement path to manufacture agreement.

The registered minimum floors—fixed before the venue result is viewed—are two
trade rows, one closed-PnL row, and one funding settlement. The registered
maximum absolute tolerances are `1e-12` quantity, `1e-8` price, and `1e-8`
amount; the registered maximum relative tolerance is `1e-9`. Both the command
and receipt verifier reject lower floors or wider tolerances, while stricter
values remain valid. The command derives the authenticated Bybit `userID` and
acquires the same host-global demo-account lease as the owner and maintenance
probe. It accepts no alternate lock path, submits no orders, and binds raw
Bybit rows and pre/post position/open-order snapshots, replays the journal, and
exits nonzero on identity, fee, P&L, funding, lineage, or flatness disagreement.
It does not allocate account-netted P&L to components or authorize deployment.

## Stopped source and fresh deployment epochs

After venue accounting passes, all 12 managed services/timers must remain
stopped while `stopped_natural_epoch_v1` seals exactly 11 old mutable roots:
the six natural demo/paper account/inbox/capture roots, four LONG/CONT
demo/paper data roots, and the natural evidence root. It also binds the
five pre-seal evidence files—freeze manifest, stopped effective-config bundle,
clock series, post-window safety manifest, and venue-accounting receipt—and all
files under those roots. The seal binds candidate/freeze/window, effective
configuration, account and strategy tapes, post-window safety, accounting,
source semantics, and the point-in-time stopped state. Its default loader can
re-open the seal after deployment without demanding services remain stopped;
deployment must use the stronger live stopped check before startup. Replay,
event comparison, kernel parity, sufficiency, and drift are deliberately not
seal inputs: create them afterward under a dedicated derived-evidence root
outside every sealed path.

Create the seal at a new absolute path outside all 11 roots. The five `--input`
roles and 11 `--root` roles are exact; omission, repetition, aliasing, active
units, changed bytes, or a pre-existing output fails:

```bash
scripts/ops.sh stopped-epoch create \
  --input freeze_manifest=/path/to/natural-cutover-freeze.json \
  --input effective_runtime_config=/path/to/effective-runtime-config-bundle.json \
  --input clock_offset_series=/path/to/clock-offset-series.json \
  --input natural_safety_flatten=/path/to/post-window-safety-manifest.json \
  --input venue_accounting=/path/to/venue-accounting.json \
  --root demo_account=/path/to/stopped-demo-account \
  --root demo_inbox=/path/to/stopped-demo-inbox \
  --root demo_capture=/path/to/stopped-demo-capture \
  --root paper_account=/path/to/stopped-paper-account \
  --root paper_inbox=/path/to/stopped-paper-inbox \
  --root paper_capture=/path/to/stopped-paper-capture \
  --root long_demo=/path/to/stopped-long-demo \
  --root long_paper=/path/to/stopped-long-paper \
  --root continuous_demo=/path/to/stopped-continuous-demo \
  --root continuous_paper=/path/to/stopped-continuous-paper \
  --root natural_evidence=/path/to/stopped-natural-evidence \
  --output /absolute/new/path/stopped-natural-epoch.json

scripts/ops.sh stopped-epoch verify \
  --seal /absolute/new/path/stopped-natural-epoch.json \
  --require-currently-stopped
```

The seal is integrity evidence, not a filesystem lock. Keep the fleet stopped
and do not mutate the namespace while derived evidence is being built. The
schema-v4 authority aggregate now reconstructs the seal's exact path/hash index,
reopens each analysis dependency, rejects unregistered live inputs, and checks
that target replay, captured replay, comparison-scope, and other derived outputs
remain outside the stopped and fresh namespaces. Constructor success is still
insufficient: no real seal, derived analysis, or authority aggregate exists
until those commands run against the frozen candidate and captured epoch.

Analysis `created_ts_ns` fields enforce a declared chronology after the stopped
seal. They are locally supplied receipt fields—not authenticated proof of
wall-clock invocation. The operational contract still requires every analysis
gate to pass before `fresh-deploy-epoch create`. Causal dependency order and
source provenance come from the exact source-reopened path/hash bindings: the
target manifest is created after its replay artifacts, event parity binds that
manifest, the scope binds event parity plus captured replay, and
kernel/sufficiency/authority reopen those dependencies.

Create `fresh_deploy_epoch_v1` only from that seal. It creates ten owner-only,
empty, pairwise-disjoint roots: demo/paper account, inbox and capture roots plus
LONG/CONT demo/paper data roots. The constructor derives and rejects overlap
with the stopped roots; it does not take an operator-declared derived namespace
as input. The schema-v4 authority aggregate separately rejects overlap between
the fresh roots and every registered analysis receipt, target-replay output,
captured-replay root, and comparison-scope path. Both epoch artifacts say
`execution_authorization=not_granted` and are machine evidence for the
`stopped_natural_epoch_and_fresh_deploy_roots` authority gate.

Only after all derived analysis gates pass, choose a new nonexistent parent
outside the sealed and derived-evidence namespaces. Creation source-reopens the
seal, requires the fleet still stopped, derives every identity from it, and
creates the canonical `fresh-deploy-epoch.json` inside that parent:

```bash
scripts/ops.sh fresh-deploy-epoch create \
  --stopped-seal /absolute/new/path/stopped-natural-epoch.json \
  --epoch-parent /absolute/new/path/fresh-deploy-epoch

scripts/ops.sh fresh-deploy-epoch verify \
  --manifest /absolute/new/path/fresh-deploy-epoch/fresh-deploy-epoch.json \
  --require-empty-roots
```

Re-open both artifacts immediately before startup and require the ten roots to
remain empty. Do not call the fresh-env materializer without this verified
manifest.

## Evidence-bound cutover authorization

`account-execution-deploy-ready` is a JSON authorization receipt, not an empty
marker. Start from an intentionally open assessment on the staged, clean VPS
checkout:

```bash
COMMIT="$(git rev-parse HEAD)"
scripts/ops.sh cutover-authority template \
  --authorized-commit "$COMMIT" \
  --authorized-by OWNER_REVIEW_ID \
  --output /absolute/new/path/account-execution-cutover-assessment.json
```

The issuer's schema-v4 aggregate machine-validates the natural freeze,
candidate-rule coverage, complete demo-rule probe, V8 calibration, schema-v3
captured-account replay, schema-v3 event-clock comparison, schema-v3 comparison
scope and schema-v4 kernel parity, schema-v3 natural sufficiency, twin drift,
venue accounting/final flatness, stopped-natural-epoch seal, and fresh-deploy
epoch.
The accounting and flatness roles may point to the same self-hashed venue
receipt. Owner-first evidence is freeze/review bound, while stopped state and
final journal/account identity come from the seal and accounting receipt. For
topology, paper/demo owner-first order, and the final evidence card, snapshot
exact source files into self-hashed reviewed-evidence wrappers;
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
  --assessment /absolute/new/path/account-execution-cutover-assessment.json \
  --repo-root /opt/liquidity-migration \
  --output /etc/liquidity-migration/account-execution-deploy-ready

scripts/ops.sh cutover-authority verify \
  --receipt /etc/liquidity-migration/account-execution-deploy-ready \
  --expected-commit "$COMMIT" \
  --repo-root /opt/liquidity-migration
```

After authority verifies and immediately before startup, the checked deploy
uses the authority-aware preparation surface. It reopens the authorization,
requires the stopped seal to still observe every registered unit stopped,
requires all ten fresh roots empty, and materializes the exact late environment
files. The equivalent explicit prestart check is:

```bash
scripts/ops.sh authorized-deploy-epoch prepare \
  --authorization /etc/liquidity-migration/account-execution-deploy-ready \
  --expected-commit "$COMMIT" \
  --repo-root /opt/liquidity-migration \
  --output-directory /etc/liquidity-migration/fresh-deploy \
  --systemctl-bin systemctl
```

The generated files cover both owners, four LONG/CONT producers, hedge, RMOM
refresh, and liveness. Deployment never shell-evaluates those systemd files:
the loader reopens the activated epoch through the authority verifier and
transfers only the exact ten root paths as NUL-delimited data. The pre-existing
Bybit/demo and two account-route EnvironmentFiles are likewise read as stable,
current-user-owned, single-link, exact-mode-`0600` data; only fixed allowlisted
keys are transferred, and the shell never sources their bytes. Repository and
host sleeve toggles use a separate strict three-key parser, so host overrides
cannot execute shell syntax. Earlier, before starting any newly staged service
for the pre-cutover evidence window, issue its separate non-deployment marker
from the exact clean candidate:

```bash
scripts/ops.sh authorized-deploy-epoch prepare-evidence-runtime \
  --expected-commit "$COMMIT" \
  --repo-root /opt/liquidity-migration
```

The marker is mode `0600`, machine/commit bound, and may be issued only before
activation state exists. Retain it through evidence collection and authority
issuance; once the authorization appears, keep every service stopped until
`prepare` consumes the marker after writing and verifying the activated latch.
`prepare` first writes a separate persistent activation-started history marker;
it survives failure and prevents a later operator from deleting partial
activation files and recreating pre-cutover evidence as though activation had
never begun.
Every
registered service starts through a wrapper that verifies the same inherited
environment and immediately replaces itself with the exact checked workload
argv. Guarded units accept no operator/runtime drop-ins or alternate effective
startup command. The wrapper does not use
a separate systemd pre-start process or deep-rehash historical archive roots on
each timer run.

Start the demo owner and wait for exact-head/capture
readiness, then start the paper owner and wait for readiness, before any
producer. Bootstrap LONG/CONT public-data producers before RMOM refresh; enable
hedge and liveness services/timers last. Poststart verification uses
`authorized_deploy_epoch verify` because healthy owners populate their roots;
it still verifies the registered path/inode identities and exact environment
bytes. `verify-processes` additionally proves that active units consumed those
exact late variables. Recovery must fail if a bound root is missing; recreating
it would create a different epoch.

The mode-`0600` receipt expires within 24 hours and is bound to the host's
machine-id fingerprint, full commit, assessment bytes, and every evidence
artifact hash. Its self-hash is corruption/tamper evidence, not a signature;
root can still replace it, so operator identity and substantive review remain
real responsibilities. Expiry blocks a new activation. It does not invalidate
the already-created latch: routine same-commit verification and recovery reopen
that latch and its exact bound artifacts without converting old authority into
a new deployment permission.

## Safe ledger reset

Preview exactly what would be archived and removed on the VPS:

```bash
scripts/ops.sh reset --leave-stopped --sleeves all --label new-forward-window
```

Only after reviewing that preview, request the guarded mutation explicitly:

```bash
scripts/ops.sh reset --execute --leave-stopped \
  --sleeves all --label new-forward-window \
  --receipt /absolute/new/path/account-epoch-reset.json
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
orders. Execute first resolves the authenticated Bybit `userID` with a
read-only credential query. Once all writers and owners are quiescent, reset
acquires the same host-global canonical demo-account lease used by the owner,
rule probe, and venue-accounting command; it holds that lease across flatness,
archive, fresh-root creation, projection replay, and boundary heartbeats. There
is no account-root or operator-selected lock override. Reset releases the lease
only immediately before the owner-first restart handoff. Lease contention
refuses before the flatness check, and an owner start failure is not retried or
followed by downstream producer startup.

The lease is a descriptor/path-inode capability, not merely an advisory lock on
whatever a pathname later resolves to. Symbolic or hard-link aliases are
refused, and an unlink or replacement makes every subsequent demo mutation fail
closed even while the original descriptor remains locked.

The receipt is available only for `--execute --leave-stopped`, is written
create-only mode `0600`, re-opens the archive/sidecar/manifest, and proves all
six new account/inbox/capture roots were empty while all 12 managed units were
stopped. Archive V8 first through the compatibility `v7-archive` route when this
reset creates the natural holdout boundary.

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
deploy is allowed to fetch or check out anything. Before systemd startup, the
deploy re-opens the authority-bound stopped/fresh epochs, requires the old units
still stopped and all ten new roots empty, and atomically materializes the nine
late environment files. It starts both owners and waits for their readiness
checks before any producer, seeds RMOM only after continuous kline bootstrap,
and starts hedge/liveness auxiliaries last. Live verification checks root
inodes, environment bytes, and active-process consumption; recovery refuses to
recreate a missing root.

For a private GitHub HTTPS remote, local deploys automatically reuse the credential
from `gh auth` when `GITHUB_TOKEN` is unset. An explicit `GITHUB_TOKEN` still
takes precedence. The credential is passed to the VPS over SSH stdin for the
fetch and one-time live-origin authorization/prepare checks, then unset before
service startup; Git receives the authorization through dynamic environment
configuration, never a process argument, and it is neither logged nor persisted.
Provider-console commands embed exact script bytes from the trusted local commit
rather than relying on anonymous access to the private repository. Initial deploy
and activated recovery install the exact `requirements.lock` with `--no-deps`;
they do not upgrade pip, resolve pyproject ranges, or perform an editable install.
Postactivation verification
and same-commit recovery use the latch and need no private-repository network
lookup.

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
