"""Report funded-engine arming inputs without exposing credential values."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import stat
import sys
from dataclasses import asdict, dataclass
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
MAINNET_PRODUCER_SOURCE_ENV = Path(
    "/etc/liquidity-migration/producer-mainnet-source.env"
)

_CREDENTIAL_KEYS = ("BYBIT_REAL_API_KEY", "BYBIT_REAL_API_SECRET")
_PRODUCER_PATH_KEYS = (
    "CANDIDATE_UNIVERSE_FILE",
    "OPERATIONAL_PROFILE_FILE",
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
        return None, CheckResult(path.name, False, f"{path} is not a regular file")
    mode = stat.S_IMODE(metadata.st_mode)
    # Windows neither exposes an effective UID nor reports POSIX chmod bits
    # faithfully. Production runs on Linux, where this remains an exact
    # root/current-owner + 0600 gate; Windows development can still parse and
    # test the policy without pretending its synthetic 0666 is authoritative.
    effective_uid = os.geteuid() if hasattr(os, "geteuid") else metadata.st_uid
    if os.name != "nt" and (metadata.st_uid not in {0, effective_uid} or mode != 0o600):
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
            "use strict KEY=value lines only",
        )
    detail = (
        f"{path} is root-owned 0600 and parses"
        if os.name != "nt"
        else f"{path} parses (POSIX ownership is enforced on the Linux runtime host)"
    )
    return values, CheckResult(path.name, True, detail)


def _credential_checks(values: Mapping[str, str]) -> list[CheckResult]:
    results: list[CheckResult] = []
    for key in _CREDENTIAL_KEYS:
        present = bool(values.get(key, "").strip())
        results.append(
            CheckResult(
                key,
                present,
                "set" if present else "empty",
                "" if present else f"set {key} in the mainnet credential file",
            )
        )
    for key in ("BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET"):
        absent = not values.get(key, "").strip()
        results.append(
            CheckResult(
                key,
                absent,
                "absent, as required" if absent else "present in the mainnet file",
                "" if absent else f"remove {key}",
            )
        )
    def host_ip(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return None
        if address.is_unspecified or address.is_loopback or address.is_multicast:
            return None
        return address

    raw_ip = values.get("BYBIT_REAL_API_KEY_IP", "").strip()
    primary_ip = host_ip(raw_ip)
    results.append(
        CheckResult(
            "BYBIT_REAL_API_KEY_IP",
            primary_ip is not None,
            "one literal primary host IP is set"
            if primary_ip is not None
            else "missing, wildcard, or not a host IP",
            "set the primary public host IP in both this file and the Bybit key allowlist",
        )
    )
    raw_backup_ip = values.get("BYBIT_REAL_API_KEY_BACKUP_IP", "").strip()
    backup_ip = host_ip(raw_backup_ip) if raw_backup_ip else None
    backup_ok = not raw_backup_ip or (backup_ip is not None and backup_ip != primary_ip)
    results.append(
        CheckResult(
            "BYBIT_REAL_API_KEY_BACKUP_IP",
            backup_ok,
            "not configured"
            if not raw_backup_ip
            else "one distinct literal backup host IP is set"
            if backup_ok
            else "wildcard, not a host IP, or duplicates the primary",
            "set one distinct backup public host IP, or leave it empty",
        )
    )
    exclusive_uid = values.get("BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID", "").strip()
    exclusive_ok = exclusive_uid.isascii() and exclusive_uid.isdigit() and int(exclusive_uid or 0) > 0
    results.append(
        CheckResult(
            "BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID",
            exclusive_ok,
            "an exact dedicated UID is acknowledged" if exclusive_ok else "missing or not a venue UID",
            "dedicate the funded UID to this engine, close all venue bots/other writers, then set its exact numeric UID",
        )
    )
    raw = values.get("REAL_MONEY", "").strip().lower()
    armed = raw in TRUE_ENV_VALUES
    if raw and raw not in TRUE_ENV_VALUES and raw not in FALSE_ENV_VALUES:
        detail = "REAL_MONEY has an unrecognised value"
        fix = f"use one of {sorted(TRUE_ENV_VALUES)} to arm"
    elif armed:
        detail = "armed by the owner"
        fix = ""
    else:
        detail = "not armed"
        fix = "set REAL_MONEY=true by hand when funded trading is intended"
    results.append(CheckResult("REAL_MONEY", armed, detail, fix))
    missing_alert = [
        key
        for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
        if not values.get(key, "").strip()
    ]
    results.append(
        CheckResult(
            "notifications",
            not missing_alert,
            "Telegram is configured"
            if not missing_alert
            else f"missing: {', '.join(missing_alert)}",
            "" if not missing_alert else "set both values before activation",
        )
    )
    return results


def _dial_checks(values: Mapping[str, str]) -> list[CheckResult]:
    declared = sum(1 for key in dial_environment_keys() if key in values)
    try:
        dials = parse_real_money_dials(values)
        _data, profile = render_real_money_profile(dials)
    except ValueError as exc:
        return [CheckResult("dials", False, str(exc), "fix the named dial")]
    return [
        CheckResult(
            "dials",
            True,
            (
                f"{declared} set explicitly; leverage {profile.account_risk.max_leverage:g}, "
                f"gross {profile.account_risk.max_account_gross_notional_usdt / profile.capital_reference_usdt:g}x equity"
            ),
        )
    ]


def _installed_profile_matches_dials(
    *, dial_values: Mapping[str, str], installed_path: str
) -> CheckResult:
    path = Path(installed_path)
    try:
        installed = path.read_bytes()
        rendered, _profile = render_real_money_profile(parse_real_money_dials(dial_values))
    except (OSError, ValueError) as exc:
        return CheckResult(
            "profile matches dials",
            False,
            f"cannot compare {path}: {exc}",
            "render the profile from the mainnet credential env",
        )
    if rendered == installed:
        return CheckResult("profile matches dials", True, "installed bytes match the dials")
    return CheckResult(
        "profile matches dials",
        False,
        f"{path} is not the render of the current dials",
        "re-render and reinstall the profile",
    )


def _producer_checks(values: Mapping[str, str]) -> list[CheckResult]:
    results: list[CheckResult] = []
    realm = values.get("PRODUCER_REALM", "").strip()
    results.append(
        CheckResult(
            "PRODUCER_REALM",
            realm == "mainnet",
            f"is {realm!r}",
            "set PRODUCER_REALM=mainnet" if realm != "mainnet" else "",
        )
    )
    for key in _PRODUCER_PATH_KEYS:
        raw = values.get(key, "").strip()
        path = Path(raw) if raw else None
        valid = bool(path and path.is_absolute() and path.is_file() and not path.is_symlink())
        results.append(
            CheckResult(
                key,
                valid,
                f"{path} is present" if valid else f"{raw or '<empty>'} is not a regular absolute file",
                f"install the reviewed {key.lower()} artifact",
            )
        )
    return results


def preflight(
    *,
    credential_env: Path = MAINNET_CREDENTIAL_ENV,
    producer_env: Path = MAINNET_PRODUCER_SOURCE_ENV,
) -> list[CheckResult]:
    """Read every funded-engine arming input and report all failures."""

    results: list[CheckResult] = []
    credentials, credential_result = _read_environment(credential_env)
    results.append(credential_result)
    if credentials is not None:
        results.extend(_credential_checks(credentials))
        results.extend(_dial_checks(credentials))

    producer, producer_result = _read_environment(producer_env)
    results.append(producer_result)
    if producer is not None:
        results.extend(_producer_checks(producer))
        installed_profile = producer.get("OPERATIONAL_PROFILE_FILE", "").strip()
        if credentials is not None and installed_profile:
            results.append(
                _installed_profile_matches_dials(
                    dial_values=credentials, installed_path=installed_profile
                )
            )
    return results


def _default_telegram(args: argparse.Namespace) -> int:
    credential = Path(args.credential_env)
    source = Path(args.from_env)
    values, check = _read_environment(credential)
    if values is None:
        print(check.render(), file=sys.stderr)
        return 2
    missing = [
        key
        for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
        if not values.get(key, "").strip()
    ]
    if not missing:
        print(f"{credential.name}: Telegram pair already present")
        return 0
    source_values, source_check = _read_environment(source)
    if source_values is None:
        print(source_check.render(), file=sys.stderr)
        return 2
    additions = {key: source_values.get(key, "").strip() for key in missing}
    if any(not value for value in additions.values()):
        print(f"{source} does not hold the complete Telegram pair", file=sys.stderr)
        return 2
    alert_chat = source_values.get("TELEGRAM_ALERT_CHAT_ID", "").strip()
    if alert_chat and not values.get("TELEGRAM_ALERT_CHAT_ID", "").strip():
        additions["TELEGRAM_ALERT_CHAT_ID"] = alert_chat
    names = ", ".join(sorted(additions))
    if not args.execute:
        print(f"dry-run: would set {names} in {credential} from {source.name}")
        return 0

    lines = credential.read_bytes().decode("utf-8").splitlines()
    remaining = dict(additions)
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        key = stripped.partition("=")[0]
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
    if remaining:
        lines.extend(f"{key}={remaining[key]}" for key in sorted(remaining))
    body = "\n".join(lines) + "\n"
    parse_systemd_environment_bytes(body.encode(), label=str(credential))
    scratch = credential.with_name(credential.name + ".tmp")
    descriptor = os.open(
        scratch,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch, credential)
        if os.name != "nt":
            directory = os.open(
                credential.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise
    print(f"set {names} in {credential} (values not printed)")
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
        sys.stdout.write(data.decode())
        print(f"\n# dry run -- dials from {source}", file=sys.stderr)
        return 0
    output = Path(args.output).expanduser()
    if output.exists() and not args.overwrite:
        print(f"refusing to overwrite {output}; pass --overwrite", file=sys.stderr)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
    if not args.overwrite:
        flags |= os.O_EXCL
    descriptor = os.open(output, flags, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    summary: dict[str, Any] = {
        "output": str(output),
        "dials": source,
        "gross_multiple_of_equity": (
            profile.account_risk.max_account_gross_notional_usdt
            / profile.capital_reference_usdt
        ),
    }
    print(json.dumps(summary, sort_keys=True, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("preflight", help="Report every arming input")
    check.add_argument("--credential-env", default=str(MAINNET_CREDENTIAL_ENV))
    check.add_argument("--producer-env", default=str(MAINNET_PRODUCER_SOURCE_ENV))
    check.add_argument("--json", action="store_true")

    render = subparsers.add_parser("render-profile", help="Render the operational profile")
    render.add_argument("--from-env", default=str(MAINNET_CREDENTIAL_ENV))
    render.add_argument("--execute", action="store_true")
    render.add_argument("--output", default="")
    render.add_argument("--overwrite", action="store_true")

    telegram = subparsers.add_parser(
        "default-telegram", help="Copy a missing Telegram pair from another env"
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
        producer_env=Path(args.producer_env),
    )
    if args.json:
        print(json.dumps([asdict(row) for row in results], indent=2))
    else:
        for row in results:
            print(row.render())
        print()
        outstanding = sum(not row.ok for row in results)
        print(
            f"{outstanding} step(s) remaining before real money can trade."
            if outstanding
            else "Every precondition is met."
        )
    return 0 if all(row.ok for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
