# Notifications and alerting

Two chat lines, two reporters, one listener. The liveness watchdog pages when the fleet stops looking
healthy, and its view of the engine is the heartbeat file the engine rewrites every few seconds;
`trade-notify` diffs the target books and sends every sleeve's entries and exits to the owner's DM. The
engines send nothing themselves (both units strip the bot token). **Nothing reports fills** — the books
say what was asked for, not what happened — and there is no hourly digest (§The hourly digest). The
listener is the control panel (§Owner control buttons): the one component that reads the chat, and it
posts the panel and its action results.

**The main line** (`TELEGRAM_CHAT_ID`) carries the control panel and its action results. **The alerts
line** (`TELEGRAM_ALERT_CHAT_ID`) carries
watchdog pages and their cleared notes, one message per watchdog run however many checks tripped. Each
alert in it is a plain one-line headline plus a stable `ref <key>`; paste the whole message to Claude to
hand the problem over. The full technical detail stays in the watchdog's journal
(`journalctl -u liquidity-migration-demo-liveness`).

An empty `TELEGRAM_ALERT_CHAT_ID` sends alerts to the main chat instead — nothing goes silent while the
second chat is not set up. To split the lines: create a Telegram group, add the bot to it, send any
message there, then read the group's chat id from the bot's `getUpdates` API and put it in the host env
file as `TELEGRAM_ALERT_CHAT_ID`.

All senders read `TELEGRAM_BOT_TOKEN` plus the chat ids
([`telegram.py`](../liquidity_migration/ops/telegram.py)). Missing token/chat is not an error: the send
returns `False` and the caller decides. A unit opts in with `TELEGRAM_ENABLED=1`.

| Unit | Telegram | Sends | Line |
| --- | --- | --- | --- |
| `demo-liveness` | on | watchdog alerts, demo scope | alerts |
| `mainnet-liveness` | on | watchdog alerts, mainnet scope | alerts |
| `telegram-controls` | on | control panel + action results; **also listens** | main |
| `engine` / `engine-mainnet` | off — the unit strips the token | nothing; the engine's live signal is its heartbeat file, which the watchdog reads | — |
| `llm-ledger` | off — the unit reads no Telegram env | nothing; its judged candidates are read by the LONG producer, and the trades they become page as LONG entries/exits | — |
| `trade-notify` | on | every sleeve's entries and exits from the target books (carry, LONG, exodus), 5-minute diff | main |
| every producer | off or unset | nothing | — |

Producers publish targets and never notify; the phone's trading story comes
from the two notifier units above, reading the books the producers publish.
The main line is the owner's DM with the bot; the alerts line is the group,
and it belongs to the watchdog. A producer that goes quiet is the watchdog's
problem.

## Owner control buttons

[`telegram_controls.py`](../liquidity_migration/ops/telegram_controls.py), an always-on daemon
(`liquidity-migration-telegram-controls.service`) and the only consumer of the bot's incoming updates —
nothing else may poll `getUpdates` on this token. Send `/controls` in the main chat to get the buttons;
`/status` for a plain fleet summary.

- **⏸ Pause trading** — stops new decisions. Writes the sleeve toggles off in the host override
  (`/etc/liquidity-migration/sleeves.env`, keeping a verbatim copy of what was there), regenerates the
  resolved toggles with the deploy's own library, and stops the producer units. The engine, its
  stops, and the watchdog keep running; open positions stay open. Because this is the designed
  host narrowing, the pause survives reboots **and deploys**, and the watchdog reads it as deliberate
  rather than paging "producer down".
- **▶️ Resume trading** — restores the saved override verbatim (a manual narrowing you had made by hand
  survives the round trip), re-resolves, and starts whichever producers resolve on.
- **There is no close button.** Closing the book is `scripts/ops.sh flatten --execute`, an operator
  command on the engine's own path ([`operations.md`](operations.md) §Flatten) — the panel says so
  itself. Left off the panel on purpose: flatten stops the producers, and a button that quietly stops
  a sleeve is the kind of thing somebody presses to see what it does.

Real-money rows appear only while the mainnet engine unit is active — i.e. after your own arming act.
Pausing mainnet stops its two producer units directly (mainnet has no sleeve toggles).

Who may press: only the configured main chat is read at all, and a press must come from the chat's own
private-chat owner. If the main chat is a group, set `TELEGRAM_CONTROL_USER_IDS` (comma-separated
numeric user ids) in the host env file — with no allow-list, every press in a group is refused. Presses
queued while the daemon was down are dropped at startup, so a stale button can never fire late; if the
bot did not react, press again.

## The hourly digest

There is no hourly digest. Nothing writes
`<ACCOUNT_EXECUTION_ROOT>/account_notifications.json`, and the watchdog does not
read it. `--account-notification-state` defaults to empty; pointing it at a file
is opt-in, for whenever a digest comes back.

The engine's heartbeat covers liveness, not accounting: see the watchdog below.
A periodic position-and-P&L summary is not something the engine does yet, and
this section will say so until it does.

The rest of this section is the shape of what is missing — the specification for
whatever fills it.

- **Hourly summary** on the UTC hour boundary: open positions with side, quantity, price, open P&L and
  stop; realized P&L (with a short `(pending: …)` note when funding/fees are not final); account health;
  entry-block counts. When journal and venue disagree the summary shows both sides rather than picking
  one ("Exchange:" vs "Our records:").
- **Event notices** as they commit, for `FILL`, `PNL`, `PROTECTION`, and `RISK_DECISION`. Everything
  else is left to the hourly roll-up. Bookkeeping detail (component ids, accounting provenance) goes to
  the owner's service journal, not the chat.
- Position truth is five-valued — `healthy`, `settling`, `mismatch`, `stale`, `unavailable`. Only the
  first two count as healthy; `settling` means venue and journal disagree by less than a settlement
  window.
- State lives at `<ACCOUNT_EXECUTION_ROOT>/account_notifications.json` (schema 3) and is committed
  **only after every page delivers**, so a stalled `last_hour_bucket` is direct evidence a digest never
  arrived.

## The liveness watchdog

[`scripts/runtime/check_fleet_liveness.py`](../scripts/runtime/check_fleet_liveness.py), one oneshot per
timer fire, every 3 minutes — the first run lands one minute after the timer is enabled.
`--account-scope` selects `demo` or `mainnet`; the mainnet scope runs only the mainnet owner and
producers against roots disjoint from demo. A unit restart opens a per-check startup grace (owner-health,
capture and cycle-age checks all honor it), so routine restarts do not page.

It **always exits 0**. A watchdog that crash-loops is a watchdog that is off, so a failure to verify
degrades to an alert instead of a non-zero exit. The unit's `TimeoutStartSec=120` sits under the 3-minute
timer so a hung run goes `failed` rather than silently never re-firing.

What it checks: systemd unit states — including a service that is enabled but not active (debounced one
interval, then CRITICAL); readiness and live-L2 capture freshness; per-sleeve producer cycle age; the
frozen demo-rule receipt's remaining life; free disk; and the engine's own heartbeat file, including how
old the engine's reading of the account is. It reads no account journal and no digest state — see
[§What is not watched](#what-is-not-watched).

| Threshold | Default | Meaning |
| --- | --- | --- |
| `--max-cycle-age-min` | 10 | no producer cycle within this many minutes |
| `--max-account-health-age-min` | 1 | how far behind its own beat the engine's reading of the account may fall — **floored at 25 minutes**, so the 1-minute default never means one minute. The reading refreshes every few seconds; the bound is there to catch one that stopped arriving, not to grade venue latency |
| `--max-account-capture-age-min` | 3 | canonical live L2 is older than this |
| `--max-ws-lag-hours` | 6 | WS kline feed lag warning |
| `--max-engine-heartbeat-age-sec` | 60 | the engine's heartbeat is older than this (only read when one is configured) |
| `--cooldown-min` | 30 | re-alert interval; **deployed as 60 for both demo and mainnet** |

### The engine's heartbeat

`--engine-heartbeat-file` (or `LIVENESS_ENGINE_HEARTBEAT_FILE`) points at the small JSON file the engine
rewrites every few seconds. Unset, the file is never opened and nothing new can alert — which is right
for a host with no engine on it. **Both fleets provision it today**, from `engine.env` and
`engine-mainnet.env`. Given a path, four things page:

| Alert | Severity | Means |
| --- | --- | --- |
| `engine_heartbeat_stale` | CRITICAL | it stopped being written, so the engine is dead or stuck — or it is dated in the future, so its age cannot be judged at all |
| `engine_account_view_stale` | CRITICAL | the engine is alive and writing beats but has stopped hearing what the account holds, so its idea of the position is guesswork. Also fires if the reading is stamped *after* the beat carrying it, which means the arithmetic is wrong rather than the account being old |
| `engine_heartbeat_latched` | CRITICAL | the engine has latched itself out of opening new positions. It is alive, its heartbeat is healthy, every other check is green, and it opens nothing. Nothing else reports this — a person has to read the engine's log |
| `engine_heartbeat_unreadable` | CRITICAL | missing, empty, half-written, missing a field this check reads, or in a mode this checker does not know. The engine's state is then unknown |

**Two ages, two clocks, and only one of them can race.** How old the *beat* is has to be measured
against this box's clock, so the check reads the file and *then* asks the clock, in that order — the
other order lets the engine rewrite the file mid-run, and the beat comes out dated in the future.

How old the *account view* is needs no such care: the engine stamps both the beat and the reading it
carries, off one clock in one process, so `account_observed_wall_ts_ms` subtracted from `wall_ts_ms` is
the engine's own arithmetic and this box's clock never enters it.

An absent account reading is not a fault: the engine has not asked yet in its first moments, and paging
on that would make every boot an alert. It is reported as absent rather than filled in with a default, so
it can never read as fresh.

Every message names the mode, which the engine always writes as `live`. The checker still accepts the
older `shadow` too, because a beat written before a restart can outlive the run that wrote it, and an
unrecognised word is refused rather than guessed at. The account number, the lease path and the process
id are optional: an engine that has not yet reached the venue may carry none of them. Anything else the
engine writes is ignored.

### What is not watched

**Nothing watches venue and our records disagreeing.** Freshness is covered —
`engine_account_view_stale`, off the engine's heartbeat — but agreement is not: the engine reconciles
and publishes no mismatch. That is a real gap, not a tidy-up. `gather_account_health_alerts()` is kept
in the watchdog, uncalled, because it is the specification for whatever writes that evidence next: it
already knows the five-valued position truth and how to say which side disagrees. Reviving it needs the
engine to publish a reconciliation mismatch, which is a design question, not a wiring one.

Nothing watches a digest arriving either, deliberately: there is no digest.

`demo_rules_age` is a WARNING about stale evidence, and it names the deploy flag that refreshes it.
Nothing in the demo runtime path reads the receipt — `run_authorized_runtime.sh` has no rule gate,
neither producer script mentions one, and the engine takes instrument rules straight off the venue.
Mainnet's receipt genuinely does gate the funded owner, so that one is CRITICAL.

### How an alert behaves

A new condition alerts immediately. A persisting one re-alerts at most once per cooldown. A cleared one
is named in a resolved note. An escalation from `WARNING` to `CRITICAL` **bypasses the cooldown** —
severity going up is new information.

**One run is one message.** A fleet going down trips several checks at once and clears them all
together, so the run's alerts and its cleared keys go out as a single message — every key still carrying
its own severity and its own `ref`. The header takes the worst severity in the batch and counts the
alerts when there is more than one. A batch too long for Telegram is split, each part headed the same
way. Nothing is sent at all on a run with neither an alert nor a clear.

An undelivered message advances nothing: every cooldown stamp and last-sent severity in it is reverted
and every cleared key is marked for another attempt, so the next run retries the whole run, escalation
intact. Cooldown state is saved after the send for exactly this reason.

### The dead-man's switch

`--heartbeat-url` (or `LIVENESS_HEARTBEAT_URL`) is pinged on a healthy run — and only when there are no
CRITICAL alerts **and** every Telegram send this run delivered. A dead notification channel pages
externally instead of reading as all-quiet. An on-box watchdog cannot report that the box died, so
without a URL a total host loss is silent. **No URL is provisioned by default.**

## Operating it

- Silence is not health, and there is no periodic "still alive" message at all. The positive signal is
  the dead-man's switch above, which is **not provisioned** — so today silence means either a healthy
  fleet or a dead one, and nothing in the chat tells you which.
- Noise is not health either, and it is the more dangerous of the two. A channel that is entirely false
  positives is worse than a quiet one, because the real alert arrives into a habit of ignoring it. If a
  check cannot clear, it is broken — fix or retire it, do not let it run.
- No watchdog alert but something looks wrong → check `TELEGRAM_*` on the watchdog unit; both channels
  share the same credentials and a bad token silences both at once.
- Alert storm after a restart → the per-check startup grace should absorb most of it, and what is left
  arrives as one message per run rather than one per check; if a slow bootstrap overlaps the cycle-age
  bound it resolves itself, and the resolved note will say so.
- Mainnet pages come from the Telegram pair inside `/etc/liquidity-migration/bybit-mainnet.env`; the
  watchdog unit strips the API keys and `REAL_MONEY` straight back out, so it can page but holds no
  trading authority.
- Thresholds here were chosen against demo latency. They are unexercised on a funded account — watch
  them during Tier 1 rather than trusting them ([`operations.md`](operations.md) §Real money).
