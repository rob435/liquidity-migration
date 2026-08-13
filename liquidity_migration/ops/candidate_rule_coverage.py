"""Source-bound exact coverage between the frozen population and demo rules."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from liquidity_migration.strategy.account_candidate_universe import load_candidate_universe
from liquidity_migration.core.artifact_snapshot import StableFileSnapshot, read_stable_file
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.core.venue_realm import VenueRealm


CANDIDATE_RULE_COVERAGE_SCHEMA_VERSION = 1
CANDIDATE_RULE_COVERAGE_KIND = "account_candidate_demo_rule_coverage"
REGISTERED_MAX_RULE_AGE_SECONDS = 7 * 24 * 60 * 60
# Rollouts re-probe once a receipt is past this age, so renewal is a side effect
# of deployment rather than a timed deadline. Half the hard bound above leaves a
# full half-life of margin before the fail-closed cliff.
REGISTERED_ROLLOUT_RULE_REFRESH_AGE_SECONDS = REGISTERED_MAX_RULE_AGE_SECONDS // 2


class CandidateRuleRefreshRequired(RuntimeError):
    """Current candidate state cannot safely reuse older per-symbol evidence."""


def require_registered_rule_age(max_rule_age_seconds: float) -> float:
    """Return a finite freshness bound that cannot weaken the natural gate."""

    observed = float(max_rule_age_seconds)
    if not math.isfinite(observed) or observed <= 0.0:
        raise ValueError("max_rule_age_seconds must be finite and positive")
    if observed > REGISTERED_MAX_RULE_AGE_SECONDS:
        raise ValueError(
            "max_rule_age_seconds cannot exceed the registered 604800-second maximum"
        )
    return observed


def _self_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json({**dict(payload), "artifact_sha256": ""})
    ).hexdigest()


def _snapshot_signature(snapshot: StableFileSnapshot) -> tuple[object, ...]:
    return (
        snapshot.path,
        snapshot.data,
        snapshot.device,
        snapshot.inode,
        snapshot.mode,
        snapshot.uid,
        snapshot.mtime_ns,
        snapshot.nlink,
    )


def _read_regular(
    path: str | Path,
    *,
    label: str,
    require_private: bool = False,
) -> StableFileSnapshot:
    snapshot = read_stable_file(
        path,
        label=label,
        require_single_link=False,
    )
    if require_private and snapshot.mode & 0o077:
        raise ValueError(f"{label} must have mode 0600")
    if require_private and snapshot.uid != os.geteuid():
        raise ValueError(f"{label} must be owned by the verifier")
    return snapshot


def _write_private_json_create_only(
    path: str | Path,
    payload: Mapping[str, Any],
) -> Path:
    """Publish one root-private JSON artifact without overwriting evidence."""

    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor = os.open(
        output,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return output.resolve(strict=True)


def _normalized_symbol_rows(
    value: object,
    *,
    label: str,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"demo-rule receipt requires a non-empty {label} object")
    output: dict[str, Mapping[str, Any]] = {}
    for raw_symbol, row in value.items():
        symbol = str(raw_symbol).upper()
        if symbol in output or not isinstance(row, Mapping):
            raise ValueError(f"demo-rule receipt {label} rows are invalid")
        output[symbol] = row
    return output


def classify_demo_rule_receipt_freshness(
    rules_path: str | Path,
    *,
    validation_now_ns: int | None = None,
    max_rule_age_seconds: float = REGISTERED_MAX_RULE_AGE_SECONDS,
) -> str:
    """Return ``fresh`` or ``expired``; reject future-dated/invalid evidence."""

    from liquidity_migration.policy.account_execution_config import load_demo_rules_bytes

    max_rule_age_seconds = require_registered_rule_age(max_rule_age_seconds)
    now_ns = time.time_ns() if validation_now_ns is None else int(validation_now_ns)
    if now_ns <= 0:
        raise ValueError("validation_now_ns must be positive")
    snapshot = _read_regular(
        rules_path,
        label="demo-rule receipt freshness source",
        require_private=True,
    )
    load_demo_rules_bytes(snapshot.data, max_age_seconds=None)
    payload = json.loads(snapshot.data)
    verified_ts_ns = int(payload.get("verified_ts_ns") or 0)
    age_ns = now_ns - verified_ts_ns
    if age_ns < 0:
        raise ValueError("demo-rule receipt is future-dated")
    if age_ns > max_rule_age_seconds * 1_000_000_000:
        return "expired"
    return "fresh"


def project_demo_rules_to_candidate_subset(
    candidate_path: str | Path,
    prior_rules_path: str | Path,
    output_path: str | Path,
    *,
    validation_now_ns: int | None = None,
    max_rule_age_seconds: float = REGISTERED_MAX_RULE_AGE_SECONDS,
    held_exposure_symbols: Iterable[str] = (),
) -> Path:
    """Rebind fresh probe evidence when a new candidate set only removes symbols.

    Keeps every empirical timestamp and evidence row, drops the retired symbols,
    and binds the result to the new candidate artifact. Any candidate *addition*
    requires a fresh venue probe.

    ``held_exposure_symbols`` names symbols the account still has exposure on.
    A removed symbol in that set keeps its prior rule and probe evidence,
    declared under ``held_exposure_symbols`` in the projected receipt, so the
    exits that clear the exposure can still be built. The probe alternative
    cannot serve here: it requires a flat account, and exposure is the one
    thing these symbols still have.
    """

    from liquidity_migration.policy.account_execution_config import load_demo_rules_bytes
    from liquidity_migration.venue.account_service_bybit import instrument_rules_from_bybit_row

    max_rule_age_seconds = require_registered_rule_age(max_rule_age_seconds)
    now_ns = time.time_ns() if validation_now_ns is None else int(validation_now_ns)
    if now_ns <= 0:
        raise ValueError("validation_now_ns must be positive")

    target_snapshot = _read_regular(
        candidate_path,
        label="projected candidate-universe artifact",
        require_private=True,
    )
    # Projection carries a demo-rule receipt forward, and demo rules only exist
    # on demo; mainnet freezes its rules from get_instruments_info instead.
    target = load_candidate_universe(
        target_snapshot.path, snapshot=target_snapshot, realm=VenueRealm.DEMO
    )
    rules_snapshot = _read_regular(
        prior_rules_path,
        label="source demo-rule receipt",
        require_private=True,
    )
    try:
        source_payload = json.loads(rules_snapshot.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("source demo-rule receipt is not valid UTF-8 JSON") from exc
    if not isinstance(source_payload, Mapping):
        raise ValueError("source demo-rule receipt must contain an object")
    loaded_rules = load_demo_rules_bytes(
        rules_snapshot.data,
        now_ns=now_ns,
        max_age_seconds=max_rule_age_seconds,
    )

    source_binding = source_payload.get("symbol_source")
    if not isinstance(source_binding, Mapping):
        raise ValueError("source demo-rule receipt lacks candidate symbol_source binding")
    source_candidate_raw = source_binding.get("path")
    if type(source_candidate_raw) is not str or not source_candidate_raw:
        raise ValueError("source demo-rule receipt candidate path is invalid")
    # Projectable only while the bound candidate artifact still validates here.
    # A schema bump or a vanished artifact means the subset relationship cannot
    # be proved, so the caller falls through to a complete fresh probe.
    try:
        source_candidate_snapshot = _read_regular(
            source_candidate_raw,
            label="source candidate-universe artifact",
            require_private=True,
        )
        source_candidate = load_candidate_universe(
            source_candidate_snapshot.path,
            snapshot=source_candidate_snapshot,
            realm=VenueRealm.DEMO,
        )
        build_candidate_rule_coverage(
            source_candidate.path,
            rules_snapshot.path,
            created_ts_ns=now_ns,
            validation_now_ns=now_ns,
            max_rule_age_seconds=max_rule_age_seconds,
            candidate_snapshot=source_candidate_snapshot,
            demo_rules_snapshot=rules_snapshot,
        )
    except (OSError, ValueError) as exc:
        raise CandidateRuleRefreshRequired(
            "prior receipt's bound candidate artifact does not validate under "
            f"this code (structural drift; fresh probe required): {exc}"
        ) from exc

    source_symbols = set(loaded_rules)
    target_symbols = set(target.symbols)
    additions = sorted(target_symbols - source_symbols)
    if additions:
        raise CandidateRuleRefreshRequired(
            "candidate population adds symbols without fresh probe evidence: "
            + ",".join(additions[:20])
            + ("..." if len(additions) > 20 else "")
        )
    exposure = {str(symbol).upper() for symbol in held_exposure_symbols}
    retained_exposure = sorted((exposure & source_symbols) - target_symbols)
    kept_symbols = sorted(target_symbols | set(retained_exposure))
    # Exposure with no rule in the source receipt cannot be retained; its
    # exits stay unbuildable until the venue settles the position. Recorded
    # in the projected artifact so the condition is visible, matching the
    # mainnet freeze's held_exposure_unruled warning.
    exposure_unruled = sorted(exposure - source_symbols - target_symbols)

    try:
        target_payload = json.loads(target_snapshot.data)
        raw_snapshot = target_payload["raw_snapshot"]
        raw_instruments = raw_snapshot["instrument_rows"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "projected candidate artifact lacks validated raw instrument rows"
        ) from exc
    if not isinstance(raw_instruments, list):
        raise ValueError("projected candidate raw instrument rows are invalid")
    current_instruments: dict[str, Mapping[str, Any]] = {}
    for row in raw_instruments:
        if not isinstance(row, Mapping):
            raise ValueError("projected candidate raw instrument row is invalid")
        symbol = str(row.get("symbol") or "").upper()
        if not symbol or symbol in current_instruments:
            raise ValueError("projected candidate raw instrument symbols are invalid")
        current_instruments[symbol] = row
    unsafe_structural_drift: list[str] = []
    for symbol in kept_symbols:
        row = current_instruments.get(symbol)
        if row is None:
            if symbol in exposure and symbol not in target_symbols:
                # The venue no longer lists it: nothing to drift against; the
                # retained rule is exactly what lets its exits die properly.
                continue
            raise ValueError(
                f"projected candidate lacks raw instrument evidence for {symbol}"
            )
        current = instrument_rules_from_bybit_row(
            row,
            source="candidate_projection_api_demo_instruments_info",
            environment="demo",
            observed_ts_ns=target.snapshot_ts_ns,
        )
        prior = loaded_rules[symbol]
        tolerance = 1e-12
        if (
            not math.isclose(current.qty_step, prior.qty_step, rel_tol=1e-12, abs_tol=tolerance)
            or not math.isclose(
                current.tick_size,
                prior.tick_size,
                rel_tol=1e-12,
                abs_tol=tolerance,
            )
            or current.min_qty > prior.min_qty + max(tolerance, prior.min_qty * 1e-12)
            or current.min_notional
            > prior.min_notional + max(tolerance, prior.min_notional * 1e-12)
            or current.max_order_qty <= 0.0
            or prior.max_order_qty <= 0.0
            or current.max_order_qty
            < prior.max_order_qty
            - max(tolerance, prior.max_order_qty * 1e-12)
            or current.max_leverage <= 0.0
            or prior.max_leverage <= 0.0
            or current.max_leverage
            < prior.max_leverage
            - max(tolerance, prior.max_leverage * 1e-12)
        ):
            unsafe_structural_drift.append(symbol)
    if unsafe_structural_drift:
        raise CandidateRuleRefreshRequired(
            "candidate structural rules changed beyond retained evidence: "
            + ",".join(unsafe_structural_drift[:20])
            + ("..." if len(unsafe_structural_drift) > 20 else "")
        )

    source_rules = _normalized_symbol_rows(source_payload.get("rules"), label="rules")
    source_evidence = _normalized_symbol_rows(
        source_payload.get("evidence"),
        label="evidence",
    )
    if set(source_rules) != source_symbols or set(source_evidence) != source_symbols:
        raise ValueError("source demo-rule receipt normalized symbol coverage is inconsistent")

    projected: dict[str, Any] = dict(source_payload)
    projected["symbol_source"] = {
        "kind": "candidate_universe_artifact",
        "path": str(target.path),
        "size_bytes": target_snapshot.size,
        "sha256": target.file_sha256,
        "artifact_sha256": target.artifact_sha256,
        "artifact_self_hash_verified": True,
    }
    projected["rules"] = {
        symbol: dict(source_rules[symbol]) for symbol in kept_symbols
    }
    projected["evidence"] = {
        symbol: dict(source_evidence[symbol]) for symbol in kept_symbols
    }
    projected["held_exposure_symbols"] = {
        symbol: {
            "basis": "prior_receipt_carryover",
            "source_receipt_sha256": rules_snapshot.sha256,
        }
        for symbol in retained_exposure
    }
    projected["candidate_projection"] = {
        "kind": "fresh_demo_rule_candidate_subset_projection",
        "projected_ts_ns": now_ns,
        "empirical_verified_ts_ns_unchanged": int(
            source_payload.get("verified_ts_ns") or 0
        ),
        "source_rules_path": str(rules_snapshot.path),
        "source_rules_file_sha256": rules_snapshot.sha256,
        "source_rules_artifact_sha256": str(
            source_payload.get("artifact_sha256") or ""
        ),
        "source_candidate_path": str(source_candidate.path),
        "source_candidate_artifact_sha256": source_candidate.artifact_sha256,
        "retained_symbol_count": len(kept_symbols),
        "removed_symbols": sorted(source_symbols - set(kept_symbols)),
        "held_exposure_retained": retained_exposure,
        "held_exposure_unruled": exposure_unruled,
        "added_symbols": [],
        "limitation": "projection_does_not_extend_empirical_evidence_freshness",
    }
    projected["artifact_sha256"] = ""
    projected["artifact_sha256"] = _self_hash(projected)

    written = _write_private_json_create_only(output_path, projected)
    try:
        build_candidate_rule_coverage(
            target.path,
            written,
            created_ts_ns=now_ns,
            validation_now_ns=now_ns,
            max_rule_age_seconds=max_rule_age_seconds,
            candidate_snapshot=target_snapshot,
        )
    except BaseException:
        written.unlink(missing_ok=True)
        raise
    return written


def _use_snapshot(
    path: str | Path,
    *,
    label: str,
    require_private: bool,
    snapshot: StableFileSnapshot | None,
) -> StableFileSnapshot:
    if snapshot is None:
        return _read_regular(path, label=label, require_private=require_private)
    if snapshot.path != Path(path).expanduser().absolute():
        raise ValueError(f"{label} snapshot path differs")
    if require_private and (
        snapshot.mode & 0o077 or snapshot.uid != os.geteuid()
    ):
        raise ValueError(f"{label} must be verifier-owned mode 0600")
    return snapshot


def build_candidate_rule_coverage(
    candidate_path: str | Path,
    demo_rules_path: str | Path,
    *,
    created_ts_ns: int | None = None,
    validation_now_ns: int | None = None,
    max_rule_age_seconds: float = REGISTERED_MAX_RULE_AGE_SECONDS,
    candidate_snapshot: StableFileSnapshot | None = None,
    demo_rules_snapshot: StableFileSnapshot | None = None,
    realm: str = "demo",
) -> dict[str, Any]:
    """Reopen both sources and prove one accepted rule per frozen symbol.

    ``realm`` selects which rules receipt is admissible: demo's comes from the
    order-placing probe, every other realm's from the read-only
    instruments-info freeze. The coverage proof is the same either way -- one
    rule per frozen symbol, bound to this exact universe artifact.
    """

    from liquidity_migration.policy.account_execution_config import load_demo_rules_bytes
    from liquidity_migration.venue.venue_instrument_rules import load_venue_rules_bytes

    selected_realm = str(realm or "").strip().lower()
    max_rule_age_seconds = require_registered_rule_age(max_rule_age_seconds)
    created = time.time_ns() if created_ts_ns is None else int(created_ts_ns)
    if created <= 0:
        raise ValueError("created_ts_ns must be positive")
    candidate_snapshot = _use_snapshot(
        candidate_path,
        label="candidate-universe artifact",
        require_private=True,
        snapshot=candidate_snapshot,
    )
    candidate = load_candidate_universe(
        candidate_snapshot.path,
        snapshot=candidate_snapshot,
        # The universe artifact records the realm it was frozen from and its
        # loader refuses the other, so the realm has to be passed through.
        realm=selected_realm,
    )
    rules_snapshot = _use_snapshot(
        demo_rules_path,
        label="demo-rule receipt",
        require_private=True,
        snapshot=demo_rules_snapshot,
    )
    rules_resolved = rules_snapshot.path
    rules_data = rules_snapshot.data
    try:
        rules_payload = json.loads(rules_data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("demo-rule receipt is not valid UTF-8 JSON") from exc
    if not isinstance(rules_payload, Mapping):
        raise ValueError("demo-rule receipt must be a JSON object")
    if selected_realm == "demo":
        rules = load_demo_rules_bytes(
            rules_data,
            now_ns=validation_now_ns,
            max_age_seconds=max_rule_age_seconds,
        )
    else:
        rules = load_venue_rules_bytes(
            rules_data,
            realm=selected_realm,
            now_ns=validation_now_ns,
            max_age_seconds=max_rule_age_seconds,
        )
    rules_snapshot_after = _read_regular(
        rules_resolved,
        label="demo-rule receipt",
        require_private=True,
    )
    if _snapshot_signature(rules_snapshot_after) != _snapshot_signature(
        rules_snapshot
    ):
        raise RuntimeError("demo-rule receipt changed during validation")
    candidate_symbols = list(candidate.symbols)
    rule_symbols = sorted(rules)
    declared_exposure = rules_payload.get("held_exposure_symbols") or {}
    if not isinstance(declared_exposure, Mapping):
        raise ValueError("demo-rule receipt held_exposure_symbols must be an object")
    exposure_symbols = {str(symbol).upper() for symbol in declared_exposure}
    lying_declarations = sorted(exposure_symbols & set(candidate_symbols))
    if lying_declarations:
        # A universe symbol needs no exposure declaration; one that carries it
        # anyway is a receipt trying to relabel ordinary coverage as carryover.
        raise ValueError(
            "demo-rule receipt declares universe symbols as held exposure: "
            f"{lying_declarations[:20]!r}"
        )
    expected_symbols = sorted(set(candidate_symbols) | exposure_symbols)
    if rule_symbols != expected_symbols:
        missing = sorted(set(expected_symbols) - set(rule_symbols))
        extra = sorted(set(rule_symbols) - set(expected_symbols))
        raise ValueError(
            "demo-rule receipt does not exactly cover candidate universe "
            "plus declared held exposure "
            f"(missing={missing[:20]!r}, undeclared_extra={extra[:20]!r})"
        )
    source = rules_payload.get("symbol_source")
    if not isinstance(source, Mapping):
        raise ValueError("demo-rule receipt lacks candidate symbol_source binding")
    expected_source = {
        "kind": "candidate_universe_artifact",
        "path": str(candidate.path),
        "size_bytes": candidate_snapshot.size,
        "sha256": candidate.file_sha256,
        "artifact_sha256": candidate.artifact_sha256,
        "artifact_self_hash_verified": True,
    }
    for field, expected in expected_source.items():
        if source.get(field) != expected:
            raise ValueError(f"demo-rule symbol_source {field} does not bind candidate artifact")
    candidate_snapshot_after = _read_regular(
        candidate.path,
        label="candidate-universe artifact",
        require_private=True,
    )
    if _snapshot_signature(candidate_snapshot_after) != _snapshot_signature(
        candidate_snapshot
    ):
        raise RuntimeError("candidate-universe artifact changed during validation")
    payload: dict[str, Any] = {
        "schema_version": CANDIDATE_RULE_COVERAGE_SCHEMA_VERSION,
        "kind": CANDIDATE_RULE_COVERAGE_KIND,
        "created_ts_ns": created,
        "status": "passed",
        "candidate_universe": {
            "path": str(candidate.path),
            "file_sha256": candidate.file_sha256,
            "artifact_sha256": candidate.artifact_sha256,
            "symbol_count": len(candidate_symbols),
        },
        "demo_rules": {
            "path": str(rules_resolved),
            "file_sha256": rules_snapshot.sha256,
            "artifact_sha256": str(rules_payload.get("artifact_sha256") or ""),
            "verified_ts_ns": int(rules_payload.get("verified_ts_ns") or 0),
            "symbol_count": len(rule_symbols),
        },
        "symbols": candidate_symbols,
        "held_exposure_symbols": sorted(exposure_symbols),
        "coverage": {
            "candidate_symbols": len(candidate_symbols),
            "rule_symbols": len(rule_symbols),
            "held_exposure_symbols": len(exposure_symbols),
            "missing": 0,
            "extra": 0,
            "accepted_probe_evidence_per_symbol": True,
            "symbol_source_bound": True,
        },
        "limitations": [
            "rule_acceptance_does_not_prove_future_order_acceptance",
            "candidate_population_is_forward_point_in_time_not_historical_pit",
            "receipt_does_not_authorize_owner_start_or_deployment",
        ],
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    return payload
