#!/usr/bin/env python3
"""Account-kernel and strategy-input liveness watchdog for the deployed fleets.

Scope is ``demo`` or ``mainnet``. Within it the checker requires the account
owner, live-L2 readiness sidecar, owner-health projection, and strategy input,
plus a recent healthy canonical venue snapshot. Strategy-daemon cycle and input
checks live here because an execution owner cannot detect a hung signal
scheduler or an empty/stale signal source. The ``mainnet`` scope is disjoint
from the demo roots and runs only its own owner and producers.

--engine-heartbeat-file (or LIVENESS_ENGINE_HEARTBEAT_FILE) adds the engine's
own heartbeat file: how long ago it was written, and whether the engine has
latched itself out of opening new positions — a state that looks perfectly
healthy from every other check here. Unset, the file is never opened and
nothing new can alert; no engine heartbeat is provisioned by default.

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
from liquidity_migration.core.env_flags import validate_systemd_invocation_id  # noqa: E402
from liquidity_migration.venue.venue_instrument_rules import load_venue_rules_bytes  # noqa: E402
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

_DEMO_ACCOUNT_OWNER_UNIT = "liquidity-migration-engine.service"
_MAINNET_ACCOUNT_OWNER_UNIT = "liquidity-migration-engine-mainnet.service"
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


# Pure decision logic (no I/O)
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
    realm: str = "demo",
) -> Alert | None:
    """Warn before rules evidence expires and the next start fails closed.

    ``realm`` changes the alert key and the remedy text only: the funded
    receipt renews through a read-only freeze on any deploy, while demo's
    slow path is the order-placing probe in a flat maintenance window.
    """
    key = "demo_rules_age" if realm == "demo" else "venue_rules_age"
    label = "demo-rule" if realm == "demo" else f"{realm} venue-rule"
    age_hours = (now_ns - verified_ts_ns) / 3_600_000_000_000.0
    remaining_hours = REGISTERED_MAX_DEMO_RULE_AGE_HOURS - age_hours
    if age_hours < 0.0:
        return Alert(
            key=key,
            severity=CRITICAL,
            message=(
                f"{label} evidence is {-age_hours:.1f}h future-dated; "
                "runtime quantity authority is invalid."
            ),
            headline="The trading-rules receipt is future-dated — invalid.",
        )
    if remaining_hours <= 0.0:
        # Nothing on demo fails closed on an expired receipt:
        # run_authorized_runtime.sh has no rule gate, neither producer script
        # mentions one, and the engine reads instrument rules off the venue.
        # Calling it CRITICAL only teaches the operator that CRITICAL can be
        # ignored. What is genuinely stale is the bound receipt the research
        # and candidate-universe tooling reads.
        #
        # Mainnet keeps both the severity and the claim: its receipt does gate
        # the funded owner, and every deploy renews it.
        demo = realm == "demo"
        return Alert(
            key=key,
            severity=WARNING if demo else CRITICAL,
            message=(
                f"{label} evidence expired {abs(remaining_hours):.1f}h ago; "
                + (
                    "nothing in the runtime path reads it, so no unit will refuse to start — what is "
                    "stale is the bound receipt the research and candidate-universe tooling reads. "
                    "Refresh it on a deploy carrying --refresh-demo-rules (live PostOnly orders, "
                    "<=200 USDT per symbol)."
                    if demo
                    else "the funded owner will refuse to start; any deploy renews it (read-only freeze)."
                )
            ),
            headline=(
                "The demo trading-rules receipt has expired — stale evidence, nothing refuses to start."
                if demo
                else "The trading-rules receipt has expired — the next restart will refuse to start."
            ),
        )
    if remaining_hours <= DEMO_RULE_MAINTENANCE_WARNING_HOURS:
        remedy = (
            "schedule the guarded flat-account maintenance window before expiry "
            "so the slow path is planned."
            if realm == "demo"
            else "any deploy renews it (read-only freeze); deploy before expiry."
        )
        return Alert(
            key=key,
            severity=WARNING,
            message=f"{label} evidence expires in {remaining_hours:.1f}h; " + remedy,
            headline=f"The trading-rules receipt expires in {remaining_hours:.0f}h — plan the refresh.",
        )
    return None


def gather_demo_rule_alerts(
    *,
    rules_path: Path,
    now_ns: int | None = None,
    realm: str = "demo",
) -> list[Alert]:
    """Reopen the bound receipt and report corruption, future dating, or expiry.

    The mainnet receipt is validated by the loader that admits one; only
    mainnet holds the registered 168-hour ceiling as a hard start refusal, so
    it is exactly the receipt that must not reach that cliff unwatched (it
    did, silently, until 2026-08-13).
    """
    try:
        snapshot = read_stable_file(
            rules_path,
            label="demo-rule receipt" if realm == "demo" else f"{realm} venue-rule receipt",
            reject_empty=True,
            require_mode=0o600,
            require_owner=True,
        )
        if realm == "demo":
            load_demo_rules(
                snapshot.path,
                max_age_seconds=None,
                snapshot=snapshot,
            )
        else:
            load_venue_rules_bytes(snapshot.data, realm=realm, max_age_seconds=None)
        payload = json.loads(snapshot.data)
        verified_ts_ns = int(payload.get("verified_ts_ns") or 0)
        alert = evaluate_demo_rule_age(
            verified_ts_ns=verified_ts_ns,
            now_ns=time.time_ns() if now_ns is None else now_ns,
            realm=realm,
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
        parts = [f"mode {self.mode}"]
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
        ("may_open", bool),
        ("market_events", int),
        ("orders_sent", int),
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
    return EngineHeartbeat(
        wall_ts_ms=int(payload["wall_ts_ms"]),
        mode=mode,
        may_open=bool(payload["may_open"]),
        market_events=int(payload["market_events"]),
        orders_sent=int(payload["orders_sent"]),
        account_user_id=account if isinstance(account, str) else None,
        pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        account_observed_wall_ts_ms=(observed if isinstance(observed, int) and not isinstance(observed, bool) else None),
    )


def evaluate_engine_heartbeat(
    *,
    heartbeat: EngineHeartbeat,
    now_ms: int,
    max_age_seconds: float,
    max_account_view_age_minutes: float = VENUE_SNAPSHOT_AGE_FLOOR_MINUTES,
) -> list[Alert]:
    """Report an engine that stopped writing, and one that stopped opening positions.

    Every message names the mode. A beat still carrying the retired `shadow`
    was written by an engine that reached the venue with nothing, so both
    conditions mean less on one of those than on a live beat.
    """
    alerts: list[Alert] = []
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


# The 25-minute venue-snapshot bound is VENUE_SNAPSHOT_AGE_FLOOR_MINUTES,
# defined once, above. A second definition here once shadowed it: function
# defaults bound the first value at def time while the runtime clamps read the
# module attribute, so editing one silently diverged the two.

# The freshness check needs the newest venue snapshot, not a genesis replay:
# a full verified read cost ~20s CPU and ~250MB peak at 28.5k segments, every
# 3 minutes, re-verifying a chain each generation already verified at startup.
# Deep enough that a ten-minute checkpoint is always inside it unless the
# journal is taking more than one transaction per second, ~250x the observed
# non-snapshot rate.
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
        "--max-account-health-age-min",
        type=float,
        default=1.0,
        help="critical alert if owner or demo reconciliation health is older than this",
    )
    p.add_argument(
        "--engine-heartbeat-file",
        default=os.environ.get("LIVENESS_ENGINE_HEARTBEAT_FILE") or "",
        help=(
            "the engine's heartbeat file; alerts when it stops being written, cannot be read, or says "
            "the engine has latched itself out of opening positions ('' to skip). Defaults to the "
            "LIVENESS_ENGINE_HEARTBEAT_FILE env var so a unit can wire it via EnvironmentFile"
        ),
    )
    p.add_argument(
        "--max-engine-heartbeat-age-sec",
        type=float,
        default=60.0,
        help="critical alert if the engine's heartbeat is older than this (it writes every few seconds)",
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
        # Empty by default: the hourly digest is retired and nothing writes
        # this file. Defaulting it from ACCOUNT_EXECUTION_ROOT points both
        # fleets at a frozen file and pages all day about a notification
        # channel that is not broken but abolished. The flag still works if a
        # digest ever returns and is pointed at it explicitly.
        default="",
        help=(
            "committed notification state to age-check; alerts when the digest stalls. Empty by "
            "default: the digest is retired and nothing writes this file"
        ),
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
    if str(args.demo_rules_file).strip():
        # Both realms: only mainnet holds the 168h ceiling as a hard start
        # refusal, so its receipt is the one that must never expire unseen.
        alerts.extend(
            gather_demo_rule_alerts(
                rules_path=Path(args.demo_rules_file),
                now_ns=now_ms * 1_000_000,
                realm="mainnet" if mainnet else "demo",
            )
        )
    # Do not check the account journal here: no engine crate names it, so the
    # file is frozen and such a check can never clear. The engine's own account
    # reading, checked in the heartbeat above, is what has a live writer.
    #
    # Nothing watches "the exchange and our records disagree" today — the engine
    # reconciles but publishes no mismatch. `gather_account_health_alerts` is
    # kept, uncalled, as the specification for whatever writes that evidence.
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
    if str(args.engine_heartbeat_file).strip():
        # Unset means the file is never opened and nothing new can alert: no
        # engine heartbeat is provisioned on the fleet as it runs today.
        alerts.extend(
            gather_engine_heartbeat_alerts(
                heartbeat_path=Path(args.engine_heartbeat_file),
                max_age_seconds=args.max_engine_heartbeat_age_sec,
                # The operator dial that bounds the engine's own account
                # reading: one knob, pointed at the reader that has a writer.
                max_account_view_age_minutes=args.max_account_health_age_min,
                # No now_ms: the engine rewrites this file every few seconds, and
                # by the time this run reaches it the clock sampled at the top of
                # main() is a second or two behind. Handing that stale reading in
                # dates any heartbeat written since as being in the future. The
                # gather reads the file and then asks the clock, in that order,
                # so the age cannot go negative unless a clock really is wrong.
            )
        )
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
                delivered = send_telegram_message(message, channel="alerts")
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
