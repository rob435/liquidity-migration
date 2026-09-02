from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.research.lab.dumps import PANEL_COLUMNS, dump_inputs, dump_path

DAY = 86_400_000
H = 3_600_000
T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "klines_1h").mkdir(parents=True)
    (root / "funding").mkdir()
    pl.DataFrame(
        dict(ts_ms=[T0 + i * H for i in range(48)], symbol=["AAA"] * 48, open=[1.0] * 48, high=[2.0] * 48,
             low=[0.5] * 48, close=[1.5] * 48, turnover_quote=[10.0] * 48, volume_base=[7.0] * 48)
    ).write_parquet(root / "klines_1h" / "part.parquet")
    pl.DataFrame(
        dict(ts_ms=[T0 + k * 8 * H for k in range(7)], symbol=["AAA"] * 7, funding_rate=[1e-4] * 7,
             funding_interval_min=[480] * 7)
    ).write_parquet(root / "funding" / "part.parquet")
    return root


def test_dump_writes_one_projected_parquet_per_dataset(tmp_path: Path) -> None:
    root = _root(tmp_path)
    written = dump_inputs(root, tmp_path / "lab", datasets=("klines_1h", "funding"))
    assert written == {"klines_1h": dump_path(tmp_path / "lab", "klines_1h"), "funding": dump_path(tmp_path / "lab", "funding")}
    klines = pl.read_parquet(written["klines_1h"])
    assert klines.columns == list(PANEL_COLUMNS["klines_1h"])
    assert klines.height == 48
    funding = pl.read_parquet(written["funding"])
    assert funding.columns == list(PANEL_COLUMNS["funding"])
    assert funding.height == 7
    assert not list((tmp_path / "lab" / "inputs").glob("*.partial"))


def test_existing_dump_is_kept_unless_forced(tmp_path: Path) -> None:
    root = _root(tmp_path)
    first = dump_inputs(root, tmp_path / "lab", datasets=("funding",))["funding"]
    pl.DataFrame(dict(ts_ms=[T0], symbol=["BBB"], funding_rate=[5e-4])).write_parquet(root / "funding" / "part.parquet")
    dump_inputs(root, tmp_path / "lab", datasets=("funding",))
    assert pl.read_parquet(first)["symbol"].to_list() == ["AAA"] * 7
    dump_inputs(root, tmp_path / "lab", datasets=("funding",), force=True)
    assert pl.read_parquet(first)["symbol"].to_list() == ["BBB"]


def test_start_is_inclusive_and_end_exclusive(tmp_path: Path) -> None:
    root = _root(tmp_path)
    written = dump_inputs(root, tmp_path / "lab", datasets=("klines_1h",), start_ms=T0 + 10 * H, end_ms=T0 + 20 * H)
    ts = pl.read_parquet(written["klines_1h"])["ts_ms"].to_list()
    assert ts == [T0 + i * H for i in range(10, 20)]


def test_a_dataset_with_nothing_to_read_is_an_error(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with pytest.raises(FileNotFoundError):
        dump_inputs(root, tmp_path / "lab", datasets=("open_interest",))
    with pytest.raises(ValueError):
        dump_inputs(root, tmp_path / "lab", datasets=("not_a_dataset",))
