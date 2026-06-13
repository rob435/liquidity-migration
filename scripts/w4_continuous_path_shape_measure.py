#!/usr/bin/env python3
"""W4 Stage 3 fixed path-shape measurements for the continuous book."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import statistics as st
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.continuous_forward_replay import frozen_config_hash  # noqa: E402
from scripts.w4_continuous_stop_exit_realism import _pit_partition_gate  # noqa: E402


SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
STAGE1_TAG = "w4_continuous_stage1_stop_exit_2026-06-13"
SWEEP_TAG = "w4_continuous_stage3_path_shape_2026-06-13"
COMPONENTS = ("turn3p3", "turn4p3", "turn4p5", "age210tp14")
CAUSAL_FEATURES = (
    "pre_6h_return",
    "pre_24h_return",
    "pre_6h_upper_extension",
    "pre_24h_realized_vol",
    "signal_bar_close_location",
)
DIAGNOSTIC_FEATURES = ("post_6h_adverse", "post_6h_favorable")
NEGATIVE_CONTROL = "symbol_hash_bucket"
MS_PER_HOUR = 3_600_000


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _code_hash() -> str:
    paths = [
        REPO / "scripts" / "w4_continuous_path_shape_measure.py",
        REPO / "scripts" / "w4_continuous_stop_exit_realism.py",
        REPO / "liquidity_migration" / "continuous_events.py",
        REPO / "liquidity_migration" / "continuous_forward_replay.py",
    ]
    payload = "|".join(f"{p.relative_to(REPO)}:{_sha256_file(p)}" for p in paths)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dates(lo_ms: int, hi_ms: int) -> list[str]:
    d0 = dt.datetime.fromtimestamp(lo_ms / 1000, tz=dt.timezone.utc).date()
    d1 = dt.datetime.fromtimestamp(hi_ms / 1000, tz=dt.timezone.utc).date()
    out: list[str] = []
    day = d0
    while day <= d1:
        out.append(day.isoformat())
        day += dt.timedelta(days=1)
    return out


@lru_cache(maxsize=300_000)
def _bars(venue: str, symbol: str, date: str) -> tuple[tuple[int, float, float, float], ...]:
    part = ROOTS[venue] / "klines_1h" / f"date={date}" / f"symbol={symbol}"
    if not part.exists():
        return ()
    df = pl.read_parquet(part, columns=["ts_ms", "high", "low", "close"]).sort("ts_ms")
    return tuple((int(ts), float(high), float(low), float(close)) for ts, high, low, close in df.iter_rows())


def _base_trade_paths(root: Path) -> dict[str, Path]:
    return {
        component: root / "reports" / STAGE1_TAG / "00_frozen_no_stop" / component / "continuous_trades.csv"
        for component in COMPONENTS
    }


def _load_base_trades(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for component, path in _base_trade_paths(root).items():
        if not path.exists():
            raise FileNotFoundError(f"missing Stage 1 control trades: {path}")
        for row in pl.read_csv(path).to_dicts():
            row["component"] = component
            rows.append(row)
    return rows


def _rank(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i + 1
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg = (i + j - 1) / 2.0
        for k in range(i, j):
            ranks[indexed[k][0]] = avg
        i = j
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(ys) < 3:
        return None
    mx, my = st.mean(xs), st.mean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0.0 or vy <= 0.0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / math.sqrt(vx * vy)


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    return _pearson(_rank(xs), _rank(ys))


def _spread_bps(rows: list[dict[str, Any]], feature: str) -> dict[str, Any]:
    clean = [
        row for row in rows
        if row.get(feature) is not None and row.get("return_per_notional") is not None
    ]
    clean.sort(key=lambda row: float(row[feature]))
    n = len(clean)
    if n < 9:
        return {"covered": n, "spread_bps": None, "bottom_mean_bps": None, "top_mean_bps": None}
    k = max(n // 3, 1)
    bottom = [float(row["return_per_notional"]) for row in clean[:k]]
    top = [float(row["return_per_notional"]) for row in clean[-k:]]
    b_mean = st.mean(bottom) * 10_000.0
    t_mean = st.mean(top) * 10_000.0
    return {"covered": n, "spread_bps": t_mean - b_mean, "bottom_mean_bps": b_mean, "top_mean_bps": t_mean}


def _effect_row(rows: list[dict[str, Any]], *, venue: str, component: str, feature: str) -> dict[str, Any]:
    clean = [
        row for row in rows
        if row.get(feature) is not None and row.get("return_per_notional") is not None
    ]
    xs = [float(row[feature]) for row in clean]
    ys = [float(row["return_per_notional"]) for row in clean]
    spread = _spread_bps(clean, feature)
    return {
        "venue": venue,
        "component": component,
        "feature": feature,
        "kind": (
            "causal" if feature in CAUSAL_FEATURES
            else "diagnostic" if feature in DIAGNOSTIC_FEATURES
            else "negative_control"
        ),
        "covered": len(clean),
        "pearson_ic": _pearson(xs, ys),
        "spearman_ic": _spearman(xs, ys),
        **spread,
    }


def _third_rows(rows: list[dict[str, Any]], feature: str) -> list[dict[str, Any]]:
    clean = [row for row in rows if row.get(feature) is not None]
    clean.sort(key=lambda row: int(row["entry_ts_ms"]))
    n = len(clean)
    out = []
    for idx in range(3):
        lo = idx * n // 3
        hi = (idx + 1) * n // 3
        part = clean[lo:hi]
        spread = _spread_bps(part, feature)
        out.append({"third": idx + 1, "feature": feature, **spread})
    return out


def _value_at_or_before(bars: list[tuple[int, float, float, float]], ts_ms: int) -> tuple[int, float, float, float] | None:
    best = None
    for bar in bars:
        if bar[0] <= ts_ms:
            best = bar
        else:
            break
    return best


def _symbol_hash_bucket(symbol: str) -> float:
    raw = int(hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:8], 16) % 1000
    return raw / 999.0


def _features_for_trade(venue: str, trade: dict[str, Any]) -> dict[str, Any]:
    symbol = str(trade["symbol"])
    signal_ts = int(trade["entry_signal_ts_ms"])
    entry_ts = int(trade["entry_ts_ms"])
    entry_price = float(trade["entry_price"])
    lo_ms = signal_ts - 30 * MS_PER_HOUR
    hi_ms = max(signal_ts, entry_ts + 6 * MS_PER_HOUR)
    bars: list[tuple[int, float, float, float]] = []
    for date in _dates(lo_ms, hi_ms):
        bars.extend(_bars(venue, symbol, date))
    bars.sort(key=lambda item: item[0])

    signal_bar = _value_at_or_before(bars, signal_ts)
    close_6 = _value_at_or_before(bars, signal_ts - 6 * MS_PER_HOUR)
    close_24 = _value_at_or_before(bars, signal_ts - 24 * MS_PER_HOUR)
    signal_close = signal_bar[3] if signal_bar else None
    features: dict[str, Any] = {
        "pre_6h_return": None,
        "pre_24h_return": None,
        "pre_6h_upper_extension": None,
        "pre_24h_realized_vol": None,
        "signal_bar_close_location": None,
        "post_6h_adverse": None,
        "post_6h_favorable": None,
        "symbol_hash_bucket": _symbol_hash_bucket(symbol),
    }
    if signal_close and signal_close > 0:
        if close_6 and close_6[3] > 0:
            features["pre_6h_return"] = signal_close / close_6[3] - 1.0
        if close_24 and close_24[3] > 0:
            features["pre_24h_return"] = signal_close / close_24[3] - 1.0
        prior6 = [bar for bar in bars if signal_ts - 6 * MS_PER_HOUR <= bar[0] <= signal_ts]
        if prior6:
            features["pre_6h_upper_extension"] = max(bar[1] for bar in prior6) / signal_close - 1.0
        prior24 = [bar for bar in bars if signal_ts - 24 * MS_PER_HOUR <= bar[0] <= signal_ts]
        closes = [bar[3] for bar in prior24 if bar[3] > 0]
        rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]
        if len(rets) >= 6:
            features["pre_24h_realized_vol"] = st.pstdev(rets)
        high, low, close = signal_bar[1], signal_bar[2], signal_bar[3]
        if high > low:
            features["signal_bar_close_location"] = (close - low) / (high - low)

    post6 = [bar for bar in bars if entry_ts < bar[0] <= entry_ts + 6 * MS_PER_HOUR]
    if post6 and entry_price > 0:
        features["post_6h_adverse"] = max(bar[1] for bar in post6) / entry_price - 1.0
        features["post_6h_favorable"] = entry_price / min(bar[2] for bar in post6) - 1.0
    return features


def _event_rows(venue: str, root: Path) -> list[dict[str, Any]]:
    events = []
    for trade in _load_base_trades(root):
        nw = abs(float(trade["notional_weight"]))
        row = {
            "venue": venue,
            "component": trade["component"],
            "trade_id": trade["trade_id"],
            "symbol": trade["symbol"],
            "entry_signal_ts_ms": int(trade["entry_signal_ts_ms"]),
            "entry_ts_ms": int(trade["entry_ts_ms"]),
            "exit_ts_ms": int(trade["exit_ts_ms"]),
            "notional_weight": nw,
            "net_return": float(trade["net_return"]),
            "return_per_notional": float(trade["net_return"]) / max(nw, 1e-12),
        }
        row.update(_features_for_trade(venue, trade))
        events.append(row)
    return events


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _decision(effect_rows: list[dict[str, Any]], third_rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_feature = {feature: {} for feature in CAUSAL_FEATURES}
    for row in effect_rows:
        if row["feature"] in by_feature and row["component"] == "all":
            by_feature[row["feature"]][row["venue"]] = row
    neg = next(
        row for row in effect_rows
        if row["feature"] == NEGATIVE_CONTROL and row["venue"] == "pooled" and row["component"] == "all"
    )
    neg_abs = abs(float(neg["spread_bps"] or 0.0))
    admissible = []
    reasons: dict[str, list[str]] = {}
    for feature in CAUSAL_FEATURES:
        rs = by_feature[feature]
        fails: list[str] = []
        bybit, binance, pooled = rs.get("bybit"), rs.get("binance"), rs.get("pooled")
        if not bybit or not binance or not pooled:
            fails.append("missing venue row")
            reasons[feature] = fails
            continue
        if int(bybit["covered"]) < 500 or int(binance["covered"]) < 500:
            fails.append("coverage <500 on a venue")
        sp_by, sp_bn = float(bybit["spearman_ic"] or 0.0), float(binance["spearman_ic"] or 0.0)
        spread_by, spread_bn = float(bybit["spread_bps"] or 0.0), float(binance["spread_bps"] or 0.0)
        if sp_by == 0.0 or sp_bn == 0.0 or (sp_by > 0) != (sp_bn > 0):
            fails.append("spearman sign mismatch")
        if spread_by == 0.0 or spread_bn == 0.0 or (spread_by > 0) != (spread_bn > 0):
            fails.append("spread sign mismatch")
        pooled_spread = float(pooled["spread_bps"] or 0.0)
        if abs(pooled_spread) < 25.0:
            fails.append("pooled spread <25bps")
        dir_positive = pooled_spread > 0
        thirds = [
            row for row in third_rows
            if row["feature"] == feature and row["venue"] == "pooled" and row["component"] == "all"
        ]
        same_dir = sum(
            1 for row in thirds
            if row["spread_bps"] is not None and ((float(row["spread_bps"]) > 0) == dir_positive)
        )
        if same_dir < 2:
            fails.append("fewer than two thirds same direction")
        if abs(pooled_spread) <= neg_abs:
            fails.append("not stronger than negative control")
        if fails:
            reasons[feature] = fails
        else:
            admissible.append(feature)
            reasons[feature] = ["admissible"]
    return {
        "admissible_features": admissible,
        "feature_reasons": reasons,
        "negative_control_abs_spread_bps": neg_abs,
    }


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# W4 Continuous Stage 3 Path-Shape Measurement",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`",
        f"- Git HEAD: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`",
        f"- Frozen forward config hash: `{payload['frozen_forward_config_hash']}`",
        "",
        "## Causal Feature Summary",
        "",
        "| Feature | Bybit Spearman | Binance Spearman | Pooled Spread Bps | Decision |",
        "|---|---:|---:|---:|---|",
    ]
    by_feature = {feature: {} for feature in CAUSAL_FEATURES}
    for row in payload["effect_rows"]:
        if row["feature"] in by_feature and row["component"] == "all":
            by_feature[row["feature"]][row["venue"]] = row
    for feature in CAUSAL_FEATURES:
        rows = by_feature[feature]
        by = rows.get("bybit", {})
        bn = rows.get("binance", {})
        pooled = rows.get("pooled", {})
        reasons = "; ".join(payload["decision"]["feature_reasons"].get(feature, []))
        lines.append(
            f"| `{feature}` | {float(by.get('spearman_ic') or 0.0):.4f} | "
            f"{float(bn.get('spearman_ic') or 0.0):.4f} | "
            f"{float(pooled.get('spread_bps') or 0.0):.2f} | {reasons} |"
        )
    lines.extend(
        [
            "",
            f"Negative-control abs spread bps: `{payload['decision']['negative_control_abs_spread_bps']:.2f}`",
            "",
            "Post-entry adverse/favorable diagnostics are reported in the CSVs but are not causal filters.",
            "",
            "This stage can nominate features for a later receipt only. It is not deployment evidence.",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venues = [venue.strip() for venue in args.venues.split(",") if venue.strip()]
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": "docs/preregistration/2026-06-13-w4-continuous-stage3-path-shape.md",
        "window": {"start": args.start, "end_exclusive": args.end},
        "out": str(out),
        "sweep_tag": SWEEP_TAG,
        "stage1_control_tag": STAGE1_TAG,
        "git_head": _git_head(),
        "code_hash": _code_hash(),
        "frozen_forward_config_hash": frozen_config_hash(),
        "roots": {venue: str(ROOTS[venue]) for venue in venues},
        "pit_partition_gate": {},
        "effect_rows": [],
        "third_rows": [],
    }

    all_events: list[dict[str, Any]] = []
    for venue in venues:
        root = ROOTS[venue]
        payload["pit_partition_gate"][venue] = _pit_partition_gate(root, start=args.start, end=args.end)
        events = _event_rows(venue, root)
        all_events.extend(events)
        _write_csv(out / f"{venue}_path_shape_events.csv", events)
        features = (*CAUSAL_FEATURES, *DIAGNOSTIC_FEATURES, NEGATIVE_CONTROL)
        for feature in features:
            payload["effect_rows"].append(_effect_row(events, venue=venue, component="all", feature=feature))
            for component in COMPONENTS:
                part = [row for row in events if row["component"] == component]
                payload["effect_rows"].append(_effect_row(part, venue=venue, component=component, feature=feature))
            for row in _third_rows(events, feature):
                payload["third_rows"].append({"venue": venue, "component": "all", **row})

    features = (*CAUSAL_FEATURES, *DIAGNOSTIC_FEATURES, NEGATIVE_CONTROL)
    for feature in features:
        payload["effect_rows"].append(_effect_row(all_events, venue="pooled", component="all", feature=feature))
        for row in _third_rows(all_events, feature):
            payload["third_rows"].append({"venue": "pooled", "component": "all", **row})

    payload["decision"] = _decision(payload["effect_rows"], payload["third_rows"])
    _write_csv(out / "stage3_path_shape_events.csv", all_events)
    _write_csv(out / "stage3_feature_effects.csv", payload["effect_rows"])
    _write_csv(out / "stage3_thirds.csv", payload["third_rows"])
    (out / "stage3_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage3_summary.md")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--start", default="2023-04-01")
    parser.add_argument("--end", default="2026-05-01")
    parser.add_argument("--out", default=str(SHARED / "w4_continuous_stage3_path_shape_2026-06-13"))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "decision": payload["decision"]}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
