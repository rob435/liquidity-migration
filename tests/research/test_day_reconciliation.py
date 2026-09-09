from __future__ import annotations

import json
import stat
import struct
from decimal import Decimal
from pathlib import Path
from typing import Any

from liquidity_migration.research.day_reconciliation import (
    day_window,
    main,
    reconcile_day,
    render_table,
)
from liquidity_migration.research.venue_wal_accounting import (
    CAPTURE_SOURCE_CONTRACT,
    CAPTURE_TRANSFER_SOURCES,
    WAL_MAGIC,
    _retention_start_ms,
    crc32c,
)

DAY = "2026-09-08"
START_MS, END_MS = day_window(DAY)
USER_ID = "3141592"
OPEN_MS = START_MS + 3_600_000
FUND_MS = START_MS + 5_400_000
CLOSE_MS = START_MS + 7_200_000
TRANSFER_MS = START_MS + 72_000_000
CAPTURE_END_MS = END_MS + 300_000
BEGIN_READING_MS = START_MS + 19_000
END_READING_MS = END_MS + 19_000
OPENING_CASH = Decimal("1000")


def _frame(record: dict[str, Any]) -> bytes:
    payload = json.dumps(record, separators=(",", ":")).encode()
    return struct.pack("<II", len(payload), crc32c(payload)) + payload


def _order(client_id: str, side: str, qty: float, reduce_only: bool) -> dict[str, Any]:
    return {
        "kind": "order_sent",
        "request": {
            "client_order_id": client_id,
            "strategy": 1,
            "symbol": 0,
            "side": side,
            "qty": qty,
            "kind": "Market",
            "stop": None,
            "reduce_only": reduce_only,
            "close_position": reduce_only,
        },
        "wire_ns": 1,
        "arrival_mid": 100.0,
    }


def _ack(client_id: str, order_id: str) -> dict[str, Any]:
    return {
        "kind": "order_update",
        "update": {"Ack": {"client_order_id": client_id, "venue_order_id": order_id, "sent_ns": 1, "ack_ns": 2}},
    }


def _fill(client_id: str, exec_id: str, side: str, qty: float, px: float, fee: float, ts_ms: int) -> dict[str, Any]:
    return {
        "kind": "order_update",
        "update": {
            "Fill": {
                "exec_id": exec_id,
                "client_order_id": client_id,
                "symbol": 0,
                "side": side,
                "qty": qty,
                "px": px,
                "fee": fee,
                "is_maker": False,
                "venue_ts_ms": ts_ms,
                "recv_ns": ts_ms * 1_000_000,
            }
        },
    }


def _segment_base(held_qty: float) -> dict[str, Any]:
    return {
        "kind": "segment_base_v7",
        "wall_ts_ms": START_MS - 2000,
        "strategies": ["carry", "long"],
        "symbols": ["BTCUSDT"],
        "open_orders": [],
        "attribution": [{"strategy": 1, "symbol": 0, "signed_qty": held_qty}],
        "logged_exposure": [{"symbol": 0, "signed_qty": held_qty}],
    }


def _wal_records(*, close: bool, rotation: bool = False, tail: bool = True) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = [_segment_base(2.0)] if rotation else []
    records += [
        {
            "kind": "boot",
            "version": "engine-core 0.1.0",
            "config_sha256": "0" * 64,
            "wall_ts_ms": START_MS - 1000,
            "commit": "a" * 40,
        },
        {"kind": "names", "strategies": ["carry", "long"], "symbols": ["BTCUSDT"]},
        _order("long-entry", "Buy", 2.0, False),
        _ack("long-entry", "venue-entry"),
        _fill("long-entry", "exec-entry", "Buy", 2.0, 100.0, 0.10, OPEN_MS),
    ]
    if close:
        records += [
            _order("long-exit", "Sell", 2.0, True),
            _ack("long-exit", "venue-exit"),
            _fill("long-exit", "exec-exit", "Sell", 2.0, 110.0, 0.11, CLOSE_MS),
        ]
    if tail:
        # A live WAL keeps writing past the end boundary reading, which is what
        # the coverage requirement reads.
        records.append({"kind": "note", "wall_ts_ms": END_MS + 60_000, "text": "past the boundary"})
    return records


def _execution(exec_id: str, order_id: str, link: str, side: str, qty: str, px: str, fee: str, ts_ms: int) -> dict[str, Any]:
    return {
        "_kind": "execution",
        "execId": exec_id,
        "orderId": order_id,
        "orderLinkId": link,
        "symbol": "BTCUSDT",
        "side": side,
        "execQty": qty,
        "execPrice": px,
        "execFee": fee,
        "isMaker": False,
        "execType": "Trade",
        "execTime": str(ts_ms),
        "_server_time_ms": str(END_MS + 1000),
    }


def _transaction(
    identity: str,
    kind: str,
    ts_ms: int,
    *,
    cash_flow: str,
    funding: str,
    fee: str,
    change: str,
    cash_balance: str,
    symbol: str = "BTCUSDT",
    trade_id: str = "",
) -> dict[str, Any]:
    return {
        # The linear transaction log excludes transfers, so a transfer row
        # reaches the capture from its own source.
        "_kind": {"TRANSFER_IN": "transfer_in", "TRANSFER_OUT": "transfer_out"}.get(kind, "transaction"),
        "id": identity,
        "type": kind,
        "symbol": symbol,
        "category": "linear",
        "currency": "USDT",
        "side": "Buy",
        "tradeId": trade_id,
        "orderId": "",
        "orderLinkId": "",
        "qty": "0",
        "size": "0",
        "tradePrice": "0",
        "cashFlow": cash_flow,
        "funding": funding,
        "fee": fee,
        "change": change,
        "cashBalance": cash_balance,
        "transactionTime": str(ts_ms),
        "_server_time_ms": str(END_MS + 1000),
    }


def _ledger(*, close: bool, extra: tuple[tuple[str, str, int, str, str, str], ...] = ()) -> list[dict[str, Any]]:
    """The venue's own cash rows, with `change` and the `cashBalance` chain computed from one opening."""

    entries = [
        ("t-open", "TRADE", OPEN_MS, "0", "0", "0.10"),
        ("t-fund", "SETTLEMENT", FUND_MS, "0", "0.50", "0"),
        *(("t-close", "TRADE", CLOSE_MS, "20", "0", "0.11"),) * int(close),
        ("t-transfer", "TRANSFER_IN", TRANSFER_MS, "5", "0", "0"),
        *extra,
    ]
    # Stable on the timestamp alone, so entries sharing one keep the order the
    # caller gave: that is the chain, and the venue's id is not.
    entries.sort(key=lambda entry: entry[2])
    balance = OPENING_CASH
    rows: list[dict[str, Any]] = []
    for identity, kind, ts_ms, cash_flow, funding, fee in entries:
        change = Decimal(cash_flow) + Decimal(funding) - Decimal(fee)
        balance += change
        rows.append(
            _transaction(
                identity,
                kind,
                ts_ms,
                cash_flow=cash_flow,
                funding=funding,
                fee=fee,
                change=f"{change:.2f}",
                cash_balance=f"{balance:.2f}",
                trade_id={"t-open": "exec-entry", "t-close": "exec-exit"}.get(identity, ""),
                symbol="" if kind == "TRANSFER_IN" else "BTCUSDT",
            )
        )
    return rows


def _equity_at(rows: list[dict[str, Any]], reading_ms: int) -> float:
    balance = OPENING_CASH + sum(
        (Decimal(row["change"]) for row in rows if int(row["transactionTime"]) < reading_ms), Decimal(0)
    )
    return float(balance)


def _capture_rows(*, close: bool, ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        _execution("exec-entry", "venue-entry", "long-entry", "Buy", "2", "100", "0.10", OPEN_MS),
        {
            "_kind": "execution",
            "execId": "exec-fund",
            "symbol": "BTCUSDT",
            "execType": "Funding",
            "execFee": "-0.50",
            "execTime": str(FUND_MS),
            "_server_time_ms": str(CAPTURE_END_MS + 1000),
        },
    ]
    if close:
        rows += [
            _execution("exec-exit", "venue-exit", "long-exit", "Sell", "2", "110", "0.11", CLOSE_MS),
            {
                "_kind": "closed_pnl",
                "orderId": "venue-exit",
                "symbol": "BTCUSDT",
                "side": "Sell",
                "execType": "Trade",
                "closedSize": "2",
                "cumEntryValue": "200",
                "cumExitValue": "220",
                "openFee": "0.10",
                "closeFee": "0.11",
                "closedPnl": "20.29",
                "fillCount": "1",
                "updatedTime": str(CLOSE_MS + 1),
                "_server_time_ms": str(CAPTURE_END_MS + 1000),
            },
        ]
    return rows + ledger


def _manifest(
    rows: list[dict[str, Any]], capture_end: int = CAPTURE_END_MS, *, transfer_sources: bool = True
) -> dict[str, Any]:
    counts = {
        name: sum(row["_kind"] == name for row in rows) for name in CAPTURE_SOURCE_CONTRACT
    }
    query_end = capture_end + 2000
    return {
        "_kind": "capture",
        "schema_version": 1,
        "complete": True,
        "captured_at_utc": "2026-09-09T00:06:00+00:00",
        "realm": "mainnet",
        "api_base_url": "https://api.bybit.com",
        "user_id": USER_ID,
        "credential_set": "read-only",
        "start_ms": START_MS,
        "end_ms_exclusive": capture_end,
        "retention_start_ms": _retention_start_ms(query_end),
        "venue_query_start_time_ms": capture_end + 1000,
        "venue_query_end_time_ms": query_end,
        "sources": {
            name: {
                "complete": True,
                "endpoint": endpoint,
                "params": dict(params),
                "slices": 1,
                "pages": 1,
                "rows": counts[name],
            }
            for name, (endpoint, params) in CAPTURE_SOURCE_CONTRACT.items()
            if transfer_sources or name not in CAPTURE_TRANSFER_SOURCES
        },
        "boundary": "[start_ms, end_ms_exclusive)",
    }


def _sample(
    ts_ms: int,
    equity: float,
    *,
    positions: int = 0,
    notional: float = 0.0,
    unrealised: float | None = None,
    holdings: list[dict[str, Any]] | None = None,
    unattributed: int = 0,
) -> dict[str, Any]:
    row = {
        "ts_ms": ts_ms,
        "realm": "mainnet",
        "kind": "engine",
        "state": "live",
        "venue": "bybit",
        "mode": "live",
        "engine_commit": "c0ffee11",
        "account_user_id": USER_ID,
        "account_age_ms": 1000,
        "heartbeat_age_ms": 500,
        "equity_usdt": equity,
        "available_usdt": equity,
        "position_count": positions,
        "position_entry_notional_usdt": notional,
        "sleeve_positions": {"carry": 0, "long": positions - unattributed}
        | ({"unattributed": unattributed} if unattributed else {}),
    }
    if unrealised is not None:
        row["unrealised_pnl_usdt"] = unrealised
        row["wallet_cash_usdt"] = equity - unrealised
        row["positions_truncated"] = False
    if holdings is not None:
        row["positions"] = holdings
    return row


def _holding(qty: float, *, sleeve: str | None = "long", symbol: str = "BTCUSDT") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "side": "long" if qty > 0 else "short",
        "qty": abs(qty),
        "entry_px": 100.0,
        "mark_px": 101.0,
        "strategy": sleeve,
    }


def _write_day(
    root: Path,
    *,
    close: bool = True,
    wal_close: bool | None = None,
    extra_cash: tuple[tuple[str, str, int, str, str, str], ...] = (),
    capture_end: int = CAPTURE_END_MS,
    drop_transactions: tuple[str, ...] = (),
    retype: dict[str, str] | None = None,
    end_sample: bool = True,
    begin_positions: int = 0,
    end_positions: int = 0,
    transfer_sources: bool = True,
    rotation: bool = False,
    wal_tail: bool = True,
    unrealised: float | None = None,
    end_unrealised: float | None = None,
    begin_holdings: list[dict[str, Any]] | None = None,
    end_holdings: list[dict[str, Any]] | None = None,
    end_unattributed: int = 0,
) -> dict[str, Path]:
    wal = root / "engine.wal"
    records = _wal_records(
        close=close if wal_close is None else wal_close, rotation=rotation, tail=wal_tail
    )
    body = WAL_MAGIC + b"".join(_frame(record) for record in records)
    (root / "engine.wal.000064" if rotation else wal).write_bytes(body)

    ledger = _ledger(close=close, extra=extra_cash)
    rows = [
        row
        for row in _capture_rows(close=close, ledger=ledger)
        if row.get("id") not in drop_transactions
        # A capture predating the transfer sources holds no transfer row either.
        and (transfer_sources or row["_kind"] not in CAPTURE_TRANSFER_SOURCES)
    ]
    for row in rows:
        if retype and row.get("id") in retype:
            row["type"] = retype[row["id"]]
    capture = root / "history.jsonl"
    with capture.open("w", encoding="utf-8") as handle:
        for row in [_manifest(rows, capture_end, transfer_sources=transfer_sources), *rows]:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    samples = root / "equity"
    samples.mkdir()
    lines = [
        _sample(
            START_MS + 20_000,
            _equity_at(ledger, BEGIN_READING_MS),
            positions=begin_positions,
            unrealised=unrealised,
            holdings=begin_holdings,
        )
    ]
    if end_sample:
        lines.append(
            _sample(
                END_MS + 20_000,
                _equity_at(ledger, END_READING_MS),
                positions=end_positions,
                unrealised=unrealised if end_unrealised is None else end_unrealised,
                holdings=end_holdings,
                unattributed=end_unattributed,
            )
        )
    (samples / "engine-mainnet-2026-09.jsonl").write_text(
        "".join(json.dumps(line, sort_keys=True) + "\n" for line in lines), encoding="utf-8"
    )
    return {"wal": wal, "capture": capture, "samples": samples, "out": root / "report.json"}


def _run(paths: dict[str, Path], **kwargs: Any) -> dict[str, Any]:
    return reconcile_day(
        realm="mainnet",
        day=DAY,
        wal_path=paths["wal"],
        capture_path=paths["capture"],
        samples_dir=paths["samples"],
        **kwargs,
    )


def test_a_complete_day_passes_with_a_zero_residual(tmp_path: Path) -> None:
    report = _run(_write_day(tmp_path))

    assert report["gate"] == "pass", report["gate_reasons"]
    assert report["gate_reasons"] == []
    assert report["missing_requirements"] == []
    assert Decimal(report["residuals"]["equity_residual_usdt"]) == 0
    assert Decimal(report["residuals"]["cash_residual_usdt"]) == 0
    assert report["residuals"]["equity_residual_is_decomposable"] is True
    assert Decimal(report["residuals"]["equity_change_usdt"]) == Decimal("25.29")
    assert Decimal(report["transactions"]["change_total_usdt"]) == Decimal("25.29")
    assert Decimal(report["transactions"]["components"]["fees_usdt"]) == Decimal("0.21")
    assert Decimal(report["transactions"]["components"]["funding_usdt"]) == Decimal("0.50")
    assert Decimal(report["transactions"]["buckets"]["transfers"]) == Decimal("5")
    assert Decimal(report["transactions"]["buckets"]["unrecognised"]) == 0
    assert Decimal(report["fills"]["wal_fee_usdt"]) == Decimal("0.21")
    assert Decimal(report["fills"]["venue_execution_fee_usdt"]) == Decimal("0.21")
    assert report["positions"]["differences"] == []
    assert report["identities"]["capture_user_id"] == USER_ID
    assert report["identities"]["boundary_sample_user_ids"] == [USER_ID]
    assert len(report["identities"]["wal_segments"]) == 1
    long_sleeve = report["sleeve_attribution"]["sleeves"]["long"]
    assert len(long_sleeve["closed_trades_in_day"]) == 1
    assert Decimal(long_sleeve["realised_net_usdt_closed_in_day"]) == Decimal("19.79")


def test_the_boundary_samples_are_selected_with_their_distance_from_midnight(tmp_path: Path) -> None:
    report = _run(_write_day(tmp_path))

    begin = report["boundaries"]["begin"]["selected"]
    end = report["boundaries"]["end"]["selected"]
    assert begin["sample_ts_utc"] == "2026-09-08T00:00:20.000Z"
    assert Decimal(begin["sample_distance_s"]) == 20
    assert Decimal(begin["venue_reading_distance_s"]) == 19
    assert end["sample_ts_utc"] == "2026-09-09T00:00:20.000Z"
    assert begin["sample_file"].endswith("engine-mainnet-2026-09.jsonl")
    assert report["scope"]["boundary_fields_read"][0] == "ts_ms"
    table = render_table(report)
    assert "2026-09-08T00:00:20.000Z" in table and "gate PASS" in table


def test_a_withheld_funding_row_leaves_its_amount_as_the_residual_and_fails(tmp_path: Path) -> None:
    report = _run(_write_day(tmp_path, drop_transactions=("t-fund",)))

    assert report["gate"] == "fail"
    assert Decimal(report["residuals"]["equity_residual_usdt"]) == Decimal("0.50")
    assert Decimal(report["residuals"]["cash_residual_usdt"]) == Decimal("0.50")
    assert Decimal(report["wallet_cash_chain"]["gap_total_usdt"]) == Decimal("0.50")
    assert [gap["transaction_id"] for gap in report["wallet_cash_chain"]["gaps"]] == ["t-close"]
    assert Decimal(report["transactions"]["components"]["funding_usdt"]) == 0
    assert any("0.50 USDT before transaction t-close" in reason for reason in report["gate_reasons"])
    assert any("equity residual 0.50 USDT, 0.50 USDT after" in r for r in report["gate_reasons"])


def test_a_withheld_transfer_row_leaves_its_amount_as_the_residual_and_fails(tmp_path: Path) -> None:
    report = _run(_write_day(tmp_path, drop_transactions=("t-transfer",)))

    assert report["gate"] == "fail"
    assert Decimal(report["residuals"]["equity_residual_usdt"]) == Decimal("5")
    assert Decimal(report["transactions"]["buckets"]["transfers"]) == 0
    assert report["wallet_cash_chain"]["gaps"] == []
    assert Decimal(report["wallet_cash_chain"]["cash_residual_usdt"]) == 0
    assert any("equity residual 5.00 USDT, 5.00 USDT after" in r for r in report["gate_reasons"])


def test_a_withheld_trade_cash_row_breaks_the_chain_and_the_fee_sums(tmp_path: Path) -> None:
    report = _run(_write_day(tmp_path, drop_transactions=("t-close",)))

    assert report["gate"] == "fail"
    assert Decimal(report["residuals"]["equity_residual_usdt"]) == Decimal("19.89")
    assert Decimal(report["wallet_cash_chain"]["gap_total_usdt"]) == Decimal("19.89")
    assert [gap["transaction_id"] for gap in report["wallet_cash_chain"]["gaps"]] == ["t-transfer"]
    assert Decimal(report["fills"]["transaction_log_trade_fee_usdt"]) == Decimal("0.10")
    assert any("transaction-log TRADE rows 0.10" in reason for reason in report["gate_reasons"])


def test_a_fill_the_wal_never_recorded_is_named_on_both_sides(tmp_path: Path) -> None:
    report = _run(_write_day(tmp_path, wal_close=False))

    assert report["gate"] == "fail"
    assert report["fills"]["venue_only_execution_ids"] == ["exec-exit"]
    assert report["fills"]["wal_only_execution_ids"] == []
    assert any("exec-exit inside the day has no WAL fill" in reason for reason in report["gate_reasons"])
    assert any("day fee sums differ: WAL 0.1, venue executions 0.21" in r for r in report["gate_reasons"])
    assert report["positions"]["boundaries"]["end"]["wal_position_count"] == 1


def test_a_cash_row_after_midnight_but_before_the_end_reading_is_listed_and_corrected(tmp_path: Path) -> None:
    paths = _write_day(tmp_path, extra_cash=(("t-next-fund", "SETTLEMENT", END_MS + 5_000, "0", "0.20", "0"),))

    report = _run(paths)

    straddle = report["residuals"]["boundary_straddle"]["end"]
    assert [row["transaction_id"] for row in straddle["rows"]] == ["t-next-fund"]
    assert Decimal(straddle["signed_change_usdt"]) == Decimal("0.20")
    assert Decimal(report["residuals"]["equity_residual_usdt"]) == Decimal("0.20")
    assert Decimal(report["residuals"]["equity_residual_less_boundary_straddle_usdt"]) == 0
    assert report["gate"] == "pass", report["gate_reasons"]
    assert "t-next-fund" in render_table(report)


def test_a_cash_row_after_midnight_but_before_the_begin_reading_is_listed_and_corrected(tmp_path: Path) -> None:
    paths = _write_day(tmp_path, extra_cash=(("t-day-fund", "SETTLEMENT", START_MS + 5_000, "0", "0.30", "0"),))

    report = _run(paths)

    straddle = report["residuals"]["boundary_straddle"]["begin"]
    assert [row["transaction_id"] for row in straddle["rows"]] == ["t-day-fund"]
    assert Decimal(straddle["signed_change_usdt"]) == Decimal("0.30")
    assert Decimal(report["residuals"]["equity_residual_usdt"]) == Decimal("-0.30")
    assert Decimal(report["residuals"]["boundary_straddle_correction_usdt"]) == Decimal("-0.30")
    assert Decimal(report["residuals"]["equity_residual_less_boundary_straddle_usdt"]) == 0
    assert Decimal(report["wallet_cash_chain"]["cash_residual_usdt"]) == 0
    assert report["gate"] == "pass", report["gate_reasons"]


def test_a_capture_stopping_at_midnight_cannot_see_the_end_straddle(tmp_path: Path) -> None:
    report = _run(_write_day(tmp_path, capture_end=END_MS))

    assert report["gate"] == "fail"
    assert report["residuals"]["boundary_straddle"]["end"]["interval_inside_capture"] is False
    assert any(
        requirement.startswith("end boundary straddle coverage: the capture does not cover")
        for requirement in report["missing_requirements"]
    )


def test_a_missing_end_boundary_names_the_missing_requirement(tmp_path: Path) -> None:
    report = _run(_write_day(tmp_path, end_sample=False))

    assert report["gate"] == "fail"
    assert report["boundaries"]["end"]["selected"] is None
    assert report["residuals"]["equity_residual_usdt"] is None
    assert any(
        requirement.startswith("end boundary observation at 2026-09-09T00:00:00.000Z")
        for requirement in report["missing_requirements"]
    )
    assert "both boundary equity readings, to compute the day's equity residual" in report["missing_requirements"]


def test_a_boundary_sample_too_far_from_midnight_names_the_missing_requirement(tmp_path: Path) -> None:
    report = _run(_write_day(tmp_path), max_boundary_distance_s=10)

    assert report["gate"] == "fail"
    assert any("boundary observation within 10 s" in requirement for requirement in report["missing_requirements"])


def test_an_unknown_transaction_type_is_listed_and_fails(tmp_path: Path) -> None:
    report = _run(_write_day(tmp_path, retype={"t-transfer": "SPOOKY_ADJUSTMENT"}))

    assert report["gate"] == "fail"
    assert report["transactions"]["unrecognised_types"] == [
        {"type": "SPOOKY_ADJUSTMENT", "rows": 1, "change_usdt": "5.00"}
    ]
    assert Decimal(report["transactions"]["buckets"]["unrecognised"]) == Decimal("5")
    assert Decimal(report["residuals"]["equity_residual_usdt"]) == 0
    assert any("SPOOKY_ADJUSTMENT is not one this reconciliation buckets" in r for r in report["gate_reasons"])


def test_a_position_the_boundary_sample_does_not_hold_is_listed_and_fails(tmp_path: Path) -> None:
    report = _run(_write_day(tmp_path, close=False))

    end = report["positions"]["boundaries"]["end"]
    assert end["wal_position_count"] == 1
    assert end["sample_position_count"] == 0
    assert [row["symbol"] for row in end["wal_positions"]] == ["BTCUSDT"]
    assert Decimal(end["wal_entry_notional_usdt"]) == Decimal("200")
    kinds = {(difference["kind"], difference["boundary"]) for difference in report["positions"]["differences"]}
    assert kinds == {("physical_position_count", "end"), ("sleeve_position_count", "end")}
    assert report["gate"] == "fail"
    assert any("BTCUSDT" in reason and "end boundary positions differ" in reason for reason in report["gate_reasons"])
    assert Decimal(report["residuals"]["equity_residual_usdt"]) == 0


def test_an_open_boundary_position_keeps_the_residual_and_refuses_to_split_it(tmp_path: Path) -> None:
    report = _run(_write_day(tmp_path, close=False, end_positions=1))

    assert report["gate"] == "fail"
    assert report["residuals"]["equity_residual_is_decomposable"] is False
    assert Decimal(report["residuals"]["equity_residual_usdt"]) == 0
    assert any(
        requirement.startswith("boundary unrealised P&L: the end boundary sample")
        for requirement in report["missing_requirements"]
    )


SAME_MS = START_MS + 14_400_000
SCRAMBLED_GROUP = (
    ("t-s3", "SETTLEMENT", SAME_MS, "0", "0.07", "0"),
    ("t-s1", "SETTLEMENT", SAME_MS, "0", "-0.05", "0"),
    ("t-s2", "SETTLEMENT", SAME_MS, "0", "-0.02", "0"),
)


def test_rows_sharing_one_timestamp_are_chained_not_sorted(tmp_path: Path) -> None:
    report = _run(_write_day(tmp_path, extra_cash=SCRAMBLED_GROUP))

    chain = report["wallet_cash_chain"]
    assert chain["gaps"] == []
    assert Decimal(chain["cash_residual_usdt"]) == 0
    assert chain["equal_timestamp_groups_walked"] == 1
    assert report["gate"] == "pass", report["gate_reasons"]


def test_a_capture_predating_the_transfer_sources_names_them_unfetched(tmp_path: Path) -> None:
    paths = _write_day(
        tmp_path,
        transfer_sources=False,
        extra_cash=(("t-late-fund", "SETTLEMENT", TRANSFER_MS + 600_000, "0", "0.40", "0"),),
    )

    report = _run(paths)

    assert report["gate"] == "fail"
    assert report["identities"]["capture_unfetched_transfer_sources"] == list(CAPTURE_TRANSFER_SOURCES)
    assert report["identities"]["capture_rows"]["transfer_in"] == 0
    assert Decimal(report["transactions"]["buckets"]["transfers"]) == 0
    unfetched = [r for r in report["gate_reasons"] if "venue transfer rows" in r]
    assert len(unfetched) == 1
    assert "5.00 USDT" in unfetched[0] and "unfetched, not zero" in unfetched[0]
    assert not any("wallet-cash chain jumps" in reason for reason in report["gate_reasons"])
    assert not any("wallet-cash residual" in reason for reason in report["gate_reasons"])
    assert [gap["transaction_id"] for gap in report["wallet_cash_chain"]["gaps"]] == ["t-late-fund"]
    assert Decimal(report["wallet_cash_chain"]["cash_residual_usdt"]) == 5
    assert not any("does not name exactly the required sources" in r for r in report["gate_reasons"])


def test_a_captured_transfer_out_row_sums_and_chains(tmp_path: Path) -> None:
    paths = _write_day(
        tmp_path, extra_cash=(("t-out", "TRANSFER_OUT", START_MS + 40_000_000, "-3", "0", "0"),)
    )

    report = _run(paths)

    assert report["gate"] == "pass", report["gate_reasons"]
    assert report["identities"]["capture_unfetched_transfer_sources"] == []
    assert report["identities"]["capture_rows"] == {
        "execution": 3,
        "closed_pnl": 1,
        "transaction": 3,
        "transfer_in": 1,
        "transfer_out": 1,
    }
    assert Decimal(report["transactions"]["buckets"]["transfers"]) == 2
    assert report["wallet_cash_chain"]["gaps"] == []


def test_a_wal_copy_that_starts_at_a_rotation_segment_folds_from_its_restatement(tmp_path: Path) -> None:
    report = _run(_write_day(tmp_path, rotation=True, begin_positions=1, end_positions=1))

    coverage = report["identities"]["wal_coverage"]
    assert coverage["segment_indices"] == [64]
    assert coverage["complete_family"] is False
    assert coverage["covers_begin_boundary_reading"] is True
    assert coverage["covers_end_boundary_reading"] is True
    assert coverage["base_restatement"]["segment"] == 64
    assert coverage["base_restatement"]["attributed_positions"] == 1
    assert coverage["base_restatement"]["logged_positions"] == 1
    assert not any("complete WAL segment family" in reason for reason in report["gate_reasons"])
    begin = report["positions"]["boundaries"]["begin"]
    assert begin["wal_position_count"] == 1
    assert begin["wal_positions"] == [
        {
            "symbol": "BTCUSDT",
            "signed_qty": "2.0",
            "entry_notional_usdt": None,
            "sleeves": ["long"],
        }
    ]
    assert begin["wal_positions_without_an_entry_price"] == ["BTCUSDT"]
    assert Decimal(begin["wal_entry_notional_usdt"]) == 0
    assert report["positions"]["differences"] == []
    assert "unpriced" in render_table(report)


def test_a_wal_copy_ending_before_the_end_boundary_reading_names_the_coverage_requirement(
    tmp_path: Path,
) -> None:
    report = _run(_write_day(tmp_path, rotation=True, wal_tail=False, begin_positions=1, end_positions=1))

    assert report["gate"] == "fail"
    assert report["identities"]["wal_coverage"]["covers_end_boundary_reading"] is False
    assert report["identities"]["wal_coverage"]["covers_begin_boundary_reading"] is True
    assert any(
        requirement.startswith("WAL coverage of the end boundary reading at 2026-09-09T00:00:19.000Z")
        for requirement in report["missing_requirements"]
    )


def test_a_rotation_copy_without_a_restatement_is_not_a_trusted_segment(tmp_path: Path) -> None:
    paths = _write_day(tmp_path)
    (tmp_path / "engine.wal").rename(tmp_path / "engine.wal.000064")

    report = _run(paths)

    coverage = report["identities"]["wal_coverage"]
    assert report["gate"] == "fail"
    assert coverage["segment_indices"] == []
    assert coverage["base_restatement"] is None
    assert any("ignored untrusted rotation segment" in note for note in coverage["notes"])
    assert "at least one trusted WAL segment in the copied family" in report["missing_requirements"]


def test_boundary_wallet_cash_splits_the_residual_over_an_open_position(tmp_path: Path) -> None:
    report = _run(_write_day(tmp_path, close=False, end_positions=1, unrealised=2.0))

    decomposition = report["residuals"]["decomposition"]
    assert decomposition["established"] is True
    assert Decimal(decomposition["wallet_cash_change_usdt"]) == Decimal("5.40")
    assert Decimal(decomposition["unrealised_pnl_change_usdt"]) == 0
    assert Decimal(decomposition["wallet_cash_residual_usdt"]) == 0
    assert Decimal(decomposition["identity_residual_usdt"]) == 0
    assert report["residuals"]["equity_residual_is_decomposable"] is True
    assert report["residuals"]["boundary_fields_used"][-2:] == ["wallet_cash_usdt", "unrealised_pnl_usdt"]
    assert not any("unrealised P&L" in requirement for requirement in report["missing_requirements"])
    assert report["scope"]["boundary_fields_absent"] == [
        "per-symbol positions: without the positions list the sample carries position_count, "
        "position_entry_notional_usdt and sleeve_positions counts, not the symbols, sides, "
        "quantities or entry prices"
    ]
    assert report["gate"] == "pass", report["gate_reasons"]


def test_a_boundary_wallet_cash_the_venue_rows_do_not_explain_fails(tmp_path: Path) -> None:
    report = _run(_write_day(tmp_path, close=False, end_positions=1, unrealised=2.0, end_unrealised=3.0))

    assert report["gate"] == "fail"
    assert Decimal(report["residuals"]["decomposition"]["wallet_cash_residual_usdt"]) == Decimal("-1")
    assert any(
        reason.startswith("boundary wallet-cash residual -1.00 USDT") for reason in report["gate_reasons"]
    )


def test_boundary_positions_gate_per_symbol_and_leave_the_owners_own_alone(tmp_path: Path) -> None:
    matched = _run(
        _write_day(
            tmp_path,
            close=False,
            end_positions=1,
            unrealised=2.0,
            begin_holdings=[],
            end_holdings=[_holding(2.0)],
        )
    )
    assert matched["gate"] == "pass", matched["gate_reasons"]
    assert matched["scope"]["boundary_fields_absent"] == []

    differing = tmp_path / "differing"
    differing.mkdir()
    report = _run(
        _write_day(
            differing,
            close=False,
            end_positions=2,
            end_unattributed=1,
            unrealised=2.0,
            begin_holdings=[],
            end_holdings=[_holding(3.0), _holding(5.0, sleeve=None, symbol="ETHUSDT")],
        )
    )

    symbols = {
        difference["symbol"]: difference
        for difference in report["positions"]["differences"]
        if difference["kind"] == "symbol_position"
    }
    assert symbols["BTCUSDT"]["gating"] is True
    assert symbols["BTCUSDT"]["sample_signed_qty"] == "3.0"
    assert symbols["BTCUSDT"]["wal_signed_qty"] == "2.0"
    assert symbols["ETHUSDT"]["gating"] is False
    assert symbols["ETHUSDT"]["sample_calls_it_hand_held"] is True
    assert report["gate"] == "fail"
    assert any('"symbol": "BTCUSDT"' in reason for reason in report["gate_reasons"])
    assert not any("ETHUSDT" in reason for reason in report["gate_reasons"])


def _argv(paths: dict[str, Path]) -> list[str]:
    return [
        "--realm", "mainnet",
        "--day", DAY,
        "--wal", str(paths["wal"]),
        "--capture", str(paths["capture"]),
        "--equity-samples", str(paths["samples"]),
        "--out", str(paths["out"]),
    ]


def test_the_cli_writes_a_mode_0600_report_and_table_and_exits_on_the_gate(tmp_path: Path) -> None:
    paths = _write_day(tmp_path)

    assert main(_argv(paths)) == 0
    table = paths["out"].with_suffix(".txt")
    for path in (paths["out"], table):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(paths["out"].read_text(encoding="utf-8"))["gate"] == "pass"
    assert "day reconciliation realm=mainnet" in table.read_text(encoding="utf-8")

    second = tmp_path / "second"
    second.mkdir()
    assert main(_argv(_write_day(second, drop_transactions=("t-fund",)))) == 1
