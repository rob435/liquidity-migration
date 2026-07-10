from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import polars as pl
import pytest

import scripts.strategy_overhaul_scout_2026_07_10 as scout


def _fake_source_snapshot(*, marker: str = "stable", clean: bool = True) -> scout.SourceSnapshot:
    patch = b"" if clean else f"diff --git a/source.py b/source.py\n{marker}\n".encode()
    untracked_files = [] if clean else [("new_source.py", marker.encode())]
    archive = scout._deterministic_untracked_archive(untracked_files)
    archive_sha = hashlib.sha256(archive).hexdigest()
    untracked_manifest = {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_untracked_source_manifest",
        "archive_sha256": archive_sha,
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
            for path, data in untracked_files
        ],
    }
    untracked_manifest["artifact_sha256"] = scout._json_hash(untracked_manifest)
    state = "verified_clean_snapshot" if clean else "verified_reconstructable_dirty_snapshot"
    manifest = {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_source_snapshot",
        "repository_commit": "abc",
        "worktree_state": state,
        "snapshot_ready": True,
        "tracked_patch_sha256": hashlib.sha256(patch).hexdigest(),
        "untracked_archive_sha256": archive_sha,
        "marker": marker,
    }
    manifest["artifact_sha256"] = scout._json_hash(manifest)
    git = {
        "commit": "abc",
        "dirty_paths": [] if clean else ["tracked:source.py", "untracked:new_source.py"],
        "clean": clean,
        "snapshot_ready": True,
        "worktree_state": state,
        "tracked_diff_sha256": hashlib.sha256(patch).hexdigest(),
        "untracked_source_bundle_sha256": archive_sha,
        "source_snapshot_sha256": manifest["artifact_sha256"],
    }
    return scout.SourceSnapshot(
        git=git,
        manifest=manifest,
        tracked_patch=patch,
        untracked_archive=archive,
        untracked_manifest=untracked_manifest,
    )


def _exact_environment_fixture() -> dict[str, object]:
    distribution = {
        "name": "x",
        "normalized_name": "x",
        "version": "1",
        "location": "/fixture",
        "provenance_files": {},
        "declared_file_count": 0,
        "declared_hashed_file_count": 0,
        "declared_file_manifest_sha256": scout._json_hash([]),
    }
    distribution["distribution_identity_sha256"] = scout._json_hash(distribution)
    executed_modules = scout._executed_module_receipts()
    python_executable = Path(scout.sys.executable).resolve()
    return {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_exact_environment_manifest",
        "identity_strength": "distribution_provenance_declared_content_and_executed_module_bytes",
        "content_identity_ready": True,
        "python": {
            "version": "3.12.0",
            "executable": str(python_executable),
            "executable_sha256": scout._sha256_file(python_executable),
        },
        "platform": {"system": "test"},
        "installed_distribution_count": 1,
        "installed_distributions": [distribution],
        "executed_module_count": len(executed_modules),
        "executed_modules": executed_modules,
        "repo_dependency_files": [],
    }


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        plan=True,
        bybit_root=str(tmp_path / "bybit"),
        binance_root=str(tmp_path / "binance"),
        deep_root_hash=False,
        write_plan=None,
    )


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "phase0@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Phase0 Test"], cwd=path, check=True)


def _commit_all(path: Path) -> None:
    subprocess.run(["git", "add", "--all"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=path, check=True)


def test_plan_freezes_discovery_surface_and_non_authorizations(tmp_path, monkeypatch) -> None:
    for venue in ("bybit", "binance"):
        (tmp_path / venue).mkdir()
    monkeypatch.setattr(
        scout,
        "_git_state",
        lambda: {"commit": "abc", "dirty_paths": [], "clean": True},
    )
    monkeypatch.setattr(
        scout,
        "_source_receipts",
        lambda: {
            "liquidity_migration/continuous_population_scout.py": {
                "present": True,
                "sha256": "c",
            },
            "liquidity_migration/long_population_scout.py": {
                "present": True,
                "sha256": "l",
            },
        },
    )
    monkeypatch.setattr(
        scout,
        "_config_receipts",
        lambda: {"long": {"sha256": "l"}, "continuous": {"sha256": "c"}},
    )
    monkeypatch.setattr(
        scout,
        "_root_plan",
        lambda venue, root, deep_root_hash: {
            "venue": venue,
            "root": str(root),
            "phase0_source_ready": True,
            "tier_a0_label_ready": True,
            "registered_receipt_ready": True,
        },
    )

    plan = scout.build_plan(_args(tmp_path))

    assert plan["readiness"]["phase0_ready"] is True
    assert plan["readiness"]["outcome_run_ready"] is False
    assert plan["proposed_minimal_labels"]["point_return_horizons_h"] == (1, 24, 72)
    assert "continuous_tp_hold_cost_surface" in plan["deferred_contracts"]
    assert "no real-money enablement" in plan["non_authorizations"]
    assert [stage["id"] for stage in plan["stages"]] == [
        "S00",
        "S01",
        "S02",
        "S03",
        "S04",
        "S05",
        "S06",
    ]

    deterministic = scout.build_plan(_args(tmp_path), include_generated_at_utc=False)
    assert "generated_at_utc" not in deterministic
    assert deterministic == scout.build_plan(_args(tmp_path), include_generated_at_utc=False)


def test_dirty_commit_keeps_plan_incomplete(tmp_path, monkeypatch) -> None:
    for venue in ("bybit", "binance"):
        (tmp_path / venue).mkdir()
    monkeypatch.setattr(
        scout,
        "_git_state",
        lambda: {"commit": "abc", "dirty_paths": [" M file"], "clean": False},
    )
    monkeypatch.setattr(
        scout,
        "_source_receipts",
        lambda: {
            "liquidity_migration/continuous_population_scout.py": {
                "present": True,
                "sha256": "c",
            },
            "liquidity_migration/long_population_scout.py": {
                "present": True,
                "sha256": "l",
            },
        },
    )
    monkeypatch.setattr(scout, "_config_receipts", lambda: {})
    monkeypatch.setattr(
        scout,
        "_root_plan",
        lambda venue, root, deep_root_hash: {
            "venue": venue,
            "phase0_source_ready": True,
            "tier_a0_label_ready": True,
            "registered_receipt_ready": True,
        },
    )

    plan = scout.build_plan(_args(tmp_path))

    assert plan["readiness"]["phase0_ready"] is True
    assert plan["readiness"]["outcome_run_ready"] is False
    assert plan["readiness"]["result_if_run_now"] == "phase0_only"


def test_wired_preflight_cannot_claim_outcome_readiness_without_population_and_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for venue in ("bybit", "binance"):
        (tmp_path / venue).mkdir()
    snapshot = _fake_source_snapshot(clean=True)
    monkeypatch.setattr(scout, "_git_state", lambda: snapshot.git)
    monkeypatch.setattr(
        scout,
        "_source_receipts",
        lambda: {
            "liquidity_migration/continuous_population_scout.py": {
                "present": True,
                "sha256": "c",
            },
            "liquidity_migration/long_population_scout.py": {
                "present": True,
                "sha256": "l",
            },
        },
    )
    monkeypatch.setattr(
        scout,
        "_root_plan",
        lambda venue, root, deep_root_hash: {
            "venue": venue,
            "root": str(root),
            "phase0_source_ready": True,
            "tier_a0_label_ready": True,
            "registered_receipt_ready": True,
        },
    )
    monkeypatch.setattr(
        scout,
        "_canonical_child_status",
        lambda sleeve, contract, analysis: {
            "sleeve": sleeve,
            "status": "READY",
            "contract_path": str(contract),
            "analysis_manifest_path": str(analysis),
        },
    )

    plan = scout.build_plan(_args(tmp_path), include_generated_at_utc=False)

    assert plan["readiness"]["s02_config_parity_wired"] is True
    assert plan["readiness"]["s02_preflight_ready"] is True
    assert plan["readiness"]["expected_population_artifacts_verified"] is False
    assert plan["readiness"]["s02_semantic_receipt_verified"] is False
    assert plan["readiness"]["outcome_run_ready"] is False


def test_source_snapshot_is_reconstructable_and_excludes_canonical_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "tracked.bin").write_bytes(b"\x00before\n")
    _commit_all(repo)
    (repo / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    (repo / "tracked.bin").write_bytes(b"\x00after\n")
    (repo / "untracked.py").write_bytes(b"print('untracked')\n")
    (repo / "untracked.py").chmod(0o755)
    for relative in scout.CANONICAL_CHILD_OUTPUT_PATHS:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"downstream child {relative}\n", encoding="utf-8")
    monkeypatch.setattr(scout, "REPO", repo)

    first = scout._capture_source_snapshot()
    expected_patch = subprocess.run(
        [
            "git",
            "diff",
            "--binary",
            "HEAD",
            "--",
            ".",
            *(f":(exclude){path}" for path in scout.CANONICAL_CHILD_OUTPUT_PATHS),
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout

    assert first.tracked_patch == expected_patch
    assert first.git["snapshot_ready"] is True
    assert first.git["clean"] is False
    assert first.git["worktree_state"] == "verified_reconstructable_dirty_snapshot"
    assert first.untracked_manifest["files"] == [
        {
            "path": "untracked.py",
            "bytes": 19,
            "sha256": hashlib.sha256(b"print('untracked')\n").hexdigest(),
            "source_mode": "0755",
            "archive_mode": "0644",
            "archive_mtime": 0,
            "archive_uid": 0,
            "archive_gid": 0,
        }
    ]
    with tarfile.open(fileobj=io.BytesIO(first.untracked_archive), mode="r:") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == ["untracked.py"]
        assert members[0].mode == 0o644
        assert members[0].mtime == 0
        assert members[0].uid == members[0].gid == 0
        assert archive.extractfile(members[0]).read() == b"print('untracked')\n"  # type: ignore[union-attr]

    restore = tmp_path / "restore"
    subprocess.run(["git", "clone", "-q", str(repo), str(restore)], check=True)
    restoration = scout._restore_source_snapshot(first, restore)
    assert restoration["status"] == "VERIFIED"
    assert (restore / "tracked.py").read_text(encoding="utf-8") == "value = 2\n"
    assert (restore / "tracked.bin").read_bytes() == b"\x00after\n"
    assert (restore / "untracked.py").read_bytes() == b"print('untracked')\n"
    assert (restore / "untracked.py").stat().st_mode & 0o777 == 0o755

    for relative in scout.CANONICAL_CHILD_OUTPUT_PATHS:
        (repo / relative).write_text("changed downstream status only\n", encoding="utf-8")
    second = scout._capture_source_snapshot()

    assert second == first
    assert not any(
        relative in dirty for relative in scout.CANONICAL_CHILD_OUTPUT_PATHS for dirty in first.git["dirty_paths"]
    )


def test_source_receipts_exclude_canonical_children_and_include_identity_builders() -> None:
    receipts = scout._source_receipts()

    assert not (set(scout.CANONICAL_CHILD_OUTPUT_PATHS) & set(receipts))
    assert receipts["liquidity_migration/strategy_overhaul_config_identity.py"]["present"] is True
    assert receipts["liquidity_migration/strategy_overhaul_instrument_map.py"]["present"] is True
    assert receipts["liquidity_migration/strategy_overhaul_population_keys.py"]["present"] is True
    assert receipts["liquidity_migration/strategy_overhaul_rmom_availability.py"]["present"] is True
    assert receipts["scripts/precompute_residual_momentum.py"]["present"] is True


def test_source_snapshot_change_detection_refuses_mixed_identity() -> None:
    first = _fake_source_snapshot(marker="before", clean=False)
    second = _fake_source_snapshot(marker="after", clean=False)

    with pytest.raises(RuntimeError, match="changed during Phase-0 scan"):
        scout._assert_source_snapshot_unchanged(first, second)


def test_environment_manifest_records_every_sorted_distribution_and_dependency_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "requirements.txt").write_text("numpy>=2\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _commit_all(tmp_path)
    monkeypatch.setattr(scout, "REPO", tmp_path)

    class Distribution:
        def __init__(self, name: str, version: str) -> None:
            self.metadata = {"Name": name}
            self.version = version
            self.files: tuple[object, ...] = ()

        def locate_file(self, path: str) -> Path:
            return tmp_path / path

        def read_text(self, filename: str) -> None:
            del filename
            return None

    monkeypatch.setattr(
        scout.importlib.metadata,
        "distributions",
        lambda **_kwargs: [Distribution("Zoo_pkg", "2.0"), Distribution("alpha.pkg", "10.1")],
    )

    manifest = scout._environment_receipt()

    assert [(row["normalized_name"], row["version"]) for row in manifest["installed_distributions"]] == [
        ("alpha-pkg", "10.1"),
        ("zoo-pkg", "2.0"),
    ]
    dependency_rows = {row["path"]: row for row in manifest["repo_dependency_files"]}
    assert dependency_rows["requirements.txt"]["classification"] == "dependency_specification"
    assert dependency_rows["requirements.txt"]["sufficient_as_environment_lock"] is False
    assert dependency_rows["uv.lock"]["classification"] == "exact_resolver_lock"
    assert manifest["artifact_type"] == "strategy_overhaul_exact_environment_manifest"
    assert manifest["content_identity_ready"] is True
    assert manifest["executed_module_count"] == len(scout._PHASE0_EXECUTED_MODULES)

    monkeypatch.setattr(
        scout.importlib.metadata,
        "distributions",
        lambda **_kwargs: [Distribution("same_name", "1"), Distribution("same-name", "2")],
    )
    with pytest.raises(RuntimeError, match="conflicting duplicate normalized installed distribution"):
        scout._environment_receipt()

    identical = Distribution("same_name", "1")
    monkeypatch.setattr(
        scout.importlib.metadata,
        "distributions",
        lambda **_kwargs: [identical, identical],
    )
    aliased = scout._environment_receipt()
    assert aliased["installed_distribution_count"] == 1
    assert [row["normalized_name"] for row in aliased["installed_distributions"]] == ["same-name"]

    discoveries = iter(([identical], [identical, identical]))
    monkeypatch.setattr(scout.importlib.metadata, "distributions", lambda **_kwargs: next(discoveries))
    assert scout._environment_receipt() == scout._environment_receipt()


def test_environment_discovery_excludes_only_repository_source_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    external_site_packages = tmp_path / "external-venv" / "site-packages"
    nested_site_packages = repository / ".venv" / "site-packages"
    external_site_packages.mkdir(parents=True)
    nested_site_packages.mkdir(parents=True)
    monkeypatch.setattr(scout, "REPO", repository)
    monkeypatch.setattr(scout.os, "getcwd", lambda: str(repository))
    monkeypatch.setattr(
        scout.sys,
        "path",
        ["", str(repository), str(repository / "."), str(external_site_packages), str(nested_site_packages)],
    )

    assert scout._installed_distribution_search_path() == [
        str(external_site_packages.resolve()),
        str(nested_site_packages.resolve()),
    ]


def test_phase0_input_plan_identity_ignores_downstream_canonical_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for venue in ("bybit", "binance"):
        (tmp_path / venue).mkdir()
    snapshot = _fake_source_snapshot(clean=False)
    monkeypatch.setattr(scout, "_git_state", lambda: snapshot.git)
    monkeypatch.setattr(
        scout,
        "_source_receipts",
        lambda: {
            "liquidity_migration/continuous_population_scout.py": {"present": True, "sha256": "c"},
            "liquidity_migration/long_population_scout.py": {"present": True, "sha256": "l"},
        },
    )
    monkeypatch.setattr(scout, "_config_receipts", lambda: {})
    monkeypatch.setattr(
        scout,
        "_root_plan",
        lambda venue, root, deep_root_hash: {
            "venue": venue,
            "root": str(root),
            "phase0_source_ready": True,
            "tier_a0_label_ready": True,
            "registered_receipt_ready": True,
        },
    )
    child_ready = {"value": False}

    def child_status(sleeve: str, contract: Path, analysis: Path) -> dict[str, object]:
        return {
            "sleeve": sleeve,
            "status": "READY" if child_ready["value"] else "NOT_READY",
            "contract_path": str(contract),
            "analysis_manifest_path": str(analysis),
        }

    monkeypatch.setattr(scout, "_canonical_child_status", child_status)
    before = scout.build_plan(_args(tmp_path), include_generated_at_utc=False)
    child_ready["value"] = True
    after = scout.build_plan(_args(tmp_path), include_generated_at_utc=False)

    assert before["canonical_child_freeze_receipt"] != after["canonical_child_freeze_receipt"]
    assert before["phase0_input_plan_sha256"] == after["phase0_input_plan_sha256"]
    assert scout._phase0_input_plan(before) == scout._phase0_input_plan(after)
    assert before["plan_sha256"] != after["plan_sha256"]
    assert before["readiness"]["outcome_run_ready"] is False
    assert after["readiness"]["outcome_run_ready"] is False
    assert after["readiness"]["s02_config_parity_wired"] is False


def test_deterministic_phase0_plan_ignores_rmom_compressed_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for venue in ("bybit", "binance"):
        root = tmp_path / venue
        root.mkdir()
        (root / "residual_momentum.parquet").write_bytes(b"numeric-outcome-payload-a")
    monkeypatch.setattr(
        scout,
        "_git_state",
        lambda: {"commit": "abc", "dirty_paths": [], "clean": True},
    )
    monkeypatch.setattr(
        scout,
        "_source_receipts",
        lambda: {
            "liquidity_migration/continuous_population_scout.py": {
                "present": True,
                "sha256": "c",
            },
            "liquidity_migration/long_population_scout.py": {
                "present": True,
                "sha256": "l",
            },
        },
    )
    monkeypatch.setattr(scout, "_config_receipts", lambda: {})

    before = scout.build_plan(_args(tmp_path), include_generated_at_utc=False)
    for venue in ("bybit", "binance"):
        (tmp_path / venue / "residual_momentum.parquet").write_bytes(b"different-and-longer-numeric-outcome-payload-b")
    after = scout.build_plan(_args(tmp_path), include_generated_at_utc=False)

    assert before == after
    assert before["roots"]["bybit"]["residual_momentum"] == {
        "path": str(tmp_path / "bybit" / "residual_momentum.parquet"),
        "present": True,
        "compression_or_byte_size_read": False,
        "deep_validation_deferred": True,
    }


def test_non_plan_mode_refuses() -> None:
    assert scout.main([]) == 2


def _phase0_inventory() -> dict[str, object]:
    return {
        "artifact_sha256": "inventory-sha",
        "window": {
            "sleeve_windows": [
                {
                    "sleeve": "continuous",
                    "signal_start_date": "2023-04-01",
                    "signal_end_date_exclusive": "2026-07-10",
                },
                {
                    "sleeve": "long",
                    "signal_start_date": "2023-06-15",
                    "signal_end_date_exclusive": "2026-07-10",
                },
            ]
        },
        "readiness": {
            "status": "PARTIAL",
            "portable_cross_venue_matching_ready": False,
        },
        "field_availability": {
            "bybit": {
                "klines_1h": {
                    "row_count": 24,
                    "grid_integrity": {"symbol_day_count": 1},
                },
                "archive_trade_manifest": {"key_provenance_projection_sha256": "manifest-key-sha"},
            }
        },
        "pit_provenance": {"venues": {"bybit": {"storage_row_count": 1, "membership_pair_count": 1}}},
        "manifest_kline_coverage": {
            "venues": {
                "bybit": {
                    "membership_coverage_fraction": 1.0,
                    "daily_counts": [
                        {
                            "date": "2023-06-14",
                            "covered_membership_symbol_day_count": 1,
                            "covered_kline_row_count": 24,
                        }
                    ],
                }
            }
        },
        "rmom_population_coverage": {"venues": {"bybit": {"daily_counts": []}}},
        "root_lineage": {
            "canonical_s01_root_lineage_ready": False,
            "all_upstream_authenticity_proven": False,
            "venues": {"bybit": {"upstream_authenticity_proven": False}},
            "limitations": ["fixture lineage is unproven"],
        },
        "resource_estimate": {
            "totals": {},
            "partition_checkpoint_plan": {"partition_key": ["venue", "month"]},
        },
        "proposed_schemas": {},
        "child_schema_registry": {"schemas": {}, "mismatches": []},
        "instrument_map_coverage": {"status": "not_provided"},
        "outcome_blind_audit": {"outcome_values_read": False},
    }


def test_config_bundle_rederives_parity_instead_of_trusting_top_level_status() -> None:
    configs = copy.deepcopy(scout._config_receipts())
    manifest = configs["s02_config_parity_manifest"]
    manifest["status"] = "WIRED"
    manifest["targets"][0]["consumer_validations"][0]["values_match"] = False

    with pytest.raises(RuntimeError, match="does not match validator evidence"):
        scout._config_bundle_artifacts(configs)


def test_registered_designs_emit_support_counts_and_cover_every_s01_input() -> None:
    inventory = _phase0_inventory()
    designs = scout._load_registered_child_designs()
    support = scout._support_design_and_counts(inventory, designs)
    snapshot = _fake_source_snapshot(clean=False)
    environment = _exact_environment_fixture()
    configs = scout._config_receipts()
    config_payloads, config_artifact_index = scout._config_bundle_artifacts(configs)
    status = scout._s01_template_input_status(
        phase0_id="phase0-test",
        plan={
            "phase0_input_plan_sha256": "plan-sha",
            "git": snapshot.git,
            "sources": {"source.py": {"sha256": "source-sha"}},
            "configs": configs,
        },
        inventory=inventory,
        environment=environment,
        designs=designs,
        support_artifact=support,
        config_artifact_index=config_artifact_index,
    )

    assert designs["artifact_sha256"]
    assert set(designs["sleeves"]) == {"continuous", "long"}
    assert support["venue_counts"]["bybit"]["kline_row_count"] == 24
    assert support["sleeve_signal_window_counts_status"] == "WINDOW_SCOPE_DECLARED"
    assert support["s01_support_substitution_ready"] is False
    assert support["sleeve_signal_window_counts"]["long"]["venues"]["bybit"]["manifest_covered_hourly_row_count"] == 24
    assert support["sleeve_signal_window_counts"]["long"]["identity_membership_start_date"] == "2023-06-14"
    assert support["sleeve_signal_window_counts"]["long"]["identity_membership_end_date_exclusive"] == "2026-07-09"
    assert support["focal_arm_counts"]["status"] == "DEFERRED_TO_S02"
    assert len(support["sleeve_design"]["continuous"]["hypotheses"]) == 2
    assert len(support["sleeve_design"]["long"]["hypotheses"]) == 2
    for sleeve in ("continuous", "long"):
        template = designs["sleeves"][sleeve]["analysis_template"]
        assert template["required_s01_outputs_before_s02"] == scout.REQUIRED_S01_OUTPUT_GATES[sleeve]
        assert template["receipt_policy"] == scout.REQUIRED_RECEIPT_POLICY
        assert scout.REQUIRED_DURABLE_POPULATION_ARTIFACTS <= set(template["required_artifacts"])
    assert status["all_sleeves_ready"] is False
    for sleeve, row in status["sleeves"].items():
        required = set(designs["sleeves"][sleeve]["analysis_template"]["required_phase0_substitutions"])
        assert set(row["resolved"]) | set(row["blockers"]) == required
        assert not (set(row["resolved"]) & set(row["blockers"]))
        assert "phase0_support_counts" in row["blockers"]
        assert "phase0_support_counts" in row["diagnostic_values_not_resolved"]
        assert "pit_manifest_receipts" in row["blockers"]
        assert "pit_manifest_receipts" in row["diagnostic_values_not_resolved"]
        assert "source_function_hashes" in row["blockers"]
        assert "label_tail_root_receipt" in row["blockers"]
        assert row["resolved"]["worktree_policy"] == "verified_reconstructable_dirty_snapshot"
        assert row["resolved"]["patch_bundle_sha256"] == snapshot.git["tracked_diff_sha256"]
        assert row["resolved"]["untracked_source_bundle_sha256"] == snapshot.git["untracked_source_bundle_sha256"]
        assert row["resolved"]["environment_lock_path"] == scout.ENVIRONMENT_MANIFEST_ARTIFACT
        assert "worktree_policy" not in row["blockers"]
        assert "environment_lock_path" not in row["blockers"]
        assert row["resolved"]["canonical_config_json_path"] == f"{sleeve}_canonical_config.json"
        assert (
            row["resolved"]["canonical_config_sha256"]
            == hashlib.sha256(scout._render_json(configs[sleeve]["canonical_config"])).hexdigest()
        )
        assert row["resolved"]["canonical_config_sha256"] != configs[sleeve]["canonical_config_sha256"]
        assert row["resolved"]["config_identity_json_path"] == f"{sleeve}_config_identity.json"
        assert row["resolved"]["registered_scope_json_path"] == f"{sleeve}_registered_scope.json"
        assert row["resolved"]["s02_config_parity_manifest_path"] == "s02_config_parity_manifest.json"
        assert "canonical_config_json_path" not in row["blockers"]
        assert row["implementation_blockers"] == {}
    assert status["config_artifacts_file_hash_verified"] is True
    assert status["s02_config_parity_status"] == "WIRED"
    assert status["config_consumer_wiring_verified_prospectively"] is True
    assert status["real_s00_s04_identity_chain_instantiated"] is False
    assert all(not any(gates.values()) for gates in status["post_s01_s02_gates"].values())
    assert set(config_payloads) == {
        "continuous_canonical_config.json",
        "continuous_registered_scope.json",
        "continuous_config_identity.json",
        "continuous_component_config.json",
        "long_canonical_config.json",
        "long_registered_scope.json",
        "long_config_identity.json",
        "s02_config_parity_manifest.json",
        "config_artifact_index.json",
    }
    for sleeve in ("continuous", "long"):
        identity_payload = config_payloads[f"{sleeve}_config_identity.json"]
        assert identity_payload["canonical_config"] == config_payloads[f"{sleeve}_canonical_config.json"]
        assert identity_payload["scope"] == config_payloads[f"{sleeve}_registered_scope.json"]
    assert (
        config_payloads["continuous_config_identity.json"]["component_config"]
        == config_payloads["continuous_component_config.json"]
    )


def test_s01_map_substitutions_require_local_identity_not_cross_venue_overlap() -> None:
    inventory = _phase0_inventory()
    inventory["instrument_map_coverage"] = {
        "status": "complete",
        "map_version": "disjoint-v1",
        "map_sha256": "map-sha",
        "venue_local_identity_ready": True,
        "portable_matching_ready": False,
        "venues": {
            venue: {
                "unmapped_membership_pair_count": 0,
                "same_venue_canonical_day_alias_collision_count": 0,
                "row_coverage_fraction": 1.0,
                "membership_symbol_count": 2,
                "mapped_symbol_count": 2,
            }
            for venue in ("bybit", "binance")
        },
    }
    designs = scout._load_registered_child_designs()
    support = scout._support_design_and_counts(inventory, designs)
    snapshot = _fake_source_snapshot()
    environment = _exact_environment_fixture()
    map_artifact = {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_instrument_map_input",
        "status": "ready",
        "source_kind": "external_json",
        "auto_derived": False,
        "version": "disjoint-v1",
        "map_sha256": "map-sha",
        "entries": [{"canonical_instrument": "fixture"}],
        "artifact_sha256": "map-artifact-sha",
    }
    status = scout._s01_template_input_status(
        phase0_id="phase0-test",
        plan={
            "phase0_input_plan_sha256": "plan-sha",
            "git": snapshot.git,
            "configs": {
                "continuous": {"sha256": "continuous-config"},
                "long": {"sha256": "long-config"},
            },
        },
        inventory=inventory,
        environment=environment,
        designs=designs,
        support_artifact=support,
        instrument_map_artifact=map_artifact,
    )

    assert status["instrument_map_identity_status"] == {
        "venue_local_substitution_ready": True,
        "portable_matching_ready": False,
        "portability_is_separate_from_venue_local_hypothesis_readiness": True,
        "reconstructable_artifact_path": "instrument_map_input.json",
        "reconstructable_artifact_sha256": "map-artifact-sha",
        "source_kind": "external_json",
        "auto_derived": False,
        "source_projection_identity_sha256": None,
        "source_projection_sha256": None,
        "source_projection_row_count": None,
        "source_registered_window_complete": None,
    }
    for sleeve in ("continuous", "long"):
        row = status["sleeves"][sleeve]
        assert row["resolved"]["instrument_map_version"] == "disjoint-v1"
        assert row["resolved"]["instrument_map_sha256"] == "map-sha"
        assert "instrument_map_version" not in row["blockers"]


def test_instrument_map_input_is_normalized_reconstructable_and_rechecked(tmp_path: Path) -> None:
    path = tmp_path / "map.json"
    payload = {
        "version": "fixture-v1",
        "entries": [
            {
                "canonical_instrument": "BYBIT::AAAUSDT::USDT_LINEAR_PERPETUAL",
                "venue": "BYBIT",
                "symbol": "AAAUSDT",
                "valid_from_date": "2023-01-01",
                "valid_to_date_exclusive": None,
                "base_asset": "aaa",
                "quote_asset": "usdt",
                "settlement_asset": "usdt",
                "contract_type": "LINEAR_PERPETUAL",
                "contract_multiplier": 1,
                "mapping_source": "fixture",
                "review_status": "mechanically_derived_venue_local",
            }
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    entries, version, artifact = scout._load_instrument_map(path, None)

    assert version == "fixture-v1"
    assert len(entries) == 1
    assert artifact["status"] == "diagnostic_untrusted"
    assert artifact["trust_class"] == "external_untrusted"
    assert artifact["review_status_trusted"] is False
    assert artifact["entries"][0]["venue"] == "bybit"
    assert artifact["entries"][0]["base_asset"] == "AAA"
    assert artifact["map_sha256"] == scout._json_hash(artifact["entries"])
    scout._assert_instrument_map_unchanged(artifact)

    path.write_text(json.dumps({**payload, "version": "changed-v2"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="instrument-map source changed"):
        scout._assert_instrument_map_unchanged(artifact)


def test_omitted_instrument_map_derives_and_rechecks_exact_manifest_projection(tmp_path: Path) -> None:
    roots = {venue: tmp_path / venue for venue in ("bybit", "binance")}
    for venue, root in roots.items():
        for day in ("2026-01-01", "2026-01-02"):
            path = root / "archive_trade_manifest" / f"date={day}" / "part.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            pl.DataFrame(
                {
                    "symbol": ["AAAUSDT"],
                    "date": [day],
                    "url": [f"fixture://{venue}/{day}"],
                    "source": [f"{venue}_archive"],
                    "ignored_outcome": [999.0],
                }
            ).write_parquet(path)

    entries, version, artifact = scout._resolve_phase0_instrument_map(
        path=None,
        version_override=None,
        roots=roots,
        start_date="2026-01-01",
        end_date_exclusive="2026-01-03",
        batch_size=64,
    )

    assert artifact["status"] == "ready"
    assert artifact["source_kind"] == "auto_derived_archive_trade_manifest_symbol_date_projection"
    assert artifact["auto_derived"] is True
    assert artifact["source_window"] == {
        "start_date": "2026-01-01",
        "end_date_exclusive": "2026-01-03",
    }
    assert artifact["source_projection_row_count"] == 4
    assert artifact["source_registered_window_complete"] is True
    assert artifact["cross_venue_portability_ready"] is False
    assert len(entries) == 2
    assert version and "-source-" in version
    assert {entry.canonical_instrument.split("::", 1)[0] for entry in entries} == {"BYBIT", "BINANCE"}

    inventory = {
        "field_availability": {
            venue: {
                "archive_trade_manifest": {
                    "row_count": artifact["source_projection"]["venues"][venue]["storage_row_count_in_window"],
                    "key_provenance_projection_sha256": artifact["source_projection"]["venues"][venue][
                        "storage_key_provenance_projection_sha256"
                    ],
                }
            }
            for venue in ("bybit", "binance")
        }
    }
    scout._assert_auto_map_matches_phase0_inventory(artifact, inventory)
    scout._assert_phase0_instrument_map_unchanged(
        artifact,
        path=None,
        version_override=None,
        roots=roots,
        start_date="2026-01-01",
        end_date_exclusive="2026-01-03",
        batch_size=64,
    )

    path = roots["bybit"] / "archive_trade_manifest" / "date=2026-01-02" / "part.parquet"
    pl.read_parquet(path).with_columns(pl.lit("fixture://bybit/changed").alias("url")).write_parquet(path)
    with pytest.raises(RuntimeError, match="source/projection changed"):
        scout._assert_phase0_instrument_map_unchanged(
            artifact,
            path=None,
            version_override=None,
            roots=roots,
            start_date="2026-01-01",
            end_date_exclusive="2026-01-03",
            batch_size=64,
        )


def test_incomplete_auto_map_is_bundled_but_not_consumed_by_phase0(tmp_path: Path) -> None:
    roots = {venue: tmp_path / venue for venue in ("bybit", "binance")}
    for venue, root in roots.items():
        path = root / "archive_trade_manifest" / "date=2026-01-01" / "part.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "symbol": ["AAAUSDT"],
                "date": ["2026-01-01"],
                "url": [f"fixture://{venue}"],
                "source": [venue],
            }
        ).write_parquet(path)

    entries, version, artifact = scout._resolve_phase0_instrument_map(
        path=None,
        version_override=None,
        roots=roots,
        start_date="2026-01-01",
        end_date_exclusive="2026-01-03",
        batch_size=64,
    )

    assert entries == []
    assert version is None
    assert artifact["status"] == "partial"
    assert artifact["entry_count"] == 2
    assert artifact["phase0_consumed_entry_count"] == 0
    assert artifact["source_registered_window_complete"] is False


def test_registered_design_loader_rejects_deleted_required_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_contract, original_analysis = scout.CANONICAL_CHILDREN["continuous"]
    source_contract = original_contract.with_name(original_contract.name.replace(".md", ".template.md"))
    source_analysis = original_analysis.with_name(
        original_analysis.name.replace(".analysis.json", ".analysis.template.json")
    )
    canonical_contract = tmp_path / original_contract.name
    canonical_analysis = tmp_path / original_analysis.name
    template_contract = canonical_contract.with_name(canonical_contract.name.replace(".md", ".template.md"))
    template_analysis = canonical_analysis.with_name(
        canonical_analysis.name.replace(".analysis.json", ".analysis.template.json")
    )
    template_contract.write_bytes(source_contract.read_bytes())
    payload = json.loads(source_analysis.read_text(encoding="utf-8"))
    payload["required_phase0_substitutions"].pop("source_function_hashes")
    template_analysis.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        scout,
        "CANONICAL_CHILDREN",
        {
            "continuous": (canonical_contract, canonical_analysis),
            "long": scout.CANONICAL_CHILDREN["long"],
        },
    )

    with pytest.raises(RuntimeError, match="required-key set changed"):
        scout._load_registered_child_designs()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("gate", "S01/S02 gate contract changed"),
        ("receipt_policy", "receipt policy changed"),
        ("artifact_inventory", "durable artifact inventory is invalid"),
    ],
)
def test_registered_design_loader_rejects_weakened_population_or_semantic_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    original_contract, original_analysis = scout.CANONICAL_CHILDREN["continuous"]
    source_contract = original_contract.with_name(original_contract.name.replace(".md", ".template.md"))
    source_analysis = original_analysis.with_name(
        original_analysis.name.replace(".analysis.json", ".analysis.template.json")
    )
    canonical_contract = tmp_path / original_contract.name
    canonical_analysis = tmp_path / original_analysis.name
    template_contract = canonical_contract.with_name(canonical_contract.name.replace(".md", ".template.md"))
    template_analysis = canonical_analysis.with_name(
        canonical_analysis.name.replace(".analysis.json", ".analysis.template.json")
    )
    template_contract.write_bytes(source_contract.read_bytes())
    payload = json.loads(source_analysis.read_text(encoding="utf-8"))
    if mutation == "gate":
        payload["required_s01_outputs_before_s02"]["full_reconstruction_verification"] = "OPTIONAL"
    elif mutation == "receipt_policy":
        payload["receipt_policy"]["semantic_s02_receipt_required_before_s03"] = False
    else:
        payload["required_artifacts"].remove("source_keys.jsonl")
    template_analysis.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        scout,
        "CANONICAL_CHILDREN",
        {
            "continuous": (canonical_contract, canonical_analysis),
            "long": scout.CANONICAL_CHILDREN["long"],
        },
    )

    with pytest.raises(RuntimeError, match=message):
        scout._load_registered_child_designs()


def _write_partial_phase0_root(root: Path, *, source: str) -> None:
    date = "2023-02-23"
    ts_ms = int(dt.datetime(2023, 2, 23, tzinfo=dt.timezone.utc).timestamp() * 1_000)
    kline = root / "klines_1h" / f"date={date}" / "part.parquet"
    kline.parent.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["AAAUSDT"],
            "ts_ms": [ts_ms],
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
            "volume_base": [100.0],
            "turnover_quote": [1_000.0],
            "source": [source],
        }
    ).write_parquet(kline)
    manifest = root / "archive_trade_manifest" / f"date={date}" / "part.parquet"
    manifest.parent.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["AAAUSDT"],
            "date": [date],
            "url": [f"fixture://{source}"],
            "source": [source],
        }
    ).write_parquet(manifest)
    pl.DataFrame(
        {
            "symbol": ["AAAUSDT"],
            "ts_ms": [ts_ms],
            "residual_momentum": [0.1],
            "is_provisional": [False],
            "source": [source],
        }
    ).write_parquet(root / "residual_momentum.parquet")


def test_full_phase0_wrapper_identity_is_invariant_to_numeric_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bybit = tmp_path / "bybit"
    binance = tmp_path / "binance"
    _write_partial_phase0_root(bybit, source="bybit_public_trading_archive")
    _write_partial_phase0_root(binance, source="binance_public_data_archive")
    sources = scout._source_receipts()
    configs = scout._config_receipts()
    source_snapshot = _fake_source_snapshot()
    monkeypatch.setattr(scout, "_capture_source_snapshot", lambda: source_snapshot)
    monkeypatch.setattr(scout, "_source_receipts", lambda: sources)
    monkeypatch.setattr(scout, "_config_receipts", lambda: configs)
    monkeypatch.setattr(
        scout,
        "_environment_receipt",
        _exact_environment_fixture,
    )

    def args(output_root: Path) -> argparse.Namespace:
        return argparse.Namespace(
            plan=False,
            phase0_inventory=True,
            bybit_root=str(bybit),
            binance_root=str(binance),
            deep_root_hash=False,
            write_plan=None,
            output_root=output_root,
            instrument_map=None,
            instrument_map_version=None,
            batch_size=1_024,
        )

    first_output = tmp_path / "first"
    assert scout.run_phase0_inventory(args(first_output)) == 2
    capsys.readouterr()
    first_dir = next(first_output.iterdir())
    receipt = json.loads((first_dir / "receipt.json").read_text(encoding="utf-8"))
    receipt_hashes = {row["path"]: row["sha256"] for row in receipt["files"]}
    required_extra_artifacts = {
        "environment_manifest.json",
        "instrument_map_input.json",
        "config_artifact_index.json",
        "continuous_canonical_config.json",
        "continuous_component_config.json",
        "continuous_config_identity.json",
        "continuous_registered_scope.json",
        "long_canonical_config.json",
        "long_config_identity.json",
        "long_registered_scope.json",
        "registered_child_designs.json",
        "s02_config_parity_manifest.json",
        "source_snapshot.json",
        "support_design_and_counts.json",
        "s01_template_input_status.json",
        "tracked_worktree.patch",
        "untracked_sources.tar",
        "untracked_sources_manifest.json",
    }
    assert required_extra_artifacts <= set(receipt_hashes)
    for name in required_extra_artifacts:
        data = (first_dir / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == receipt_hashes[name]
    s01_status = json.loads((first_dir / "s01_template_input_status.json").read_text(encoding="utf-8"))
    for sleeve in ("continuous", "long"):
        resolved = s01_status["sleeves"][sleeve]["resolved"]
        assert resolved["worktree_policy"] == "verified_clean_snapshot"
        assert resolved["patch_bundle_sha256"] == receipt_hashes["tracked_worktree.patch"]
        assert resolved["untracked_source_bundle_sha256"] == receipt_hashes["untracked_sources.tar"]
        assert resolved["environment_lock_path"] == "environment_manifest.json"
        assert resolved["environment_lock_sha256"] == receipt_hashes["environment_manifest.json"]
        assert resolved["canonical_config_json_path"] == f"{sleeve}_canonical_config.json"
        assert resolved["canonical_config_sha256"] == receipt_hashes[f"{sleeve}_canonical_config.json"]
        assert resolved["config_identity_json_path"] == f"{sleeve}_config_identity.json"
        assert resolved["config_identity_sha256"] == receipt_hashes[f"{sleeve}_config_identity.json"]
        assert resolved["registered_scope_json_path"] == f"{sleeve}_registered_scope.json"
        assert resolved["registered_scope_sha256"] == receipt_hashes[f"{sleeve}_registered_scope.json"]
        assert resolved["s02_config_parity_manifest_path"] == "s02_config_parity_manifest.json"
        assert resolved["s02_config_parity_manifest_sha256"] == receipt_hashes["s02_config_parity_manifest.json"]
        assert s01_status["sleeves"][sleeve]["implementation_blockers"] == {}
    continuous_resolved = s01_status["sleeves"]["continuous"]["resolved"]
    assert continuous_resolved["component_config_json_path"] == "continuous_component_config.json"
    assert continuous_resolved["component_config_sha256"] == receipt_hashes["continuous_component_config.json"]
    assert s01_status["s02_config_parity_status"] == "WIRED"
    assert s01_status["config_consumer_wiring_verified_prospectively"] is True
    assert s01_status["real_s00_s04_identity_chain_instantiated"] is False
    assert s01_status["config_artifact_index_file_sha256"] == receipt_hashes["config_artifact_index.json"]

    for root in (bybit, binance):
        kline_path = next((root / "klines_1h").rglob("*.parquet"))
        pl.read_parquet(kline_path).with_columns(
            (pl.col("open") * 17).alias("open"),
            (pl.col("high") * 19).alias("high"),
            (pl.col("low") * 11).alias("low"),
            (pl.col("close") * 23).alias("close"),
            (pl.col("volume_base") * 29).alias("volume_base"),
            (pl.col("turnover_quote") * 31).alias("turnover_quote"),
        ).write_parquet(kline_path)
        rmom_path = root / "residual_momentum.parquet"
        pl.read_parquet(rmom_path).with_columns(
            (pl.col("residual_momentum") * -999).alias("residual_momentum")
        ).write_parquet(rmom_path)

    second_output = tmp_path / "second"
    assert scout.run_phase0_inventory(args(second_output)) == 2
    capsys.readouterr()
    second_dir = next(second_output.iterdir())

    assert first_dir.name == second_dir.name
    assert (first_dir / "receipt.json").read_bytes() == (second_dir / "receipt.json").read_bytes()

    environment_calls = {"count": 0}

    def changing_environment() -> dict[str, object]:
        payload = _exact_environment_fixture()
        environment_calls["count"] += 1
        if environment_calls["count"] > 1:
            payload["python"] = {"version": "changed-during-scan"}
        return payload

    monkeypatch.setattr(scout, "_environment_receipt", changing_environment)
    with pytest.raises(RuntimeError, match="environment changed during Phase-0 scan"):
        scout.run_phase0_inventory(args(tmp_path / "environment-race"))
    assert not (tmp_path / "environment-race").exists()

    monkeypatch.setattr(scout, "_environment_receipt", _exact_environment_fixture)
    config_calls = {"count": 0}

    def drifting_configs() -> dict[str, object]:
        payload = json.loads(json.dumps(configs))
        config_calls["count"] += 1
        if config_calls["count"] > 1:
            payload["s02_config_parity_manifest"]["status"] = "DRIFTED_DURING_SCAN"
        return payload

    monkeypatch.setattr(scout, "_config_receipts", drifting_configs)
    with pytest.raises(RuntimeError, match="config identities changed during Phase-0 scan"):
        scout.run_phase0_inventory(args(tmp_path / "config-race"))
    assert not (tmp_path / "config-race").exists()


def test_phase0_bundle_is_immutable_and_reusable(tmp_path: Path) -> None:
    identity = {"schema_version": 1, "input": "abc"}
    inventory = _phase0_inventory()
    run_dir, reused = scout._write_phase0_bundle(
        tmp_path,
        phase0_id="phase0-test",
        identity=identity,
        plan={"mode": "phase0"},
        inventory=inventory,
    )
    assert reused is False
    receipt = json.loads((run_dir / "receipt.json").read_text())
    assert receipt["phase0_id"] == "phase0-test"
    assert receipt["outcome_run_authorized"] is False
    assert (run_dir / "outcome_blind_audit.json").is_file()

    same_dir, reused = scout._write_phase0_bundle(
        tmp_path,
        phase0_id="phase0-test",
        identity=identity,
        plan={"mode": "phase0"},
        inventory=inventory,
    )
    assert same_dir == run_dir
    assert reused is True

    with pytest.raises(RuntimeError, match="deterministic payload"):
        scout._write_phase0_bundle(
            tmp_path,
            phase0_id="phase0-test",
            identity=identity,
            plan={"mode": "different-phase0-plan"},
            inventory=inventory,
        )

    (run_dir / "pit_provenance.json").write_text("tampered\n")
    with pytest.raises(RuntimeError, match="artifact hash mismatch"):
        scout._write_phase0_bundle(
            tmp_path,
            phase0_id="phase0-test",
            identity=identity,
            plan={"mode": "phase0"},
            inventory=inventory,
        )
