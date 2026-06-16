#!/usr/bin/env python3
"""W6 A1 — Squeeze-proxy SIZING tilt (mean-1, gross-neutral, causal).

Sizes the SAME selected trades of the frozen continuous control by a causal
squeeze proxy (OI buildup / funding level), via the additive ``size_mult_lookup``
hook in ``continuous_events``. Entries / exits / breadth / gates are byte-identical
to the control (the multiplier is applied after every gate); only each trade's
notional changes, and resize/impact cost is recomputed at the new size. This is the
W5 Stage-5 sizing harness, re-pointed at the squeeze proxy instead of path-shape.

Pre-registration: docs/preregistration/2026-06-15-w6-squeeze-proxy-sizing.md

The squeeze score per entry is causal at the signal bar:
- ``oi``       : z(oi_chg_24h)                       (bybit primary)
- ``funding``  : z(funding_level)                    (binance leg; same-sign both-venue)
- ``combined`` : z(oi_chg_24h) + z(funding_level)    (both-venue where OI exists)
where z = within-symbol standardization (CAUSAL expanding per-symbol mean over
strictly-prior entries, global residual sigma) clipped to [-3, 3]. Sizing multiplier
``m = clip(1 + k*z, 1-cap, 1+cap)``, then prior-month causal renormalization so the
booked gross is mean-1.

Controls (multi-seed, >=5 — the Stage-4d lesson: single-seed nulls have huge MAR
variance):
- ``shuffle``      : the real per-row z permuted WITHIN each symbol (kills causal
                     timing, keeps the per-symbol score distribution).
- ``randpsym``     : a random N(0,1) draw per SYMBOL broadcast to its rows (the
                     Stage-5 Z6 symbol-dispersion-luck control, randomized).
A real squeeze tilt is only harvestable if it beats BOTH control distributions.

Decision rule (receipt, operator bar 2026-06-15, bybit-primary): a forward-watch
candidate iff (1) bybit dMAR > 0 across the WHOLE k*cap grid, (2) beats the shuffle
AND randpsym control distributions on bybit, (3) >=2/3 chronological thirds same-sign
on bybit, (4) survives 1.5x resize/impact cost stress, (5) binance "not completely
losing" on the funding variant. Admissibility != harvestable. Tier-3 UNCHANGED.

Staged run (Stage-4d compute discipline — cheap decisive check first):
    # Phase 1 (cheap probe): bybit grid only, 1x cost.
    POLARS_MAX_THREADS=8 PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/bin/python \
        scripts/w6_squeeze_proxy_sizing.py --venues bybit --scores oi \
        --seeds "" --cost-mults 1.0 \
        --out ~/SHARED_DATA/w6_squeeze_proxy_sizing_2026-06-15_probe
    # Phase 2 (binding full): both venues, oi+funding, 5 seeds, 1.5x stress.
    POLARS_MAX_THREADS=8 PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/bin/python \
        scripts/w6_squeeze_proxy_sizing.py --venues bybit,binance --scores oi,funding \
        --seeds 0,1,2,3,4 --cost-mults 1.0,1.5 \
        --out ~/SHARED_DATA/w6_squeeze_proxy_sizing_2026-06-15
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import statistics as st
import subprocess
import sys
import time
import types
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

# Defensive: some erased-W4 helpers may be referenced transitively. _w5_shared does
# NOT need it, but stub the deleted module so any incidental import cannot break us.
_w4_stub = types.ModuleType("scripts.w4_continuous_stop_exit_realism")
_w4_stub._pit_partition_gate = lambda *a, **k: {}  # type: ignore[attr-defined]
sys.modules.setdefault("scripts.w4_continuous_stop_exit_realism", _w4_stub)

import liquidity_migration.continuous_events as _ce  # noqa: E402
from liquidity_migration.continuous_events import (  # noqa: E402
    _daily_pnl_metrics,
    run_continuous_event_research,
)
from liquidity_migration.continuous_forward_replay import (  # noqa: E402
    build_full_ledger,
    frozen_config_hash,
)
from scripts._w5_shared import (  # noqa: E402
    COMPONENTS,
    MS_PER_DAY,
    MS_PER_HOUR,
    ROOTS,
    SHARED,
    _btc_inputs,
    _component_config,
    _load_component,
    _pit_partition_gate,
)


# ----------------------------------------------------------------------------------
# FUNDING-GUARD NOTE (research-scoped correction; engine/live untouched, nothing pushed)
# ----------------------------------------------------------------------------------
# The audit-landed engine guard `_assert_funding_one_per_settlement` (commit 7d39d61,
# cost-funding-5) fails any run whose loaded funding has a per-symbol observed cadence
# finer than HALF the stored `funding_interval_min`. On bybit_full_pit that flags ~85
# in-window symbols (ANIMEUSDT, 0GUSDT, ...) as "snapshot scrapes". This is a FALSE
# POSITIVE, verified 2026-06-15:
#   * The local rows match the AUTHORITATIVE Bybit get_funding_rate_history endpoint
#     row-for-row, with DISTINCT realized rates (EGLDUSDT 2022-01, ANIMEUSDT 2025-05).
#   * instruments.fundingInterval == 240 (4h) for ANIMEUSDT, != the stored 480 — these
#     symbols genuinely settle sub-8h (Bybit per-symbol / dynamic intervals); BTCUSDT
#     stays 8h, so it is NOT a uniform hourly ticker scrape.
#   * The engine funding CHARGE is correct regardless: _funding_lookup/_perp_funding_return
#     sum raw funding_rate at EVERY distinct settlement stamp (exact-stamp), which its own
#     docstring documents as one-row-per-settlement; the charge never reads funding_interval_min.
# Root cause: the dataset stamps the CURRENT funding_interval_min (480) on ALL history,
# so the guard compares real sub-8h cadence against a stale 480 label. Real perp funding
# never settles more often than hourly, so the methodologically-correct guard flags ONLY
# clearly sub-60min cadence (a true snapshot scrape), not real <=8h intervals. We install
# that corrected guard here (scoped to this research script). Proper upstream fix
# (operator-gated): relabel funding_interval_min from observed gaps + recompute
# funding_rate_8h_equiv, and/or land this floor in the engine guard.
def _corrected_funding_guard(funding, *, root) -> None:  # noqa: ANN001
    if funding is None or getattr(funding, "is_empty", lambda: True)():
        return
    if not {"symbol", "ts_ms"} <= set(funding.columns):
        return
    spacing = (
        funding.select("symbol", "ts_ms").drop_nulls().unique(["symbol", "ts_ms"])
        .sort(["symbol", "ts_ms"])
        .with_columns((pl.col("ts_ms") - pl.col("ts_ms").shift(1).over("symbol")).alias("_g"))
        .drop_nulls("_g").filter(pl.col("_g") > 0)
        .group_by("symbol")
        .agg(pl.col("_g").median().alias("_m"), pl.len().alias("_n"))
        .filter((pl.col("_n") >= 4) & (pl.col("_m") < 55 * 60_000))  # < 55min = impossible real funding
    )
    if not spacing.is_empty():
        bad = spacing.sort("symbol").head(5)["symbol"].to_list()
        raise RuntimeError(
            f"funding sub-hourly oversampling (true snapshot scrape) under {root}: "
            f"{int(spacing.height)} symbol(s) settle < 55min, e.g. {bad}"
        )


_ce._assert_funding_one_per_settlement = _corrected_funding_guard

STAGE0_TAG = "w5_continuous_stage0_candidate_tape_2026-06-14"
SWEEP_TAG = "w6_squeeze_proxy_sizing_2026-06-15"
PREREG = "docs/preregistration/2026-06-15-w6-squeeze-proxy-sizing.md"
Z_CLIP = 3.0
STANDARDIZER = "causal_expanding_within_symbol_mean__causal_expanding_residual_sigma"

# Per-venue raw layer dir + value column (mirrors scripts/w6_squeeze_proxy_screen.py).
LAYERS = {
    "bybit": {
        "oi": ("open_interest", "open_interest"),
        "funding": ("funding", "funding_rate_8h_equiv"),
    },
    "binance": {
        "oi": ("binance_usdm_open_interest", "open_interest"),
        "funding": ("binance_usdm_funding", "funding_rate"),
    },
}


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
        REPO / "scripts" / "w6_squeeze_proxy_sizing.py",
        REPO / "scripts" / "_w5_shared.py",
        REPO / "liquidity_migration" / "continuous_events.py",
        REPO / "liquidity_migration" / "continuous_forward_replay.py",
    ]
    payload = "|".join(f"{p.relative_to(REPO)}:{_sha256_file(p)}" for p in paths if p.exists())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _date_to_ms(date: str) -> int:
    return int(dt.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


# ----------------------------- squeeze score (causal) -----------------------------
def _selected_rows(stage0_dir: Path, venue: str) -> list[dict[str, Any]]:
    """UNIQUE selected entries keyed by (symbol, signal_ts). The squeeze score is a
    property of the entry's causal OI/funding context, INDEPENDENT of which component
    selected it; the engine applies size_mult_lookup.get((sym, sig_ts)) shared across
    components. Deduping here is required so the within-symbol expanding mean is computed
    over distinct entry times, not component-duplicated rows."""
    tape = pl.read_parquet(stage0_dir / f"candidate_tape_{venue}.parquet").filter(pl.col("selected"))
    cols = ["symbol", "signal_ts_ms", "symbol_hash_bucket"]
    df = (
        tape.select([c for c in cols if c in tape.columns])
        .unique(subset=["symbol", "signal_ts_ms"], keep="first")
    )
    rows = df.to_dicts()
    rows.sort(key=lambda r: (int(r["signal_ts_ms"]), str(r["symbol"])))
    return rows


def _load_series(root: Path, layer_dir: str, value_col: str, symbols: set[str],
                 lo_ms: int, hi_ms: int) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    base = root / layer_dir
    if not base.is_dir():
        return {}
    df = (
        pl.scan_parquet(str(base / "**" / "*.parquet"))
        .select("ts_ms", "symbol", pl.col(value_col).alias("v"))
        .filter(pl.col("symbol").is_in(list(symbols)) & pl.col("ts_ms").is_between(lo_ms, hi_ms))
        .drop_nulls("v")
        .sort("ts_ms")
        .collect()
    )
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for sym, sub in df.group_by("symbol"):
        ts = sub["ts_ms"].to_numpy()
        out[str(sym[0] if isinstance(sym, tuple) else sym)] = (ts, sub["v"].to_numpy())
    return out


def _asof(series: dict[str, tuple[np.ndarray, np.ndarray]], symbol: str, anchor: int) -> float | None:
    pair = series.get(symbol)
    if pair is None:
        return None
    ts, val = pair
    i = int(np.searchsorted(ts, anchor, side="right")) - 1
    if i < 0:
        return None
    return float(val[i])


def _attach_raw_features(rows: list[dict[str, Any]], root: Path, venue: str) -> dict[str, int]:
    """Attach causal raw squeeze features (oi_chg_24h, funding_level) to each row."""
    symbols = {r["symbol"] for r in rows}
    if not rows:
        return {"oi_chg_24h": 0, "funding_level": 0}
    lo = min(r["signal_ts_ms"] for r in rows) - 25 * MS_PER_HOUR
    hi = max(r["signal_ts_ms"] for r in rows) + MS_PER_HOUR
    cfg = LAYERS[venue]
    oi = _load_series(root, *cfg["oi"], symbols, lo, hi)
    fund = _load_series(root, *cfg["funding"], symbols, lo, hi)
    cov = {"oi_chg_24h": 0, "funding_level": 0}
    for r in rows:
        s, t = r["symbol"], r["signal_ts_ms"]
        oi_now, oi_24 = _asof(oi, s, t), _asof(oi, s, t - MS_PER_DAY)
        r["oi_chg_24h"] = (oi_now / oi_24 - 1.0) if (oi_now and oi_24 and oi_24 > 0) else None
        r["funding_level"] = _asof(fund, s, t)
        if r["oi_chg_24h"] is not None:
            cov["oi_chg_24h"] += 1
        if r["funding_level"] is not None:
            cov["funding_level"] += 1
    return cov


_SIGMA_WARMUP = 30


def _within_symbol_z(rows: list[dict[str, Any]], feature: str) -> list[float | None]:
    """Per-row within-symbol z, FULLY CAUSAL: each entry is centered by its symbol's mean
    over STRICTLY-PRIOR same-symbol entries (rows are ts-sorted) and scaled by an EXPANDING
    residual sigma over residuals seen STRICTLY BEFORE this row (RMS; within-symbol residuals
    are ~zero-mean), clipped to [-Z_CLIP, Z_CLIP]. No look-ahead in either the centering or
    the scale (the prior global-sigma version was a whole-sample look-ahead that fed the
    cap-clip nonlinearity — review finding). None where the feature is absent, no prior
    same-symbol entry exists (first obs per symbol), or fewer than _SIGMA_WARMUP residuals
    have accrued (sigma not yet estimable -> no tilt)."""
    run_sum: dict[str, float] = defaultdict(float)
    run_cnt: dict[str, int] = defaultdict(int)
    mu_of: list[float | None] = []
    for r in rows:
        v = r.get(feature)
        s = str(r["symbol"])
        if v is None:
            mu_of.append(None)
            continue
        mu_of.append((run_sum[s] / run_cnt[s]) if run_cnt[s] >= 1 else None)
        run_sum[s] += float(v)
        run_cnt[s] += 1
    out: list[float | None] = []
    run_ss = 0.0  # sum of squared prior residuals
    run_n = 0
    for i, r in enumerate(rows):
        if mu_of[i] is None:
            out.append(None)
            continue
        resid_i = float(r[feature]) - mu_of[i]
        sigma_i = (run_ss / run_n) ** 0.5 if run_n >= _SIGMA_WARMUP else None
        if sigma_i and sigma_i > 0:
            out.append(float(np.clip(resid_i / sigma_i, -Z_CLIP, Z_CLIP)))
        else:
            out.append(None)  # warmup: sigma not yet estimable -> leave untilted
        run_ss += resid_i * resid_i
        run_n += 1
    return out


def _score_array(rows: list[dict[str, Any]], score: str) -> list[float | None]:
    if score == "oi":
        return _within_symbol_z(rows, "oi_chg_24h")
    if score == "funding":
        return _within_symbol_z(rows, "funding_level")
    if score == "combined":
        zo = _within_symbol_z(rows, "oi_chg_24h")
        zf = _within_symbol_z(rows, "funding_level")
        return [None if (zo[i] is None or zf[i] is None) else zo[i] + zf[i] for i in range(len(rows))]
    raise ValueError(f"unknown score {score!r}")


def _month_of(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m")


def _normalize_prior_month(
    raw: dict[tuple[str, int], float], meta: dict[str, Any]
) -> tuple[dict[tuple[str, int], float], dict[str, Any]]:
    """Divide every raw multiplier by the PRIOR calendar month's mean raw multiplier
    (fully causal — no within-month look-ahead) so the deployed gross tracks ~1. The
    first month uses 1.0. MAR is scale-invariant; the binding check is the realized
    ledger notional ratio reported per arm."""
    items = list(raw.items())  # insertion order == (ts, symbol) sort order
    month_ms: dict[str, list[float]] = defaultdict(list)
    for key, m in items:
        month_ms[_month_of(key[1])].append(m)
    months = sorted(month_ms)
    mean_by = {mon: (st.mean(month_ms[mon]) if month_ms[mon] else 1.0) for mon in months}
    prior = {months[i]: mean_by[months[i - 1]] for i in range(1, len(months))}
    if months:
        prior[months[0]] = 1.0
    lookup: dict[tuple[str, int], float] = {}
    for key, m in items:
        norm = prior.get(_month_of(key[1]), 1.0)
        lookup[key] = m / (norm if norm > 0 else 1.0)
    all_m = list(lookup.values())
    meta.update({
        "n_tilted": len(lookup), "mean_mult": (st.mean(all_m) if all_m else 1.0),
        "min_mult": (min(all_m) if all_m else 1.0), "max_mult": (max(all_m) if all_m else 1.0),
    })
    return lookup, meta


def _sizing_from_z(rows, z_vals, k, cap) -> dict[tuple[str, int], float]:
    raw: dict[tuple[str, int], float] = {}
    for i, r in enumerate(rows):
        z = z_vals[i]
        if z is None:
            continue  # no score -> engine default m=1
        raw[(str(r["symbol"]), int(r["signal_ts_ms"]))] = float(np.clip(1.0 + k * z, 1.0 - cap, 1.0 + cap))
    return raw


def _build_lookup(rows, arm: dict[str, Any]) -> tuple[dict[tuple[str, int], float] | None, dict[str, Any]]:
    kind = arm["kind"]
    if kind == "control":
        return None, {"kind": "control"}
    k, cap, score = arm["k"], arm["cap"], arm["score"]
    if kind == "real":
        z_vals = _score_array(rows, score)
    elif kind == "shuffle":
        z_vals = _score_array(rows, score)
        rng = np.random.default_rng(arm["seed"])
        by_sym: dict[str, list[int]] = defaultdict(list)
        for i, r in enumerate(rows):
            by_sym[str(r["symbol"])].append(i)
        shuffled: list[float | None] = list(z_vals)
        for idxs in by_sym.values():
            present = [i for i in idxs if z_vals[i] is not None]
            vals = [z_vals[i] for i in present]
            perm = rng.permutation(len(vals))
            for slot, i in enumerate(present):
                shuffled[i] = vals[perm[slot]]
        z_vals = shuffled
    elif kind == "randpsym":
        rng = np.random.default_rng(10_000 + arm["seed"])
        base = _score_array(rows, score)  # only to mark which rows are "covered"
        sym_draw: dict[str, float] = {}
        z_vals = []
        for i, r in enumerate(rows):
            s = str(r["symbol"])
            if s not in sym_draw:
                sym_draw[s] = float(np.clip(rng.standard_normal(), -Z_CLIP, Z_CLIP))
            z_vals.append(None if base[i] is None else sym_draw[s])
    else:
        raise ValueError(f"unknown arm kind {kind!r}")
    raw = _sizing_from_z(rows, z_vals, k, cap)
    lookup, meta = _normalize_prior_month(raw, {"kind": kind, "score": score, "k": k, "cap": cap})
    meta["seed"] = arm.get("seed")
    return lookup, meta


# ----------------------------- metrics helpers -----------------------------
def _subperiod_metrics(ledger: pl.DataFrame, lo_ts: int, hi_ts: int) -> dict[str, Any]:
    sub = ledger.filter((pl.col("ts_ms") >= lo_ts) & (pl.col("ts_ms") < hi_ts)).sort("ts_ms")
    if sub.is_empty():
        return {"mar": None, "total_return": 0.0, "max_drawdown": 0.0, "n_days": 0}
    sub = sub.with_columns((1.0 + pl.col("basket_return").cum_sum()).alias("equity"))
    sub = sub.with_columns((pl.col("equity") - pl.col("equity").cum_max()).alias("drawdown"))
    m = _daily_pnl_metrics(sub.select("ts_ms", "equity", "drawdown", "basket_return"))
    m["n_days"] = int(sub.height)
    return m


def _thirds(ledger: pl.DataFrame) -> list[dict[str, Any]]:
    days = sorted(int(t) for t in ledger["ts_ms"].to_list())
    if len(days) < 3:
        return []
    n = len(days)
    cuts = [days[0], days[n // 3], days[2 * n // 3], days[-1] + 1]
    return [_subperiod_metrics(ledger, cuts[i], cuts[i + 1]) for i in range(3)]


def _concentration(pieces_trades: dict[str, pl.DataFrame]) -> dict[str, Any]:
    sym_notional: dict[str, float] = defaultdict(float)
    total = 0.0
    for trades in pieces_trades.values():
        if trades.is_empty():
            continue
        for sym, nw in trades.select("symbol", "notional_weight").iter_rows():
            w = abs(float(nw))
            sym_notional[str(sym)] += w
            total += w
    if total <= 0:
        return {"total_notional": 0.0, "top5_share": None, "n_symbols": 0}
    shares = sorted((v / total for v in sym_notional.values()), reverse=True)
    return {"total_notional": total, "top5_share": float(sum(shares[:5])), "n_symbols": len(shares)}


def _run_arm(root: Path, venue: str, arm_name: str, lookup, start: str, end: str,
             cost_mult: float, run_tag: str) -> dict[str, Any]:
    pieces: dict[str, Any] = {}
    pieces_trades: dict[str, pl.DataFrame] = {}
    cfg_hashes: dict[str, str] = {}
    overrides = {} if cost_mult == 1.0 else {"round_trip_cost_multiplier": float(cost_mult)}
    t0 = time.time()
    for component, (_artifact_root, cell) in COMPONENTS.items():
        report_dir = root / "reports" / run_tag / arm_name / component
        cfg = _component_config(component, cell, start=start, end=end, arm_overrides=overrides)
        if getattr(cfg, "entry_pause_after_adverse_exits", 0):
            raise RuntimeError(
                "entry_pause_after_adverse_exits != 0: the circuit breaker is net_return-dependent, "
                "so sizing would change which entries are taken -> breadth NOT byte-identical to S0. "
                "This harness assumes the breaker is OFF (it is in the frozen control).")
        cfg_hashes[component] = cfg.config_hash()
        run_continuous_event_research(root, config=cfg, report_dir=report_dir, size_mult_lookup=lookup)
        comp, trades, _report = _load_component(report_dir)
        pieces[component] = comp
        pieces_trades[component] = trades
    all_days = sorted({day for comp in pieces.values() for day in comp.days})
    btc_rets, btc_funding = _btc_inputs(root, venue, all_days)
    ledger = build_full_ledger(pieces, btc_rets, btc_funding)
    arm_dir = root / "reports" / run_tag / arm_name
    arm_dir.mkdir(parents=True, exist_ok=True)
    ledger.write_csv(arm_dir / "ensemble_hedged_ledger.csv")
    full = _daily_pnl_metrics(ledger.select("ts_ms", "equity", "drawdown", "basket_return"))
    return {
        "arm": arm_name, "venue": venue, "cost_mult": cost_mult,
        "elapsed_s": round(time.time() - t0, 1),
        "n_trades": int(sum(int(t.height) for t in pieces_trades.values())),
        "n_ledger_days": int(ledger.height),
        "config_hashes": cfg_hashes, "full": full, "thirds": _thirds(ledger),
        "concentration": _concentration(pieces_trades),
    }


# ----------------------------- arm planning -----------------------------
def _plan_arms(scores: list[str], ks: list[float], caps: list[float], seeds: list[int],
               cost_mults: list[float], ctrl_k: float, ctrl_cap: float) -> list[dict[str, Any]]:
    """One arm per (kind, score, k, cap, seed, cost). S0 control reused per cost."""
    arms: list[dict[str, Any]] = []
    for cm in cost_mults:
        tag = "" if cm == 1.0 else f"_x{cm:g}"
        arms.append({"name": f"S0_control{tag}", "kind": "control", "cost_mult": cm,
                     "score": None, "k": None, "cap": None, "seed": None})
        for score in scores:
            for k in ks:
                for cap in caps:
                    arms.append({"name": f"R_{score}_k{k:g}_c{cap:g}{tag}", "kind": "real",
                                 "cost_mult": cm, "score": score, "k": k, "cap": cap, "seed": None})
        # Controls only at the primary cell, 1x cost (control distribution for the harvest test).
        if cm == 1.0:
            for score in scores:
                for seed in seeds:
                    arms.append({"name": f"shuffle_{score}_s{seed}", "kind": "shuffle", "cost_mult": cm,
                                 "score": score, "k": ctrl_k, "cap": ctrl_cap, "seed": seed})
                    arms.append({"name": f"randpsym_{score}_s{seed}", "kind": "randpsym", "cost_mult": cm,
                                 "score": score, "k": ctrl_k, "cap": ctrl_cap, "seed": seed})
    return arms


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    stage0_dir = Path(args.stage0).expanduser()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    scores = [s.strip() for s in args.scores.split(",") if s.strip()]
    ks = [float(x) for x in args.ks.split(",") if x.strip()]
    caps = [float(x) for x in args.caps.split(",") if x.strip()]
    seeds = [int(x) for x in args.seeds.split(",") if x.strip()] if args.seeds.strip() else []
    cost_mults = [float(x) for x in args.cost_mults.split(",") if x.strip()]
    code_hash = _code_hash()
    (out / "code_hash.txt").write_text(code_hash + "\n", encoding="utf-8")
    run_tag = out.name  # namespace engine report dirs by the out dir so probe/full don't collide
    arms = _plan_arms(scores, ks, caps, seeds, cost_mults, args.ctrl_k, args.ctrl_cap)
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": PREREG, "stage": "w6_a1_squeeze_proxy_sizing",
        "window": {"start": args.start, "end_exclusive": args.end}, "out": str(out),
        "sweep_tag": SWEEP_TAG, "run_tag": run_tag, "git_head": _git_head(), "code_hash": code_hash,
        "frozen_forward_config_hash": frozen_config_hash(), "run_label": "exploratory",
        "scores": scores, "ks": ks, "caps": caps, "seeds": seeds, "cost_mults": cost_mults,
        "ctrl_cell": {"k": args.ctrl_k, "cap": args.ctrl_cap}, "z_clip": Z_CLIP,
        "standardizer": STANDARDIZER, "roots": {v: str(ROOTS[v]) for v in venues},
        "n_arms_planned": len(arms), "pit_gate": {}, "coverage": {}, "venues": {}, "lookup_meta": {},
    }
    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        payload["pit_gate"][venue] = _pit_partition_gate(root, start=args.start, end=args.end)
        rows = _selected_rows(stage0_dir, venue)
        payload["coverage"][venue] = _attach_raw_features(rows, root, venue)
        payload["venues"][venue] = {"n_selected": len(rows), "arms": {}}
        payload["lookup_meta"].setdefault(venue, {})
        for arm in arms:
            if arm["kind"] != "control" and arm["score"] not in scores:
                continue
            lookup, meta = _build_lookup(rows, arm)
            payload["lookup_meta"][venue][arm["name"]] = meta
            res = _run_arm(root, venue, arm["name"], lookup, args.start, args.end, arm["cost_mult"], run_tag)
            payload["venues"][venue]["arms"][arm["name"]] = res
            print(f"[{venue}] {arm['name']}: trades={res['n_trades']} "
                  f"MAR={res['full'].get('mar')} ret={res['full'].get('total_return'):.4f} "
                  f"({res['elapsed_s']}s)", flush=True)
            # incremental persistence so a long run is auditable mid-flight
            (out / "summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    _decide(payload, scores)
    (out / "summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "summary.md")
    return payload


# ----------------------------- decision rule -----------------------------
def _mar(payload, venue, arm) -> float | None:
    a = payload["venues"].get(venue, {}).get("arms", {}).get(arm)
    if not a or a["full"].get("mar") is None:
        return None
    return float(a["full"]["mar"])


def _delta(payload, venue, arm, cost_tag="") -> float | None:
    s0 = _mar(payload, venue, f"S0_control{cost_tag}")
    am = _mar(payload, venue, arm)
    return None if (s0 is None or am is None) else am - s0


def _gross_ratio(payload, venue, arm, cost_tag="") -> float | None:
    """Realized booked-gross ratio of an arm vs its matched S0 control. A mean-1 sizing
    tilt must keep this in [0.95, 1.05]; outside that the dMAR is partly a leverage
    artifact, not a risk reallocation (the Stage-5 binance gross-creep trap)."""
    a = payload["venues"].get(venue, {}).get("arms", {}).get(arm)
    z0 = payload["venues"].get(venue, {}).get("arms", {}).get(f"S0_control{cost_tag}")
    if not a or not z0:
        return None
    an = a.get("concentration", {}).get("total_notional")
    zn = z0.get("concentration", {}).get("total_notional")
    if not an or not zn:
        return None
    return float(an) / float(zn)


def _thirds_delta_signs(payload, venue, arm) -> list[int]:
    a = payload["venues"][venue]["arms"].get(arm)
    s0 = payload["venues"][venue]["arms"].get("S0_control")
    if not a or not s0 or not a.get("thirds") or not s0.get("thirds"):
        return []
    signs = []
    for ta, t0 in zip(a["thirds"], s0["thirds"]):
        if ta.get("mar") is None or t0.get("mar") is None:
            signs.append(0)
        else:
            d = ta["mar"] - t0["mar"]
            signs.append(1 if d > 0 else (-1 if d < 0 else 0))
    return signs


def _decide(payload: dict[str, Any], scores: list[str]) -> None:
    venues = [v for v, b in payload["venues"].items() if "arms" in b]
    ks, caps = payload["ks"], payload["caps"]
    decisions: dict[str, Any] = {}
    for score in scores:
        grid = [f"R_{score}_k{k:g}_c{cap:g}" for k in ks for cap in caps]
        primary = f"R_{score}_k{payload['ctrl_cell']['k']:g}_c{payload['ctrl_cell']['cap']:g}"
        d: dict[str, Any] = {"score": score, "grid": grid, "primary": primary}
        # gate 1: bybit dMAR > 0 across the WHOLE grid
        if "bybit" in venues:
            grid_d = {a: _delta(payload, "bybit", a) for a in grid}
            d["bybit_grid_dmar"] = grid_d
            d["gate1_bybit_grid_all_pos"] = all(v is not None and v > 0 for v in grid_d.values())
        else:
            d["gate1_bybit_grid_all_pos"] = None
        # gate 3: >=2/3 thirds same-sign (positive) on bybit primary
        if "bybit" in venues:
            signs = _thirds_delta_signs(payload, "bybit", primary)
            d["bybit_thirds_signs"] = signs
            d["gate3_thirds_2of3_pos"] = (sum(1 for s in signs if s > 0) >= 2) if signs else None
        # gate 2: beats shuffle AND randpsym control distributions on bybit (primary cell)
        for ctrl in ("shuffle", "randpsym"):
            seeds = payload["seeds"]
            cds = [_delta(payload, "bybit", f"{ctrl}_{score}_s{s}") for s in seeds] if "bybit" in venues else []
            cds = [c for c in cds if c is not None]
            real_d = _delta(payload, "bybit", primary)
            d[f"ctrl_{ctrl}"] = {
                "n": len(cds), "deltas": cds,
                "mean": (st.mean(cds) if cds else None), "max": (max(cds) if cds else None),
                "real_dmar": real_d,
                "beats_max": (None if (real_d is None or not cds) else real_d > max(cds)),
                "beats_mean": (None if (real_d is None or not cds) else real_d > st.mean(cds)),
            }
        # gate 4: 1.5x cost stress (primary cell) still bybit-positive
        if "bybit" in payload["venues"] and any(cm != 1.0 for cm in payload["cost_mults"]):
            stress = [cm for cm in payload["cost_mults"] if cm != 1.0]
            d["bybit_cost_stress"] = {f"x{cm:g}": _delta(payload, "bybit", f"{primary}_x{cm:g}", f"_x{cm:g}")
                                      for cm in stress}
            d["gate4_cost_stress_pos"] = all(v is not None and v > 0 for v in d["bybit_cost_stress"].values())
        # gate 5: binance "not completely losing" (funding/combined variants; OI is bybit-only)
        binance_evaluable = "binance" in venues and (payload["coverage"].get("binance", {}).get(
            {"oi": "oi_chg_24h", "funding": "funding_level", "combined": "funding_level"}.get(score, "funding_level"), 0) >= 1)
        if "binance" in venues:
            d["binance_primary_dmar"] = _delta(payload, "binance", primary)
            d["binance_evaluable"] = binance_evaluable
            d["gate5_binance_not_losing"] = (None if not binance_evaluable else
                                             (d["binance_primary_dmar"] is not None
                                              and d["binance_primary_dmar"] >= -0.05))
        # gate 6: realized gross-ratio within [0.95,1.05] across the bybit grid (+ binance primary)
        gr = {}
        if "bybit" in venues:
            for a in grid:
                gr[f"bybit:{a}"] = _gross_ratio(payload, "bybit", a)
        if binance_evaluable:
            gr[f"binance:{primary}"] = _gross_ratio(payload, "binance", primary)
        d["gross_ratios"] = gr
        d["gate6_gross_ok"] = all(v is not None and 0.95 <= v <= 1.05 for v in gr.values()) if gr else None
        decisions[score] = d
    payload["decision"] = decisions
    # overall verdict per score: g1&g2&g3&g4&g6 always; g5 also required when binance is evaluable.
    passes = []
    for score, d in decisions.items():
        g1 = d.get("gate1_bybit_grid_all_pos")
        g2 = (d.get("ctrl_shuffle", {}).get("beats_max") and d.get("ctrl_randpsym", {}).get("beats_max"))
        g3 = d.get("gate3_thirds_2of3_pos")
        g4 = d.get("gate4_cost_stress_pos")
        g6 = d.get("gate6_gross_ok")
        core = bool(g1 and g2 and g3 and g4 and g6)
        if d.get("binance_evaluable"):
            core = core and bool(d.get("gate5_binance_not_losing"))
        d["verdict_pass"] = core
        passes.append(core)
    payload["verdict_pass"] = any(passes)


def _f(x, fmt="{:+.3f}") -> str:
    return "" if x is None else fmt.format(x)


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# W6 A1 — Squeeze-proxy sizing tilt (mean-1, gross-neutral, causal)",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`  Git: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`  Frozen cfg: `{payload['frozen_forward_config_hash']}`",
        f"- Window: `{payload['window']['start']} <= signal_ts < {payload['window']['end_exclusive']}`",
        f"- Scores `{payload['scores']}`  k `{payload['ks']}`  cap `{payload['caps']}`  "
        f"seeds `{payload['seeds']}`  cost `{payload['cost_mults']}`",
        f"- Coverage (raw feature non-null on selected): `{payload.get('coverage')}`",
        f"- Run label: `{payload['run_label']}`  Pre-registration: `{payload['preregistration']}`",
        "",
        f"## Verdict: {'PASS (>=1 score harvestable)' if payload.get('verdict_pass') else 'NULL (no score harvestable)'}",
        "",
        "## Per-arm (full window)",
        "",
        "| Venue | Arm | Trades | Return | MAR | MaxDD | mean_mult | gross_ratio | top5 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for venue, block in payload["venues"].items():
        s0n = block.get("arms", {}).get("S0_control", {}).get("concentration", {}).get("total_notional")
        for arm, a in block.get("arms", {}).items():
            mm = payload["lookup_meta"].get(venue, {}).get(arm, {}).get("mean_mult")
            fm, cc = a["full"], a["concentration"]
            an = cc.get("total_notional")
            gr = (an / s0n) if (an and s0n) else None
            lines.append(
                f"| `{venue}` | `{arm}` | {a['n_trades']} | {_f(fm.get('total_return'), '{:.4f}')} | "
                f"{_f(fm.get('mar'), '{:.3f}')} | {_f(fm.get('max_drawdown'), '{:.4f}')} | "
                f"{_f(mm, '{:.3f}')} | {_f(gr, '{:.4f}')} | {_f(cc.get('top5_share'), '{:.3f}')} |")
    lines += ["", "## Decision (per score, bybit-primary)", ""]
    for score, d in payload.get("decision", {}).items():
        lines.append(f"### `{score}`")
        lines.append(f"- gate1 bybit grid all dMAR>0: **{d.get('gate1_bybit_grid_all_pos')}**  "
                     f"grid={ {a: _f(v) for a, v in (d.get('bybit_grid_dmar') or {}).items()} }")
        lines.append(f"- gate3 thirds >=2/3 pos: **{d.get('gate3_thirds_2of3_pos')}**  "
                     f"signs={d.get('bybit_thirds_signs')}")
        for ctrl in ("shuffle", "randpsym"):
            c = d.get(f"ctrl_{ctrl}", {})
            lines.append(f"- gate2 vs {ctrl}: real={_f(c.get('real_dmar'))} "
                         f"ctrl mean={_f(c.get('mean'))} max={_f(c.get('max'))} "
                         f"beats_max=**{c.get('beats_max')}** beats_mean={c.get('beats_mean')} (n={c.get('n')})")
        if "gate4_cost_stress_pos" in d:
            lines.append(f"- gate4 cost stress pos: **{d.get('gate4_cost_stress_pos')}**  "
                         f"{ {kk: _f(vv) for kk, vv in (d.get('bybit_cost_stress') or {}).items()} }")
        if "gate5_binance_not_losing" in d:
            lines.append(f"- gate5 binance not losing: **{d.get('gate5_binance_not_losing')}**  "
                         f"binance primary dMAR={_f(d.get('binance_primary_dmar'))} "
                         f"(evaluable={d.get('binance_evaluable')})")
        lines.append(f"- gate6 gross-ratio in [0.95,1.05]: **{d.get('gate6_gross_ok')}**  "
                     f"{ {kk: _f(vv, '{:.4f}') for kk, vv in (d.get('gross_ratios') or {}).items()} }")
        lines.append(f"- **score verdict: {d.get('verdict_pass')}**")
        lines.append("")
    lines += ["Admissibility != harvestable. A pass nominates a demo/paper forward-watch shadow",
              "(operator-gated); Tier-3 real-money gate UNCHANGED.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--start", default="2023-04-01")
    parser.add_argument("--end", default="2026-05-01")
    parser.add_argument("--stage0", default=str(SHARED / STAGE0_TAG))
    parser.add_argument("--scores", default="oi,funding,combined")
    parser.add_argument("--ks", default="0.25,0.5")
    parser.add_argument("--caps", default="0.5,1.0")
    parser.add_argument("--seeds", default="0,1,2,3,4,5,6,7,8,9")
    parser.add_argument("--cost-mults", dest="cost_mults", default="1.0,1.5")
    parser.add_argument("--ctrl-k", dest="ctrl_k", type=float, default=0.5)
    parser.add_argument("--ctrl-cap", dest="ctrl_cap", type=float, default=1.0)
    parser.add_argument("--out", default=str(SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "verdict_pass": payload.get("verdict_pass"),
                      "decision": {s: {"gate1": d.get("gate1_bybit_grid_all_pos"),
                                       "gate3": d.get("gate3_thirds_2of3_pos")}
                                   for s, d in payload.get("decision", {}).items()}}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
