"""Point-in-time coverage / staleness diagnostics for a data root.

The recurring operational failure this guards against: the ``archive_trade_manifest``
(PIT membership, sourced from Bybit's ~1-day-lagged public trade archive) silently
drifts behind the kline data because ``download-data`` refreshes klines/funding but
NOT the manifest. A recent signal then hard-rejects with ``pit_membership_fail`` and
the backtest<->paper reconciliation breaks for no obvious reason.

These helpers turn that silent drift into a loud, actionable diagnostic. They are
deliberately cheap: coverage is read from the dataset's ``date=YYYY-MM-DD``
partition directory names, so no parquet is parsed.

Membership is keyed on the signal's TRADING DAY (the day whose close produced the
signal); the gate lives in ``volume_events_pit.py`` (consumed by the active
long/continuous engines) and ``docs/data.md``. The most recent fully-closed daily bar as of ``now`` has
trading day ``today_utc - 1``; that is the latest trading day a daily-close signal
can reference, so the manifest must cover at least that day for a strict reconcile.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from .symbol_codec import SymbolIdentityError, decode_symbol_partition

_DATE_DIR_RE = re.compile(r"date=(\d{4}-\d{2}-\d{2})")
_SYMBOL_DIR_RE = re.compile(r"symbol=([^\\/]+)")

MANIFEST_DATASET = "archive_trade_manifest"
KLINE_DATASET = "klines_1h"


def _max_partition_date(dataset_dir: Path) -> _dt.date | None:
    """Return the maximum ``date=YYYY-MM-DD`` partition under ``dataset_dir``.

    Scans directory names only (no parquet read). Works whether the dataset is
    partitioned ``date=.../symbol=...`` or ``symbol=.../date=...``.
    """
    if not dataset_dir.exists():
        return None
    best: _dt.date | None = None
    # Coverage is a diagnostic — it must never crash the operation it annotates
    # (a download, a reconcile). Any filesystem / glob quirk degrades to "unknown"
    # (None), not an exception. We scan the immediate children for ``date=*`` dirs
    # (the canonical layout) and fall back to a recursive scan otherwise.
    try:
        candidates = [c for c in dataset_dir.iterdir() if c.name.startswith("date=")]
        if not candidates:
            candidates = list(dataset_dir.rglob("date=*"))
    except OSError:
        return None
    for child in candidates:
        if not child.is_dir():
            continue
        m = _DATE_DIR_RE.search(child.name)
        if not m:
            continue
        try:
            d = _dt.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        # An EMPTY date= partition must not count as coverage: write_dataset
        # mkdir's date=X/symbol=Y BEFORE writing the part, so a crashed / refused
        # / interrupted build leaves an empty partition dir. Counting its name
        # would report a stale manifest as FRESH. Require a real parquet under it.
        try:
            has_parquet = next(child.rglob("*.parquet"), None) is not None
        except OSError:
            has_parquet = False
        if not has_parquet:
            continue
        if best is None or d > best:
            best = d
    return best


def _symbol_from_path(path: Path) -> str | None:
    for part in path.parts:
        m = _SYMBOL_DIR_RE.fullmatch(part)
        if m:
            try:
                return decode_symbol_partition(m.group(1))
            except SymbolIdentityError:
                return None
    return None


def _has_parquet(path: Path) -> bool:
    try:
        return next(path.rglob("*.parquet"), None) is not None
    except OSError:
        return False


def _symbols_from_parquet_files(path: Path) -> set[str]:
    symbols: set[str] = set()
    try:
        files = list(path.glob("*.parquet"))
    except OSError:
        return symbols
    for file in files:
        try:
            df = pl.read_parquet(file, columns=["symbol"])
        except Exception:
            continue
        for symbol in df.get_column("symbol").drop_nulls().unique().to_list():
            symbols.add(str(symbol))
    return symbols


def _symbols_on_date(dataset_dir: Path, day: _dt.date) -> set[str]:
    """Return symbols with at least one parquet partition for ``day``.

    The canonical layout is ``date=YYYY-MM-DD/symbol=SYMBOL``. The recursive
    fallback keeps the diagnostic compatible with symbol-first layouts without
    scanning every date on the normal path.
    """
    if not dataset_dir.exists():
        return set()
    date_name = f"date={day.isoformat()}"
    symbols: set[str] = set()
    date_dir = dataset_dir / date_name
    if date_dir.exists():
        try:
            children = list(date_dir.iterdir())
        except OSError:
            return set()
        for child in children:
            if not child.is_dir() or not child.name.startswith("symbol="):
                continue
            if _has_parquet(child):
                symbol = _symbol_from_path(child)
                if symbol:
                    symbols.add(symbol)
        symbols.update(_symbols_from_parquet_files(date_dir))
        return symbols

    try:
        candidates = list(dataset_dir.rglob(date_name))
    except OSError:
        return set()
    for candidate in candidates:
        if not candidate.is_dir() or not _has_parquet(candidate):
            continue
        symbol = _symbol_from_path(candidate)
        if symbol:
            symbols.add(symbol)
    return symbols


def _today_utc() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


def latest_signal_trading_day(today: _dt.date | None = None) -> _dt.date:
    """The most recent trading day a daily-close signal can reference right now.

    A daily bar for trading day D closes at D+1 00:00 UTC, so as of ``today`` the
    most recent fully-closed bar has trading day ``today - 1``.
    """
    today = today or _today_utc()
    return today - _dt.timedelta(days=1)


@dataclass(frozen=True)
class SymbolCoverageLag:
    symbol: str
    kline_day: _dt.date
    manifest_end: _dt.date | None

    @property
    def lag_days(self) -> int | None:
        if self.manifest_end is None:
            return None
        return (self.kline_day - self.manifest_end).days


@dataclass(frozen=True)
class CoverageStatus:
    data_root: str
    manifest_end: _dt.date | None
    kline_end: _dt.date | None
    latest_signal_trading_day: _dt.date
    # manifest_end - latest_signal_trading_day, in days. >= 0 means the manifest
    # covers every trading day a current daily-close signal could reference.
    manifest_margin_days: int | None
    # kline_end - manifest_end, in days. > 0 means the manifest lags the klines.
    manifest_lag_vs_klines_days: int | None
    # Symbols with latest-day kline partitions whose manifest coverage is missing
    # or behind within the recent tail lookback.
    per_symbol_manifest_lags: tuple[SymbolCoverageLag, ...] = ()

    @property
    def manifest_covers_latest_signal(self) -> bool:
        return self.manifest_margin_days is not None and self.manifest_margin_days >= 0

    @property
    def is_stale(self) -> bool:
        """The manifest cannot PIT-validate the most recent daily-close signal."""
        return not self.manifest_covers_latest_signal

def _per_symbol_manifest_lags(
    *,
    manifest_dir: Path,
    kline_dir: Path,
    kline_day: _dt.date | None,
    lookback_days: int,
) -> tuple[SymbolCoverageLag, ...]:
    if kline_day is None:
        return ()
    kline_symbols = _symbols_on_date(kline_dir, kline_day)
    if not kline_symbols:
        return ()

    manifest_latest: dict[str, _dt.date] = {}
    for offset in range(max(1, lookback_days)):
        day = kline_day - _dt.timedelta(days=offset)
        for symbol in _symbols_on_date(manifest_dir, day):
            manifest_latest.setdefault(symbol, day)

    lags: list[SymbolCoverageLag] = []
    for symbol in sorted(kline_symbols):
        manifest_end = manifest_latest.get(symbol)
        if manifest_end is None or manifest_end < kline_day:
            lags.append(SymbolCoverageLag(symbol=symbol, kline_day=kline_day, manifest_end=manifest_end))
    return tuple(lags)


def coverage_status(
    data_root: str | Path,
    *,
    today: _dt.date | None = None,
    per_symbol_lookback_days: int = 7,
) -> CoverageStatus:
    root = Path(data_root).expanduser()
    today = today or _today_utc()
    manifest_dir = root / MANIFEST_DATASET
    kline_dir = root / KLINE_DATASET
    manifest_end = _max_partition_date(manifest_dir)
    kline_end = _max_partition_date(kline_dir)
    sig_day = latest_signal_trading_day(today)
    margin = (manifest_end - sig_day).days if manifest_end is not None else None
    lag = (
        (kline_end - manifest_end).days
        if (manifest_end is not None and kline_end is not None)
        else None
    )
    per_symbol_check_day = min(kline_end, sig_day) if kline_end is not None else None
    per_symbol_lags = _per_symbol_manifest_lags(
        manifest_dir=manifest_dir,
        kline_dir=kline_dir,
        kline_day=per_symbol_check_day,
        lookback_days=per_symbol_lookback_days,
    )
    return CoverageStatus(
        data_root=str(root),
        manifest_end=manifest_end,
        kline_end=kline_end,
        latest_signal_trading_day=sig_day,
        manifest_margin_days=margin,
        manifest_lag_vs_klines_days=lag,
        per_symbol_manifest_lags=per_symbol_lags,
    )


def format_coverage(status: CoverageStatus) -> str:
    """A compact, human-readable coverage table with an actionable verdict."""
    def _d(v: _dt.date | None) -> str:
        return v.isoformat() if v is not None else "MISSING"

    lines = [
        f"PIT coverage @ {status.data_root}",
        f"  archive manifest end : {_d(status.manifest_end)}",
        f"  klines_1h end        : {_d(status.kline_end)}",
        f"  latest signal day    : {status.latest_signal_trading_day.isoformat()} "
        f"(trading day of the most recent fully-closed daily bar)",
    ]
    if status.manifest_end is None:
        lines.append("  VERDICT: ❌ no archive manifest — run `archive-manifest` before any PIT backtest.")
        return "\n".join(lines)
    if status.manifest_covers_latest_signal:
        margin = status.manifest_margin_days
        lines.append(
            f"  VERDICT: ✅ manifest covers the latest signal day (margin {margin:+d}d). "
            "Strict PIT reconcile is valid."
        )
    else:
        gap = -(status.manifest_margin_days or 0)
        lines.append(
            f"  VERDICT: ⚠️  manifest is {gap}d behind the latest signal day. "
            "Recent signals will hard-reject with pit_membership_fail."
        )
        lines.append(
            "           FIX: run `archive-manifest --start <recent> --end "
            f"{(status.latest_signal_trading_day + _dt.timedelta(days=2)).isoformat()}` "
            "to refresh PIT membership (download-data does NOT touch the manifest),"
        )
        lines.append(
            "           Until the manifest is refreshed, any current-universe diagnostic "
            "cannot support a historical-universe claim."
        )
    if status.manifest_lag_vs_klines_days is not None and status.manifest_lag_vs_klines_days > 0:
        lines.append(
            f"  note: manifest is {status.manifest_lag_vs_klines_days}d behind klines_1h "
            "(klines were refreshed without refreshing the manifest)."
        )
    if status.per_symbol_manifest_lags:
        examples = []
        for lag in status.per_symbol_manifest_lags[:5]:
            manifest = lag.manifest_end.isoformat() if lag.manifest_end is not None else "MISSING"
            lag_text = f"{lag.lag_days}d" if lag.lag_days is not None else "unknown"
            examples.append(f"{lag.symbol}: manifest {manifest}, kline {lag.kline_day.isoformat()}, lag {lag_text}")
        lines.append(
            f"  WARNING: {len(status.per_symbol_manifest_lags)} symbols have latest-day "
            "klines_1h coverage without matching archive-manifest coverage."
        )
        lines.append("           examples: " + "; ".join(examples))
    return "\n".join(lines)
