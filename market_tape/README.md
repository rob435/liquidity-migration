# Market Tape Engine (`market_tape`)

Standalone package for high-fidelity market data capture, zstd compression, Google Drive archiving, and point-in-time replay.

---

## 1. CLI Commands

```bash
# Configuration & Recording
python -m market_tape check  --config deploy/capture/bybit-linear.toml
python -m market_tape record --config deploy/capture/bybit-linear.toml --root /var/lib/liquidity-migration/forward-market

# Google Drive Packing
python -m market_tape pack --tape bybit-linear=/var/lib/liquidity-migration/forward-market \
                           --tape binance-usdm=/var/lib/liquidity-migration/forward-market-binance \
                           --remote-base gdrive:LiquidityMigration/market-tape \
                           --keep-hours 6

# Data Inspection & Analytics
python -m market_tape hours  SOURCE
python -m market_tape rows   SOURCE --hours 2026-09-02T20..2026-09-02T23 --symbols BTCUSDT --kinds public_trade
python -m market_tape bars   SOURCE --hours 2026-09-02T00..2026-09-02T23 --interval 1 --out bars.parquet
python -m market_tape book   SOURCE --hour 2026-09-02T22 --symbol BTCUSDT
```
* `SOURCE`: Local directory, remote rclone path (`rclone:<remote:path>`), or Google Drive tar directory.

---

## 2. Dynamic Capture Tiers & Sensors

| Tier Sensor | Trigger Rule | Use Case |
| :--- | :--- | :--- |
| `top_turnover` | 24h turnover ranks in top $N$; leaves below rank $M$, and no sooner than `sticky_hours` after it last ranked inside $N$ | Core liquid universe (LONG), held through the sleeve's hold. |
| `top_movers` | 24h price change ranks in top $N$ either way | Day's top gainers / losers. |
| `funding_below` | Predicted funding $\le -\text{threshold\_bp}$ | Extreme negative funding (CARRY / Exodus). |
| `funding_above` | Predicted funding $\ge +\text{threshold\_bp}$ | Overheated long crowds. |
| `turnover_surge` | 24h turnover $\ge \text{ratio} \times$ baseline snapshot | Early breakout / sudden volume pumps. |
| `price_move` | 24h price change $\ge \text{pct}\%$ either way | Large daily price expansion. |
| `price_burst` | Price moves $\ge \text{pct}\%$ over last $H$ hours | Rapid intraday expansion. |
| `volume_burst` | Volume $\ge \text{ratio} \times$ same hour yesterday | Abnormal hourly flow. |
| `oi_change` | Open interest moves $\ge \text{pct}\%$ over last $H$ hours | Rapid leverage accumulation / flush. |

---

## 3. Venue Market Differences

| Metric | Bybit Linear | Binance USD-M |
| :--- | :--- | :--- |
| **Recorded book** | `book:50` on every acting tier, `book:1` on the canary | **None.** Cross-venue reference only: ticker and trades |
| **Listed universe** | `status=Trading`, `contractType=LinearPerpetual`, `symbolType` in `""`/`innovation` — stocks, ETFs, commodities out | `status=TRADING`, `contractType=PERPETUAL` — `TRADIFI_PERPETUAL` out |
| **Book Chaining** | `u == last_u + 1` per topic; `type: snapshot` or `u == 1` re-bases; any other `u` is a gap until the next snapshot, and the recorder re-subscribes that topic. `seq` is recorded, never chained (§Book sequence contract) | `first_update_id` ($U$), `update_id` ($u$), `pu` |
| **Top of Book** | `book:1` stream | `bookTicker` stream, 434 KB/s for 20 names — costlier than the deep book |
| **Ticker Stream** | Real-time `tickers.<symbol>` | `@markPrice@1s` + 24h `ticker` |
| **Predicted Funding**| Real-time predicted rate for upcoming settlement | Last settled rate (reacts 1 period later) |
| **Public Trades** | Every individual fill | `aggTrades` (aggregated fill groups) |
| **Liquidations** | Per-symbol WebSocket stream | Single market-wide stream |

---

## 4. File Layouts & Archival

### Host Storage Layout (`/var/lib/liquidity-migration/forward-market/`)
```text
<YYYY-MM-DD>/<HH>/<SYMBOL>/segment-NNNNNN.jsonl.zst   Symbol data rolled at 64 MB raw
<YYYY-MM-DD>/<HH>/_meta/instruments-<stamp>.json.zst  Daily instrument specifications
<YYYY-MM-DD>/<HH>/_meta/tickers-<stamp>.json.zst      Daily market-wide ticker snapshot
manifest.jsonl                                         Receipts: row count, bytes, SHA-256
status.json                                            Health status updated every 30s
```
* `status.json` schema: `started_at_ns`, `last_receive_ns`, `disk_blocked`, `dropped_frames`, `disk_dropped_frames`, `shards[].connected`, `shards[].reanchors`, `shards[].resyncs`, `budget.projected_month_gb`, `budget.over`, `budget.shed`, `budget.shed_gb_month`.
* `status.json` is this unit's heartbeat: the watchdog reads its mtime and its `last_receive_ns` (`deploy/fleet_manifest.tsv`, limit 120 s). `started_at_ns` is when this process began recording; the watchdog measures silence and socket loss from it, so a recorder younger than the 120 s limit reads as starting up, not as a dead venue. The maintenance tick that writes it **must never walk the tape** — `Retention.writable()` is one `statvfs`. Retention itself is the `tape-retention` thread, one pass every `RETENTION_INTERVAL_SECONDS` (300); a pass stats each file once, reads free space once, and carries free space forward by the bytes it unlinks.

### Budget (`[budget]` in the capture config)

| Field | Meaning |
| :--- | :--- |
| `monthly_gb` | Inbound allowance for the month. Absent: the recorder only measures. |
| `shed` | `tier:feed` pairs in the order they are given up. Pairs not listed are never shed. |
| `act_every_minutes` (60) | One action per interval: a shed takes as many pairs, in order, as the projection needs; a restore returns the last pair shed. |
| `restore_below` (0.8) | A pair comes back only when its GB/month as measured at its shed, added to what is still subscribed, is under this fraction of `monthly_gb`. |

* The projection is the trailing day (or the uptime, if shorter) of bytes from the pairs still subscribed, scaled to a month. A shed pair's bytes in the window are left out.
* Over budget with every listed pair shed is a `WARNING` per action naming the overshoot: the config decides what else goes.
* `_meta` table snapshots are pruned by `retention_days` only, never for disk room.

### Hourly book anchoring

`connection.reanchor_books_each_hour` (default true) re-subscribes every book topic once per UTC hour. The venue answers a subscribe with a snapshot, so each hour of tape opens with one per symbol. The pass is bounded at `REANCHOR_TOPICS_PER_TICK` topics per maintenance tick and resumes where it stopped, so ~500 topics take about six minutes of the hour; each `REANCHOR_CHUNK` of topics is dropped and re-taken in one pair of messages.

| Property | Value |
| :--- | :--- |
| **Why** | A book delta means nothing without a snapshot. The hour is the archive's unit: one directory, one uploaded tar. Anchored hourly, any single hour replays on its own; anchored only at recorder start, replaying hour N means reading every hour since. |
| **Bytes** | One snapshot per book symbol per hour, about 2.5 KB each: under 1 GB/month for 500 names. |
| **Paid for it** | One round trip per symbol per hour with its book topic unsubscribed, which the snapshot row that follows marks. |
| **Counter** | `shards[].reanchors` in `status.json`. |

Only order-book topics are re-subscribed. A trade, ticker, or liquidation row means the same thing standing alone.

### Book sequence contract (Bybit)

One rule, shared by the live engine, the recorder, the tape rebuild and the backtester's tape reader, pinned by `tests/fixtures/bybit_orderbook_sequence.jsonl` (17 raw venue frames with the verdict each side must reach; `engine-marketdata` and `engine-tools` read it with `include_str!`, `tests/market_tape/test_bybit_sequence.py` reads the same file).

| Event on one topic | Live engine (`engine-marketdata/src/bybit/state.rs`) | Recorder (`market_tape/venues/bybit.py`, `record.py`) | Rebuild (`market_tape/book.py`, `engine-tools/src/backtest/tape.rs`, `quote_lab/book.py`) |
| :--- | :--- | :--- | :--- |
| Frame whose `type` is not `delta` (`snapshot`, or absent) | re-bases the book | `orderbook_snapshot` row, `sequence_gap=false` | replaces the book; valid |
| Delta with `u == 1` (venue restart) | re-bases | `orderbook_snapshot` row, `restart_snapshot=true` | replaces the book; valid |
| Delta with `u == last_u + 1` | applied | `orderbook_delta`, `sequence_gap=false` | applied |
| Delta with any other `u` (jump or regression) | `Resync(SequenceGap)`: drops and refreshes that one topic | `sequence_gap=true`; re-subscribes that one topic within ~0.1 s, one outstanding per topic, counted in `shards[].resyncs` | invalid until the next snapshot |
| Delta before any snapshot | `Resync(DeltaBeforeSnapshot)` | `sequence_gap=true`; the subscription's own snapshot re-bases | refused; invalid |
| Deltas after a gap, `u + 1` or not | resync until a snapshot | `sequence_gap=true` until a snapshot | refused until a snapshot |
| `seq` (cross-topic sequence) | recorded on `Depth.seq` / `Quote.seq`; not a continuity rule | `cross_sequence` / `previous_cross_sequence` on every row; not a rule | not read |
| `orderbook.1` | own state per (symbol, depth); the venue pushes it as snapshots | own sequence state per topic | one `Book` per symbol and depth |

Evidence boundary: the recorded hour `tests/market_tape/fixtures/host/bybit-linear/2026-08-30/00` holds 1,429 depth-50 deltas (BTCUSDT 1,179 over 53 s, PENDLEUSDT 250 over 123 s), every one `u == previous_update_id + 1`, zero jumps, zero `seq` regressions; its 225 depth-1 rows are all snapshots.

### Google Drive Layout
Uploaded hourly at :10 past the hour:
```text
LiquidityMigration/market-tape/<tape>/YYYY/MM/DD/<YYYY-MM-DD>T<HH>Z.tar
```

### Local Sliding Window (`market_tape pack --keep-hours`)

The Drive is the archive; the host holds the last `--keep-hours` of tape plus whatever has not shipped yet. The same `pack` run that uploads also deletes, after its uploads, in `prune_shipped`.

| Rule | Value |
| :--- | :--- |
| Licence to delete | The hour's `remote_path` is in `<state-dir>/uploaded-tapes.jsonl`, which is written only after the Drive's size and MD5 matched the upload. |
| When | `now >= hour_end + keep_hours * 3600`. The deployed unit passes `--keep-hours 6`. |
| What goes | Every `*.zst` under the hour directory except `_meta/`; empty directories after it. |
| What stays | `_meta/` snapshots (the day's point-in-time tables; the recorder prunes them by `retention_days`), any hour not in the ledger, any hour inside the window. |
| Receipt | One `segment_deleted` row per file in the recorder's `manifest.jsonl`, `reason=shipped`, with the `remote_path`. |
| Stamp | `keep_hours`, `pruned_hours`, `pruned_bytes` in `market-tape-upload.last-success`. |

* An hour the Drive did not confirm is **never** deleted here, whatever its age. The recorder's `retention_days`, `max_disk_gb` and `min_free_disk_gb` remain the backstop for a tape the Drive is not taking.
* `Retention.prune` on the recorder and `prune_shipped` in the uploader may unlink concurrently; each treats a file the other took first as not its own.
