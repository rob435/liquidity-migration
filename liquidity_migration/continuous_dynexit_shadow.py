"""Forward paper-shadow of the fade-completion dynamic exit (P5 follow-up, 2026-06-10).

The dynamic exit (tp = entry*(1-0.5*anchor)) was a pre-registered in-sample NULL —
a bybit-only, 2026-carried historical in-sample result.
Per the forward-shadow receipt (continuous-dynexit-forward-shadow-2026-06-10.md)
the ONLY legitimate way it can ever be adopted is FORWARD evidence: this module
computes, per live entry, what the dynamic exit WOULD have done, and logs it to an
append-only JSONL ledger. It submits nothing, modifies nothing, and is safe to run
on every cycle.

Events (one JSON object per line):
  arm         — entry observed; anchor/target computed causally at the signal bar
  shadow_exit — the shadow position closed (dyn_tp: target touched by a bar low;
                real_exit: the real trade closed first — shadow closes at the real
                exit price, exactly the replay convention)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl

SHADOW_FILE = "continuous_dynexit_shadow.jsonl"
F_TARGET = 0.5
ANCHOR_LO, ANCHOR_HI = 0.03, 0.60
MS_H = 3_600_000


def _read_state(path: Path) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Replay the JSONL into (armed_by_trade_id, exited_trade_ids)."""
    armed: dict[str, dict[str, Any]] = {}
    exited: set[str] = set()
    if not path.exists():
        return armed, exited
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = str(row.get("trade_id", ""))
            if row.get("event") == "arm":
                armed[tid] = row
            elif row.get("event") == "shadow_exit":
                exited.add(tid)
    return armed, exited


def _append(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def compute_shadow_anchor(
    klines: pl.DataFrame | None, *, symbol: str, signal_ts_ms: int
) -> float | None:
    """anchor = clip(max(runup24h, ret1), lo, hi) at the signal bar, causal.

    Uses the WS kline cache closes: the signal close is the close of the bar opening
    at ``signal_ts_ms``; runup24h/ret1 read 24h/1h earlier closes. None when the
    cache lacks the needed bars (shadow simply doesn't arm — counted, not imputed).
    """
    if klines is None or klines.is_empty():
        return None
    sub = (
        klines.filter(
            (pl.col("symbol") == symbol)
            & (pl.col("ts_ms") >= signal_ts_ms - 25 * MS_H)
            & (pl.col("ts_ms") <= signal_ts_ms)
        )
        .sort("ts_ms")
        .select(["ts_ms", "close"])
    )
    if sub.is_empty():
        return None
    ts = sub["ts_ms"].to_list()
    close = sub["close"].to_list()

    def at(t: int) -> float | None:
        best = None
        for i, x in enumerate(ts):
            if x <= t:
                best = float(close[i])
            else:
                break
        return best if best and best > 0 else None

    c_sig = at(signal_ts_ms)
    c_24 = at(signal_ts_ms - 24 * MS_H)
    c_1 = at(signal_ts_ms - MS_H)
    # The dynamic exit being shadowed is defined as anchor = clip(max(runup24h,
    # ret1)). Require BOTH reference closes (and the signal close): arming on ret1
    # ALONE when the 24h bar is missing would shadow a DIFFERENT anchor and bias
    # the forward statistic. Matches the documented "doesn't arm when the cache
    # lacks the needed bars" contract (counted, not imputed).
    if not (c_sig and c_24 and c_1):
        return None
    runup24h = c_sig / c_24 - 1.0
    ret1 = c_sig / c_1 - 1.0
    return min(max(max(runup24h, ret1), ANCHOR_LO), ANCHOR_HI)


def _arm_entry(
    entry: dict[str, Any],
    *,
    armed: dict[str, dict[str, Any]],
    exited: set[str],
    klines: pl.DataFrame | None,
    now_ms: int,
    out: list[dict[str, Any]],
    stats: dict[str, int],
) -> None:
    """Arm one entry row (fresh exec_entry OR a recovered open ledger trade).

    Idempotent: a trade already in ``armed``/``exited`` or lacking the causal
    inputs is skipped. On a successful arm the row is appended to ``out`` (to be
    flushed to the JSONL) and registered in ``armed`` so duplicate sources in the
    same cycle can't double-arm.
    """
    tid = str(entry.get("trade_id", ""))
    if not tid or tid in armed or tid in exited:
        return
    symbol = str(entry.get("symbol", ""))
    entry_price = float(entry.get("entry_price") or 0.0)
    signal_ts = int(entry.get("signal_ts_ms") or 0)
    if not symbol or entry_price <= 0.0 or signal_ts <= 0:
        return
    anchor = compute_shadow_anchor(klines, symbol=symbol, signal_ts_ms=signal_ts)
    if anchor is None:
        stats["no_anchor"] += 1
        return
    row = {
        "event": "arm", "trade_id": tid, "symbol": symbol, "ts_ms": now_ms,
        "entry_ts_ms": int(entry.get("entry_ts_ms") or now_ms), "entry_price": entry_price,
        "signal_ts_ms": signal_ts, "anchor": round(anchor, 6), "f": F_TARGET,
        "target_price": entry_price * (1.0 - F_TARGET * anchor),
    }
    armed[tid] = row
    out.append(row)
    stats["armed"] += 1


def update_dynexit_shadow(
    root: Path,
    *,
    all_trades: pl.DataFrame,
    fresh_entries: list[dict[str, Any]],
    klines: pl.DataFrame | None,
    now_ms: int,
) -> dict[str, int]:
    """One per-cycle shadow sweep (pure bookkeeping; no orders).

    Arms fresh entries, exits armed shadows on a bar-low touch of the dynamic
    target (fill AT the target — the engine's TP convention), and closes shadows
    at the real exit when the real trade closed first.
    """
    path = Path(root) / SHADOW_FILE
    armed, exited = _read_state(path)
    out: list[dict[str, Any]] = []
    stats = {"armed": 0, "dyn_tp_exits": 0, "real_exits": 0, "no_anchor": 0}

    for entry in fresh_entries:
        _arm_entry(entry, armed=armed, exited=exited, klines=klines, now_ms=now_ms, out=out, stats=stats)

    # shadows-2: arming off the fresh-entries stream ALONE loses any arm whose
    # JSONL line was torn by a mid-write crash — that trade is no longer "fresh"
    # next cycle (it is already open in the ledger, not a new exec_entry), and
    # _read_state silently drops the truncated line, so it would never re-arm and
    # its shadow observation is lost forever (biased toward high-write/high-vol
    # cycles). Make arm recovery independent of the fresh stream: scan the open
    # ledger trades for this sleeve and arm any not yet armed/exited from the row
    # itself (entry_price/signal_ts_ms are present). Idempotent and self-healing
    # for historical gaps; the kline cache simply may not reach far enough back for
    # an old trade (counted as no_anchor, not imputed), exactly the live contract.
    if not all_trades.is_empty() and "status" in all_trades.columns:
        open_trades = all_trades.filter(pl.col("status") == "open")
        if not open_trades.is_empty():
            for trade in open_trades.to_dicts():
                _arm_entry(trade, armed=armed, exited=exited, klines=klines, now_ms=now_ms, out=out, stats=stats)

    closed_real: dict[str, dict[str, Any]] = {}
    if not all_trades.is_empty() and "status" in all_trades.columns:
        closed = all_trades.filter(pl.col("status") == "closed")
        if not closed.is_empty():
            closed_real = {str(t.get("trade_id", "")): t for t in closed.to_dicts()}

    for tid, arm in armed.items():
        if tid in exited:
            continue
        symbol = str(arm.get("symbol", ""))
        target = float(arm.get("target_price") or 0.0)
        entry_ts = int(arm.get("entry_ts_ms") or 0)
        hit_ts: int | None = None
        if klines is not None and not klines.is_empty() and target > 0.0:
            sub = (
                klines.filter(
                    (pl.col("symbol") == symbol)
                    & (pl.col("ts_ms") >= entry_ts)
                    & (pl.col("ts_ms") <= now_ms)
                    & (pl.col("low") <= target)
                )
                .sort("ts_ms")
            )
            if not sub.is_empty():
                hit_ts = int(sub["ts_ms"][0]) + MS_H
        if hit_ts is not None:
            out.append({
                "event": "shadow_exit", "trade_id": tid, "symbol": symbol, "ts_ms": now_ms,
                "exit_ts_ms": hit_ts, "exit_price": target, "reason": "dyn_tp",
                "shadow_ret": (float(arm["entry_price"]) - target) / float(arm["entry_price"]),
            })
            exited.add(tid)
            stats["dyn_tp_exits"] += 1
        elif tid in closed_real:
            real = closed_real[tid]
            exit_price = float(real.get("exit_price") or 0.0)
            if exit_price > 0.0:
                out.append({
                    "event": "shadow_exit", "trade_id": tid, "symbol": symbol, "ts_ms": now_ms,
                    "exit_ts_ms": int(real.get("exit_ts_ms") or now_ms), "exit_price": exit_price,
                    "reason": "real_exit",
                    "shadow_ret": (float(arm["entry_price"]) - exit_price) / float(arm["entry_price"]),
                })
                exited.add(tid)
                stats["real_exits"] += 1

    _append(path, out)
    return stats
