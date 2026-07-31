from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import polars as pl
import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "research" / "continuous_deployed_equity_refresh.py"
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
    cells = sorted(
        {
            refresh.ACTIVE_CONTINUOUS_COMPONENT_BY_KEY[name].artifact_cell
            for name in refresh.WINNER_WEIGHTS
        }
    )
    duplicate = {
        "entry_ts_ms": 1_682_640_000_000,
        "symbol": "BTCUSDT",
        "side": "short",
    }
    # One duplicated (entry, symbol, side) row per active cell plus a repeat in
    # the first cell: the count must collapse the overlap however many active
    # component books exist.
    _write_trades(
        venue_root / cells[0] / "continuous_trades.csv",
        [
            duplicate,
            duplicate,
            {
                "entry_ts_ms": 1_685_318_400_000,
                "symbol": "ETHUSDT",
                "side": "short",
            },
        ],
    )
    for cell in cells[1:]:
        _write_trades(venue_root / cell / "continuous_trades.csv", [duplicate])

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


def test_deployed_equity_literals_match_active_config() -> None:
    from liquidity_migration.continuous_profile import (
        ACTIVE_CONTINUOUS_CONFIG,
        active_hedge_rule,
        active_rebalance_rule,
    )
    from liquidity_migration.continuous_rebalance import ContinuousHedgeRule

    assert refresh.WINNER_WEIGHTS == ACTIVE_CONTINUOUS_CONFIG["weights"]
    assert refresh.winner_rule() == active_rebalance_rule()
    assert ContinuousHedgeRule(90, 60, 2.0, 5.0) == active_hedge_rule()


def test_active_component_config_is_code_defined_tp12() -> None:
    from liquidity_migration.continuous_profile import (
        ACTIVE_CONTINUOUS_COMPONENT_BY_KEY,
        CONTINUOUS_PROFILE_ID,
        CONTINUOUS_PROFILE_REVISION,
    )

    for key, component in ACTIVE_CONTINUOUS_COMPONENT_BY_KEY.items():
        cfg = refresh.active_component_config(
            key,
            start_date="2024-01-01",
            end_date="2026-06-25",
            backtest_leverage=5.0,
        )
        assert cfg.profile_id == CONTINUOUS_PROFILE_ID
        assert cfg.profile_revision == CONTINUOUS_PROFILE_REVISION
        assert cfg.component_key == key
        assert cfg.entry_event_trigger == component.entry_event_trigger
        assert cfg.age_days_min == component.age_days_min
        assert cfg.take_profit_pct == pytest.approx(0.12)
        assert cfg.funding_min_at_entry == component.funding_min_at_entry
        assert cfg.btc_trend_gate == "uptrend"
        assert cfg.gross_exposure == pytest.approx(2.5)


def test_component_report_match_checks_hash_and_gross_exposure() -> None:
    payload = {
        "config_hash": "active-hash",
        "config": {"start_date": "2023-04-01", "end_date": "2026-06-25", "take_profit_pct": 0.12, "gross_exposure": 2.5}
    }

    assert refresh.component_report_matches_window(
        payload,
        start_date="2023-04-01",
        end_date="2026-06-25",
        expected_gross_exposure=2.5,
        expected_config_hash="active-hash",
    )
    assert not refresh.component_report_matches_window(
        payload,
        start_date="2023-04-01",
        end_date="2026-06-25",
        expected_gross_exposure=2.5,
        expected_config_hash="wrong-hash",
    )


def test_non_active_component_receipt_is_rejected() -> None:
    from liquidity_migration.continuous_profile import ACTIVE_CONTINUOUS_COMPONENT_BY_KEY

    payloads = []
    for component in ACTIVE_CONTINUOUS_COMPONENT_BY_KEY.values():
        payloads.append(
            {
                "_component": component.artifact_cell,
                "config": {
                    "profile_id": "",
                    "profile_revision": "",
                    "component_key": component.key,
                    "entry_event_trigger": component.entry_event_trigger,
                    "age_days_min": component.age_days_min,
                    "take_profit_pct": 0.10,
                    "btc_trend_gate": "uptrend",
                },
            }
        )

    with pytest.raises(RuntimeError, match="not the code-defined active continuous component"):
        refresh._assert_active_component_reports(payloads)


def test_strict_hedge_inputs_refuse_missing_returns_and_funding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = 86_400_000
    days = [1_700_006_400_000, 1_700_006_400_000 + day]
    panel = pl.DataFrame(
        {
            "symbol": ["BTCUSDT"],
            "d": [pl.Series(["2023-11-14"]).str.to_date()[0]],
            "close": [35_000.0],
        }
    )
    monkeypatch.setattr(refresh, "funding_root", lambda venue, data_root=None: tmp_path / "funding")

    with pytest.raises(RuntimeError, match="strict bybit BTCUSDT hedge coverage failed"):
        refresh.instrument_inputs(
            "bybit",
            days,
            "BTCUSDT",
            panel,
            data_root=tmp_path,
            strict_coverage=True,
        )


def test_write_continuous_equity_report_emits_auditable_artifacts(tmp_path: Path) -> None:
    from liquidity_migration.continuous_profile import (
        ACTIVE_CONTINUOUS_COMPONENT_BY_KEY,
        CONTINUOUS_HISTORICAL_RUN_LABEL,
        CONTINUOUS_PROFILE_ID,
        CONTINUOUS_PROFILE_REVISION,
    )

    output_root = tmp_path / "continuous"
    out_dir = output_root / "bybit"
    out_dir.mkdir(parents=True)
    for component in ACTIVE_CONTINUOUS_COMPONENT_BY_KEY.values():
        component_dir = output_root / "components" / "bybit" / component.artifact_cell
        component_dir.mkdir(parents=True)
        (component_dir / "continuous_report.json").write_text(
            json.dumps({
                "config": {
                    "profile_id": CONTINUOUS_PROFILE_ID,
                    "profile_revision": CONTINUOUS_PROFILE_REVISION,
                    "component_key": component.key,
                    "entry_event_trigger": component.entry_event_trigger,
                    "age_days_min": component.age_days_min,
                    "take_profit_pct": component.take_profit_pct,
                    "stop_loss_pct": component.stop_loss_pct,
                    "funding_min_at_entry": component.funding_min_at_entry,
                    "btc_trend_gate": "uptrend",
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
            }),
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
        backtest_leverage=5.0,
    )

    report = (out_dir / "continuous_equity_report.md").read_text(encoding="utf-8")
    summary = json.loads((out_dir / "continuous_equity_summary.json").read_text(encoding="utf-8"))
    assert "Run label: exploratory_historical_equity" in report
    assert f"Strategy run label: {CONTINUOUS_HISTORICAL_RUN_LABEL}" in report
    assert "Config authority: liquidity_migration.continuous_profile" in report
    assert "Data root:" in report
    assert "BTC trend gate: uptrend" in report
    assert "## Cost Model" in report
    assert "OOS window:" in report
    assert summary["backtest_leverage"] == pytest.approx(5.0)
    assert summary["strategy_profile"] == CONTINUOUS_PROFILE_ID
    assert summary["profile_revision"] == CONTINUOUS_PROFILE_REVISION
    assert summary["btc_trend_gate"] == "uptrend"
    assert "btc_risk_sizing" not in summary
    assert summary["final_equity"] == pytest.approx(1.0302)
    assert summary["funding_modes"] == ["modeled"]
