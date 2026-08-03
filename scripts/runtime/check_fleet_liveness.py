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
provisioned by default. Telegram delivery uses TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID.

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
from liquidity_migration.account.account_kernel import AccountEventType, read_account_journal  # noqa: E402
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
from liquidity_migration.strategy.continuous_hedge_manager import (  # noqa: E402
    HEDGE_MODEL_PRIOR_KIND,
    load_hedge_model_prior,
    require_usable_hedge_model_prior,
)
from liquidity_migration.data.storage import read_dataset  # noqa: E402
from liquidity_migration.strategy.strategy_cycle_health import (  # noqa: E402
    StrategyCycleHealth,
    read_strategy_cycle_health,
)
from liquidity_migration.ops.telegram import send_telegram_message  # noqa: E402

# Severity order for message framing only.
CRITICAL = "CRITICAL"
WARNING = "WARNING"
DEMO_RULE_MAINTENANCE_WARNING_HOURS = 24.0

_DEMO_ACCOUNT_OWNER_UNIT = "liquidity-migration-account-execution.service"
_MAINNET_ACCOUNT_OWNER_UNIT = "liquidity-migration-account-execution-mainnet.service"
_REQUIRED_ACCOUNT_OWNER_UNITS = (_DEMO_ACCOUNT_OWNER_UNIT,)
_ACCOUNT_SCOPES = ("demo", "mainnet")
_LONG_DEMO_UNIT = "liquidity-migration-bybit-long-demo.service"
_LONG_MAINNET_UNIT = "liquidity-migration-bybit-long-mainnet.service"
_CONTINUOUS_DEMO_UNIT = "liquidity-migration-bybit-continuous-demo.service"
_CARRY_DEMO_UNIT = "liquidity-migration-bybit-carry-demo.service"
_CARRY_MAINNET_UNIT = "liquidity-migration-bybit-carry-mainnet.service"
def _default_root(rel: str) -> str:
    """Anchor a default data root at the repo dir, not the CWD."""
    return str(_REPO_ROOT / rel)


def _sleeve_on(env_var: str, *, default: str = "off") -> bool:
    """Read a sleeve toggle, failing safe to the supplied default."""
    return os.environ.get(env_var, default).strip().lower() in {"on", "1", "true", "yes"}


def _continuous_rmom_refresh_on() -> bool:
    """Match the deploy predicate: the CONTINUOUS sleeve needs RMOM refresh."""
    return _sleeve_on("CONTINUOUS_SLEEVE", default="off")


@dataclass(frozen=True)
class Alert:
    key: str  # stable identity for cooldown/dedup
    severity: str
    message: str


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
        )
    age_min = (now_ms - latest_cycle_ts_ms) / 60_000.0
    if age_min < 0.0:
        return Alert(
            key=f"liveness:{label}",
            severity=CRITICAL,
            message=(
                f"{label}: latest cycle is {-age_min:.1f} min future-dated; scheduler liveness evidence is invalid."
            ),
        )
    if age_min > max_age_minutes:
        return Alert(
            key=f"liveness:{label}",
            severity=CRITICAL,
            message=(
                f"{label}: DAEMON DOWN/HUNG — last cycle {age_min:.1f} min ago "
                f"(> {max_age_minutes:.0f} min). Check positions; manual close may be needed."
            ),
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
    )


def evaluate_unit_states(
    unit_states: dict[str, str], *, prior_not_active_timers: set[str] | None = None
) -> list[Alert]:
    """Alert on failed services and debounce inactive timers for one interval.

    A disabled timer stays silent otherwise; the one-interval debounce avoids
    escalating a transient deploy transition.
    """
    prior = prior_not_active_timers or set()
    alerts: list[Alert] = []
    for unit, state in sorted(unit_states.items()):
        if unit.endswith(".timer"):
            if state != "active":
                persistent = unit in prior
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
            )
        )
    return alerts


def gather_hedge_model_prior_alerts(*, model_prior_path: Path, now_ms: int, book_nonflat: bool = False) -> list[Alert]:
    """Check prior integrity, not wall-clock freshness (the prior is immutable)."""
    try:
        require_usable_hedge_model_prior(
            load_hedge_model_prior(model_prior_path),
            as_of_date=datetime.fromtimestamp(now_ms / 1000, tz=UTC).date(),
        )
    except (OSError, ValueError) as exc:
        return [
            Alert(
                key="hedge_model_prior_invalid",
                severity=CRITICAL if book_nonflat else WARNING,
                message=(
                    f"continuous hedge {HEDGE_MODEL_PRIOR_KIND} is unusable: {str(exc)[:300]}. "
                    "The armed hedge will fail closed until the commit-owned artifact is repaired."
                ),
            )
        ]
    return []


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
        )
    return None


def evaluate_rmom_staleness(*, max_rmom_day_ts: int, now_ms: int, max_stale_days: float, label: str) -> Alert | None:
    """Alert when the continuous rmom gate is missing or older than ``max_stale_days``.

    Without today's row in residual_momentum.parquet the decile join's
    ``is_not_null`` filter empties the whole cross-section — a silent zero-signal
    blackout that reads as a quiet market (live_d9_symbols=0, rmom_present=True).
    """
    if not max_rmom_day_ts:
        return Alert(
            key=f"rmom:{label}",
            severity=CRITICAL,
            message=(
                f"{label}: rmom signal gate EMPTY (max_rmom_day_ts=0) — the live decile drops every "
                f"symbol (silent zero-signal blackout). Rebuild residual_momentum.parquet "
                f"(precompute_residual_momentum.py) and check the continuous-rmom-refresh timer."
            ),
        )
    stale_days = (now_ms - max_rmom_day_ts) / 86_400_000.0
    if stale_days > max_stale_days:
        return Alert(
            key=f"rmom:{label}",
            severity=CRITICAL,
            message=(
                f"{label}: rmom signal gate STALE — newest residual_momentum day {stale_days:.1f}d old "
                f"(> {max_stale_days:.0f}d). The live decile silently empties; the continuous-rmom-refresh "
                f"timer likely failed — rebuild residual_momentum.parquet."
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
        )
    if remaining_hours <= 0.0:
        return Alert(
            key="demo_rules_age",
            severity=CRITICAL,
            message=(
                f"demo-rule evidence expired {abs(remaining_hours):.1f}h ago; "
                "the next authorized runtime start will fail closed and require a full probe."
            ),
        )
    if remaining_hours <= DEMO_RULE_MAINTENANCE_WARNING_HOURS:
        return Alert(
            key="demo_rules_age",
            severity=WARNING,
            message=(
                f"demo-rule evidence expires in {remaining_hours:.1f}h; schedule the guarded "
                "flat-account maintenance window before expiry so the slow path is planned."
            ),
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
    )


def evaluate_oneshot_runtime(
    *,
    unit: str,
    max_seconds: float,
    start_monotonic_usec: int | None,
    exit_monotonic_usec: int | None,
) -> Alert | None:
    """Warn when a periodic oneshot's completed run consumed too much wall time."""
    if not start_monotonic_usec or not exit_monotonic_usec:
        return None
    if exit_monotonic_usec <= start_monotonic_usec:
        return None  # currently running, or stale ordering
    duration_seconds = (exit_monotonic_usec - start_monotonic_usec) / 1e6
    if duration_seconds <= max_seconds:
        return None
    return Alert(
        key=f"oneshot_runtime:{unit}",
        severity=WARNING,
        message=(
            f"{unit} last run took {duration_seconds:.0f}s "
            f"(bound {max_seconds:.0f}s); on a saturated host it can overrun "
            "its own cadence and starve the fleet"
        ),
    )


def _oneshot_run_window_usec(unit: str) -> tuple[int | None, int | None]:
    try:
        proc = subprocess.run(
            [
                "systemctl",
                "show",
                unit,
                "-p",
                "ExecMainStartTimestampMonotonic",
                "-p",
                "ExecMainExitTimestampMonotonic",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    values: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and value.strip().isdigit():
            values[key.strip()] = int(value.strip())
    return (
        values.get("ExecMainStartTimestampMonotonic"),
        values.get("ExecMainExitTimestampMonotonic"),
    )


# Pending resolved-note retries must not share alert cooldown keys.
_RESOLVED_PREFIX = "resolved:"
# Records inactive timers for the one-interval escalation debounce.
_PENDING_TIMER_PREFIX = "pending_timer:"
# Records last-sent severity so escalation bypasses the cooldown.
_SEV_PREFIX = "sev:"
_RESERVED_PREFIXES = (_RESOLVED_PREFIX, _PENDING_TIMER_PREFIX, _SEV_PREFIX)
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
    if _continuous_rmom_refresh_on():
        units.extend(
            [
                # Same predicate the deploy uses, so a timer disabled in
                # sleeves.env is never paged on.
                "liquidity-migration-continuous-rmom-refresh.service",
                "liquidity-migration-continuous-rmom-refresh.timer",
            ]
        )
    if _sleeve_on("CONTINUOUS_SLEEVE", default="off"):
        units.extend(
            [
                _CONTINUOUS_DEMO_UNIT,
                "liquidity-migration-continuous-hedge.timer",
                # The service too: a failed oneshot leaves the timer
                # active/waiting, so only the service reports "failed".
                "liquidity-migration-continuous-hedge.service",
            ]
        )
    elif _sleeve_on("CONTINUOUS_HEDGE_TIMER", default="off"):
        units.extend(
            [
                "liquidity-migration-continuous-hedge.timer",
                "liquidity-migration-continuous-hedge.service",
            ]
        )
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
        mainnet_units = [_MAINNET_ACCOUNT_OWNER_UNIT]
        if _sleeve_on("CARRY_MAINNET_SLEEVE"):
            mainnet_units.append(_CARRY_MAINNET_UNIT)
        if _sleeve_on("LONG_MAINNET_SLEEVE"):
            mainnet_units.append(_LONG_MAINNET_UNIT)
        return mainnet_units
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


def _observe_completed_cycle(
    *,
    root: Path,
    cycles_dataset: str,
    runtime: UnitRuntime,
    sleeve: str,
    environment: str,
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
        cycles = read_dataset(root, cycles_dataset)
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


def gather_continuous_alerts(
    *,
    continuous_root: Path,
    now_ms: int | None = None,
    args: argparse.Namespace,
    cycles_dataset: str = "continuous_fade_demo_cycles",
    cycle_checks: bool = True,
    environment: str = "demo",
    unit_runtime: UnitRuntime | None = None,
) -> list[Alert]:
    """Check the continuous strategy scheduler and its causal signal inputs.

    Execution, positions, reconciliation, and protection belong to the account
    owner and are checked elsewhere.
    """
    if not continuous_root.exists():
        return []
    label = continuous_root.name
    alerts: list[Alert] = []
    if cycle_checks:
        observation: CompletedCycleObservation | None = None
        row: dict[str, Any] | None
        liveness_ts_ms: int | None
        generation_bound = unit_runtime is not None and unit_runtime.invocation_id is not None
        if generation_bound:
            assert unit_runtime is not None
            observation, detail = _observe_completed_cycle(
                root=continuous_root,
                cycles_dataset=cycles_dataset,
                runtime=unit_runtime,
                sleeve="continuous",
                environment=environment,
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
                cyc = read_dataset(continuous_root, cycles_dataset)
            except Exception:  # noqa: BLE001 — watchdog never crashes
                cyc = pl.DataFrame()
            if cyc is not None and not cyc.is_empty() and "mode" in cyc.columns:
                cyc = cyc.filter(pl.col("mode").fill_null("") != "ledger_reset_boundary")
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
            rmom_alert = evaluate_rmom_staleness(
                max_rmom_day_ts=int(row.get("max_rmom_day_ts") or 0),
                now_ms=observed_now_ms,
                max_stale_days=args.max_rmom_stale_days,
                label=label,
            )
            if rmom_alert:
                alerts.append(rmom_alert)
            # An empty universe or kline input masquerades as a quiet market.
            universe_n = row.get("universe_symbols")
            if universe_n is not None and int(universe_n) == 0:
                alerts.append(
                    Alert(
                        key=f"continuous_universe_empty:{label}",
                        severity=WARNING,
                        message=f"{label}: continuous sleeve resolved an EMPTY universe (discover/ingestion failure?); zero candidates -- looks like a quiet market.",
                    )
                )
            kline_rows = (
                observation.health.ws_kline_store_rows
                if observation is not None and observation.health.ws_kline_store_rows is not None
                else row.get("kline_store_rows")
            )
            if kline_rows is not None and int(kline_rows) == 0:
                detail = (
                    "current WS kline store is EMPTY"
                    if observation is not None
                    else "latest cycle used zero WS kline rows"
                )
                alerts.append(
                    Alert(
                        key=f"continuous_kline_store_empty:{label}",
                        severity=WARNING,
                        message=(
                            f"{label}: continuous sleeve {detail} (rows=0); "
                            "public REST fallback may be carrying the cycle."
                        ),
                    )
                )
    return alerts


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
                cyc = read_dataset(long_root, cycles_dataset)
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

    Carry has no WS kline plane — market inputs arrive by bounded public REST
    inside the cycle — so freshness is the cycle heartbeat itself.
    ``decision_stale`` means the daemon is holding previous targets and pages.
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
                cyc = read_dataset(carry_root, cycles_dataset)
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
) -> list[Alert]:
    """Detect an owner that is active/restarting but no longer ingesting L2.

    The "capture" naming is kept only so existing alert cooldown keys stay
    stable; the checked artifact is a bounded live-market readiness sidecar.
    """
    suffix = f"_{label}" if label else ""
    owner_label = f"{label} account execution" if label else "account execution"
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
        return [
            Alert(
                key=f"account_capture_missing{suffix}",
                severity=CRITICAL,
                message=(
                    f"{owner_label} owner has no usable bounded live-L2 readiness; "
                    f"decisions cannot be executed safely ({type(exc).__name__}: {str(exc)[:160]})."
                ),
            )
        ]
    observed_now_ms = _now_ms() if now_ms is None else now_ms
    age_minutes = (observed_now_ms - newest_ms) / 60_000.0
    if age_minutes > max_age_minutes:
        return [
            Alert(
                key=f"account_capture_stale{suffix}",
                severity=CRITICAL,
                message=(
                    f"{owner_label} live L2 is {age_minutes:.1f} min stale "
                    f"(> {max_age_minutes:g} min); owner may be hung or disconnected."
                ),
            )
        ]
    return []


# The reconciler journals a venue snapshot on semantic change or its 30s
# heartbeat, and one busy owner iteration can defer the next heartbeat past a
# 1-minute bound while healthy. Four heartbeat intervals of headroom; a wedged
# owner still pages via gather_account_owner_health_alerts.
VENUE_SNAPSHOT_AGE_FLOOR_MINUTES = 2.0


def gather_account_health_alerts(
    *,
    account_root: Path,
    max_age_minutes: float,
    now_ms: int | None = None,
) -> list[Alert]:
    """Require a fresh, healthy venue snapshot from the canonical account journal."""

    try:
        events = read_account_journal(account_root, verify=True)
    except Exception as exc:  # noqa: BLE001 - corrupt authority must page, not crash the watchdog
        return [
            Alert(
                key="account_health_unreadable",
                severity=CRITICAL,
                message=(f"canonical account health journal is unreadable: {type(exc).__name__}: {str(exc)[:200]}"),
            )
        ]
    snapshots = [event for event in events if event.event_type == AccountEventType.VENUE_SNAPSHOT.value]
    if not snapshots:
        return [
            Alert(
                key="account_health_missing",
                severity=CRITICAL,
                message=(
                    "canonical account journal has no venue reconciliation health snapshot; "
                    "demo execution health is unproven."
                ),
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
        # head_binding="allow_behind": liveness needs freshness and healthy
        # status, not the exact-head capital binding sizing consumers require —
        # during active trading the journal normally runs one transaction ahead
        # of the on-disk health projection.
        if now_ms is None:
            require_recent_account_owner_health(
                account_root,
                environment=environment,
                max_age_ns=max_age_ns,
                head_binding="allow_behind",
            )
        else:
            require_recent_account_owner_health(
                account_root,
                environment=environment,
                max_age_ns=max_age_ns,
                now_ns=now_ms * 1_000_000,
                head_binding="allow_behind",
            )
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, AccountOwnerMarketWarmupPending):
            # Nonterminal <=30s dynamic-subscription transition; the owner stays
            # blocked and producers still fail closed. A latched warmup timeout
            # raises a different error and still pages.
            return []
        return [
            Alert(
                key=f"account_owner_health:{environment}",
                severity=CRITICAL,
                message=(
                    f"{environment} account owner has no fresh healthy status/capital evidence: "
                    f"{type(exc).__name__}: {str(exc)[:400]}"
                ),
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
        "--continuous-root",
        default=os.environ.get("CONTINUOUS_DEMO_DATA_ROOT") or _default_root("data/bybit-continuous-demo-event"),
        help="continuous-fade sleeve root for cycle/input freshness ('' to skip)",
    )
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
    p.add_argument(
        "--hedge-model-prior",
        default=_default_root("deploy/hedge_warmstart/bybit_warmstart.csv"),
        help="commit-owned immutable hedge model prior; validated while the hedge timer is on ('' to skip)",
    )
    p.add_argument(
        "--max-rmom-stale-days",
        type=float,
        default=2.0,
        help="alert if the continuous rmom signal gate's newest day is older than this (silent-blackout guard)",
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
    p.add_argument(
        "--max-oneshot-run-seconds",
        type=float,
        default=180.0,
        help="warn when a monitored periodic oneshot's completed run exceeds this wall time",
    )
    p.add_argument("--telegram", action="store_true", help="send alerts via Telegram (else stdout only)")
    p.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="cooldown state file (default: <continuous-root>/.cache/liveness_watchdog.json; per-scope for mainnet)",
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
    continuous_root = Path(args.continuous_root) if str(args.continuous_root).strip() else None
    long_root = Path(args.long_root) if str(args.long_root).strip() else None
    carry_root = Path(args.carry_root) if str(args.carry_root).strip() else None
    carry_mainnet_root = Path(args.carry_mainnet_root) if str(args.carry_mainnet_root).strip() else None
    long_mainnet_root = Path(args.long_mainnet_root) if str(args.long_mainnet_root).strip() else None
    # Keep cooldown state stable even when both sleeve roots are skipped, and
    # off the demo watchdog's file: the two scopes share no alert keys.
    _state_root = (_REPO_ROOT / "data") if mainnet else (continuous_root or long_root or (_REPO_ROOT / "data"))
    _state_name = "liveness_watchdog_mainnet.json" if mainnet else "liveness_watchdog.json"
    state_file = args.state_file or (_state_root / ".cache" / _state_name)
    now_ms = _now_ms()

    # Timer escalation depends on the prior run's inactive set.
    state = _load_state(state_file)
    prior_not_active_timers = {k[len(_PENDING_TIMER_PREFIX) :] for k in state if k.startswith(_PENDING_TIMER_PREFIX)}

    # Disabled sleeves skip cycle checks but keep every other check: turning a
    # sleeve off does not flatten it.
    unit_states = _unit_states(units)
    runtime_units = list(dict.fromkeys([*units, *_default_units_for_scope(args.account_scope)]))
    unit_runtime = _unit_runtime_metadata(runtime_units)
    not_active_timers = {u for u, s in unit_states.items() if u.endswith(".timer") and s != "active"}
    owner_states = {unit: unit_states.get(unit, "unknown") for unit in required_account_owner_units}
    non_owner_states = {unit: state for unit, state in unit_states.items() if unit not in required_account_owner_units}
    alerts = evaluate_unit_states(
        non_owner_states,
        prior_not_active_timers=prior_not_active_timers,
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
    if not mainnet and continuous_root is not None:
        alerts.extend(
            gather_continuous_alerts(
                continuous_root=continuous_root,
                args=args,
                cycle_checks=_sleeve_on("CONTINUOUS_SLEEVE", default="off"),
                environment="demo",
                unit_runtime=unit_runtime.get(_CONTINUOUS_DEMO_UNIT),
            )
        )
    hedge_model_prior = Path(args.hedge_model_prior) if str(args.hedge_model_prior).strip() else None
    if not mainnet and hedge_model_prior is not None and _sleeve_on("CONTINUOUS_HEDGE_TIMER", default="off"):
        alerts.extend(
            gather_hedge_model_prior_alerts(
                model_prior_path=hedge_model_prior,
                now_ms=now_ms,
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
    if mainnet and carry_mainnet_root is not None and _sleeve_on("CARRY_MAINNET_SLEEVE"):
        alerts.extend(
            gather_carry_alerts(
                carry_root=carry_mainnet_root,
                args=args,
                cycles_dataset="carry_hold_mainnet_cycles",
                environment="mainnet",
                unit_runtime=unit_runtime.get(_CARRY_MAINNET_UNIT),
            )
        )
    if mainnet and long_mainnet_root is not None and _sleeve_on("LONG_MAINNET_SLEEVE"):
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
    hedge_service = "liquidity-migration-continuous-hedge.service"
    if hedge_service in units:
        start_usec, exit_usec = _oneshot_run_window_usec(hedge_service)
        runtime_alert = evaluate_oneshot_runtime(
            unit=hedge_service,
            max_seconds=args.max_oneshot_run_seconds,
            start_monotonic_usec=start_usec,
            exit_monotonic_usec=exit_usec,
        )
        if runtime_alert is not None:
            alerts.append(runtime_alert)
    to_send, resolved, new_state = select_alerts_to_send(
        active=alerts, state=state, now_ms=now_ms, cooldown_minutes=args.cooldown_min
    )
    # Escalate a timer only after two consecutive inactive observations.
    new_state = {k: v for k, v in new_state.items() if not k.startswith(_PENDING_TIMER_PREFIX)}
    for unit in not_active_timers:
        new_state[f"{_PENDING_TIMER_PREFIX}{unit}"] = now_ms

    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    telegram_send_failed = False
    for alert in to_send:
        line = f"🚨 [{alert.severity}] liquidity-migration {ts}\n{alert.message}"
        print(line)
        if args.telegram:
            delivered = False
            try:
                delivered = send_telegram_message(line)
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
        line = f"✅ liquidity-migration {ts}: resolved — {key}"
        print(line)
        retry_key = f"{_RESOLVED_PREFIX}{key}"
        if args.telegram:
            delivered = False
            try:
                delivered = send_telegram_message(line)
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
