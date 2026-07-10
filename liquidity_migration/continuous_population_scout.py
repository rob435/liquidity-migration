"""Pure population-tape primitives for the CONTINUOUS overhaul scout.

The production candidate tape starts after residual-momentum, decile, event, and
liquidity filtering.  This module deliberately works one layer earlier: every
valid supplied symbol/hour remains a row and the current strategy choices become
observable columns.

``build_continuous_feature_tape`` is outcome blind.  The registered outcome
surface is split into ``build_continuous_entry_anchor`` (S03) and
``append_continuous_path_labels`` (S04); the latter consumes the frozen S03
projection plus hourly bars.  The extended atlas remains explicit opt-in and is
not part of the initial A0 contract.

The price labels are descriptive ideal-close paths.  A signal bar stamped ``t``
closes at ``t + 1h``; the frozen one-hour confirmation delay makes the first
next-executable hourly-close anchor the following bar close at ``t + 2h``.  None
of the labels model an order, latency, spread, fees, funding, or market impact.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import numpy as np
import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR
from .continuous_events import _entry_event_expr, per_symbol_timeseries_features
from .strategy_overhaul_projection import (
    artifact_polars_schema,
    empty_artifact_frame,
    project_artifact_frame,
)
from .strategy_overhaul_schemas import CONTINUOUS_ENTRY_SCHEMA_ID, CONTINUOUS_LABEL_SCHEMA_ID

CURRENT_RMOM_QUANTILE = 0.25
CURRENT_LIQUIDITY_FLOOR = 500_000.0
CURRENT_SELECTION_DECILE = 9
CURRENT_FEATURE_SET = ("max_ret168",)
CURRENT_ENTRY_CONFIRM_DELAY_HOURS = 1
CURRENT_STRATEGY_PROFILE = "continuous_ensemble_v2"
CURRENT_SIDE = "short"
FROZEN_FORWARD_HORIZONS_HOURS = (1, 2, 4, 8, 12, 24, 48, 72)
FROZEN_FAVORABLE_FIRST_PASSAGE_PCTS = (0.10, 0.12, 0.15)
FROZEN_ADVERSE_FIRST_PASSAGE_PCTS = (0.10, 0.25, 0.50, 1.00)
MINIMAL_RETURN_HORIZONS_HOURS = (1, 24, 72)
MINIMAL_EXCURSION_HORIZONS_HOURS = (24, 72)
EVENT_WAVE_MAX_ADJACENT_GAP_HOURS = 6
EVENT_WAVE_MAX_SPAN_HOURS = 72

COMPONENT_ORDER = ("p3", "p4p3", "p4p5")
COMPONENT_TRIGGERS = {
    "p3": "turn3_pop3",
    "p4p3": "turn4_pop3",
    "p4p5": "turn4_pop5",
}
COMPONENT_AGE_DAYS_MIN = {component: 240 for component in COMPONENT_ORDER}
COMPONENT_BITS = {component: 1 << ordinal for ordinal, component in enumerate(COMPONENT_ORDER)}
COMPONENT_WEIGHTS = {"p3": 1.0 / 3.0, "p4p3": 2.0 / 9.0, "p4p5": 4.0 / 9.0}

_KLINE_COLUMN_ORDER = ("symbol", "ts_ms", "open", "high", "low", "close", "turnover_quote")
_REQUIRED_KLINE_COLUMNS = set(_KLINE_COLUMN_ORDER)
_OPTIONAL_SOURCE_IDENTITY_COLUMNS = ("venue", "canonical_instrument_id")
_KEY_COLUMNS = ["symbol", "ts_ms"]


def _pct_tag(value: float) -> str:
    return f"{int(round(float(value) * 100))}pct"


def _validate_hourly_klines(hourly_klines: pl.DataFrame) -> pl.DataFrame:
    """Return a strict, typed source projection or fail on an invalid key/path.

    Only raw kline fields and the two optional reviewed identity fields may
    survive this boundary.  In particular, a caller cannot smuggle an outcome,
    precomputed decision, or stale derived feature into S02 by attaching an
    arbitrary column to the source frame.
    """

    missing = sorted(_REQUIRED_KLINE_COLUMNS - set(hourly_klines.columns))
    if missing:
        raise ValueError(f"hourly_klines missing required columns: {missing}")
    identity_columns = [column for column in _OPTIONAL_SOURCE_IDENTITY_COLUMNS if column in hourly_klines.columns]
    k = hourly_klines.select(
        *[pl.col(column).cast(pl.String) for column in identity_columns],
        pl.col("symbol").cast(pl.String),
        pl.col("ts_ms").cast(pl.Int64),
        pl.col("open").cast(pl.Float64),
        pl.col("high").cast(pl.Float64),
        pl.col("low").cast(pl.Float64),
        pl.col("close").cast(pl.Float64),
        pl.col("turnover_quote").cast(pl.Float64),
    )
    invalid_key = k.filter(
        pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | pl.col("ts_ms").is_null()
        | ((pl.col("ts_ms") % MS_PER_HOUR) != 0)
    )
    if not invalid_key.is_empty():
        raise ValueError("hourly_klines contains blank/null symbols or off-grid timestamps")
    for column in identity_columns:
        invalid_identity = k.filter(pl.col(column).is_null() | (pl.col(column).str.strip_chars() == ""))
        if not invalid_identity.is_empty():
            raise ValueError(f"hourly_klines contains blank/null {column}")
    duplicates = k.group_by(_KEY_COLUMNS).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise ValueError(
            f"hourly_klines has duplicate (symbol,ts_ms) keys: {duplicates.select(_KEY_COLUMNS).head(5).to_dicts()}"
        )
    invalid_ohlc = k.filter(
        pl.any_horizontal(
            pl.col("open").is_null() | (~pl.col("open").is_finite()) | (pl.col("open") <= 0.0),
            pl.col("high").is_null() | (~pl.col("high").is_finite()) | (pl.col("high") <= 0.0),
            pl.col("low").is_null() | (~pl.col("low").is_finite()) | (pl.col("low") <= 0.0),
            pl.col("close").is_null() | (~pl.col("close").is_finite()) | (pl.col("close") <= 0.0),
            pl.col("high") < pl.max_horizontal("open", "close"),
            pl.col("low") > pl.min_horizontal("open", "close"),
            pl.col("high") < pl.col("low"),
        )
    )
    if not invalid_ohlc.is_empty():
        raise ValueError(
            f"hourly_klines contains invalid OHLC rows: {invalid_ohlc.select(_KEY_COLUMNS).head(5).to_dicts()}"
        )
    invalid_turnover = k.filter(
        pl.col("turnover_quote").is_not_null()
        & ((~pl.col("turnover_quote").is_finite()) | (pl.col("turnover_quote") < 0.0))
    )
    if not invalid_turnover.is_empty():
        raise ValueError("hourly_klines contains non-finite or negative turnover_quote")
    return k.sort(_KEY_COLUMNS)


def _gap_safe_per_symbol_features(k: pl.DataFrame) -> pl.DataFrame:
    """Compute row-window features without crossing a missing hourly bar.

    ``per_symbol_timeseries_features`` intentionally matches the production
    row-window implementation and groups only by ``symbol``.  Give every exact
    consecutive-hour segment a temporary unique symbol, run that shared helper,
    and restore the source symbol afterward.  This preserves parity on complete
    grids while forcing every return and rolling feature to warm up again after
    an interior gap.
    """

    previous_symbol = pl.col("symbol").shift(1)
    previous_ts = pl.col("ts_ms").shift(1)
    segment_head = (
        previous_symbol.is_null()
        | (pl.col("symbol") != previous_symbol)
        | ((pl.col("ts_ms") - previous_ts) != MS_PER_HOUR)
    ).fill_null(True)
    segmented = (
        k.with_columns(
            pl.col("symbol").alias("_source_symbol"),
            segment_head.cast(pl.Int64).cum_sum().alias("_history_segment_id"),
        )
        .with_columns(pl.col("_history_segment_id").alias("symbol"))
    )
    population = per_symbol_timeseries_features(segmented).with_columns(
        pl.col("ret1")
        .shift(1)
        .rolling_max(window_size=168, min_samples=48)
        .over("symbol")
        .alias("prior_max_ret168_lag1")
    )
    return population.with_columns(pl.col("_source_symbol").alias("symbol")).drop(
        ["_source_symbol", "_history_segment_id"]
    )


def _normalize_rmom(stable_rmom: pl.DataFrame | None) -> tuple[pl.DataFrame, bool]:
    """Normalize either persisted ``ts_ms`` RMOM or an already-normalized ``day_ts`` table.

    Provisional rows remain joinable so the population can distinguish provisional
    from absent data, but only non-provisional finite values enter ranks.
    """

    schema = {
        "symbol": pl.String,
        "day_ts": pl.Int64,
        "residual_momentum": pl.Float64,
        "rmom_is_provisional": pl.Boolean,
        "rmom_source_row_present": pl.Boolean,
    }
    if stable_rmom is None or stable_rmom.is_empty():
        return pl.DataFrame(schema=schema), False
    required = {"symbol", "residual_momentum"}
    missing = sorted(required - set(stable_rmom.columns))
    if missing:
        raise ValueError(f"stable_rmom missing required columns: {missing}")
    time_columns = [name for name in ("ts_ms", "day_ts") if name in stable_rmom.columns]
    if len(time_columns) != 1:
        raise ValueError("stable_rmom must contain exactly one of ts_ms or day_ts")
    time_col = time_columns[0]
    provenance_declared = "is_provisional" in stable_rmom.columns
    provisional = (
        pl.col("is_provisional").cast(pl.Boolean).fill_null(True)
        if provenance_declared
        # An old table without an explicit provenance bit can contain a mutable
        # tail.  Keep its rows visible for coverage diagnostics, but never let
        # undeclared provenance enter a stable rank.
        else pl.lit(True, dtype=pl.Boolean)
    )
    rmom = stable_rmom.select(
        pl.col("symbol").cast(pl.String),
        pl.col(time_col).cast(pl.Int64).alias("day_ts"),
        pl.col("residual_momentum").cast(pl.Float64),
        provisional.alias("rmom_is_provisional"),
        pl.lit(True, dtype=pl.Boolean).alias("rmom_source_row_present"),
    ).sort(["symbol", "day_ts"])
    invalid = rmom.filter(
        pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | pl.col("day_ts").is_null()
        | ((pl.col("day_ts") % MS_PER_DAY) != 0)
        | (pl.col("residual_momentum").is_not_null() & (~pl.col("residual_momentum").is_finite()))
    )
    if not invalid.is_empty():
        raise ValueError("stable_rmom contains invalid keys or non-finite residual momentum")
    duplicates = rmom.group_by(["symbol", "day_ts"]).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise ValueError(
            "stable_rmom has duplicate (symbol,day_ts) keys: "
            f"{duplicates.select(['symbol', 'day_ts']).head(5).to_dicts()}"
        )
    return rmom, provenance_declared


def _finite(column: str) -> pl.Expr:
    return pl.col(column).is_not_null() & pl.col(column).is_finite()


def _attach_rank_block(
    frame: pl.DataFrame,
    *,
    value_col: str,
    prefix: str,
    denominator: str,
) -> pl.DataFrame:
    """Attach support and average-tie ranks without hiding unrankable peers.

    The new full-population diagnostic uses its finite/rankable peer count.  The
    production reconstruction deliberately uses every post-q25 survivor in its
    denominator, matching ``cross_sectional_decile`` even when a feature is null.
    """

    if denominator not in {"rankable", "population"}:
        raise ValueError("denominator must be 'rankable' or 'population'")

    available = _finite(value_col)
    peer_col = f"{prefix}_rankable_peer_count"
    missing_col = f"{prefix}_missing_peer_count"
    value_rank_col = f"{prefix}_value_rank"
    score_col = f"{prefix}_score"
    score_rank_col = f"{prefix}_score_rank"
    decile_col = f"{prefix}_decile"
    tie_col = f"{prefix}_tie_count"
    out = frame.with_columns(
        available.cast(pl.Int64).sum().over("ts_ms").alias(peer_col),
        pl.len().over("ts_ms").alias(f"{prefix}_population_peer_count"),
        pl.when(available).then(pl.len().over(["ts_ms", value_col])).otherwise(None).cast(pl.Int64).alias(tie_col),
        pl.when(available)
        .then(pl.col(value_col).rank(method="average").over("ts_ms"))
        .otherwise(None)
        .alias("_value_ordinal"),
    )
    denominator_count_col = f"{prefix}_rank_denominator_count"
    denominator_source_col = peer_col if denominator == "rankable" else f"{prefix}_population_peer_count"
    out = out.with_columns(
        (pl.col(f"{prefix}_population_peer_count") - pl.col(peer_col)).alias(missing_col),
        pl.col(denominator_source_col).cast(pl.Int64).alias(denominator_count_col),
        pl.lit("average").alias(f"{prefix}_tie_method"),
        pl.lit(f"{denominator}_peers_minus_one_clamped_1").alias(f"{prefix}_rank_denominator_rule"),
    )
    out = out.with_columns(
        pl.when(available)
        .then((pl.col("_value_ordinal") - 1.0) / pl.max_horizontal(pl.col(denominator_count_col) - 1, pl.lit(1)))
        .otherwise(None)
        .alias(value_rank_col),
    )
    # The current profile's one-feature composite is max_ret168's normalized
    # cross-sectional rank. Rank it once more to reproduce the decile construction.
    out = out.with_columns(pl.col(value_rank_col).alias(score_col))
    out = out.with_columns(
        pl.when(_finite(score_col))
        .then(pl.col(score_col).rank(method="average").over("ts_ms"))
        .otherwise(None)
        .alias("_score_ordinal")
    )
    return out.with_columns(
        pl.when(_finite(score_col))
        .then(
            (((pl.col("_score_ordinal") - 1.0) * 10.0) / pl.max_horizontal(pl.col(denominator_count_col), pl.lit(1)))
            .floor()
            .clip(0, 9)
        )
        .otherwise(None)
        .cast(pl.Int64)
        .alias(decile_col),
        pl.col(value_rank_col).alias(score_rank_col),
    ).drop(["_value_ordinal", "_score_ordinal"])


def _attach_membership_spells(frame: pl.DataFrame, *, membership_col: str, prefix: str) -> pl.DataFrame:
    """Attach deterministic consecutive-hour spell identity without deleting non-members."""

    id_col = f"{prefix}_id"
    head_col = f"{prefix}_head"
    start_col = f"{prefix}_start_ts_ms"
    index_col = f"{prefix}_hour_index"
    active = frame.filter(pl.col(membership_col).fill_null(False)).select(_KEY_COLUMNS)
    if active.is_empty():
        return frame.with_columns(
            pl.lit(None, dtype=pl.String).alias(id_col),
            pl.lit(False).alias(head_col),
            pl.lit(None, dtype=pl.Int64).alias(start_col),
            pl.lit(None, dtype=pl.Int64).alias(index_col),
        )
    active = active.with_columns(
        ((pl.col("ts_ms") - pl.col("ts_ms").shift(1).over("symbol")) > MS_PER_HOUR).fill_null(True).alias(head_col)
    )
    active = active.with_columns(pl.col(head_col).cast(pl.Int64).cum_sum().over("symbol").alias("_spell_ordinal"))
    active = active.with_columns(
        pl.col("ts_ms").min().over(["symbol", "_spell_ordinal"]).alias(start_col)
    ).with_columns(
        pl.concat_str([pl.col("symbol"), pl.lit("|"), pl.col(start_col).cast(pl.String)]).alias(id_col),
        ((pl.col("ts_ms") - pl.col(start_col)) // MS_PER_HOUR).cast(pl.Int64).alias(index_col),
    )
    return frame.join(
        active.select(_KEY_COLUMNS + [id_col, head_col, start_col, index_col]), on=_KEY_COLUMNS, how="left"
    ).with_columns(pl.col(head_col).fill_null(False))


def _attach_event_waves(frame: pl.DataFrame) -> pl.DataFrame:
    """Attach the frozen causal, venue-level p3 event-wave identity.

    Unique p3 timestamps are scanned in ascending order.  A timestamp remains in
    the current wave only while its gap from the previous timestamp is at most
    six hours and it is strictly earlier than wave-start plus 72 hours.  The ID
    contains only the wave start, so future triggers cannot revise an earlier
    row.  A feature tape may cover at most one venue; no-venue synthetic frames
    are treated as one unnamed venue.
    """

    venue_prefix = ""
    if "venue" in frame.columns:
        venues = frame.select(pl.col("venue").cast(pl.String)).unique().to_series().to_list()
        if len(venues) != 1 or venues[0] is None or not str(venues[0]).strip():
            raise ValueError("continuous feature tape must contain exactly one non-blank venue")
        venue_prefix = f"{str(venues[0]).strip()}|"

    primary_trigger_column = f"trigger_{COMPONENT_TRIGGERS[COMPONENT_ORDER[0]]}"
    trigger_timestamps = (
        frame.filter(pl.col(primary_trigger_column)).select("ts_ms").unique().sort("ts_ms").to_series().to_list()
    )
    if not trigger_timestamps:
        return frame.with_columns(pl.lit(None, dtype=pl.String).alias("event_wave_id"))

    max_gap_ms = EVENT_WAVE_MAX_ADJACENT_GAP_HOURS * MS_PER_HOUR
    max_span_ms = EVENT_WAVE_MAX_SPAN_HOURS * MS_PER_HOUR
    wave_rows: list[dict[str, int | str]] = []
    wave_start: int | None = None
    previous_ts: int | None = None
    for raw_ts in trigger_timestamps:
        ts_ms = int(raw_ts)
        if (
            wave_start is None
            or previous_ts is None
            or ts_ms - previous_ts > max_gap_ms
            or ts_ms >= wave_start + max_span_ms
        ):
            wave_start = ts_ms
        wave_rows.append(
            {
                "ts_ms": ts_ms,
                "_event_wave_id": f"{venue_prefix}wave|{wave_start}",
            }
        )
        previous_ts = ts_ms

    wave_map = pl.DataFrame(
        wave_rows,
        schema={"ts_ms": pl.Int64, "_event_wave_id": pl.String},
    )
    return (
        frame.join(wave_map, on="ts_ms", how="left")
        .with_columns(
            pl.when(pl.col(primary_trigger_column)).then(pl.col("_event_wave_id")).otherwise(None).alias("event_wave_id")
        )
        .drop("_event_wave_id")
    )


def build_continuous_feature_tape(
    hourly_klines: pl.DataFrame,
    stable_rmom: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Build the causal, pre-deletion CONTINUOUS symbol/hour population.

    The output has exactly one row for every valid input ``(symbol, ts_ms)``.
    Missing or provisional RMOM is represented by flags and null current-q25
    ranks; it never removes the underlying population row.
    """

    k = _validate_hourly_klines(hourly_klines)
    if k.is_empty():
        return k
    rmom, provenance_declared = _normalize_rmom(stable_rmom)
    population = _gap_safe_per_symbol_features(k)
    if population.height != k.height:
        raise RuntimeError("per-symbol feature build changed the validated hourly population")
    population = population.with_columns(
        pl.col("ret168").rank(method="average").over("ts_ms").alias("xsret7"),
        pl.col("ret72").rank(method="average").over("ts_ms").alias("xsret3"),
        ((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"),
        pl.col("turnover_quote").is_not_null().alias("turnover_quote_available"),
    )
    population = population.join(rmom, on=["symbol", "day_ts"], how="left")
    population = (
        population.with_columns(
            pl.col("rmom_source_row_present").fill_null(False),
            pl.col("residual_momentum").is_not_null().alias("rmom_present"),
            pl.col("rmom_is_provisional").fill_null(False),
            pl.lit(provenance_declared).alias("rmom_provenance_declared"),
        )
        .with_columns(
            (pl.col("rmom_present") & (~pl.col("rmom_is_provisional")) & pl.col("residual_momentum").is_finite()).alias(
                "rmom_stable_available"
            )
        )
        .with_columns(
            pl.when(pl.col("rmom_stable_available"))
            .then(pl.col("residual_momentum"))
            .otherwise(None)
            .alias("_stable_residual_momentum")
        )
    )
    population = population.with_columns(
        pl.len().over("ts_ms").cast(pl.Int64).alias("rmom_population_peer_count"),
        pl.col("rmom_stable_available").cast(pl.Int64).sum().over("ts_ms").alias("rmom_rankable_peer_count"),
    ).with_columns(
        (pl.col("rmom_population_peer_count") - pl.col("rmom_rankable_peer_count")).alias(
            "rmom_missing_peer_count"
        ),
        pl.when(pl.col("rmom_stable_available"))
        .then(pl.col("_stable_residual_momentum").rank(method="average").over("ts_ms"))
        .otherwise(None)
        .alias("_rmom_ordinal"),
        pl.when(pl.col("rmom_stable_available"))
        .then(pl.len().over(["ts_ms", "_stable_residual_momentum"]))
        .otherwise(None)
        .cast(pl.Int64)
        .alias("rmom_tie_count"),
    )
    population = population.with_columns(
        pl.when(pl.col("rmom_stable_available"))
        .then((pl.col("_rmom_ordinal") - 1.0) / pl.max_horizontal(pl.col("rmom_rankable_peer_count") - 1, pl.lit(1)))
        .otherwise(None)
        .alias("residual_momentum_rank")
    ).drop(["_rmom_ordinal", "_stable_residual_momentum"])
    population = population.with_columns(
        (pl.col("rmom_stable_available") & (pl.col("residual_momentum_rank") <= CURRENT_RMOM_QUANTILE)).alias(
            "current_q25_pass"
        ),
        pl.lit(CURRENT_RMOM_QUANTILE).alias("current_rmom_quantile_cutoff"),
    )

    population = _attach_rank_block(
        population,
        value_col=CURRENT_FEATURE_SET[0],
        prefix="full_population",
        denominator="rankable",
    )
    population = _attach_rank_block(
        population,
        value_col="turnover_quote",
        prefix="full_population_liquidity",
        denominator="rankable",
    )
    population = population.with_columns(
        (pl.col("full_population_decile") == CURRENT_SELECTION_DECILE).fill_null(False).alias("full_population_d9"),
        pl.col("current_q25_pass").cast(pl.Int64).sum().over("ts_ms").alias("current_q25_population_peer_count"),
        (pl.col("current_q25_pass") & _finite(CURRENT_FEATURE_SET[0]))
        .cast(pl.Int64)
        .sum()
        .over("ts_ms")
        .alias("current_q25_rankable_peer_count"),
    ).with_columns(
        (pl.col("current_q25_population_peer_count") - pl.col("current_q25_rankable_peer_count")).alias(
            "current_q25_missing_peer_count"
        )
    )
    q25 = _attach_rank_block(
        population.filter(pl.col("current_q25_pass")),
        value_col=CURRENT_FEATURE_SET[0],
        prefix="current_q25",
        denominator="population",
    )
    q25 = _attach_rank_block(
        q25,
        value_col="turnover_quote",
        prefix="current_q25_liquidity",
        denominator="population",
    )
    q25_columns = [
        "current_q25_value_rank",
        "current_q25_score",
        "current_q25_score_rank",
        "current_q25_decile",
        "current_q25_tie_count",
        "current_q25_rank_denominator_count",
        "current_q25_tie_method",
        "current_q25_rank_denominator_rule",
        "current_q25_liquidity_value_rank",
        "current_q25_liquidity_score",
        "current_q25_liquidity_score_rank",
        "current_q25_liquidity_decile",
        "current_q25_liquidity_tie_count",
        "current_q25_liquidity_population_peer_count",
        "current_q25_liquidity_rankable_peer_count",
        "current_q25_liquidity_missing_peer_count",
        "current_q25_liquidity_rank_denominator_count",
        "current_q25_liquidity_tie_method",
        "current_q25_liquidity_rank_denominator_rule",
    ]
    population = population.join(q25.select(_KEY_COLUMNS + q25_columns), on=_KEY_COLUMNS, how="left")
    population = population.with_columns(
        pl.col("current_q25_liquidity_value_rank").alias("liquidity_rank"),
        (pl.col("current_q25_pass") & (pl.col("current_q25_decile") == CURRENT_SELECTION_DECILE))
        .fill_null(False)
        .alias("current_q25_d9"),
        ((pl.col("ts_ms") + MS_PER_HOUR).cast(pl.Int64)).alias("signal_bar_close_ts_ms"),
        ((pl.col("ts_ms") + CURRENT_ENTRY_CONFIRM_DELAY_HOURS * MS_PER_HOUR).cast(pl.Int64)).alias(
            "decision_ts_ms"
        ),
        ((pl.col("ts_ms") + MS_PER_HOUR).cast(pl.Int64)).alias("feature_data_available_ts_ms"),
        pl.col("day_ts").cast(pl.Int64).alias("rmom_source_day_ts_ms"),
        pl.col("day_ts").cast(pl.Int64).alias("rmom_data_available_ts_ms"),
        pl.col("ts_ms").cast(pl.Int64).alias("signal_ts_ms"),
        (pl.col("turnover_quote") >= CURRENT_LIQUIDITY_FLOOR).fill_null(False).alias("current_liquidity_500k_pass"),
    ).with_columns(
        pl.max_horizontal("feature_data_available_ts_ms", "rmom_data_available_ts_ms").alias("data_available_ts_ms")
    )

    population = population.with_columns(
        *(
            _entry_event_expr(COMPONENT_TRIGGERS[component])
            .fill_null(False)
            .alias(f"trigger_{COMPONENT_TRIGGERS[component]}")
            for component in COMPONENT_ORDER
        ),
    ).with_columns(
        pl.col(f"trigger_{COMPONENT_TRIGGERS[COMPONENT_ORDER[0]]}").alias("trigger_any_current_component"),
        sum(
            (
                pl.col(f"trigger_{COMPONENT_TRIGGERS[component]}").cast(pl.Int8) * COMPONENT_BITS[component]
                for component in COMPONENT_ORDER
            ),
            start=pl.lit(0, dtype=pl.Int8),
        ).alias("component_mask"),
        sum(
            (
                pl.col(f"trigger_{COMPONENT_TRIGGERS[component]}").cast(pl.Float64)
                * COMPONENT_WEIGHTS[component]
                for component in COMPONENT_ORDER
            ),
            start=pl.lit(0.0, dtype=pl.Float64),
        ).alias("implied_tier_weight"),
        pl.sum_horizontal(*(f"trigger_{COMPONENT_TRIGGERS[component]}" for component in COMPONENT_ORDER))
        .cast(pl.Int8)
        .alias("component_membership_count"),
    )
    trigger_columns = [f"trigger_{COMPONENT_TRIGGERS[component]}" for component in COMPONENT_ORDER]
    bad_nesting = population.filter(
        pl.any_horizontal(
            pl.col(trigger_columns[index]) & (~pl.col(trigger_columns[index - 1]))
            for index in range(1, len(trigger_columns))
        )
    )
    if not bad_nesting.is_empty():
        raise RuntimeError("continuous trigger predicates violated their nested invariant")
    population = population.with_columns(
        pl.concat_str(
            [
                pl.when(pl.col(f"trigger_{COMPONENT_TRIGGERS[component]}"))
                .then(pl.lit(component))
                .otherwise(pl.lit(""))
                for component in COMPONENT_ORDER
            ],
            separator=",",
        )
        .str.replace_all(r",+", ",")
        .str.strip_chars(",")
        .alias("component_tags"),
        pl.concat_str([pl.col("symbol"), pl.lit("|"), pl.col("ts_ms").cast(pl.String)]).alias("unique_decision_id"),
        pl.col("trigger_any_current_component")
        .cast(pl.Int64)
        .sum()
        .over("ts_ms")
        .alias("simultaneous_trigger_decision_count"),
        *(
            (pl.col("current_q25_d9") & pl.col(f"trigger_{COMPONENT_TRIGGERS[component]}")).alias(
                f"current_{component}_component_membership"
            )
            for component in COMPONENT_ORDER
        ),
        pl.when(pl.col("current_q25_d9"))
        .then(pl.col("component_mask"))
        .otherwise(0)
        .cast(pl.Int8)
        .alias("current_component_mask_before_liquidity"),
    )
    population = _attach_event_waves(population)

    population = _attach_membership_spells(population, membership_col="full_population_d9", prefix="full_d9_spell")
    population = _attach_membership_spells(population, membership_col="current_q25_d9", prefix="current_q25_d9_spell")
    population = _attach_membership_spells(
        population,
        membership_col="trigger_any_current_component",
        prefix="trigger_spell",
    )
    for component in COMPONENT_ORDER:
        population = _attach_membership_spells(
            population,
            membership_col=f"trigger_{COMPONENT_TRIGGERS[component]}",
            prefix=f"{component}_trigger_spell",
        )
    for component in COMPONENT_ORDER:
        population = _attach_membership_spells(
            population,
            membership_col=f"current_{component}_component_membership",
            prefix=f"current_{component}_component_spell",
        )
    population = population.with_columns(
        pl.col("trigger_spell_id").alias("pump_event_cluster_id"),
        pl.col("trigger_spell_head").alias("raw_trigger_spell_head"),
        pl.any_horizontal(*(f"current_{component}_component_spell_head" for component in COMPONENT_ORDER)).alias(
            "component_spell_head"
        ),
    ).sort(_KEY_COLUMNS)
    if population.height != k.height or population.select(_KEY_COLUMNS).n_unique() != k.height:
        raise RuntimeError("population tape lost or duplicated validated hourly keys")
    return population


def _future_window(values: np.ndarray, *, width: int) -> np.ndarray:
    """Window ``i`` contains path observations 1..``width`` after entry row ``i+1``."""

    padded = np.concatenate([values.astype(float, copy=False), np.full(width + 2, np.nan)])
    return np.lib.stride_tricks.sliding_window_view(padded, width)[2 : 2 + values.size]


def _nan_reduce(window: np.ndarray, *, kind: str) -> np.ndarray:
    out = np.full(window.shape[0], np.nan)
    available = np.isfinite(window).any(axis=1)
    if available.any():
        if kind == "max":
            out[available] = np.nanmax(window[available], axis=1)
        elif kind == "min":
            out[available] = np.nanmin(window[available], axis=1)
        else:  # pragma: no cover - internal misuse guard
            raise ValueError(f"unknown reduction kind: {kind}")
    return out


def _first_true_hour(mask: np.ndarray) -> np.ndarray:
    found = mask.any(axis=1)
    out = np.full(mask.shape[0], np.nan)
    if found.any():
        out[found] = np.argmax(mask[found], axis=1) + 1
    return out


def _contiguous_segments(ts_ms: np.ndarray) -> Iterable[tuple[int, int]]:
    if ts_ms.size == 0:
        return
    cuts = np.flatnonzero(np.diff(ts_ms) != MS_PER_HOUR) + 1
    starts = np.concatenate([np.array([0]), cuts])
    ends = np.concatenate([cuts, np.array([ts_ms.size])])
    yield from zip(starts.tolist(), ends.tolist())


_CONTINUOUS_ENTRY_SCHEMA = artifact_polars_schema(CONTINUOUS_ENTRY_SCHEMA_ID)
_ENTRY_OWNED_COLUMNS = frozenset(
    {
        "entry_bar_start_ts_ms",
        "entry_anchor_ts_ms",
        "entry_price",
        "entry_anchor_available",
        "missing_anchor_reason",
    }
)


def _require_dtypes(
    frame: pl.DataFrame,
    expected: Mapping[str, pl.DataType],
    *,
    name: str,
) -> None:
    mismatched = {
        column: {"expected": str(dtype), "actual": str(frame.schema[column])}
        for column, dtype in expected.items()
        if frame.schema[column] != dtype
    }
    if mismatched:
        raise ValueError(f"{name} has invalid stage dtypes: {mismatched}")


def _empty_continuous_entry_anchor() -> pl.DataFrame:
    return empty_artifact_frame(CONTINUOUS_ENTRY_SCHEMA_ID)


def _empty_continuous_minimal_labels() -> pl.DataFrame:
    return empty_artifact_frame(CONTINUOUS_LABEL_SCHEMA_ID)


def _validate_feature_tape_for_entry(feature_tape: pl.DataFrame) -> pl.DataFrame:
    required_dtypes = {
        "venue": pl.String,
        "canonical_instrument_id": pl.String,
        "symbol": pl.String,
        "signal_ts_ms": pl.Int64,
        "decision_ts_ms": pl.Int64,
    }
    missing = sorted(set(required_dtypes) - set(feature_tape.columns))
    if missing:
        raise ValueError(f"feature_tape missing entry-anchor columns: {missing}")
    collisions = sorted(_ENTRY_OWNED_COLUMNS & set(feature_tape.columns))
    if collisions:
        raise ValueError(f"feature_tape contains precomputed S03-owned columns: {collisions}")
    _require_dtypes(feature_tape, required_dtypes, name="feature_tape")
    if feature_tape.is_empty():
        return feature_tape

    duplicates = feature_tape.group_by(["symbol", "signal_ts_ms"]).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise ValueError("feature_tape has duplicate (symbol,signal_ts_ms) keys")
    invalid_identity = feature_tape.filter(
        pl.col("venue").is_null()
        | (pl.col("venue").str.strip_chars() == "")
        | pl.col("canonical_instrument_id").is_null()
        | (pl.col("canonical_instrument_id").str.strip_chars() == "")
        | pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | pl.col("signal_ts_ms").is_null()
        | pl.col("decision_ts_ms").is_null()
        | ((pl.col("signal_ts_ms") % MS_PER_HOUR) != 0)
        | ((pl.col("decision_ts_ms") % MS_PER_HOUR) != 0)
        | (pl.col("decision_ts_ms") != pl.col("signal_ts_ms") + MS_PER_HOUR)
    )
    if not invalid_identity.is_empty():
        raise ValueError("feature_tape has invalid identity or signal/decision timestamp semantics")
    return feature_tape


def _validate_entry_anchor_tape(entry_anchor_tape: pl.DataFrame) -> pl.DataFrame:
    projected = project_artifact_frame(entry_anchor_tape, CONTINUOUS_ENTRY_SCHEMA_ID)
    _require_dtypes(entry_anchor_tape, _CONTINUOUS_ENTRY_SCHEMA, name="entry_anchor_tape")
    if projected.is_empty():
        return projected

    duplicate_signal_keys = projected.group_by(["symbol", "signal_ts_ms"]).len().filter(pl.col("len") > 1)
    if not duplicate_signal_keys.is_empty():
        raise ValueError("entry_anchor_tape has duplicate signal keys")

    available = pl.col("entry_anchor_available").fill_null(False)
    available_semantics_invalid = available & (
        pl.col("entry_anchor_ts_ms").is_null()
        | (pl.col("entry_anchor_ts_ms") != pl.col("signal_ts_ms") + 2 * MS_PER_HOUR)
        | pl.col("entry_price").is_null()
        | (~pl.col("entry_price").is_finite())
        | (pl.col("entry_price") <= 0.0)
        | pl.col("missing_anchor_reason").is_not_null()
    )
    unavailable_semantics_invalid = (~available) & (
        pl.col("entry_anchor_ts_ms").is_not_null()
        | pl.col("entry_price").is_not_null()
        | pl.col("missing_anchor_reason").is_null()
        | (pl.col("missing_anchor_reason") != "no_next_entry_bar")
    )
    invalid = projected.filter(
        pl.col("venue").is_null()
        | (pl.col("venue").str.strip_chars() == "")
        | pl.col("canonical_instrument_id").is_null()
        | (pl.col("canonical_instrument_id").str.strip_chars() == "")
        | pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | pl.col("decision_ts_ms").is_null()
        | pl.col("signal_ts_ms").is_null()
        | pl.col("entry_bar_start_ts_ms").is_null()
        | pl.col("entry_anchor_available").is_null()
        | ((pl.col("signal_ts_ms") % MS_PER_HOUR) != 0)
        | ((pl.col("decision_ts_ms") % MS_PER_HOUR) != 0)
        | ((pl.col("entry_bar_start_ts_ms") % MS_PER_HOUR) != 0)
        | (pl.col("decision_ts_ms") != pl.col("signal_ts_ms") + MS_PER_HOUR)
        | (pl.col("entry_bar_start_ts_ms") != pl.col("decision_ts_ms"))
        | available_semantics_invalid
        | unavailable_semantics_invalid
    )
    if not invalid.is_empty():
        raise ValueError("entry_anchor_tape violates frozen S03 timestamp/anchor/reason semantics")
    return projected


def build_continuous_entry_anchor(
    feature_tape: pl.DataFrame,
    hourly_klines: pl.DataFrame,
) -> pl.DataFrame:
    """Build the exact S03 common next-close anchor as a separate projection.

    This step reads only the immediately following hourly close needed by the
    registered one-hour confirmation delay; later OHLC rows are key-filtered out
    before any value validation.  It computes no path return or excursion label.
    Identity/PIT adapters must already have added ``venue`` and
    ``canonical_instrument_id`` to S02.
    """

    feature_tape = _validate_feature_tape_for_entry(feature_tape)
    if feature_tape.is_empty():
        return _empty_continuous_entry_anchor()

    missing_hourly = sorted(_REQUIRED_KLINE_COLUMNS - set(hourly_klines.columns))
    if missing_hourly:
        raise ValueError(f"hourly_klines missing required columns: {missing_hourly}")
    entry_keys = feature_tape.select(
        pl.col("symbol").cast(pl.String),
        (pl.col("signal_ts_ms") + MS_PER_HOUR).cast(pl.Int64).alias("ts_ms"),
    )
    entry_bars = _validate_hourly_klines(
        hourly_klines.with_columns(
            pl.col("symbol").cast(pl.String),
            pl.col("ts_ms").cast(pl.Int64),
        ).join(entry_keys, on=["symbol", "ts_ms"], how="semi")
    ).select(
        "symbol",
        pl.col("ts_ms").alias("entry_bar_start_ts_ms"),
        pl.col("close").alias("_next_close"),
    )
    ordered = (
        feature_tape.sort(["symbol", "signal_ts_ms"])
        .with_columns((pl.col("signal_ts_ms") + MS_PER_HOUR).cast(pl.Int64).alias("entry_bar_start_ts_ms"))
        .join(
            entry_bars,
            on=["symbol", "entry_bar_start_ts_ms"],
            how="left",
        )
    )
    next_close_available = (
        pl.col("_next_close").is_not_null() & pl.col("_next_close").is_finite() & (pl.col("_next_close") > 0.0)
    )
    available = next_close_available
    output = ordered.select(
        pl.col("venue").cast(pl.String),
        pl.col("symbol").cast(pl.String),
        pl.col("decision_ts_ms").cast(pl.Int64),
        pl.col("canonical_instrument_id").cast(pl.String),
        pl.col("signal_ts_ms").cast(pl.Int64),
        pl.col("entry_bar_start_ts_ms").cast(pl.Int64),
        pl.when(available)
        .then(pl.col("signal_ts_ms") + 2 * MS_PER_HOUR)
        .otherwise(None)
        .cast(pl.Int64)
        .alias("entry_anchor_ts_ms"),
        pl.when(available).then(pl.col("_next_close")).otherwise(None).cast(pl.Float64).alias("entry_price"),
        available.fill_null(False).alias("entry_anchor_available"),
        pl.when(~next_close_available.fill_null(False))
        .then(pl.lit("no_next_entry_bar"))
        .otherwise(None)
        .alias("missing_anchor_reason"),
    )
    return project_artifact_frame(output, CONTINUOUS_ENTRY_SCHEMA_ID).sort(
        ["venue", "symbol", "decision_ts_ms"]
    )


def _append_path_labels(population: pl.DataFrame, *, extended: bool) -> pl.DataFrame:
    """Attach either the minimal registered labels or an explicit extended atlas.

    The function uses only OHLC rows already present in ``population`` and splits
    every symbol at timestamp gaps.  Incomplete trailing or gapped paths remain as
    rows with null labels and explicit completeness/count fields.
    """

    missing = sorted((_REQUIRED_KLINE_COLUMNS | set(_KEY_COLUMNS)) - set(population.columns))
    if missing:
        raise ValueError(f"population missing path-label columns: {missing}")
    duplicates = population.group_by(_KEY_COLUMNS).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise ValueError("population has duplicate (symbol,ts_ms) keys")
    if population.is_empty():
        return population
    ordered = population.sort(_KEY_COLUMNS).with_row_index("_population_row_id")
    n_rows = ordered.height
    horizons = FROZEN_FORWARD_HORIZONS_HOURS if extended else MINIMAL_RETURN_HORIZONS_HOURS
    excursion_horizons = FROZEN_FORWARD_HORIZONS_HOURS if extended else MINIMAL_EXCURSION_HORIZONS_HOURS
    max_horizon = max(horizons)

    float_outputs: dict[str, np.ndarray] = {
        "ideal_entry_price": np.full(n_rows, np.nan),
        "observed_path_hours_through_72h": np.full(n_rows, np.nan),
    }
    bool_outputs: dict[str, np.ndarray] = {"ideal_entry_available": np.zeros(n_rows, dtype=bool)}
    for horizon in horizons:
        float_outputs[f"path_{horizon}h_underlying_return"] = np.full(n_rows, np.nan)
        if not extended:
            float_outputs[f"path_{horizon}h_short_directional_return"] = np.full(n_rows, np.nan)
            float_outputs[f"path_{horizon}h_observed_hours"] = np.zeros(n_rows)
        if horizon in excursion_horizons:
            float_outputs[f"path_{horizon}h_mfe"] = np.full(n_rows, np.nan)
            float_outputs[f"path_{horizon}h_mae"] = np.full(n_rows, np.nan)
        if extended:
            for suffix in (
                "close",
                "short_mark_return",
                "max_abs_bar_return",
                "max_abs_gap_return",
            ):
                float_outputs[f"path_{horizon}h_{suffix}"] = np.full(n_rows, np.nan)
        bool_outputs[f"path_{horizon}h_complete"] = np.zeros(n_rows, dtype=bool)
        if not extended:
            bool_outputs[f"path_{horizon}h_available"] = np.zeros(n_rows, dtype=bool)
    if extended:
        for threshold in FROZEN_FAVORABLE_FIRST_PASSAGE_PCTS:
            float_outputs[f"first_favorable_{_pct_tag(threshold)}_hours"] = np.full(n_rows, np.nan)
        for threshold in FROZEN_ADVERSE_FIRST_PASSAGE_PCTS:
            float_outputs[f"first_adverse_{_pct_tag(threshold)}_hours"] = np.full(n_rows, np.nan)
        if "full_population_d9" in ordered.columns:
            float_outputs["first_leave_full_d9_hours"] = np.full(n_rows, np.nan)
        if "current_q25_d9" in ordered.columns:
            float_outputs["first_leave_current_q25_d9_hours"] = np.full(n_rows, np.nan)

    for symbol_frame in ordered.partition_by("symbol", maintain_order=True):
        row_ids = symbol_frame["_population_row_id"].to_numpy()
        ts_all = symbol_frame["ts_ms"].to_numpy()
        for start, end in _contiguous_segments(ts_all):
            ids = row_ids[start:end]
            frame = symbol_frame.slice(start, end - start)
            close = frame["close"].to_numpy().astype(float)
            high = frame["high"].to_numpy().astype(float)
            low = frame["low"].to_numpy().astype(float)
            open_ = frame["open"].to_numpy().astype(float)
            size = close.size

            entry = np.full(size, np.nan)
            if size > 1:
                entry[:-1] = close[1:]
            float_outputs["ideal_entry_price"][ids] = entry
            bool_outputs["ideal_entry_available"][ids] = np.isfinite(entry)

            future_close = _future_window(close, width=max_horizon)
            future_high = _future_window(high, width=max_horizon)
            future_low = _future_window(low, width=max_horizon)
            if extended:
                bar_return = np.full(size, np.nan)
                gap_return = np.full(size, np.nan)
                if size > 1:
                    bar_return[1:] = close[1:] / close[:-1] - 1.0
                    gap_return[1:] = open_[1:] / close[:-1] - 1.0
                future_abs_bar = _future_window(np.abs(bar_return), width=max_horizon)
                future_abs_gap = _future_window(np.abs(gap_return), width=max_horizon)
            observed = np.isfinite(future_close).sum(axis=1)
            float_outputs["observed_path_hours_through_72h"][ids] = observed

            for horizon in horizons:
                path_close = future_close[:, horizon - 1]
                available = np.isfinite(entry) & np.isfinite(path_close)
                complete = available & (observed >= horizon)
                underlying = np.full(size, np.nan)
                underlying[complete] = path_close[complete] / entry[complete] - 1.0
                float_outputs[f"path_{horizon}h_underlying_return"][ids] = underlying
                if not extended:
                    float_outputs[f"path_{horizon}h_short_directional_return"][ids] = -underlying
                    float_outputs[f"path_{horizon}h_observed_hours"][ids] = np.minimum(observed, horizon)
                    bool_outputs[f"path_{horizon}h_available"][ids] = available
                if horizon in excursion_horizons:
                    path_high = _nan_reduce(future_high[:, :horizon], kind="max")
                    path_low = _nan_reduce(future_low[:, :horizon], kind="min")
                    mfe = np.full(size, np.nan)
                    mae = np.full(size, np.nan)
                    mfe[complete] = np.maximum(0.0, 1.0 - path_low[complete] / entry[complete])
                    mae[complete] = np.maximum(0.0, path_high[complete] / entry[complete] - 1.0)
                    float_outputs[f"path_{horizon}h_mfe"][ids] = mfe
                    float_outputs[f"path_{horizon}h_mae"][ids] = mae
                if extended:
                    short_mark = np.full(size, np.nan)
                    short_mark[complete] = 1.0 - path_close[complete] / entry[complete]
                    float_outputs[f"path_{horizon}h_close"][ids] = np.where(complete, path_close, np.nan)
                    float_outputs[f"path_{horizon}h_short_mark_return"][ids] = short_mark
                    float_outputs[f"path_{horizon}h_max_abs_bar_return"][ids] = np.where(
                        complete,
                        _nan_reduce(future_abs_bar[:, :horizon], kind="max"),
                        np.nan,
                    )
                    float_outputs[f"path_{horizon}h_max_abs_gap_return"][ids] = np.where(
                        complete,
                        _nan_reduce(future_abs_gap[:, :horizon], kind="max"),
                        np.nan,
                    )
                bool_outputs[f"path_{horizon}h_complete"][ids] = complete

            if extended:
                entry_matrix = entry[:, None]
                for threshold in FROZEN_FAVORABLE_FIRST_PASSAGE_PCTS:
                    mask = (
                        np.isfinite(future_low)
                        & np.isfinite(entry_matrix)
                        & (future_low <= entry_matrix * (1.0 - threshold))
                    )
                    output = f"first_favorable_{_pct_tag(threshold)}_hours"
                    float_outputs[output][ids] = _first_true_hour(mask)
                for threshold in FROZEN_ADVERSE_FIRST_PASSAGE_PCTS:
                    mask = (
                        np.isfinite(future_high)
                        & np.isfinite(entry_matrix)
                        & (future_high >= entry_matrix * (1.0 + threshold))
                    )
                    output = f"first_adverse_{_pct_tag(threshold)}_hours"
                    float_outputs[output][ids] = _first_true_hour(mask)

                for membership_col, output_col in (
                    ("full_population_d9", "first_leave_full_d9_hours"),
                    ("current_q25_d9", "first_leave_current_q25_d9_hours"),
                ):
                    if membership_col not in frame.columns:
                        continue
                    membership = frame[membership_col].fill_null(False).to_numpy().astype(float)
                    future_membership = _future_window(membership, width=max_horizon)
                    leave_mask = np.isfinite(future_membership) & (future_membership < 0.5)
                    leave = _first_true_hour(leave_mask)
                    leave[~frame[membership_col].fill_null(False).to_numpy()] = np.nan
                    float_outputs[output_col][ids] = leave

    expressions: list[pl.Series] = [pl.Series(name, values) for name, values in float_outputs.items()]
    expressions.extend(pl.Series(name, values) for name, values in bool_outputs.items())
    labelled = (
        ordered.with_columns(expressions)
        .with_columns([pl.col(name).fill_nan(None) for name in float_outputs])
        .with_columns(
            (pl.col("ts_ms") + MS_PER_HOUR).alias("ideal_entry_bar_start_ts_ms"),
            (pl.col("ts_ms") + 2 * MS_PER_HOUR).alias("ideal_entry_close_ts_ms"),
        )
    )
    labelled = labelled.with_columns(
        [
            (pl.col("ideal_entry_close_ts_ms") + horizon * MS_PER_HOUR).alias(f"path_{horizon}h_close_ts_ms")
            for horizon in horizons
        ]
    )
    integer_label_columns = ["observed_path_hours_through_72h"]
    if not extended:
        integer_label_columns.extend(f"path_{horizon}h_observed_hours" for horizon in horizons)
    if extended:
        integer_label_columns.extend(
            name
            for name in float_outputs
            if name.startswith("first_favorable_")
            or name.startswith("first_adverse_")
            or name.startswith("first_leave_")
        )
    labelled = labelled.with_columns([pl.col(name).cast(pl.Int64) for name in integer_label_columns])
    completeness_col = "path_all_frozen_horizons_complete" if extended else "path_all_minimal_labels_complete"
    labelled = labelled.with_columns(
        pl.all_horizontal([pl.col(f"path_{horizon}h_complete") for horizon in horizons]).alias(completeness_col)
    )
    if not extended:
        labelled = labelled.with_columns(
            [
                pl.when(~pl.col("ideal_entry_available"))
                .then(pl.lit("no_entry_anchor"))
                .when(~pl.col(f"path_{horizon}h_available"))
                .then(pl.lit("endpoint_unavailable"))
                .when(~pl.col(f"path_{horizon}h_complete"))
                .then(pl.lit("incomplete_path"))
                .otherwise(None)
                .alias(f"path_{horizon}h_missing_reason")
                for horizon in horizons
            ]
            + [pl.lit(True).alias(f"path_{horizon}h_hourly_extrema_interval_censored") for horizon in horizons]
        ).with_columns(
            pl.when(~pl.col("ideal_entry_available"))
            .then(pl.lit("no_next_executable_close"))
            .when(~pl.col("path_1h_complete"))
            .then(pl.lit("incomplete_1h_path"))
            .when(~pl.col("path_24h_complete"))
            .then(pl.lit("incomplete_24h_path"))
            .when(~pl.col("path_72h_complete"))
            .then(pl.lit("incomplete_72h_path"))
            .otherwise(None)
            .alias("missing_path_reason"),
            pl.lit(True).alias("hourly_extrema_interval_censored"),
        )
    return labelled.drop("_population_row_id").sort(_KEY_COLUMNS)


def append_continuous_path_labels(
    entry_anchor_tape: pl.DataFrame,
    hourly_klines: pl.DataFrame,
) -> pl.DataFrame:
    """Build the exact separate S04 minimal-label projection.

    S03 owns the anchor.  This function verifies its anchor against the supplied
    hourly grid, then exposes only underlying/short returns at 1/24/72h,
    short-perspective MFE/MAE at 24/72h, and registered support/completeness
    metadata.  It emits no first passage, cost, fill, execution, or alternate
    horizon.
    """

    entry_anchor_tape = _validate_entry_anchor_tape(entry_anchor_tape)
    if entry_anchor_tape.is_empty():
        return _empty_continuous_minimal_labels()

    k = _validate_hourly_klines(hourly_klines)
    candidate = _append_path_labels(k, extended=False).select(
        pl.col("symbol"),
        pl.col("ts_ms").alias("signal_ts_ms"),
        pl.col("ideal_entry_available").alias("_candidate_entry_available"),
        pl.col("ideal_entry_close_ts_ms").alias("_candidate_entry_anchor_ts_ms"),
        pl.col("ideal_entry_price").alias("_candidate_entry_price"),
        *[
            pl.col(column)
            for horizon in MINIMAL_RETURN_HORIZONS_HOURS
            for column in (
                f"path_{horizon}h_close_ts_ms",
                f"path_{horizon}h_observed_hours",
                f"path_{horizon}h_available",
                f"path_{horizon}h_complete",
                f"path_{horizon}h_missing_reason",
                f"path_{horizon}h_underlying_return",
                f"path_{horizon}h_short_directional_return",
                f"path_{horizon}h_hourly_extrema_interval_censored",
            )
        ],
        *[
            pl.col(column)
            for horizon in MINIMAL_EXCURSION_HORIZONS_HOURS
            for column in (f"path_{horizon}h_mfe", f"path_{horizon}h_mae")
        ],
        pl.col("path_all_minimal_labels_complete"),
        pl.col("missing_path_reason"),
    )
    joined = entry_anchor_tape.join(candidate, on=["symbol", "signal_ts_ms"], how="left")
    bad_anchor = joined.filter(
        pl.col("_candidate_entry_available").is_null()
        | (pl.col("_candidate_entry_available") != pl.col("entry_anchor_available"))
        | (
            pl.col("entry_anchor_available")
            & (
                (pl.col("_candidate_entry_anchor_ts_ms") != pl.col("entry_anchor_ts_ms"))
                | (pl.col("_candidate_entry_price") != pl.col("entry_price"))
            )
        )
    )
    if not bad_anchor.is_empty():
        raise RuntimeError("S03 entry anchor disagrees with the S04 hourly path grid")

    expressions: list[pl.Expr] = [
        pl.col("venue").cast(pl.String),
        pl.col("symbol").cast(pl.String),
        pl.col("decision_ts_ms").cast(pl.Int64),
        pl.col("canonical_instrument_id").cast(pl.String),
    ]
    for horizon in MINIMAL_RETURN_HORIZONS_HOURS:
        expressions.append(
            pl.when(pl.col("entry_anchor_available"))
            .then(pl.col(f"path_{horizon}h_close_ts_ms"))
            .otherwise(None)
            .cast(pl.Int64)
            .alias(f"path_{horizon}h_close_ts_ms")
        )
        expressions.extend(
            pl.col(column)
            for column in (
                f"path_{horizon}h_observed_hours",
                f"path_{horizon}h_available",
                f"path_{horizon}h_complete",
                f"path_{horizon}h_missing_reason",
                f"path_{horizon}h_underlying_return",
                f"path_{horizon}h_short_directional_return",
                f"path_{horizon}h_hourly_extrema_interval_censored",
            )
        )
        if horizon in MINIMAL_EXCURSION_HORIZONS_HOURS:
            expressions.extend(
                (
                    pl.col(f"path_{horizon}h_mfe").alias(f"path_{horizon}h_short_mfe"),
                    pl.col(f"path_{horizon}h_mae").alias(f"path_{horizon}h_short_mae"),
                )
            )
    expressions.extend((pl.col("path_all_minimal_labels_complete"), pl.col("missing_path_reason")))
    output = joined.select(expressions)
    return project_artifact_frame(output, CONTINUOUS_LABEL_SCHEMA_ID).sort(
        ["venue", "symbol", "decision_ts_ms"]
    )


def append_continuous_extended_path_atlas(population: pl.DataFrame) -> pl.DataFrame:
    """Explicitly append the non-default exploratory 1..72h path atlas."""

    return _append_path_labels(population, extended=True)


def attach_ideal_next_close_path_labels(population: pl.DataFrame) -> pl.DataFrame:
    """Backward-compatible explicit alias for the extended, non-default atlas."""

    return append_continuous_extended_path_atlas(population)


def build_continuous_population_features(
    hourly_klines: pl.DataFrame,
    stable_rmom: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Backward-compatible alias for the outcome-blind feature tape."""

    return build_continuous_feature_tape(hourly_klines, stable_rmom)


__all__ = [
    "COMPONENT_BITS",
    "COMPONENT_AGE_DAYS_MIN",
    "COMPONENT_ORDER",
    "COMPONENT_TRIGGERS",
    "COMPONENT_WEIGHTS",
    "CURRENT_ENTRY_CONFIRM_DELAY_HOURS",
    "CURRENT_FEATURE_SET",
    "CURRENT_LIQUIDITY_FLOOR",
    "CURRENT_RMOM_QUANTILE",
    "CURRENT_SELECTION_DECILE",
    "CURRENT_SIDE",
    "CURRENT_STRATEGY_PROFILE",
    "EVENT_WAVE_MAX_ADJACENT_GAP_HOURS",
    "EVENT_WAVE_MAX_SPAN_HOURS",
    "FROZEN_ADVERSE_FIRST_PASSAGE_PCTS",
    "FROZEN_FAVORABLE_FIRST_PASSAGE_PCTS",
    "FROZEN_FORWARD_HORIZONS_HOURS",
    "MINIMAL_EXCURSION_HORIZONS_HOURS",
    "MINIMAL_RETURN_HORIZONS_HOURS",
    "append_continuous_extended_path_atlas",
    "append_continuous_path_labels",
    "attach_ideal_next_close_path_labels",
    "build_continuous_entry_anchor",
    "build_continuous_feature_tape",
    "build_continuous_population_features",
]
