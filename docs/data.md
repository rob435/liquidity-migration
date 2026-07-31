# Data

Where research data lives, what its timestamps mean, how point-in-time membership works, and how to
refresh a root. Operational account roots are a separate trust domain, listed last. Never point an
order-writing runtime at a research root, and never use a demo ledger as a research population source.

## Research roots

| Root | Venue | Builder |
| --- | --- | --- |
| `~/SHARED_DATA/bybit_full_pit` | Bybit USDT linear perps | [`scripts/build_full_pit_bybit.sh`](../scripts/build_full_pit_bybit.sh) |
| `~/SHARED_DATA/binance_full_pit` | Binance USD-M perps | [`scripts/build_full_pit_binance.sh`](../scripts/build_full_pit_binance.sh) |

Mutable local datasets. Not committed, not holdouts by virtue of their name.

```bash
BYBIT_START=2021-01-01 BYBIT_END=YYYY-MM-DD  bash scripts/build_full_pit_bybit.sh
BINANCE_START=2019-09-01 BINANCE_END=YYYY-MM-DD bash scripts/build_full_pit_binance.sh
```

**Every `--end` and `*_END` in this repository is exclusive.** The named day is not in the output.
Both scripts are environment-only and refuse positional arguments. Other variables:
`BYBIT_FULL_ROOT`/`BINANCE_FULL_ROOT`, `BYBIT_CATEGORY` (`linear`, enforced), `BINANCE_WORKERS` (24),
`BINANCE_JOB_BATCH_SIZE` (48), `BINANCE_MAX_FAILURE_RATIO` (0 — one failed download aborts). Bybit
stages: `archive-manifest` → `archive-download-klines-1h-api` → `validate-manifest` → `download-data`
ancillaries. Binance: `binance_vision build-binance-oos` (monthly plus daily tail, klines and manifest
published as one atomic pair) → `download-binance-proxy`.

- `symbol=` components are percent-encoded by [`symbol_codec.py`](../liquidity_migration/symbol_codec.py). Decode with that module; never read the directory name as an exchange symbol. ASCII symbols are unchanged.
- A Binance build interrupted mid-publication leaves `.binance_vision_publish_incomplete.json`, and the next build refuses before any network access. Read the marker's staging and backup paths and recover deliberately.
- The `bybit_render_1m` and `binance_vision_alt` plans and their fetchers were removed 2026-07-21. Do not recreate them from old documents.

## Coverage census, 2026-07-31

Counted 2026-07-31. A directory existing is not a panel, and a partition count of zero is not an
empty dataset — some roots are keyed by symbol. Recount before designing a claim.

| Tier | Datasets | Reality |
| --- | --- | --- |
| A — deep, both venues | `bybit/{klines_1h,funding,premium_index_1h,mark_price_1h,index_price_1h,archive_trade_manifest}`; `binance/{klines_1h,archive_trade_manifest}`, `binance_usdm_funding`, `binance_usdm_{premium_index,mark_price,index_price}_1h` | Bybit 2021-01-01 → 2026-07-27, 2034 days, 955 symbols. Binance 2019-09/2020-01 → 2026-07-27, 2400–2513 days, 823–828 symbols. 741 symbols common to both kline roots. |
| B — deep, Bybit only | `bybit/open_interest` | 2021-01-01 → 2026-07-27, 2034 days, 694 symbols. |
| C — wide, shallow, Binance only | `binance_usdm_open_interest`, `binance_usdm_taker_flow_1h` | 679 symbols but only 78–80 days, from 2026-04-27. |
| D — **not a panel** | `bybit/taker_flow_5m`, `bybit/tick_ohlc_1m` | 2023-03-29 → 2026-05-24, 401 symbols, **median 11 days each** (1–78). Event windows; no cross-sectional flow or microstructure study can be built on them. |
| E — per-symbol layout, not date-partitioned | `bybit/positioning_lsr` (66 files, 1.2 MB), `binance_usdm_metrics_5m` (373, 36 MB), `binance/klines_5m` (686, 3.1 GB) | These hold real data. They are keyed by symbol, not `date=`, so a partition-name count reports zero. Count files, not partitions. |

## Timestamps

Every research field is **milliseconds**. Every account-journal clock is **nanoseconds** and ends in
`_ns`. No field is ambiguous between the two.

| Market-data field | Meaning |
| --- | --- |
| `klines_1h.ts_ms` | Bar **open**, UTC ms. The bar closes at `ts_ms + 1h`; a decision on that bar is stamped `ts_ms + 1h`. |
| daily panel `ts_ms` | 00:00 UTC of the trading day. `close` is the day's last hourly close; `first_bar_close` is the 00:00→01:00 bar close — the fill price for an entry decided at the previous day's EOD. Lags join on explicit `ts_ms ± 1 day`, never positional `shift(1)`, so a symbol with a missing day gets null instead of a multi-day move labelled 1d. |
| `funding.ts_ms` | Settlement instant, known only at or after it, so every join to funding is backward as-of. `funding_interval_min` is per symbol and has shortened over time. |
| `date=` partition | UTC date derived from `ts_ms`. `volume_events_pit.py` raises if a declared `date` disagrees with its `ts_ms`. |
| `residual_momentum.parquet` | `(symbol, ts_ms, residual_momentum, is_provisional)`. `is_provisional=true` is a tail row a later refresh may change. |

Every canonical journal event carries `wall_ts_ns` (owner's wall clock at append), `monotonic_ns`
(local sequencing and latency), `exchange_ts_ns` (venue timestamp when supplied, else `0` — **zero
means absent, not Unix epoch**), and a root-global `sequence`, the durable ordering authority. Venue
latency, fill price, fee, close and P&L come from acknowledgement/fill/P&L events — never from a
planning timestamp below, never from file-write time.

| Strategy projection | Meaning |
| --- | --- |
| `signal_ts_ms` | Closed kline boundary that caused the decision. Part of component identity; hourly paths align to the hour. No execution meaning. |
| `entry_ready_ts_ms` | Earliest time the signal may be acted on. Eligibility, not a fill. |
| `entry_target_ts_ms` | Wall time of the first accepted non-zero component target. Planning clock; does not start protection or max hold. |
| `entry_ts_ms` | On `canonical_strategy_trade_rows`: local receive time of the first journal-confirmed fill for the component. Null before a fill, and when an aggregate same-symbol fill cannot be attributed. |
| `max_hold_duration_ms` | Hold duration published with the entry target — a duration, not an absolute deadline. |
| `max_hold_deadline_ts_ms` | First attributable fill time plus `max_hold_duration_ms`. Null before a fill. |
| target parquet `ts_ms` | Local projection write time. Orders projection writes only; not authoritative over `sequence`. |

Stop and take-profit prices derive from confirmed fill VWAP, never a decision reference price;
[`tests/test_account_strategy_state.py`](../tests/test_account_strategy_state.py) pins this. Archived
pre-account-kernel rows used `entry_ts_ms` for an actual fill time or, in paper, a submit-time
idealization, and the 2026-05-25 WAVESUSDT rows came from a retired path that decoded an order-link
signal timestamp into that field — making the position look hours older than the venue fill. Do not
merge those rows with current projections unlabelled.

## Point-in-time membership

```text
{root}/archive_trade_manifest/date=YYYY-MM-DD/part.parquet
```

Partitioned by date only. Bybit columns: `date`, `symbol`, `url`, `source`, `membership_source`,
`membership_inferred`, `first_archive_observed_date`, `v5_observed_launch_date`,
`membership_provenance_limitation`; Binance omits the last two. Bybit rows come from two classes —
**archive-observed** (a public trade-archive object existed for that symbol/day) and
**current-listing-derived** (inferred from a currently `Trading` v5 instrument and its launch date
through the build boundary). The second closes archive lag but observes no historical suspension or
delisting, so "full PIT" means complete under this manifest, not perfect venue history.
`membership_inferred` marks which is which; `v5_observed_launch_date` separates reused ticker
incarnations without upgrading an inferred row to an observation.

**The rule.** A symbol may enter the universe on day D only if the manifest lists it as a member on D,
and that filter runs *before* any rolling feature or cross-sectional rank —
`filter_klines_to_pit_membership` in [`volume_events_pit.py`](../liquidity_migration/volume_events_pit.py).
A later filter can stop an ineligible symbol trading but cannot undo its influence on ranks, cut-offs,
and rolling state.

**Trading-day key.** A daily-close signal for trading day D is stamped 00:00 UTC on D+1, so the
membership day is `date(signal_ts_ms - 1ms)`, not the stamp date. `latest_signal_trading_day()` in
[`pit_coverage.py`](../liquidity_migration/pit_coverage.py) is `today_utc - 1`. Hourly bars key on
their own bar-stamp date, unadjusted.

**Staleness.** `download-data` does not touch the manifest unless `--refresh-manifest` is passed, so a
freshly downloaded root can carry stale membership and hard-reject recent signals with
`pit_membership_fail`:

```bash
python -m liquidity_migration --data-root ROOT archive-manifest --start YYYY-MM-DD --end YYYY-MM-DD
python -m liquidity_migration.binance_vision validate-manifest --data-root ROOT
```

`validate-manifest` fails on missing kline coverage (≥20 hourly bars per symbol-day) without deleting
membership rows. `filter-manifest` conforms a manifest to observed klines and is correct only where
archive klines *are* the declared membership source (Binance Vision); it refuses provenance-bearing
Bybit manifests, because deleting an uncovered row would make the check self-certifying. The LONG
research runner always measures manifest/kline agreement and records `full_pit_universe_pass`, with no
switch to disable it; a non-passing run can be a current-universe or data diagnostic, not a
historical-universe performance claim. The CONTINUOUS equity runner reads the kline root, which
establishes nothing about historical membership. Changing the population treatment after seeing a
result does not rescue the original claim.

## Refresh

[`scripts/ops.sh research-refresh`](../scripts/ops.sh) →
[`scripts/research_refresh.sh`](../scripts/research_refresh.sh) → `scripts/research_refresh.py`.
Offline: no orders, no private venue APIs, no VPS checkout, no promotion.

```bash
scripts/ops.sh research-refresh plan --end 2026-07-31   # print, mutate nothing
scripts/ops.sh research-refresh run  --end 2026-07-31   # append-first (tail)
scripts/ops.sh research-refresh run  --end YYYY-MM-DD --start YYYY-MM-DD \
  --data-mode canonical --run-id RUN-ID                 # full rebuild
```

| Phase | `tail` behavior (default `--overlap-days 7`) |
| --- | --- |
| Bybit membership | Rebuild the independent manifest over its canonical history. A narrow scan would silently lose delisted names, so membership is never tail-only. |
| Bybit klines | Recheck the trailing overlap, fetch missing or sub-20-bar partitions, validate the whole root against the manifest. Failure triggers a full missing-only scan. |
| Binance klines/membership | Append strict current-month daily archives; crossing an unmaterialized month falls back to the atomic monthly builder. Ancillary data re-fetches from the stalest dataset boundary minus the overlap. |
| Residual momentum | Recompute the overlap, require stable rows unchanged, atomically append the provisional tail. A mismatch fails closed; `--force-rmom-full-rewrite` is the explicit recovery path, not a waiver. |
| Backtests | Reuse an identical completed run-scoped report, else recompute the fixed window from a clean sleeve directory. No incremental replay is appended to an old account journal. |

`--data-mode canonical` invokes both full-PIT builders and regenerates residual momentum from its fixed
causal start. Every selected dataset must expose a partition for `end - 1 day`, manifest validation must
pass on all roots, and each backtest summary must match the frozen start/end at 1x modeled exposure.

Artifacts land in `reports/research-refresh/<run-id>/`: `manifest.json` (code/config/root/window
identity), `events.jsonl` (append-only start/failure/success/resume ledger), `logs/`,
`summary.<hash>.json`, `backtests/<venue>/`, `reconciliation/`. Reusing a `--run-id` skips only a step
whose exact command fingerprint succeeded and whose artifact still exists; changed windows, roots,
source commits, or configuration are refused under an existing ID. `research-refresh reconcile
--run-dir ... --demo-account-root ... --paper-account-root ...` compares demo, paper and backtest on
the grain `(sleeve, active component, symbol, causal signal_ts_ms)` from quiescent read-only account
snapshots; agreement supports a structural entry-key claim and nothing else — not fills, slippage,
fees, or P&L.

## Operational roots

Exact VPS paths come from the host env files, not from this document:
`/etc/liquidity-migration/account-execution.env` and
`/etc/liquidity-migration/account-paper-execution.env`. Each route names its own
`ACCOUNT_EXECUTION_ROOT` (canonical journal), `ACCOUNT_INTENT_INBOX_ROOT` (target inbox) and
`ACCOUNT_CAPTURE_ROOT` / `ACCOUNT_PAPER_CAPTURE_ROOT` (market capture); demo and paper roots are
absolute, pairwise-disjoint and non-nested. The paper mirror additionally reads
`DEMO_ACCOUNT_EXECUTION_ROOT` and `DEMO_ACCOUNT_CAPTURE_ROOT`. The demo account owner alone mutates
Bybit; the paper owner alone advances the deterministic paper account. Their journals own lifecycle
and accounting state, and Parquet views are rebuildable projections. Strategy `DATA_ROOT` directories
hold signal inputs, caches and cycle telemetry — not position or P&L authority. Dataset and account
roots use persistent `flock(2)` leaves: ownership is the kernel lock on the open file description, not
the file's contents, and release or crash recovery never unlinks the leaf. Never delete a lock file as
"stale".

[`deploy/sleeves.env`](../deploy/sleeves.env) is the repository ceiling for target producers, and a host
override can only narrow it. Turning a producer off stops publication; it does not erase its last
accepted target or flatten the account. Inspect with `scripts/ops.sh status` (runtime topology) and
`scripts/ops.sh venue-accounting` (stopped demo accounting interval). Research evidence and runtime
accounting stay separate; neither upgrades the other. See [`AGENTS.md`](../AGENTS.md) for how
evidence is graded and [`architecture.md`](architecture.md) for module ownership.
