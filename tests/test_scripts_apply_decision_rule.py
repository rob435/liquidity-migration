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


def _write_csv_with_window(tmp_path: Path, rows: list[dict[str, str | int | float]]) -> Path:
    """Variant emitting an additional `window_days` column for investigation
    rule tests. Defaults to 1126 days (R1 window 2023-04-01 → 2026-04-30
    inclusive of start) when not specified per row."""
    path = tmp_path / "summary_window.csv"
    header = ("avg_split_sharpe,cell_id,description,elapsed_seconds,max_drawdown,"
              "promotable,report_dir,sharpe_like,status,total_return,trades,"
              "venue,window_days,worst_90d\n")
    path.write_text(header + "\n".join(_row_to_csv_with_window(r) for r in rows) + "\n")
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


def _row_to_csv_with_window(row: dict[str, str | int | float]) -> str:
    defaults = {
        "avg_split_sharpe": 0.0,
        "description": "",
        "elapsed_seconds": 0.0,
        "promotable": False,
        "report_dir": "",
        "status": "ok",
        "window_days": 1126,
        "worst_90d": 0.0,
    }
    merged = {**defaults, **row}
    cols = ["avg_split_sharpe", "cell_id", "description", "elapsed_seconds",
            "max_drawdown", "promotable", "report_dir", "sharpe_like", "status",
            "total_return", "trades", "venue", "window_days", "worst_90d"]
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
    raw, _excluded = MOD._read_csv(csv_path)
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
    raw, _excluded = MOD._read_csv(csv_path)
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
    raw, _excluded = MOD._read_csv(csv_path)
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
    raw, _excluded = MOD._read_csv(csv_path)
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
    raw, _excluded = MOD._read_csv(csv_path)
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


def test_read_csv_surfaces_failed_cells_as_excluded(tmp_path):
    """M6: a status!=ok cell must be returned as an explicit exclusion (a
    falsifier), never silently dropped."""
    path = tmp_path / "s.csv"
    path.write_text(
        "cell_id,venue,status,sharpe_like,max_drawdown,trades,total_return,error\n"
        "00_baseline,bybit,ok,1.0,-0.4,400,1.0,\n"
        "crashed_cell,bybit,failed,,,,,partial-pit abort\n"
    )
    metrics, excluded = MOD._read_csv(path)
    assert [m.cell_id for m in metrics] == ["00_baseline"]
    assert len(excluded) == 1
    assert excluded[0]["cell_id"] == "crashed_cell"
    assert excluded[0]["status"] == "failed"
    assert "partial-pit" in excluded[0]["error"]


def test_read_csv_captures_full_pit_flag(tmp_path):
    """The full_pit_universe_pass column is parsed into CellMetrics so the
    analyzer can flag survivorship-tainted (non-full-PIT) cells."""
    path = tmp_path / "s.csv"
    path.write_text(
        "cell_id,venue,status,sharpe_like,max_drawdown,trades,total_return,full_pit_universe_pass\n"
        "00_baseline,bybit,ok,1.0,-0.4,400,1.0,True\n"
        "biased_cell,bybit,ok,1.6,-0.3,350,1.5,False\n"
    )
    metrics, _excluded = MOD._read_csv(path)
    by_cell = {m.cell_id: m for m in metrics}
    assert by_cell["00_baseline"].full_pit_universe_pass is True
    assert by_cell["biased_cell"].full_pit_universe_pass is False


def test_missing_csv_columns_raises(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("cell_id,venue\nA,bybit\n")
    with pytest.raises(SystemExit) as exc:
        MOD._read_csv(path)
    assert "csv missing columns" in str(exc.value)


# ────────────────────────── MAR + investigation tier ──────────────────────────

def test_compute_annualized_return_one_year_window_is_identity():
    """At window_days = 365.25 (one calendar year), annualized = total_return.
    Sanity check on the geometric annualization formula."""
    assert MOD.compute_annualized_return(0.50, 365.25) == pytest.approx(0.50, abs=1e-9)
    assert MOD.compute_annualized_return(-0.20, 365.25) == pytest.approx(-0.20, abs=1e-9)


def test_compute_annualized_return_half_year_window_squares():
    """At window_days = 182.625 (half year), annualized = (1+r)^2 - 1."""
    r = 0.50
    expected = (1 + r) ** 2 - 1  # +125%/yr
    assert MOD.compute_annualized_return(r, 182.625) == pytest.approx(expected, rel=1e-6)


def test_compute_annualized_return_round1_actual_baseline():
    """The actual Round 1 / Phase 0 / R1 baseline at 1125 days (Bybit):
    total_return = +38.5606x → annualized ≈ +230.0%/yr.

    Reproduced bit-identically by R1_baseline_v2 in the 2026-05-28 R1
    sweep (matches Phase 0's 00_baseline). The Round 2 plan's worked
    example for Bybit (annualized +231.5%/yr) matches this within
    rounding — earlier-draft confusion in the plan's total_return cell
    (+518.76% from a different sweep) is corrected in the plan as of
    the same commit landing this test."""
    ann = MOD.compute_annualized_return(38.5606, 1125)
    assert 2.28 < ann < 2.32, f"expected ~+230.0%/yr at 1125d, got {ann*100:.1f}%/yr"


def test_compute_mar_round1_actual_baseline():
    """Bybit actual R1 baseline MAR at the 1125-day window ≈ +5.46.

    Pinned from R1_baseline_v2's per-cell metrics:
      total_return = +38.5606x, max_drawdown = -42.11%.

    Promotion threshold ΔMAR ≥ +0.5 against this baseline means a
    candidate cell needs MAR ≥ +5.96 on Bybit — challenging but
    achievable (e.g. R1_retest_rank_max hit +6.90)."""
    mar = MOD.compute_mar(38.5606, -0.4211, 1125)
    assert 5.40 < mar < 5.50, f"expected ~+5.46, got {mar:+.2f}"


def test_compute_mar_2026_05_28_sweep_baseline_at_1125_days():
    """Cross-sweep sanity: the 2026-05-28 cost-tweak sweep's Bybit
    baseline was total +518.76% / -42.11% DD over an ~18-month window
    (different cost config from the R-phase baseline). Annualizing
    that total at the R1 window of 1125 days would give MAR ≈ +1.92;
    that's NOT the R-phase baseline, just a sanity-check that the
    formula handles the "different sweep / different window" case
    cleanly without false-promoting the cell.

    This number is what the now-superseded plan-fix commit 3e86b69
    cited (incorrectly attributing the 2026-05-28 sweep's total_return
    to the R1 baseline). Keep the test as documentation of why the
    earlier "fix" was wrong."""
    mar = MOD.compute_mar(5.1876, -0.4211, 1125)
    assert 1.90 < mar < 1.95, f"expected ~+1.92, got {mar:+.2f}"


def test_compute_mar_handles_negative_return_cell():
    """A losing cell has negative MAR (negative annualized return ÷ positive |DD|)."""
    mar = MOD.compute_mar(-0.30, -0.40, 365.25)
    assert mar < 0, f"expected negative MAR for losing cell, got {mar:+.2f}"


def test_compute_mar_zero_drawdown_returns_zero():
    """Degenerate case: no drawdown → MAR returns 0 (caller treats as not-positive)."""
    assert MOD.compute_mar(1.0, 0.0, 365.25) == 0.0


def test_compute_annualized_return_total_loss_capped():
    """A cell that lost more than 100% (impossible in practice but the math
    handles it) returns the -1.0 floor without throwing."""
    assert MOD.compute_annualized_return(-1.5, 365.25) == -1.0


def test_investigation_both_venues_positive_mar_is_positive(tmp_path):
    """Cell improves MAR on both venues → investigation_positive."""
    rows = [
        # Baseline: ~+45%/yr Binance, ~+232%/yr Bybit at ~42% DD
        {"cell_id": "00_baseline", "venue": "bybit",   "sharpe_like": 2.27,
         "max_drawdown": -0.42, "trades": 416, "total_return": 5.19},
        {"cell_id": "00_baseline", "venue": "binance", "sharpe_like": 0.98,
         "max_drawdown": -0.41, "trades": 319, "total_return": 0.66},
        # R1_dropX: higher return at same DD on both venues → MAR up
        {"cell_id": "R1_dropX", "venue": "bybit",   "sharpe_like": 2.40,
         "max_drawdown": -0.42, "trades": 380, "total_return": 6.0},
        {"cell_id": "R1_dropX", "venue": "binance", "sharpe_like": 1.10,
         "max_drawdown": -0.41, "trades": 290, "total_return": 0.80},
    ]
    csv_path = _write_csv_with_window(tmp_path, rows)
    raw, _excluded = MOD._read_csv(csv_path)
    indexed = MOD._index_by_cell(raw)
    v = MOD.evaluate_cell_investigation(
        cell_id="R1_dropX",
        cell_rows=indexed["R1_dropX"],
        control_rows=indexed["00_baseline"],
        window_days=1126,
    )
    assert v.verdict == "investigation_positive", f"reasons: {v.reasons}"
    assert v.bybit_mar_d > 0
    assert v.binance_mar_d > 0


def test_investigation_one_venue_positive_within_tolerance_is_positive(tmp_path):
    """1/2 venues positive, other within -0.5 MAR → investigation_positive (majority rule).

    Use window_days = 365.25 (one calendar year) so MAR = total_return / |dd|
    transparently — keeps the test arithmetic easy to verify by eye.
    """
    rows = [
        # baseline bybit MAR = 1.0/0.5 = 2.0 ; baseline binance MAR = 0.5/0.5 = 1.0
        {"cell_id": "00_baseline", "venue": "bybit",   "sharpe_like": 2.0,
         "max_drawdown": -0.50, "trades": 400, "total_return": 1.0, "window_days": 365.25},
        {"cell_id": "00_baseline", "venue": "binance", "sharpe_like": 1.0,
         "max_drawdown": -0.50, "trades": 300, "total_return": 0.5, "window_days": 365.25},
        # R1_dropY bybit MAR = 1.4/0.4 = +3.5 (Δ = +1.5)
        # R1_dropY binance MAR = 0.42/0.55 ≈ +0.764 (Δ ≈ -0.236; within -0.5 tolerance)
        {"cell_id": "R1_dropY", "venue": "bybit",   "sharpe_like": 2.2,
         "max_drawdown": -0.40, "trades": 350, "total_return": 1.4, "window_days": 365.25},
        {"cell_id": "R1_dropY", "venue": "binance", "sharpe_like": 0.95,
         "max_drawdown": -0.55, "trades": 280, "total_return": 0.42, "window_days": 365.25},
    ]
    csv_path = _write_csv_with_window(tmp_path, rows)
    raw, _excluded = MOD._read_csv(csv_path)
    indexed = MOD._index_by_cell(raw)
    v = MOD.evaluate_cell_investigation(
        cell_id="R1_dropY",
        cell_rows=indexed["R1_dropY"],
        control_rows=indexed["00_baseline"],
        window_days=365.25,
    )
    assert v.verdict == "investigation_positive", (
        f"reasons: {v.reasons} dMAR=({v.bybit_mar_d:+.2f}, {v.binance_mar_d:+.2f})"
    )


def test_investigation_one_venue_positive_other_beyond_tolerance_is_descriptive(tmp_path):
    """1/2 positive but other venue is worse than -0.5 MAR → descriptive (not positive, not falsifier).

    Same window_days = 365.25 trick for clean math.
    """
    rows = [
        # baseline bybit MAR = 1.0/0.5 = 2.0 ; baseline binance MAR = 0.5/0.5 = 1.0
        {"cell_id": "00_baseline", "venue": "bybit",   "sharpe_like": 2.0,
         "max_drawdown": -0.50, "trades": 400, "total_return": 1.0, "window_days": 365.25},
        {"cell_id": "00_baseline", "venue": "binance", "sharpe_like": 1.0,
         "max_drawdown": -0.50, "trades": 300, "total_return": 0.5, "window_days": 365.25},
        # R1_dropZ bybit MAR = 2.0/0.4 = +5.0 (Δ = +3.0)
        # R1_dropZ binance MAR = 0.2/0.5 = +0.4 (Δ = -0.6; beyond -0.5 but still > -1.0 so not falsifier)
        {"cell_id": "R1_dropZ", "venue": "bybit",   "sharpe_like": 2.4,
         "max_drawdown": -0.40, "trades": 350, "total_return": 2.0, "window_days": 365.25},
        {"cell_id": "R1_dropZ", "venue": "binance", "sharpe_like": 0.6,
         "max_drawdown": -0.50, "trades": 290, "total_return": 0.20, "window_days": 365.25},
    ]
    csv_path = _write_csv_with_window(tmp_path, rows)
    raw, _excluded = MOD._read_csv(csv_path)
    indexed = MOD._index_by_cell(raw)
    v = MOD.evaluate_cell_investigation(
        cell_id="R1_dropZ",
        cell_rows=indexed["R1_dropZ"],
        control_rows=indexed["00_baseline"],
        window_days=365.25,
    )
    assert v.verdict == "descriptive", (
        f"expected descriptive, got {v.verdict} reasons={v.reasons} "
        f"dMAR=({v.bybit_mar_d:+.2f}, {v.binance_mar_d:+.2f})"
    )


def test_investigation_mar_falsify_at_minus_one(tmp_path):
    """MAR Δ ≤ -1.0 on either venue → falsifier."""
    rows = [
        {"cell_id": "00_baseline", "venue": "bybit",   "sharpe_like": 2.27,
         "max_drawdown": -0.42, "trades": 416, "total_return": 5.19},
        {"cell_id": "00_baseline", "venue": "binance", "sharpe_like": 0.98,
         "max_drawdown": -0.41, "trades": 319, "total_return": 0.66},
        # Bybit MAR collapses ≥ 1.0
        {"cell_id": "R1_blowup", "venue": "bybit",   "sharpe_like": 1.0,
         "max_drawdown": -0.60, "trades": 350, "total_return": 1.5},
        {"cell_id": "R1_blowup", "venue": "binance", "sharpe_like": 1.0,
         "max_drawdown": -0.41, "trades": 290, "total_return": 0.70},
    ]
    csv_path = _write_csv_with_window(tmp_path, rows)
    raw, _excluded = MOD._read_csv(csv_path)
    indexed = MOD._index_by_cell(raw)
    v = MOD.evaluate_cell_investigation(
        cell_id="R1_blowup",
        cell_rows=indexed["R1_blowup"],
        control_rows=indexed["00_baseline"],
        window_days=1126,
    )
    assert v.verdict == "reject", f"reasons: {v.reasons}"
    assert any("MAR Δ" in r for r in v.reasons), f"reasons: {v.reasons}"


def test_investigation_dd_70pct_is_falsifier(tmp_path):
    rows = [
        {"cell_id": "00_baseline", "venue": "bybit",   "sharpe_like": 2.0,
         "max_drawdown": -0.40, "trades": 400, "total_return": 4.0},
        {"cell_id": "00_baseline", "venue": "binance", "sharpe_like": 1.0,
         "max_drawdown": -0.40, "trades": 300, "total_return": 0.5},
        # DD beyond -70% on Bybit
        {"cell_id": "R1_dd_blowout", "venue": "bybit",   "sharpe_like": 1.5,
         "max_drawdown": -0.72, "trades": 350, "total_return": 3.0},
        {"cell_id": "R1_dd_blowout", "venue": "binance", "sharpe_like": 1.0,
         "max_drawdown": -0.40, "trades": 290, "total_return": 0.5},
    ]
    csv_path = _write_csv_with_window(tmp_path, rows)
    raw, _excluded = MOD._read_csv(csv_path)
    indexed = MOD._index_by_cell(raw)
    v = MOD.evaluate_cell_investigation(
        cell_id="R1_dd_blowout",
        cell_rows=indexed["R1_dd_blowout"],
        control_rows=indexed["00_baseline"],
        window_days=1126,
    )
    assert v.verdict == "reject"
    assert any("DD" in r and ("70" in r or "-70%" in r) for r in v.reasons)


def test_investigation_return_negative_on_positive_control_is_falsifier(tmp_path):
    rows = [
        {"cell_id": "00_baseline", "venue": "bybit",   "sharpe_like": 2.0,
         "max_drawdown": -0.40, "trades": 400, "total_return": 4.0},
        {"cell_id": "00_baseline", "venue": "binance", "sharpe_like": 1.0,
         "max_drawdown": -0.40, "trades": 300, "total_return": 0.5},
        # Bybit goes negative even though control was positive
        {"cell_id": "R1_signflip", "venue": "bybit",   "sharpe_like": -0.5,
         "max_drawdown": -0.55, "trades": 350, "total_return": -0.3},
        {"cell_id": "R1_signflip", "venue": "binance", "sharpe_like": 1.0,
         "max_drawdown": -0.40, "trades": 290, "total_return": 0.5},
    ]
    csv_path = _write_csv_with_window(tmp_path, rows)
    raw, _excluded = MOD._read_csv(csv_path)
    indexed = MOD._index_by_cell(raw)
    v = MOD.evaluate_cell_investigation(
        cell_id="R1_signflip",
        cell_rows=indexed["R1_signflip"],
        control_rows=indexed["00_baseline"],
        window_days=1126,
    )
    assert v.verdict == "reject"
    assert any("return went negative" in r for r in v.reasons)


def test_investigation_trade_floor_is_falsifier(tmp_path):
    """Trade count below the 30 by / 20 bn floor → falsifier (signal population vanished)."""
    rows = [
        {"cell_id": "00_baseline", "venue": "bybit",   "sharpe_like": 2.0,
         "max_drawdown": -0.40, "trades": 400, "total_return": 4.0},
        {"cell_id": "00_baseline", "venue": "binance", "sharpe_like": 1.0,
         "max_drawdown": -0.40, "trades": 300, "total_return": 0.5},
        {"cell_id": "R1_dry", "venue": "bybit",   "sharpe_like": 2.2,
         "max_drawdown": -0.35, "trades": 12, "total_return": 4.5},  # < 30
        {"cell_id": "R1_dry", "venue": "binance", "sharpe_like": 1.05,
         "max_drawdown": -0.38, "trades": 290, "total_return": 0.6},
    ]
    csv_path = _write_csv_with_window(tmp_path, rows)
    raw, _excluded = MOD._read_csv(csv_path)
    indexed = MOD._index_by_cell(raw)
    v = MOD.evaluate_cell_investigation(
        cell_id="R1_dry",
        cell_rows=indexed["R1_dry"],
        control_rows=indexed["00_baseline"],
        window_days=1126,
    )
    assert v.verdict == "reject"
    assert any("trades" in r for r in v.reasons)


def test_investigation_main_requires_window_days(tmp_path):
    """Without window_days in the CSV or --window-days CLI, investigation mode errors."""
    rows = [
        {"cell_id": "00_baseline", "venue": "bybit",   "sharpe_like": 1.0,
         "max_drawdown": -0.40, "trades": 400, "total_return": 1.0},
        {"cell_id": "00_baseline", "venue": "binance", "sharpe_like": 1.0,
         "max_drawdown": -0.40, "trades": 400, "total_return": 1.0},
        {"cell_id": "R1_x", "venue": "bybit",   "sharpe_like": 1.1,
         "max_drawdown": -0.39, "trades": 350, "total_return": 1.1},
        {"cell_id": "R1_x", "venue": "binance", "sharpe_like": 1.1,
         "max_drawdown": -0.39, "trades": 280, "total_return": 1.1},
    ]
    csv_path = _write_csv(tmp_path, rows)  # no window_days column
    rc = MOD.main([str(csv_path), "--control", "00_baseline", "--rule", "investigation"])
    assert rc == 2  # EXIT_USAGE


def test_investigation_main_accepts_window_days_cli(tmp_path):
    """With --window-days N flag, investigation mode runs even on a legacy CSV."""
    rows = [
        {"cell_id": "00_baseline", "venue": "bybit",   "sharpe_like": 2.27,
         "max_drawdown": -0.42, "trades": 416, "total_return": 5.19},
        {"cell_id": "00_baseline", "venue": "binance", "sharpe_like": 0.98,
         "max_drawdown": -0.41, "trades": 319, "total_return": 0.66},
        {"cell_id": "R1_drop", "venue": "bybit",   "sharpe_like": 2.40,
         "max_drawdown": -0.42, "trades": 380, "total_return": 6.0},
        {"cell_id": "R1_drop", "venue": "binance", "sharpe_like": 1.10,
         "max_drawdown": -0.41, "trades": 290, "total_return": 0.80},
    ]
    csv_path = _write_csv(tmp_path, rows)  # no window_days column
    rc = MOD.main([
        str(csv_path), "--control", "00_baseline",
        "--rule", "investigation", "--window-days", "1126",
    ])
    assert rc == 0


def test_investigation_main_reads_window_days_from_csv_column(tmp_path):
    """CSV-emitted window_days takes precedence; no CLI flag needed."""
    rows = [
        {"cell_id": "00_baseline", "venue": "bybit",   "sharpe_like": 2.27,
         "max_drawdown": -0.42, "trades": 416, "total_return": 5.19, "window_days": 1126},
        {"cell_id": "00_baseline", "venue": "binance", "sharpe_like": 0.98,
         "max_drawdown": -0.41, "trades": 319, "total_return": 0.66, "window_days": 1126},
        {"cell_id": "R1_drop", "venue": "bybit",   "sharpe_like": 2.40,
         "max_drawdown": -0.42, "trades": 380, "total_return": 6.0, "window_days": 1126},
        {"cell_id": "R1_drop", "venue": "binance", "sharpe_like": 1.10,
         "max_drawdown": -0.41, "trades": 290, "total_return": 0.80, "window_days": 1126},
    ]
    csv_path = _write_csv_with_window(tmp_path, rows)
    rc = MOD.main([
        str(csv_path), "--control", "00_baseline",
        "--rule", "investigation",
    ])
    assert rc == 0


def test_investigation_does_not_change_manifesto_verdicts(tmp_path):
    """Regression: running --rule manifesto on a CSV that has a window_days
    column produces the same verdicts as without it. Round 2 must NOT
    silently move the Round 1 bar."""
    rows = [
        {"cell_id": "00_baseline", "venue": "bybit",   "sharpe_like": 2.27,
         "max_drawdown": -0.4211, "trades": 416, "total_return": 5.1876, "window_days": 1126},
        {"cell_id": "00_baseline", "venue": "binance", "sharpe_like": 0.98,
         "max_drawdown": -0.4072, "trades": 319, "total_return": 0.6612, "window_days": 1126},
        {"cell_id": "B1_rankimp_200", "venue": "bybit",   "sharpe_like": 2.71,
         "max_drawdown": -0.3878, "trades": 382, "total_return": 7.4689, "window_days": 1126},
        {"cell_id": "B1_rankimp_200", "venue": "binance", "sharpe_like": 0.05,
         "max_drawdown": -0.6211, "trades": 281, "total_return": -0.1992, "window_days": 1126},
    ]
    csv_path = _write_csv_with_window(tmp_path, rows)
    raw, _excluded = MOD._read_csv(csv_path)
    indexed = MOD._index_by_cell(raw)
    v = MOD.evaluate_cell(
        cell_id="B1_rankimp_200",
        cell_rows=indexed["B1_rankimp_200"],
        control_rows=indexed["00_baseline"],
        sharpe_delta_min=0.5,
        dd_delta_pp_max=5.0,
        min_trades_bybit=50,
        min_trades_binance=30,
    )
    # Round 1 verdict was REJECT (sign-flip + DD blowout). Pin that.
    assert v.verdict == "reject"
