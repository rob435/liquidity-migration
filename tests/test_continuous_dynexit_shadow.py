"""Dynamic-exit forward-shadow tests (paper-only ledger; 2026-06-10 wiring)."""

from __future__ import annotations

import json

import polars as pl

from liquidity_migration.continuous_dynexit_shadow import (
    MS_H,
    SHADOW_FILE,
    compute_shadow_anchor,
    update_dynexit_shadow,
)

T0 = 1_700_000_000_000 - (1_700_000_000_000 % MS_H)


def _klines(symbol="AAAUSDT", n=30, base=100.0, lows=None, closes=None):
    rows = []
    for i in range(n):
        c = closes[i] if closes else base
        rows.append({"symbol": symbol, "ts_ms": T0 + i * MS_H, "open": c, "high": c * 1.01,
                     "low": (lows[i] if lows else c * 0.99), "close": c})
    return pl.DataFrame(rows)


def _entry(tid="t1", symbol="AAAUSDT", price=100.0, signal_i=25):
    return {"trade_id": tid, "symbol": symbol, "entry_price": price, "status": "open",
            "signal_ts_ms": T0 + signal_i * MS_H, "entry_ts_ms": T0 + (signal_i + 2) * MS_H}


def _read(path):
    return [json.loads(x) for x in (path / SHADOW_FILE).read_text(encoding="utf-8").splitlines() if x.strip()]


def test_anchor_uses_max_of_runup_and_ret1_clipped() -> None:
    closes = [100.0] * 30
    closes[25] = 120.0  # signal bar close: +20% vs 24h ago and vs 1h ago
    k = _klines(closes=closes)
    a = compute_shadow_anchor(k, symbol="AAAUSDT", signal_ts_ms=T0 + 25 * MS_H)
    assert abs(a - 0.20) < 1e-9
    # floor: a flat tape clips to 0.03
    a2 = compute_shadow_anchor(_klines(), symbol="AAAUSDT", signal_ts_ms=T0 + 25 * MS_H)
    assert abs(a2 - 0.03) < 1e-9
    assert compute_shadow_anchor(k, symbol="MISSING", signal_ts_ms=T0 + 25 * MS_H) is None


def test_arm_then_dyn_tp_exit_on_low_touch(tmp_path) -> None:
    closes = [100.0] * 30
    closes[25] = 120.0
    k = _klines(closes=closes)
    e = _entry()
    trades = pl.DataFrame([e], infer_schema_length=None)
    st = update_dynexit_shadow(tmp_path, all_trades=trades, fresh_entries=[e], klines=k,
                               now_ms=T0 + 27 * MS_H)
    assert st["armed"] == 1 and st["dyn_tp_exits"] == 0
    rows = _read(tmp_path)
    assert rows[0]["event"] == "arm"
    target = rows[0]["target_price"]
    assert abs(target - 100.0 * (1 - 0.5 * 0.20)) < 1e-9  # 90.0
    # next cycle: a bar low touches the target
    lows = [c * 0.99 for c in closes] + [89.0]
    closes2 = closes + [95.0]
    k2 = _klines(n=31, closes=closes2, lows=lows)
    st2 = update_dynexit_shadow(tmp_path, all_trades=trades, fresh_entries=[], klines=k2,
                                now_ms=T0 + 31 * MS_H)
    assert st2["dyn_tp_exits"] == 1
    last = _read(tmp_path)[-1]
    assert last["event"] == "shadow_exit" and last["reason"] == "dyn_tp"
    assert abs(last["exit_price"] - target) < 1e-9
    assert abs(last["shadow_ret"] - 0.10) < 1e-9


def test_real_exit_closes_shadow(tmp_path) -> None:
    closes = [100.0] * 30
    closes[25] = 120.0
    k = _klines(closes=closes)
    e = _entry()
    update_dynexit_shadow(tmp_path, all_trades=pl.DataFrame([e], infer_schema_length=None),
                          fresh_entries=[e], klines=k, now_ms=T0 + 27 * MS_H)
    closed = pl.DataFrame([{**e, "status": "closed", "exit_price": 97.0,
                            "exit_ts_ms": T0 + 28 * MS_H}], infer_schema_length=None)
    st = update_dynexit_shadow(tmp_path, all_trades=closed, fresh_entries=[], klines=k,
                               now_ms=T0 + 29 * MS_H)
    assert st["real_exits"] == 1
    last = _read(tmp_path)[-1]
    assert last["reason"] == "real_exit" and abs(last["exit_price"] - 97.0) < 1e-9


def test_idempotent_no_duplicate_events(tmp_path) -> None:
    closes = [100.0] * 30
    closes[25] = 120.0
    k = _klines(closes=closes)
    e = _entry()
    trades = pl.DataFrame([e], infer_schema_length=None)
    update_dynexit_shadow(tmp_path, all_trades=trades, fresh_entries=[e], klines=k, now_ms=T0 + 27 * MS_H)
    update_dynexit_shadow(tmp_path, all_trades=trades, fresh_entries=[e], klines=k, now_ms=T0 + 28 * MS_H)
    rows = _read(tmp_path)
    assert sum(1 for r in rows if r["event"] == "arm") == 1
