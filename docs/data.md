# Data & Market Tape Specification

## Purpose

Define historical inputs, causal delivery, market capture, instrument metadata and backtest execution assumptions.

## Spec Tables

---

### 1. Storage Roots & Authority Separation

Research data, live signal state, and execution evidence are strictly separated:

| Root / Authority | Path | Content | Authority / Access |
| :--- | :--- | :--- | :--- |
| **Research Root** | `DATA_ROOT` (e.g. `data/`) | Historical klines, funding, parquet bars, reports | Python offline analytics only. No credentials. |
| **Signal Worker State**| `/var/lib/liquidity-migration-signal-worker-{demo,mainnet,mexc}` | Public kline history, funding cache, source checkpoints | Rust signal worker only. Public market data. |
| **Signal Spool** | `/var/lib/liquidity-migration/signals/{demo,mainnet,mexc}` | `stream.sock` IPC socket + fallback `.json` spool | Read by Engine, written by Signal Worker (`0770`). |
| **Execution WAL** | `/var/lib/liquidity-migration-engine[-mainnet\|-mexc]` | `engine.wal`, `heartbeat.json`, `trades.jsonl` | Sole execution & accounting authority. |
| **Market Tape Root** | `/var/lib/liquidity-migration/forward-market[-binance]` | Compressed `.jsonl.zst` segments, manifests | Public tape capture only. Independent units. |

---

### 2. Market Tape Capture Tiers

The host runs two continuous market data capture services:
* **Bybit Linear**: `liquidity-migration-forward-capture.service`. The venue we trade, so this is the tape with order books, and the recorder adapter whose book deltas `engine backtest` reconstructs.
* **Binance USD-M**: `liquidity-migration-forward-capture-binance.service`. Cross-venue reference only: ticker (funding rate, mark, index) on every listed name plus trades where flow matters, and **no order book**. `market_tape.bars` gives it `funding_rate`, `mark_price`, `index_price` from the ticker and `open/high/low/close/vwap` from the trades, which is every column the cross-venue studies read.

Invariants:
* The two tapes never share a path: separate roots, separate systemd `StateDirectory`, separate Google Drive prefixes (`market-tape/bybit-linear` and `market-tape/binance-usdm`), and every row names its own `venue`.
* Recorder book rows use Bybit sequence/reset rules. Other venues supply reconstructed, ordered snapshots through `historical_v1`; their deltas are never relabelled as Bybit updates.
* Every book topic is re-subscribed once per UTC hour (`connection.reanchor_books_each_hour`), so each hour of tape — one directory, one uploaded tar — opens with a snapshot per symbol and can be replayed without the hours before it.

### Name coverage against what the sleeves trade

The tiers are keyed on the same signals the sleeves decide from, so a tradeable name is captured by construction rather than by a list: LONG's top-turnover names are `core`, CARRY's and EXODUS's negative-funding names are `crowded` (entry is $\le -10$ bp, capture starts at $-8$ bp), the maker canary is `pinned`, and every other listed crypto perpetual is `wide` on ticker and liquidations. `core` is LONG's live rank band (enter 120, leave 160) with a 96-hour floor, so every name the sleeve can hold has its book through the hold; `crowded` watches CARRY's whole hold zone — predicted funding at $-3$ bp, the sleeve's exit line — for 72 hours past the last such reading. Verified 2026-09-03 against the funded book: `NEARUSDT` and `ZECUSDT` both held, both carrying a 50-level snapshot, deltas, prints and ticker.

**Coverage boundary.** The current policy uses `leave_top = 160`, `sticky_hours = 96` for core and `threshold_bp = 3`, `sticky_hours = 72` for crowded. These are capture policies, not proof of delivered data. Verify symbol membership, snapshots, gaps and shedding in each selected archive; the recorder reads no private position state.

### What the tape gives each sleeve's exit study

The first purpose of the tape is exits; the second is breadth. Every listed
crypto name carries every print, the ticker (price, mark, index, funding, open
interest, 24h turnover) and liquidations at all times, so a bar-level backtest of
any strategy — and every *signal-level* exit: trailing stop, funding turn, OI
unwind, cascade — runs on the whole universe. The deep tiers add the 50-level
book for *execution* and microstructure work, and are shaped so a held name
never loses it mid-hold:

| Sleeve | Hold | Deep coverage guarantee | Exit questions the tape can answer |
| :--- | :--- | :--- | :--- |
| **LONG** | ≤ 72 h on a name that surged into turnover rank ≤ 10 | `core`: rank ≤ 120, leaves below 160, **and 96 h after it last ranked inside 120** — the pump can fade to rank 300 and the book stays | trailing stop vs. time exit; volume decay (prints); OI unwind; bid-depth thinning; funding turning positive; what the exit left on the table (ticker tail) |
| **CARRY** | days to weeks while settled funding sits between the $-10$ bp entry and the $-3$ bp exit | `crowded`: predicted funding $\le -3$ bp — the exit line — held 72 h past the last such reading; top-100 names are in `core` anyway | funding trajectory vs. the $-3$ hysteresis; 2-day recovery; OI unwind as the crowd leaves; short-liquidation squeezes; taker buy pressure; bid depth at exit |
| **EXODUS** | ~60–75 min: short at CARRY's pre-settlement fire, cover hard at S+60 | the name is a CARRY hold seconds earlier, so it is in `crowded` or `core` with book and prints; ticker at ~100 ms, book at 20 ms | cover at S+15/30/60/120; cover on OI stabilisation or price reversal; the settlement print itself (`fundingRate` at `nextFundingTime` roll) |

The hourly book re-anchor runs in the first minutes of each hour, which is also
when funding settles. Each re-anchored name loses one round trip of deltas and
opens on a fresh snapshot in that window; the snapshot row marks it. For a
60-minute EXODUS window this is immaterial, and it is recorded here so no study
mistakes the seam for a venue event.

### Coverage of the discovery tiers

Coverage of the domain is total by construction, not by sampling, and the
domain is crypto. The venue lists 855 instruments; 747 are USDT
`LinearPerpetual`, and of those 230 are stocks (177), ETFs (49) and commodities
(4) that Bybit files in the same category and marks with `symbolType`. The
recorder leaves every `symbolType` but `""` and `"innovation"` out of every
tier — the same two labels the signal worker's live universe and the research
universe table keep, pinned together by `tests/repo/test_crypto_domain_is_one_line.py`.
No sleeve can hold a stock perpetual, and its session-shaped activity fires
`volume_burst` and `oi_change` on every US open, so before the filter `levering`
resolved to seven names and all seven were equities. The 517 crypto names that
remain are covered in full: a name is never absent, only shallower. Binance
needs no such filter; it files the same products as `TRADIFI_PERPETUAL`, which
its adapter already refuses.

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
| **Bybit** | `core` | Top 120 by 24h turnover, leaves below rank 160 — LONG's live `enter_rank`/`leave_rank` — and stays 96 h after it last ranked inside 120 | `book:50`, `trades`, `ticker`, `liquidations` |
| **Bybit** | `crowded` | Predicted funding $\le -3\text{ bp}$ — CARRY's *exit* line, so the whole hold zone from its $-10$ bp settled entry — held 72 hours past the last such reading | `book:50`, `trades` |
| **Bybit** | `overheated`| Predicted funding $\ge +5\text{ bp}$ (held 48 hours); no sleeve trades it, first to shed | `book:50`, `trades` |
| **Bybit** | `surging` | 24h turnover $\ge 3\times$ baseline (held 24 hours) | `book:50`, `trades` |
| **Bybit** | `movers` | Top 10 price gainers/losers (leaves below rank 15) | `book:50`, `trades` |
| **Bybit** | `bursting` | Price move $\ge 5\%$ inside 1 hour (held 6 hours) | `book:50`, `trades` |
| **Bybit** | `flooding` | Volume $\ge 3\times$ volume of same hour yesterday | `book:50`, `trades` |
| **Bybit** | `levering` | Open interest change $\ge 10\%$ inside 1 hour | `book:50`, `trades` |
| **Bybit** | `wide` | **All other listed crypto USDT perpetuals** (`symbolType` `""` or `innovation`) | `trades`, `ticker`, `liquidations` — every name has a complete trade tape; only the book is tiered |
| **Binance** | `core` | Top 15 by 24h turnover (leaves below rank 22) | `trades`, `ticker`, `liquidations` |
| **Binance** | `crowded`..`flooding`| Same rules as Bybit (no open interest tier) | `trades` |
| **Binance** | `wide` | **All other listed USDT `PERPETUAL`s** (`TRADIFI_PERPETUAL` excluded) | `ticker` (`@markPrice@1s`), `liquidations` |

---

### 3. Byte Budget & Shedding Hierarchy

The two recorders hold separate quotas out of the host's 4 TB line. Bybit is the
venue that gets replayed, so it holds the larger share; Binance exists only as a
cross-venue reference and records no book.

| | Bybit linear | Binance USD-M |
| :--- | :--- | :--- |
| `monthly_gb` | 2,400 | 700 |
| `max_disk_gb` | 60 | 18 |
| `min_free_disk_gb` | 25 | 25 |
| `retention_days` | 30 (the disk cap binds first) | 30 |

### Automated Shedding Priority

The projection is the trailing day of bytes from the pairs **still subscribed**,
scaled to a month; a shed pair's bytes are left out of it. One action per
`act_every_minutes`: a shed takes as many pairs from the list, in order, as the
projection needs, and a restore returns the last pair shed once its own measured
GB/month fits under `restore_below` of the allowance.

Bybit gives up what no sleeve trades first, then the pump-discovery books, then
their prints — and nothing of a sleeve's own universe:

1. `overheated` — `book:50`, then `trades`
2. `wide:trades` — the thin names' prints; their price and volume stay on the ticker
3. `bursting`, `flooding`, `levering`, `movers`, `surging` — `book:50`
4. the same five tiers — `trades`

**Invariants — what `shed` must never contain, whatever the projection says:**

* `core:book:50` — the book every replay and the maker sleeve run on.
* `core:trades` — the prints a resting order fills against; without them a maker
  replay on a core name cannot fill at all.
* `crowded:*` — CARRY's names, observed from $-3$ bp predicted, the sleeve's
  exit line, so the book is recording for the whole hold and its exit.
* `*:ticker` — funding, open interest and price: the sensor every tier is
  resolved from, and CARRY's entry signal.
* Anything in the `pinned` canary tier.

Over budget with every listed pair already shed is a `WARNING` per action naming
the overshoot. The recorder does not reach for anything above; the config decides
what else goes.

**What the allowance is built from** (measured 2026-09-03 on the crypto-only
domain): a top-30 `core` name runs 17.8 GB/month of 50-level book and 3.6 of
prints; a mid-rank crowd name 7.3 and 1.9; a thin one 2.4. LONG's band adds ~90
names at roughly 7 GB each, the 5 bp crowd thresholds ~11 more, and the 230
non-crypto names that left `wide` return 84 — about 2,300 GB at full 48-hour
sticky width against 2,400 allowed. `overheated` sits first in the shed list
because it is the one deep tier no sleeve trades; the pump books are cheap
(~2 GB/month per name) and come after it.

---

### 4. Storage & Archive Layout

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

### 5. Timestamp Conventions & Clocks

| Timestamp Suffix | Unit | Base Clock | Meaning & Usage |
| :--- | :--- | :--- | :--- |
| `_ms` | Milliseconds | Unix Epoch | Wall-clock time used in Python research and venue timestamps. |
| `local_receive_ts_ns`, historical `recv_ns` | Nanoseconds | Unix Epoch | Recorder wall time at receipt; historical delivery sort key. The virtual replay clock uses this origin. |
| Runtime monotonic `_ns` | Nanoseconds | Process monotonic | Runtime duration measurement; cannot be carried across process restarts. |
| `feature_ts_ms` | Milliseconds | Unix Epoch | End-of-interval boundary for computed feature batches. |
| `decision_ts_ms` | Milliseconds | Unix Epoch | Point-in-time decision anchor (e.g. 00:00 UTC for CARRY). |
| `expires_at_ms` | Milliseconds | Unix Epoch | Oldest actionable ticker clock plus allowed age window. |

---

### Historical adapters and execution

| Layer | Owner | Contract |
| --- | --- | --- |
| Recorder decoding | `engine-tools/src/backtest/tape.rs` | `market_tape` JSONL / `.zst`; Bybit snapshots/deltas become valid depth snapshots or explicit invalidations. Missing receipt time, broken ordering and failed decompression are errors |
| External decoding | `liquidity_migration/data/history.py` | Existing archive text reader for CSV/gzip/zip; batched Parquet; disk sort by availability then input ordinal; identical trade IDs deduplicate, conflicting IDs fail |
| Normalized events | `engine-tools/src/backtest/source.rs` | `HistoricalSource` yields book, trade, ticker or completed bar facts; header venue and membership bind every row |
| Instruments | `engine-tools/src/backtest/instruments.rs` | Recorder `instruments_snapshot` or `instrument_catalog_v1` with `specs: [[symbol, ExactInstrumentSpec], ...]`; decimal strings retain exact constraints, absent assets/bounds stay unknown |
| Delivery | `backtest/feed.rs`, `scheduler.rs`, `signals.rs` | Availability drives one virtual timeline; receipt and exchange time remain separate; signals use their recorded availability |
| Decisions/accounting | Existing `engine-strategies`, `engine-risk`, `engine-core`, WAL | Same reducers, risk, durability and accounting for every adapter; no Python strategy fork |

| Execution flag | Required inputs | Declared approximation |
| --- | --- | --- |
| `--execution books` (default) | Observed depth; trades for queue consumption | Existing depth/queue matching and binary64 fill contract; a broken chain removes stale executable depth |
| `--execution trades` | Prints and explicit USDT settlement metadata | Orders wait for the next print; shared `volume * participation` capacity; half-spread plus slippage for takers; passive limits use print reach, zero queue ahead and maker fees; stops trigger on last trade |
| `--execution bars` | Completed OHLCV and explicit USDT settlement metadata | Only orders present at candle start use its completed close, delivered at availability. Carried stops execute first at the adverse high/low, sharing capacity; newly opened positions cannot use the candle's earlier range |
| Both sparse modes | Explicit `--spread-bps`, `--slippage-bps`, `--participation` | USDT linear cash model; market/IOC remainder cancels; amendments require cancel/replace; derived exact grid quantities; risk quotes are modeled spread/capacity, never observed depth. Liquidation uses full reference-price closure without a depth/capacity bound |
| Funding | Timestamped rate plus next settlement boundary; valuation price | Missing funding remains unmeasured. A trade/bar-only zero in the ledger does not establish zero historical funding cost |
| Fixed metadata | One supplied catalog and explicit `instrument_assumption` | No time-varying tick/contract schedule; split runs at a metadata change with appropriate state handling. Missing contract/asset facts cannot be claimed historical |

| Mapping key | Schema / rule |
| --- | --- |
| `source` | `bybit_archive` uses the verified archive columns below; `mapped` uses explicit `columns` |
| `kind`, `venue` | `trade`, `bar`, `ticker`, or reconstructed `book`; one unchanged venue identity |
| `columns`, `symbols`, optional `sides` | Canonical-field → source-column, source-symbol → native-symbol, source-side → buyer-aggressor boolean; no inferred aliases |
| `timestamp_unit`, optional `receive_timestamp_unit` | `s`, `ms`, `us`, `ns`; decimal parsing without a binary64 timestamp round trip |
| `delivery` | `{"kind":"observed"}` requires `recv_ns`; otherwise `{"kind":"exchange_plus_delay","delay_ns":1000000}` explicitly models delivery |
| `membership` | `[{"symbol":"BTCUSDT","start_ns":...,"end_ns":...}]`; end exclusive; event exchange time and opening-order arrival must belong to an interval |
| `instrument_assumption` | Required text describing the supplied fixed catalog's evidence/assumptions |
| Trade columns | `symbol`, `exchange_ts_ns`, `price`, `qty`, `side`, `trade_id`; Bybit maps `timestamp`, `symbol`, `price`, `size`, `side`, `trdMatchID` and seconds |
| Bar columns | `symbol`, `exchange_ts_ns`, `start_ns`, `end_ns`, OHLC, `volume`; exchange time equals exclusive candle end; availability cannot precede end |
| Ticker columns | Optional `last_price`, `mark_price`, `index_price`, `funding_rate`, `next_funding_time_ms`; absent values remain null, not zero |
| Book columns | `symbol`, timestamps, `depth`, `update_id`, `cross_sequence`, `bids`, `asks`; sides are JSON arrays of `[price, qty]`. Supply reconstructed snapshots; normalized JSONL can also invalidate a book with `valid:false` |
| Normalized header | First JSONL row: `schema:"historical_v1"`, venue, delivery description, channels, membership, instrument assumption; subsequent rows include kind, venue, symbol and both integer timestamps |

| Strategy input | Supported generation / unresolved requirement |
| --- | --- |
| Probe | Existing quote-based strategy works with the declared trade/bar risk quote; supplied configs are execution diagnostics, not candidate strategies |
| Quoter | Requires observed depth; sparse execution modes refuse it |
| LONG | `build_long_research_inputs` calls the existing `rules.long_native.build_long_features` on PIT-filtered hourly history; `run_long_native_research` uses the existing Rust decision reducer. Needs cross-sectional history/warmup, `archive_trade_manifest` and funding; the small BTC sample cannot generate this strategy's required inputs |
| CARRY / EXODUS | Existing CARRY panel/native reducer path uses settled observations; pre-settlement actions need timestamped running rates and marks. EXODUS also depends on the CARRY event stream and initial state |
| Full engine signal replay | LONG/CARRY refuse absent `--signals`; unmanaged observation directories replay at availability. Managed directories remain unsupported without replay of chronological producer grants/seals. Accepted WAL observations alone do not reconstruct a fresh boot's producer lifecycle handshake |

The fixture provenance records the original [Bybit archive](https://public.bybit.com/trading/BTCUSDT/BTCUSDT2020-03-25.csv.gz) and SHA256. The 64 original trades preserve fractional-second stamps. Twelve interior hourly candles use the existing archive aggregation function. The catalog, delivery, spread, participation and probe settings are explicitly illustrative; all fixture data shaped these diagnostics and grades no strategy.

## Invariants

1. **Strict Causal Ordering**: Features may use only data known prior to `decision_ts_ms`. No forward-peeking or future revision data.
2. **Membership Manifests**: Universe membership is point-in-time data. Delisted symbols stay in historical frames; new listings cannot appear before their first verified trading row.
3. **Daily Bar Minimum**: A claimed daily bar requires at least 20 aligned hourly rows. Midday listings with null pre-listing prices state that price was not known and do not count as observed bars.

---

- Must preserve exchange time, availability, venue identity and missing values independently.
- Must never claim a modeled quote/candle range is observed order-book liquidity.
- Must compare discrete decisions and ledger keys exactly. Equivalent adapters use byte-identical WAL/trade outputs and exact same-process floating values; cross-platform continuous comparisons must state tolerances and matching NaN positions. Existing flat-account venue/engine net reconciliation uses `1e-6 * max(abs(engine_net), 1)` USDT; adapter equivalence is stricter.
- A selected execution mode must receive its required book/trade/bar channel; an empty mode is an error even if the header declares it.
- Must distinguish observed execution accounting from hypothetical fill simulation and strategy evidence.

## Operational Recipes

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

Build the offline tool and import the actual sample; every output path must be new:

```bash
rustup run 1.90.0 cargo build --release -p engine-tools --bin engine-tools --manifest-path engine/Cargo.toml
TASK_HISTORY=$(mktemp -d)
.venv/bin/python -m liquidity_migration.data.history --input tests/fixtures/history/bybit-trades.csv --mapping tests/fixtures/history/bybit-mapping.json --output "$TASK_HISTORY/bybit.jsonl"
.venv/bin/python -m liquidity_migration.data.history --input tests/fixtures/history/bybit-trades.csv --mapping tests/fixtures/history/csv-mapping.json --output "$TASK_HISTORY/csv.jsonl"
.venv/bin/python -m liquidity_migration.data.history --input tests/fixtures/history/bybit-trades.parquet --mapping tests/fixtures/history/csv-mapping.json --output "$TASK_HISTORY/parquet.jsonl"
for SOURCE in bybit csv parquet; do
  engine/target/release/engine-tools backtest --config tests/fixtures/history/trade-probe.toml --source normalized --tape "$TASK_HISTORY/$SOURCE.jsonl" --execution trades --spread-bps 10 --slippage-bps 5 --participation 0.1 --instruments tests/fixtures/history/instruments.json --wal "$TASK_HISTORY/$SOURCE.wal" --trades "$TASK_HISTORY/$SOURCE-trades.jsonl" --report "$TASK_HISTORY/$SOURCE-report.json"
done
cmp "$TASK_HISTORY/bybit.wal" "$TASK_HISTORY/csv.wal"
cmp "$TASK_HISTORY/bybit.wal" "$TASK_HISTORY/parquet.wal"
for FORMAT in csv parquet; do
  .venv/bin/python -m liquidity_migration.data.history --input "tests/fixtures/history/bars.$FORMAT" --mapping tests/fixtures/history/bar-mapping.json --output "$TASK_HISTORY/bars-$FORMAT.jsonl"
  engine/target/release/engine-tools backtest --config tests/fixtures/history/bar-probe.toml --source normalized --tape "$TASK_HISTORY/bars-$FORMAT.jsonl" --execution bars --spread-bps 10 --slippage-bps 5 --participation 0.1 --instruments tests/fixtures/history/instruments.json --wal "$TASK_HISTORY/bars-$FORMAT.wal" --report "$TASK_HISTORY/bars-$FORMAT-report.json"
done
cmp "$TASK_HISTORY/bars-csv.wal" "$TASK_HISTORY/bars-parquet.wal"
```

Recorder input uses the committed real BTC tape excerpt and its fixed metadata snapshot. The existing market-tape reader supplies the schema-one row venue from the verified archive context:

```bash
RECORDER_SAMPLE=tests/market_tape/fixtures/host/bybit-linear/2026-08-30/00
.venv/bin/python -m market_tape rows tests/market_tape/fixtures/host/bybit-linear --hours 2026-08-30T00 --symbols BTCUSDT > "$TASK_HISTORY/recorder.jsonl"
engine/target/release/engine-tools backtest --config tests/fixtures/history/quoter.toml --source tape --execution books --tape "$TASK_HISTORY/recorder.jsonl" --instruments "$RECORDER_SAMPLE/_meta/instruments-20260830T003422Z.json.zst" --capital 100000 --wal "$TASK_HISTORY/books.wal" --report "$TASK_HISTORY/books-report.json"
rustup run 1.90.0 cargo test -p engine-tools normalized_book_facts_preserve_orders_fills_accounting_and_wal_bytes --manifest-path engine/Cargo.toml
```

Generate LONG features through the retained research implementation, with a complete chosen PIT root and explicit end-exclusive dates:

```bash
.venv/bin/python - "$DATA_ROOT" "$START_DATE" "$END_DATE" "$TASK_HISTORY/long-features.parquet" <<'PYCODE'
import sys
from dataclasses import replace
from liquidity_migration.rules.long_native import long_v12_profile
from liquidity_migration.research.backtest.long_native import build_long_research_inputs
config = replace(long_v12_profile(), start_date=sys.argv[2], end_date=sys.argv[3])
inputs = build_long_research_inputs(sys.argv[1], config=config)
inputs["features"].write_parquet(sys.argv[4])
print(inputs["pit_coverage_scope"], inputs["full_pit_universe_pass"])
PYCODE
```

This generates historical features, not managed producer grants or a full-engine production replay. Use `run_long_native_research` for the existing hourly reducer diagnostic; preserve its missing intraday execution and shaped-versus-graded labels.
