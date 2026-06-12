#!/usr/bin/env python3
"""Fast liveness + safety watchdog for the live demo book.

Answers "is the system ALIVE and are open positions PROTECTED?" and runs every
few minutes so the operator can manually close positions when something breaks.
(The hourly short-sleeve entry-health companion was erased with that sleeve,
2026-06-11.) It Telegrams on:

  * DAEMON DOWN / HUNG -- no cycle has been written within --max-cycle-age-min
    (catches a crash-loop under ``Restart=always``, a hang, or a stop), and/or a
    monitored systemd unit is not ``active``.
  * UNPROTECTED POSITION -- an open ledger position whose Bybit position carries
    no / a wrong server-side ``stopLoss`` (the risk daemon should re-arm it; a
    persistent miss means CLOSE MANUALLY). This is the authoritative check that
    the resting exchange-side stop is actually in place.
  * LEDGER<->VENUE MISMATCH -- a position open on one side but not the other.
  * WS FEED STALL -- the WS kline store's newest bar is far behind wall clock
    (REST still covers correctness, so this is a warning).

Alerts are de-duplicated with a cooldown state file: a new condition alerts
immediately, a persisting one re-alerts at most every --cooldown-min, and a
cleared one sends a one-line "resolved" note. --heartbeat-url (or the
LIVENESS_HEARTBEAT_URL env var) is pinged on every healthy run so an EXTERNAL
dead-man's-switch (e.g. healthchecks.io) catches a total box death the on-box
watchdog cannot — NOTE: no URL is provisioned by default, so until the operator
sets one this protection does not exist (audit 2026-06-12 round 3).

Reads TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID and (for the stop-protection check)
the Bybit demo creds from the daemon environment. Exits 0 always (a watchdog
must not crash-loop); failures to verify degrade to a warning alert.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from liquidity_migration.storage import read_dataset  # noqa: E402
from liquidity_migration.telegram import send_telegram_message  # noqa: E402

# Severity order for message framing only.
CRITICAL = "CRITICAL"
WARNING = "WARNING"


def _sleeve_on(env_var: str, *, default: str = "off") -> bool:
    """A sleeve is active unless its kill-switch toggle (deploy/sleeves.env, loaded into this
    watchdog's env via the liveness service EnvironmentFile) is off. ``default`` is the
    last-resort value when the toggle is UNSET; it mirrors deploy/lib_sleeves.sh exactly
    (EVERY sleeve fails safe to off since audit 2026-06-12 round 3 — a missing config
    must never resurrect an order-submitting sleeve), so this watchdog can never page
    for a retired sleeve nor expect a disabled sleeve to be up on a stripped/manual
    invocation. In production the EnvironmentFile always sets the toggle, so the
    default only matters off-VPS."""
    return os.environ.get(env_var, default).strip().lower() in {"on", "1", "true", "yes"}


@dataclass(frozen=True)
class Alert:
    key: str  # stable identity for cooldown/dedup
    severity: str
    message: str


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


def evaluate_unit_states(unit_states: dict[str, str]) -> list[Alert]:
    """SERVICES alert only on the TERMINAL systemd ``failed`` state; TIMERS alert
    on anything not ``active``.

    A deploy (or any ``Restart=always`` recovery) walks a service through
    activating -> active -> deactivating -> inactive -> activating, so alerting
    on anything-not-active would fire on EVERY deploy. ``failed`` is the only
    unambiguous "systemd gave up" state; a daemon that is merely down/hung/stopped
    is caught (naturally debounced) by the per-data-root cycle-age check instead.

    TIMERS are different: a timer essentially never enters ``failed`` — a stopped
    or disabled timer reports ``inactive`` and its scheduled job (hedge, daily
    reports) simply never fires again, silently and forever (audit 2026-06-12
    round 3). A healthy monitored timer is ``active`` (waiting), so not-active IS
    the alarm; deploys restart timers in well under one watchdog interval."""
    alerts: list[Alert] = []
    for unit, state in sorted(unit_states.items()):
        if unit.endswith(".timer"):
            if state != "active":
                alerts.append(
                    Alert(
                        key=f"unit:{unit}",
                        severity=CRITICAL,
                        message=(
                            f"systemd timer {unit} is {state.upper()} (not active) — its scheduled "
                            f"job will never fire. Re-enable it: systemctl enable --now {unit}"
                        ),
                    )
                )
        elif state == "failed":
            alerts.append(
                Alert(
                    key=f"unit:{unit}",
                    severity=CRITICAL,
                    message=f"systemd unit {unit} is FAILED (systemd gave up restarting it). Check positions.",
                )
            )
    return alerts


def evaluate_stop_protection(
    *,
    open_trades: list[dict],
    venue_positions: dict[str, dict],
    tolerance_frac: float = 0.02,
) -> list[Alert]:
    """For every open ledger trade, confirm the Bybit position carries a
    server-side stopLoss close to the ledger's stop_price. A missing/wrong stop
    on a live position is the single most dangerous state -> CRITICAL."""
    alerts: list[Alert] = []
    for trade in open_trades:
        symbol = str(trade.get("symbol") or "")
        if not symbol:
            continue
        stop_price = _f(trade.get("stop_price"))
        pos = venue_positions.get(symbol)
        venue_size = _f((pos or {}).get("size"))
        if pos is None or venue_size <= 0.0:
            alerts.append(
                Alert(
                    key=f"mismatch:{symbol}",
                    severity=WARNING,
                    message=(
                        f"{symbol}: ledger shows an OPEN position but Bybit reports none "
                        f"(size={venue_size}). Reconciler should resolve it; verify manually."
                    ),
                )
            )
            continue
        if stop_price <= 0.0:
            continue  # trade carries no stop spec (shouldn't happen on the promoted profile)
        venue_stop = _f(pos.get("stopLoss"))
        if venue_stop <= 0.0 or abs(venue_stop - stop_price) > stop_price * tolerance_frac:
            alerts.append(
                Alert(
                    key=f"unprotected:{symbol}",
                    severity=CRITICAL,
                    message=(
                        f"{symbol}: UNPROTECTED — Bybit stopLoss={venue_stop or 'NONE'} vs expected "
                        f"{stop_price:.6g}. The risk daemon should re-arm it; if this persists, "
                        f"CLOSE THE POSITION MANUALLY."
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
        )
    return None


def evaluate_rmom_staleness(
    *, max_rmom_day_ts: int, now_ms: int, max_stale_days: float, label: str
) -> Alert | None:
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


def select_alerts_to_send(
    *, active: list[Alert], state: dict[str, int], now_ms: int, cooldown_minutes: float
) -> tuple[list[Alert], list[str], dict[str, int]]:
    """Cooldown + resolve logic. Returns (alerts_to_send, resolved_keys, new_state).

    New condition -> send now. Persisting condition -> re-send only after the
    cooldown. A key present in state but no longer active -> resolved."""
    cooldown_ms = cooldown_minutes * 60_000.0
    active_by_key = {a.key: a for a in active}
    to_send: list[Alert] = []
    new_state = dict(state)
    for key, alert in active_by_key.items():
        last = state.get(key)
        if last is None or (now_ms - last) >= cooldown_ms:
            to_send.append(alert)
            new_state[key] = now_ms
    resolved = [k for k in state if k not in active_by_key]
    for k in resolved:
        new_state.pop(k, None)
    return to_send, resolved, new_state


def _f(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
# I/O at the edges
# --------------------------------------------------------------------------- #
def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _latest_cycle_df(cycles_root: Path) -> pl.DataFrame | None:
    if not cycles_root.exists():
        return None
    date_dirs = sorted(cycles_root.glob("date=*"))
    if not date_dirs:
        return None
    parts = sorted(date_dirs[-1].glob("*.parquet"))
    if not parts:
        return None
    try:
        return pl.read_parquet(parts[-1])
    except Exception:  # noqa: BLE001 - watchdog never crashes
        return None


def _unit_states(units: list[str]) -> dict[str, str]:
    states: dict[str, str] = {}
    for unit in units:
        try:
            out = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True, text=True, timeout=10,
            )
            states[unit] = (out.stdout or out.stderr).strip() or "unknown"
        except Exception:  # noqa: BLE001
            states[unit] = "unknown"
    return states


def _unit_enabled(unit: str) -> bool:
    """systemctl is-enabled, fail-quiet (a watchdog must never crash)."""
    try:
        return (
            subprocess.run(
                ["systemctl", "is-enabled", "--quiet", unit],
                capture_output=True, text=True, timeout=10,
            ).returncode
            == 0
        )
    except Exception:  # noqa: BLE001
        return False


def _default_units_for_toggles() -> list[str]:
    units = [
        "liquidity-migration-bybit-risk.service",
        # Always-on forward-evidence collector (enabled by every deploy): a failed
        # collector is unbuyable history silently lost — page on it (audit 2026-06-12).
        "liquidity-migration-liquidation-collector.service",
        # The daily reports are the operator's heartbeat; BOTH oneshot services exit 1
        # on a failed send (combined-book since round 2, forward-report since round 3),
        # so monitoring them pages instead of "the report just never arrived". Their
        # TIMERS are monitored too: a stopped timer is "inactive", never "failed",
        # and a dead timer means the report silently never runs again (round 3).
        "liquidity-migration-combined-book-report.service",
        "liquidity-migration-combined-book-report.timer",
        "liquidity-migration-continuous-forward-report.service",
        "liquidity-migration-continuous-forward-report.timer",
    ]
    if _sleeve_on("LONG_SLEEVE"):
        units.extend(
            [
                "liquidity-migration-bybit-long-demo.service",
                "liquidity-migration-bybit-long-paper.service",
            ]
        )
    if _sleeve_on("CONTINUOUS_SLEEVE", default="off"):
        units.extend(
            [
                "liquidity-migration-bybit-continuous-demo.service",
                "liquidity-migration-continuous-hedge.timer",
                # The SERVICE too, not just the timer: a failed oneshot leaves the
                # timer active/waiting, so a crashed (order-submitting, SUBMIT_HEDGE=1)
                # hedge run would otherwise never page — is-active on a failed oneshot
                # reports "failed", which evaluate_unit_states alerts on.
                "liquidity-migration-continuous-hedge.service",
                # The daily rmom refresh feeds the live entry gate; unmonitored, a
                # failing refresh was silent until the rmom-staleness CRITICAL at
                # ~2 days — the order-submitting sleeve traded against an aging
                # gate the whole time (audit 2026-06-12 round 3).
                "liquidity-migration-continuous-rmom-refresh.service",
                "liquidity-migration-continuous-rmom-refresh.timer",
            ]
        )
    if _sleeve_on("CONTINUOUS_PAPER_SLEEVE"):
        units.append("liquidity-migration-bybit-continuous-paper.service")
    # The depth collector is operator-gated (deploy installs but never enables
    # it). When the operator HAS enabled it, monitor it like the liquidation
    # leg — same unbuyable-history argument; it could reach terminal 'failed'
    # with nothing paging (round 4).
    if _unit_enabled("liquidity-migration-depth-collector.service"):
        units.append("liquidity-migration-depth-collector.service")
    return units


def _venue_positions(settle_coin: str = "USDT") -> tuple[dict[str, dict], str | None]:
    """Return (positions_by_symbol, error). Degrades gracefully if creds/API down.

    Bybit's linear get_positions requires ``settleCoin`` (or a symbol), so the
    daemon calls ``get_positions(settle_coin="USDT")`` — the watchdog must too,
    or every call fails 'params error'."""
    try:
        from liquidity_migration.bybit import BybitPrivateClient, resolve_private_credentials

        api_key, api_secret, demo = resolve_private_credentials()
        if not api_key or not api_secret:
            return {}, "no demo API creds in environment"
        client = BybitPrivateClient(api_key=api_key, api_secret=api_secret, demo=demo)
        raw = client.get_positions(settle_coin=settle_coin)
        rows = raw if isinstance(raw, list) else raw.get("list", [])
        return {str(p.get("symbol")): p for p in rows if str(p.get("symbol"))}, None
    except Exception as exc:  # noqa: BLE001
        return {}, f"{type(exc).__name__}: {exc}"[:200]


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
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, sort_keys=True))
    except Exception:  # noqa: BLE001
        pass


def gather_risk_alerts(*, risk_root: Path, now_ms: int, args: argparse.Namespace) -> list[Alert]:
    """Liveness for the ws_risk engine — THE single reconcile/stop-repair authority.

    It writes a heartbeat cycle row every ~10s to ``<risk-root>/event_demo_cycles``;
    no fresh row within --max-cycle-age-min means the engine is STOPPED or HUNG.
    ``systemctl is-active`` catches neither (a stopped unit is "inactive", a hung
    one "active") — only a fast crash-loop ever reaches "failed". The short-sleeve
    erasure (e03e9ab) deleted the only check that covered this; while ws_risk is
    down, stop repair, orphan adoption and fill reconciliation are all silently
    dead (audit 2026-06-12 round 3). The risk service has no sleeve toggle, so
    this gather is unconditional (skip with --risk-root '')."""
    if not risk_root.exists():
        return []
    cyc = _latest_cycle_df(risk_root / "event_demo_cycles")
    latest_ts = (
        int(cyc.select(pl.col("ts_ms").max()).item())
        if (cyc is not None and not cyc.is_empty() and "ts_ms" in cyc.columns) else None
    )
    live = evaluate_cycle_liveness(
        latest_cycle_ts_ms=latest_ts, now_ms=now_ms,
        max_age_minutes=args.max_cycle_age_min, label="ws_risk",
    )
    return [live] if live else []


def gather_liquidation_capture_alerts(
    *, liquidations_root: Path, now_ms: int, max_age_hours: float
) -> list[Alert]:
    """Freshness of the forward liquidation capture. The collector unit can never
    reach systemd "failed" (Restart=always with RestartSec=15 spaces starts beyond
    the default start-limit window), so a crash-looping or hung-but-connected
    collector loses unbuyable history silently forever (audit 2026-06-12 round 3).
    Bybit liquidations on a 400+ symbol universe arrive near-continuously; a stale
    newest-file mtime means capture stopped. Venues that have never written (the
    region-blocked Binance leg) contribute no files and never alarm."""
    if not liquidations_root.exists():
        return []
    try:
        newest_mtime_ms = max(
            (int(p.stat().st_mtime * 1000) for p in liquidations_root.glob("*/*.jsonl")),
            default=0,
        )
    except OSError:
        newest_mtime_ms = 0
    if newest_mtime_ms <= 0:
        return []  # nothing ever captured here (fresh box) — the unit check covers a dead service
    age_h = (now_ms - newest_mtime_ms) / 3_600_000.0
    if age_h > max_age_hours:
        return [Alert(
            key="liquidation_capture_stale",
            severity=WARNING,
            message=(
                f"liquidation capture STALE — newest JSONL {age_h:.1f}h old (> {max_age_hours:.0f}h). "
                f"The collector unit is alive-but-not-writing (it cannot reach systemd 'failed'); "
                f"check its journal. Every silent hour is forward history lost."
            ),
        )]
    return []


def gather_depth_capture_alerts(
    *, depth_root: Path, now_ms: int, max_age_hours: float
) -> list[Alert]:
    """Freshness of the forward depth capture, gated on the operator having
    enabled the collector (round 4 — it was monitored by nothing). Mirrors the
    liquidation gather: a stale newest-JSONL mtime means capture stopped while
    the unit looks alive; Bybit has no historical book data, so every silent
    hour is deployed-venue capacity data lost forever."""
    if not depth_root.exists() or not _unit_enabled("liquidity-migration-depth-collector.service"):
        return []
    try:
        newest_mtime_ms = max(
            (int(p.stat().st_mtime * 1000) for p in depth_root.glob("*/*.jsonl")),
            default=0,
        )
    except OSError:
        newest_mtime_ms = 0
    if newest_mtime_ms <= 0:
        return []  # nothing ever captured (collector just enabled) — the unit check covers a dead service
    age_h = (now_ms - newest_mtime_ms) / 3_600_000.0
    if age_h > max_age_hours:
        return [Alert(
            key="depth_capture_stale",
            severity=WARNING,
            message=(
                f"depth capture STALE — newest JSONL {age_h:.1f}h old (> {max_age_hours:.0f}h) "
                f"with the collector enabled. Check its journal; Bybit book history is unbuyable."
            ),
        )]
    return []


def gather_continuous_alerts(
    *,
    continuous_root: Path,
    now_ms: int,
    args: argparse.Namespace,
    cycles_dataset: str = "continuous_fade_demo_cycles",
    trades_dataset: str = "continuous_fade_demo_trades",
    check_stops: bool = True,
    cycle_checks: bool = True,
) -> list[Alert]:
    """Per-sleeve liveness for the continuous-fade daemon: cycle-age (hung/down), rmom-gate freshness
    (the silent-blackout class), and server-side stop protection on its own open trades. The
    continuous sleeve writes a SEPARATE ledger root + an unpartitioned cycle dataset, so it needs
    its own gather. Paper mode passes the
    ``continuous_fade_paper_*`` datasets and skips live stop checks because it submits no orders.
    ``cycle_checks=False`` (sleeve toggled OFF) skips the daemon-liveness/rmom/universe checks —
    an off daemon is intentional — but stop/mismatch checks still run on any residual open rows,
    because "off" does NOT flatten: positions ride until natural exit and stayed unverified the
    moment the toggle flipped (audit 2026-06-12 round 3)."""
    if not continuous_root.exists():
        return []
    label = continuous_root.name
    alerts: list[Alert] = []
    if cycle_checks:
        try:
            cyc = read_dataset(continuous_root, cycles_dataset)
        except Exception:  # noqa: BLE001 — watchdog never crashes
            cyc = pl.DataFrame()
        latest_ts = (
            int(cyc.select(pl.col("ts_ms").max()).item())
            if (cyc is not None and not cyc.is_empty() and "ts_ms" in cyc.columns) else None
        )
        live = evaluate_cycle_liveness(
            latest_cycle_ts_ms=latest_ts, now_ms=now_ms, max_age_minutes=args.max_cycle_age_min, label=label
        )
        if live:
            alerts.append(live)
        if cyc is not None and not cyc.is_empty() and "ts_ms" in cyc.columns:
            row = cyc.sort("ts_ms").tail(1).to_dicts()[0]
            rmom_alert = evaluate_rmom_staleness(
                max_rmom_day_ts=int(row.get("max_rmom_day_ts") or 0), now_ms=now_ms,
                max_stale_days=args.max_rmom_stale_days, label=label,
            )
            if rmom_alert:
                alerts.append(rmom_alert)
            # Zero universe / empty kline store is the SAME silent-zero-signal failure as a stale rmom
            # gate but via a different upstream cause (discover/ingestion or WS-kline failure) the rmom
            # guard does not see -- both produce zero candidates that read like a quiet market.
            # Keys carry the {label} so the demo and paper gathers can't collide in
            # the cooldown map and suppress each other (audit 2026-06-12 round 3).
            universe_n = row.get("universe_symbols")
            if universe_n is not None and int(universe_n) == 0:
                alerts.append(Alert(
                    key=f"continuous_universe_empty:{label}", severity=WARNING,
                    message=f"{label}: continuous sleeve resolved an EMPTY universe (discover/ingestion failure?); zero candidates -- looks like a quiet market.",
                ))
            kline_rows = row.get("kline_store_rows")
            if kline_rows is not None and int(kline_rows) == 0:
                alerts.append(Alert(
                    key=f"continuous_kline_store_empty:{label}", severity=WARNING,
                    message=f"{label}: continuous sleeve WS kline store is EMPTY (kline_store_rows=0); zero candidates -- looks like a quiet market.",
                ))
    # Stop protection on the continuous sleeve's own open trades. Account-wide same-symbol exclusion
    # (Rule A) means each netted venue position maps to one sleeve, so the same check is valid here.
    try:
        tdf = read_dataset(continuous_root, trades_dataset)
        open_ct = (
            tdf.filter(pl.col("status") == "open").to_dicts()
            if (not tdf.is_empty() and "status" in tdf.columns) else []
        )
    except Exception:  # noqa: BLE001
        open_ct = []
    if check_stops and open_ct:
        positions, perr = _venue_positions(args.settle_coin)
        if perr is None:
            alerts.extend(evaluate_stop_protection(open_trades=open_ct, venue_positions=positions))
        else:
            # A creds/API failure must not silently leave
            # the continuous open positions unverified.
            alerts.append(Alert(
                key="stop_verify_unavailable_continuous", severity=WARNING,
                message=f"could not verify continuous stop protection ({perr}); {len(open_ct)} open trade(s) unchecked.",
            ))
    return alerts


def gather_long_alerts(
    *, long_root: Path, now_ms: int, args: argparse.Namespace, cycle_checks: bool = True
) -> list[Alert]:
    """Per-sleeve liveness for the long-native demo daemon: cycle-age (hung/down-but-active, which
    the systemd-failed check never catches under Restart=always), WS-staleness, and server-side stop
    protection on its own open trades. The long sleeve writes a SEPARATE ledger root +
    ``long_native_demo_cycles``/``long_native_demo_trades``.
    sees it. No rmom check (the long sleeve has no residual-momentum gate).
    ``cycle_checks=False`` (sleeve OFF) keeps the stop/mismatch checks on residual
    open rows — "off" does not flatten (audit 2026-06-12 round 3)."""
    if not long_root.exists():
        return []
    label = long_root.name
    alerts: list[Alert] = []
    if cycle_checks:
        try:
            cyc = read_dataset(long_root, "long_native_demo_cycles")
        except Exception:  # noqa: BLE001 — watchdog never crashes
            cyc = pl.DataFrame()
        latest_ts = (
            int(cyc.select(pl.col("ts_ms").max()).item())
            if (cyc is not None and not cyc.is_empty() and "ts_ms" in cyc.columns) else None
        )
        live = evaluate_cycle_liveness(
            latest_cycle_ts_ms=latest_ts, now_ms=now_ms, max_age_minutes=args.max_cycle_age_min, label=label
        )
        if live:
            alerts.append(live)
        if cyc is not None and not cyc.is_empty() and "kline_store_max_ts_ms" in cyc.columns:
            store_max = int(cyc.select(pl.col("kline_store_max_ts_ms").max()).item() or 0)
            ws = evaluate_ws_staleness(store_max_ts_ms=store_max, now_ms=now_ms, max_lag_hours=args.max_ws_lag_hours, label=label)
            if ws:
                alerts.append(ws)
    try:
        tdf = read_dataset(long_root, "long_native_demo_trades")
        open_ct = (
            tdf.filter(pl.col("status") == "open").to_dicts()
            if (not tdf.is_empty() and "status" in tdf.columns) else []
        )
    except Exception:  # noqa: BLE001
        open_ct = []
    if open_ct:
        positions, perr = _venue_positions(args.settle_coin)
        if perr is None:
            alerts.extend(evaluate_stop_protection(open_trades=open_ct, venue_positions=positions))
        else:
            alerts.append(Alert(
                key="stop_verify_unavailable_long", severity=WARNING,
                message=f"could not verify long-sleeve stop protection ({perr}); {len(open_ct)} open trade(s) unchecked.",
            ))
    return alerts


def build_arg_parser() -> argparse.ArgumentParser:
    """Exposed for the unit↔argparse parity test: the demo-liveness unit once
    passed an arg this script had dropped (--data-root, 2026-06-11 purge) and
    the watchdog crash-looped with only the VPS journal noticing."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--unit",
        action="append",
        default=None,
        help="systemd unit(s) to liveness-check (repeatable). Defaults to the core demo/paper units.",
    )
    p.add_argument("--max-cycle-age-min", type=float, default=10.0, help="alert if no cycle within this many minutes")
    p.add_argument("--max-ws-lag-hours", type=float, default=6.0, help="warn if the WS kline feed is this stale")
    # Roots stay str (NOT type=Path): argparse type=Path turns the documented '' skip
    # sentinel into Path('.') — truthy and existing — so the skip never skipped and the
    # gather ran against the repo CWD, paging a FALSE CRITICAL with an empty label.
    p.add_argument("--continuous-root", default="data/bybit-continuous-demo-event",
                   help="continuous-fade sleeve root for its per-sleeve cycle-age + rmom-freshness + stop checks ('' to skip)")
    p.add_argument("--continuous-paper-root", default="data/bybit-continuous-paper-event",
                   help="continuous-fade paper root for no-order evidence cycle-age + rmom-freshness checks ('' to skip)")
    p.add_argument("--long-root", default="data/bybit-long-demo-event",
                   help="long-native sleeve root for its per-sleeve cycle-age + WS-staleness + stop checks ('' to skip)")
    p.add_argument("--risk-root", default="data/bybit-demo-event",
                   help="ws_risk engine root for its heartbeat cycle-age check — the risk service has no sleeve toggle ('' to skip)")
    p.add_argument("--liquidations-root", default="data/liquidations",
                   help="forward liquidation-capture root for the newest-JSONL freshness check ('' to skip)")
    p.add_argument("--max-liquidation-age-hours", type=float, default=3.0,
                   help="warn if the newest captured liquidation JSONL is older than this")
    p.add_argument("--depth-root", default="data/depth",
                   help="forward depth-capture root for the newest-JSONL freshness check; "
                        "only checked when the depth collector unit is enabled ('' to skip)")
    p.add_argument("--max-depth-age-hours", type=float, default=3.0,
                   help="warn if the newest captured depth JSONL is older than this (enabled collector only)")
    p.add_argument("--max-rmom-stale-days", type=float, default=2.0,
                   help="alert if the continuous rmom signal gate's newest day is older than this (silent-blackout guard)")
    p.add_argument("--settle-coin", default="USDT", help="settle coin for the Bybit get_positions stop-protection check")
    p.add_argument("--cooldown-min", type=float, default=30.0, help="re-alert interval for a persisting condition")
    p.add_argument("--heartbeat-url", default=os.environ.get("LIVENESS_HEARTBEAT_URL") or None,
                   help="ping this URL on a healthy run (external dead-man's-switch); "
                        "defaults to the LIVENESS_HEARTBEAT_URL env var so the unit can wire it via EnvironmentFile")
    p.add_argument("--telegram", action="store_true", help="send alerts via Telegram (else stdout only)")
    p.add_argument("--state-file", type=Path, default=None, help="cooldown state file (default: <continuous-root>/.cache/liveness_watchdog.json)")
    return p


def main() -> int:
    args = build_arg_parser().parse_args()

    units = args.unit or _default_units_for_toggles()
    continuous_root = Path(args.continuous_root) if str(args.continuous_root).strip() else None
    continuous_paper_root = Path(args.continuous_paper_root) if str(args.continuous_paper_root).strip() else None
    long_root = Path(args.long_root) if str(args.long_root).strip() else None
    risk_root = Path(args.risk_root) if str(args.risk_root).strip() else None
    liquidations_root = Path(args.liquidations_root) if str(args.liquidations_root).strip() else None
    _state_root = continuous_root or long_root or Path("data")
    state_file = args.state_file or (_state_root / ".cache" / "liveness_watchdog.json")
    now_ms = _now_ms()

    # Per-sleeve kill-switch: skip an intentionally-off sleeve's DAEMON checks so a
    # deliberately-retired daemon doesn't false-page as "down" — but stop/mismatch
    # checks on residual open rows always run ("off" does not flatten). Unset-defaults
    # mirror deploy/lib_sleeves.sh: LONG off (round-3 fail-safe change), CONTINUOUS off.
    # (The daily-short sleeve was erased 2026-06-11.)
    alerts = evaluate_unit_states(_unit_states(units))
    if risk_root is not None:
        alerts.extend(gather_risk_alerts(risk_root=risk_root, now_ms=now_ms, args=args))
    if liquidations_root is not None:
        alerts.extend(gather_liquidation_capture_alerts(
            liquidations_root=liquidations_root, now_ms=now_ms,
            max_age_hours=args.max_liquidation_age_hours,
        ))
    depth_root = Path(args.depth_root) if str(args.depth_root).strip() else None
    if depth_root is not None:
        alerts.extend(gather_depth_capture_alerts(
            depth_root=depth_root, now_ms=now_ms,
            max_age_hours=args.max_depth_age_hours,
        ))
    if continuous_root is not None:
        alerts.extend(gather_continuous_alerts(
            continuous_root=continuous_root, now_ms=now_ms, args=args,
            cycle_checks=_sleeve_on("CONTINUOUS_SLEEVE", default="off"),
        ))
    if continuous_paper_root is not None and _sleeve_on("CONTINUOUS_PAPER_SLEEVE"):
        alerts.extend(
            gather_continuous_alerts(
                continuous_root=continuous_paper_root,
                now_ms=now_ms,
                args=args,
                cycles_dataset="continuous_fade_paper_cycles",
                trades_dataset="continuous_fade_paper_trades",
                check_stops=False,
            )
        )
    if long_root is not None:
        alerts.extend(gather_long_alerts(
            long_root=long_root, now_ms=now_ms, args=args,
            cycle_checks=_sleeve_on("LONG_SLEEVE"),
        ))
    state = _load_state(state_file)
    to_send, resolved, new_state = select_alerts_to_send(
        active=alerts, state=state, now_ms=now_ms, cooldown_minutes=args.cooldown_min
    )

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
                # send_telegram_message returns False (no exception) when the
                # TELEGRAM_* env is missing or the API answers non-2xx — previously
                # invisible AND recorded as sent, so a CRITICAL alert was suppressed
                # for the whole cooldown without ever reaching the operator (audit
                # 2026-06-12). Surface it and DON'T advance this alert's cooldown:
                # the next run retries.
                print("(telegram send returned False — TELEGRAM_* env missing or API non-2xx; will retry next run)")
                if alert.key in state:
                    new_state[alert.key] = state[alert.key]
                else:
                    new_state.pop(alert.key, None)
    for key in resolved:
        line = f"✅ liquidity-migration {ts}: resolved — {key}"
        print(line)
        if args.telegram:
            delivered = False
            try:
                delivered = send_telegram_message(line)
            except Exception as exc:  # noqa: BLE001
                print(f"(telegram send failed: {exc})")
            if not delivered and key in state:
                # Keep the key in state so the next run re-detects the resolution
                # and retries the note — a dropped "resolved" left the operator
                # believing the condition was still active (audit 2026-06-12 r3).
                new_state[key] = state[key]
                print("(telegram send returned False — resolved note will retry next run)")
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
