# Autonomous improvement cycle 003: preserve independent PIT requirements

## Evidence claim and scope

- Audit timestamp: `2026-07-16T01:34:41Z`.
- Audited commit: `cd2abdcbf87869af924d4ae931c15852e0d4b80d`
  on `codex/demo-operational-cutover`, plus the named local changes below.
- Claim assessed: whether the canonical Bybit build and LONG PIT gate preserve
  membership evidence independently of kline coverage and reject a missing
  required symbol-day.
- Validity of the old `full_pit_universe_pass=True` evidence: **invalid for a
  historical-population-completeness claim on an affected filtered root**. It
  may still support a narrower current-universe/data diagnostic; this finding
  does not itself judge strategy alpha, fills, costs, accounting, or deployment.
- This was a methodology/code/data audit, not a strategy experiment. No
  backtest, equity curve, parameter variant, VPS operation, credential, or venue
  mutation was used. No mainnet authority exists.

## Two independent failures

### 1. Active tail requirements were inferred from the data under test

`_required_pit_date_symbols()` bounded every symbol by its first and last
observed kline. A manifest row independently synthesized from a currently
`Trading` v5 listing could lie after a truncated kline tail, but the helper
would redefine the symbol's lifespan to end at the truncated tail and return
full-PIT true.

A prospective regression with 24 bars on days 1–2 and a
`source=bybit_v5_listing` requirement on day 3 failed before the fix:

```text
expected required pair: 2025-01-03/AAA
actual required pairs:   2025-01-01/AAA, 2025-01-02/AAA
actual full-PIT verdict: true
```

The metadata simultaneously counted the day-3 manifest pair as missing, so the
old pass flag contradicted its own diagnostic.

### 2. The canonical builder erased missing requirements

`scripts/build_full_pit_bybit.sh` ran the generic `filter-manifest` stage after
kline download. `rewrite_manifest_to_coverage()` inner-joined expected
membership to observed >=20-bar kline coverage and overwrote the manifest.
Missing requirements therefore disappeared before the LONG gate read them.

This is demonstrated by current local artifacts, independently re-read during
the audit:

- raw expected membership:
  `/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_full_pit/reports/archive_manifest_bybit-public-trading.csv`;
- persisted filtered manifest:
  `/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_full_pit/archive_trade_manifest/`;
- kline partitions:
  `/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_full_pit/klines_1h/`; and
- downloader receipt:
  `/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_full_pit/reports/archive_klines_1h_api_bybit-v5-market-klines-1h.csv`.

For `DATAUSDT`:

| Date | Raw expected row | Persisted rows | Kline rows | Downloader receipt |
| --- | ---: | ---: | ---: | --- |
| 2026-07-06 | 1 v5 | 0 | 0 | empty, 0 valid bars |
| 2026-07-07 | 1 v5 | 0 | 0 | empty, 0 valid bars |
| 2026-07-08 | 1 v5 | 0 | 0 | empty, 0 valid bars |
| 2026-07-09 | 1 archive | 2 provenance variants | 24 | outside narrow receipt |
| 2026-07-10 | 1 v5 | 1 | 24 | downloaded, 24 valid bars |

Across the persisted `DATAUSDT` slice there were 12,696 kline rows and 530
manifest rows representing 529 unique pairs. The persisted, filtered manifest
returned full-PIT true. Substituting the raw expected-membership artifact
returned false. The three missing days are mid-history, so the intended
pre-listing/post-delist phantom exception does not explain their deletion.

## Implementation

- Exact `bybit_v5_listing` pairs at or after a symbol's first observed kline are
  now required even beyond the observed tail. Archive-only pre-listing and
  post-delist phantom rows retain their existing exceptions.
- Added a reusable coverage assessment containing manifest symbols, kline
  symbols, required pairs, covered pairs, and exact missing sets. The boolean
  helper and validation CLI consume the same assessment.
- Added `validate-manifest`, a non-mutating command that exits nonzero with
  missing symbol/pair counts and samples. It never conforms expected membership
  to observed klines.
- Replaced the Bybit builder's destructive stage 3 with this validator. Ancillary
  downloads run only after membership/kline validation passes.
- Guarded `filter-manifest` against provenance-bearing Bybit manifests. Coverage
  rewriting remains available for the Binance Vision construction, where the
  archive klines are the declared membership source.
- Added required-pair and required-missing-pair counts to LONG metadata while
  threading the already computed sets to avoid a second multi-GB group-by.
- Updated `docs/pit_gate.md` and `docs/data_roots.md` to state the evidence
  boundary and repair workflow.

## Regression coverage

- current-listing tail truncation fails;
- current-listing provenance does not turn a pre-listing day into a requirement;
- genuine mid-history gaps still fail;
- archive-only pre-listing/post-delist phantom boundaries still pass;
- non-mutating validation retains failed expected rows;
- coverage rewriting refuses independent Bybit membership;
- the canonical builder orders manifest -> klines -> validation -> ancillaries
  and contains no filtering stage;
- package cold imports and shell syntax remain valid.

## Validation

- Focused local suite: 43 passed in 0.91 seconds.
- Full local pytest suite: 1,604 passed in 21.17 seconds.
- Repository-wide Ruff: passed.
- Package-wide local mypy: 85 modules passed.
- CLI help for `validate-manifest`: passed.
- `bash -n scripts/build_full_pit_bybit.sh`: passed.
- Locked environment: Python 3.11.5, pytest 8.4.2, mypy 1.20.2,
  Ruff 0.15.14, Polars 1.41.0.
- Locked focused suite: 43 passed in 3.64 seconds.
- Locked package-wide mypy, focused Ruff, and shell syntax: passed.
- `git diff --check`: passed before this report was written.

The locked installer warned that the pinned Polars 1.41.0 packages are yanked,
without a supplied reason. Compatibility passed, but dependency replacement is
a separate evidence-backed maintenance candidate.

No performance improvement is claimed, so no before/after benchmark applies.
The LONG trading calculations are unchanged; PIT validity labels and metadata
are intentionally stricter when independent requirements are missing.

## Current-root limitation and next action

The local Bybit root remains filtered/corrupted; this code change does not
silently reconstruct or bless it. Before using that root for a new
historical-population claim:

1. rebuild the independent manifest over the declared end-exclusive window;
2. retry only the missing kline surface;
3. run `validate-manifest`; and
4. preserve unresolved empties as contradictory evidence.

The observed `DATAUSDT` empties may reflect a real suspension or a source gap.
The current sources do not identify that distinction. Do not delete the rows to
force a pass; add an evidence-backed suspension/status contract prospectively if
the venue fact can be reconstructed.

One residual tail edge remains: if an archive-sourced terminal pair already
exists, the v5 synthesizer does not emit a duplicate marker, so exact-pair
provenance alone cannot prove that terminal pair belongs to an active symbol.
Treating any historical v5 row as symbol-global activity would be unsafe because
unioned manifests retain stale rows after delisting. A robust extension needs an
explicit current-status/build-boundary field or terminal active marker.

Other next candidates remain the empty-tail coverage-marker bug in
`downloaders.py`, independent liveness-unit ownership, sparse archive gap
over-fetch, and cross-branch VPS deployment serialization.
