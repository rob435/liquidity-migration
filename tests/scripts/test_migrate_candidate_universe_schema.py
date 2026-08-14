from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.core.venue_realm import VenueRealm
from liquidity_migration.strategy.account_candidate_universe import (
    load_candidate_universe,
    write_candidate_universe,
)
from tests.strategy.test_account_candidate_universe import (
    SNAPSHOT_NS,
    _payload,
    downgrade_payload_to_schema_four,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_MS = SNAPSHOT_NS // 1_000_000


def _module() -> Any:
    path = REPO_ROOT / "scripts" / "maintain" / "migrate_candidate_universe_schema.py"
    spec = importlib.util.spec_from_file_location(
        "migrate_candidate_universe_schema_test", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema_four_file(tmp_path: Path, payload: dict[str, Any] | None = None) -> Path:
    old = payload if payload is not None else downgrade_payload_to_schema_four(_payload())
    path = tmp_path / "candidate-v4.json"
    path.write_text(json.dumps(old, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _reseal(payload: dict[str, Any]) -> dict[str, Any]:
    payload["artifact_sha256"] = ""
    payload["artifact_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    return payload


def _registry(path: Path, *, artifact_sha256: str, first_observed_ts_ms: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "kind": "candidate_retirement_registry",
            "candidate_universe_artifact_sha256": artifact_sha256,
            "records": [{
                "symbol": "BBBUSDT",
                "delivery_time_ms": SNAPSHOT_MS + 100_000_000,
                "first_observed_ts_ms": first_observed_ts_ms,
                "observed_status": "Trading",
                "evidence_source": "live_instrument_delivery_time",
            }],
        }),
        encoding="utf-8",
    )


def test_round_trip_keeps_every_symbol_and_rewrites_the_schema(tmp_path: Path) -> None:
    source = _schema_four_file(tmp_path)
    old = json.loads(source.read_text(encoding="utf-8"))
    output = tmp_path / "candidate-v5.json"

    assert _module().main([
        "--input", str(source),
        "--output", str(output),
        "--execute",
    ]) == 0

    converted = json.loads(output.read_text(encoding="utf-8"))
    assert converted["schema_version"] == 5
    assert converted["symbols"] == old["symbols"]
    assert converted["strategy_instruments"] == old["profile_eligible_symbols"]["continuous"]
    assert converted["snapshot_ts_ns"] == old["snapshot_ts_ns"]
    assert converted["raw_snapshot"] == old["raw_snapshot"]
    assert "continuous" not in converted["profile_inputs"]
    assert set(converted["profile_eligible_symbols"]) == {"long", "carry"}
    # The whole point: the schema-5 loader accepts what the schema-4 one held.
    assert load_candidate_universe(output).symbols == tuple(old["symbols"])


def test_dry_run_reports_the_hashes_and_writes_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _schema_four_file(tmp_path)
    old = json.loads(source.read_text(encoding="utf-8"))
    output = tmp_path / "candidate-v5.json"

    assert _module().main(["--input", str(source), "--output", str(output)]) == 0

    assert not output.exists()
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "planned"
    assert report["old_symbol_count"] == len(old["symbols"])
    assert report["new_symbol_count"] == len(old["symbols"])
    assert report["old_artifact_sha256"] == old["artifact_sha256"]
    assert report["new_artifact_sha256"] != old["artifact_sha256"]


def test_conversion_refuses_when_the_symbol_list_would_change(tmp_path: Path) -> None:
    old = downgrade_payload_to_schema_four(_payload())
    # The rebuild reads raw_snapshot, so a source claiming a different
    # population than its own raw rows produce must not be waved through.
    old["symbols"] = [symbol for symbol in old["symbols"] if symbol != "BBBUSDT"]
    source = _schema_four_file(tmp_path, _reseal(old))

    with pytest.raises(SystemExit) as excinfo:
        _module().main([
            "--input", str(source),
            "--output", str(tmp_path / "candidate-v5.json"),
            "--execute",
        ])

    assert "would change the tradable symbol list" in str(excinfo.value)
    assert "BBBUSDT" in str(excinfo.value)
    assert not (tmp_path / "candidate-v5.json").exists()


def test_refuses_a_source_whose_retired_profile_was_not_the_instrument_set(
    tmp_path: Path,
) -> None:
    old = downgrade_payload_to_schema_four(_payload())
    old["profile_inputs"]["continuous"]["min_turnover_24h"] = 2_000_000.0
    source = _schema_four_file(tmp_path, _reseal(old))

    with pytest.raises(SystemExit) as excinfo:
        _module().main([
            "--input", str(source),
            "--output", str(tmp_path / "candidate-v5.json"),
            "--execute",
        ])

    assert "not the unrestricted instrument set" in str(excinfo.value)


def test_scheduled_retirements_are_rekeyed_and_keep_their_causal_anchor(
    tmp_path: Path,
) -> None:
    """The artifact hash changes, and LONG files retirements under it.

    Without the re-key the recorded delistings are orphaned, and each one's
    first-observed timestamp — the evidence for why the symbol left the entry
    population — is lost.
    """

    source = _schema_four_file(tmp_path)
    old = json.loads(source.read_text(encoding="utf-8"))
    registry_dir = tmp_path / "candidate_retirements"
    first_observed = SNAPSHOT_MS + 1_000
    _registry(
        registry_dir / f"{old['artifact_sha256']}.json",
        artifact_sha256=old["artifact_sha256"],
        first_observed_ts_ms=first_observed,
    )
    output = tmp_path / "candidate-v5.json"

    assert _module().main([
        "--input", str(source),
        "--output", str(output),
        "--retirement-registry-dir", str(registry_dir),
        "--execute",
    ]) == 0

    frozen = load_candidate_universe(output, realm=VenueRealm.DEMO)
    rekeyed = registry_dir / f"{frozen.artifact_sha256}.json"
    assert frozen.artifact_sha256 != old["artifact_sha256"]
    assert rekeyed.exists()
    assert rekeyed.stat().st_mode & 0o777 == 0o600
    payload = json.loads(rekeyed.read_text(encoding="utf-8"))
    assert payload["candidate_universe_artifact_sha256"] == frozen.artifact_sha256
    assert payload["records"] == [{
        "symbol": "BBBUSDT",
        "delivery_time_ms": SNAPSHOT_MS + 100_000_000,
        "first_observed_ts_ms": first_observed,
        "observed_status": "Trading",
        "evidence_source": "live_instrument_delivery_time",
    }]


def test_a_registry_naming_another_artifact_is_refused(tmp_path: Path) -> None:
    source = _schema_four_file(tmp_path)
    old = json.loads(source.read_text(encoding="utf-8"))
    registry_dir = tmp_path / "candidate_retirements"
    _registry(
        registry_dir / f"{old['artifact_sha256']}.json",
        artifact_sha256="0" * 64,
        first_observed_ts_ms=SNAPSHOT_MS + 1_000,
    )

    with pytest.raises(SystemExit) as excinfo:
        _module().main([
            "--input", str(source),
            "--output", str(tmp_path / "candidate-v5.json"),
            "--retirement-registry-dir", str(registry_dir),
            "--execute",
        ])

    assert "retirement registry" in str(excinfo.value)


def test_schema_five_input_is_refused(tmp_path: Path) -> None:
    source = write_candidate_universe(tmp_path / "candidate-v5-input.json", _payload())

    with pytest.raises(SystemExit) as excinfo:
        _module().main([
            "--input", str(source),
            "--output", str(tmp_path / "candidate-out.json"),
            "--execute",
        ])

    assert "is schema 5, not 4" in str(excinfo.value)
