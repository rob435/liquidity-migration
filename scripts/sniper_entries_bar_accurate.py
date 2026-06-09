"""S1 Amendment 3: bar-accurate add-on snipe leg (engine-equivalent for an ADD-ON).

Pre-registered in docs/preregistration/sniper-staged-entries-2026-06-09.md (Amendment 3).
The official A book stays the bit-exact ledger; B legs are simulated from raw 1h bars:
fill at touch (high >= level) strictly after the entry bar, B's OWN take-profit
(fill at level on bar low <= level, counted only from the bar AFTER the fill bar),
else exit at the original trade exit; funding pro-rata from actual funding events in
(fill_ts, exit_ts]; costs per tranche notional.

Usage:
    PYTHONIOENCODING=utf-8 .venv/bin/python scripts/sniper_entries_bar_accurate.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from functools import lru_cache
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import continuous_ensemble_rebalance_scout as scout  # noqa: E402
import continuous_hedge_engine_driver as drv  # noqa: E402

from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousHedgeRule,
    ContinuousRebalanceComponents,
    apply_rebalance_rule,
    combine_continuous_components,
)

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "sniper_entries_2026-06-09"
MS_PER_DAY = 86_400_000
MS_PER_HOUR = 3_600_000
TP_PCT = 0.10
WEIGHTS = drv.WINNER_WEIGHTS
KLINE_ROOT = {v: SHARED / f"{v}_full_pit" / "klines_1h" for v in ("bybit", "binance")}
FUNDING_ROOT = {
    "bybit": SHARED / "bybit_full_pit" / "funding",
    "binance": SHARED / "binance_full_pit" / "binance_usdm_funding",
}
_VENUE = {"v": "bybit"}


@lru_cache(maxsize=200_000)
def _bars(symbol: str, date: str) -> tuple:
    part = KLINE_ROOT[_VENUE["v"]] / f"date={date}" / f"symbol={symbol}"
    if not part.exists():
        return ()
    df = pl.read_parquet(part, columns=["ts_ms", "high", "low"]).sort("ts_ms")
    return tuple(zip(df["ts_ms"].to_list(), df["high"].to_list(), df["low"].to_list()))


@lru_cache(maxsize=200_000)
def _funding_events(symbol: str, date: str) -> tuple:
    part = FUNDING_ROOT[_VENUE["v"]] / f"date={date}" / f"symbol={symbol}"
    if not part.exists():
        return ()
    df = pl.read_parquet(part, columns=["ts_ms", "funding_rate"]).unique(["ts_ms"]).sort("ts_ms")
    return tuple(zip(df["ts_ms"].to_list(), df["funding_rate"].to_list()))


def _dates(lo_ms: int, hi_ms: int) -> list[str]:
    d0 = dt.datetime.fromtimestamp(lo_ms / 1000, tz=dt.timezone.utc).date()
    d1 = dt.datetime.fromtimestamp(hi_ms / 1000, tz=dt.timezone.utc).date()
    out, d = [], d0
    while d <= d1:
        out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def sim_b_leg(trade: dict, x: float, *, strict: float = 0.0) -> tuple[float, float, int] | None:
    """Returns (gross_b_pn, funding_b_pn, fill_ts) or None if no fill."""
    sym = trade["symbol"]
    e_ts, x_ts = int(trade["entry_ts_ms"]), int(trade["exit_ts_ms"])
    p0, p_exit = float(trade["entry_price"]), float(trade["exit_price"])
    level = p0 * (1.0 + x + strict)
    pb = p0 * (1.0 + x)
    bars = []
    for date in _dates(e_ts, x_ts):
        bars.extend(_bars(sym, date))
    fill_bar_ts = None
    for ts, high, low in bars:
        if ts < e_ts or ts >= x_ts:
            continue
        if high >= level:
            fill_bar_ts = ts
            break
    if fill_bar_ts is None:
        return None
    fill_ts = fill_bar_ts + MS_PER_HOUR  # position live from the fill bar's END (conservative)
    tp_level = pb * (1.0 - TP_PCT)
    exit_used, exit_ts_used = p_exit, x_ts
    for ts, high, low in bars:
        if ts < fill_ts or ts >= x_ts:
            continue  # TP counted only from the bar AFTER the fill bar
        if low <= tp_level:
            exit_used, exit_ts_used = tp_level, ts + MS_PER_HOUR
            break
    gross_pn = (pb - exit_used) / pb
    fund = 0.0
    for date in _dates(fill_ts, exit_ts_used):
        for fts, rate in _funding_events(sym, date):
            if fill_ts < fts <= exit_ts_used:
                fund += rate  # short receives positive funding
    return gross_pn, fund, fill_ts


def component_deltas(trades: pl.DataFrame, legs: list[tuple[float, float]], *, strict: float = 0.0, maker: bool = False, cost_mult: float = 1.0) -> pl.DataFrame:
    rows = []
    stats = {"fills": 0, "tp_exits": 0, "n": trades.height * len(legs)}
    for tr in trades.to_dicts():
        w = abs(float(tr["notional_weight"]))
        cost_pn = float(tr["cost_return"]) / max(w, 1e-12) * cost_mult + (7e-4 if maker else 0.0)
        exit_day = (int(tr["exit_ts_ms"]) // MS_PER_DAY) * MS_PER_DAY
        delta = 0.0
        for x, b in legs:
            res = sim_b_leg(tr, x, strict=strict)
            if res is None:
                continue
            gross_pn, fund_pn, _ = res
            stats["fills"] += 1
            if gross_pn > 0.0 and abs(gross_pn - TP_PCT / (1.0)) < 0.101 and gross_pn >= TP_PCT - 1e-9:
                stats["tp_exits"] += 1
            delta += b * w * (gross_pn + cost_pn + fund_pn)
        if delta != 0.0:
            rows.append({"exit_day": exit_day, "delta": delta})
    df = pl.DataFrame(rows) if rows else pl.DataFrame({"exit_day": pl.Series([], dtype=pl.Int64), "delta": pl.Series([], dtype=pl.Float64)})
    df.attrs = stats if hasattr(df, "attrs") else None
    return df, stats


def adjust(pieces, dmap):
    out = {}
    for name, comp in pieces.items():
        dd = dmap[name].group_by("exit_day").agg(pl.col("delta").sum()) if dmap[name].height else dmap[name]
        add = {int(r["exit_day"]): float(r["delta"]) for r in dd.to_dicts()} if dmap[name].height else {}
        out[name] = ContinuousRebalanceComponents(
            days=comp.days,
            raw_by_day={d: comp.raw_by_day.get(d, 0.0) + add.get(d, 0.0) for d in comp.days},
            gross_by_day={d: comp.gross_by_day.get(d, 0.0) + add.get(d, 0.0) for d in comp.days},
            cost_events=comp.cost_events,
            funding_by_day=comp.funding_by_day,
            active_gross_start=comp.active_gross_start,
            impact_exponent=comp.impact_exponent,
        )
    return out


def main() -> None:
    report: dict = {"preregistration": "Amendment 3, docs/preregistration/sniper-staged-entries-2026-06-09.md"}
    for venue in ["bybit", "binance"]:
        _VENUE["v"] = venue
        _bars.cache_clear()
        _funding_events.cache_clear()
        pieces = {s: scout._load_source(scout.SOURCES[s], venue)[0] for s in WEIGHTS}
        pieces2x = {s: scout._apply_cost_multiplier(p, 2.0) for s, p in pieces.items()}
        trades_by = {s: pl.read_csv(scout._source_paths(scout.SOURCES[s], venue)[1]) for s in WEIGHTS}
        h_ret, h_fund = drv.btc_inputs(venue, sorted(set().union(*[set(p.days) for p in pieces.values()])))

        def hm(pcs):
            df = apply_rebalance_rule(combine_continuous_components(pcs, WEIGHTS), drv.winner_rule(4), ContinuousHedgeRule(90, 60, 2.0, 5.0), h_ret, h_fund)
            return drv.metrics(df)

        base, base2x = hm(pieces), hm(pieces2x)
        vres = {"base_mar": base["mar"], "base2x_mar": base2x["mar"]}
        CELLS = {
            "x8_b25": ([(0.08, 0.25)], {}),
            "x5_b25": ([(0.05, 0.25)], {}),
            "ladder": ([(0.05, 0.125), (0.08, 0.125)], {}),
            "x8_b25_strict": ([(0.08, 0.25)], {"strict": 0.002}),
            "x8_b25_maker": ([(0.08, 0.25)], {"maker": True}),
        }
        for cid, (legs, kw) in CELLS.items():
            dmap, allstats = {}, {"fills": 0, "tp_exits": 0, "n": 0}
            for s in WEIGHTS:
                d, st = component_deltas(trades_by[s], legs, **kw)
                dmap[s] = d
                for k in allstats:
                    allstats[k] += st[k]
            m = hm(adjust(pieces, dmap))
            vres[cid] = {
                "mar": m["mar"], "d_mar": round(m["mar"] - base["mar"], 2),
                "sharpe": m["sharpe"], "dd": m["dd_pct"], "d_dd": round(m["dd_pct"] - base["dd_pct"], 2),
                "s2324": m["sharpe_2324"], "fill_rate": round(allstats["fills"] / max(allstats["n"], 1), 3),
                "tp_share_of_fills": round(allstats["tp_exits"] / max(allstats["fills"], 1), 3),
            }
            print(f"[{venue}] {cid:14s} MAR {m['mar']:5.2f} dMAR {vres[cid]['d_mar']:+.2f} dDD {vres[cid]['d_dd']:+.2f} Sh {m['sharpe']:.3f} fill {vres[cid]['fill_rate']:.1%} TP-exit share {vres[cid]['tp_share_of_fills']:.1%}")
        # 2x cost on the primary
        dmap = {}
        for s in WEIGHTS:
            d, _ = component_deltas(trades_by[s], [(0.08, 0.25)], cost_mult=2.0)
            dmap[s] = d
        m = hm(adjust(pieces2x, dmap))
        vres["x8_b25_2x"] = {"mar": m["mar"], "d_mar_vs_2xbase": round(m["mar"] - base2x["mar"], 2), "dd": m["dd_pct"]}
        print(f"[{venue}] x8_b25_2x      MAR {m['mar']:5.2f} dMAR-vs-2xbase {vres['x8_b25_2x']['d_mar_vs_2xbase']:+.2f}")
        report[venue] = vres
    with open(OUT / "amendment3_bar_accurate.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"wrote {OUT / 'amendment3_bar_accurate.json'}")


if __name__ == "__main__":
    main()
