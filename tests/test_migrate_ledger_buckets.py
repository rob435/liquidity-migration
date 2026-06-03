"""Tests for scripts/migrate_ledger_buckets.py (reconcile-ledger-5 / quality-dup-5).

The migration drains a legacy monolithic ledger part.parquet into month buckets. It
must be idempotent, dedup-safe (no double-count across the monolith+bucket window), and
must remove the legacy file ONLY after the deduped union is proven unchanged.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import polars as pl

from liquidity_migration.storage import _LEDGER_MONTH_COL, dataset_path, read_dataset

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "migrate_ledger_buckets.py"

_MS_PER_DAY = 86_400_000


def _load_module():
    spec = importlib.util.spec_from_file_location("migrate_ledger_buckets", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["migrate_ledger_buckets"] = module
    spec.loader.exec_module(module)
    return module


def _trade(trade_id: str, entry_ts_ms: int, **over) -> dict:
    row = {
        "trade_id": trade_id, "symbol": "AAAUSDT", "side": "short", "status": "open",
        "entry_ts_ms": entry_ts_ms, "ts_ms": entry_ts_ms, "qty": "1", "entry_price": 100.0,
        "updated_at_ms": entry_ts_ms,
    }
    row.update(over)
    return row


def _write_legacy_monolith(root: Path, dataset: str, rows: list[dict]) -> None:
    path = dataset_path(root, dataset)
    path.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(path / "part.parquet")


def test_migration_drains_monolith_into_buckets_preserving_union(tmp_path: Path) -> None:
    jan = 1_704_067_200_000           # 2024-01
    mar = jan + 70 * _MS_PER_DAY      # ~2024-03
    _write_legacy_monolith(
        tmp_path, "event_demo_trades",
        [_trade("t-jan", jan), _trade("t-mar", mar, status="closed")],
    )
    before = read_dataset(tmp_path, "event_demo_trades")
    mod = _load_module()

    did = mod.migrate_one(tmp_path, "event_demo_trades", dry_run=False)
    assert did is True

    root = dataset_path(tmp_path, "event_demo_trades")
    # Legacy monolith drained (removed); month buckets created.
    assert not (root / "part.parquet").exists()
    assert sorted(p.name for p in root.glob(f"{_LEDGER_MONTH_COL}=*")) == [
        f"{_LEDGER_MONTH_COL}=202401", f"{_LEDGER_MONTH_COL}=202403",
    ]
    # The deduped union is byte-for-byte unchanged in key-set + row-count.
    after = read_dataset(tmp_path, "event_demo_trades")
    assert after.height == before.height
    assert set(after["trade_id"].to_list()) == set(before["trade_id"].to_list())


def test_migration_is_idempotent_and_dedup_safe(tmp_path: Path) -> None:
    """Running twice must not double-count, and a trade_id already in a bucket coexisting
    with the legacy monolith collapses to the freshest version."""
    jan = 1_704_067_200_000
    _write_legacy_monolith(tmp_path, "event_demo_trades", [_trade("t1", jan, updated_at_ms=jan)])
    mod = _load_module()
    mod.migrate_one(tmp_path, "event_demo_trades", dry_run=False)
    # Re-run on a freshly-recreated legacy monolith carrying a NEWER version of t1.
    _write_legacy_monolith(tmp_path, "event_demo_trades", [_trade("t1", jan, status="closed", updated_at_ms=jan + 5000)])
    mod.migrate_one(tmp_path, "event_demo_trades", dry_run=False)
    out = read_dataset(tmp_path, "event_demo_trades")
    assert out.height == 1                       # not doubled across runs / monolith+bucket
    assert out["status"].to_list() == ["closed"]  # freshest version wins


def test_migration_dry_run_writes_nothing(tmp_path: Path) -> None:
    jan = 1_704_067_200_000
    _write_legacy_monolith(tmp_path, "event_demo_trades", [_trade("t1", jan)])
    mod = _load_module()
    mod.migrate_one(tmp_path, "event_demo_trades", dry_run=True)
    root = dataset_path(tmp_path, "event_demo_trades")
    assert (root / "part.parquet").exists()                       # monolith untouched
    assert not list(root.glob(f"{_LEDGER_MONTH_COL}=*"))          # no buckets written


def test_migration_skips_dataset_with_no_legacy_monolith(tmp_path: Path) -> None:
    mod = _load_module()
    assert mod.migrate_one(tmp_path, "event_demo_trades", dry_run=False) is False
