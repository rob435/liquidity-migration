#!/usr/bin/env python3
"""Account-kernel and strategy-input liveness watchdog for the deployed fleets.

Scope is ``demo`` or ``mainnet``. Within it the checker requires the account
owner, live-L2 readiness sidecar, owner-health projection, and strategy input,
plus a recent healthy canonical venue snapshot. Strategy-daemon cycle and input
checks live here because an execution owner cannot detect a hung signal
scheduler or an empty/stale signal source. The ``mainnet`` scope is disjoint
from the demo roots and runs only its own owner and producers.

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

import polars as pl

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from liquidity_migration.core._common import exact_duration_ms  # noqa: E402
from liquidity_migration.core.artifact_snapshot import read_stable_file  # noqa: E402
from liquidity_migration.account.account_kernel import AccountEventType, read_recent_account_events  # noqa: E402
from liquidity_migration.policy.account_execution_config import (  # noqa: E402
    REGISTERED_MAX_DEMO_RULE_AGE_HOURS,
    load_demo_rules,
)
from liquidity_migration.account.account_owner_health import (  # noqa: E402
    AccountOwnerMarketWarmupPending,
    require_recent_account_owner_health,
    validate_systemd_invocation_id,
)
from liquidity_migration.runtime.account_owner_readiness import latest_market_readiness  # noqa: E402
from liquidity_migration.data.storage import read_dataset_columns  # noqa: E402
from liquidity_migration.strategy.strategy_cycle_health import (  # noqa: E402
    StrategyCycleHealth,
    read_strategy_cycle_health,
)
from liquidity_migration.ops.telegram import send_telegram_message  # noqa: E402

# Severity order for message framing only.
CRITICAL = "CRITICAL"
WARNING = "WARNING"


def _plain_name(label: str) -> str:
    """Human name for a data root or systemd unit, for Telegram headlines."""
    name = label.removeprefix("liquidity-migration-")
    name = name.removesuffix(".service").removesuffix(".timer")
    name = name.removeprefix("bybit-").removesuffix("-event")
    return name.replace("-", " ") or label
DEMO_RULE_MAINTENANCE_WARNING_HOURS = 24.0

_DEMO_ACCOUNT_OWNER_UNIT = "liquidity-migration-account-execution.service"
_MAINNET_ACCOUNT_OWNER_UNIT = "liquidity-migration-account-execution-mainnet.service"
_REQUIRED_ACCOUNT_OWNER_UNITS = (_DEMO_ACCOUNT_OWNER_UNIT,)
_ACCOUNT_SCOPES = ("demo", "mainnet")
_LONG_DEMO_UNIT = "liquidity-migration-bybit-long-demo.service"
_LONG_MAINNET_UNIT = "liquidity-migration-bybit-long-mainnet.service"
_CARRY_DEMO_UNIT = "liquidity-migration-bybit-carry-demo.service"
_CARRY_MAINNET_UNIT = "liquidity-migration-bybit-carry-mainnet.service"
def _default_root(rel: str) -> str:
    """Anchor a default data root at the repo dir, not the CWD."""
    return str(_REPO_ROOT / rel)


def _sleeve_on(env_var: str, *, default: str = "off") -> bool:
    """Read a sleeve toggle, failing safe to the supplied default."""
    return os.environ.get(env_var, default).strip().lower() in {"on", "1", "true", "yes"}


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


@dataclass(frozen=True)
class CompletedCycleObservation:
    """A completion projection bound back to its durable causal cycle row."""

    health: StrategyCycleHealth
    row: dict[str, Any]


# --------------------------------------------------------------------------- #
# Pure decision logic (no I/O)
# --------------------------------------------------------------------------- #
def evaluate_cycle_liveness(
    *, latest_cycle_ts_ms: int | None, now_ms: int, max_age_minutes: float, label: str
) -> Alert | None:
    """No cycle written within the freshness window -> the daemon is down/hung."""
    if latest_cycle_ts_ms is None:
        return Alert(
            key=f"liveness:{label}",
            severity=CRITICAL,
            message=f"{label}: no cycle reports found — daemon may have never started.",
            headline=f"{_plain_name(label)}: no cycles ever recorded — the producer may never have started.",
        )
    age_min = (now_ms - latest_cycle_ts_ms) / 60_000.0
    if age_min < 0.0:
        return Alert(
            key=f"liveness:{label}",
            severity=CRITICAL,
            message=(
                f"{label}: latest cycle is {-age_min:.1f} min future-dated; scheduler liveness evidence is invalid."
            ),
            headline=f"{_plain_name(label)}: cycle timestamps are in the future — clock or data problem.",
        )
    if age_min > max_age_minutes:
        return Alert(
            key=f"liveness:{label}",
            severity=CRITICAL,
            message=(
                f"{label}: DAEMON DOWN/HUNG — last cycle {age_min:.1f} min ago "
                f"(> {max_age_minutes:.0f} min). Check positions; manual close may be needed."
            ),
            headline=f"{_plain_name(label)}: producer down or hung — last cycle {age_min:.0f} min ago.",
        )
    return None


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


def _unverified_generation_cycle_alert(*, label: str, detail: str) -> Alert:
    return Alert(
        key=f"liveness:{label}",
        severity=CRITICAL,
        message=(
            f"{label}: DAEMON DOWN/HUNG — no verified completed cycle for the "
            f"current service generation ({detail[:300]})."
        ),
        headline=f"{_plain_name(label)}: producer restarted but has not completed a checkable cycle.",
    )


# require_recent_account_owner_health() prefixes every owner-reported blocked
# status with this; every other error it raises means the evidence is missing,
# stale, or from a previous generation.
_OWNER_BLOCKED_PREFIX = "account owner is blocked:"

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


def evaluate_ws_staleness(
    *, store_max_ts_ms: int | None, now_ms: int, max_lag_hours: float, label: str
) -> Alert | None:
    if not store_max_ts_ms:
        return None
    lag_h = (now_ms - store_max_ts_ms) / 3_600_000.0
    if lag_h > max_lag_hours:
        return Alert(
            key=f"ws_stale:{label}",
            severity=WARNING,
            message=(
                f"{label}: WS kline feed stalled — newest bar {lag_h:.1f}h old "
                f"(> {max_lag_hours:.0f}h). REST fallback still covers data; watch for escalation."
            ),
            headline=(
                f"{_plain_name(label)}: live price feed stalled — newest bar {lag_h:.1f}h old "
                "(a fallback source still covers it)."
            ),
        )
    return None


def evaluate_demo_rule_age(
    *,
    verified_ts_ns: int,
    now_ns: int,
) -> Alert | None:
    """Warn before demo-rule evidence expires and the next start fails closed."""
    age_hours = (now_ns - verified_ts_ns) / 3_600_000_000_000.0
    remaining_hours = REGISTERED_MAX_DEMO_RULE_AGE_HOURS - age_hours
    if age_hours < 0.0:
        return Alert(
            key="demo_rules_age",
            severity=CRITICAL,
            message=(
                f"demo-rule evidence is {-age_hours:.1f}h future-dated; "
                "runtime quantity authority is invalid."
            ),
            headline="The trading-rules receipt is future-dated — invalid.",
        )
    if remaining_hours <= 0.0:
        return Alert(
            key="demo_rules_age",
            severity=CRITICAL,
            message=(
                f"demo-rule evidence expired {abs(remaining_hours):.1f}h ago; "
                "the next authorized runtime start will fail closed and require a full probe."
            ),
            headline="The trading-rules receipt has expired — the next restart will refuse to start.",
        )
    if remaining_hours <= DEMO_RULE_MAINTENANCE_WARNING_HOURS:
        return Alert(
            key="demo_rules_age",
            severity=WARNING,
            message=(
                f"demo-rule evidence expires in {remaining_hours:.1f}h; schedule the guarded "
                "flat-account maintenance window before expiry so the slow path is planned."
            ),
            headline=f"The trading-rules receipt expires in {remaining_hours:.0f}h — plan the refresh.",
        )
    return None


def gather_demo_rule_alerts(
    *,
    rules_path: Path,
    now_ns: int | None = None,
) -> list[Alert]:
    """Reopen the bound receipt and report corruption, future dating, or expiry."""
    try:
        snapshot = read_stable_file(
            rules_path,
            label="demo-rule receipt",
            reject_empty=True,
            require_mode=0o600,
            require_owner=True,
        )
        load_demo_rules(
            snapshot.path,
            max_age_seconds=None,
            snapshot=snapshot,
        )
        payload = json.loads(snapshot.data)
        verified_ts_ns = int(payload.get("verified_ts_ns") or 0)
        alert = evaluate_demo_rule_age(
            verified_ts_ns=verified_ts_ns,
            now_ns=time.time_ns() if now_ns is None else now_ns,
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return [
            Alert(
                key="demo_rules_invalid",
                severity=CRITICAL,
                message=(
                    "bound demo-rule evidence is unreadable or invalid: "
                    f"{type(exc).__name__}: {str(exc)[:300]}"
                ),
                headline="The trading-rules receipt cannot be read.",
            )
        ]
    return [] if alert is None else [alert]


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


def evaluate_notification_delivery(
    *,
    state_path: Path,
    now_ns: int,
    max_missed_hours: int = 2,
) -> Alert | None:
    """Alert when the demo owner's hourly digest has stopped committing.

    The notifier commits state only after every Telegram page delivers, so a
    stalled last_hour_bucket is direct evidence the digest never arrived.
    """
    try:
        payload = json.loads(state_path.read_bytes())
        last_hour_bucket = int(payload.get("last_hour_bucket") or 0)
    except FileNotFoundError:
        return None  # owner runs without Telegram, or has not sent yet
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return Alert(
            key="account_digest_stale",
            severity=WARNING,
            message=(
                "account notification state is unreadable: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
            headline="The digest bookkeeping file cannot be read.",
        )
    if last_hour_bucket <= 0:
        return None
    now_bucket = int(now_ns // 3_600_000_000_000)
    missed = now_bucket - last_hour_bucket - 1  # a 0-1 bucket gap is normal
    if missed < max_missed_hours:
        return None
    return Alert(
        key="account_digest_stale",
        severity=WARNING,
        message=(
            f"account Telegram digest has not committed for {missed} full hour(s); "
            "the notification channel may be dead (token/chat change or API outage) "
            "while the fleet looks healthy"
        ),
        headline=(
            f"The hourly digest has not arrived for {missed} hour(s) — "
            "the main Telegram line may be dead."
        ),
    )


# Pending resolved-note retries must not share alert cooldown keys.
_RESOLVED_PREFIX = "resolved:"
# Records inactive timers for the one-interval escalation debounce.
_PENDING_TIMER_PREFIX = "pending_timer:"
# Same debounce for an enabled service observed not running.
_PENDING_SERVICE_PREFIX = "pending_service:"
# Records last-sent severity so escalation bypasses the cooldown.
_SEV_PREFIX = "sev:"
_RESERVED_PREFIXES = (_RESOLVED_PREFIX, _PENDING_TIMER_PREFIX, _PENDING_SERVICE_PREFIX, _SEV_PREFIX)
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


# --------------------------------------------------------------------------- #
# I/O at the edges
# --------------------------------------------------------------------------- #
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
        except Exception:  # noqa: BLE001
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

    Missing or malformed metadata yields no grace period; the caller falls back
    to the causal-cycle check.
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
            active_age_minutes: float | None = None
            if boot_ns is not None and active_enter_us > 0:
                age_ns = boot_ns - active_enter_us * 1_000
                if age_ns >= 0:
                    active_age_minutes = age_ns / 60_000_000_000.0
            metadata[unit] = UnitRuntime(
                invocation_id=invocation_id,
                active_age_minutes=active_age_minutes,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            metadata[unit] = UnitRuntime(
                invocation_id=None,
                active_age_minutes=None,
            )
    return metadata


def _default_units_for_toggles() -> list[str]:
    units = list(_REQUIRED_ACCOUNT_OWNER_UNITS)
    if _sleeve_on("LONG_SLEEVE"):
        units.append(_LONG_DEMO_UNIT)
    if _sleeve_on("CARRY_SLEEVE"):
        units.append(_CARRY_DEMO_UNIT)
    return units


def _default_units_for_scope(account_scope: str) -> list[str]:
    """Narrow the toggle-derived unit inventory to the authorized owners.

    ``demo`` monitors the demo owner and its toggled producers; ``mainnet``
    shares no unit with it and is built from its own toggles.
    """
    if account_scope not in _ACCOUNT_SCOPES:
        raise ValueError(f"unsupported account liveness scope: {account_scope}")
    if account_scope == "mainnet":
        # An armed mainnet fleet always runs both registered producers; the
        # installed risk profile, not a toggle, decides their shares.
        return [_MAINNET_ACCOUNT_OWNER_UNIT, _CARRY_MAINNET_UNIT, _LONG_MAINNET_UNIT]
    return _default_units_for_toggles()


def _ping_heartbeat(url: str) -> None:
    try:
        urllib.request.urlopen(url, timeout=10)  # noqa: S310 - operator-supplied URL
    except Exception:  # noqa: BLE001
        pass


def _load_state(path: Path) -> dict[str, int]:
    try:
        return {str(k): int(v) for k, v in json.loads(path.read_text()).items()}
    except Exception:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001
            pass


# Columns each sleeve's checks actually inspect. Projected reads keep the
# watchdog's cost independent of how wide or old the cycle datasets grow.
LONG_CYCLE_COLUMNS = ["cycle_id", "ts_ms", "kline_store_max_ts_ms"]
CARRY_CYCLE_COLUMNS = ["cycle_id", "ts_ms", "decision_stale", "decision_error"]


def _read_cycles_columns(root: Path, cycles_dataset: str, columns: list[str]) -> pl.DataFrame:
    """Projected, lock-free read of a producer's cycle dataset.

    An observer must not take the producers' write locks (that stalls live
    cycle writes) or create lock files under roots it only observes, so it
    reads without the lock and retries once if it straddles a concurrent
    part replace (a torn read raises).
    """

    try:
        return read_dataset_columns(root, cycles_dataset, columns=columns, lock=False)
    except Exception:  # noqa: BLE001 — one concurrent-writer retry, then report
        time.sleep(0.1)
        return read_dataset_columns(root, cycles_dataset, columns=columns, lock=False)


def _observe_completed_cycle(
    *,
    root: Path,
    cycles_dataset: str,
    runtime: UnitRuntime,
    sleeve: str,
    environment: str,
    columns: list[str],
) -> tuple[CompletedCycleObservation | None, str]:
    """Read the receipt first, then bind it to its durable cycle row.

    Producers publish the dataset before the receipt, so reading in that order
    lets a concurrent update advance the receipt past an already-snapshotted
    dataset and pair rows from different generations. Receipt-first guarantees
    the referenced cycle was durable before the receipt could be read.
    """
    invocation_id = runtime.invocation_id
    if invocation_id is None:
        return None, "current systemd invocation id is unavailable"
    try:
        health = read_strategy_cycle_health(root)
    except (OSError, RuntimeError, ValueError) as exc:
        return None, f"completion receipt unavailable: {type(exc).__name__}: {exc}"
    if health.invocation_id != invocation_id:
        return None, "completion receipt belongs to a prior service generation"
    if health.sleeve != sleeve or health.environment != environment:
        return None, (
            f"completion receipt scope mismatch: {health.sleeve}/{health.environment} != {sleeve}/{environment}"
        )
    try:
        cycles = _read_cycles_columns(root, cycles_dataset, columns)
    except Exception as exc:  # noqa: BLE001 — watchdog reports unreadable evidence
        return None, f"durable cycle output is unreadable: {type(exc).__name__}: {exc}"
    if cycles is None or cycles.is_empty() or "cycle_id" not in cycles.columns or "ts_ms" not in cycles.columns:
        return None, "durable cycle output is unavailable"
    matching = cycles.filter((pl.col("cycle_id") == health.cycle_id) & (pl.col("ts_ms") == health.cycle_ts_ms))
    if matching.is_empty():
        return None, "completion receipt does not match a durable causal cycle row"
    return CompletedCycleObservation(
        health=health,
        row=matching.tail(1).to_dicts()[0],
    ), ""


def gather_long_alerts(
    *,
    long_root: Path,
    now_ms: int | None = None,
    args: argparse.Namespace,
    cycle_checks: bool = True,
    cycles_dataset: str = "long_native_demo_cycles",
    environment: str = "demo",
    unit_runtime: UnitRuntime | None = None,
) -> list[Alert]:
    """Check the LONG strategy scheduler and WS input freshness only."""
    if not long_root.exists():
        return []
    label = long_root.name
    alerts: list[Alert] = []
    if cycle_checks:
        row: dict[str, Any] | None
        liveness_ts_ms: int | None
        generation_bound = unit_runtime is not None and unit_runtime.invocation_id is not None
        if generation_bound:
            assert unit_runtime is not None
            observation, detail = _observe_completed_cycle(
                root=long_root,
                cycles_dataset=cycles_dataset,
                runtime=unit_runtime,
                sleeve="long",
                environment=environment,
                columns=LONG_CYCLE_COLUMNS,
            )
            if observation is None:
                if _within_startup_grace(
                    unit_runtime,
                    max_age_minutes=args.max_cycle_age_min,
                ):
                    return alerts
                alerts.append(_unverified_generation_cycle_alert(label=label, detail=detail))
                return alerts
            row = observation.row
            liveness_ts_ms = observation.health.completed_ts_ns // 1_000_000
        else:
            try:
                cyc = _read_cycles_columns(long_root, cycles_dataset, LONG_CYCLE_COLUMNS)
            except Exception:  # noqa: BLE001 — watchdog never crashes
                cyc = pl.DataFrame()
            latest_row = (
                cyc.sort("ts_ms").tail(1).to_dicts()[0]
                if (cyc is not None and not cyc.is_empty() and "ts_ms" in cyc.columns)
                else None
            )
            row = latest_row
            liveness_ts_ms = (
                int(latest_row["ts_ms"]) if latest_row is not None and latest_row.get("ts_ms") is not None else None
            )
        observed_now_ms = _now_ms() if now_ms is None else now_ms
        live = evaluate_cycle_liveness(
            latest_cycle_ts_ms=liveness_ts_ms,
            now_ms=observed_now_ms,
            max_age_minutes=args.max_cycle_age_min,
            label=label,
        )
        if live:
            alerts.append(live)
        if row is not None and row.get("kline_store_max_ts_ms") is not None:
            store_max = int(row.get("kline_store_max_ts_ms") or 0)
            ws = evaluate_ws_staleness(
                store_max_ts_ms=store_max,
                now_ms=observed_now_ms,
                max_lag_hours=args.max_ws_lag_hours,
                label=label,
            )
            if ws:
                alerts.append(ws)
    return alerts


def gather_carry_alerts(
    *,
    carry_root: Path,
    now_ms: int | None = None,
    args: argparse.Namespace,
    cycle_checks: bool = True,
    cycles_dataset: str = "carry_hold_demo_cycles",
    environment: str = "demo",
    unit_runtime: UnitRuntime | None = None,
) -> list[Alert]:
    """Check the CARRY strategy scheduler's cycle heartbeat and decision health.

    Carry streams its klines like LONG with REST as the gap fallback, and the
    cycle never blocks on the stream — so freshness is the cycle heartbeat
    itself. ``decision_stale`` means the daemon is holding previous targets
    and pages.
    """
    if not carry_root.exists():
        return []
    label = carry_root.name
    alerts: list[Alert] = []
    if cycle_checks:
        row: dict[str, Any] | None
        liveness_ts_ms: int | None
        generation_bound = unit_runtime is not None and unit_runtime.invocation_id is not None
        if generation_bound:
            assert unit_runtime is not None
            observation, detail = _observe_completed_cycle(
                root=carry_root,
                cycles_dataset=cycles_dataset,
                runtime=unit_runtime,
                sleeve="carry",
                environment=environment,
                columns=CARRY_CYCLE_COLUMNS,
            )
            if observation is None:
                if _within_startup_grace(
                    unit_runtime,
                    max_age_minutes=args.max_cycle_age_min,
                ):
                    return alerts
                alerts.append(_unverified_generation_cycle_alert(label=label, detail=detail))
                return alerts
            row = observation.row
            liveness_ts_ms = observation.health.completed_ts_ns // 1_000_000
        else:
            try:
                cyc = _read_cycles_columns(carry_root, cycles_dataset, CARRY_CYCLE_COLUMNS)
            except Exception:  # noqa: BLE001 — watchdog never crashes
                cyc = pl.DataFrame()
            latest_row = (
                cyc.sort("ts_ms").tail(1).to_dicts()[0]
                if (cyc is not None and not cyc.is_empty() and "ts_ms" in cyc.columns)
                else None
            )
            row = latest_row
            liveness_ts_ms = (
                int(latest_row["ts_ms"]) if latest_row is not None and latest_row.get("ts_ms") is not None else None
            )
        observed_now_ms = _now_ms() if now_ms is None else now_ms
        live = evaluate_cycle_liveness(
            latest_cycle_ts_ms=liveness_ts_ms,
            now_ms=observed_now_ms,
            max_age_minutes=args.max_cycle_age_min,
            label=label,
        )
        if live:
            alerts.append(live)
        if row is not None:
            if bool(row.get("decision_stale")):
                detail = str(row.get("decision_error") or "").strip()
                suffix = f" (decision_error: {detail[:200]})" if detail else ""
                alerts.append(
                    Alert(
                        key=f"carry_decision_stale:{label}",
                        severity=CRITICAL,
                        message=(
                            f"{label}: carry sleeve is holding PREVIOUS targets — the latest "
                            f"cycle could not produce a fresh decision{suffix}."
                        ),
                        headline=(
                            f"{_plain_name(label)}: carry is holding old targets — "
                            "it could not make a fresh decision."
                        ),
                    )
                )
            elif str(row.get("decision_error") or "").strip():
                alerts.append(
                    Alert(
                        key=f"carry_decision_error:{label}",
                        severity=WARNING,
                        message=(
                            f"{label}: carry cycle reported a decision error: "
                            f"{str(row.get('decision_error'))[:300]}"
                        ),
                        headline=f"{_plain_name(label)}: carry reported a decision error.",
                    )
                )
    return alerts


def gather_account_capture_alerts(
    *,
    capture_root: Path,
    now_ms: int | None = None,
    max_age_minutes: float,
    label: str = "",
    expected_owner_uid: int | None = None,
    startup_grace_minutes: float = 0.0,
    unit_runtime: UnitRuntime | None = None,
) -> list[Alert]:
    """Detect an owner that is active/restarting but no longer ingesting L2.

    The "capture" naming is kept only so existing alert cooldown keys stay
    stable; the checked artifact is a bounded live-market readiness sidecar.
    An owner inside its startup grace has not subscribed yet; that window is a
    restart, not an outage.
    """
    suffix = f"_{label}" if label else ""
    owner_label = f"{label} account execution" if label else "account execution"
    in_startup_grace = _within_startup_grace(unit_runtime, max_age_minutes=startup_grace_minutes)
    try:
        readiness = latest_market_readiness(
            capture_root,
            expected_owner_uid=expected_owner_uid,
        )
        oldest_required_ns = readiness.oldest_required_receive_ts_ns
        if oldest_required_ns is None:
            raise RuntimeError("required live-L2 receive timestamp is unavailable")
        newest_ms = oldest_required_ns // 1_000_000
    except (OSError, RuntimeError, ValueError) as exc:
        if in_startup_grace:
            return []
        return [
            Alert(
                key=f"account_capture_missing{suffix}",
                severity=CRITICAL,
                message=(
                    f"{owner_label} owner has no usable bounded live-L2 readiness; "
                    f"decisions cannot be executed safely ({type(exc).__name__}: {str(exc)[:160]})."
                ),
                headline=(
                    f"The {owner_label} owner has no live price feed — it cannot trade safely."
                ),
            )
        ]
    observed_now_ms = _now_ms() if now_ms is None else now_ms
    age_minutes = (observed_now_ms - newest_ms) / 60_000.0
    if age_minutes > max_age_minutes and not in_startup_grace:
        return [
            Alert(
                key=f"account_capture_stale{suffix}",
                severity=CRITICAL,
                message=(
                    f"{owner_label} live L2 is {age_minutes:.1f} min stale "
                    f"(> {max_age_minutes:g} min); owner may be hung or disconnected."
                ),
                headline=(
                    f"Live prices for the {owner_label} owner are {age_minutes:.0f} min stale — "
                    "it may be hung or disconnected."
                ),
            )
        ]
    return []


# Sub-minute proof that the owner is still reading the venue comes from
# venue_facts_at_ns in the owner-health file, checked below. The journal now
# records venue-fact changes on a ten-minute floor, so this bound only has to
# catch a journal that stopped receiving venue facts at all: two missed
# checkpoints plus one 3-minute watchdog interval, rounded up.
VENUE_SNAPSHOT_AGE_FLOOR_MINUTES = 25.0

# How old the owner's last authenticated venue observation may be. One minute,
# the same bound the owner-health projection itself gets, so an owner that is
# alive but whose venue loop is wedged pages at least as fast as it did when
# the journal carried this proof.
VENUE_FACT_AGE_FLOOR_MINUTES = 1.0

# The freshness check needs the newest venue snapshot, not a genesis replay:
# a full verified read cost ~20s CPU and ~250MB peak at 28.5k segments, every
# 3 minutes, re-verifying a chain each owner generation already verified at
# startup. Deep enough that a ten-minute checkpoint is always inside it unless
# the journal is taking more than one transaction per second, ~250x the
# observed non-snapshot rate.
ACCOUNT_HEALTH_TAIL_SEGMENTS = 1024


def gather_account_health_alerts(
    *,
    account_root: Path,
    max_age_minutes: float,
    now_ms: int | None = None,
) -> list[Alert]:
    """Require a fresh, healthy venue snapshot from the canonical account journal."""

    try:
        recent = read_recent_account_events(account_root, max_segments=ACCOUNT_HEALTH_TAIL_SEGMENTS)
    except Exception as exc:  # noqa: BLE001 - corrupt authority must page, not crash the watchdog
        return [
            Alert(
                key="account_health_unreadable",
                severity=CRITICAL,
                message=(f"canonical account health journal is unreadable: {type(exc).__name__}: {str(exc)[:200]}"),
                headline="The account journal cannot be read.",
            )
        ]
    events = () if recent is None else recent.events
    snapshots = [event for event in events if event.event_type == AccountEventType.VENUE_SNAPSHOT.value]
    if not snapshots:
        window_note = (
            ""
            if recent is None or recent.total_segments <= recent.window_segments
            else f" in the newest {recent.window_segments} of {recent.total_segments} transactions"
        )
        return [
            Alert(
                key="account_health_missing",
                severity=CRITICAL,
                message=(
                    f"canonical account journal has no venue reconciliation health snapshot{window_note}; "
                    "demo execution health is unproven."
                ),
                headline="The account journal has no recent health snapshot — health is unproven.",
            )
        ]
    latest = max(
        snapshots,
        key=lambda event: int(event.payload.get("local_receive_ts_ns") or event.wall_ts_ns),
    )
    observed_ns = int(latest.payload.get("local_receive_ts_ns") or latest.wall_ts_ns)
    observed_now_ms = _now_ms() if now_ms is None else now_ms
    age_minutes = (observed_now_ms - observed_ns / 1_000_000.0) / 60_000.0
    bound_minutes = max(max_age_minutes, VENUE_SNAPSHOT_AGE_FLOOR_MINUTES)
    if age_minutes < 0.0 or age_minutes > bound_minutes:
        return [
            Alert(
                key="account_health_stale",
                severity=CRITICAL,
                message=(
                    f"canonical account reconciliation health is {age_minutes:.1f} min old "
                    f"(allowed 0..{bound_minutes:g} min); owner health is stale or future-dated."
                ),
                headline=(
                    f"Account health is {age_minutes:.1f} min old — the owner may be stuck."
                ),
            )
        ]
    if not bool(latest.payload.get("healthy")):
        mismatches = latest.payload.get("mismatches")
        detail = "; ".join(str(item) for item in mismatches) if isinstance(mismatches, list) else "unknown mismatch"
        return [
            Alert(
                key="account_health_unhealthy",
                severity=CRITICAL,
                message=f"canonical account reconciliation is UNHEALTHY: {detail[:500]}",
                headline="The exchange and our records disagree — the account needs checking.",
            )
        ]
    return []


def gather_account_owner_health_alerts(
    *,
    account_root: Path,
    environment: str,
    max_age_minutes: float,
    now_ms: int | None = None,
    startup_grace_minutes: float = 0.0,
    unit_runtime: UnitRuntime | None = None,
) -> list[Alert]:
    """Require fresh process, capital, rule-readiness, and status evidence."""
    try:
        max_age_ns = max(1, int(max_age_minutes * 60 * 1_000_000_000))
        venue_fact_max_age_ns = max(
            max_age_ns,
            int(VENUE_FACT_AGE_FLOOR_MINUTES * 60 * 1_000_000_000),
        )
        # head_binding="allow_behind": liveness needs freshness and healthy
        # status, not the exact-head capital binding sizing consumers require —
        # during active trading the journal normally runs one transaction ahead
        # of the on-disk health projection.
        if now_ms is None:
            require_recent_account_owner_health(
                account_root,
                environment=environment,
                max_age_ns=max_age_ns,
                max_venue_fact_age_ns=venue_fact_max_age_ns,
                head_binding="allow_behind",
            )
        else:
            require_recent_account_owner_health(
                account_root,
                environment=environment,
                max_age_ns=max_age_ns,
                max_venue_fact_age_ns=venue_fact_max_age_ns,
                now_ns=now_ms * 1_000_000,
                head_binding="allow_behind",
            )
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, AccountOwnerMarketWarmupPending):
            # Nonterminal <=30s dynamic-subscription transition; the owner stays
            # blocked and producers still fail closed. A latched warmup timeout
            # raises a different error and still pages.
            return []
        if not str(exc).startswith(_OWNER_BLOCKED_PREFIX) and _within_startup_grace(
            unit_runtime, max_age_minutes=startup_grace_minutes
        ):
            # A freshly (re)started owner has not written its first health
            # projection for this generation yet. Nothing is unprotected in that
            # window — producers plan entries as blocked and keep publishing
            # exits — so paging on missing/stale/previous-generation evidence
            # turns every routine restart into a CRITICAL. An owner that is
            # alive enough to report BLOCKED still pages: that is evidence, not
            # the absence of it.
            return []
        detail = str(exc)
        if detail.startswith(_OWNER_BLOCKED_PREFIX):
            headline = (
                f"The {environment} account owner is blocked: "
                f"{detail[len(_OWNER_BLOCKED_PREFIX):].strip()[:160]}"
            )
        elif detail.startswith("account-owner venue facts are stale"):
            headline = (
                f"The {environment} account owner is running but has not read "
                "the exchange recently — its venue loop may be stuck."
            )
        else:
            headline = f"The {environment} account owner has no fresh proof it is healthy."
        return [
            Alert(
                key=f"account_owner_health:{environment}",
                severity=CRITICAL,
                message=(
                    f"{environment} account owner has no fresh healthy status/capital evidence: "
                    f"{type(exc).__name__}: {str(exc)[:400]}"
                ),
                headline=headline,
            )
        ]
    return []


def coalesce_demo_account_alerts(
    reconciliation_alerts: list[Alert],
    owner_alerts: list[Alert],
) -> list[Alert]:
    """Drop only the owner-health echo of an already-reported root mismatch."""
    reconciliation_unhealthy = any(
        alert.key == "account_health_unhealthy"
        for alert in reconciliation_alerts
    )
    if not reconciliation_unhealthy:
        return [*reconciliation_alerts, *owner_alerts]
    dependent_fragments = (
        "account reconciliation unhealthy",
        "account reconciliation mismatch",
    )
    independent_owner_alerts = [
        alert
        for alert in owner_alerts
        if not (
            alert.key == "account_owner_health:demo"
            and any(
                fragment in alert.message.lower()
                for fragment in dependent_fragments
            )
        )
    ]
    return [*reconciliation_alerts, *independent_owner_alerts]


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
    p.add_argument("--max-cycle-age-min", type=float, default=10.0, help="alert if no cycle within this many minutes")
    p.add_argument("--max-ws-lag-hours", type=float, default=6.0, help="warn if the WS kline feed is this stale")
    # Roots stay strings so the empty-string skip sentinel does not become Path('.').
    p.add_argument(
        "--long-root",
        default=os.environ.get("LONG_DEMO_DATA_ROOT") or _default_root("data/bybit-long-demo-event"),
        help="long-native sleeve root for cycle/input freshness ('' to skip)",
    )
    p.add_argument(
        "--carry-root",
        default=os.environ.get("CARRY_DEMO_DATA_ROOT") or _default_root("data/bybit-carry-demo-event"),
        help="carry-hold sleeve root for cycle/decision freshness ('' to skip)",
    )
    p.add_argument(
        "--carry-mainnet-root",
        default=os.environ.get("CARRY_MAINNET_DATA_ROOT") or _default_root("data/bybit-carry-mainnet-event"),
        help="carry-hold mainnet root for cycle/decision freshness ('' to skip)",
    )
    p.add_argument(
        "--long-mainnet-root",
        default=os.environ.get("LONG_MAINNET_DATA_ROOT") or _default_root("data/bybit-long-mainnet-event"),
        help="long-native mainnet root for cycle/input freshness ('' to skip)",
    )
    p.add_argument(
        "--account-root",
        default=os.environ.get("ACCOUNT_EXECUTION_ROOT") or _default_root("data/bybit-account-execution"),
        help="canonical demo account journal root for reconciliation health",
    )
    p.add_argument(
        "--account-capture-root",
        default=os.environ.get("ACCOUNT_CAPTURE_ROOT") or _default_root("data/bybit-account-market-capture"),
        help="demo account-owner market/readiness and decision-context root",
    )
    p.add_argument(
        "--demo-rules-file",
        default=os.environ.get("ACCOUNT_DEMO_RULES_FILE") or "",
        help="bound empirical demo-rule receipt; warns during its final 24 hours ('' to skip)",
    )
    p.add_argument(
        "--max-account-capture-age-min",
        type=float,
        default=3.0,
        help="critical alert if canonical account-owner live L2 is older than this",
    )
    p.add_argument(
        "--max-account-health-age-min",
        type=float,
        default=1.0,
        help="critical alert if owner or demo reconciliation health is older than this",
    )
    p.add_argument("--cooldown-min", type=float, default=30.0, help="re-alert interval for a persisting condition")
    p.add_argument(
        "--heartbeat-url",
        default=os.environ.get("LIVENESS_HEARTBEAT_URL") or None,
        help="ping this URL on a healthy run (external dead-man's-switch); "
        "defaults to the LIVENESS_HEARTBEAT_URL env var so the unit can wire it via EnvironmentFile",
    )
    p.add_argument(
        "--account-notification-state",
        default=(
            str(Path(os.environ["ACCOUNT_EXECUTION_ROOT"]) / "account_notifications.json")
            if os.environ.get("ACCOUNT_EXECUTION_ROOT")
            else ""
        ),
        help="demo owner's committed notification state; alerts when the hourly digest stalls ('' to skip)",
    )
    p.add_argument("--telegram", action="store_true", help="send alerts via Telegram (else stdout only)")
    p.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="cooldown state file (default: <repo>/data/.cache/liveness_watchdog.json; per-scope for mainnet)",
    )
    return p


def main() -> int:
    args = build_arg_parser().parse_args()

    mainnet = args.account_scope == "mainnet"
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
    long_root = Path(args.long_root) if str(args.long_root).strip() else None
    carry_root = Path(args.carry_root) if str(args.carry_root).strip() else None
    carry_mainnet_root = Path(args.carry_mainnet_root) if str(args.carry_mainnet_root).strip() else None
    long_mainnet_root = Path(args.long_mainnet_root) if str(args.long_mainnet_root).strip() else None
    # Repo-data anchoring for both scopes: the state file used to live under
    # the first sleeve root, which meant a retired sleeve's directory kept
    # being recreated just to hold it. The two scopes still share no alert
    # keys, so they keep separate file names.
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

    # Disabled sleeves skip cycle checks but keep every other check: turning a
    # sleeve off does not flatten it.
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
    if not mainnet and str(args.demo_rules_file).strip():
        alerts.extend(
            gather_demo_rule_alerts(
                rules_path=Path(args.demo_rules_file),
                now_ns=now_ms * 1_000_000,
            )
        )
    alerts.extend(
        gather_account_capture_alerts(
            capture_root=Path(args.account_capture_root),
            max_age_minutes=args.max_account_capture_age_min,
            # Both watchdogs page the same chat; an unlabelled owner alert cannot
            # be told apart from the demo one.
            label="mainnet" if mainnet else "",
            startup_grace_minutes=args.max_cycle_age_min,
            unit_runtime=unit_runtime.get(
                _MAINNET_ACCOUNT_OWNER_UNIT if mainnet else _DEMO_ACCOUNT_OWNER_UNIT
            ),
        )
    )
    if mainnet:
        alerts.extend(
            gather_account_owner_health_alerts(
                account_root=Path(args.account_root),
                environment="mainnet",
                max_age_minutes=args.max_account_health_age_min,
                startup_grace_minutes=args.max_cycle_age_min,
                unit_runtime=unit_runtime.get(_MAINNET_ACCOUNT_OWNER_UNIT),
            )
        )
    else:
        demo_reconciliation_alerts = gather_account_health_alerts(
            account_root=Path(args.account_root),
            max_age_minutes=args.max_account_health_age_min,
        )
        demo_owner_alerts = gather_account_owner_health_alerts(
            account_root=Path(args.account_root),
            environment="demo",
            max_age_minutes=args.max_account_health_age_min,
            startup_grace_minutes=args.max_cycle_age_min,
            unit_runtime=unit_runtime.get(_DEMO_ACCOUNT_OWNER_UNIT),
        )
        alerts.extend(
            coalesce_demo_account_alerts(
                demo_reconciliation_alerts,
                demo_owner_alerts,
            )
        )
    if not mainnet and long_root is not None:
        alerts.extend(
            gather_long_alerts(
                long_root=long_root,
                args=args,
                cycle_checks=_sleeve_on("LONG_SLEEVE"),
                environment="demo",
                unit_runtime=unit_runtime.get(_LONG_DEMO_UNIT),
            )
        )
    if not mainnet and carry_root is not None:
        alerts.extend(
            gather_carry_alerts(
                carry_root=carry_root,
                args=args,
                cycle_checks=_sleeve_on("CARRY_SLEEVE"),
                environment="demo",
                unit_runtime=unit_runtime.get(_CARRY_DEMO_UNIT),
            )
        )
    if mainnet and carry_mainnet_root is not None:
        alerts.extend(
            gather_carry_alerts(
                carry_root=carry_mainnet_root,
                args=args,
                cycles_dataset="carry_hold_mainnet_cycles",
                environment="mainnet",
                unit_runtime=unit_runtime.get(_CARRY_MAINNET_UNIT),
            )
        )
    if mainnet and long_mainnet_root is not None:
        alerts.extend(
            gather_long_alerts(
                long_root=long_mainnet_root,
                args=args,
                cycles_dataset="long_native_mainnet_cycles",
                environment="mainnet",
                unit_runtime=unit_runtime.get(_LONG_MAINNET_UNIT),
            )
        )
    disk_alert = evaluate_disk_space(path=str(_REPO_ROOT))
    if disk_alert is not None:
        alerts.append(disk_alert)
    if str(args.account_notification_state).strip():
        digest_alert = evaluate_notification_delivery(
            state_path=Path(args.account_notification_state),
            now_ns=time.time_ns(),
        )
        if digest_alert is not None:
            alerts.append(digest_alert)
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
    for alert in to_send:
        # Full technical detail goes to stdout/journald for debugging; the
        # Telegram alerts channel gets the plain headline plus the stable key
        # (the "ref") so the owner can hand the alert over verbatim.
        print(f"[{alert.severity}] liquidity-migration {ts} {alert.key}: {alert.message}")
        icon = "🚨" if alert.severity == CRITICAL else "⚠️"
        line = (
            f"{icon} {alert.severity} · {scope_name} fleet · {ts}\n"
            f"{alert.telegram_line}\n"
            f"ref {alert.key}"
        )
        if args.telegram:
            delivered = False
            try:
                delivered = send_telegram_message(line, channel="alerts")
            except Exception as exc:  # noqa: BLE001
                print(f"(telegram send failed: {exc})")
            if not delivered:
                telegram_send_failed = True
                print("(telegram send returned False — TELEGRAM_* env missing or API non-2xx; will retry next run)")
                # Revert cooldown stamp and severity marker so an undelivered
                # alert advances neither and the next run retries it.
                if alert.key in state:
                    new_state[alert.key] = state[alert.key]
                else:
                    new_state.pop(alert.key, None)
                sev_key = f"{_SEV_PREFIX}{alert.key}"
                if sev_key in state:
                    new_state[sev_key] = state[sev_key]
                else:
                    new_state.pop(sev_key, None)
    for key in resolved:
        line = f"✅ {scope_name} fleet · {ts} · cleared: {key}"
        print(line)
        retry_key = f"{_RESOLVED_PREFIX}{key}"
        if args.telegram:
            delivered = False
            try:
                delivered = send_telegram_message(line, channel="alerts")
            except Exception as exc:  # noqa: BLE001
                print(f"(telegram send failed: {exc})")
            if not delivered:
                telegram_send_failed = True
                # Retry under a separate namespace so it cannot arm alert cooldown.
                new_state[retry_key] = now_ms
                print("(telegram send returned False — resolved note will retry next run)")
                continue
        # Delivered: clear the retry marker so the note isn't re-sent forever.
        new_state.pop(retry_key, None)
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
