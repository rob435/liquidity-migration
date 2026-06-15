"""Regression: the continuous equity path must inherit the frozen deployed start
(start_date=None) unless the user explicitly asks for a window via --start/--years.

Before the audit2c fix, ``main`` unconditionally computed a rolling 3y start and
passed it as ``start_date`` to ``continuous_refresh.run_venue``, overriding the
frozen continuous component start (~2023-04). These tests pin the corrected
routing and FAIL on the old code (which always passed a concrete date).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "equity_curves.py"
SPEC = importlib.util.spec_from_file_location("equity_curves", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
equity_curves = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(equity_curves)


def _drive_main(monkeypatch, tmp_path: Path, argv: list[str]) -> dict[str, object]:
    """Run ``main`` for the continuous sleeve and capture run_venue's kwargs."""
    captured: dict[str, object] = {}

    def fake_run_venue(venue, **kwargs):
        captured["venue"] = venue
        captured.update(kwargs)
        return {"1x": {"total_return_pct": 10.0, "max_drawdown_pct": -5.0, "mar": 2.0}}

    monkeypatch.setitem(
        sys.modules,
        "continuous_deployed_equity_refresh",
        SimpleNamespace(run_venue=fake_run_venue),
    )
    # Keep the test hermetic: no YAML read, no matplotlib fallback render.
    monkeypatch.setattr(equity_curves, "load_config", lambda _path: SimpleNamespace(costs=object()))
    monkeypatch.setattr(equity_curves, "_find_png", lambda _out: None)
    monkeypatch.setattr(equity_curves, "_plot_equity_csv", lambda _out, _sleeve: None)

    root = tmp_path / "bybit_full_pit"
    out = tmp_path / "reports"
    full_argv = [
        "equity_curves",
        "--sleeves",
        "continuous",
        "--root",
        str(root),
        "--out",
        str(out),
        "--end",
        "2026-06-12",
        *argv,
    ]
    monkeypatch.setattr(sys, "argv", full_argv)
    assert equity_curves.main() == 0
    return captured


def test_continuous_inherits_frozen_start_when_window_unset(monkeypatch, tmp_path: Path) -> None:
    captured = _drive_main(monkeypatch, tmp_path, argv=[])
    # The frozen deployed start (~2023-04) is preserved, NOT a rolling 3y override.
    assert captured["start_date"] is None
    assert captured["venue"] == "bybit"


def test_continuous_uses_explicit_start_when_given(monkeypatch, tmp_path: Path) -> None:
    captured = _drive_main(monkeypatch, tmp_path, argv=["--start", "2024-01-15"])
    assert captured["start_date"] == "2024-01-15"


def test_continuous_uses_rolling_start_when_years_given(monkeypatch, tmp_path: Path) -> None:
    # Explicit --years opts into the rolling window: a concrete date flows through.
    captured = _drive_main(monkeypatch, tmp_path, argv=["--years", "2"])
    start = captured["start_date"]
    assert isinstance(start, str)
    assert start != "" and start[:2] == "20"  # a real YYYY-MM-DD, not None
