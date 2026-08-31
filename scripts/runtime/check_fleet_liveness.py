#!/usr/bin/env python3
"""Rust account-owner and strategy-input liveness watchdog for the deployed fleets.

Scope is ``demo`` or ``mainnet``. Within it the checker requires the Rust account
owner, its exact-identity heartbeat, and the realm's credential-free Rust signal
worker. The worker heartbeat is bound to its current systemd process and to the
exact signal, rule, risk, engine, and universe bytes it loaded. The ``mainnet``
scope is disjoint from demo and reads only its own owner and worker.

--engine-heartbeat-file (or LIVENESS_ENGINE_HEARTBEAT_FILE) adds the engine's
own heartbeat file: how long ago it was written, and whether the engine has
latched itself out of opening new positions — a state that looks perfectly
healthy from every other check here. The installed fleet binds this path from
the realm's engine environment; an explicit empty value skips the file.

Alerts are de-duplicated with a cooldown state file: a new condition alerts
immediately, a persisting one re-alerts at most every --cooldown-min, and a
cleared one sends a one-line "resolved" note. --heartbeat-url (or
LIVENESS_HEARTBEAT_URL) is pinged on every healthy run so an external
dead-man's-switch catches a box death the on-box watchdog cannot; no URL is
provisioned by default. Telegram delivery uses TELEGRAM_BOT_TOKEN with
TELEGRAM_ALERT_CHAT_ID (falling back to TELEGRAM_CHAT_ID), so alerts land on
a separate line from the trading digest: plain headline + `ref <key>` in the
chat, full technical detail on stdout/journald.

Exits 0 always (a watchdog must not crash-loop); a failure to verify degrades to
an alert.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from liquidity_migration.core._common import exact_duration_ms  # noqa: E402
from liquidity_migration.core.env_flags import validate_systemd_invocation_id  # noqa: E402
from liquidity_migration.ops.telegram import as_block, send_telegram_message  # noqa: E402

# Severity order for message framing only.
CRITICAL = "CRITICAL"
WARNING = "WARNING"


def _plain_name(label: str) -> str:
    """Human name for a data root or systemd unit, for Telegram headlines."""
    name = label.removeprefix("liquidity-migration-")
    name = name.removesuffix(".service").removesuffix(".timer")
    name = name.removeprefix("bybit-").removesuffix("-event")
    return name.replace("-", " ") or label


@dataclass(frozen=True)
class FleetUnitSpec:
    """The liveness fields read from the canonical fleet manifest."""

    unit: str
    kind: str
    realm: str
    lifecycle: str
    stop_order: int
    activation: str
    health: str
    output_artifact: str | None


def _load_fleet_manifest() -> tuple[FleetUnitSpec, ...]:
    path = _REPO_ROOT / "deploy" / "fleet_manifest.tsv"
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    parsed = list(csv.reader(lines, delimiter="|"))
    if not parsed or any(len(row) != 16 for row in parsed):
        raise ValueError("fleet manifest row shape is invalid")
    rows = tuple(
        FleetUnitSpec(
            unit=row[0],
            kind=row[1],
            realm=row[2],
            lifecycle=row[3],
            stop_order=int(row[4]),
            activation=row[5],
            health=row[8],
            output_artifact=None if row[9] == "-" else row[9],
        )
        for row in parsed
    )
    if len({row.unit for row in rows}) != len(rows):
        raise ValueError("fleet manifest contains duplicate units")
    return rows


_FLEET_UNITS = _load_fleet_manifest()


def _manifest_owner(realm: str) -> str:
    matches = [
        row.unit
        for row in _FLEET_UNITS
        if row.kind == "service"
        and row.realm == realm
        and row.lifecycle == "owner"
    ]
    if len(matches) != 1:
        raise ValueError(f"fleet manifest must name one {realm} account owner")
    return matches[0]


def _manifest_signal_worker(realm: str) -> FleetUnitSpec:
    matches = [
        row
        for row in _FLEET_UNITS
        if row.realm == realm
        and row.unit == f"liquidity-migration-signal-worker-{realm}.service"
        and row.health == "active"
        and row.output_artifact is not None
    ]
    if len(matches) != 1:
        raise ValueError(f"fleet manifest must name one {realm} signal worker")
    return matches[0]


_DEMO_ACCOUNT_OWNER_UNIT = _manifest_owner("demo")
_MAINNET_ACCOUNT_OWNER_UNIT = _manifest_owner("mainnet")
_REQUIRED_ACCOUNT_OWNER_UNITS = (_DEMO_ACCOUNT_OWNER_UNIT,)
_ACCOUNT_SCOPES = ("demo", "mainnet")
_DEMO_SIGNAL_WORKER = _manifest_signal_worker("demo")
_MAINNET_SIGNAL_WORKER = _manifest_signal_worker("mainnet")
def _manifest_signal_heartbeat(realm: str) -> str:
    artifact = _manifest_signal_worker(realm).output_artifact
    if artifact is None:
        raise ValueError(f"{realm} signal worker has no heartbeat artifact")
    return artifact


@dataclass(frozen=True)
class Alert:
    key: str  # stable identity for cooldown/dedup
    severity: str
    message: str
    #: Plain-language one-liner for the Telegram alerts channel. The full
    #: technical ``message`` always goes to stdout/journald for debugging;
    #: an empty headline falls back to it.
    headline: str = ""

    @property
    def telegram_line(self) -> str:
        return self.headline or self.message


@dataclass(frozen=True)
class UnitRuntime:
    """Current systemd generation metadata, used for bounded startup logic."""

    invocation_id: str | None
    active_age_minutes: float | None
    main_pid: int | None = None


def evaluate_signal_worker_heartbeat(
    payload: dict[str, Any],
    *,
    now_ms: int,
    max_age_seconds: float,
    expected_pid: int | None,
    expected_hashes: dict[str, str],
    label: str,
) -> Alert | None:
    """Validate the Rust worker's exact running generation and public inputs."""
    problems: list[str] = []
    if payload.get("schema_version") != 1:
        problems.append("schema_version is not 1")
    if payload.get("kind") != "liquidity_migration_signal_worker_heartbeat":
        problems.append("kind is not the directional worker contract")
    if payload.get("status") != "ready":
        problems.append("status is not ready")
    if payload.get("credential_free") is not True:
        problems.append("credential_free is not true")
    if payload.get("public_market_realm") != "mainnet" or payload.get(
        "public_bybit_host"
    ) != "api.bybit.com":
        problems.append("public Bybit source is not mainnet api.bybit.com")
    if expected_pid is None or payload.get("pid") != expected_pid:
        problems.append("heartbeat process id is not the current systemd process")
    updated_at_ms = payload.get("updated_at_ms")
    if type(updated_at_ms) is not int:
        problems.append("updated_at_ms is missing")
    else:
        age_ms = now_ms - updated_at_ms
        if age_ms < 0:
            problems.append("heartbeat is future-dated")
        elif age_ms > max_age_seconds * 1000:
            problems.append(f"heartbeat is {age_ms / 1000:.0f}s old")
    for key, expected in expected_hashes.items():
        if payload.get(key) != expected:
            problems.append(f"{key} does not match the installed input")
    for key in ("last_input_sequence", "long_output_sequence", "carry_output_sequence"):
        if type(payload.get(key)) is not int or payload[key] <= 0:
            problems.append(f"{key} has not advanced")
    if type(payload.get("last_observed_ts_ms")) is not int or payload[
        "last_observed_ts_ms"
    ] <= 0:
        problems.append("no causal public observation is recorded")
    if not problems:
        return None
    return Alert(
        key=f"signal-worker:{label}",
        severity=CRITICAL,
        message=f"{label}: " + "; ".join(problems),
        headline=f"{_plain_name(label)}: Rust signal input is stale, mismatched, or not ready.",
    )


def gather_signal_worker_alerts(
    *,
    heartbeat_path: Path,
    signal_config: Path,
    long_rule: Path,
    carry_config: Path,
    operational_config: Path,
    engine_config: Path,
    universe: Path,
    runtime: UnitRuntime | None,
    now_ms: int,
    max_age_seconds: float,
    startup_grace_minutes: float,
    label: str,
) -> list[Alert]:
    try:
        universe_payload = json.loads(universe.read_bytes())
        expected_hashes = {
            "signal_config_sha256": hashlib.sha256(signal_config.read_bytes()).hexdigest(),
            "long_rule_sha256": hashlib.sha256(long_rule.read_bytes()).hexdigest(),
            "carry_config_sha256": hashlib.sha256(carry_config.read_bytes()).hexdigest(),
            "operational_config_sha256": hashlib.sha256(
                operational_config.read_bytes()
            ).hexdigest(),
            "engine_config_sha256": hashlib.sha256(engine_config.read_bytes()).hexdigest(),
            "universe_file_sha256": hashlib.sha256(universe.read_bytes()).hexdigest(),
            "universe_artifact_sha256": str(universe_payload["artifact_sha256"]),
        }
        payload = json.loads(heartbeat_path.read_bytes())
        if not isinstance(payload, dict):
            raise ValueError("heartbeat is not an object")
        alert = evaluate_signal_worker_heartbeat(
            payload,
            now_ms=now_ms,
            max_age_seconds=max_age_seconds,
            expected_pid=runtime.main_pid if runtime is not None else None,
            expected_hashes=expected_hashes,
            label=label,
        )
    except (KeyError, OSError, TypeError, ValueError) as exc:
        alert = Alert(
            key=f"signal-worker:{label}",
            severity=CRITICAL,
            message=f"{label}: signal heartbeat or installed input is unreadable: {type(exc).__name__}: {exc}",
            headline=f"{_plain_name(label)}: Rust signal heartbeat is unreadable.",
        )
    if alert is None:
        return []
    if _within_startup_grace(runtime, max_age_minutes=startup_grace_minutes):
        return []
    return [alert]


def _within_startup_grace(
    runtime: UnitRuntime | None,
    *,
    max_age_minutes: float,
) -> bool:
    """True only for a known current service generation inside a finite grace."""
    if runtime is None or runtime.invocation_id is None:
        return False
    age = runtime.active_age_minutes
    return age is not None and 0.0 <= age <= max_age_minutes


# `systemctl is-enabled` values that mean "this unit is supposed to be running".
# `static`, `disabled`, `indirect`, and `linked` are not: a timer-driven oneshot
# is static and inactive between runs by design.
_ENABLED_UNIT_STATES = frozenset({"enabled", "enabled-runtime"})


def evaluate_unit_states(
    unit_states: dict[str, str],
    *,
    prior_not_active_timers: set[str] | None = None,
    unit_enabled_states: dict[str, str] | None = None,
    prior_not_active_services: set[str] | None = None,
) -> list[Alert]:
    """Alert on failed units, and on enabled units that are simply not running.

    ``failed`` is only one of the ways a unit stops: a service whose dependency
    failed is left ENABLED but INACTIVE, with no failure of its own to report.
    That is the shape the 2026-08-01..03 outage took, and nothing here saw it.
    Timers and enabled services both debounce one interval before escalating, so
    a deploy transition does not page. Static and disabled units stay silent —
    a timer-driven oneshot is inactive between runs by design.
    """
    prior_timers = prior_not_active_timers or set()
    prior_services = prior_not_active_services or set()
    enabled_states = unit_enabled_states or {}
    alerts: list[Alert] = []
    for unit, state in sorted(unit_states.items()):
        if unit.endswith(".timer"):
            if state != "active":
                persistent = unit in prior_timers
                alerts.append(
                    Alert(
                        key=f"unit:{unit}",
                        severity=CRITICAL if persistent else WARNING,
                        message=(
                            f"systemd timer {unit} is {state.upper()} (not active) — its scheduled "
                            f"job will never fire. Re-enable it: systemctl enable --now {unit}"
                            + (
                                ""
                                if persistent
                                else " (debouncing one interval; escalates to CRITICAL if still down next run)"
                            )
                        ),
                        headline=f"The {_plain_name(unit)} timer is off — its scheduled job will not run.",
                    )
                )
        elif state == "failed":
            alerts.append(
                Alert(
                    key=f"unit:{unit}",
                    severity=CRITICAL,
                    message=(
                        f"systemd unit {unit} is FAILED. Inspect its journal and "
                        "verify account positions; a timer-driven oneshot may retry "
                        "on its next scheduled activation."
                    ),
                    headline=f"The {_plain_name(unit)} service has failed.",
                )
            )
        elif state != "active" and enabled_states.get(unit) in _ENABLED_UNIT_STATES:
            persistent = unit in prior_services
            alerts.append(
                Alert(
                    key=f"unit:{unit}",
                    severity=CRITICAL if persistent else WARNING,
                    message=(
                        f"systemd unit {unit} is ENABLED but {state.upper()} (not active) — it is "
                        "not running and reports no failure of its own; a failed dependency or a "
                        f"manual stop leaves exactly this state. Start it: systemctl start {unit}"
                        + (
                            ""
                            if persistent
                            else " (debouncing one interval; escalates to CRITICAL if still down next run)"
                        )
                    ),
                    headline=f"The {_plain_name(unit)} service is stopped but should be running.",
                )
            )
    return alerts


def evaluate_required_account_owner_states(
    unit_states: dict[str, str],
    *,
    required_units: tuple[str, ...] = _REQUIRED_ACCOUNT_OWNER_UNITS,
) -> list[Alert]:
    """Every non-active owner state is critical, not just ``failed``.

    Cycle rows cannot prove the execution authority is alive, and a recently
    written capture can outlive a stopped owner for a few minutes.
    """
    alerts: list[Alert] = []
    for unit in required_units:
        state = unit_states.get(unit, "unknown")
        if state == "active":
            continue
        alerts.append(
            Alert(
                key=f"unit:{unit}",
                severity=CRITICAL,
                message=(
                    f"required account owner {unit} is {state.upper()} (not active); "
                    "target execution and account-owned protection are unavailable."
                ),
                headline=(
                    f"The account owner is {state} — nothing can trade or protect positions."
                ),
            )
        )
    return alerts


def evaluate_disk_space(
    *,
    path: str,
    warning_fraction: float = 0.80,
    critical_fraction: float = 0.90,
) -> Alert | None:
    """Page on the root cause before disk exhaustion cascades as owner-health noise."""
    try:
        usage = os.statvfs(path)
    except OSError as exc:
        return Alert(
            key="disk_space",
            severity=WARNING,
            message=f"disk usage for {path} is unreadable: {type(exc).__name__}: {exc}",
            headline="Disk usage cannot be read.",
        )
    total = usage.f_blocks * usage.f_frsize
    if total <= 0:
        return None
    available = usage.f_bavail * usage.f_frsize
    used_fraction = 1.0 - (available / total)
    if used_fraction < warning_fraction:
        return None
    severity = CRITICAL if used_fraction >= critical_fraction else WARNING
    return Alert(
        key="disk_space",
        severity=severity,
        message=(
            f"disk holding {path} is {used_fraction:.0%} full "
            f"({available / 1e9:.1f} GB free); journals and capture roots "
            "fail closed when it fills"
        ),
        headline=(
            f"The disk is {used_fraction:.0%} full ({available / 1e9:.1f} GB free) — "
            "trading stops if it fills."
        ),
    )


# The engine writes this small JSON file every few seconds and nothing else
# reads it, so the watchdog is the only thing that can notice it stopped.
ENGINE_MODE_LIVE = "live"
# No engine writes this any more — the shadow mode is gone and every beat says
# `live`. It stays readable because a beat outlives the run that wrote it, so a
# file left by an older engine must still be judged rather than alarmed on.
ENGINE_MODE_SHADOW = "shadow"
ENGINE_MODES = (ENGINE_MODE_LIVE, ENGINE_MODE_SHADOW)

# How far behind its own beat the engine's reading of the account may fall.
#
# The engine refreshes that reading every few seconds — measured at 3.0s and
# 5.4s on the two live engines — so this bound is a couple of hundred times the
# working cadence. It is deliberately loose: the job is to catch a view that has
# stopped arriving altogether, not to grade the venue's latency, and a tight
# bound here would page on ordinary jitter. Tightening it is an operator dial,
# not a thing to discover by being woken up.
VENUE_SNAPSHOT_AGE_FLOOR_MINUTES = 25.0


@dataclass(frozen=True)
class StrategyError:
    """One sleeve that cannot reduce its current inputs."""

    strategy: str
    error: str


@dataclass(frozen=True)
class EngineHeartbeat:
    """One checked reading of the engine's heartbeat file."""

    wall_ts_ms: int
    mode: str
    may_open: bool
    market_events: int
    orders_sent: int
    #: An engine that has not yet reached the venue has nothing to say here,
    #: so both of these can be absent from an ordinary healthy heartbeat.
    account_user_id: str | None
    pid: int | None
    engine_version: str
    venue: str
    realm: str
    strategy_errors: tuple[StrategyError, ...]
    #: When the venue reading this beat carries was taken, on the same clock as
    #: ``wall_ts_ms``. Absent until the engine has taken one at all.
    account_observed_wall_ts_ms: int | None = None

    @property
    def account_view_lag_minutes(self) -> float | None:
        """How far behind the beat the account reading is, by the engine's own
        arithmetic.

        Both stamps are written by one process off one clock, so this number
        owes nothing to the clock on the box reading it. That is the whole
        point: the age of the *beat* has to be measured against our clock and
        was the source of a long-running false alarm, while the lag *inside* the
        beat cannot be.
        """
        if self.account_observed_wall_ts_ms is None:
            return None
        return (self.wall_ts_ms - self.account_observed_wall_ts_ms) / 60_000.0

    @property
    def shadow(self) -> bool:
        return self.mode == ENGINE_MODE_SHADOW

    @property
    def mode_note(self) -> str:
        """Spelled out where the mode changes what the alert is worth."""
        return "shadow — it was sending nothing anyway" if self.shadow else "live"

    @property
    def detail(self) -> str:
        parts = [
            f"engine {self.engine_version}",
            f"venue {self.venue}/{self.realm}",
            f"mode {self.mode}",
        ]
        if self.account_user_id is not None:
            parts.append(f"account {self.account_user_id}")
        if self.pid is not None:
            # The number an operator uses to go and look at the process.
            parts.append(f"pid {self.pid}")
        parts.append(f"{self.market_events} market events seen")
        parts.append(f"{self.orders_sent} orders sent")
        return ", ".join(parts)


def _engine_heartbeat_unreadable(path: Path, reason: str) -> Alert:
    return Alert(
        key="engine_heartbeat_unreadable",
        severity=CRITICAL,
        message=(
            f"engine heartbeat at {path} cannot be read: {reason}. The engine's own state is "
            "unknown — it may be running, stopped, or refusing to open positions."
        ),
        headline="The engine's heartbeat cannot be read — we cannot tell what the engine is doing.",
    )


def parse_engine_heartbeat(data: bytes) -> EngineHeartbeat:
    """Check the raw file into a reading, or raise ValueError saying what is wrong.

    Every field is checked before it is believed. A ``may_open`` that arrived as
    the string ``"false"`` is truthy in Python, so an engine that had latched
    itself out of opening positions would read as healthy; the same trap sits
    under ``bool`` being a subclass of ``int`` for the timestamp.
    """
    if not data.strip():
        raise ValueError("the file is empty")
    try:
        payload = json.loads(data)
    except ValueError as exc:  # JSONDecodeError and bad UTF-8 both land here
        raise ValueError(f"it is not valid JSON ({str(exc)[:120]})") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"the top level is a {type(payload).__name__}, not a JSON object")

    # Every field the decision rests on is required: a heartbeat missing one of
    # them is a different contract from the one built against, and saying so
    # beats half-trusting the rest of the document. The account number, the
    # process id, and the lease path are not in here — an engine that has not
    # reached the venue yet holds no lease and knows no account number, so
    # requiring either would turn its first beats into a page. Everything else
    # the engine writes is ignored.
    wrong: list[str] = []
    for field, expected in (
        ("wall_ts_ms", int),
        ("mode", str),
        ("engine_version", str),
        ("venue", str),
        ("realm", str),
        ("may_open", bool),
        ("market_events", int),
        ("orders_sent", int),
        ("strategy_errors", list),
    ):
        value = payload.get(field)
        if expected is int and isinstance(value, bool):
            wrong.append(field)
        elif not isinstance(value, expected):
            wrong.append(field)
    if wrong:
        raise ValueError("these fields are missing or the wrong type: " + ", ".join(wrong))

    mode = str(payload["mode"])
    if mode not in ENGINE_MODES:
        # Guessing is worse than paging here: reading an unknown mode as live
        # overstates what is at risk, reading it as shadow hides a real one.
        raise ValueError(f'the mode is "{mode[:40]}", which this checker does not know')

    account = payload.get("account_user_id")
    pid = payload.get("pid")
    # Optional for the same reason the account id is: an engine that has not
    # read the venue yet writes it as null. `bool` being a subclass of `int`
    # would let `true` through as a timestamp, so it is excluded by hand.
    observed = payload.get("account_observed_wall_ts_ms")
    strategy_errors: list[StrategyError] = []
    seen_strategies: set[str] = set()
    for index, row in enumerate(payload["strategy_errors"]):
        if not isinstance(row, dict):
            raise ValueError(f"strategy_errors[{index}] is not an object")
        strategy = row.get("strategy")
        error = row.get("error")
        if not isinstance(strategy, str) or not strategy.strip():
            raise ValueError(f"strategy_errors[{index}].strategy is not a non-empty string")
        if not isinstance(error, str) or not error.strip():
            raise ValueError(f"strategy_errors[{index}].error is not a non-empty string")
        if strategy in seen_strategies:
            raise ValueError(f"strategy_errors repeats strategy {strategy!r}")
        seen_strategies.add(strategy)
        strategy_errors.append(StrategyError(strategy=strategy, error=error))
    return EngineHeartbeat(
        wall_ts_ms=int(payload["wall_ts_ms"]),
        mode=mode,
        may_open=bool(payload["may_open"]),
        market_events=int(payload["market_events"]),
        orders_sent=int(payload["orders_sent"]),
        account_user_id=account if isinstance(account, str) else None,
        pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        engine_version=str(payload["engine_version"]),
        venue=str(payload["venue"]),
        realm=str(payload["realm"]),
        strategy_errors=tuple(strategy_errors),
        account_observed_wall_ts_ms=(observed if isinstance(observed, int) and not isinstance(observed, bool) else None),
    )


def evaluate_engine_heartbeat(
    *,
    heartbeat: EngineHeartbeat,
    now_ms: int,
    max_age_seconds: float,
    max_account_view_age_minutes: float = VENUE_SNAPSHOT_AGE_FLOOR_MINUTES,
    expected_account_user_id: str = "",
    expected_venue: str = "",
    expected_realm: str = "",
    expected_engine_version: str = "",
) -> list[Alert]:
    """Report an engine that stopped writing, and one that stopped opening positions.

    Every message names the mode. A beat still carrying the retired `shadow`
    was written by an engine that reached the venue with nothing, so both
    conditions mean less on one of those than on a live beat.
    """
    alerts: list[Alert] = []
    binding_mismatches: list[str] = []
    for label, expected, observed in (
        ("account", expected_account_user_id, heartbeat.account_user_id),
        ("venue", expected_venue, heartbeat.venue),
        ("realm", expected_realm, heartbeat.realm),
        ("engine version", expected_engine_version, heartbeat.engine_version),
    ):
        if expected and observed != expected:
            binding_mismatches.append(f"{label} expected {expected!r}, observed {observed!r}")
    if binding_mismatches:
        alerts.append(
            Alert(
                key="engine_heartbeat_binding",
                severity=CRITICAL,
                message=(
                    "engine heartbeat does not match the account/runtime this watchdog is assigned to: "
                    + "; ".join(binding_mismatches)
                    + ". A fresh heartbeat from the wrong process must not make this fleet look healthy."
                ),
                headline="The funded engine heartbeat belongs to the wrong account or runtime.",
            )
        )
    age_seconds = (now_ms - heartbeat.wall_ts_ms) / 1000.0
    if age_seconds < 0.0:
        alerts.append(
            Alert(
                key="engine_heartbeat_stale",
                severity=CRITICAL,
                message=(
                    f"engine heartbeat is dated {-age_seconds:.0f}s in the future; its age cannot be "
                    f"trusted, so a dead engine would still read as fresh. Its last write said: "
                    f"{heartbeat.detail}."
                ),
                headline=(
                    "The engine's heartbeat is dated in the future — the clock is wrong, "
                    "so we cannot tell whether the engine is alive."
                ),
            )
        )
    elif age_seconds > max_age_seconds:
        alerts.append(
            Alert(
                key="engine_heartbeat_stale",
                severity=CRITICAL,
                message=(
                    f"engine heartbeat is {age_seconds:.0f}s old (allowed {max_age_seconds:.0f}s): it has "
                    f"stopped being written, so the engine is dead or stuck. Its last write said: "
                    f"{heartbeat.detail}."
                ),
                headline=(
                    f"The engine stopped writing its heartbeat {age_seconds:.0f}s ago — "
                    f"it is dead or stuck ({heartbeat.mode})."
                ),
            )
        )
    if not heartbeat.may_open:
        # Live, this is the alert that would otherwise never arrive: the engine
        # is alive, its heartbeat is healthy, every other check is green, and it
        # is quietly opening nothing. In shadow nothing was reaching the venue
        # anyway, so it is worth knowing but not worth a night page.
        alerts.append(
            Alert(
                key="engine_heartbeat_latched",
                severity=WARNING if heartbeat.shadow else CRITICAL,
                message=(
                    "engine has latched itself out of opening new positions (may_open is false): it is "
                    "alive and writing healthy heartbeats, so nothing else reports this. It will open "
                    f"nothing new. Its last write said: {heartbeat.detail}. Read the engine's log to "
                    "find out why it latched."
                ),
                headline=(
                    f"The engine is alive but has stopped opening new positions ({heartbeat.mode_note}) — "
                    "someone has to read the engine's log to find out why."
                ),
            )
        )
    if heartbeat.strategy_errors:
        failures = "; ".join(
            f"{row.strategy}: {row.error[:300]}" for row in heartbeat.strategy_errors
        )
        alerts.append(
            Alert(
                key="engine_strategy_error",
                severity=CRITICAL,
                message=(
                    "one or more strategies cannot reduce their current inputs even though the "
                    f"engine is alive: {failures}. Read the engine log for the first matching fault."
                ),
                headline="The engine is alive, but a strategy reducer is broken.",
            )
        )
    lag_minutes = heartbeat.account_view_lag_minutes
    # The floor lives here, not at the call site, so no caller can tighten the
    # bound below it by accident. The operator dial it is fed from defaults to
    # one minute — against a reading that refreshes every few seconds that
    # sounds generous, but it is only twelve times the working cadence, and one
    # slow venue reply would page. The journal check this replaced applied the
    # same floor for the same reason.
    bound_minutes = max(max_account_view_age_minutes, VENUE_SNAPSHOT_AGE_FLOOR_MINUTES)
    # Absent is not a fault: a shadow run may never ask the venue anything, and
    # a live one has not asked yet in its first moments. Paging on that would
    # make every boot an alert. It must not read as fresh either, which is why
    # nothing here fills in a default.
    if lag_minutes is not None and (lag_minutes < 0.0 or lag_minutes > bound_minutes):
        incoherent = lag_minutes < 0.0
        alerts.append(
            Alert(
                key="engine_account_view_stale",
                severity=CRITICAL,
                message=(
                    (
                        f"engine's account reading is stamped {-lag_minutes:.1f} min after the beat "
                        "carrying it; both stamps come off one clock in one process, so the freshness "
                        "arithmetic is wrong rather than the account being old."
                        if incoherent
                        else f"engine's reading of the account is {lag_minutes:.1f} min old (allowed "
                        f"0..{bound_minutes:g} min): the engine is alive and writing "
                        "beats, but it has stopped hearing what the account holds, so its idea of the "
                        "position is guesswork."
                    )
                    + f" Its last write said: {heartbeat.detail}."
                ),
                headline=(
                    "The engine's account reading is dated after the beat carrying it — "
                    "its freshness cannot be trusted."
                    if incoherent
                    else f"The engine has not heard what the account holds for {lag_minutes:.0f} min "
                    f"({heartbeat.mode_note})."
                ),
            )
        )
    return alerts


def gather_engine_heartbeat_alerts(
    *,
    heartbeat_path: Path,
    max_age_seconds: float,
    max_account_view_age_minutes: float = VENUE_SNAPSHOT_AGE_FLOOR_MINUTES,
    expected_account_user_id: str = "",
    expected_venue: str = "",
    expected_realm: str = "",
    expected_engine_version: str = "",
    now_ms: int | None = None,
) -> list[Alert]:
    """Read the engine's heartbeat file, or report that it could not be read.

    Another process writes this file, so it can be absent, empty, half-written
    by a crash, from an older engine build with fewer fields, or hold a type
    that was never expected. Each of those degrades to an alert: this script
    exits 0 by design, and a traceback here would take every other check in the
    run down with it.
    """
    try:
        data = heartbeat_path.read_bytes()
    except FileNotFoundError:
        return [_engine_heartbeat_unreadable(heartbeat_path, "the file does not exist")]
    except Exception as exc:  # noqa: BLE001 — an unreadable file must page, not crash the watchdog
        return [_engine_heartbeat_unreadable(heartbeat_path, f"{type(exc).__name__}: {str(exc)[:160]}")]
    try:
        heartbeat = parse_engine_heartbeat(data)
    except Exception as exc:  # noqa: BLE001 — same for anything the file's contents can throw
        detail = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
        return [_engine_heartbeat_unreadable(heartbeat_path, detail[:300])]
    # Read the file first, ask the clock second. The content in hand was written
    # before the read, so it cannot be newer than a reading taken now, and the
    # age cannot come out negative unless a clock genuinely disagrees. Sampling
    # first — or being handed a caller's earlier sample — pages every time the
    # engine writes its heartbeat mid-run.
    observed_now_ms = _now_ms() if now_ms is None else now_ms
    return evaluate_engine_heartbeat(
        heartbeat=heartbeat,
        now_ms=observed_now_ms,
        max_age_seconds=max_age_seconds,
        max_account_view_age_minutes=max_account_view_age_minutes,
        expected_account_user_id=expected_account_user_id,
        expected_venue=expected_venue,
        expected_realm=expected_realm,
        expected_engine_version=expected_engine_version,
    )


def _pretty_ns(value: Any) -> str:
    """Nanoseconds in units a phone reads at a glance; a dash for anything absent."""
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        return "—"
    ns = float(value)
    if ns < 1_000:
        return f"{ns:.0f}ns"
    if ns < 1_000_000:
        return f"{ns / 1_000:.1f}us"
    if ns < 1_000_000_000:
        return f"{ns / 1_000_000:.2f}ms"
    return f"{ns / 1_000_000_000:.2f}s"


def _digest_day(now_ms: int) -> int:
    """The UTC day as one comparable integer, e.g. 20260830."""
    return int(datetime.fromtimestamp(now_ms / 1000.0, UTC).strftime("%Y%m%d"))


_DIGEST_DAY_KEY = "digest_sent_day"


def build_daily_digest(payload: dict[str, Any], *, scope_name: str, ts: str) -> str:
    """One plain-text execution-health message a day, built from the raw
    heartbeat JSON rather than the parsed reading — the parsed one keeps only
    what the alert checks need, and a field an older engine does not write
    must degrade to a dash here, never to a crash or a guess.

    The engine's own sign conventions are kept (this is the debugging line,
    not the trading story): arrival is positive when it cost us, the markout
    positive when the price went our way.
    """

    def num(key: str) -> Any:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return value

    def count(key: str) -> str:
        value = num(key)
        return "—" if value is None else f"{int(value)}"

    def pretty_uptime(seconds: Any) -> str:
        if not isinstance(seconds, int | float) or isinstance(seconds, bool) or seconds < 0:
            return "up —"
        total = int(seconds)
        if total < 3_600:
            return f"up {total // 60}m"
        return f"up {total // 3_600}h {(total % 3_600) // 60:02d}m"

    equity = num("account_equity_usdt")
    positions = payload.get("positions")
    held = len(positions) if isinstance(positions, list) else None
    may_open = payload.get("may_open")
    if may_open is True:
        standing = "may open"
    elif may_open is False:
        standing = "NOT OPENING — latched or stream-down"
    else:
        standing = "may-open unknown"

    maker = num("fills_maker_share")
    arrival = num("fill_arrival_shortfall_bps")
    markout = num("fill_markout_1m_our_way_bps")
    offset = num("venue_clock_offset_ms")

    lines = [
        f"{scope_name.upper()} engine daily health · {ts}",
        " · ".join(
            [
                standing,
                pretty_uptime(num("uptime_s")),
                "equity —" if equity is None else f"equity ${equity:,.0f}",
                "positions —" if held is None else f"{held} position(s)",
                f"orders sent {count('orders_sent')}",
            ]
        ),
        " · ".join(
            [
                f"fills {count('fills')}",
                "maker —" if maker is None else f"maker {maker * 100:.0f}%",
                "slip —"
                if arrival is None
                else f"slip {abs(arrival):.1f} bp {'paid' if arrival >= 0 else 'saved'}",
                "1m markout —"
                if markout is None
                else f"1m markout {markout:+.1f} bp our way",
            ]
        ),
        " · ".join(
            [
                f"submit p50 {_pretty_ns(num('wire_p50_ns'))}",
                f"p99 {_pretty_ns(num('wire_p99_ns'))}",
                f"API round trip p50 {_pretty_ns(num('ack_p50_ns'))}",
            ]
        ),
        " · ".join(
            [
                f"disk wait p99 {_pretty_ns(num('barrier_wait_p99_ns'))}",
                f"quota hold p99 {_pretty_ns(num('quota_hold_p99_ns'))}",
            ]
        ),
        " · ".join(
            [
                f"amends priced by venue {count('amends_confirmed')}",
                f"pulled unanswered {count('amends_pulled_unconfirmed')}",
                f"stream resets {count('stream_resets')}",
            ]
        ),
        "venue clock offset —" if offset is None else f"venue clock offset {offset:+.0f} ms",
    ]
    return "\n".join(lines)


def evaluate_host_clock(*, runner: Any = subprocess.run) -> list[Alert]:
    """Page when the box's clock has stopped being disciplined.

    Every venue-stamp comparison — feed freshness, the heartbeat's own venue
    clock offset, signed request windows — quietly degrades on a drifting
    clock, and nothing else notices. `timedatectl` absent or failing is not a
    fault: a development box is not the fleet, and the check must not invent
    an alert no host state can clear.
    """
    try:
        result = runner(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:  # noqa: BLE001 — absent binary, timeout: not a clock fault
        return []
    if result.returncode != 0:
        return []
    answer = (result.stdout or "").strip()
    if answer == "yes":
        return []
    if answer != "no":
        return []
    return [
        Alert(
            key="host_clock_unsynced",
            severity=WARNING,
            message=(
                "timedatectl reports NTPSynchronized=no: the box's clock is running "
                "undisciplined. Venue-stamp comparisons, feed freshness, and signed "
                "request windows all degrade quietly while this holds."
            ),
            headline="This box's clock is not being kept in sync.",
        )
    ]


def evaluate_backup_stamp(*, stamp_path: Path, now_ms: int, max_age_hours: float) -> list[Alert]:
    """Page when the configured off-box backup has stopped landing.

    Only ever called with a configured stamp path: an owner who has not set
    backups up must not be paged about them. The stamp is written by the
    backup script after a successful copy, so its age is the age of the last
    backup that actually completed.
    """
    try:
        age_hours = (now_ms / 1000.0 - stamp_path.stat().st_mtime) / 3600.0
    except FileNotFoundError:
        return [
            Alert(
                key="backup_stale",
                severity=WARNING,
                message=(
                    f"the backup stamp at {stamp_path} does not exist, so no off-box backup "
                    "has ever completed on this host. The WAL and journals exist in one place."
                ),
                headline="Off-box backup has never completed on this host.",
            )
        ]
    if age_hours <= max_age_hours:
        return []
    return [
        Alert(
            key="backup_stale",
            severity=WARNING,
            message=(
                f"the last completed off-box backup was {age_hours:.1f}h ago "
                f"(bound {max_age_hours:.0f}h). The WAL and journals are one disk "
                "failure from gone."
            ),
            headline="Off-box backup has stopped landing.",
        )
    ]


# Pending resolved-note retries must not share alert cooldown keys.
_RESOLVED_PREFIX = "resolved:"
# Records inactive timers for the one-interval escalation debounce.
_PENDING_TIMER_PREFIX = "pending_timer:"
# Same debounce for an enabled service observed not running.
_PENDING_SERVICE_PREFIX = "pending_service:"
# Records last-sent severity so escalation bypasses the cooldown.
_SEV_PREFIX = "sev:"
_RESERVED_PREFIXES = (
    _RESOLVED_PREFIX,
    _PENDING_TIMER_PREFIX,
    _PENDING_SERVICE_PREFIX,
    _SEV_PREFIX,
    _DIGEST_DAY_KEY,
)
_SEVERITY_RANK = {WARNING: 1, CRITICAL: 2}


def select_alerts_to_send(
    *, active: list[Alert], state: dict[str, int], now_ms: int, cooldown_minutes: float
) -> tuple[list[Alert], list[str], dict[str, int]]:
    """Cooldown + resolve logic. Returns (alerts_to_send, resolved_keys, new_state).

    New condition -> send now. Persisting condition -> re-send after the cooldown,
    or immediately if its severity escalated above the last-sent severity. A key
    in state but no longer active -> resolved; a pending ``resolved:<key>`` retry
    whose key is still inactive is re-surfaced for another delivery attempt.
    """
    cooldown_ms = exact_duration_ms(minutes=cooldown_minutes)
    active_by_key = {a.key: a for a in active}
    # Reserved bookkeeping entries stay out of the cooldown namespace so they
    # neither suppress a re-fire nor look like a stale active alert to resolve.
    cooldown_state = {k: v for k, v in state.items() if not k.startswith(_RESERVED_PREFIXES)}
    to_send: list[Alert] = []
    new_state = dict(state)
    for key, alert in active_by_key.items():
        last = cooldown_state.get(key)
        rank = _SEVERITY_RANK.get(alert.severity, 0)
        last_rank = state.get(f"{_SEV_PREFIX}{key}", 0)
        escalated = rank > last_rank
        if last is None or (now_ms - last) >= cooldown_ms or escalated:
            to_send.append(alert)
            new_state[key] = now_ms
            new_state[f"{_SEV_PREFIX}{key}"] = rank
    resolved_keys: set[str] = set()
    for key in cooldown_state:
        if key not in active_by_key:
            resolved_keys.add(key)
            new_state.pop(key, None)
            new_state.pop(f"{_SEV_PREFIX}{key}", None)
    for key in state:
        if key.startswith(_RESOLVED_PREFIX):
            bare = key[len(_RESOLVED_PREFIX) :]
            if bare in active_by_key:
                new_state.pop(key, None)  # condition came back; it's an active alert now
            else:
                resolved_keys.add(bare)
    return to_send, sorted(resolved_keys), new_state


#: Telegram refuses a message over 4096 characters. Split well below it — the
#: cost of a second message is one notification, the cost of a refused one is
#: the whole run's alerts.
_TELEGRAM_CHUNK_CHARS = 3500


def format_alert_digest(
    to_send: list[Alert], resolved: list[str], *, scope_name: str, ts: str
) -> list[str]:
    """One run's alerts and clears as the messages to send — usually just one.

    A fleet going down trips six checks at once and clears all six together.
    One message per key made a routine restart twenty-eight notifications, so
    nobody read any of them. Every key still appears, with its own severity and
    its own `ref`, in one message per run.
    """
    if not to_send and not resolved:
        return []
    worst = max((_SEVERITY_RANK.get(a.severity, 0) for a in to_send), default=0)
    icon = {2: "🚨", 1: "⚠️"}.get(worst, "✅")
    header = f"{icon} {scope_name} fleet · {ts}"
    if len(to_send) > 1:
        header += f" · {len(to_send)} alerts"

    blocks: list[str] = []
    for alert in to_send:
        mark = "🚨" if alert.severity == CRITICAL else "⚠️"
        blocks.append(f"{mark} {alert.severity} {alert.telegram_line}\nref {alert.key}")
    if resolved:
        blocks.append("✅ cleared: " + ", ".join(resolved))

    messages: list[str] = []
    current = header
    for block in blocks:
        candidate = f"{current}\n\n{block}"
        if len(candidate) > _TELEGRAM_CHUNK_CHARS and current != header:
            messages.append(current)
            current = f"{header}\n\n{block}"
        else:
            current = candidate
    messages.append(current)
    return messages


# I/O at the edges
def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _unit_states(units: list[str]) -> dict[str, str]:
    states: dict[str, str] = {}
    for unit in units:
        try:
            out = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=10,
            )
            states[unit] = (out.stdout or out.stderr).strip() or "unknown"
        except Exception:
            states[unit] = "unknown"
    return states


def _unit_enabled_states(units: list[str]) -> dict[str, str]:
    """Read install state, so "not active" can be told apart from "not wanted".

    ``systemctl is-enabled`` exits nonzero for disabled/static units and still
    prints the state, so the status is ignored and the word is what counts.
    """
    states: dict[str, str] = {}
    for unit in dict.fromkeys(unit for unit in units if unit.endswith(".service")):
        try:
            out = subprocess.run(
                ["systemctl", "is-enabled", unit],
                capture_output=True,
                text=True,
                timeout=10,
            )
            reported = (out.stdout or out.stderr).strip()
            states[unit] = reported.splitlines()[0].strip() if reported else "unknown"
        except Exception:  # noqa: BLE001 — a watchdog never crashes on a probe
            states[unit] = "unknown"
    return states


def _boottime_ns() -> int | None:
    """Return Linux CLOCK_BOOTTIME, or None — never a wall-clock substitute."""
    clock_id = getattr(time, "CLOCK_BOOTTIME", None)
    if clock_id is None:
        return None
    try:
        return time.clock_gettime_ns(clock_id)
    except (OSError, ValueError):
        return None


def _unit_runtime_metadata(units: list[str]) -> dict[str, UnitRuntime]:
    """Read generation id and monotonic active age for systemd services.

    Missing or malformed metadata yields no signal-worker startup grace.
    """
    boot_ns = _boottime_ns()
    metadata: dict[str, UnitRuntime] = {}
    for unit in dict.fromkeys(unit for unit in units if unit.endswith(".service")):
        try:
            result = subprocess.run(
                [
                    "systemctl",
                    "show",
                    unit,
                    "--property=InvocationID",
                    "--property=ActiveEnterTimestampMonotonic",
                    "--property=MainPID",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            values = {
                key: value for line in result.stdout.splitlines() if "=" in line for key, value in [line.split("=", 1)]
            }
            raw_invocation_id = values.get("InvocationID") or None
            invocation_id = validate_systemd_invocation_id(raw_invocation_id) if raw_invocation_id is not None else None
            active_enter_us = int(values.get("ActiveEnterTimestampMonotonic") or "0")
            main_pid_value = int(values.get("MainPID") or "0")
            active_age_minutes: float | None = None
            if boot_ns is not None and active_enter_us > 0:
                age_ns = boot_ns - active_enter_us * 1_000
                if age_ns >= 0:
                    active_age_minutes = age_ns / 60_000_000_000.0
            metadata[unit] = UnitRuntime(
                invocation_id=invocation_id,
                active_age_minutes=active_age_minutes,
                main_pid=main_pid_value if main_pid_value > 0 else None,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            metadata[unit] = UnitRuntime(
                invocation_id=None,
                active_age_minutes=None,
            )
    return metadata


def _default_units_for_scope(account_scope: str) -> list[str]:
    """Narrow the toggle-derived unit inventory to the authorized owners.

    Each scope monitors one account owner and its credential-free signal worker.
    """
    if account_scope not in _ACCOUNT_SCOPES:
        raise ValueError(f"unsupported account liveness scope: {account_scope}")
    if account_scope == "mainnet":
        return [_MAINNET_ACCOUNT_OWNER_UNIT, _MAINNET_SIGNAL_WORKER.unit]
    return [*_REQUIRED_ACCOUNT_OWNER_UNITS, _DEMO_SIGNAL_WORKER.unit]


def _ping_heartbeat(url: str) -> None:
    try:
        urllib.request.urlopen(url, timeout=10)  # noqa: S310 - operator-supplied URL
    except Exception:
        pass


def _load_state(path: Path) -> dict[str, int]:
    try:
        return {str(k): int(v) for k, v in json.loads(path.read_text()).items()}
    except Exception:
        return {}


def _save_state(path: Path, state: dict[str, int]) -> None:
    """Atomically persist cooldown state so a mid-write SIGKILL cannot truncate it.

    A truncated file makes ``_load_state`` return ``{}``, resetting every
    cooldown; temp-file + fsync + ``os.replace`` keeps the last good state.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, sort_keys=True)
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — a write error must not crash the watchdog
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--unit",
        action="append",
        default=None,
        help="systemd unit(s) to liveness-check (repeatable). Defaults to the scope's core units.",
    )
    p.add_argument(
        "--account-scope",
        choices=_ACCOUNT_SCOPES,
        default=os.environ.get("ACCOUNT_LIVENESS_SCOPE") or "demo",
        help=(
            "require the demo owner/runtime or the mainnet owner/runtime "
            "(default: environment or demo)"
        ),
    )
    p.add_argument(
        "--max-signal-startup-min",
        type=float,
        default=30.0,
        help=(
            "how long the current Rust worker process may perform its bounded cold public-data "
            "fill before a missing ready heartbeat pages"
        ),
    )
    p.add_argument(
        "--signal-worker-heartbeat-file",
        default="",
        help="Rust directional worker heartbeat (defaults from the fleet manifest)",
    )
    p.add_argument(
        "--max-signal-heartbeat-age-sec",
        type=float,
        default=30.0,
        help="critical alert if the ready Rust worker heartbeat is older than this",
    )
    p.add_argument(
        "--signal-config-file",
        default="",
        help="machine signal config (defaults to configs/signal-worker.<scope>.json)",
    )
    p.add_argument(
        "--long-rule-file",
        default=str(_REPO_ROOT / "configs" / "long_native_v12.json"),
        help="registered LONG rule consumed by the Rust worker",
    )
    p.add_argument(
        "--carry-config-file",
        default=str(_REPO_ROOT / "configs" / "lane2_carry_hold_v7.json"),
        help="registered CARRY rule consumed by the Rust worker",
    )
    p.add_argument(
        "--operational-config-file",
        default=os.environ.get("OPERATIONAL_PROFILE_FILE") or "",
        help="installed operational profile consumed by the Rust worker",
    )
    p.add_argument(
        "--worker-engine-config-file",
        default=os.environ.get("ENGINE_CONFIG_FILE") or "",
        help="installed engine config whose native strategy slots route worker output",
    )
    p.add_argument(
        "--candidate-universe-file",
        default=os.environ.get("CANDIDATE_UNIVERSE_FILE") or "",
        help="reviewed realm-specific candidate universe consumed by the Rust worker",
    )
    p.add_argument(
        "--max-account-health-age-min",
        type=float,
        default=1.0,
        help="critical alert if owner or demo reconciliation health is older than this",
    )
    p.add_argument(
        "--engine-heartbeat-file",
        default=os.environ.get("LIVENESS_ENGINE_HEARTBEAT_FILE") or "",
        help=(
            "the engine's heartbeat file; alerts when it stops being written, cannot be read, names "
            "a strategy fault, or says the engine has latched itself out of opening positions ('' to skip). Defaults to the "
            "LIVENESS_ENGINE_HEARTBEAT_FILE env var so a unit can wire it via EnvironmentFile"
        ),
    )
    p.add_argument(
        "--max-engine-heartbeat-age-sec",
        type=float,
        default=60.0,
        help="critical alert if the engine's heartbeat is older than this (it writes every few seconds)",
    )
    p.add_argument(
        "--expected-account-user-id",
        default=os.environ.get("EXPECTED_ENGINE_ACCOUNT_USER_ID") or "",
        help="require the heartbeat to name this exact venue account id",
    )
    p.add_argument(
        "--expected-engine-venue",
        default=os.environ.get("EXPECTED_ENGINE_VENUE") or "",
        help="require the heartbeat to name this exact venue",
    )
    p.add_argument(
        "--expected-engine-realm",
        default=os.environ.get("EXPECTED_ENGINE_REALM") or "",
        help="require the heartbeat to name this exact realm",
    )
    p.add_argument(
        "--expected-engine-version",
        default=os.environ.get("EXPECTED_ENGINE_VERSION") or "",
        help="optionally require the heartbeat to name this exact engine version",
    )
    p.add_argument("--cooldown-min", type=float, default=30.0, help="re-alert interval for a persisting condition")
    p.add_argument(
        "--heartbeat-url",
        default=os.environ.get("LIVENESS_HEARTBEAT_URL") or None,
        help="ping this URL on a healthy run (external dead-man's-switch); "
        "defaults to the LIVENESS_HEARTBEAT_URL env var so the unit can wire it via EnvironmentFile",
    )
    p.add_argument("--telegram", action="store_true", help="send alerts via Telegram (else stdout only)")
    p.add_argument(
        "--no-daily-digest",
        action="store_true",
        help=(
            "skip the once-a-day engine execution-health message. On by default whenever "
            "--telegram and an engine heartbeat file are both set"
        ),
    )
    p.add_argument(
        "--host-clock-check",
        action="store_true",
        help=(
            "alert when timedatectl reports the box's clock unsynchronised. Off by default; "
            "turn it on in exactly one scope per box, or one cause pages twice"
        ),
    )
    p.add_argument(
        "--backup-stamp-file",
        default=os.environ.get("LIVENESS_BACKUP_STAMP_FILE") or "",
        help=(
            "stamp file the off-box backup script touches after a completed copy; alerts when it "
            "goes stale ('' skips — an owner who has not set backups up is not paged about them). "
            "Defaults to the LIVENESS_BACKUP_STAMP_FILE env var"
        ),
    )
    p.add_argument(
        "--max-backup-age-hours",
        type=float,
        default=26.0,
        help="alert when the last completed backup is older than this (default 26, one daily run plus slack)",
    )
    p.add_argument(
        "--state-file",
        type=Path,
        default=Path(os.environ["LIVENESS_STATE_FILE"]) if os.environ.get("LIVENESS_STATE_FILE") else None,
        help="cooldown state file (default: environment, then <repo>/data/.cache; per scope)",
    )
    return p


def main() -> int:
    args = build_arg_parser().parse_args()

    mainnet = args.account_scope == "mainnet"
    if mainnet:
        missing_binding = [
            name
            for name, value in (
                ("EXPECTED_ENGINE_ACCOUNT_USER_ID", args.expected_account_user_id),
                ("EXPECTED_ENGINE_VENUE", args.expected_engine_venue),
                ("EXPECTED_ENGINE_REALM", args.expected_engine_realm),
            )
            if not str(value).strip()
        ]
        if missing_binding:
            raise SystemExit(
                "mainnet liveness requires an explicit engine binding: "
                + ", ".join(missing_binding)
            )
        if args.expected_engine_realm != args.account_scope:
            raise SystemExit("engine binding realm must equal --account-scope")
    required_account_owner_units = (
        (_MAINNET_ACCOUNT_OWNER_UNIT,) if mainnet else _REQUIRED_ACCOUNT_OWNER_UNITS
    )

    units = list(
        dict.fromkeys(
            [
                *(args.unit or _default_units_for_scope(args.account_scope)),
                *required_account_owner_units,
            ]
        )
    )
    scope = args.account_scope
    signal_worker = _MAINNET_SIGNAL_WORKER if mainnet else _DEMO_SIGNAL_WORKER
    signal_heartbeat = Path(
        args.signal_worker_heartbeat_file or _manifest_signal_heartbeat(scope)
    )
    signal_config = Path(
        args.signal_config_file
        or _REPO_ROOT / "configs" / f"signal-worker.{scope}.json"
    )
    required_worker_inputs = {
        "operational config": args.operational_config_file,
        "engine config": args.worker_engine_config_file,
        "candidate universe": args.candidate_universe_file,
    }
    # Missing bindings become one actionable heartbeat alert below. A watchdog
    # never crashes and silently stops paging because its own config is bad.
    worker_input_paths = {
        label: Path(value) if str(value).strip() else Path(f"/missing/{label.replace(' ', '-')}")
        for label, value in required_worker_inputs.items()
    }
    # Repo-data anchoring for both scopes, so no sleeve root is recreated just
    # to hold this file. The two scopes share no alert keys, so they keep
    # separate file names.
    _state_root = _REPO_ROOT / "data"
    _state_name = "liveness_watchdog_mainnet.json" if mainnet else "liveness_watchdog.json"
    state_file = args.state_file or (_state_root / ".cache" / _state_name)
    now_ms = _now_ms()

    # Timer escalation depends on the prior run's inactive set.
    state = _load_state(state_file)
    prior_not_active_timers = {k[len(_PENDING_TIMER_PREFIX) :] for k in state if k.startswith(_PENDING_TIMER_PREFIX)}
    prior_not_active_services = {
        k[len(_PENDING_SERVICE_PREFIX) :] for k in state if k.startswith(_PENDING_SERVICE_PREFIX)
    }

    unit_states = _unit_states(units)
    runtime_units = list(dict.fromkeys([*units, *_default_units_for_scope(args.account_scope)]))
    unit_runtime = _unit_runtime_metadata(runtime_units)
    not_active_timers = {u for u, s in unit_states.items() if u.endswith(".timer") and s != "active"}
    owner_states = {unit: unit_states.get(unit, "unknown") for unit in required_account_owner_units}
    non_owner_states = {unit: state for unit, state in unit_states.items() if unit not in required_account_owner_units}
    # Install state separates "stopped and should not be" from a static oneshot
    # that is idle between timer activations.
    unit_enabled_states = _unit_enabled_states(list(non_owner_states))
    not_active_services = {
        u
        for u, s in non_owner_states.items()
        if u.endswith(".service") and s not in ("active", "failed") and unit_enabled_states.get(u) in _ENABLED_UNIT_STATES
    }
    alerts = evaluate_unit_states(
        non_owner_states,
        prior_not_active_timers=prior_not_active_timers,
        unit_enabled_states=unit_enabled_states,
        prior_not_active_services=prior_not_active_services,
    )
    alerts.extend(
        evaluate_required_account_owner_states(
            owner_states,
            required_units=required_account_owner_units,
        )
    )
    alerts.extend(
        gather_signal_worker_alerts(
            heartbeat_path=signal_heartbeat,
            signal_config=signal_config,
            long_rule=Path(args.long_rule_file),
            carry_config=Path(args.carry_config_file),
            operational_config=worker_input_paths["operational config"],
            engine_config=worker_input_paths["engine config"],
            universe=worker_input_paths["candidate universe"],
            runtime=unit_runtime.get(signal_worker.unit),
            now_ms=now_ms,
            max_age_seconds=args.max_signal_heartbeat_age_sec,
            startup_grace_minutes=args.max_signal_startup_min,
            label=signal_worker.unit,
        )
    )
    disk_alert = evaluate_disk_space(path=str(_REPO_ROOT))
    if disk_alert is not None:
        alerts.append(disk_alert)
    if args.host_clock_check:
        alerts.extend(evaluate_host_clock())
    if str(args.backup_stamp_file).strip():
        alerts.extend(
            evaluate_backup_stamp(
                stamp_path=Path(args.backup_stamp_file),
                now_ms=now_ms,
                max_age_hours=args.max_backup_age_hours,
            )
        )
    if str(args.engine_heartbeat_file).strip():
        alerts.extend(
            gather_engine_heartbeat_alerts(
                heartbeat_path=Path(args.engine_heartbeat_file),
                max_age_seconds=args.max_engine_heartbeat_age_sec,
                # The operator dial that bounds the engine's own account
                # reading: one knob, pointed at the reader that has a writer.
                max_account_view_age_minutes=args.max_account_health_age_min,
                expected_account_user_id=args.expected_account_user_id,
                expected_venue=args.expected_engine_venue,
                expected_realm=args.expected_engine_realm,
                expected_engine_version=args.expected_engine_version,
                # No now_ms: the engine rewrites this file every few seconds, and
                # by the time this run reaches it the clock sampled at the top of
                # main() is a second or two behind. Handing that stale reading in
                # dates any heartbeat written since as being in the future. The
                # gather reads the file and then asks the clock, in that order,
                # so the age cannot go negative unless a clock really is wrong.
            )
        )
    to_send, resolved, new_state = select_alerts_to_send(
        active=alerts, state=state, now_ms=now_ms, cooldown_minutes=args.cooldown_min
    )
    # Escalate a timer, or an enabled-but-stopped service, only after two
    # consecutive inactive observations.
    new_state = {
        k: v
        for k, v in new_state.items()
        if not k.startswith((_PENDING_TIMER_PREFIX, _PENDING_SERVICE_PREFIX))
    }
    for unit in not_active_timers:
        new_state[f"{_PENDING_TIMER_PREFIX}{unit}"] = now_ms
    for unit in not_active_services:
        new_state[f"{_PENDING_SERVICE_PREFIX}{unit}"] = now_ms

    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    scope_name = "mainnet" if mainnet else "demo"
    telegram_send_failed = False
    # Full technical detail goes to stdout/journald for debugging; the Telegram
    # alerts channel gets the plain headline plus the stable key (the "ref") so
    # the owner can hand the alert over verbatim.
    for alert in to_send:
        print(f"[{alert.severity}] liquidity-migration {ts} {alert.key}: {alert.message}")
    for key in resolved:
        print(f"✅ {scope_name} fleet · {ts} · cleared: {key}")

    if args.telegram:
        for message in format_alert_digest(to_send, resolved, scope_name=scope_name, ts=ts):
            delivered = False
            try:
                delivered = send_telegram_message(
                    as_block(message), channel="alerts", parse_mode="HTML"
                )
            except Exception as exc:
                print(f"(telegram send failed: {exc})")
            if not delivered:
                telegram_send_failed = True
                print("(telegram send returned False — TELEGRAM_* env missing or API non-2xx; will retry next run)")
                break

    if telegram_send_failed:
        # Nothing in this run reached the phone, so nothing in it may advance:
        # revert every cooldown stamp and severity marker the run set, and mark
        # every cleared key for another attempt.
        for alert in to_send:
            for key in (alert.key, f"{_SEV_PREFIX}{alert.key}"):
                if key in state:
                    new_state[key] = state[key]
                else:
                    new_state.pop(key, None)
        for key in resolved:
            # A separate namespace, so a retry cannot arm an alert cooldown.
            new_state[f"{_RESOLVED_PREFIX}{key}"] = now_ms
    else:
        for key in resolved:
            new_state.pop(f"{_RESOLVED_PREFIX}{key}", None)
    # Once a day, on the same line the alerts use: the engine's execution
    # health, from its own heartbeat. Sent only after the run's alerts, only
    # when the channel is working, and the day advances only on a delivered
    # message — an undelivered digest retries on the next run, like an alert.
    # An unreadable heartbeat sends nothing and advances nothing: the
    # unreadable-heartbeat alert above is already paging, and a digest of
    # dashes would add noise to it.
    digest_wanted = (
        args.telegram
        and not args.no_daily_digest
        and not telegram_send_failed
        and str(args.engine_heartbeat_file).strip()
        and _digest_day(now_ms) > int(state.get(_DIGEST_DAY_KEY, 0))
    )
    if digest_wanted:
        try:
            payload = json.loads(Path(args.engine_heartbeat_file).read_bytes())
        except Exception:  # noqa: BLE001 — absent/torn file: the alert path owns saying so
            payload = None
        if isinstance(payload, dict):
            digest = build_daily_digest(payload, scope_name=scope_name, ts=ts)
            print(digest)
            delivered = False
            try:
                delivered = send_telegram_message(
                    as_block(digest), channel="alerts", parse_mode="HTML"
                )
            except Exception as exc:  # noqa: BLE001 — a digest must never take the watchdog down
                print(f"(telegram digest send failed: {exc})")
            if delivered:
                new_state[_DIGEST_DAY_KEY] = _digest_day(now_ms)
            else:
                telegram_send_failed = True
                print("(telegram digest send returned False; will retry next run)")

    # Saved after the sends so an undelivered alert's cooldown stays unset.
    _save_state(state_file, new_state)

    # Ping the external dead-man's-switch only with no CRITICAL alerts and every
    # Telegram send delivered — a dead notification channel must page externally
    # instead of looking like "all quiet".
    if (
        args.heartbeat_url
        and not any(a.severity == CRITICAL for a in alerts)
        and not telegram_send_failed
    ):
        _ping_heartbeat(args.heartbeat_url)

    if not to_send and not resolved:
        print(f"ok ({ts}): {len(alerts)} active alert(s) within cooldown; monitored {len(units)} unit(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
