"""TC1 — trade-outcome atlas on the CONTINUOUS winner_base book (Stage A).

Pre-registered in docs/preregistration/trade-atlas-continuous-2026-06-11.md
(exploratory hypothesis-generator; operator-directed). Winners-vs-losers
characterization of the pooled deduped winner_base book, 9 fixed causal entry
features, both venues. Survival = direction consistent across BOTH venues AND
>=1 venue CI excludes zero AND statable mechanism.

    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=6 .venv/bin/python scripts/trade_atlas_continuous.py
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import continuous_ensemble_rebalance_scout as scout  # noqa: E402
import continuous_rs_squeeze_probe as probe  # noqa: E402

from liquidity_migration._common import MS_PER_DAY, is_weekend_ms  # noqa: E402

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "trade_atlas_continuous_2026-06-11"
WINNER_WEIGHTS = {"turn3p3": 0.30, "turn4p3": 0.20, "turn4p5": 0.40, "age210tp14": 0.10}
# dedupe priority: highest ensemble weight first (pre-registered)
DEDUP_PRIORITY = ["turn4p5", "turn3p3", "turn4p3", "age210tp14"]
SEED = 20260611
N_BOOT = 1000
VENUES = ("bybit", "binance")


def load_pooled_book(venue: str) -> pl.DataFrame:
    frames = []
    for src in WINNER_WEIGHTS:
        spec = scout.SOURCES[src]
        df = pl.read_csv(spec.root / venue / spec.cell / "continuous_trades.csv")
        frames.append(df.with_columns(pl.lit(src).alias("component")))
    allt = pl.concat(frames, how="vertical_relaxed")
    prio = {s: i for i, s in enumerate(DEDUP_PRIORITY)}
    allt = (
        allt.with_columns(pl.col("component").replace_strict(prio).alias("_prio"))
        .sort(["symbol", "entry_signal_ts_ms", "_prio"])
        .unique(subset=["symbol", "entry_signal_ts_ms"], keep="first", maintain_order=True)
        .sort("entry_ts_ms")
        .drop("_prio")
    )
    return allt


def add_features(book: pl.DataFrame, venue: str) -> pl.DataFrame:
    panel = probe.load_daily_panel(venue)
    # prior-day per-symbol close/turnover and EW market prior-day return
    panel = panel.sort(["symbol", "d"]).with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("day_ret"),
    )
    mkt = (
        panel.group_by("d")
        .agg(pl.col("day_ret").mean().alias("mkt_day_ret"), pl.len().alias("n_syms"))
        .sort("d")
    )
    mkt_prev = {d: r for d, r in mkt.select("d", "mkt_day_ret").iter_rows()}
    btc = {
        d: c
        for d, c in panel.filter(pl.col("symbol") == "BTCUSDT").select("d", "close").iter_rows()
    }
    btc_days = sorted(btc)
    sym_prev: dict[tuple[str, dt.date], tuple[float | None, float | None]] = {
        (s, d): (c, t)
        for s, d, c, t in panel.select("symbol", "d", "close", "turnover_quote").iter_rows()
    }

    rows = book.to_dicts()
    # book-state features need chronological scan
    rows.sort(key=lambda r: int(r["entry_ts_ms"]))
    last_entry_by_sym: dict[str, int] = {}
    closed: list[tuple[int, float]] = []  # (exit_ts, net) sorted by append order of exits
    feats = []
    for r in rows:
        e_ts = int(r["entry_ts_ms"])
        e_day = dt.datetime.fromtimestamp(e_ts / 1000, tz=dt.timezone.utc).date()
        prev_day = e_day - dt.timedelta(days=1)
        prev2_day = e_day - dt.timedelta(days=2)
        hour = (e_ts // 3_600_000) % 24
        session = "asia" if hour < 8 else ("eu" if hour < 16 else "us")
        prior = last_entry_by_sym.get(r["symbol"])
        repeat30 = prior is not None and (e_ts - prior) <= 30 * MS_PER_DAY
        last_entry_by_sym[r["symbol"]] = e_ts
        # concurrent: count open trades at entry (scan recent; book is small)
        concurrent = sum(
            1
            for rr in rows
            if int(rr["entry_ts_ms"]) < e_ts < int(rr["exit_ts_ms"]) and rr is not r
        )
        # trailing 10 closed-trade PnL strictly before entry
        prior_closed = [n for (x, n) in closed if x <= e_ts]
        trail10 = float(sum(prior_closed[-10:])) if prior_closed else 0.0
        # BTC trailing-30d return, lagged 1 day
        b_end = btc.get(prev_day)
        b_start_day = prev_day - dt.timedelta(days=30)
        b_start = btc.get(b_start_day)
        if b_start is None:
            cands = [d for d in btc_days if abs((d - b_start_day).days) <= 3]
            b_start = btc[min(cands, key=lambda d: abs((d - b_start_day).days))] if cands else None
        btc30 = (b_end / b_start - 1.0) if (b_end and b_start) else None
        prev_close, prev_turn = sym_prev.get((r["symbol"], prev_day), (None, None))
        log_turn = math.log10(prev_turn) if prev_turn and prev_turn > 0 else None
        feats.append(
            {
                "weekend": bool(is_weekend_ms(e_ts)),
                "session": session,
                "repeat30d": bool(repeat30),
                "concurrent": concurrent,
                "trail10_pnl": trail10,
                "btc30_mag": btc30,
                "mkt_prevday": mkt_prev.get(prev_day),
                "log_turn_prev": log_turn,
                "_prev2": str(prev2_day),
            }
        )
        closed.append((int(r["exit_ts_ms"]), float(r["net_return"] or 0.0)))
        closed.sort(key=lambda t: t[0])
    out = pl.concat([pl.DataFrame(rows), pl.DataFrame(feats)], how="horizontal")
    return out.with_columns(
        (pl.col("net_return") > 0).alias("win"),
    )


def boot_ci(wins: np.ndarray, losses: np.ndarray, rng: np.random.Generator) -> tuple[float, float, float]:
    delta = float(np.median(wins) - np.median(losses))
    reps = []
    for _ in range(N_BOOT):
        w = rng.choice(wins, size=len(wins), replace=True)
        ls = rng.choice(losses, size=len(losses), replace=True)
        reps.append(np.median(w) - np.median(ls))
    lo, hi = np.percentile(reps, [5, 95])
    return delta, float(lo), float(hi)


def binom_ci(p: float, n: int) -> float:
    return 1.645 * math.sqrt(max(p * (1 - p), 1e-9) / max(n, 1))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(SEED)
    report: dict = {"seed": SEED, "n_boot": N_BOOT, "venues": {}}
    for venue in VENUES:
        book = load_pooled_book(venue)
        df = add_features(book, venue)
        df.write_parquet(OUT / f"atlas_{venue}.parquet")
        wr_all = float(df["win"].mean())
        res: dict = {"trades": df.height, "win_rate": round(wr_all, 4), "features": {}}
        # categorical
        for feat in ("weekend", "repeat30d", "session"):
            buckets = {}
            for key, sub in df.group_by(feat, maintain_order=True):
                wr = float(sub["win"].mean())
                buckets[str(key[0])] = {
                    "n": sub.height,
                    "win_rate": round(wr, 4),
                    "wr_delta_vs_rest": round(
                        wr - float(df.filter(pl.col(feat) != key[0])["win"].mean()), 4
                    ),
                    "ci90_half": round(binom_ci(wr, sub.height), 4),
                }
            res["features"][feat] = buckets
        # continuous: winners-vs-losers median delta + bootstrap CI
        for feat in ("concurrent", "trail10_pnl", "btc30_mag", "mkt_prevday", "log_turn_prev", "rank"):
            sub = df.filter(pl.col(feat).is_not_null())
            wins = sub.filter(pl.col("win"))[feat].to_numpy().astype(float)
            losses = sub.filter(~pl.col("win"))[feat].to_numpy().astype(float)
            if len(wins) < 20 or len(losses) < 20:
                res["features"][feat] = {"skipped": f"n={len(wins)}/{len(losses)}"}
                continue
            delta, lo, hi = boot_ci(wins, losses, rng)
            res["features"][feat] = {
                "n": sub.height,
                "w_minus_l_median": round(delta, 6),
                "ci90": [round(lo, 6), round(hi, 6)],
                "excl_zero": bool(lo > 0 or hi < 0),
            }
        report["venues"][venue] = res
        print(f"\n== {venue}: {df.height} pooled trades, WR {wr_all:.2%}")
        for feat, v in res["features"].items():
            print(f"  {feat}: {json.dumps(v, default=str)}")
    (OUT / "atlas_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nreport -> {OUT / 'atlas_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
