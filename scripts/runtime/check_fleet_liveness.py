#!/usr/bin/env python3
"""Liveness watchdog for the deployed fleet and for the host itself.

Scope is ``demo``, ``mainnet``, ``mexc``, ``hyperliquid``, or ``host``. The realm
scopes read the fleet
manifest, require every always-on unit in the realm to be active, require each
heartbeat-bearing unit's heartbeat file to be fresh, require each signal worker
to leave its bounded startup and report ready, and alert when an engine reports
it can no longer open positions or that its rolling-loss trip is on.
The ``host`` scope watches the units the manifest marks independent — the
market recorder, its hourly upload, the state backup — plus disk space, the
off-box backup stamp, the recorder's own status file, the upload receipt, and
the host clock. It runs whether or not the trading fleet is up.

Severity says who has to act. ``CRITICAL`` is a fault somebody must fix: a dead
unit, a stale or contract-breaking heartbeat, a degraded worker, a broken route.
``WARNING`` is a reading heading the wrong way. ``NOTICE`` is a restriction the
system is enforcing on purpose, such as a rolling-loss trip — real, worth
reading, and nothing to repair. Only ``CRITICAL`` fires the incident routine and
holds back the dead-man ping; every severity reaches Telegram and resolves there.

Telegram alerts repeat at most every --cooldown-min, while the incident routine
fires once per active CRITICAL fault and rearms only after resolution. Each sink
keeps its own delivery state: a failed call retries on the next timer run. The host
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
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from liquidity_migration.ops.telegram import as_block, send_telegram_message  # noqa: E402
from liquidity_migration.core.venue_realm import MAINNET_REST_ENDPOINT  # noqa: E402
from liquidity_migration.policy.oncall_environment import (  # noqa: E402
    NOTIFICATION_KEYS,
    ONCALL_KEYS,
    validate_notifications,
    validate_oncall,
)

_MANIFEST = _REPO_ROOT / "deploy" / "fleet_manifest.tsv"
_DEPLOY_LOCK = Path("/run/liquidity-migration/deploy.lock")
_MAX_DEPLOY_AGE_SEC = 1_800.0
_DISK_FORECAST_SEC = 195.0  # Host timer: 180-second cadence plus 15-second accuracy.
_BOOT_ID_FILE = Path("/proc/sys/kernel/random/boot_id")
_ACCOUNT_SCOPES = ("demo", "mainnet", "mexc", "hyperliquid", "host")
#: Realms whose units run only while their own credential file is armed. Their
#: watchdog timers are expected up only once enabled or once their engine runs.
_FUNDED_REALMS = ("mainnet", "mexc", "hyperliquid")
_SIGNAL_WORKER_HEARTBEAT_KIND = "liquidity_migration_signal_worker_heartbeat"
_DEPLOY_TRANSITIONAL_ALERT_PREFIXES = (
    "unit:",
    "heartbeat:",
    "heartbeat-parse:",
    "heartbeat-contract:",
    "may-open:",
    "rolling-loss:",
    "strategy-errors:",
    "worker-status:",
    "worker-spool:",
    "capture-",
    "watchdog:",
    "engine-",
    "manifest",
)
_ENGINE_UNITS = {
    "liquidity-migration-engine.service",
    "liquidity-migration-engine-mainnet.service",
    "liquidity-migration-engine-mexc.service",
    "liquidity-migration-engine-hyperliquid.service",
}
_ENGINE_WAL_BYTES_PER_SECOND = 1_048_576
_ENGINE_RSS_BYTES = 1_610_612_736
_DEMO_SOAK_SECONDS = 300
_DEMO_SOAK_INTERVAL_SECONDS = 10
_CGROUP_ROOT = Path("/sys/fs/cgroup")


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
    # and shared fleet units; each funded realm watches only its own realm, so
    # one cause cannot page two scopes.
    if scope == "host":
        return [row for row in rows if row.lifecycle == "independent"]
    realms = {"demo", "shared"} if scope == "demo" else {scope}
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
            alerts.append(Alert(f"unit:{row.unit}", "CRITICAL", f"{row.unit} is {state}"))
    return alerts


def evaluate_heartbeats(rows: list[FleetUnit], *, now: float, max_age_sec: float) -> list[Alert]:
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
        return f"rolling loss is past the limit inside {window}"
    return f"rolling loss is {abs(net):.2f} USDT inside {window} against a {limit:.2f} USDT limit"


def _transport_reasons(payload: dict[str, object], *, now: float) -> list[str]:
    """The transport inputs the worker's own verdict turns on that no other
    line reports: a kline subscription short of the symbol count, and a frame
    drought. Either one alone flips a bounded cold fill to ``degraded``."""

    reasons: list[str] = []
    accepted = _number(payload.get("bybit_ws_kline_topics_accepted"))
    capacity = _number(payload.get("bybit_ws_ticker_capacity"))
    if accepted is not None and capacity is not None and accepted != capacity:
        reasons.append(f"{accepted:g}/{capacity:g} kline topics accepted")
    last_frame_ms = _number(payload.get("bybit_ws_last_frame_ts_ms"))
    if last_frame_ms is None:
        reasons.append("no Bybit WebSocket frame recorded")
        return reasons
    limit_ms = _number(payload.get("bybit_ws_max_frame_age_ms"))
    written_ms = _number(payload.get("updated_at_ms"))
    clock_ms = written_ms if written_ms else now * 1000
    age_ms = clock_ms - last_frame_ms
    if age_ms < 0:
        reasons.append("Bybit WebSocket frame timestamp is in the future")
    elif limit_ms is not None and age_ms > limit_ms:
        reasons.append(
            f"no Bybit WebSocket frame for {age_ms / 1000:.0f}s (limit {limit_ms / 1000:.0f}s)"
        )
    return reasons


def _signal_worker_detail(payload: dict[str, object], *, now: float) -> str:
    reasons: list[str] = []
    if payload.get("bybit_ws_connected") is not True:
        reasons.append("Bybit WebSocket disconnected")
    else:
        reasons.extend(_transport_reasons(payload, now=now))
    if payload.get("bybit_ws_gap_open") is True:
        since_ms = _number(payload.get("bybit_ws_gap_open_since_wall_ts_ms"))
        if since_ms is None:
            reasons.append("Bybit WebSocket repair gap open")
        else:
            age_sec = max(0.0, now - since_ms / 1000)
            reasons.append(f"Bybit WebSocket repair gap open for {age_sec:.0f}s")
    if payload.get("bybit_ws_ticker_coverage_complete") is not True:
        # The counts say whether the fill is short by a few symbols or empty.
        rows = _number(payload.get("bybit_ws_ticker_rows"))
        capacity = _number(payload.get("bybit_ws_ticker_capacity"))
        accepted = _number(payload.get("bybit_ws_ticker_topics_accepted"))
        if rows is None or capacity is None or accepted is None:
            reasons.append("ticker coverage incomplete")
        else:
            reasons.append(
                f"ticker coverage incomplete ({rows:g}/{capacity:g} rows, "
                f"{accepted:g}/{capacity:g} topics accepted)"
            )
    ticker_quarantined = _number(payload.get("bybit_ws_ticker_topics_quarantined"))
    kline_quarantined = _number(payload.get("bybit_ws_kline_topics_quarantined"))
    if ticker_quarantined is not None and ticker_quarantined > 0:
        reasons.append(f"{ticker_quarantined:g} ticker topics quarantined")
    if kline_quarantined is not None and kline_quarantined > 0:
        reasons.append(f"{kline_quarantined:g} kline topics quarantined")
    now_ms = now * 1000
    for lane, completed_key, cadence_key, due_key in (
        ("LONG", "last_long_cycle_completed_wall_ts_ms", "long_cycle_cadence_ms", None),
        (
            "carry",
            "last_carry_cycle_completed_wall_ts_ms",
            "carry_cycle_cadence_ms",
            "carry_cycle_not_before_wall_ts_ms",
        ),
    ):
        completed_ms = _number(payload.get(completed_key))
        cadence_ms = _number(payload.get(cadence_key))
        # The carry lane scores a daily decision boundary and cannot complete
        # for it before the boundary's own funding print is publishable, so the
        # worker publishes that instant and the age runs from it.
        due_ms = _number(payload.get(due_key)) if due_key else None
        if completed_ms is None:
            reasons.append(f"{lane} cycle has not completed")
            continue
        if completed_ms > now_ms:
            reasons.append(f"{lane} cycle timestamp is in the future")
            continue
        age_from_ms = max(completed_ms, due_ms) if due_ms is not None else completed_ms
        if cadence_ms is not None and now_ms - age_from_ms > cadence_ms * 3:
            reasons.append(
                f"{lane} cycle is {(now_ms - age_from_ms) / 1000:.0f}s old (limit {cadence_ms * 3 / 1000:.0f}s)"
            )
    return "; ".join(reasons) or "worker self-check is degraded"


def evaluate_engine_heartbeat(unit: str, path: Path, *, now: float | None = None) -> list[Alert]:
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
        if status not in ("starting", "recovering", "ready"):
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
            field for field in ("may_open", "rolling_loss_tripped") if not isinstance(payload.get(field), bool)
        ]
    else:
        invalid_verdicts = []
    if invalid_verdicts:
        alerts.append(
            Alert(
                f"heartbeat-contract:{unit}",
                "CRITICAL",
                f"{unit} heartbeat has no boolean verdict for {', '.join(invalid_verdicts)}",
            )
        )
    if "may_open" in payload and payload.get("may_open") is not True:
        alerts.append(Alert(f"may-open:{unit}", "CRITICAL", f"{unit} cannot open positions"))
    if payload.get("rolling_loss_tripped") is True:
        # The breaker doing its job is not a fault: NOTICE reports it and leaves
        # the incident routine for things a fix can change.
        alerts.append(
            Alert(
                f"rolling-loss:{unit}",
                "NOTICE",
                f"{unit} rolling-loss trip is on: {_rolling_loss_detail(payload)}; entries refused",
            )
        )
    strategy_errors = payload.get("strategy_errors")
    if unit in _ENGINE_UNITS and isinstance(strategy_errors, list) and strategy_errors:
        detail = "; ".join(
            f"{row.get('strategy', 'unknown')}: {row.get('error', 'unspecified')}"
            for row in strategy_errors
            if isinstance(row, dict)
        )
        alerts.append(
            Alert(
                f"strategy-errors:{unit}",
                "CRITICAL",
                f"{unit} reports strategy errors: {detail or str(strategy_errors)}",
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

    `counters` holds the drop counts and partial shard loss seen on the
    previous run. A drop warns once per increase; partial shard loss must
    persist for two runs, while complete connection loss remains immediate.
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
    # Dynamic tiers also publish their new shard before its socket connects;
    # require partial loss on two host ticks so that sub-second handoff cannot
    # create a warning and resolution pair. Total loss remains immediate.
    if isinstance(shards, list):
        down = [shard for shard in shards if isinstance(shard, dict) and shard.get("connected") is False]
        down_key = key("shards_down")
        previous_down = counters.get(down_key)
        next_counters[down_key] = float(len(down))
        if not warming_up and down and len(down) == len(shards):
            alerts.append(Alert(key("capture-shards"), "CRITICAL", f"{who} has no live venue connection"))
        elif not warming_up and down and previous_down is not None and previous_down > 0:
            alerts.append(
                Alert(
                    key("capture-shards"), "WARNING", f"{who} has {len(down)} of {len(shards)} venue connections down"
                )
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


def _sample_number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def _wal_files(family: Path) -> dict[str, float] | None:
    files = {}
    try:
        for entry in family.parent.iterdir():
            suffix = entry.name.removeprefix(f"{family.name}.")
            numbered = (
                entry.name.startswith(f"{family.name}.")
                and 6 <= len(suffix) <= 20
                and suffix.isascii()
                and suffix.isdigit()
                and 2 <= int(suffix) <= 2**64 - 1
                and suffix == f"{int(suffix):06}"
            )
            if entry.name != family.name and not numbered:
                continue
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(metadata.st_mode):
                files[f"{metadata.st_dev}:{metadata.st_ino}"] = float(metadata.st_size)
    except OSError:
        return None
    return files or None


def _wal_attribution(rows: list[FleetUnit], previous: dict[str, float], counters: dict[str, float]) -> str:
    details = []
    seen = set()
    for row in rows:
        if row.unit not in _ENGINE_UNITS or not Path(row.output_artifact).is_absolute():
            continue
        # Fleet templates place engine.wal beside the manifest heartbeat.
        # This describes those canonical files, not arbitrary config overrides.
        family = Path(row.output_artifact).with_name("engine.wal")
        if family in seen:
            continue
        seen.add(family)
        files = _wal_files(family)
        if files is None:
            details.append(f"{row.realm}=unavailable")
            continue
        prefix = f"wal:{row.realm}:"
        current = {f"{prefix}{identity}": size for identity, size in files.items()}
        prior = {key: value for key, value in previous.items() if key.startswith(prefix)}
        counters.update(current)
        total = sum(current.values())
        if prior and all(
            key in current and _sample_number(size) is not None and current[key] >= size for key, size in prior.items()
        ):
            delta = f"+{total - sum(prior.values()):.0f}"
        else:
            delta = "unavailable"
        details.append(f"{row.realm}={total:.0f} bytes (delta {delta})")
    return "canonical WAL logical bytes: " + (", ".join(details) or "unavailable")


def engine_service_sample(unit: str) -> dict[str, float]:
    result = subprocess.run(
        ["systemctl", "show", unit, "--property=MainPID,NRestarts,ControlGroup,ActiveState"],
        capture_output=True, text=True, check=True, timeout=5,
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if values.get("ActiveState") != "active" or int(values["MainPID"]) <= 0:
        raise ValueError(f"{unit} has no active process")
    group = values["ControlGroup"]
    if not group.startswith("/") or ".." in Path(group).parts:
        raise ValueError(f"{unit} has no valid cgroup")
    memory = dict(line.split() for line in (_CGROUP_ROOT / group.lstrip("/") / "memory.stat").read_text().splitlines())
    return {"pid": float(values["MainPID"]), "restarts": float(values["NRestarts"]), "rss": float(memory["anon"])}


def engine_error_count(unit: str, since: float, until: float) -> int:
    result = subprocess.run(
        ["journalctl", "-u", unit, "--since", f"@{since:.6f}", "--until", f"@{until:.6f}",
         "--no-pager", "--output=json"],
        capture_output=True, text=True, check=True, timeout=5,
    )
    count = 0
    for line in result.stdout.splitlines():
        row = json.loads(line)
        message = row.get("MESSAGE", "")
        priority = int(row.get("PRIORITY", 6))
        if priority <= 3 or (isinstance(message, str) and re.search(r"\bERROR\b|panicked at|^engine:", message)):
            count += 1
    return count


def evaluate_engine_rates(
    rows: list[FleetUnit], *, now: float, counters: dict[str, float],
) -> list[Alert]:
    alerts = []
    for row in rows:
        if row.unit not in _ENGINE_UNITS:
            continue
        prefix = f"engine-rate:{row.unit}:"
        previous = {key.removeprefix(prefix): value for key, value in counters.items() if key.startswith(prefix)}
        try:
            sample = engine_service_sample(row.unit)
            files = _wal_files(Path(row.output_artifact).with_name("engine.wal"))
            if files is None:
                raise ValueError("WAL family is unreadable")
            sample.update({f"wal:{identity}": size for identity, size in files.items()})
            sample["time"] = now
            if sample["rss"] > _ENGINE_RSS_BYTES:
                alerts.append(Alert(f"engine-rss:{row.unit}", "CRITICAL",
                                    f"{row.unit} anonymous RSS {sample['rss']:.0f} bytes exceeds {_ENGINE_RSS_BYTES}"))
            if previous:
                elapsed = now - previous["time"]
                if elapsed <= 0 or elapsed > 60:
                    raise ValueError(f"resource sample gap {elapsed:.1f}s is outside (0, 60]")
                prior_wal = {key: size for key, size in previous.items() if key.startswith("wal:")}
                if any(key not in sample or sample[key] < size for key, size in prior_wal.items()):
                    raise ValueError("WAL family shrank or lost a retained segment")
                growth = sum(files.values()) - sum(prior_wal.values())
                if growth / elapsed > _ENGINE_WAL_BYTES_PER_SECOND:
                    alerts.append(Alert(f"engine-wal-rate:{row.unit}", "CRITICAL",
                                        f"{row.unit} WAL {growth / elapsed:.0f} bytes/s exceeds {_ENGINE_WAL_BYTES_PER_SECOND}"))
                if sample["pid"] != previous["pid"] or sample["restarts"] != previous["restarts"]:
                    alerts.append(Alert(f"engine-restarts:{row.unit}", "CRITICAL",
                                        f"{row.unit} process changed or restarted during the sample interval"))
                errors = engine_error_count(row.unit, previous["time"], now)
                if errors:
                    alerts.append(Alert(f"engine-error-rate:{row.unit}", "CRITICAL",
                                        f"{row.unit} {errors} errors in {elapsed:.1f}s ({errors / elapsed:.3f}/s); limit 0"))
            for key in list(counters):
                if key.startswith(prefix):
                    del counters[key]
            counters.update({prefix + key: value for key, value in sample.items()})
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            alerts.append(Alert(f"engine-resource-sample:{row.unit}", "CRITICAL", f"{row.unit}: {error}"))
            # An unavailable interval cannot establish recovery; a later pair can.
            for key in list(counters):
                if key.startswith(prefix):
                    del counters[key]
    return alerts


def deployment_blockers(alerts: list[Alert]) -> list[Alert]:
    # A rolling-loss restriction must not prevent replacing a running engine.
    # The risk kernel still refuses entries; routine liveness still reports the trip.
    return [alert for alert in alerts if not alert.key.startswith("rolling-loss:")]


def run_demo_soak() -> int:
    rows = [row for row in load_fleet_manifest()
            if row.realm == "demo" and (row.unit in _ENGINE_UNITS or "signal-worker" in row.unit)]
    counters: dict[str, float] = {}
    reported_restrictions: set[str] = set()
    started = time.monotonic()
    while True:
        now = time.time()
        alerts = evaluate_units("demo", rows)
        alerts.extend(evaluate_heartbeats(rows, now=now, max_age_sec=30))
        alerts.extend(evaluate_engine_rates(rows, now=now, counters=counters))
        for alert in alerts:
            if alert.key.startswith("rolling-loss:") and alert.key not in reported_restrictions:
                print(f"{alert.severity} {alert.key}: {alert.message}", flush=True)
                reported_restrictions.add(alert.key)
        alerts = deployment_blockers(alerts)
        if alerts:
            lines = [f"CRITICAL {alert.key}: {alert.message}" for alert in alerts]
            message = "demo soak refused; funded realms remain on their incumbent runtimes\n" + "\n".join(lines)
            print(message, flush=True)
            try:
                if not send_telegram_message(as_block(message), channel="alerts", parse_mode="HTML"):
                    raise RuntimeError("Telegram route is not configured")
            except (OSError, RuntimeError, ValueError) as error:
                print(f"CRITICAL telegram: {transport_error(error)}", flush=True)
            try:
                fire_incident_routine(os.environ[INCIDENT_FIRE_URL_ENV], os.environ[INCIDENT_FIRE_TOKEN_ENV],
                                      incident_text("demo", lines, alerts))
            except (KeyError, OSError, RuntimeError, ValueError) as error:
                print(f"CRITICAL incident-routine: {transport_error(error)}", flush=True)
            return 1
        elapsed = time.monotonic() - started
        print(f"demo-soak healthy elapsed={elapsed:.0f}s required={_DEMO_SOAK_SECONDS}s", flush=True)
        if elapsed >= _DEMO_SOAK_SECONDS:
            return 0
        time.sleep(min(_DEMO_SOAK_INTERVAL_SECONDS, _DEMO_SOAK_SECONDS - elapsed))


def evaluate_disk(
    *,
    path: str = "/var/lib",
    min_free_gb: float = 5.0,
    counters: dict[str, float] | None = None,
    rows: list[FleetUnit] | None = None,
) -> list[Alert]:
    free_bytes = shutil.disk_usage(path).free
    free_gb = free_bytes / 1e9
    alerts = []
    if free_gb < min_free_gb:
        alerts.append(
            Alert(
                "disk",
                "CRITICAL",
                f"{path} has {free_gb:.1f} GB free (limit {min_free_gb:.0f} GB)",
            )
        )
    if counters is None:
        return alerts
    previous = dict(counters)
    for key in previous:
        if key.startswith(("disk:", "wal:")):
            del counters[key]
    counters["disk:forecast_valid"] = float(bool(alerts))
    try:
        boot = _BOOT_ID_FILE.read_text(encoding="utf-8").strip()
        device = Path(path).stat().st_dev
    except OSError:
        return alerts
    observed = _sample_number(time.monotonic())
    if not boot or observed is None or _sample_number(free_bytes) is None:
        return alerts
    prefix = f"disk:{boot}:{device}:"
    counters.update({f"{prefix}time": observed, f"{prefix}free": float(free_bytes)})
    prior_time = _sample_number(previous.get(f"{prefix}time"))
    prior_free = _sample_number(previous.get(f"{prefix}free"))
    elapsed = None if prior_time is None else observed - prior_time
    current_interval = elapsed is not None and 0 < elapsed <= _DISK_FORECAST_SEC and prior_free is not None
    counters["disk:forecast_valid"] = float(bool(alerts) or current_interval)
    attribution = _wal_attribution(rows or [], previous if current_interval else {}, counters)
    if current_interval and prior_free is not None and elapsed is not None and prior_free > free_bytes and not alerts:
        rate = (prior_free - free_bytes) / elapsed
        seconds = (free_bytes - min_free_gb * 1e9) / rate
        if seconds < _DISK_FORECAST_SEC:
            alerts.append(
                Alert(
                    "disk-growth",
                    "WARNING",
                    f"{path} has {free_gb:.1f} GB free; observed consumption {rate / 1e6:.2f} MB/s "
                    f"projects the {min_free_gb:.0f} GB floor in {seconds:.0f}s, before the next "
                    f"{_DISK_FORECAST_SEC:.0f}s observation; {attribution}",
                )
            )
    return alerts


def evaluate_host_clock() -> list[Alert]:
    result = subprocess.run(
        ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != "yes":
        return [Alert("host-clock", "CRITICAL", "host clock is not NTP-synchronised")]
    try:
        before = time.time()
        started = time.monotonic()
        with urllib.request.urlopen(f"{MAINNET_REST_ENDPOINT}/v5/market/time", timeout=5) as response:
            body = json.load(response)
        after = time.time()
        elapsed = time.monotonic() - started
        if body.get("retCode") != 0:
            raise ValueError("venue time request was rejected")
        venue_time = int(body["result"]["timeNano"]) / 1e9
        if not math.isfinite(venue_time) or venue_time <= 0:
            raise ValueError("invalid venue time")
    except (OSError, ValueError, KeyError, TypeError) as error:
        return [Alert("host-clock", "WARNING", f"cannot measure venue clock offset: {error}")]
    if abs((after - before) - elapsed) > 0.05:
        return [Alert("host-clock", "CRITICAL", "host clock stepped during venue clock measurement")]
    if elapsed > 1.0:
        return [Alert("host-clock", "WARNING", f"venue clock measurement is inconclusive: RTT {elapsed * 1000:.0f}ms")]
    # The server timestamp lies within the request interval; half RTT bounds its uncertainty.
    offset = venue_time - (before + after) / 2
    if abs(offset) - elapsed / 2 > 0.25:
        return [Alert("host-clock", "CRITICAL", f"venue clock offset {offset * 1000:+.0f}ms, uncertainty {elapsed * 500:.0f}ms exceeds 250ms")]
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


def active_deploy_age(path: Path, *, now: float, lock_table: Path = Path("/proc/locks")) -> float | None:
    """Seconds the deploy lock has been held, or None when no deploy owns it."""

    try:
        metadata = path.stat()
    except FileNotFoundError:
        return None
    identity = f"{os.major(metadata.st_dev):02x}:{os.minor(metadata.st_dev):02x}:{metadata.st_ino}"
    for line in lock_table.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[1] == "FLOCK" and fields[3] == "WRITE" and fields[5] == identity:
            return max(0.0, now - metadata.st_mtime)
    return None


def evaluate_watchdog_chain(
    *,
    now: float | None = None,
    deploy_lock: Path = _DEPLOY_LOCK,
    max_deploy_age_sec: float = _MAX_DEPLOY_AGE_SEC,
) -> list[Alert]:
    """The host watchdog supervises the realm watchdogs that cannot see themselves.

    The deploy's existing exclusive lock is the maintenance boundary. A bounded
    lock suppresses transitional timer states; a stuck lock pages. Outside that
    boundary, demo is always required and each funded realm is required while
    either its timer is enabled or its engine is running.
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
                f"deployment lock has been held for {deploy_age:.0f}s (limit {max_deploy_age_sec:.0f}s)",
            )
        ]

    timers = {
        realm: f"liquidity-migration-{realm}-liveness.timer"
        for realm in ("demo", *_FUNDED_REALMS)
    }
    engines = {
        "mainnet": "liquidity-migration-engine-mainnet.service",
        "mexc": "liquidity-migration-engine-mexc.service",
        "hyperliquid": "liquidity-migration-engine-hyperliquid.service",
    }
    active = unit_states([*timers.values(), *engines.values()])
    alerts: list[Alert] = []
    for realm, timer in timers.items():
        enabled = unit_enabled_state(timer)
        expected = realm == "demo" or enabled.startswith("enabled")
        engine = engines.get(realm)
        if engine is not None and active.get(engine) == "active":
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


def evaluate_backup_stamp(*, stamp_path: Path, now: float, max_age_hours: float) -> list[Alert]:
    try:
        age_hours = (now - stamp_path.stat().st_mtime) / 3600
    except OSError:
        return [Alert("backup", "WARNING", f"backup stamp is missing: {stamp_path}")]
    if age_hours > max_age_hours:
        return [
            Alert(
                "backup",
                "WARNING",
                f"last completed backup is {age_hours:.1f}h old (limit {max_age_hours:g}h)",
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
    alerts: list[Alert],
    *,
    state: dict[str, float],
    now: float,
    cooldown_sec: float,
    preserve_keys: set[str] | None = None,
) -> tuple[list[str], dict[str, float]]:
    preserved = preserve_keys or set()
    lines = []
    next_state: dict[str, float] = {key: state[key] for key in preserved if key in state}
    current = {alert.key: alert for alert in alerts}
    for key, alert in sorted(current.items()):
        last = state.get(key)
        if last is None or now - last >= cooldown_sec:
            lines.append(f"{alert.severity} {alert.message}\nref {key}")
            next_state[key] = now
        else:
            next_state[key] = last
    for key in sorted(state):
        if key not in current and key not in preserved:
            lines.append(f"RESOLVED {key}")
    return lines, next_state


def select_incidents_to_fire(
    alerts: list[Alert], *, state: dict[str, float], now: float, preserve_keys: set[str] | None = None
) -> tuple[list[Alert], dict[str, float]]:
    """Return critical faults not yet handed to an agent in this lifetime."""

    current = {alert.key: alert for alert in alerts if alert.severity == "CRITICAL"}
    due = [current[key] for key in sorted(current) if key not in state]
    next_state = {key: state.get(key, now) for key in current}
    for key in preserve_keys or set():
        if key in state:
            next_state[key] = state[key]
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
INCIDENT_ERROR_BODY_MAX = 4_096
_INCIDENT_ERROR_TYPES = frozenset(
    {
        "invalid_request_error",
        "authentication_error",
        "permission_error",
        "not_found_error",
        "rate_limit_error",
        "api_error",
        "overloaded_error",
    }
)


class IncidentRoutineError(RuntimeError):
    def __init__(self, code: int, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"HTTP {code} ({detail})")


def _incident_error_detail(error: urllib.error.HTTPError, token: str) -> str:
    try:
        with error:
            body = error.read(INCIDENT_ERROR_BODY_MAX + 1)
    except (OSError, ValueError):
        return "unreadable error response"
    if len(body) > INCIDENT_ERROR_BODY_MAX:
        return "response too large"
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        return "invalid error response"
    problem = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(problem, dict) or not isinstance(problem.get("message"), str):
        return "invalid error response"
    kind = problem.get("type")
    if not isinstance(kind, str) or kind not in _INCIDENT_ERROR_TYPES:
        kind = "unknown_error"
    message = problem["message"]
    if token:
        message = message.replace(token, "[redacted]")
    message = re.sub(r"(?i)\b(?:authorization|proxy-authorization|x-api-key)\s*:[^\r\n]*", "[redacted header]", message)
    message = re.sub(r"(?i)\bBearer\s+\S+|\bsk-ant-[\w-]+", "[redacted token]", message)
    message = re.sub(r"(?i)https?://\S+", "[redacted URL]", message)
    message = " ".join("".join(char if char.isprintable() else " " for char in message).split())
    return f"{kind}: {message[:300]}"


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
        "strategy-errors:",
        "worker-status:",
        "worker-spool:",
        "engine-wal-rate:",
        "engine-rss:",
        "engine-restarts:",
        "engine-error-rate:",
        "engine-resource-sample:",
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
    new_alerts = due if due is not None else [alert for alert in alerts if alert.severity == "CRITICAL"]
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
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        raise IncidentRoutineError(error.code, _incident_error_detail(error, token)) from None
    return str(payload.get("claude_code_session_url") or "")


def transport_error(error: BaseException) -> str:
    if isinstance(error, IncidentRoutineError):
        return str(error)
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
    p.add_argument("--check-heartbeat", nargs=4, metavar=("UNIT", "PATH", "PID", "SINCE"),
                   help="require a fresh healthy heartbeat from the process just started")
    p.add_argument("--engine-rates", action="store_true", help="watch WAL bytes/s, errors, anonymous RSS and restarts")
    p.add_argument("--demo-soak", action="store_true", help="require five healthy demo minutes before any funded handover")
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
            "alert on unsynchronised NTP or measured venue clock drift. Off by "
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
        default=(Path(os.environ["LIVENESS_STATE_FILE"]) if os.environ.get("LIVENESS_STATE_FILE") else None),
        help="cooldown state file (default: environment, then <repo>/data/.cache; per scope)",
    )
    return p


def main() -> int:
    args = build_arg_parser().parse_args()
    if args.check_heartbeat:
        unit, raw_path, raw_pid, raw_since = args.check_heartbeat
        path = Path(raw_path)
        try:
            row = json.loads(path.read_bytes())
            pid, since = int(raw_pid), float(raw_since)
            field = "updated_at_ms" if "signal-worker" in unit else "wall_ts_ms"
            stamp = row.get(field)
            now = time.time()
            if (type(row.get("pid")) is not int or row.get("pid") != pid or pid <= 0 or type(stamp) is not int
                    or not max(since, now - 30) * 1000 <= stamp <= (now + 5) * 1000):
                raise ValueError("heartbeat does not describe the fresh started process")
            heartbeat_alerts = evaluate_engine_heartbeat(unit, path, now=now)
            for alert in heartbeat_alerts:
                print(f"{alert.severity} {alert.key}: {alert.message}")
            return int(bool(deployment_blockers(heartbeat_alerts)))
        except (OSError, ValueError, AttributeError) as heartbeat_error:
            print(f"heartbeat readiness failed: {heartbeat_error}", file=sys.stderr)
            return 1
    scope = args.account_scope
    deadman_url = args.heartbeat_url or (os.environ.get("ONCALL_DEADMAN_URL") if scope == "host" else None)
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
    if args.demo_soak:
        if scope != "demo" or not args.require_oncall:
            print("demo soak requires --account-scope demo --require-oncall", file=sys.stderr)
            return 2
        return run_demo_soak()
    now = time.time()
    # Every scope consults the lock. The transitional keys held below —
    # worker-status, worker-spool, may-open, rolling-loss, strategy-errors,
    # and the fleet's unit and heartbeat keys — come from realm scopes; host
    # watches the independent units. A lock held past _MAX_DEPLOY_AGE_SEC still
    # pages, through the host scope's deploy-lock check.
    try:
        deploy_age = active_deploy_age(_DEPLOY_LOCK, now=now)
    except OSError:
        deploy_age = None
    deploy_maintenance = deploy_age is not None and deploy_age <= _MAX_DEPLOY_AGE_SEC
    state_file = args.state_file or (_REPO_ROOT / "data" / ".cache" / f"liveness-{scope}.json")
    counters_file = state_file.with_name(state_file.stem + ".counters.json")
    counters = load_state(counters_file)

    alerts: list[Alert] = []
    fleet_rows: list[FleetUnit] = []
    if not deploy_maintenance:
        try:
            fleet_rows = load_fleet_manifest()
            rows = scope_units(scope, fleet_rows)
            alerts.extend(evaluate_units(scope, rows))
            alerts.extend(evaluate_heartbeats(rows, now=now, max_age_sec=args.max_heartbeat_age_sec))
        except (OSError, ValueError) as error:
            alerts.append(Alert("manifest", "CRITICAL", f"cannot read the fleet manifest: {error}"))
    if scope == "host":
        alerts.extend(evaluate_disk(counters=counters, rows=fleet_rows))
        alerts.extend(evaluate_watchdog_chain())
    if args.engine_rates and not deploy_maintenance:
        alerts.extend(evaluate_engine_rates(scope_units(scope, fleet_rows), now=now, counters=counters))
    elif args.engine_rates:
        counters = {key: value for key, value in counters.items() if not key.startswith("engine-rate:")}
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
    if capture_status_files and not deploy_maintenance:
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
    if scope == "host" or args.engine_rates or (capture_status_files and not deploy_maintenance):
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
    preserved_alert_keys = (
        {key for key in state if key.startswith(_DEPLOY_TRANSITIONAL_ALERT_PREFIXES)} if deploy_maintenance else set()
    )
    if scope == "host" and not counters.get("disk:forecast_valid") and "disk-growth" in state:
        preserved_alert_keys.add("disk-growth")
    lines, next_state = select_alerts_to_send(
        alerts,
        state=state,
        now=now,
        cooldown_sec=args.cooldown_min * 60,
        preserve_keys=preserved_alert_keys,
    )
    routine_state_file = state_file.with_name(state_file.stem + ".routine.json")
    routine_state = load_state(routine_state_file)
    if not routine_state_file.exists():
        current_critical = {alert.key for alert in alerts if alert.severity == "CRITICAL"}
        routine_state = {key: sent_at for key, sent_at in state.items() if key in current_critical}
    preserved_routine_keys = (
        {key for key in routine_state if key.startswith(_DEPLOY_TRANSITIONAL_ALERT_PREFIXES)}
        if deploy_maintenance
        else set()
    )
    due_incidents, next_routine_state = select_incidents_to_fire(
        alerts,
        state=routine_state,
        now=now,
        preserve_keys=preserved_routine_keys,
    )

    for alert in alerts:
        print(f"{alert.severity} {alert.key}: {alert.message}")
    routing_failed = any(alert.key == "deadman" for alert in alerts)
    if lines:
        message = f"fleet liveness ({scope})\n" + "\n".join(lines)
        if args.telegram:
            try:
                delivered = send_telegram_message(as_block(message), channel="alerts", parse_mode="HTML")
                if not delivered:
                    raise RuntimeError("Telegram route is not configured")
            except (OSError, RuntimeError, ValueError) as error:
                print(f"CRITICAL telegram: cannot deliver alerts ({transport_error(error)})")
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
            print(f"CRITICAL incident-routine: cannot fire the on-call agent ({transport_error(error)})")
            routing_failed = True
            retained = {key: fired_at for key, fired_at in routine_state.items() if key in next_routine_state}
            save_state(routine_state_file, retained)
        else:
            save_state(routine_state_file, next_routine_state)
    elif args.require_oncall:
        save_state(routine_state_file, next_routine_state)
    has_critical = any(alert.severity == "CRITICAL" for alert in alerts)
    if not has_critical:
        if not alerts:
            if deploy_maintenance:
                print(f"ok scope={scope} sanctioned-deploy-in-progress")
            else:
                print(f"ok scope={scope} units-and-heartbeats-healthy")
        else:
            print(f"ok scope={scope} warnings-present-no-critical")
    return 1 if routing_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
