#!/usr/bin/env python3
"""Liveness watchdog for the deployed fleet and for the host itself.

Scope is ``demo``, ``mainnet``, or ``host``. The realm scopes read the fleet
manifest, require every always-on unit in the realm to be active, require each
heartbeat-bearing unit's heartbeat file to be fresh, require each signal worker
to leave its bounded startup and report ready, and alert when an engine reports
it can no longer open positions or that its rolling-loss trip is on.
The ``host`` scope watches the units the manifest marks independent — the
market recorder, its hourly upload, the state backup — plus disk space, the
off-box backup stamp, the recorder's own status file, the upload receipt, and
the host clock. It runs whether or not the trading fleet is up.

Telegram alerts repeat at most every --cooldown-min, while the incident routine
fires once per active fault and rearms only after resolution. Each sink keeps
its own delivery state: a failed call retries on the next timer run. The host
scope alone pings ONCALL_DEADMAN_URL on healthy runs so an external check catches
a dead box or watchdog plane without one surviving realm masking another.

Health faults exit 0 after they are reported. Broken routing or an unreachable
dead-man exits non-zero so systemd and the independent dead-man expose a broken
watchdog rather than painting it green.
"""

from __future__ import annotations

import argparse
import hashlib
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
from liquidity_migration.policy.oncall_environment import (  # noqa: E402
    NOTIFICATION_KEYS,
    ONCALL_KEYS,
    validate_notifications,
    validate_oncall,
)

_MANIFEST = _REPO_ROOT / "deploy" / "fleet_manifest.tsv"
_DEPLOY_LOCK = Path("/run/liquidity-migration/deploy.lock")
_ACCOUNT_SCOPES = ("demo", "mainnet", "host")
_SIGNAL_WORKER_HEARTBEAT_KIND = "liquidity_migration_signal_worker_heartbeat"
_ENGINE_UNITS = {
    "liquidity-migration-engine.service",
    "liquidity-migration-engine-mainnet.service",
}


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
    if not units:
        return {}
    result = subprocess.run(
        ["systemctl", "is-active", *units],
        capture_output=True,
        text=True,
        check=False,
    )
    states = result.stdout.splitlines()
    if len(states) != len(units):
        states = result.stdout.split()
    if len(states) != len(units):
        resolved: dict[str, str] = {}
        for unit in units:
            unit_res = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                check=False,
            )
            resolved[unit] = unit_res.stdout.strip() or "unknown"
        return resolved
    return dict(zip(units, [s.strip() for s in states], strict=True))


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
        alerts.extend(evaluate_engine_heartbeat(row.unit, path, now=now))
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


def _signal_worker_detail(payload: dict[str, object], *, now: float) -> str:
    reasons: list[str] = []
    if payload.get("bybit_ws_connected") is not True:
        reasons.append("Bybit WebSocket disconnected")
    if payload.get("bybit_ws_gap_open") is True:
        since_ms = _number(payload.get("bybit_ws_gap_open_since_wall_ts_ms"))
        if since_ms is None:
            reasons.append("Bybit WebSocket repair gap open")
        else:
            age_sec = max(0.0, now - since_ms / 1000)
            reasons.append(f"Bybit WebSocket repair gap open for {age_sec:.0f}s")
    if payload.get("bybit_ws_ticker_coverage_complete") is not True:
        reasons.append("ticker coverage incomplete")
    ticker_quarantined = _number(payload.get("bybit_ws_ticker_topics_quarantined"))
    kline_quarantined = _number(payload.get("bybit_ws_kline_topics_quarantined"))
    if ticker_quarantined is not None and ticker_quarantined > 0:
        reasons.append(f"{ticker_quarantined:g} ticker topics quarantined")
    if kline_quarantined is not None and kline_quarantined > 0:
        reasons.append(f"{kline_quarantined:g} kline topics quarantined")
    now_ms = now * 1000
    for lane, completed_key, cadence_key in (
        ("LONG", "last_long_cycle_completed_wall_ts_ms", "long_cycle_cadence_ms"),
        ("carry", "last_carry_cycle_completed_wall_ts_ms", "carry_cycle_cadence_ms"),
    ):
        completed_ms = _number(payload.get(completed_key))
        cadence_ms = _number(payload.get(cadence_key))
        if completed_ms is None:
            reasons.append(f"{lane} cycle has not completed")
        elif completed_ms > now_ms:
            reasons.append(f"{lane} cycle timestamp is in the future")
        elif cadence_ms is not None and now_ms - completed_ms > cadence_ms * 3:
            reasons.append(
                f"{lane} cycle is {(now_ms - completed_ms) / 1000:.0f}s old "
                f"(limit {cadence_ms * 3 / 1000:.0f}s)"
            )
    return "; ".join(reasons) or "worker self-check is degraded"


def evaluate_engine_heartbeat(
    unit: str, path: Path, *, now: float | None = None
) -> list[Alert]:
    # Freshness alone is not health. Signal workers publish their own verdict;
    # engines publish entry and loss latches. Other heartbeat-bearing units do
    # not carry these fields and receive only the structural JSON check here.
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return [Alert(f"heartbeat-parse:{unit}", "CRITICAL", f"{unit} heartbeat is not JSON")]
    if not isinstance(payload, dict):
        return [
            Alert(
                f"heartbeat-parse:{unit}",
                "CRITICAL",
                f"{unit} heartbeat is not a JSON object",
            )
        ]
    alerts = []
    is_signal_worker = "signal-worker" in unit
    if is_signal_worker and payload.get("kind") != _SIGNAL_WORKER_HEARTBEAT_KIND:
        alerts.append(
            Alert(
                f"heartbeat-contract:{unit}",
                "CRITICAL",
                f"{unit} heartbeat has the wrong or missing kind",
            )
        )
        return alerts
    if payload.get("kind") == _SIGNAL_WORKER_HEARTBEAT_KIND:
        status = payload.get("status")
        if status not in ("starting", "ready"):
            alerts.append(
                Alert(
                    f"worker-status:{unit}",
                    "CRITICAL",
                    f"{unit} reports {status!r}: "
                    f"{_signal_worker_detail(payload, now=time.time() if now is None else now)}",
                )
            )
        if payload.get("spool_backpressured") is True:
            alerts.append(
                Alert(
                    f"worker-spool:{unit}",
                    "CRITICAL",
                    f"{unit} signal spool is backpressured",
                )
            )
    if unit in _ENGINE_UNITS:
        invalid_verdicts = [
            field
            for field in ("may_open", "rolling_loss_tripped")
            if not isinstance(payload.get(field), bool)
        ]
    else:
        invalid_verdicts = []
    if invalid_verdicts:
        alerts.append(
            Alert(
                f"heartbeat-contract:{unit}",
                "CRITICAL",
                f"{unit} heartbeat has no boolean verdict for "
                f"{', '.join(invalid_verdicts)}",
            )
        )
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

    Silence and socket loss are measured from `started_at_ns`: a recorder that
    has just started has no frames and no connected sockets yet, and neither is
    a fault until it has been up longer than `max_silence_sec`. A status file
    with no `started_at_ns` predates the field and gets no grace.
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
    started_at_ns = _number(payload.get("started_at_ns"))
    uptime = None if started_at_ns is None or started_at_ns <= 0 else now - started_at_ns / 1e9
    warming_up = uptime is not None and uptime < max_silence_sec
    last_receive_ns = _number(payload.get("last_receive_ns"))
    if last_receive_ns is None or last_receive_ns <= 0:
        if uptime is None:
            alerts.append(Alert(key("capture-silent"), "CRITICAL", f"{who} has received no market frame yet"))
        elif not warming_up:
            alerts.append(
                Alert(
                    key("capture-silent"),
                    "CRITICAL",
                    f"{who} has received no market frame in the {uptime:.0f}s since it started "
                    f"(limit {max_silence_sec:.0f}s)",
                )
            )
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
    # A shard's socket connects a moment after the process opens it, so
    # connectivity says nothing about the venue until the grace window is out.
    if isinstance(shards, list) and not warming_up:
        down = [shard for shard in shards if isinstance(shard, dict) and shard.get("connected") is False]
        if down and len(down) == len(shards):
            alerts.append(Alert(key("capture-shards"), "CRITICAL", f"{who} has no live venue connection"))
        elif down:
            alerts.append(
                Alert(key("capture-shards"), "WARNING", f"{who} has {len(down)} of {len(shards)} venue connections down")
            )
    budget = payload.get("budget")
    if isinstance(budget, dict) and budget.get("over") is True:
        shed = budget.get("shed") or []
        alerts.append(
            Alert(
                key("capture-budget"),
                "WARNING",
                f"{who} projects {budget.get('projected_month_gb')} GB inbound this month against {budget.get('monthly_gb')} allowed; "
                + (f"shedding {', '.join(str(item) for item in shed)}" if shed else "nothing shed yet"),
            )
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


def unit_enabled_state(unit: str) -> str:
    result = subprocess.run(
        ["systemctl", "is-enabled", unit],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def unit_result(unit: str) -> str:
    result = subprocess.run(
        ["systemctl", "show", unit, "--property=Result", "--value"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def active_deploy_age(
    path: Path, *, now: float, lock_table: Path = Path("/proc/locks")
) -> float | None:
    """Seconds the deploy lock has been held, or None when no deploy owns it."""

    try:
        metadata = path.stat()
    except FileNotFoundError:
        return None
    identity = (
        f"{os.major(metadata.st_dev):02x}:{os.minor(metadata.st_dev):02x}:"
        f"{metadata.st_ino}"
    )
    for line in lock_table.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if (
            len(fields) >= 6
            and fields[1] == "FLOCK"
            and fields[3] == "WRITE"
            and fields[5] == identity
        ):
            return max(0.0, now - metadata.st_mtime)
    return None


def evaluate_watchdog_chain(
    *,
    now: float | None = None,
    deploy_lock: Path = _DEPLOY_LOCK,
    max_deploy_age_sec: float = 1_800.0,
) -> list[Alert]:
    """The host watchdog supervises the realm watchdogs that cannot see themselves.

    The deploy's existing exclusive lock is the maintenance boundary. A bounded
    lock suppresses transitional timer states; a stuck lock pages. Outside that
    boundary, demo is always required and mainnet is required while either its
    timer is enabled or its funded engine is running.
    """

    checked_at = time.time() if now is None else now
    try:
        deploy_age = active_deploy_age(deploy_lock, now=checked_at)
    except OSError as error:
        return [
            Alert(
                "deploy-lock",
                "CRITICAL",
                f"cannot inspect deployment lock: {error}",
            )
        ]
    if deploy_age is not None:
        if deploy_age <= max_deploy_age_sec:
            return []
        return [
            Alert(
                "deploy-lock",
                "CRITICAL",
                f"deployment lock has been held for {deploy_age:.0f}s "
                f"(limit {max_deploy_age_sec:.0f}s)",
            )
        ]

    timers = {
        "demo": "liquidity-migration-demo-liveness.timer",
        "mainnet": "liquidity-migration-mainnet-liveness.timer",
    }
    mainnet_engine = "liquidity-migration-engine-mainnet.service"
    active = unit_states([*timers.values(), mainnet_engine])
    alerts: list[Alert] = []
    for realm, timer in timers.items():
        enabled = unit_enabled_state(timer)
        expected = realm == "demo" or enabled.startswith("enabled")
        if realm == "mainnet" and active.get(mainnet_engine) == "active":
            expected = True
        if not expected:
            continue
        state = active.get(timer, "unknown")
        if state != "active":
            alerts.append(
                Alert(
                    f"watchdog:{realm}",
                    "CRITICAL",
                    f"{realm} watchdog timer is {state} ({enabled})",
                )
            )
            continue
        service = f"liquidity-migration-{realm}-liveness.service"
        result = unit_result(service)
        if result not in {"", "success"}:
            alerts.append(
                Alert(
                    f"watchdog:{realm}",
                    "CRITICAL",
                    f"{realm} watchdog last run result is {result}",
                )
            )
    return alerts


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


def select_incidents_to_fire(
    alerts: list[Alert], *, state: dict[str, float], now: float
) -> tuple[list[Alert], dict[str, float]]:
    """Return critical faults not yet handed to an agent in this lifetime."""

    current = {alert.key: alert for alert in alerts if alert.severity == "CRITICAL"}
    due = [current[key] for key in sorted(current) if key not in state]
    next_state = {key: state.get(key, now) for key in current}
    return due, next_state


def ping_heartbeat(url: str) -> None:
    with urllib.request.urlopen(url, timeout=10):
        pass


# The Claude Code routine API: one POST fires one agent run with the text as
# its untrusted payload. Both values come from the dedicated oncall.env; the
# token is per routine and is never an argument or log field.
INCIDENT_FIRE_URL_ENV = "INCIDENT_ROUTINE_FIRE_URL"
INCIDENT_FIRE_TOKEN_ENV = "INCIDENT_ROUTINE_FIRE_TOKEN"
INCIDENT_FIRE_BETA = "experimental-cc-routine-2026-04-01"
INCIDENT_TEXT_MAX = 60_000


def unit_journal_tail(unit: str, lines: int = 40) -> str:
    try:
        completed = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"(journal unavailable: {error})"
    if completed.returncode != 0:
        return f"(journal unavailable: exit {completed.returncode})"
    return completed.stdout.strip()


def _incident_units(scope: str, alerts: list[Alert]) -> list[str]:
    unit_alert_prefixes = (
        "unit:",
        "heartbeat:",
        "heartbeat-parse:",
        "heartbeat-contract:",
        "may-open:",
        "rolling-loss:",
        "worker-status:",
        "worker-spool:",
    )
    units = sorted(
        {
            alert.key.split(":", 1)[1]
            for alert in alerts
            if alert.severity == "CRITICAL"
            and alert.key.startswith(unit_alert_prefixes)
            and alert.key.endswith(".service")
        }
    )
    keys = {alert.key for alert in alerts if alert.severity == "CRITICAL"}
    if any(key.startswith("capture") and "forward-market-binance" in key for key in keys):
        units.append("liquidity-migration-forward-capture-binance.service")
    if any(key.startswith("capture") and "forward-market-binance" not in key for key in keys):
        units.append("liquidity-migration-forward-capture.service")
    if any(key.startswith("tape-upload") for key in keys):
        units.append("liquidity-migration-market-tape-upload.service")
    if "backup" in keys:
        units.append("liquidity-migration-backup.service")
    if scope == "host" and any(key.startswith("watchdog:") for key in keys):
        for key in keys:
            if key.startswith("watchdog:"):
                units.append(f"liquidity-migration-{key.split(':', 1)[1]}-liveness.service")
    return sorted(set(units))


def incident_text(
    scope: str,
    lines: list[str],
    alerts: list[Alert],
    due: list[Alert] | None = None,
) -> str:
    new_alerts = due if due is not None else [
        alert for alert in alerts if alert.severity == "CRITICAL"
    ]
    incident_key = "\n".join([scope, *(alert.key for alert in new_alerts)])
    incident_id = hashlib.sha256(incident_key.encode()).hexdigest()[:16]
    parts = [
        "schema_version=2",
        "event_kind=incident",
        f"incident_id={scope}-{incident_id}",
        f"scope={scope}",
        f"host={os.uname().nodename}",
        "new_critical_refs=" + ",".join(alert.key for alert in new_alerts),
        "",
        *lines,
    ]
    for unit in _incident_units(scope, alerts):
        parts += ["", f"--- journalctl -u {unit} -n 40", unit_journal_tail(unit)]
    text = "\n".join(parts)
    return text[:INCIDENT_TEXT_MAX]


def validate_runtime_routing() -> list[str]:
    errors: list[str] = []
    try:
        validate_notifications({key: os.environ.get(key, "") for key in NOTIFICATION_KEYS})
    except ValueError as exc:
        errors.append(str(exc))
    try:
        validate_oncall({key: os.environ.get(key, "") for key in ONCALL_KEYS})
    except ValueError as exc:
        errors.append(str(exc))
    return errors


def fire_incident_routine(url: str, token: str, text: str) -> str:
    """POST the incident to the routine; returns the run's session URL or ''."""
    body = json.dumps({"text": text}).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": INCIDENT_FIRE_BETA,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode() or "{}")
    return str(payload.get("claude_code_session_url") or "")


def transport_error(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, int):
        return f"HTTP {code}"
    return type(error).__name__


def run_delivery_drill(scope: str, deadman_url: str | None) -> int:
    if scope != "host":
        print("delivery drill requires --account-scope host", file=sys.stderr)
        return 2
    failed = False
    message = (
        "ON-CALL DRILL\n"
        f"host {os.uname().nodename}\n"
        "Telegram, incident routine, and external dead-man delivery test; no fault."
    )
    try:
        if not send_telegram_message(as_block(message), channel="alerts", parse_mode="HTML"):
            raise RuntimeError("Telegram route is not configured")
        print("delivery drill: telegram accepted")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"delivery drill: telegram failed ({transport_error(error)})")
        failed = True
    try:
        session = fire_incident_routine(
            os.environ[INCIDENT_FIRE_URL_ENV],
            os.environ[INCIDENT_FIRE_TOKEN_ENV],
            "\n".join(
                (
                    "schema_version=2",
                    "event_kind=drill",
                    "incident_id=delivery-drill",
                    f"scope={scope}",
                    f"host={os.uname().nodename}",
                    "No incident exists. Acknowledge receipt and make no changes.",
                )
            ),
        )
        print(f"delivery drill: incident routine accepted ({session or 'no session URL'})")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"delivery drill: incident routine failed ({transport_error(error)})")
        failed = True
    try:
        if not deadman_url:
            raise RuntimeError("dead-man route is not configured")
        ping_heartbeat(deadman_url)
        print("delivery drill: dead-man accepted")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"delivery drill: dead-man failed ({transport_error(error)})")
        failed = True
    return 1 if failed else 0


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
        "--require-oncall",
        action="store_true",
        help="fail when any Telegram, incident-routine, or dead-man route is missing",
    )
    p.add_argument(
        "--delivery-drill",
        action="store_true",
        help="exercise all three routes without evaluating fleet health (host scope only)",
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
        default=None,
        help="override the host scope's ONCALL_DEADMAN_URL",
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
    deadman_url = args.heartbeat_url or (
        os.environ.get("ONCALL_DEADMAN_URL") if scope == "host" else None
    )
    if args.require_oncall:
        errors = validate_runtime_routing()
        if errors:
            for error in errors:
                print(f"CRITICAL oncall-config: {error}")
            return 2
        args.telegram = True
    if args.delivery_drill:
        if not args.require_oncall:
            print("delivery drill requires --require-oncall", file=sys.stderr)
            return 2
        return run_delivery_drill(scope, deadman_url)
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
        alerts.extend(evaluate_watchdog_chain())
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

    if deadman_url and not any(alert.severity == "CRITICAL" for alert in alerts):
        try:
            ping_heartbeat(deadman_url)
        except (OSError, ValueError) as error:
            alerts.append(
                Alert(
                    "deadman",
                    "CRITICAL",
                    f"external dead-man ping failed ({transport_error(error)})",
                )
            )

    state = load_state(state_file)
    lines, next_state = select_alerts_to_send(
        alerts, state=state, now=now, cooldown_sec=args.cooldown_min * 60
    )
    routine_state_file = state_file.with_name(state_file.stem + ".routine.json")
    routine_state = load_state(routine_state_file)
    if not routine_state_file.exists():
        current_critical = {
            alert.key for alert in alerts if alert.severity == "CRITICAL"
        }
        routine_state = {
            key: sent_at for key, sent_at in state.items() if key in current_critical
        }
    due_incidents, next_routine_state = select_incidents_to_fire(
        alerts, state=routine_state, now=now
    )

    for alert in alerts:
        print(f"{alert.severity} {alert.key}: {alert.message}")
    routing_failed = any(alert.key == "deadman" for alert in alerts)
    if lines:
        message = f"fleet liveness ({scope})\n" + "\n".join(lines)
        if args.telegram:
            try:
                delivered = send_telegram_message(
                    as_block(message), channel="alerts", parse_mode="HTML"
                )
                if not delivered:
                    raise RuntimeError("Telegram route is not configured")
            except (OSError, RuntimeError, ValueError) as error:
                print(
                    "CRITICAL telegram: cannot deliver alerts "
                    f"({transport_error(error)})"
                )
                routing_failed = True
            else:
                save_state(state_file, next_state)
        else:
            print(message)
            save_state(state_file, next_state)
    if due_incidents and args.require_oncall:
        try:
            session = fire_incident_routine(
                os.environ[INCIDENT_FIRE_URL_ENV],
                os.environ[INCIDENT_FIRE_TOKEN_ENV],
                incident_text(scope, lines, alerts, due_incidents),
            )
            print(f"incident routine fired: {session or 'accepted'}")
        except (KeyError, OSError, RuntimeError, ValueError) as error:
            print(
                "CRITICAL incident-routine: cannot fire the on-call agent "
                f"({transport_error(error)})"
            )
            routing_failed = True
            retained = {
                key: fired_at
                for key, fired_at in routine_state.items()
                if key in next_routine_state
            }
            save_state(routine_state_file, retained)
        else:
            save_state(routine_state_file, next_routine_state)
    elif args.require_oncall:
        save_state(routine_state_file, next_routine_state)
    has_critical = any(alert.severity == "CRITICAL" for alert in alerts)
    if not has_critical:
        if not alerts:
            print(f"ok scope={scope} units-and-heartbeats-healthy")
        else:
            print(f"ok scope={scope} warnings-present-no-critical")
    return 1 if routing_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
