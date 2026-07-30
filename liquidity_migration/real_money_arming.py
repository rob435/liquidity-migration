"""Tell the owner exactly what is left before real money can trade.

Arming used to be an eight-step runbook where every step failed in its own
vocabulary and half of them only failed at start-up, over a funded account.
This turns the whole checklist into one read-only command that reports every
step at once — what passes, what does not, and the exact fix for each — so the
owner discovers a missing file or a mistyped dial on a terminal instead of
discovering it as a crash loop next to an open position.

What it deliberately does **not** do:

* It never prints a secret. A credential is reported as present or absent, by
  variable name, and its value is never read into the report at all.
* It never writes a credential, sets ``REAL_MONEY``, or
  starts a unit. Every one of those is the owner's act, and a tool that could
  do them on the owner's behalf would be a tool that could do them by accident.
* It is not itself a safety layer. Every check here is re-run, independently
  and fail-closed, by the credential resolver and the owner runner. This
  exists so the owner is not the one discovering those failures one at a
  time.

``render-profile`` is the one mutating command, and it writes exactly one
non-secret artifact: the operational profile, derived from the dials in the
owner's env file and refused unless it passes the same load-time envelope proof
the account owner will apply to it.
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

from .env_flags import FALSE_ENV_VALUES, TRUE_ENV_VALUES
from .real_money_profile import (
    RealMoneyDials,
    dial_environment_keys,
    parse_real_money_dials,
    render_real_money_profile,
)
from .systemd_environment import parse_systemd_environment_bytes

__all__ = ["CheckResult", "preflight", "main"]

MAINNET_CREDENTIAL_ENV = Path("/etc/liquidity-migration/bybit-mainnet.env")
MAINNET_OWNER_ENV = Path("/etc/liquidity-migration/account-execution-mainnet.env")

#: Reported by name only. Their values are never read into the report.
_CREDENTIAL_KEYS = ("BYBIT_REAL_API_KEY", "BYBIT_REAL_API_SECRET")

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
    # Root-owned or caller-owned, so a
    # non-root dry run on a workstation reports the same thing the VPS will.
    # The deployed file is read by a root service and must be root-owned there;
    # the owner runner is what enforces that, fail-closed.
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
    """Presence only. The values are never read into the report."""

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
    # The mainnet owner unit sets TELEGRAM_ENABLED=1 and its runner exits
    # rather than start half-notified. Without this check the owner discovers
    # an empty token as a start-up failure *after* arming, which is the worst
    # possible moment to learn it. There is no mainnet watchdog unit, so this
    # is also the only channel that reports the owner is alive.
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
    """The single most likely arming mistake: edit a dial, forget to re-render.

    Reporting the dial-derived envelope as a PASS row while the *installed*
    profile enforces something else is worse than not reporting it at all: the
    owner reads a 2x envelope off the terminal and arms a 4x one. The receipt
    does not catch it either — it hashes the env file and the profile
    independently and never checks that one is the render of the other.
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
    # Issuance hard-requires these to exist, be directories, and be owned by
    # the issuing user. Nothing in the repository creates them, so without this
    # the owner discovers it as an issuance failure *after* arming.
    missing_roots = [
        key
        for key in ("ACCOUNT_EXECUTION_ROOT", "ACCOUNT_INTENT_INBOX_ROOT", "ACCOUNT_CAPTURE_ROOT")
        if values.get(key, "").strip() and not Path(values[key]).is_dir()
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
            else (
                "create them root-owned: mkdir -p "
                + " ".join(values[key] for key in missing_roots)
            ),
        )
    )
    artifacts = {
        "candidate universe": (
            values.get("ACCOUNT_SYMBOLS_FILE", ""),
            "scripts/freeze_account_candidate_universe.py (mainnet endpoint)",
        ),
        "instrument rules": (
            values.get("ACCOUNT_DEMO_RULES_FILE", ""),
            "scripts/freeze_venue_instrument_rules.py --realm mainnet",
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


def _sleeve_checks() -> list[CheckResult]:
    path = Path("/etc/liquidity-migration/sleeves.resolved.env")
    try:
        values = parse_systemd_environment_bytes(path.read_bytes(), label=str(path))
    except (OSError, ValueError) as exc:
        return [
            CheckResult(
                "sleeve toggles",
                False,
                f"{path} is unavailable: {exc}",
                "run the staged install so the resolved sleeve file is regenerated",
            )
        ]
    enabled = [
        key
        for key in ("CARRY_MAINNET_SLEEVE", "LONG_MAINNET_SLEEVE")
        if values.get(key, "").strip().lower() == "on"
    ]
    return [
        CheckResult(
            "sleeve toggles",
            bool(enabled),
            f"enabled: {', '.join(enabled)}" if enabled else "no mainnet producer is enabled",
            ""
            if enabled
            else (
                "turn CARRY_MAINNET_SLEEVE and/or LONG_MAINNET_SLEEVE on in "
                "deploy/sleeves.env and re-install -- repo-off is a hard ceiling a "
                "host override cannot lift"
            ),
        )
    ]


def preflight(
    *,
    credential_env: Path = MAINNET_CREDENTIAL_ENV,
    owner_env: Path = MAINNET_OWNER_ENV,
    check_sleeves: bool = True,
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
    if check_sleeves:
        results.extend(_sleeve_checks())
    return results


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
    check.add_argument("--no-sleeve-check", action="store_true")
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

    args = parser.parse_args(argv)
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
        check_sleeves=not args.no_sleeve_check,
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
