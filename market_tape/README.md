# market_tape

Record the public market tape of crypto perpetual venues, ship it to Google
Drive one hour at a time, and read it back as typed rows, rebuilt books, and
bars. Bybit linear and Binance USD-M perpetuals are the venues today.

The package stands alone. It imports nothing from the trading repository it
lives in (a test enforces this), depends only on the standard library plus
`websocket-client` (and `polars` for bars), and can move to its own repository
with `git subtree split -P market_tape` plus `tests/market_tape/`.

## Commands

```bash
python -m market_tape check  --config deploy/capture/bybit-linear.toml       # validate, no network
python -m market_tape record --config deploy/capture/bybit-linear.toml --root /var/lib/liquidity-migration/forward-market
python -m market_tape pack   --tape bybit-linear=/var/lib/liquidity-migration/forward-market \
                             --tape binance-usdm=/var/lib/liquidity-migration/forward-market-binance \
                             --remote-base gdrive:LiquidityMigration/market-tape --state-dir STATE --stamp-file STAMP
python -m market_tape hours  SOURCE
python -m market_tape rows   SOURCE --hours 2026-08-30T00..2026-08-30T03 --symbols BTCUSDT --kinds public_trade
python -m market_tape bars   SOURCE --hours 2026-08-30T00..2026-08-31T00 --interval 1 --out bars.parquet
python -m market_tape book   SOURCE --hour 2026-08-30T00 --symbol BTCUSDT --at 1788049490035742267
```

`SOURCE` is a recorder root on a host, a directory laid out like the Drive
folder (`YYYY/MM/DD/<day>T<HH>Z.tar`), or `rclone:<remote:path>` to read the
Drive through a local cache.

## What gets recorded

A recorder records one venue from one TOML config: a list of tiers, each a
universe of symbols and the feeds to take for them. A symbol in several tiers
gets the union of their feeds; each venue topic is subscribed once. The config
format, the feed vocabulary (`book:<levels>`, `trades`, `ticker`,
`liquidations`, `kline:<interval>`, `open_interest:<seconds>`), and the
universe kinds are documented in [`config.py`](config.py).

The ticker is the sensor. It carries every listed name's funding rate, open
interest, price, 24h turnover and change, and best bid and ask, as the venue
pushes them, and it is cheap. The live universes read it as it is written:

| Kind | Promotes a name when | Meant for |
| --- | --- | --- |
| `top_turnover` | its 24h turnover ranks in the top `top`; it leaves below rank `leave_top` | LONG's liquid universe, following the action |
| `top_movers` | the size of its 24h price change ranks in the top `top`, either way; same `leave_top` | the day's movers |
| `funding_below` | its funding rate is at or below `-threshold_bp` | the crowd fee (funding) CARRY and Exodus trade |
| `funding_above` | its funding rate is at or above `threshold_bp` | longs paying up: squeezes, the short side of the carry |
| `turnover_surge` | its 24h turnover is `ratio` times what the day's table snapshot showed | a pump starting in a name outside the top |
| `price_move` | its 24h price change is at least `pct` either way | large moves, up or down |
| `price_burst` | its price moved `pct` either way over the last `window_hours` | the pop as it happens |
| `volume_burst` | its 24h turnover grew, over the last `window_hours`, by `ratio` average windows | an hour trading far beyond the same hour a day ago |
| `oi_change` | its open interest moved `pct` either way over the last `window_hours` | leverage piling in or being flushed |

A name that qualified stays for `sticky_hours` after its last qualifying
observation; the two ranked kinds use `leave_top` instead. The windowed kinds
compare against a ticker sample the recorder took `window_hours` earlier (one
sample a minute is kept, as far back as the longest window), so they see
nothing until it has run that long. Promotion changes the live subscription in
place: topics are added to and removed from the open connections, and a
connection only reconnects when the venue drops it. `listed` follows the daily
table snapshot; `symbols` and `file` are fixed.

Every received byte is metered per tier and per feed (`status.json` →
`bytes`). With a `[budget]` the recorder projects a month from its last day of
bytes and, when the projection is over `monthly_gb`, gives up the configured
`tier:feed` pairs in order, one an hour, restoring them in reverse once under
`restore_below` of the allowance. The host watchdog warns while a recorder is
over budget. Measured on the host on 2026-09-02: one Bybit deep book costs
0.5 to 1 GB a day inbound for a liquid name, and top of book plus trades for
660 quiet names cost about as much as the deep books of the eighty busiest.

The host configs are under `deploy/capture/`. `examples/bybit-full-universe.toml`
is the configuration for a machine with unbounded bandwidth and disk: one
tier, every listed perpetual, every feed, no budget.

## The row contract

Every row is one JSON object on one line, sorted by `local_receive_ts_ns`, the
recorder's wall clock at receipt. Kinds: `orderbook_snapshot`,
`orderbook_delta`, `public_trade`, `ticker`, `liquidation`, `kline`. The exact
fields, the typed rows they parse into, and the schema history are in
[`schema.py`](schema.py); the writer builds rows only through its constructors
and the reader turns them back with `parse_row`. Prices and sizes inside book
levels are the venue's decimal strings; the typed rows convert to float.

Venue differences that matter when reading:

| | Bybit | Binance |
| --- | --- | --- |
| Book chaining | `update_id` increases; a `snapshot` message or `update_id == 1` restarts | `first_update_id` (U), `update_id` (u), `previous_update_id` (pu); the REST snapshot's `lastUpdateId` anchors the chain |
| Top of book | `book:1` stream, up to every 10 ms | `bookTicker` stream, on change |
| Ticker | one stream with last, mark, index, funding, open interest, best bid and ask, 24h turnover | `markPrice@1s` (mark, index, funding) and the 24h `ticker`; best bid and ask come from `book:1`; open interest only by REST poll |
| `funding_rate` | the upcoming (predicted) rate for the next settlement | the last settled rate; a `funding_below` tier therefore reacts one settlement later than on Bybit |
| Trades | every public trade | aggregate trades (one row per aggressor fill group) |
| Liquidations | per-symbol stream | one all-market stream |

`book.Book` applies the right chaining rule from the row's `venue`.

## Layouts

On the recording host, under the root:

```text
<day>/<HH>/<SYMBOL>/segment-NNNNNN.jsonl.zst   one symbol, one UTC hour, rolled at the size cap
<day>/<HH>/_meta/instruments-<stamp>.json.zst  the venue's instrument table, as of that moment
<day>/<HH>/_meta/tickers-<stamp>.json.zst      the venue's ticker table, as of that moment
manifest.jsonl                                 one receipt per compressed file: rows, span, bytes, SHA-256
status.json                                    the recorder's own health, rewritten on a timer
```

On the Drive, one uncompressed tar per finished hour, `MANIFEST.json` first
(every member's bytes, SHA-256, row count, time span), checked against the
Drive's own hash before the hour is marked shipped:

```text
LiquidityMigration/market-tape/<tape>/YYYY/MM/DD/<day>T<HH>Z.tar
LiquidityMigration/market-tape/<tape>/YYYY/MM/DD/<day>.legacy.tar   a day recorded before the hourly layout
```

The host keeps a retention window (days, total bytes, free-disk floor, all in
the config); the Drive is the copy that lasts.

## Reading

`load.open_source` detects the layout; `load.iter_rows(source, hours, ...)`
streams typed rows across symbols in receive order; `book.Book` rebuilds one
symbol's book and reports best bid and ask, mid, spread, depth within a
distance, and imbalance; `bars.build_bars` turns any row stream into
fixed-interval bars (trades, volume by aggressor, OHLC, VWAP, top of book,
mark, index, funding, open interest, liquidations) as a polars frame.

`tests/market_tape/fixtures/` holds one small real hour of Bybit tape in both
layouts with its expected numbers; `test_fixture_hour.py` is the frozen-schema
regression. Rebuild it with `fixtures/build_fixture.py`.

## Status file

`status.json` is what the host watchdog reads: `last_receive_ns`,
`disk_blocked`, `dropped_frames`, `disk_dropped_frames`,
`shards[].connected`, and `budget.over`. It also lists each tier's symbol
count (and names when few), its feeds and whatever the budget has shed from
it, the shards with their reconnect counts, the bytes received in the last day
by tier and by feed, the month's projection, and the venue.
