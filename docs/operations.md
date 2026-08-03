# Operations

[`scripts/ops.sh`](../scripts/ops.sh) is the operator router and
[`scripts/deploy_vps_live.sh`](../scripts/deploy_vps_live.sh) the deploy engine behind it;
both act on one VPS over SSH. `scripts/ops.sh help` is the current surface. The funded
fleet — its envelope, arming runbook, and what is still unproven — is the *Real money*
section below.

## Commands

| Command | Effect |
| --- | --- |
| `status` | Read-only topology verification (`deploy_vps_live.sh verify`). Prints a unit table and every mismatch it found. |
| `units` | List the fleet's units and timers. |
| `logs UNIT [LINES]` | One unit's journal, newest last (default 100 lines). |
| `restart` / `stop` / `start UNIT...` | Unit lifecycle, one or more units at a time. |
| `equity [ARGS]` | Descriptive equity curves. `--sleeves long,continuous,carry` (`carry` renders `configs/lane2_carry_hold_v3.json` from the cross-venue panel, not a daemon replay), `--years N`, `--chart-leverage X`. |
| `research-refresh {plan,run}` | Append-first data/features/backtest workflow. `plan` mutates nothing. |
| `reset [ARGS]` | Demo ledger reset. Preview unless `--execute`. |
| `flatten --environment ENV [ARGS]` | Take one account to zero exposure through its own owner. Reads only unless `--execute`. |
| `venue-accounting [ARGS]` | Reconcile the demo journal against Bybit executions, fees, closed P&L, funding, positions, open orders. Runs on the host with the host's credentials; `LOCAL=1` runs it against this checkout. Read-only. |
| `wedged-command {report,probe,resolve}` | Read venue truth for an order command that can no longer progress; `resolve` writes one journal transition, never resends an order, and refuses while the venue still holds it. The wrapper owns the demo account root/id/realm and loads the owner's credentials on the host, so the operator passes only the subcommand and its flags. Demo resolves these itself — see below. |
| `real-money {preflight,render-profile,create-state-roots}` | Read-only arming report; profile render (`--execute --output PATH` writes one non-secret file); mainnet journal directories (dry-run unless `--execute`). Starts nothing. |
| `deploy MODE [ARGS]` | `MODE` is `install`, `activate`, `staged`, `rollout` or `stop-mainnet`. |
| `help` | Print the current surface and do nothing else. |

A `UNIT` that does not already start with `liquidity-migration-` gets the prefix, so `logs
bybit-carry-demo.service` reads `liquidity-migration-bybit-carry-demo.service`.

Tests are local and are not an operator route: `scripts/dev.sh test`,
`scripts/dev.sh check`, or `.venv/bin/python -m pytest -q`.

`SSH_TARGET` (`root@116.202.15.128`), `REPO_DIR` (`/opt/liquidity-migration`) and `PYTHON`
override the defaults; `LOCAL=1` runs `real-money` and `venue-accounting` against this
checkout. `EXPECTED_COMMIT` is optional: left unset it defaults to `$REMOTE/$BRANCH`
(`origin/main`) and falls back to local `HEAD`, printing which it chose. Set explicitly it
must still be a full lowercase 40-character commit and is still validated. Deploy also
reads `BRANCH` (default `main`), `REPO_URL`, `REMOTE`, `SSH_OPTS`, `GITHUB_TOKEN` (falls
back to `gh auth token`) and the two `RMOM_BOOTSTRAP_*` durations. Deploy, activate, verify
and an executing reset share `/run/liquidity-migration/maintenance.lock`; a collision fails
before reading or mutating anything.

## Deployment

One command covers the ordinary case; the two-step form is there when you want to inspect
the stopped host between install and activation.

```bash
scripts/ops.sh deploy staged --profile operational   # install + profile + activate
scripts/ops.sh status
```

```bash
scripts/ops.sh deploy install     # fleet stays stopped
scripts/ops.sh deploy activate
```

`EXPECTED_COMMIT` is optional throughout; unset, it deploys `origin/main`.

**install** needs a clean remote checkout. A running fleet is **stopped automatically**
(`stop-first: stopping the running fleet`) as long as no mainnet sleeve is on; with a
mainnet sleeve on, or with `--no-stop-first`, it refuses instead and names the units to
quiesce. It checks out the exact commit from `$REMOTE/$BRANCH`, installs
`requirements.lock` with `--no-deps`, installs the unit manifest, disables every project
unit, removes unknown `liquidity-migration-*` units, resolves the hedge timer, writes
`/etc/liquidity-migration/sleeves.resolved.env` and normalizes the demo runtime trees.
Prints `install-ok commit=<sha> units_started=0`. It runs no linter and no test suite —
CI on `main` is the test gate, not the stopped window on the host.

**activate** reads `/etc/liquidity-migration/profile` (defaulting to `operational` when
absent), checks demo-key order permission, validates the hedge model prior when the hedge
timer is on, starts owners before producers, seeds residual momentum and enables the RMOM
timer when a CONTINUOUS sleeve is on, enables the liveness timer, then verifies. It
auto-stops on the same terms as `install`.

**staged** is `install`, the profile marker, and `activate` in one command, so it needs
`--profile operational`. `install` alone does not write the marker.

**verify** (`ops.sh status`) is the read-only report. It asserts owners, producers and
timers match the profile and resolved toggles; no failed oneshot; every installed unit file
byte-identical to the checkout's manifest with no drop-ins. It collects **every** mismatch
rather than dying on the first, prints a `verify-units unit|expected|active|enabled` table,
then the `verify-mismatch` lines. The demo order-permission probe and the hedge model prior
are reported here and gate nothing (`verify-warn ...`); both are still fatal in `activate`.
With no explicit `EXPECTED_COMMIT`, a host on a different commit is reported as
`verify-drift installed=... expected=...` rather than failed. The mainnet half is
conditional on the resolved toggles: with both mainnet sleeves off, the mainnet owner, both
mainnet producers and the mainnet liveness timer must all be inactive and disabled; with
either on, the funded fleet is asserted up exactly like the others. A clean run prints
`verify-ok` with commit, profile and both mainnet toggles.

## Guarded rollout

```bash
scripts/ops.sh deploy rollout --profile operational
```

`--profile operational` is required and lands in the profile marker just
before activation. Phases, each printing start/ok/failed with elapsed seconds: prefetch
the target and confirm it is on `$REMOTE/$BRANCH`; verify the current topology;
flat-account check (no venue position, no working order, zero aggregate target, read from
the journal and from Bybit directly); stop producers, timers and watchdogs, recheck
flatness against the exact post-producer journal head, stop owners, recheck stopped;
stopped install; record the profile; activate and verify. Failure before the install phase
restores the previous topology, and from the install phase on every managed unit is left
stopped.

**The flat-account proof is advisory on a fleet with no mainnet sleeve on.** Residual demo
exposure prints `rollout-flat-warn ...` and the rollout continues, which is what makes
rollout and its rollback usable on demo at all. Pass `--require-flat`, or turn any mainnet
sleeve on, and it is a hard gate again: a non-flat or unreadable account then fails at the
first flat check with the fleet untouched.

Demo instrument rules carry a 7-day age limit and a rollout past half of it re-probes, so
freshness is a side effect of ordinary deployment rather than an operator deadline. Force
the re-probe with `--refresh-demo-rules` on `install` or `staged` (the
`ROLLOUT_REFRESH_STALE_DEMO_RULES=1` environment variable does the same). The probe places
and cancels bounded PostOnly demo orders, only after the stopped flat checks pass.
[`demo_rule_probe.py`](../liquidity_migration/venue/demo_rule_probe.py) exists because the
Bybit demo realm rejects orders its own `minNotionalValue` accepts — the real per-symbol
boundary is measured, not readable from the instrument spec.

The GitHub Actions workflow dispatches four of the seven modes — `rollout`, `install`,
`activate`, `verify` — and passes `--profile` on rollout. A push runs CI only. The two
mainnet modes are deliberately absent from it: arming a funded account is the owner's own
act at a shell, not a button in CI. All dispatches share one repository-wide VPS
concurrency group whatever Git ref they select.

Two details worth knowing when a deploy behaves oddly. Deploy Git commands inherit no
caller `GIT_*` variables, user or system Git configuration, replacement objects, external
index, or hooks — commit selection is insulated from ordinary Git-environment drift (not
from a compromised host). And the stopped window between `install` and `activate` is where
host-side environment, roots, candidate universe, rules, risk policy and sleeve toggles are
meant to be edited.

## Real money

CARRY and LONG on one funded Bybit account, each holding a private share of an
envelope scaled to observed wallet equity. As of today `REAL_MONEY` is unset, no
mainnet credential exists, and no mainnet unit has started.

**The single arming switch is `REAL_MONEY=true` in
`/etc/liquidity-migration/bybit-mainnet.env`** — the same root-owned `0600` file
the live API key goes into, edited on the VPS by the owner's own hand. There is
no repo toggle, so a git commit can never arm. When the switch is armed, a plain
`activate` or `rollout` creates the mainnet state roots, requires `real-money
preflight` to pass, then starts the mainnet owner, both producers, and the
liveness timer. Which sleeves trade, and at what share, is the installed risk
profile's decision.

**stop-mainnet** (`scripts/ops.sh deploy stop-mainnet`) disables and stops the
mainnet timer, watchdog, both producers and the owner, and fails if any
survives. It stops publication only — exposure is unchanged, so flatten. While
`REAL_MONEY` stays armed, `verify` fails and the next `activate` or `rollout`
restarts the fleet; set `REAL_MONEY=false` to make a stop stick, then
`scripts/ops.sh flatten --execute --environment mainnet --reason ...` to close
the book.

### The envelope

No money amount binds anything. `capital_reference_usdt` in
[`configs/operational.mainnet.json`](../configs/operational.mainnet.json) is only
a scale: the runtime reference tracks observed wallet equity, and every cap in
the profile is a ratio of it
([`equity_anchored_envelope.py`](../liquidity_migration/policy/equity_anchored_envelope.py)).
Equity down rescales the caps immediately; equity up waits for a move larger
than the dead band; missing or stale equity holds the current reference, and
below the floor it clamps rather than collapsing. `account_risk.sleeve_limits`
partitions the account gross and margin caps into per-sleeve shares; the kernel
holds each sleeve to its share on every exposure-increasing batch and refuses a
sleeve the partition does not name. Risk-reducing batches bypass every cap, so
exits are always possible. A dedicated subaccount would put the ceiling at the
venue instead of in software; declined, so these caps are what hold size.

### Dials

One file: [`deploy/bybit-mainnet.env.template`](../deploy/bybit-mainnet.env.template)
→ `/etc/liquidity-migration/bybit-mainnet.env`. Every dial is optional (omitting
one takes the committed default) and every dial is a ratio except the floor.

| Dial | Default | Meaning |
| --- | --- | --- |
| `RM_EQUITY_FRACTION` | 1.0 | Fraction of the wallet the envelope scales to. Cannot exceed 1. |
| `RM_EQUITY_FLOOR_USDT` / `RM_EXPAND_DEAD_BAND_FRACTION` | 100.0 / 0.05 | Reference floor (the one dial that is an amount) and the expansion-only dead band, in `[0, 1)`. |
| `RM_MAX_LEVERAGE` / `RM_ENTRY_LEVERAGE` | 2.0 / 2.0 | Hard ceiling — the renderer refuses above 2.0 — and what producers request. Entry ≤ max. |
| `RM_ACCOUNT_GROSS_MULTIPLE` | 2.0 | Gross ceiling ×reference. Bounded by leverage and by `entry_leverage × initial_margin_fraction`. |
| `RM_SYMBOL_NOTIONAL_FRACTION` | 0.5 | Largest single-symbol position. |
| `RM_INITIAL_MARGIN_FRACTION` | 1.0 | Margin ceiling. Cannot exceed 1. |
| `RM_DAILY_LOSS_FRACTION` | 0.1 | Daily realised-loss halt. Trips a flatten. |
| `RM_CARRY_GROSS_SHARE` / `RM_LONG_GROSS_SHARE` | 0.55 / 0.40 | Per-sleeve shares of the gross and margin caps. With the 0.01 retired-CONTINUOUS token share they must sum ≤ 1. |
| `RM_CARRY_STOP_LOSS_FRACTION` | 0.35 | Venue-native stop distance, armed with the entry. |
| `RM_CARRY_NOTIONAL_MULTIPLIER` / `RM_LONG_NOTIONAL_MULTIPLIER` | 1.0 / 0.4 | Per-sleeve sizing. |

Plus `RM_{CARRY,LONG}_MAX_NEW_ENTRIES_PER_CYCLE` (10 / 5, positive integers) and
`RM_LONG_MAX_PROJECTED_INITIAL_MARGIN_PCT_EQUITY` (0.5).
`scripts/ops.sh real-money render-profile` turns all of them into the profile the
kernel enforces; a dial set that cannot produce a loadable profile is refused
there, naming the dial to move, instead of at start-up over a funded account.

### Arming (owner-executed)

Run `scripts/ops.sh real-money preflight` at any point to see which of these is
still outstanding (`LOCAL=1` runs it against this checkout instead of the VPS).
It reads only, reports a credential by name and never by value, takes `--json`,
and exits 1 while anything is outstanding.

1. **Confirm the account is flat by hand.** No manual position, no open order.
   The owner's startup check and the reconciler see USDT-settled linear only, so
   anything else on the UID stays invisible to both — see *Still unproven*.
2. **Create the API key** on the funded account: contract trading only,
   **withdrawal disabled**, IP-allowlisted to the VPS.
3. **Fill in the credential file.** Copy
   [`deploy/bybit-mainnet.env.template`](../deploy/bybit-mainnet.env.template) to
   `/etc/liquidity-migration/bybit-mainnet.env`, root-owned `0600`, edited on the
   VPS by hand — the key and secret never enter an agent session. Paste into
   `BYBIT_REAL_API_KEY` / `BYBIT_REAL_API_SECRET` — deliberately different
   variables from the demo pair — and set the dials.
4. **Copy the route file.**
   [`deploy/account-execution-mainnet.env.template`](../deploy/account-execution-mainnet.env.template)
   to `/etc/liquidity-migration/account-execution-mainnet.env`, root-owned `0600`.
   Its roots are disjoint from demo.
5. **Create the state roots**: `scripts/ops.sh real-money create-state-roots
   --execute` (dry run without `--execute`). It refuses a relative root, a root
   that is not a directory, and any root inside a directory the demo owner
   declares.
6. **Render the profile** (`D=/etc/liquidity-migration/account-execution-mainnet`):
   `scripts/ops.sh real-money render-profile --execute --output $D/risk-policy.json`.
7. **Freeze the inputs** — universe first, then rules against it:
   `scripts/maintain/freeze_account_candidate_universe.py --realm mainnet` and
   `scripts/maintain/freeze_venue_instrument_rules.py --realm mainnet`. Rules
   come from the read-only `get_instruments_info` endpoint; never run the demo
   order probe against mainnet (it places live orders and refuses any realm but
   demo by name).
8. **Flip the switch**: `REAL_MONEY=true` in the credential file, by your own
   hand. This is the whole arming decision.
9. **Start the fleet**, at Tier 1 size: `scripts/ops.sh deploy activate`.

Changing any dial afterwards means re-rendering the profile and reinstalling.
Preflight verifies: both env files exist as strict root-owned `0600` `KEY=value`;
credentials present with both demo variables absent; `REAL_MONEY` a recognised
value (an unrecognised value fails rather than being guessed); the Telegram pair
set; the dials parse, render, and load; all 12 route keys declared with
`ACCOUNT_VENUE_REALM=mainnet`; state roots and frozen artifacts exist; and the
installed profile is byte-identical to the render of the current dials — the
likeliest arming mistake is editing a dial and forgetting to re-render.

### Capital controls in force

Absolute pre-trade caps (component gross, account gross, initial margin,
available margin, leverage) rejected atomically inside the journal transaction
and the per-sleeve partition: [`account_kernel.py`](../liquidity_migration/account/account_kernel.py).
Caps rescale with observed equity: [`equity_anchored_envelope.py`](../liquidity_migration/policy/equity_anchored_envelope.py).
Daily loss halt → `run_safety_flat_once`: [`account_loss_guard.py`](../liquidity_migration/policy/account_loss_guard.py).
Venue-native stop armed in the same `place_order` call and read back after
create: [`venue_protection.py`](../liquidity_migration/venue/venue_protection.py).
One owner process per account and journal ↔ venue reconciliation:
[`account_owner_lease.py`](../liquidity_migration/account/account_owner_lease.py),
[`account_reconcile.py`](../liquidity_migration/venue/account_reconcile.py).
An independent watchdog (`liquidity-migration-mainnet-liveness.timer`) pages
every 3 minutes holding no credential and no ordering edge. The mainnet client
refuses to construct while `REAL_MONEY` is unset, and producers get no
credentials and no arming switch in any realm — order authority is the account
owner's alone.

### Still unproven

Every step above exists and is tested; none of it has run against a funded
account. The venue behaviour the code asserts — the post-create stop assertion,
the reconciler, the wedged-command path — is untried against a mainnet key.
Known hazards, kept verbatim from the pre-arming review:

- **The ownership and position reads are realm-aware but narrow, and fail
  open.** Both ask Bybit for `category=linear, settleCoin=USDT` only. A
  USDC-settled, inverse, spot or options order *or position* on the same UID is
  invisible: startup passes, the reconciler reports `healthy` with that exposure
  omitted — while `totalEquity` does count it, so the caps and the daily loss
  halt size against exposure the owner cannot see or close. Keep the UID to
  USDT-linear by hand. Bybit's own liquidation and ADL orders carry no kernel
  `orderLinkId`, so they classify unowned — that blocks the owner's own
  reduce-only close on the symbol being liquidated. Hedge mode is unsupported
  and shows up as `dual_side_position_not_supported`.
- **Preflight never reads the wallet.** The first authenticated wallet read
  happens at owner bootstrap, so a UNIFIED row that blanks account-wide equity
  fields, a non-UTA account, or a hedge-mode account surfaces as a
  `Restart=always` loop rather than a preflight line.
- **Wallet equity counts non-USDT collateral.** `totalEquity` is an account-wide
  USD aggregate, so on a mixed-collateral account the envelope scales off assets
  the strategy cannot post as USDT margin, and a collateral drawdown alone can
  rescale the caps or trip the daily loss halt. `RM_EQUITY_FRACTION=1.0` is only
  right on a USDT-only account.
- **Three funding shapes stop the owner permanently, outside the degrade
  path**: a linear settlement row in any currency but USDT; more than ~357
  settlements/day (the 2,500-row epoch-replay page bound); and, off demo, a
  settlement row carrying nonzero `cashFlow`. Each is a `Restart=always` crash
  loop with the book unmanaged, permanent until dealt with by hand. Demo still
  *books* a `cashFlow`-bearing settlement (double-count latent, pinned by test);
  whether demo should refuse too is an open owner decision.
- **Nothing refreshes the mainnet candidate universe** — the automatic refreeze
  is demo-only; re-freeze by hand to admit a new listing.
- **The watchdog's mainnet scope is unexercised** — thresholds chosen, not
  measured. Watch it during Tier 1 rather than relying on it.
- **Known rough edges.** Reconcile staleness floors (30 s funding / 15 s
  positions) and the owner's 4 s health-age bound may trip on mainnet latency; a
  stale protection sync raises exactly like an absent stop; release-to-pending
  retries without a ceiling; `account_gross` comes from target quantities, not
  venue position value; margin tiers unmodelled.

### Ramp

The demo record behind Tier 1 — real funding, simulated fills, and the exit-depth
measurements — is in [`research/carry_hold.md`](research/carry_hold.md) and
[`research/research_findings.md`](research/research_findings.md). Tier 1 buys
real fills, not returns. Any breach drops to 0, not one tier down.

| Tier | Capital | Gate to the next tier |
| --- | --- | --- |
| 1 | venue minimums on 3 names | 5 days: no unexplained journal/venue mismatches, no protection-stale events, no resize churn, realised cost within 1.5× the 15.56bp model |
| 2 | 10% of ceiling | 10 more days clean, plus one funding-regime turn survived |
| 3 | 50% | 20 more days clean, plus one ≥5% drawdown handled unattended |
| 4 | 100% | 30 more days clean |

## Flatten

```bash
scripts/ops.sh flatten --environment demo --reason "why"            # reads only
scripts/ops.sh flatten --execute --environment demo --reason "why"  # publishes
```

Flatten takes one account to zero exposure through its own owner. It publishes a zero
replacement target for every component that still holds exposure, then watches the journal
until the owner has converged. It places no order itself: every close is an ordinary
owner-side reduce-only command, so risk accounting, protection cleanup and the journal read
exactly as they do for a strategy exit.

Zero targets are strictly risk-reducing, which is why this works when nothing else does —
the kernel exempts a reducing batch from the capital, leverage, freshness and partition
checks that would otherwise refuse an order on a degraded account, and retries a reduction
without limit. A tripped loss guard does not block an exit.

`--environment` is named explicitly and has no default; it accepts `demo` or
`mainnet`. `--symbol` and `--sleeve` narrow the plan, but a narrowed flatten will not
satisfy a rollout, which wants the whole account flat.

**Stop the producing sleeve first.** Flatten manages no units, so a producer left running
can publish a new nonzero target while it is converging. It detects that and says so rather
than fighting it.

Terminal states, which are also the exit codes:

| Status | Exit | Means |
| --- | --- | --- |
| `already_flat`, `flat`, `planned` | 0 | Nothing to do, converged, or a dry run |
| `dust_limited` | 4 | Residual is below the venue's minimum order size, so no admissible order can express it. As flat as the venue allows; read from the kernel's own `below_min_qty` rejection |
| `timed_out` | 5 | Did not converge in `--wait-seconds`. The detail names what is still standing, including any target a producer republished |
| `publication_failed` | 6 | One or more zero targets did not reach the inbox |

Exposure with no component owner needs no target: owner convergence drives an orphan to
flat on its own. Flatten reports them and waits.

## Profiles and sleeves

| Profile | Runs |
| --- | --- |
| `operational` | The demo owner, the demo producers its toggles allow, hedge/RMOM timers, liveness. The only profile; `demo-operational` is rejected with a message naming its retirement, and a host marker still reading it self-heals on the next rollout. |

[`deploy/sleeves.env`](../deploy/sleeves.env) is the repository ceiling; the host file
`/etc/liquidity-migration/sleeves.env` may only narrow `on` to `off`.

| Toggle | Units it gates |
| --- | --- |
| `LONG_SLEEVE` | `bybit-long-demo` |
| `CARRY_SLEEVE` | `bybit-carry-demo` |
| `CONTINUOUS_SLEEVE` | `bybit-continuous-demo`; forces the hedge timer on |

Which are on right now is in the file itself, not here. The mainnet units have
no sleeve toggle: `REAL_MONEY=true` in the host's `bybit-mainnet.env` is the
single arming switch (see *Real money* above), and it brings up the mainnet
owner, both producers, and the liveness timer together.

The retired paper toggles (`CONTINUOUS_PAPER_SLEEVE`, `CARRY_PAPER_SLEEVE`,
`PAPER_TARGET_MIRROR`) are ignored with a warning if a stale host override still
carries them.

Turning a sleeve off stops new targets; it does not flatten an existing target or venue
position — the last targets stay standing in the journal, which is why a sleeve-off fleet
still fails `rollout`'s flat proof. Turn the sleeve off, then flatten. Each unit names only
`UNIT:ENTRYPOINT`, and
[`run_authorized_runtime.sh`](../scripts/run_authorized_runtime.sh) maps that pair to one
complete command line and execs it; callers cannot append argv.

Sizing lives in [`configs/operational.demo.json`](../configs/operational.demo.json): edit the
repository copy, never the installed `/etc` copy, then reinstall. `pytest -q
tests/policy/test_operational_profile.py` runs the loader, which rejects unknown keys, non-finite
values, producer leverage above the account maximum and envelopes that cannot fit the owner
caps at `capital_reference_usdt`.

## Ledger reset

```bash
scripts/ops.sh reset --sleeves all                       # preview (default)
scripts/ops.sh reset --execute --leave-stopped --sleeves all --label planned-reset
```

Also `--sleeves long|continuous|carry|all`, `--archive-dir DIR` (default `data/_archive`),
`--include-reports`, `--include-caches`, `--settle-seconds N`, `--env-file`,
`--account-env-file`.

`--execute` refuses unless the demo account is already flat with no open orders, the
maintenance lock is free, mainnet configuration is absent, every managed unit reports
`inactive`, and every submit-armed unit loads the same credential file. It never cancels an
order or closes a position.
[`reset_path_safety.py`](../liquidity_migration/ops/reset_path_safety.py) validates every target
path before anything is deleted, and both account-owner leases are held across the
destructive boundary. Journals, inboxes and captures are archived with a SHA-256 sidecar
re-checked immediately before deletion; configs, lock inodes, `residual_momentum.parquet`
and root-level market data survive. After the first removal there is no rollback — a failure
leaves the units stopped and the archive as the only recovery source.

## When something goes wrong

Standing rules first: preserve the journal, unit state and logs, and diagnose from the
exact installed commit. Do not hand-start a partial fleet, edit an installed `ExecStart`,
or mutate state to get a green result. Do not cancel or close by hand on the venue while an
owner is running — the journal becomes the thing that disagrees with the account; use
`ops.sh flatten` instead. `phase-failed name=<phase> ... status=N` names a failing deploy
step.

**A command is wedged.** On demo, nothing. The owner's reconciler probes any command stuck
past the wedge bound (300 s) on its own ~2 s pass and terminalizes it on venue evidence, so
it clears within seconds of going wedged; a live order or unreduced fills always refuse
that transition. A wedge that survives the automatic pass appears in account health as a
`wedged_command:...` mismatch and pages through the ordinary health alerting. On mainnet
the reconciler only classifies — the transition stays an operator act, and until it is done
the wedge blocks new entries while same-symbol exits keep flowing:

```bash
scripts/ops.sh wedged-command report
scripts/ops.sh wedged-command probe --command-id <ID>
scripts/ops.sh wedged-command resolve --all --operator "$USER" --reason "why"
```

`resolve` also takes `--command-id <ID>` for one command and `--wedge-after-seconds N` to
change the age bound. `--operator` and `--reason` are optional and are recorded in the
journal event. A command the journal proves was never submitted needs no
`--authorize-absent-order`; that flag is only for one that *was* attempted and now shows
nothing at the venue, where the absence may be visibility lag.

**A unit is down.**

```bash
scripts/ops.sh units                       # what is running
scripts/ops.sh logs bybit-carry-demo 200   # why it stopped
scripts/ops.sh restart bybit-carry-demo
```

One unit failing no longer takes the fleet with it. Producers only *want* the owner, so a
dead owner leaves them up: they plan exits and block entries each cycle until it returns.

**A request is sitting in the inbox.** It will not sit there forever. A request that fails
continuously for ten minutes retires to `failed/` carrying the error that retired it, and
the producing sleeve's next cycle publishes a fresh one. Files in `failed/` are evidence to
read, not a queue to drain by hand.

**The fleet is wrong after a bad deploy.** Re-deploy the good commit —
`scripts/ops.sh deploy staged --profile operational`, with `EXPECTED_COMMIT` set to that
commit. Quiescing is not your job: `staged` stops the running fleet itself. A failed
rollout leaves everything stopped, and the same `staged` finishes it; add
`--refresh-demo-rules` if it died inside demo-rule maintenance.

Host replacement, SSH or deploy-key recovery, and expected-commit drift: the `vps-migrate`
skill.

## Rules

- Local gates, none of which touch the VPS: `scripts/dev.sh doctor`, `scripts/dev.sh check`,
  `.venv/bin/python -m pytest -q`. CI on `main` runs the same gates; the deploy does not.
- Mainnet arming: the *Real money* section above. Agent working rules:
  [`AGENTS.md`](../AGENTS.md).
