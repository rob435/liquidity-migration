# Notifications and alerting

Two independent Telegram channels. The account owner reports what the book did; the liveness
watchdog reports that the fleet is still running. They watch each other — the watchdog alerts when
the owner's digest stops arriving, and the owner is the only thing that reports a fill.

Both read `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
([`telegram.py`](../liquidity_migration/ops/telegram.py)). Missing either is not an error: the send
returns `False` and the caller decides. A unit opts in with `TELEGRAM_ENABLED=1`.

| Unit | Telegram | Sends |
| --- | --- | --- |
| `account-execution` (demo owner) | on | digest + event notices |
| `account-paper-execution` | on | digest + event notices, paper book |
| `account-execution-mainnet` | on | digest + event notices, funded book |
| `demo-liveness` | on | watchdog alerts, demo/paper scope |
| `mainnet-liveness` | on | watchdog alerts, mainnet scope |
| every producer, hedge, rmom, target-mirror | off or unset | nothing |

Producers publish targets and never notify. A producer that goes quiet is the watchdog's problem,
not its own.

## The owner's digest

[`account_notifications.py`](../liquidity_migration/ops/account_notifications.py), rendered from the
canonical account journal — never from a projection or a venue read.

- **Hourly summary** on the UTC hour boundary: open positions with side, quantity, entry, mark and
  active stop; realized P&L; account health; position-truth status; entry-rejection counts. When
  journal and venue disagree the summary shows both sides rather than picking one.
- **Event notices** as they commit, for `FILL`, `PNL`, `PROTECTION`, and `RISK_DECISION`. Everything
  else is left to the hourly roll-up.
- Position truth is five-valued — `healthy`, `settling`, `mismatch`, `stale`, `unavailable`. Only
  the first two count as healthy; `settling` means venue and journal disagree by less than a
  settlement window.

State lives at `<ACCOUNT_EXECUTION_ROOT>/account_notifications.json` (schema 3) and is committed
**only after every page delivers**, so a stalled `last_hour_bucket` is direct evidence the digest
never arrived — which is what the watchdog reads.

## The liveness watchdog

[`scripts/runtime/check_fleet_liveness.py`](../scripts/runtime/check_fleet_liveness.py), one oneshot per timer fire,
every 3 minutes after a 10-minute cold-start grace. `--account-scope` selects `demo`, `demo-paper`,
or `mainnet`; the mainnet scope runs only the mainnet owner and producers against roots disjoint
from demo and paper.

It **always exits 0**. A watchdog that crash-loops is a watchdog that is off, so a failure to verify
degrades to an alert instead of a non-zero exit. The unit's `TimeoutStartSec=120` sits under the
3-minute timer so a hung run goes `failed` rather than silently never re-firing.

What it checks: systemd unit states; account-owner health and readiness freshness; live-L2 capture
freshness; per-sleeve producer cycle age; demo/paper book agreement; the frozen demo-rule receipt's
remaining life; residual-momentum signal staleness; the committed hedge model prior; oneshot
run duration; free disk; and the owner's digest.

| Threshold | Default | Meaning |
| --- | --- | --- |
| `--max-cycle-age-min` | 10 | no producer cycle within this many minutes |
| `--max-account-health-age-min` | 1 | owner-health or reconciliation projection is older than this |
| `--max-account-capture-age-min` | 3 | canonical live L2 is older than this |
| `--max-ws-lag-hours` | 6 | WS kline feed lag warning |
| `--max-rmom-stale-days` | 2 | residual-momentum gate's newest day |
| `--max-oneshot-run-seconds` | 180 | a completed periodic oneshot ran longer than this |
| `--cooldown-min` | 30 | re-alert interval; **deployed as 360 for demo, 60 for mainnet** |

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
- Alert storm after a restart → the cold-start grace is 10 minutes and cycle-age alerts fire at 10;
  a slow bootstrap can overlap. It resolves itself, and the resolved notes will say so.
- Mainnet pages come from the Telegram pair inside `/etc/liquidity-migration/bybit-mainnet.env`;
  the watchdog unit strips the API keys and `REAL_MONEY` straight back out, so it can page but holds
  no trading authority.
- Thresholds here were chosen against demo latency. They are unexercised on a funded account —
  watch them during Tier 1 rather than trusting them ([`real_money.md`](real_money.md)).
