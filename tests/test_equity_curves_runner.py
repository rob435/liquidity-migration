from __future__ import annotations

import importlib.util
import sys
from types import SimpleNamespace
from pathlib import Path

from liquidity_migration import promoted


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "equity_curves.py"
SPEC = importlib.util.spec_from_file_location("equity_curves", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
equity_curves = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(equity_curves)


def test_continuous_is_runner_sleeve_and_promoted_profile() -> None:
    # CONTINUOUS is both an equity-tool runner sleeve AND (since the 2026-06-15
    # registry) an active profile.
    assert set(equity_curves.RUNNERS) == {"long", "continuous"}
    assert promoted.PROFILES == {
        "long": promoted.long_profile,
        "continuous": promoted.continuous_profile,
    }


def test_continuous_venue_inference_from_root() -> None:
    assert equity_curves._infer_venue_from_root("~/SHARED_DATA/bybit_full_pit") == "bybit"
    assert equity_curves._infer_venue_from_root("~/SHARED_DATA/binance_full_pit") == "binance"
    assert equity_curves._infer_venue_from_root("/tmp/custom-root", explicit="binance") == "binance"


def test_continuous_payload_normalizes_percent_stats(tmp_path: Path) -> None:
    payload = equity_curves._continuous_payload_from_summary(
        {
            "1x": {
                "total_return_pct": 12.5,
                "max_drawdown_pct": -4.0,
                "mar": 3.1,
                "sharpe_daily_ann": 1.8,
            }
        },
        report_dir=tmp_path,
    )

    assert payload["run_label"] == "continuous_demo_paper_research_stage"
    assert payload["summary"] == {
        "total_return": 0.125,
        "max_drawdown": -0.04,
        "mar": 3.1,
        "sharpe_like": 1.8,
    }


def test_find_png_prefers_unlevered_chart(tmp_path: Path) -> None:
    unlevered = tmp_path / "continuous_equity_btc.png"
    levered = tmp_path / "continuous_equity_btc_4x.png"
    levered.write_bytes(b"4x")
    unlevered.write_bytes(b"1x")

    assert equity_curves._find_png(tmp_path) == unlevered


def test_find_png_prefers_book_chart_over_component_chart(tmp_path: Path) -> None:
    book = tmp_path / "bybit" / "continuous_equity_btc.png"
    component = tmp_path / "components" / "bybit" / "merged_signal" / "continuous_equity_btc.png"
    component.parent.mkdir(parents=True)
    book.parent.mkdir(parents=True)
    component.write_bytes(b"component")
    book.write_bytes(b"book")

    assert equity_curves._find_png(tmp_path) == book


def test_run_continuous_delegates_to_refresh(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    def fake_run_venue(**kwargs):
        msg = "run_venue should be called with venue as positional arg"
        raise AssertionError(msg)

    def fake_run_venue_positional(venue, **kwargs):
        captured["venue"] = venue
        captured.update(kwargs)
        return {"1x": {"total_return_pct": 10.0, "max_drawdown_pct": -5.0, "mar": 2.0}}

    monkeypatch.setitem(
        sys.modules,
        "continuous_deployed_equity_refresh",
        SimpleNamespace(run_venue=fake_run_venue_positional, unused=fake_run_venue),
    )

    root = tmp_path / "bybit_full_pit"
    out = tmp_path / "reports"
    fallback = tmp_path / "fallback"
    payload = equity_curves._run_continuous(
        str(root),
        costs=object(),
        start="2023-06-01",
        end="2026-06-12",
        out=out,
        pit_tol=0.0,
        venue="bybit",
        render_only=True,
        frozen_fallback=fallback,
        chart_leverage=2.5,
        component_take_profit_pct=0.12,
        btc_risk_sizing=True,
        backtest_leverage=5.0,
    )

    assert captured == {
        "venue": "bybit",
        "output_root": out,
        "start_date": "2023-06-01",
        "end_date": "2026-06-12",
        "render_only": True,
        "frozen_fallback": fallback,
        "data_root": root,
        "chart_leverage": 2.5,
        "component_take_profit_pct": 0.12,
        "btc_risk_sizing": True,
        "backtest_leverage": 5.0,
    }
    assert payload["run_label"] == "continuous_demo_paper_research_stage"
    assert payload["summary"]["total_return"] == 0.1
