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
                           --remote-base gdrive:LiquidityMigration/market-tape

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
| `top_turnover` | 24h turnover ranks in top $N$; leaves below rank $M$ | Core liquid universe (LONG). |
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
| **Book Chaining** | Monotonic `update_id`; resets on snapshot | `first_update_id` ($U$), `update_id` ($u$), `pu` |
| **Top of Book** | `book:1` stream | `bookTicker` stream |
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
* `status.json` schema: `last_receive_ns`, `disk_blocked`, `dropped_frames`, `disk_dropped_frames`, `shards[].connected`, `budget.projected_month_gb`, `budget.over`, `budget.shed`, `budget.shed_gb_month`.

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

### Google Drive Layout
Uploaded hourly at :10 past the hour:
```text
LiquidityMigration/market-tape/<tape>/YYYY/MM/DD/<YYYY-MM-DD>T<HH>Z.tar
```
