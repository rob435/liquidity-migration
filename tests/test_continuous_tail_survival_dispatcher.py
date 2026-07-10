from __future__ import annotations

import json
import os
from pathlib import Path

import polars as pl
import pytest

import scripts.continuous_tail_survival_2026_07_10 as tail
from liquidity_migration.continuous_events import ContinuousEventConfig


DAY = 86_400_000


def _ts(date: str) -> int:
    return int(pl.Series([date]).str.to_date().cast(pl.Datetime("ms")).dt.timestamp("ms")[0])


def _write_root(
    root: Path,
    *,
    venue: str = "bybit",
    start_date: str = "2026-07-08",
    signal_end_date: str = "2026-07-10",
    exit_end_date: str = "2026-07-12",
) -> None:
    funding = tail.FUNDING_DATASET[venue]
    for dataset, boundary in (
        ("klines_1h", exit_end_date),
        ("archive_trade_manifest", signal_end_date),
        (funding, exit_end_date),
    ):
        for date in tail._date_range(start_date, boundary):
            path = root / dataset / f"date={date}" / "part.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"{dataset}:{date}".encode())
    stable_days = tail._date_range(start_date, signal_end_date)
    symbols = [f"S{i:02d}USDT" for i in range(tail.MIN_STABLE_RMOM_SYMBOLS)]
    pl.DataFrame(
        {
            "symbol": [symbol for day in stable_days for symbol in symbols],
            "ts_ms": [
                _ts(day) for day in stable_days for _symbol in symbols
            ],
            "residual_momentum": [0.1] * (len(symbols) * len(stable_days)),
            "is_provisional": [False] * (len(symbols) * len(stable_days)),
        }
    ).write_parquet(root / "residual_momentum.parquet")


def _passing_metrics(*, mar: float = 1.0) -> dict[str, object]:
    return {
        "total_return_frac": 0.10,
        "max_drawdown_frac": -0.05,
        "mar": mar,
        "cdar95_loss_frac": 0.05,
        "daily_es99_loss_frac": 0.02,
        "one_name_100_shock_loss_frac": 0.10,
        "three_name_50_shock_loss_frac": 0.10,
        "worst_90d_return_frac": -0.03,
        "max_no_new_high_days": 30,
        "component_candidate_rows": 100,
        "component_trade_rows": 50,
        "skipped_capacity_rows": 2,
        "risk_clamped_trade_rows": 20,
        "funding_modes": ["modeled"],
        "split_metrics": {
            "pre_2025_06_01": {"total_return_frac": 0.05, "mar": mar},
            "post_2025_06_01": {"total_return_frac": 0.05, "mar": mar},
        },
    }


def _control_metrics(*, mar: float = 1.0) -> dict[str, object]:
    metrics = _passing_metrics(mar=mar)
    metrics.update(
        {
            "max_drawdown_frac": -0.06,
            "cdar95_loss_frac": 0.10,
            "daily_es99_loss_frac": 0.04,
            "one_name_100_shock_loss_frac": 0.20,
            "three_name_50_shock_loss_frac": 0.20,
            "worst_90d_return_frac": -0.04,
            "risk_clamped_trade_rows": 0,
        }
    )
    return metrics


def test_registered_cells_are_budget_only_and_transform_pins_other_hooks_off() -> None:
    assert list(tail.CELLS) == ["control", "budget_010", "budget_015", "budget_025"]
    transformed = tail.cell_transform(tail.CELLS["budget_015"])(
        ContinuousEventConfig(
            entry_portfolio_heat_cap_frac=0.9,
            failed_fade_hours=6,
            failed_fade_loss_pct=0.04,
            failed_fade_min_mfe_pct=0.01,
        )
    )
    assert transformed.entry_disaster_loss_budget_frac == pytest.approx(0.0015)
    assert transformed.entry_disaster_shock_frac == pytest.approx(1.0)
    assert transformed.entry_portfolio_heat_cap_frac == 0.0
    assert transformed.failed_fade_hours == 0
    assert transformed.failed_fade_loss_pct == 0.0
    assert transformed.failed_fade_min_mfe_pct == 0.0


def test_frozen_forward_and_effective_control_hashes_are_pinned() -> None:
    hashes = tail.effective_component_config_hashes()
    assert hashes == tail.EXPECTED_EFFECTIVE_COMPONENT_CONFIG_HASHES
    assert hashes["budget_015"] == {
        "turn3p3": "4c85020e4a61",
        "turn4p3": "44dca1702a0b",
        "turn4p5": "7f401b73a216",
    }


def test_root_inventory_requires_future_exit_tail_and_no_date_holes(tmp_path: Path) -> None:
    _write_root(tmp_path)
    unverified = tail.root_inventory(
        "bybit",
        tmp_path,
        start_date="2026-07-08",
        signal_end_date="2026-07-10",
        exit_end_date="2026-07-12",
    )
    assert unverified["data_ready"] is True
    assert unverified["full_pit_ready"] is False
    tail.write_root_build_receipt(
        "bybit",
        tmp_path,
        start_date="2026-07-08",
        signal_end_date="2026-07-10",
        exit_end_date="2026-07-12",
        verification_gates=["test independent audit"],
    )
    ready = tail.root_inventory(
        "bybit",
        tmp_path,
        start_date="2026-07-08",
        signal_end_date="2026-07-10",
        exit_end_date="2026-07-12",
    )
    assert ready["full_pit_ready"] is True
    # Membership correctly ends at the signal cutoff; klines/funding must carry
    # the held-position exit path through July 11.
    assert ready["datasets"]["membership"]["required_end_date_exclusive"] == "2026-07-10"
    assert ready["datasets"]["klines"]["required_end_date_exclusive"] == "2026-07-12"
    (tmp_path / "klines_1h" / "date=2026-07-11" / "part.parquet").unlink()
    stale = tail.root_inventory(
        "bybit",
        tmp_path,
        start_date="2026-07-08",
        signal_end_date="2026-07-10",
        exit_end_date="2026-07-12",
    )
    assert stale["full_pit_ready"] is False
    assert stale["datasets"]["klines"]["empty_partition_sample"] == ["2026-07-11"]


def test_root_content_fingerprint_changes_on_in_place_file_rewrite(tmp_path: Path) -> None:
    _write_root(tmp_path)
    before = tail.root_inventory(
        "bybit", tmp_path,
        start_date="2026-07-08", signal_end_date="2026-07-10", exit_end_date="2026-07-12",
    )["content_fingerprint_sha256"]
    target = tmp_path / "funding" / "date=2026-07-09" / "part.parquet"
    target.write_bytes(target.read_bytes() + b"changed")
    after = tail.root_inventory(
        "bybit", tmp_path,
        start_date="2026-07-08", signal_end_date="2026-07-10", exit_end_date="2026-07-12",
    )["content_fingerprint_sha256"]
    assert before != after


def test_root_exact_fingerprint_detects_same_size_rewrite_with_restored_mtime(
    tmp_path: Path,
) -> None:
    _write_root(tmp_path)
    target = tmp_path / "funding" / "date=2026-07-09" / "part.parquet"
    stat = target.stat()
    before = tail.root_inventory(
        "bybit",
        tmp_path,
        start_date="2026-07-08",
        signal_end_date="2026-07-10",
        exit_end_date="2026-07-12",
    )
    original = target.read_bytes()
    target.write_bytes(b"X" * len(original))
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = tail.root_inventory(
        "bybit",
        tmp_path,
        start_date="2026-07-08",
        signal_end_date="2026-07-10",
        exit_end_date="2026-07-12",
    )

    assert before["fast_content_fingerprint_sha256"] == after["fast_content_fingerprint_sha256"]
    assert before["data_fingerprint_sha256"] != after["data_fingerprint_sha256"]


def test_root_inventory_refuses_legacy_or_provisional_only_rmom(tmp_path: Path) -> None:
    _write_root(tmp_path)
    pl.DataFrame(
        {
            "symbol": ["AAAUSDT"],
            "ts_ms": [_ts("2026-07-09")],
            "residual_momentum": [0.1],
            "is_provisional": [True],
        }
    ).write_parquet(tmp_path / "residual_momentum.parquet")
    got = tail.root_inventory(
        "bybit", tmp_path,
        start_date="2026-07-08", signal_end_date="2026-07-10", exit_end_date="2026-07-12",
    )
    assert got["full_pit_ready"] is False
    assert "no stable finite" in " ".join(got["residual_momentum"]["failures"])


def test_root_inventory_requires_stable_rmom_for_every_signal_day(tmp_path: Path) -> None:
    _write_root(tmp_path)
    path = tmp_path / "residual_momentum.parquet"
    frame = pl.read_parquet(path).filter(pl.col("ts_ms") != _ts("2026-07-08"))
    frame.write_parquet(path)

    got = tail.root_inventory(
        "bybit",
        tmp_path,
        start_date="2026-07-08",
        signal_end_date="2026-07-10",
        exit_end_date="2026-07-12",
    )

    assert got["full_pit_ready"] is False
    assert got["residual_momentum"]["missing_stable_day_count"] == 1
    assert "history is incomplete" in " ".join(got["residual_momentum"]["failures"])


@pytest.mark.parametrize("malformation", ["small", "duplicate", "nonfinite", "blank_symbol"])
def test_root_inventory_refuses_malformed_stable_rmom_cross_section(
    tmp_path: Path,
    malformation: str,
) -> None:
    _write_root(tmp_path)
    cutoff = _ts("2026-07-09")
    symbols = [f"S{i:02d}USDT" for i in range(tail.MIN_STABLE_RMOM_SYMBOLS)]
    values = [0.1] * len(symbols)
    if malformation == "small":
        symbols = symbols[:-1]
        values = values[:-1]
    elif malformation == "duplicate":
        symbols[-1] = symbols[0]
    elif malformation == "nonfinite":
        values[-1] = float("nan")
    elif malformation == "blank_symbol":
        symbols[-1] = ""
    pl.DataFrame(
        {
            "symbol": symbols,
            "ts_ms": [cutoff] * len(symbols),
            "residual_momentum": values,
            "is_provisional": [False] * len(symbols),
        }
    ).write_parquet(tmp_path / "residual_momentum.parquet")

    got = tail.root_inventory(
        "bybit",
        tmp_path,
        start_date="2026-07-08",
        signal_end_date="2026-07-10",
        exit_end_date="2026-07-12",
    )

    assert got["full_pit_ready"] is False
    failures = " ".join(got["residual_momentum"]["failures"])
    expected = {
        "small": "cross-section is too small",
        "duplicate": "duplicate (symbol,ts_ms)",
        "nonfinite": "null/non-finite",
        "blank_symbol": "null, blank, or non-daily keys",
    }[malformation]
    assert expected in failures


def test_root_build_receipt_absence_is_explicit_limitation(tmp_path: Path) -> None:
    _write_root(tmp_path)
    got = tail.root_inventory(
        "bybit", tmp_path,
        start_date="2026-07-08", signal_end_date="2026-07-10", exit_end_date="2026-07-12",
    )
    assert got["root_build_receipt"]["present"] is False
    assert got["registered_evidence_ready"] is False
    assert "cannot support a positive" in got["root_build_receipt"]["limitation"]


def test_root_build_receipt_is_bound_to_exact_data_fingerprint(tmp_path: Path) -> None:
    _write_root(tmp_path)
    tail.write_root_build_receipt(
        "bybit",
        tmp_path,
        start_date="2026-07-08",
        signal_end_date="2026-07-10",
        exit_end_date="2026-07-12",
        verification_gates=["test independent audit"],
    )
    verified = tail.root_inventory(
        "bybit",
        tmp_path,
        start_date="2026-07-08",
        signal_end_date="2026-07-10",
        exit_end_date="2026-07-12",
    )
    assert verified["registered_evidence_ready"] is True

    target = tmp_path / "funding" / "date=2026-07-09" / "part.parquet"
    target.write_bytes(target.read_bytes() + b"changed")
    changed = tail.root_inventory(
        "bybit",
        tmp_path,
        start_date="2026-07-08",
        signal_end_date="2026-07-10",
        exit_end_date="2026-07-12",
    )
    assert changed["data_ready"] is True
    assert changed["registered_evidence_ready"] is False
    assert "data_fingerprint_sha256 mismatch" in " ".join(
        changed["root_build_receipt"]["failures"]
    )


def _write_funding(path: Path, symbol: str, date: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": [symbol] * 3,
            "funding_rate": [0.0001] * 3,
            "ts_ms": [_ts(date) + hours * 3_600_000 for hours in (0, 8, 16)],
            "funding_interval_min": [480] * 3,
        }
    ).write_parquet(path)


def test_exact_trade_funding_validation_refuses_missing_symbol_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tail, "START_DATE", "2026-07-09")
    monkeypatch.setattr(tail, "EXIT_DATA_END_DATE", "2026-07-12")
    cell_root = tmp_path / "cell"
    root = tmp_path / "root"
    signal = _ts("2026-07-09")
    for component in tail.WINNER_WEIGHTS:
        source = tail.CONTINUOUS_COMPONENT_SOURCES[component]
        path = cell_root / "components" / "bybit" / source.cell / "continuous_trades.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "symbol": ["AAAUSDT"],
                "entry_signal_ts_ms": [signal],
                "entry_ts_ms": [signal],
                "exit_ts_ms": [signal + DAY],
            }
        ).write_csv(path)
    manifest = root / "archive_trade_manifest" / "date=2026-07-09" / "part.parquet"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": ["AAAUSDT"], "date": ["2026-07-09"]}).write_parquet(manifest)
    for date in ("2026-07-09", "2026-07-10", "2026-07-11"):
        for symbol in ("BTCUSDT", "ETHUSDT"):
            _write_funding(tail._funding_symbol_path(root, "bybit", date, symbol), symbol, date)
    _write_funding(
        tail._funding_symbol_path(root, "bybit", "2026-07-09", "AAAUSDT"),
        "AAAUSDT",
        "2026-07-09",
    )
    with pytest.raises(RuntimeError, match="AAAUSDT"):
        tail.validate_trade_data_planes(cell_root, "bybit", root)


def test_exact_funding_validation_refuses_internal_settlement_holes(tmp_path: Path) -> None:
    path = tail._funding_symbol_path(
        tmp_path,
        "bybit",
        "2026-07-09",
        "AAAUSDT",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": ["AAAUSDT"],
            "funding_rate": [0.0001],
            "ts_ms": [_ts("2026-07-09")],
            "funding_interval_min": [480],
        }
    ).write_parquet(path)

    failure = tail._validate_funding_file(
        path,
        symbol="AAAUSDT",
        date="2026-07-09",
    )

    assert failure is not None
    assert "settlement gap" in failure


def test_shock_metrics_aggregate_components_by_symbol_and_time() -> None:
    rows = [
        {"symbol": "A", "entry_ts_ms": 1, "exit_ts_ms": 10, "ensemble_notional_weight": 0.001},
        {"symbol": "A", "entry_ts_ms": 2, "exit_ts_ms": 10, "ensemble_notional_weight": 0.002},
        {"symbol": "B", "entry_ts_ms": 3, "exit_ts_ms": 10, "ensemble_notional_weight": 0.004},
        {"symbol": "C", "entry_ts_ms": 4, "exit_ts_ms": 10, "ensemble_notional_weight": 0.006},
    ]
    metrics = tail.shock_metrics_from_rows(rows)
    assert metrics["one_name_100_shock_loss_frac"] == pytest.approx(0.006)
    assert metrics["three_name_50_shock_loss_frac"] == pytest.approx(0.0065)


def test_equity_metrics_use_unrounded_calendar_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tail, "START_DATE", "2026-07-08")
    monkeypatch.setattr(tail, "EXIT_DATA_END_DATE", "2026-07-11")
    path = tmp_path / "equity.csv"
    pl.DataFrame(
        {
            "ts_ms": [_ts("2026-07-08"), _ts("2026-07-10")],
            "basket_return": [-0.010004, 0.02],
            "equity": [0.989996, 1.00979592],
        }
    ).write_csv(path)
    metrics = tail.equity_tail_metrics(path)
    assert metrics["calendar_days"] == 3
    assert metrics["worst_day_frac"] == pytest.approx(-0.010004)
    assert metrics["total_return_frac"] == pytest.approx((1 - 0.010004) * 1.02 - 1)


def test_equity_metrics_count_initial_loss_from_starting_capital(tmp_path: Path) -> None:
    path = tmp_path / "equity.csv"
    pl.DataFrame(
        {
            "ts_ms": [_ts("2026-07-08"), _ts("2026-07-09")],
            "basket_return": [-0.10, 0.01],
            "equity": [0.90, 0.909],
        }
    ).write_csv(path)

    metrics = tail.equity_tail_metrics(
        path,
        start_date="2026-07-08",
        end_date="2026-07-10",
    )

    assert metrics["max_drawdown_frac"] == pytest.approx(-0.10)
    assert metrics["cdar95_loss_frac"] == pytest.approx(0.10)
    assert tail._series_metrics(pl.Series([-0.10, 0.01]).to_numpy())["max_drawdown_frac"] == pytest.approx(-0.10)


def test_incomplete_candidate_is_incomplete_not_reject_or_pass() -> None:
    rows = {"bybit": {"control": _control_metrics(), "budget_010": _passing_metrics()}}
    verdict = tail.cell_verdict("budget_010", rows)
    assert verdict["status"] == "incomplete"


def test_verdict_uses_raw_mar_at_rounding_boundary() -> None:
    control = _control_metrics(mar=1.0049)
    candidate = _passing_metrics(mar=0.95464)
    # Both values display as 1.00/0.95 at two decimals, but the raw candidate is
    # just below the exact 95% floor (0.954655).
    rows = {
        venue: {"control": control, "budget_010": candidate}
        for venue in ("bybit", "binance")
    }
    verdict = tail.cell_verdict("budget_010", rows)
    assert verdict["status"] == "reject"
    assert any("raw MAR below 95%" in reason for reason in verdict["reasons"])


def _write_receipt(
    output: Path,
    *,
    run_id: str,
    cell: str,
    venue: str,
    diagnostic_only: bool,
    metrics: dict[str, object],
) -> None:
    path = output / cell / venue / "cell_receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / "artifact.json").write_text(
        json.dumps(metrics, sort_keys=True),
        encoding="utf-8",
    )
    artifact_fingerprint = tail._cell_artifact_fingerprint(output / cell, venue)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "cell": cell,
                "venue": venue,
                "status": "complete",
                "family": tail.CELLS[cell].family,
                "diagnostic_only": diagnostic_only,
                "metrics": metrics,
                "artifact_fingerprint": artifact_fingerprint,
            }
        )
    )


def test_summary_ignores_receipts_from_other_run_id(tmp_path: Path) -> None:
    manifest = {
        "run_id": "current",
        "registered_venues": ["bybit", "binance"],
        "diagnostic_only": False,
    }
    _write_receipt(
        tmp_path,
        run_id="stale",
        cell="control",
        venue="bybit",
        diagnostic_only=False,
        metrics=_control_metrics(),
    )
    tail.write_summary(tmp_path, manifest=manifest)
    payload = json.loads((tmp_path / "summary.json").read_text())
    assert payload["matching_receipt_count"] == 0
    assert payload["verdicts"]["budget_010"]["status"] == "incomplete"


def test_summary_ignores_receipt_after_artifact_mutation(tmp_path: Path) -> None:
    manifest = {
        "run_id": "current",
        "registered_venues": ["bybit", "binance"],
        "diagnostic_only": False,
    }
    _write_receipt(
        tmp_path,
        run_id="current",
        cell="control",
        venue="bybit",
        diagnostic_only=False,
        metrics=_control_metrics(),
    )
    artifact = tmp_path / "control" / "bybit" / "artifact.json"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "\nmutated", encoding="utf-8")

    tail.write_summary(tmp_path, manifest=manifest)

    payload = json.loads((tmp_path / "summary.json").read_text())
    assert payload["matching_receipt_count"] == 0


def test_partial_full_venue_cell_never_emits_early_reject_or_pass(tmp_path: Path) -> None:
    manifest = {
        "run_id": "partial",
        "registered_venues": ["bybit", "binance"],
        "diagnostic_only": False,
    }
    for venue in ("bybit", "binance"):
        _write_receipt(
            tmp_path,
            run_id="partial",
            cell="control",
            venue=venue,
            diagnostic_only=False,
            metrics=_control_metrics(),
        )
        rejected = _passing_metrics()
        rejected["total_return_frac"] = -0.01
        _write_receipt(
            tmp_path,
            run_id="partial",
            cell="budget_010",
            venue=venue,
            diagnostic_only=False,
            metrics=rejected,
        )
    tail.write_summary(tmp_path, manifest=manifest)
    payload = json.loads((tmp_path / "summary.json").read_text())
    assert payload["complete_registered_matrix"] is False
    assert payload["verdicts"]["budget_010"]["status"] == "incomplete"


def test_complete_diagnostic_matrix_cannot_pass(tmp_path: Path) -> None:
    manifest = {
        "run_id": "diagnostic",
        "registered_venues": ["bybit", "binance"],
        "diagnostic_only": True,
    }
    for venue in ("bybit", "binance"):
        _write_receipt(
            tmp_path,
            run_id="diagnostic",
            cell="control",
            venue=venue,
            diagnostic_only=True,
            metrics=_control_metrics(),
        )
        for cell in ("budget_010", "budget_015", "budget_025"):
            _write_receipt(
                tmp_path,
                run_id="diagnostic",
                cell=cell,
                venue=venue,
                diagnostic_only=True,
                metrics=_passing_metrics(),
            )
    tail.write_summary(tmp_path, manifest=manifest)
    payload = json.loads((tmp_path / "summary.json").read_text())
    assert payload["complete_registered_matrix"] is True
    assert payload["verdicts"]["budget_015"]["status"] == "diagnostic_only"


def test_begin_rerun_invalidates_old_complete_verdict_before_purge(tmp_path: Path) -> None:
    manifest = {
        "run_id": "current",
        "registered_venues": ["bybit", "binance"],
        "diagnostic_only": False,
    }
    for venue in ("bybit", "binance"):
        _write_receipt(
            tmp_path,
            run_id="current",
            cell="control",
            venue=venue,
            diagnostic_only=False,
            metrics=_control_metrics(),
        )
        for cell in ("budget_010", "budget_015", "budget_025"):
            _write_receipt(
                tmp_path,
                run_id="current",
                cell=cell,
                venue=venue,
                diagnostic_only=False,
                metrics=_passing_metrics(),
            )
    tail.write_summary(tmp_path, manifest=manifest)
    before = json.loads((tmp_path / "summary.json").read_text())
    assert before["complete_registered_matrix"] is True
    assert before["verdicts"]["budget_010"]["status"] == "pass_followup_only"

    signature_payload = {
        "run_id": "current",
        "cell": "budget_010",
        "spec": {},
        "venue": "bybit",
    }
    receipt_path = tail._begin_cell_venue(
        tmp_path,
        manifest=manifest,
        cell="budget_010",
        venue="bybit",
        signature_payload=signature_payload,
        signature="new-signature",
    )

    after = json.loads((tmp_path / "summary.json").read_text())
    assert after["complete_registered_matrix"] is False
    assert after["verdicts"]["budget_010"]["status"] == "incomplete"
    assert json.loads(receipt_path.read_text())["status"] == "running"
