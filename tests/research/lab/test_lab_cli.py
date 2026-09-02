from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import polars as pl

from liquidity_migration.research.lab.cli import build_parser, main
from liquidity_migration.research.lab.panel import FROZEN_COLUMNS

DAY = 86_400_000
H = 3_600_000
T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC
REPO = Path(__file__).resolve().parents[3]


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "klines_1h").mkdir(parents=True)
    (root / "funding").mkdir()
    rows = []
    for d in range(3):
        for h in range(24):
            px = 100.0 + d + h * 0.01
            rows.append(dict(ts_ms=T0 + d * DAY + h * H, symbol="AAA", open=px, high=px + 1, low=px - 1, close=px,
                             turnover_quote=5.0))
    pl.DataFrame(rows).write_parquet(root / "klines_1h" / "part.parquet")
    pl.DataFrame(dict(ts_ms=[T0 + k * 8 * H for k in range(10)], symbol=["AAA"] * 10, funding_rate=[1e-4] * 10)).write_parquet(
        root / "funding" / "part.parquet"
    )
    return root


def test_dump_then_panel_end_to_end(tmp_path: Path, capsys) -> None:
    root = _root(tmp_path)
    lab = tmp_path / "lab"
    assert main(["dump", "--data-root", str(root), "--out", str(lab), "--datasets", "klines_1h", "funding",
                 "--start", "2024-01-02", "--end", "2024-01-03"]) == 0
    out = capsys.readouterr().out
    assert f"klines_1h: {lab / 'inputs' / 'klines_1h.parquet'}" in out
    assert pl.read_parquet(lab / "inputs" / "klines_1h.parquet")["ts_ms"].to_list() == [T0 + DAY + h * H for h in range(24)]
    panel_path = lab / "panel" / "daily.parquet"
    assert main(["panel", "--inputs", str(lab / "inputs"), "--out", str(panel_path)]) == 0
    assert "1 rows, 1 symbols, 2024-01-02 to 2024-01-02" in capsys.readouterr().out
    panel = pl.read_parquet(panel_path)
    assert panel.columns == list(FROZEN_COLUMNS)
    # the 08:00 and 16:00 settlements; the midnight one that closes 01-02 is stamped 01-03 and the end is exclusive
    assert panel["n_settle"].to_list() == [2]


def test_parser_requires_a_command_and_module_runs_with_dash_m() -> None:
    parser = build_parser()
    args = parser.parse_args(["dump", "--data-root", "r", "--out", "o", "--force"])
    assert args.command == "dump" and args.force and args.datasets == ["klines_1h", "funding", "open_interest", "premium_index_1h"]
    completed = subprocess.run(
        [sys.executable, "-m", "liquidity_migration.research.lab.cli", "--help"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0
    assert "dump" in completed.stdout and "panel" in completed.stdout
