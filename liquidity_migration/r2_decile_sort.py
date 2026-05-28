"""R2 per-feature standalone decile-sort analytics.

Implements the descriptive backtest spec in
``docs/preregistration/2026-05-28-r2-per-feature-standalone.md``:

- :func:`decile_spread_pnl` — daily P&L from shorting the top-decile-by-feature
  basket (risk-equal weighted), held for ``horizon`` days, realized on
  signal date (a documented simplification valid for descriptive analysis;
  R9 uses proper position-lifecycle accounting).
- :func:`summarize_pnl_series` — total_return / annualized / max_drawdown /
  sharpe / MAR / signal-day count given a daily P&L series.
- :func:`spearman_correlation_matrix` — N × N rank-correlation matrix over
  per-feature daily P&L series.
- :func:`pca_variance_shares` — variance shares per PCA component from the
  per-feature daily P&L matrix.

Convention: ``max_drawdown`` is negative (-0.42 = -42%). ``cum_return``
is the multiplier-minus-one (5.1876 = +518.76%). MAR is positive when
profitable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import polars as pl

TRADING_DAYS_PER_YEAR = 365  # crypto trades 7 days/week; calendar-day basis


@dataclass(frozen=True, slots=True)
class DecileSpreadSummary:
    """Per-cell summary metrics for an R2 decile-sort run."""

    feature: str
    horizon: int
    venue: str
    window_days: int
    n_signal_days: int
    total_signals: int
    total_return: float
    annualized_return: float
    max_drawdown: float
    sharpe_like: float
    mar: float


def decile_spread_pnl(
    panel: pl.DataFrame,
    *,
    feature: str,
    horizon: int,
    top_decile: float = 0.10,
    cost_round_trip_bps: float = 18.0,
    realized_vol_col: str = "realized_vol_7d",
    use_risk_weights: bool = True,
) -> pl.DataFrame:
    """Per-day P&L from shorting the top-decile-by-feature basket.

    Returns a per-day frame with columns:
      ``date``, ``n_signals``, ``daily_pnl``, ``equity``, ``drawdown``.

    P&L formulation (per the pre-reg, descriptive simplification):
      - Each day with ≥ ``ceil(top_decile * n_eligible)`` non-null feature
        values produces a "signal day".
      - Top decile = symbols with feature value in the top
        ``top_decile`` fraction of that day's eligible cross-section.
      - Position is SHORT each top-decile name. Per-name realized return
        ≈ ``-fwd_ret_horizon_d``. ``fwd_ret`` already accounts for the
        +1h entry-delay convention via the signal_harness panel build.
      - Risk weights (``use_risk_weights=True``, default) — per-name
        weight ∝ 1 / ``realized_vol_<col>``, normalized so the basket
        sums to unit gross exposure.
      - Equal weights (``use_risk_weights=False``) — per-name weight =
        1 / K_D where K_D is the top-decile count on day D.
      - Round-trip cost ``cost_round_trip_bps`` (default 18 bps =
        ``cost_multiplier=3`` × 6 bps round-trip) is subtracted per
        basket per signal day. Cost is independent of position size in
        the simple model.

    Compounding: equity_D = equity_{D-1} × (1 + day_pnl_D) on signal days;
    flat on non-signal days. Initial equity = 1.0; final cum_return =
    equity_end - 1.

    Drawdown_t = (equity_t - max(equity_s, s ≤ t)) / max(equity_s, s ≤ t).
    """
    if feature not in panel.columns:
        raise KeyError(f"panel missing feature column {feature!r}")
    fwd_col = f"fwd_ret_{horizon}d"
    if fwd_col not in panel.columns:
        raise KeyError(f"panel missing forward-return column {fwd_col!r}")
    if use_risk_weights and realized_vol_col not in panel.columns:
        raise KeyError(
            f"panel missing realized_vol column {realized_vol_col!r}; "
            f"either include it in the build_feature_panel feature set or pass use_risk_weights=False"
        )
    if not (0.0 < top_decile <= 0.5):
        raise ValueError(f"top_decile must be in (0, 0.5], got {top_decile}")

    cost_round_trip = cost_round_trip_bps / 10_000.0  # bps → fraction

    # Filter to rows with both the feature value AND the forward return
    # available. fwd_ret is null at the right edge of the panel where
    # horizon extends past the data window.
    df = panel.filter(
        pl.col(feature).is_not_null() & pl.col(fwd_col).is_not_null()
    )

    if df.is_empty():
        return pl.DataFrame(
            schema={
                "date": pl.String,
                "n_signals": pl.Int64,
                "daily_pnl": pl.Float64,
                "equity": pl.Float64,
                "drawdown": pl.Float64,
            }
        )

    # Per-day rank: descending so the LARGEST feature values get the smallest
    # rank (rank 1 = the most-extreme-by-feature). signal_rank_frac =
    # rank / day_count is in (0, 1]; top decile = signal_rank_frac ≤ top_decile.
    df = df.with_columns(
        (
            pl.col(feature)
            .rank(method="min", descending=True)
            .over("ts_ms")
            .alias("_rank_desc")
        ),
        pl.col(feature).count().over("ts_ms").alias("_xs_n"),
    ).with_columns(
        (pl.col("_rank_desc") / pl.col("_xs_n")).alias("_signal_rank_frac")
    )

    top_basket = df.filter(pl.col("_signal_rank_frac") <= top_decile)

    if top_basket.is_empty():
        return pl.DataFrame(
            schema={
                "date": pl.String,
                "n_signals": pl.Int64,
                "daily_pnl": pl.Float64,
                "equity": pl.Float64,
                "drawdown": pl.Float64,
            }
        )

    # Per-name weight pre-normalization. For risk-equal: 1 / realized_vol;
    # for equal-weight: 1.0 each.
    if use_risk_weights:
        weight_expr = pl.when(pl.col(realized_vol_col) > 0.0).then(
            1.0 / pl.col(realized_vol_col)
        ).otherwise(0.0)
    else:
        weight_expr = pl.lit(1.0)

    top_basket = top_basket.with_columns(weight_expr.alias("_raw_weight"))

    # Per-day total weight for normalization (so basket sums to 1.0).
    day_grouped = (
        top_basket.group_by("ts_ms", maintain_order=True)
        .agg(
            pl.col("date").first().alias("date"),
            pl.len().alias("n_signals"),
            pl.col("_raw_weight").sum().alias("_basket_weight_sum"),
            (pl.col(fwd_col) * pl.col("_raw_weight")).sum().alias("_weighted_fwd_ret_sum"),
        )
        .with_columns(
            pl.when(pl.col("_basket_weight_sum") > 0.0)
            .then(-pl.col("_weighted_fwd_ret_sum") / pl.col("_basket_weight_sum") - cost_round_trip)
            .otherwise(0.0)
            .alias("daily_pnl")
        )
        .sort("ts_ms")
        .select("date", "n_signals", "daily_pnl")
    )

    # Equity curve + drawdown (cumulative product, then peak-to-date).
    pnl_values = day_grouped["daily_pnl"].to_numpy()
    equity = np.cumprod(1.0 + pnl_values)
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak

    return day_grouped.with_columns(
        pl.Series("equity", equity),
        pl.Series("drawdown", drawdown),
    )


def summarize_pnl_series(
    pnl_df: pl.DataFrame,
    *,
    feature: str,
    horizon: int,
    venue: str,
    window_days: int,
) -> DecileSpreadSummary:
    """Reduce a per-day P&L frame from :func:`decile_spread_pnl` to summary
    metrics. ``window_days`` should be the calendar-day span of the backtest
    (used for annualization), independent of the number of signal days."""
    if window_days <= 0:
        raise ValueError(f"window_days must be > 0, got {window_days}")

    if pnl_df.is_empty():
        return DecileSpreadSummary(
            feature=feature,
            horizon=horizon,
            venue=venue,
            window_days=window_days,
            n_signal_days=0,
            total_signals=0,
            total_return=0.0,
            annualized_return=0.0,
            max_drawdown=0.0,
            sharpe_like=0.0,
            mar=0.0,
        )

    n_signal_days = int(pnl_df.height)
    total_signals = int(pnl_df["n_signals"].sum())
    final_equity = float(pnl_df["equity"].tail(1)[0])
    total_return = final_equity - 1.0
    max_drawdown = float(pnl_df["drawdown"].min())  # negative

    # Geometric annualization on the calendar-day window.
    growth = 1.0 + total_return
    if growth <= 0:
        annualized_return = -1.0
    else:
        annualized_return = growth ** (365.25 / window_days) - 1.0

    # Sharpe — mean / std over signal-day P&L, annualized.
    daily_pnl = pnl_df["daily_pnl"].to_numpy()
    if daily_pnl.size < 2:
        sharpe_like = 0.0
    else:
        std = float(daily_pnl.std(ddof=1))
        mean = float(daily_pnl.mean())
        # Annualize sqrt(signal_days_per_year) — but signal_days is variable per
        # feature. For comparability across features we use sqrt(N) over the
        # full window's calendar-trading-day basis (TRADING_DAYS_PER_YEAR).
        sharpe_like = (mean / std * math.sqrt(TRADING_DAYS_PER_YEAR)) if std > 0 else 0.0

    mar = annualized_return / abs(max_drawdown) if max_drawdown != 0 else 0.0

    return DecileSpreadSummary(
        feature=feature,
        horizon=horizon,
        venue=venue,
        window_days=window_days,
        n_signal_days=n_signal_days,
        total_signals=total_signals,
        total_return=total_return,
        annualized_return=annualized_return,
        max_drawdown=max_drawdown,
        sharpe_like=sharpe_like,
        mar=mar,
    )


def spearman_correlation_matrix(
    per_feature_pnl: dict[str, pl.DataFrame],
    feature_names: list[str],
) -> pl.DataFrame:
    """Spearman rank-correlation matrix over per-feature daily P&L series.

    ``per_feature_pnl[feature]`` is a frame with columns ``date`` and
    ``daily_pnl``. The matrix is computed on the **intersection** of signal
    days across features — features with disjoint signal-day sets contribute
    nothing to the pairwise sample. If the intersection is < 5 days for any
    pair, that cell of the matrix is NaN with a stderr note.

    Returns an N × N DataFrame with feature names as both the index column
    (``feature``) and the column headers.
    """
    if not feature_names:
        raise ValueError("feature_names must be non-empty")
    missing = [f for f in feature_names if f not in per_feature_pnl]
    if missing:
        raise KeyError(f"per_feature_pnl missing entries for {missing}")

    # Align all per-feature pnl series on `date`, producing a wide frame
    # `date, pnl_<feature1>, pnl_<feature2>, ...`.
    aligned: pl.DataFrame | None = None
    for f in feature_names:
        sub = per_feature_pnl[f].select(
            pl.col("date"),
            pl.col("daily_pnl").alias(f"pnl_{f}"),
        )
        aligned = sub if aligned is None else aligned.join(sub, on="date", how="full", coalesce=True)
    assert aligned is not None

    n_features = len(feature_names)
    matrix = np.full((n_features, n_features), np.nan)

    for i in range(n_features):
        for j in range(n_features):
            if i == j:
                matrix[i, j] = 1.0
                continue
            col_i = f"pnl_{feature_names[i]}"
            col_j = f"pnl_{feature_names[j]}"
            paired = aligned.select(col_i, col_j).drop_nulls()
            if paired.height < 5:
                continue  # leave NaN
            a = paired[col_i].to_numpy()
            b = paired[col_j].to_numpy()
            # Spearman = Pearson on ranks
            a_rank = _rank_average(a)
            b_rank = _rank_average(b)
            corr = float(np.corrcoef(a_rank, b_rank)[0, 1])
            matrix[i, j] = corr

    out_cols = [pl.Series("feature", feature_names)] + [
        pl.Series(feature_names[j], matrix[:, j].tolist())
        for j in range(n_features)
    ]
    return pl.DataFrame(out_cols)


def _rank_average(arr: np.ndarray) -> np.ndarray:
    """Average-method rank — ties get the mean of their tied ranks. Matches
    polars's `rank(method="average")` and scipy.stats.rankdata default."""
    sorter = np.argsort(arr, kind="stable")
    ranks = np.empty_like(sorter, dtype=float)
    ranks[sorter] = np.arange(1, len(arr) + 1)
    # Handle ties via group-by-value averaging
    unique_vals, inverse = np.unique(arr, return_inverse=True)
    if len(unique_vals) == len(arr):
        return ranks
    for k in range(len(unique_vals)):
        mask = inverse == k
        if mask.sum() > 1:
            ranks[mask] = ranks[mask].mean()
    return ranks


def pca_variance_shares(
    per_feature_pnl: dict[str, pl.DataFrame],
    feature_names: list[str],
) -> dict[str, list[float]]:
    """Compute PCA variance shares on the per-feature daily P&L matrix.

    Returns:
      ``explained_variance_ratio``: shape (n_features,), each component's
        fractional variance explained.
      ``cumulative_variance``: running sum of the above.

    Uses NumPy's SVD via ``np.linalg.svd`` on the centered N × n_features
    matrix. Requires at least 3 signal days in the date intersection;
    raises ValueError otherwise.
    """
    if not feature_names:
        raise ValueError("feature_names must be non-empty")
    missing = [f for f in feature_names if f not in per_feature_pnl]
    if missing:
        raise KeyError(f"per_feature_pnl missing entries for {missing}")

    aligned: pl.DataFrame | None = None
    for f in feature_names:
        sub = per_feature_pnl[f].select(
            pl.col("date"),
            pl.col("daily_pnl").alias(f"pnl_{f}"),
        )
        aligned = sub if aligned is None else aligned.join(sub, on="date", how="full", coalesce=True)
    assert aligned is not None

    aligned = aligned.drop_nulls()
    if aligned.height < 3:
        raise ValueError(
            f"PCA needs ≥ 3 days in the date intersection across features, got {aligned.height}"
        )

    cols = [f"pnl_{f}" for f in feature_names]
    matrix = aligned.select(cols).to_numpy().astype(float)
    centered = matrix - matrix.mean(axis=0, keepdims=True)
    # SVD: covariance eigenvalues = singular values squared / (n - 1)
    _, sigma, _ = np.linalg.svd(centered, full_matrices=False)
    eigenvalues = (sigma ** 2) / max(1, centered.shape[0] - 1)
    total = float(eigenvalues.sum())
    if total == 0:
        # Degenerate: all features identical (or all-zero). Spread uniformly.
        ratios = [1.0 / len(feature_names)] * len(feature_names)
    else:
        ratios = (eigenvalues / total).tolist()
    cumulative = np.cumsum(ratios).tolist()
    return {
        "explained_variance_ratio": ratios,
        "cumulative_variance": cumulative,
    }
