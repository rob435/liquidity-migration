from __future__ import annotations

import json
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from liquidity_migration.clock_offset_receipt import (
    capture_clock_offset,
    verify_clock_offset_receipt,
    write_clock_offset_receipt,
)


def _next(values: Iterator[int]):
    return lambda: next(values)


def test_low_rtt_midpoint_receipt_is_self_hashed_and_private(tmp_path: Path) -> None:
    base = 1_800_000_000_000_000_000
    rtts = [20_000_000, 8_000_000, 12_000_000, 10_000_000, 30_000_000]
    offset = 3_000_000
    wall_values: list[int] = []
    mono_values: list[int] = []
    responses: list[bytes] = []
    elapsed = 0
    for index, rtt in enumerate(rtts):
        start = base + index * 100_000_000
        end = start + rtt
        midpoint = start + rtt // 2
        exchange = midpoint - offset
        wall_values.extend((start, end))
        mono_values.extend((elapsed, elapsed + rtt))
        elapsed += rtt + 1_000_000
        responses.append(json.dumps({
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "timeSecond": str(exchange // 1_000_000_000),
                "timeNano": str(exchange),
            },
            "time": exchange // 1_000_000,
        }).encode())

    response_iter = iter(responses)
    receipt = capture_clock_offset(
        request_once=lambda: next(response_iter),
        ntp_synchronized=True,
        endpoint="https://api-demo.bybit.com/v5/market/time",
        sample_count=5,
        selected_count=3,
        interval_seconds=0.0,
        max_rtt_ns=50_000_000,
        max_error_ns=10_000_000,
        wall_time_ns=_next(iter(wall_values)),
        monotonic_ns=_next(iter(mono_values)),
    )

    assert receipt["clock_offset_gate_passed"] is True
    assert receipt["local_minus_exchange_ns"] == offset
    assert receipt["selected_sample_indexes"] == [1, 3, 2]
    assert verify_clock_offset_receipt(
        receipt,
        now_ns=int(receipt["observed_ts_ns"]) + 1,
        max_age_hours=24.0,
        require_registered_contract=False,
    ) == offset

    path = write_clock_offset_receipt((tmp_path / "clock-offset.json").resolve(), receipt)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["artifact_sha256"] == receipt["artifact_sha256"]
    with pytest.raises(FileExistsError, match="preserve"):
        write_clock_offset_receipt(path, receipt)


def test_clock_receipt_tamper_and_failed_quality_gate_are_rejected() -> None:
    base = 1_800_000_000_000_000_000
    wall_values = [value for index in range(5) for value in (
        base + index * 100_000_000,
        base + index * 100_000_000 + 20_000_000,
    )]
    mono_values = [value for index in range(5) for value in (
        index * 30_000_000,
        index * 30_000_000 + 20_000_000,
    )]
    responses = iter(
        json.dumps({
            "retCode": 0,
            "result": {"timeNano": str(base + index * 100_000_000 + 10_000_000)},
            "time": (base + index * 100_000_000 + 10_000_000) // 1_000_000,
        }).encode()
        for index in range(5)
    )
    receipt = capture_clock_offset(
        request_once=lambda: next(responses),
        ntp_synchronized=False,
        endpoint="https://api-demo.bybit.com/v5/market/time",
        sample_count=5,
        selected_count=3,
        interval_seconds=0.0,
        wall_time_ns=_next(iter(wall_values)),
        monotonic_ns=_next(iter(mono_values)),
    )
    assert receipt["clock_offset_gate_passed"] is False
    with pytest.raises(ValueError, match="NTP|quality gate"):
        verify_clock_offset_receipt(
            receipt,
            now_ns=int(receipt["observed_ts_ns"]) + 1,
            max_age_hours=24.0,
            require_registered_contract=False,
        )

    tampered = {**receipt, "ntp_synchronized": True, "clock_offset_gate_passed": True}
    with pytest.raises(ValueError, match="hash"):
        verify_clock_offset_receipt(
            tampered,
            now_ns=int(receipt["observed_ts_ns"]) + 1,
            max_age_hours=24.0,
            require_registered_contract=False,
        )


def test_clock_receipt_registered_contract_cannot_be_weakened() -> None:
    base = 1_800_000_000_000_000_000
    wall_values = [value for index in range(5) for value in (
        base + index * 100_000_000,
        base + index * 100_000_000 + 10_000_000,
    )]
    mono_values = [value for index in range(5) for value in (
        index * 20_000_000,
        index * 20_000_000 + 10_000_000,
    )]
    responses = iter(
        json.dumps({
            "retCode": 0,
            "result": {"timeNano": str(base + index * 100_000_000 + 5_000_000)},
            "time": (base + index * 100_000_000 + 5_000_000) // 1_000_000,
        }).encode()
        for index in range(5)
    )
    receipt = capture_clock_offset(
        request_once=lambda: next(responses),
        ntp_synchronized=True,
        endpoint="https://api-demo.bybit.com/v5/market/time",
        sample_count=5,
        selected_count=3,
        interval_seconds=0.0,
        wall_time_ns=_next(iter(wall_values)),
        monotonic_ns=_next(iter(mono_values)),
    )

    with pytest.raises(ValueError, match="registered sample count"):
        verify_clock_offset_receipt(
            receipt,
            now_ns=int(receipt["observed_ts_ns"]) + 1,
            max_age_hours=24.0,
        )
