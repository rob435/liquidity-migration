# PIT Membership Gate

Point-in-time membership controls which symbols a historical-universe claim may
trade. It is a research-data validity gate, not an execution reconciler.

## Manifest contract

The archive trade manifest stores:

```text
{data_root}/archive_trade_manifest/date=YYYY-MM-DD/symbol=SYMBOL/part.parquet
```

with `symbol`, `date`, `url`, and `source`. Bybit manifests contain two source
classes:

1. archive-observed rows, where a public trade-archive object existed for the
   symbol/day;
2. current-listing-derived rows, inferred from a currently `Trading` v5
   instrument and its reported launch date through the build boundary.

The second class closes archive lag and missing current symbols, but does not
observe every historical suspension, delisting, or trading day. Therefore
“full PIT” means complete under this manifest contract, not perfect historical
venue knowledge.

The builder is:

```bash
python -m liquidity_migration --data-root ROOT archive-manifest \
  --start YYYY-MM-DD --end YYYY-MM-DD
```

`END` is exclusive. `download-data` does not refresh the manifest unless its
explicit refresh option is used; inspect the emitted coverage warning.

The canonical Bybit root keeps this expected-membership manifest independent of
observed klines. `scripts/build_full_pit_bybit.sh` runs the non-mutating
`validate-manifest` step and fails when a required pair lacks coverage. The
generic `filter-manifest` command is only for roots whose archive klines are
themselves the declared membership source (the Binance Vision construction); it
refuses provenance-bearing Bybit manifests. Deleting an uncovered Bybit
membership row would make the gate self-certifying and destroy the repair
evidence.

## Trading-day convention

A daily-close signal is stamped at 00:00 UTC after the bar it summarizes. The
membership key is therefore:

```text
date(signal_ts_ms - 1ms)
```

For example, a signal stamped `2026-05-30 00:00` uses the 2026-05-29 manifest
day. The former signal-stamp-date lookup was a one-day look-ahead and delayed
current validation; it was fixed on 2026-05-30 in
`volume_events_pit.py`. Hourly kline partitions use their own bar stamp date and
do not apply this signal-plane adjustment.

Manifest scope drops pre-listing and post-delist phantom rows caused by genuine
empty archive objects while retaining mid-history gaps as failures. The
same rule is applied per ticker incarnation when a persisted v5 `launchTime`
shows that a venue symbol was reused: the relisting boundary separates the old
post-delist tail from the new pre-trade interval, while any gap inside either
traded incarnation still fails. The binding convention is covered by the PIT
tests and archived design receipt.

## Current runner behavior

There is no surviving `require_full_pit_universe` strategy switch. The LONG
research runner always measures manifest/kline agreement and records
`full_pit_universe_pass`, warnings, taint, and a scoped run label. A non-passing
run may remain a current-universe or data diagnostic, but it cannot support a
historical-universe performance claim.

The CONTINUOUS equity runner reads the selected kline root but does not by that
fact establish manifest-backed historical membership. Its historical curves
therefore remain limited for claims whose population depends on historical
venue membership unless the exact run supplies separate PIT evidence.

Changing the population treatment after inspecting a result cannot rescue the
original claim. Judge the emitted artifacts and effective code path, not the
root name.

## Workflow

1. State the claim's population, venue, and end-exclusive boundary.
2. Inspect the selected root's expected manifest and kline coverage, including
   per-symbol tail gaps and `source` provenance. Do not conform the expected
   manifest to the observed klines.
3. Rebuild only the missing surface using current command help.
4. Run the exact research command and preserve its PIT status, warnings, and
   population treatment.
5. Preserve root/config/code identities, coverage warnings, run label, and
   artifacts.

When the gate fails, identify whether membership, klines, or both are missing.
Do not substitute a current universe or silently drop symbols. A partial run
may still support a narrower diagnostic when population completeness is not
part of that proposition, but the limitation must remain explicit under
`docs/governance.md`.

Runtime journal, fill, and venue-accounting evidence is separate. It cannot
upgrade a failed PIT population claim, and PIT coverage cannot prove runtime
order/fill agreement or authorize deployment.
