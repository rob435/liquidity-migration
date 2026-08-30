"""Contracts for the tape-level mechanics grader.

The load-bearing pins: the two entry bounds split on an at-limit print, the
stop walk's arithmetic on a two-level book, sequence-gap rows refusing book
state until the next clean snapshot, and a zero-coverage run completing with
the confession instead of failing.
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.research.check_v12_mechanics_on_tape import _kernel_identity, main, walk_book  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd is not installed")

# 2027-01-15 08:00:00 UTC; tape day directories are named by the receive day.
T0_MS = 1_800_000_000_000
DAY = "2027-01-15"


def book_row(
    symbol: str,
    ts_ms: int,
    *,
    bids: list[list[str]],
    asks: list[list[str]],
    depth: int = 50,
    kind: str = "orderbook_snapshot",
    sequence_gap: bool = False,
    restart_snapshot: bool = False,
) -> dict[str, object]:
    return {
        "kind": kind,
        "symbol": symbol,
        "depth": depth,
        "local_receive_ts_ns": ts_ms * 1_000_000,
        "exchange_engine_ts_ns": ts_ms * 1_000_000,
        "bids": bids,
        "asks": asks,
        "update_id": 10,
        "sequence_gap": sequence_gap,
        "restart_snapshot": restart_snapshot,
    }


def trade_row(symbol: str, ts_ms: int, price: float, qty: float = 1.0, side: str = "Sell") -> dict[str, object]:
    return {
        "kind": "public_trade",
        "symbol": symbol,
        "local_receive_ts_ns": ts_ms * 1_000_000,
        "exchange_ts_ns": ts_ms * 1_000_000,
        "trade_id": f"t{ts_ms}",
        "price": price,
        "qty": qty,
        "side": side,
    }


def ticker_row(symbol: str, ts_ms: int, mark_price: float) -> dict[str, object]:
    return {
        "kind": "ticker",
        "symbol": symbol,
        "local_receive_ts_ns": ts_ms * 1_000_000,
        "values": {"mark_price": mark_price},
    }


def write_segment(
    root: Path,
    symbol: str,
    rows: list[dict[str, object]],
    index: int = 0,
    *,
    day: str = DAY,
) -> None:
    directory = root / day / symbol
    directory.mkdir(parents=True, exist_ok=True)
    raw = directory / f"segment-{index:06d}.jsonl"
    raw.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    subprocess.run(["zstd", "-q", "-3", "--rm", "--", str(raw)], check=True)


def write_trades_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fields = [
        "trade_id",
        "symbol",
        "evidence_kind",
        "entry_signal_ts_ms",
        "entry_ts_ms",
        "entry_price",
        "exit_ts_ms",
        "exit_price",
        "exit_reason",
        "stop_price",
        "limit_price",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({"evidence_kind": "registered_model_trade", **row} for row in rows)


def run_and_read(tape_root: Path, trades: Path, out: Path, notional: float = 1000.0) -> list[dict[str, str]]:
    code = main(
        [
            "--tape-root",
            str(tape_root),
            "--model-trades",
            str(trades),
            "--out",
            str(out),
            "--notional-usdt",
            str(notional),
        ]
    )
    assert code == 0
    with (out / "mechanics_per_trade.csv").open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_at_limit_print_fills_optimistic_but_not_conservative(tmp_path: Path) -> None:
    symbol = "AAAUSDT"
    tape = tmp_path / "tape"
    # Signal close 100.0 -> derived limit 99.0. One print AT 99.0, one THROUGH at 98.4.
    write_segment(
        tape,
        symbol,
        [
            trade_row(symbol, T0_MS - 1_000, 100.0),
            book_row(symbol, T0_MS + 1_000, bids=[["98.0", "5"]], asks=[["100.5", "5"]]),
            trade_row(symbol, T0_MS + 60_000, 99.0),
            trade_row(symbol, T0_MS + 120_000, 98.4),
        ],
    )
    trades = tmp_path / "trades.csv"
    write_trades_csv(
        trades,
        [
            {
                "trade_id": "derived-limit",
                "symbol": symbol,
                "entry_signal_ts_ms": T0_MS,
                "entry_ts_ms": T0_MS + 2 * 3_600_000,
                "entry_price": 100.2,
                "exit_ts_ms": T0_MS + 24 * 3_600_000,
                "exit_price": 101.0,
                "exit_reason": "time_stop",
            },
            {
                "trade_id": "explicit-limit",
                "symbol": symbol,
                "entry_signal_ts_ms": T0_MS,
                "entry_ts_ms": T0_MS + 2 * 3_600_000,
                "entry_price": 100.2,
                "limit_price": 98.5,
            },
            {
                "trade_id": "never-fills",
                "symbol": symbol,
                "entry_signal_ts_ms": T0_MS,
                "limit_price": 90.0,
            },
        ],
    )
    out = tmp_path / "out"
    rows = {row["trade_id"]: row for row in run_and_read(tape, trades, out)}

    derived = rows["derived-limit"]
    assert derived["limit_price"] == "99.0"
    assert derived["limit_source"] == "derived_tape_close"
    assert derived["signal_close_tape"] == "100.0"
    # The 99.0 print is at the limit, not through it: optimistic only.
    assert derived["entry_optimistic_fill_ts_ms"] == str(T0_MS + 60_000)
    assert derived["entry_conservative_fill_ts_ms"] == str(T0_MS + 120_000)
    # Kernel assumed a fill at the 2h next-bar open; the tape says both bounds beat it.
    assert float(derived["entry_optimistic_gap_s"]) == 60.0 - 7200.0
    assert float(derived["entry_limit_vs_kernel_bp"]) == pytest.approx((100.2 - 99.0) / 99.0 * 1e4)

    explicit = rows["explicit-limit"]
    assert explicit["limit_source"] == "explicit"
    # 98.4 < 98.5: through the level, so both bounds fill at the same print.
    assert explicit["entry_conservative_fill_ts_ms"] == str(T0_MS + 120_000)
    assert explicit["entry_optimistic_fill_ts_ms"] == str(T0_MS + 120_000)

    never = rows["never-fills"]
    assert never["entry_optimistic_fill_ts_ms"] == ""
    assert "entry window is not fully bracketed" in never["entry_note"]
    # These rows overlap tape but their six-hour entry windows are not
    # bracketed. Observed fills remain in the per-row diagnostic, not the
    # aggregate comparison.
    summary = (out / "mechanics_summary.md").read_text(encoding="utf-8")
    assert "fully bracketed rows with an entry limit graded: 0" in summary


def test_trade_received_exactly_at_signal_is_the_derived_close(tmp_path: Path) -> None:
    symbol = "AABUSDT"
    tape = tmp_path / "tape"
    write_segment(
        tape,
        symbol,
        [
            trade_row(symbol, T0_MS - 1_000, 100.0),
            trade_row(symbol, T0_MS, 101.0),
            book_row(symbol, T0_MS + 1_000, bids=[["100.0", "5"]], asks=[["102.0", "5"]]),
        ],
    )
    trades = tmp_path / "trades.csv"
    write_trades_csv(
        trades,
        [
            {
                "trade_id": "exact-signal-close",
                "symbol": symbol,
                "entry_signal_ts_ms": T0_MS,
                "entry_ts_ms": T0_MS + 2 * 3_600_000,
                "entry_price": 102.0,
            }
        ],
    )
    (row,) = run_and_read(tape, trades, tmp_path / "out")

    assert row["signal_close_tape"] == "101.0"
    assert row["signal_close_age_s"] == "0.0"
    assert float(row["limit_price"]) == pytest.approx(99.99)


def test_stop_walk_arithmetic_on_two_level_book(tmp_path: Path) -> None:
    symbol = "BBBUSDT"
    tape = tmp_path / "tape"
    write_segment(
        tape,
        symbol,
        [
            book_row(symbol, T0_MS + 1_000, bids=[["99.0", "6"], ["98.0", "10"]], asks=[["101.0", "5"]]),
            ticker_row(symbol, T0_MS + 10_000, 100.5),
            book_row(
                symbol,
                T0_MS + 19_500,
                bids=[],
                asks=[],
                kind="orderbook_delta",
            ),
            ticker_row(symbol, T0_MS + 20_000, 99.9),
        ],
    )
    trades = tmp_path / "trades.csv"
    write_trades_csv(
        trades,
        [
            {
                "trade_id": "stopper",
                "symbol": symbol,
                "entry_signal_ts_ms": T0_MS - 3_600_000,
                "entry_ts_ms": T0_MS + 5_000,
                "entry_price": 100.5,
                "exit_ts_ms": T0_MS + 3_600_000,
                "exit_price": 100.0,
                "exit_reason": "stop_loss",
                "stop_price": 100.0,
            }
        ],
    )
    (row,) = run_and_read(tape, trades, tmp_path / "out")

    # Mark 100.5 does not cross; mark 99.9 does. A 1000 USDT entry at 100.5
    # buys 9.95025 units: 6 at 99.0 then the rest at 98.0.
    assert row["stop_trigger_ts_ms"] == str(T0_MS + 20_000)
    assert row["stop_trigger_source"] == "mark_price"
    target_qty = 1000.0 / 100.5
    expected_avg = (6.0 * 99.0 + (target_qty - 6.0) * 98.0) / target_qty
    assert float(row["stop_walk_avg_price"]) == pytest.approx(expected_avg)
    assert float(row["stop_walk_shortfall_bp"]) == pytest.approx((100.0 - expected_avg) * 100.0)
    assert float(row["stop_walk_filled_fraction"]) == pytest.approx(1.0)
    assert float(row["stop_walk_target_qty"]) == pytest.approx(target_qty)
    assert float(row["stop_trigger_vs_kernel_exit_s"]) == pytest.approx(20.0 - 3_600.0)


def test_sequence_gap_refuses_book_state_until_next_snapshot(tmp_path: Path) -> None:
    symbol = "CCCUSDT"
    tape = tmp_path / "tape"
    write_segment(
        tape,
        symbol,
        [
            book_row(symbol, T0_MS + 1_000, bids=[["99.0", "6"], ["98.0", "10"]], asks=[["101.0", "5"]]),
            # A gapped delta claiming a fat 99.5 bid: the mirror must refuse it.
            book_row(
                symbol,
                T0_MS + 5_000,
                bids=[["99.5", "100"]],
                asks=[],
                kind="orderbook_delta",
                sequence_gap=True,
            ),
            ticker_row(symbol, T0_MS + 10_000, 99.9),
            # Clean snapshot restores the book; a later trigger walks fine.
            book_row(symbol, T0_MS + 40_000, bids=[["99.0", "6"], ["98.0", "10"]], asks=[["101.0", "5"]]),
            ticker_row(symbol, T0_MS + 40_500, 99.9),
        ],
    )
    trades = tmp_path / "trades.csv"
    common = {
        "symbol": symbol,
        "entry_signal_ts_ms": T0_MS - 3_600_000,
        "entry_price": 100.5,
        "exit_ts_ms": T0_MS + 3_600_000,
        "exit_price": 100.0,
        "exit_reason": "stop_loss",
        "stop_price": 100.0,
    }
    write_trades_csv(
        trades,
        [
            {"trade_id": "during-gap", "entry_ts_ms": T0_MS + 2_000, **common},
            {"trade_id": "after-snapshot", "entry_ts_ms": T0_MS + 40_250, **common},
        ],
    )
    rows = {row["trade_id"]: row for row in run_and_read(tape, trades, tmp_path / "out")}

    during = rows["during-gap"]
    assert during["stop_trigger_ts_ms"] == str(T0_MS + 10_000)
    assert during["stop_walk_avg_price"] == ""
    assert "unhealthy" in during["stop_note"]

    after = rows["after-snapshot"]
    assert after["stop_trigger_ts_ms"] == str(T0_MS + 40_500)
    # Had the gapped 99.5x100 bid been applied, the walk would average 99.5.
    target_qty = 1000.0 / 100.5
    expected_avg = (6.0 * 99.0 + (target_qty - 6.0) * 98.0) / target_qty
    assert float(after["stop_walk_avg_price"]) == pytest.approx(expected_avg)


def test_zero_coverage_run_completes_with_confession(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tape = tmp_path / "tape"
    write_segment(tape, "ZZZUSDT", [trade_row("ZZZUSDT", T0_MS, 1.0)])
    trades = tmp_path / "trades.csv"
    write_trades_csv(
        trades,
        [
            {
                "trade_id": "uncovered",
                "symbol": "DDDUSDT",
                "entry_signal_ts_ms": T0_MS,
                "entry_ts_ms": T0_MS + 3_600_000,
                "entry_price": 5.0,
                "exit_ts_ms": T0_MS + 7_200_000,
                "exit_price": 5.0,
                "exit_reason": "time_stop",
                "stop_price": 4.5,
            }
        ],
    )
    out = tmp_path / "out"
    (row,) = run_and_read(tape, trades, out)
    printed = capsys.readouterr().out

    assert row["covered"] == "False"
    assert "no tape for this symbol" in row["entry_note"]
    assert "rows with any tape overlap:       0" in printed
    assert "grades nothing" in printed
    assert (out / "mechanics_summary.md").exists()
    provenance = json.loads((out / "mechanics_provenance.json").read_text(encoding="utf-8"))
    assert provenance["model_kernel"]["pinned"] is False
    assert provenance["tape_contract"]["event_time_basis"] == "local_receive_ts_ns"
    assert provenance["other_inputs"]["model_trades"]["sha256"]


def test_walk_book_partial_fill_reports_fraction() -> None:
    avg, filled = walk_book([(99.0, 6.0), (98.0, 4.0)], 20.0)
    assert avg == pytest.approx((6 * 99.0 + 4 * 98.0) / 10.0)
    assert filled == pytest.approx(10.0)
    assert walk_book([], 5.0) == (None, 0.0)


def test_model_final_stop_reconstructs_the_48_hour_v12_path(tmp_path: Path) -> None:
    symbol = "EEEUSDT"
    tape = tmp_path / "tape"
    entry_ms = T0_MS + 5_000
    decay_ms = entry_ms + 48 * 3_600_000
    write_segment(
        tape,
        symbol,
        [
            trade_row(symbol, T0_MS - 1_000, 100.0),
            book_row(symbol, entry_ms + 1_000, bids=[["96.4", "20"]], asks=[["96.8", "20"]]),
            # Below the final 97 stop but above the initial 94 stop: no early trigger.
            ticker_row(symbol, entry_ms + 2_000, 96.5),
        ],
    )
    write_segment(
        tape,
        symbol,
        [
            book_row(symbol, decay_ms + 1_000, bids=[["96.4", "20"]], asks=[["96.8", "20"]]),
            ticker_row(symbol, decay_ms + 2_000, 96.5),
        ],
        day="2027-01-17",
    )
    trades = tmp_path / "trades.csv"
    write_trades_csv(
        trades,
        [
            {
                "trade_id": "decayed-stop",
                "symbol": symbol,
                "entry_signal_ts_ms": T0_MS,
                "entry_ts_ms": entry_ms,
                "entry_price": 100.0,
                "exit_ts_ms": decay_ms + 3_600_000,
                "exit_price": 97.0,
                "exit_reason": "stop_loss",
                # The kernel CSV stores the final 1.5x-ATR stop only.
                "stop_price": 97.0,
                "limit_price": 90.0,
            }
        ],
    )
    (row,) = run_and_read(tape, trades, tmp_path / "out")

    assert float(row["stop_initial_level"]) == pytest.approx(94.0)
    assert int(row["stop_decay_ts_ms"]) == decay_ms
    assert float(row["stop_decayed_level"]) == pytest.approx(97.0)
    assert row["stop_path_source"] == "model_csv_final_decayed"
    assert row["stop_trigger_ts_ms"] == str(decay_ms + 2_000)
    assert float(row["stop_walk_target_qty"]) == pytest.approx(10.0)
    assert row["stop_observation_bracketed"] == "False"
    assert "2027-01-16" in row["coverage_note"]


def test_evidence_kinds_are_explicit_in_every_output(tmp_path: Path) -> None:
    symbol = "FFFUSDT"
    tape = tmp_path / "tape"
    write_segment(
        tape,
        symbol,
        [
            trade_row(symbol, T0_MS - 1_000, 100.0),
            book_row(symbol, T0_MS + 1_000, bids=[["99.5", "20"]], asks=[["100.5", "20"]]),
            ticker_row(symbol, T0_MS + 6 * 3_600_000, 100.0),
        ],
    )
    trades = tmp_path / "trades.csv"
    write_trades_csv(
        trades,
        [
            {
                "trade_id": "proxy-with-an-opaque-id",
                "symbol": symbol,
                "evidence_kind": "tape_derived_proxy",
                "entry_signal_ts_ms": T0_MS,
                "entry_ts_ms": T0_MS + 2 * 3_600_000,
                "entry_price": 101.0,
                "exit_ts_ms": T0_MS + 3 * 3_600_000,
                "exit_price": 100.0,
                "exit_reason": "time_stop",
                "stop_price": 90.0,
            },
            {
                "trade_id": "opaque-second-id",
                "symbol": symbol,
                "evidence_kind": "artificial_exercise",
                "entry_signal_ts_ms": T0_MS,
                "entry_ts_ms": T0_MS + 2 * 3_600_000,
                "entry_price": 101.0,
                "exit_ts_ms": T0_MS + 3 * 3_600_000,
                "exit_price": 100.0,
                "exit_reason": "time_stop",
                "stop_price": 90.0,
                "limit_price": 99.0,
            },
        ],
    )
    out = tmp_path / "out"
    rows = run_and_read(tape, trades, out)

    assert {row["evidence_kind"] for row in rows} == {"tape_derived_proxy", "artificial_exercise"}
    summary = (out / "mechanics_summary.md").read_text(encoding="utf-8")
    assert "registered_model_trade=0" in summary
    assert "tape_derived_proxy=1" in summary
    assert "artificial_exercise=1" in summary
    assert "registered model rows in any bracketed comparison: 0" in summary

    comparison = json.loads((out / "mechanics_comparison.json").read_text(encoding="utf-8"))
    assert comparison["registered_model_rows_graded"] == 0
    assert comparison["evidence_kind_counts"]["entry_comparison"] == {
        "artificial_exercise": 1,
        "live_state_observation": 0,
        "live_transition_observation": 0,
        "registered_model_trade": 0,
        "tape_derived_proxy": 1,
    }
    provenance = json.loads((out / "mechanics_provenance.json").read_text(encoding="utf-8"))
    assert provenance["scope"]["registered_model_rows_graded"] == 0
    assert provenance["scope"]["evidence_kind_counts"] == comparison["evidence_kind_counts"]
    assert any(item["path"].endswith("mechanics_comparison.json") for item in provenance["outputs"])


def test_model_rows_without_explicit_evidence_kind_fail_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trades = tmp_path / "trades.csv"
    trades.write_text(
        "trade_id,symbol,entry_signal_ts_ms\nmissing-kind,AAAUSDT,1800000000000\n",
        encoding="utf-8",
    )
    code = main(
        [
            "--tape-root",
            str(tmp_path / "tape"),
            "--model-trades",
            str(trades),
            "--out",
            str(tmp_path / "out"),
        ]
    )

    assert code == 2
    assert "must declare evidence_kind" in capsys.readouterr().err


def test_model_commit_refuses_a_different_current_profile_blob(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    rules = repo / "liquidity_migration/rules"
    backtest = repo / "liquidity_migration/research/backtest"
    rules.mkdir(parents=True)
    backtest.mkdir(parents=True)
    (rules / "long_native.py").write_text("PROFILE = 'v12'\n", encoding="utf-8")
    (backtest / "long_native.py").write_text("FILL = 'next_open'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "tape@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tape Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "pin"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    identity = _kernel_identity(commit, repo=repo)
    assert identity["matches_current_checkout"] is True
    assert identity["source_blobs"] == identity["current_source_blobs"]

    (rules / "long_native.py").write_text("PROFILE = 'different'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match the current checkout"):
        _kernel_identity(commit, repo=repo)
