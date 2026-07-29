from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from liquidity_migration.continuous_profile import CONTINUOUS_HISTORICAL_RUN_LABEL

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "equity_curves.py"
SPEC = importlib.util.spec_from_file_location("equity_curves", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
equity_curves = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(equity_curves)


def test_deployed_sleeves_are_runner_sleeves() -> None:
    assert set(equity_curves.RUNNERS) == {"long", "continuous", "carry"}


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

    assert payload["run_label"] == CONTINUOUS_HISTORICAL_RUN_LABEL
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


def test_prepare_sleeve_output_removes_only_requested_derived_tree(tmp_path: Path) -> None:
    sleeve = tmp_path / "equity_curves" / "long"
    sibling = tmp_path / "equity_curves" / "continuous" / "keep.txt"
    sleeve.mkdir(parents=True)
    sibling.parent.mkdir(parents=True)
    (sleeve / "partial-journal.jsonl").write_text("stale", encoding="utf-8")
    sibling.write_text("keep", encoding="utf-8")

    equity_curves._prepare_sleeve_output(sleeve, fresh=True)

    assert sleeve.is_dir()
    assert list(sleeve.iterdir()) == []
    assert sibling.read_text(encoding="utf-8") == "keep"


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
    payload = equity_curves._run_continuous(
        str(root),
        costs=object(),
        start="2023-06-01",
        end="2026-06-12",
        out=out,
        pit_tol=0.0,
        venue="bybit",
        render_only=True,
        chart_leverage=2.5,
        backtest_leverage=5.0,
    )

    assert captured == {
        "venue": "bybit",
        "output_root": out,
        "start_date": "2023-06-01",
        "end_date": "2026-06-12",
        "render_only": True,
        "data_root": root,
        "chart_leverage": 2.5,
        "backtest_leverage": 5.0,
        "research_disable_btc_gate": False,
    }
    assert payload["run_label"] == CONTINUOUS_HISTORICAL_RUN_LABEL
    assert payload["summary"]["total_return"] == 0.1


def test_main_returns_nonzero_when_a_requested_sleeve_fails(monkeypatch, tmp_path: Path) -> None:
    def fail_long(*_args, **_kwargs):
        raise RuntimeError("deliberate cell failure")

    monkeypatch.setattr(equity_curves, "_run_long", fail_long)
    monkeypatch.setattr(
        equity_curves,
        "load_config",
        lambda _path: SimpleNamespace(costs=object()),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "equity_curves",
            "--sleeves",
            "long",
            "--root",
            str(tmp_path / "root"),
            "--out",
            str(tmp_path / "reports"),
            "--start",
            "2023-07-16",
            "--end",
            "2026-07-16",
        ],
    )

    assert equity_curves.main() == 1
