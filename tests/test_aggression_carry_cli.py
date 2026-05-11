from __future__ import annotations

from pathlib import Path

from aggression_carry import cli as cli_module
from aggression_carry.cli import build_parser, main


def test_cli_fixture_pipeline_end_to_end(tmp_path: Path) -> None:
    data_root = tmp_path / "data"

    assert main(["--data-root", str(data_root), "download-data", "--fixture"]) == 0
    assert main(["--data-root", str(data_root), "volume-alpha"]) == 0
    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "volume-backtest",
                "--start",
                "2025-01-02",
                "--end",
                "2025-01-05",
                "--hold-days",
                "1",
                "--rebalance-days",
                "1",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "volume-grid",
                "--start",
                "2025-01-02",
                "--end",
                "2025-01-05",
                "--hold-days",
                "1",
                "--quantiles",
                "0.5",
                "--fixed-stops",
                "0,0.001",
                "--vol-stops",
                "",
                "--rank-exits",
                "false",
                "--workers",
                "1",
            ]
        )
        == 0
    )

    assert (data_root / "reports" / "volume_alpha_report.md").exists()
    assert (data_root / "reports" / "volume_backtest_report.md").exists()
    assert (data_root / "reports" / "volume_grid_report.md").exists()
    assert main(["--data-root", str(data_root), "forward-report"]) == 0
    assert (data_root / "reports" / "forward_paper_report.md").exists()
    assert main(["--data-root", str(data_root), "forward-audit"]) == 0
    assert (data_root / "reports" / "forward_demo_audit_report.md").exists()


def test_cli_parses_forward_sleeves_alias(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "forward-sleeves",
            "--now",
            "2026-01-15T22:06:00+00:00",
            "--telegram",
        ]
    )

    assert args.command == "forward-sleeves"
    assert args.telegram is True


def test_cli_reads_symbol_allowlist_file(tmp_path: Path) -> None:
    symbols_file = tmp_path / "symbols.txt"
    symbols_file.write_text("ethusdt\nSOLUSDT,btcusdt\n", encoding="utf-8")

    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "download-data",
            "--symbols",
            "BTCUSDT",
            "--symbols-file",
            str(symbols_file),
            "--start",
            "2025-01-01",
            "--end",
            "2025-01-02",
        ]
    )

    assert args.symbols_file == str(symbols_file)
    assert cli_module._symbols_from_cli(args.symbols, args.symbols_file) == ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def test_cli_parses_archive_discard_archives(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "archive-download-klines",
            "--include-flow",
            "--discard-archives",
        ]
    )

    assert args.include_flow is True
    assert args.discard_archives is True


def test_cli_parses_forward_audit_telegram(tmp_path: Path) -> None:
    args = build_parser().parse_args(["--data-root", str(tmp_path), "forward-audit", "--telegram"])

    assert args.command == "forward-audit"
    assert args.telegram is True


def test_cli_parses_daily_close_fade_entry_risk_knobs(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "daily-close-fade",
            "--stop-delay-minutes",
            "0",
            "--profit-protection-delay-minutes",
            "15",
            "--twap-stop-adding-pct",
            "0.05",
        ]
    )

    assert args.command == "daily-close-fade"
    assert args.stop_delay_minutes == 0
    assert args.profit_protection_delay_minutes == 15
    assert args.twap_stop_adding_pct == 0.05


def test_cli_parses_daily_close_fade_diagnostics(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "daily-close-fade-diagnostics",
            "--signal-times",
            "22:00,23:00",
            "--entry-delays",
            "0,15,60",
            "--horizons",
            "60,180",
            "--scores",
            "day_return,vol_adjusted_day_return",
            "--top-ns",
            "3,5",
            "--buckets",
            "5",
            "--start",
            "2025-01-01",
            "--end",
            "2025-02-01",
            "--cost-multiplier",
            "2",
            "--pump-filter",
            "pump",
            "--liquidity-rank-min",
            "31",
            "--liquidity-rank-max",
            "150",
        ]
    )

    assert args.command == "daily-close-fade-diagnostics"
    assert args.signal_times == "22:00,23:00"
    assert args.entry_delays == "0,15,60"
    assert args.horizons == "60,180"
    assert args.buckets == 5
    assert args.start == "2025-01-01"
    assert args.end == "2025-02-01"
    assert args.cost_multiplier == 2
    assert args.pump_filter == "pump"
    assert args.liquidity_rank_min == 31
    assert args.liquidity_rank_max == 150


def test_cli_parses_daily_close_fade_grid_dates(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "daily-close-fade-grid",
            "--start",
            "2025-01-01",
            "--end",
            "2025-02-01",
            "--profit-protection-delay-minutes",
            "0,15,30",
        ]
    )

    assert args.command == "daily-close-fade-grid"
    assert args.start == "2025-01-01"
    assert args.end == "2025-02-01"
    assert args.profit_protection_delay_minutes == "0,15,30"


def test_cli_parses_bybit_demo_probe(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "bybit-demo-probe",
            "--symbol",
            "ETHUSDT",
            "--place-order",
            "--i-understand-demo-order",
        ]
    )

    assert args.command == "bybit-demo-probe"
    assert args.symbol == "ETHUSDT"
    assert args.place_order is True
    assert args.i_understand_demo_order is True


def test_cli_parses_bybit_demo_sync(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "bybit-demo-sync",
            "--submit-orders",
            "--i-understand-demo-sync",
            "--max-order-notional",
            "7",
            "--use-wallet-balance",
        ]
    )

    assert args.command == "bybit-demo-sync"
    assert args.submit_orders is True
    assert args.i_understand_demo_sync is True
    assert args.max_order_notional == 7
    assert args.use_wallet_balance is True


def test_cli_parses_bybit_demo_cycle(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "bybit-demo-cycle",
            "--now",
            "2026-01-15T22:06:00+00:00",
            "--submit-orders",
            "--i-understand-demo-sync",
            "--telegram",
            "--max-order-notional",
            "0",
            "--max-new-orders",
            "2",
            "--max-total-new-notional",
            "0",
            "--use-wallet-balance",
            "--wallet-balance-fraction",
            "0.5",
            "--max-order-notional-pct-equity",
            "0.8",
            "--max-total-new-notional-pct-equity",
            "1.0",
            "--cancel-stale-minutes",
            "0",
            "--price-offset-bps",
            "3",
            "--no-market-exit",
            "--active-start",
            "22:05",
            "--active-end",
            "02:30",
            "--ignore-active-window",
        ]
    )

    assert args.command == "bybit-demo-cycle"
    assert args.now == "2026-01-15T22:06:00+00:00"
    assert args.submit_orders is True
    assert args.i_understand_demo_sync is True
    assert args.telegram is True
    assert args.max_order_notional == 0
    assert args.max_new_orders == 2
    assert args.max_total_new_notional == 0
    assert args.use_wallet_balance is True
    assert args.wallet_balance_fraction == 0.5
    assert args.max_order_notional_pct_equity == 0.8
    assert args.max_total_new_notional_pct_equity == 1.0
    assert args.cancel_stale_minutes == 0
    assert args.price_offset_bps == 3
    assert args.no_market_exit is True
    assert args.active_start == "22:05"
    assert args.active_end == "02:30"
    assert args.ignore_active_window is True


def test_cli_parses_bybit_demo_emergency_commands(tmp_path: Path) -> None:
    cancel_args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "bybit-demo-cancel-all",
            "--symbols",
            "BTCUSDT,ETHUSDT",
        ]
    )
    flatten_args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "bybit-demo-flatten",
            "--i-understand-demo-flatten",
        ]
    )

    assert cancel_args.command == "bybit-demo-cancel-all"
    assert cancel_args.symbols == "BTCUSDT,ETHUSDT"
    assert flatten_args.command == "bybit-demo-flatten"
    assert flatten_args.i_understand_demo_flatten is True
