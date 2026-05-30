"""CLI/diagnostic coverage for the PIT-overhaul fixes (FIX B / C / D).

- FIX B: `--pit-membership {strict,current-universe}` maps to
  `VolumeEventResearchConfig.require_pit_membership` (strict -> True).
- FIX C: download-data prints a staleness WARNING (it does NOT refresh the
  archive manifest) when the manifest lags the signal universe.
- FIX D: a membership-rejecting run surfaces the actionable uncovered-tail
  diagnostic (manifest coverage end + count of event rows whose TRADING DAY,
  date of ts_ms-1ms, is past it), in both the helper and the markdown report.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from liquidity_migration.cli import (
    _download_manifest_staleness_lines,
    build_parser,
)
from liquidity_migration.volume_events import (
    VolumeEventResearchConfig,
    _pit_membership_diagnostic,
    format_volume_event_report,
)


def _mk(root, dataset, *dates):
    base = root / dataset
    for d in dates:
        (base / f"date={d}" / "symbol=FOO").mkdir(parents=True, exist_ok=True)


def _stamp_ms(y, m, d):
    # Daily-close signals are stamped at 00:00 UTC of the day AFTER the bar, so a
    # stamp at (y, m, d) 00:00 represents the (y, m, d-1) trading day.
    return int(dt.datetime(y, m, d, tzinfo=dt.timezone.utc).timestamp() * 1000)


# --- FIX B: flag -> config.require_pit_membership ---------------------------


def test_pit_membership_flag_maps_to_config():
    parser = build_parser()
    strict = parser.parse_args(["--data-root", "/tmp/x", "volume-events"])
    assert strict.pit_membership == "strict"
    assert (strict.pit_membership == "strict") is True  # -> require_pit_membership=True

    biased = parser.parse_args(
        ["--data-root", "/tmp/x", "volume-events", "--pit-membership", "current-universe"]
    )
    assert biased.pit_membership == "current-universe"
    assert (biased.pit_membership == "strict") is False  # -> require_pit_membership=False

    # The mapping the handler applies must round-trip through the config dataclass.
    assert VolumeEventResearchConfig(require_pit_membership=(strict.pit_membership == "strict")).require_pit_membership is True
    assert VolumeEventResearchConfig(require_pit_membership=(biased.pit_membership == "strict")).require_pit_membership is False


# --- FIX C: download-data staleness WARNING --------------------------------


def test_download_staleness_warning_when_manifest_lags(tmp_path, monkeypatch):
    # Manifest ends well behind the klines -> stale -> WARNING + remediation cmd.
    _mk(tmp_path, "archive_trade_manifest", "2026-05-10")
    _mk(tmp_path, "klines_1h", "2026-05-29")

    import liquidity_migration.pit_coverage as pc

    monkeypatch.setattr(pc, "_today_utc", lambda: dt.date(2026, 5, 30))
    text = "\n".join(_download_manifest_staleness_lines(tmp_path))
    assert "WARNING" in text
    assert "does NOT refresh the archive" in text
    assert "archive-manifest" in text
    assert "--refresh-manifest" in text


def test_download_no_warning_when_manifest_fresh(tmp_path, monkeypatch):
    _mk(tmp_path, "archive_trade_manifest", "2026-05-29")
    _mk(tmp_path, "klines_1h", "2026-05-29")

    import liquidity_migration.pit_coverage as pc

    monkeypatch.setattr(pc, "_today_utc", lambda: dt.date(2026, 5, 30))
    text = "\n".join(_download_manifest_staleness_lines(tmp_path))
    assert "WARNING" not in text
    assert "✅" in text


# --- FIX D: actionable pit_membership_fail diagnostic ----------------------


def test_pit_membership_diagnostic_counts_uncovered_tail(tmp_path):
    # Manifest covers through 2024-01-10. Two feature rows: one stamped 2024-01-10
    # (trading day 2024-01-09, covered) and one stamped 2024-01-15 (trading day
    # 2024-01-14, uncovered) -> exactly one uncovered-tail event row.
    _mk(tmp_path, "archive_trade_manifest", "2024-01-09", "2024-01-10")
    features = pl.DataFrame(
        {"symbol": ["FOO", "FOO"], "ts_ms": [_stamp_ms(2024, 1, 10), _stamp_ms(2024, 1, 15)]},
        schema={"symbol": pl.Utf8, "ts_ms": pl.Int64},
    )
    manifest = pl.DataFrame({"symbol": ["FOO"], "date": ["2024-01-10"]})
    diag = _pit_membership_diagnostic(tmp_path, manifest, features)
    assert diag["manifest_end"] == "2024-01-10"
    assert diag["uncovered_tail_event_rows"] == 1


def test_format_report_surfaces_membership_fail_block():
    # FIX D: when pit_membership_pass is False, the markdown report's Inputs section
    # gains a concise, actionable membership-fail line.
    metadata = {
        "run_label": "biased_benchmark",
        "rows": {"features": 3, "scenarios": 0, "pre_pit_promotable": 0, "promotable": 0},
        "config": {},
        "date_range": {"start": "2024-01-01", "end": "2024-01-15"},
        "pit_manifest": {},
        "promotion_note": "",
        "pit_membership_pass": False,
        "pit_membership_diagnostic": {"manifest_end": "2024-01-10", "uncovered_tail_event_rows": 7},
    }
    report = format_volume_event_report(pl.DataFrame(), metadata)
    assert "PIT membership FAIL" in report
    assert "2024-01-10" in report
    assert "uncovered tail" in report
    assert "current-universe" in report
