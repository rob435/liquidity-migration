#!/usr/bin/env python3
"""Convert a schema-4 candidate universe to schema 5, offline and exactly.

Schema 4 kept the venue's whole tradable instrument set under the retired
CONTINUOUS sleeve's name, and every producer read the union of the three
profiles. Schema 5 calls that set ``strategy_instruments`` and drops the
retired name. The symbols do not move: the two sleeve profiles are the
instrument set with extra gates switched on, so unioning them back in only
ever returned the instrument set.

Nothing here reads the venue. The new artifact is rebuilt from the raw
snapshot the old one already carries, at the same ``snapshot_ts_ns``, so the
population is reproduced rather than re-observed. The run refuses if the
symbol list would change by even one entry.

The artifact's own hash changes, and LONG stores its retirement registry under
that hash. Pass ``--retirement-registry-dir`` so the recorded delistings —
each one's first-observed timestamp is the causal anchor for why a symbol left
the entry population — are re-keyed to the new hash instead of being orphaned.

Dry run by default; ``--execute`` writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from liquidity_migration.core.deterministic_serialization import canonical_json  # noqa: E402
from liquidity_migration.core.venue_realm import venue_realm  # noqa: E402
from liquidity_migration.strategy.account_candidate_universe import (  # noqa: E402
    CANDIDATE_UNIVERSE_KIND,
    CANDIDATE_UNIVERSE_SCHEMA_VERSION,
    ScheduledCandidateRetirement,
    _write_retirement_registry,
    build_candidate_universe_artifact_from_inputs,
    load_candidate_universe,
    strategy_instruments_universe_inputs,
    write_candidate_universe,
)

_SOURCE_SCHEMA_VERSION = 4
_RETIRED_PROFILE = "continuous"
_REGISTRY_KIND = "candidate_retirement_registry"
_REGISTRY_SCHEMA_VERSION = 1
_RECORD_FIELDS = {
    "symbol",
    "delivery_time_ms",
    "first_observed_ts_ms",
    "observed_status",
    "evidence_source",
}


def _read_source(path: Path) -> Mapping[str, Any]:
    """Parse the schema-4 artifact without the schema-5 loader.

    The loader refuses schema 4 by design, so this reads and checks the parts
    the conversion depends on and leaves the rest to the rebuild.
    """

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise SystemExit(f"{path} is not a JSON object")
    if payload.get("schema_version") != _SOURCE_SCHEMA_VERSION:
        raise SystemExit(
            f"{path} is schema {payload.get('schema_version')!r}, "
            f"not {_SOURCE_SCHEMA_VERSION}"
        )
    if payload.get("kind") != CANDIDATE_UNIVERSE_KIND:
        raise SystemExit(f"{path} is not a candidate-universe artifact")
    profile_inputs = payload.get("profile_inputs")
    if not isinstance(profile_inputs, Mapping) or set(profile_inputs) != {
        "long",
        _RETIRED_PROFILE,
        "carry",
    }:
        raise SystemExit(f"{path} does not carry the three schema-4 profiles")
    # If the retired profile was ever anything other than every-gate-off, it
    # was a real population and not the venue instrument set, so renaming it
    # would be a lie about what the account may trade.
    retired = dict(profile_inputs[_RETIRED_PROFILE])
    if canonical_json(retired) != canonical_json(strategy_instruments_universe_inputs()):
        raise SystemExit(
            f"{path}: the {_RETIRED_PROFILE!r} profile is not the unrestricted "
            "instrument set; this artifact cannot be converted by renaming"
        )
    return payload


def _converted_payload(source: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild the artifact under schema 5 from the old raw snapshot."""

    raw_snapshot = source.get("raw_snapshot")
    if not isinstance(raw_snapshot, Mapping):
        raise SystemExit("source artifact has no raw_snapshot to rebuild from")
    instrument_rows = raw_snapshot.get("instrument_rows")
    ticker_rows = raw_snapshot.get("ticker_rows")
    if not isinstance(instrument_rows, list) or not isinstance(ticker_rows, list):
        raise SystemExit("source raw_snapshot rows are not lists")
    profile_inputs = source["profile_inputs"]
    payload = build_candidate_universe_artifact_from_inputs(
        instrument_rows,
        ticker_rows,
        snapshot_ts_ns=int(source["snapshot_ts_ns"]),
        population_inputs={
            "long": dict(profile_inputs["long"]),
            "carry": dict(profile_inputs["carry"]),
            "strategy_instruments": strategy_instruments_universe_inputs(),
        },
        realm=venue_realm(source["environment"]),
    )
    # The freeze script binds the two endpoint calls' acquisition interval
    # after building, so it lives outside the builder. Carry it across or the
    # converted artifact loses when the snapshot was actually taken.
    for field in ("snapshot_started_ts_ns", "snapshot_completed_ts_ns"):
        if field in source:
            payload[field] = source[field]
    payload["artifact_sha256"] = ""
    payload["artifact_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    return payload


def _read_registry(path: Path, *, expected_sha256: str) -> list[ScheduledCandidateRetirement]:
    """Parse the old retirement registry, preserving every field verbatim."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read retirement registry {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise SystemExit(f"retirement registry {path} is not a JSON object")
    if (
        payload.get("schema_version") != _REGISTRY_SCHEMA_VERSION
        or payload.get("kind") != _REGISTRY_KIND
    ):
        raise SystemExit(f"retirement registry {path} identity is invalid")
    if payload.get("candidate_universe_artifact_sha256") != expected_sha256:
        raise SystemExit(
            f"retirement registry {path} names artifact "
            f"{payload.get('candidate_universe_artifact_sha256')!r}, not {expected_sha256!r}"
        )
    records = payload.get("records")
    if not isinstance(records, list):
        raise SystemExit(f"retirement registry {path} records must be a list")
    output: list[ScheduledCandidateRetirement] = []
    for raw in records:
        if not isinstance(raw, Mapping) or set(raw) != _RECORD_FIELDS:
            raise SystemExit(f"retirement registry {path} record fields are invalid")
        output.append(
            ScheduledCandidateRetirement(
                symbol=str(raw["symbol"]),
                delivery_time_ms=int(raw["delivery_time_ms"]),
                first_observed_ts_ms=int(raw["first_observed_ts_ms"]),
                observed_status=str(raw["observed_status"]),
                evidence_source=str(raw["evidence_source"]),
            )
        )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Schema-4 artifact to convert.")
    parser.add_argument("--output", required=True, help="Schema-5 artifact to create.")
    parser.add_argument(
        "--retirement-registry-dir",
        default="",
        help=(
            "Directory holding <artifact_sha256>.json retirement registries. "
            "Given one, the recorded delistings are re-keyed to the new hash."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write the artifact. Without it this reports the plan and exits.",
    )
    args = parser.parse_args(argv)

    source_path = Path(args.input).expanduser()
    source = _read_source(source_path)
    old_symbols = source.get("symbols")
    if not isinstance(old_symbols, list):
        raise SystemExit("source artifact symbols must be a list")
    old_sha256 = str(source.get("artifact_sha256") or "")

    payload = _converted_payload(source)
    new_symbols = payload["symbols"]
    if sorted(old_symbols) != sorted(new_symbols):
        added = sorted(set(new_symbols) - set(old_symbols))
        dropped = sorted(set(old_symbols) - set(new_symbols))
        raise SystemExit(
            "conversion would change the tradable symbol list and was refused: "
            f"{len(added)} added {added[:10]}, {len(dropped)} dropped {dropped[:10]}"
        )

    registry_dir = (
        Path(args.retirement_registry_dir).expanduser()
        if args.retirement_registry_dir
        else None
    )
    retirements: list[ScheduledCandidateRetirement] = []
    registry_source: Path | None = None
    if registry_dir is not None:
        candidate = registry_dir / f"{old_sha256}.json"
        if candidate.exists():
            registry_source = candidate
            retirements = _read_registry(candidate, expected_sha256=old_sha256)

    report: dict[str, Any] = {
        "status": "converted" if args.execute else "planned",
        "input": str(source_path),
        "output": str(Path(args.output).expanduser()),
        "old_schema_version": _SOURCE_SCHEMA_VERSION,
        "new_schema_version": CANDIDATE_UNIVERSE_SCHEMA_VERSION,
        "old_symbol_count": len(old_symbols),
        "new_symbol_count": len(new_symbols),
        "symbols_unchanged": True,
        "old_artifact_sha256": old_sha256,
        "new_artifact_sha256": payload["artifact_sha256"],
        "profile_symbol_counts": {
            profile: len(values)
            for profile, values in payload["profile_eligible_symbols"].items()
        },
        "retirement_registry_source": str(registry_source) if registry_source else "",
        "retirement_records": len(retirements),
        "retirement_symbols": sorted(row.symbol for row in retirements),
    }

    if not args.execute:
        report["retirement_registry_output"] = (
            str(registry_dir / f"{payload['artifact_sha256']}.json")
            if registry_dir is not None
            else ""
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    output = write_candidate_universe(args.output, payload)
    report["output"] = str(output)
    if registry_dir is not None and retirements:
        # Re-key through the freshly written artifact, so the hash inside the
        # registry is the one the loader will actually derive from the file.
        frozen = load_candidate_universe(
            output,
            realm=venue_realm(source["environment"]),
        )
        registry_output = registry_dir / f"{frozen.artifact_sha256}.json"
        _write_retirement_registry(
            registry_output,
            frozen=frozen,
            records={row.symbol: row for row in retirements},
        )
        report["retirement_registry_output"] = str(registry_output)
    else:
        report["retirement_registry_output"] = ""
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
