# Observability Specification

What the fleet records about itself, where it lands, and how to see it.

---

## 1. Surfaces

| Surface | Unit | Cadence | Output | Authority |
| :--- | :--- | :--- | :--- | :--- |
| **Equity & recorder samples** | `liquidity-migration-equity-recorder.timer` | Every minute at :20 | JSONL under `/var/lib/liquidity-migration/equity`, plus one optional HTTP push | Read-only; loads no venue environment |
| **Engine heartbeat** | the engine itself | Every 5 s | `heartbeat.json`, overwritten | The engine's own statement of health |
| **Closed trades** | the engine itself | Per closed round trip | `trades.jsonl`, appended | Realized accounting authority |
| **Liveness alerts** | `*-liveness.timer` | Every 3 min | Telegram, plus the on-call agent | See [notifications.md](notifications.md) |

The three engine artifacts answer different questions and none substitutes for
another: the heartbeat says how the engine is **now**, `trades.jsonl` says what
was **realized**, and the equity samples say what the account was **worth over
time**, including the minutes it lost money without closing anything.

## 2. Sample Schema

`scripts/runtime/record_equity.py` reads every artifact the fleet manifest
declares and appends one line per artifact per run.

| File | Written when |
| :--- | :--- |
| `engine-<realm>-<YYYY-MM>.jsonl` | always, one line per realm per minute |
| `recorder-<venue>-<YYYY-MM>.jsonl` | always, one line per tape recorder per minute |

Four lines a minute, about 2.4 KB: **3.4 MB a day, 1.2 GB a year**, in monthly
files. Nothing rotates or prunes them — next to 1.7 TB a month of tape this is
noise, and the history is the point.

| Field | Meaning |
| :--- | :--- |
| `ts_ms` | When the sample was taken, wall clock |
| `state` | `live`, or `absent` / `unreadable` / `unparsable` with an `error` |
| `equity_usdt`, `available_usdt` | The venue's own reading, from the heartbeat |
| `heartbeat_age_ms`, `account_age_ms` | Age of the heartbeat, and of the venue reading inside it |
| `position_count`, `position_entry_notional_usdt`, `sleeve_positions` | Holdings, and how many each sleeve owns |
| `may_open`, `entry_blockers`, `strategy_errors` | Whether new risk is admitted, and why not |
| `rolling_loss_net_usdt`, `rolling_loss_limit_usdt`, `rolling_loss_tripped` | The 24h breaker against its ceiling |
| `uptime_s`, `market_events`, `orders_sent`, `fills`, `stream_resets` | Since-boot counters; all reset on restart |
| `fills_maker_share`, `fill_all_in_arrival_bps` | What the trading cost |
| `end_to_end_p50_ns`, `end_to_end_p99_ns` | The order path, from the engine's latency ledger |
| `projected_month_gb`, `monthly_gb`, `shed_feeds`, `dropped_frames` | Recorder rows only: inbound projection against allowance |

## 3. Invariants

* **Must**: a realm with no readable heartbeat still get a line, and still push
  `up=0`. A curve that stops cannot be told from a sampler that stopped.
* **Must**: the local append happen before the push, and a failed push exit 0
  with a `WARNING` on the journal. The file is the record; the remote is a view.
* **Must**: realms and artifact paths come from `deploy/fleet_manifest.tsv`, so
  the sampler cannot drift from the fleet the deploy installs.
* **Must Never**: this unit load a venue credential file. It reads published
  artifacts and pushes numbers; its credential surface is empty by construction.
* **Must Never**: a missed minute be replayed. `Persistent=false`; the gap is
  the fact worth keeping.

## 4. Reading the Curve on the Host

```bash
# Last 240 minutes of the funded account: sparkline, range, gaps, last 20 rows
scripts/ops.sh curve mainnet

# A whole day of demo
scripts/ops.sh curve demo 1440

# Or on the host directly
/opt/liquidity-migration/.venv/bin/python \
  /opt/liquidity-migration/scripts/runtime/record_equity.py --show mainnet --samples 240

# Raw samples, newest last
tail -3 /var/lib/liquidity-migration/equity/engine-mainnet-$(date -u +%Y-%m).jsonl | jq .
```

## 5. Grafana Cloud

The free tier is enough: it holds 10k active series and this fleet pushes about
70. Metrics retention there is 14 days, which is why the host files are the
record and the dashboard is the view.

### One-Time Setup

| Step | Where | What |
| :--- | :--- | :--- |
| 1 | grafana.com/auth/sign-up/create-user | Create the account; take the free tier |
| 2 | the stack's **Metrics** / Prometheus page | Copy the **Influx** write URL (ends `/api/v1/push/influx/write`) and the numeric instance ID beside it |
| 3 | **Access Policies** → create token | Scope `metrics:write`, copy the token once |
| 4 | the host | Put all three in `/etc/liquidity-migration/observability.env` (below) |
| 5 | Dashboards → **New** → **Import** | Upload `deploy/grafana/liquidity-migration-fleet.json`, pick the stack's Prometheus datasource |

```bash
# On the host, as root. The template ships the same three keys with comments.
install -o root -g liquidity-migration -m 0640 \
  /opt/liquidity-migration/deploy/observability.env.template \
  /etc/liquidity-migration/observability.env
vi /etc/liquidity-migration/observability.env    # fill URL, user, token

# Prove it in one run, without waiting for the timer
systemctl start liquidity-migration-equity-recorder.service
journalctl -u liquidity-migration-equity-recorder.service -n 5 --no-pager
# "recorded and pushed 4 samples" means the sink accepted it.
```

### Configuration

| Variable | Description |
| :--- | :--- |
| `METRICS_PUSH_URL` | InfluxDB line-protocol write endpoint. Any sink that speaks it works |
| `METRICS_PUSH_USER` | HTTP basic auth user. Grafana Cloud: the numeric instance ID |
| `METRICS_PUSH_TOKEN` | HTTP basic auth password. Grafana Cloud: an Access Policy token |

All three empty or missing means local files only, which is a supported
configuration and prints `no metrics sink configured`.

### Metric Names

Line protocol `lm_engine,realm=mainnet equity_usdt=130.28` arrives as the
Prometheus series `lm_engine_equity_usdt{realm="mainnet"}` — measurement,
underscore, field. Every numeric sample field in §2 becomes one series.
`lm_engine_up` and `lm_recorder_up` are 1 or 0.

`realm` is the only label, deliberately: a label that changes value starts a
new series, so labelling `state` or a `venue` known only while the engine is
up would split a realm's history in two at the moment it went down. The
identity fields (`venue`, `mode`, `engine_commit`, `account_user_id`) stay in
the host record, which is where a forensic question is answered anyway.

Confirm the names the sink actually chose before trusting an empty panel:
**Explore** → the stack's Prometheus datasource → metrics browser → type
`lm_`. If they differ from the above, the dashboard needs one find-and-replace
in `deploy/grafana/liquidity-migration-fleet.json`, not a change to the
sampler.

## 6. Diagnostic Commands

```bash
scripts/ops.sh curve mainnet
scripts/ops.sh logs equity-recorder.service 50
scripts/ops.sh units | grep equity-recorder
```
