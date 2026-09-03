# Data & Market Tape Specification

Data roots, point-in-time universe rules, market tape capture tiers, byte budgets, and timestamp semantics.

---

## 1. Storage Roots & Authority Separation

Research data, live signal state, and execution evidence are strictly separated:

| Root / Authority | Path | Content | Authority / Access |
| :--- | :--- | :--- | :--- |
| **Research Root** | `DATA_ROOT` (e.g. `data/`) | Historical klines, funding, parquet bars, reports | Python offline analytics only. No credentials. |
| **Signal Worker State**| `/var/lib/liquidity-migration-signal-worker-{demo,mainnet}` | Public kline history, funding cache, source checkpoints | Rust signal worker only. Public market data. |
| **Signal Spool** | `/var/lib/liquidity-migration/signals/{demo,mainnet}` | `stream.sock` IPC socket + fallback `.json` spool | Read by Engine, written by Signal Worker (`0770`). |
| **Execution WAL** | `/var/lib/liquidity-migration-engine[-mainnet]` | `engine.wal`, `heartbeat.json`, `trades.jsonl` | Sole execution & accounting authority. |
| **Market Tape Root** | `/var/lib/liquidity-migration/forward-market[-binance]` | Compressed `.jsonl.zst` segments, manifests | Public tape capture only. Independent units. |

---

## 2. Market Tape Capture Tiers

The host runs two continuous market data capture services:
* **Bybit Linear**: `liquidity-migration-forward-capture.service`
* **Binance USD-M**: `liquidity-migration-forward-capture-binance.service`

| Venue | Tier | Universe Membership Criteria | Feeds Captured |
| :--- | :--- | :--- | :--- |
| **Bybit** | `pinned` | Maker canary list (`deploy/forward-capture-symbols.txt`) | `book:50`, `book:1`, `trades`, `ticker`, `liquidations` |
| **Bybit** | `core` | Top 30 by 24h turnover (leaves below rank 45) | `book:50`, `trades`, `ticker`, `liquidations` |
| **Bybit** | `crowded` | Predicted funding $\le -8\text{ bp}$ (held 48 hours) | `book:50`, `trades` |
| **Bybit** | `overheated`| Predicted funding $\ge +8\text{ bp}$ (held 48 hours) | `book:50`, `trades` |
| **Bybit** | `surging` | 24h turnover $\ge 3\times$ baseline (held 24 hours) | `book:50`, `trades` |
| **Bybit** | `movers` | Top 10 price gainers/losers (leaves below rank 15) | `book:50`, `trades` |
| **Bybit** | `bursting` | Price move $\ge 5\%$ inside 1 hour (held 6 hours) | `book:50`, `trades` |
| **Bybit** | `flooding` | Volume $\ge 3\times$ volume of same hour yesterday | `book:50`, `trades` |
| **Bybit** | `levering` | Open interest change $\ge 10\%$ inside 1 hour | `book:50`, `trades` |
| **Bybit** | `wide` | **All other listed USDT perpetuals** | `ticker`, `liquidations` |
| **Binance** | `core` | Top 15 by 24h turnover (leaves below rank 22) | `book:1000`, `trades`, `ticker`, `liquidations` |
| **Binance** | `crowded`..`flooding`| Same rules as Bybit (no open interest tier) | `book:1000`, `trades` |
| **Binance** | `wide` | **All other listed USDT perpetuals** | `ticker` (`@markPrice@1s`), `liquidations` |

---

## 3. Byte Budget & Shedding Hierarchy

Each venue capture carries a **1,300 GB / month** inbound quota to stay within the host's 4 TB monthly bandwidth allocation:

### Automated Shedding Priority
When projected monthly usage exceeds 1,300 GB, the recorder sheds feeds one by one each hour in this exact order (and restores them in reverse once under pace):
1. Deep books of short-lived tiers (`bursting`, `flooding`, `movers`, `surging`, `overheated`).
2. Public trades of short-lived tiers.
3. Core trades (`core:trades`).
4. Wide ticker (`wide:ticker`, Binance only).
* **Invariant**: The `pinned` canary tier is **never shed**.

---

## 4. Storage & Archive Layout

### Local VPS Layout (`/var/lib/liquidity-migration/forward-market[-binance]/`)
```text
<YYYY-MM-DD>/<HH>/<SYMBOL>/segment-NNNNNN.jsonl.zst   Raw JSONL compressed with zstd (rolled at 64 MB raw)
<YYYY-MM-DD>/<HH>/_meta/instruments-<stamp>.json.zst  Instrument table snapshot (tick size, lot size, rules)
<YYYY-MM-DD>/<HH>/_meta/tickers-<stamp>.json.zst      Venue ticker snapshot
manifest.jsonl                                         Atomic receipts: path, row count, byte size, SHA-256
status.json                                            Watchdog status updated every 30 seconds
```
* **Retention**: 30 days is the ceiling; the disk cap binds first — **40 GB Bybit, 30 GB Binance**, sized on measured ingest (8.0 and 5.8 GB/day) and summing under the 118 GB filesystem so neither recorder races the other. That is roughly five days of local tape; the hourly Drive archive is the history. Stops writing if disk free space falls below 25 GB.

### Google Drive Archive Layout
Finished hours are tarred and uploaded ten minutes past each hour:
```text
LiquidityMigration/market-tape/<tape>/YYYY/MM/DD/<day>T<HH>Z.tar
```

---

## 5. Timestamp Conventions & Clocks

| Timestamp Suffix | Unit | Base Clock | Meaning & Usage |
| :--- | :--- | :--- | :--- |
| `_ms` | Milliseconds | Unix Epoch | Wall-clock time used in Python research and venue timestamps. |
| `_ns` | Nanoseconds | Monotonic | Monotonic host receive clock (`local_receive_ts_ns`) used in Rust engine/tape. |
| `feature_ts_ms` | Milliseconds | Unix Epoch | End-of-interval boundary for computed feature batches. |
| `decision_ts_ms` | Milliseconds | Unix Epoch | Point-in-time decision anchor (e.g. 00:00 UTC for CARRY). |
| `expires_at_ms` | Milliseconds | Unix Epoch | Oldest actionable ticker clock plus allowed age window. |

---

## 6. Point-in-Time (PIT) Invariants

1. **Strict Causal Ordering**: Features may use only data known prior to `decision_ts_ms`. No forward-peeking or future revision data.
2. **Membership Manifests**: Universe membership is point-in-time data. Delisted symbols stay in historical frames; new listings cannot appear before their first verified trading row.
3. **Daily Bar Minimum**: A claimed daily bar requires at least 20 aligned hourly rows. Midday listings with null pre-listing prices state that price was not known and do not count as observed bars.

---

## 7. Operational CLI Commands

```bash
# Check dataset coverage across dates and symbols
python -m liquidity_migration --data-root data/ coverage

# Read market tape hours and records
python -m market_tape hours /var/lib/liquidity-migration/forward-market
python -m market_tape rows /var/lib/liquidity-migration/forward-market --hours 2026-09-02T22 --symbols BTCUSDT

# Generate fixed-interval bars from tape
python -m market_tape bars /var/lib/liquidity-migration/forward-market --hours 2026-09-02T00..2026-09-02T23 --interval 60 --out bars_1m.parquet

# Research refresh workflow
scripts/ops.sh research-refresh plan --end YYYY-MM-DD
scripts/ops.sh research-refresh run --end YYYY-MM-DD
```
