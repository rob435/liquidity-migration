"""Source-bound periodic local-vs-Bybit clock evidence for natural windows.

Each member is the existing registered, credential-free Bybit demo server-time
receipt.  The series binds the initial member frozen before T0, requires later
members to bracket the complete natural window, and caps the *observed* gap
between members.  Linear interpolation supplies a timestamp-specific point
estimate for feed-latency calculations; its uncertainty is explicitly an
estimate, not a hard bound on unobserved clock motion between samples.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import inspect
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .clock_offset_receipt import (
    REGISTERED_MAX_AGE_HOURS,
    verify_clock_offset_receipt,
)
from .deterministic_serialization import canonical_json
from .natural_cutover_freeze_manifest import (
    WINDOW_HOURS,
    load_natural_cutover_freeze_manifest,
)


CLOCK_OFFSET_SERIES_SCHEMA_VERSION = 1
CLOCK_OFFSET_SERIES_KIND = "bybit_demo_clock_offset_series"
CLOCK_OFFSET_SERIES_VALIDATOR = "bybit_demo_clock_offset_series_v1"
HOUR_NS = 60 * 60 * 1_000_000_000
TARGET_CADENCE_HOURS = 6
MAX_OBSERVED_GAP_HOURS = 8
MAX_ENDPOINT_DISTANCE_HOURS = 6
TARGET_CADENCE_NS = TARGET_CADENCE_HOURS * HOUR_NS
MAX_OBSERVED_GAP_NS = MAX_OBSERVED_GAP_HOURS * HOUR_NS
MAX_ENDPOINT_DISTANCE_NS = MAX_ENDPOINT_DISTANCE_HOURS * HOUR_NS
INTERPOLATION_METHOD = "piecewise_linear_local_minus_exchange_ns"
UNCERTAINTY_METHOD = (
    "max_bracketing_receipt_error_plus_absolute_bracketing_offset_change"
)
LIMITATIONS = (
    "public_server_time_is_not_matching_engine_time",
    "six_hour_target_cadence_with_two_hour_operational_allowance",
    "observed_eight_hour_gap_cap_is_not_a_clock_drift_rate_bound",
    "interpolated_uncertainty_is_an_estimate_not_a_hard_between_sample_bound",
    "series_grants_no_execution_or_deploy_authority",
)


@dataclass(frozen=True, slots=True)
class FileIdentity:
    label: str
    path: str
    size_bytes: int
    sha256: str
    device: int
    inode: int
    mtime_ns: int
    mode: int
    uid: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ClockCorrectionEstimate:
    timestamp_ns: int
    local_minus_exchange_ns: int
    estimated_uncertainty_ns: int
    interval_low_ns: int
    interval_high_ns: int
    left_sample_index: int
    right_sample_index: int
    left_observed_ts_ns: int
    right_observed_ts_ns: int
    left_distance_ns: int
    right_distance_ns: int
    bracket_gap_ns: int
    exact_sample: bool
    uncertainty_is_hard_bound: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ClockOffsetInterpolator:
    """In-memory evaluator for an already source-verified series payload."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        samples = payload.get("samples")
        window = payload.get("window")
        if not isinstance(samples, list) or not isinstance(window, Mapping):
            raise ValueError("clock-offset series lacks samples/window")
        if payload.get("clock_offset_series_gate_passed") is not True:
            raise ValueError("clock-offset series quality gate has not passed")
        self.t0_ns = _positive_int(window.get("t0_ns"), label="series T0")
        self.t1_ns = _positive_int(window.get("t1_ns"), label="series T1")
        self._observed: list[int] = []
        self._offsets: list[int] = []
        self._errors: list[int] = []
        for expected_index, raw in enumerate(samples):
            if not isinstance(raw, Mapping) or raw.get("sample_index") != expected_index:
                raise ValueError("clock-offset series sample identity is malformed")
            self._observed.append(
                _positive_int(raw.get("observed_ts_ns"), label="sample observation")
            )
            self._offsets.append(
                _int(raw.get("local_minus_exchange_ns"), label="sample correction")
            )
            self._errors.append(
                _nonnegative_int(
                    raw.get("estimated_max_error_ns"), label="sample error estimate"
                )
            )
        if len(self._observed) < 2 or any(
            right <= left for left, right in zip(self._observed, self._observed[1:])
        ):
            raise ValueError("clock-offset series observations are not strictly ordered")

    def estimate(self, timestamp_ns: int) -> ClockCorrectionEstimate:
        if type(timestamp_ns) is not int or not self.t0_ns <= timestamp_ns <= self.t1_ns:
            raise ValueError("clock correction timestamp falls outside [T0,T1]")
        right = bisect.bisect_left(self._observed, timestamp_ns)
        if right < len(self._observed) and self._observed[right] == timestamp_ns:
            correction = self._offsets[right]
            uncertainty = self._errors[right]
            return ClockCorrectionEstimate(
                timestamp_ns=timestamp_ns,
                local_minus_exchange_ns=correction,
                estimated_uncertainty_ns=uncertainty,
                interval_low_ns=correction - uncertainty,
                interval_high_ns=correction + uncertainty,
                left_sample_index=right,
                right_sample_index=right,
                left_observed_ts_ns=timestamp_ns,
                right_observed_ts_ns=timestamp_ns,
                left_distance_ns=0,
                right_distance_ns=0,
                bracket_gap_ns=0,
                exact_sample=True,
            )
        if right == 0 or right >= len(self._observed):
            raise ValueError("clock correction timestamp lacks bracketing samples")
        left = right - 1
        left_ts = self._observed[left]
        right_ts = self._observed[right]
        gap = right_ts - left_ts
        numerator = timestamp_ns - left_ts
        delta = self._offsets[right] - self._offsets[left]
        correction = self._offsets[left] + round(Fraction(delta * numerator, gap))
        # This deliberately covers both endpoint receipt errors plus all offset
        # motion observed across the bracket.  It remains only a sensitivity
        # estimate: an unsampled excursion can exceed both endpoints.
        uncertainty = max(self._errors[left], self._errors[right]) + abs(delta)
        return ClockCorrectionEstimate(
            timestamp_ns=timestamp_ns,
            local_minus_exchange_ns=correction,
            estimated_uncertainty_ns=uncertainty,
            interval_low_ns=correction - uncertainty,
            interval_high_ns=correction + uncertainty,
            left_sample_index=left,
            right_sample_index=right,
            left_observed_ts_ns=left_ts,
            right_observed_ts_ns=right_ts,
            left_distance_ns=timestamp_ns - left_ts,
            right_distance_ns=right_ts - timestamp_ns,
            bracket_gap_ns=gap,
            exact_sample=False,
        )


def _self_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json({**dict(payload), "artifact_sha256": ""})
    ).hexdigest()


def _strict_json(data: bytes, *, label: str) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in pairs:
            if key in output:
                raise ValueError(f"{label} contains duplicate JSON key {key!r}")
            output[key] = value
        return output

    try:
        parsed = json.loads(data, object_pairs_hook=object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return parsed


def _int(value: object, *, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return value


def _positive_int(value: object, *, label: str) -> int:
    output = _int(value, label=label)
    if output <= 0:
        raise ValueError(f"{label} must be positive")
    return output


def _nonnegative_int(value: object, *, label: str) -> int:
    output = _int(value, label=label)
    if output < 0:
        raise ValueError(f"{label} must be nonnegative")
    return output


def _lower_sha256(value: object, *, label: str) -> str:
    digest = str(value or "")
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    return digest


def _private_snapshot(path: str | Path, *, label: str) -> StableFileSnapshot:
    return read_stable_file(
        path,
        label=label,
        require_mode=0o600,
        require_owner=True,
        require_single_link=False,
    )


def _load_freeze_snapshot(
    path: str | Path,
    snapshot: StableFileSnapshot,
) -> dict[str, Any]:
    try:
        accepts_snapshot = "snapshot" in inspect.signature(
            load_natural_cutover_freeze_manifest
        ).parameters
    except (TypeError, ValueError):
        accepts_snapshot = False
    if accepts_snapshot:
        return load_natural_cutover_freeze_manifest(path, snapshot=snapshot)
    return load_natural_cutover_freeze_manifest(path)


def _read_identity(
    path: str | Path,
    *,
    label: str,
    snapshot: StableFileSnapshot | None = None,
) -> tuple[FileIdentity, bytes]:
    if snapshot is None:
        snapshot = _private_snapshot(path, label=label)
    elif snapshot.path != Path(path).expanduser().absolute():
        raise ValueError(f"{label} snapshot path differs")
    return (
        FileIdentity(
            label=label,
            path=str(snapshot.path),
            size_bytes=snapshot.size,
            sha256=snapshot.sha256,
            device=snapshot.device,
            inode=snapshot.inode,
            mtime_ns=snapshot.mtime_ns,
            mode=snapshot.mode,
            uid=snapshot.uid,
        ),
        snapshot.data,
    )


def _identity_matches(raw: object, identity: FileIdentity, *, label: str) -> None:
    if not isinstance(raw, Mapping) or dict(raw) != identity.to_dict():
        raise ValueError(f"{label} source identity changed")


def _window_from_freeze(freeze: Mapping[str, Any]) -> tuple[int, int]:
    window = freeze.get("window")
    if not isinstance(window, Mapping):
        raise ValueError("natural freeze lacks a window")
    t0_ns = _positive_int(window.get("t0_ns"), label="natural T0")
    t1_ns = _positive_int(window.get("t1_ns"), label="natural T1")
    if t1_ns - t0_ns != WINDOW_HOURS * HOUR_NS:
        raise ValueError("clock-offset series requires the registered 120-hour window")
    return t0_ns, t1_ns


def build_clock_offset_series(
    *,
    freeze_manifest_file: str | Path,
    receipt_files: Sequence[str | Path],
    created_ts_ns: int,
) -> dict[str, Any]:
    """Build a prospective periodic series from immutable registered receipts."""

    if type(created_ts_ns) is not int or created_ts_ns <= 0:
        raise ValueError("clock-offset series creation time must be positive")
    if len(receipt_files) < 2:
        raise ValueError("clock-offset series requires at least two receipts")
    freeze_snapshot = _private_snapshot(
        freeze_manifest_file,
        label="natural cutover freeze manifest",
    )
    freeze_identity, freeze_data = _read_identity(
        freeze_manifest_file,
        label="natural cutover freeze manifest",
        snapshot=freeze_snapshot,
    )
    freeze = _load_freeze_snapshot(
        freeze_identity.path,
        freeze_snapshot,
    )
    if _strict_json(freeze_data, label="natural cutover freeze manifest") != freeze:
        raise RuntimeError("natural cutover freeze changed while it was verified")
    t0_ns, t1_ns = _window_from_freeze(freeze)
    frozen_clock = freeze.get("clock")
    frozen_receipt = (
        frozen_clock.get("receipt") if isinstance(frozen_clock, Mapping) else None
    )
    if not isinstance(frozen_receipt, Mapping):
        raise ValueError("natural freeze lacks its initial clock receipt binding")

    identities: list[FileIdentity] = []
    payloads: list[dict[str, Any]] = []
    sample_rows: list[dict[str, Any]] = []
    observed_times: list[int] = []
    resolved_paths: set[str] = set()
    file_identities: set[tuple[int, int]] = set()
    artifact_hashes: set[str] = set()
    for index, path in enumerate(receipt_files):
        identity, data = _read_identity(path, label=f"clock receipt {index}")
        if identity.path in resolved_paths:
            raise ValueError("clock-offset series receipt paths must be distinct")
        if (identity.device, identity.inode) in file_identities:
            raise ValueError("clock-offset series receipts alias the same file identity")
        receipt = _strict_json(data, label=f"clock receipt {index}")
        observed_ns = _positive_int(
            receipt.get("observed_ts_ns"), label=f"clock receipt {index} observation"
        )
        correction_ns = verify_clock_offset_receipt(
            receipt,
            now_ns=observed_ns,
            max_age_hours=REGISTERED_MAX_AGE_HOURS,
            require_registered_contract=True,
        )
        artifact_hash = _lower_sha256(
            receipt.get("artifact_sha256"), label=f"clock receipt {index} artifact"
        )
        if artifact_hash in artifact_hashes:
            raise ValueError("clock-offset series receipt artifacts must be distinct")
        if observed_times and observed_ns <= observed_times[-1]:
            raise ValueError("clock-offset series receipts must be strictly time ordered")
        if observed_ns > created_ts_ns:
            raise ValueError("clock-offset series creation predates a member receipt")
        resolved_paths.add(identity.path)
        file_identities.add((identity.device, identity.inode))
        artifact_hashes.add(artifact_hash)
        identities.append(identity)
        payloads.append(receipt)
        observed_times.append(observed_ns)
        sample_rows.append(
            {
                "sample_index": index,
                "role": "frozen_initial" if index == 0 else "periodic_public_sample",
                "source_identity": identity.to_dict(),
                "receipt_artifact_sha256": artifact_hash,
                "observed_ts_ns": observed_ns,
                "local_minus_exchange_ns": correction_ns,
                "estimated_max_error_ns": _nonnegative_int(
                    receipt.get("estimated_max_error_ns"),
                    label=f"clock receipt {index} error estimate",
                ),
                "max_selected_rtt_ns": _positive_int(
                    receipt.get("max_selected_rtt_ns"),
                    label=f"clock receipt {index} selected RTT",
                ),
            }
        )

    expected_frozen = {
        "path": identities[0].path,
        "file_sha256": identities[0].sha256,
        "artifact_sha256": sample_rows[0]["receipt_artifact_sha256"],
    }
    if dict(frozen_receipt) != expected_frozen:
        raise ValueError("clock-offset series initial receipt differs from natural freeze")
    if observed_times[0] > t0_ns:
        raise ValueError("clock-offset series initial receipt does not cover T0 from the left")
    left_distance_ns = t0_ns - observed_times[0]
    if left_distance_ns > MAX_ENDPOINT_DISTANCE_NS:
        raise ValueError("clock-offset series initial receipt is more than six hours before T0")
    if observed_times[-1] < t1_ns:
        raise ValueError("clock-offset series lacks a receipt at or after T1")
    right_distance_ns = observed_times[-1] - t1_ns
    if right_distance_ns > MAX_ENDPOINT_DISTANCE_NS:
        raise ValueError("clock-offset series final receipt is more than six hours after T1")
    gaps = [right - left for left, right in zip(observed_times, observed_times[1:])]
    max_gap_ns = max(gaps)
    if max_gap_ns > MAX_OBSERVED_GAP_NS:
        raise ValueError("clock-offset series exceeds the registered eight-hour sample gap")
    if not any(t0_ns < observed_ns < t1_ns for observed_ns in observed_times[1:]):
        raise ValueError("clock-offset series lacks periodic observations inside (T0,T1)")

    gap_rows = [
        {
            "left_sample_index": index,
            "right_sample_index": index + 1,
            "left_observed_ts_ns": observed_times[index],
            "right_observed_ts_ns": observed_times[index + 1],
            "gap_ns": gap,
        }
        for index, gap in enumerate(gaps)
    ]
    payload: dict[str, Any] = {
        "schema_version": CLOCK_OFFSET_SERIES_SCHEMA_VERSION,
        "kind": CLOCK_OFFSET_SERIES_KIND,
        "validator": CLOCK_OFFSET_SERIES_VALIDATOR,
        "created_ts_ns": created_ts_ns,
        "capture_surface": "credential_free_public_bybit_demo_server_time",
        "execution_authorization": "not_granted",
        "freeze": {
            "source_identity": freeze_identity.to_dict(),
            "freeze_id": str(freeze.get("freeze_id") or ""),
            "artifact_sha256": _lower_sha256(
                freeze.get("artifact_sha256"), label="natural freeze artifact"
            ),
            "initial_clock_receipt": expected_frozen,
        },
        "window": {
            "t0_ns": t0_ns,
            "t1_ns": t1_ns,
            "hours": WINDOW_HOURS,
            "feed_interval": "half_open_[T0,T1)",
            "clock_sample_coverage": "closed_bracketing_[T0,T1]",
        },
        "contract": {
            "target_cadence_hours": TARGET_CADENCE_HOURS,
            "target_cadence_ns": TARGET_CADENCE_NS,
            "max_observed_gap_hours": MAX_OBSERVED_GAP_HOURS,
            "max_observed_gap_ns": MAX_OBSERVED_GAP_NS,
            "max_endpoint_distance_hours": MAX_ENDPOINT_DISTANCE_HOURS,
            "max_endpoint_distance_ns": MAX_ENDPOINT_DISTANCE_NS,
            "interpolation_method": INTERPOLATION_METHOD,
            "uncertainty_method": UNCERTAINTY_METHOD,
            "uncertainty_is_hard_bound": False,
        },
        "samples": sample_rows,
        "coverage": {
            "sample_count": len(sample_rows),
            "first_observed_ts_ns": observed_times[0],
            "last_observed_ts_ns": observed_times[-1],
            "left_endpoint_distance_ns": left_distance_ns,
            "right_endpoint_distance_ns": right_distance_ns,
            "max_observed_gap_ns": max_gap_ns,
            "t0_bracketed": True,
            "t1_bracketed": True,
            "gaps": gap_rows,
        },
        "clock_offset_series_gate_passed": True,
        "limitations": list(LIMITATIONS),
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)

    final_freeze_identity, final_freeze_data = _read_identity(
        freeze_identity.path, label="natural cutover freeze manifest"
    )
    if final_freeze_identity != freeze_identity or final_freeze_data != freeze_data:
        raise RuntimeError("natural cutover freeze mutated during series construction")
    for index, (identity, expected_payload) in enumerate(zip(identities, payloads)):
        final_identity, final_data = _read_identity(
            identity.path, label=f"clock receipt {index}"
        )
        if final_identity != identity or _strict_json(
            final_data, label=f"clock receipt {index}"
        ) != expected_payload:
            raise RuntimeError(f"clock receipt {index} mutated during series construction")
    return payload


def _precheck(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    if set(value) != {
        "schema_version",
        "kind",
        "validator",
        "created_ts_ns",
        "capture_surface",
        "execution_authorization",
        "freeze",
        "window",
        "contract",
        "samples",
        "coverage",
        "clock_offset_series_gate_passed",
        "limitations",
        "artifact_sha256",
    }:
        raise ValueError("clock-offset series has unexpected or missing fields")
    if (
        value.get("schema_version") != CLOCK_OFFSET_SERIES_SCHEMA_VERSION
        or value.get("kind") != CLOCK_OFFSET_SERIES_KIND
        or value.get("validator") != CLOCK_OFFSET_SERIES_VALIDATOR
        or value.get("capture_surface")
        != "credential_free_public_bybit_demo_server_time"
        or value.get("execution_authorization") != "not_granted"
        or value.get("clock_offset_series_gate_passed") is not True
        or value.get("limitations") != list(LIMITATIONS)
        or value.get("artifact_sha256") != _self_hash(value)
    ):
        raise ValueError("clock-offset series identity, gate, or self-hash is invalid")
    contract = value.get("contract")
    if not isinstance(contract, Mapping) or dict(contract) != {
        "target_cadence_hours": TARGET_CADENCE_HOURS,
        "target_cadence_ns": TARGET_CADENCE_NS,
        "max_observed_gap_hours": MAX_OBSERVED_GAP_HOURS,
        "max_observed_gap_ns": MAX_OBSERVED_GAP_NS,
        "max_endpoint_distance_hours": MAX_ENDPOINT_DISTANCE_HOURS,
        "max_endpoint_distance_ns": MAX_ENDPOINT_DISTANCE_NS,
        "interpolation_method": INTERPOLATION_METHOD,
        "uncertainty_method": UNCERTAINTY_METHOD,
        "uncertainty_is_hard_bound": False,
    }:
        raise ValueError("clock-offset series contract differs from the registered contract")
    return value


def verify_clock_offset_series(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Re-open the freeze and every member receipt, then reproduce exactly."""

    value = _precheck(payload)
    freeze = value.get("freeze")
    samples = value.get("samples")
    if not isinstance(freeze, Mapping) or not isinstance(samples, list):
        raise ValueError("clock-offset series source bindings are malformed")
    freeze_identity = freeze.get("source_identity")
    if not isinstance(freeze_identity, Mapping):
        raise ValueError("clock-offset series lacks freeze source identity")
    receipt_paths: list[str] = []
    for index, sample in enumerate(samples):
        if not isinstance(sample, Mapping):
            raise ValueError("clock-offset series contains a malformed sample")
        identity = sample.get("source_identity")
        if not isinstance(identity, Mapping):
            raise ValueError("clock-offset series sample lacks source identity")
        receipt_paths.append(str(identity.get("path") or ""))
    rebuilt = build_clock_offset_series(
        freeze_manifest_file=str(freeze_identity.get("path") or ""),
        receipt_files=receipt_paths,
        created_ts_ns=_positive_int(
            value.get("created_ts_ns"), label="clock-offset series creation time"
        ),
    )
    if canonical_json(rebuilt) != canonical_json(value):
        raise ValueError("clock-offset series does not reproduce from source receipts")
    return value


def _atomic_create(path: str | Path, payload: Mapping[str, Any]) -> Path:
    output = Path(path).expanduser()
    if not output.is_absolute():
        raise ValueError("clock-offset series output must be absolute")
    output.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json(dict(payload)) + b"\n"
    descriptor = os.open(str(output), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("clock-offset series write made no progress")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        output.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    directory = os.open(str(output.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return output


def write_clock_offset_series(path: str | Path, payload: Mapping[str, Any]) -> Path:
    value = verify_clock_offset_series(payload)
    return _atomic_create(path, value)


def load_clock_offset_series(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    if snapshot is None:
        snapshot = _private_snapshot(path, label="clock-offset series")
    elif snapshot.path != Path(path).expanduser().absolute():
        raise ValueError("clock-offset series snapshot path differs")
    payload = _strict_json(snapshot.data, label="clock-offset series")
    return verify_clock_offset_series(payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build or verify periodic public Bybit demo clock evidence"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--freeze-manifest", required=True)
    build.add_argument(
        "--clock-offset-receipt",
        action="append",
        required=True,
        help="repeat in strict observation-time order; first must be the frozen receipt",
    )
    build.add_argument("--output", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--series", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            payload = build_clock_offset_series(
                freeze_manifest_file=args.freeze_manifest,
                receipt_files=args.clock_offset_receipt,
                created_ts_ns=time.time_ns(),
            )
            output = write_clock_offset_series(args.output, payload)
        else:
            payload = load_clock_offset_series(args.series)
            output = Path(args.series).expanduser().absolute()
        print(
            json.dumps(
                {
                    "output": str(output),
                    "artifact_sha256": payload["artifact_sha256"],
                    "sample_count": payload["coverage"]["sample_count"],
                    "max_observed_gap_ns": payload["coverage"][
                        "max_observed_gap_ns"
                    ],
                    "clock_offset_series_gate_passed": payload[
                        "clock_offset_series_gate_passed"
                    ],
                    "execution_authorization": payload["execution_authorization"],
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        print(f"clock-offset series failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
