from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path

import polars as pl
import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "continuous_v2_ab_research_runner.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("continuous_v2_ab_research_runner", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_control_component_config_pins_current_v2_knobs() -> None:
    runner = _load_runner()

    cfg = runner.v2_component_config(
        runner.V2_COMPONENTS[0],
        start_date="2023-04-01",
        end_date="2026-06-18",
    )

    assert cfg.rmom_quantile == 0.25
    assert cfg.feature_set == ("max_ret168",)
    assert cfg.btc_trend_gate == "uptrend"
    assert cfg.sizing_mode == "inverse_vol"
    assert cfg.target_vol_per_name == 0.01
    assert cfg.vol_weight_clamp == 2.0
    assert cfg.hold_hours == 24
    assert cfg.take_profit_pct == 0.12  # operator override 2026-06-19: TP promoted 0.10 -> 0.12
    assert cfg.stop_loss_pct == 0.0
    assert cfg.stop_approach_frac == 0.0
    assert cfg.entry_pause_after_adverse_exits == 8
    assert cfg.entry_crowding_max_fresh == 2


def test_experimental_arm_requires_control_in_same_run_dir(tmp_path: Path) -> None:
    runner = _load_runner()
    out = tmp_path / "run"

    with pytest.raises(RuntimeError, match="without V2_CONTROL"):
        runner.enforce_control_guard(["A4_REGIME_HEDGE_INTENSITY"], ["bybit"], out)

    control_dir = out / "V2_CONTROL" / "bybit"
    control_dir.mkdir(parents=True)
    (control_dir / "summary.json").write_text("{}", encoding="utf-8")
    runner.enforce_control_guard(["A4_REGIME_HEDGE_INTENSITY"], ["bybit"], out)


def test_registered_experimental_arms_are_not_runnable_before_almanac() -> None:
    runner = _load_runner()

    with pytest.raises(RuntimeError, match="not runnable yet"):
        runner._assert_implemented("C3_FLOW_SQUEEZE_HEDGE_INTENSITY")


def test_runner_does_not_import_old_local_artifact_dirs() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")

    assert "FULL_LIVE_ARTIFACT" not in source
    assert "continuous_live_feature_ablation_runner" not in source
    assert "sys.path.insert(0, str(SHARED" not in source


def test_control_run_writes_strict_artifacts(monkeypatch, tmp_path: Path) -> None:
    runner = _load_runner()
    data_root = tmp_path / "bybit_full_pit"
    data_root.mkdir()
    monkeypatch.setitem(runner.ROOTS, "bybit", data_root)

    t0 = 1_680_307_200_000
    day = runner.MS_PER_DAY
    calls = []

    def fake_run_continuous_event_research(_data_root, *, config, report_dir, **_kwargs):
        calls.append(config.config_hash())
        report_dir = Path(report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        trades = pl.DataFrame(
            {
                "symbol": ["AAAUSDT"],
                "side": ["short"],
                "entry_ts_ms": [t0 + runner.MS_PER_HOUR],
                "exit_ts_ms": [t0 + day + runner.MS_PER_HOUR],
                "entry_date": ["2023-04-01"],
                "notional_weight": [0.01],
                "gross_return": [0.001],
                "cost_return": [-0.0001],
                "funding_return": [0.0],
                "net_return": [0.0009],
                "exit_reason": ["max_hold"],
            }
        )
        mtm = pl.DataFrame(
            {
                "ts_ms": [t0, t0 + day, t0 + 2 * day],
                "basket_return": [0.001, -0.0005, 0.002],
                "equity": [1.001, 1.0005, 1.0025],
                "drawdown": [0.0, -0.0005, 0.0],
            }
        )
        trades.write_csv(report_dir / "continuous_trades.csv")
        mtm.write_csv(report_dir / "continuous_mtm_equity.csv")
        mtm.write_csv(report_dir / "continuous_equity.csv")
        payload = {
            "config": asdict(config),
            "config_hash": config.config_hash(),
            "n_trades": 1,
            "funding_mode": "modeled",
            "skips": {},
        }
        (report_dir / "continuous_report.json").write_text(json.dumps(payload), encoding="utf-8")
        return payload

    monkeypatch.setattr(runner, "run_continuous_event_research", fake_run_continuous_event_research)
    monkeypatch.setattr(
        runner,
        "instrument_inputs",
        lambda *_args, **_kwargs: ({t0: 0.0, t0 + day: 0.0, t0 + 2 * day: 0.0}, {}),
    )

    out_root = tmp_path / "ab"
    summary = runner.run_arm_venue(
        "V2_CONTROL",
        "bybit",
        out_root=out_root,
        start_date="2023-04-01",
        end_date="2023-04-04",
        resume=False,
    )
    runner.write_pooled_tables(out_root, [summary])

    arm_dir = out_root / "V2_CONTROL" / "bybit"
    for name in [
        "config.json",
        "config_hash.txt",
        "trades.csv",
        "orders_or_fill_model.csv",
        "mtm.csv",
        "equity.csv",
        "monthly.csv",
        "splits.json",
        "summary.json",
        "run_report.md",
        "checkpoint.json",
    ]:
        assert (arm_dir / name).exists(), name

    assert (out_root / "ab_table.csv").exists()
    assert (out_root / "pooled_ab_table.csv").exists()
    assert (out_root / "decision_rule_input.csv").exists()
    assert summary["run_label"] == runner.RUN_LABEL
    assert summary["n_trades"] == 3
    report = (arm_dir / "run_report.md").read_text(encoding="utf-8")
    assert "Run label: `exploratory`" in report
    assert "Data root:" in report
    assert "## Split Metrics" in report
    assert "## OOS / Forward Status" in report

    calls.clear()
    runner.run_arm_venue(
        "V2_CONTROL",
        "bybit",
        out_root=out_root,
        start_date="2023-04-01",
        end_date="2023-04-04",
        resume=True,
    )
    assert calls == []

    almanac_root = tmp_path / "a4b_almanac"
    almanac_root.mkdir()
    pl.DataFrame(
        {
            "venue": ["bybit"] * 3,
            "component": ["turn3p3", "turn4p3", "turn4p5"],
            "component_live_tag": ["p3", "p4p3", "p4p5"],
            "symbol": ["AAAUSDT", "AAAUSDT", "AAAUSDT"],
            "signal_ts_ms": [t0, t0, t0],
            "decision_ts_ms": [t0 + runner.MS_PER_HOUR] * 3,
            "order_submit_ts_ms": [t0 + 2 * runner.MS_PER_HOUR] * 3,
            "day_ts": [t0, t0, t0],
            "btc_vol_percentile_250d": [0.5, 0.5, 0.5],
            "market_dispersion_1d": [0.1, 0.1, 0.1],
            "market_breadth_1d": [0.6, 0.6, 0.6],
            "alt_minus_btc_1d": [0.01, 0.01, 0.01],
            "funding_level": [0.0001, 0.0001, 0.0001],
            "funding_change": [0.0, 0.0, 0.0],
            "premium_level": [0.0, 0.0, 0.0],
            "premium_change": [0.0, 0.0, 0.0],
            "btc_drawdown_30d": [-0.02, -0.02, -0.02],
        }
    ).write_parquet(almanac_root / "feature_tape_bybit.parquet")
    calls.clear()
    a4b_summary = runner.run_arm_venue(
        runner.A4B_ARM,
        "bybit",
        out_root=out_root,
        start_date="2023-04-01",
        end_date="2023-04-04",
        resume=False,
        almanac_root=almanac_root,
    )
    assert calls == []
    assert a4b_summary["amendment"] == runner.AMENDMENT_A4B
    assert (out_root / runner.A4B_ARM / "bybit" / "hedge_intensity.csv").exists()

    runner.write_pooled_tables(out_root, [summary, a4b_summary])
    robustness = runner.write_robustness_report(out_root, n_boot=20, block=1, seed=7)
    assert robustness["run_label"] == "continuous_v2_ab_robustness"
    assert (out_root / "robustness.csv").exists()
    assert (out_root / "robustness_report.md").exists()

    stale_summary = json.loads((arm_dir / "summary.json").read_text(encoding="utf-8"))
    stale_summary["config_hash"] = "stale"
    (arm_dir / "summary.json").write_text(json.dumps(stale_summary), encoding="utf-8")
    for component in runner.V2_COMPONENTS:
        report_path = arm_dir / "components" / component.key / "continuous_report.json"
        report_payload = json.loads(report_path.read_text(encoding="utf-8"))
        report_payload["config_hash"] = "stale"
        report_path.write_text(json.dumps(report_payload), encoding="utf-8")
    runner.run_arm_venue(
        "V2_CONTROL",
        "bybit",
        out_root=out_root,
        start_date="2023-04-01",
        end_date="2023-04-04",
        resume=True,
    )
    assert len(calls) == len(runner.V2_COMPONENTS)


def test_feature_screen_writes_discovery_artifacts(tmp_path: Path) -> None:
    runner = _load_runner()
    venue = "bybit"
    ab_root = tmp_path / "ab"
    almanac_root = tmp_path / "almanac"
    arm_dir = ab_root / "V2_CONTROL" / venue
    arm_dir.mkdir(parents=True)
    almanac_root.mkdir(parents=True)

    t0 = 1_680_307_200_000
    rows = []
    tape_rows = []
    neg_rows = []
    equity_rows = []
    for i in range(40):
        ts = t0 + i * runner.MS_PER_DAY
        sym = f"S{i % 5}USDT"
        rows.append(
            {
                "trade_id": f"t{i}",
                "entry_signal_ts_ms": ts,
                "entry_ts_ms": ts + runner.MS_PER_HOUR,
                "exit_ts_ms": ts + 25 * runner.MS_PER_HOUR,
                "symbol": sym,
                "component": "turn3p3",
                "net_return": (i - 20) / 1_000_000,
                "mae": -abs(i - 20) / 10_000,
            }
        )
        tape_rows.append(
            {
                "venue": venue,
                "component": "turn3p3",
                "component_live_tag": "p3",
                "symbol": sym,
                "signal_ts_ms": ts,
                "decision_ts_ms": ts + runner.MS_PER_HOUR,
                "order_submit_ts_ms": ts + 2 * runner.MS_PER_HOUR,
                "day_ts": ts,
                "current_composite": i / 40,
                "score_margin_d9_d8": i / 100,
                "btc_ret_30d": i / 200,
                "funding_level": 0.0001,
                "premium_level": i / 10_000,
            }
        )
        neg_rows.append(
            {
                "venue": venue,
                "component": "turn3p3",
                "symbol": sym,
                "signal_ts_ms": ts,
                "symbol_hash": i % 5,
                "calendar_hash": i,
                "shuffled_within_symbol": 40 - i,
                "shuffled_within_day": i * 7,
            }
        )
        equity_rows.append(
            {
                "ts_ms": ts,
                "basket_return": (i - 20) / 100_000,
                "equity": 1.0 + i / 10_000,
                "drawdown": -0.001,
            }
        )

    pl.DataFrame(rows).write_csv(arm_dir / "trades.csv")
    pl.DataFrame(equity_rows).write_csv(arm_dir / "equity.csv")
    pl.DataFrame(tape_rows).write_parquet(almanac_root / f"feature_tape_{venue}.parquet")
    pl.DataFrame(neg_rows).write_csv(almanac_root / "negative_controls.csv")
    pl.DataFrame(
        [
            {
                "feature": feature,
                "source_table": "test",
                "family": "test",
                "venue": venue,
                "candidate_rows": 40,
                "coverage": 1.0,
                "earliest_available_ts": t0,
                "latest_available_ts": t0,
                "earliest_available_date": "2023-04-01",
                "latest_available_date": "2023-04-01",
                "decision_lag": "closed-bar causal",
                "admissible_for_full_ab": True,
                "known_gaps": "",
            }
            for feature in ["current_composite", "score_margin_d9_d8", "btc_ret_30d", "funding_level", "premium_level"]
        ]
    ).write_csv(almanac_root / "feature_inventory.csv")

    out_root = tmp_path / "screens"
    summary = runner.build_feature_screens(
        out_root=out_root,
        venues=[venue],
        almanac_root=almanac_root,
        ab_root=ab_root,
    )

    assert summary["run_label"] == "feature_screen_discovery"
    assert summary["venue_summaries"][venue]["unmatched_feature_rows"] == 0
    assert (out_root / "trade_feature_screen.csv").exists()
    assert (out_root / "daily_feature_screen.csv").exists()
    assert (out_root / f"executed_feature_tape_{venue}.parquet").exists()
    assert (out_root / "readme.md").exists()


def test_binance_metrics_archive_feeds_oi_and_flow(tmp_path: Path) -> None:
    runner = _load_runner()
    root = tmp_path / "binance"
    metrics = root / "binance_usdm_metrics_5m"
    metrics.mkdir(parents=True)
    t0 = 1_704_067_200_000
    rows = []
    for idx in range(30):
        rows.append(
            {
                "symbol": "AAAUSDT",
                "ts_ms": t0 + idx * runner.MS_PER_HOUR,
                "sum_open_interest": 1000.0 + idx,
                "sum_open_interest_value": 2000.0 + idx,
                "sum_taker_long_short_vol_ratio": 2.0,
            }
        )
        rows.append(
            {
                "symbol": "BBBUSDT",
                "ts_ms": t0 + idx * runner.MS_PER_HOUR,
                "sum_open_interest": 3000.0 + idx,
                "sum_open_interest_value": 4000.0 + idx,
                "sum_taker_long_short_vol_ratio": 0.5,
            }
        )
    pl.DataFrame(rows).write_parquet(metrics / "AAAUSDT.parquet")
    pl.DataFrame([r for r in rows if r["symbol"] == "BBBUSDT"]).write_parquet(metrics / "BBBUSDT.parquet")
    pl.DataFrame([r for r in rows if r["symbol"] == "AAAUSDT"]).write_parquet(metrics / "AAAUSDT.parquet")
    tape = pl.DataFrame(
        {
            "symbol": ["AAAUSDT"],
            "decision_ts_ms": [t0 + 29 * runner.MS_PER_HOUR + runner.MS_PER_HOUR],
        }
    )

    hourly = runner._binance_metrics_hourly(root, tape)
    oi = runner._oi_feature_values(root, tape, hourly)
    flow = runner._flow_feature_values(root, tape, hourly)

    assert not hourly.is_empty()
    assert {"oi_level", "oi_change_24h", "oi_acceleration"}.issubset(set(oi.columns))
    assert {"taker_imbalance_1h", "market_flow", "idiosyncratic_flow"}.issubset(set(flow.columns))
    latest_flow = flow.filter(pl.col("symbol") == "AAAUSDT").sort("available_ts_ms").tail(1).to_dicts()[0]
    assert latest_flow["taker_imbalance_1h"] == pytest.approx(1 / 3)
    assert latest_flow["market_flow"] == pytest.approx(0.0)
    assert latest_flow["idiosyncratic_flow"] == pytest.approx(1 / 3)


def test_binance_only_flow_arms_registered_and_scoped() -> None:
    runner = _load_runner()
    for arm in (
        runner.C0_FLOW_SCREEN_ARM,
        runner.C1_FLOW_SIZING_ARM,
        runner.C1H_FLOW_SIZING_HASH_ARM,
        runner.C2_FLOW_ARM,
        runner.C3_FLOW_ARM,
        runner.C4_FLOW_ADMISSION_ARM,
        runner.C5_FLOW_EXIT_ARM,
        runner.C6_FLOW_NONLINEAR_ARM,
        runner.C7_FLOW_HASH_ARM,
    ):
        assert arm in runner.ARM_DEFINITIONS
        definition = runner.ARM_DEFINITIONS[arm]
        assert definition.claimed_venue_scope == runner.CLAIMED_SCOPE_BINANCE_FLOW
        assert definition.venues_allowed == ("binance",)
        assert runner.claimed_scope_for(arm) == runner.CLAIMED_SCOPE_BINANCE_FLOW

    # Implemented overlays/sizing vs registered-but-blocked exploratory arms.
    assert runner.ARM_DEFINITIONS[runner.C2_FLOW_ARM].implemented
    assert runner.ARM_DEFINITIONS[runner.C3_FLOW_ARM].implemented
    assert runner.ARM_DEFINITIONS[runner.C7_FLOW_HASH_ARM].implemented
    assert runner.ARM_DEFINITIONS[runner.C1_FLOW_SIZING_ARM].implemented
    assert runner.ARM_DEFINITIONS[runner.C1H_FLOW_SIZING_HASH_ARM].implemented
    # C1 flow sizing is a de-risk arm (negative sign) and routes to its own amendment.
    assert runner.SIZING_ARM_SPECS[runner.C1_FLOW_SIZING_ARM]["sign"] == -1.0
    assert runner.amendment_for(runner.C1_FLOW_SIZING_ARM) == runner.AMENDMENT_C1_FLOW_SIZING
    assert runner.ARM_DEFINITIONS[runner.C0_FLOW_SCREEN_ARM].screen_only
    assert not runner.ARM_DEFINITIONS[runner.C0_FLOW_SCREEN_ARM].implemented
    for blocked in (runner.C4_FLOW_ADMISSION_ARM, runner.C5_FLOW_EXIT_ARM, runner.C6_FLOW_NONLINEAR_ARM):
        assert not runner.ARM_DEFINITIONS[blocked].implemented

    # The two-venue candidate-track ids stay blocked (Bybit full-market flow gap).
    assert not runner.ARM_DEFINITIONS["C2_MARKET_FLOW_HEDGE_INTENSITY"].implemented
    assert not runner.ARM_DEFINITIONS["C3_FLOW_SQUEEZE_HEDGE_INTENSITY"].implemented
    assert runner.claimed_scope_for("V2_CONTROL") == runner.CLAIMED_SCOPE_TWO_VENUE


def test_binance_only_arm_refuses_bybit(tmp_path: Path) -> None:
    runner = _load_runner()
    with pytest.raises(RuntimeError, match="scoped to venues"):
        runner.run_arm_venue(
            runner.C2_FLOW_ARM,
            "bybit",
            out_root=tmp_path / "ab",
            start_date="2023-04-01",
            end_date="2023-04-04",
            resume=False,
        )

    with pytest.raises(RuntimeError, match="discovery screen"):
        runner._assert_implemented(runner.C0_FLOW_SCREEN_ARM)


def test_expanding_resid_series_is_causal() -> None:
    runner = _load_runner()
    x = [float(i) for i in range(30)]
    y = [2.0 * i + 1.0 for i in range(30)]
    resid = runner._expanding_resid_series(y, x, min_obs=20)
    assert all(v is None for v in resid[:20])
    # A perfect linear relationship -> ~0 residual once the expanding fit warms up.
    assert resid[25] == pytest.approx(0.0, abs=1e-9)

    # Causality: changing a FUTURE y must not move any earlier residual.
    y_future = list(y)
    y_future[-1] = 999.0
    resid_future = runner._expanding_resid_series(y_future, x, min_obs=20)
    assert resid[:29] == resid_future[:29]
    assert resid[29] != resid_future[29]

    z = runner._expanding_prior_z_series([float(i) for i in range(15)], min_obs=10)
    assert all(v is None for v in z[:10])
    assert z[10] is not None


def test_attach_flow_residual_and_squeeze_is_binance_only(tmp_path: Path) -> None:
    runner = _load_runner()
    t0 = 1_704_067_200_000
    n = 30
    base = {
        "venue": ["binance"] * n,
        "symbol": ["AAAUSDT"] * n,
        "component": ["turn3p3"] * n,
        "signal_ts_ms": [t0 + i * runner.MS_PER_DAY for i in range(n)],
        "decision_ts_ms": [t0 + i * runner.MS_PER_DAY + runner.MS_PER_HOUR for i in range(n)],
        "path_max_ret168": [float(i) for i in range(n)],
        "oi_change_24h": [0.01 * ((i % 5) - 2) for i in range(n)],
        "funding_level": [0.0001 * ((i % 3) - 1) for i in range(n)],
    }
    binance = pl.DataFrame({**base, "taker_imbalance_24h": [2.0 * i + 1.0 for i in range(n)]})
    out = runner._attach_flow_residual_and_squeeze(binance).sort("signal_ts_ms")
    assert {"flow_squeeze", "flow_resid_return"}.issubset(set(out.columns))
    resid = out["flow_resid_return"].to_list()
    squeeze = out["flow_squeeze"].to_list()
    assert all(v is None for v in resid[:20])
    assert resid[25] == pytest.approx(0.0, abs=1e-9)
    assert all(v is None for v in squeeze[:10])
    assert squeeze[-1] is not None

    # Bybit-like tape: no value-built taker flow -> flow_squeeze/flow_resid stay null
    # even though funding_level is present (no funding-only pseudo-squeeze).
    bybit = pl.DataFrame(
        {**base, "venue": ["bybit"] * n, "taker_imbalance_24h": [None] * n}
    ).with_columns(pl.col("taker_imbalance_24h").cast(pl.Float64))
    out_bybit = runner._attach_flow_residual_and_squeeze(bybit)
    assert out_bybit["flow_squeeze"].null_count() == n
    assert out_bybit["flow_resid_return"].null_count() == n


def test_overlay_extra_intensity_is_mean_one_and_hashable(tmp_path: Path) -> None:
    runner = _load_runner()
    almanac_root = tmp_path / "almanac"
    almanac_root.mkdir()
    t0 = 1_704_067_200_000
    n = 80
    days = [t0 + i * runner.MS_PER_DAY for i in range(n)]
    pl.DataFrame(
        {
            "day_ts": days,
            "market_flow": [0.2 * ((i % 7) - 3) for i in range(n)],
            "flow_squeeze": [0.1 * ((i % 5) - 2) for i in range(n)],
        }
    ).write_parquet(almanac_root / "feature_tape_binance.parquet")

    extra, diag = runner._overlay_extra_intensity(
        arm_id=runner.C2_FLOW_ARM, days=days, venue="binance", almanac_root=almanac_root
    )
    assert set(extra) == set(days)
    assert sum(extra.values()) / len(extra) == pytest.approx(1.0, abs=1e-9)
    assert all(runner.OVERLAY_CLIP[0] - 1e-9 <= v <= runner.OVERLAY_CLIP[1] + 1e-9 for v in extra.values())
    assert "market_flow_z" in diag.columns
    assert not bool(diag["overlay_hash_control"][0])

    hashed, diag_hash = runner._overlay_extra_intensity(
        arm_id=runner.C7_FLOW_HASH_ARM, days=days, venue="binance", almanac_root=almanac_root
    )
    assert sum(hashed.values()) / len(hashed) == pytest.approx(1.0, abs=1e-9)
    assert bool(diag_hash["overlay_hash_control"][0])


def test_robustness_forces_exploratory_for_binance_only_flow(tmp_path: Path) -> None:
    runner = _load_runner()
    out_root = tmp_path / "ab"
    out_root.mkdir()
    months = [f"2025-{m:02d}" for m in range(1, 9)]

    def _write_monthly(arm: str, venue: str, rets: list[float]) -> str:
        path = out_root / arm / venue
        path.mkdir(parents=True, exist_ok=True)
        monthly = path / "monthly.csv"
        pl.DataFrame(
            {"month": months, "strategy_return": rets, "trades": [50] * len(months)}
        ).write_csv(monthly)
        return str(monthly)

    rows = []
    for arm, venue, total, mar, rets in (
        ("V2_CONTROL", "binance", 0.50, 8.0, [0.05] * 8),
        (runner.C2_FLOW_ARM, "binance", 0.55, 9.0, [0.06] * 8),
    ):
        rows.append(
            {
                "arm_id": arm,
                "venue": venue,
                "run_label": runner.RUN_LABEL,
                "total_return": total,
                "max_drawdown": -0.05,
                "mar": mar,
                "sharpe_like": 3.0,
                "worst_day_return": -0.03,
                "n_trades": 400,
                "config_hash": "deadbeef",
                "summary_path": str(out_root / arm / venue / "summary.json"),
                "monthly_path": _write_monthly(arm, venue, rets),
            }
        )
    pl.DataFrame(rows).write_csv(out_root / "ab_table.csv")

    summary = runner.write_robustness_report(out_root, n_boot=50, block=1, seed=1)
    by_arm = {row["arm_id"]: row for row in summary["cross_venue"]}
    assert runner.C2_FLOW_ARM in by_arm
    assert by_arm[runner.C2_FLOW_ARM]["verdict"] == "EXPLORATORY single-venue flow (no Tier-2 candidate pass)"
    assert by_arm[runner.C2_FLOW_ARM]["claimed_venue_scope"] == runner.CLAIMED_SCOPE_BINANCE_FLOW


def test_tp_variant_arms_override_take_profit() -> None:
    runner = _load_runner()
    for arm in (runner.EXIT_TP12_ARM, runner.EXIT_TP15_ARM):
        assert arm in runner.ARM_DEFINITIONS
        assert arm in runner.TP_VARIANT_ARMS
        assert runner.ARM_DEFINITIONS[arm].implemented
        assert runner.ARM_DEFINITIONS[arm].venues_allowed == ("bybit", "binance")
        assert runner.claimed_scope_for(arm) == runner.CLAIMED_SCOPE_TWO_VENUE
    assert runner.tp_override_for(runner.EXIT_TP12_ARM) == 0.12
    assert runner.tp_override_for(runner.EXIT_TP15_ARM) == 0.15
    assert runner.tp_override_for("V2_CONTROL") is None
    assert runner.amendment_for(runner.EXIT_TP12_ARM) == runner.AMENDMENT_F2_EXIT_ALPHA
    # The override flows into the component config; control is the promoted 0.12 (operator
    # override 2026-06-19), and an explicit override still re-pins it.
    spec = runner.V2_COMPONENTS[0]
    base = runner.v2_component_config(spec, start_date="2023-04-01", end_date="2026-06-18")
    over = runner.v2_component_config(spec, start_date="2023-04-01", end_date="2026-06-18", take_profit_pct=0.15)
    assert base.take_profit_pct == 0.12
    assert over.take_profit_pct == 0.15
    assert base.config_hash() != over.config_hash()


def test_sizing_arms_registered_two_venue() -> None:
    runner = _load_runner()
    for arm in (
        runner.B1_SCORE_SIZING_ARM,
        runner.B1P_PATH_SIZING_ARM,
        runner.B6_SCORE_HASH_ARM,
        runner.B6P_PATH_HASH_ARM,
    ):
        assert arm in runner.ARM_DEFINITIONS
        assert arm in runner.SIZING_ARMS
        assert runner.ARM_DEFINITIONS[arm].implemented
        # Candidate-track: both venues, two-venue scope (these are NOT flow arms).
        assert runner.ARM_DEFINITIONS[arm].venues_allowed == ("bybit", "binance")
        assert runner.claimed_scope_for(arm) == runner.CLAIMED_SCOPE_TWO_VENUE
        assert arm not in runner.OVERLAY_ARMS
    assert runner.amendment_for(runner.B1_SCORE_SIZING_ARM) == runner.AMENDMENT_B_SIZING
    # Cache key is stable and changes with the feature.
    k1 = runner._sizing_cache_key(runner.B1_SCORE_SIZING_ARM, "turn3p3")
    assert k1 == runner._sizing_cache_key(runner.B1_SCORE_SIZING_ARM, "turn3p3")
    assert k1 != runner._sizing_cache_key(runner.B1P_PATH_SIZING_ARM, "turn3p3")


def test_sizing_mult_lookup_is_causal_and_hashable(tmp_path: Path) -> None:
    runner = _load_runner()
    almanac_root = tmp_path / "almanac"
    almanac_root.mkdir()
    t0 = 1_704_067_200_000
    rows = []
    for sym, base in (("AAAUSDT", 0.0), ("BBBUSDT", 5.0)):
        for i in range(30):
            # Oscillating (stationary) feature so the expanding-prior z is ~mean-0,
            # i.e. the multiplier is ~mean-1 (a monotone feature would bias z positive).
            rows.append(
                {
                    "component": "turn3p3",
                    "symbol": sym,
                    "signal_ts_ms": t0 + i * runner.MS_PER_DAY,
                    "score_margin_d9_d8": base + float((i * 5) % 9) - 4.0,
                }
            )
    pl.DataFrame(rows).write_parquet(almanac_root / "feature_tape_binance.parquet")

    lookup, diag = runner._sizing_mult_lookup(
        arm_id=runner.B1_SCORE_SIZING_ARM, venue="binance", component_key="turn3p3", almanac_root=almanac_root
    )
    assert len(lookup) == 60
    assert all(isinstance(k[0], str) and isinstance(k[1], int) for k in lookup)
    lo, hi = runner.SIZING_CLIP
    assert all(lo - 1e-9 <= v <= hi + 1e-9 for v in lookup.values())
    assert 0.85 <= diag["mean_mult"] <= 1.15
    assert diag["nontrivial"] > 0

    # Strict causality: changing a FUTURE row's feature must not move an earlier mult.
    early_key = ("AAAUSDT", t0 + 15 * runner.MS_PER_DAY)
    rows_future = [dict(r) for r in rows]
    for r in rows_future:
        if r["symbol"] == "AAAUSDT" and r["signal_ts_ms"] == t0 + 29 * runner.MS_PER_DAY:
            r["score_margin_d9_d8"] = 999.0
    pl.DataFrame(rows_future).write_parquet(almanac_root / "feature_tape_binance.parquet")
    lookup_future, _ = runner._sizing_mult_lookup(
        arm_id=runner.B1_SCORE_SIZING_ARM, venue="binance", component_key="turn3p3", almanac_root=almanac_root
    )
    assert lookup_future[early_key] == pytest.approx(lookup[early_key])

    # Hash control: same multiplier multiset, different (symbol, ts) assignment.
    hashed, _ = runner._sizing_mult_lookup(
        arm_id=runner.B6_SCORE_HASH_ARM, venue="binance", component_key="turn3p3", almanac_root=almanac_root
    )
    assert sorted(hashed.values()) == pytest.approx(sorted(lookup_future.values()))
    assert hashed != lookup_future
