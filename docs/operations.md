# Operations

[`scripts/ops.sh`](../scripts/ops.sh) is the operator router and
[`scripts/deploy_vps_live.sh`](../scripts/deploy_vps_live.sh) the deploy engine behind it; both act on
one VPS over SSH. `scripts/ops.sh help` is the current surface. The funded fleet — its envelope, arming
runbook, and what is still unproven — is *Real money* below. Deployed state:
[`STATE.md`](../STATE.md); dated history: [`CHANGELOG.md`](../CHANGELOG.md).

## Commands

| Command | Effect |
| --- | --- |
| `status` | Read-only topology verification (`deploy_vps_live.sh verify`). Prints a unit table and every mismatch it found. |
| `units` | List the fleet's units and timers. |
| `logs UNIT [LINES]` | One unit's journal, newest last (default 100 lines). |
| `restart` / `stop` / `start UNIT...` | Unit lifecycle, one or more units at a time. |
| `equity [ARGS]` | Descriptive equity curves. `--sleeves long,carry` (`carry` renders the deployed rule `configs/lane2_carry_hold_v6.json` — the file the v7 profile trades; the v7 pre-settle exit clock is not modeled — from the cross-venue panel, not a daemon replay), `--years N`. |
| `research-refresh {plan,run}` | Append-first data/features/backtest workflow. `plan` mutates nothing. |
| `reset [ARGS]` | Demo ledger reset. Preview unless `--execute`. |
| `flatten --environment ENV [ARGS]` | Take one account to zero exposure on the engine's own path. Reads only unless `--execute`. |
| `venue-accounting [ARGS]` | Reconcile the demo journal against Bybit executions, fees, closed P&L, funding, positions, open orders. Runs on the host with the host's credentials; `LOCAL=1` runs it against this checkout. Read-only. |
| `wedged-command [--environment demo\|mainnet] {report,probe,resolve}` | Read venue truth for an order command that can no longer progress; `resolve` writes one journal transition, never resends an order, and refuses while the venue still holds it. The wrapper owns the account root/id/realm and loads that owner's credentials on the host. Defaults to demo. |
| `real-money {preflight,render-profile,create-state-roots}` | Read-only arming report; profile render (`--execute --output PATH` writes one non-secret file); mainnet journal directories (dry-run unless `--execute`). Starts nothing. |
| `deploy MODE [ARGS]` | `MODE` is `install`, `activate`, `staged`, `rollout` or `stop-mainnet`. |
| `help` | Print the current surface and do nothing else. |

A `UNIT` that does not already start with `liquidity-migration-` gets the prefix, so `logs
bybit-carry-demo.service` reads `liquidity-migration-bybit-carry-demo.service`.

`SSH_TARGET` (`root@116.202.15.128`), `REPO_DIR` (`/opt/liquidity-migration`) and `PYTHON` override the
defaults; `LOCAL=1` runs `real-money` and `venue-accounting` against this checkout. `EXPECTED_COMMIT` is
optional: left unset it defaults to `$REMOTE/$BRANCH` (`origin/main`) and falls back to local `HEAD`,
printing which it chose. Set explicitly it must still be a full lowercase 40-character commit and is
still validated. Deploy also reads `BRANCH` (default `main`), `REPO_URL`, `REMOTE`, `SSH_OPTS`, and
`GITHUB_TOKEN` (falls back to `gh auth token`). Deploy, activate, verify and an executing reset share
`/run/liquidity-migration/maintenance.lock`; a collision fails before reading or mutating anything.

## Deployment

One command covers the ordinary case; the two-step form is there when you want to inspect the stopped
host between install and activation.

```bash
scripts/ops.sh deploy staged --profile operational   # install + profile + activate
scripts/ops.sh status
```

```bash
scripts/ops.sh deploy install     # fleet stays stopped
scripts/ops.sh deploy activate
```

**install** needs a clean remote checkout. A running fleet is **stopped automatically** (`stop-first:
stopping the running fleet`) as long as no mainnet sleeve is on; with a mainnet sleeve on, or with
`--no-stop-first`, it refuses instead and names the units to quiesce. It checks out the exact commit
from `$REMOTE/$BRANCH`, installs `requirements.lock` with `--no-deps`, installs the unit manifest,
disables every project unit, removes unknown `liquidity-migration-*` units, writes
`/etc/liquidity-migration/sleeves.resolved.env` and normalizes the demo runtime trees. Prints
`install-ok commit=<sha> units_started=0`. It runs no linter and no test suite — CI on `main` is the
test gate, not the stopped window on the host.

**activate** reads `/etc/liquidity-migration/profile` (defaulting to `operational` when absent), checks
demo-key order permission, starts the producers first, enables the liveness timer and the Telegram
control panel, and starts the engine last, then verifies. The engine is last on purpose: an engine
start that will not take is reported by the verification, not by aborting a fleet-up that has already
brought everything else up. It auto-stops on the same terms as `install`.

**staged** is `install`, the profile marker, and `activate` in one command, so it needs `--profile
operational`. `install` alone does not write the marker.

**The owner's one-click:** `scripts/deploy_everything.command` (double-click in Finder, or run it like
any script) is `deploy staged --profile operational --stop-first` with the target commit printed first
and a note when local main is ahead of GitHub. `--stop-first` stops the funded units too, and
activation starts them back whenever `REAL_MONEY` is armed. No prompts.

**verify** (`ops.sh status`) is the read-only report. It asserts owners, producers and timers match the
profile and resolved toggles; no failed oneshot; every installed unit file byte-identical to the
checkout's manifest with no drop-ins. It collects **every** mismatch rather than dying on the first,
prints a `verify-units unit|expected|active|enabled` table, then the `verify-mismatch` lines. The demo
order-permission probe is reported here and gates nothing (`verify-warn ...`); it is still fatal in
`activate`. With no explicit `EXPECTED_COMMIT`, a host on a different commit is reported as
`verify-drift installed=... expected=...` rather than failed. The mainnet half is conditional on the
resolved toggles: with both mainnet sleeves off, the mainnet engine unit, both mainnet producers and the
mainnet liveness timer must all be inactive and disabled; with either on, the funded fleet is asserted
up exactly like the others. A clean run prints `verify-ok` with commit, profile and both mainnet
toggles.

## Guarded rollout

```bash
scripts/ops.sh deploy rollout --profile operational
```

`--profile operational` is required and lands in the profile marker just before activation. Phases,
each printing start/ok/failed with elapsed seconds: prefetch the target and confirm it is on
`$REMOTE/$BRANCH`; verify the current topology; flat-account check (no venue position and no venue
order, read from Bybit directly, plus — while the fleet runs — the engine's heartbeat recent and
naming an empty holdings list); stop producers, timers and watchdogs, recheck flatness with the
engine still beating, stop the engines, recheck stopped on the venue alone; stopped install; record
the profile; activate and verify. Failure before the install phase restores the previous topology;
from the install phase on, every managed unit is left stopped.

**The flat-account proof is advisory on a fleet with no mainnet sleeve on.** Residual demo exposure
prints `rollout-flat-warn ...` and the rollout continues, which is what makes rollout and its rollback
usable on demo at all. Pass `--require-flat`, or turn any mainnet sleeve on, and it is a hard gate
again: a non-flat or unreadable account then fails at the first flat check with the fleet untouched.

Demo instrument rules carry a 7-day age limit and a rollout past half of it re-probes. Force the
re-probe with `--refresh-demo-rules` on `install` or `staged` (or
`ROLLOUT_REFRESH_STALE_DEMO_RULES=1`). The probe places and cancels bounded PostOnly demo orders, only
after the stopped flat checks pass (why it exists: [`architecture.md`](architecture.md), *Realms and
credentials*).

The GitHub Actions workflow dispatches four of the modes — `rollout`, `install`, `activate`, `verify` —
and passes `--profile` on rollout. A push runs CI only. `staged` and `stop-mainnet` are not exposed:
`install` then `activate` covers `staged` from CI, and arming or stopping a funded account is the
owner's own act at a shell, not a button in CI. All dispatches share one repository-wide VPS
concurrency group whatever Git ref they select.

Deploy Git commands inherit no caller `GIT_*` variables, user or system Git configuration, replacement
objects, external index, or hooks — commit selection is insulated from ordinary Git-environment drift
(not from a compromised host). The stopped window between `install` and `activate` is where host-side
environment, roots, candidate universe, rules, risk policy and sleeve toggles are meant to be edited.

## Real money

CARRY and LONG on one funded Bybit account, under one account-wide envelope scaled to observed
wallet equity. There is no per-sleeve share: either sleeve can spend the whole of it.

**The single arming switch is `REAL_MONEY=true` in `/etc/liquidity-migration/bybit-mainnet.env`** — the
same root-owned `0600` file the live API key goes into, edited on the VPS by the owner's own hand.
There is no repo toggle, so a git commit can never arm. When the switch is armed, a plain `activate` or
`rollout` creates the mainnet state roots, requires `real-money preflight` to pass, then starts the
mainnet engine (`liquidity-migration-engine-mainnet.service`), both producers, and the liveness timer.
Which sleeves trade, and at what size multiplier, is the installed risk profile's decision;
the caps themselves are account-wide.

**stop-mainnet** (`scripts/ops.sh deploy stop-mainnet`) disables and stops the mainnet timer, watchdog,
both producers and the mainnet engine unit, and fails if any survives. It stops publication only —
exposure is unchanged, so flatten. While `REAL_MONEY` stays armed, `verify` fails and the next `activate` or
`rollout` restarts the fleet; set `REAL_MONEY=false` to make a stop stick, then `scripts/ops.sh flatten
--execute --environment mainnet --reason ...` to close the book.

### The envelope

No money amount binds anything. `capital_reference_usdt` in
[`configs/operational.mainnet.json`](../configs/operational.mainnet.json) is only a scale: the runtime
reference tracks observed wallet equity, and every cap in the profile is a ratio of it
([`envelope.rs`](../engine/engine-risk/src/envelope.rs)). Equity
down rescales the caps immediately; equity up waits for a move larger than the dead band; missing or
stale equity holds the current reference, and below the floor it clamps rather than collapsing.
Every cap is account-wide: no sleeve holds a private share, so any one of them can spend the lot.
Risk-reducing batches bypass every cap, so exits are always possible. A
dedicated subaccount would put the ceiling at the venue instead of in software; declined, so these caps
are what hold size.

### Dials

Sizing is three dials the producers read out of their own environment, so each one has to
sit in a file that producer unit loads: `/etc/liquidity-migration/bybit-demo.env` on demo,
and `account-execution-mainnet.env` on the funded fleet — **not** `bybit-mainnet.env`,
which the funded producers never load because it holds the key. Each entry = the strategy's
base slot (at most 10% of equity) × its multiplier. Omit a line and the committed profile's
value applies; a malformed line refuses the producer's start rather than falling back.
Editing one takes a restart of that producer unit, not a profile re-render.

| Dial | Default | Meaning |
| --- | --- | --- |
| `CARRY_NOTIONAL_MULTIPLIER` | 3.0 | Each new carry name = at most 10% of equity × this, so 30% for a name at full weight; the depth, persistence, flow and whale terms only ever cut it. |
| `LONG_NOTIONAL_MULTIPLIER` | 3.0 | Each LONG entry = 10% of equity × this, before LONG's own vol/weekend scaling (up to ~1.9× on top). |
| `EXODUS_NOTIONAL_MULTIPLIER` | 3.0 | The exodus short's own multiplier; omit it and it inherits carry's. |
| `RM_CARRY_STOP_LOSS_FRACTION` | 0.35 | Venue-native disaster-stop distance, armed with the entry. |

The BOOK ceiling is separate and lives in `configs/operational.mainnet.json` (gross cap =
wallet × 5 at entry leverage 5, split between the sleeves): a book the multipliers build
past it is refused per entry by the engine's runtime admission, never resized. Very high
multipliers can also exceed some symbols' own venue leverage limits. A retired `RM_*`
variable left in an env file is refused by name;
`scripts/ops.sh real-money render-profile` re-renders the account document after a change
to it.

### Arming (owner-executed)

Two acts, both yours:

1. **Write the one file.** On the VPS, edit `/etc/liquidity-migration/bybit-mainnet.env` (start from
   [`deploy/bybit-mainnet.env.template`](../deploy/bybit-mainnet.env.template)): paste
   `BYBIT_REAL_API_KEY` / `BYBIT_REAL_API_SECRET` (contract trading only, **withdrawal disabled**,
   IP-allowlisted to the VPS), set `RM_CARRY_STOP_LOSS_FRACTION` if you want it off its default, and
   set `REAL_MONEY=true` — the whole arming decision, by your own hand. The live key never passes
   through an agent session. The three sizing dials go in `account-execution-mainnet.env` instead
   (§Dials).
2. **Start the fleet**: `scripts/ops.sh deploy --execute activate`.

Activation derives everything else before anything starts: the route env installs from the committed
template when absent (static, no secrets — a separate file so producer units never load the file
holding the key), file ownership and mode are normalized, a missing Telegram pair is copied from the
demo env (a funded book that cannot page is a hazard), the risk profile is re-rendered from the current
dials on every activation (so a dial edit can never drift from what the kernel enforces), the mainnet
candidate universe and instrument rules freeze themselves when absent, and the state roots are created.
Then preflight runs as the gate: any check it cannot satisfy stops the deploy with the item named, and
nothing mainnet starts.

`scripts/ops.sh real-money preflight` is yours to run any time as a read-only diagnostic (`LOCAL=1` for
this checkout, `--json` for tools; credentials are reported by name, never by value). It verifies: both
env files strict root-owned `0600` `KEY=value`; credentials present with both demo variables absent;
`REAL_MONEY` a recognised value (an unrecognised value fails rather than being guessed); the Telegram
pair set; the dials parse, render, and load; all 12 route keys declared with
`ACCOUNT_VENUE_REALM=mainnet`; state roots and frozen artifacts exist; and the installed profile
byte-identical to the render of the current dials.

Before flipping the switch, confirm the funded account is flat by hand — the owner's startup check and
the reconciler see USDT-settled linear only, so anything else on the account stays invisible to both
(*Still unproven*). Start small: the three `*_NOTIONAL_MULTIPLIER` dials above are the size controls,
and the envelope and the watchdog thresholds are unexercised on a funded account until Tier 1 runs.

### Capital controls in force

Absolute pre-trade caps (component gross, account gross, initial margin, available margin, leverage),
enforced in the engine's risk kernel before any order leaves:
[`kernel.rs`](../engine/engine-risk/src/kernel.rs). Caps rescale with observed
equity: [`envelope.rs`](../engine/engine-risk/src/envelope.rs).
There is no account-level daily loss ceiling — the owner's standing decision;
the per-position venue stop is the loss bound. Venue-native stop armed
in the same `place_order` call and read back after create:
[`working.rs`](../engine/engine-core/src/working.rs). One writer process per
account — the engine holds the account lease
([`account_owner_lease.py`](../liquidity_migration/account/account_owner_lease.py) defines the
protocol) — and journal ↔ venue reconciliation is the engine's own boot pass
([`reconcile.rs`](../engine/engine-core/src/reconcile.rs)). An independent watchdog
(`liquidity-migration-mainnet-liveness.timer`) pages every 3 minutes holding no credential and no
ordering edge. The mainnet client refuses to construct while `REAL_MONEY` is unset, and producers get
no credentials and no arming switch in any realm — order authority is the engine's alone.

### Still unproven

Every step above exists and is tested; the reconciler and wedged-command paths have run against the
funded account. These hazards remain open:

- **Ownership and position reads are narrow and fail open.** Both ask Bybit for `category=linear,
  settleCoin=USDT` only. A USDC-settled, inverse, spot or options order *or position* on the same UID
  is invisible: startup passes and the reconciler reports `healthy` with that exposure omitted, while
  `totalEquity` does count it — so the caps size against exposure the owner cannot
  see or close. Keep the UID to USDT-linear by hand.
- **One-way position mode only.** Hedge mode is unsupported and surfaces as
  `dual_side_position_not_supported`. Bybit's own liquidation and ADL orders carry no kernel
  `orderLinkId`, so they classify unowned — blocking the owner's own reduce-only close on the symbol
  being liquidated.
- **Preflight never reads the wallet.** The first authenticated wallet read is at owner bootstrap, so a
  UNIFIED row that blanks account-wide equity fields, a non-UTA account, or a hedge-mode account
  surfaces as a `Restart=always` loop rather than a preflight line.
- **Wallet equity counts non-USDT collateral.** `totalEquity` is an account-wide USD aggregate, so on a
  mixed-collateral account the envelope scales off assets the strategy cannot post as USDT margin, and
  a collateral drawdown alone can rescale the caps. Only right on a USDT-only account.
- **Three funding shapes stop the owner permanently, outside the degrade path**: a linear settlement
  row in any currency but USDT; more than ~357 settlements/day (the 2,500-row epoch-replay page bound);
  and, off demo, a settlement row carrying nonzero `cashFlow`. Each is a `Restart=always` crash loop
  with the book unmanaged, permanent until dealt with by hand. Demo still *books* a `cashFlow`-bearing
  settlement (double-count latent, pinned by test); whether demo should refuse too is an open owner
  decision.
- **Nothing refreshes the mainnet candidate universe** — the automatic refreeze is demo-only; re-freeze
  by hand to admit a new listing.
- **The watchdog's mainnet scope is unexercised** — thresholds chosen, not measured.
- **Known rough edges.** Reconcile staleness floors (30 s funding / 15 s positions) and the owner's 4 s
  health-age bound may trip on mainnet latency; a stale protection sync raises exactly like an absent
  stop; release-to-pending retries without a ceiling; `account_gross` comes from target quantities, not
  venue position value; margin tiers unmodelled.

### Ramp

The demo record behind Tier 1 — real funding, simulated fills, exit-depth measurements — is in
[`research/carry_hold.md`](research/carry_hold.md) and
[`research/research_findings.md`](research/research_findings.md). Tier 1 buys real fills, not returns.
Any breach drops to 0, not one tier down.

| Tier | Capital | Gate to the next tier |
| --- | --- | --- |
| 1 | venue minimums on 3 names | 5 days: no unexplained journal/venue mismatches, no protection-stale events, no resize churn, realised cost within 1.5× the 15.56bp model |
| 2 | 10% of ceiling | 10 more days clean, plus one funding-regime turn survived |
| 3 | 50% | 20 more days clean, plus one ≥5% drawdown handled unattended |
| 4 | 100% | 30 more days clean |

## Flatten

```bash
scripts/ops.sh flatten --environment demo --reason "why"            # dry run: reads and reports only
scripts/ops.sh flatten --environment demo --reason "why" --execute  # stops producers, writes zero books
```

Flatten takes one account to zero exposure on the engine's own path. It stops that realm's
producers (left running they would rewrite their books within a minute), then writes each sleeve's
target book as explicit zero rows naming everything the engine's heartbeat says is held — an
absolute book that names a symbol at zero is a decision to hold none of it, and the engine does the
closing. Explicit rows rather than an empty book, because an empty book only reaches the names the
plug already has in hand. It then watches the heartbeat until nothing is held or `--wait-seconds`
(default 300) runs out.

The exits it produces are reduce-only, and reduce-only orders pass every gate the engine has: the
boot latch exempts them, the risk kernel clamps a genuine exit to the position and returns before its
staleness check, and a book's expiry stops entries but never exits.

`--environment` is named explicitly and has no default; it accepts `demo` or `mainnet`. There is no
`--symbol` or `--sleeve`: the zero book reaches the whole account, and a narrower close is an
ordinary target-book decision, not this command.

The producers come back stopped, not disabled — a deploy's activate starts them again. To make the
close stick, set the sleeve off in `/etc/liquidity-migration/sleeves.env`.

There is no close from the phone: the Telegram buttons pause and resume the producers (the pause
survives reboots and deploys until resumed), and the close itself is this command
([`notifications.md`](notifications.md)).

Terminal states, which are also the exit codes:

| Status | Exit | Means |
| --- | --- | --- |
| `already_flat`, `planned`, `flat` | 0 | Nothing held, a dry run, or converged |
| usage | 2 | Bad or missing arguments; `--environment` has no default |
| refused: no heartbeat | 3 | No engine heartbeat where this realm's engine writes one |
| refused: holdings unknown | 4 | The engine does not publish what it holds; refusing is the only honest answer, because this command's whole job is to close what is there |
| refused: engine not running, or `timed_out` | 5 | Nothing would read the book — or the wait ran out with symbols still held (stderr names them) |

## Profiles and sleeves

| Profile | Runs |
| --- | --- |
| `operational` | The demo engine (`liquidity-migration-engine.service`), the demo producers its toggles allow, the Telegram control panel, liveness. The only profile; `demo-operational` is refused, and a host marker still carrying it self-heals on the next rollout. |

[`deploy/sleeves.env`](../deploy/sleeves.env) is the repository ceiling; the host file
`/etc/liquidity-migration/sleeves.env` may only narrow `on` to `off`.

| Toggle | Units it gates |
| --- | --- |
| `LONG_SLEEVE` | `bybit-long-demo` |
| `CARRY_SLEEVE` | `bybit-carry-demo` |

Which are on right now is in the file itself, not here.

The exodus short is not in that file. It rides the carry producer and is dialled by
`EXODUS_SHORT_PROFILE=v1`, set as an `Environment=` line on the demo carry unit
(`deploy/systemd/liquidity-migration-bybit-carry-demo.service`) and unset on mainnet, which
is what keeps it demo-only. Unsetting it does flatten: the next cycle publishes a cover for
every open exodus short rather than leaving the book standing — the one sleeve here whose
off switch drains it. The mainnet units have no sleeve toggle:
`REAL_MONEY=true` in the host's `bybit-mainnet.env` is the single arming switch (see *Real money*
above), and it brings up the mainnet engine, both producers, and the liveness timer together.

Retired toggles (`CONTINUOUS_SLEEVE`, `CONTINUOUS_HEDGE_TIMER`, `CONTINUOUS_PAPER_SLEEVE`,
`CARRY_PAPER_SLEEVE`, `PAPER_TARGET_MIRROR`) are ignored with a warning if a stale host override still
carries them.

Turning a sleeve off stops new targets; it does not flatten an existing target or venue position — the
last targets stay standing in the journal, which is why a sleeve-off fleet still fails `rollout`'s flat
proof. Turn the sleeve off, then flatten.

The three multipliers (§Dials) live in the fleet env file and **override** this file, so editing the
JSON alone changes no size while a dial is set. What
[`configs/operational.demo.json`](../configs/operational.demo.json) holds on its own is entry leverage,
the per-cycle entry caps and the account envelope: edit the repository copy, never the installed `/etc`
copy, then reinstall. `pytest -q
tests/policy/test_operational_profile.py` runs the loader, which rejects unknown keys, non-finite
values, producer leverage above the account maximum and envelopes that cannot fit the owner caps at
`capital_reference_usdt`.

## Ledger reset

```bash
scripts/ops.sh reset --sleeves all                       # preview (default)
scripts/ops.sh reset --execute --leave-stopped --sleeves all --label planned-reset
```

Also `--sleeves long|carry|all`, `--archive-dir DIR` (default `data/_archive`), `--include-reports`,
`--include-caches`, `--settle-seconds N`, `--env-file`, `--account-env-file`.

`--execute` refuses unless the demo account is already flat with no open orders, the maintenance lock is
free, mainnet configuration is absent, every managed unit reports `inactive`, and every submit-armed
unit loads the same credential file. It never cancels an order or closes a position.
[`reset_path_safety.py`](../liquidity_migration/ops/reset_path_safety.py) validates every target path
before anything is deleted, and the demo account-owner lease — the one the engine holds while it
runs — is held across the destructive boundary.
Journals, inboxes and captures are archived with a SHA-256 sidecar re-checked immediately before
deletion; configs, lock inodes, `residual_momentum.parquet` and root-level market data survive. After
the first removal there is no rollback — a failure leaves the units stopped and the archive as the only
recovery source.

## When something goes wrong

Standing rules first: preserve the journal, unit state and logs, and diagnose from the exact installed
commit. Do not hand-start a partial fleet, edit an installed `ExecStart`, or mutate state to get a green
result. Do not cancel or close by hand on the venue while an owner is running — the journal becomes the
thing that disagrees with the account; use `ops.sh flatten` instead. `phase-failed name=<phase> ...
status=N` names a failing deploy step.

**A command is wedged.** The engine reconciles its own orders: while it runs, its in-flight ledger
tracks every order it sent, and at boot it compares its log against what the venue actually holds
([`reconcile.rs`](../engine/engine-core/src/reconcile.rs)), repairs what it can — a missing stop is
put back — and refuses to open on exposure the log cannot account for. What nothing clears
automatically is an old journal command the engine never sent, or an order placed outside the engine.
For those, the operator reads venue truth and terminalizes the command by hand:

```bash
scripts/ops.sh wedged-command report
scripts/ops.sh wedged-command probe --command-id <ID>
scripts/ops.sh wedged-command resolve --all --operator "$USER" --reason "why"
```

Add `--environment mainnet` before the subcommand for the funded book; it defaults to demo. `resolve`
also takes `--command-id <ID>` for one command and `--wedge-after-seconds N` to change the age bound.
`--operator` and `--reason` are optional and are recorded in the journal event. A command the journal
proves was never submitted needs no `--authorize-absent-order`; that flag is only for one that *was*
attempted and now shows nothing at the venue, where the absence may be visibility lag.

**A unit is down.**

```bash
scripts/ops.sh units                       # what is running
scripts/ops.sh logs bybit-carry-demo 200   # why it stopped
scripts/ops.sh restart bybit-carry-demo
```

One unit failing does not take the fleet with it. No unit depends on another — each wants only
`network-online.target` — so a dead engine leaves the producers up, still writing their target books,
and the engine acts on the standing books when it comes back.

**A target book stopped refreshing.** Producers write target books
([`rules/engine_targets.py`](../liquidity_migration/rules/engine_targets.py)) and the engine reads
them directly, so a book that stops refreshing is a producer fault, not an execution one, and the
watchdog's cycle-age check pages it.

**The fleet is wrong after a bad deploy.** Re-deploy the good commit — `scripts/ops.sh deploy staged
--profile operational --stop-first`, with `EXPECTED_COMMIT` set to that commit. `--stop-first`
quiesces a still-running fleet; a plain `staged` refuses while any unit is active. A failed rollout
leaves everything stopped, and a plain `staged` finishes it; add `--refresh-demo-rules` if it died
inside demo-rule maintenance.

Host replacement, SSH or deploy-key recovery, and expected-commit drift: the `vps-migrate` skill.

## Rules

- Tests are local and are not an operator route: `scripts/dev.sh doctor`, `scripts/dev.sh test`,
  `scripts/dev.sh check`, `.venv/bin/python -m pytest -q`. None of them touch the VPS. CI on `main`
  runs the same gates; the deploy does not.
- Mainnet arming: *Real money* above. Agent working rules: [`AGENTS.md`](../AGENTS.md).
