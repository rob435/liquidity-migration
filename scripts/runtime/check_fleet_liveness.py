#!/usr/bin/env python3
"""Fleet liveness watchdog for the deployed demo and mainnet realms.

Scope is ``demo`` or ``mainnet``. The checker reads the fleet manifest, requires
every always-on unit in the scope to be active, requires each heartbeat-bearing
unit's heartbeat file to be fresh, and alerts when an engine reports it can no
longer open positions. It also watches disk space, an optional off-box backup
stamp, and (in one scope per box) the host clock.

Alerts are de-duplicated with a cooldown state file: a new condition alerts
immediately, a persisting one re-alerts at most every --cooldown-min, and a
cleared one sends a one-line "resolved" note. --heartbeat-url (or
LIVENESS_HEARTBEAT_URL) is pinged on every healthy run so an external
dead-man's-switch catches a box death the on-box watchdog cannot. Telegram
delivery uses TELEGRAM_BOT_TOKEN with TELEGRAM_ALERT_CHAT_ID (falling back to
TELEGRAM_CHAT_ID).

Exits 0 always (a watchdog must not crash-loop); a failure to verify degrades
to an alert.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from liquidity_migration.ops.telegram import as_block, send_telegram_message  # noqa: E402

_MANIFEST = _REPO_ROOT / "deploy" / "fleet_manifest.tsv"
_ACCOUNT_SCOPES = ("demo", "mainnet")


@dataclass(frozen=True)
class FleetUnit:
    unit: str
    kind: str
    realm: str
    activation: str
    health: str
    output_artifact: str


@dataclass(frozen=True)
class Alert:
    key: str
    severity: str
    message: str


def load_fleet_manifest(path: Path = _MANIFEST) -> list[FleetUnit]:
    rows: list[FleetUnit] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("|")
        if len(fields) != 16:
            raise ValueError(f"fleet manifest row has {len(fields)} fields: {line!r}")
        rows.append(
            FleetUnit(
                unit=fields[0],
                kind=fields[1],
                realm=fields[2],
                activation=fields[5],
                health=fields[8],
                output_artifact=fields[9],
            )
        )
    if not rows:
        raise ValueError(f"fleet manifest is empty: {path}")
    return rows


def scope_units(scope: str, rows: list[FleetUnit]) -> list[FleetUnit]:
    # Demo watches demo and shared units; mainnet watches only its own realm,
    # so one cause cannot page both scopes.
    realms = {"demo", "shared"} if scope == "demo" else {"mainnet"}
    wanted = []
    for row in rows:
        if row.realm not in realms:
            continue
        if scope == "demo" and row.activation not in {"always", "job", "job-now"}:
            continue
        wanted.append(row)
    return wanted


def unit_states(units: list[str]) -> dict[str, str]:
    result = subprocess.run(
        ["systemctl", "is-active", *units],
        capture_output=True,
        text=True,
        check=False,
    )
    states = result.stdout.split()
    if len(states) != len(units):
        return {unit: "unknown" for unit in units}
    return dict(zip(units, states, strict=True))


def evaluate_units(scope: str, rows: list[FleetUnit]) -> list[Alert]:
    checked = [row for row in rows if row.health in {"active", "timer"}]
    if not checked:
        return [Alert("manifest", "CRITICAL", f"no {scope} units to check")]
    states = unit_states([row.unit for row in checked])
    alerts = []
    for row in checked:
        state = states.get(row.unit, "unknown")
        if state != "active":
            alerts.append(
                Alert(f"unit:{row.unit}", "CRITICAL", f"{row.unit} is {state}")
            )
    return alerts


def evaluate_heartbeats(
    rows: list[FleetUnit], *, now: float, max_age_sec: float
) -> list[Alert]:
    alerts = []
    for row in rows:
        if row.output_artifact == "-":
            continue
        path = Path(row.output_artifact)
        try:
            age = now - path.stat().st_mtime
        except OSError:
            alerts.append(
                Alert(
                    f"heartbeat:{row.unit}",
                    "CRITICAL",
                    f"{row.unit} heartbeat is unreadable: {path}",
                )
            )
            continue
        if age > max_age_sec:
            alerts.append(
                Alert(
                    f"heartbeat:{row.unit}",
                    "CRITICAL",
                    f"{row.unit} heartbeat is {age:.0f}s old (limit {max_age_sec:.0f}s)",
                )
            )
            continue
        alerts.extend(evaluate_engine_heartbeat(row.unit, path))
    return alerts


def evaluate_engine_heartbeat(unit: str, path: Path) -> list[Alert]:
    # A fresh engine heartbeat can still say the engine latched itself out of
    # opening positions — a state every other check here reads as healthy.
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return [Alert(f"heartbeat-parse:{unit}", "CRITICAL", f"{unit} heartbeat is not JSON")]
    if not isinstance(payload, dict) or "may_open" not in payload:
        return []
    if payload.get("may_open") is not True:
        return [Alert(f"may-open:{unit}", "CRITICAL", f"{unit} cannot open positions")]
    return []


def evaluate_disk(*, path: str = "/var/lib", min_free_gb: float = 5.0) -> list[Alert]:
    free_gb = shutil.disk_usage(path).free / 1e9
    if free_gb < min_free_gb:
        return [
            Alert(
                "disk",
                "CRITICAL",
                f"{path} has {free_gb:.1f} GB free (limit {min_free_gb:.0f} GB)",
            )
        ]
    return []


def evaluate_host_clock() -> list[Alert]:
    result = subprocess.run(
        ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != "yes":
        return [Alert("host-clock", "CRITICAL", "host clock is not NTP-synchronised")]
    return []


def evaluate_backup_stamp(
    *, stamp_path: Path, now: float, max_age_hours: float
) -> list[Alert]:
    try:
        age_hours = (now - stamp_path.stat().st_mtime) / 3600
    except OSError:
        return [Alert("backup", "WARNING", f"backup stamp is missing: {stamp_path}")]
    if age_hours > max_age_hours:
        return [
            Alert(
                "backup",
                "WARNING",
                f"last completed backup is {age_hours:.1f}h old (limit {max_age_hours:.0f}h)",
            )
        ]
    return []


def load_state(path: Path) -> dict[str, float]:
    try:
        payload = json.loads(path.read_bytes())
        return {str(key): float(value) for key, value in payload.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_state(path: Path, state: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def select_alerts_to_send(
    alerts: list[Alert], *, state: dict[str, float], now: float, cooldown_sec: float
) -> tuple[list[str], dict[str, float]]:
    lines = []
    next_state: dict[str, float] = {}
    current = {alert.key: alert for alert in alerts}
    for key, alert in sorted(current.items()):
        last = state.get(key)
        if last is None or now - last >= cooldown_sec:
            lines.append(f"{alert.severity} {alert.message}\nref {key}")
            next_state[key] = now
        else:
            next_state[key] = last
    for key in sorted(state):
        if key not in current:
            lines.append(f"RESOLVED {key}")
    return lines, next_state


def ping_heartbeat(url: str) -> None:
    try:
        with urllib.request.urlopen(url, timeout=10):
            pass
    except OSError:
        pass


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--account-scope",
        choices=_ACCOUNT_SCOPES,
        default=os.environ.get("ACCOUNT_LIVENESS_SCOPE") or "demo",
        help="which realm's units and heartbeats to check (default: environment or demo)",
    )
    p.add_argument(
        "--max-heartbeat-age-sec",
        type=float,
        default=60.0,
        help="critical alert if a unit's heartbeat file is older than this",
    )
    p.add_argument(
        "--cooldown-min",
        type=float,
        default=30.0,
        help="re-alert interval for a persisting condition",
    )
    p.add_argument(
        "--telegram",
        action="store_true",
        help="send alerts via Telegram (else stdout only)",
    )
    p.add_argument(
        "--host-clock-check",
        action="store_true",
        help=(
            "alert when timedatectl reports the box's clock unsynchronised. Off by "
            "default; turn it on in exactly one scope per box, or one cause pages twice"
        ),
    )
    p.add_argument(
        "--heartbeat-url",
        default=os.environ.get("LIVENESS_HEARTBEAT_URL") or None,
        help="ping this URL on a healthy run (external dead-man's-switch)",
    )
    p.add_argument(
        "--backup-stamp-file",
        default=os.environ.get("LIVENESS_BACKUP_STAMP_FILE") or "",
        help="stamp the backup script touches after a completed copy ('' skips)",
    )
    p.add_argument(
        "--max-backup-age-hours",
        type=float,
        default=26.0,
        help="alert when the last completed backup is older than this",
    )
    p.add_argument(
        "--state-file",
        type=Path,
        default=(
            Path(os.environ["LIVENESS_STATE_FILE"])
            if os.environ.get("LIVENESS_STATE_FILE")
            else None
        ),
        help="cooldown state file (default: environment, then <repo>/data/.cache; per scope)",
    )
    return p


def main() -> int:
    args = build_arg_parser().parse_args()
    scope = args.account_scope
    now = time.time()

    alerts: list[Alert] = []
    try:
        rows = scope_units(scope, load_fleet_manifest())
        alerts.extend(evaluate_units(scope, rows))
        alerts.extend(
            evaluate_heartbeats(rows, now=now, max_age_sec=args.max_heartbeat_age_sec)
        )
    except (OSError, ValueError) as error:
        alerts.append(Alert("manifest", "CRITICAL", f"cannot read the fleet manifest: {error}"))
    alerts.extend(evaluate_disk())
    if args.host_clock_check:
        alerts.extend(evaluate_host_clock())
    if args.backup_stamp_file:
        alerts.extend(
            evaluate_backup_stamp(
                stamp_path=Path(args.backup_stamp_file),
                now=now,
                max_age_hours=args.max_backup_age_hours,
            )
        )

    state_file = args.state_file or (
        _REPO_ROOT / "data" / ".cache" / f"liveness-{scope}.json"
    )
    state = load_state(state_file)
    lines, next_state = select_alerts_to_send(
        alerts, state=state, now=now, cooldown_sec=args.cooldown_min * 60
    )
    save_state(state_file, next_state)

    for alert in alerts:
        print(f"{alert.severity} {alert.key}: {alert.message}")
    if lines:
        message = f"fleet liveness ({scope})\n" + "\n".join(lines)
        if args.telegram:
            try:
                send_telegram_message(
                    as_block(message), channel="alerts", parse_mode="HTML"
                )
            except OSError as error:
                print(f"CRITICAL telegram: cannot deliver alerts: {error}")
        else:
            print(message)
    if not alerts:
        print(f"ok scope={scope} units-and-heartbeats-healthy")
        if args.heartbeat_url:
            ping_heartbeat(args.heartbeat_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
