from __future__ import annotations

import importlib.util
import json
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


def test_component_overrides_model_take_profit_and_leverage() -> None:
    from liquidity_migration.continuous_events import ContinuousEventConfig

    cfg = ContinuousEventConfig(take_profit_pct=0.10, gross_exposure=0.5)

    out = refresh._with_optional_component_overrides(
        cfg,
        component_take_profit_pct=0.12,
        backtest_leverage=5.0,
    )

    assert out.take_profit_pct == pytest.approx(0.12)
    assert out.gross_exposure == pytest.approx(2.5)


def test_btc_trend_gate_override_changes_config_hash() -> None:
    from liquidity_migration.continuous_events import ContinuousEventConfig

    cfg = ContinuousEventConfig(btc_trend_gate="uptrend")
    out = refresh._with_btc_trend_gate(cfg, "off")

    assert out.btc_trend_gate == "off"
    assert out.config_hash() != cfg.config_hash()


def test_component_report_match_checks_tp_and_gross_exposure() -> None:
    payload = {"config": {"start_date": "2023-04-01", "end_date": "2026-06-25", "take_profit_pct": 0.12, "gross_exposure": 2.5}}

    assert refresh.component_report_matches_window(
        payload,
        start_date="2023-04-01",
        end_date="2026-06-25",
        component_take_profit_pct=0.12,
        expected_gross_exposure=2.5,
    )
    assert not refresh.component_report_matches_window(
        payload,
        start_date="2023-04-01",
        end_date="2026-06-25",
        component_take_profit_pct=0.10,
        expected_gross_exposure=2.5,
    )


def test_write_continuous_equity_report_emits_auditable_artifacts(tmp_path: Path) -> None:
    output_root = tmp_path / "continuous"
    out_dir = output_root / "bybit"
    component_dir = output_root / "components" / "bybit" / "merged_signal"
    component_dir.mkdir(parents=True)
    out_dir.mkdir(parents=True)
    (component_dir / "continuous_report.json").write_text(
        json.dumps(
            {
                "config": {
                    "take_profit_pct": 0.12,
                    "btc_trend_gate": "off",
                    "gross_exposure": 2.5,
                    "taker_fee_bps": 5.5,
                    "spread_bps": 2.5,
                    "impact_coef_bps": 50.0,
                    "impact_exponent": 0.5,
                    "use_funding": True,
                },
                "config_hash": "abc123",
                "n_trades": 3,
                "funding_mode": "modeled",
                "metrics": {"full": {"total_return": 0.02, "max_drawdown": -0.01}},
            }
        ),
        encoding="utf-8",
    )
    df = pl.DataFrame(
        {
            "ts_ms": [1_700_000_000_000, 1_700_086_400_000],
            "basket_return": [0.01, 0.02],
            "equity": [1.01, 1.0302],
        }
    )
    venue_summary = {"1x": refresh.stats(df)}

    refresh.write_continuous_equity_report(
        venue="bybit",
        output_root=output_root,
        out_dir=out_dir,
        data_root=tmp_path / "bybit_full_pit",
        end_date="2026-06-26",
        start_date="2023-04-01",
        venue_summary=venue_summary,
        df=df,
        chart_leverage=1.0,
        component_take_profit_pct=0.12,
        btc_risk_sizing=True,
        backtest_leverage=5.0,
    )

    report = (out_dir / "continuous_equity_report.md").read_text(encoding="utf-8")
    summary = json.loads((out_dir / "continuous_equity_summary.json").read_text(encoding="utf-8"))
    assert "Run label: exploratory" in report
    assert "Data root:" in report
    assert "BTC trend gate: off" in report
    assert "## Cost Model" in report
    assert "OOS window:" in report
    assert summary["backtest_leverage"] == pytest.approx(5.0)
    assert summary["btc_trend_gate"] == "off"
    assert summary["final_equity"] == pytest.approx(1.0302)
    assert summary["funding_modes"] == ["modeled"]
