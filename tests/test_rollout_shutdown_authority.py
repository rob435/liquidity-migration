from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from liquidity_migration.candidate_rule_coverage import (
    REGISTERED_MAX_RULE_AGE_SECONDS,
)


ROOT = Path(__file__).resolve().parents[1]


def _module() -> Any:
    path = ROOT / "scripts" / "verify_rollout_shutdown_authority.py"
    spec = importlib.util.spec_from_file_location(
        "verify_rollout_shutdown_authority_test",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Authority:
    def __init__(self, *, verified_ts_ns: int, error: str) -> None:
        self.verified_ts_ns = verified_ts_ns
        self.error = error
        self.verify_calls = 0
        self.coverage_now_ns: int | None = None
        self.build_candidate_rule_coverage = self._coverage

    def _coverage(self, *_args: Any, **kwargs: Any) -> dict[str, Any]:
        self.coverage_now_ns = kwargs.get("validation_now_ns")
        return {"status": "passed"}

    def verify_operational_authorization(self, **_kwargs: Any) -> dict[str, Any]:
        self.verify_calls += 1
        if self.verify_calls == 1:
            raise ValueError(self.error)
        self.build_candidate_rule_coverage("candidate", "rules")
        return {"profile": "operational", "authorized_commit": "a" * 40}

    def _load_receipt(self, _path: Path) -> tuple[object, dict[str, Any]]:
        return object(), {"profile": "operational"}

    def _parse_environment_snapshots(
        self,
        _profile: str,
    ) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
        return {}, {
            "account-execution.env": {
                "ACCOUNT_DEMO_RULES_FILE": "/rules.json",
            }
        }

    def read_stable_file(self, *_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            data=json.dumps({"verified_ts_ns": self.verified_ts_ns}).encode()
        )


def test_shutdown_verifier_relaxes_only_genuine_rule_expiry() -> None:
    module = _module()
    now_ns = 2_000_000_000_000_000_000
    verified_ts_ns = now_ns - (
        REGISTERED_MAX_RULE_AGE_SECONDS + 1
    ) * 1_000_000_000
    authority = _Authority(
        verified_ts_ns=verified_ts_ns,
        error="demo rules receipt is stale or future-dated",
    )

    payload = module.verify_rollout_shutdown_authority(
        receipt_path=Path("/receipt"),
        repo_root=Path("/repo"),
        now_ns=now_ns,
        authority_module=authority,
        include_rollout_status=True,
    )

    assert payload["authorized_commit"] == "a" * 40
    assert payload["_rollout_shutdown_expired_demo_rules"] is True
    assert authority.verify_calls == 2
    assert authority.coverage_now_ns == verified_ts_ns


def test_shutdown_verifier_never_accepts_future_dated_rules() -> None:
    module = _module()
    now_ns = 2_000_000_000_000_000_000
    authority = _Authority(
        verified_ts_ns=now_ns + 1,
        error="demo rules receipt is stale or future-dated",
    )

    with pytest.raises(ValueError, match="future-dated"):
        module.verify_rollout_shutdown_authority(
            receipt_path=Path("/receipt"),
            repo_root=Path("/repo"),
            now_ns=now_ns,
            authority_module=authority,
        )

    assert authority.verify_calls == 1


def test_shutdown_verifier_preserves_every_other_strict_failure() -> None:
    module = _module()
    authority = _Authority(
        verified_ts_ns=1,
        error="operational input changed after authorization",
    )

    with pytest.raises(ValueError, match="input changed"):
        module.verify_rollout_shutdown_authority(
            receipt_path=Path("/receipt"),
            repo_root=Path("/repo"),
            authority_module=authority,
        )

    assert authority.verify_calls == 1
