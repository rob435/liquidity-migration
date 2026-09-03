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
* **Bybit Linear**: `liquidity-migration-forward-capture.service`. The venue we trade, so this is the tape with order books, and the only one `engine backtest` replays.
* **Binance USD-M**: `liquidity-migration-forward-capture-binance.service`. Cross-venue reference only: ticker (funding rate, mark, index) on every listed name plus trades where flow matters, and **no order book**. `market_tape.bars` gives it `funding_rate`, `mark_price`, `index_price` from the ticker and `open/high/low/close/vwap` from the trades, which is every column the cross-venue studies read.

Invariants:
* The two tapes never share a path: separate roots, separate systemd `StateDirectory`, separate Google Drive prefixes (`market-tape/bybit-linear` and `market-tape/binance-usdm`), and every row names its own `venue`.
* Book rows chain by their venue's own rule. `engine backtest` implements Bybit's (monotone `update_id`, restarted by a snapshot) and **refuses a book row from any other venue** rather than building a book that is not the venue's.
* Every book topic is re-subscribed once per UTC hour (`connection.reanchor_books_each_hour`), so each hour of tape — one directory, one uploaded tar — opens with a snapshot per symbol and can be replayed without the hours before it.

### Name coverage against what the sleeves trade

The tiers are keyed on the same signals the sleeves decide from, so a tradeable name is captured by construction rather than by a list: LONG's top-turnover names are `core`, CARRY's and EXODUS's negative-funding names are `crowded` (entry is $\le -10$ bp, capture starts at $-8$ bp), the maker canary is `pinned`, and every other listed perpetual is `wide` on ticker and liquidations. Verified 2026-09-03 against the funded book: `NEARUSDT` and `ZECUSDT` both held, both carrying a 50-level snapshot, deltas, prints and ticker.

**Known limit.** Membership follows market state, not the position book. `core` releases a name below turnover rank 45 and `crowded` 48 hours after funding recovers, while LONG holds for about three days, so a held name that drifts out mid-hold keeps its ticker but loses its book and prints for the remainder. Nothing pins a held name: the recorder reads no engine state by design (public data only, no credentials, its own user). Widening `core`'s `leave_top` is the lever if this ever costs a study, at roughly 21 GB/month per additional name.

### Coverage of the discovery tiers

Coverage is total by construction, not by sampling. The venue lists 855
instruments; 747 are USDT `LinearPerpetual` (the rest are USDC, or dated
`LinearFutures`), and the tiers hold 716 `wide` + 30 `core` + 1 `pinned` = 747,
with an open segment on disk for every one. A name is therefore never absent —
only shallower.

The discovery sensors resolve to real names and their books chain. Rebuilt from
one recorded hour with `market_tape book`, `valid: true` and `held_deltas: 0`
each — a chained book, not a fragment:

| Tier | Name | Deltas applied | Rebuilt spread |
| :--- | :--- | ---: | ---: |
| `movers` | `APRUSDT` | 87,997 | 3.5 bp |
| `movers` | `MAGMAUSDT` | 20,709 | 5.5 bp |
| `overheated` | `POETUSDT` | 6,885 | 24.5 bp |

**Known limit — the windowed sensors are blind for one hour after a restart.**
`price_burst`, `volume_burst` and `oi_change` compare the live ticker against a
sample one `window_hours` back, and that history lives in memory. A recorder
restart empties it, so `bursting`, `flooding` and `levering` resolve to zero
names until an hour of ticker has accumulated. `turnover_surge` is the same
against its day baseline. The funding and rank tiers (`crowded`, `overheated`,
`core`, `movers`) need no history and repopulate within one maintenance tick.

**A thin name's segment can legitimately read 0 bytes.** `SegmentWriter` opens
with `buffering=65536`, so a `wide`-only name shows nothing on disk until 64 KB
of rows accumulate. Check `status.json` for tier membership, not `ls`.

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
| **Binance** | `core` | Top 15 by 24h turnover (leaves below rank 22) | `trades`, `ticker`, `liquidations` |
| **Binance** | `crowded`..`flooding`| Same rules as Bybit (no open interest tier) | `trades` |
| **Binance** | `wide` | **All other listed USDT perpetuals** | `ticker` (`@markPrice@1s`), `liquidations` |

---

## 3. Byte Budget & Shedding Hierarchy

The two recorders hold separate quotas out of the host's 4 TB line. Bybit is the
venue that gets replayed, so it holds the larger share; Binance exists only as a
cross-venue reference and records no book.

| | Bybit linear | Binance USD-M |
| :--- | :--- | :--- |
| `monthly_gb` | 1,800 | 700 |
| `max_disk_gb` | 60 | 18 |
| `min_free_disk_gb` | 25 | 25 |
| `retention_days` | 30 (the disk cap binds first) | 30 |

### Automated Shedding Priority

The projection is the trailing day of bytes from the pairs **still subscribed**,
scaled to a month; a shed pair's bytes are left out of it. One action per
`act_every_minutes`: a shed takes as many pairs from the list, in order, as the
projection needs, and a restore returns the last pair shed once its own measured
GB/month fits under `restore_below` of the allowance.

Bybit gives up the discovery books first and the crowd books last:

1. `bursting`, `flooding`, `levering`, `movers`, `surging` — `book:50`
2. the same five tiers — `trades`
3. `overheated:book:50`, then `crowded:book:50`

**Invariants — what `shed` must never contain, whatever the projection says:**

* `core:book:50` — the book every replay and the maker sleeve run on.
* `core:trades` — the prints a resting order fills against; without them a maker
  replay on a core name cannot fill at all.
* `*:ticker` — funding, open interest and price: the sensor every tier is
  resolved from, and CARRY's entry signal.
* Anything in the `pinned` canary tier.

Over budget with every listed pair already shed is a `WARNING` per action naming
the overshoot. The recorder does not reach for anything above; the config decides
what else goes.

**Consequence to weigh before widening a tier.** Because the discovery books sit
first in the list, a projection over 1,800 GB costs the pump tiers their books
before it costs the crowd theirs. Those pairs are cheap — roughly 2 GB/month per
name against ~21 GB for a `core` name — so shedding all ten frees little. The
allowance is set to leave headroom instead: 1,710 GB projected at full 48-hour
sticky width against 1,800 allowed.

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
* **Retention**: 30 days is the ceiling; the disk cap binds first — **60 GB Bybit, 18 GB Binance**, summing under the 118 GB filesystem so neither recorder races the other. That is about three days of Bybit tape locally; the hourly Drive archive is the permanent history. Either recorder stops writing if free space falls below 25 GB.

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
