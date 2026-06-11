"""TA1: trade-outcome atlas — winners vs losers across the deployed books.

Pre-registered in docs/preregistration/trade-atlas-2026-06-11.md. Fixed 11-feature
menu of ENTRY-context characteristics joined to the four fresh deployed-profile
ledgers (SHORT/LONG x bybit/binance). Hypothesis-generator only: survivors are
ranked by cross-book consistency and get armed forward/OOS receipts — never a
spent-window promotion.

Usage:
    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=6 .venv/bin/python scripts/trade_atlas.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import continuous_rs_squeeze_probe as probe  # noqa: E402
from depth_impact_calibration import load_depth_1pct  # noqa: E402

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "trade_atlas_2026-06-11"
SEED, REPS = 20260611, 1000
MS_PER_HOUR = 3_600_000
LEDGERS = {
    "short_bybit": ("bybit", "short", SHARED / "bybit_full_pit/reports/short_promoted_2026-06-10/short/volume_event_best_trades.csv"),
    "short_binance": ("binance", "short", SHARED / "binance_full_pit/reports/short_promoted_2026-06-10/short/volume_event_best_trades.csv"),
    "long_bybit": ("bybit", "long", SHARED / "bybit_full_pit/reports/long_regularity_2026-06-10/00_baseline/long_native_trades.csv"),
    "long_binance": ("binance", "long", SHARED / "binance_full_pit/reports/long_regularity_2026-06-10/00_baseline/long_native_trades.csv"),
}


def venue_context(venue: str) -> tuple[dict, dict, dict]:
    """BTC closes by date, EW alt-market daily return by date, turnover by (sym, date)."""
    panel = probe.load_daily_panel(venue)
    p = panel.sort(["symbol", "d"]).with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret1"))
    btc = {r[0]: r[1] for r in p.filter(pl.col("symbol") == "BTCUSDT").select(["d", "close"]).iter_rows()}
    mkt = {r[0]: r[1] for r in p.filter(pl.col("symbol") != "BTCUSDT").group_by("d").agg(
        pl.col("ret1").mean()).iter_rows()}
    turn = {(r[0], r[1]): r[2] for r in p.select(["symbol", "d", "turnover_quote"]).iter_rows()}
    return btc, mkt, turn


def build_rows(name: str, venue: str, book: str, path: Path,
               ctx: tuple[dict, dict, dict], depth: pl.DataFrame | None) -> pl.DataFrame:
    btc, mkt, turn = ctx
    t = pl.read_csv(path)
    cols = {c.lower(): c for c in t.columns}
    net_col = cols.get("net_return")
    t = t.with_columns(pl.col(cols["entry_ts_ms"]).cast(pl.Int64).alias("ets"),
                       pl.col(cols["exit_ts_ms"]).cast(pl.Int64).alias("xts"),
                       pl.col(net_col).cast(pl.Float64).alias("net"))
    rows = []
    recs = t.sort("ets").to_dicts()
    exit_history: list[tuple[int, float]] = sorted((r["xts"], r["net"]) for r in recs)
    sym_entries: dict[str, list[int]] = {}
    for r in recs:
        sym_entries.setdefault(str(r[cols["symbol"]]), []).append(r["ets"])
    for r in recs:
        ets, sym = r["ets"], str(r[cols["symbol"]])
        d = dt.datetime.fromtimestamp(ets / 1000, tz=dt.timezone.utc)
        d_prev = d.date() - dt.timedelta(days=1)
        # trailing book pnl: last 10 trades EXITED before this entry
        prior = [n for x, n in exit_history if x < ets][-10:]
        concurrent = sum(1 for o in recs if o is not r and o["ets"] <= ets < o["xts"])
        repeat = any(0 < ets - e <= 30 * 86_400_000 for e in sym_entries[sym] if e < ets)
        btc_now, btc_31 = btc.get(d_prev), btc.get(d_prev - dt.timedelta(days=30))
        row = {
            "ledger": name, "venue": venue, "book": book, "symbol": sym,
            "net": r["net"], "win": r["net"] > 0,
            "session": ("asia", "eu", "us")[d.hour // 8],
            "weekend": d.weekday() >= 5,
            "concurrent": concurrent,
            "trail10_pnl": float(sum(prior)) if prior else None,
            "repeat30d": repeat,
            "btc30_mag": (btc_now / btc_31 - 1.0) if btc_now and btc_31 else None,
            "mkt_day_ret": mkt.get(d.date()),
            "log_turn": float(np.log10(turn[(sym, d_prev)])) if turn.get((sym, d_prev)) else None,
            "crowding": r.get(cols.get("crowding_entry_hour_signal_count", ""), None),
        }
        if depth is not None:
            hr = (ets // MS_PER_HOUR) * MS_PER_HOUR
            m = depth.filter((pl.col("symbol") == sym) & (pl.col("ts_ms") == hr))
            if m.height:
                bid = m["bid"][0] if "bid" in m.columns else None
                ask = m["ask"][0] if "ask" in m.columns else None
                row["depth_entry_side"] = bid if book == "short" else ask
                if bid is not None and ask is not None and (bid + ask) > 0:
                    row["depth_imbalance"] = (bid - ask) / (bid + ask)
        rows.append(row)
    return pl.DataFrame(rows)


def boot_median_diff(w: np.ndarray, l: np.ndarray) -> dict:  # noqa: E741
    if len(w) < 10 or len(l) < 10:
        return {"n_w": len(w), "n_l": len(l)}
    rng = np.random.default_rng(SEED)
    diffs = [float(np.median(rng.choice(w, len(w))) - np.median(rng.choice(l, len(l)))) for _ in range(REPS)]
    return {"n_w": int(len(w)), "n_l": int(len(l)),
            "median_diff": round(float(np.median(w) - np.median(l)), 6),
            "ci90": [round(float(np.quantile(diffs, q)), 6) for q in (0.05, 0.95)]}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ctx_cache: dict[str, tuple] = {}
    depth_cache: dict[str, pl.DataFrame] = {}
    frames = []
    for name, (venue, book, path) in LEDGERS.items():
        if not path.exists():
            print(f"[skip] {name}: missing {path}")
            continue
        if venue not in ctx_cache:
            ctx_cache[venue] = venue_context(venue)
        depth = None
        if venue == "binance":
            syms = set(pl.read_csv(path)["symbol"].unique().to_list())
            key = ",".join(sorted(syms))[:64]
            if key not in depth_cache:
                depth_cache[key] = load_depth_1pct(syms)
            depth = depth_cache[key]
        frames.append(build_rows(name, venue, book, path, ctx_cache[venue], depth))
        print(f"[{name}] {frames[-1].height} trades")
    atlas = pl.concat(frames, how="diagonal")
    atlas.write_parquet(OUT / "atlas.parquet")

    report: dict = {"preregistration": "docs/preregistration/trade-atlas-2026-06-11.md"}
    cont_feats = ["concurrent", "trail10_pnl", "btc30_mag", "mkt_day_ret", "log_turn",
                  "crowding", "depth_entry_side", "depth_imbalance"]
    cat_feats = ["session", "weekend", "repeat30d"]
    for name in atlas["ledger"].unique().to_list():
        sub = atlas.filter(pl.col("ledger") == name)
        res: dict = {"n": sub.height, "win_rate": round(float(sub["win"].mean()), 3)}
        for f in cont_feats:
            if f not in sub.columns or sub[f].null_count() == sub.height:
                continue
            s = sub.filter(pl.col(f).is_not_null())
            w = s.filter(pl.col("win"))[f].to_numpy().astype(float)
            l = s.filter(~pl.col("win"))[f].to_numpy().astype(float)  # noqa: E741
            res[f] = boot_median_diff(w, l)
        for f in cat_feats:
            buckets = {}
            for key, g in sub.group_by(f):
                k = key[0] if isinstance(key, tuple) else key
                wr = float(g["win"].mean())
                n = g.height
                se = (wr * (1 - wr) / n) ** 0.5 if n else 0.0
                buckets[str(k)] = {"n": n, "win_rate": round(wr, 3), "se": round(se, 3),
                                   "mean_net": round(float(g["net"].mean()), 5)}
            res[f] = buckets
        report[name] = res
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(json.dumps(report, indent=1, default=str)[:4000])
    print(f"wrote {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
