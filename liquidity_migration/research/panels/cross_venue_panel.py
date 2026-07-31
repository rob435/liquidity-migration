"""Minimal causal cross-venue panel — the P0 research substrate.

One reusable hourly panel over symbols listed on both Bybit and Binance, built
from the Tier-A/Tier-B datasets that `docs/data.md` certifies as real:
price, mark, index, premium, settled funding, turnover, and Bybit open
interest. It exists so anomaly research stops rebuilding bespoke readers.

Causality contract — the whole point of this module:

* An hourly bar stamped ``ts_ms`` is the bar's *open*. It is not complete, and
  therefore not knowable, until ``ts_ms + 1h``. Every row is keyed by
  ``decision_ts_ms = bar_ts_ms + 1h + execution_delay_ms``: the earliest wall
  time at which every non-funding field on that row was published. Joining on
  ``bar_ts_ms`` instead would leak one hour of future information into every
  observation.
* Settled funding is known from its settlement instant onward, so it is the one
  field carried forward — by a backward as-of join that can never see a future
  settlement. The boundary is inclusive: a settlement stamped exactly at
  ``decision_ts_ms`` is visible, because the venue publishes the rate at that
  instant. Staleness is exposed as ``by_funding_age_h`` / ``bn_funding_age_h``
  rather than hidden, so a consumer can tighten the boundary itself by
  filtering on age if a claim needs a stricter contract.
* Nothing else is filled across a gap. A missing source row yields null plus a
  false coverage flag, never a stale value wearing a fresh timestamp.

Known source defect — read this before using ``by_open_interest``:

    Bybit open interest is **survivorship-contaminated**. It was backfilled
    only for contracts still listed at backfill time. Measured on the 2024
    shard, 95.9% of symbols carrying OI history are still listed today, versus
    0.0% of those without it. Price, funding, and premium are population-
    complete across both cohorts; only OI is restricted.

    So filtering on ``cov_bybit_oi`` silently selects survivors, and delisted
    alts are exactly where short exposure pays. Use funding and premium as the
    primary crowding measurements; treat OI as corroboration on the survivor
    cohort, and replicate any OI-conditioned result funding-only on the full
    population before believing it. Detail in ``docs/research_findings.md`` §4.

The builder resolves its symbol universe by canonical identity
(`symbol_codec`), rejects within-venue partition collisions instead of
silently collapsing them, and emits a manifest recording the git commit, a
config hash, population/date bounds, per-venue-year coverage, and every
exclusion with its reason. It reads research roots only.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import polars as pl

from liquidity_migration.core.symbol_codec import SymbolIdentityError, decode_symbol_partition, normalize_exchange_symbol

HOUR_MS = 3_600_000
FUNDING_LOOKBACK_DAYS = 2
FUNDING_KIND_COLUMN = "funding_event_kind"


@dataclasses.dataclass(frozen=True)
class _Source:
    """One venue dataset and the output prefix its columns are given."""

    venue: str
    dataset: str
    prefix: str
    columns: tuple[str, ...]
    required_for_universe: bool = False


#: Every field the panel carries, with a collision-free output prefix.
SOURCES: tuple[_Source, ...] = (
    _Source("bybit", "klines_1h", "by", ("close", "turnover_quote"), required_for_universe=True),
    _Source("bybit", "mark_price_1h", "by_mark", ("close",)),
    _Source("bybit", "index_price_1h", "by_index", ("close",)),
    _Source("bybit", "premium_index_1h", "by_premium", ("close",), required_for_universe=True),
    # ``open_interest_value`` is dropped: it is a byte-for-byte duplicate of
    # ``open_interest`` across the whole dataset, not a quote-currency
    # notional. The panel derives a real notional below instead.
    _Source("bybit", "open_interest", "by", ("open_interest",)),
    _Source("binance", "klines_1h", "bn", ("close", "turnover_quote"), required_for_universe=True),
    _Source("binance", "binance_usdm_premium_index_1h", "bn_premium", ("close",), required_for_universe=True),
)
SPINE = SOURCES[0]
"""Bybit klines define the hourly grid: the venue we trade, and the field whose
completion sets the decision clock."""

FUNDING_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("bybit", "funding", "by"),
    ("binance", "binance_usdm_funding", "bn"),
)

COVERAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("cov_bybit_price", "by_close"),
    ("cov_bybit_mark", "by_mark_close"),
    ("cov_bybit_index", "by_index_close"),
    ("cov_bybit_premium", "by_premium_close"),
    ("cov_bybit_oi", "by_open_interest"),
    ("cov_bybit_funding", "by_funding"),
    ("cov_binance_price", "bn_close"),
    ("cov_binance_premium", "bn_premium_close"),
    ("cov_binance_funding", "bn_funding"),
)


class PanelBuildError(RuntimeError):
    """The requested panel cannot be built from the given roots and bounds."""


@dataclasses.dataclass(frozen=True)
class PanelSpec:
    """Declared, hashable configuration for one panel build.

    ``end`` is exclusive, matching every other date boundary in this
    repository. ``execution_delay_ms`` is the claim-appropriate delay between a
    field becoming public and a decision acting on it; it is additive to the
    mandatory one-hour bar-completion lag and is recorded in the manifest.
    """

    bybit_root: Path
    binance_root: Path
    start: dt.date
    end: dt.date
    execution_delay_ms: int = 0
    symbols: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise PanelBuildError(f"end {self.end} must be after start {self.start} (end is exclusive)")
        if self.execution_delay_ms < 0:
            raise PanelBuildError("execution_delay_ms must not be negative")

    def root_for(self, venue: str) -> Path:
        return self.bybit_root if venue == "bybit" else self.binance_root

    def config_hash(self) -> str:
        payload = {
            "bybit_root": str(self.bybit_root),
            "binance_root": str(self.binance_root),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "execution_delay_ms": self.execution_delay_ms,
            "symbols": sorted(self.symbols) if self.symbols else None,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _partition_symbols(
    dataset_dir: Path, start: dt.date, end: dt.date
) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(canonical -> raw partition, unreadable raw -> reason)``.

    Only date partitions inside ``[start, end)`` are walked. Scanning a
    dataset's whole history to answer a one-month question costs millions of
    directory stats and silently widens the population beyond the declared
    window.

    Two distinct upstream identifiers that decode to the same canonical symbol
    are an identity failure, not a merge opportunity: silently keeping one
    would attribute another contract's history to this symbol.

    A partition whose name is not canonical/reversible under `symbol_codec` is
    dropped — but reported, never silently swallowed. A silently missing symbol
    is indistinguishable from one the venue never listed, which is exactly the
    kind of invisible population edit that makes a study unreadable later.
    """

    resolved: dict[str, str] = {}
    rejected: dict[str, str] = {}
    if not dataset_dir.is_dir():
        return resolved, rejected
    for date_dir in dataset_dir.iterdir():
        if not date_dir.is_dir() or not date_dir.name.startswith("date="):
            continue
        try:
            day = dt.date.fromisoformat(date_dir.name.split("=", 1)[1])
        except ValueError:
            continue
        if not start <= day < end:
            continue
        for symbol_dir in date_dir.iterdir():
            if not symbol_dir.is_dir() or not symbol_dir.name.startswith("symbol="):
                continue
            raw = symbol_dir.name.split("=", 1)[1]
            try:
                canonical = normalize_exchange_symbol(decode_symbol_partition(raw))
            except SymbolIdentityError as exc:
                rejected.setdefault(raw, f"unreadable partition in {dataset_dir.name}: {exc}")
                continue
            previous = resolved.get(canonical)
            if previous is not None and previous != raw:
                raise PanelBuildError(
                    f"symbol collision in {dataset_dir}: partitions {previous!r} and {raw!r} "
                    f"both resolve to {canonical!r}"
                )
            resolved[canonical] = raw
    return resolved, rejected


def _date_globs(root: Path, dataset: str, start: dt.date, end: dt.date) -> list[str]:
    """Return one glob per in-range date partition that exists on disk."""

    dataset_dir = root / dataset
    if not dataset_dir.is_dir():
        return []
    globs: list[str] = []
    for date_dir in sorted(dataset_dir.iterdir()):
        if not date_dir.is_dir() or not date_dir.name.startswith("date="):
            continue
        try:
            day = dt.date.fromisoformat(date_dir.name.split("=", 1)[1])
        except ValueError:
            continue
        if start <= day < end:
            globs.append(str(date_dir / "symbol=*" / "part.parquet"))
    return globs


def _canonicalize(frame: pl.DataFrame, keep: Sequence[str]) -> pl.DataFrame:
    """Normalize the in-file symbol column and restrict to ``keep``.

    Normalization runs over the small set of distinct values rather than every
    row, so a multi-million-row scan does not pay per-row Python cost.
    """

    if frame.is_empty():
        return frame
    mapping: dict[str, str] = {}
    for raw in frame.get_column("symbol").unique().to_list():
        try:
            mapping[raw] = normalize_exchange_symbol(raw)
        except SymbolIdentityError:
            continue
    allowed = set(keep)
    mapping = {raw: canonical for raw, canonical in mapping.items() if canonical in allowed}
    if not mapping:
        return frame.clear()
    return frame.filter(pl.col("symbol").is_in(list(mapping))).with_columns(
        pl.col("symbol").replace_strict(mapping, return_dtype=pl.String)
    )


def _scan(source: _Source, spec: PanelSpec, keep: Sequence[str]) -> pl.DataFrame:
    """Read one dataset over the window into ``ts_ms, symbol, <prefixed cols>``."""

    renamed = {c: f"{source.prefix}_{c}" for c in source.columns}
    empty = pl.DataFrame(
        schema={"ts_ms": pl.Int64, "symbol": pl.String, **{v: pl.Float64 for v in renamed.values()}}
    )
    globs = _date_globs(spec.root_for(source.venue), source.dataset, spec.start, spec.end)
    if not globs:
        return empty
    frame = (
        pl.scan_parquet(globs)
        .select("ts_ms", "symbol", *source.columns)
        .rename(renamed)
        .collect()
    )
    frame = _canonicalize(frame, keep)
    if frame.is_empty():
        return empty
    return frame.unique(subset=["ts_ms", "symbol"], keep="first")


def _has_event_kind(glob_pattern: str) -> bool:
    """Whether this date partition carries the ``funding_event_kind`` column."""

    date_dir = Path(glob_pattern).parent.parent
    sample = next(date_dir.glob("symbol=*/part.parquet"), None)
    if sample is None:
        return False
    try:
        return FUNDING_KIND_COLUMN in pl.read_parquet_schema(sample)
    except (OSError, pl.exceptions.PolarsError):
        return False


def _scan_funding(venue: str, dataset: str, prefix: str, spec: PanelSpec, keep: Sequence[str]) -> pl.DataFrame:
    """Read settled funding, widened one lookback window before ``start``.

    The lookback lets the first in-window hours carry the settlement that was
    already public before the window opened, rather than showing a spurious
    gap. It never reaches forward.

    ``funding_event_kind`` was added to these datasets only recently, so the
    two schema generations are scanned separately. Where the column exists the
    scan keeps settlements only — a ``predicted`` rate describes an interval
    that has not settled yet, and admitting one would put an unknowable number
    on a decision row. Where the column does not exist the partition predates
    prediction rows and every row is a settlement.
    """

    empty = pl.DataFrame(
        schema={"ts_ms": pl.Int64, "symbol": pl.String, f"{prefix}_funding": pl.Float64}
    )
    lookback = spec.start - dt.timedelta(days=FUNDING_LOOKBACK_DAYS)
    globs = _date_globs(spec.root_for(venue), dataset, lookback, spec.end)
    if not globs:
        return empty

    typed = [g for g in globs if _has_event_kind(g)]
    untyped = [g for g in globs if g not in set(typed)]
    parts: list[pl.DataFrame] = []
    for group, settled_only in ((untyped, False), (typed, True)):
        if not group:
            continue
        scan = pl.scan_parquet(group)
        if settled_only:
            scan = scan.filter(pl.col(FUNDING_KIND_COLUMN) == "settlement")
        parts.append(
            scan.select("ts_ms", "symbol", pl.col("funding_rate").alias(f"{prefix}_funding")).collect()
        )
    if not parts:
        return empty
    frame = _canonicalize(pl.concat(parts, how="vertical"), keep)
    if frame.is_empty():
        return empty
    return frame.unique(subset=["ts_ms", "symbol"], keep="last")


def resolve_universe(spec: PanelSpec) -> tuple[tuple[str, ...], dict[str, str]]:
    """Return the both-venue symbol population and the exclusion reasons."""

    present: dict[str, set[str]] = {}
    exclusions: dict[str, str] = {}
    for source in SOURCES:
        label = f"{source.venue}/{source.dataset}"
        resolved, rejected = _partition_symbols(
            spec.root_for(source.venue) / source.dataset, spec.start, spec.end
        )
        present[label] = set(resolved)
        for raw, reason in rejected.items():
            exclusions.setdefault(raw, reason)

    required = [f"{s.venue}/{s.dataset}" for s in SOURCES if s.required_for_universe]
    eligible = set.intersection(*(present[label] for label in required))

    for label in required:
        for symbol in present[label] - eligible:
            exclusions.setdefault(symbol, f"missing {label}")

    if spec.symbols is not None:
        requested = {normalize_exchange_symbol(s) for s in spec.symbols}
        for symbol in requested - eligible:
            exclusions.setdefault(symbol, "requested but not in both-venue population")
        eligible &= requested
    return tuple(sorted(eligible)), exclusions


def build_panel(spec: PanelSpec) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Build the causal cross-venue panel and its manifest.

    Returns ``(panel, manifest)``. The panel is sorted by
    ``(symbol, decision_ts_ms)`` and carries one ``cov_*`` flag per field
    group, so a consumer can never mistake an absent field for a zero.
    """

    universe, exclusions = resolve_universe(spec)
    if not universe:
        raise PanelBuildError(
            f"no symbol is present in every required dataset on both venues for "
            f"[{spec.start}, {spec.end}); {len(exclusions)} symbols excluded"
        )

    panel = _scan(SPINE, spec, universe)
    if panel.is_empty():
        raise PanelBuildError(f"no {SPINE.venue}/{SPINE.dataset} rows in [{spec.start}, {spec.end})")
    for source in SOURCES[1:]:
        panel = panel.join(_scan(source, spec, universe), on=["ts_ms", "symbol"], how="left")

    panel = (
        panel.rename({"ts_ms": "bar_ts_ms"})
        .with_columns((pl.col("bar_ts_ms") + HOUR_MS + spec.execution_delay_ms).alias("decision_ts_ms"))
        .sort(["decision_ts_ms", "symbol"])
    )

    for venue, dataset, prefix in FUNDING_SOURCES:
        funding = _scan_funding(venue, dataset, prefix, spec, universe)
        if funding.is_empty():
            panel = panel.with_columns(
                pl.lit(None, dtype=pl.Float64).alias(f"{prefix}_funding"),
                pl.lit(None, dtype=pl.Float64).alias(f"{prefix}_funding_age_h"),
            )
            continue
        stamp = f"{prefix}_funding_ts_ms"
        funding = funding.with_columns(pl.col("ts_ms").alias(stamp)).sort(["ts_ms", "symbol"])
        panel = (
            panel.join_asof(
                funding.drop("ts_ms"),
                left_on="decision_ts_ms",
                right_on=stamp,
                by="symbol",
                strategy="backward",
                # Both sides are globally sorted ts-first, which implies
                # per-`by`-group order. polars cannot verify sortedness once
                # `by` groups are given, so assert what the sorts guarantee.
                check_sortedness=False,
            )
            .with_columns(
                ((pl.col("decision_ts_ms") - pl.col(stamp)) / HOUR_MS).alias(f"{prefix}_funding_age_h")
            )
            .drop(stamp)
        )

    panel = panel.with_columns(
        [pl.col(field).is_not_null().alias(flag) for flag, field in COVERAGE_FIELDS]
    ).with_columns(
        # Cross-venue differences: mechanical transforms of aligned fields,
        # not signals.
        basis_bp=(pl.col("by_close") / pl.col("bn_close") - 1.0) * 10_000.0,
        premium_diff_bp=(pl.col("by_premium_close") - pl.col("bn_premium_close")) * 10_000.0,
        funding_diff_bp=(pl.col("by_funding") - pl.col("bn_funding")) * 10_000.0,
        # Open interest in contract units is not comparable across symbols;
        # the mark price is the venue's own valuation basis, so this is the
        # quote-currency notional the source dataset lacks.
        by_oi_notional=pl.col("by_open_interest") * pl.col("by_mark_close"),
    )

    panel = panel.sort(["symbol", "decision_ts_ms"])
    return panel, _manifest(spec, panel, universe, exclusions)


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def _manifest(
    spec: PanelSpec,
    panel: pl.DataFrame,
    universe: Sequence[str],
    exclusions: Mapping[str, str],
) -> dict[str, Any]:
    """Describe exactly what was built, from what, and what was left out."""

    flags = [flag for flag, _ in COVERAGE_FIELDS]
    by_year: dict[str, dict[str, Any]] = {}
    if not panel.is_empty():
        yearly = (
            panel.with_columns(
                pl.from_epoch(pl.col("bar_ts_ms"), time_unit="ms").dt.year().alias("year")
            )
            .group_by("year")
            .agg(
                pl.len().alias("rows"),
                pl.col("symbol").n_unique().alias("symbols"),
                *[pl.col(f).mean().alias(f) for f in flags],
            )
            .sort("year")
        )
        for row in yearly.iter_rows(named=True):
            year = str(row.pop("year"))
            by_year[year] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in row.items()}

    return {
        "artifact": "cross_venue_panel",
        "git_commit": _git_commit(),
        "config_hash": spec.config_hash(),
        "roots": {"bybit": str(spec.bybit_root), "binance": str(spec.binance_root)},
        "date_bounds": {"start": spec.start.isoformat(), "end_exclusive": spec.end.isoformat()},
        "timing": {
            "bar_completion_lag_ms": HOUR_MS,
            "execution_delay_ms": spec.execution_delay_ms,
            "decision_key": "decision_ts_ms = bar_ts_ms + bar_completion_lag_ms + execution_delay_ms",
            "funding_join": "backward as-of on decision_ts_ms; age exposed as *_funding_age_h",
            "gap_policy": "no backward fill; missing source rows stay null with a false cov_* flag",
        },
        "population": {
            "symbols": len(universe),
            "excluded": len(exclusions),
            "exclusion_reasons": dict(sorted(exclusions.items())),
        },
        "rows": panel.height,
        "coverage_by_year": by_year,
    }


def write_panel(panel: pl.DataFrame, manifest: Mapping[str, Any], out_dir: Path) -> tuple[Path, Path]:
    """Write ``panel.parquet`` and ``manifest.json`` under ``out_dir``."""

    out_dir.mkdir(parents=True, exist_ok=True)
    panel_path = out_dir / "panel.parquet"
    manifest_path = out_dir / "manifest.json"
    panel.write_parquet(panel_path)
    payload = dict(manifest)
    payload["panel_sha256"] = hashlib.sha256(panel_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return panel_path, manifest_path
