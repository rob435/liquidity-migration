#!/usr/bin/env python3
"""Fast account-kernel and strategy-input liveness watchdog for demo/paper.

The account execution owners are the only execution, reconciliation, and
protection authorities.  The checked operational authorization binds this
process to either ``demo`` or ``demo-paper`` scope.  The checker requires every
owner, live-L2 readiness sidecar, owner-health projection, and strategy input
inside that scope plus a recent healthy canonical demo venue snapshot. It
checks only the surviving account-owner and strategy-input surfaces.

Strategy-daemon cycle and input checks remain because an execution owner cannot
detect a hung signal scheduler or an empty/stale signal source.

Alerts are de-duplicated with a cooldown state file: a new condition alerts
immediately, a persisting one re-alerts at most every --cooldown-min, and a
cleared one sends a one-line "resolved" note. --heartbeat-url (or the
LIVENESS_HEARTBEAT_URL env var) is pinged on every healthy run so an EXTERNAL
dead-man's-switch (e.g. healthchecks.io) catches a total box death the on-box
watchdog cannot. No URL is provisioned by default.

Reads TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID only when Telegram delivery is
enabled. Exits 0 always (a watchdog must not crash-loop); failures to verify
degrade to an alert.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from liquidity_migration._common import exact_duration_ms  # noqa: E402
from liquidity_migration.artifact_snapshot import read_stable_file  # noqa: E402
from liquidity_migration.account_kernel import AccountEventType, read_account_journal  # noqa: E402
from liquidity_migration.account_owner_health import (  # noqa: E402
    AccountOwnerMarketWarmupPending,
    require_recent_account_owner_health,
    validate_systemd_invocation_id,
)
from liquidity_migration.account_owner_readiness import latest_market_readiness  # noqa: E402
from liquidity_migration.continuous_hedge_manager import (  # noqa: E402
    HEDGE_MODEL_PRIOR_KIND,
    load_hedge_model_prior,
    require_usable_hedge_model_prior,
)
from liquidity_migration.storage import read_dataset  # noqa: E402
from liquidity_migration.strategy_cycle_health import (  # noqa: E402
    StrategyCycleHealth,
    read_strategy_cycle_health,
)
from liquidity_migration.systemd_environment import parse_systemd_environment_bytes  # noqa: E402
from liquidity_migration.telegram import send_telegram_message  # noqa: E402

# Severity order for message framing only.
CRITICAL = "CRITICAL"
WARNING = "WARNING"

_DEMO_ACCOUNT_OWNER_UNIT = "liquidity-migration-account-execution.service"
_PAPER_ACCOUNT_OWNER_UNIT = "liquidity-migration-account-paper-execution.service"
_REQUIRED_ACCOUNT_OWNER_UNITS = (_DEMO_ACCOUNT_OWNER_UNIT, _PAPER_ACCOUNT_OWNER_UNIT)
_ACCOUNT_SCOPES = ("demo", "demo-paper")
_PAPER_RUNTIME_USER = "liquidity-migration-paper"
_PAPER_RUNTIME_GROUP = "liquidity-migration-paper"
_LONG_DEMO_UNIT = "liquidity-migration-bybit-long-demo.service"
_LONG_PAPER_UNIT = "liquidity-migration-bybit-long-paper.service"
_CONTINUOUS_DEMO_UNIT = "liquidity-migration-bybit-continuous-demo.service"
_CONTINUOUS_PAPER_UNIT = "liquidity-migration-bybit-continuous-paper.service"
def _default_root(rel: str) -> str:
    """Anchor a default data root at the repo dir (NOT the CWD).

    A manual/cron invocation from another directory must not silently point
    account, capture, or strategy checks at an empty relative root.
    """
    return str(_REPO_ROOT / rel)


def _paper_roots_from_environment(path: str | Path) -> tuple[Path, Path]:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError("paper owner environment path must be absolute")
    snapshot = read_stable_file(
        candidate,
        label="paper owner environment",
        reject_empty=True,
        require_mode=0o640,
        require_owner=True,
    )
    try:
        paper_gid = grp.getgrnam(_PAPER_RUNTIME_GROUP).gr_gid
    except KeyError as exc:
        raise ValueError(f"paper runtime group is not provisioned: {_PAPER_RUNTIME_GROUP}") from exc
    if snapshot.metadata.st_gid != paper_gid:
        raise ValueError("paper owner environment has the wrong runtime group")
    values = parse_systemd_environment_bytes(
        snapshot.data,
        label=f"paper owner environment {snapshot.path}",
    )
    roots = (
        Path(values.get("ACCOUNT_EXECUTION_ROOT", "")).expanduser(),
        Path(values.get("ACCOUNT_PAPER_CAPTURE_ROOT", "")).expanduser(),
    )
    if any(not root.is_absolute() for root in roots):
        raise ValueError("paper owner environment requires absolute account and capture roots")
    return roots


def _sleeve_on(env_var: str, *, default: str = "off") -> bool:
    """Read a sleeve toggle, failing safe to the supplied default."""
    return os.environ.get(env_var, default).strip().lower() in {"on", "1", "true", "yes"}


def _continuous_rmom_refresh_on() -> bool:
    """Match the deploy predicate: either CONTINUOUS sleeve needs RMOM refresh."""
    return _sleeve_on("CONTINUOUS_SLEEVE", default="off") or _sleeve_on("CONTINUOUS_PAPER_SLEEVE", default="off")


@dataclass(frozen=True)
class Alert:
    key: str  # stable identity for cooldown/dedup
    severity: str
    message: str


@dataclass(frozen=True)
class UnitRuntime:
    """Current systemd generation metadata used only for bounded startup logic."""

    invocation_id: str | None
    active_age_minutes: float | None


@dataclass(frozen=True)
class CompletedCycleObservation:
    """A completion projection bound back to its durable causal cycle row."""

    health: StrategyCycleHealth
    row: dict[str, Any]


# --------------------------------------------------------------------------- #
# Pure decision logic (unit-tested; no I/O)
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

    Cycle freshness catches hung/stopped strategy services. Timers need an
    explicit inactive check because a disabled timer otherwise stays silent;
    the debounce avoids escalating a transient deploy transition.
    """
    prior = prior_not_active_timers or set()
    alerts: list[Alert] = []
    for unit, state in sorted(unit_states.items()):
        if unit.endswith(".timer"):
            if state != "active":
                # Escalate only if the timer was ALSO not-active on the previous run;
                # a single transient observation (a deploy restart) stays a WARNING.
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
    """Account owners are required continuously, not merely checked for ``failed``.

    Strategy cycle rows cannot prove that the sole execution authority is alive,
    and a recently written capture can outlive a stopped owner for a few minutes.
    Therefore every non-active owner state is immediately critical.
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
    """Check immutable-prior integrity, not meaningless wall-clock freshness."""

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
    """The continuous decile join drops EVERY symbol when residual_momentum.parquet lacks today's row
    (the ``is_not_null`` filter empties the cross-section) — a SILENT zero-signal blackout that reads as
    'quiet market' (live_d9_symbols=0, rmom_present=True). Alert if the latest cycle's rmom day is
    missing or older than ``max_stale_days``. This is the watchdog half of the rmom-freshness fix
    (the precompute now defaults --end to tomorrow so the daily refresh keeps it current)."""
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

    New condition -> send now. Persisting condition -> re-send only after the
    cooldown, UNLESS its severity escalated above the last-sent severity (then send
    immediately — a WARNING that became CRITICAL must page now). A key present in
    state but no longer active -> resolved. A pending resolved-note retry (a
    ``resolved:<key>`` entry whose <key> is not active again) is re-surfaced for
    another delivery attempt; reserved bookkeeping entries (``resolved:``/
    ``pending_timer:``/``sev:``) never arm the alert-side cooldown, so a dropped
    resolved note can no longer suppress a genuine re-alert."""
    cooldown_ms = exact_duration_ms(minutes=cooldown_minutes)
    active_by_key = {a.key: a for a in active}
    # The alert-cooldown namespace excludes the reserved bookkeeping entries (pending
    # resolved-note retries, the timer-debounce markers, last-sent severity) so they
    # neither suppress a re-fire nor get mistaken for a stale active alert to resolve.
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
    # Newly-cleared conditions (active last run, gone now): drop the cooldown key (and
    # its severity marker) and mark it for a resolved note. Already-pending resolved
    # retries are re-surfaced too, unless the condition re-fired (then it's active).
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
    """Return Linux CLOCK_BOOTTIME without inventing a wall-clock substitute."""

    clock_id = getattr(time, "CLOCK_BOOTTIME", None)
    if clock_id is None:
        return None
    try:
        return time.clock_gettime_ns(clock_id)
    except (OSError, ValueError):
        return None


def _unit_runtime_metadata(units: list[str]) -> dict[str, UnitRuntime]:
    """Read generation id and monotonic active age for systemd services.

    Missing or malformed metadata does not create a grace period.  The caller
    falls back to the legacy causal-cycle check, so this observer never turns a
    systemd query failure into an unbounded suppression.
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
        units.extend(
            [
                _LONG_DEMO_UNIT,
                _LONG_PAPER_UNIT,
            ]
        )
    if _continuous_rmom_refresh_on():
        units.extend(
            [
                # The rmom refresh feeds the continuous entry gate for demo and paper
                # roots. Monitor it under the same predicate the deploy uses, so the
                # watchdog never pages on a timer intentionally disabled by sleeves.env.
                "liquidity-migration-continuous-rmom-refresh.service",
                "liquidity-migration-continuous-rmom-refresh.timer",
            ]
        )
    if _sleeve_on("CONTINUOUS_SLEEVE", default="off"):
        units.extend(
            [
                _CONTINUOUS_DEMO_UNIT,
                "liquidity-migration-continuous-hedge.timer",
                # The SERVICE too, not just the timer: a failed target-publisher
                # oneshot leaves the timer active/waiting and would otherwise never
                # page. is-active on a failed oneshot reports "failed", which
                # evaluate_unit_states alerts on. The hedge timer
                # rides $CONTINUOUS_SLEEVE alone, not the
                # rmom-refresh predicate, so it stays in this DEMO-only branch.
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
    if _sleeve_on("CONTINUOUS_PAPER_SLEEVE"):
        units.append(_CONTINUOUS_PAPER_UNIT)
    return units


def _default_units_for_scope(account_scope: str) -> list[str]:
    """Narrow the existing toggle-derived inventory to the authorized owners.

    The full ``demo-paper`` path preserves the toggle-derived unit inventory.
    ``demo`` removes every paper owner/producer and does not monitor
    the shared RMOM refresh solely because a disabled paper sleeve is on.
    """

    if account_scope not in _ACCOUNT_SCOPES:
        raise ValueError(f"unsupported account liveness scope: {account_scope}")
    units = _default_units_for_toggles()
    if account_scope == "demo-paper":
        return units
    paper_units = {
        _PAPER_ACCOUNT_OWNER_UNIT,
        _LONG_PAPER_UNIT,
        _CONTINUOUS_PAPER_UNIT,
    }
    if not _sleeve_on("CONTINUOUS_SLEEVE", default="off"):
        paper_units.update(
            {
                "liquidity-migration-continuous-rmom-refresh.service",
                "liquidity-migration-continuous-rmom-refresh.timer",
            }
        )
    return [unit for unit in units if unit not in paper_units]


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

    Write a same-directory temp file, fsync it, then ``os.replace`` onto the
    target. A bare ``write_text`` can leave a
    partial JSON line on crash, and ``_load_state`` then returns ``{}``, silently
    resetting every cooldown (fail-safe but noisy). The atomic write preserves the
    prior cooldown state across crashes instead.
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
    except Exception:  # noqa: BLE001
        # Fail-safe: a write error must not crash the watchdog. The next healthy run
        # re-attempts; cooldown state stays at the last good write.
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
    """Observe a receipt before binding it to its exact durable output row.

    Producers publish the cycle dataset before the completion receipt.  Reading
    in that same order lets a concurrent producer update advance the receipt
    after the watchdog has snapshotted the dataset, creating an impossible
    cross-generation pair.  Receipt-first observation preserves the causal
    publication order: its referenced cycle was already durable before the
    receipt could be read.
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

    Execution, positions, reconciliation, and protection belong exclusively to
    the account owner and are intentionally absent from this sleeve check.
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
            # Empty universe or kline input can masquerade as a quiet market.
            # Include the root label so demo and paper cooldown keys stay distinct.
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


def gather_account_capture_alerts(
    *,
    capture_root: Path,
    now_ms: int | None = None,
    max_age_minutes: float,
    label: str = "",
    expected_owner_uid: int | None = None,
) -> list[Alert]:
    """Detect an owner that is active/restarting but no longer ingesting L2.

    The historical function/key name is retained to avoid resetting alert
    cooldown state.  The checked artifact is now a bounded live-market sidecar,
    not a growing raw-capture segment.
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
    if age_minutes < 0.0 or age_minutes > max_age_minutes:
        return [
            Alert(
                key="account_health_stale",
                severity=CRITICAL,
                message=(
                    f"canonical account reconciliation health is {age_minutes:.1f} min old "
                    f"(allowed 0..{max_age_minutes:g} min); owner health is stale or future-dated."
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
        if now_ms is None:
            require_recent_account_owner_health(
                account_root,
                environment=environment,
                max_age_ns=max_age_ns,
            )
        else:
            require_recent_account_owner_health(
                account_root,
                environment=environment,
                max_age_ns=max_age_ns,
                now_ns=now_ms * 1_000_000,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, AccountOwnerMarketWarmupPending):
            # The owner remains blocked and target producers still fail closed.
            # This exact detail is the nonterminal <=30s dynamic-subscription
            # transition. Suppress only its observer page; a latched timeout has
            # a different detail and still pages regardless of service age.
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
    """Build the parser used by the systemd argument-parity test."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--unit",
        action="append",
        default=None,
        help="systemd unit(s) to liveness-check (repeatable). Defaults to the core demo/paper units.",
    )
    p.add_argument(
        "--account-scope",
        choices=_ACCOUNT_SCOPES,
        default=os.environ.get("ACCOUNT_LIVENESS_SCOPE") or "demo-paper",
        help="require demo owner/runtime only, or both demo and paper (default: environment or demo-paper)",
    )
    p.add_argument("--max-cycle-age-min", type=float, default=10.0, help="alert if no cycle within this many minutes")
    p.add_argument("--max-ws-lag-hours", type=float, default=6.0, help="warn if the WS kline feed is this stale")
    # Keep roots as strings so the empty-string skip sentinel does not become Path('.').
    # Defaults are anchored at the repo dir via _default_root (NOT relative to the CWD)
    # so a manual/cron invocation from another directory cannot silently disable
    # account/capture/strategy safety gathers.
    p.add_argument(
        "--continuous-root",
        default=os.environ.get("CONTINUOUS_DEMO_DATA_ROOT") or _default_root("data/bybit-continuous-demo-event"),
        help="continuous-fade sleeve root for cycle/input freshness ('' to skip)",
    )
    p.add_argument(
        "--continuous-paper-root",
        default=os.environ.get("CONTINUOUS_PAPER_DATA_ROOT") or _default_root("data/bybit-continuous-paper-event"),
        help="continuous-fade paper root for cycle/input freshness ('' to skip)",
    )
    p.add_argument(
        "--long-root",
        default=os.environ.get("LONG_DEMO_DATA_ROOT") or _default_root("data/bybit-long-demo-event"),
        help="long-native sleeve root for cycle/input freshness ('' to skip)",
    )
    p.add_argument(
        "--long-paper-root",
        default=os.environ.get("LONG_PAPER_DATA_ROOT") or _default_root("data/bybit-long-paper-event"),
        help="long-native paper sleeve root for cycle/input freshness ('' to skip)",
    )
    p.add_argument(
        "--account-root",
        default=os.environ.get("ACCOUNT_EXECUTION_ROOT") or _default_root("data/bybit-account-execution"),
        help="canonical demo account journal root for reconciliation health",
    )
    p.add_argument(
        "--account-paper-root",
        default=os.environ.get("ACCOUNT_PAPER_EXECUTION_ROOT") or _default_root("data/bybit-account-paper"),
        help="canonical paper account root for owner-health evidence",
    )
    p.add_argument(
        "--account-paper-environment-file",
        default=os.environ.get("ACCOUNT_PAPER_EXECUTION_ENV_FILE") or "",
        help="private paper-owner EnvironmentFile whose bound roots override paper root arguments",
    )
    p.add_argument(
        "--account-capture-root",
        default=os.environ.get("ACCOUNT_CAPTURE_ROOT") or _default_root("data/bybit-account-market-capture"),
        help="demo account-owner market/readiness and decision-context root",
    )
    p.add_argument(
        "--account-paper-capture-root",
        default=os.environ.get("ACCOUNT_PAPER_CAPTURE_ROOT")
        or _default_root("data/bybit-account-paper-market-capture"),
        help="paper account-owner market/readiness and decision-context root",
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
    p.add_argument("--telegram", action="store_true", help="send alerts via Telegram (else stdout only)")
    p.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="cooldown state file (default: <continuous-root>/.cache/liveness_watchdog.json)",
    )
    return p


def main() -> int:
    args = build_arg_parser().parse_args()

    required_account_owner_units = (
        (_DEMO_ACCOUNT_OWNER_UNIT,) if args.account_scope == "demo" else _REQUIRED_ACCOUNT_OWNER_UNITS
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
    continuous_paper_root = Path(args.continuous_paper_root) if str(args.continuous_paper_root).strip() else None
    long_root = Path(args.long_root) if str(args.long_root).strip() else None
    long_paper_root = Path(args.long_paper_root) if str(args.long_paper_root).strip() else None
    paper_account_root = Path(args.account_paper_root)
    paper_capture_root = Path(args.account_paper_capture_root)
    paper_owner_uid: int | None = None
    paper_root_alert: Alert | None = None
    if args.account_scope == "demo-paper":
        try:
            paper_owner_uid = pwd.getpwnam(_PAPER_RUNTIME_USER).pw_uid
            if str(args.account_paper_environment_file).strip():
                paper_account_root, paper_capture_root = _paper_roots_from_environment(
                    args.account_paper_environment_file
                )
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            paper_root_alert = Alert(
                key="paper_account_environment_invalid",
                severity=CRITICAL,
                message=f"paper owner identity/environment roots are unavailable: {type(exc).__name__}: {str(exc)[:400]}",
            )
    # Keep cooldown state stable even when both sleeve roots are skipped.
    _state_root = continuous_root or long_root or (_REPO_ROOT / "data")
    state_file = args.state_file or (_state_root / ".cache" / "liveness_watchdog.json")
    now_ms = _now_ms()

    # Timer escalation depends on the prior run's inactive set.
    state = _load_state(state_file)
    prior_not_active_timers = {k[len(_PENDING_TIMER_PREFIX) :] for k in state if k.startswith(_PENDING_TIMER_PREFIX)}

    # Skip cycle checks for disabled sleeves, but still inspect residual open state:
    # turning a sleeve off does not flatten it.
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
    if paper_root_alert is not None:
        alerts.append(paper_root_alert)
    alerts.extend(
        gather_account_capture_alerts(
            capture_root=Path(args.account_capture_root),
            max_age_minutes=args.max_account_capture_age_min,
        )
    )
    if args.account_scope == "demo-paper" and paper_root_alert is None:
        alerts.extend(
            gather_account_capture_alerts(
                capture_root=paper_capture_root,
                max_age_minutes=args.max_account_capture_age_min,
                label="paper",
                expected_owner_uid=paper_owner_uid,
            )
        )
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
    if args.account_scope == "demo-paper" and paper_root_alert is None:
        alerts.extend(
            gather_account_owner_health_alerts(
                account_root=paper_account_root,
                environment="paper",
                max_age_minutes=args.max_account_health_age_min,
                startup_grace_minutes=args.max_cycle_age_min,
                unit_runtime=unit_runtime.get(_PAPER_ACCOUNT_OWNER_UNIT),
            )
        )
    if continuous_root is not None:
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
    if hedge_model_prior is not None and _sleeve_on("CONTINUOUS_HEDGE_TIMER", default="off"):
        alerts.extend(
            gather_hedge_model_prior_alerts(
                model_prior_path=hedge_model_prior,
                now_ms=now_ms,
            )
        )
    if (
        args.account_scope == "demo-paper"
        and continuous_paper_root is not None
        and _sleeve_on("CONTINUOUS_PAPER_SLEEVE")
    ):
        alerts.extend(
            gather_continuous_alerts(
                continuous_root=continuous_paper_root,
                args=args,
                cycles_dataset="continuous_fade_paper_cycles",
                environment="paper",
                unit_runtime=unit_runtime.get(_CONTINUOUS_PAPER_UNIT),
            )
        )
    if long_root is not None:
        alerts.extend(
            gather_long_alerts(
                long_root=long_root,
                args=args,
                cycle_checks=_sleeve_on("LONG_SLEEVE"),
                environment="demo",
                unit_runtime=unit_runtime.get(_LONG_DEMO_UNIT),
            )
        )
    if args.account_scope == "demo-paper" and long_paper_root is not None and _sleeve_on("LONG_SLEEVE"):
        alerts.extend(
            gather_long_alerts(
                long_root=long_paper_root,
                args=args,
                cycles_dataset="long_native_paper_cycles",
                environment="paper",
                unit_runtime=unit_runtime.get(_LONG_PAPER_UNIT),
            )
        )
    to_send, resolved, new_state = select_alerts_to_send(
        active=alerts, state=state, now_ms=now_ms, cooldown_minutes=args.cooldown_min
    )
    # Escalate a timer only after two consecutive inactive observations.
    new_state = {k: v for k, v in new_state.items() if not k.startswith(_PENDING_TIMER_PREFIX)}
    for unit in not_active_timers:
        new_state[f"{_PENDING_TIMER_PREFIX}{unit}"] = now_ms

    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
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
                # An undelivered alert must not advance its cooldown or severity.
                print("(telegram send returned False — TELEGRAM_* env missing or API non-2xx; will retry next run)")
                # Revert BOTH the cooldown stamp and the last-sent-severity marker to
                # their pre-send values so an undelivered alert advances neither — the
                # next run re-evaluates and retries (incl. a still-pending escalation).
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
                # Retry under a separate namespace so it cannot arm alert cooldown.
                new_state[retry_key] = now_ms
                print("(telegram send returned False — resolved note will retry next run)")
                continue
        # Delivered (or stdout-only mode, which always "delivers"): clear any pending
        # retry marker so the resolved note isn't re-sent forever.
        new_state.pop(retry_key, None)
    # State saved AFTER the sends so an undelivered alert's cooldown stays unset.
    _save_state(state_file, new_state)

    # Healthy run -> ping the external dead-man's-switch so a TOTAL box death is
    # caught by the external monitor (the on-box watchdog cannot alert if the box
    # is gone). Only ping when there are no CRITICAL alerts firing.
    if args.heartbeat_url and not any(a.severity == CRITICAL for a in alerts):
        _ping_heartbeat(args.heartbeat_url)

    if not to_send and not resolved:
        print(f"ok ({ts}): {len(alerts)} active alert(s) within cooldown; monitored {len(units)} unit(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
