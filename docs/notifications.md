# Notifications and alerting

Two message senders, two chat lines, plus one listener. The account owner reports what the book
did; the liveness watchdog reports that the fleet is still running. They watch each other — the
watchdog alerts when the owner's digest stops arriving, and the owner is the only thing that
reports a fill. The listener is the control panel (§Owner control buttons): the one component that
reads the chat instead of writing to it.

**The main line** (`TELEGRAM_CHAT_ID`) carries only the book's story: the hourly digest, fills,
closes, stop events, loss warnings, entry blocks. **The alerts line** (`TELEGRAM_ALERT_CHAT_ID`)
carries watchdog pages and their cleared notes — the "something needs fixing" traffic. Each alert
is a plain one-line headline plus a stable `ref <key>`; paste the whole message to Claude to hand
the problem over. The full technical detail is never in the chat — it stays in the watchdog's
journal (`journalctl -u liquidity-migration-demo-liveness`).

An empty `TELEGRAM_ALERT_CHAT_ID` sends alerts to the main chat instead — nothing goes silent
while the second chat is not set up. To split the lines: create a Telegram group, add the bot to
it, send any message there, then read the group's chat id from the bot's `getUpdates` API and put
it in the host env file as `TELEGRAM_ALERT_CHAT_ID`.

All senders read `TELEGRAM_BOT_TOKEN` plus the chat ids
([`telegram.py`](../liquidity_migration/ops/telegram.py)). Missing token/chat is not an error: the
send returns `False` and the caller decides. A unit opts in with `TELEGRAM_ENABLED=1`.

| Unit | Telegram | Sends | Line |
| --- | --- | --- | --- |
| `account-execution` (demo owner) | on | digest + event notices | main |
| `account-execution-mainnet` | on | digest + event notices, funded book | main |
| `demo-liveness` | on | watchdog alerts, demo scope | alerts |
| `mainnet-liveness` | on | watchdog alerts, mainnet scope | alerts |
| `telegram-controls` | on | control panel + action results; **also listens** | main |
| every producer, hedge, rmom | off or unset | nothing | — |

Producers publish targets and never notify. A producer that goes quiet is the watchdog's problem,
not its own.

## Owner control buttons

[`telegram_controls.py`](../liquidity_migration/ops/telegram_controls.py), an always-on daemon
(`liquidity-migration-telegram-controls.service`) and the only consumer of the bot's incoming
updates — nothing else may poll `getUpdates` on this token. Send `/controls` in the main chat to
get the buttons; `/status` for a plain fleet summary.

- **⏸ Pause trading** — stops new decisions. Writes the sleeve toggles off in the host override
  (`/etc/liquidity-migration/sleeves.env`, keeping a verbatim copy of what was there), regenerates
  the resolved toggles with the deploy's own library, and stops the producer units. The account
  owner, its protections, and the watchdog keep running; open positions stay open. Because this is
  the designed host narrowing, the pause survives reboots **and deploys**, and the watchdog reads
  it as deliberate rather than paging "producer down".
- **▶️ Resume trading** — restores the saved override verbatim (a manual narrowing you had made by
  hand survives the round trip), re-resolves, and starts whichever producers resolve on.
- **🚨 Close ALL positions** — two taps: the button, then a confirmation that expires after 120
  seconds. Pauses first so a producer cannot republish, then market-closes the whole book through
  the account owner's own flatten path (every close is an ordinary reduce-only command; crumbs
  below the venue minimum are reported, not retried forever). Trading stays paused afterwards
  until you press Resume.

Real-money rows appear only while the mainnet owner unit is active — i.e. after your own arming
act. Pausing mainnet stops its two producer units directly (mainnet has no sleeve toggles).

Who may press: only the configured main chat is read at all, and a press must come from the chat's
own private-chat owner. If the main chat is a group, set `TELEGRAM_CONTROL_USER_IDS` (comma-
separated numeric user ids) in the host env file — with no allow-list, every press in a group is
refused. Presses queued while the daemon was down are dropped at startup, so a stale button can
never fire late; if the bot did not react, press again.

## The owner's digest

[`account_notifications.py`](../liquidity_migration/ops/account_notifications.py), rendered from the
canonical account journal — never from a projection or a venue read.

- **Hourly summary** on the UTC hour boundary: open positions with side, quantity, price, open P&L
  and stop; realized P&L (with a short `(pending: …)` note when funding/fees are not final);
  account health; entry-block counts. When journal and venue disagree the summary shows both sides
  rather than picking one ("Exchange:" vs "Our records:").
- **Event notices** as they commit, for `FILL`, `PNL`, `PROTECTION`, and `RISK_DECISION`. Everything
  else is left to the hourly roll-up. Bookkeeping detail (component ids, accounting provenance) is
  logged to the owner's service journal, not sent to the chat.
- Position truth is five-valued — `healthy`, `settling`, `mismatch`, `stale`, `unavailable`. Only
  the first two count as healthy; `settling` means venue and journal disagree by less than a
  settlement window.

State lives at `<ACCOUNT_EXECUTION_ROOT>/account_notifications.json` (schema 3) and is committed
**only after every page delivers**, so a stalled `last_hour_bucket` is direct evidence the digest
never arrived — which is what the watchdog reads.

## The liveness watchdog

[`scripts/runtime/check_fleet_liveness.py`](../scripts/runtime/check_fleet_liveness.py), one oneshot per timer fire,
every 3 minutes — the first run lands one minute after the timer is enabled, so a
recovery is graded almost immediately. `--account-scope` selects `demo` or
`mainnet`; the mainnet scope runs only the mainnet owner and producers against roots disjoint
from demo. A unit restart opens a per-check startup grace (owner-health, capture,
and cycle-age checks all honor it), so routine restarts do not page.

It **always exits 0**. A watchdog that crash-loops is a watchdog that is off, so a failure to verify
degrades to an alert instead of a non-zero exit. The unit's `TimeoutStartSec=120` sits under the
3-minute timer so a hung run goes `failed` rather than silently never re-firing.

What it checks: systemd unit states — including a service that is enabled but not
active (the dependency-failure shape of the Aug 1–3 outage; debounced one interval,
then CRITICAL); account-owner health and readiness freshness; live-L2 capture
freshness; per-sleeve producer cycle age; the frozen demo-rule receipt's
remaining life; residual-momentum signal staleness; the committed hedge model prior; oneshot
run duration; free disk; and the owner's digest.

| Threshold | Default | Meaning |
| --- | --- | --- |
| `--max-cycle-age-min` | 10 | no producer cycle within this many minutes |
| `--max-account-health-age-min` | 1 | owner-health or reconciliation projection is older than this, and how stale the owner's last authenticated exchange read may be |
| `--max-account-capture-age-min` | 3 | canonical live L2 is older than this |
| `--max-ws-lag-hours` | 6 | WS kline feed lag warning |
| `--max-rmom-stale-days` | 2 | residual-momentum gate's newest day |
| `--max-oneshot-run-seconds` | 180 | a completed periodic oneshot ran longer than this |
| `--cooldown-min` | 30 | re-alert interval; **deployed as 60 for both demo and mainnet** |

### How an alert behaves

A new condition alerts immediately. A persisting one re-alerts at most once per cooldown. A cleared
one sends a one-line resolved note. An escalation from `WARNING` to `CRITICAL` **bypasses the
cooldown** — severity going up is new information.

An undelivered alert advances neither its cooldown nor its last-sent severity, so the next run
retries it, escalation intact. Cooldown state is saved after the sends for exactly this reason.

### The dead-man's switch

`--heartbeat-url` (or `LIVENESS_HEARTBEAT_URL`) is pinged on a healthy run — and only when there are
no CRITICAL alerts **and** every Telegram send this run delivered. A dead notification channel pages
externally instead of reading as all-quiet. An on-box watchdog cannot report that the box died, so
without a URL a total host loss is silent. **No URL is provisioned by default.**

## Operating it

- Silence is not health. Confirm the hourly digest is arriving; that is the only positive signal
  the owner is alive.
- Digest stopped, no watchdog alert → check `TELEGRAM_*` on the watchdog unit; both channels share
  the same credentials and a bad token silences both at once.
- Alert storm after a restart → the per-check startup grace should absorb it; if a
  slow bootstrap overlaps the cycle-age bound it resolves itself, and the resolved
  notes will say so.
- Mainnet pages come from the Telegram pair inside `/etc/liquidity-migration/bybit-mainnet.env`;
  the watchdog unit strips the API keys and `REAL_MONEY` straight back out, so it can page but holds
  no trading authority.
- Thresholds here were chosen against demo latency. They are unexercised on a funded account —
  watch them during Tier 1 rather than trusting them ([`operations.md`](operations.md) §Real money).
