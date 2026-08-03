"""Behavioral contract for the demo ledger reset.

Ported 2026-08-03 from textual assertions on the retired bash implementation
to direct tests of ``liquidity_migration.ops.demo_ledger_reset``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from liquidity_migration.ops import demo_ledger_reset as reset
from liquidity_migration.ops.demo_ledger_reset import (
    ACCOUNT_BOUND_UNITS,
    DOWNSTREAM_RESTART_UNITS,
    NON_RESTARTABLE_ONESHOTS,
    OWNER_RESTART_UNITS,
    RESTART_UNITS,
    STOP_UNITS,
    CleanCheckout,
    Execution,
    ResetError,
    ResetOptions,
    build_plan,
    check_account_root_disjointness,
    check_archive_dir_containment,
    parse_options,
    parse_sleeves,
    refresh_existing_targets,
    run_reset,
    validate_real_money_value,
    verify_exclusive_unit_environment,
)


class FakeSystemctl:
    """Recording double for the validated systemctl wrapper."""

    def __init__(
        self,
        *,
        active: set[str] | None = None,
        fail_start: set[str] | None = None,
        fail_stop: set[str] | None = None,
        properties: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.active = set(active or ())
        self.fail_start = set(fail_start or ())
        self.fail_stop = set(fail_stop or ())
        self.properties = dict(properties or {})
        self.calls: list[tuple[str, str]] = []

    def show_value(self, unit: str, property_name: str) -> str | None:
        self.calls.append((f"show:{property_name}", unit))
        if (unit, property_name) in self.properties:
            return self.properties[(unit, property_name)]
        if property_name == "LoadState":
            return "loaded"
        if property_name == "ActiveState":
            return "active" if unit in self.active else "inactive"
        return ""

    def is_active(self, unit: str) -> bool:
        self.calls.append(("is-active", unit))
        return unit in self.active

    def stop(self, unit: str) -> bool:
        self.calls.append(("stop", unit))
        if unit in self.fail_stop:
            return False
        self.active.discard(unit)
        return True

    def start(self, unit: str) -> bool:
        self.calls.append(("start", unit))
        if unit in self.fail_start:
            return False
        self.active.add(unit)
        return True

    def reset_failed(self, unit: str) -> bool:
        self.calls.append(("reset-failed", unit))
        return True


def _fixture_repository(tmp_path: Path) -> Path:
    repository = tmp_path
    (repository / "liquidity_migration").mkdir(exist_ok=True)
    (repository / "data").mkdir(exist_ok=True)
    env_file = repository / "account-execution.env"
    env_file.write_text(
        "ACCOUNT_EXECUTION_KERNEL_REQUIRED=1\n"
        "ACCOUNT_EXECUTION_ROOT=data/bybit-demo-account\n"
        "ACCOUNT_INTENT_INBOX_ROOT=data/bybit-demo-account-inbox\n"
        "ACCOUNT_CAPTURE_ROOT=data/bybit-demo-account-capture\n",
        encoding="utf-8",
    )
    return repository


def _dry_run(repository: Path, *arguments: str, capsys: pytest.CaptureFixture) -> str:
    exit_code = run_reset(
        ["--account-env-file", str(repository / "account-execution.env"), *arguments],
        repository=repository,
    )
    assert exit_code == 0
    return capsys.readouterr().out


def test_reset_defaults_to_dry_run_before_any_service_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    repository = _fixture_repository(tmp_path)
    out = _dry_run(repository, capsys=capsys)
    assert "mode: dry-run" in out
    assert "DRY RUN: no services or files were changed." in out
    # The preview names the execute refusals instead of performing any of them.
    assert "Execute will refuse REAL_MONEY" in out


def test_dry_run_previews_existing_targets_without_traversal(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    repository = _fixture_repository(tmp_path)
    (repository / "data/bybit-long-demo-event/long_native_demo_cycles").mkdir(parents=True)
    out = _dry_run(repository, "--sleeves", "long", capsys=capsys)
    assert "sleeves: long" in out
    assert (
        "    - data/bybit-long-demo-event/long_native_demo_cycles "
        "(present; size not traversed during preview)" in out
    )
    assert "existing targets: 1" in out


def test_dry_run_hint_reconstructs_the_selected_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    repository = _fixture_repository(tmp_path)
    out = _dry_run(
        repository,
        "--sleeves",
        "carry",
        "--label",
        "exit-overhaul",
        "--include-reports",
        "--leave-stopped",
        capsys=capsys,
    )
    assert (
        "scripts/maintain/reset_demo_ledgers.sh --execute --sleeves carry "
        "--label exit-overhaul --include-reports --leave-stopped" in out
    )


def test_reset_rejects_real_money_and_bad_routes_before_service_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    repository = _fixture_repository(tmp_path)
    monkeypatch.setenv("REAL_MONEY", "true")
    with pytest.raises(ResetError, match="selects mainnet"):
        run_reset(
            ["--account-env-file", str(repository / "account-execution.env")],
            repository=repository,
        )
    monkeypatch.delenv("REAL_MONEY")

    (repository / "account-execution.env").write_text(
        "ACCOUNT_EXECUTION_KERNEL_REQUIRED=1\n"
        "ACCOUNT_EXECUTION_ROOT=/etc/passwd-adjacent\n"
        "ACCOUNT_INTENT_INBOX_ROOT=data/inbox\n"
        "ACCOUNT_CAPTURE_ROOT=data/capture\n",
        encoding="utf-8",
    )
    with pytest.raises(ResetError, match="must stay below"):
        run_reset(
            ["--account-env-file", str(repository / "account-execution.env")],
            repository=repository,
        )


@pytest.mark.parametrize(
    "value",
    ["", "0", "false", "no", "off", "__unset__"],
)
def test_real_money_falsy_values_pass(value: str) -> None:
    validate_real_money_value("test", value)


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_real_money_truthy_values_die(value: str) -> None:
    with pytest.raises(ResetError, match="selects mainnet"):
        validate_real_money_value("test", value)


def test_real_money_ambiguous_values_die() -> None:
    with pytest.raises(ResetError, match="ambiguous REAL_MONEY"):
        validate_real_money_value("test", "maybe")


def test_account_roots_must_be_disjoint_from_each_other_and_strategy_roots(
    tmp_path: Path,
) -> None:
    with pytest.raises(ResetError, match="pairwise disjoint"):
        check_account_root_disjointness(
            tmp_path, ("data/account", "data/account/inbox", "data/capture")
        )
    with pytest.raises(ResetError, match="pairwise disjoint"):
        check_account_root_disjointness(
            tmp_path,
            ("data/bybit-long-demo-event/nested", "data/inbox", "data/capture"),
        )
    check_account_root_disjointness(tmp_path, ("data/a", "data/b", "data/c"))


def test_sleeve_selection_is_canonical_and_rejects_unknowns() -> None:
    assert parse_sleeves("all") == ("long", "continuous", "carry")
    assert parse_sleeves("carry,long") == ("long", "carry")
    assert parse_sleeves("CONTINUOUS") == ("continuous",)
    with pytest.raises(SystemExit) as excinfo:
        parse_sleeves("margin")
    assert excinfo.value.code == 2
    with pytest.raises(ResetError, match="--sleeves must not be empty"):
        parse_sleeves(" , ")


def test_option_validation_matches_the_retired_shell() -> None:
    with pytest.raises(ResetError, match="--label must match"):
        parse_options(["--label", "-bad"])
    with pytest.raises(ResetError, match="must not exceed 60"):
        parse_options(["--settle-seconds", "61"])
    with pytest.raises(ResetError, match="must be an integer"):
        parse_options(["--settle-seconds", "-1"])
    with pytest.raises(ResetError, match="--archive-dir must not be empty"):
        parse_options(["--archive-dir", ""])
    with pytest.raises(SystemExit) as excinfo:
        parse_options(["--frobnicate"])
    assert excinfo.value.code == 2


def test_plan_composition_dedupes_and_appends_reports_and_caches() -> None:
    options = ResetOptions(sleeves_raw="long,long", include_reports=True, include_caches=True)
    plan = build_plan(options, ("data/account", "data/inbox", "data/capture"))
    assert plan.selected_sleeves == ("long",)
    assert plan.selected_roots == ("data/bybit-long-demo-event",)
    assert plan.targets.count("data/bybit-long-demo-event/strategy_event_tape.jsonl") == 1
    assert "data/bybit-long-demo-event/reports" in plan.targets
    assert "data/bybit-long-demo-event/.cache" in plan.targets
    assert plan.targets[-2:] == (
        "data/bybit-long-demo-event/reports",
        "data/bybit-long-demo-event/.cache",
    )


def test_refresh_existing_targets_refuses_non_data_and_traversal_targets(
    tmp_path: Path,
) -> None:
    plan = build_plan(ResetOptions(), ("data/bybit-a", "data/bybit-b", "data/bybit-c"))
    object.__setattr__(plan, "targets", ("configs/secrets",))
    with pytest.raises(ResetError, match="non-data target"):
        refresh_existing_targets(plan, tmp_path)
    object.__setattr__(plan, "targets", ("data/bybit-../escape",))
    with pytest.raises(ResetError, match="traversal target"):
        refresh_existing_targets(plan, tmp_path)


def test_archive_dir_must_be_outside_reset_targets(tmp_path: Path) -> None:
    with pytest.raises(ResetError, match="must be outside reset targets"):
        check_archive_dir_containment(
            "data/bybit-long-demo-event/_archive",
            ("data/bybit-long-demo-event",),
            tmp_path,
        )
    with pytest.raises(ResetError, match="must not contain reset targets"):
        check_archive_dir_containment("data", ("data/bybit-long-demo-event",), tmp_path)
    check_archive_dir_containment("data/_archive", ("data/bybit-long-demo-event",), tmp_path)


def test_stop_order_quiesces_producers_before_the_account_owner() -> None:
    owner = "liquidity-migration-account-execution.service"
    assert STOP_UNITS[-1] == owner
    for producer in (
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-continuous-demo.service",
        "liquidity-migration-bybit-carry-demo.service",
    ):
        assert STOP_UNITS.index(producer) < STOP_UNITS.index(owner)
    # Restart is the reverse handoff: the owner starts first, producers follow.
    assert RESTART_UNITS[0] == owner
    assert OWNER_RESTART_UNITS == (owner,)
    assert owner in ACCOUNT_BOUND_UNITS
    for oneshot in NON_RESTARTABLE_ONESHOTS:
        assert oneshot in STOP_UNITS
        assert oneshot not in RESTART_UNITS


def test_restart_handoff_is_owner_first_with_settle_and_verification() -> None:
    systemctl = FakeSystemctl()
    execution = Execution(
        systemctl=systemctl,  # type: ignore[arg-type]
        options=ResetOptions(settle_seconds=0),
        active_before=RESTART_UNITS,
    )
    assert execution.restart_previously_active("test")
    starts = [unit for verb, unit in systemctl.calls if verb == "start"]
    assert starts == list(RESTART_UNITS)
    assert starts[0] == "liquidity-migration-account-execution.service"


def test_failed_owner_start_leaves_downstream_stopped() -> None:
    owner = "liquidity-migration-account-execution.service"
    systemctl = FakeSystemctl(fail_start={owner})
    execution = Execution(
        systemctl=systemctl,  # type: ignore[arg-type]
        options=ResetOptions(settle_seconds=0),
        active_before=RESTART_UNITS,
    )
    assert not execution.restart_previously_active("test")
    starts = [unit for verb, unit in systemctl.calls if verb == "start"]
    assert starts == [owner]
    assert not any(unit in starts for unit in DOWNSTREAM_RESTART_UNITS)


def test_failed_handoff_fails_closed_to_every_unit_stopped() -> None:
    owner = "liquidity-migration-account-execution.service"
    systemctl = FakeSystemctl(fail_start={owner})
    execution = Execution(
        systemctl=systemctl,  # type: ignore[arg-type]
        options=ResetOptions(settle_seconds=0),
        active_before=RESTART_UNITS,
        services_stopped=True,
        failure_recovery_allowed=True,
    )
    execution.fail_closed_cleanup()
    stops = [unit for verb, unit in systemctl.calls if verb == "stop"]
    assert stops == list(STOP_UNITS)
    assert not systemctl.active


def test_no_auto_restart_after_the_destructive_boundary() -> None:
    systemctl = FakeSystemctl()
    execution = Execution(
        systemctl=systemctl,  # type: ignore[arg-type]
        options=ResetOptions(settle_seconds=0),
        active_before=RESTART_UNITS,
        services_stopped=True,
        failure_recovery_allowed=False,
    )
    execution.fail_closed_cleanup()
    starts = [unit for verb, unit in systemctl.calls if verb == "start"]
    assert starts == []
    stops = [unit for verb, unit in systemctl.calls if verb == "stop"]
    assert stops == list(STOP_UNITS)


def test_leave_stopped_failure_never_restarts() -> None:
    systemctl = FakeSystemctl()
    execution = Execution(
        systemctl=systemctl,  # type: ignore[arg-type]
        options=ResetOptions(settle_seconds=0, leave_stopped=True),
        active_before=RESTART_UNITS,
        services_stopped=True,
        failure_recovery_allowed=True,
    )
    execution.fail_closed_cleanup()
    starts = [unit for verb, unit in systemctl.calls if verb == "start"]
    assert starts == []


def test_exclusive_environment_accepts_only_the_selected_file(tmp_path: Path) -> None:
    expected = tmp_path / "bybit-demo.env"
    expected.write_text("BYBIT_DEMO_API_KEY=k\n", encoding="utf-8")
    systemctl = FakeSystemctl(
        properties={
            ("owner", "EnvironmentFiles"): f"{expected} (ignore_errors=no)",
            ("owner", "Environment"): "",
        }
    )
    verify_exclusive_unit_environment(
        systemctl,  # type: ignore[arg-type]
        "owner",
        expected,
        frozenset({"REAL_MONEY", "DEMO"}),
        protected_prefix="BYBIT_",
        failure="refused",
    )


def test_exclusive_environment_rejects_a_conflicting_second_file(tmp_path: Path) -> None:
    expected = tmp_path / "bybit-demo.env"
    expected.write_text("BYBIT_DEMO_API_KEY=k\n", encoding="utf-8")
    rogue = tmp_path / "rogue.env"
    rogue.write_text("BYBIT_DEMO_API_KEY=other\n", encoding="utf-8")
    systemctl = FakeSystemctl(
        properties={
            ("owner", "EnvironmentFiles"): (
                f"{expected} (ignore_errors=no)\n{rogue} (ignore_errors=yes)"
            ),
            ("owner", "Environment"): "",
        }
    )
    with pytest.raises(ResetError, match="refused"):
        verify_exclusive_unit_environment(
            systemctl,  # type: ignore[arg-type]
            "owner",
            expected,
            frozenset({"REAL_MONEY", "DEMO"}),
            protected_prefix="BYBIT_",
            failure="refused",
        )


def test_exclusive_environment_rejects_direct_assignments_and_missing_file(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "bybit-demo.env"
    expected.write_text("BYBIT_DEMO_API_KEY=k\n", encoding="utf-8")
    systemctl = FakeSystemctl(
        properties={
            ("owner", "EnvironmentFiles"): f"{expected} (ignore_errors=no)",
            ("owner", "Environment"): "REAL_MONEY=true",
        }
    )
    with pytest.raises(ResetError, match="refused"):
        verify_exclusive_unit_environment(
            systemctl,  # type: ignore[arg-type]
            "owner",
            expected,
            frozenset({"REAL_MONEY", "DEMO"}),
            protected_prefix="BYBIT_",
            failure="refused",
        )
    systemctl = FakeSystemctl(
        properties={("owner", "EnvironmentFiles"): "", ("owner", "Environment"): ""}
    )
    with pytest.raises(ResetError, match="refused"):
        verify_exclusive_unit_environment(
            systemctl,  # type: ignore[arg-type]
            "owner",
            expected,
            frozenset({"REAL_MONEY", "DEMO"}),
            protected_prefix="BYBIT_",
            failure="refused",
        )


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )
    return completed.stdout.strip()


def test_reset_clean_candidate_check_ignores_git_replace_refs(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    tracked = repository / "tracked.txt"
    tracked.write_text("original\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-qm", "original")
    original = _git(repository, "rev-parse", "HEAD")
    tracked.write_text("replacement\n", encoding="utf-8")
    _git(repository, "commit", "-qam", "replacement")
    replacement = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", original)
    _git(repository, "replace", original, replacement)
    tracked.write_text("replacement\n", encoding="utf-8")

    checkout = CleanCheckout(repository)
    status = checkout._status(original)
    # GIT_NO_REPLACE_OBJECTS makes the replace ref invisible, so the doctored
    # worktree is reported dirty instead of silently matching the replacement.
    assert status is not None
    assert "tracked worktree differs from HEAD" in status


def test_clean_candidate_bind_requires_a_real_git_directory(tmp_path: Path) -> None:
    checkout = CleanCheckout(tmp_path)
    with pytest.raises(ResetError, match="real checkout .git directory"):
        checkout.bind()


def test_flatness_guard_reports_conditional_orders(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import liquidity_migration.venue.bybit as bybit

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def get_positions(self, **_kwargs: object) -> list[dict]:
            return []

        def get_open_orders(self, **kwargs: object) -> list[dict]:
            if kwargs.get("order_filter") == "StopOrder":
                return [{"orderId": "c1", "symbol": "TESTUSDT", "orderStatus": "Untriggered"}]
            return []

    monkeypatch.setattr(bybit, "BybitPrivateClient", FakeClient)
    monkeypatch.setattr(bybit, "resolve_demo_credentials", lambda: ("k", "s"))
    with pytest.raises(ResetError, match="not flat"):
        reset.verify_demo_account_flat()
    captured = capsys.readouterr()
    assert "open_orders=1" in captured.err
    assert "TESTUSDT" in captured.err


def test_flatness_guard_passes_a_flat_account(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import liquidity_migration.venue.bybit as bybit

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def get_positions(self, **_kwargs: object) -> list[dict]:
            return [{"size": "0"}]

        def get_open_orders(self, **_kwargs: object) -> list[dict]:
            return []

    monkeypatch.setattr(bybit, "BybitPrivateClient", FakeClient)
    monkeypatch.setattr(bybit, "resolve_demo_credentials", lambda: ("k", "s"))
    reset.verify_demo_account_flat()
    assert "demo-account-flat-ok positions=0 open_orders=0" in capsys.readouterr().out


def test_scrubbed_credentials_expose_exactly_the_frozen_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BYBIT_REAL_API_KEY", "real")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "stale")
    with reset.scrubbed_demo_credentials("1", "false", "key", "secret"):
        assert os.environ["BYBIT_DEMO_API_KEY"] == "key"
        assert os.environ["BYBIT_DEMO_API_SECRET"] == "secret"
        assert "BYBIT_REAL_API_KEY" not in os.environ
        assert "TELEGRAM_BOT_TOKEN" not in os.environ
    assert os.environ["BYBIT_REAL_API_KEY"] == "real"
    assert os.environ["BYBIT_DEMO_API_KEY"] == "stale"


def test_usage_names_the_operator_entry_point() -> None:
    text = reset.usage()
    assert "scripts/maintain/reset_demo_ledgers.sh" in text
    assert "--execute" in text
    assert "never cancels orders or closes positions" in text


def test_shell_wrapper_only_pins_path_and_execs_the_module() -> None:
    wrapper = (
        Path(__file__).resolve().parents[2] / "scripts" / "maintain" / "reset_demo_ledgers.sh"
    ).read_text(encoding="utf-8")
    assert "PATH=/usr/sbin:/usr/bin:/sbin:/bin" in wrapper
    assert 'exec "$PYTHON" -m liquidity_migration.ops.demo_ledger_reset "$@"' in wrapper
    # The wrapper must stay logic-free: no systemctl, tar, lease, or git calls.
    code_lines = "\n".join(
        line for line in wrapper.splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in ("systemctl", "tar ", "flock", "git "):
        assert forbidden not in code_lines
