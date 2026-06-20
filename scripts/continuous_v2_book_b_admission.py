#!/usr/bin/env python3
"""Problem Book B — entry admission via causal 1m pre-entry path features (continuous v2 next-level).

Receipt: docs/preregistration/2026-06-20-continuous-v2-book-b-admission-construction.md

The E1 closure (Bybit-only) found that selling a fade short INTO intrabar strength
selects continuation-risk losers. With both-venue 1m now built (Wave 1), this tests
the entry side directly: compute STRICTLY CAUSAL pre-entry 1m path features (using
only bars with ts < entry decision), measure their IC vs the realized control trade
return, and test whether admitting the favorable subset beats (a) the full control
book and (b) a hash-random subset of the same size, on BOTH venues. A delayed-copy
control (features from the prior hour) checks the IC is using fresh pre-entry info.

Features (all causal, computed on [entry - PRE_MIN min, entry)):
- ret_last15:   15-min 1m momentum into entry (E1's "selling into strength" axis).
- run_up_120:   (last pre-close / window low) - 1   (size of the pop being faded).
- dist_from_hi: (window high - last pre-close)/last pre-close (pulled back from high = exhaustion).
- upper_wick:   mean upper-wick fraction (sellers rejecting highs = exhaustion).
- rv_30:        realized 1m vol over the last 30 min (entry-time turbulence).

EXPLORATORY screen: per-trade realized PnL, no rebalance/hedge re-solve; admission
changes the book, so any survivor needs full-ledger validation + operator gating.
Not a candidate; not real-money evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[1]
import sys  # noqa: E402

sys.path.insert(0, str(REPO))
from liquidity_migration.intrabar_engine import CACHE_ROOT  # noqa: E402

MS_MIN = 60_000
MS_DAY = 86_400_000
PRE_MIN = 120  # pre-entry window minutes
ANN_DAYS = 365.25
FEATURES = ["ret_last15", "run_up_120", "dist_from_hi", "upper_wick", "rv_30"]
# expected IC sign vs short net_return (fade): + means high feature -> better fade
EXPECTED_SIGN = {"ret_last15": -1, "run_up_120": +1, "dist_from_hi": +1, "upper_wick": +1, "rv_30": -1}


def _hash01(s: str) -> float:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def mar_proxy(daily: dict[int, float]) -> dict[str, Any]:
    days = sorted(daily)
    if not days:
        return {"total": 0.0, "mar": None, "max_dd": 0.0}
    eq = 1.0
    peak = 1.0
    maxdd = 0.0
    for d in days:
        eq *= 1.0 + daily[d]
        peak = max(peak, eq)
        maxdd = min(maxdd, eq / peak - 1.0)
    total = eq - 1.0
    yrs = max((days[-1] - days[0]) / ANN_DAYS, 1e-9)
    return {"total": total, "max_dd": maxdd, "mar": (total / yrs) / abs(maxdd) if abs(maxdd) > 1e-12 else None}


def spearman(x: list[float], y: list[float]) -> float:
    a = np.asarray(x, float)
    b = np.asarray(y, float)
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 30:
        return float("nan")
    ar = np.argsort(np.argsort(a[m]))
    br = np.argsort(np.argsort(b[m]))
    ar = ar - ar.mean()
    br = br - br.mean()
    den = math.sqrt((ar * ar).sum() * (br * br).sum())
    return float((ar * br).sum() / den) if den > 0 else float("nan")


class Loader:
    def __init__(self, venue: str, cache_root: Path):
        self.venue = venue
        self.root = cache_root
        self._c: dict[tuple[str, str], pl.DataFrame | None] = {}

    def _day(self, symbol: str, d: str) -> pl.DataFrame | None:
        key = (symbol, d)
        if key not in self._c:
            p = self.root / self.venue / "klines_1m" / f"date={d}" / f"symbol={symbol}" / "data.parquet"
            self._c[key] = pl.read_parquet(p, columns=["ts_ms", "open", "high", "low", "close"]) if p.exists() else None
        return self._c[key]

    def window(self, symbol: str, lo_ts: int, hi_ts: int, entry_date: str) -> pl.DataFrame:
        d0 = date.fromisoformat(entry_date[:10]) - timedelta(days=1)
        frames = [f for f in (self._day(symbol, (d0 + timedelta(days=k)).isoformat()) for k in range(3)) if f is not None]
        if not frames:
            return pl.DataFrame()
        return pl.concat(frames).filter((pl.col("ts_ms") >= lo_ts) & (pl.col("ts_ms") < hi_ts)).unique("ts_ms").sort("ts_ms")


def features(win: pl.DataFrame, *, ref_close: float) -> dict[str, float] | None:
    if win.height < 20:
        return None
    o = win["open"].to_list()
    h = win["high"].to_list()
    low = win["low"].to_list()
    c = win["close"].to_list()
    last = c[-1]
    if last is None or last <= 0:
        return None
    c15 = c[-16] if len(c) >= 16 else c[0]
    win_low = min(x for x in low if x is not None)
    win_hi = max(x for x in h if x is not None)
    wicks = []
    for oo, hh, ll, cc in zip(o, h, low, c):
        if None in (oo, hh, ll, cc) or hh <= ll:
            continue
        wicks.append((hh - max(oo, cc)) / (hh - ll))
    rets = [math.log(c[i] / c[i - 1]) for i in range(max(1, len(c) - 30), len(c)) if c[i] and c[i - 1]]
    return {
        "ret_last15": (last / c15 - 1.0) if c15 else 0.0,
        "run_up_120": (last / win_low - 1.0) if win_low else 0.0,
        "dist_from_hi": (win_hi - last) / last,
        "upper_wick": float(np.mean(wicks)) if wicks else 0.0,
        "rv_30": float(np.std(rets)) if len(rets) > 2 else 0.0,
    }


def _contrib(net: float) -> float:
    return net  # ledger net_return already = side_return*nw + cost + fund (per-trade book contribution)


def evaluate_venue(trades: list[dict[str, Any]], loader: Loader, *, lag: bool = False) -> dict[str, Any]:
    rows = []
    for tr in trades:
        ets = int(tr["entry_ts_ms"])
        hi = ets - (MS_MIN * 60 if lag else 0)  # delayed-copy: features from the prior hour
        lo = hi - PRE_MIN * MS_MIN
        win = loader.window(str(tr["symbol"]), lo, hi, str(tr["entry_date"]))
        feat = features(win, ref_close=float(tr["entry_price"]))
        if feat is None:
            continue
        rows.append((tr, feat))
    n = len(rows)
    out: dict[str, Any] = {"n_input": len(trades), "n_with_features": n, "ic": {}, "admission": {}, "sizing": {}, "lag": lag}
    if n < 50:
        return out
    rets = [float(tr["net_return"]) for tr, _ in rows]
    days = [int(tr["exit_ts_ms"]) // MS_DAY for tr, _ in rows]
    control_daily: dict[int, float] = {}
    for (tr, _), r, d in zip(rows, rets, days):
        control_daily[d] = control_daily.get(d, 0.0) + r
    out["control_mar"] = mar_proxy(control_daily)["mar"]
    for f in FEATURES:
        fv = [feat[f] for _, feat in rows]
        ic = spearman(fv, rets)
        out["ic"][f] = {"spearman": ic, "expected_sign": EXPECTED_SIGN[f], "signed_ic": ic * EXPECTED_SIGN[f]}
        # admission: admit the favorable half by signed feature; hash null admits a random half.
        order = np.argsort([v * EXPECTED_SIGN[f] for v in fv])[::-1]  # best first
        keep = set(order[: n // 2].tolist())
        adm_daily: dict[int, float] = {}
        for i, ((tr, _), r, d) in enumerate(zip(rows, rets, days)):
            if i in keep:
                adm_daily[d] = adm_daily.get(d, 0.0) + r
        # hash null: random half of same size
        hsel = sorted(range(n), key=lambda i: _hash01(f"B:{f}:{rows[i][0]['symbol']}:{rows[i][0]['entry_ts_ms']}"))[: n // 2]
        hset = set(hsel)
        h_daily: dict[int, float] = {}
        for i, ((tr, _), r, d) in enumerate(zip(rows, rets, days)):
            if i in hset:
                h_daily[d] = h_daily.get(d, 0.0) + r
        am = mar_proxy(adm_daily)
        hm = mar_proxy(h_daily)
        out["admission"][f] = {
            "admit_mar": am["mar"], "hash_admit_mar": hm["mar"], "control_mar": out["control_mar"],
            "beats_control": (am["mar"] is not None and out["control_mar"] is not None and am["mar"] > out["control_mar"]),
            "beats_hash": (am["mar"] is not None and hm["mar"] is not None and am["mar"] > hm["mar"]),
        }
        # B4 sizing tilt: keep ALL trades, tilt size by z(feature) (mean-1, gross-constant);
        # B7 hash-tilt null permutes the SAME multipliers across trades. Screen z is full-sample
        # (fair for the feature-vs-hash relative test; a candidate needs causal-z full-ledger).
        arr = np.asarray(fv, float)
        sd = np.nanstd(arr)
        z = (arr - np.nanmean(arr)) / sd if sd > 0 else np.zeros(n)
        mult = np.clip(1.0 + 0.5 * z * EXPECTED_SIGN[f], 0.5, 1.5)
        mult = mult / mult.mean()
        perm = np.array(sorted(range(n), key=lambda i: _hash01(f"B7:{f}:{rows[i][0]['symbol']}:{rows[i][0]['entry_ts_ms']}")))
        hmult = mult[perm]
        tilt_daily: dict[int, float] = {}
        htilt_daily: dict[int, float] = {}
        for i, (r, d) in enumerate(zip(rets, days)):
            tilt_daily[d] = tilt_daily.get(d, 0.0) + r * float(mult[i])
            htilt_daily[d] = htilt_daily.get(d, 0.0) + r * float(hmult[i])
        tm = mar_proxy(tilt_daily)
        thm = mar_proxy(htilt_daily)
        out["sizing"][f] = {
            "tilt_mar": tm["mar"], "hash_tilt_mar": thm["mar"], "control_mar": out["control_mar"],
            "beats_control": (tm["mar"] is not None and out["control_mar"] is not None and tm["mar"] > out["control_mar"]),
            "beats_hash": (tm["mar"] is not None and thm["mar"] is not None and tm["mar"] > thm["mar"]),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ab-root", default="backtest-runs/continuous_v2_phase0_freeze_2026-06-19")
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--cache-root", default=str(CACHE_ROOT))
    ap.add_argument("--out", default="backtest-runs/continuous_v2_book_b_2026-06-20")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"run_label": "exploratory_screen", "pre_min": PRE_MIN, "features": FEATURES, "venues": {}}
    for venue in [v.strip() for v in args.venues.split(",") if v.strip()]:
        tr = pl.read_csv(Path(args.ab_root) / "V2_CONTROL" / venue / "trades.csv").filter(pl.col("side") == "short")
        if args.limit:
            tr = tr.head(args.limit)
        rows = tr.to_dicts()
        loader = Loader(venue, Path(args.cache_root))
        live = evaluate_venue(rows, loader, lag=False)
        lagged = evaluate_venue(rows, loader, lag=True)
        live["delayed_copy_ic"] = {f: lagged["ic"].get(f, {}).get("signed_ic") for f in FEATURES} if lagged.get("ic") else {}
        summary["venues"][venue] = live
        print(f"[{venue}] n={live['n_with_features']} control_mar={live.get('control_mar')}", flush=True)
        for f in FEATURES:
            ic = live["ic"].get(f, {})
            ad = live["admission"].get(f, {})
            sz = live["sizing"].get(f, {})
            print(f"   {f}: signed_IC={ic.get('signed_ic'):+.4f} (lag {live['delayed_copy_ic'].get(f)}) | "
                  f"ADMIT mar={ad.get('admit_mar')} ctrl={ad.get('beats_control')} hash={ad.get('beats_hash')} | "
                  f"SIZE mar={sz.get('tilt_mar')} ctrl={sz.get('beats_control')} hash={sz.get('beats_hash')}", flush=True)
    # both-venue: a feature must have same-sign IC AND admission beat control+hash on BOTH venues
    venues = list(summary["venues"])
    winners = []
    if len(venues) == 2:
        a, b = venues
        for f in FEATURES:
            ia = summary["venues"][a]["ic"].get(f, {}).get("signed_ic", float("nan"))
            ib = summary["venues"][b]["ic"].get(f, {}).get("signed_ic", float("nan"))
            aa = summary["venues"][a]["admission"].get(f, {})
            ab = summary["venues"][b]["admission"].get(f, {})
            if (ia > 0 and ib > 0 and aa.get("beats_control") and ab.get("beats_control")
                    and aa.get("beats_hash") and ab.get("beats_hash")):
                winners.append(f)
    sizing_winners = []
    if len(venues) == 2:
        a, b = venues
        for f in FEATURES:
            sa = summary["venues"][a]["sizing"].get(f, {})
            sb = summary["venues"][b]["sizing"].get(f, {})
            ia = summary["venues"][a]["ic"].get(f, {}).get("signed_ic", float("nan"))
            ib = summary["venues"][b]["ic"].get(f, {}).get("signed_ic", float("nan"))
            if (ia > 0 and ib > 0 and sa.get("beats_control") and sb.get("beats_control")
                    and sa.get("beats_hash") and sb.get("beats_hash")):
                sizing_winners.append(f)
    summary["both_venue_sizing_winners"] = sizing_winners
    summary["both_venue_admission_winners"] = winners
    (out / "book_b_admission.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"both_venue_admission_winners": winners, "both_venue_sizing_winners": sizing_winners}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
