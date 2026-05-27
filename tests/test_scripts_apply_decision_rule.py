"""Pin the Strictness Manifesto decision-rule analyzer.

Specifically validates against the known 2026-05-28 sweep outcome (REJECTED,
zero candidates) and against a synthetic "would-be candidate" cell that
passes every gate, to confirm both branches work.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "apply_decision_rule.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("apply_decision_rule", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["apply_decision_rule"] = module
    spec.loader.exec_module(module)
    return module


MOD = _load_module()


def _write_csv(tmp_path: Path, rows: list[dict[str, str | int | float]]) -> Path:
    path = tmp_path / "summary.csv"
    header = "avg_split_sharpe,cell_id,description,elapsed_seconds,max_drawdown,promotable,report_dir,sharpe_like,status,total_return,trades,venue,worst_90d\n"
    path.write_text(header + "\n".join(_row_to_csv(r) for r in rows) + "\n")
    return path


def _row_to_csv(row: dict[str, str | int | float]) -> str:
    defaults = {
        "avg_split_sharpe": 0.0,
        "description": "",
        "elapsed_seconds": 0.0,
        "promotable": False,
        "report_dir": "",
        "status": "ok",
        "worst_90d": 0.0,
    }
    merged = {**defaults, **row}
    cols = ["avg_split_sharpe", "cell_id", "description", "elapsed_seconds",
            "max_drawdown", "promotable", "report_dir", "sharpe_like", "status",
            "total_return", "trades", "venue", "worst_90d"]
    return ",".join(str(merged[c]) for c in cols)


def test_real_2026_05_28_sweep_zero_candidates(tmp_path):
    """The 2026-05-28 sweep verdict was REJECTED. Pin that exactly."""
    # Real numbers transcribed from docs/preregistration/2026-05-28-…/Post-run results.
    rows = [
        {"cell_id": "00_baseline", "venue": "bybit", "sharpe_like": 2.27, "max_drawdown": -0.4211, "trades": 416, "total_return": 5.1876},
        {"cell_id": "00_baseline", "venue": "binance", "sharpe_like": 0.98, "max_drawdown": -0.4072, "trades": 319, "total_return": 0.6612},
        {"cell_id": "A3_turnover_10M", "venue": "bybit", "sharpe_like": 2.23, "max_drawdown": -0.2837, "trades": 290, "total_return": 4.5136},
        {"cell_id": "A3_turnover_10M", "venue": "binance", "sharpe_like": 0.08, "max_drawdown": -0.6197, "trades": 303, "total_return": -0.1685},
        {"cell_id": "B1_rankimp_200", "venue": "bybit", "sharpe_like": 2.71, "max_drawdown": -0.3878, "trades": 382, "total_return": 7.4689},
        {"cell_id": "B1_rankimp_200", "venue": "binance", "sharpe_like": 0.05, "max_drawdown": -0.6211, "trades": 281, "total_return": -0.1992},
        {"cell_id": "D1_hold2", "venue": "bybit", "sharpe_like": 2.37, "max_drawdown": -0.3805, "trades": 428, "total_return": 5.6469},
        {"cell_id": "D1_hold2", "venue": "binance", "sharpe_like": 0.73, "max_drawdown": -0.4804, "trades": 324, "total_return": 0.3388},
    ]
    csv_path = _write_csv(tmp_path, rows)
    rc = MOD.main([str(csv_path), "--control", "00_baseline"])
    assert rc == 0
    # Re-evaluate explicitly to inspect verdicts
    raw = MOD._read_csv(csv_path)
    indexed = MOD._index_by_cell(raw)
    control = indexed["00_baseline"]
    cells = [c for c in indexed if c != "00_baseline"]
    verdicts = [
        MOD.evaluate_cell(
            cell_id=c, cell_rows=indexed[c], control_rows=control,
            sharpe_delta_min=0.5, dd_delta_pp_max=5.0,
            min_trades_bybit=50, min_trades_binance=30,
        )
        for c in cells
    ]
    candidates = [v for v in verdicts if v.verdict == "candidate"]
    assert candidates == [], f"expected zero candidates, got: {[v.cell_id for v in candidates]}"
    # B1 and A3 should both falsify on sign-flip / DD-blowout
    by_cell = {v.cell_id: v for v in verdicts}
    assert by_cell["B1_rankimp_200"].verdict == "reject"
    assert by_cell["A3_turnover_10M"].verdict == "reject"


def test_synthetic_passing_candidate_promotes_through(tmp_path):
    """Construct a cell that passes EVERY gate; verify it's labelled candidate."""
    rows = [
        {"cell_id": "00_baseline", "venue": "bybit", "sharpe_like": 1.0, "max_drawdown": -0.40, "trades": 400, "total_return": 1.0},
        {"cell_id": "00_baseline", "venue": "binance", "sharpe_like": 1.0, "max_drawdown": -0.40, "trades": 400, "total_return": 1.0},
        # Clean win: both venues +0.6 sharpe, both DDs improve by 8pp, both positive return, ample trade counts
        {"cell_id": "ideal_cell", "venue": "bybit", "sharpe_like": 1.6, "max_drawdown": -0.32, "trades": 350, "total_return": 1.5},
        {"cell_id": "ideal_cell", "venue": "binance", "sharpe_like": 1.6, "max_drawdown": -0.32, "trades": 350, "total_return": 1.5},
    ]
    csv_path = _write_csv(tmp_path, rows)
    raw = MOD._read_csv(csv_path)
    indexed = MOD._index_by_cell(raw)
    v = MOD.evaluate_cell(
        cell_id="ideal_cell", cell_rows=indexed["ideal_cell"], control_rows=indexed["00_baseline"],
        sharpe_delta_min=0.5, dd_delta_pp_max=5.0,
        min_trades_bybit=50, min_trades_binance=30,
    )
    assert v.verdict == "candidate", f"expected candidate, got {v.verdict} reasons={v.reasons}"


def test_inconclusive_when_one_side_fails(tmp_path):
    """A near-miss is inconclusive (Bybit passes, Binance falls short on sharpe Δ)."""
    rows = [
        {"cell_id": "00_baseline", "venue": "bybit", "sharpe_like": 1.0, "max_drawdown": -0.40, "trades": 400, "total_return": 1.0},
        {"cell_id": "00_baseline", "venue": "binance", "sharpe_like": 1.0, "max_drawdown": -0.40, "trades": 400, "total_return": 1.0},
        {"cell_id": "near_miss", "venue": "bybit", "sharpe_like": 1.6, "max_drawdown": -0.32, "trades": 350, "total_return": 1.5},
        {"cell_id": "near_miss", "venue": "binance", "sharpe_like": 1.2, "max_drawdown": -0.34, "trades": 350, "total_return": 1.1},  # only +0.2 sharpe Δ
    ]
    csv_path = _write_csv(tmp_path, rows)
    raw = MOD._read_csv(csv_path)
    indexed = MOD._index_by_cell(raw)
    v = MOD.evaluate_cell(
        cell_id="near_miss", cell_rows=indexed["near_miss"], control_rows=indexed["00_baseline"],
        sharpe_delta_min=0.5, dd_delta_pp_max=5.0,
        min_trades_bybit=50, min_trades_binance=30,
    )
    assert v.verdict == "inconclusive"
    assert any("binance sharpe" in r for r in v.reasons)


def test_sign_flip_is_falsifier(tmp_path):
    rows = [
        {"cell_id": "00_baseline", "venue": "bybit", "sharpe_like": 1.0, "max_drawdown": -0.40, "trades": 400, "total_return": 1.0},
        {"cell_id": "00_baseline", "venue": "binance", "sharpe_like": 1.0, "max_drawdown": -0.40, "trades": 400, "total_return": 1.0},
        {"cell_id": "signflip", "venue": "bybit", "sharpe_like": 1.6, "max_drawdown": -0.32, "trades": 350, "total_return": 1.5},
        {"cell_id": "signflip", "venue": "binance", "sharpe_like": 1.5, "max_drawdown": -0.32, "trades": 350, "total_return": -0.1},
    ]
    csv_path = _write_csv(tmp_path, rows)
    raw = MOD._read_csv(csv_path)
    indexed = MOD._index_by_cell(raw)
    v = MOD.evaluate_cell(
        cell_id="signflip", cell_rows=indexed["signflip"], control_rows=indexed["00_baseline"],
        sharpe_delta_min=0.5, dd_delta_pp_max=5.0,
        min_trades_bybit=50, min_trades_binance=30,
    )
    assert v.verdict == "reject"
    assert any("sign flip" in r for r in v.reasons)


def test_dd_blowout_is_falsifier(tmp_path):
    rows = [
        {"cell_id": "00_baseline", "venue": "bybit", "sharpe_like": 1.0, "max_drawdown": -0.40, "trades": 400, "total_return": 1.0},
        {"cell_id": "00_baseline", "venue": "binance", "sharpe_like": 1.0, "max_drawdown": -0.40, "trades": 400, "total_return": 1.0},
        {"cell_id": "blowout", "venue": "bybit", "sharpe_like": 1.6, "max_drawdown": -0.65, "trades": 350, "total_return": 1.5},
        {"cell_id": "blowout", "venue": "binance", "sharpe_like": 1.6, "max_drawdown": -0.32, "trades": 350, "total_return": 1.5},
    ]
    csv_path = _write_csv(tmp_path, rows)
    raw = MOD._read_csv(csv_path)
    indexed = MOD._index_by_cell(raw)
    v = MOD.evaluate_cell(
        cell_id="blowout", cell_rows=indexed["blowout"], control_rows=indexed["00_baseline"],
        sharpe_delta_min=0.5, dd_delta_pp_max=5.0,
        min_trades_bybit=50, min_trades_binance=30,
    )
    assert v.verdict == "reject"
    assert any("DD" in r and "-60%" in r for r in v.reasons)


def test_missing_control_exits_usage(tmp_path):
    rows = [
        {"cell_id": "no_control_here", "venue": "bybit", "sharpe_like": 1.0, "max_drawdown": -0.40, "trades": 400, "total_return": 1.0},
    ]
    csv_path = _write_csv(tmp_path, rows)
    with pytest.raises(SystemExit) as exc:
        MOD.main([str(csv_path), "--control", "00_baseline"])
    assert exc.value.code == 2


def test_missing_csv_columns_raises(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("cell_id,venue\nA,bybit\n")
    with pytest.raises(SystemExit) as exc:
        MOD._read_csv(path)
    assert "csv missing columns" in str(exc.value)
