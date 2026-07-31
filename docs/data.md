# Data

Where research data lives, what its timestamps mean, how point-in-time membership works, and how to
refresh a root. Operational account roots are a separate trust domain, listed last. Never point an
order-writing runtime at a research root, and never use a demo ledger as a research population source.

## Research roots

| Root | Venue | Builder |
| --- | --- | --- |
| `~/SHARED_DATA/bybit_full_pit` | Bybit USDT linear perps | [`scripts/data/build_full_pit_bybit.sh`](../scripts/data/build_full_pit_bybit.sh) |
| `~/SHARED_DATA/binance_full_pit` | Binance USD-M perps | [`scripts/data/build_full_pit_binance.sh`](../scripts/data/build_full_pit_binance.sh) |

Mutable local datasets. Not committed, not holdouts by virtue of their name.

```bash
BYBIT_START=2021-01-01 BYBIT_END=YYYY-MM-DD  bash scripts/data/build_full_pit_bybit.sh
BINANCE_START=2019-09-01 BINANCE_END=YYYY-MM-DD bash scripts/data/build_full_pit_binance.sh
```

**Every `--end` and `*_END` in this repository is exclusive.** The named day is not in the output.
Both scripts are environment-only and refuse positional arguments. Other variables:
`BYBIT_FULL_ROOT`/`BINANCE_FULL_ROOT`, `BYBIT_CATEGORY` (`linear`, enforced), `BINANCE_WORKERS` (24 —
the ThreadPoolExecutor's `max_workers`, the actual concurrency knob), `BINANCE_JOB_BATCH_SIZE` (48 — a
scheduling/memory bound, not parallelism: it sizes the slice of jobs submitted at a time,
`binance_vision.py:1493-1494, :953-954`, and doubles as the pending-frame flush threshold at `:1514`),
`BINANCE_MAX_FAILURE_RATIO` (0 — one failed download aborts). Bybit
stages: `archive-manifest` → `archive-download-klines-1h-api` → `validate-manifest` → `download-data`
ancillaries. Binance: `binance_vision build-binance-oos` (monthly plus daily tail, klines and manifest
published as one atomic pair) → `download-binance-proxy`. Monthly history and the bounded current-month
daily tail are assembled in **one** staging generation specifically so daily-only new contracts cannot
be dropped before a later top-up, and membership is then derived from their *combined* ≥20-bar coverage
(`scripts/data/build_full_pit_binance.sh:56-59`). Splitting that into a cheaper two-pass reintroduces the
survivorship hole it closes.

- `symbol=` components are percent-encoded by [`symbol_codec.py`](../liquidity_migration/core/symbol_codec.py). Decode with that module; never read the directory name as an exchange symbol. ASCII symbols are unchanged.
- Unsupported, ambiguous, or path-like identifiers fail with `SymbolIdentityError` *before* any root mutation. `normalize_exchange_symbol` (`symbol_codec.py:14-36`) NFC-normalizes, then rejects: anything that is not one non-blank untrimmed `str`; any value whose NFKC form differs from its NFC form (compatibility/confusable); identifiers over 192 UTF-8 bytes; any character outside Unicode categories L/N; any character where `c != c.upper()`. Two upstream identifiers that normalize to the same key are also rejected (`:73, :91-96`). A build aborting on one odd venue symbol is that guard, not a bug.
- An *ordinary* Binance publication failure rolls back — the new trees are quarantined, the backups restored, a `prior_presence` invariant checked per dataset — and then `marker_path.unlink(missing_ok=True)`: the root is intact and **no** marker is left. Retry. `.binance_vision_publish_incomplete.json` survives only a hard process kill or an *incomplete* rollback, where `RuntimeError("Binance publication failed and rollback was incomplete: ...")` is raised before the unlink (`binance_vision.py:1241-1276`, docstring `:1181-1185`). A marker that is present therefore means one of those two things; the second needs the backup root inspected, not just a retry. The next build refuses before any network access — read the marker's staging and backup paths and recover deliberately.
- Publication runs inside the per-dataset `exclusive_file_lock`s, acquired in sorted dataset order with `stale_seconds=21_600` (`:1219-1227`, via `storage.dataset_lock_path`), so a build can legitimately appear to hang while blocked on a dataset lock. A concurrent publisher that already owns the marker is refused outright — "Binance build REFUSED: another publication owns {marker_path}" (`:1210-1215`) — and that loser deletes its own staging/backup without touching live data.
- **Universe-shrink gate**, three distinct refusals. Two fire before any download, off the discovery inventory: `historical_dropped_symbols` → "binance OOS build REFUSED: current daily inventory cannot replace missing monthly history for N persisted symbols" (`:1464-1470`, only when a daily tail is staged) and `dropped_symbols` → "binance OOS build REFUSED: combined archive universe (N symbols) shrank vs the persisted klines_1h (M symbols) — K symbols would be stranded" (`:1471-1478`). The third fires after staging: `missing_persisted` → "binance OOS build REFUSED: verified monthly-plus-daily staging lost N persisted symbols" (`:1553-1558`). `--allow-degraded` / `allow_degraded=True` is the only override (`:1359, :1632`) and `scripts/data/build_full_pit_binance.sh` never passes it — overriding means invoking `binance_vision build-binance-oos` by hand, and overwriting a wide root with a narrow one after a transient S3 listing shortfall is exactly what the gate exists to stop. Separately, the staged `klines_1h` + `archive_trade_manifest` pair is verified from persisted Parquet, not in-memory inputs (`_verify_staged_binance_datasets`, `:1047-1052`), and re-verified against the live root after the rename (`after_publish`, `:1608-1614`).
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

Those top-level clocks cannot supply an execution-latency measurement. Every venue-sourced event is
stamped `wall_ts_ns = max(local_receive_ts_ns, 1)` (`account_kernel.py:2687, 2717, 2787, 2836, 2869,
2912, 3039, 3121, 3584, 3624, 3675, 3717, 3758`), so the top-level wall clock *is* the local-receive
clock. Fill, acknowledgement, P&L, protection, order-status and venue-snapshot payloads carry their own
clocks **inside the payload** — `exchange_ts_ns` plus `local_receive_ts_ns` (fill / pnl / protection /
order_status / venue_snapshot) or `local_ack_ts_ns` (ack, ack_observation) — and latency and TCA are
measured from those: payload `local_receive_ts_ns` minus payload `exchange_ts_ns`.

The canonical strategy read model joins sleeve target metadata to execution anchors reconstructed from
the journal; target clocks and fill clocks stay on separate planes, which is why some columns are
null-before-fill and others are always populated, and why `entry_target_*` and `entry_fill_*` prefixes
sit side by side (`account_strategy_state.py:1660-1705`).

| Strategy projection | Meaning |
| --- | --- |
| `signal_ts_ms` | Closed kline boundary that caused the decision. Part of component identity and republished on the target (`account_strategy_state.py:1994`, `:1773`); hourly paths align to the hour. No execution meaning. |
| `entry_ready_ts_ms` | Earliest time the signal may be acted on: fixed-delay entries use `signal_ts_ms + entry_delay`, sniper/retrace entries their first qualifying boundary or deadline. Eligibility, not a fill. Does not reach the read model. |
| `entry_target_ts_ms` | Wall time of the first accepted non-zero component target (`entry_event.wall_ts_ns // 1_000_000`, `:1663`). Planning clock; does not start protection or max hold. May precede a fill. |
| `entry_ts_ms` | On `canonical_strategy_trade_rows`: local receive time of the first journal-confirmed fill for the component — `max(payload.local_receive_ts_ns, event.wall_ts_ns) // 1_000_000` (`:1649-1655`). Null before a fill, and when an aggregate same-symbol fill cannot be attributed. |
| `max_hold_duration_ms` | Hold duration published with the entry target — a duration, not an absolute deadline. |
| `max_hold_deadline_ts_ms` | First attributable fill time plus `max_hold_duration_ms`. Null in **two** cases: no attributable entry fill, and no duration available from the entry target's metadata (`:1664-1668`, `:1766-1772`). |
| `max_hold_deadline_basis` | Which of the two: `unavailable_without_attributable_entry_fill`, `unavailable_without_duration`, or `entry_first_fill_plus_<basis>`. A filled position with a null deadline usually means the producer published none of `max_hold_duration_ms` / `max_hold_hours` / `max_hold_days` (`_max_hold_duration_from_entry_target`, `:1974-1984`). |
| target parquet `ts_ms` | Local projection write time. Orders projection writes only; not authoritative over `sequence`. |

Fill-derived lifecycle fields are never replaced by, or derived from, target acceptance time.
`entry_ts_ms` is never backfilled with the target-acceptance clock, and `max_hold_deadline_ts_ms` is
never derived from target acceptance — only from the attributable first fill plus the declared
duration. This is enforced, not aspirational: `account_strategy_state.py:1662` marks the target-plane
clocks as never lifecycle fallbacks, and `:1776-1788` re-applies the four `max_hold_*` fields *after*
the `**lifecycle["planning_metadata"]` splat under "Never let target metadata overwrite fill-derived
lifecycle fields." A null `entry_ts_ms` beside a populated `entry_target_ts_ms` is not licence to use
the target time.

Passing `max_hold_deadline_ts_ms` makes the sleeve publish a replacement zero target
(`continuous_demo.py:811-818`, `long_native_event_demo.py:1086-1128`); it asserts nothing about when
the account owner fills the resulting aggregate order. The actual close is a later journal fill event,
surfaced as `exit_ts_ms` / `closed_at_ms` (`account_strategy_state.py:1725-1726`).

Two ordering invariants, unenforced by code but load-bearing when auditing a projection or a replay:
`signal_ts_ms` must not be later than the strategy decision that cites it (it holds by construction —
it is the closed-bar boundary), and `entry_target_ts_ms` must not precede the signal decision that
produced it. `entry_target_ts_ms` comes from an independent journal clock on a locally-originated
target event (`account_kernel.py:2053`), so a row violating the second is a genuine defect.

*Component* stop and take-profit prices derive from confirmed fill VWAP, never a decision
reference price (`protection_engine.py:123-151`);
[`tests/strategy/test_account_strategy_state.py`](../tests/strategy/test_account_strategy_state.py) pins this. The
account owner's venue-native entry stop is the other plane and is anchored to the decision
reference price, because it is armed in the same `place_order` call as the entry and no fill exists
yet ([`architecture.md`](architecture.md), *Venue-native protection*). Four
distinct legacy semantics for `entry_ts_ms` exist in archived roots: an actual fill time; in paper, a
submit-time idealization; the planning/target-acceptance clock now exposed as `entry_target_ts_ms`
(which may precede a fill); and the 2026-05-25 WAVESUSDT rows from a retired path that decoded an
order-link signal timestamp into that field — making the position look hours older than the venue fill.
Do not merge those rows with current projections unlabelled.

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
`filter_klines_to_pit_membership` in [`volume_events_pit.py`](../liquidity_migration/data/volume_events_pit.py).
A later filter can stop an ineligible symbol trading but cannot undo its influence on ranks, cut-offs,
and rolling state.

**Trading-day key.** A daily-close signal for trading day D is stamped 00:00 UTC on D+1, so the
membership day is `date(signal_ts_ms - 1ms)`, not the stamp date. `latest_signal_trading_day()` in
[`pit_coverage.py`](../liquidity_migration/data/pit_coverage.py) is `today_utc - 1`. Hourly bars key on
their own bar-stamp date, unadjusted.

**What kline coverage is required.** Not every manifest row. Coverage is required only for manifest
`(date, symbol)` pairs inside each symbol's traded span `[first_kline_date, last_kline_date]`
(`_required_pit_date_symbols`, `volume_events_pit.py:292`; pinned by
`tests/data/test_volume_events_pit.py`). Rows before the first kline (listing or
announcement precedes the first trade bar) and after the last kline (an isolated 0-trade
settlement/marker archive object landing weeks-to-months later) are excluded: genuinely empty archive
objects, untradable, and re-downloading them returns Empty every time. A gap *inside* the span is still
required and still fails the gate — FHEUSDT 2025-08-29..10-21, a 54-day archive-download hole, was
correctly flagged and then backfilled.

Three corrections that keep that rule from reading false-permissive:

- The span exclusion is one-sided at the top. A pair sourced `bybit_v5_listing` (`V5_LISTING_SOURCE` /
  `V5_LISTING_URL_SENTINEL`, `archive_manifest.py:40`) at or after the symbol's first observed kline is
  required **even beyond the current kline tail**, because that provenance independently records the
  symbol as `Trading` through the manifest build boundary. Inferring the upper bound from the klines
  under validation would let an incomplete active-symbol tail redefine itself as a completed lifespan
  and false-pass. The same provenance creates no requirement *before* the first kline. So a
  currently-listed coin can fail the gate on days with no klines at all.
- Bounds are per **ticker incarnation**, not per symbol. The span is split at every persisted v5
  `launchTime` (`v5_observed_launch_date`) observed strictly after the symbol's first stored kline, and
  min/max recomputed inside each segment (`_incarnation_segment_bounds` + `bisect_right`), so a reused
  ticker's post-delist tail and its pre-relist interval fall outside the requirement while a gap inside
  either traded incarnation still fails.
- Symbol-level coverage is a separate, unconditional condition the span rule does not relax.
  `FullPitUniverseCoverage.passed` requires all four of: a non-empty manifest symbol set;
  `missing_symbols` empty (`manifest_symbols - kline_symbols` — any manifest symbol with no klines
  *anywhere* fails, even though it contributes zero required date-pairs); a non-empty required
  date-symbol set; and `missing_required_date_symbols` empty. A wholly-missing delisted coin fails on
  `missing_symbols`; `validate-manifest` reports it as "N manifest symbol(s) have no klines".

So `manifest_date_symbols_missing_from_klines > 0` beside `full_pit_universe_pass = true` is the gate
working, not the gate leaking.

**Staleness.** `download-data` does not touch the manifest unless `--refresh-manifest` is passed, so a
freshly downloaded root can carry stale membership and hard-reject recent signals with
`pit_membership_fail`. `download-data` emits the PIT coverage table itself and, when the manifest is
stale, prints a WARNING block carrying the exact remediation command and the `--refresh-manifest`
alternative (`_download_manifest_staleness_lines`, `cli.py:44` → `coverage_status`/`format_coverage` in
[`pit_coverage.py`](../liquidity_migration/data/pit_coverage.py); reads `date=` partition names only, no
parquet, no network). That output also reports manifest end, kline end, latest signal day, margin in
days, manifest-vs-kline lag, and up to five per-symbol lag examples — the fastest way to tell whole-root
staleness from a handful of newly listed symbols. There is no standalone coverage subcommand; scroll
back for it. The remediation:

```bash
python -m liquidity_migration --data-root ROOT archive-manifest --start YYYY-MM-DD --end YYYY-MM-DD
python -m liquidity_migration.data.binance_vision validate-manifest --data-root ROOT
```

`validate-manifest` fails on missing kline coverage (≥20 hourly bars per symbol-day) without deleting
membership rows. `filter-manifest` conforms a manifest to observed klines and is correct only where
archive klines *are* the declared membership source (Binance Vision); it refuses provenance-bearing
Bybit manifests, because deleting an uncovered row would make the check self-certifying. Note that the
routine Binance tail refresh performs exactly that rewrite — see Refresh below.

The LONG research runner always measures manifest/kline agreement and records
`full_pit_universe_pass`, with no switch to disable it — the `require_full_pit_universe` strategy
switch no longer exists and must not be recreated; a non-passing run can be a current-universe or
data diagnostic, not a historical-universe performance claim. It also stamps, per run, a warning list
([`run_diagnostics.py`](../liquidity_migration/research/backtest/run_diagnostics.py) `RunWarning`: stable greppable
`code`, `severity` in `info` < `warn` < `tainted`, one-sentence `message`, one-line `fix`; the PIT codes
are `PIT_MANIFEST_EMPTY` and `PIT_SURVIVORSHIP`, both `tainted`), a data-integrity `run_label`, and a
`methodology_run_label` (`exploratory` | `biased_benchmark` | `invalid`). Any `tainted` warning means
the result is survivorship- or look-ahead-biased and must not be cited as clean. Both labels are printed
at the top of the long-native report and are the cheapest thing to check before quoting any LONG number;
the full label vocabulary is in [`trading_logic.md`](trading_logic.md). The CONTINUOUS equity runner
reads the kline root, which establishes nothing about historical membership. Changing the population
treatment after seeing a result does not rescue the original claim.

## Refresh

[`scripts/ops.sh research-refresh`](../scripts/ops.sh) →
[`scripts/research/research_refresh.sh`](../scripts/research/research_refresh.sh) → `scripts/research/research_refresh.py`.
Offline: no orders, no private venue APIs, no VPS checkout, no promotion.

```bash
scripts/ops.sh research-refresh plan --end 2026-07-31   # print, mutate nothing
scripts/ops.sh research-refresh run  --end 2026-07-31   # append-first (tail)
scripts/ops.sh research-refresh run  --end YYYY-MM-DD --start YYYY-MM-DD \
  --data-mode canonical --run-id RUN-ID                 # full rebuild
```

| Phase | `tail` behavior |
| --- | --- |
| Bybit membership | Rebuild the independent manifest over its canonical history. A narrow scan would silently lose delisted names, so membership is never tail-only. |
| Bybit klines | Recheck the trailing `--overlap-days` (default 7), fetch missing or sub-20-bar partitions, validate the whole root against the manifest. Failure triggers a full missing-only scan. |
| Binance klines/membership | Append strict current-month daily archives; crossing an unmaterialized month falls back to the atomic monthly builder. `topup-daily-klines` discovers symbols from the daily archive, writes only archive-backed rows, then **rewrites** `archive_trade_manifest` from actual kline coverage — `rewrite_manifest_to_coverage(root, archive_membership_source="binance_vision_archive")`, keeping only `(symbol, date)` pairs with ≥20 hourly bars and stamping them `membership_inferred=False` (`binance_vision.py:464-478, :554-557, :680-760`, `MIN_HOURLY_BARS = 20` at `:73`). A routine `run --end` therefore can change Binance membership. |
| Ancillary, **both venues** | Its own tail phase per venue: `data.bybit.ancillary_tail` (`download-data`: funding, open_interest, mark_price_1h, index_price_1h, premium_index_1h, `research_refresh.py:507-537`) and `data.binance.ancillary_tail` (`download-binance-proxy`: the same five plus taker_flow_1h, `:587-617`). Each starts from the stalest of its own venue's ancillary datasets minus `--overlap-days`, clamped to `[base_start, end-1d]` (`:170-185`), over that venue's whole validated manifest. Bybit ancillaries are **not** skipped by a tail run. |
| Residual momentum | Recompute a fixed **14-day** checked overlap (`DEFAULT_APPEND_OVERLAP_DAYS = 14`, `scripts/data/precompute_residual_momentum.py:67` — `research_refresh.py` never passes `--append-overlap-days`, so `--overlap-days` does not move it), require stable rows unchanged, atomically append the provisional tail. Rows older than the overlap window are preserved verbatim, so final causal rows survive for symbols that have since aged out. A table lacking `is_provisional`, or unreadable, is automatically promoted to one atomic full rewrite (reason `legacy_schema` / `unreadable_existing_table`); every *other* stable-overlap mismatch — key count, NaN positions, changed values, or a stable row demoted to provisional — fails closed for inspection (`:183-204`). `--force-rmom-full-rewrite` is the explicit recovery path, not a waiver. |
| Backtests | Reuse an identical completed run-scoped report, else recompute the fixed window from a clean sleeve directory. No incremental replay is appended to an old account journal. |

The download stages reuse valid existing data rather than re-fetching it: per-symbol ancillary writers
keep `_download_markers/{symbol}_{start_ms}_{end_ms}.done` files and skip or tail-trim any range already
covered (`downloaders.py:47, :453-495`), and the Bybit kline tail runs with `--min-existing-bars 20`
(`research_refresh.py:463-464`). This holds under `--data-mode canonical` too — the full-PIT builders
call the same writers — with the Binance monthly membership/kline pair as the exception, rebuilt in
verified staging and republished atomically. Corollary trap: deleting a dataset directory while keeping
its markers produces a silent stale skip.

`--data-mode canonical` invokes both full-PIT builders and regenerates residual momentum from its fixed
causal start. Every selected dataset must expose a partition for `end - 1 day`, manifest validation must
pass on all roots, and each backtest summary must match the frozen start/end at 1x modeled exposure.
`--force-rmom-full-rewrite` touches only the feature table — it rebuilds residual momentum from
`--start 2023-03-01` and leaves `tail` market-data behavior unchanged. The choice and its reason
(`operator_forced`, `canonical_data_mode`, `legacy_schema`, `unreadable_existing_table`) are frozen in
the run manifest configuration and in that step's command fingerprint, so using it is permanently
visible in the run identity. `--preregistration PATH` (on `plan`/`run`, not `reconcile`) resolves the
path strictly, hashes the file, and freezes `{"path": ..., "sha256": ...}` into the manifest's
configuration — which is also the block a resumed `--run-id` must match exactly. It is the mechanism
binding a canonical rebuild to a named contract.

A partial sleeve makes `scripts/research/equity_curves.sh` return nonzero: the runner keeps going across sleeves
but exits 1 if any sleeve errored (`scripts/research/equity_curves.py:512-514`), so a driver cannot accept an
incomplete benchmark as complete. A failed step leaves its record in `events.jsonl` and its output in
`logs/`; a retry clears only that run's partial derived sleeve directory under
`reports/research-refresh/<run-id>/backtests/<venue>/equity_curves/<sleeve>` (`equity_curves.sh` runs
with `--fresh-output`, which rmtree's exactly that one directory and refuses to replace a
non-directory). Raw data roots are never touched by a retry — re-running is cheap. `logs/<step>.log` is
opened in **append** mode (`research_refresh.py:326-328`), each attempt separated by a `$ <command>`
line, so earlier failure output survives a resume.

Artifacts land in `reports/research-refresh/<run-id>/`: `manifest.json` (code/config/root/window
identity), `events.jsonl` (append-only start/failure/success/resume ledger), `logs/`,
`summary.<hash>.json`, `backtests/<venue>/`, `reconciliation/`. `summary.<hash>.json` is the run's
immutable card — a coverage snapshot per venue, one cell result per venue/sleeve (run label,
window/date range, stats or summary, warnings, report path) and a sha256 plus byte size for every
emitted artifact; the filename carries the first 16 hex of the payload sha256, and rewriting it with
different bytes raises `run summary hash collision or changed immutable artifact` rather than
overwriting (`:887-969`). Reusing a `--run-id` skips only a step whose exact command fingerprint
succeeded and whose artifact still exists; changed windows, roots, source commits, or configuration are
refused under an existing ID.

**Three-way reconciliation.** Either inline — `plan` and `run` both accept
`--demo-account-root` / `--paper-account-root`, which must be supplied together (`demo and paper
account roots must be supplied together`), and a `run` given neither records
`reconcile.demo_paper_backtest / skipped_no_account_snapshots` in the event ledger rather than failing
(`:1114-1126, :1238-1244`) — or attached afterwards:

```bash
research-refresh reconcile --run-dir ... --demo-account-root ... --paper-account-root ... \
  [--account-snapshot-commit <40-hex>]
```

The snapshots are frozen copies the operator makes; the tool does not copy a live account root while
writers are active. It verifies each journal before reading (`read_account_journal(..., verify=True)`
plus `verify_account_journal`) and stores the receipt in the report's `source_identity`
(`three_way_reconciliation.py:302-306, :577, :587`). `--account-snapshot-commit` is optional and
recorded in `source_identity` alongside `code_commit` and `code_identity_status`; a non-40-lowercase-hex
value raises. Without it the tool falls back to commit strings scavenged from journal payloads, and if
that is ambiguous the report is stamped `unverified` with the warning `demo/paper code identity is not
proven by the supplied account snapshots` (`:523-541`). The run manifest's own `code_commit` must be 40
characters or `reconcile` refuses outright.

Comparison is on the grain `(sleeve, active component, symbol, causal signal_ts_ms)`. CONTINUOUS runtime
component tags are mapped to their code-defined names first — `p3`→`turn3p3`, `p4p3`→`turn4p3`,
`p4p5`→`turn4p5` (`_CONTINUOUS_COMPONENT_ALIASES`, `:39-46, :160-177`); an unmappable tag becomes
`unknown:<raw>` and raises the warning `unmapped live continuous component identities`. So a journal
`p4p3` against a backtest `turn4p3` is already reconciled — not a disagreement.

The report keeps three distinct states per key, not one: `demo_proposed`/`paper_proposed` (target
published), `demo_accepted`/`paper_accepted` (risk decision accepted), and
`demo_execution_states`/`paper_execution_states` (account-level net-symbol command/fill state), plus
`backtest_modeled`. The headline `three_way_overlap` and `three_way_exact` counts are computed on the
**accepted** sets only, so a proposed-but-risk-rejected key is not a demo/paper disagreement. Execution
states are one of `proposal_without_risk_decision`, `risk_rejected`, `accepted_batch_symbol_filled`,
`accepted_target_command_rejected`, `accepted_target_commanded_without_fill`,
`accepted_target_no_net_command` (`:246-258`).

Agreement supports a structural entry-key claim and nothing else. The report's own `claim_scope`
(`:610-613`) reads "accepted entry-key structural agreement only; execution quality, fill attribution,
account PnL, backtest performance, and runtime parity remain separate" — **runtime parity** is the most
tempting over-claim, because the demo/paper execution-state columns sit right there. The markdown
footer adds that the columns "must not be read as separately attributable component fills"
(`:716-718`). `reconciliation/` is immutable in the same sense as the run summary:
`three_way_reconciliation.json`, `three_way_reconciliation.md` and `entry_agreement.csv` are written
idempotently, and re-running with different evidence raises `immutable reconciliation artifact changed`
rather than overwriting (`:724-730, :775-777`) — choose a new `--reconcile-out`.

## Operational roots

Exact VPS paths come from the host env files, not from this document:
`/etc/liquidity-migration/account-execution.env` and
`/etc/liquidity-migration/account-paper-execution.env`. Each route names its own
`ACCOUNT_EXECUTION_ROOT` (canonical journal), `ACCOUNT_INTENT_INBOX_ROOT` (target inbox) and
`ACCOUNT_CAPTURE_ROOT` / `ACCOUNT_PAPER_CAPTURE_ROOT` (market capture); demo and paper roots are
absolute, **real**, **owner-controlled**, pairwise-disjoint and non-nested. Real and owner-controlled
are code-enforced, not stylistic: `reset_path_safety.py:1050` ("paper runtime root must be a real
directory"), `:1054` (paper lock namespace), `:1057` ("paper runtime tree contains a symlink"), and
`:1134` / `:1142` / `:1145` for the demo root, each demo route leaf and the demo lock namespace, plus
the uid/gid/mode rebinding at `:732-844`. A symlinked account root or a convenience bind mount is not a
legal layout. The paper mirror additionally reads
`DEMO_ACCOUNT_EXECUTION_ROOT` and `DEMO_ACCOUNT_CAPTURE_ROOT`. The demo account owner alone mutates
Bybit; the paper owner alone advances the deterministic paper account. Their journals own lifecycle
and accounting state, and Parquet views are rebuildable projections. Strategy `DATA_ROOT` directories
hold signal inputs, caches and cycle telemetry — not position or P&L authority.

Dataset and account roots use persistent `flock(2)` leaves: ownership is the kernel lock on the open
file description, not the file's contents, and release or crash recovery never unlinks the leaf. Never
delete a lock file as "stale". The protocol requires a **local POSIX filesystem with working advisory
`flock(2)`** and cooperative repository clients (`exclusive_file_lock`'s own contract,
`storage.py:222-225`); putting a dataset root or an account root on NFS or any network filesystem
silently voids the mutex. These are advisory, cooperative locks and not protection against a hostile
privileged process: the Bash descriptor handoff cannot itself request `O_NOFOLLOW`, so
private/root-controlled parent namespaces and the helper's post-open identity validation are part of
the boundary, and that validation detects a replacement without undoing an open-time side effect.
Explicitly forking inside a held critical section is unsupported; fork/exec
helpers and forks from other threads are cleaned up by the module's at-fork handler, which closes every
inherited flock descriptor in the child and re-creates the module's thread mutexes (`:41-77`). Adding
multiprocessing or a `fork()` inside a locked section silently double-owns the mutex.

Pre-existing JSON or empty lock leaves from the retired create/PID/unlink protocol are adopted in place
rather than replaced, and `fchmod`-ed to `0600` after safe acquisition (`storage.py:248-250`, bracketed
by two `_validate_lock_fd_path` calls); multiply-linked leaves are reconciled by
`_recover_internal_lock_alias` (`:439-463`). The mode change is the migration, not corruption — do not
hand-clean them.

Paper `.locks` directories and leaves are owned by `liquidity-migration-paper`, mode `0700`. Deployment
pre-creates them via `liquidity_migration.ops.reset_path_safety normalize-paper` / `normalize-demo`
(`scripts/deploy_vps_live.sh:584-607`; `reset_path_safety.py:908-955` does `os.mkdir(".locks", 0o700,
dir_fd=root_fd)` and rebinds/validates owner), and `deploy_vps_live.sh:642-643` then verifies the paper
user can write `$root/.locks`. Reset restores the same boundary before restarting paper services. At
runtime root may *observe* an owner-controlled lock but a non-root user may only use its own
(`_lock_owner_allowed`, `storage.py:273-276`); when euid 0 is the first reader of a paper-owned root,
`_ensure_lock_directory` fchowns/fchmods the directory to the *data root's* owner and 0700
(`:308-356`). The lock directory — real, not group/world-writable — is the ownership authority for its
leaves, and a leaf whose uid does not match its directory is rejected: "lock path owner must match its
lock directory" (`:377-378`). Creating a root-owned `.locks` under a paper root wedges every paper
service.

Host-maintenance and account-owner leases follow the same persistent-inode rule: parent namespaces and
leaves are opened with no-follow descriptors and checked for single-link ownership and mount identity;
normal owners keep the validated descriptor. During reset the lease identities are handed to the shell
as inherited file descriptors plus (device, inode) metadata rather than paths, and revalidated around
acquisition — account-lease metadata is written only after the inherited descriptor still matches the
prepared path **and** holds the kernel flock (`account_owner_lease.py:535-645`,
`revalidate_inherited_account_owner_lease` at `:647-727`; canonical demo lease directory
`/run/lock/liquidity-migration` at `:26`; `scripts/maintain/reset_demo_paper_ledgers.sh:145-172`, where the host
maintenance lock dir is the different path `/run/liquidity-migration`). Simplifying that handoff to a
plain path breaks the revalidation.

The "never delete a lock" rule has exactly one exception: a separately designed full-root retirement may
remove a canonical leaf, and only while every possible client is stopped under a stronger operational
boundary. Consistent with `account_route.py:484-501`, where a persistent lock inode (`.lock` suffix or
under `.locks`, regular, `st_nlink == 1`, uid == root uid, mode 0600) is classified as infrastructure
rather than prior account state during cutover.

[`deploy/sleeves.env`](../deploy/sleeves.env) is the repository ceiling for target producers, and a host
override can only narrow it. Turning a producer off stops publication; it does not erase its last
accepted target or flatten the account. Inspect with `scripts/ops.sh status` (runtime topology) and
`scripts/ops.sh venue-accounting` (stopped demo accounting interval). Research evidence and runtime
accounting stay separate; neither upgrades the other. See [`AGENTS.md`](../AGENTS.md) for how
evidence is graded and [`architecture.md`](architecture.md) for module ownership.
