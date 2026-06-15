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


def test_anchor_requires_both_refs_no_ret1_only_arm() -> None:
    # Signal + 1h-ago bars exist, but the 24h-ago bar is missing (cold start /
    # gap). The anchor is clip(max(runup24h, ret1)); arming on ret1 alone would
    # shadow a different exit, so it must NOT arm (counted, not imputed).
    sig = T0 + 25 * MS_H
    k = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "ts_ms": sig - MS_H, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
            {"symbol": "AAAUSDT", "ts_ms": sig, "open": 120.0, "high": 121.0, "low": 119.0, "close": 120.0},
        ]
    )
    assert compute_shadow_anchor(k, symbol="AAAUSDT", signal_ts_ms=sig) is None


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


# ---------------------------------------------------------------------------
# audit2
# [0] dyn-exit shadow: "whichever first" — a target touched AFTER the real exit
# must NOT count as a dyn_tp (look-ahead vs the frozen pre-registration).
# ---------------------------------------------------------------------------

def _write_arm(root, *, target_price: float, entry_i: int) -> None:
    rec = {
        "event": "arm",
        "trade_id": "t1",
        "symbol": "AAAUSDT",
        "target_price": target_price,
        "entry_ts_ms": T0 + entry_i * MS_H,
        "entry_price": 100.0,
    }
    (root / SHADOW_FILE).write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _closed_trade_df(*, exit_i: int, exit_price: float) -> pl.DataFrame:
    return pl.DataFrame(
        [{
            "symbol": "AAAUSDT",
            "status": "closed",
            "trade_id": "t1",
            "exit_price": exit_price,
            "exit_ts_ms": T0 + exit_i * MS_H,
        }]
    )


def _klines_with_low_touch(touch_i: int, *, low: float = 94.0) -> pl.DataFrame:
    rows = []
    for i in range(25):
        rows.append({
            "symbol": "AAAUSDT",
            "ts_ms": T0 + i * MS_H,
            "low": low if i == touch_i else 100.0,
        })
    return pl.DataFrame(rows)


def _read_exits(root):
    text = (root / SHADOW_FILE).read_text(encoding="utf-8")
    return [json.loads(x) for x in text.splitlines() if x.strip() and json.loads(x).get("event") == "shadow_exit"]


def test_dynexit_target_touch_after_real_exit_is_capped(tmp_path) -> None:
    # Real trade force-closed at i=10 (price 99). The dyn target (95) is only touched
    # at i=15 — AFTER the position was really closed. Must fall through to real_exit.
    _write_arm(tmp_path, target_price=95.0, entry_i=7)
    update_dynexit_shadow(
        tmp_path,
        all_trades=_closed_trade_df(exit_i=10, exit_price=99.0),
        fresh_entries=[],
        klines=_klines_with_low_touch(touch_i=15),
        now_ms=T0 + 20 * MS_H,
    )
    exits = _read_exits(tmp_path)
    assert len(exits) == 1
    assert exits[0]["reason"] == "real_exit"  # NOT dyn_tp (was the look-ahead)
    assert exits[0]["exit_price"] == 99.0


def test_dynexit_target_touch_before_real_exit_still_fires(tmp_path) -> None:
    # Control: the target is touched at i=8, BEFORE the real exit at i=10 -> dyn_tp.
    _write_arm(tmp_path, target_price=95.0, entry_i=7)
    update_dynexit_shadow(
        tmp_path,
        all_trades=_closed_trade_df(exit_i=10, exit_price=99.0),
        fresh_entries=[],
        klines=_klines_with_low_touch(touch_i=8),
        now_ms=T0 + 20 * MS_H,
    )
    exits = _read_exits(tmp_path)
    assert len(exits) == 1
    assert exits[0]["reason"] == "dyn_tp"
    assert exits[0]["exit_price"] == 95.0
