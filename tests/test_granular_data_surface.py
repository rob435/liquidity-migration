from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "granular_data_surface.py"


def _load():
    spec = importlib.util.spec_from_file_location("granular_data_surface", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _ts(day: str, minute: int = 0) -> int:
    dt = datetime.fromisoformat(day).replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000) + minute * 60_000


def _manifest(root: Path, day: str, symbols: list[str]) -> None:
    path = root / "archive_trade_manifest" / f"date={day}"
    path.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": symbols,
            "date": [day] * len(symbols),
            "url": [f"fixture://{symbol}/{day}" for symbol in symbols],
        }
    ).write_parquet(path / "part.parquet")


def _partition(root: Path, dataset: str, day: str, symbol: str, rows: int) -> None:
    path = root / dataset / f"date={day}" / f"symbol={symbol}"
    path.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": [symbol] * rows,
            "ts_ms": [_ts(day, index * 5) for index in range(rows)],
            "open": [100.0] * rows,
            "high": [101.0] * rows,
            "low": [99.0] * rows,
            "close": [100.5] * rows,
        }
    ).write_parquet(path / "part.parquet")


def _flat(root: Path, dataset: str, day: str, symbol: str, rows: int) -> None:
    path = root / dataset
    path.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": [symbol] * rows,
            "ts_ms": [_ts(day, index * 5) for index in range(rows)],
            "open": [100.0] * rows,
            "high": [101.0] * rows,
            "low": [99.0] * rows,
            "close": [100.5] * rows,
        }
    ).write_parquet(path / f"{symbol}.parquet")


def test_exact_audit_marks_complete_partial_and_missing_symbol_days(tmp_path: Path) -> None:
    mod = _load()
    root = tmp_path / "bybit"
    _manifest(root, "2026-01-01", ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    _partition(root, "klines_5m", "2026-01-01", "BTCUSDT", 288)
    _partition(root, "klines_5m", "2026-01-01", "ETHUSDT", 287)

    report = mod.audit_venue(
        "bybit",
        root,
        logical_names=("klines_5m",),
        start="2026-01-01",
        end="2026-01-02",
        symbols=(),
        max_symbol_days=100,
        min_complete_coverage=0.995,
    )
    row = report["datasets"][0]
    assert row["status"] == "PARTIAL"
    assert row["complete_symbol_days"] == 1
    assert row["partial_symbol_days"] == 1
    assert row["missing_symbol_days"] == 1
    assert row["complete_coverage"] == pytest.approx(1 / 3)
    assert row["content_identity_sha256"]
    assert report["pit_manifest_content_sha256"]


def test_content_identity_detects_same_size_rewrite_with_restored_mtime(tmp_path: Path) -> None:
    mod = _load()
    path = tmp_path / "part.parquet"
    path.write_bytes(b"original")
    stat = path.stat()
    before = mod._content_identity([path])
    path.write_bytes(b"mutated!")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    assert path.stat().st_size == stat.st_size
    assert path.stat().st_mtime_ns == stat.st_mtime_ns
    assert mod._content_identity([path]) != before


def test_execute_dataset_support_is_validated_per_venue() -> None:
    mod = _load()

    with pytest.raises(ValueError, match="no maintained resume-safe bybit downloader"):
        mod._validate_executable_selection(("bybit",), ("taker_flow",))
    with pytest.raises(ValueError, match="no maintained resume-safe bybit downloader"):
        mod._validate_executable_selection(("bybit",), ("metrics_5m",))
    mod._validate_executable_selection(
        ("bybit", "binance"),
        ("klines_5m", "funding", "open_interest", "premium_index_1h"),
    )


def test_flat_symbol_layout_is_audited_exactly(tmp_path: Path) -> None:
    mod = _load()
    root = tmp_path / "binance"
    _manifest(root, "2026-01-01", ["BTCUSDT"])
    _flat(root, "klines_5m", "2026-01-01", "BTCUSDT", 288)
    report = mod.audit_venue(
        "binance",
        root,
        logical_names=("klines_5m",),
        start="2026-01-01",
        end="2026-01-02",
        symbols=(),
        max_symbol_days=100,
        min_complete_coverage=1.0,
    )
    row = report["datasets"][0]
    assert row["layout"] == "flat_symbol"
    assert row["status"] == "READY"
    assert row["complete_symbol_days"] == 1


def test_exact_grid_rejects_duplicate_timestamp_even_with_288_rows(tmp_path: Path) -> None:
    mod = _load()
    root = tmp_path / "bybit"
    _manifest(root, "2026-01-01", ["BTCUSDT"])
    path = root / "klines_5m" / "date=2026-01-01" / "symbol=BTCUSDT"
    path.mkdir(parents=True)
    timestamps = [_ts("2026-01-01", index * 5) for index in range(287)]
    timestamps.append(timestamps[-1])
    pl.DataFrame(
        {
            "symbol": ["BTCUSDT"] * 288,
            "ts_ms": timestamps,
            "open": [100.0] * 288,
            "high": [101.0] * 288,
            "low": [99.0] * 288,
            "close": [100.5] * 288,
        }
    ).write_parquet(path / "part.parquet")
    report = mod.audit_venue(
        "bybit",
        root,
        logical_names=("klines_5m",),
        start="2026-01-01",
        end="2026-01-02",
        symbols=(),
        max_symbol_days=100,
        min_complete_coverage=1.0,
    )
    row = report["datasets"][0]
    assert row["status"] == "INVALID_DATA"
    assert row["complete_symbol_days"] == 0
    assert row["partial_symbol_days"] == 1
    assert row["duplicate_rows"] == 1


def test_audit_cap_fails_before_unbounded_scan(tmp_path: Path) -> None:
    mod = _load()
    root = tmp_path / "root"
    _manifest(root, "2026-01-01", ["BTCUSDT", "ETHUSDT"])
    with pytest.raises(RuntimeError, match="above --max-symbol-days"):
        mod.load_manifest_pairs(
            root,
            start="2026-01-01",
            end="2026-01-02",
            max_symbol_days=1,
        )


def test_manifest_window_refuses_missing_requested_day(tmp_path: Path) -> None:
    mod = _load()
    root = tmp_path / "root"
    _manifest(root, "2026-01-01", ["BTCUSDT"])
    with pytest.raises(RuntimeError, match="missing PIT manifest date partition"):
        mod.load_manifest_pairs(root, start="2026-01-01", end="2026-01-03")


def test_manifest_window_refuses_unreadable_and_wrong_date_identity(tmp_path: Path) -> None:
    mod = _load()
    unreadable = tmp_path / "unreadable"
    bad_path = unreadable / "archive_trade_manifest" / "date=2026-01-01"
    bad_path.mkdir(parents=True)
    (bad_path / "part.parquet").write_bytes(b"not parquet")
    with pytest.raises(RuntimeError, match="unreadable PIT manifest partition"):
        mod.load_manifest_pairs(unreadable, start="2026-01-01", end="2026-01-02")

    wrong_date = tmp_path / "wrong-date"
    path = wrong_date / "archive_trade_manifest" / "date=2026-01-01"
    path.mkdir(parents=True)
    pl.DataFrame({"symbol": ["BTCUSDT"], "date": ["2026-01-02"], "url": ["fixture://bad"]}).write_parquet(
        path / "part.parquet"
    )
    with pytest.raises(RuntimeError, match="path/content identity mismatch"):
        mod.load_manifest_pairs(wrong_date, start="2026-01-01", end="2026-01-02")


def test_kline_schema_and_ohlc_content_fail_closed(tmp_path: Path) -> None:
    mod = _load()
    missing_schema = tmp_path / "missing-schema"
    _manifest(missing_schema, "2026-01-01", ["BTCUSDT"])
    path = missing_schema / "klines_5m" / "date=2026-01-01" / "symbol=BTCUSDT"
    path.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["BTCUSDT"],
            "ts_ms": [_ts("2026-01-01")],
            "open": [100.0],
            "low": [99.0],
            "close": [100.5],
        }
    ).write_parquet(path / "part.parquet")
    row = mod.audit_venue(
        "bybit",
        missing_schema,
        logical_names=("klines_5m",),
        start="2026-01-01",
        end="2026-01-02",
        symbols=(),
        max_symbol_days=10,
        min_complete_coverage=1.0,
    )["datasets"][0]
    assert row["status"] == "INVALID_DATA"
    assert "missing required column" in row["reason"]

    invalid_ohlc = tmp_path / "invalid-ohlc"
    _manifest(invalid_ohlc, "2026-01-01", ["BTCUSDT"])
    _partition(invalid_ohlc, "klines_5m", "2026-01-01", "BTCUSDT", 288)
    bad_file = invalid_ohlc / "klines_5m" / "date=2026-01-01" / "symbol=BTCUSDT" / "part.parquet"
    frame = pl.read_parquet(bad_file).with_columns(
        pl.when(pl.int_range(pl.len()) == 0).then(pl.lit(98.0)).otherwise(pl.col("high")).alias("high")
    )
    frame.write_parquet(bad_file)
    row = mod.audit_venue(
        "bybit",
        invalid_ohlc,
        logical_names=("klines_5m",),
        start="2026-01-01",
        end="2026-01-02",
        symbols=(),
        max_symbol_days=10,
        min_complete_coverage=1.0,
    )["datasets"][0]
    assert row["status"] == "INVALID_DATA"
    assert row["invalid_content_rows"] == 1
    assert "invalid_required_content" in row["invalid_reasons"]


def test_partition_audit_reads_every_fragment_and_detects_cross_fragment_duplicate(tmp_path: Path) -> None:
    mod = _load()
    root = tmp_path / "root"
    _manifest(root, "2026-01-01", ["BTCUSDT"])
    _partition(root, "klines_5m", "2026-01-01", "BTCUSDT", 288)
    pair_root = root / "klines_5m" / "date=2026-01-01" / "symbol=BTCUSDT"
    pl.read_parquet(pair_root / "part.parquet").head(1).write_parquet(pair_root / "second.parquet")
    row = mod.audit_venue(
        "bybit",
        root,
        logical_names=("klines_5m",),
        start="2026-01-01",
        end="2026-01-02",
        symbols=(),
        max_symbol_days=10,
        min_complete_coverage=1.0,
    )["datasets"][0]
    assert row["files"] == 2
    assert row["status"] == "INVALID_DATA"
    assert row["duplicate_rows"] == 1
    assert "duplicate_observation_key" in row["invalid_reasons"]


def test_dataset_partition_path_symbol_and_date_identity_are_enforced(tmp_path: Path) -> None:
    mod = _load()
    root = tmp_path / "root"
    _manifest(root, "2026-01-01", ["BTCUSDT"])
    path = root / "klines_5m" / "date=2026-01-01" / "symbol=BTCUSDT"
    path.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["ETHUSDT"],
            "ts_ms": [_ts("2026-01-02")],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
        }
    ).write_parquet(path / "part.parquet")
    row = mod.audit_venue(
        "bybit",
        root,
        logical_names=("klines_5m",),
        start="2026-01-01",
        end="2026-01-02",
        symbols=(),
        max_symbol_days=10,
        min_complete_coverage=1.0,
    )["datasets"][0]
    assert row["status"] == "INVALID_DATA"
    assert row["invalid_symbol_identity_rows"] == 1
    assert row["invalid_date_identity_rows"] == 1
    assert "path_symbol_identity_mismatch" in row["invalid_reasons"]
    assert "path_or_stored_date_identity_mismatch" in row["invalid_reasons"]


def test_funding_and_oi_content_contracts_fail_closed(tmp_path: Path) -> None:
    mod = _load()
    funding_root = tmp_path / "funding-root"
    _manifest(funding_root, "2026-01-01", ["BTCUSDT"])
    funding_path = funding_root / "funding" / "date=2026-01-01" / "symbol=BTCUSDT"
    funding_path.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["BTCUSDT"],
            "ts_ms": [_ts("2026-01-01")],
            "funding_rate": [float("nan")],
        }
    ).write_parquet(funding_path / "part.parquet")
    funding = mod.audit_venue(
        "bybit",
        funding_root,
        logical_names=("funding",),
        start="2026-01-01",
        end="2026-01-02",
        symbols=(),
        max_symbol_days=10,
        min_complete_coverage=1.0,
    )["datasets"][0]
    assert funding["status"] == "INVALID_DATA"
    assert funding["invalid_content_rows"] == 1

    oi_root = tmp_path / "oi-root"
    _manifest(oi_root, "2026-01-01", ["BTCUSDT"])
    oi_path = oi_root / "open_interest" / "date=2026-01-01" / "symbol=BTCUSDT"
    oi_path.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["BTCUSDT"] * 20,
            "ts_ms": [_ts("2026-01-01", index * 60) for index in range(20)],
            "open_interest": [1_000.0] * 20,
            "open_interest_value": [100_000.0] * 20,
            "open_interest_interval": ["1h"] * 19 + ["5min"],
        }
    ).write_parquet(oi_path / "part.parquet")
    oi = mod.audit_venue(
        "bybit",
        oi_root,
        logical_names=("open_interest",),
        start="2026-01-01",
        end="2026-01-02",
        symbols=(),
        max_symbol_days=10,
        min_complete_coverage=1.0,
    )["datasets"][0]
    assert oi["status"] == "INVALID_DATA"
    assert oi["mixed_constant_groups"] == 1
    assert "mixed_interval_or_constant_identity" in oi["invalid_reasons"]


def test_bookdepth_requires_ten_unique_bands_per_hour(tmp_path: Path) -> None:
    mod = _load()
    root = tmp_path / "binance"
    _manifest(root, "2026-01-01", ["BTCUSDT"])
    path = root / "binance_usdm_bookdepth_1h"
    path.mkdir()
    bands = ["-5", "-4", "-3", "-2", "-1", "1", "2", "3", "4", "5"]
    rows = [
        {
            "symbol": "BTCUSDT",
            "ts_ms": _ts("2026-01-01", hour * 60),
            "percentage": band,
            "depth_mean": 1.0,
            "notional_mean": 100.0,
            "depth_last": 1.0,
            "notional_last": 100.0,
            "n_snaps": 60,
        }
        for hour in range(24)
        for band in bands
    ]
    pl.DataFrame(rows).write_parquet(path / "BTCUSDT.parquet")
    ready = mod.audit_venue(
        "binance",
        root,
        logical_names=("bookdepth_1h",),
        start="2026-01-01",
        end="2026-01-02",
        symbols=(),
        max_symbol_days=10,
        min_complete_coverage=1.0,
    )["datasets"][0]
    assert ready["status"] == "READY"

    pl.DataFrame(rows[1:]).write_parquet(path / "BTCUSDT.parquet")
    invalid = mod.audit_venue(
        "binance",
        root,
        logical_names=("bookdepth_1h",),
        start="2026-01-01",
        end="2026-01-02",
        symbols=(),
        max_symbol_days=10,
        min_complete_coverage=1.0,
    )["datasets"][0]
    assert invalid["status"] == "INVALID_DATA"
    assert invalid["invalid_observations_per_timestamp"] == 1


def test_both_venue_roots_must_be_disjoint_including_resolved_child_scopes(tmp_path: Path) -> None:
    mod = _load()
    same = tmp_path / "same"
    same.mkdir()
    with pytest.raises(RuntimeError, match="must be disjoint"):
        mod.validate_disjoint_roots(same, same, ("funding",))

    nested = same / "nested"
    nested.mkdir()
    with pytest.raises(RuntimeError, match="must be disjoint"):
        mod.validate_disjoint_roots(same, nested, ("funding",))

    bybit = tmp_path / "bybit"
    binance = tmp_path / "binance"
    shared = tmp_path / "shared-funding"
    for path in (bybit, binance, shared):
        path.mkdir()
    (bybit / "funding").symlink_to(shared, target_is_directory=True)
    (binance / "binance_usdm_funding").symlink_to(shared, target_is_directory=True)
    with pytest.raises(RuntimeError, match="must be disjoint"):
        mod.validate_disjoint_roots(bybit, binance, ("funding",))


def test_receipt_path_is_new_json_and_outside_data_or_manifest_roots(tmp_path: Path) -> None:
    mod = _load()
    root = tmp_path / "data-root"
    root.mkdir()
    with pytest.raises(ValueError, match="outside every data root"):
        mod.validate_receipt_path(root / "reports" / "audit.json", data_roots=(root,))
    with pytest.raises(ValueError, match=r"\.json"):
        mod.validate_receipt_path(tmp_path / "audit.txt", data_roots=(root,))
    with pytest.raises(ValueError, match="manifest path"):
        mod.validate_receipt_path(
            tmp_path / "_download_markers" / "audit.json",
            data_roots=(root,),
        )
    existing = tmp_path / "existing.json"
    existing.write_text("owner-data", encoding="utf-8")
    with pytest.raises(FileExistsError, match="immutable"):
        mod.validate_receipt_path(existing, data_roots=(root,))
    assert existing.read_text(encoding="utf-8") == "owner-data"
    assert mod.validate_receipt_path(tmp_path / "new.json", data_roots=(root,)) == (tmp_path / "new.json").resolve()


def test_default_mode_never_runs_downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load()
    bybit = tmp_path / "bybit"
    binance = tmp_path / "binance"
    for root in (bybit, binance):
        _manifest(root, "2026-01-01", ["BTCUSDT"])
    monkeypatch.setattr(mod.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("network command ran"))
    output = tmp_path / "audit.json"
    rc = mod.main(
        [
            "--bybit-root",
            str(bybit),
            "--binance-root",
            str(binance),
            "--start",
            "2026-01-01",
            "--end",
            "2026-01-02",
            "--datasets",
            "klines_5m",
            "--output",
            str(output),
        ]
    )
    assert rc == 0
    payload = json.loads(output.read_text())
    assert payload["mode"] == "audit"
    assert payload["network_used"] is False
    assert payload["run_label"] == "data_readiness_only"


def test_execute_requires_bounds_scope_datasets_and_receipt(tmp_path: Path) -> None:
    mod = _load()
    with pytest.raises(ValueError, match="explicit --datasets"):
        mod.main(["--execute"])
    with pytest.raises(ValueError, match="explicit --start and --end"):
        mod.main(["--execute", "--datasets", "funding", "--output", str(tmp_path / "receipt.json")])


def test_command_builder_reuses_resume_safe_downloaders(tmp_path: Path) -> None:
    mod = _load()
    bybit = tmp_path / "bybit"
    bybit.mkdir()
    commands = mod.build_download_commands(
        "bybit",
        bybit,
        logical_names=("funding", "open_interest", "premium_index_1h"),
        start="2026-01-01",
        end="2026-01-02",
        symbols=("BTCUSDT",),
        workers=3,
        python_bin="python-fixture",
        bybit_oi_interval="5min",
    )
    assert len(commands) == 1
    assert commands[0][:5] == [
        "python-fixture",
        "-m",
        "liquidity_migration",
        "--data-root",
        str(bybit),
    ]
    assert commands[0][commands[0].index("--open-interest-interval") + 1] == "5min"
    requested = commands[0][commands[0].index("--datasets") + 1].split(",")
    assert requested == ["funding", "open_interest", "premium_index_1h"]


def test_binance_flat_5m_extension_is_refused(tmp_path: Path) -> None:
    mod = _load()
    root = tmp_path / "binance"
    _flat(root, "klines_5m", "2026-01-01", "BTCUSDT", 288)
    with pytest.raises(RuntimeError, match="must not be mixed with flat files"):
        mod.build_download_commands(
            "binance",
            root,
            logical_names=("klines_5m",),
            start="2026-01-01",
            end="2026-01-02",
            symbols=("BTCUSDT",),
            workers=1,
            python_bin="python-fixture",
            bybit_oi_interval="1h",
        )


def test_execution_receipt_checkpoints_and_stops_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load()
    returns = iter((0, 7))

    class Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    monkeypatch.setattr(mod.subprocess, "run", lambda *_args, **_kwargs: Result(next(returns)))
    output = tmp_path / "receipt.json"
    receipt = {"status": "running"}
    rc = mod._run_commands((("one",), ("two",), ("never",)), receipt, output)
    assert rc == 7
    saved = json.loads(output.read_text())
    assert saved["status"] == "failed"
    assert saved["failed_command_index"] == 1
    assert [row["status"] for row in saved["commands"]] == ["complete", "failed"]


def test_execution_receipt_never_replaces_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load()
    output = tmp_path / "receipt.json"
    output.write_text("operator-owned", encoding="utf-8")
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("command ran before immutable receipt refusal"),
    )
    with pytest.raises(FileExistsError):
        mod._run_commands((("never",),), {"status": "running"}, output)
    assert output.read_text(encoding="utf-8") == "operator-owned"


def test_forward_tapes_are_explicitly_non_acceptance(tmp_path: Path) -> None:
    mod = _load()
    tape = tmp_path / "bybit"
    tape.mkdir()
    (tape / "2026-01-01.jsonl").write_text("{}\n", encoding="utf-8")
    row = mod._audit_forward_tape(tape, "bybit_depth")
    assert row["status"] == "PRESENT"
    assert "never historical acceptance" in row["evidence_use"]
