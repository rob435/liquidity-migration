from __future__ import annotations

import math
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration import continuous_addon_shadow as cas
from liquidity_migration._common import finite_float
from liquidity_migration.cli import build_parser
from liquidity_migration.cli import main as cli_main
from liquidity_migration.continuous_addon_shadow import (
    ContinuousAddonShadowAuditConfig,
    _cooldown_simulation_summary,
    _cost_corrected_return,
    run_continuous_addon_shadow_audit,
)
from liquidity_migration.storage import write_dataset


def _write_shadow_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    primary_root = tmp_path / "primary"
    addon_root = tmp_path / "addon"
    primary_root.mkdir()
    addon_root.mkdir()

    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "p-a",
                    "symbol": "AAAUSDT",
                    "strategy_id": "continuous_fade_v2_paper",
                    "side": "short",
                    "status": "open",
                    "signal_ts_ms": 10_000,
                    "entry_ts_ms": 20_000,
                    "entry_price": 100.0,
                    "qty": "1",
                },
                {
                    "trade_id": "p-b",
                    "symbol": "BBBUSDT",
                    "strategy_id": "continuous_fade_v2_paper",
                    "side": "short",
                    "status": "closed",
                    "signal_ts_ms": 30_000,
                    "entry_ts_ms": 40_000,
                    "exit_ts_ms": 60_000,
                    "entry_price": 100.0,
                    "exit_price": 90.0,
                    "qty": "1",
                },
            ]
        ),
        primary_root,
        "continuous_fade_paper_trades",
        partition_by=(),
    )
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "lm-en-c-AAA-1",
                    "trade_id": "p-a",
                    "symbol": "AAAUSDT",
                    "strategy_id": "continuous_fade_v2_paper",
                    "reduce_only": False,
                    "status": "planned",
                    "signal_ts_ms": 10_000,
                },
                {
                    "order_link_id": "lm-en-c-AAA-1",
                    "trade_id": "p-a",
                    "symbol": "AAAUSDT",
                    "strategy_id": "continuous_fade_v2_paper",
                    "reduce_only": False,
                    "status": "planned",
                    "signal_ts_ms": 10_000,
                },
            ]
        ),
        primary_root,
        "continuous_fade_paper_orders",
        partition_by=(),
    )
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "a-a",
                    "symbol": "AAAUSDT",
                    "strategy_id": "continuous_fade_addon_v2_paper",
                    "side": "short",
                    "status": "open",
                    "signal_ts_ms": 10_000,
                    "entry_ts_ms": 50_000,
                    "entry_price": 120.0,
                    "qty": "1",
                },
                {
                    "trade_id": "a-b",
                    "symbol": "BBBUSDT",
                    "strategy_id": "continuous_fade_addon_v2_paper",
                    "side": "short",
                    "status": "closed",
                    "signal_ts_ms": 30_000,
                    "entry_ts_ms": 40_000,
                    "exit_ts_ms": 55_000,
                    "entry_price": 110.0,
                    "exit_price": 100.0,
                    "qty": "1",
                },
                {
                    "trade_id": "a-c-extra",
                    "symbol": "CCCUSDT",
                    "strategy_id": "continuous_fade_addon_v2_paper",
                    "side": "short",
                    "status": "closed",
                    "signal_ts_ms": 70_000,
                    "entry_ts_ms": 80_000,
                    "exit_ts_ms": 90_000,
                    "entry_price": 10.0,
                    "exit_price": 9.0,
                    "qty": "1",
                },
            ]
        ),
        addon_root,
        "continuous_fade_paper_trades",
        partition_by=(),
    )
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "lm-en-ca-AAA-1",
                    "trade_id": "a-a",
                    "symbol": "AAAUSDT",
                    "strategy_id": "continuous_fade_addon_v2_paper",
                    "reduce_only": False,
                    "status": "planned",
                    "signal_ts_ms": 10_000,
                },
                {
                    "order_link_id": "lm-en-ca-AAA-1",
                    "trade_id": "a-a",
                    "symbol": "AAAUSDT",
                    "strategy_id": "continuous_fade_addon_v2_paper",
                    "reduce_only": False,
                    "status": "submitted_unconfirmed",
                    "signal_ts_ms": 10_000,
                },
                {
                    "order_link_id": "lm-en-ca-AAA-2",
                    "trade_id": "a-a-retry",
                    "symbol": "AAAUSDT",
                    "strategy_id": "continuous_fade_addon_v2_paper",
                    "reduce_only": False,
                    "status": "planned",
                    "signal_ts_ms": 10_000,
                },
                {
                    "order_link_id": "lm-en-ca-BBB-1",
                    "trade_id": "a-b",
                    "symbol": "BBBUSDT",
                    "strategy_id": "continuous_fade_addon_v2_paper",
                    "reduce_only": False,
                    "status": "filled",
                    "signal_ts_ms": 30_000,
                },
                {
                    "order_link_id": "lm-ux-ca-AAA-exit",
                    "trade_id": "a-a",
                    "symbol": "AAAUSDT",
                    "strategy_id": "continuous_fade_addon_v2_paper",
                    "reduce_only": True,
                    "status": "filled",
                    "signal_ts_ms": 10_000,
                },
            ]
        ),
        addon_root,
        "continuous_fade_paper_orders",
        partition_by=(),
    )
    write_dataset(
        pl.DataFrame(
            [
                {
                    "cycle_id": "c1",
                    "ts_ms": 1_700_000_000_000,
                    "addon_primary_pnl_gate_skips": 2,
                    "skipped_same_signal_reentry": 1,
                    "entry_candidates": 5,
                    "entries": 1,
                },
                {
                    "cycle_id": "c2",
                    "ts_ms": 1_700_000_060_000,
                    "addon_primary_pnl_gate_skips": 3,
                    "skipped_same_signal_reentry": 2,
                    "candidates": 4,
                    "entries_executed": 2,
                },
            ]
        ),
        addon_root,
        "continuous_fade_paper_cycles",
        partition_by=(),
    )

    historical_csv = tmp_path / "blended_trades.csv"
    pl.DataFrame(
        [
            {
                "blend_source": "primary",
                "symbol": "AAAUSDT",
                "entry_signal_ts_ms": 10_000,
                "entry_ts_ms": 20_000,
                "exit_ts_ms": 0,
            },
            {
                "blend_source": "addon",
                "symbol": "AAAUSDT",
                "entry_signal_ts_ms": 10_000,
                "entry_ts_ms": 50_000,
                "exit_ts_ms": 0,
                "entry_price": 1.0,
                "qty": 1.0,
            },
            {
                "blend_source": "addon",
                "symbol": "DDDUSDT",
                "entry_signal_ts_ms": 90_000,
                "entry_ts_ms": 95_000,
                "exit_ts_ms": 100_000,
                "entry_price": 999.0,
                "qty": 1.0,
            },
        ]
    ).write_csv(historical_csv)
    return primary_root, addon_root, historical_csv


def test_continuous_addon_shadow_audit_reports_overlap_and_history_diff(tmp_path: Path) -> None:
    primary_root, addon_root, historical_csv = _write_shadow_fixture(tmp_path)
    out_dir = tmp_path / "audit"

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            historical_blended_trades_csv=str(historical_csv),
            expected_primary_strategy_id="continuous_fade_v2_paper",
            expected_addon_strategy_id="continuous_fade_addon_v2_paper",
            expected_primary_entry_order_prefix="lm-en-c",
            expected_addon_entry_order_prefix="lm-en-ca",
            output_dir=str(out_dir),
            audit_now_ms=1_700_000_120_000,
        )
    )

    summary = payload["summary"]
    assert summary["primary"]["trades"] == 2
    assert summary["addon"]["trades"] == 3
    assert summary["shadow_overlap"] == {
        "active_same_symbol_primary": 2,
        "exact_same_entry_primary": 1,
    }
    assert summary["shadow_anatomy"] == {
        "addon_to_primary_ratio": 1.5,
        "active_same_symbol_overlap_fraction": 2 / 3,
        "exact_same_entry_fraction": 1 / 3,
    }
    assert summary["primary"]["repeated_entry_keys"] == 0
    assert summary["primary"]["repeated_entry_rows"] == 0
    assert summary["primary"]["max_entries_per_entry_key"] == 1
    assert summary["addon"]["repeated_entry_keys"] == 0
    assert summary["addon"]["repeated_entry_rows"] == 0
    assert summary["addon"]["max_entries_per_entry_key"] == 1
    assert summary["primary_orders"] == {
        "entry_order_attempts": 1,
        "unique_symbols": 1,
        "problem_entry_order_attempts": 0,
        "status_counts": {"planned": 1},
        "repeated_entry_keys": 0,
        "repeated_entry_rows": 0,
        "max_entries_per_entry_key": 1,
    }
    assert summary["addon_orders"] == {
        "entry_order_attempts": 3,
        "unique_symbols": 2,
        "problem_entry_order_attempts": 1,
        "status_counts": {"filled": 1, "planned": 1, "submitted_unconfirmed": 1},
        "repeated_entry_keys": 1,
        "repeated_entry_rows": 1,
        "max_entries_per_entry_key": 2,
    }
    assert summary["primary_order_trade_reconciliation"] == {
        "entry_order_attempts_without_trade_key": 0,
        "live_entry_order_attempts_without_trade_key": 0,
        "max_unmatched_entry_order_age_minutes": 0.0,
        "max_unmatched_live_entry_order_age_minutes": 0.0,
    }
    assert summary["addon_order_trade_reconciliation"] == {
        "entry_order_attempts_without_trade_key": 0,
        "live_entry_order_attempts_without_trade_key": 0,
        "max_unmatched_entry_order_age_minutes": 0.0,
        "max_unmatched_live_entry_order_age_minutes": 0.0,
    }
    assert summary["addon_cycles"]["addon_primary_pnl_gate_skips"] == 5
    assert summary["addon_cycles"]["skipped_same_signal_reentry"] == 3
    assert summary["addon_cycles"]["entry_candidates"] == 9
    assert summary["addon_cycles"]["entries"] == 3
    assert summary["addon_cycles"]["candidate_pressure"] == 14
    assert summary["addon_cycles"]["entry_acceptance_fraction"] == pytest.approx(3 / 14)
    assert summary["addon_cycles"]["same_signal_reentry_skip_fraction"] == pytest.approx(3 / 14)
    assert summary["addon_cycles"]["addon_primary_pnl_gate_skip_fraction"] == pytest.approx(5 / 14)
    assert summary["addon_cycles"]["latest_cycle_ts_ms"] == 1_700_000_060_000
    assert summary["addon_cycles"]["latest_cycle_age_minutes"] == pytest.approx(1.0)
    assert summary["addon_cycles"]["max_cycle_gap_minutes"] == pytest.approx(1.0)
    assert summary["addon_cycles"]["max_candidate_pressure"] == 9
    assert summary["addon_cycles"]["worst_entry_acceptance_fraction"] == pytest.approx(0.2)
    assert summary["addon_cycles"]["worst_same_signal_reentry_skip_fraction"] == pytest.approx(2 / 9)
    assert summary["addon_cycles"]["worst_addon_primary_pnl_gate_skip_fraction"] == pytest.approx(0.4)
    assert summary["historical"]["key_diff"] == {
        "matched_addon_keys": 1,
        "missing_from_shadow": 1,
        "extra_shadow": 2,
    }
    assert summary["historical_anatomy_drift"]["addon_to_primary_ratio"] == pytest.approx(0.5)
    assert summary["historical_anatomy_drift"]["active_same_symbol_overlap_fraction"] == pytest.approx(1 / 6)
    assert summary["historical_anatomy_drift"]["exact_same_entry_fraction"] == pytest.approx(1 / 3)
    assert summary["addon_concentration"]["top1_weight_share"] == pytest.approx(0.5)
    assert summary["addon_concentration"]["top1_symbol"] == "AAAUSDT"
    assert summary["historical"]["concentration"]["top1_weight_share"] == pytest.approx(0.999)
    assert summary["historical_concentration_drift"]["top1_weight_share"] == pytest.approx(0.499)
    assert summary["primary_strategy"] == {
        "expected_strategy_id": "continuous_fade_v2_paper",
        "strategy_counts": {"continuous_fade_v2_paper": 2},
        "expected_strategy_rows": 2,
        "unexpected_strategy_rows": 0,
        "missing_strategy_rows": 0,
    }
    assert summary["addon_strategy"] == {
        "expected_strategy_id": "continuous_fade_addon_v2_paper",
        "strategy_counts": {"continuous_fade_addon_v2_paper": 3},
        "expected_strategy_rows": 3,
        "unexpected_strategy_rows": 0,
        "missing_strategy_rows": 0,
    }
    assert summary["primary_order_prefix"] == {
        "expected_entry_order_prefix": "lm-en-c",
        "entry_order_prefix_counts": {"lm-en-c": 1},
        "expected_entry_order_prefix_rows": 1,
        "unexpected_entry_order_prefix_rows": 0,
        "missing_entry_order_prefix_rows": 0,
    }
    assert summary["addon_order_prefix"] == {
        "expected_entry_order_prefix": "lm-en-ca",
        "entry_order_prefix_counts": {"lm-en-ca": 3},
        "expected_entry_order_prefix_rows": 3,
        "unexpected_entry_order_prefix_rows": 0,
        "missing_entry_order_prefix_rows": 0,
    }
    assert summary["active_exposure"] == {
        "max_active_addon_trades": 2,
        "max_active_combined_trades": 4,
        "max_active_addon_weight": 230.0,
        "max_active_combined_weight": 430.0,
        "current_open_primary_trades": 1,
        "current_open_addon_trades": 1,
        "current_open_combined_trades": 2,
        "current_open_primary_weight": 100.0,
        "current_open_addon_weight": 120.0,
        "current_open_combined_weight": 220.0,
    }
    assert summary["weight_sources"] == {
        "primary": {"entry_price_qty": 2},
        "addon": {"entry_price_qty": 3},
        "primary_unit_weight_rows": 0,
        "addon_unit_weight_rows": 0,
        "combined_unit_weight_rows": 0,
    }
    assert summary["daily_ticket_rate"] == {
        "days": 1,
        "max_primary_trades_per_day": 2,
        "max_addon_trades_per_day": 3,
        "max_combined_trades_per_day": 5,
        "max_combined_trades_day": 0,
        "max_primary_entry_order_attempts_per_day": 1,
        "max_addon_entry_order_attempts_per_day": 3,
        "max_combined_entry_order_attempts_per_day": 4,
        "max_combined_entry_order_attempts_day": 0,
    }
    assert summary["symbol_day_ticket_rate"] == {
        "symbol_days": 3,
        "max_primary_trades_per_symbol_day": 1,
        "max_addon_trades_per_symbol_day": 1,
        "max_combined_trades_per_symbol_day": 2,
        "max_combined_trades_symbol": "AAAUSDT",
        "max_combined_trades_day": 0,
        "max_primary_entry_order_attempts_per_symbol_day": 1,
        "max_addon_entry_order_attempts_per_symbol_day": 2,
        "max_combined_entry_order_attempts_per_symbol_day": 3,
        "max_combined_entry_order_attempts_symbol": "AAAUSDT",
        "max_combined_entry_order_attempts_day": 0,
    }
    same_symbol_gaps = summary["same_symbol_entry_gaps"]
    assert same_symbol_gaps["gap_rows"] == 5
    assert math.isinf(same_symbol_gaps["min_primary_same_symbol_trade_gap_minutes"])
    assert math.isinf(same_symbol_gaps["min_addon_same_symbol_trade_gap_minutes"])
    assert same_symbol_gaps["min_combined_same_symbol_trade_gap_minutes"] == 0.0
    assert math.isinf(same_symbol_gaps["min_primary_same_symbol_entry_order_gap_minutes"])
    assert same_symbol_gaps["min_addon_same_symbol_entry_order_gap_minutes"] == 0.0
    assert same_symbol_gaps["min_combined_same_symbol_entry_order_gap_minutes"] == 0.0
    assert summary["addon_cooldown_simulation"] == {
        "trade_cooldown_minutes": -1.0,
        "entry_order_cooldown_minutes": -1.0,
        "trade_candidates": 3,
        "entry_order_candidates": 3,
        "skipped_trades": 0,
        "skipped_entry_order_attempts": 0,
        "accepted_trades": 3,
        "accepted_entry_order_attempts": 3,
        "trade_suppression_fraction": 0.0,
        "entry_order_suppression_fraction": 0.0,
        "skipped_symbols": 0,
        "max_skips_per_symbol": 0,
        "skipped_trade_return_observations": 0,
        "skipped_trade_return_sum": 0,
        "skipped_trade_return_mean": 0.0,
        "skipped_trade_positive_return_fraction": 0.0,
    }
    assert summary["gate"] == {"passed": True, "failures": []}
    assert Path(payload["report_path"]).exists()
    assert payload["key_diff_csv_path"]
    diff = pl.read_csv(payload["key_diff_csv_path"])
    assert set(diff["diff_type"].to_list()) == {"missing_from_shadow", "extra_shadow"}
    assert payload["addon_concentration_csv_path"]
    addon_concentration = pl.read_csv(payload["addon_concentration_csv_path"])
    assert addon_concentration.columns == [
        "rank",
        "symbol",
        "trades",
        "weight",
        "weight_share",
        "cumulative_weight_share",
    ]
    assert addon_concentration.row(0, named=True) == {
        "rank": 1,
        "symbol": "AAAUSDT",
        "trades": 1,
        "weight": 120.0,
        "weight_share": 0.5,
        "cumulative_weight_share": 0.5,
    }
    assert payload["historical_addon_concentration_csv_path"]
    historical_concentration = pl.read_csv(payload["historical_addon_concentration_csv_path"])
    assert historical_concentration.row(0, named=True) == {
        "rank": 1,
        "symbol": "DDDUSDT",
        "trades": 1,
        "weight": 999.0,
        "weight_share": 0.999,
        "cumulative_weight_share": 0.999,
    }
    assert payload["active_exposure_csv_path"]
    active_exposure = pl.read_csv(payload["active_exposure_csv_path"])
    assert active_exposure.columns == [
        "ts_ms",
        "active_primary_trades",
        "active_addon_trades",
        "active_combined_trades",
        "active_primary_weight",
        "active_addon_weight",
        "active_combined_weight",
    ]
    assert active_exposure.sort("active_combined_weight", descending=True).row(0, named=True) == {
        "ts_ms": 50_000,
        "active_primary_trades": 2,
        "active_addon_trades": 2,
        "active_combined_trades": 4,
        "active_primary_weight": 200.0,
        "active_addon_weight": 230.0,
        "active_combined_weight": 430.0,
    }
    assert payload["active_exposure_contributors_csv_path"]
    active_contributors = pl.read_csv(payload["active_exposure_contributors_csv_path"])
    assert active_contributors.columns == [
        "ts_ms",
        "source",
        "symbol",
        "trade_id",
        "signal_ts_ms",
        "entry_ts_ms",
        "exit_ts_ms",
        "status",
        "weight",
        "weight_source",
        "active_primary_weight",
        "active_addon_weight",
        "active_combined_weight",
    ]
    peak_contributors = active_contributors.filter(pl.col("ts_ms") == 50_000).sort(["source", "symbol"])
    assert peak_contributors.select(["source", "symbol", "trade_id", "weight", "weight_source"]).to_dicts() == [
        {"source": "addon", "symbol": "AAAUSDT", "trade_id": "a-a", "weight": 120.0, "weight_source": "entry_price_qty"},
        {"source": "addon", "symbol": "BBBUSDT", "trade_id": "a-b", "weight": 110.0, "weight_source": "entry_price_qty"},
        {"source": "primary", "symbol": "AAAUSDT", "trade_id": "p-a", "weight": 100.0, "weight_source": "entry_price_qty"},
        {"source": "primary", "symbol": "BBBUSDT", "trade_id": "p-b", "weight": 100.0, "weight_source": "entry_price_qty"},
    ]
    assert peak_contributors.select(
        ["active_primary_weight", "active_addon_weight", "active_combined_weight"]
    ).unique().to_dicts() == [
        {
            "active_primary_weight": 200.0,
            "active_addon_weight": 230.0,
            "active_combined_weight": 430.0,
        }
    ]
    assert payload["daily_ticket_rate_csv_path"]
    daily_ticket_rate = pl.read_csv(payload["daily_ticket_rate_csv_path"])
    assert daily_ticket_rate.columns == [
        "trade_day",
        "day_start_ts_ms",
        "primary_trades",
        "addon_trades",
        "combined_trades",
        "primary_entry_order_attempts",
        "addon_entry_order_attempts",
        "combined_entry_order_attempts",
    ]
    assert daily_ticket_rate.row(0, named=True) == {
        "trade_day": 0,
        "day_start_ts_ms": 0,
        "primary_trades": 2,
        "addon_trades": 3,
        "combined_trades": 5,
        "primary_entry_order_attempts": 1,
        "addon_entry_order_attempts": 3,
        "combined_entry_order_attempts": 4,
    }
    assert payload["daily_ticket_contributors_csv_path"]
    daily_ticket_contributors = pl.read_csv(payload["daily_ticket_contributors_csv_path"])
    assert daily_ticket_contributors.columns == [
        "trade_day",
        "day_start_ts_ms",
        "record_type",
        "source",
        "symbol",
        "signal_ts_ms",
        "ts_ms",
        "entry_ts_ms",
        "exit_ts_ms",
        "trade_id",
        "order_link_id",
        "entry_order_prefix",
        "status",
    ]
    assert daily_ticket_contributors.group_by("record_type").len().sort("record_type").to_dicts() == [
        {"record_type": "entry_order_attempt", "len": 4},
        {"record_type": "trade", "len": 5},
    ]
    assert daily_ticket_contributors.filter(
        (pl.col("record_type") == "entry_order_attempt")
        & (pl.col("source") == "addon")
        & (pl.col("symbol") == "AAAUSDT")
        & (pl.col("order_link_id") == "lm-en-ca-AAA-1")
    ).select(["trade_id", "entry_order_prefix", "status"]).to_dicts() == [
        {
            "trade_id": "a-a",
            "entry_order_prefix": "lm-en-ca",
            "status": "submitted_unconfirmed",
        }
    ]
    assert payload["symbol_day_ticket_rate_csv_path"]
    symbol_day_ticket_rate = pl.read_csv(payload["symbol_day_ticket_rate_csv_path"])
    assert symbol_day_ticket_rate.columns == [
        "trade_day",
        "day_start_ts_ms",
        "symbol",
        "primary_trades",
        "addon_trades",
        "combined_trades",
        "primary_entry_order_attempts",
        "addon_entry_order_attempts",
        "combined_entry_order_attempts",
    ]
    assert symbol_day_ticket_rate.filter(pl.col("symbol") == "AAAUSDT").row(0, named=True) == {
        "trade_day": 0,
        "day_start_ts_ms": 0,
        "symbol": "AAAUSDT",
        "primary_trades": 1,
        "addon_trades": 1,
        "combined_trades": 2,
        "primary_entry_order_attempts": 1,
        "addon_entry_order_attempts": 2,
        "combined_entry_order_attempts": 3,
    }
    assert payload["same_symbol_entry_gap_csv_path"]
    same_symbol_entry_gaps = pl.read_csv(payload["same_symbol_entry_gap_csv_path"])
    assert same_symbol_entry_gaps.columns == [
        "source",
        "record_type",
        "symbol",
        "previous_source",
        "current_source",
        "previous_ts_ms",
        "ts_ms",
        "gap_minutes",
        "previous_signal_ts_ms",
        "signal_ts_ms",
        "previous_trade_id",
        "trade_id",
        "previous_order_link_id",
        "order_link_id",
        "previous_status",
        "status",
    ]
    assert same_symbol_entry_gaps.filter(
        (pl.col("source") == "addon")
        & (pl.col("record_type") == "entry_order_attempt")
        & (pl.col("symbol") == "AAAUSDT")
    ).select(["previous_order_link_id", "order_link_id", "gap_minutes"]).to_dicts() == [
        {
            "previous_order_link_id": "lm-en-ca-AAA-1",
            "order_link_id": "lm-en-ca-AAA-2",
            "gap_minutes": 0.0,
        }
    ]
    report_text = Path(payload["report_path"]).read_text(encoding="utf-8")
    assert "continuous_addon_shadow_audit_addon_symbol_concentration.csv" in report_text
    assert "continuous_addon_shadow_audit_historical_addon_symbol_concentration.csv" in report_text
    assert "Primary trade strategy ids: `continuous_fade_v2_paper=2`" in report_text
    assert "Add-on trade strategy ids: `continuous_fade_addon_v2_paper=3`" in report_text
    assert "Primary unexpected strategy rows: `0`" in report_text
    assert "Add-on unexpected strategy rows: `0`" in report_text
    assert "Primary entry order prefixes: `lm-en-c=1`" in report_text
    assert "Add-on entry order prefixes: `lm-en-ca=3`" in report_text
    assert "Primary unexpected entry order prefix rows: `0`" in report_text
    assert "Add-on unexpected entry order prefix rows: `0`" in report_text
    assert "Current open add-on weight: `120.0000`" in report_text
    assert "Current open combined weight: `220.0000`" in report_text
    assert "Max active add-on weight: `230.0000`" in report_text
    assert "Max active combined weight: `430.0000`" in report_text
    assert "Primary weight sources: `entry_price_qty=2`" in report_text
    assert "Add-on weight sources: `entry_price_qty=3`" in report_text
    assert "Unit-weight trade rows: `0`" in report_text
    assert "continuous_addon_shadow_audit_active_exposure.csv" in report_text
    assert "continuous_addon_shadow_audit_active_exposure_contributors.csv" in report_text
    assert "Add-on latest cycle ts_ms: `1700000060000`" in report_text
    assert "Add-on latest cycle age minutes: `1.0000`" in report_text
    assert "Add-on max cycle gap minutes: `1.0000`" in report_text
    assert "Add-on cycle candidate pressure: `14`" in report_text
    assert "Add-on cycle entry candidates recorded: `9`" in report_text
    assert "Add-on cycle entries recorded: `3`" in report_text
    assert "Add-on cycle entry acceptance fraction: `0.2143`" in report_text
    assert "Add-on primary-PnL gate skip fraction: `0.3571`" in report_text
    assert "Add-on same-signal re-entry skips recorded: `3`" in report_text
    assert "Add-on same-signal re-entry skip fraction: `0.2143`" in report_text
    assert "Primary entry order attempts: `1` (`0` repeat rows, max `1` per key)" in report_text
    assert "Primary problem entry order attempts: `0`" in report_text
    assert "Primary entry order statuses: `planned=1`" in report_text
    assert "Primary unmatched entry order attempts: `0` (live `0`, max age `0.0000`, live max age `0.0000`)" in report_text
    assert "Add-on entry order attempts: `3` (`1` repeat rows, max `2` per key)" in report_text
    assert "Add-on problem entry order attempts: `1`" in report_text
    assert "Add-on entry order statuses: `filled=1; planned=1; submitted_unconfirmed=1`" in report_text
    assert "Add-on unmatched entry order attempts: `0` (live `0`, max age `0.0000`, live max age `0.0000`)" in report_text
    assert "Max combined trades/day: `5` (day `0`)" in report_text
    assert "Max primary trades/day: `2`" in report_text
    assert "Max add-on trades/day: `3`" in report_text
    assert "Max combined entry order attempts/day: `4` (day `0`)" in report_text
    assert "Max primary entry order attempts/day: `1`" in report_text
    assert "Max add-on entry order attempts/day: `3`" in report_text
    assert "continuous_addon_shadow_audit_daily_ticket_rate.csv" in report_text
    assert "continuous_addon_shadow_audit_daily_ticket_contributors.csv" in report_text
    assert "Max combined trades/symbol-day: `2` (`AAAUSDT`, day `0`)" in report_text
    assert "Max primary trades/symbol-day: `1`" in report_text
    assert "Max add-on trades/symbol-day: `1`" in report_text
    assert "Max combined entry order attempts/symbol-day: `3` (`AAAUSDT`, day `0`)" in report_text
    assert "Max primary entry order attempts/symbol-day: `1`" in report_text
    assert "Max add-on entry order attempts/symbol-day: `2`" in report_text
    assert "continuous_addon_shadow_audit_symbol_day_ticket_rate.csv" in report_text
    assert "Min add-on same-symbol entry order gap minutes: `0.0000`" in report_text
    assert "continuous_addon_shadow_audit_same_symbol_entry_gaps.csv" in report_text
    assert "Add-on entry-order cooldown simulation: `0` skipped of `3` (`0.0000`)" in report_text
    assert "Add-on cooldown skipped-trade return sum: `0.0000` (`0` observations)" in report_text
    assert "Cooldown simulation CSV: `-`" in report_text
    assert "continuous_addon_shadow_audit_entry_order_attempts.csv" in report_text
    assert "Unmatched entry order attempt CSV: `-`" in report_text
    assert payload["entry_order_attempts_csv_path"]
    entry_order_attempts = pl.read_csv(payload["entry_order_attempts_csv_path"])
    assert entry_order_attempts.columns == [
        "source",
        "symbol",
        "signal_ts_ms",
        "entry_order_attempts_for_key",
        "order_link_id",
        "trade_id",
        "status",
        "ts_ms",
        "submit_mode",
        "error",
    ]
    aaa_attempts = entry_order_attempts.filter(
        (pl.col("source") == "addon") & (pl.col("symbol") == "AAAUSDT")
    ).sort("order_link_id")
    assert aaa_attempts.select(
        ["order_link_id", "status", "entry_order_attempts_for_key"]
    ).to_dicts() == [
        {
            "order_link_id": "lm-en-ca-AAA-1",
            "status": "submitted_unconfirmed",
            "entry_order_attempts_for_key": 2,
        },
        {
            "order_link_id": "lm-en-ca-AAA-2",
            "status": "planned",
            "entry_order_attempts_for_key": 2,
        },
    ]
    assert "Add-on max cycle candidate pressure: `9`" in report_text
    assert "Add-on worst-cycle entry acceptance fraction: `0.2000`" in report_text
    assert "Add-on worst-cycle primary-PnL gate skip fraction: `0.4000`" in report_text
    assert "Add-on worst-cycle same-signal re-entry skip fraction: `0.2222`" in report_text
    assert "continuous_addon_shadow_audit_cycle_pressure.csv" in report_text
    assert payload["cycle_pressure_csv_path"]
    cycle_pressure = pl.read_csv(payload["cycle_pressure_csv_path"])
    assert cycle_pressure.columns == [
        "cycle_id",
        "ts_ms",
        "entry_candidates",
        "entries",
        "candidate_pressure",
        "addon_primary_pnl_gate_skips",
        "skipped_same_signal_reentry",
        "entry_acceptance_fraction",
        "addon_primary_pnl_gate_skip_fraction",
        "same_signal_reentry_skip_fraction",
    ]
    assert cycle_pressure.row(0, named=True) == {
        "cycle_id": "c1",
        "ts_ms": 1_700_000_000_000,
        "entry_candidates": 5,
        "entries": 1,
        "candidate_pressure": 5,
        "addon_primary_pnl_gate_skips": 2,
        "skipped_same_signal_reentry": 1,
        "entry_acceptance_fraction": 0.2,
        "addon_primary_pnl_gate_skip_fraction": 0.4,
        "same_signal_reentry_skip_fraction": 0.2,
    }
    c2 = cycle_pressure.row(1, named=True)
    assert c2["cycle_id"] == "c2"
    assert c2["ts_ms"] == 1_700_000_060_000
    assert c2["entry_candidates"] == 4
    assert c2["entries"] == 2
    assert c2["candidate_pressure"] == 9
    assert c2["addon_primary_pnl_gate_skips"] == 3
    assert c2["skipped_same_signal_reentry"] == 2
    assert c2["entry_acceptance_fraction"] == pytest.approx(2 / 9)
    assert c2["addon_primary_pnl_gate_skip_fraction"] == pytest.approx(3 / 9)
    assert c2["same_signal_reentry_skip_fraction"] == pytest.approx(2 / 9)


def test_continuous_addon_shadow_audit_flags_repeated_signal_keys(tmp_path: Path) -> None:
    primary_root, addon_root, _historical_csv = _write_shadow_fixture(tmp_path)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "a-c-repeat",
                    "symbol": "CCCUSDT",
                    "side": "short",
                    "status": "closed",
                    "signal_ts_ms": 70_000,
                    "entry_ts_ms": 85_000,
                    "exit_ts_ms": 95_000,
                    "entry_price": 10.0,
                    "exit_price": 9.5,
                    "qty": "1",
                },
            ]
        ),
        addon_root,
        "continuous_fade_paper_trades",
        partition_by=(),
    )

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            output_dir=str(tmp_path / "audit"),
        )
    )

    addon = payload["summary"]["addon"]
    assert addon["trades"] == 4
    assert addon["repeated_entry_keys"] == 1
    assert addon["repeated_entry_rows"] == 1
    assert addon["max_entries_per_entry_key"] == 2
    report_text = Path(payload["report_path"]).read_text(encoding="utf-8")
    assert "Add-on repeated entry keys: `1` (`1` repeat rows, max `2` per key)" in report_text


def test_continuous_addon_shadow_audit_repeated_entry_row_gate(tmp_path: Path) -> None:
    primary_root, addon_root, _historical_csv = _write_shadow_fixture(tmp_path)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "a-c-repeat",
                    "symbol": "CCCUSDT",
                    "side": "short",
                    "status": "closed",
                    "signal_ts_ms": 70_000,
                    "entry_ts_ms": 85_000,
                    "exit_ts_ms": 95_000,
                    "entry_price": 10.0,
                    "exit_price": 9.5,
                    "qty": "1",
                },
            ]
        ),
        addon_root,
        "continuous_fade_paper_trades",
        partition_by=(),
    )

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            output_dir=str(tmp_path / "audit"),
            max_addon_repeated_entry_rows=0,
        )
    )

    gate = payload["summary"]["gate"]
    assert gate["passed"] is False
    assert gate["failures"] == ["add-on repeated entry rows 1 > max_addon_repeated_entry_rows 0"]


def test_continuous_addon_shadow_audit_repeated_entry_order_row_gate(tmp_path: Path) -> None:
    primary_root, addon_root, _historical_csv = _write_shadow_fixture(tmp_path)

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            output_dir=str(tmp_path / "audit"),
            max_primary_repeated_entry_order_rows=0,
            max_addon_repeated_entry_order_rows=0,
        )
    )

    gate = payload["summary"]["gate"]
    assert gate["passed"] is False
    assert gate["failures"] == ["add-on repeated entry order rows 1 > max_addon_repeated_entry_order_rows 0"]


def test_continuous_addon_shadow_audit_problem_entry_order_attempt_gate(tmp_path: Path) -> None:
    primary_root, addon_root, _historical_csv = _write_shadow_fixture(tmp_path)

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            output_dir=str(tmp_path / "audit"),
            max_primary_problem_entry_order_attempts=0,
            max_addon_problem_entry_order_attempts=0,
        )
    )

    gate = payload["summary"]["gate"]
    assert gate["passed"] is False
    assert gate["failures"] == ["add-on problem entry order attempts 1 > max_addon_problem_entry_order_attempts 0"]


def test_continuous_addon_shadow_audit_unmatched_entry_order_attempt_gate(tmp_path: Path) -> None:
    primary_root, addon_root, _historical_csv = _write_shadow_fixture(tmp_path)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "lm-en-ca-ORPHAN-1",
                    "trade_id": "a-orphan",
                    "symbol": "ORPHANUSDT",
                    "strategy_id": "continuous_fade_addon_v2_paper",
                    "reduce_only": False,
                    "status": "filled",
                    "signal_ts_ms": 130_000,
                    "ts_ms": 131_000,
                },
            ]
        ),
        addon_root,
        "continuous_fade_paper_orders",
        partition_by=(),
    )

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            output_dir=str(tmp_path / "audit"),
            max_primary_unmatched_entry_order_attempts=0,
            max_addon_unmatched_entry_order_attempts=0,
            max_primary_unmatched_live_entry_order_attempts=0,
            max_addon_unmatched_live_entry_order_attempts=0,
            max_primary_unmatched_entry_order_age_minutes=0.0,
            max_addon_unmatched_entry_order_age_minutes=1.5,
            max_primary_unmatched_live_entry_order_age_minutes=0.0,
            max_addon_unmatched_live_entry_order_age_minutes=1.5,
            audit_now_ms=251_000,
        )
    )

    summary = payload["summary"]
    assert summary["primary_order_trade_reconciliation"] == {
        "entry_order_attempts_without_trade_key": 0,
        "live_entry_order_attempts_without_trade_key": 0,
        "max_unmatched_entry_order_age_minutes": 0.0,
        "max_unmatched_live_entry_order_age_minutes": 0.0,
    }
    assert summary["addon_order_trade_reconciliation"] == {
        "entry_order_attempts_without_trade_key": 1,
        "live_entry_order_attempts_without_trade_key": 1,
        "max_unmatched_entry_order_age_minutes": 2.0,
        "max_unmatched_live_entry_order_age_minutes": 2.0,
    }
    gate = summary["gate"]
    assert gate["passed"] is False
    assert gate["failures"] == [
        "add-on unmatched entry order attempts 1 > max_addon_unmatched_entry_order_attempts 0",
        "add-on unmatched live entry order attempts 1 > max_addon_unmatched_live_entry_order_attempts 0",
        "add-on unmatched entry order age minutes 2.0000 > max_addon_unmatched_entry_order_age_minutes 1.5000",
        "add-on unmatched live entry order age minutes 2.0000 > max_addon_unmatched_live_entry_order_age_minutes 1.5000",
    ]
    assert payload["unmatched_entry_order_attempts_csv_path"]
    unmatched = pl.read_csv(payload["unmatched_entry_order_attempts_csv_path"])
    assert unmatched.row(0, named=True) == {
        "source": "addon",
        "symbol": "ORPHANUSDT",
        "signal_ts_ms": 130_000,
        "order_link_id": "lm-en-ca-ORPHAN-1",
        "trade_id": "a-orphan",
        "status": "filled",
        "ts_ms": 131_000,
        "submit_mode": "",
        "entry_order_prefix": "lm-en-ca",
        "live_order_status": True,
        "age_minutes": 2.0,
    }


def test_continuous_addon_shadow_audit_thresholds_fail_loudly(tmp_path: Path) -> None:
    primary_root, addon_root, historical_csv = _write_shadow_fixture(tmp_path)

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            historical_blended_trades_csv=str(historical_csv),
            output_dir=str(tmp_path / "audit"),
            min_addon_trades=4,
            min_matched_addon_keys=2,
            max_missing_addon_keys=0,
            max_extra_addon_keys=1,
            max_missing_addon_key_fraction=0.25,
        )
    )

    gate = payload["summary"]["gate"]
    assert gate["passed"] is False
    assert len(gate["failures"]) == 5
    assert any("addon trades 3 < min_addon_trades 4" in failure for failure in gate["failures"])
    report_text = Path(payload["report_path"]).read_text(encoding="utf-8")
    assert "## Gate Verdict" in report_text
    assert "Passed: `False`" in report_text


def test_continuous_addon_shadow_audit_anatomy_thresholds_fail_loudly(tmp_path: Path) -> None:
    primary_root, addon_root, historical_csv = _write_shadow_fixture(tmp_path)

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            historical_blended_trades_csv=str(historical_csv),
            output_dir=str(tmp_path / "audit"),
            max_addon_to_primary_ratio=1.0,
            min_active_same_symbol_overlap_fraction=0.80,
            max_exact_same_entry_fraction=0.20,
        )
    )

    gate = payload["summary"]["gate"]
    assert gate["passed"] is False
    assert len(gate["failures"]) == 3
    assert any("add-on / primary trade ratio 1.5000 > max_addon_to_primary_ratio 1.0000" in failure for failure in gate["failures"])
    assert any(
        "active same-symbol overlap fraction 0.6667 < min_active_same_symbol_overlap_fraction 0.8000" in failure
        for failure in gate["failures"]
    )
    assert any("exact same-entry fraction 0.3333 > max_exact_same_entry_fraction 0.2000" in failure for failure in gate["failures"])


def test_continuous_addon_shadow_audit_historical_anatomy_drift_gate(tmp_path: Path) -> None:
    primary_root, addon_root, historical_csv = _write_shadow_fixture(tmp_path)

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            historical_blended_trades_csv=str(historical_csv),
            output_dir=str(tmp_path / "audit"),
            max_historical_anatomy_drift=0.20,
        )
    )

    gate = payload["summary"]["gate"]
    assert gate["passed"] is False
    assert len(gate["failures"]) == 2
    assert any(
        "historical anatomy drift addon_to_primary_ratio 0.5000 > max_historical_anatomy_drift 0.2000" in failure
        for failure in gate["failures"]
    )
    assert any(
        "historical anatomy drift exact_same_entry_fraction 0.3333 > max_historical_anatomy_drift 0.2000" in failure
        for failure in gate["failures"]
    )


def test_continuous_addon_shadow_audit_concentration_gates(tmp_path: Path) -> None:
    primary_root, addon_root, historical_csv = _write_shadow_fixture(tmp_path)

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            historical_blended_trades_csv=str(historical_csv),
            output_dir=str(tmp_path / "audit"),
            max_addon_top1_weight_share=0.40,
            max_historical_concentration_drift=0.20,
        )
    )

    gate = payload["summary"]["gate"]
    assert gate["passed"] is False
    assert len(gate["failures"]) == 2
    assert any(
        "add-on top1 symbol weight share 0.5000 > max_addon_top1_weight_share 0.4000" in failure
        for failure in gate["failures"]
    )
    assert any(
        "historical concentration drift top1_weight_share 0.4990 > max_historical_concentration_drift 0.2000" in failure
        for failure in gate["failures"]
    )


def test_continuous_addon_shadow_audit_active_exposure_gates(tmp_path: Path) -> None:
    primary_root, addon_root, historical_csv = _write_shadow_fixture(tmp_path)

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            historical_blended_trades_csv=str(historical_csv),
            output_dir=str(tmp_path / "audit"),
            max_active_addon_weight=200.0,
            max_active_combined_weight=400.0,
        )
    )

    gate = payload["summary"]["gate"]
    assert gate["passed"] is False
    assert gate["failures"] == [
        "max active add-on weight 230.0000 > max_active_addon_weight 200.0000",
        "max active combined weight 430.0000 > max_active_combined_weight 400.0000",
    ]


def test_continuous_addon_shadow_audit_daily_ticket_rate_gates(tmp_path: Path) -> None:
    primary_root, addon_root, _historical_csv = _write_shadow_fixture(tmp_path)

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            output_dir=str(tmp_path / "audit"),
            max_primary_trades_per_day=1,
            max_addon_trades_per_day=2,
            max_combined_trades_per_day=4,
            max_primary_entry_order_attempts_per_day=0,
            max_addon_entry_order_attempts_per_day=2,
            max_combined_entry_order_attempts_per_day=3,
        )
    )

    gate = payload["summary"]["gate"]
    assert gate["passed"] is False
    assert gate["failures"] == [
        "max primary trades per day 2 > max_primary_trades_per_day 1",
        "max add-on trades per day 3 > max_addon_trades_per_day 2",
        "max combined trades per day 5 > max_combined_trades_per_day 4",
        "max primary entry order attempts per day 1 > max_primary_entry_order_attempts_per_day 0",
        "max add-on entry order attempts per day 3 > max_addon_entry_order_attempts_per_day 2",
        "max combined entry order attempts per day 4 > max_combined_entry_order_attempts_per_day 3",
    ]


def test_continuous_addon_shadow_audit_symbol_day_ticket_rate_gates(tmp_path: Path) -> None:
    primary_root, addon_root, _historical_csv = _write_shadow_fixture(tmp_path)

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            output_dir=str(tmp_path / "audit"),
            max_primary_trades_per_symbol_day=0,
            max_addon_trades_per_symbol_day=0,
            max_combined_trades_per_symbol_day=1,
            max_primary_entry_order_attempts_per_symbol_day=0,
            max_addon_entry_order_attempts_per_symbol_day=1,
            max_combined_entry_order_attempts_per_symbol_day=2,
        )
    )

    gate = payload["summary"]["gate"]
    assert gate["passed"] is False
    assert gate["failures"] == [
        "max primary trades per symbol-day 1 > max_primary_trades_per_symbol_day 0",
        "max add-on trades per symbol-day 1 > max_addon_trades_per_symbol_day 0",
        "max combined trades per symbol-day 2 > max_combined_trades_per_symbol_day 1",
        "max primary entry order attempts per symbol-day 1 > max_primary_entry_order_attempts_per_symbol_day 0",
        "max add-on entry order attempts per symbol-day 2 > max_addon_entry_order_attempts_per_symbol_day 1",
        "max combined entry order attempts per symbol-day 3 > max_combined_entry_order_attempts_per_symbol_day 2",
    ]


def test_continuous_addon_shadow_audit_same_symbol_gap_gates(tmp_path: Path) -> None:
    primary_root, addon_root, _historical_csv = _write_shadow_fixture(tmp_path)

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            output_dir=str(tmp_path / "audit"),
            min_addon_same_symbol_entry_order_gap_minutes=0.1,
            min_combined_same_symbol_trade_gap_minutes=0.1,
            min_combined_same_symbol_entry_order_gap_minutes=0.1,
        )
    )

    gate = payload["summary"]["gate"]
    assert gate["passed"] is False
    assert gate["failures"] == [
        "combined same-symbol trade gap minutes 0.0000 < min_combined_same_symbol_trade_gap_minutes 0.1000",
        "add-on same-symbol entry order gap minutes 0.0000 < min_addon_same_symbol_entry_order_gap_minutes 0.1000",
        "combined same-symbol entry order gap minutes 0.0000 < min_combined_same_symbol_entry_order_gap_minutes 0.1000",
    ]


def test_continuous_addon_shadow_audit_cooldown_simulation(tmp_path: Path) -> None:
    primary_root, addon_root, _historical_csv = _write_shadow_fixture(tmp_path)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "a-a-second",
                    "symbol": "AAAUSDT",
                    "strategy_id": "continuous_fade_addon_v2_paper",
                    "side": "short",
                    "status": "open",
                    "signal_ts_ms": 54_000,
                    "entry_ts_ms": 55_000,
                    "entry_price": 121.0,
                    "qty": "1",
                    "net_return": -0.02,
                },
            ]
        ),
        addon_root,
        "continuous_fade_paper_trades",
        partition_by=(),
    )
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "lm-en-ca-BBB-2",
                    "trade_id": "a-b-second",
                    "symbol": "BBBUSDT",
                    "strategy_id": "continuous_fade_addon_v2_paper",
                    "reduce_only": False,
                    "status": "planned",
                    "signal_ts_ms": 33_000,
                    "ts_ms": 33_000,
                },
            ]
        ),
        addon_root,
        "continuous_fade_paper_orders",
        partition_by=(),
    )

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            output_dir=str(tmp_path / "audit"),
            simulate_addon_same_symbol_trade_cooldown_minutes=0.1,
            simulate_addon_same_symbol_entry_order_cooldown_minutes=0.1,
        )
    )

    summary = payload["summary"]["addon_cooldown_simulation"]
    assert summary == {
        "trade_cooldown_minutes": 0.1,
        "entry_order_cooldown_minutes": 0.1,
        "trade_candidates": 4,
        "entry_order_candidates": 4,
        "skipped_trades": 1,
        "skipped_entry_order_attempts": 2,
        "accepted_trades": 3,
        "accepted_entry_order_attempts": 2,
        "trade_suppression_fraction": 0.25,
        "entry_order_suppression_fraction": 0.5,
        "skipped_symbols": 2,
        "max_skips_per_symbol": 2,
        "skipped_trade_return_observations": 1,
        "skipped_trade_return_sum": -0.02,
        "skipped_trade_return_mean": -0.02,
        "skipped_trade_positive_return_fraction": 0.0,
    }
    assert payload["cooldown_simulation_csv_path"]
    cooldown_rows = pl.read_csv(payload["cooldown_simulation_csv_path"])
    assert cooldown_rows.columns == [
        "source",
        "record_type",
        "symbol",
        "cooldown_minutes",
        "gap_minutes",
        "previous_ts_ms",
        "ts_ms",
        "previous_signal_ts_ms",
        "signal_ts_ms",
        "previous_trade_id",
        "trade_id",
        "previous_order_link_id",
        "order_link_id",
        "previous_status",
        "status",
        "previous_return_value",
        "return_value",
    ]
    assert cooldown_rows.filter(pl.col("record_type") == "trade").row(0, named=True) == {
        "source": "addon",
        "record_type": "trade",
        "symbol": "AAAUSDT",
        "cooldown_minutes": 0.1,
        "gap_minutes": pytest.approx(5_000 / 60_000),
        "previous_ts_ms": 50_000,
        "ts_ms": 55_000,
        "previous_signal_ts_ms": 10_000,
        "signal_ts_ms": 54_000,
        "previous_trade_id": "a-a",
        "trade_id": "a-a-second",
        "previous_order_link_id": "",
        "order_link_id": "",
        "previous_status": "open",
        "status": "open",
        "previous_return_value": None,
        "return_value": -0.02,
    }
    assert cooldown_rows.filter(pl.col("record_type") == "entry_order_attempt").select(
        ["symbol", "previous_order_link_id", "order_link_id", "gap_minutes"]
    ).to_dicts() == [
        {
            "symbol": "AAAUSDT",
            "previous_order_link_id": "lm-en-ca-AAA-1",
            "order_link_id": "lm-en-ca-AAA-2",
            "gap_minutes": 0.0,
        },
        {
            "symbol": "BBBUSDT",
            "previous_order_link_id": "lm-en-ca-BBB-1",
            "order_link_id": "lm-en-ca-BBB-2",
            "gap_minutes": 0.05,
        },
    ]


def test_continuous_addon_shadow_audit_unit_weight_gate(tmp_path: Path) -> None:
    primary_root, addon_root, _historical_csv = _write_shadow_fixture(tmp_path)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "a-unit",
                    "symbol": "UNITUSDT",
                    "side": "short",
                    "status": "closed",
                    "signal_ts_ms": 100_000,
                    "entry_ts_ms": 101_000,
                    "exit_ts_ms": 102_000,
                },
            ]
        ),
        addon_root,
        "continuous_fade_paper_trades",
        partition_by=(),
    )

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            output_dir=str(tmp_path / "audit"),
            max_unit_weight_rows=0,
        )
    )

    summary = payload["summary"]
    assert summary["weight_sources"]["addon"] == {"entry_price_qty": 3, "unit": 1}
    assert summary["weight_sources"]["combined_unit_weight_rows"] == 1
    gate = summary["gate"]
    assert gate["passed"] is False
    assert gate["failures"] == ["unit-weight trade rows 1 > max_unit_weight_rows 0"]
    contributors = pl.read_csv(payload["active_exposure_contributors_csv_path"])
    assert contributors.filter(pl.col("trade_id") == "a-unit").select(
        ["source", "symbol", "weight", "weight_source"]
    ).to_dicts() == [
        {"source": "addon", "symbol": "UNITUSDT", "weight": 1.0, "weight_source": "unit"},
    ]


def test_continuous_addon_shadow_audit_strategy_identity_gate(tmp_path: Path) -> None:
    primary_root, addon_root, _historical_csv = _write_shadow_fixture(tmp_path)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "a-wrong-strategy",
                    "symbol": "WRONGUSDT",
                    "strategy_id": "continuous_fade_v2_paper",
                    "side": "short",
                    "status": "closed",
                    "signal_ts_ms": 110_000,
                    "entry_ts_ms": 111_000,
                    "exit_ts_ms": 112_000,
                    "entry_price": 10.0,
                    "qty": "1",
                },
            ]
        ),
        addon_root,
        "continuous_fade_paper_trades",
        partition_by=(),
    )

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            output_dir=str(tmp_path / "audit"),
            expected_primary_strategy_id="continuous_fade_v2_paper",
            expected_addon_strategy_id="continuous_fade_addon_v2_paper",
            max_primary_unexpected_strategy_rows=0,
            max_addon_unexpected_strategy_rows=0,
        )
    )

    summary = payload["summary"]
    assert summary["primary_strategy"]["unexpected_strategy_rows"] == 0
    assert summary["addon_strategy"]["strategy_counts"] == {
        "continuous_fade_addon_v2_paper": 3,
        "continuous_fade_v2_paper": 1,
    }
    assert summary["addon_strategy"]["unexpected_strategy_rows"] == 1
    gate = summary["gate"]
    assert gate["passed"] is False
    assert gate["failures"] == [
        "add-on unexpected strategy rows 1 > max_addon_unexpected_strategy_rows 0",
    ]


def test_continuous_addon_shadow_audit_order_prefix_identity_gate(tmp_path: Path) -> None:
    primary_root, addon_root, _historical_csv = _write_shadow_fixture(tmp_path)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "lm-en-c-WRONG-1",
                    "trade_id": "a-wrong-prefix",
                    "symbol": "WRONGUSDT",
                    "strategy_id": "continuous_fade_addon_v2_paper",
                    "reduce_only": False,
                    "status": "planned",
                    "signal_ts_ms": 120_000,
                    "ts_ms": 121_000,
                },
            ]
        ),
        addon_root,
        "continuous_fade_paper_orders",
        partition_by=(),
    )

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            output_dir=str(tmp_path / "audit"),
            expected_primary_entry_order_prefix="lm-en-c",
            expected_addon_entry_order_prefix="lm-en-ca",
            max_primary_unexpected_entry_order_prefix_rows=0,
            max_addon_unexpected_entry_order_prefix_rows=0,
        )
    )

    summary = payload["summary"]
    assert summary["primary_order_prefix"]["unexpected_entry_order_prefix_rows"] == 0
    assert summary["addon_order_prefix"]["entry_order_prefix_counts"] == {
        "lm-en-c": 1,
        "lm-en-ca": 3,
    }
    assert summary["addon_order_prefix"]["unexpected_entry_order_prefix_rows"] == 1
    gate = summary["gate"]
    assert gate["passed"] is False
    assert gate["failures"] == [
        "add-on unexpected entry order prefix rows 1 > max_addon_unexpected_entry_order_prefix_rows 0",
    ]


def test_continuous_addon_shadow_audit_cycle_pressure_gates(tmp_path: Path) -> None:
    primary_root, addon_root, historical_csv = _write_shadow_fixture(tmp_path)

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            historical_blended_trades_csv=str(historical_csv),
            output_dir=str(tmp_path / "audit"),
            min_cycle_entry_acceptance_fraction=0.50,
            max_cycle_same_signal_reentry_skip_fraction=0.10,
            max_cycle_addon_primary_pnl_gate_skip_fraction=0.20,
        )
    )

    gate = payload["summary"]["gate"]
    assert gate["passed"] is False
    assert len(gate["failures"]) == 3
    assert any(
        "cycle entry acceptance fraction 0.2143 < min_cycle_entry_acceptance_fraction 0.5000" in failure
        for failure in gate["failures"]
    )
    assert any(
        "cycle same-signal re-entry skip fraction 0.2143 > max_cycle_same_signal_reentry_skip_fraction 0.1000"
        in failure
        for failure in gate["failures"]
    )
    assert any(
        "cycle add-on primary-PnL gate skip fraction 0.3571 > "
        "max_cycle_addon_primary_pnl_gate_skip_fraction 0.2000" in failure
        for failure in gate["failures"]
    )


def test_continuous_addon_shadow_audit_worst_cycle_burst_gates(tmp_path: Path) -> None:
    primary_root, addon_root, historical_csv = _write_shadow_fixture(tmp_path)

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            historical_blended_trades_csv=str(historical_csv),
            output_dir=str(tmp_path / "audit"),
            max_cycle_candidate_pressure=8,
            min_worst_cycle_entry_acceptance_fraction=0.21,
            max_worst_cycle_same_signal_reentry_skip_fraction=0.21,
            max_worst_cycle_addon_primary_pnl_gate_skip_fraction=0.35,
        )
    )

    gate = payload["summary"]["gate"]
    assert gate["passed"] is False
    assert len(gate["failures"]) == 4
    assert any("max cycle candidate pressure 9 > max_cycle_candidate_pressure 8" in failure for failure in gate["failures"])
    assert any(
        "worst-cycle entry acceptance fraction 0.2000 < min_worst_cycle_entry_acceptance_fraction 0.2100"
        in failure
        for failure in gate["failures"]
    )
    assert any(
        "worst-cycle same-signal re-entry skip fraction 0.2222 > "
        "max_worst_cycle_same_signal_reentry_skip_fraction 0.2100" in failure
        for failure in gate["failures"]
    )
    assert any(
        "worst-cycle add-on primary-PnL gate skip fraction 0.4000 > "
        "max_worst_cycle_addon_primary_pnl_gate_skip_fraction 0.3500" in failure
        for failure in gate["failures"]
    )


def test_continuous_addon_shadow_audit_cycle_liveness_gates(tmp_path: Path) -> None:
    primary_root, addon_root, historical_csv = _write_shadow_fixture(tmp_path)

    payload = run_continuous_addon_shadow_audit(
        ContinuousAddonShadowAuditConfig(
            primary_data_root=str(primary_root),
            addon_data_root=str(addon_root),
            historical_blended_trades_csv=str(historical_csv),
            output_dir=str(tmp_path / "audit"),
            min_addon_cycles=3,
            max_latest_cycle_age_minutes=1.5,
            max_cycle_gap_minutes=0.5,
            audit_now_ms=1_700_000_180_000,
        )
    )

    cycles = payload["summary"]["addon_cycles"]
    assert cycles["cycles"] == 2
    assert cycles["latest_cycle_ts_ms"] == 1_700_000_060_000
    assert cycles["latest_cycle_age_minutes"] == pytest.approx(2.0)
    gate = payload["summary"]["gate"]
    assert gate["passed"] is False
    assert gate["failures"] == [
        "add-on cycles 2 < min_addon_cycles 3",
        "latest add-on cycle age minutes 2.0000 > max_latest_cycle_age_minutes 1.5000",
        "max add-on cycle gap minutes 1.0000 > max_cycle_gap_minutes 0.5000",
    ]


def test_continuous_addon_shadow_audit_cli_can_fail_on_threshold_breach(tmp_path: Path) -> None:
    primary_root, addon_root, historical_csv = _write_shadow_fixture(tmp_path)

    code = cli_main(
        [
            "continuous-addon-shadow-audit",
            "--primary-data-root",
            str(primary_root),
            "--addon-data-root",
            str(addon_root),
            "--historical-blended-trades-csv",
            str(historical_csv),
            "--max-missing-addon-keys",
            "0",
            "--fail-on-threshold-breach",
        ]
    )

    assert code == 1


def test_continuous_addon_shadow_audit_cli_parser() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "continuous-addon-shadow-audit",
            "--primary-data-root",
            "primary",
            "--addon-data-root",
            "addon",
            "--historical-blended-trades-csv",
            "blend.csv",
            "--primary-orders-dataset",
            "primary_orders",
            "--addon-orders-dataset",
            "addon_orders",
            "--expected-primary-strategy-id",
            "continuous_fade_v2_paper",
            "--expected-addon-strategy-id",
            "continuous_fade_addon_v2_paper",
            "--expected-primary-entry-order-prefix",
            "lm-en-c",
            "--expected-addon-entry-order-prefix",
            "lm-en-ca",
            "--min-addon-trades",
            "10",
            "--max-missing-addon-key-fraction",
            "0.05",
            "--min-addon-to-primary-ratio",
            "0.10",
            "--max-active-same-symbol-overlap-fraction",
            "1.0",
            "--max-historical-anatomy-drift",
            "0.25",
            "--max-addon-top1-weight-share",
            "0.50",
            "--max-historical-concentration-drift",
            "0.15",
            "--max-active-addon-weight",
            "0.12",
            "--max-active-combined-weight",
            "0.20",
            "--max-unit-weight-rows",
            "0",
            "--max-primary-trades-per-day",
            "2",
            "--max-addon-trades-per-day",
            "3",
            "--max-combined-trades-per-day",
            "5",
            "--max-primary-entry-order-attempts-per-day",
            "1",
            "--max-addon-entry-order-attempts-per-day",
            "3",
            "--max-combined-entry-order-attempts-per-day",
            "4",
            "--max-primary-trades-per-symbol-day",
            "1",
            "--max-addon-trades-per-symbol-day",
            "1",
            "--max-combined-trades-per-symbol-day",
            "2",
            "--max-primary-entry-order-attempts-per-symbol-day",
            "1",
            "--max-addon-entry-order-attempts-per-symbol-day",
            "2",
            "--max-combined-entry-order-attempts-per-symbol-day",
            "3",
            "--min-primary-same-symbol-trade-gap-minutes",
            "4.5",
            "--min-addon-same-symbol-trade-gap-minutes",
            "5.5",
            "--min-combined-same-symbol-trade-gap-minutes",
            "6.5",
            "--min-primary-same-symbol-entry-order-gap-minutes",
            "7.5",
            "--min-addon-same-symbol-entry-order-gap-minutes",
            "8.5",
            "--min-combined-same-symbol-entry-order-gap-minutes",
            "9.5",
            "--simulate-addon-same-symbol-trade-cooldown-minutes",
            "10.5",
            "--simulate-addon-same-symbol-entry-order-cooldown-minutes",
            "11.5",
            "--max-primary-unexpected-strategy-rows",
            "0",
            "--max-addon-unexpected-strategy-rows",
            "0",
            "--max-primary-unexpected-entry-order-prefix-rows",
            "0",
            "--max-addon-unexpected-entry-order-prefix-rows",
            "0",
            "--max-primary-repeated-entry-rows",
            "0",
            "--max-addon-repeated-entry-rows",
            "0",
            "--max-primary-repeated-entry-order-rows",
            "0",
            "--max-addon-repeated-entry-order-rows",
            "0",
            "--max-primary-problem-entry-order-attempts",
            "0",
            "--max-addon-problem-entry-order-attempts",
            "0",
            "--max-primary-unmatched-entry-order-attempts",
            "0",
            "--max-addon-unmatched-entry-order-attempts",
            "0",
            "--max-primary-unmatched-live-entry-order-attempts",
            "0",
            "--max-addon-unmatched-live-entry-order-attempts",
            "0",
            "--max-primary-unmatched-entry-order-age-minutes",
            "2.5",
            "--max-addon-unmatched-entry-order-age-minutes",
            "2.5",
            "--max-primary-unmatched-live-entry-order-age-minutes",
            "1.5",
            "--max-addon-unmatched-live-entry-order-age-minutes",
            "1.5",
            "--min-cycle-entry-acceptance-fraction",
            "0.25",
            "--max-cycle-same-signal-reentry-skip-fraction",
            "0.20",
            "--max-cycle-addon-primary-pnl-gate-skip-fraction",
            "0.30",
            "--max-cycle-candidate-pressure",
            "12",
            "--min-worst-cycle-entry-acceptance-fraction",
            "0.15",
            "--max-worst-cycle-same-signal-reentry-skip-fraction",
            "0.35",
            "--max-worst-cycle-addon-primary-pnl-gate-skip-fraction",
            "0.45",
            "--min-addon-cycles",
            "24",
            "--max-latest-cycle-age-minutes",
            "15",
            "--max-cycle-gap-minutes",
            "3",
            "--audit-now-ms",
            "1700000120000",
            "--fail-on-threshold-breach",
        ]
    )
    assert args.command == "continuous-addon-shadow-audit"
    assert args.primary_data_root == "primary"
    assert args.addon_data_root == "addon"
    assert args.primary_orders_dataset == "primary_orders"
    assert args.addon_orders_dataset == "addon_orders"
    assert args.expected_primary_strategy_id == "continuous_fade_v2_paper"
    assert args.expected_addon_strategy_id == "continuous_fade_addon_v2_paper"
    assert args.expected_primary_entry_order_prefix == "lm-en-c"
    assert args.expected_addon_entry_order_prefix == "lm-en-ca"
    assert args.min_addon_trades == 10
    assert args.max_missing_addon_key_fraction == 0.05
    assert args.min_addon_to_primary_ratio == 0.10
    assert args.max_active_same_symbol_overlap_fraction == 1.0
    assert args.max_historical_anatomy_drift == 0.25
    assert args.max_addon_top1_weight_share == 0.50
    assert args.max_historical_concentration_drift == 0.15
    assert args.max_active_addon_weight == 0.12
    assert args.max_active_combined_weight == 0.20
    assert args.max_unit_weight_rows == 0
    assert args.max_primary_trades_per_day == 2
    assert args.max_addon_trades_per_day == 3
    assert args.max_combined_trades_per_day == 5
    assert args.max_primary_entry_order_attempts_per_day == 1
    assert args.max_addon_entry_order_attempts_per_day == 3
    assert args.max_combined_entry_order_attempts_per_day == 4
    assert args.max_primary_trades_per_symbol_day == 1
    assert args.max_addon_trades_per_symbol_day == 1
    assert args.max_combined_trades_per_symbol_day == 2
    assert args.max_primary_entry_order_attempts_per_symbol_day == 1
    assert args.max_addon_entry_order_attempts_per_symbol_day == 2
    assert args.max_combined_entry_order_attempts_per_symbol_day == 3
    assert args.min_primary_same_symbol_trade_gap_minutes == 4.5
    assert args.min_addon_same_symbol_trade_gap_minutes == 5.5
    assert args.min_combined_same_symbol_trade_gap_minutes == 6.5
    assert args.min_primary_same_symbol_entry_order_gap_minutes == 7.5
    assert args.min_addon_same_symbol_entry_order_gap_minutes == 8.5
    assert args.min_combined_same_symbol_entry_order_gap_minutes == 9.5
    assert args.simulate_addon_same_symbol_trade_cooldown_minutes == 10.5
    assert args.simulate_addon_same_symbol_entry_order_cooldown_minutes == 11.5
    assert args.max_primary_unexpected_strategy_rows == 0
    assert args.max_addon_unexpected_strategy_rows == 0
    assert args.max_primary_unexpected_entry_order_prefix_rows == 0
    assert args.max_addon_unexpected_entry_order_prefix_rows == 0
    assert args.max_primary_repeated_entry_rows == 0
    assert args.max_addon_repeated_entry_rows == 0
    assert args.max_primary_repeated_entry_order_rows == 0
    assert args.max_addon_repeated_entry_order_rows == 0
    assert args.max_primary_problem_entry_order_attempts == 0
    assert args.max_addon_problem_entry_order_attempts == 0
    assert args.max_primary_unmatched_entry_order_attempts == 0
    assert args.max_addon_unmatched_entry_order_attempts == 0
    assert args.max_primary_unmatched_live_entry_order_attempts == 0
    assert args.max_addon_unmatched_live_entry_order_attempts == 0
    assert args.max_primary_unmatched_entry_order_age_minutes == 2.5
    assert args.max_addon_unmatched_entry_order_age_minutes == 2.5
    assert args.max_primary_unmatched_live_entry_order_age_minutes == 1.5
    assert args.max_addon_unmatched_live_entry_order_age_minutes == 1.5
    assert args.min_cycle_entry_acceptance_fraction == 0.25
    assert args.max_cycle_same_signal_reentry_skip_fraction == 0.20
    assert args.max_cycle_addon_primary_pnl_gate_skip_fraction == 0.30
    assert args.max_cycle_candidate_pressure == 12
    assert args.min_worst_cycle_entry_acceptance_fraction == 0.15
    assert args.max_worst_cycle_same_signal_reentry_skip_fraction == 0.35
    assert args.max_worst_cycle_addon_primary_pnl_gate_skip_fraction == 0.45
    assert args.min_addon_cycles == 24
    assert args.max_latest_cycle_age_minutes == 15
    assert args.max_cycle_gap_minutes == 3
    assert args.audit_now_ms == 1_700_000_120_000
    assert args.fail_on_threshold_breach is True


# audit2c: cooldown skipped-trade returns must be cost-corrected.
#
# Pins the fix in continuous_addon_shadow: the cooldown simulation feeds
# skipped_trade_return_{sum,mean,positive_fraction} from the stored `net_return`,
# but on the LIVE ledger that field is GROSS-of-cost (fees live in separate
# entry_fee_usdt/exit_fee_usdt, funding uncosted -- see continuous_demo DEPLOY
# NOTE ~L1398). Before the fix the aggregation overstated saved/forfeited PnL by
# round-trip fees and could be wrong-signed for marginal trades. The fix mirrors
# the breaker net_of_fees adjustment.


def test_live_gross_row_is_fee_reduced() -> None:
    # Gross-of-cost live row: net_return is gtr*notional_weight, fees stored apart.
    # fee fraction = (3 + 3) / 10_000 = 6e-4, so net = 0.01 - 6e-4 = 0.0094.
    row = {
        "net_return": 0.01,
        "entry_fee_usdt": 3.0,
        "exit_fee_usdt": 3.0,
        "equity_usdt": 10_000.0,
    }
    corrected = _cost_corrected_return(row)
    assert corrected is not None
    assert corrected == 0.01 - (3.0 + 3.0) / 10_000.0
    # Strictly below the gross value it would have reported before the fix.
    assert corrected < 0.01


def test_marginal_trade_flips_sign_after_fees() -> None:
    # A marginally-positive GROSS trade whose fees exceed the edge is net-negative.
    # gross +5e-5, fee fraction (4+4)/10_000 = 8e-4 -> net = -7.5e-4 < 0.
    row = {
        "net_return": 5e-5,
        "entry_fee_usdt": 4.0,
        "exit_fee_usdt": 4.0,
        "equity_usdt": 10_000.0,
    }
    corrected = _cost_corrected_return(row)
    assert corrected is not None
    assert corrected < 0.0  # wrong-signed under the old gross extraction


def test_already_net_row_is_unchanged() -> None:
    # Backtest-style row already NET (net = gross + cost + funding) carries no
    # entry_fee_usdt/exit_fee_usdt -> no fee subtracted, no double-subtraction.
    row = {"net_return": -0.02}
    assert _cost_corrected_return(row) == -0.02


def test_funding_return_folded_with_additive_sign() -> None:
    # If a true funding_return field is present it nets in additively (net = gross + funding).
    row = {
        "net_return": 0.01,
        "entry_fee_usdt": 2.0,
        "exit_fee_usdt": 2.0,
        "equity_usdt": 10_000.0,
        "funding_return": -0.0005,
    }
    expected = 0.01 - (2.0 + 2.0) / 10_000.0 + (-0.0005)
    assert _cost_corrected_return(row) == expected


def test_zero_equity_skips_fee_subtraction() -> None:
    # Adopted/recovered rows with no positive equity_usdt cannot form a fee fraction;
    # the gross figure passes through (matches the breaker's null-equity guard).
    row = {
        "net_return": 0.01,
        "entry_fee_usdt": 3.0,
        "exit_fee_usdt": 3.0,
        "equity_usdt": 0.0,
    }
    assert _cost_corrected_return(row) == 0.01


def _cooldown_skipped_trade(*, net_return: float, **extra: object) -> list[dict[str, object]]:
    """Two same-symbol addon trades inside the cooldown window: the 2nd is skipped."""
    base = {
        "symbol": "AAAUSDT",
        "side": "short",
        "status": "open",
    }
    first = {
        **base,
        "trade_id": "a-a-first",
        "signal_ts_ms": 50_000,
        "entry_ts_ms": 51_000,
    }
    second = {
        **base,
        "trade_id": "a-a-second",
        "signal_ts_ms": 54_000,
        "entry_ts_ms": 55_000,
        "net_return": net_return,
        **extra,
    }
    return [first, second]


def test_cooldown_summary_sum_is_fee_reduced() -> None:
    # End-to-end: the skipped (2nd) trade carries a gross net_return + nonzero fees.
    # 4 ms entry gap << 5-minute cooldown -> the 2nd trade is the skipped one.
    fee_frac = (5.0 + 5.0) / 10_000.0
    gross = 0.01
    rows = _cooldown_skipped_trade(
        net_return=gross,
        entry_fee_usdt=5.0,
        exit_fee_usdt=5.0,
        equity_usdt=10_000.0,
    )
    summary = _cooldown_simulation_summary(
        trade_rows=rows,
        order_rows=[],
        trade_cooldown_minutes=5.0,
        entry_order_cooldown_minutes=-1.0,
    )
    assert summary["skipped_trades"] == 1
    assert summary["skipped_trade_return_observations"] == 1
    # Fee-reduced, NOT the gross 0.01 the old code would have aggregated.
    assert summary["skipped_trade_return_sum"] == gross - fee_frac
    assert summary["skipped_trade_return_mean"] == gross - fee_frac
    assert summary["skipped_trade_return_sum"] < gross


def test_cooldown_summary_already_net_row_unchanged() -> None:
    # Backtest-style skipped row (no fee fields) is aggregated as-is: regression
    # guard that the fix does not alter already-net returns (frozen behaviour).
    rows = _cooldown_skipped_trade(net_return=-0.02)
    summary = _cooldown_simulation_summary(
        trade_rows=rows,
        order_rows=[],
        trade_cooldown_minutes=5.0,
        entry_order_cooldown_minutes=-1.0,
    )
    assert summary["skipped_trades"] == 1
    assert summary["skipped_trade_return_sum"] == -0.02
    assert summary["skipped_trade_return_mean"] == -0.02


# --------------------------------------------------------------------------- #
# Relocated from tests/test_audit_fix_b12.py (audit bucket b12 regressions):
# shadows-3: same primary/addon source must fail loud (silent 2x double-count).
# shadows-4: a trade keyed by entry_ts_ms must not flag its signal-keyed order
# as an unmatched entry-order attempt.
# --------------------------------------------------------------------------- #
def _write_addon_fixture(root: Path, trades: list[dict], orders: list[dict]) -> None:
    write_dataset(pl.DataFrame(trades), root, "continuous_fade_paper_trades", partition_by=())
    write_dataset(pl.DataFrame(orders), root, "continuous_fade_paper_orders", partition_by=())
    write_dataset(
        pl.DataFrame([{"ts_ms": 1_000, "entry_candidates": 1, "entries": 1}]),
        root,
        "continuous_fade_paper_cycles",
        partition_by=(),
    )


def test_addon_audit_same_root_and_dataset_raises(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    trade = {
        "symbol": "BTCUSDT",
        "signal_ts_ms": 1_000,
        "entry_ts_ms": 1_000 + 2 * 3_600_000,
        "trade_id": "t1",
        "strategy_id": "primary",
    }
    order = {
        "symbol": "BTCUSDT",
        "signal_ts_ms": 1_000,
        "trade_id": "t1",
        "status": "filled",
        "order_link_id": "lm-en-ca-1",
        "strategy_id": "primary",
    }
    _write_addon_fixture(root, [trade], [order])

    config = cas.ContinuousAddonShadowAuditConfig(
        primary_data_root=str(root),
        addon_data_root=str(root),
        output_dir=str(tmp_path / "out"),
    )
    with pytest.raises(ValueError, match="same trades source"):
        cas.run_continuous_addon_shadow_audit(config)


def test_addon_audit_same_root_with_strategy_split_is_allowed(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    rows = [
        {
            "symbol": "BTCUSDT",
            "signal_ts_ms": 1_000,
            "entry_ts_ms": 1_000 + 2 * 3_600_000,
            "trade_id": "p1",
            "strategy_id": "primary_strat",
        },
        {
            "symbol": "ETHUSDT",
            "signal_ts_ms": 2_000,
            "entry_ts_ms": 2_000 + 2 * 3_600_000,
            "trade_id": "a1",
            "strategy_id": "addon_strat",
        },
    ]
    orders = [
        {
            "symbol": "BTCUSDT",
            "signal_ts_ms": 1_000,
            "trade_id": "p1",
            "status": "filled",
            "order_link_id": "lm-en-ca-p",
            "strategy_id": "primary_strat",
        },
        {
            "symbol": "ETHUSDT",
            "signal_ts_ms": 2_000,
            "trade_id": "a1",
            "status": "filled",
            "order_link_id": "lm-en-ca-a",
            "strategy_id": "addon_strat",
        },
    ]
    _write_addon_fixture(root, rows, orders)

    config = cas.ContinuousAddonShadowAuditConfig(
        primary_data_root=str(root),
        addon_data_root=str(root),
        expected_primary_strategy_id="primary_strat",
        expected_addon_strategy_id="addon_strat",
        output_dir=str(tmp_path / "out"),
    )
    # Must not raise, and the two sides must be disjoint (1 trade each), not 2/2.
    payload = cas.run_continuous_addon_shadow_audit(config)
    assert payload["summary"]["primary"]["trades"] == 1
    assert payload["summary"]["addon"]["trades"] == 1


def test_reconciliation_signal_ts_has_no_entry_ts_fallback() -> None:
    # Healthy rows carry signal_ts_ms -> identical to _signal_ts.
    healthy = {"signal_ts_ms": 5_000, "entry_ts_ms": 9_000}
    assert cas._reconciliation_signal_ts(healthy) == 5_000
    # A row that dropped the signal fields must NOT fall back to entry_ts_ms
    # (that mis-keys the reconciliation, the shadows-4 bug).
    degenerate = {"entry_ts_ms": 9_000}
    assert cas._reconciliation_signal_ts(degenerate) == 0
    assert cas._signal_ts(degenerate) == 9_000  # legacy fallback unchanged


def test_signal_less_trade_does_not_falsely_flag_its_order_unmatched() -> None:
    # Entry occurs 2 bars after the signal, so entry_ts_ms != signal_ts_ms.
    signal_ts = 1_000_000
    entry_ts = signal_ts + 2 * 3_600_000
    # Trade DROPPED signal_ts_ms (future schema / crash-recovery row) but keeps
    # entry_ts_ms and the authoritative trade_id.
    trade = {"symbol": "BTCUSDT", "entry_ts_ms": entry_ts, "trade_id": "tid-1"}
    # Its order carries signal_ts_ms and the same trade_id.
    order = {
        "symbol": "BTCUSDT",
        "signal_ts_ms": signal_ts,
        "trade_id": "tid-1",
        "status": "filled",
        "order_link_id": "lm-en-ca-1",
    }

    summary = cas._entry_order_trade_reconciliation_summary(order_rows=[order], trade_rows=[trade])
    # trade_id reconciliation must match it -> NOT flagged unmatched.
    assert summary["entry_order_attempts_without_trade_key"] == 0
    assert summary["live_entry_order_attempts_without_trade_key"] == 0


def test_genuinely_unmatched_order_is_still_flagged() -> None:
    # An order with a valid signal key but no corresponding trade (no trade_id
    # match, no signal-key match) must still be counted unmatched.
    order = {
        "symbol": "BTCUSDT",
        "signal_ts_ms": 1_000_000,
        "trade_id": "orphan",
        "status": "filled",
        "order_link_id": "lm-en-ca-9",
    }
    summary = cas._entry_order_trade_reconciliation_summary(order_rows=[order], trade_rows=[])
    assert summary["entry_order_attempts_without_trade_key"] == 1


def test_healthy_signal_keyed_trade_and_order_reconcile() -> None:
    # The live path: trade and order share signal_ts_ms -> reconcile on the key.
    signal_ts = 1_000_000
    trade = {"symbol": "BTCUSDT", "signal_ts_ms": signal_ts, "entry_ts_ms": signal_ts + 3_600_000}
    order = {
        "symbol": "BTCUSDT",
        "signal_ts_ms": signal_ts,
        "status": "filled",
        "order_link_id": "lm-en-ca-1",
    }
    summary = cas._entry_order_trade_reconciliation_summary(order_rows=[order], trade_rows=[trade])
    assert summary["entry_order_attempts_without_trade_key"] == 0


# --------------------------------------------------------------------------
# code-quality-5 (relocated from audit bucket iF): continuous_addon_shadow._float
# routes through the canonical _common.finite_float and drops the divergent
# `float(value or 0.0)` idiom.
# --------------------------------------------------------------------------


def test_addon_shadow_float_drops_divergent_or_idiom() -> None:
    """code-quality-5: the pre-fix continuous_addon_shadow._float used
    `float(value or 0.0)`, which silently rewrites falsy-but-meaningful inputs
    (e.g. 0, 0.0, "") via the `or`. Routing through finite_float makes it agree
    with the canonical helper. Concretely: a string "0.0" still coerces to 0.0,
    and a real 0 stays 0.0 -- but the coercion is now the one shared policy, not
    a divergent local idiom."""
    assert cas._float("0.0") == (finite_float("0.0", default=0.0) or 0.0)
    assert cas._float(0) == 0.0
    # math is still imported in addon_shadow (used elsewhere); the helper no
    # longer re-implements the isfinite guard itself.
    assert cas._float(2.5) == 2.5


def test_addon_audit_same_orders_source_raises(tmp_path: Path) -> None:
    """audit-iter3: distinct trades datasets but a SHARED orders dataset (no strategy
    split) must still fail loud — the orders double-count combined ticket-rate metrics."""
    root = tmp_path / "shared"
    root.mkdir()
    trade = {
        "symbol": "BTCUSDT", "signal_ts_ms": 1_000, "entry_ts_ms": 1_000 + 2 * 3_600_000,
        "trade_id": "t1", "strategy_id": "primary",
    }
    order = {
        "symbol": "BTCUSDT", "signal_ts_ms": 1_000, "trade_id": "t1", "status": "filled",
        "order_link_id": "lm-en-ca-1", "strategy_id": "primary",
    }
    write_dataset(pl.DataFrame([trade]), root, "continuous_fade_paper_trades", partition_by=())
    write_dataset(
        pl.DataFrame([dict(trade, trade_id="t2")]), root,
        "continuous_fade_demo_trades", partition_by=(),
    )
    write_dataset(pl.DataFrame([order]), root, "continuous_fade_paper_orders", partition_by=())
    write_dataset(
        pl.DataFrame([{"ts_ms": 1_000, "entry_candidates": 1, "entries": 1}]),
        root, "continuous_fade_paper_cycles", partition_by=(),
    )
    config = cas.ContinuousAddonShadowAuditConfig(
        primary_data_root=str(root),
        addon_data_root=str(root),
        addon_trades_dataset="continuous_fade_demo_trades",  # distinct -> no trades collision
        output_dir=str(tmp_path / "out"),
    )
    with pytest.raises(ValueError, match="same orders source"):
        cas.run_continuous_addon_shadow_audit(config)
