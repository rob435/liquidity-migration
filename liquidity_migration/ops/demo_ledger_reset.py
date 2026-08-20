"""Archive and rebuild demo account state and strategy projections on the VPS.

Python port of ``scripts/maintain/reset_demo_ledgers.sh`` (2026-08-03): the
shell now only pins PATH and execs this module. Safe defaults are unchanged:

* no flag means DRY RUN (no service or file mutation)
* ``--execute`` is required to stop services, archive, and rebuild projections
* concurrent execute attempts are refused by the host maintenance locks
* execute holds the account-owner lease from quiescence through archive/reset,
  then releases it only for the owner-first restart handoff
* REAL_MONEY/mainnet configuration is refused
* the demo owner must load the same resolved demo credential env file
* the canonical demo account root, inbox, and capture are archived and
  recreated empty in the same maintenance transaction
* the Bybit demo account must have no positions and no open orders
* only initially-active daemons/timers are restarted and verified, unless
  ``--leave-stopped`` is selected for a controlled evidence cutover
* configs, locks, reports, signal files, market-data caches, and the
  account-equity high-water risk state are preserved
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, NoReturn, Sequence

from liquidity_migration.account.account_owner_lease import (
    AccountOwnerLeaseAlreadyHeldError,
    DemoAccountIdentity,
    acquire_inherited_account_owner_lease,
    canonical_demo_account_lease_path,
    revalidate_inherited_account_owner_lease,
)
from liquidity_migration.ops.account_epoch_reset import (
    clear_account_epoch_roots_preserving_locks,
)
from liquidity_migration.ops.account_reset_archive import (
    create_reset_archive,
    verify_reset_archive,
)
from liquidity_migration.ops.maintenance_lock import (
    acquire_inherited_locks,
    prepare_host_maintenance_locks,
)
from liquidity_migration.ops.reset_path_safety import (
    normalize_demo_runtime_roots,
    normalize_private_runtime_roots,
    preflight_demo_runtime_roots,
    preflight_reset_targets,
    remove_reset_targets,
)
from liquidity_migration.policy.systemd_environment import (
    parse_systemd_environment_bytes,
)

_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_STRATEGY_ROOTS = (
    "data/bybit-long-demo-event",
    "data/bybit-carry-demo-event",
)
_SLEEVE_LEDGER_TARGETS = {
    "long": (
        "data/bybit-long-demo-event/long_native_demo_cycles",
        "data/bybit-long-demo-event/strategy_event_tape.jsonl",
        "data/bybit-long-demo-event/strategy_event_decision_tape.jsonl",
        "data/bybit-long-demo-event/strategy_target_scheduling_capture.jsonl",
    ),
    "carry": (
        "data/bybit-carry-demo-event/carry_hold_demo_cycles",
        "data/bybit-carry-demo-event/strategy_event_tape.jsonl",
        "data/bybit-carry-demo-event/strategy_event_decision_tape.jsonl",
        "data/bybit-carry-demo-event/strategy_target_scheduling_capture.jsonl",
    ),
}
_SLEEVE_ROOTS = {
    "long": ("data/bybit-long-demo-event",),
    "carry": ("data/bybit-carry-demo-event",),
}
_SLEEVE_ORDER = ("long", "carry")

# The account is shared, so every target producer and both account owners must
# be quiesced even for a one-sleeve reset. Producers stop before owners; this
# prevents new targets from being queued while their sole consumer is down.
STOP_UNITS = (
    "liquidity-migration-demo-liveness.timer",
    "liquidity-migration-bybit-long-demo.service",
    "liquidity-migration-bybit-carry-demo.service",
    "liquidity-migration-demo-liveness.service",
)
# Empty. A unit belongs here only if it must load exactly the credential file
# selected by --env-file, and the engine — the one venue-mutating process —
# does not read the account route env file at all: it has its own env, its own
# state directory, and its own log.
ACCOUNT_BOUND_UNITS: tuple[str, ...] = ()
NON_RESTARTABLE_ONESHOTS = (
    "liquidity-migration-demo-liveness.service",
)
OWNER_RESTART_UNITS: tuple[str, ...] = ()
DOWNSTREAM_RESTART_UNITS = (
    "liquidity-migration-bybit-long-demo.service",
    "liquidity-migration-bybit-carry-demo.service",
    "liquidity-migration-demo-liveness.timer",
)
RESTART_UNITS = OWNER_RESTART_UNITS + DOWNSTREAM_RESTART_UNITS

_ENVIRONMENT_FILES_PATTERN = re.compile(
    r"(?:^|[\n ])(.+?) \(ignore_errors=(?:yes|no)\)(?=[\n ]|$)"
)


class ResetError(RuntimeError):
    """A fatal reset condition; formatted as ``ERROR: …`` like the shell."""


class _Signalled(BaseException):
    def __init__(self, exit_code: int) -> None:
        super().__init__(exit_code)
        self.exit_code = exit_code


def _die(message: str) -> NoReturn:
    raise ResetError(message)


@dataclass
class ResetOptions:
    mode: str = "dry-run"
    leave_stopped: bool = False
    sleeves_raw: str = "all"
    archive_dir: str = "data/_archive"
    label: str = ""
    include_reports: bool = False
    include_caches: bool = False
    env_file: str = "/etc/liquidity-migration/bybit-demo.env"
    account_env_file: str = ""
    settle_seconds: int = 3


@dataclass
class ResetPlan:
    selected_sleeves: tuple[str, ...]
    account_state_targets: tuple[str, ...]
    targets: tuple[str, ...]
    selected_roots: tuple[str, ...]
    existing_targets: tuple[str, ...] = ()


def usage() -> str:
    return """Usage: scripts/maintain/reset_demo_ledgers.sh [options]

Archive and rebuild demo account state and strategy telemetry.
Canonical account journals begin a new epoch only after the venue
flatness guard; the prior journals remain in the verified archive.
Default mode is a read-only preview. Mutation requires --execute.

Options:
  --execute                 stop writers, verify demo account flat, archive, rebuild,
                            restart previously-active units, and verify them
  --leave-stopped           execute the reset but leave every managed unit stopped;
                            required before explicit demo-owner staging
  --dry-run                 explicit preview (the default)
  --sleeves LIST            all (default), long, carry, or comma-separated list
  --archive-dir DIR         archive destination (default: data/_archive)
  --label LABEL             optional safe suffix added after the UTC timestamp
  --include-reports         also archive/reset reports/ in selected roots
  --include-caches          also archive/reset .cache/ in selected roots (slow rebuild)
  --env-file FILE           demo credential env (default: /etc/liquidity-migration/bybit-demo.env)
  --account-env-file FILE   demo owner route env (default: /etc/liquidity-migration/account-execution.env)
  --settle-seconds N        wait before restart verification (default: 3; max: 60)
  -h, --help                show this help

The account-owner root, intent inbox, raw capture, and strategy telemetry are
archived in full before a fresh epoch is created; the reset additionally
requires venue-flat proof.

The command never removes configs, persistent lock inodes, residual_momentum.parquet,
or root-level market-data datasets. It never cancels orders or closes positions: execute
refuses until the configured Bybit demo account is already flat with no open orders.
Execute also refuses if another deploy/reset maintenance operation holds the shared
host lock or if any submit-armed systemd unit does not load the same resolved
credential env file. The authenticated demo-account lock path has no operator
override; it is derived from Bybit `userID` through the same repository helper as
the owner, rule probe, and venue-accounting command.
"""


def parse_options(argv: Sequence[str]) -> ResetOptions:
    options = ResetOptions(
        account_env_file=os.environ.get(
            "ACCOUNT_EXECUTION_ENV_FILE", "/etc/liquidity-migration/account-execution.env"
        ),
    )
    settle_raw = os.environ.get("LEDGER_RESET_SETTLE_SECONDS", "3")
    arguments = list(argv)
    index = 0

    def value_for(flag: str) -> str:
        nonlocal index
        if index + 1 >= len(arguments):
            _die(f"{flag} requires a value")
        index += 1
        return arguments[index]

    while index < len(arguments):
        argument = arguments[index]
        if argument == "--execute":
            options.mode = "execute"
        elif argument == "--dry-run":
            options.mode = "dry-run"
        elif argument == "--leave-stopped":
            options.leave_stopped = True
        elif argument == "--sleeves":
            options.sleeves_raw = value_for("--sleeves")
        elif argument == "--archive-dir":
            options.archive_dir = value_for("--archive-dir")
        elif argument == "--label":
            options.label = value_for("--label")
        elif argument == "--include-reports":
            options.include_reports = True
        elif argument == "--include-caches":
            options.include_caches = True
        elif argument == "--env-file":
            options.env_file = value_for("--env-file")
        elif argument == "--account-env-file":
            options.account_env_file = value_for("--account-env-file")
        elif argument == "--settle-seconds":
            settle_raw = value_for("--settle-seconds")
        elif argument in ("-h", "--help"):
            print(usage(), end="")
            raise SystemExit(0)
        else:
            print(f"unknown option: {argument}", file=sys.stderr)
            print(usage(), end="", file=sys.stderr)
            raise SystemExit(2)
        index += 1

    if not options.archive_dir:
        _die("--archive-dir must not be empty")
    options.archive_dir = options.archive_dir.rstrip("/") or "/"
    if not re.fullmatch(r"[0-9]+", settle_raw):
        _die("--settle-seconds must be an integer from 0 to 60")
    options.settle_seconds = int(settle_raw)
    if options.settle_seconds > 60:
        _die("--settle-seconds must not exceed 60")
    if options.label and _LABEL_PATTERN.fullmatch(options.label) is None:
        _die("--label must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    return options


def validate_real_money_value(source: str, raw: str) -> None:
    value = raw.lower()
    if value in ("", "0", "false", "no", "off", "__unset__"):
        return
    if value in ("1", "true", "yes", "on"):
        _die(
            f"refusing ledger reset: REAL_MONEY='{raw}' from {source} selects mainnet. "
            "This workflow is demo only."
        )
    _die(
        f"refusing ledger reset: ambiguous REAL_MONEY='{raw}' from {source}. "
        "Use false/off/0 for demo."
    )


def parse_sleeves(raw: str) -> tuple[str, ...]:
    tokens = [token for token in raw.replace(",", " ").split() if token]
    if not tokens:
        _die("--sleeves must not be empty")
    selected = {name: False for name in _SLEEVE_ORDER}
    for token in tokens:
        name = token.lower()
        if name == "all":
            for key in selected:
                selected[key] = True
        elif name in selected:
            selected[name] = True
        else:
            print(f"unknown sleeve: {token}", file=sys.stderr)
            print(
                "expected: all, long, carry, or a comma-separated combination",
                file=sys.stderr,
            )
            raise SystemExit(2)
    return tuple(name for name in _SLEEVE_ORDER if selected[name])


def systemd_env_value(path: Path, key: str) -> str:
    """Read one systemd EnvironmentFile value as data, never by sourcing."""

    try:
        values = parse_systemd_environment_bytes(path.read_bytes(), label=str(path))
    except (OSError, ValueError) as exc:
        _die(str(exc))
    return values.get(key, "")


def repo_data_path(raw: str, repository: Path) -> str:
    candidate = Path(raw).expanduser()
    resolved = (candidate if candidate.is_absolute() else repository / candidate).resolve(
        strict=False
    )
    try:
        relative = resolved.relative_to(repository.resolve())
        relative.relative_to("data")
    except ValueError:
        _die(f"account state path must stay below {repository / 'data'}: {resolved}")
    return str(relative)


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def check_account_root_disjointness(
    repository: Path, account_targets: Sequence[str]
) -> None:
    resolved = [(repository / raw).resolve(strict=False) for raw in account_targets]
    reserved = [(repository / raw).resolve(strict=False) for raw in _STRATEGY_ROOTS]
    for index, left in enumerate(resolved):
        for right in resolved[index + 1 :]:
            if _overlaps(left, right):
                _die(
                    "account execution roots must be pairwise disjoint and separate "
                    f"from strategy roots (overlapping account roots: {left} and {right})"
                )
        for right in reserved:
            if _overlaps(left, right):
                _die(
                    "account execution roots must be pairwise disjoint and separate "
                    f"from strategy roots (account root overlaps strategy root: {left} and {right})"
                )


def build_plan(options: ResetOptions, account_targets: Sequence[str]) -> ResetPlan:
    sleeves = parse_sleeves(options.sleeves_raw)
    targets: list[str] = []
    for sleeve in sleeves:
        for target in _SLEEVE_LEDGER_TARGETS[sleeve]:
            if target not in targets:
                targets.append(target)
    for target in account_targets:
        if target not in targets:
            targets.append(target)
    selected_roots: list[str] = []
    for sleeve in sleeves:
        for root in _SLEEVE_ROOTS[sleeve]:
            if root not in selected_roots:
                selected_roots.append(root)
    if options.include_reports:
        for root in selected_roots:
            if f"{root}/reports" not in targets:
                targets.append(f"{root}/reports")
    if options.include_caches:
        for root in selected_roots:
            if f"{root}/.cache" not in targets:
                targets.append(f"{root}/.cache")
    return ResetPlan(
        selected_sleeves=sleeves,
        account_state_targets=tuple(account_targets),
        targets=tuple(targets),
        selected_roots=tuple(selected_roots),
    )


def refresh_existing_targets(plan: ResetPlan, repository: Path) -> ResetPlan:
    existing: list[str] = []
    for target in plan.targets:
        if not target.startswith("data/bybit-"):
            _die(f"internal safety error: non-data target '{target}'")
        if ".." in target:
            _die(f"internal safety error: traversal target '{target}'")
        path = repository / target
        if path.exists() or path.is_symlink():
            existing.append(target)
    plan.existing_targets = tuple(existing)
    return plan


def check_archive_dir_containment(
    archive_dir: str, targets: Sequence[str], repository: Path
) -> None:
    # The archive must never sit inside anything being archived, or tar
    # consumes its own growing output. Canonicalise through symlinks; a lexical
    # prefix check misses ``target/./_archive``.
    archive_compare = (
        Path(archive_dir)
        if Path(archive_dir).is_absolute()
        else repository / archive_dir
    ).resolve(strict=False)
    for target in targets:
        target_compare = (repository / target).resolve(strict=False)
        if archive_compare == target_compare or target_compare in archive_compare.parents:
            _die(
                f"--archive-dir must be outside reset targets (archive '{archive_dir}', "
                f"target '{target}')"
            )
        if archive_compare in target_compare.parents:
            _die(
                f"--archive-dir must not contain reset targets (archive '{archive_dir}', "
                f"target '{target}')"
            )


class Systemctl:
    """Thin, injectable systemctl runner bound to one validated binary."""

    def __init__(self, binary: str) -> None:
        if not binary.startswith("/") or Path(binary).is_symlink() or not os.access(binary, os.X_OK):
            _die(f"systemctl must be one absolute non-symlink executable: {binary}")
        self.binary = binary

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, *arguments], check=False, capture_output=True, text=True
        )

    def show_value(self, unit: str, property_name: str) -> str | None:
        completed = self._run("show", unit, f"--property={property_name}", "--value")
        if completed.returncode != 0:
            return None
        return completed.stdout.rstrip("\n")

    def is_active(self, unit: str) -> bool:
        return self._run("is-active", "--quiet", unit).returncode == 0

    def stop(self, unit: str) -> bool:
        return self._run("stop", unit).returncode == 0

    def start(self, unit: str) -> bool:
        return self._run("start", unit).returncode == 0

    def reset_failed(self, unit: str) -> bool:
        return self._run("reset-failed", unit).returncode == 0


class CleanCheckout:
    """Scrubbed-environment git cleanliness binding for the execute path."""

    def __init__(self, repository: Path) -> None:
        self.repository = repository
        self.bound_commit = ""

    def _git(self, index_path: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        git_dir = self.repository / ".git"
        if not git_dir.is_dir() or git_dir.is_symlink():
            _die("execute requires a real checkout .git directory")
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
        if index_path:
            environment["GIT_INDEX_FILE"] = index_path
        return subprocess.run(
            [
                "/usr/bin/git",
                "--no-optional-locks",
                f"--git-dir={git_dir}",
                f"--work-tree={self.repository}",
                "-c",
                f"safe.directory={self.repository}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.filemode=true",
                "-C",
                str(self.repository),
                *arguments,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def _head(self) -> str:
        completed = self._git("", "rev-parse", "--verify", "HEAD^{commit}")
        return completed.stdout.strip() if completed.returncode == 0 else ""

    def _status(self, expected_commit: str) -> str | None:
        """Cleanliness findings, empty string when clean, None on failure."""

        with tempfile.TemporaryDirectory(prefix="liqmig-reset-index.") as directory:
            os.chmod(directory, 0o700)
            index_path = str(Path(directory) / "index")
            if self._git(index_path, "read-tree", expected_commit).returncode != 0:
                return None
            refresh = self._git(index_path, "update-index", "--refresh")
            if refresh.returncode > 1:
                return None
            diff = self._git(index_path, "diff-index", "--quiet", expected_commit, "--")
            if diff.returncode > 1:
                return None
            untracked = self._git(index_path, "ls-files", "--others", "--exclude-standard")
            if untracked.returncode != 0:
                return None
            findings: list[str] = []
            if refresh.returncode != 0:
                findings.append("tracked worktree metadata differs from HEAD")
            if diff.returncode != 0:
                findings.append("tracked worktree differs from HEAD")
            if untracked.stdout.strip():
                findings.append(untracked.stdout.strip())
            return "\n".join(findings)

    def bind(self) -> None:
        commit = self._head()
        if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            _die("execute requires a repository checkout with one full Git HEAD")
        status = self._status(commit)
        if status is None:
            _die("cannot verify execute checkout cleanliness")
        if status:
            _die("execute requires an exact clean candidate checkout")
        if self._head() != commit:
            _die("repository HEAD changed while candidate cleanliness was bound")
        self.bound_commit = commit

    def verify(self) -> None:
        if self._head() != self.bound_commit:
            _die("repository HEAD changed during the reset")
        status = self._status(self.bound_commit)
        if status is None:
            _die("cannot recheck execute checkout cleanliness")
        if status:
            _die("execute checkout changed during the reset")
        if self._head() != self.bound_commit:
            _die("repository HEAD changed during candidate cleanliness recheck")


def verify_exclusive_unit_environment(
    systemctl: Systemctl,
    unit: str,
    expected_file: Path,
    protected_keys: frozenset[str],
    *,
    protected_prefix: str | None,
    failure: str,
) -> None:
    """The unit must load ``expected_file`` and nothing else may define the keys."""

    raw_files = systemctl.show_value(unit, "EnvironmentFiles")
    if raw_files is None:
        _die(f"failed to resolve EnvironmentFiles for unit: {unit}")
    raw_environment = systemctl.show_value(unit, "Environment")
    if raw_environment is None:
        _die(f"failed to resolve direct Environment assignments for unit: {unit}")

    def protected(key: str) -> bool:
        if protected_prefix is not None and key.startswith(protected_prefix):
            return True
        return key in protected_keys

    try:
        expected = expected_file.resolve(strict=True)
    except OSError:
        _die(failure)
    found_expected = False
    for value in _ENVIRONMENT_FILES_PATTERN.findall(raw_files or ""):
        try:
            candidate = Path(value).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if candidate == expected:
            found_expected = True
            continue
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip()
            if protected(key):
                _die(failure)
    try:
        direct_assignments = shlex.split(raw_environment or "")
    except ValueError:
        _die(failure)
    for assignment in direct_assignments:
        key = assignment.split("=", 1)[0]
        if "=" in assignment and protected(key):
            _die(failure)
    if not found_expected:
        _die(failure)


@contextlib.contextmanager
def scrubbed_demo_credentials(
    demo: str, real_money: str, api_key: str, api_secret: str
) -> Iterator[None]:
    """Expose exactly the frozen demo credential set; restore the caller env."""

    scrubbed = (
        "DEMO",
        "REAL_MONEY",
        "BYBIT_DEMO_API_KEY",
        "BYBIT_DEMO_API_SECRET",
        "BYBIT_REAL_API_KEY",
        "BYBIT_REAL_API_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_ALERT_CHAT_ID",
    )
    saved = {name: os.environ.get(name) for name in scrubbed}
    try:
        for name in scrubbed:
            os.environ.pop(name, None)
        os.environ["DEMO"] = demo
        os.environ["REAL_MONEY"] = real_money
        os.environ["BYBIT_DEMO_API_KEY"] = api_key
        os.environ["BYBIT_DEMO_API_SECRET"] = api_secret
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _position_amount(row: dict) -> float:
    for key in ("size", "qty", "positionAmt"):
        try:
            value = abs(float(row.get(key) or 0.0))
        except (TypeError, ValueError):
            continue
        if value > 0.0:
            return value
    return 0.0


def resolve_demo_account_lease_path() -> str:
    from liquidity_migration.venue.bybit import BybitPrivateClient, resolve_demo_credentials

    api_key, api_secret = resolve_demo_credentials()
    if not api_key or not api_secret:
        _die("cannot derive the authenticated canonical demo-account lease")
    client = BybitPrivateClient(api_key=api_key, api_secret=api_secret, demo=True)
    identity = DemoAccountIdentity.from_api_key_info(
        api_key=api_key,
        api_key_info=client.get_api_key_information(),
    )
    return str(canonical_demo_account_lease_path(identity))


def verify_demo_account_flat() -> None:
    from liquidity_migration.venue.bybit import BybitPrivateClient, resolve_demo_credentials

    api_key, api_secret = resolve_demo_credentials()
    if not api_key or not api_secret:
        _die(
            "missing BYBIT_DEMO_API_KEY/BYBIT_DEMO_API_SECRET; cannot prove account is flat"
        )
    client = BybitPrivateClient(api_key=api_key, api_secret=api_secret, demo=True)
    positions = [
        row for row in client.get_positions(settle_coin="USDT") if _position_amount(row) > 0.0
    ]
    # Bybit documents the omitted orderFilter as all linear order kinds, but
    # query StopOrder explicitly too rather than trust a default to expose
    # conditionals.
    all_orders = list(client.get_open_orders(settle_coin="USDT"))
    conditional_orders = list(
        client.get_open_orders(settle_coin="USDT", order_filter="StopOrder")
    )
    orders_by_identity: dict[str, dict] = {}
    for source, rows in (("all-kinds", all_orders), ("conditional", conditional_orders)):
        for index, row in enumerate(rows):
            identity = str(row.get("orderId") or row.get("orderLinkId") or f"{source}:{index}")
            orders_by_identity[identity] = row
    orders = list(orders_by_identity.values())
    if positions or orders:
        print(
            f"ERROR: demo account is not flat: open_positions={len(positions)} "
            f"open_orders={len(orders)}. Close/cancel them through the normal demo "
            "workflow, then retry; this reset never does that for you.",
            file=sys.stderr,
        )
        for row in positions[:20]:
            print(
                f"  position symbol={row.get('symbol', '?')} side={row.get('side', '?')} "
                f"size={_position_amount(row):g}",
                file=sys.stderr,
            )
        for row in orders[:20]:
            print(
                f"  order symbol={row.get('symbol', '?')} side={row.get('side', '?')} "
                f"status={row.get('orderStatus', '?')} link={row.get('orderLinkId', '?')}",
                file=sys.stderr,
            )
        raise ResetError(
            "demo account is not flat; close positions and cancel orders first"
        )
    print("  demo-account-flat-ok positions=0 open_orders=0")


@dataclass
class Execution:
    """Mutable fail-closed state mirrored from the shell's globals and traps."""

    systemctl: Systemctl | None
    options: ResetOptions
    active_before: tuple[str, ...] = ()
    services_stopped: bool = False
    restart_complete: bool = False
    failure_recovery_allowed: bool = True
    lease_descriptor: int = -1
    lease_path: str = ""
    lease_held: bool = False
    maintenance_descriptors: tuple[int, ...] = field(default=())

    def _systemctl(self) -> Systemctl:
        assert self.systemctl is not None, "systemctl is bound on the execute path"
        return self.systemctl

    def was_active(self, unit: str) -> bool:
        return unit in self.active_before

    def acquire_maintenance_locks(self) -> None:
        # fd triple mirrors the shell's 9/6/5: the canonical cross-operation
        # mutex plus the two legacy bridges, all held to process exit.
        if os.geteuid() != 0:
            _die("host maintenance locks require root")
        try:
            identities = prepare_host_maintenance_locks(
                run_directory="/run",
                modern_directory_name="liquidity-migration",
                legacy_directory_name="lock",
                expected_uid=0,
                expected_gid=0,
                require_mount_ids=True,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            _die(f"cannot prepare persistent host maintenance locks safely ({exc})")
        paths = (
            "/run/liquidity-migration/maintenance.lock",
            "/run/liquidity-migration/deploy.lock",
            "/run/lock/liquidity-migration-ledger-reset.lock",
        )
        descriptors: list[int] = []
        try:
            for path in paths:
                descriptors.append(os.open(path, os.O_RDONLY | os.O_NOFOLLOW))
            acquire_inherited_locks(
                [
                    (descriptor, path, identity.device, identity.inode)
                    for descriptor, path, identity in zip(descriptors, paths, identities)
                ],
                expected_uid=0,
                expected_gid=0,
                require_mount_ids=True,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            for descriptor in descriptors:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            _die(f"another maintenance operation is active or a lock path changed ({exc})")
        self.maintenance_descriptors = tuple(descriptors)

    def acquire_demo_account_lease(self) -> None:
        parent = Path(self.lease_path).parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            _die("cannot create the canonical demo-account lease directory")
        try:
            descriptor = os.open(self.lease_path, os.O_RDWR | os.O_CREAT, 0o666)
        except OSError:
            _die(
                "cannot open canonical demo-account lease without truncation: "
                f"{self.lease_path}"
            )
        try:
            acquire_inherited_account_owner_lease(
                descriptor, self.lease_path, "demo", "ledger_reset"
            )
        except AccountOwnerLeaseAlreadyHeldError:
            os.close(descriptor)
            _die(f"canonical demo-account lease is already held: {self.lease_path}")
        except (OSError, RuntimeError, ValueError):
            os.close(descriptor)
            _die(
                f"cannot acquire canonical demo-account lease: {self.lease_path} "
                "(no alternate path is allowed)"
            )
        self.lease_descriptor = descriptor
        self.lease_held = True
        print(
            "  canonical demo-account lease acquired for flatness/archive/reset: "
            f"{self.lease_path}"
        )

    def release_demo_account_lease(self, context: str) -> None:
        if self.lease_held:
            with contextlib.suppress(OSError):
                os.close(self.lease_descriptor)
            self.lease_descriptor = -1
            self.lease_held = False
            print(
                "  canonical demo-account lease released for owner-first restart "
                f"handoff ({context})"
            )

    def restart_previously_active(self, context: str) -> bool:
        print()
        print(f"Restarting the previously-active account owner first ({context}) ...")
        failed = False
        for unit in OWNER_RESTART_UNITS:
            if self.was_active(unit):
                if self._systemctl().start(unit):
                    print(f"  started {unit}")
                else:
                    print(f"  FAILED to start {unit}", file=sys.stderr)
                    failed = True
            else:
                print(f"  left inactive {unit} (it was inactive before reset)")
        if failed:
            print("  owner start failed; downstream producers remain stopped", file=sys.stderr)
            return False
        if self.options.settle_seconds > 0:
            print(
                f"Waiting {self.options.settle_seconds}s for account owners before "
                "downstream startup ..."
            )
            time.sleep(self.options.settle_seconds)
        for unit in OWNER_RESTART_UNITS:
            if self.was_active(unit) and not self._systemctl().is_active(unit):
                print(
                    f"  owner did not remain active: {unit}; downstream producers "
                    "remain stopped",
                    file=sys.stderr,
                )
                failed = True
        if failed:
            return False
        print(f"Restarting previously-active downstream producers/timers ({context}) ...")
        for unit in DOWNSTREAM_RESTART_UNITS:
            if self.was_active(unit):
                if self._systemctl().start(unit):
                    print(f"  started {unit}")
                else:
                    print(f"  FAILED to start {unit}", file=sys.stderr)
                    failed = True
            else:
                print(f"  left inactive {unit} (it was inactive before reset)")
        return not failed

    def stop_all_managed_units_after_failed_handoff(self) -> bool:
        print("Stopping every managed unit after the failed reset handoff ...", file=sys.stderr)
        # STOP_UNITS is ordered downstream-before-owner. Repeating the full stop
        # is safe for units that never restarted and closes the partial-topology
        # case.
        failed = False
        for unit in STOP_UNITS:
            if self._systemctl().stop(unit):
                print(f"  stopped {unit}", file=sys.stderr)
            else:
                print(f"  FAILED to stop {unit}", file=sys.stderr)
                failed = True
        for unit in STOP_UNITS:
            if self._systemctl().is_active(unit):
                print(f"  STILL ACTIVE after failed handoff: {unit}", file=sys.stderr)
                failed = True
        return not failed

    def cleanup_notice(self, message: str) -> None:
        # If the transport dies mid-reset, sshd HUPs the remote process group
        # and later stderr writes raise; never let a failed write abort the
        # fail-closed handoff. Mirror each line into the host journal.
        with contextlib.suppress(Exception):
            subprocess.run(
                ["logger", "-t", "liquidity-migration-reset", "-p", "daemon.err", "--", message],
                check=False,
                capture_output=True,
            )
        with contextlib.suppress(Exception):
            print(message, file=sys.stderr)

    def fail_closed_cleanup(self) -> None:
        if not (self.services_stopped and not self.restart_complete):
            return
        # Once cleanup owns the handoff, a second signal must not interrupt the
        # fail-closed stop sequence.
        for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGPIPE):
            with contextlib.suppress(OSError, ValueError):
                signal.signal(signal_number, signal.SIG_IGN)
        if self.failure_recovery_allowed and not self.options.leave_stopped:
            self.release_demo_account_lease("failure recovery")
            self.cleanup_notice(
                "Reset did not complete; attempting to restore pre-reset service state."
            )
            if not self.restart_previously_active("failure recovery"):
                self.cleanup_notice(
                    "Failure recovery produced a partial topology; failing closed."
                )
                if not self.stop_all_managed_units_after_failed_handoff():
                    self.cleanup_notice(
                        "CRITICAL: at least one managed unit could not be stopped."
                    )
        else:
            if self.stop_all_managed_units_after_failed_handoff():
                self.cleanup_notice(
                    "Reset handoff did not complete; managed units are stopped and verified."
                )
            else:
                self.cleanup_notice(
                    "CRITICAL: reset handoff failed and at least one managed unit remains active."
                )
            self.release_demo_account_lease("failed stopped handoff")


def print_plan(options: ResetOptions, plan: ResetPlan) -> None:
    print("Ledger reset plan")
    print(f"  mode: {options.mode}")
    print(f"  sleeves: {' '.join(plan.selected_sleeves)}")
    print(f"  archive dir: {options.archive_dir}")
    print(f"  include reports: {int(options.include_reports)}")
    print(f"  include caches: {int(options.include_caches)}")
    print(f"  leave managed units stopped: {int(options.leave_stopped)}")
    print(f"  canonical account state: {' '.join(plan.account_state_targets)}")
    print(f"  existing targets: {len(plan.existing_targets)}")
    for target in plan.existing_targets:
        # Do not recursively inspect an unvalidated target during preview.
        # Execute performs descriptor/mount preflight after every writer is
        # quiescent.
        print(f"    - {target} (present; size not traversed during preview)")
    print(
        "  preserved by default: configs/, .locks/, reports/, .cache/, "
        "residual_momentum.parquet, root-level market data"
    )
    if options.include_reports:
        print("  selected exception: reports/ will be archived and reset")
    if options.include_caches:
        print("  selected exception: .cache/ will be archived and reset; expect market-data bootstrap")
    print(
        f"  quiesced units: {len(STOP_UNITS)} (all shared-account writers plus "
        "maintenance readers/timers)"
    )


def print_dry_run_hint(options: ResetOptions) -> None:
    print()
    print("DRY RUN: no services or files were changed.")
    print("Execute only after reviewing the plan:")
    hint = f"scripts/maintain/reset_demo_ledgers.sh --execute --sleeves {options.sleeves_raw}"
    if options.label:
        hint += f" --label {options.label}"
    if options.include_reports:
        hint += " --include-reports"
    if options.include_caches:
        hint += " --include-caches"
    if options.leave_stopped:
        hint += " --leave-stopped"
    print(f"  {hint}")
    print(
        "Execute will refuse REAL_MONEY, missing demo credentials, any open demo "
        "position/order, missing units, or restart verification failure."
    )


def run_reset(argv: Sequence[str], *, repository: Path | None = None) -> int:
    options = parse_options(argv)
    if repository is None:
        repository = Path(__file__).resolve().parents[2]
    working = Path.cwd().resolve()
    if not (working / "liquidity_migration").is_dir() or not (working / "data").is_dir():
        _die("run from the repo root (expected liquidity_migration/ and data/)")
    if working != repository:
        _die(f"run from the repository containing this reset script: {repository}")

    systemctl_binary = os.environ.get("SYSTEMCTL_BIN", "/usr/bin/systemctl")
    checkout = CleanCheckout(repository)
    execution = Execution(systemctl=None, options=options)

    if options.mode == "execute":
        execution.acquire_maintenance_locks()
        checkout.bind()

    if "REAL_MONEY" in os.environ:
        validate_real_money_value("the caller environment", os.environ["REAL_MONEY"])

    account_env_file = Path(options.account_env_file)
    if not account_env_file.is_file() or not os.access(account_env_file, os.R_OK):
        _die(f"demo account env is missing or unreadable: {account_env_file}")
    if systemd_env_value(account_env_file, "ACCOUNT_EXECUTION_KERNEL_REQUIRED") != "1":
        _die("demo account env must set ACCOUNT_EXECUTION_KERNEL_REQUIRED=1")
    raw_roots = {
        key: systemd_env_value(account_env_file, key)
        for key in ("ACCOUNT_EXECUTION_ROOT", "ACCOUNT_INTENT_INBOX_ROOT", "ACCOUNT_CAPTURE_ROOT")
    }
    if not all(raw_roots.values()):
        _die(
            "demo account env must set ACCOUNT_EXECUTION_ROOT, "
            "ACCOUNT_INTENT_INBOX_ROOT, and ACCOUNT_CAPTURE_ROOT"
        )
    account_targets = tuple(
        repo_data_path(raw, repository) for raw in raw_roots.values()
    )
    # Refuse route layouts that overlap each other or any strategy root: a
    # narrow account reset could otherwise recursively erase a preserved
    # journal, cache, config, or unselected sleeve.
    check_account_root_disjointness(repository, account_targets)

    plan = build_plan(options, account_targets)
    refresh_existing_targets(plan, repository)
    check_archive_dir_containment(options.archive_dir, plan.targets, repository)
    print_plan(options, plan)

    if options.mode == "dry-run":
        print_dry_run_hint(options)
        return 0

    env_file = Path(options.env_file)
    if not env_file.is_file() or not os.access(env_file, os.R_OK):
        _die(f"demo env file is missing or unreadable: {env_file}")
    execution.systemctl = Systemctl(systemctl_binary)

    # Freeze the selected demo credential for the identity and flatness checks.
    # Do not export it into systemctl, tar, or projection-rebuild children.
    credential_demo = systemd_env_value(env_file, "DEMO")
    credential_real_money = systemd_env_value(env_file, "REAL_MONEY")
    credential_api_key = systemd_env_value(env_file, "BYBIT_DEMO_API_KEY")
    credential_api_secret = systemd_env_value(env_file, "BYBIT_DEMO_API_SECRET")
    validate_real_money_value(str(env_file), credential_real_money or "__unset__")
    if not credential_api_key or not credential_api_secret:
        _die(f"missing BYBIT_DEMO_API_KEY/BYBIT_DEMO_API_SECRET in {env_file}")

    # Resolve the venue account before any service mutation. The returned lease
    # path is keyed by Bybit userID, so it is account-wide across API keys and
    # cannot be steered by a caller-selected ledger root.
    with scrubbed_demo_credentials(
        credential_demo, credential_real_money, credential_api_key, credential_api_secret
    ):
        lease_path = resolve_demo_account_lease_path()
    if not lease_path.startswith("/") or "\n" in lease_path:
        _die("canonical demo-account lease path is not one absolute path")
    execution.lease_path = lease_path
    print(f"  authenticated demo-account lease: {lease_path}")

    active_before: list[str] = []
    for unit in STOP_UNITS:
        load_state = execution.systemctl.show_value(unit, "LoadState") or ""
        if not load_state or load_state == "not-found":
            _die(f"required systemd unit is not installed: {unit}")
        if execution.systemctl.is_active(unit):
            active_before.append(unit)
    execution.active_before = tuple(active_before)

    # systemctl show reflects unit drop-ins and daemon-reloaded state, unlike
    # grepping the checked-in unit files. Resolve both sides so an intentional
    # symlink alias passes but a different credential/account file cannot.
    for unit in ACCOUNT_BOUND_UNITS:
        verify_exclusive_unit_environment(
            execution.systemctl,
            unit,
            env_file,
            frozenset({"REAL_MONEY", "DEMO"}),
            protected_prefix="BYBIT_",
            failure=(
                f"submit-armed unit {unit} has an ambiguous credential environment or "
                f"does not exclusively load the selected demo env file (resolved from "
                f"{env_file}); refusing before stopping services"
            ),
        )

    for unit in NON_RESTARTABLE_ONESHOTS:
        if execution.was_active(unit):
            _die(
                f"transient oneshot is active: {unit}. Retry after it finishes; "
                "reset will not interrupt and re-run it."
            )

    def _raise_signal(exit_code: int):
        def handler(signal_number, frame):  # noqa: ANN001 - signal contract
            raise _Signalled(exit_code)

        return handler

    signal.signal(signal.SIGINT, _raise_signal(130))
    signal.signal(signal.SIGTERM, _raise_signal(143))
    # An SSH client death delivers HUP, then PIPE on the next write; both must
    # route through the fail-closed handoff instead of killing the process.
    signal.signal(signal.SIGHUP, _raise_signal(129))
    signal.signal(signal.SIGPIPE, _raise_signal(141))

    manifest_directory: str | None = None
    try:
        print()
        print("Stopping shared-account writers and maintenance units ...")
        execution.services_stopped = True
        for unit in STOP_UNITS:
            if not execution.systemctl.stop(unit):
                _die(f"failed to stop unit: {unit}")
            print(f"  stopped {unit}")
        for unit in STOP_UNITS:
            if execution.systemctl.is_active(unit):
                _die(f"unit remained active after stop: {unit}")
        # A stopped service may retain ActiveState=failed from an earlier
        # timeout, and downstream checks require literal `inactive`. Reset only
        # units that really are failed: systemd may garbage-collect an inactive
        # unit object between stop and reset-failed, so an unconditional
        # reset-failed races a healthy unit.
        for unit in STOP_UNITS:
            stopped_load_state = execution.systemctl.show_value(unit, "LoadState")
            if stopped_load_state is None:
                _die(f"could not read stopped unit load state: {unit}")
            stopped_active_state = execution.systemctl.show_value(unit, "ActiveState")
            if stopped_active_state is None:
                _die(f"could not read stopped unit state: {unit}")
            if stopped_load_state != "loaded":
                _die(f"managed unit is not loaded after stop: {unit} ({stopped_load_state})")
            if stopped_active_state == "failed":
                if not execution.systemctl.reset_failed(unit):
                    _die(f"could not clear stopped failure state: {unit}")
                stopped_load_state = execution.systemctl.show_value(unit, "LoadState")
                if stopped_load_state is None:
                    _die(f"could not re-read stopped unit load state: {unit}")
                stopped_active_state = execution.systemctl.show_value(unit, "ActiveState")
                if stopped_active_state is None:
                    _die(f"could not re-read stopped unit state: {unit}")
            if stopped_load_state != "loaded":
                _die(
                    "managed unit is not loaded after failure-state normalization: "
                    f"{unit} ({stopped_load_state})"
                )
            if stopped_active_state != "inactive":
                _die(
                    "unit is not literally inactive after stop/reset-failed: "
                    f"{unit} ({stopped_active_state})"
                )
        print("  quiescence verified (all managed units loaded and inactive)")

        # Bind every filesystem tree before the account lease can create,
        # chown, truncate, or write a lock leaf.
        data_anchor = repository / "data"
        try:
            preflight_reset_targets(
                data_anchor,
                [repository / target for target in plan.account_state_targets],
                reject_symlinks=True,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            _die(f"account root descriptor/mount preflight failed before owner lease ({exc})")
        try:
            preflight_demo_runtime_roots(
                data_anchor,
                [repository / root for root in _STRATEGY_ROOTS],
            )
        except (OSError, RuntimeError, ValueError) as exc:
            _die(f"demo runtime descriptor/mount preflight failed before owner lease ({exc})")
        try:
            preflight_reset_targets(
                data_anchor,
                [
                    repository / target
                    for target in (*plan.selected_roots, *plan.existing_targets)
                ],
            )
        except (OSError, RuntimeError, ValueError) as exc:
            _die(f"reset target descriptor/mount preflight failed before owner lease ({exc})")

        execution.acquire_demo_account_lease()

        print()
        print("Checking demo/mainnet boundary and flat-account precondition ...")
        with scrubbed_demo_credentials(
            credential_demo, credential_real_money, credential_api_key, credential_api_secret
        ):
            validate_real_money_value(str(env_file), os.environ.get("REAL_MONEY") or "__unset__")
            verify_demo_account_flat()

        # The preview inventory was collected while producers were still
        # running. Refresh after quiescence so an inbox, capture, or projection
        # created during that interval cannot survive into the fresh epoch
        # merely because its path did not exist during planning.
        refresh_existing_targets(plan, repository)

        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = f"-{options.label}" if options.label else ""
        archive_stem = f"ledger-reset-{stamp}{suffix}"
        os.umask(0o077)

        manifest_directory = tempfile.mkdtemp(prefix="ledger-reset-manifest.")
        manifest_path = Path(manifest_directory) / "ledger-reset-manifest.txt"

        checkout.verify()
        manifest_lines = [
            f"ledger_reset_utc={stamp}",
            f"git_head={checkout.bound_commit}",
            f"sleeves={' '.join(plan.selected_sleeves)}",
            f"include_reports={int(options.include_reports)}",
            f"include_caches={int(options.include_caches)}",
            f"leave_stopped={int(options.leave_stopped)}",
            f"env_file={env_file}",
            f"account_env_file={account_env_file}",
            f"demo_account_lease_path={execution.lease_path}",
            "demo_boundary=venue_verified_flat_positions_0_open_orders_0",
            f"active_before={' '.join(execution.active_before)}",
        ]
        manifest_lines.extend(
            f"account_epoch_target={target}" for target in plan.account_state_targets
        )
        manifest_lines.extend(f"target={target}" for target in plan.existing_targets)
        manifest_path.write_text("".join(f"{line}\n" for line in manifest_lines), encoding="utf-8")

        print()
        print(f"Archiving {len(plan.existing_targets)} reset target(s) ...")
        try:
            archive = create_reset_archive(
                repository=repository,
                archive_directory=options.archive_dir,
                stem=archive_stem,
                manifest=manifest_path,
                targets=plan.existing_targets,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            _die(f"descriptor-bound reset archive publication failed ({exc})")
        print(f"  archive verified: {archive.path} ({archive.size_bytes} bytes)")
        print(f"  sha256: {archive.sha256}")
        print(f"  digest sidecar: {archive.sidecar_path}")

        checkout.verify()
        try:
            verify_reset_archive(
                repository=repository,
                archive=archive.path,
                sidecar=archive.sidecar_path,
                expected_sha256=archive.sha256,
                expected_device=archive.device,
                expected_inode=archive.inode,
                expected_size=archive.size_bytes,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            _die(f"reset archive changed before live-state removal ({exc})")

        # From the first live-state removal onward a failure leaves every
        # managed unit stopped: restarting owners into a partially cleared
        # epoch is worse than an outage.
        execution.failure_recovery_allowed = False

        print()
        print("Removing only archived generated projections and epoch telemetry ...")
        revalidate_inherited_account_owner_lease(
            execution.lease_descriptor, execution.lease_path
        )
        for result in clear_account_epoch_roots_preserving_locks(plan.account_state_targets):
            print(
                f"  cleared {result.root} removed_entries={result.removed_entries} "
                f"preserved_lock_files={len(result.preserved_lock_files)}"
            )
        generic_targets = []
        for target in plan.existing_targets:
            if target in plan.account_state_targets:
                print(f"  preserving persistent locks while clearing {target} in place")
                continue
            generic_targets.append(repository / target)
        if generic_targets:
            try:
                remove_reset_targets(data_anchor, generic_targets)
            except (OSError, RuntimeError, ValueError) as exc:
                _die(f"descriptor-rooted generated-target removal failed ({exc})")

        # Reset runs as root while every writer is stopped. Restore the private
        # account trees and the demo-cache boundary through held descriptors.
        try:
            normalize_private_runtime_roots(
                data_anchor,
                [repository / root for root in plan.account_state_targets],
                uid=0,
                gid=0,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            _die(f"descriptor-rooted demo account permission normalization failed ({exc})")
        try:
            normalize_demo_runtime_roots(
                data_anchor,
                [repository / root for root in _STRATEGY_ROOTS],
                uid=0,
                gid=0,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            _die(f"descriptor-rooted shared demo cache normalization failed ({exc})")

        if options.leave_stopped:
            print()
            print("Leaving every managed unit stopped for explicit owner-first staging ...")
            for unit in STOP_UNITS:
                if execution.systemctl.is_active(unit):
                    _die(f"leave-stopped verification failed: {unit} is active")
                print(f"  inactive: {unit}")
            execution.release_demo_account_lease("post-receipt stopped handoff")
        else:
            execution.release_demo_account_lease("normal completion")
            if not execution.restart_previously_active("normal completion"):
                _die("restart handoff failed")
            if options.settle_seconds > 0:
                print(
                    f"Waiting {options.settle_seconds}s before final service verification ..."
                )
                time.sleep(options.settle_seconds)
            for unit in RESTART_UNITS:
                if execution.was_active(unit):
                    if not execution.systemctl.is_active(unit):
                        _die(f"restart verification failed: {unit} is not active")
                    print(f"  active: {unit}")

        execution.restart_complete = True
        execution.services_stopped = False

        print()
        print("Ledger reset complete.")
        print(f"  archive: {archive.path}")
        print(f"  archive sha256: {archive.sha256}")
        print(f"  archive digest: {archive.sidecar_path}")
        print(f"  archived/reset targets: {len(plan.existing_targets)}")
        print("  account journal/inbox/capture: archived; fresh empty epoch created")
        print("  boundary truth: demo venue-flat verified")
        if options.leave_stopped:
            print("  service state: all managed units stopped and verified")
        else:
            print("  service state: restored to the pre-reset active set and verified")
        print(
            "  preserved: configs, locks, signal files, account-equity high-water "
            "state, and unselected caches/reports"
        )
        return 0
    except _Signalled as caught:
        execution.fail_closed_cleanup()
        return caught.exit_code
    except ResetError:
        execution.fail_closed_cleanup()
        raise
    except BaseException:
        execution.fail_closed_cleanup()
        raise
    finally:
        if manifest_directory is not None:
            with contextlib.suppress(OSError):
                shutil.rmtree(manifest_directory)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run_reset(list(sys.argv[1:] if argv is None else argv))
    except ResetError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
