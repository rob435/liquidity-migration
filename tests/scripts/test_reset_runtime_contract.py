from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "maintain" / "reset_demo_ledgers.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    match = re.search(
        rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}\n",
        _text(),
    )
    assert match is not None, f"missing shell function {name}"
    return match.group(0)


def test_reset_defaults_to_dry_run_before_any_service_mutation() -> None:
    text = _text()
    assert "PATH=/usr/sbin:/usr/bin:/sbin:/bin" in text
    assert "export PATH" in text
    assert 'SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-/usr/bin/systemctl}"' in text
    assert '[[ "$SYSTEMCTL_BIN" == /* && -x "$SYSTEMCTL_BIN" && ! -L "$SYSTEMCTL_BIN" ]]' in text
    assert 'MODE="dry-run"' in text
    dry_run = text.index('if [[ "$MODE" == "dry-run" ]]')
    first_stop = text.index('"$SYSTEMCTL_BIN" stop')
    assert dry_run < first_stop
    assert "DRY RUN: no services or files were changed." in text[dry_run:first_stop]


def test_reset_rejects_real_money_and_bad_routes_before_service_mutation() -> None:
    text = _text()
    first_stop = text.index('"$SYSTEMCTL_BIN" stop')
    prefix = text[:first_stop]
    assert "validate_real_money_value" in prefix
    assert "account execution roots must be pairwise disjoint" in prefix
    assert "--archive-dir must be outside reset targets" in prefix
    assert "--archive-dir must not contain reset targets" in prefix
    archive_checks = prefix[prefix.index('archive_compare="') :]
    assert 'case "$archive_compare/"' in archive_checks
    assert 'case "$target_compare/"' in archive_checks


def test_reset_stops_producers_before_the_account_owner() -> None:
    text = _text()
    units = text[text.index("STOP_UNITS=(") : text.index("ACCOUNT_BOUND_UNITS=(")]
    demo_owner = units.index("liquidity-migration-account-execution.service")
    for producer in (
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-continuous-demo.service",
        "liquidity-migration-bybit-carry-demo.service",
        "liquidity-migration-continuous-hedge.service",
    ):
        assert units.index(producer) < demo_owner


def test_reset_holds_process_and_account_leases_across_archive() -> None:
    text = _text()
    archive = text.index("liquidity_migration.ops.account_reset_archive")
    lease_helper = (
        SCRIPT.parents[2] / "liquidity_migration" / "account" / "account_owner_lease.py"
    ).read_text(encoding="utf-8")
    assert "LOCK_EX | fcntl.LOCK_NB" in lease_helper
    assert "canonical_demo_account_lease_path" in text
    assert text.index("\nacquire_demo_account_lease\n") < archive
    assert archive < text.index('release_demo_account_lease "normal completion"')
    assert "canonical demo-account lease is already held" in text
    assert "failure recovery" in text


def test_reset_opens_the_account_lease_without_path_truncation() -> None:
    text = _text()
    demo = text[text.index("acquire_demo_account_lease()") : text.index("release_demo_account_lease()")]
    nontruncating_open = demo.index('exec 8<>"$DEMO_ACCOUNT_LEASE_PATH"')
    acquire = demo.index("acquire-inherited", nontruncating_open)
    assert nontruncating_open < acquire
    assert "install -d -m 0700" in demo[:nontruncating_open]
    assert "mkdir -p" not in demo
    assert 'exec 8>"' not in demo


def test_reset_and_deploy_share_host_maintenance_lock_with_legacy_bridge() -> None:
    reset = _text()
    deploy = (
        SCRIPT.parents[1].joinpath("deploy_vps_live.sh")
        .read_text(encoding="utf-8")
    )
    legacy_reset = "/run/lock/liquidity-migration-ledger-reset.lock"

    assert "MAINTENANCE_LOCK_DIR=/run/liquidity-migration" in reset
    assert 'MAINTENANCE_LOCK_FILE="$MAINTENANCE_LOCK_DIR/maintenance.lock"' in reset
    assert 'LEGACY_DEPLOY_LOCK_FILE="$MAINTENANCE_LOCK_DIR/deploy.lock"' in reset
    assert "local lock_dir=/run/liquidity-migration" in deploy
    assert '"$lock_dir/maintenance.lock"' in deploy
    assert '"$lock_dir/deploy.lock"' in deploy
    assert legacy_reset in reset and legacy_reset in deploy
    assert "prepare-host" in reset and "acquire-inherited" in reset
    assert "prepare-host" in deploy and "acquire-inherited" in deploy
    assert 'exec 9<"$MAINTENANCE_LOCK_FILE"' in reset
    assert 'exec 6<"$LEGACY_DEPLOY_LOCK_FILE"' in reset
    assert 'exec 5<"$LEGACY_RESET_LOCK_FILE"' in reset
    assert 'exec 9<"$lock_dir/maintenance.lock"' in deploy
    assert 'exec 8<"$lock_dir/deploy.lock"' in deploy
    assert "exec 7</run/lock/liquidity-migration-ledger-reset.lock" in deploy
    assert 'exec 9>"' not in reset and 'exec 9>"' not in deploy
    acquire = reset.index("\n  acquire_host_maintenance_locks\n")
    first_deployed_read = reset.index("[[ -d liquidity_migration && -d data ]]")
    assert acquire < first_deployed_read


def test_execute_binds_clean_candidate_before_deployed_state_and_rechecks_before_clear() -> None:
    text = _text()
    acquire = text.index("\n  acquire_host_maintenance_locks\n")
    bind = text.index("\n  bind_clean_candidate_checkout\n", acquire)
    first_env_read = text.index('[[ -r "$ACCOUNT_ENV_FILE" ]]')
    archive_durable = text.index('echo "  digest sidecar: $SHA_PATH"')
    final_recheck = text.index("\nverify_clean_candidate_checkout\n", archive_durable)
    final_archive_recheck = text.index("account_reset_archive verify", final_recheck)
    destructive_boundary = text.index("FAILURE_RECOVERY_ALLOWED=0", final_recheck)

    assert acquire < bind < first_env_read
    assert archive_durable < final_recheck < final_archive_recheck < destructive_boundary
    clean_git = text[text.index("clean_candidate_git()") : text.index("bind_clean_candidate_checkout()")]
    assert "/usr/bin/env -i" in clean_git
    assert '"PATH=/usr/bin:/bin"' in clean_git
    assert 'HOME=/nonexistent' in clean_git
    assert 'GIT_CONFIG_NOSYSTEM=1' in clean_git
    assert 'GIT_NO_REPLACE_OBJECTS=1' in clean_git
    assert "/usr/bin/git --no-optional-locks" in clean_git
    assert '-C "$PWD"' in clean_git
    assert '--git-dir="$PWD/.git"' in clean_git
    assert '--work-tree="$PWD"' in clean_git
    assert '[[ -d "$PWD/.git" && ! -L "$PWD/.git" ]]' in clean_git
    clean_status = text[
        text.index("clean_candidate_checkout_status()") : text.index("bind_clean_candidate_checkout()")
    ]
    assert 'read-tree "$expected_commit"' in clean_status
    assert 'diff-index --quiet "$expected_commit" --' in clean_status
    assert "ls-files --others --exclude-standard" in clean_status
    assert "repository HEAD changed while candidate cleanliness was bound" in text
    assert "repository HEAD changed during candidate cleanliness recheck" in text


def test_reset_clean_candidate_check_ignores_git_replace_refs(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"],
        check=True,
    )
    tracked = repository / "tracked.txt"
    tracked.write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "original"],
        check=True,
    )
    original = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tracked.write_text("replacement\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "commit", "-qam", "replacement"], check=True)
    replacement = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repository), "checkout", "-q", original], check=True)
    subprocess.run(["git", "-C", str(repository), "replace", original, replacement], check=True)
    tracked.write_text("replacement\n", encoding="utf-8")

    temporary_root = tmp_path / "temporary-indexes"
    temporary_root.mkdir()
    harness = (
        "set -euo pipefail\n"
        "die() { printf '%s\\n' \"$*\" >&2; return 1; }\n"
        f"{_function_source('clean_candidate_git')}\n"
        f"{_function_source('clean_candidate_checkout_status')}\n"
        f"cd {shlex.quote(str(repository))}\n"
        f"clean_candidate_checkout_status {original}\n"
    )
    completed = subprocess.run(
        ["bash", "-c", harness],
        env={**os.environ, "TMPDIR": str(temporary_root)},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "tracked worktree differs from HEAD" in completed.stdout
    assert list(temporary_root.iterdir()) == []


def test_reset_requires_explicit_flatness_including_conditional_orders() -> None:
    text = _text()
    flat = text[text.index("Checking demo/mainnet boundary") : text.index("Archiving ${#EXISTING_TARGETS")]
    assert "get_positions" in flat
    assert "get_open_orders(settle_coin=\"USDT\")" in flat
    assert 'order_filter="StopOrder"' in flat
    assert "open_positions={len(positions)} open_orders={len(orders)}" in flat


def test_reset_clears_account_epochs_in_place_without_retiring_lock_inodes() -> None:
    text = _text()
    invocation = text.index('"$PYTHON" - \\\n  "$DEMO_ACCOUNT_LEASE_PATH"')
    clear = text.index("clear_account_epoch_roots_preserving_locks")
    generic_remove = text.index("generic_remove_args=(remove", clear)
    normalize = text.index("demo_account_normalize_args=(", generic_remove)

    assert invocation < clear < generic_remove < normalize
    assert "preserving persistent locks while clearing" in text[generic_remove:normalize]
    assert '"${ACCOUNT_STATE_TARGETS[@]}"' in text[invocation:clear]
    assert 'rm -rf -- "$target"' not in text
    assert "Finalizing fresh canonical account roots" not in text


def test_reset_revalidates_the_held_owner_lease_in_clear_process() -> None:
    text = _text()
    clear_process = text[text.index('"$PYTHON" - \\\n  "$DEMO_ACCOUNT_LEASE_PATH"') :]
    demo_check = clear_process.index("revalidate_inherited_account_owner_lease(8, sys.argv[1])")
    helper_call = clear_process.index(
        "clear_account_epoch_roots_preserving_locks(sys.argv[3:])"
    )

    assert demo_check < helper_call


def test_reset_restores_private_ownership_and_only_shares_public_demo_inputs() -> None:
    text = _text()
    boundary = text[
        text.index("# Reset runs as root") : text.index(
            'release_demo_account_lease "normal completion"'
        )
    ]
    assert "ROOT_RUNTIME_UID" in boundary and "ROOT_RUNTIME_GID" in boundary
    assert "demo_account_normalize_args" in boundary
    assert "normalize-private" in boundary
    assert "normalize-demo" in boundary
    assert "--continuous-root" in boundary
    for forbidden in ("chown -R", 'find "$root"', "install -d", "mkdir -p"):
        assert forbidden not in boundary


def test_reset_strictly_preflights_runtime_paths_before_owner_leases_and_clear() -> None:
    text = _text()
    stopped = text.index('"$SYSTEMCTL_BIN" stop "$unit"')
    load_state = text.index("unit_load_state=", stopped)
    failed_only = text.index('if [[ "$unit_active_state" == failed ]]', load_state)
    reset_failed = text.index('"$SYSTEMCTL_BIN" reset-failed "$unit"', failed_only)
    literal_inactive = text.index("unit is not literally inactive", reset_failed)
    quiescence = text.index(
        'echo "  quiescence verified (all managed units loaded and inactive)"',
        literal_inactive,
    )
    strict = text.index("account_preflight_args=(preflight", quiescence)
    demo = text.index("preflight-demo", strict)
    demo_lease = text.index("\nacquire_demo_account_lease\n", demo)
    destructive = text.index("FAILURE_RECOVERY_ALLOWED=0", demo_lease)

    assert stopped < load_state < failed_only < reset_failed < literal_inactive < quiescence
    assert quiescence < strict < demo < demo_lease < destructive
    assert "--reject-symlinks" in text[strict:demo]


def test_reset_never_auto_restarts_after_destructive_epoch_clear_begins() -> None:
    text = _text()
    archive_durable = text.index('echo "  digest sidecar: $SHA_PATH"')
    disable_recovery = text.index("FAILURE_RECOVERY_ALLOWED=0", archive_durable)
    destructive_clear = text.index("Removing only archived generated projections", archive_durable)

    assert archive_durable < disable_recovery < destructive_clear


def test_failed_post_clear_handoff_stops_and_verifies_every_managed_unit() -> None:
    text = _text()
    cleanup = text[text.index("cleanup() {") : text.index("trap cleanup EXIT")]
    failed_stop = text.index("stop_all_managed_units_after_failed_handoff()")
    cleanup_stop = cleanup.index("stop_all_managed_units_after_failed_handoff")
    stopped_release = cleanup.index('release_demo_account_lease "failed stopped handoff"')

    assert failed_stop < text.index("cleanup() {")
    assert cleanup_stop < stopped_release
    assert "trap '' INT TERM" in cleanup[:cleanup_stop]
    assert '"$SYSTEMCTL_BIN" stop "$unit"' in text[failed_stop : text.index("cleanup() {")]
    assert '"$SYSTEMCTL_BIN" is-active --quiet "$unit"' in text[
        failed_stop : text.index("cleanup() {")
    ]


