#!/usr/bin/env python3
"""Fast liveness + safety watchdog for the live demo book.

Complements ``check_demo_entry_health.py`` (which answers "is the strategy
*firing*?", hourly). This watchdog answers "is the system ALIVE and are open
positions PROTECTED?" and runs every few minutes so the operator can manually
close positions when something breaks. It Telegrams on:

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
  * EXCHANGE ERRORS -- recent cycles reporting position/order snapshot errors.

Alerts are de-duplicated with a cooldown state file: a new condition alerts
immediately, a persisting one re-alerts at most every --cooldown-min, and a
cleared one sends a one-line "resolved" note. An optional --heartbeat-url is
pinged on every healthy run so an EXTERNAL dead-man's-switch (e.g.
healthchecks.io) catches a total box death the on-box watchdog cannot.

Reads TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID and (for the stop-protection check)
the Bybit demo creds from the daemon environment. Exits 0 always (a watchdog
must not crash-loop); failures to verify degrade to a warning alert.
"""
from __future__ import annotations

import argparse
import json
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
    """A monitored systemd unit not in {active} -> alert. ``activating`` (the
    Restart=always recovery window) is tolerated; ``failed``/``inactive`` are not."""
    alerts: list[Alert] = []
    for unit, state in sorted(unit_states.items()):
        if state not in {"active", "activating"}:
            alerts.append(
                Alert(
                    key=f"unit:{unit}",
                    severity=CRITICAL,
                    message=f"systemd unit {unit} is '{state}' (expected active).",
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


def evaluate_exchange_errors(*, recent: list[dict], label: str) -> list[Alert]:
    """Recent cycles reporting position/order snapshot errors or fill failures."""
    pos_errs = [str(r.get("position_report_error") or "") for r in recent]
    pos_errs = [e for e in pos_errs if e]
    fill_errs = sum(int(r.get("pending_order_fill_errors") or 0) for r in recent)
    alerts: list[Alert] = []
    if pos_errs:
        alerts.append(
            Alert(
                key=f"exch_pos_err:{label}",
                severity=WARNING,
                message=f"{label}: position-snapshot errors in recent cycles: {pos_errs[-1]}",
            )
        )
    if fill_errs > 0:
        alerts.append(
            Alert(
                key=f"exch_fill_err:{label}",
                severity=WARNING,
                message=f"{label}: {fill_errs} order-fill reconciliation error(s) in recent cycles.",
            )
        )
    return alerts


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


def _open_trades(data_root: Path) -> list[dict]:
    try:
        df = read_dataset(data_root, "event_demo_trades")
    except Exception:  # noqa: BLE001
        return []
    if df.is_empty() or "status" not in df.columns:
        return []
    return df.filter(pl.col("status") == "open").to_dicts()


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


def _venue_positions() -> tuple[dict[str, dict], str | None]:
    """Return (positions_by_symbol, error). Degrades gracefully if creds/API down."""
    try:
        from liquidity_migration.bybit import BybitPrivateClient, resolve_private_credentials

        api_key, api_secret, demo = resolve_private_credentials()
        if not api_key or not api_secret:
            return {}, "no demo API creds in environment"
        client = BybitPrivateClient(api_key=api_key, api_secret=api_secret, demo=demo)
        raw = client.get_positions()
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


def gather_alerts(*, data_root: Path, units: list[str], now_ms: int, args: argparse.Namespace) -> list[Alert]:
    label = data_root.name
    alerts: list[Alert] = []

    cycles_root = data_root / "event_demo_cycles"
    df = _latest_cycle_df(cycles_root)
    latest_ts = int(df.select(pl.col("ts_ms").max()).item()) if df is not None and not df.is_empty() else None
    live = evaluate_cycle_liveness(
        latest_cycle_ts_ms=latest_ts, now_ms=now_ms, max_age_minutes=args.max_cycle_age_min, label=label
    )
    if live:
        alerts.append(live)

    alerts.extend(evaluate_unit_states(_unit_states(units)))

    open_trades = _open_trades(data_root)
    if open_trades:
        positions, perr = _venue_positions()
        if perr is not None:
            alerts.append(
                Alert(
                    key="stop_verify_unavailable",
                    severity=WARNING,
                    message=f"could not verify stop protection ({perr}); {len(open_trades)} open trade(s) unchecked.",
                )
            )
        else:
            alerts.extend(evaluate_stop_protection(open_trades=open_trades, venue_positions=positions))

    if df is not None and not df.is_empty():
        store_max = int(df.select(pl.col("kline_store_max_ts_ms").max()).item() or 0) if "kline_store_max_ts_ms" in df.columns else None
        ws = evaluate_ws_staleness(store_max_ts_ms=store_max, now_ms=now_ms, max_lag_hours=args.max_ws_lag_hours, label=label)
        if ws:
            alerts.append(ws)
        alerts.extend(evaluate_exchange_errors(recent=df.tail(20).to_dicts(), label=label))

    return alerts


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, default=Path("data/bybit-demo-event"))
    p.add_argument(
        "--unit",
        action="append",
        default=None,
        help="systemd unit(s) to liveness-check (repeatable). Defaults to the 5 core demo units.",
    )
    p.add_argument("--max-cycle-age-min", type=float, default=10.0, help="alert if no cycle within this many minutes")
    p.add_argument("--max-ws-lag-hours", type=float, default=6.0, help="warn if the WS kline feed is this stale")
    p.add_argument("--cooldown-min", type=float, default=30.0, help="re-alert interval for a persisting condition")
    p.add_argument("--heartbeat-url", default=None, help="ping this URL on a healthy run (external dead-man's-switch)")
    p.add_argument("--telegram", action="store_true", help="send alerts via Telegram (else stdout only)")
    p.add_argument("--state-file", type=Path, default=None, help="cooldown state file (default: <data-root>/.cache/liveness_watchdog.json)")
    args = p.parse_args()

    units = args.unit or [
        "liquidity-migration-bybit-demo.service",
        "liquidity-migration-bybit-risk.service",
        "liquidity-migration-bybit-paper.service",
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-long-paper.service",
    ]
    state_file = args.state_file or (args.data_root / ".cache" / "liveness_watchdog.json")
    now_ms = _now_ms()

    alerts = gather_alerts(data_root=args.data_root, units=units, now_ms=now_ms, args=args)
    state = _load_state(state_file)
    to_send, resolved, new_state = select_alerts_to_send(
        active=alerts, state=state, now_ms=now_ms, cooldown_minutes=args.cooldown_min
    )
    _save_state(state_file, new_state)

    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    for alert in to_send:
        line = f"🚨 [{alert.severity}] liquidity-migration {ts}\n{alert.message}"
        print(line)
        if args.telegram:
            try:
                send_telegram_message(line)
            except Exception as exc:  # noqa: BLE001
                print(f"(telegram send failed: {exc})")
    for key in resolved:
        line = f"✅ liquidity-migration {ts}: resolved — {key}"
        print(line)
        if args.telegram:
            try:
                send_telegram_message(line)
            except Exception as exc:  # noqa: BLE001
                print(f"(telegram send failed: {exc})")

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
