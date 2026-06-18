from __future__ import annotations

import importlib.util
from pathlib import Path

import polars as pl
import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "continuous_deployed_equity_refresh.py"
SPEC = importlib.util.spec_from_file_location("continuous_deployed_equity_refresh", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
refresh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refresh)


def _write_trades(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_csv(path)


def test_monthly_trade_counts_dedupes_component_overlap(tmp_path: Path) -> None:
    output_root = tmp_path / "continuous"
    venue_root = output_root / "components" / "bybit"
    cells = {refresh.CONTINUOUS_COMPONENT_SOURCES[name].cell for name in refresh.WINNER_WEIGHTS}
    first, second = sorted(cells)[:2]
    duplicate = {
        "entry_ts_ms": 1_682_640_000_000,
        "symbol": "BTCUSDT",
        "side": "short",
    }
    _write_trades(venue_root / first / "continuous_trades.csv", [duplicate])
    _write_trades(
        venue_root / second / "continuous_trades.csv",
        [
            duplicate,
            {
                "entry_ts_ms": 1_685_318_400_000,
                "symbol": "ETHUSDT",
                "side": "short",
            },
        ],
    )

    rows = refresh.monthly_trade_counts(output_root=output_root, venue="bybit").to_dicts()

    assert rows == [{"month": "2023-04", "trades": 1}, {"month": "2023-05", "trades": 1}]


def test_monthly_returns_with_trades_uses_trade_counts() -> None:
    equity = pl.DataFrame(
        {
            "date": ["2023-04-05", "2023-04-06", "2023-05-01"],
            "basket_return": [0.10, -0.05, 0.02],
        }
    )
    trades = pl.DataFrame({"month": ["2023-04"], "trades": [7]})

    rows = refresh.monthly_returns_with_trades(equity, trades).to_dicts()

    assert rows[0]["month"] == "2023-04"
    assert rows[0]["strategy_return"] == pytest.approx(0.045)
    assert rows[0]["trades"] == 7
    assert rows[1]["month"] == "2023-05"
    assert rows[1]["strategy_return"] == pytest.approx(0.02)
    assert rows[1]["trades"] == 0


def test_chart_leverages_always_include_one_x() -> None:
    assert refresh.chart_leverages(4.0) == (1.0, 4.0)
    assert refresh.chart_leverages(1.0) == (1.0,)


def test_stats_sharpe_none_on_flat_series() -> None:
    day = 86_400_000
    df = pl.DataFrame({"ts_ms": [i * day for i in range(5)], "basket_return": [0.0] * 5})
    assert refresh.stats(df)["sharpe_daily_ann"] is None


def test_stats_sharpe_uses_sample_std() -> None:
    day = 86_400_000
    rets = [0.01, -0.02, 0.03, -0.01, 0.02]
    df = pl.DataFrame({"ts_ms": [i * day for i in range(5)], "basket_return": rets})
    s = refresh.stats(df)["sharpe_daily_ann"]
    assert s is not None and isinstance(s, float)


def test_deployed_equity_literals_match_frozen_config() -> None:
    from liquidity_migration.continuous_forward_replay import (
        FROZEN_FORWARD_CONFIG,
        frozen_hedge_rule,
        frozen_rebalance_rule,
    )
    from liquidity_migration.continuous_rebalance import ContinuousHedgeRule

    assert refresh.WINNER_WEIGHTS == FROZEN_FORWARD_CONFIG["weights"]
    assert refresh.winner_rule() == frozen_rebalance_rule()
    assert ContinuousHedgeRule(90, 60, 2.0, 5.0) == frozen_hedge_rule()
