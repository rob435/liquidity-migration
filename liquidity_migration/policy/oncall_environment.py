"""Prepare and validate the notification and on-call EnvironmentFiles."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlparse

from liquidity_migration.policy.systemd_environment import (
    load_private_systemd_environment,
)


NOTIFICATION_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_ALERT_CHAT_ID",
    "TELEGRAM_CONTROL_USER_IDS",
)
ONCALL_KEYS = (
    "INCIDENT_ROUTINE_FIRE_URL",
    "INCIDENT_ROUTINE_FIRE_TOKEN",
    "ONCALL_DEADMAN_URL",
)
_LEGACY_DEADMAN_KEY = "LIVENESS_HEARTBEAT_URL"
_CONTROL_IDS = re.compile(r"-?[0-9]+(?:\s*[,;]\s*-?[0-9]+)*")


def _required(values: Mapping[str, str], name: str, label: str) -> str:
    value = str(values.get(name) or "").strip()
    if not value:
        raise ValueError(f"{label} requires {name}")
    if any(character.isspace() for character in value):
        raise ValueError(f"{label} {name} contains whitespace")
    return value


def _https_url(values: Mapping[str, str], name: str, label: str) -> str:
    value = _required(values, name, label)
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{label} {name} must be an HTTPS URL without embedded credentials")
    return value


def validate_notifications(values: Mapping[str, str]) -> dict[str, str]:
    extras = sorted(set(values) - set(NOTIFICATION_KEYS))
    if extras:
        raise ValueError(f"notifications environment has unsupported keys: {', '.join(extras)}")
    filtered = {key: str(values.get(key) or "").strip() for key in NOTIFICATION_KEYS}
    _required(filtered, "TELEGRAM_BOT_TOKEN", "notifications environment")
    if not (filtered["TELEGRAM_ALERT_CHAT_ID"] or filtered["TELEGRAM_CHAT_ID"]):
        raise ValueError(
            "notifications environment requires TELEGRAM_ALERT_CHAT_ID or TELEGRAM_CHAT_ID"
        )
    control_ids = filtered["TELEGRAM_CONTROL_USER_IDS"]
    if control_ids and not _CONTROL_IDS.fullmatch(control_ids):
        raise ValueError("notifications environment TELEGRAM_CONTROL_USER_IDS is invalid")
    return {key: value for key, value in filtered.items() if value}


def validate_oncall(values: Mapping[str, str]) -> dict[str, str]:
    extras = sorted(set(values) - set(ONCALL_KEYS))
    if extras:
        raise ValueError(f"on-call environment has unsupported keys: {', '.join(extras)}")
    filtered = {key: str(values.get(key) or "").strip() for key in ONCALL_KEYS}
    routine_url = _https_url(
        filtered, "INCIDENT_ROUTINE_FIRE_URL", "on-call environment"
    )
    parsed = urlparse(routine_url)
    if parsed.hostname != "api.anthropic.com" or not re.fullmatch(
        r"/v1/claude_code/routines/[^/]+/fire", parsed.path
    ):
        raise ValueError(
            "on-call environment INCIDENT_ROUTINE_FIRE_URL is not a Claude Code routine fire URL"
        )
    _required(filtered, "INCIDENT_ROUTINE_FIRE_TOKEN", "on-call environment")
    _https_url(filtered, "ONCALL_DEADMAN_URL", "on-call environment")
    return filtered


def _write_private_environment(path: Path, values: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            for key, value in values.items():
                handle.write(f"{key}={value}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def prepare_environments(
    *,
    notifications_path: Path,
    oncall_path: Path,
    legacy_telegram_path: Path,
    legacy_liveness_path: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    if notifications_path.exists():
        notifications = validate_notifications(
            load_private_systemd_environment(notifications_path)
        )
    else:
        legacy = load_private_systemd_environment(legacy_telegram_path)
        notifications = validate_notifications(
            {key: legacy[key] for key in NOTIFICATION_KEYS if legacy.get(key)}
        )
        _write_private_environment(notifications_path, notifications)

    if oncall_path.exists():
        oncall = validate_oncall(load_private_systemd_environment(oncall_path))
    else:
        legacy = load_private_systemd_environment(legacy_liveness_path)
        migrated = {
            "INCIDENT_ROUTINE_FIRE_URL": legacy.get("INCIDENT_ROUTINE_FIRE_URL", ""),
            "INCIDENT_ROUTINE_FIRE_TOKEN": legacy.get(
                "INCIDENT_ROUTINE_FIRE_TOKEN", ""
            ),
            "ONCALL_DEADMAN_URL": legacy.get(_LEGACY_DEADMAN_KEY, ""),
        }
        oncall = validate_oncall(migrated)
        _write_private_environment(oncall_path, oncall)
    return notifications, oncall


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notifications", type=Path, required=True)
    parser.add_argument("--oncall", type=Path, required=True)
    parser.add_argument("--legacy-telegram", type=Path, required=True)
    parser.add_argument("--legacy-liveness", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("preparing private runtime files requires --execute")
    try:
        prepare_environments(
            notifications_path=args.notifications,
            oncall_path=args.oncall,
            legacy_telegram_path=args.legacy_telegram,
            legacy_liveness_path=args.legacy_liveness,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"on-call environment error: {exc}", file=sys.stderr)
        return 2
    print("on-call configuration ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "NOTIFICATION_KEYS",
    "ONCALL_KEYS",
    "prepare_environments",
    "validate_notifications",
    "validate_oncall",
]
