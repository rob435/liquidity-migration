"""Trade-level forensics for long-only momentum factor research.

Enriches individual trades with path stats and cross-sectional context, pools
many sweep variants into a large ledger, discovers simple entry filters on a
train window, and simulates filtered basket books for honest daily Sharpe.

Run label: exploratory — trade-level filter mining is hypothesis generation,
not promotion evidence until re-run through the full factor pipeline on held-out
time and OOS roots.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import polars as pl

from ._common import date_ms
from .config import TradeLifecycleConfig
from .momentum_factor import MomentumFactorConfig
from .trade_lifecycle import build_equity_curve, summarize_baskets, summarize_trade_backtest


TRADE_SPLIT_TRAIN_END = "2024-05-03"
TRADE_SPLIT_VAL_END = "2025-05-03"

# Entry-time features only — path_mfe/mae are post-hoc and must NOT be used for filters.
FILTER_FEATURES = (
    "momentum_avg",
    "ts_momentum",
    "carry",
    "realized_vol",
    "score",
    "cs_rank_pct",
    "universe_breadth",
    "momentum_dispersion",
    "btc_7d_return",
    "turnover_rank",
)

# Post-hoc path stats — segmentation tables only, never filter discovery.
PATH_FEATURES = ("path_mfe", "path_mae", "path_efficiency")


@dataclass(frozen=True, slots=True)
class TradeFilterRule:
    feature: str
    op: str  # ">=" or "<="
    threshold: float

    def passes(self, row: dict[str, Any]) -> bool:
        val = row.get(self.feature)
        if val is None or (isinstance(val, float) and not math.isfinite(val)):
            return False
        v = float(val)
        if self.op == ">=":
            return v >= self.threshold
        if self.op == "<=":
            return v <= self.threshold
        raise ValueError(f"unsupported op {self.op!r}")


@dataclass(frozen=True, slots=True)
class FilterDiscoveryResult:
    rules: tuple[TradeFilterRule, ...]
    train_trades: int
    train_mean_net: float
    train_sharpe_proxy: float
    val_trades: int
    val_mean_net: float
    val_sharpe_proxy: float
    test_trades: int
    test_mean_net: float
    test_sharpe_proxy: float
    basket_sharpe_test: float
    basket_total_return_test: float


def enrich_trades(
    trades: pl.DataFrame,
    *,
    features: pl.DataFrame,
    bars_by_symbol: dict[str, dict[str, Any]],
    btc_symbol: str = "BTCUSDT",
) -> pl.DataFrame:
    """Attach path stats and signal-day cross-sectional context to each trade."""
    if trades.is_empty():
        return trades

    feat_ctx = _build_signal_context(features, btc_symbol=btc_symbol)
    rows: list[dict[str, Any]] = []
    for row in trades.iter_rows(named=True):
        enriched = dict(row)
        key = (row["symbol"], int(row["entry_signal_ts_ms"]))
        ctx = feat_ctx.get(key, {})
        enriched.update(ctx)
        path = _path_stats(
            bars_by_symbol.get(str(row["symbol"])),
            entry_ts_ms=int(row["entry_ts_ms"]),
            exit_ts_ms=int(row["exit_ts_ms"]),
            entry_price=float(row["entry_price"]),
            side=str(row["side"]),
        )
        enriched.update(path)
        rows.append(enriched)
    return pl.DataFrame(rows, infer_schema_length=None)


def _build_signal_context(features: pl.DataFrame, *, btc_symbol: str) -> dict[tuple[str, int], dict[str, Any]]:
    """Per (symbol, signal_ts_ms): ranks, breadth, dispersion, BTC trend."""
    if features.is_empty():
        return {}

    # Universe rows only for cross-sectional stats.
    uni = features.filter(pl.col("in_universe") == True)  # noqa: E712
    if uni.is_empty():
        return {}

    mom_cols = [c for c in uni.columns if c.startswith("momentum_") and c.endswith("d")]
    btc = (
        features.filter(pl.col("symbol") == btc_symbol)
        .sort("ts_ms")
        .select(["ts_ms", "close"])
    )
    btc_ret: dict[int, float] = {}
    if btc.height >= 8:
        b = btc.with_columns(
            (pl.col("close") / pl.col("close").shift(7) - 1.0).alias("btc_7d_return")
        )
        for r in b.iter_rows(named=True):
            if r["btc_7d_return"] is not None and math.isfinite(float(r["btc_7d_return"])):
                btc_ret[int(r["ts_ms"])] = float(r["btc_7d_return"])

    ctx: dict[tuple[str, int], dict[str, Any]] = {}
    for part in uni.partition_by("ts_ms", maintain_order=True):
        ts = int(part["ts_ms"][0])
        n = part.height
        if n == 0:
            continue
        breadth = float((part["log_return"] > 0).mean()) if "log_return" in part.columns else float("nan")
        mom_vals = []
        for c in mom_cols:
            mom_vals.extend([float(v) for v in part[c].to_list() if v is not None and math.isfinite(float(v))])
        dispersion = float(np.std(mom_vals)) if len(mom_vals) >= 2 else float("nan")
        btc_7d = btc_ret.get(ts, float("nan"))

        scores = []
        for r in part.iter_rows(named=True):
            m_vals = [float(r[c]) for c in mom_cols if r.get(c) is not None and math.isfinite(float(r[c]))]
            if not m_vals:
                continue
            scores.append((r["symbol"], float(np.mean(m_vals))))

        if not scores:
            continue
        scores.sort(key=lambda x: x[1], reverse=True)
        rank_map = {sym: i + 1 for i, (sym, _) in enumerate(scores)}

        for r in part.iter_rows(named=True):
            sym = str(r["symbol"])
            rank = rank_map.get(sym)
            ctx[(sym, ts)] = {
                "turnover_rank": int(r["turnover_rank"]) if r.get("turnover_rank") is not None else None,
                "cs_rank_pct": (rank / n) if rank is not None else float("nan"),
                "universe_breadth": breadth,
                "momentum_dispersion": dispersion,
                "btc_7d_return": btc_7d,
                "regime_on": bool(r.get("regime_on", False)),
            }
    return ctx


def _path_stats(
    bars: dict[str, Any] | None,
    *,
    entry_ts_ms: int,
    exit_ts_ms: int,
    entry_price: float,
    side: str,
) -> dict[str, float]:
    if bars is None or entry_price <= 0:
        return {"path_mfe": 0.0, "path_mae": 0.0, "path_efficiency": 0.0}

    ends = bars["ends"]
    closes = bars["close"]
    best = 0.0
    worst = 0.0
    for i in range(len(ends)):
        if ends[i] < entry_ts_ms:
            continue
        if ends[i] > exit_ts_ms:
            break
        px = float(closes[i])
        if not math.isfinite(px):
            continue
        r = px / entry_price - 1.0
        if side == "short":
            r = -r
        best = max(best, r)
        worst = min(worst, r)

    exit_r = 0.0
    exit_idx = bars["by_end"].get(exit_ts_ms)
    if exit_idx is not None:
        exit_r = float(closes[exit_idx]) / entry_price - 1.0
        if side == "short":
            exit_r = -exit_r

    efficiency = exit_r / best if best > 1e-9 else 0.0
    return {"path_mfe": best, "path_mae": worst, "path_efficiency": efficiency}


def assign_trade_split(trades: pl.DataFrame) -> pl.DataFrame:
    train_end = date_ms(TRADE_SPLIT_TRAIN_END)
    val_end = date_ms(TRADE_SPLIT_VAL_END)

    def _split(ts: int) -> str:
        if ts < train_end:
            return "train"
        if ts < val_end:
            return "val"
        return "test"

    return trades.with_columns(
        pl.col("entry_signal_ts_ms")
        .map_elements(_split, return_dtype=pl.String)
        .alias("trade_split")
    )


def discover_single_axis_filters(
    train: pl.DataFrame,
    *,
    features: tuple[str, ...] = FILTER_FEATURES,
    min_trades: int = 40,
) -> list[dict[str, Any]]:
    """Grid-search one-feature thresholds maximizing train Sharpe proxy."""
    results: list[dict[str, Any]] = []
    for feat in features:
        if feat not in train.columns:
            continue
        series = train[feat]
        vals = sorted(
            float(v)
            for v in series.to_list()
            if v is not None and isinstance(v, (int, float)) and math.isfinite(float(v))
        )
        if len(vals) < min_trades:
            continue
        if len(vals) < 10:
            continue
        # Test decile thresholds for both directions.
        candidates = sorted({vals[int(len(vals) * q)] for q in (0.1, 0.2, 0.3, 0.5, 0.7, 0.8, 0.9)})
        for thr in candidates:
            for op in (">=", "<="):
                rule = TradeFilterRule(feat, op, thr)
                sub = _apply_rules(train, (rule,))
                if sub.height < min_trades:
                    continue
                proxy = _trade_sharpe_proxy(sub)
                results.append(
                    {
                        "feature": feat,
                        "op": op,
                        "threshold": thr,
                        "train_trades": sub.height,
                        "train_sharpe_proxy": proxy,
                        "train_mean_net": float(sub["net_return"].mean()),
                        "train_win_rate": float((sub["net_return"] > 0).mean()),
                    }
                )
    results.sort(key=lambda r: r["train_sharpe_proxy"], reverse=True)
    return results


def discover_combo_filters(
    pooled: pl.DataFrame,
    singles: list[dict[str, Any]],
    *,
    top_n_singles: int = 8,
    max_combo_size: int = 3,
    min_trades: int = 35,
) -> list[FilterDiscoveryResult]:
    """Combine top single-axis rules; rank by validation Sharpe proxy."""
    base_rules = [
        TradeFilterRule(s["feature"], s["op"], float(s["threshold"]))
        for s in singles[:top_n_singles]
    ]
    val = pooled.filter(pl.col("trade_split") == "val") if "trade_split" in pooled.columns else pl.DataFrame()
    test = pooled.filter(pl.col("trade_split") == "test") if "trade_split" in pooled.columns else pl.DataFrame()
    train_only = pooled.filter(pl.col("trade_split") == "train") if "trade_split" in pooled.columns else pooled

    discovered: list[FilterDiscoveryResult] = []
    for size in range(1, max_combo_size + 1):
        for combo in combinations(base_rules, size):
            sub_train = _apply_rules(train_only, combo)
            if sub_train.height < min_trades:
                continue
            sub_val = _apply_rules(val, combo) if not val.is_empty() else pl.DataFrame()
            sub_test = _apply_rules(test, combo) if not test.is_empty() else pl.DataFrame()
            basket_test = simulate_filtered_baskets(sub_test, rules=combo)
            discovered.append(
                FilterDiscoveryResult(
                    rules=combo,
                    train_trades=sub_train.height,
                    train_mean_net=float(sub_train["net_return"].mean()),
                    train_sharpe_proxy=_trade_sharpe_proxy(sub_train),
                    val_trades=sub_val.height,
                    val_mean_net=float(sub_val["net_return"].mean()) if not sub_val.is_empty() else 0.0,
                    val_sharpe_proxy=_trade_sharpe_proxy(sub_val) if not sub_val.is_empty() else 0.0,
                    test_trades=sub_test.height,
                    test_mean_net=float(sub_test["net_return"].mean()) if not sub_test.is_empty() else 0.0,
                    test_sharpe_proxy=_trade_sharpe_proxy(sub_test) if not sub_test.is_empty() else 0.0,
                    basket_sharpe_test=_basket_sharpe(basket_test),
                    basket_total_return_test=float(basket_test["basket_return"].sum()) if not basket_test.is_empty() else 0.0,
                )
            )
    # Rank by validation basket Sharpe (causal features only).
    discovered.sort(key=lambda r: (r.basket_sharpe_test, r.val_sharpe_proxy), reverse=True)
    return discovered


def _apply_rules(df: pl.DataFrame, rules: Iterable[TradeFilterRule]) -> pl.DataFrame:
    if df.is_empty() or not rules:
        return df
    mask = pl.lit(True)
    for rule in rules:
        col = pl.col(rule.feature)
        if rule.op == ">=":
            mask = mask & (col >= rule.threshold)
        else:
            mask = mask & (col <= rule.threshold)
    return df.filter(mask)


def simulate_filtered_baskets(
    trades: pl.DataFrame,
    *,
    rules: Iterable[TradeFilterRule],
    hold_days: int = 7,
) -> pl.DataFrame:
    """Re-aggregate filtered trades into baskets (equal-weight within basket)."""
    filtered = _apply_rules(trades, rules)
    if filtered.is_empty():
        return pl.DataFrame({"basket_id": [], "basket_return": [], "entry_signal_ts_ms": []})
    grouped = (
        filtered.group_by("basket_id")
        .agg(
            pl.col("net_return").sum().alias("basket_return"),
            pl.col("entry_signal_ts_ms").first().alias("entry_signal_ts_ms"),
            pl.len().alias("trade_count"),
        )
        .sort("entry_signal_ts_ms")
    )
    return grouped


def _trade_sharpe_proxy(trades: pl.DataFrame) -> float:
    if trades.height < 5:
        return 0.0
    x = trades["net_return"].to_numpy()
    stdev = float(np.std(x, ddof=1))
    if stdev < 1e-12:
        return 0.0
    return float(np.mean(x) / stdev * math.sqrt(min(trades.height, 252)))


def _basket_sharpe(baskets: pl.DataFrame) -> float:
    if baskets.is_empty() or baskets.height < 5:
        return 0.0
    x = baskets["basket_return"].to_numpy()
    stdev = float(np.std(x, ddof=1))
    if stdev < 1e-12:
        return 0.0
    annual = math.sqrt(365.0 / 7.0)  # weekly-ish rebalance default
    return float(np.mean(x) / stdev * annual)


def daily_sharpe_from_equity(equity: pl.DataFrame) -> float:
    """Honest calendar Sharpe from daily equity returns."""
    if equity.is_empty() or "equity" not in equity.columns:
        return 0.0
    rets = equity["equity"].pct_change().drop_nulls()
    if rets.len() < 10:
        return 0.0
    x = rets.to_numpy()
    stdev = float(np.std(x, ddof=1))
    if stdev < 1e-12:
        return 0.0
    return float(np.mean(x) / stdev * math.sqrt(365.0))


def archetype_labels(trades: pl.DataFrame) -> pl.DataFrame:
    """Jane-Street-style coarse trade archetypes for segmentation tables."""
    if trades.is_empty():
        return trades

    def _label(row: dict[str, Any]) -> str:
        mom = float(row.get("momentum_avg") or 0.0)
        ts = float(row.get("ts_momentum") or 0.0)
        vol = float(row.get("realized_vol") or 1.0)
        carry = float(row.get("carry") or 0.0)
        mfe = float(row.get("path_mfe") or 0.0)
        if mom > 0.5 and ts > 0.5:
            return "rocket"
        if vol > 1.5:
            return "high_beta_lottery"
        if carry > 0.005:
            return "crowded_long"
        if mom < 0.15 and mfe > 0.05:
            return "slow_grind"
        if mom > 0.25 and ts < 0.2:
            return "fade_risk"
        return "core_momentum"

    labels = [_label(r) for r in trades.iter_rows(named=True)]
    return trades.with_columns(pl.Series("archetype", labels))


def archetype_summary(trades: pl.DataFrame) -> pl.DataFrame:
    trades = archetype_labels(trades)
    return (
        trades.group_by(["trade_split", "archetype"])
        .agg(
            pl.len().alias("n"),
            pl.col("net_return").mean().alias("mean_net"),
            pl.col("net_return").sum().alias("sum_net"),
            (pl.col("net_return") > 0).mean().alias("win_rate"),
        )
        .sort(["trade_split", "mean_net"], descending=[False, True])
    )


def rules_to_factor_config(rules: Iterable[TradeFilterRule], base: MomentumFactorConfig) -> MomentumFactorConfig:
    """Map discovered trade filters to pipeline entry gates (best-effort)."""
    from dataclasses import replace

    kwargs: dict[str, Any] = {}
    for rule in rules:
        if rule.feature == "ts_momentum" and rule.op == ">=":
            kwargs["min_ts_momentum"] = rule.threshold
        elif rule.feature == "momentum_avg" and rule.op == ">=":
            kwargs["min_momentum_avg"] = rule.threshold
        elif rule.feature == "carry" and rule.op == "<=":
            kwargs["max_carry"] = rule.threshold
        elif rule.feature == "realized_vol" and rule.op == "<=":
            kwargs["max_realized_vol"] = rule.threshold
        elif rule.feature == "score" and rule.op == ">=":
            kwargs["min_composite_score"] = rule.threshold
        elif rule.feature == "turnover_rank" and rule.op == "<=":
            kwargs["max_turnover_rank"] = int(rule.threshold)
        elif rule.feature == "turnover_rank" and rule.op == ">=":
            kwargs["min_turnover_rank"] = int(rule.threshold)
    return replace(base, **kwargs)


def run_forensics_report(
    pooled_trades: pl.DataFrame,
    *,
    output_dir: str | Path,
    hold_days: int = 7,
) -> dict[str, Any]:
    """Full forensics pipeline on a pooled trade ledger."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    pooled = assign_trade_split(pooled_trades)
    pooled = archetype_labels(pooled)
    pooled.write_csv(out / "pooled_trades_enriched.csv")

    arch = archetype_summary(pooled)
    arch.write_csv(out / "archetype_by_split.csv")

    train = pooled.filter(pl.col("trade_split") == "train")
    singles = discover_single_axis_filters(train)
    pl.DataFrame(singles).write_csv(out / "single_axis_filters.csv")

    combos = discover_combo_filters(pooled, singles)
    combo_rows = [
        {
            **{f"rule_{i}": str(r) for i, r in enumerate(c.rules)},
            "rules": [asdict(r) for r in c.rules],
            **{k: getattr(c, k) for k in c.__dataclass_fields__ if k != "rules"},
        }
        for c in combos[:50]
    ]
    (out / "combo_filters.json").write_text(json.dumps(combo_rows, indent=2), encoding="utf-8")

    best = combos[0] if combos else None
    metadata = {
        "pooled_trades": pooled.height,
        "splits": pooled.group_by("trade_split").len().to_dicts(),
        "top_single_axis": singles[:15],
        "best_combo": asdict(best) if best else None,
        "best_rules": [asdict(r) for r in best.rules] if best else [],
        "run_label": "exploratory_trade_forensics",
    }
    (out / "forensics_report.json").write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")
    return metadata


def filtered_backtest_metrics(
    trades: pl.DataFrame,
    *,
    rules: Iterable[TradeFilterRule],
    hold_days: int = 7,
    cost_multiplier: float = 3.0,
) -> dict[str, float]:
    """Basket + daily Sharpe on filtered trade ledger."""
    filtered = _apply_rules(trades, rules)
    bt_config = TradeLifecycleConfig(
        score="momentum_factor_filtered",
        hold_days=hold_days,
        rebalance_days=hold_days,
        cost_multiplier=cost_multiplier,
    )
    baskets = summarize_baskets(filtered, config=bt_config)
    equity = build_equity_curve(baskets)
    summary = summarize_trade_backtest(filtered, baskets, equity, config=bt_config)
    return {
        **summary,
        "daily_sharpe": daily_sharpe_from_equity(equity),
        "trades_kept": filtered.height,
        "trades_total": trades.height,
        "keep_rate": filtered.height / trades.height if trades.height else 0.0,
    }
