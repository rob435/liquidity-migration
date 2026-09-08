from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "research" / "equity_curves.py"
SPEC = importlib.util.spec_from_file_location("equity_curves", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
equity_curves = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(equity_curves)


def test_deployed_sleeves_are_runner_sleeves() -> None:
    assert set(equity_curves.RUNNERS) == {"long", "carry"}


def test_find_png_prefers_unlevered_chart(tmp_path: Path) -> None:
    unlevered = tmp_path / "long_native_equity_btc.png"
    levered = tmp_path / "long_native_equity_btc_4x.png"
    levered.write_bytes(b"4x")
    unlevered.write_bytes(b"1x")

    assert equity_curves._find_png(tmp_path) == unlevered


def test_find_png_prefers_book_chart_over_component_chart(tmp_path: Path) -> None:
    book = tmp_path / "long" / "long_native_equity_btc.png"
    component = tmp_path / "components" / "long" / "part_a" / "long_native_equity_btc.png"
    component.parent.mkdir(parents=True)
    book.parent.mkdir(parents=True)
    component.write_bytes(b"component")
    book.write_bytes(b"book")

    assert equity_curves._find_png(tmp_path) == book


def test_prepare_sleeve_output_removes_only_requested_derived_tree(tmp_path: Path) -> None:
    sleeve = tmp_path / "equity_curves" / "long"
    sibling = tmp_path / "equity_curves" / "carry" / "keep.txt"
    sleeve.mkdir(parents=True)
    sibling.parent.mkdir(parents=True)
    (sleeve / "partial-journal.jsonl").write_text("stale", encoding="utf-8")
    sibling.write_text("keep", encoding="utf-8")

    equity_curves._prepare_sleeve_output(sleeve, fresh=True)

    assert sleeve.is_dir()
    assert list(sleeve.iterdir()) == []
    assert sibling.read_text(encoding="utf-8") == "keep"


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


def test_explicit_all_in_cost_is_charged_per_side_without_legacy_multiplier(monkeypatch, tmp_path: Path) -> None:
    from liquidity_migration.research.backtest import long_native
    observed = {}

    def run(_root, **kwargs):
        observed.update(kwargs)
        return {}

    monkeypatch.setattr(long_native, "run_long_native_research", run)
    equity_curves._run_long(
        str(tmp_path), SimpleNamespace(base_entry_exit_cost_bps=15.0),
        "2025-09-08", "2026-09-08", tmp_path, 0.0,
        all_in_cost_bps=14.35751876,
    )
    assert observed["effective_config"].round_trip_cost_bps == 28.71503752
    assert observed["config"].cost_multiplier == 3.0


def test_delisted_trades_do_not_turn_failed_pit_coverage_into_a_pass() -> None:
    verdict = equity_curves._pit_verdict("pit_membership_filtered_current_universe", 27)
    assert "[OK]" not in verdict
    assert "coverage failed" in verdict
