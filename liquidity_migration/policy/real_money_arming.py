"""Report every remaining arming precondition in one read-only pass.

``preflight`` reports each step, its status, and its fix. Two commands mutate:
``render-profile`` writes the operational profile derived from the env-file
dials, refusing dials that fail the same envelope proof the account owner
applies at load time, and ``create-state-roots`` creates the mainnet journal
directories. Credentials are reported present/absent by variable name; their
values are never read into the report.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from liquidity_migration.core.env_flags import FALSE_ENV_VALUES, TRUE_ENV_VALUES
from liquidity_migration.policy.real_money_profile import (
    RealMoneyDials,
    dial_environment_keys,
    parse_real_money_dials,
    render_real_money_profile,
)
from liquidity_migration.policy.systemd_environment import parse_systemd_environment_bytes

__all__ = ["CheckResult", "preflight", "main"]

MAINNET_CREDENTIAL_ENV = Path("/etc/liquidity-migration/bybit-mainnet.env")
MAINNET_OWNER_ENV = Path("/etc/liquidity-migration/account-execution-mainnet.env")
DEMO_OWNER_ENV = Path("/etc/liquidity-migration/account-execution.env")

_CREDENTIAL_KEYS = ("BYBIT_REAL_API_KEY", "BYBIT_REAL_API_SECRET")

_STATE_ROOT_KEYS = (
    "ACCOUNT_EXECUTION_ROOT",
    "ACCOUNT_INTENT_INBOX_ROOT",
    "ACCOUNT_CAPTURE_ROOT",
)

_OWNER_ENV_KEYS = (
    "ACCOUNT_EXECUTION_KERNEL_REQUIRED",
    "ACCOUNT_VENUE_REALM",
    "ACCOUNT_RAW_MARKET_PERSISTENCE",
    "ACCOUNT_EXECUTION_ROOT",
    "ACCOUNT_INTENT_INBOX_ROOT",
    "ACCOUNT_CAPTURE_ROOT",
    "STRATEGY_TARGET_CAPTURE_PATH",
    "CANDIDATE_UNIVERSE_FILE",
    "ACCOUNT_SYMBOLS_FILE",
    "ACCOUNT_DEMO_RULES_FILE",
    "ACCOUNT_RISK_POLICY_FILE",
    "DISASTER_STOP_FRACTION",
)


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    fix: str = ""

    def render(self) -> str:
        mark = "PASS" if self.ok else "TODO"
        line = f"[{mark}] {self.name}: {self.detail}"
        if not self.ok and self.fix:
            line += f"\n       -> {self.fix}"
        return line


def _read_environment(path: Path) -> tuple[Mapping[str, str] | None, CheckResult]:
    try:
        metadata = path.lstat()
    except OSError:
        return None, CheckResult(
            path.name,
            False,
            f"{path} does not exist",
            f"install the template: deploy/{path.name}.template",
        )
    if not stat.S_ISREG(metadata.st_mode):
        return None, CheckResult(path.name, False, f"{path} is not a regular file", "")
    mode = stat.S_IMODE(metadata.st_mode)
    # Caller-owned is accepted so a non-root dry run reports what the VPS will;
    # root ownership on the deployed host is enforced by the owner runner.
    if metadata.st_uid not in {0, os.geteuid()} or mode != 0o600:
        return None, CheckResult(
            path.name,
            False,
            f"{path} is uid={metadata.st_uid} mode={mode:04o}, must be root-owned 0600",
            f"chown root:root {path} && chmod 0600 {path}",
        )
    try:
        values = parse_systemd_environment_bytes(path.read_bytes(), label=str(path))
    except (OSError, ValueError) as exc:
        return None, CheckResult(
            path.name,
            False,
            f"{path} is unreadable: {exc}",
            "strict KEY=value lines only -- no quotes, no inline comments",
        )
    return values, CheckResult(path.name, True, f"{path} is root-owned 0600 and parses")


def _credential_checks(values: Mapping[str, str]) -> list[CheckResult]:
    """Report credential presence by variable name; values are never read."""

    results: list[CheckResult] = []
    for key in _CREDENTIAL_KEYS:
        present = bool(values.get(key, "").strip())
        results.append(
            CheckResult(
                key,
                present,
                "set" if present else "empty",
                (
                    ""
                    if present
                    else f"paste the mainnet key into {key}= (contract trading only, "
                    "withdrawal DISABLED, IP-allowlisted to this host)"
                ),
            )
        )
    for key in ("BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET"):
        absent = not values.get(key, "").strip()
        results.append(
            CheckResult(
                key,
                absent,
                "absent, as required" if absent else "present in the mainnet file",
                "" if absent else f"remove {key} -- demo keys must never reach a funded run",
            )
        )
    raw = values.get("REAL_MONEY", "").strip().lower()
    armed = raw in TRUE_ENV_VALUES
    if raw and raw not in TRUE_ENV_VALUES and raw not in FALSE_ENV_VALUES:
        detail = "REAL_MONEY is set to an unrecognised value; refusing to guess"
        fix = f"use one of {sorted(TRUE_ENV_VALUES)} to arm"
    elif armed:
        detail = "armed by the owner"
        fix = ""
    else:
        detail = "not armed -- this is the switch that means 'trade my money'"
        fix = "set REAL_MONEY=true, by hand, when you intend to trade real capital"
    results.append(CheckResult("REAL_MONEY", armed, detail, fix))
    # Both the mainnet owner and the mainnet liveness watchdog set
    # TELEGRAM_ENABLED=1 and read this pair, so an empty one silences both.
    telegram = [
        key for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not values.get(key, "").strip()
    ]
    results.append(
        CheckResult(
            "notifications",
            not telegram,
            "Telegram is configured"
            if not telegram
            else f"the owner unit enables Telegram but {', '.join(telegram)} is empty",
            ""
            if not telegram
            else (
                "fill both in, or the owner will refuse to start; there is no "
                "mainnet watchdog unit, so this is the only alive signal"
            ),
        )
    )
    return results


def _dial_checks(values: Mapping[str, str]) -> list[CheckResult]:
    declared = sorted(key for key in dial_environment_keys() if key in values)
    try:
        dials = parse_real_money_dials(values)
    except ValueError as exc:
        return [
            CheckResult(
                "dials",
                False,
                str(exc),
                "fix the dial in the env file; see deploy/bybit-mainnet.env.template",
            )
        ]
    try:
        _data, profile = render_real_money_profile(dials)
    except ValueError as exc:
        return [
            CheckResult(
                "dials",
                False,
                f"these dials do not produce a provable envelope: {exc}",
                "adjust the dial the message names, then re-run preflight",
            )
        ]
    shares = ", ".join(
        f"{limit.sleeve} {limit.max_gross_notional_usdt / profile.capital_reference_usdt:.2f}x"
        for limit in profile.account_risk.sleeve_limits
    )
    return [
        CheckResult(
            "dials",
            True,
            (
                f"{len(declared)} set explicitly, rest defaulted; "
                f"leverage {profile.account_risk.max_leverage:g}, "
                f"gross {profile.account_risk.max_account_gross_notional_usdt / profile.capital_reference_usdt:g}x "
                f"equity, partition {shares}"
            ),
        )
    ]


def _installed_profile_matches_dials(
    *, dial_values: Mapping[str, str], installed_path: str
) -> CheckResult:
    """Check the installed profile is the render of the current dials.

    Otherwise the report shows a dial-derived envelope while a different one is
    enforced (dial edited, re-render forgotten).
    """

    path = Path(installed_path)
    try:
        installed = path.read_bytes()
    except OSError:
        return CheckResult(
            "profile matches dials",
            False,
            f"{path} cannot be read, so the dials cannot be compared to it",
            "render it: scripts/ops.sh real-money render-profile --execute --output <path>",
        )
    try:
        rendered, _profile = render_real_money_profile(parse_real_money_dials(dial_values))
    except ValueError as exc:
        return CheckResult("profile matches dials", False, str(exc), "fix the dial, then re-render")
    if rendered == installed:
        return CheckResult(
            "profile matches dials", True, "the installed profile is the render of these dials"
        )
    return CheckResult(
        "profile matches dials",
        False,
        f"{path} is NOT the render of the dials in the env file; the envelope "
        "reported above is not the one that would be enforced",
        "re-render it: scripts/ops.sh real-money render-profile --execute "
        f"--output {path} --overwrite, then reinstall",
    )


def _path_checks(values: Mapping[str, str]) -> list[CheckResult]:
    results: list[CheckResult] = []
    missing = [key for key in _OWNER_ENV_KEYS if not values.get(key, "").strip()]
    results.append(
        CheckResult(
            "owner route",
            not missing,
            "every route and input path is declared"
            if not missing
            else f"missing: {', '.join(missing)}",
            "" if not missing else "copy deploy/account-execution-mainnet.env.template",
        )
    )
    if values.get("ACCOUNT_VENUE_REALM", "") != "mainnet":
        results.append(
            CheckResult(
                "ACCOUNT_VENUE_REALM",
                False,
                f"is {values.get('ACCOUNT_VENUE_REALM', '')!r}, must be 'mainnet'",
                "set ACCOUNT_VENUE_REALM=mainnet",
            )
        )
    candidate = values.get("CANDIDATE_UNIVERSE_FILE", "")
    symbols = values.get("ACCOUNT_SYMBOLS_FILE", "")
    if candidate and symbols and candidate != symbols:
        results.append(
            CheckResult(
                "candidate universe",
                False,
                "CANDIDATE_UNIVERSE_FILE and ACCOUNT_SYMBOLS_FILE differ",
                "they must name the same frozen artifact",
            )
        )
    # Issuance requires these directories to exist and be owned by the issuing user.
    missing_roots = [
        key for key in _STATE_ROOT_KEYS if values.get(key, "").strip() and not Path(values[key]).is_dir()
    ]
    results.append(
        CheckResult(
            "state roots",
            not missing_roots,
            "every mainnet root exists"
            if not missing_roots
            else f"missing directories: {', '.join(missing_roots)}",
            ""
            if not missing_roots
            else "create them: scripts/ops.sh real-money create-state-roots --execute",
        )
    )
    artifacts = {
        "candidate universe": (
            values.get("ACCOUNT_SYMBOLS_FILE", ""),
            "scripts/maintain/freeze_account_candidate_universe.py --realm mainnet",
        ),
        "instrument rules": (
            values.get("ACCOUNT_DEMO_RULES_FILE", ""),
            "scripts/maintain/freeze_venue_instrument_rules.py --realm mainnet",
        ),
        "operational profile": (
            values.get("ACCOUNT_RISK_POLICY_FILE", ""),
            "scripts/ops.sh real-money render-profile --execute --output <path>",
        ),
    }
    for name, (raw, fix) in artifacts.items():
        if not raw:
            continue
        path = Path(raw)
        exists = path.is_file()
        results.append(
            CheckResult(
                name,
                exists,
                f"{path} is present" if exists else f"{path} is missing",
                "" if exists else f"freeze it: {fix}",
            )
        )
    return results


def preflight(
    *,
    credential_env: Path = MAINNET_CREDENTIAL_ENV,
    owner_env: Path = MAINNET_OWNER_ENV,
) -> list[CheckResult]:
    """Every arming precondition, reported at once. Reads only; writes nothing."""

    results: list[CheckResult] = []
    credentials, credential_result = _read_environment(credential_env)
    results.append(credential_result)
    if credentials is not None:
        results.extend(_credential_checks(credentials))
        results.extend(_dial_checks(credentials))
    owner, owner_result = _read_environment(owner_env)
    results.append(owner_result)
    if owner is not None:
        results.extend(_path_checks(owner))
        installed_profile = owner.get("ACCOUNT_RISK_POLICY_FILE", "").strip()
        if credentials is not None and installed_profile:
            results.append(
                _installed_profile_matches_dials(
                    dial_values=credentials, installed_path=installed_profile
                )
            )
    return results


def _declared_roots(path: Path) -> list[Path]:
    """Absolute ``*_ROOT`` values from another realm's env file. Not installed: none."""

    if not path.exists():
        return []
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(
            f"{path} is installed but unreadable, so its roots cannot be excluded: {exc}"
        ) from exc
    values = parse_systemd_environment_bytes(data, label=str(path))
    return [
        Path(value)
        for key, value in values.items()
        if key.endswith("_ROOT") and value.startswith("/")
    ]


def _state_root_targets(values: Mapping[str, str]) -> list[Path]:
    targets: list[Path] = []
    for key in _STATE_ROOT_KEYS:
        raw = values.get(key, "").strip()
        if not raw:
            raise ValueError(f"{key} is not declared in the owner env file")
        if not Path(raw).is_absolute():
            raise ValueError(f"{key}={raw} is not an absolute path")
        targets.append(Path(raw))
    # A file path: its parent is ours to create, the file itself never is.
    capture = values.get("STRATEGY_TARGET_CAPTURE_PATH", "").strip()
    if capture:
        if not Path(capture).is_absolute():
            raise ValueError(f"STRATEGY_TARGET_CAPTURE_PATH={capture} is not an absolute path")
        targets.append(Path(capture).parent)
    ordered: list[Path] = []
    for target in targets:
        if target not in ordered:
            ordered.append(target)
    return ordered


def _create_state_roots(args: argparse.Namespace) -> int:
    owner_env = Path(args.owner_env)
    values, result = _read_environment(owner_env)
    if values is None:
        print(result.render(), file=sys.stderr)
        return 2
    targets = _state_root_targets(values)
    # Resolved on both sides: a lexical compare misses a symlinked or ``..`` path
    # that lands in another realm's tree.
    resolved = {target: target.resolve() for target in targets}
    for other in (DEMO_OWNER_ENV,):
        for root in _declared_roots(other):
            here = root.resolve()
            for target, full in resolved.items():
                if full == here or here in full.parents:
                    raise ValueError(f"{target} is at or inside {root}, declared in {other.name}")

    existing: list[Path] = []
    missing: list[Path] = []
    for target in targets:
        try:
            metadata = target.stat()  # follows symlinks, as the preflight check does
        except FileNotFoundError:
            if target.is_symlink():
                raise ValueError(f"{target} is a symlink to nothing; repoint it by hand") from None
            missing.append(target)
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{target} exists and is not a directory; move it aside by hand")
        existing.append(target)

    if not args.execute:
        for target in existing:
            print(f"[have] {target}")
        for target in missing:
            print(f"[make] {target} mode 0700")
        print(
            f"\n# dry run -- {len(missing)} directory(ies) to create; pass --execute",
            file=sys.stderr,
        )
        return 0

    for target in missing:
        # exist_ok: one missing root can be the parent of another, and creating the
        # child materialises it first. A non-directory still raises.
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.chmod(0o700)  # mkdir's mode is masked by the umask; chmod is not
    summary: dict[str, Any] = {
        "owner_env": str(owner_env),
        "created": [str(target) for target in missing],
        "already_present": [str(target) for target in existing],
        "roots": {
            str(target): {
                "mode": f"{stat.S_IMODE(target.stat().st_mode):04o}",
                "uid": target.stat().st_uid,
            }
            for target in targets
        },
    }
    print(json.dumps(summary, sort_keys=True, indent=2))
    return 0


def _default_telegram(args: argparse.Namespace) -> int:
    """Copy a missing Telegram pair into the credential file from another env.

    A funded book that cannot page is a hazard, so activation defaults the
    pair from the demo file when the owner left it out. Existing values are
    never touched, values are never printed, and the append preserves the
    file's strict format (the parser accepts comments and blank lines).
    """
    credential = Path(args.credential_env)
    source = Path(args.from_env)
    values, check = _read_environment(credential)
    if values is None:
        print(check.render(), file=sys.stderr)
        return 2
    missing = [
        key
        for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
        if not str(values.get(key) or "").strip()
    ]
    if not missing:
        print(f"{credential.name}: telegram pair already present; nothing to do")
        return 0
    source_values, source_check = _read_environment(source)
    if source_values is None:
        print(
            f"{credential.name} has no telegram pair and the fallback is unusable:",
            file=sys.stderr,
        )
        print(source_check.render(), file=sys.stderr)
        return 2
    additions: dict[str, str] = {}
    for key in missing:
        value = str(source_values.get(key) or "").strip()
        if not value:
            print(
                f"{source} does not hold {key}; set the pair by hand in {credential}",
                file=sys.stderr,
            )
            return 2
        additions[key] = value
    alert_chat = str(source_values.get("TELEGRAM_ALERT_CHAT_ID") or "").strip()
    if alert_chat and not str(values.get("TELEGRAM_ALERT_CHAT_ID") or "").strip():
        additions["TELEGRAM_ALERT_CHAT_ID"] = alert_chat
    names = ", ".join(sorted(additions))
    if not args.execute:
        print(f"dry-run: would set {names} in {credential} from {source.name}")
        return 0
    # The template ships the keys as empty assignments, so a plain append would
    # duplicate them and the strict parser refuses duplicates: replace existing
    # assignments in place and append only keys the file does not carry at all.
    lines = credential.read_bytes().decode("utf-8").splitlines()
    remaining = dict(additions)
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        key = stripped.partition("=")[0]
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
    body = "\n".join(lines)
    if remaining:
        body += "\n# Telegram pair defaulted from the demo env at activation.\n"
        body += "\n".join(f"{key}={remaining[key]}" for key in sorted(remaining))
    if not body.endswith("\n"):
        body += "\n"
    parse_systemd_environment_bytes(body.encode("utf-8"), label=str(credential))
    scratch = credential.with_name(credential.name + ".tmp")
    descriptor = os.open(
        scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch, credential)
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise
    print(f"set {names} in {credential} (values from {source.name}, never printed)")
    return 0


def _render(args: argparse.Namespace) -> int:
    dials = RealMoneyDials()
    source = "committed defaults"
    if args.from_env:
        path = Path(args.from_env)
        values = parse_systemd_environment_bytes(path.read_bytes(), label=str(path))
        dials = parse_real_money_dials(values)
        source = str(path)
    data, profile = render_real_money_profile(dials)
    if not args.execute:
        sys.stdout.write(data.decode("utf-8"))
        print(
            f"\n# dry run -- dials from {source}; pass --execute --output PATH to write",
            file=sys.stderr,
        )
        return 0
    output = Path(args.output).expanduser()
    if output.exists() and not args.overwrite:
        print(f"refusing to overwrite {output}; pass --overwrite", file=sys.stderr)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        str(output),
        os.O_CREAT | os.O_WRONLY | os.O_TRUNC | (0 if args.overwrite else os.O_EXCL),
        0o600,
    )
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    summary: dict[str, Any] = {
        "output": str(output),
        "dials": source,
        "capital_reference_usdt": profile.capital_reference_usdt,
        "tracks_equity": profile.capital_reference.tracks_equity,
        "sleeve_shares": {
            limit.sleeve: limit.max_gross_notional_usdt
            for limit in profile.account_risk.sleeve_limits
        },
    }
    print(json.dumps(summary, sort_keys=True, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser(
        "preflight", help="Report every arming precondition. Read-only."
    )
    check.add_argument("--credential-env", default=str(MAINNET_CREDENTIAL_ENV))
    check.add_argument("--owner-env", default=str(MAINNET_OWNER_ENV))
    check.add_argument("--json", action="store_true")

    render = subparsers.add_parser(
        "render-profile",
        help="Render the operational profile from the env dials, and prove it.",
    )
    render.add_argument(
        "--from-env",
        default=str(MAINNET_CREDENTIAL_ENV),
        help="Env file holding the RM_* dials. Its credentials are never read.",
    )
    render.add_argument("--execute", action="store_true")
    render.add_argument("--output", default="")
    render.add_argument("--overwrite", action="store_true")

    roots = subparsers.add_parser(
        "create-state-roots",
        help="Create the mainnet journal directories the owner env file declares.",
    )
    roots.add_argument("--owner-env", default=str(MAINNET_OWNER_ENV))
    roots.add_argument("--execute", action="store_true")

    telegram = subparsers.add_parser(
        "default-telegram",
        help="Copy a missing Telegram pair into the credential file from another env file.",
    )
    telegram.add_argument("--credential-env", default=str(MAINNET_CREDENTIAL_ENV))
    telegram.add_argument("--from-env", required=True)
    telegram.add_argument("--execute", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "default-telegram":
        try:
            return _default_telegram(args)
        except (OSError, ValueError) as exc:
            print(f"default-telegram failed: {exc}", file=sys.stderr)
            return 2
    if args.command == "create-state-roots":
        try:
            return _create_state_roots(args)
        except (OSError, ValueError) as exc:
            print(f"create-state-roots failed: {exc}", file=sys.stderr)
            return 2
    if args.command == "render-profile":
        if args.execute and not args.output:
            parser.error("--execute requires --output")
        try:
            return _render(args)
        except (OSError, ValueError) as exc:
            print(f"render failed: {exc}", file=sys.stderr)
            return 2

    results = preflight(
        credential_env=Path(args.credential_env),
        owner_env=Path(args.owner_env),
    )
    if args.json:
        print(
            json.dumps(
                [
                    {"name": row.name, "ok": row.ok, "detail": row.detail, "fix": row.fix}
                    for row in results
                ],
                indent=2,
            )
        )
    else:
        for row in results:
            print(row.render())
        outstanding = [row for row in results if not row.ok]
        print()
        if outstanding:
            print(f"{len(outstanding)} step(s) remaining before real money can trade.")
        else:
            print(
                "Every precondition is met. Remaining owner act: activate the "
                "mainnet units."
            )
    return 0 if all(row.ok for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
