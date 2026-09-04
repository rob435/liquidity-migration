# Observability Specification

What the fleet records about itself, where it lands, and how to see it.

---

## 1. Surfaces

| Surface | Unit | Cadence | Output | Authority |
| :--- | :--- | :--- | :--- | :--- |
| **Equity & recorder samples** | `liquidity-migration-equity-recorder.timer` | Every minute at :20 | JSONL under `/var/lib/liquidity-migration/equity`, plus one optional HTTP push | Read-only; loads no venue environment |
| **Order-path probe** | the `probe` sleeve of the **demo** engine | Every 15 min at :00, :15, :30, :45 | One venue-minimum post-only `BTCUSDT` buy 3% under the bid, pulled 2 s later: one measurement in the engine's latency ledger, read by the :20 sample | Demo only; never a page, never a Telegram message; stands down while another sleeve holds the symbol ([trading_logic.md](trading_logic.md) §1) |
| **Engine heartbeat** | the engine itself | Every 5 s | `heartbeat.json`, overwritten | The engine's own statement of health |
| **Signal-worker heartbeat** | each realm's credential-free producer | At most every 5 s | `heartbeat.json`, overwritten; sampled locally and remotely every minute | Worker verdict plus raw WebSocket, coverage, cycle, queue, and spool facts |
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
| `worker-<realm>-<YYYY-MM>.jsonl` | always, one line per realm per minute |
| `recorder-<venue>-<YYYY-MM>.jsonl` | always, one line per tape recorder per minute |

Six lines a minute, about 7 KB: **10 MB a day, 3.7 GB a year**, in monthly
files. Nothing rotates or prunes them — next to 1.7 TB a month of tape this is
noise, and the history is the point.

| Field | Meaning |
| :--- | :--- |
| `ts_ms` | When the sample was taken, wall clock |
| `state` | `live`, or `absent` / `unreadable` / `unparsable` with an `error` |
| `equity_usdt`, `available_usdt` | The venue's own reading, from the heartbeat |
| `heartbeat_age_ms`, `account_age_ms` | Age of the heartbeat, and of the venue reading inside it |
| `position_count`, `position_entry_notional_usdt`, `sleeve_positions` | Holdings, and how many each **configured** sleeve owns, zero included; `unattributed` is the owner's hand exposure |
| `sleeve_entries_enabled`, `sleeve_blockers` | Per configured sleeve: the effective entry gate (1/0) and how many symbols it is blocked on |
| `may_open`, `entry_blockers`, `strategy_errors`, `working_entries`, `pending_flatten_requests` | Whether new risk is admitted and why not; orders resting at the venue; flattens not yet acknowledged |
| `rolling_loss_net_usdt`, `rolling_loss_limit_usdt`, `rolling_loss_tripped`, `rolling_loss_trades` | The 24h breaker against its ceiling |
| `uptime_s`, `market_events`, `orders_sent`, `fills`, `stream_resets`, `amends_confirmed`, `amends_pulled_unconfirmed` | Since-boot counters; all reset on restart. The dashboard reads them as `increase()` |
| `fills_maker_share`, `fill_all_in_arrival_bps`, `fill_arrival_shortfall_bps`, `fill_fee_coverage`, `fill_markout_1m_our_way_bps` | What the trading cost |
| `decide_*`, `durable_*`, `wire_*`, `ack_*`, `dispatch_queue_*`, `venue_task_*`, `core_resume_*`, `end_to_end_*` (`_p50_ns`, `_p99_ns`), `barrier_wait_p99_ns`, `quota_hold_p99_ns` | The order path step by step, from the engine's 60-second latency ledger. Null in the file and **absent from the push** in a minute nothing went out: an empty window is no measurement, never zero. `wire` is the whole venue task (decision to completion) and so contains the round trip; `ack` is the round trip alone and records only where the adapter stamped the socket write — see the note below |
| `status_healthy`, `status_ready`, `status_starting`, `status_recovering`, `heartbeat_age_ms` | Worker rows only: the bounded producer verdict and heartbeat freshness; `starting` is cold fill, while `recovering` is a live repair inside its two-minute bound |
| `ws_connected`, `ws_gap_open`, `ws_gap_age_ms`, `ws_last_frame_age_ms`, `ticker_coverage_complete` | Worker rows only: raw transport and coverage state. A coverage miss remains visible while the producer applies its two-minute persistence bound |
| `ticker_rows`, `ticker_capacity`, `*_topics_accepted`, `*_topics_quarantined`, `ws_queue_fill` | Worker rows only: exact subscription and bounded in-memory queue facts |
| `long_cycle_age_ms`, `carry_cycle_age_ms`, `rest_ticker_*_count`, `spool_*`, `replaceable_outputs_coalesced` | Worker rows only: reducer progress, fallback outcomes, and durable handoff pressure |
| `projected_month_gb`, `monthly_gb`, `budget_over`, `shed_feeds` | Recorder rows only: inbound projection against allowance |
| `received_frames`, `written_rows`, `dropped_frames`, `disk_dropped_frames`, `queued_frames`, `queue_capacity`, `queue_fill`, `disk_blocked` | Recorder rows only: the writer queue. A dropped frame is an overrun, and every overrun reconnects the shard |
| `shards`, `shards_connected`, `reconnects`, `bytes_24h`, `free_disk_bytes`, `snapshot_failures` | Recorder rows only: the connections and the disk. `reconnects` is since boot, summed over shards |
| `started_at_ns`, `pid` | Recorder status only: startup grace and process identity used by liveness and deploy readiness checks |

## 3. Invariants

* **Must**: a realm with no readable heartbeat still get a line, and still push
  `up=0`. A curve that stops cannot be told from a sampler that stopped.
* **Must**: the local append happen before the push, and a failed push exit 0
  with a `WARNING` on the journal. The file is the record; the remote is a view.
* **Must**: realms and artifact paths come from `deploy/fleet_manifest.tsv`, so
  the sampler cannot drift from the fleet the deploy installs.
* **Must**: every sleeve the heartbeat lists in `strategies` be a series, zero
  included. A sleeve that has held nothing since the sampler started is a line
  at zero, not a missing line.
* **Must Never**: an empty latency window be pushed as zero. The ledger's null
  is absent from the line; the dashboard plots the order path as points.
* **Must Never**: the probe page or message anybody. It reports through
  `entry_blockers`, never `strategy_errors`, and `notify_book_changes.py` hides
  its sleeve.
* **Must Never**: the probe place on a symbol another sleeve holds. A Bybit
  entry carries `stopLoss` with `tpslMode: Full`, so the stop it names belongs
  to the whole position, and the probe's stop is deliberately far away.
* **Must Never**: this unit load a venue credential file. It reads published
  artifacts and pushes numbers; its credential surface is empty by construction.
* **Must Never**: a missed minute be replayed. `Persistent=false`; the gap is
  the fact worth keeping.
* **Must Never**: Grafana be the only pager. It is a remote view fed by this
  host; Telegram, the incident routine, and the watchdog-plane dead-man are
  independent delivery paths defined in [notifications.md](notifications.md).

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
220. Metrics retention there is 14 days, which is why the host files are the
record and the dashboard is the view.

### One-Time Setup

| Step | Where | What |
| :--- | :--- | :--- |
| 1 | grafana.com/auth/sign-up/create-user | Create the account; take the free tier |
| 2 | the stack's **Metrics** / Prometheus page | Copy the **Influx** write URL (ends `/api/v1/push/influx/write`) and the numeric instance ID beside it |
| 3 | **Access Policies** → create token | Scope `metrics:write`, copy the token once |
| 4 | the host | Put all three in `/etc/liquidity-migration/observability.env` (below) |
| 5 | Dashboards → **New** → **Import** | Upload `deploy/grafana/liquidity-migration-fleet.json`, pick the stack's Prometheus datasource. Re-import after a change; the UID `liqmig-fleet` is stable, so it replaces in place |

```bash
# On the host, as root. The template ships the same three keys with comments.
install -o root -g liquidity-migration -m 0640 \
  /opt/liquidity-migration/deploy/observability.env.template \
  /etc/liquidity-migration/observability.env
vi /etc/liquidity-migration/observability.env    # fill URL, user, token

# Prove it in one run, without waiting for the timer
systemctl start liquidity-migration-equity-recorder.service
journalctl -u liquidity-migration-equity-recorder.service -n 5 --no-pager
# "recorded and pushed 6 samples" means the sink accepted it.
```

### Configuration

| Variable | Description |
| :--- | :--- |
| `METRICS_PUSH_URL` | InfluxDB line-protocol write endpoint. Any sink that speaks it works |
| `METRICS_PUSH_USER` | HTTP basic auth user. Grafana Cloud: the numeric instance ID |
| `METRICS_PUSH_TOKEN` | HTTP basic auth password. Grafana Cloud: an Access Policy token |

All three empty or missing means local files only, which is a supported
configuration and prints `no metrics sink configured`.

### The Dashboard

`deploy/grafana/liquidity-migration-fleet.json` is rendered by
`deploy/grafana/render_dashboard.py`; edit the script, run it, commit both.
`render_dashboard.py --check` and the test suite refuse a JSON that is not what
the script renders.

| Section | Panels | Reads |
| :--- | :--- | :--- |
| **Status** | engine, entry permission, worker verdict and coverage, recorder state | `lm_engine_{up,may_open}`, `lm_worker_{status_healthy,ticker_coverage_complete}`, `lm_recorder_up` |
| **Account** | equity and open entry notional as time series | `equity_usdt`, `position_entry_notional_usdt` |
| **Execution** | orders, fills, and stream resets per 15 minutes; p99 order-path latency with end-to-end emphasized | `increase({orders_sent,fills,stream_resets}[15m])`, `{end_to_end,ack,durable,decide}_p99_ns` |
| **Data pipeline** | market-data freshness, combined worker and recorder capacity, recorder faults | engine, worker, and recorder ages; worker and recorder fill ratios; recorder shard, drop, and reconnect series |

The six-hour default view is an operator view. Change the time range for incident
analysis. Empty order-path windows are absent, so the latency chart marks only
real measurements.

**`ack` is empty on demo, and that is a property of the venue path, not a
fault in the sampler.** `engine latency --wal` is the authority and states it
outright. Check before comparing venue-round-trip details outside this summary dashboard:

```bash
scripts/ops.sh deploy verify   # or, on the host, per realm:
/opt/liquidity-migration-engine/bin/engine latency \
  --wal /var/lib/liquidity-migration-engine/engine.wal
```

### Metric Names

Line protocol `lm_engine,realm=mainnet equity_usdt=130.28` arrives as the
Prometheus series `lm_engine_equity_usdt{realm="mainnet"}` — measurement,
underscore, field. Every numeric sample field in §2 becomes one series.
`lm_engine_up`, `lm_worker_up`, and `lm_recorder_up` are 1 or 0. The per-sleeve dictionaries
flatten to `lm_engine_sleeve_<name>_positions`,
`lm_engine_sleeve_<name>_entries_enabled` and `lm_engine_sleeve_<name>_blockers`,
one series per configured sleeve, and the dashboard turns the name back into a
`sleeve` label with `label_replace`.

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

# The probe, on the demo engine: one "probe rested" a quarter hour, refusals
# and fills at warn. The fleet runs at RUST_LOG=info.
scripts/ops.sh logs engine.service 400 | grep -i probe

# The dashboard JSON is what the renderer says it is
python deploy/grafana/render_dashboard.py --check
```
