"""Independent local-vs-Bybit clock-offset evidence.

The public server-time endpoint is sampled with local wall/monotonic timestamps.
The estimator uses the lowest-RTT observations and records a conservative error
bound; it does not infer matching-engine time from an HTTP response.
"""

from __future__ import annotations

import hashlib
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .deterministic_serialization import canonical_json


CLOCK_OFFSET_SCHEMA_VERSION = 2
CLOCK_OFFSET_SOURCE = "bybit_public_server_time_persistent_low_rtt_midpoint_v2"
CLOCK_OFFSET_ENDPOINT = "https://api-demo.bybit.com/v5/market/time"
CLOCK_OFFSET_TRANSPORT = "single_preconnected_tls_session_http11"
REGISTERED_SAMPLE_COUNT = 21
REGISTERED_SELECTED_COUNT = 5
REGISTERED_MAX_RTT_NS = 250_000_000
REGISTERED_MAX_ERROR_NS = 100_000_000
REGISTERED_MAX_AGE_HOURS = 24.0
_RECEIPT_FIELDS = frozenset({
    "schema_version",
    "source",
    "endpoint",
    "transport",
    "observed_ts_ns",
    "ntp_synchronized",
    "sample_count",
    "selected_count",
    "selection_method",
    "local_minus_exchange_ns",
    "estimated_max_error_ns",
    "max_selected_rtt_ns",
    "registered_max_rtt_ns",
    "registered_max_error_ns",
    "selected_sample_indexes",
    "samples",
    "clock_offset_gate_passed",
    "artifact_sha256",
})
_SAMPLE_FIELDS = frozenset({
    "sample_index",
    "start_wall_ts_ns",
    "end_wall_ts_ns",
    "rtt_ns",
    "exchange_ts_ns",
    "local_midpoint_ts_ns",
    "local_minus_exchange_ns",
})


def _response_server_ns(payload: Mapping[str, Any]) -> int:
    if int(payload.get("retCode", -1)) != 0:
        raise ValueError(f"Bybit server-time request failed: {payload.get('retMsg')!r}")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("Bybit server-time response lacks result")
    server_ns = int(result.get("timeNano") or 0)
    if server_ns <= 0:
        raise ValueError("Bybit server-time response lacks timeNano")
    top_level_ms = int(payload.get("time") or 0)
    if top_level_ms <= 0 or abs(server_ns // 1_000_000 - top_level_ms) > 2:
        raise ValueError("Bybit server-time fields disagree")
    return server_ns


def capture_clock_offset(
    *,
    request_once: Callable[[], bytes],
    ntp_synchronized: bool,
    endpoint: str,
    transport: str = CLOCK_OFFSET_TRANSPORT,
    sample_count: int = 21,
    selected_count: int = 5,
    interval_seconds: float = 0.05,
    max_rtt_ns: int = 250_000_000,
    max_error_ns: int = 50_000_000,
    wall_time_ns: Callable[[], int] = time.time_ns,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Capture a self-hashed offset receipt from an injected public request."""

    if sample_count < 5 or selected_count < 3 or selected_count > sample_count:
        raise ValueError("clock offset requires at least five samples and three selected")
    if interval_seconds < 0.0 or max_rtt_ns <= 0 or max_error_ns <= 0:
        raise ValueError("clock offset timing bounds are invalid")
    if not endpoint.startswith("https://"):
        raise ValueError("clock offset endpoint must use HTTPS")
    if not transport:
        raise ValueError("clock offset transport identity is required")

    samples: list[dict[str, int]] = []
    for index in range(sample_count):
        start_wall_ns = wall_time_ns()
        start_monotonic_ns = monotonic_ns()
        raw = request_once()
        end_monotonic_ns = monotonic_ns()
        end_wall_ns = wall_time_ns()
        rtt_ns = end_monotonic_ns - start_monotonic_ns
        if rtt_ns <= 0:
            raise ValueError("clock offset sample has non-positive monotonic RTT")
        wall_elapsed_ns = end_wall_ns - start_wall_ns
        if wall_elapsed_ns <= 0 or abs(wall_elapsed_ns - rtt_ns) > 5_000_000:
            raise ValueError("local wall and monotonic clocks moved inconsistently")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid Bybit server-time JSON") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("Bybit server-time response must be an object")
        exchange_ns = _response_server_ns(payload)
        local_midpoint_ns = start_wall_ns + wall_elapsed_ns // 2
        samples.append({
            "sample_index": index,
            "start_wall_ts_ns": start_wall_ns,
            "end_wall_ts_ns": end_wall_ns,
            "rtt_ns": rtt_ns,
            "exchange_ts_ns": exchange_ns,
            "local_midpoint_ts_ns": local_midpoint_ns,
            "local_minus_exchange_ns": local_midpoint_ns - exchange_ns,
        })
        if index + 1 < sample_count and interval_seconds:
            sleeper(interval_seconds)

    selected = sorted(samples, key=lambda row: (row["rtt_ns"], row["sample_index"]))[:selected_count]
    correction_ns = int(round(statistics.median(
        row["local_minus_exchange_ns"] for row in selected
    )))
    error_bound_ns = max(
        abs(row["local_minus_exchange_ns"] - correction_ns) + row["rtt_ns"] // 2
        for row in selected
    )
    observed_ts_ns = max(row["end_wall_ts_ns"] for row in samples)
    gate = (
        ntp_synchronized
        and all(row["rtt_ns"] <= max_rtt_ns for row in selected)
        and error_bound_ns <= max_error_ns
    )
    receipt: dict[str, Any] = {
        "schema_version": CLOCK_OFFSET_SCHEMA_VERSION,
        "source": CLOCK_OFFSET_SOURCE,
        "endpoint": endpoint,
        "transport": transport,
        "observed_ts_ns": observed_ts_ns,
        "ntp_synchronized": ntp_synchronized,
        "sample_count": sample_count,
        "selected_count": selected_count,
        "selection_method": "lowest_rtt_then_median_midpoint_offset",
        "local_minus_exchange_ns": correction_ns,
        "estimated_max_error_ns": error_bound_ns,
        "max_selected_rtt_ns": max(row["rtt_ns"] for row in selected),
        "registered_max_rtt_ns": max_rtt_ns,
        "registered_max_error_ns": max_error_ns,
        "selected_sample_indexes": [row["sample_index"] for row in selected],
        "samples": samples,
        "clock_offset_gate_passed": gate,
        "artifact_sha256": "",
    }
    receipt["artifact_sha256"] = hashlib.sha256(canonical_json(receipt)).hexdigest()
    return receipt


def verify_clock_offset_receipt(
    payload: Mapping[str, Any],
    *,
    now_ns: int,
    max_age_hours: float,
    require_registered_contract: bool = True,
) -> int:
    """Verify identity, freshness, and the prospective quality gate."""

    if set(payload) != _RECEIPT_FIELDS:
        missing = sorted(_RECEIPT_FIELDS - set(payload))
        unknown = sorted(set(payload) - _RECEIPT_FIELDS)
        raise ValueError(
            f"clock-offset receipt fields are invalid; missing={missing}, unknown={unknown}"
        )
    if int(payload.get("schema_version") or 0) != CLOCK_OFFSET_SCHEMA_VERSION:
        raise ValueError("clock-offset receipt schema is unsupported")
    if payload.get("source") != CLOCK_OFFSET_SOURCE:
        raise ValueError("clock-offset receipt source is unsupported")
    if not isinstance(payload.get("endpoint"), str) or not str(payload["endpoint"]).startswith(
        "https://"
    ):
        raise ValueError("clock-offset receipt endpoint is invalid")
    if not isinstance(payload.get("transport"), str) or not payload["transport"]:
        raise ValueError("clock-offset receipt transport is invalid")
    if require_registered_contract:
        if payload["endpoint"] != CLOCK_OFFSET_ENDPOINT:
            raise ValueError("clock-offset receipt does not use the registered Bybit demo endpoint")
        if payload["transport"] != CLOCK_OFFSET_TRANSPORT:
            raise ValueError("clock-offset receipt does not use the registered persistent transport")
        if int(payload.get("sample_count") or 0) != REGISTERED_SAMPLE_COUNT:
            raise ValueError("clock-offset receipt does not contain the registered sample count")
        if int(payload.get("selected_count") or 0) != REGISTERED_SELECTED_COUNT:
            raise ValueError("clock-offset receipt does not contain the registered selected count")
        if int(payload.get("registered_max_rtt_ns") or 0) != REGISTERED_MAX_RTT_NS:
            raise ValueError("clock-offset receipt RTT bound differs from the registered contract")
        if int(payload.get("registered_max_error_ns") or 0) != REGISTERED_MAX_ERROR_NS:
            raise ValueError("clock-offset receipt error bound differs from the registered contract")
        if max_age_hours > REGISTERED_MAX_AGE_HOURS:
            raise ValueError("clock-offset receipt age allowance exceeds the registered contract")
    observed_ns = int(payload.get("observed_ts_ns") or 0)
    if observed_ns <= 0 or observed_ns > now_ns:
        raise ValueError("clock-offset receipt has an invalid observation time")
    if max_age_hours <= 0.0 or now_ns - observed_ns > max_age_hours * 3_600_000_000_000:
        raise ValueError("clock-offset receipt is stale")
    if payload.get("ntp_synchronized") is not True:
        raise ValueError("clock-offset receipt lacks NTP synchronization")
    if payload.get("clock_offset_gate_passed") is not True:
        raise ValueError("clock-offset receipt failed its registered quality gate")
    observed_hash = str(payload.get("artifact_sha256") or "")
    expected_hash = hashlib.sha256(
        canonical_json({**dict(payload), "artifact_sha256": ""})
    ).hexdigest()
    if observed_hash != expected_hash:
        raise ValueError("clock-offset receipt artifact hash is invalid")
    samples = payload.get("samples")
    if not isinstance(samples, list) or len(samples) != int(payload.get("sample_count") or 0):
        raise ValueError("clock-offset receipt sample count is invalid")
    selected = payload.get("selected_sample_indexes")
    if not isinstance(selected, list) or len(selected) != int(payload.get("selected_count") or 0):
        raise ValueError("clock-offset receipt selected sample count is invalid")
    normalized_samples: list[dict[str, int]] = []
    for expected_index, raw in enumerate(samples):
        if not isinstance(raw, Mapping):
            raise ValueError("clock-offset receipt contains a non-object sample")
        if set(raw) != _SAMPLE_FIELDS or any(type(raw.get(key)) is not int for key in _SAMPLE_FIELDS):
            raise ValueError("clock-offset receipt sample fields/types are invalid")
        row = {
            key: int(raw[key])
            for key in (
                "sample_index",
                "start_wall_ts_ns",
                "end_wall_ts_ns",
                "rtt_ns",
                "exchange_ts_ns",
                "local_midpoint_ts_ns",
                "local_minus_exchange_ns",
            )
        }
        if row["sample_index"] != expected_index or row["rtt_ns"] <= 0:
            raise ValueError("clock-offset receipt sample identity/RTT is invalid")
        wall_elapsed_ns = row["end_wall_ts_ns"] - row["start_wall_ts_ns"]
        if (
            row["start_wall_ts_ns"] <= 0
            or row["exchange_ts_ns"] <= 0
            or wall_elapsed_ns <= 0
            or abs(wall_elapsed_ns - row["rtt_ns"]) > 5_000_000
        ):
            raise ValueError("clock-offset receipt sample wall interval is invalid")
        expected_midpoint = (
            row["start_wall_ts_ns"]
            + (row["end_wall_ts_ns"] - row["start_wall_ts_ns"]) // 2
        )
        if row["local_midpoint_ts_ns"] != expected_midpoint:
            raise ValueError("clock-offset receipt midpoint is inconsistent")
        if row["local_minus_exchange_ns"] != expected_midpoint - row["exchange_ts_ns"]:
            raise ValueError("clock-offset receipt sample offset is inconsistent")
        normalized_samples.append(row)
    if observed_ns != max(row["end_wall_ts_ns"] for row in normalized_samples):
        raise ValueError("clock-offset receipt observation time is inconsistent")
    selected_count = int(payload.get("selected_count") or 0)
    expected_selected_rows = sorted(
        normalized_samples,
        key=lambda row: (row["rtt_ns"], row["sample_index"]),
    )[:selected_count]
    expected_selected = [row["sample_index"] for row in expected_selected_rows]
    if any(type(value) is not int for value in selected) or selected != expected_selected:
        raise ValueError("clock-offset receipt low-RTT selection is inconsistent")
    correction_ns = int(payload["local_minus_exchange_ns"])
    if abs(correction_ns) > 10_000_000_000:
        raise ValueError("clock-offset correction is implausibly large")
    expected_correction = int(round(statistics.median(
        row["local_minus_exchange_ns"] for row in expected_selected_rows
    )))
    expected_error = max(
        abs(row["local_minus_exchange_ns"] - expected_correction) + row["rtt_ns"] // 2
        for row in expected_selected_rows
    )
    expected_max_rtt = max(row["rtt_ns"] for row in expected_selected_rows)
    if (
        correction_ns != expected_correction
        or int(payload.get("estimated_max_error_ns") or 0) != expected_error
        or int(payload.get("max_selected_rtt_ns") or 0) != expected_max_rtt
    ):
        raise ValueError("clock-offset receipt estimator result is inconsistent")
    registered_max_rtt = int(payload.get("registered_max_rtt_ns") or 0)
    registered_max_error = int(payload.get("registered_max_error_ns") or 0)
    if registered_max_rtt <= 0 or registered_max_error <= 0:
        raise ValueError("clock-offset receipt registered bounds are invalid")
    expected_gate = (
        payload.get("ntp_synchronized") is True
        and expected_max_rtt <= registered_max_rtt
        and expected_error <= registered_max_error
    )
    if payload.get("clock_offset_gate_passed") is not expected_gate:
        raise ValueError("clock-offset receipt quality gate is inconsistent")
    return correction_ns


def write_clock_offset_receipt(path: str | Path, receipt: Mapping[str, Any]) -> Path:
    """Atomically create a private timing input without overwriting evidence."""

    output = Path(path).expanduser()
    if not output.is_absolute():
        raise ValueError("clock-offset receipt output must be absolute")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.{time.time_ns()}.tmp")
    data = canonical_json(dict(receipt)) + b"\n"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("clock-offset receipt write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary, output)
        except FileExistsError as exc:
            raise FileExistsError(
                f"clock-offset receipt already exists; preserve it: {output}"
            ) from exc
        directory = os.open(str(output.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return output
