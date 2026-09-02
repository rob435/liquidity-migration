#!/usr/bin/env python3
"""Liveness watchdog for the deployed fleet and for the host itself.

Scope is ``demo``, ``mainnet``, or ``host``. The realm scopes read the fleet
manifest, require every always-on unit in the realm to be active, require each
heartbeat-bearing unit's heartbeat file to be fresh, and alert when an engine
reports it can no longer open positions or that its rolling-loss trip is on.
The ``host`` scope watches the units the manifest marks independent — the
market recorder, its hourly upload, the state backup — plus disk space, the
off-box backup stamp, the recorder's own status file, the upload receipt, and
the host clock. It runs whether or not the trading fleet is up.

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
_ACCOUNT_SCOPES = ("demo", "mainnet", "host")


@dataclass(frozen=True)
class FleetUnit:
    unit: str
    kind: str
    realm: str
    activation: str
    health: str
    output_artifact: str
    lifecycle: str = "downstream"


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
                lifecycle=fields[3],
                activation=fields[5],
                health=fields[8],
                output_artifact=fields[9],
            )
        )
    if not rows:
        raise ValueError(f"fleet manifest is empty: {path}")
    return rows


def scope_units(scope: str, rows: list[FleetUnit]) -> list[FleetUnit]:
    # Host watches the independent units and nothing else. Demo watches demo
    # and shared fleet units; mainnet watches only its own realm, so one cause
    # cannot page two scopes.
    if scope == "host":
        return [row for row in rows if row.lifecycle == "independent"]
    realms = {"demo", "shared"} if scope == "demo" else {"mainnet"}
    wanted = []
    for row in rows:
        if row.lifecycle == "independent" or row.realm not in realms:
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


def _number(value: object) -> float | None:
    """The value as a float, or None where the engine sent null or a non-number."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _rolling_loss_detail(payload: dict[str, object]) -> str:
    window_ms = _number(payload.get("rolling_loss_window_ms"))
    window = "the window" if window_ms is None else f"{window_ms / 3_600_000:g}h"
    net = _number(payload.get("rolling_loss_net_usdt"))
    limit = _number(payload.get("rolling_loss_limit_usdt"))
    if net is None or limit is None:
        return f"own closed trades are past the limit inside {window}"
    return f"own closed trades lost {abs(net):.2f} USDT inside {window} against a {limit:.2f} USDT limit"


def evaluate_engine_heartbeat(unit: str, path: Path) -> list[Alert]:
    # A fresh engine heartbeat can still say the engine latched itself out of
    # opening positions, or that its rolling-loss trip is on — states every
    # other check here reads as healthy. A heartbeat carrying neither field is
    # a worker's, a recorder's, or an older engine's, and pages for neither.
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return [Alert(f"heartbeat-parse:{unit}", "CRITICAL", f"{unit} heartbeat is not JSON")]
    if not isinstance(payload, dict):
        return []
    alerts = []
    if "may_open" in payload and payload.get("may_open") is not True:
        alerts.append(Alert(f"may-open:{unit}", "CRITICAL", f"{unit} cannot open positions"))
    if payload.get("rolling_loss_tripped") is True:
        alerts.append(
            Alert(
                f"rolling-loss:{unit}",
                "CRITICAL",
                f"{unit} rolling-loss trip is on: {_rolling_loss_detail(payload)}; entries refused",
            )
        )
    return alerts


def evaluate_capture_status(
    path: Path,
    *,
    now: float,
    max_silence_sec: float,
    counters: dict[str, float],
    label: str = "",
) -> tuple[list[Alert], dict[str, float]]:
    """A recorder's own status file: is data arriving, and is any being lost.

    `counters` holds the drop counts seen on the previous run; the returned
    copy holds this run's, so a drop pages once per increase, not forever.
    `label` tells one recorder's alerts and counters from another's when the
    host runs several.
    """

    def key(name: str) -> str:
        return f"{name}:{label}" if label else name

    who = f"recorder {label}" if label else "recorder"
    try:
        payload = json.loads(path.read_bytes())
    except OSError:
        return [Alert(key("capture-status"), "CRITICAL", f"{who} status is unreadable: {path}")], counters
    except ValueError:
        return [Alert(key("capture-status"), "CRITICAL", f"{who} status is not JSON")], counters
    if not isinstance(payload, dict):
        return [Alert(key("capture-status"), "CRITICAL", f"{who} status is not a JSON object")], counters
    alerts = []
    last_receive_ns = _number(payload.get("last_receive_ns"))
    if last_receive_ns is None or last_receive_ns <= 0:
        alerts.append(Alert(key("capture-silent"), "CRITICAL", f"{who} has received no market frame yet"))
    else:
        silence = now - last_receive_ns / 1e9
        if silence > max_silence_sec:
            alerts.append(
                Alert(
                    key("capture-silent"),
                    "CRITICAL",
                    f"{who} has received no market frame for {silence:.0f}s (limit {max_silence_sec:.0f}s)",
                )
            )
    if payload.get("disk_blocked") is True:
        alerts.append(
            Alert(key("capture-disk"), "CRITICAL", f"{who} storage is blocked; frames are counted but not written")
        )
    next_counters = dict(counters)
    for field_name, reason in (
        ("dropped_frames", "queue overran"),
        ("disk_dropped_frames", "storage was blocked"),
    ):
        count = _number(payload.get(field_name))
        if count is None:
            continue
        previous = counters.get(key(field_name))
        next_counters[key(field_name)] = count
        if previous is not None and count > previous:
            alerts.append(
                Alert(
                    key(f"capture-{field_name}"),
                    "WARNING",
                    f"{who} dropped {count - previous:.0f} frames since the last check ({reason})",
                )
            )
    shards = payload.get("shards")
    if isinstance(shards, list):
        down = [shard for shard in shards if isinstance(shard, dict) and shard.get("connected") is False]
        if down and len(down) == len(shards):
            alerts.append(Alert(key("capture-shards"), "CRITICAL", f"{who} has no live venue connection"))
        elif down:
            alerts.append(
                Alert(key("capture-shards"), "WARNING", f"{who} has {len(down)} of {len(shards)} venue connections down")
            )
    return alerts, next_counters


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


def _stamp_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def evaluate_upload_stamp(
    *,
    stamp_path: Path,
    now: float,
    max_age_hours: float,
    min_remote_free_gb: float,
) -> list[Alert]:
    """The market-tape upload's receipt: recent, and the Drive still has room."""

    try:
        age_hours = (now - stamp_path.stat().st_mtime) / 3600
        values = _stamp_values(stamp_path)
    except OSError:
        return [Alert("tape-upload", "WARNING", f"market-tape upload receipt is missing: {stamp_path}")]
    alerts = []
    if age_hours > max_age_hours:
        alerts.append(
            Alert(
                "tape-upload",
                "WARNING",
                f"last completed market-tape upload is {age_hours:.1f}h old (limit {max_age_hours:.0f}h)",
            )
        )
    free = values.get("remote_free_bytes", "")
    if free.isdigit() and int(free) / 1e9 < min_remote_free_gb:
        alerts.append(
            Alert(
                "tape-remote-space",
                "WARNING",
                f"the upload destination has {int(free) / 1e9:.0f} GB free (limit {min_remote_free_gb:.0f} GB)",
            )
        )
    return alerts


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
        help="which units and heartbeats to check: a realm, or the host itself (default: environment or demo)",
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
        help="stamp the backup script writes after a completed copy ('' skips)",
    )
    p.add_argument(
        "--max-backup-age-hours",
        type=float,
        default=26.0,
        help="alert when the last completed backup is older than this",
    )
    p.add_argument(
        "--capture-status-file",
        action="append",
        default=None,
        help="a market recorder's status.json; repeat for each recorder (none skips the data-flow checks)",
    )
    p.add_argument(
        "--max-capture-silence-sec",
        type=float,
        default=120.0,
        help="critical alert when the recorder has received no frame for this long",
    )
    p.add_argument(
        "--upload-stamp-file",
        default=os.environ.get("LIVENESS_UPLOAD_STAMP_FILE") or "",
        help="receipt the market-tape upload writes after a completed run ('' skips)",
    )
    p.add_argument(
        "--max-upload-age-hours",
        type=float,
        default=3.0,
        help="alert when the last completed market-tape upload is older than this",
    )
    p.add_argument(
        "--min-remote-free-gb",
        type=float,
        default=200.0,
        help="alert when the upload destination reports less free space than this",
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
    state_file = args.state_file or (
        _REPO_ROOT / "data" / ".cache" / f"liveness-{scope}.json"
    )
    counters_file = state_file.with_name(state_file.stem + ".counters.json")

    alerts: list[Alert] = []
    try:
        rows = scope_units(scope, load_fleet_manifest())
        alerts.extend(evaluate_units(scope, rows))
        alerts.extend(
            evaluate_heartbeats(rows, now=now, max_age_sec=args.max_heartbeat_age_sec)
        )
    except (OSError, ValueError) as error:
        alerts.append(Alert("manifest", "CRITICAL", f"cannot read the fleet manifest: {error}"))
    if scope == "host":
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
    capture_status_files = args.capture_status_file or (
        [os.environ["LIVENESS_CAPTURE_STATUS_FILE"]] if os.environ.get("LIVENESS_CAPTURE_STATUS_FILE") else []
    )
    if capture_status_files:
        counters = load_state(counters_file)
        for index, status_file in enumerate(capture_status_files):
            # The first recorder keeps the bare alert keys; later ones are
            # told apart by their state directory's name.
            label = "" if index == 0 else Path(status_file).parent.name
            capture_alerts, counters = evaluate_capture_status(
                Path(status_file),
                now=now,
                max_silence_sec=args.max_capture_silence_sec,
                counters=counters,
                label=label,
            )
            alerts.extend(capture_alerts)
        save_state(counters_file, counters)
    if args.upload_stamp_file:
        alerts.extend(
            evaluate_upload_stamp(
                stamp_path=Path(args.upload_stamp_file),
                now=now,
                max_age_hours=args.max_upload_age_hours,
                min_remote_free_gb=args.min_remote_free_gb,
            )
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
