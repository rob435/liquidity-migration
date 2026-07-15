from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

import pytest

from liquidity_migration import clock_offset_series as series_module
from liquidity_migration.clock_offset_receipt import (
    CLOCK_OFFSET_ENDPOINT,
    capture_clock_offset,
    write_clock_offset_receipt,
)
from liquidity_migration.clock_offset_series import (
    HOUR_NS,
    ClockOffsetInterpolator,
    build_clock_offset_series,
    load_clock_offset_series,
    write_clock_offset_series,
)
from liquidity_migration.deterministic_serialization import canonical_json


T0_NS = 1_900_000_000_000_000_000
T1_NS = T0_NS + 120 * HOUR_NS
BASE_OFFSET_NS = 80_000_000


def _registered_clock(path: Path, *, base_ns: int, offset_ns: int) -> Path:
    wall_values: list[int] = []
    monotonic_values: list[int] = []
    responses: list[bytes] = []
    for index in range(21):
        start = base_ns + index * 100_000_000
        end = start + 10_000_000
        midpoint = start + 5_000_000
        exchange = midpoint - offset_ns
        wall_values.extend((start, end))
        monotonic_values.extend(
            (index * 20_000_000, index * 20_000_000 + 10_000_000)
        )
        responses.append(
            json.dumps(
                {
                    "retCode": 0,
                    "result": {"timeNano": str(exchange)},
                    "time": exchange // 1_000_000,
                }
            ).encode()
        )

    def next_value(values: Iterator[int]) -> Any:
        return lambda: next(values)

    response_iter = iter(responses)
    receipt = capture_clock_offset(
        request_once=lambda: next(response_iter),
        ntp_synchronized=True,
        endpoint=CLOCK_OFFSET_ENDPOINT,
        sample_count=21,
        selected_count=5,
        interval_seconds=0.0,
        max_rtt_ns=250_000_000,
        max_error_ns=100_000_000,
        wall_time_ns=next_value(iter(wall_values)),
        monotonic_ns=next_value(iter(monotonic_values)),
    )
    return write_clock_offset_receipt(path.resolve(), receipt)


def _artifact_ref(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_bytes())
    return {
        "path": str(path),
        "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "artifact_sha256": str(payload["artifact_sha256"]),
    }


def _sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, list[Path]]:
    receipts = [
        _registered_clock(
            tmp_path / f"clock-{index:02d}.json",
            base_ns=T0_NS - 3 * HOUR_NS + index * 6 * HOUR_NS,
            offset_ns=BASE_OFFSET_NS + index * 1_000_000,
        )
        for index in range(22)
    ]
    freeze: dict[str, Any] = {
        "freeze_id": "clock-series-test-freeze",
        "window": {"t0_ns": T0_NS, "t1_ns": T1_NS},
        "clock": {"receipt": _artifact_ref(receipts[0])},
        "artifact_sha256": "",
    }
    freeze["artifact_sha256"] = hashlib.sha256(canonical_json(freeze)).hexdigest()
    freeze_path = (tmp_path / "freeze.json").resolve()
    freeze_path.write_bytes(canonical_json(freeze) + b"\n")
    freeze_path.chmod(0o600)

    def load_test_freeze(path: str | Path) -> dict[str, Any]:
        value = json.loads(Path(path).read_bytes())
        expected = hashlib.sha256(
            canonical_json({**value, "artifact_sha256": ""})
        ).hexdigest()
        if value.get("artifact_sha256") != expected:
            raise ValueError("test freeze hash mismatch")
        return value

    monkeypatch.setattr(
        series_module, "load_natural_cutover_freeze_manifest", load_test_freeze
    )
    return freeze_path, receipts


def test_series_reopens_sources_and_interpolates_with_nonhard_uncertainty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze, receipts = _sources(tmp_path, monkeypatch)
    payload = build_clock_offset_series(
        freeze_manifest_file=freeze,
        receipt_files=receipts,
        created_ts_ns=T1_NS + 4 * HOUR_NS,
    )

    assert payload["clock_offset_series_gate_passed"] is True
    assert payload["coverage"]["sample_count"] == 22
    assert payload["coverage"]["max_observed_gap_ns"] == 6 * HOUR_NS
    assert payload["coverage"]["t0_bracketed"] is True
    assert payload["coverage"]["t1_bracketed"] is True
    assert payload["contract"]["max_observed_gap_hours"] == 8
    assert payload["contract"]["uncertainty_is_hard_bound"] is False

    interpolator = ClockOffsetInterpolator(payload)
    left = payload["samples"][10]
    right = payload["samples"][11]
    midpoint = (left["observed_ts_ns"] + right["observed_ts_ns"]) // 2
    estimate = interpolator.estimate(midpoint)
    assert estimate.local_minus_exchange_ns == BASE_OFFSET_NS + 10_500_000
    assert estimate.estimated_uncertainty_ns == (
        max(left["estimated_max_error_ns"], right["estimated_max_error_ns"])
        + 1_000_000
    )
    assert estimate.uncertainty_is_hard_bound is False
    exact = interpolator.estimate(left["observed_ts_ns"])
    assert exact.exact_sample is True
    assert exact.estimated_uncertainty_ns == left["estimated_max_error_ns"]

    output = (tmp_path / "series.json").resolve()
    write_clock_offset_series(output, payload)
    assert output.stat().st_mode & 0o777 == 0o600
    assert load_clock_offset_series(output) == payload
    with pytest.raises(FileExistsError):
        write_clock_offset_series(output, payload)

    original = receipts[12].read_bytes()
    receipts[12].write_bytes(original.replace(b'"ntp_synchronized":true', b'"ntp_synchronized":false'))
    receipts[12].chmod(0o600)
    with pytest.raises(ValueError):
        load_clock_offset_series(output)


def test_series_rejects_reordering_gaps_and_missing_endpoint_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze, receipts = _sources(tmp_path, monkeypatch)

    reordered = list(receipts)
    reordered[5], reordered[6] = reordered[6], reordered[5]
    with pytest.raises(ValueError, match="strictly time ordered"):
        build_clock_offset_series(
            freeze_manifest_file=freeze,
            receipt_files=reordered,
            created_ts_ns=T1_NS + 4 * HOUR_NS,
        )

    with_gap = [path for index, path in enumerate(receipts) if index != 10]
    with pytest.raises(ValueError, match="eight-hour sample gap"):
        build_clock_offset_series(
            freeze_manifest_file=freeze,
            receipt_files=with_gap,
            created_ts_ns=T1_NS + 4 * HOUR_NS,
        )

    with pytest.raises(ValueError, match="at or after T1"):
        build_clock_offset_series(
            freeze_manifest_file=freeze,
            receipt_files=receipts[:-1],
            created_ts_ns=T1_NS + 4 * HOUR_NS,
        )
