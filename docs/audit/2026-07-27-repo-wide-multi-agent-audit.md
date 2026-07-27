# 2026-07-27 repo-wide multi-agent audit — findings register (no fixes applied)

**Status: REMEDIATED 2026-07-27.** The register below is preserved verbatim as
the finding record; every one of the 53 unique items plus the four
operator-observed items has since been fixed on `main` (see "Remediation" at the
end of this file). At audit time the working tree was clean at `47406f3`
(deployed batch `13754d0be` live on the VPS); the fixes are NOT yet deployed —
the rollout dispatch belongs to the owner.

## Method and honesty label

- Ten read-only finder agents swept the repository in parallel, one per
  subsystem (account kernel, streams/WS, CONTINUOUS strategy, LONG/research,
  data pipeline, ops-Python, shell/deploy/systemd/CI, tests, docs,
  security/safety). Each finding carries quoted evidence from the actual file.
- The planned adversarial verification pass was **stopped by operator
  instruction before it ran**. Except where marked
  **[operator-corroborated]**, every item below is a **single-agent claim
  with quoted evidence, not yet independently confirmed**. Treat severities
  as provisional; confirm each item before acting on it.
- The operator (this session) independently checked the VPS runtime and the
  local quality gates; those observations are in the two closing sections.

## Baseline at audit time (all green)

- `scripts/dev.sh check` (doctor, ruff, mypy, pytest): pass — 2391 passed,
  1 skipped, 4 warnings (the warnings are one item below).
- `scripts/dev.sh doctor --json`: overall ready; lock matched (26 deps),
  skill mirrors matched, Graphify ready.
- VPS (`root@116.202.15.128`): installed commit `13754d0be` == origin/main;
  all 6 services + 3 timers active, 0 unit restarts since the 14:03 UTC
  deploy, no failed units, checkout clean; journald capped at 1G (770M
  used); fail2ban active; disk 36 %; CI green on the last 5 runs.

## Severity counts

| Severity | Count |
| --- | --- |
| high | 6 |
| medium | 22 |
| low | 25 |
| **unique total** | **53** (54 raw; two agents duplicated the paper-equity fallback) |

---

## High

### H1. Rollout/recover phases run with errexit disabled — quality and safety gates become non-fatal warnings
`scripts/deploy_vps_live.sh:182` — bug — **finder reports local reproduction**

`run_phase` executes its payload as `if "$@"; then`, and bash suppresses
`set -e` inside any function invoked in a condition context. Rollout and
recover invoke whole modes this way (`run_phase stopped-install
install_mode` line 1566, `run_phase activate-and-verify activate_mode`
line 1572, `run_phase current-topology-verification verify_topology`
line 1529), and inside `install_mode` the gates have no `|| fail`:
pip install (1030), ruff (1033), mypy (1034), focused pytest (1035), plus
`check_demo_order_permissions` (1301, 1350), `validate_hedge_model_prior`
(1277, 1352), and the demo-rule probe chain (946–950) rely on errexit alone.
The finder reproduced the structure locally: `phase-failed name=ruff
status=1` followed by `install-ok`, `phase-ok name=stopped-install`,
`rollout-ok`. Direct `MODE=install` aborts correctly — only the guarded
rollout/recover paths are fail-open. Concrete scenario: a rollout with a
transient pip failure installs nothing, tests the stale venv, and still
reports `rollout-ok`; a locally dispatched rollout of a commit with failing
focused tests activates anyway (the GitHub path is saved only by the
separate `needs: ci` job).

Suggested fix: append `|| fail "<phase> failed"` to every gated command
inside `install_mode` / `activate_mode` / `verify_topology` /
`refresh_stale_demo_rules_if_requested` (the `run_phase_pair … || fail`
pattern at 618–625 already exists), or stop nesting mode functions inside
`run_phase`'s condition context; add a test that a failing inner phase
aborts a simulated rollout.

### H2. LONG K2 kill criterion gated on epoch day-90, but the registration says "whenever 40 round trips exist"
`liquidity_migration/sleeve_kill_criteria.py:202` — bug (fail-open vs the registered contract)

Code requires `day90_reached and forward_days >= 60 and closed_in_epoch >=
40` for LONG K2; the governing registration
(`docs/preregistration/sleeve_kill_criteria_2026-07-20.md`) applies the
day-90/60-forward-day gate **only to CONTINUOUS** and says LONG K2
evaluates "once 40 completed round trips exist (whenever that occurs)". The
module docstring claims to be the executable form of that registration.
`tests/test_sleeve_kill_criteria.py::test_k2_and_k3_evaluate_only_at_day90`
encodes the wrong behavior. Consequence: a dead LONG run can keep trading
up to ~2 extra months. Either the code follows the registration, or the
registration is amended explicitly — currently they silently disagree in
the fail-open direction.

### H3. 25× scale-up left absolute-USDT kill thresholds unamended — K1 now trips on a single routine stop-out
`liquidity_migration/sleeve_kill_criteria.py:33` — config — **owner decision required**

`K1_DRAWDOWN_LIMIT_USDT = {"continuous": -500, "long": -400}` and the
40-USDT unattributed-P&L provisional flag were registered against the 10k
capital reference ("roughly a 10 % loss of maximum deployed gross"). Commit
`58c3432` scaled the profile 25× but touched neither these constants nor
the registration. At 250k equity one normal 1.5-ATR stop-out (~1,100+ USDT)
exceeds the −400 LONG limit; −500 for CONTINUOUS is ~0.1 % of the 500k
gross cap. K1's meaning changed from ~10 % of max gross to ~0.4 %. Because
the registration governs, the fix is an explicit amendment note (scale the
limits with the capital reference) or an explicit re-affirmation — not a
silent constant change. A false K1 trip is near-certain at the new sizing.

### H4. ORDER_STATUS "filled" tolerance mismatch can permanently wedge reconciliation on large multi-fill orders
`liquidity_migration/account_kernel.py:437` — bug — materialized by the 25× scale-up

The reducer's terminal-status check uses an **absolute** 1e-12 tolerance
(`abs(order.filled_signed_qty - order.signed_qty) > 1e-12` → raise), while
fill reconstruction (`_apply_fill`, lines 159/174) and the WS/REST consumer
gate (`account_execution_stream.py:463,499`) use a **quantity-scaled**
tolerance `max(abs(signed_qty) * 1e-12, 1e-12)`, and `filled_signed_qty` is
never snapped to `signed_qty`. A market order with qty ≳ 1e4 base units
(routine post-25×: a $5k+ position in a sub-cent coin is 1e5–1e7 units)
filling in 2+ partials can accumulate ~1-ulp float error — inside the scaled
tolerance, outside the absolute one — so the venue's Filled row raises
`AccountTransitionError` inside the journal transaction forever: the WS
consumer retries every 0.25 s, `reconcile_once` raises every pass, the
position-truth report goes stale, owner health latches BLOCKED, and even
reduce-only exits are refused until manual intervention. Fix: use the
scaled tolerance in the ORDER_STATUS branch and/or snap
`filled_signed_qty = signed_qty` on the filled transition.

### H5. Crash replay of a committed convergence batch recomputes the request hash from the live L2 price — replay raises instead of resubmitting
`liquidity_migration/account_service.py:1865` — bug

Convergence batches call `kernel.submit_targets` without
`request_content_hash`, so the batch hash covers derived target payloads
including `reference_price` taken from a **fresh** L2 book on every call.
The documented crash-replay path (`converge_once` lines 1994–2008) reuses
the same batch id with a recomputed hash; any price movement between commit
and replay makes the builder raise `AccountJournalIntegrityError("batch id
… reused but request content changed")` instead of resubmitting the
commanded orders. `_target_batch_request_hash`'s own docstring states the
opposite intent, and the existing replay test passes only because its mock
market returns a constant price. Consequence: after a crash mid-convergence
the commanded reduce-only close is never resubmitted, the raise aborts the
whole plans loop (starving all symbols), and the owner sits BLOCKED with an
open venue position. Fix: stable `request_content_hash` for convergence and
entry-unwind batches (exclude `reference_price`), or rebuild replay targets
from the committed batch's own TARGET payloads; add a replay test whose
market moves between commit and replay.

### H6. STATE.md "Risk envelope" still states pre-25× caps
`STATE.md:187` — doc-staleness — **[operator-corroborated]**

"symbol notional at **5,000 USDT**, component/account gross at **20,000
USDT**, initial margin at **10,000 USDT**" contradicts
`configs/operational.demo.json` (125k / 500k / 250k) and the same file's
own Deployment bullet recording the 25× profile deployed 2026-07-27 ~14:03
UTC. Update to the deployed figures.

---

## Medium

### M1. Paper twin silently falls back to 10,000 USDT equity when PAPER_EQUITY_USDT is unset
`scripts/run_account_paper_execution_service.sh:17` — config (merged: reported independently by two finders)

`PAPER_EQUITY_USDT="${PAPER_EQUITY_USDT:-10000}"` is now 25× stale versus
`capital_reference_usdt: 250000` and contradicts 58c3432's stated design
("derives PAPER_EQUITY_USDT from the committed profile's capital reference
instead of a hidden per-host tuning value";
`tests/test_runtime_scripts.py:308` asserts it was removed from
allowed_tuning). Every sibling required input in the script fails closed
(`exit 2`), and neither authority issuance nor
`_validate_paper_runtime_environments`
(`operational_runtime_authority.py:1144–1216`) checks the key exists in the
env file — so a hand-edited env silently runs the twin 25× under-scaled.
Fix: required-variable check (exit 2 when unset/empty), optionally assert
presence in the paper authority validation, plus a test that the script
contains no hardcoded equity fallback. (The demo wrapper already refuses
hidden defaults for DISASTER_STOP_FRACTION.)

### M2. Live crowding-gate counting base diverges from the evidence engine
`liquidity_migration/continuous_demo.py:1248` — bug

The live sleeve counts crowding on the age-filtered, funding-admitted
`funded` frame; the engine that produced the adopted single_fund0 evidence
counts crowding **before** the age gate and only on fresh decile entrants
(`continuous_events.py:879–882`, `_fresh_entries` line 678; age gate
applied per-entry afterwards at 1075–1081). With crowd cap 2 and age floor
240 d, an hour where two old pumps share the signal ts with one young pump
is counted 3 by the engine (skip all) but 2 live (enter both) — the live
book takes entries the validated engine crowd-skipped, in exactly the
fresh-listing-squeeze hours the gates were designed around; the reverse
divergence exists for held/cooldown re-qualifiers. All crowding tests use
`age_days_min=0`, so the interplay is unpinned. Fix: mirror the engine's
counting base (count before `eligible_symbols`, restrict to fresh
entrants), then pin a parity test.

### M3. `entry_first_rejection_reason` reports "capacity" when the funding floor rejected everything
`liquidity_migration/continuous_demo.py:1440` — bug

"funding" is missing from `_first_entry_rejection_reason`'s totals; when
every age-qualified candidate is funding-rejected the cascade falls through
to `return "capacity"`, and funding-rejected names never enter
`selection_rejections`. Operator telemetry then shows "first rejection
capacity" while the funnel shows funding 0 — contradictory for the
signature rejection mode of the deployed funding-gated profile. Fix: add a
`funding` total and a `funding_admission` return between the age and
available checks; name the rejected symbols in `selection_rejections`; add
a test.

### M4. Segment rollover in SegmentedCaptureStore is not exception-safe — one failure wedges that symbol's capture until restart
`liquidity_migration/market_capture.py:445` — bug

`_segment()` closes the old segment, then mkdir/open/fchmod/fstat run, and
only on success is `self._segments[key]` replaced. A raise after the close
(EIO, EACCES, inode exhaustion — not covered by the free-disk floor) leaves
the registry pointing at a CLOSED file; every later append re-enters
rollover and raises `ValueError('I/O operation on closed file')` forever,
and `store.close()` also raises mid-iteration, leaving other segments
unflushed. Fix: pop the retiring segment before closing, tolerate
close-failures, or open-and-install the new segment first; make `close()`
per-segment tolerant.

### M5. `rewrite_manifest_to_coverage` deletes the PIT membership dataset outside the lock and non-atomically
`liquidity_migration/binance_vision.py:781` — bug

`rmtree(archive_trade_manifest)` runs with no lock held; the lock is only
acquired inside the subsequent `write_dataset`. `storage.py:866–869`
promises readers a consistent snapshot under that lock; a concurrent
reader can lose part files mid-collect, and SIGKILL between rmtree and
write leaves the root's PIT membership dataset gone/partial. Runs
routinely via `topup_binance_daily_klines`. Fix: take the dataset lock
around the whole replacement and stage+rename (the pattern
`_publish_staged_binance_datasets` already uses).

### M6. Binance/Bybit REST fetchers are end-inclusive while the documented contract is end-exclusive `[start, end)`
`liquidity_migration/binance.py:154` (also `bybit_market_data.py:253/326/369`) — bug

Row filters use `start <= ts <= end` while `cli_parsers.py` documents
`--end` as exclusive and `downloaders.py:500` prints `[start..end)`. A
`--end 2026-07-01` download writes the 00:00 bar of 2026-07-01 into a
`date=2026-07-01` partition on both venue roots; `pit_coverage` then reads
kline coverage one day fresher than real, and the panel's daily aggregate
can turn the 1-bar day into a bogus daily close at the tail. Fix: filter
`start <= ts < end` (or request `endTime = end - 1`).

### M7. Bybit window-paged kline fetchers lack the mid-range empty-page guard; the downloader then seals the hole with a completeness marker
`liquidity_migration/bybit_market_data.py:250` — bug

`get_klines` and `_get_price_index_klines` advance past an empty page with
no guard — unlike `_paged_time_range` in the same file, which raises on
"an empty page after a full page is a mid-range hole", and unlike all
Binance pagers. A transient retCode-0 empty window silently drops up to
limit×interval bars, after which the full-range `.done` marker is written
(frame non-empty) and the gap is never refetched. Fix: raise/retry on
mid-range empty windows, or hour-grid contiguity-check the fetched frame
before writing the marker.

### M8. Demo-cycle kline gap detection is head-blind — a missing window head is never backfilled
`liquidity_migration/event_demo_data.py:863` — bug

The hole check is interior-only (`n < (hi-lo)//h + 1`) and otherwise
fetches tail-only from `hi`; nothing compares a symbol's earliest cached
bar to `start_ms`. Widening `lookback_days` beyond the 45(+3)-day pruned
retention leaves the head silently absent for ~48 days, degrading every
warm-up-dependent feature with no signal; a partially-bootstrapped WS-store
symbol can also count as fully covered. Fix: flag `lo > start_ms + 1h` as
incomplete (unless explained by `launch_time_ms`) and fetch the head.

### M9. `_read_window` reads the entire multi-year dataset then filters; `since_date` pruning has zero production callers
`liquidity_migration/daily_feature_panel.py:95` — performance

`read_dataset_columns` was given `since_date` precisely to prune
date-partitioned files before opening parquet ("a full walk … ~500k files /
tens of seconds"), but its only references are storage.py and one test.
`build_feature_panel` (used by the live residual-momentum refresh) walks
full history of four datasets per windowed build. Fix: pass
`since_date=UTC(read_start − 60d pad)` for all four datasets.

### M10. Rollout/reset remote scripts don't trap HUP/PIPE — SSH loss mid-operation skips cleanup and can leave a partial fleet
`scripts/deploy_vps_live.sh:1547` (also `reset_demo_paper_ledgers.sh:1232`) — safety

Traps cover EXIT/INT/TERM only. When the client dies (laptop sleep, network
drop, Actions cancellation/timeout), sshd HUPs the remote process group and
writes to the dead pipe raise SIGPIPE; bash skips the EXIT trap on untrapped
fatal signals, so `rollout_cleanup`/`stop_all_rollout_units_best_effort`
and the reset's fail-closed handoff never run. A drop between
stop-downstream-units and stop-account-owners leaves owners trading with
producers stopped. (Same class as the real 2026-07-26 timeout-kill
incident.) Fix: add `trap 'exit 129' HUP` and `trap 'exit 141' PIPE`, and
make cleanup output survive a dead stdout (logger/file).

### M11. All three Type=oneshot units have infinite start timeouts — one wedged run silences its own timer, including the liveness watchdog
`deploy/systemd/liquidity-migration-demo-liveness.service:10` (also continuous-hedge, rmom-refresh) — safety

systemd's oneshot default is `TimeoutStartSec=infinity`; `OnUnitActiveSec`
timers cannot re-trigger while the unit is still activating. A single hung
liveness run silently stops the 3-minute position-safety watchdog — the
fail-open case its own comment warns about. `seed_rmom`'s bounded-retry
deadline also can't fire during a hung synchronous start, stalling a
rollout indefinitely. Fix: explicit `TimeoutStartSec` per unit (e.g.
120 s liveness / 300 s hedge / 900 s rmom), letting a wedged run go
`failed` (already handled by verify/reset paths).

### M12. STATE.md paper-Telegram section still says "committed locally, not deployed" / "paper fleet remains Telegram-silent"
`STATE.md:95` — doc-staleness — **[operator-corroborated]**

Contradicted by the same file's Deployment bullet (paper Telegram is in the
deployed batch, first hourly digest at the next full hour). Retitle and
rewrite as deployed.

### M13. STATE.md installed-profile SHA-256 is the pre-25× hash
`STATE.md:89` — doc-staleness — **[operator-corroborated** via the VPS authority receipt, which pins risk-policy.json at `8e7cdffe…`**]**

`cf68369c…` is the pre-scale-up profile; the deployed 25× profile hashes to
`8e7cdffe6c6b6c775d9b8e887855def9d05ead614eef2c8eb2cf115a9bf2a443`.

### M14. STATE.md "Timing note" contradicts the half-life re-probe one sentence above it, and quotes an expired receipt date
`STATE.md:148` — doc-staleness — **[operator-corroborated]**

The pre-36cf76a "auto-refreshes only once expired / dispatch shortly after
2026-07-29" advice survives directly under the sentence declaring it
obsolete; the current receipt was refreshed 2026-07-27, expires
~2026-08-03. Delete the residual sentence.

### M15. STATE.md scale-up paragraph still claims the old 20k/5k caps bind pending rollout
`STATE.md:128` — doc-staleness — **[operator-corroborated]** — the 25× profile deployed the same day; rewrite in past tense and note the hedge venue-minimum threshold is now crossed.

### M16. plain_english_guide §5 claims "the old smaller caps still bind on the server" — violating its own same-change freshness contract
`docs/plain_english_guide.md:186` — doc-staleness — the deployment receipt commit (47406f3) touched only STATE.md. Update §5 to present tense (250k caps live as of 2026-07-27).

### M17. REAL_MONEY=true/ambiguous rejection untested at the runtime-authority and paper-owner layers
`tests/test_operational_runtime_authority.py:441` — test-gap

The rejection code exists at three layers
(`operational_runtime_authority.py:1013,1487`;
`account_paper_runner.py:106`) but grep finds zero tests setting
REAL_MONEY=true/"maybe" against them — only the bybit client layer has
negative-path tests. Add the negative-path tests at issuance, verification,
and paper-owner startup.

### M18. The 7af59f3 null-not-0.0 equity fix is pinned only for the LONG daemon
`tests/test_liquidity_migration_continuous_demo.py:1514` — test-gap

`continuous_demo.py:2667` has the identical fix (a literal 0.0 reads as a
−100 % equity spike in cycles-derived curves) but the only continuous test
mocks owner health as always healthy. A regression to 0.0 would pass the
whole suite. Add the blocked-owner-health continuous test asserting
`equity_usdt is None`.

### M19. `daily_scores` never charges exit/re-entry turnover across flat days
`liquidity_migration/financed_longs.py:334` — bug

Turnover iterates only bars present in `weights`; a flat decision day
produces no row, so the exit into it and re-entry out of it are both
uncharged while gross treats the book as liquidated. Finder reproduction:
0.778 bp charged vs 2.334 bp actually paid over a 3-day flat-gap pattern.
Understates costs of both Lane-2 financed-longs configs (t 4.88 / t 4.04)
in proportion to gate-flip frequency and removes flat days from the
day count. Fix: emit a weight row (empty dict) for every decision bar;
extend the misnamed entry-and-exit test to assert the exit leg.

### M20. End-of-window force-close sweep deletes scan-exited positions before `_flush_exits`
`liquidity_migration/long_native.py:1251` — bug

Violates the mark-source invariant `_scan_all_positions` itself documents
and enforces; two positions scan-exiting at different timestamps in the
final sweep make the earlier batch lack prices for the later symbol and
`HistoricalAccountSession.submit_decisions` raises, aborting the research
run. Fix: mirror `_scan_all_positions` (flush first, then delete).

### M21. `residual_momentum_expr` is row-positional — mid-history gaps silently stretch the registered [D-9..D-3] calendar window
`liquidity_migration/residual_momentum.py:23` — bug

`rolling_sum(7).shift(3)` over per-symbol rows deviates from the registered
calendar definition for gapped symbols (delist/relist, archive holes,
dropped factor days); `_common.calendar_shift` exists precisely for this
failure class (BAC-1/BAC-7). Causality is preserved, but gapped-symbol
values deviate from the registered definition, and a later backfill changes
stable values — which the append-verify then hard-fails. Fix: pad missing
per-symbol days with null residuals before the expression (min_samples=4
already tolerates ≤3 missing), or amend the registered definition.

### M22. `_convergence_plans` rebuilds an O(journal) dict + full event-list copy at least twice per ~0.1 s owner loop
`liquidity_migration/account_service.py:1507` — performance

`journal.events()` returns a full copy; the dict's only consumer is a
single `get(revision_sequence)`, and journal verification already
guarantees contiguous 1-based sequences, so `events[revision_sequence-1]`
suffices. On a long-lived 25×-scale journal (1e5+ events) this is two full
scans per 100 ms in the single-owner hot path. Fix: bounds-checked direct
index off the owner-internal snapshot.

---

## Low

### L1. Cumulative silent-window clock counts frames whose recorder callback failed as accepted
`liquidity_migration/market_capture.py:1466` — bug — the exception path rolls back only the per-symbol clock; `_last_frame_monotonic` (added in 7af59f3) and the outage-warning latch are not restored, so under persistent callback failure the "no accepted frame for Xs" warning can never fire. Roll both back, mirroring the per-symbol path.

### L2. Ticker WS silence watchdog never fires for a stream that never delivered a single event
`liquidity_migration/long_native_event_demo_daemon.py:629` — bug — `seconds_since_last_ws_event()` returns inf until the first push and the health check skips inf; the recovery path only rebuilds when no stream object is installed. A born-silent subscription is neither warned about nor rebuilt. Track install time; treat installed+inf+past-threshold as silence.

### L3. Kline follower records the new snapshot signature even when `recover_from_disk()` failed
`liquidity_migration/kline_follower.py:236` — bug — a failed read is treated as consumed; the merge retries only when the leader's next flush changes the file (up to ~an hour), with REST fallback paying the difference and `_refreshes` misreporting. Only update `_last_sig` when rows were actually merged (or distinguish empty-vs-failed).

### L4. Post-fill markout `missing_reason` diagnosis branches are unreachable dead code
`liquidity_migration/market_capture.py:1069` — cleanliness — the only route to status="missing" is lateness overflow, so the crossed_book / no_snapshot / gap arms never execute; every missing markout gets the generic reason. Delete the arms or emit the book condition as a separate field.

### L5. Download coverage markers accumulate unboundedly and are re-globbed per symbol per refresh
`liquidity_migration/downloaders.py:605` — performance — ~3.6k files/day forever, O(symbols × total_markers) rescans. Unlink superseded markers on write; glob once per (dataset,suffix).

### L6. Stale `binance_proxy_failed_jobs.json` survives a clean re-run
`liquidity_migration/downloaders.py:171` — cleanliness — the artifact is only written when the current run has failures; the binance_vision path always rewrites (empty list on success). Always pass `artifact_path`.

### L7. SIGKILL mid-build permanently orphans multi-GB hidden staging/backup trees
`liquidity_migration/binance_vision.py:1576` — cleanliness — cleanup is in-process only and tokens are per-run; nothing ever sweeps `.{root}.binance-oos-staging-*` orphans. Sweep unreferenced, aged orphans at build start.

### L8. `densify_trade_klines_1h` forward-fills before restoring row order
`liquidity_migration/ingestion.py:148` — bug — relies on unspecified polars left-join row order; a permuted grid could fill a gap hour from a later close (look-ahead inside densified archive bars), hidden by the subsequent sort. Sort before computing the fill (or `maintain_order="left"`).

### L9. Workflow SSH_OPTS override drops keepalives and the vps job has no timeout-minutes
`.github/workflows/vps-deploy.yml:134` — config — the override loses `ServerAliveInterval/CountMax` present in the script default and leaves the 6-hour job default, compounding the M10 disconnect class. Re-add keepalives; set a realistic `timeout-minutes`.

### L10. RMOM refresh is the only managed unit with no memory bound
`deploy/systemd/liquidity-migration-continuous-rmom-refresh.service:8` — config — the most memory-hungry workload (POLARS_MAX_THREADS=8, full-rewrite) is the one unit that can pressure the global OOM killer against the co-resident owner. Add MemoryHigh/MemoryMax sized from observed peaks.

### L11. `ops.sh` remote helpers expand possibly-empty arrays unguarded under `set -u`
`scripts/ops.sh:83` — bug — breaks the documented no-argument `ops.sh reset` preview (and bare `clock-offset --execute`) on Bash 3.2 operator laptops the repo elsewhere deliberately supports; the portable guard idiom is already used three lines earlier. Use `${arr[@]+"${arr[@]}"}` in all four remote_* helpers.

### L12. `capture_clock_offset()` default max_error_ns=50 ms contradicts the registered 100 ms contract its own verifier enforces
`liquidity_migration/clock_offset_receipt.py:86` — config — a future caller using the default produces receipts that can never verify (fail-closed dead end); the sole production caller works around it. Default from `REGISTERED_MAX_ERROR_NS` (and the other REGISTERED_* constants).

### L13. Paper owner's per-command component resolver copies and scans the entire journal per order submission
`liquidity_migration/account_paper_runner.py:296` — performance — O(journal) per command inside the 250 ms decision window, growing for the epoch's life; reduce-only commands don't even need the answer. Resolve from reduced state (target_proposals), memoize, or short-circuit reduce-only.

### L14. `_flush_terminal` full-journal rescan per new terminal and per 0.25 s blocked retry
`liquidity_migration/account_execution_stream.py:493` — performance — track last-scanned sequence and scan the delta, or drop the rescan (`record_order_status` is already idempotent in-transaction).

### L15. BTC-risk authoritative reconciliation replays the full receipt chain from genesis with O(n²) scans every 60 s
`liquidity_migration/continuous_btc_risk.py:713` — performance — re-hashes every receipt and rebuilds candidate lists per iteration each cycle; fine today (chain reset 2026-07-26), hundreds of ms of pure Python forever at a few thousand decisions. Short-circuit on unchanged authoritative set; index by predecessor hash; bisect for percentile scoring.

### L16. GitHub token placed on a process command line during authenticated fetch on the VPS
`scripts/deploy_vps_live.sh:765` — safety — because the command begins with `/usr/bin/env`, the `GIT_CONFIG_VALUE_0="AUTHORIZATION: Basic …"` word is argv of env, world-readable via /proc during the fork-exec window (and it's the operator's long-lived `gh auth token` on local dispatches). Every other secret path in the repo keeps credentials off argv. Export in a subshell and `exec git`, or use GIT_ASKPASS reading a 0600 file.

### L17. Dead-man's-switch heartbeat suppression on Telegram send failure has zero test coverage
`scripts/check_demo_liveness.py:1660` — test-gap — the one 7af59f3 watchdog addition that didn't get a test; a regression makes the external monitor read "all quiet" exactly when the alert channel is dead. Add the three main()-level tests (pings when healthy; no ping on send failure; no ping on CRITICAL).

### L18. The registered Lane-2 venue-scoped-admission forward scorer has no tests
`scripts/render_continuous_admission_variants.py:127` — test-gap — the committed scorer for the registered `fund0_venue_scoped` lead is unpinned (patched filter, +1 h bisect boundary, concat ordering); silent drift corrupts the forward comparison any promotion depends on. Add hermetic unit tests on the mode functions with in-memory frames.

### L19. `lag_screen` is dead code and raises TypeError whenever `periods_per_year` is supplied
`liquidity_migration/cross_section.py:221` — cleanliness — forwards kwargs to a function without **kwargs while its own next line expects the key; zero callers or tests. Delete it (git history preserves) or fix the kwarg plumbing.

### L20. `_mtm_daily_curve` docstring claims per-trade telescoping that is false for notional_weight < 1
`liquidity_migration/long_native.py:206` — doc-staleness — the nw-scaled arithmetic daily sums differ from the booked per-trade totals by O(nw·r²), material for FC trades with 15–20 % daily moves. Correct the docstring (or distribute log-returns if tie-out is wanted).

### L21. STATE.md L2-stale alert row still marks d11db79+7af59f3 "pending rollout"
`STATE.md:267` — doc-staleness — **[operator-corroborated]** — the batch deployed 2026-07-27 ~14:03 UTC. Update, and record whether post-deploy transport logs captured Bybit's close/error codes on the next episode.

### L22. docs/operations.md command table omits `ops.sh kill-criteria`
`docs/operations.md:24` — doc-staleness — the weekly K1/K2/K3 check the active contract relies on isn't in the operator-facing table. Add the row.

### L23. docs/next_agent_prompt.md still frames the CONTINUOUS replacement as committed-locally, rollout pending
`docs/next_agent_prompt.md:8` — doc-staleness — it deployed 2026-07-26 and was superseded 2026-07-27; the referenced "local-candidate section" of STATE.md now describes something else. Rewrite the header paragraph.

### L24. docs/strategy_program.md "Current truth" bullet claims the account-kernel remediation "remains undeployed"
`docs/strategy_program.md:22` — doc-staleness — stale since the 2026-07-25/26/27 deployments of canonical main. Delete or restate in past tense.

### L25. docs/backtesting_errors_we_never_repeat.md points to an "evidence card" concept governance.md no longer contains
`docs/backtesting_errors_we_never_repeat.md:172` — doc-staleness — the rewritten governance doc defines a "short evidence note" (§4). Fix the cross-reference.

---

## Operator-observed items (this session, not from the finder agents)

### O1. `join_asof` sortedness warning in the test suite
`liquidity_migration/cross_venue_panel.py:401` — cleanliness — **[operator-corroborated]**

Four `tests/test_cross_venue_panel.py::TestTiming` tests emit
`UserWarning: Sortedness of columns cannot be checked when 'by' groups
provided`. Both sides are provably sorted (`panel` by
`[decision_ts_ms, symbol]`, `funding` by `[ts_ms, symbol]` — a global
ts-first sort implies per-group order), so the warning is noise; polars
1.41 supports `check_sortedness=False`. Fix: pass it with a comment stating
why sortedness is guaranteed, keeping outputs byte-identical.

### O2. VPS kernel-update reboot is pending and now safe to schedule — owner action
The `/var/run/reboot-required` marker is present with OS updates staged.
Per STATE.md, receipts were refreshed during the 14:03 UTC rollout
(demo-rules receipt expires ~2026-08-03), so the reboot is now safe to
schedule; it restarts the fleet, so it belongs with an owner-directed
maintenance window (reboot only after receipts, never before — that
condition is already satisfied).

### O3. VPS CPU is saturated by design — capacity observation, not a defect
Load average ~6.1 on 2 cores. The four producers are niced below the
account owners (RNsl/SNsl vs S<sl), so owner latency is protected, but the
known ">100 s LONG cycle in 32 % of 20-min windows" shape (STATE.md) is
consistent with steady-state CPU starvation of the producers. If cycle
latency ever matters more than hosting cost, more cores is the lever; no
code change recommended.

### O4. sshd brute-force noise in the journal — no action needed
Routine internet background scanning against root/admin; fail2ban is
active, key-only auth in effect, journal errors are all preauth failures.
Recorded so a future operator doesn't re-diagnose.

---

## Suggested remediation order (when fixes are authorized)

1. **H1** (fail-open rollout gates) and **M10/M11/L9** (the disconnect/
   timeout cluster) — deployment-safety machinery first.
2. **H4/H5** (kernel wedge + replay defect) with their new regression
   tests — account-owner robustness at the new 25× sizing.
3. **H2/H3** — kill-criteria registration reconciliation (owner decision on
   amendment vs re-affirmation; H3 blocks honest weekly kill checks at the
   new scale).
4. **M2/M3** — CONTINUOUS live-vs-engine parity + telemetry truthfulness.
5. Docs cluster (**H6, M12–M16, L21–L25**) in one sweep — most are
   operator-corroborated already.
6. Data-pipeline correctness (**M5–M8, L8**), then performance items
   (**M9, M22, L5, L13–L15**), then remaining lows.

Every item above should be re-verified against current source before its
fix lands — the adversarial verification pass this audit planned was
stopped, and single-finder claims occasionally dissolve under scrutiny.

---

## Remediation (2026-07-27, committed on `main`, not yet deployed)

Every item above was re-verified against current source before its fix landed,
as the closing note asked. No single-finder claim dissolved under scrutiny;
three were narrowed while being fixed and are noted below. Each behavioural fix
carries a regression test that fails on the pre-fix code.

**High.** H1 replaced the fail-open nesting outright: mode-level phases now run
through `run_strict_phase`, which never puts its payload in a condition context,
so errexit stays lethal for every command a mode runs; `run_phase` additionally
aborts via `fail` (`exit` is honoured regardless of errexit state), and the named
gates carry `|| fail`. H2/H3 were resolved by amending the registration
(`docs/preregistration/sleeve_kill_criteria_2026-07-20.md`, "Amendment —
2026-07-27"): K1 binds to the committed profile's `capital_reference_usdt` at the
registered −5%/−4%, and LONG K2 is no longer gated on epoch day 90. LONG K3's
registered day-180 leg, which simply had no executable form, was implemented at
the same time. H4 introduced one shared `quantity_tolerance()` rule across the
kernel, the execution stream and the venue-protection path, and snaps
`filled_signed_qty` on the filled transition. H5 gives owner-generated
convergence/entry-unwind batches a stable `request_content_hash` that excludes
`reference_price`. H6 and the whole docs cluster were rewritten to the deployed
figures; the 25× profile's SHA-256 was recomputed locally rather than copied
(`8e7cdffe…`).

**Medium.** M2 was implemented as the audit specified — the crowd count now runs
on the funding-admitted *fresh entrant* frame before the age gate — which
required carrying the previous confirmed bar's decile/trigger columns alongside
the deciding bar so the live sleeve can reproduce `_fresh_entries` exactly. When
that prior-bar state is unavailable the fallback treats every row as fresh, which
over-counts the crowd and therefore skips more entries: the conservative
direction for a gate whose purpose is refusing fresh-listing-squeeze windows.
This is an intended change in live entry behaviour, not a refactor. M5 added
`storage.replace_dataset` (stage + swap under the dataset lock) rather than
patching the one call site. M7 was narrowed while being fixed: raising on *any*
mid-range empty window would false-positive on symbols listed or delisted inside
the requested range, so the guard fires only on an empty window bracketed by
populated ones, and an empty window is re-requested once first. M8 needed the
symbol's listing time to avoid refetching a young symbol's absent head every
cycle, so `launch_time_ms` is threaded from the cycle universe. M19's fix changes
the reported cost of both Lane-2 financed-longs configs: turnover now spans every
decision bar between the first and last weighted bar, so a flat gate-flip day
charges its exit and re-entry and counts as a day.

**Low.** L4 was fixed as "emit the book condition as a separate field" rather
than by deleting the arms, so the diagnostic they were reaching for survives in
`missing_book_condition`. L19 was fixed rather than deleted: `lag_screen` now
takes explicit scoring parameters and has tests. L12 discovered that only
`max_error_ns` had drifted; all five defaults now derive from the `REGISTERED_*`
constants so the class of drift cannot recur.

**Operator-observed.** O1 is fixed (`check_sortedness=False` with the reason
stated); the suite now runs warning-free. O2 (kernel-update reboot) and O3
(CPU capacity) remain owner decisions and are unchanged by this work; O4 needed
no action.

**Owner decision (2026-07-27): M2, M19 and M21 approved.** Recorded as change
points in `docs/strategy_program.md` ("2026-07-27 — recorded change points") and
summarised operationally in `STATE.md`. Follow-through:

- M2 is a live entry-behaviour change (strictly more crowd-skips, never fewer);
  the forward CONTINUOUS record is continuous across it but entry counts in
  crowded hours are not comparable to earlier days.
- M19 turned out to *restore* reproduction parity rather than invalidate
  anything: the full-calendar correction had been recorded in
  `docs/research_2026-07-26_financed_longs.md` on 2026-07-26 but never reached
  `daily_scores`, so the documented reproduction command had been printing the
  superseded active-days-only view. Re-scored on the current panel the
  registered bench-window table reproduces exactly (Sharpe 2.56 / 2.21 / 1.66,
  t 4.69 / 4.04 / 3.03); the three full-sample t-values still quoted on the old
  basis are corrected to 4.87 / 4.01 / 2.77 and no verdict moves. Total turnover
  actually charged rose 1–3%.
- M21 cannot break the deployed path: `run_continuous_rmom_refresh.sh` already
  passes `--full-rewrite`. On a stable research root the append overlap verify
  fires once by design; its message now distinguishes a deliberate definition
  change from source drift, and that behaviour is pinned by a test.

Nothing in this remediation was deployed, no sleeve toggle moved, and
`REAL_MONEY` is untouched. The rollout dispatch belongs to the owner.
