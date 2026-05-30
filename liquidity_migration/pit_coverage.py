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
signal); see ``volume_events_features._attach_event_archive_membership`` and
``docs/pit_gate.md``. The most recent fully-closed daily bar as of ``now`` has
trading day ``today_utc - 1``; that is the latest trading day a daily-close signal
can reference, so the manifest must cover at least that day for a strict reconcile.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path

_DATE_DIR_RE = re.compile(r"date=(\d{4}-\d{2}-\d{2})")

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
        if best is None or d > best:
            best = d
    return best


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

    @property
    def manifest_covers_latest_signal(self) -> bool:
        return self.manifest_margin_days is not None and self.manifest_margin_days >= 0

    @property
    def is_stale(self) -> bool:
        """The manifest cannot PIT-validate the most recent daily-close signal."""
        return not self.manifest_covers_latest_signal


def coverage_status(data_root: str | Path, *, today: _dt.date | None = None) -> CoverageStatus:
    root = Path(data_root).expanduser()
    today = today or _today_utc()
    manifest_end = _max_partition_date(root / MANIFEST_DATASET)
    kline_end = _max_partition_date(root / KLINE_DATASET)
    sig_day = latest_signal_trading_day(today)
    margin = (manifest_end - sig_day).days if manifest_end is not None else None
    lag = (
        (kline_end - manifest_end).days
        if (manifest_end is not None and kline_end is not None)
        else None
    )
    return CoverageStatus(
        data_root=str(root),
        manifest_end=manifest_end,
        kline_end=kline_end,
        latest_signal_trading_day=sig_day,
        manifest_margin_days=margin,
        manifest_lag_vs_klines_days=lag,
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
            "           or pass `--pit-membership current-universe` for a clearly "
            "biased same-day diagnostic (never promotion evidence)."
        )
    if status.manifest_lag_vs_klines_days is not None and status.manifest_lag_vs_klines_days > 0:
        lines.append(
            f"  note: manifest is {status.manifest_lag_vs_klines_days}d behind klines_1h "
            "(klines were refreshed without refreshing the manifest)."
        )
    return "\n".join(lines)
