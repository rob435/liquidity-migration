"""TC1 Stage B — atlas-survivor gates on the continuous winner_base ensemble.

Pre-registered in docs/preregistration/trade-atlas-continuous-2026-06-11.md.
Survivors (Stage A): session US-penalty, mkt_prevday (up prior day favours
winners), log_turn_prev (lower prior-day turnover favours winners). Declared
forms: m=1.5 tilt on the favourable (non-US) session buckets; m=0 threshold
filters at the POOLED-book median (one threshold across venues, missing -> m=1).
All cells go through the parity-verified per-trade daily-split machinery ->
frozen winner weights -> the deployed w90/tv0.045/max4/ddh-0.04 rule, unhedged.
Numbers are in-sample-derived descriptives on the spent window — NEVER
promotion evidence.

    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=6 .venv/bin/python scripts/trade_atlas_continuous_gates.py
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
import continuous_oi_tilt_stage2_driver as oi  # noqa: E402
import continuous_participation_cap_driver as cap  # noqa: E402
import continuous_rs_squeeze_probe as probe  # noqa: E402

from liquidity_migration.continuous_rebalance import apply_rebalance_rule  # noqa: E402

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "trade_atlas_continuous_2026-06-11"
WINNER_WEIGHTS = cap.WINNER_WEIGHTS
VENUES = ("bybit", "binance")


def pooled_medians() -> tuple[float, float]:
    frames = [pl.read_parquet(OUT / f"atlas_{v}.parquet", columns=["mkt_prevday", "log_turn_prev"]) for v in VENUES]
    both = pl.concat(frames)
    return (
        float(both["mkt_prevday"].drop_nulls().median()),
        float(both["log_turn_prev"].drop_nulls().median()),
    )


def feature_maps(venue: str) -> tuple[dict, dict]:
    panel = probe.load_daily_panel(venue)
    panel = panel.sort(["symbol", "d"]).with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("day_ret"),
    )
    mkt_prev = {
        d: r
        for d, r in panel.group_by("d").agg(pl.col("day_ret").mean()).sort("d").iter_rows()
    }
    turn_prev = {
        (s, d): (math.log10(t) if t and t > 0 else None)
        for s, d, t in panel.select("symbol", "d", "turnover_quote").iter_rows()
    }
    return mkt_prev, turn_prev


def trade_features(trades: pl.DataFrame, mkt_prev: dict, turn_prev: dict) -> list[dict]:
    out = []
    for t in trades.to_dicts():
        e_ts = int(t["entry_ts_ms"])
        e_day = dt.datetime.fromtimestamp(e_ts / 1000, tz=dt.timezone.utc).date()
        prev_day = e_day - dt.timedelta(days=1)
        hour = (e_ts // 3_600_000) % 24
        out.append(
            {
                "session": "asia" if hour < 8 else ("eu" if hour < 16 else "us"),
                "mkt_prevday": mkt_prev.get(prev_day),
                "log_turn_prev": turn_prev.get((t["symbol"], prev_day)),
            }
        )
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    mkt_med, turn_med = pooled_medians()
    print(f"pooled thresholds: mkt_prevday median={mkt_med:.5f}  log_turn_prev median={turn_med:.3f}")

    def cell_multiplier(cell: str, f: dict) -> float:
        m = 1.0
        if cell in ("TC_session15", "TC_combo") and f["session"] != "us":
            m *= 1.5
        if cell in ("TC_mktup", "TC_combo"):
            v = f["mkt_prevday"]
            if v is not None and v < mkt_med:
                m = 0.0
        if cell == "TC_lowturn":
            v = f["log_turn_prev"]
            if v is not None and v > turn_med:
                m = 0.0
        return m

    cells = ["rebuilt_baseline", "TC_session15", "TC_mktup", "TC_lowturn", "TC_combo"]
    results: list[dict] = []
    parity_all = True
    for venue in VENUES:
        closes = cap.panel_closes(venue)
        trades_by_src = cap.load_trades(venue)
        mkt_prev, turn_prev = feature_maps(venue)

        pieces_official = {}
        comp_days: dict[str, list[int]] = {}
        for src in WINNER_WEIGHTS:
            comp, _n, _cfg = scout._load_source(scout.SOURCES[src], venue)
            pieces_official[src] = comp
            comp_days[src] = comp.days
        official = scout._combine_components(pieces_official, WINNER_WEIGHTS)
        df_off = apply_rebalance_rule(official, cap.winner_rule())
        m_off = cap.metrics(df_off)
        results.append({"venue": venue, "cell": "official_control", "trades_on": None, **m_off})

        comp_rows: dict[str, list[dict]] = {}
        comp_feats: dict[str, list[dict]] = {}
        impact_exp: dict[str, float] = {}
        for src, (trades, cfg) in trades_by_src.items():
            r, miss = cap.trade_day_splits(trades, closes)
            cap.invert_participation(r, cfg)
            comp_rows[src] = r
            comp_feats[src] = trade_features(trades, mkt_prev, turn_prev)
            impact_exp[src] = float(cfg.get("impact_exponent", 0.5))
            if miss:
                print(f"[{venue}] {src}: {miss} trades booked entry-day (missing boundary close)")

        for cell in cells:
            pieces = {}
            n_on = 0
            n_tot = 0
            for src in WINNER_WEIGHTS:
                ms = [1.0 if cell == "rebuilt_baseline" else cell_multiplier(cell, f) for f in comp_feats[src]]
                n_on += sum(1 for m in ms if m > 0)
                n_tot += len(ms)
                pieces[src] = oi.build_component_tilted(
                    comp_rows[src], comp_days[src], ms, 1.0, impact_exp[src]
                )
            combined = scout._combine_components(pieces, WINNER_WEIGHTS)
            df = apply_rebalance_rule(combined, cap.winner_rule())
            m = cap.metrics(df)
            if cell == "rebuilt_baseline":
                a = df_off.sort("ts_ms")["basket_return"].to_numpy()
                b = df.sort("ts_ms")["basket_return"].to_numpy()
                n = min(len(a), len(b))
                corr = float(np.corrcoef(a[:n], b[:n])[0, 1])
                ret_diff = abs(m["ret_pct"] - m_off["ret_pct"])
                ok = corr >= 0.999 and ret_diff <= max(1.0, 0.01 * abs(m_off["ret_pct"]))
                parity_all &= ok
                print(f"[{venue}] parity: corr={corr:.6f} ret_diff={ret_diff:.2f}pp -> {'PASS' if ok else 'FAIL'}")
            df.write_parquet(OUT / f"gates_{venue}_{cell}.parquet")
            results.append({"venue": venue, "cell": cell, "trades_on": f"{n_on}/{n_tot}", **m})

    out = OUT / "gates_summary.json"
    out.write_text(json.dumps({"parity_pass": parity_all, "mkt_med": mkt_med, "turn_med": turn_med,
                               "rows": results}, indent=2), encoding="utf-8")
    print(f"\n{'venue':<9}{'cell':<18}{'trades_on':>12}{'ret%':>9}{'dd%':>8}{'MAR':>7}{'sharpe':>8}{'worst_day':>10}")
    for r in results:
        print(f"{r['venue']:<9}{r['cell']:<18}{str(r['trades_on']):>12}{r['ret_pct']:>9.1f}{r['dd_pct']:>8.1f}"
              f"{r['mar']:>7.2f}{r['sharpe']:>8.3f}{r['worst_day_pct']:>10.2f}")
    print(f"\nparity_pass={parity_all}  summary -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
