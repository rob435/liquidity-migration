"""CARRY target producer: the crowd-fee collector.

The daily book replays the selected registered rule over a rolling window of
Bybit hourly data. It calls the same scorer functions used by Lane-2 research;
the live frame omits research's forward-return field at the decision bar.

The rule replay recomputes its hysteresis from ``REPLAY_DAYS`` on every full
build. The producer separately persists the state that must survive a restart:
per-decision sizing equity, settled-print exit masks, and the hash-chained
pre-settlement handoff consumed by the independent Exodus daemon.

``CARRY_STRATEGY_PROFILE`` selects the profile resolved by
``resolve_carry_strategy_profile``. Profiles v3 and v4 select their own rule
files. Profiles v6 and v7 select ``lane2_carry_hold_v7``; v7 also enables the
pre-settlement running-rate clock, while v6 uses the settled-print clock. A
missed pre-settlement read leaves the settled-print fallback in force.

Rules with the whale leg read Binance's public top-trader position long/short
ratio. The producer caches end-of-day values per symbol and applies the rule's
48-hour freshness clause. Missing or stale values fail open to full size; this
public feed has no key, account read, or order path.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from liquidity_migration.core._common import coerce_int
from liquidity_migration.rules.engine_targets import (
    ParsedTargetBook,
    PublishedTargetBook,
    publish_target_book,
    read_target_book,
)
from liquidity_migration.strategy.account_candidate_universe import (
    carry_profile_universe_inputs,
    load_candidate_universe,
    require_profile_binding,
)
from liquidity_migration.marketdata.binance import BinanceDataError, BinanceUSDMData
from liquidity_migration.marketdata.bybit_market_data import BybitMarketData
from liquidity_migration.core.config import ExchangeConfig, ResearchConfig
from liquidity_migration.strategy.event_demo_data import (
    _demo_instruments,
    _download_recent_1h_klines,
    _kline_window,
    _launch_time_ms_by_symbol,
    _resolve_ticker_snapshot,
    _utc_now_ms,
    rank_top_turnover_symbols,
)
from liquidity_migration.policy.execution_environment import candidate_universe_realm, execution_environment
from liquidity_migration.rules.carry_hold import (
    CarryHoldConfig,
    carry_hold_weights,
    daily_grid,
    prepare_decision,
    top_n_universe,
)
from liquidity_migration.rules.carry_contract import (
    FLEET_EXECUTION_RULES,
    CarryDecision,
    DecisionInput as CarryDecisionInput,
    PresettlementObservation as CarryContractPresettlementObservation,
    PriorState as CarryContractPriorState,
    SizingAnchorRequest as CarrySizingAnchorRequest,
    StrategyConfig as CarryContractConfig,
    decide as decide_carry,
    render_target_book_text as render_carry_contract_book,
)
from liquidity_migration.strategy.carry_state import (
    CarryCycleState,
    load_carry_exit_state as _load_early_exits,
    persist_carry_exit_state as _save_early_exits,
)
from liquidity_migration.strategy.carry_runtime import (
    carry_holdings,
    carry_presettlement_observation,
    carry_reducer_clock_ms,
    carry_strategy_config,
    commit_carry_output,
    durable_presettlement_fire,
    in_scope_presettlement_events,
    load_durable_presettlement_events,
    presettlement_event_from_fire,
    settled_funding_observations,
)
from liquidity_migration.strategy.carry_market_inputs import (
    CarryPresettlementInput,
    CarryPresettlementTicker,
    build_carry_presettlement_inputs,
    carry_mark_prices,
    fetch_carry_presettlement_tickers as _fetch_presettle_tickers,
)
from liquidity_migration.strategy.carry_config import (
    CARRY_CONFIG_PATH,
    CARRY_STRATEGY_PROFILE_CHOICES,  # noqa: F401 - compatibility re-export
    DEFAULT_CARRY_STRATEGY_PROFILE,  # noqa: F401 - compatibility re-export
    EARLY_EXIT_STATE_NAME,
    MIN_REPLAY_DAYS,
    CarryConfigProvenance,
    CarryDemoCycleConfig,
    CarryEffectiveConfig,
    carry_cycles_dataset,
    resolve_carry_strategy_profile,
    validate_carry_demo_config as _validate_carry_demo_config,
)
from liquidity_migration.strategy.presettlement_events import (
    CarryPresettlementEvent,
    append_carry_presettlement_event,
)
from liquidity_migration.data.storage import (
    exclusive_file_lock,
    read_dataset,
    read_dataset_columns,
    write_dataset,
)
from liquidity_migration.runtime.engine_account_health import (
    EngineAccountReading,
    TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
    require_recent_engine_account,
)
from liquidity_migration.strategy.target_book_evidence import PublishedTargetCyclePayload
from liquidity_migration.core.venue_realm import VenueRealm

DAY_MS = 86_400_000
HOUR_MS = 3_600_000

#: Minimum universe symbols on the decision bar. Below this the data build is
#: broken and the engine fails closed, holding the previous targets, rather than
#: flattening a healthy book on a data hole. The real universe is 100 names.
MIN_DECISION_SYMBOLS = 50
_CONFIGS_DIR = CARRY_CONFIG_PATH.parent

class CarrySleeveError(RuntimeError):
    """Raised when the carry decision cannot be produced safely."""


def load_carry_config(path: Path) -> CarryHoldConfig:
    """The registered rule parameters, byte-identical to the Lane-2 file."""
    return CarryHoldConfig.from_json(str(path))


#: Per-process registered-rule memo for the cycle path; see ``_registered_rule``.
_REGISTERED_RULE_CACHE: dict[str, CarryHoldConfig] = {}


def _registered_rule(config_path: Path) -> CarryHoldConfig:
    """The cycle path's registered rule, parsed once per process per file.

    Never invalidated on purpose: registered rule files are immutable once
    committed, and changing the deployed rule already requires a producer
    restart operationally because the profile dial is read at startup. A
    60-second cycle therefore reuses the frozen ``CarryHoldConfig`` instance.
    """

    key = str(config_path)
    rule = _REGISTERED_RULE_CACHE.get(key)
    if rule is None:
        rule = load_carry_config(config_path)
        _REGISTERED_RULE_CACHE[key] = rule
    return rule


def resolve_carry_effective_config(
    cycle: "CarryDemoCycleConfig",
    *,
    exchange: ExchangeConfig,
    exchange_source: str,
    data_root: str | Path,
    data_root_source: str,
    target_book_path: str | Path,
    engine_heartbeat_path: str | Path,
    expected_account_user_id: str,
    invocation_id: str = "",
    operational_profile_source: str,
) -> CarryEffectiveConfig:
    """Resolve every process-wide CARRY input once with field provenance."""

    _validate_carry_demo_config(cycle)
    if len(cycle.operational_profile_sha256) != 64 or any(
        ch not in "0123456789abcdef" for ch in cycle.operational_profile_sha256
    ):
        raise ValueError("CARRY operational_profile_sha256 must be 64 lowercase hex characters")
    profile = resolve_carry_strategy_profile(cycle.strategy_profile)
    rule = _registered_rule(profile.config_path)
    rule_sha256 = hashlib.sha256(profile.config_path.read_bytes()).hexdigest()
    operational = str(operational_profile_source).strip()
    if not operational:
        raise ValueError("CARRY operational profile provenance source is required")
    if not exchange_source.strip():
        raise ValueError("CARRY exchange provenance source is required")
    if not data_root_source.strip():
        raise ValueError("CARRY data-root provenance source is required")
    raw_data_root = Path(data_root).expanduser()
    if not str(data_root).strip():
        raise ValueError("CARRY data root is required")
    resolved_data_root = raw_data_root.resolve()
    raw_candidate_path = str(cycle.candidate_universe_file).strip()
    resolved_candidate_path = Path(raw_candidate_path).expanduser().resolve() if raw_candidate_path else None
    raw_event_path = str(cycle.presettlement_event_path).strip()
    resolved_event_path = (
        Path(raw_event_path).expanduser().resolve()
        if raw_event_path
        else resolved_data_root / "carry_presettlement_events.jsonl"
    )
    resolved_cycle = dataclasses.replace(
        cycle,
        candidate_universe_file=(str(resolved_candidate_path) if resolved_candidate_path is not None else ""),
        presettlement_event_path=str(resolved_event_path),
    )
    raw_target_path = Path(target_book_path).expanduser()
    raw_heartbeat_path = Path(engine_heartbeat_path).expanduser()
    if not str(target_book_path).strip() or not raw_target_path.is_absolute():
        raise ValueError("CARRY target book path must be absolute")
    if not str(engine_heartbeat_path).strip() or not raw_heartbeat_path.is_absolute():
        raise ValueError("CARRY engine heartbeat path must be absolute")
    resolved_target_path = raw_target_path.resolve()
    resolved_heartbeat_path = raw_heartbeat_path.resolve()
    expected_user_id = str(expected_account_user_id).strip()
    if not expected_user_id:
        raise ValueError("CARRY expected engine account user id is required")
    exchange_detail = json.dumps(dataclasses.asdict(exchange), sort_keys=True, separators=(",", ":"))
    provenance = (
        CarryConfigProvenance(
            "strategy_profile",
            f"registered_profile:{cycle.strategy_profile}",
            f"{profile.config_path.resolve()}#{rule_sha256}",
        ),
        CarryConfigProvenance("execution_environment", "cycle_input"),
        CarryConfigProvenance(
            "candidate_universe_file",
            "cycle_input" if resolved_candidate_path is not None else "disabled",
            str(resolved_candidate_path or ""),
        ),
        CarryConfigProvenance(
            "presettlement_event_path",
            "cycle_input" if raw_event_path else "derived:data_root",
            str(resolved_event_path),
        ),
        CarryConfigProvenance("early_exit_enabled", "cycle_input"),
        CarryConfigProvenance("notional_multiplier", operational),
        CarryConfigProvenance("entry_leverage", operational),
        CarryConfigProvenance("declared_stop_loss_fraction", operational),
        CarryConfigProvenance("max_new_entries_per_cycle", operational),
        CarryConfigProvenance("capital_reference_usdt", operational),
        CarryConfigProvenance("operational_profile_sha256", operational),
        CarryConfigProvenance("replay_days", "cycle_input"),
        CarryConfigProvenance("workers", "cycle_input"),
        CarryConfigProvenance("ws_klines_enabled", "cycle_input"),
        CarryConfigProvenance("ws_klines_bootstrap_workers", "cycle_input"),
        CarryConfigProvenance("ws_klines_lookback_days", "cycle_input"),
        CarryConfigProvenance("ws_klines_universe_refresh_seconds", "cycle_input"),
        CarryConfigProvenance("ws_klines_topics_per_connection", "cycle_input"),
        CarryConfigProvenance("ws_klines_stale_warning_seconds", "cycle_input"),
        CarryConfigProvenance("ws_klines_stale_reconnect_seconds", "cycle_input"),
        CarryConfigProvenance("exchange", exchange_source, exchange_detail),
        CarryConfigProvenance("data_root", data_root_source, str(resolved_data_root)),
        CarryConfigProvenance(
            "sizing_anchor_path",
            "derived:data_root",
            str(resolved_data_root / ".cache" / "carry_sizing_anchors.json"),
        ),
        CarryConfigProvenance(
            "early_exit_state_path",
            "derived:data_root",
            str(resolved_data_root / _EARLY_EXIT_STATE_NAME),
        ),
        CarryConfigProvenance(
            "cycles_dataset",
            "derived:execution_environment",
            carry_cycles_dataset(resolved_cycle),
        ),
        CarryConfigProvenance("target_book_path", "runtime_environment", str(resolved_target_path)),
        CarryConfigProvenance("engine_heartbeat_path", "runtime_environment", str(resolved_heartbeat_path)),
        CarryConfigProvenance("expected_account_user_id", "runtime_environment", expected_user_id),
        CarryConfigProvenance("invocation_id", "service_manager", str(invocation_id)),
        *(
            CarryConfigProvenance(
                f"rule.{field.name}",
                f"registered_profile:{cycle.strategy_profile}",
                f"{profile.config_path.resolve()}#{rule_sha256}",
            )
            for field in dataclasses.fields(CarryHoldConfig)
        ),
    )
    expected = {
        *(field.name for field in dataclasses.fields(CarryDemoCycleConfig)),
        "exchange",
        "data_root",
        "sizing_anchor_path",
        "early_exit_state_path",
        "cycles_dataset",
        "target_book_path",
        "engine_heartbeat_path",
        "expected_account_user_id",
        "invocation_id",
        *(f"rule.{field.name}" for field in dataclasses.fields(CarryHoldConfig)),
    }
    actual = [row.field for row in provenance]
    if len(actual) != len(expected) or set(actual) != expected:
        raise ValueError("CARRY effective config provenance is incomplete")
    return CarryEffectiveConfig(
        cycle=resolved_cycle,
        profile=profile,
        rule=rule,
        exchange=exchange,
        data_root=resolved_data_root,
        sizing_anchor_path=(resolved_data_root / ".cache" / "carry_sizing_anchors.json"),
        early_exit_state_path=resolved_data_root / _EARLY_EXIT_STATE_NAME,
        presettlement_event_path=resolved_event_path,
        cycles_dataset=carry_cycles_dataset(resolved_cycle),
        target_book_path=resolved_target_path,
        engine_heartbeat_path=resolved_heartbeat_path,
        expected_account_user_id=expected_user_id,
        invocation_id=str(invocation_id),
        provenance=provenance,
    )


def decide_book(
    view: pl.DataFrame,
    cfg: CarryHoldConfig,
    decision_ts_ms: int,
) -> CarryDecision:
    """Replay the registered state machine and return the decision-bar book.

    ``view`` is a Bybit hourly frame with the venue-view columns
    (``symbol``, ``bar_ts_ms``, ``by_close``, ``by_turnover_quote``,
    ``by_funding``, ``by_funding_age_h``) covering at least
    ``MIN_REPLAY_DAYS`` before ``decision_ts_ms`` and starting exactly on a
    00:00 UTC bar (the daily grid inherits its phase from the window's first
    bar; a misaligned window would silently move the decision clock, which
    is a registered parameter).

    Fails closed when the window is misaligned, too short, or the decision bar
    is missing/thin. An *empty* book on a healthy decision bar is not an error:
    cash is a legitimate state (28% of days in the full record).
    """
    if decision_ts_ms % DAY_MS != 0:
        raise CarrySleeveError(f"decision ts {decision_ts_ms} is not a 00:00 UTC bar")
    if view.height == 0:
        raise CarrySleeveError("empty market view")
    first_ts = int(view["bar_ts_ms"].min())  # type: ignore[arg-type]
    last_ts = int(view["bar_ts_ms"].max())  # type: ignore[arg-type]
    if first_ts % DAY_MS != 0:
        raise CarrySleeveError(
            f"window starts at {first_ts}, not a 00:00 UTC bar; the daily grid "
            "phase would shift off the registered decision clock"
        )
    if last_ts < decision_ts_ms:
        raise CarrySleeveError(
            f"window ends {(decision_ts_ms - last_ts) // HOUR_MS}h before the decision bar; data build is stale"
        )
    replay_days = (decision_ts_ms - first_ts) // DAY_MS
    if replay_days < MIN_REPLAY_DAYS:
        raise CarrySleeveError(f"replay window {replay_days}d is below the {MIN_REPLAY_DAYS}d floor")

    grid = daily_grid(prepare_decision(view.filter(pl.col("bar_ts_ms") <= decision_ts_ms)))
    universe = top_n_universe(grid, cfg.universe_top_n)
    at_bar = universe.filter(pl.col("bar_ts_ms") == decision_ts_ms)
    if at_bar.height < MIN_DECISION_SYMBOLS:
        raise CarrySleeveError(
            f"decision bar carries {at_bar.height} universe symbols "
            f"(< {MIN_DECISION_SYMBOLS}); refusing to decide on a broken build"
        )
    book = carry_hold_weights(universe, cfg).filter(pl.col("bar_ts_ms") == decision_ts_ms)
    weights = {str(s): float(w) for s, w in zip(book["symbol"], book["w"], strict=True)}
    gross = sum(weights.values())
    if gross > cfg.gross_cap + 1e-9:
        raise CarrySleeveError(f"gross {gross:.6f} exceeds the registered cap {cfg.gross_cap}")
    if any(w <= 0.0 or w > cfg.per_name_cap + 1e-9 for w in weights.values()):
        raise CarrySleeveError("a weight violates the registered per-name bounds")
    return CarryDecision(
        decision_ts_ms=decision_ts_ms,
        weights=weights,
        universe_size=at_bar.height,
        replay_days=int(replay_days),
        gross=gross,
    )


# ---------------------------------------------------------------------------
# Cycle layer: the deployed CARRY target-book producer.
# ---------------------------------------------------------------------------

_logger = logging.getLogger(__name__)

#: Stable source id. The version lives in the registered strategy profile.
CARRY_STRATEGY_ID = "carry_hold"
ENGINE_CARRY_SLEEVE = "carry"
CARRY_FUNDING_DATASET = "carry_funding_events"

#: Fetch-universe breadth. The registered rule ranks its own top-100 by adv24
#: inside the replay, so the fetch set only needs to be a superset; 150 by
#: current 24h turnover comfortably covers the adv24 top-100 through rank churn.
CARRY_FETCH_UNIVERSE_TOP_N = 150

#: The daily decision becomes computable once the last kline of the prior UTC
#: day (23:00-00:00) is reliably served by REST — the same 20-minute margin the
#: rmom refresh timer uses for exactly the same bar.
DECISION_KLINE_LAG_MS = 20 * 60 * 1000

#: How long before the decision deadline a cycle may compute and freeze the
#: upcoming day's book. The window sits entirely inside the 20-minute kline
#: lag, so every input row for the new decision bar is already public and
#: cached when it opens; one 60-second grid cycle always lands in
#: 90 seconds, which is what lets the deadline wake publish instead of compute.
FREEZE_AHEAD_WINDOW_MS = 90 * 1000
#: Entry-signal validity. A new name not admitted within six hours belongs to
#: a stale decision and must wait for the next daily book.
SIGNAL_VALIDITY_MS = FLEET_EXECUTION_RULES.signal_validity_ms
#: Producer-side guard band before ``signal_valid_until_ms``. The engine's own
#: stale-entry cutoff is stricter; this prevents adding a name
#: to a producer book that is already too old to act on.
ENTRY_PUBLISH_GUARD_MS = FLEET_EXECUTION_RULES.engine_entry_cutoff_ms
#: Where to write the decided book for the Rust execution engine to follow.
#: Set on the fleet's units: the engine owns the account and this book is how
#: a carry decision reaches it. It is mandatory for every cycle.
ENGINE_TARGET_BOOK_PATH_ENV = "CARRY_ENGINE_TARGET_BOOK_PATH"
#: A sleeve whose newest successful decision is older than this is loudly
#: stale: today's decision still failing past 06:00 the next day.
DECISION_STALE_MS = FLEET_EXECUTION_RULES.book_validity_ms
#: Settled prints are carried into the first in-window bars from before the
#: window opens (same convention as ``cross_venue_panel.FUNDING_LOOKBACK_DAYS``)
#: so the earliest bars never show a spurious coverage gap.
FUNDING_LOOKBACK_DAYS = 2
#: Resize dead-band. A resize is a round trip at a measured ~15.6bp, so the
#: band sits where the tracking error it buys is worth the spread it spends:
#: closing a 5% notional gap costs ~0.8bp of the position, closing a 0.1% gap
#: costs ~0.02bp and buys nothing a daily sleeve can use. A band below the
#: sizing input's own noise floor churns the book on equity wiggle alone;
#: :meth:`CarryCycleState.sizing_equity` removes that cause and this band is the
#: backstop against fill rounding and partial fills re-creating it.
RESIZE_MIN_NOTIONAL_USDT = FLEET_EXECUTION_RULES.resize_floor_usdt
RESIZE_MIN_FRACTION_OF_STANDING = FLEET_EXECUTION_RULES.resize_floor_fraction
#: Entries below this notional could quantize to zero venue quantity and come
#: back as a terminal (permanently suppressing) rejection; skip them instead.
#: The venue's own floor is 5 USDT per order and the kernel enforces the exact
#: per-symbol rule (min qty, min notional, step rounding), so this is only a
#: coarse pre-filter with headroom over 5 — not a second safety margin. At
#: 10.0 it silently blanked a small account: the funded book missed both its
#: entries at 0.1 x 99.94 = 9.99 USDT, six cents under.
ENTRY_MIN_NOTIONAL_USDT = FLEET_EXECUTION_RULES.entry_floor_usdt
#: Decision-bar rows with a settled print, as a fraction of all decision-bar
#: rows. Every listed perp settles at least every 8h, so this sits near 1.0 when
#: healthy; a collapsed fraction means the funding cache is broken, and an empty
#: book computed from missing funding would flatten a healthy standing book.
MIN_DECISION_FUNDING_COVERAGE = 0.5
#: A standing symbol with fresh klines whose last cached print is older than
#: this has a funding-data hole (max settle interval is 8h). The hole decays the
#: trailing-funding series toward zero, which the velocity exit reads as a
#: recovery: a false exit taken on missing data.
STANDING_FUNDING_MAX_AGE_H = 25.0


def carry_decision_ts_ms(now_ms: int) -> int:
    """Return the day boundary whose decision is computable at ``now_ms``.

    The last kline of day D-1 (open 23:00, close 00:00) is reliably available
    ~minutes after 00:00; the 20-minute margin matches the rmom refresh timer,
    which waits on exactly the same bar. Before 00:20 UTC the target is still
    the PREVIOUS day's boundary — recomputing yesterday's decision is a no-op
    against a converged standing book, so cycles in that window stay quiet.
    """

    day_ts = (int(now_ms) // DAY_MS) * DAY_MS
    if now_ms >= day_ts + DECISION_KLINE_LAG_MS:
        return day_ts
    return day_ts - DAY_MS


def next_carry_decision_deadline_ts_ms(now_ms: int) -> int:
    """The next instant a NEW daily decision becomes computable (00:20 UTC).

    The daemon cuts its timer wait short at this instant, so the day's
    exit-first diff runs when the decision bar lands instead of up to a full
    grid interval later. Between boundaries every pass is an idempotent diff
    against the frozen decision, so no other instant is worth a wake.
    """

    day_ts = (int(now_ms) // DAY_MS) * DAY_MS
    candidate = day_ts + DECISION_KLINE_LAG_MS
    return candidate if int(now_ms) < candidate else candidate + DAY_MS


# ---------------------------------------------------------------------------
# Whale-ratio feed for rules with that leg: Binance top-trader position long/short
# end-of-day values, the live twin of the research panel's ``bn_tt_ls``. Reads
# a public no-key endpoint; every failure fails OPEN under the registered 48h
# freshness clause, so a dead feed thins the whale halving instead of blocking
# a decision.
# ---------------------------------------------------------------------------

#: Trailing complete UTC days of EOD values the cache maintains. The decision
#: bar needs yesterday's EOD and the value 72 bars earlier (~EOD four days
#: back); six covers both through a one-day feed hole, and anything staler
#: fails open under the registered 48h freshness clause anyway.
WHALE_FEED_DAYS = 6
#: While pairs are missing, retry no more than every five minutes — a Binance
#: outage must not add a fetch attempt to every 60-second cycle.
_WHALE_REFRESH_COOLDOWN_MS = 5 * 60 * 1000
#: Wall-clock bound on one refresh pass. Pairs that miss it stay missing and
#: heal on a later cycle; the decision never waits longer than this.
_WHALE_FETCH_DEADLINE_S = 45.0
_WHALE_FETCH_WORKERS = 8
_WHALE_STORE_KEEP_DAYS = 30
_WHALE_STORE_NAME = "binance_whale_daily.parquet"

_WHALE_STORE_SCHEMA: dict[str, Any] = {
    "symbol": pl.String,
    # The day's END stamp (next UTC midnight, ms) — when the EOD value becomes
    # knowable, and the key the panel's as-of attach uses.
    "day_end_ms": pl.Int64,
    # Null value = the venue has nothing for this symbol-day (not listed on
    # Binance, or no ratio rows). Recorded so the pair is not refetched.
    "bn_tt_ls": pl.Float64,
    "fetched_ms": pl.Int64,
}


def _whale_store_path(root: Path) -> Path:
    return root / _WHALE_STORE_NAME


def _load_whale_store(root: Path) -> pl.DataFrame:
    path = _whale_store_path(root)
    if path.exists():
        try:
            df = pl.read_parquet(path)
            if set(_WHALE_STORE_SCHEMA) <= set(df.columns):
                return df.select(list(_WHALE_STORE_SCHEMA))
        except Exception:  # noqa: BLE001 - a torn cache refetches; it never blocks
            _logger.warning("whale cache unreadable, refetching: %s", path)
    return pl.DataFrame(schema=_WHALE_STORE_SCHEMA)


def _whale_missing_pairs(store: pl.DataFrame, symbols: list[str], now_ms: int) -> list[tuple[str, int]]:
    newest_end = (int(now_ms) // DAY_MS) * DAY_MS
    wanted_ends = [newest_end - k * DAY_MS for k in range(WHALE_FEED_DAYS)]
    have: set[tuple[str, int]] = set()
    if store.height:
        have = set(zip(store["symbol"].to_list(), store["day_end_ms"].to_list(), strict=True))
    return [(s, e) for s in symbols for e in wanted_ends if (s, e) not in have]


def _fetch_whale_pair(symbol: str, day_end_ms: int, client_factory: Any) -> tuple[str, int, float | None] | None:
    """One (symbol, day) EOD read: the last 5-minute ratio print of the day,
    the same value ``refresh_binance_metrics.py`` collapses to ``tt_ls_eod``.

    ``None`` = transient failure, nothing recorded, retried on a later pass.
    A tuple with a null value = the venue definitively has nothing here.
    """
    client = client_factory()
    try:
        rows = client.get_top_trader_ls_position_ratio(symbol, "5m", int(day_end_ms) - 6 * HOUR_MS, int(day_end_ms))
    except BinanceDataError as exc:
        if getattr(exc, "permanent", False):
            return (symbol, int(day_end_ms), None)
        return None
    except Exception:  # noqa: BLE001 - transport oddity; retry on a later pass
        return None
    if not rows:
        return (symbol, int(day_end_ms), None)
    last = max(rows, key=lambda r: int(r["timestamp"]))
    try:
        return (symbol, int(day_end_ms), float(last["longShortRatio"]))
    except (KeyError, TypeError, ValueError):
        return (symbol, int(day_end_ms), None)


def _whale_client_factory() -> BinanceUSDMData:
    # Snappier than the offline-build defaults: a missed pair heals on the
    # next cooldown pass, so long retries only stall the cycle.
    return BinanceUSDMData(retries=2, retry_sleep_seconds=0.25, timeout_seconds=5.0)


def _refresh_carry_whale_cache(
    root: Path,
    symbols: list[str],
    *,
    now_ms: int,
    state: CarryCycleState,
    client_factory: Any = None,
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Serve the whale EOD event frame, refreshing missing symbol-days first.

    Never raises. The returned frame holds one row per known (symbol, day)
    EOD value — ``symbol``, ``_tt_ls_ts_ms`` (day-end stamp), ``bn_tt_ls`` —
    ready for the view's as-of attach; rows the venue has nothing for are
    held in the store as nulls (so they are not refetched) and excluded here,
    which is exactly how the research panel treats them.
    """
    factory = client_factory or _whale_client_factory
    stats: dict[str, Any] = {}
    store = state.whale_store
    try:
        if store is None:
            store = _load_whale_store(root)
        missing = _whale_missing_pairs(store, symbols, now_ms)
        cooling = (
            state.whale_last_attempt_ms is not None
            and int(now_ms) - state.whale_last_attempt_ms < _WHALE_REFRESH_COOLDOWN_MS
        )
        fetched = 0
        if missing and not cooling:
            state.whale_last_attempt_ms = int(now_ms)
            rows: list[dict[str, Any]] = []
            pool = ThreadPoolExecutor(max_workers=_WHALE_FETCH_WORKERS)
            futures = [pool.submit(_fetch_whale_pair, sym, end, factory) for sym, end in missing]
            try:
                for fut in as_completed(futures, timeout=_WHALE_FETCH_DEADLINE_S):
                    res = fut.result()
                    if res is not None:
                        rows.append(
                            {
                                "symbol": res[0],
                                "day_end_ms": res[1],
                                "bn_tt_ls": res[2],
                                "fetched_ms": int(now_ms),
                            }
                        )
            except TimeoutError:
                undone = sum(1 for f in futures if not f.done())
                _logger.warning(
                    "whale refresh hit the %.0fs bound with %d pairs pending; they retry later",
                    _WHALE_FETCH_DEADLINE_S,
                    undone,
                )
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            if rows:
                fetched = len(rows)
                keep_from = (int(now_ms) // DAY_MS) * DAY_MS - _WHALE_STORE_KEEP_DAYS * DAY_MS
                store = (
                    pl.concat([store, pl.DataFrame(rows, schema=_WHALE_STORE_SCHEMA)])
                    .unique(subset=["symbol", "day_end_ms"], keep="last")
                    .filter(pl.col("day_end_ms") >= keep_from)
                    .sort(["symbol", "day_end_ms"])
                )
                path = _whale_store_path(root)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".tmp")
                store.write_parquet(tmp)
                os.replace(tmp, path)
        state.whale_store = store
        stats = {
            "whale_pairs_missing": max(0, len(missing) - fetched),
            "whale_pairs_fetched": fetched,
        }
    except Exception as exc:  # noqa: BLE001 - the whale leg fails open, never the cycle
        _logger.exception("whale refresh failed; the whale halving fails open this cycle")
        stats["whale_error"] = f"{type(exc).__name__}: {exc}"[:200]
        if store is None:
            store = pl.DataFrame(schema=_WHALE_STORE_SCHEMA)
    events = (
        store.filter(pl.col("bn_tt_ls").is_not_null())
        .select(
            "symbol",
            pl.col("day_end_ms").alias("_tt_ls_ts_ms"),
            "bn_tt_ls",
        )
        .sort(["_tt_ls_ts_ms", "symbol"])
    )
    stats["whale_event_rows"] = events.height
    return events, stats


# ---------------------------------------------------------------------------
# Early exit: sell an exiting name at the print that ends it rather than at
# the next midnight. A held name's exit condition is the
# registered one — the latest settled print at or above -exit_bp — and every
# print that can fire it settles intraday on the modern (sub-8h) book, so the
# fire needs no new threshold and no new data: the hourly funding sweep
# already carries the print. Fired names are masked out of the desired book
# until the next decision bar so the frozen day cannot re-buy them; if the
# next midnight print is deep again, the next decision re-enters normally
# (the measured misfire cost, charged in the research note).
# ---------------------------------------------------------------------------

_EARLY_EXIT_STATE_NAME = EARLY_EXIT_STATE_NAME


def _early_exit_state_path(root: Path) -> Path:
    return root / _EARLY_EXIT_STATE_NAME


def _apply_early_exits(
    *,
    decision: CarryDecision,
    rule: CarryHoldConfig,
    funding: pl.DataFrame | None,
    state: CarryCycleState,
    state_path: Path,
    now_ms: int,
) -> tuple[CarryDecision, list[str]]:
    """Compatibility wrapper over the shared CARRY lifecycle reducer."""

    output = decide_carry(
        CarryDecisionInput(
            now_ms=int(now_ms),
            decision=decision,
            settled_funding=settled_funding_observations(
                funding,
                decision_ts_ms=decision.decision_ts_ms,
                now_ms=int(now_ms),
            ),
        ),
        state.reducer_prior(exit_state_path=state_path),
        carry_strategy_config(
            profile_name="carry_compat_v1",
            compatibility_source="carry_compat_v1",
            rule=rule,
            early_exit_enabled=True,
            presettlement_exit_enabled=False,
            notional_multiplier=1.0,
            entry_leverage=1.0,
            stop_loss_fraction=0.35,
            max_new_entries_per_cycle=1,
            capital_reference_usdt=0.0,
        ),
    )
    state.persist_reducer_state(exit_state_path=state_path, state=output.next_state)
    assert output.effective_decision is not None
    return output.effective_decision, list(output.settled_exit_fires)


# --- the v7 pre-settlement exit read ---------------------------------------
# Bybit locks the upcoming crowd-fee rate just under a minute before it pays
# (tardis tick evidence, 2026-08-19), so inside the last minutes the ticker's
# running rate IS the print, visible early. v7 fires the same registered exit
# test on that read up to 15 minutes ahead and sells before the post-payment
# dump instead of one minute into it. Window and margin (15 min, none) are the
# measured optimum. The settled-print path stays active when this read is
# missing or fails.

_PRESETTLE_WINDOW_MS = FLEET_EXECUTION_RULES.presettlement_window_ms
#: Fetch gate slack: every Bybit settlement sits on an hour boundary, so the
#: batch read only runs when one is at most window+slack away.
_PRESETTLE_FETCH_SLACK_MS = 90_000


@dataclasses.dataclass(frozen=True, slots=True)
class CarryPresettlementPlan:
    """Pure result: events to durably publish, then apply to CARRY state."""

    publication_events: tuple[CarryPresettlementEvent, ...]
    transition_events: tuple[CarryPresettlementEvent, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class CarryPresettlementTransition:
    """Pure state transition after every planned event is durable."""

    decision: CarryDecision
    fired: tuple[tuple[str, int], ...]
    new_fires: tuple[str, ...]
    fire_details: tuple[CarryPresettlementEvent, ...]

    def fired_by_symbol(self) -> dict[str, int]:
        return dict(self.fired)


def plan_carry_presettlement_exits(
    *,
    decision: CarryDecision,
    rule: CarryHoldConfig,
    prior_fired: Mapping[str, int],
    inputs: tuple[CarryPresettlementInput, ...],
    durable_events: tuple[CarryPresettlementEvent, ...],
    environment: str,
    source_profile: str,
) -> CarryPresettlementPlan:
    """Compatibility view of the shared reducer's handoff effects.

    Durable rows rebuild the mask silently. Only newly planned events are
    returned for publication, so crash recovery never appends a prior event a
    second time.
    """

    relevant_durable = tuple(
        event
        for event in durable_events
        if event.environment == environment
        and event.source_config_id == rule.config_id
        and event.decision_ts_ms == decision.decision_ts_ms
        and event.symbol in decision.weights
    )
    observations = tuple(
        carry_presettlement_observation(
            symbol=row.ticker.symbol,
            observed_ts_ms=row.observed_ts_ms,
            settlement_ts_ms=row.ticker.settlement_ts_ms,
            running_rate=row.ticker.running_rate,
            mark_px=row.ticker.mark_px,
            carry_side=row.carry_side,
            carry_qty=row.carry_qty,
            carry_avg_entry_px=row.carry_avg_entry_px,
        )
        for row in inputs
    )
    now_ms = max(
        [decision.decision_ts_ms + 1]
        + [row.observed_ts_ms for row in observations]
        + [event.fired_ts_ms for event in relevant_durable]
    )
    output = decide_carry(
        CarryDecisionInput(
            now_ms=now_ms,
            decision=decision,
            presettlement=observations,
            durable_presettlement_fires=tuple(
                durable_presettlement_fire(event) for event in relevant_durable
            ),
        ),
        CarryContractPriorState(fired_exits=tuple(sorted(prior_fired.items()))),
        carry_strategy_config(
            profile_name=source_profile,
            compatibility_source=source_profile,
            rule=rule,
            early_exit_enabled=True,
            presettlement_exit_enabled=True,
            notional_multiplier=1.0,
            entry_leverage=1.0,
            stop_loss_fraction=0.35,
            max_new_entries_per_cycle=1,
            capital_reference_usdt=0.0,
        ),
    )
    publication_events = tuple(
        presettlement_event_from_fire(
            fire,
            environment=environment,
            source_profile=source_profile,
            source_config_id=rule.config_id,
        )
        for fire in output.presettlement_fires
    )
    transition_by_symbol = {event.symbol: event for event in relevant_durable}
    transition_by_symbol.update({event.symbol: event for event in publication_events})
    transition_events = tuple(
        sorted(transition_by_symbol.values(), key=lambda row: row.to_strategy_event().order_key)
    )
    return CarryPresettlementPlan(
        publication_events=tuple(
            sorted(publication_events, key=lambda row: row.to_strategy_event().order_key)
        ),
        transition_events=transition_events,
    )


def publish_carry_presettlement_plan(path: Path, plan: CarryPresettlementPlan) -> None:
    """Durably append every planned handoff before CARRY state may change."""

    if not path.is_absolute():
        raise ValueError("CARRY pre-settlement event path must be absolute")
    for event in plan.publication_events:
        append_carry_presettlement_event(path, event)


def transition_carry_presettlement_state(
    *,
    decision: CarryDecision,
    prior_fired: Mapping[str, int],
    durable_events: tuple[CarryPresettlementEvent, ...],
) -> CarryPresettlementTransition:
    """Purely apply events whose publication has already completed."""

    fired = dict(prior_fired)
    new_fires: list[str] = []
    for event in durable_events:
        if event.decision_ts_ms != decision.decision_ts_ms or event.symbol not in decision.weights:
            raise ValueError("CARRY pre-settlement transition event is out of scope")
        fired[event.symbol] = decision.decision_ts_ms
        new_fires.append(event.symbol)
    masked = {symbol: weight for symbol, weight in decision.weights.items() if symbol not in fired}
    return CarryPresettlementTransition(
        decision=dataclasses.replace(decision, weights=masked, gross=sum(masked.values())),
        fired=tuple(sorted(fired.items())),
        new_fires=tuple(new_fires),
        fire_details=durable_events,
    )


def persist_carry_presettlement_state(path: Path, transition: CarryPresettlementTransition) -> None:
    """Persist the already-published transition to CARRY's private mask."""

    _save_early_exits(path, transition.fired_by_symbol())


def _apply_presettle_exits(
    *,
    decision: CarryDecision,
    rule: CarryHoldConfig,
    state: CarryCycleState,
    state_path: Path,
    event_path: Path,
    inputs: tuple[CarryPresettlementInput, ...],
    environment: str,
    source_profile: str,
) -> tuple[CarryDecision, list[str], list[CarryPresettlementEvent]]:
    """Orchestrate input replay, publication, transition, and persistence."""

    if state.early_exits is None:
        state.early_exits = _load_early_exits(state_path)
    prior_fired = dict(state.early_exits)
    plan = plan_carry_presettlement_exits(
        decision=decision,
        rule=rule,
        prior_fired=prior_fired,
        inputs=inputs,
        durable_events=load_durable_presettlement_events(event_path),
        environment=environment,
        source_profile=source_profile,
    )
    publish_carry_presettlement_plan(event_path, plan)
    transition = transition_carry_presettlement_state(
        decision=decision,
        prior_fired=prior_fired,
        durable_events=plan.transition_events,
    )
    if transition.new_fires:
        state.early_exits = transition.fired_by_symbol()
        try:
            persist_carry_presettlement_state(state_path, transition)
        except Exception:  # noqa: BLE001 - the durable event rebuilds this mask
            _logger.warning("early-exit state not persisted; the durable handoff will rebuild it")
    return (
        transition.decision,
        [event.symbol for event in plan.publication_events],
        list(plan.publication_events),
    )


def _apply_drop_exits(
    *,
    decision: CarryDecision,
    state: CarryCycleState,
) -> tuple[CarryDecision, list[str], int]:
    """Compatibility wrapper over the shared reducer's upcoming-book mask."""

    upcoming = state.frozen_decision(decision.decision_ts_ms + DAY_MS)
    if upcoming is None:
        return decision, [], 0
    output = decide_carry(
        CarryDecisionInput(
            now_ms=decision.decision_ts_ms + 1,
            decision=decision,
            upcoming_decision=upcoming[0],
        ),
        CarryContractPriorState(),
        CarryContractConfig(
            profile_name="carry_compat_v1",
            accepted_book_sources=(),
            exit_bp=1.0,
            early_exit_enabled=False,
            presettlement_exit_enabled=False,
            notional_multiplier=1.0,
            entry_leverage=1.0,
            stop_loss_fraction=0.35,
            max_new_entries_per_cycle=1,
        ),
    )
    assert output.effective_decision is not None
    dropped = list(output.drop_exit_fires)
    return output.effective_decision, dropped, len(dropped)


def _attach_whale_columns(view: pl.DataFrame, whale_events: pl.DataFrame | None) -> pl.DataFrame:
    """Attach ``bn_tt_ls`` / ``bn_tt_ls_age_h`` exactly the way the research
    panel does — backward as-of of day-end EOD events per symbol, age in
    float hours — so the registered rule computes the whale change from the
    same shape live. ``None`` leaves the input frame unchanged for a rule with
    no whale leg.
    """
    if whale_events is None:
        return view
    if whale_events.is_empty():
        return view.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("bn_tt_ls"),
            pl.lit(None, dtype=pl.Float64).alias("bn_tt_ls_age_h"),
        )
    events = whale_events.select(
        pl.col("symbol").cast(pl.String),
        pl.col("_tt_ls_ts_ms").cast(pl.Int64),
        pl.col("bn_tt_ls").cast(pl.Float64),
    ).sort(["_tt_ls_ts_ms", "symbol"])
    return (
        view.join_asof(
            events,
            left_on="bar_ts_ms",
            right_on="_tt_ls_ts_ms",
            by="symbol",
            strategy="backward",
            # Same global-ts-then-symbol sortedness argument as the funding
            # join above; polars cannot verify it once `by` groups are given.
            check_sortedness=False,
        )
        .with_columns(((pl.col("bar_ts_ms") - pl.col("_tt_ls_ts_ms")) / HOUR_MS).alias("bn_tt_ls_age_h"))
        .drop("_tt_ls_ts_ms")
    )


def _empty_venue_view() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "symbol": pl.String,
            "bar_ts_ms": pl.Int64,
            "by_close": pl.Float64,
            "by_turnover_quote": pl.Float64,
            "by_funding": pl.Float64,
            "by_funding_age_h": pl.Float64,
        }
    )


def _carry_venue_view(
    klines: pl.DataFrame,
    funding: pl.DataFrame,
    *,
    window_start_ms: int,
    max_bar_ts_ms: int,
    whale_events: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Build the Bybit venue-view frame the registered engine decides on.

    KEY CONVENTION — knowledge-time keying. A kline row stamped ``ts_ms`` is
    the bar's OPEN and is not knowable until ``ts_ms + 1h``; the research
    panel (`cross_venue_panel`) therefore keys every decision row by
    ``decision_ts_ms = bar_ts_ms + 1h``. This live view applies the same shift
    directly: each kline is keyed by its CLOSE (``bar_ts_ms = ts_ms + 1h``),
    so a row keyed T carries exactly the information public at T — the close
    printed at T and the last funding print settled at or before T. That is
    what makes the 00:00 decision bar computable at 00:20: it is the
    23:00-00:00 kline plus the 00:00 settlement, both public by then. Keying
    by open instead would leave the decision bar unknowable until 01:00 and
    silently shift the registered daily decision clock by an hour.

    Funding is the one field carried forward (backward as-of join per symbol,
    inclusive boundary), with staleness exposed as ``by_funding_age_h`` in
    exact float hours — a settlement stamped exactly at a bar key gets age
    0.0, which the registered settlement detector depends on. Bars with no
    prior settlement in-window keep a null ``by_funding``; nothing else is
    filled across gaps.
    """

    if klines.is_empty():
        return _empty_venue_view()
    keyed = (
        klines.select(
            pl.col("symbol").cast(pl.String),
            (pl.col("ts_ms").cast(pl.Int64) + HOUR_MS).alias("bar_ts_ms"),
            pl.col("close").cast(pl.Float64).alias("by_close"),
            pl.col("turnover_quote").cast(pl.Float64).alias("by_turnover_quote"),
        )
        .filter(pl.col("bar_ts_ms").is_between(int(window_start_ms), int(max_bar_ts_ms)))
        .sort(["bar_ts_ms", "symbol"])
    )
    if keyed.is_empty():
        return _empty_venue_view()
    if funding.is_empty():
        view = keyed.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("by_funding"),
            pl.lit(None, dtype=pl.Float64).alias("by_funding_age_h"),
        )
        return _attach_whale_columns(view, whale_events).sort(["symbol", "bar_ts_ms"])
    events = (
        funding.select(
            pl.col("symbol").cast(pl.String),
            pl.col("funding_ts_ms").cast(pl.Int64),
            pl.col("funding_rate").cast(pl.Float64).alias("by_funding"),
        )
        .filter(pl.col("funding_ts_ms") <= int(max_bar_ts_ms))
        .unique(subset=["symbol", "funding_ts_ms"], keep="last")
        .sort(["funding_ts_ms", "symbol"])
    )
    view = keyed.join_asof(
        events,
        left_on="bar_ts_ms",
        right_on="funding_ts_ms",
        by="symbol",
        strategy="backward",
        # Both sides are globally sorted ts-first, which implies per-`by`-group
        # order; polars cannot verify sortedness once `by` groups are given
        # (same assertion the research panel makes for the same join).
        check_sortedness=False,
    ).with_columns(((pl.col("bar_ts_ms") - pl.col("funding_ts_ms")) / HOUR_MS).alias("by_funding_age_h"))
    view = view.drop("funding_ts_ms")
    return _attach_whale_columns(view, whale_events).sort(["symbol", "bar_ts_ms"])


def _validate_carry_view_health(
    view: pl.DataFrame,
    *,
    decision_ts_ms: int,
    standing_symbols: set[str],
) -> None:
    """Refuse decisions whose funding inputs are visibly broken.

    ``decide_book``'s floor counts decision-bar SYMBOLS, which stays healthy
    even when every funding value is null, and an all-null build yields a
    legitimate-looking EMPTY book whose diff would flatten a healthy standing
    book. Two guards close that:

    * decision-bar settled-print coverage below ``MIN_DECISION_FUNDING_COVERAGE``
      means the funding cache, not the market, is broken;
    * a STANDING symbol with a fresh decision-bar kline but a stale or absent
      funding print has a per-symbol hole, which decays its trailing-funding
      series toward zero and reads to the velocity exit as a recovery. A
      delisted standing symbol never trips this (its klines end too).
    """

    at_bar = view.filter(pl.col("bar_ts_ms") == int(decision_ts_ms))
    if at_bar.is_empty():
        # decide_book raises its own, more precise staleness error.
        return
    covered = int(at_bar.get_column("by_funding").is_finite().fill_null(False).sum())
    coverage = covered / at_bar.height
    if coverage < MIN_DECISION_FUNDING_COVERAGE:
        raise CarrySleeveError(
            f"decision-bar settled-print coverage {coverage:.2f} is below "
            f"{MIN_DECISION_FUNDING_COVERAGE}; the funding cache looks broken and an "
            "empty book computed from missing funding must not flatten the standing book"
        )
    if not standing_symbols:
        return
    stale_standing = at_bar.filter(
        pl.col("symbol").is_in(sorted(standing_symbols))
        & (
            pl.col("by_funding_age_h").is_null()
            | (pl.col("by_funding_age_h") > STANDING_FUNDING_MAX_AGE_H)
            | ~pl.col("by_funding").is_finite().fill_null(False)
        )
    )
    if stale_standing.height:
        names = ",".join(sorted(stale_standing.get_column("symbol").to_list()))
        raise CarrySleeveError(
            f"standing symbols with live klines but stale funding prints or non-finite rates: "
            f"{names}; "
            "holding the book rather than risking a false velocity exit on a data hole"
        )


def _trailing_settled_funding(
    funding: pl.DataFrame,
    *,
    decision_ts_ms: int,
) -> dict[str, float]:
    """Per-symbol sum of settled prints over ``(decision-24h, decision]``.

    Ordering/journaling aid only — the deepest (most negative) trailing crowd
    payment ranks competing entries under the per-cycle cap. Computed straight
    from the funding events so a kline gap cannot distort the ordering; the
    rule's own sizing uses its registered in-frame construction.
    """

    if funding.is_empty():
        return {}
    window = funding.filter(
        (pl.col("funding_ts_ms") > int(decision_ts_ms) - DAY_MS) & (pl.col("funding_ts_ms") <= int(decision_ts_ms))
    )
    if window.is_empty():
        return {}
    sums = window.group_by("symbol").agg(pl.col("funding_rate").sum().alias("trail"))
    return {str(row["symbol"]): float(row["trail"]) for row in sums.iter_rows(named=True)}


def _freeze_decision_ahead(
    *,
    state: CarryCycleState,
    rule: CarryHoldConfig,
    klines: pl.DataFrame,
    funding: pl.DataFrame,
    build_stats: dict[str, Any],
    ahead_ts_ms: int,
    current_decision_ts_ms: int,
    replay_days: int,
    standing_symbols: set[str],
    whale_events: pl.DataFrame | None = None,
) -> bool:
    """Compute and freeze the UPCOMING day's book from this cycle's build.

    A decision keyed ``ahead_ts_ms`` reads only rows stamped at or before that
    key, and inside :data:`FREEZE_AHEAD_WINDOW_MS` every such row is already
    public and cached (the 20-minute decision clock is a REST-serving margin,
    not a data-arrival instant). Computing the same frame tens of seconds
    early therefore reads identical inputs, and the gates below refuse
    whenever this build carries repair-pending evidence — klines that needed
    REST repair or never came from the WS store, or a funding sweep with
    fetch failures (an outage that heals before the deadline would hand the
    deadline's own rebuild prints this build never saw). One residual is
    documented rather than gated: the top-150 fetch universe is sampled from
    the ticker snapshot at this instant, so per-symbol ticker staleness that
    heals inside the window can shrink the frozen decision's reachable set.
    A total ticker outage already refuses itself (an empty universe fails the
    build; a standing-only universe fails ``decide_book``'s 50-symbol floor).
    Failure here is never a cycle failure: the deadline pass computes
    authoritatively, as before.
    """

    if ahead_ts_ms <= current_decision_ts_ms or ahead_ts_ms % DAY_MS != 0:
        return False
    if state.frozen_decision(ahead_ts_ms) is not None:
        # Already warmed by an earlier in-window cycle. False, not True:
        # the return value feeds the payload's "this cycle froze it" flag,
        # and a duplicate receipt would say two cycles both froze the day.
        return False
    if int(build_stats.get("kline_fetched_rows", 0)) != 0:
        return False
    if int(build_stats.get("kline_store_rows", 0)) <= 0:
        return False
    if int(build_stats.get("funding_fetch_failures", 0)) != 0:
        return False
    try:
        view = _carry_venue_view(
            klines,
            funding,
            window_start_ms=ahead_ts_ms - replay_days * DAY_MS,
            max_bar_ts_ms=ahead_ts_ms,
            whale_events=whale_events,
        )
        if view.is_empty():
            return False
        # Same daily-grid phase trim as the deadline path would apply.
        first_ts = int(view.get_column("bar_ts_ms").min())  # type: ignore[arg-type]
        if first_ts % DAY_MS != 0:
            aligned_start = ((first_ts // DAY_MS) + 1) * DAY_MS
            view = view.filter(pl.col("bar_ts_ms") >= aligned_start)
        universe_eligible = int(view.get_column("symbol").n_unique()) if not view.is_empty() else 0
        _validate_carry_view_health(view, decision_ts_ms=ahead_ts_ms, standing_symbols=standing_symbols)
        trail_by_symbol = _trailing_settled_funding(funding, decision_ts_ms=ahead_ts_ms)
        decision = decide_book(view, rule, ahead_ts_ms)
    except Exception as exc:  # noqa: BLE001 - warm-up only; the deadline retries from scratch
        _logger.info("carry freeze-ahead for %s not ready: %s", ahead_ts_ms, exc)
        return False
    state.freeze_decision(
        decision_ts_ms=ahead_ts_ms,
        decision=decision,
        trail_by_symbol=trail_by_symbol,
        universe_eligible=universe_eligible,
        input_evidence={
            "candidate_universe_artifact_sha256": str(build_stats.get("candidate_universe_artifact_sha256", "")),
            "candidate_universe_file_sha256": str(build_stats.get("candidate_universe_file_sha256", "")),
        },
        frozen_ahead=True,
    )
    _logger.info(
        "carry decision for %s frozen ahead of the deadline: book=%d gross=%.3f universe=%d",
        ahead_ts_ms,
        len(decision.weights),
        decision.gross,
        universe_eligible,
    )
    return True


@dataclasses.dataclass(frozen=True, slots=True)
class CarryTargetPlan:
    """The exact absolute book and its per-reason admission counts."""

    desired_book_size: int
    desired_gross_weight: float
    planned_exits: int
    planned_entries: int
    planned_resizes: int
    resize_mark_missing_skips: int
    entry_cap_deferrals: int
    entry_validity_expired_skips: int
    entry_dust_skips: int
    engine_blocked_entries: int
    entry_blocked_reason: str
    book_written: bool
    target_book_object_path: str


@dataclasses.dataclass(frozen=True, slots=True)
class CarryPlanningOutput:
    """Pure planner result; publication has not touched the filesystem."""

    plan: CarryTargetPlan
    target_book_text: str | None


def _empty_carry_plan(*, entry_blocked_reason: str = "") -> CarryTargetPlan:
    return CarryTargetPlan(
        desired_book_size=0,
        desired_gross_weight=0.0,
        planned_exits=0,
        planned_entries=0,
        planned_resizes=0,
        resize_mark_missing_skips=0,
        entry_cap_deferrals=0,
        entry_validity_expired_skips=0,
        entry_dust_skips=0,
        engine_blocked_entries=0,
        entry_blocked_reason=entry_blocked_reason,
        book_written=False,
        target_book_object_path="",
    )


def _write_engine_target_book(
    *,
    target_book_path: str | Path,
    desired: Mapping[str, float],
    decision_ts_ms: int,
    sizing_equity_usdt: float,
    notional_multiplier: float,
    stop_loss_fraction: float,
    entry_leverage: float,
    strategy_profile: str,
) -> PublishedTargetBook:
    """Durably publish one decided absolute book to the Rust engine.

    The daily decision has two distinct deadlines. Its rows may only open or
    grow through the measured six-hour signal window, while the absolute book
    remains serveable until the decision itself is operationally stale. The
    latter lets a late-day restart publish a valid hold/reduction book without
    re-authorizing an old entry.
    """
    path_text = str(target_book_path).strip()
    if not path_text:
        raise ValueError("target_book_path must name the Rust target book")
    return publish_target_book(
        Path(path_text),
        render_carry_target_book(
            desired=desired,
            decision_ts_ms=decision_ts_ms,
            sizing_equity_usdt=sizing_equity_usdt,
            notional_multiplier=notional_multiplier,
            stop_loss_fraction=stop_loss_fraction,
            entry_leverage=entry_leverage,
            strategy_profile=strategy_profile,
        ),
    )


def render_carry_target_book(
    *,
    desired: Mapping[str, float],
    decision_ts_ms: int,
    sizing_equity_usdt: float,
    notional_multiplier: float,
    stop_loss_fraction: float,
    entry_leverage: float,
    strategy_profile: str,
) -> str:
    """Pure serialization of one decided absolute CARRY book."""

    if not sizing_equity_usdt > 0.0:
        raise ValueError("cannot render a target book without positive sizing equity")
    return render_carry_contract_book(
        desired=desired,
        decision_ts_ms=decision_ts_ms,
        sizing_equity_usdt=sizing_equity_usdt,
        config=CarryContractConfig(
            profile_name=strategy_profile,
            accepted_book_sources=(),
            exit_bp=1.0,
            early_exit_enabled=False,
            presettlement_exit_enabled=False,
            notional_multiplier=float(notional_multiplier),
            entry_leverage=float(entry_leverage),
            stop_loss_fraction=float(stop_loss_fraction),
            max_new_entries_per_cycle=1,
        ),
    )


def plan_carry_targets(
    *,
    decision: CarryDecision | None,
    standing_rows: Mapping[str, tuple[str, float, float]],
    trail_by_symbol: Mapping[str, float],
    demo: CarryDemoCycleConfig,
    sizing_equity_usdt: float | None,
    engine_account_health_error: str,
    previous_book: ParsedTargetBook | None,
    entry_blockers: Mapping[str, str] | None = None,
    mark_px_by_symbol: Mapping[str, float] | None = None,
    cycle_now_ms: int,
    strategy_profile: str,
) -> CarryPlanningOutput:
    """Compatibility wrapper over the shared CARRY lifecycle reducer."""

    book_source = str(strategy_profile).strip()
    if not book_source:
        raise ValueError("strategy_profile must name the resolved CARRY profile")
    health_error = str(engine_account_health_error)
    equity = float(sizing_equity_usdt or 0.0)
    if not health_error and equity <= 0.0:
        health_error = "sizing_equity_unavailable"
    prior = CarryContractPriorState(
        sizing_anchors=(
            ((decision.decision_ts_ms, equity),)
            if decision is not None and equity > 0.0
            else ()
        )
    )
    output = decide_carry(
        CarryDecisionInput(
            now_ms=int(cycle_now_ms),
            decision=decision,
            holdings=carry_holdings(
                dict(standing_rows),
                mark_px_by_symbol=mark_px_by_symbol,
            ),
            trail_by_symbol=tuple(sorted((str(key), float(value)) for key, value in trail_by_symbol.items())),
            entry_blockers=tuple(sorted((entry_blockers or {}).items())),
            account_health_error=health_error,
            equity_usdt=equity,
            previous_book=previous_book,
        ),
        prior,
        CarryContractConfig(
            profile_name=book_source,
            accepted_book_sources=(str(demo.strategy_profile),),
            exit_bp=1.0,
            early_exit_enabled=False,
            presettlement_exit_enabled=False,
            notional_multiplier=float(demo.notional_multiplier),
            entry_leverage=float(demo.entry_leverage),
            stop_loss_fraction=float(demo.declared_stop_loss_fraction),
            max_new_entries_per_cycle=int(demo.max_new_entries_per_cycle),
            capital_reference_usdt=float(demo.capital_reference_usdt),
        ),
    )
    summary = output.summary
    return CarryPlanningOutput(
        CarryTargetPlan(
            desired_book_size=summary.desired_book_size,
            desired_gross_weight=summary.desired_gross_weight,
            planned_exits=summary.planned_exits,
            planned_entries=summary.planned_entries,
            planned_resizes=summary.planned_resizes,
            resize_mark_missing_skips=summary.resize_mark_missing_skips,
            entry_cap_deferrals=summary.entry_cap_deferrals,
            entry_validity_expired_skips=summary.entry_validity_expired_skips,
            entry_dust_skips=summary.entry_dust_skips,
            engine_blocked_entries=summary.engine_blocked_entries,
            entry_blocked_reason=summary.entry_blocked_reason,
            book_written=False,
            target_book_object_path="",
        ),
        output.target_book_text,
    )


def publish_carry_plan(path: str | Path, output: CarryPlanningOutput) -> CarryTargetPlan:
    """Publish one pure planner result and attach the durable receipt."""

    if output.target_book_text is None:
        return output.plan
    publication = publish_target_book(Path(path), output.target_book_text)
    return dataclasses.replace(
        output.plan,
        book_written=True,
        target_book_object_path=str(publication.object_path),
    )


def _carry_target_plan(
    *,
    decision: CarryDecision | None,
    standing_rows: Mapping[str, tuple[str, float, float]],
    trail_by_symbol: dict[str, float],
    demo: CarryDemoCycleConfig,
    equity_usdt: float,
    engine_account_health_error: str,
    entry_blockers: Mapping[str, str] | None = None,
    mark_px_by_symbol: Mapping[str, float] | None = None,
    cycle_now_ms: int,
    target_book_path: str | Path,
    cycle_state: CarryCycleState,
    strategy_profile: str,
) -> CarryTargetPlan:
    """Transition sizing state, call the pure planner, then publish."""

    path_text = str(target_book_path).strip()
    if not path_text:
        raise ValueError("target_book_path must name the Rust target book")
    sizing_equity: float | None = None
    previous: ParsedTargetBook | None = None
    if decision is not None and not engine_account_health_error and equity_usdt > 0.0:
        sizing_equity = cycle_state.sizing_equity(
            decision_ts_ms=decision.decision_ts_ms,
            equity_usdt=equity_usdt,
        )
        if demo.capital_reference_usdt > 0.0:
            sizing_equity = min(sizing_equity, float(demo.capital_reference_usdt))
    elif decision is not None:
        try:
            previous = read_target_book(path_text)
        except (OSError, RuntimeError, ValueError):
            previous = None
    output = plan_carry_targets(
        decision=decision,
        standing_rows=standing_rows,
        trail_by_symbol=trail_by_symbol,
        demo=demo,
        sizing_equity_usdt=sizing_equity,
        engine_account_health_error=engine_account_health_error,
        previous_book=previous,
        entry_blockers=entry_blockers,
        mark_px_by_symbol=mark_px_by_symbol,
        cycle_now_ms=cycle_now_ms,
        strategy_profile=strategy_profile,
    )
    return publish_carry_plan(path_text, output)


def _empty_funding_events() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "symbol": pl.String,
            "funding_ts_ms": pl.Int64,
            "funding_rate": pl.Float64,
        }
    )


def _normalized_funding_events(frame: pl.DataFrame) -> pl.DataFrame:
    if frame.is_empty() or not {"symbol", "funding_ts_ms", "funding_rate"} <= set(frame.columns):
        return _empty_funding_events()
    normalized = frame.select(
        pl.col("symbol").cast(pl.String),
        pl.col("funding_ts_ms").cast(pl.Int64),
        pl.col("funding_rate").cast(pl.Float64),
    )
    invalid = normalized.filter(pl.col("funding_rate").is_null() | ~pl.col("funding_rate").is_finite().fill_null(False))
    if invalid.height:
        raise CarrySleeveError(f"carry funding events contain {invalid.height} null or non-finite rates")
    return normalized.unique(subset=["symbol", "funding_ts_ms"], keep="last")


def _refresh_carry_funding_cache(
    root: Path,
    market: Any,
    symbols: list[str],
    *,
    now_ms: int,
    replay_days: int,
    state: CarryCycleState,
    workers: int = 1,
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Maintain the on-disk settled-print cache and return the full frame.

    Incremental: each symbol is fetched from one hour before its newest cached
    print (settlements land on hour boundaries, so the overlap only re-observes
    the boundary print, which the strict ``>`` filter drops), or cold from the
    replay window plus the as-of join lookback. Sweeps are throttled to one per
    wall hour, since prints only change on hour boundaries.

    Per-symbol failures are loud but NON-fatal: the view-health guards decide
    whether the resulting frame is safe to decide on. A sweep in which EVERY
    symbol failed does not count as swept, so the next cycle retries.
    """

    cached = _normalized_funding_events(read_dataset(root, CARRY_FUNDING_DATASET))
    stats: dict[str, Any] = {
        "funding_swept": False,
        "funding_rows_appended": 0,
        "funding_fetch_failures": 0,
        "funding_failed_symbols": "",
    }
    current_hour_ts = int(now_ms) - int(now_ms) % HOUR_MS
    if state.funding_swept_hour_ts == current_hour_ts:
        return cached, stats
    last_by_symbol: dict[str, int] = {}
    if not cached.is_empty():
        last_by_symbol = {
            str(row["symbol"]): int(row["last_ts"])
            for row in cached.group_by("symbol")
            .agg(pl.col("funding_ts_ms").max().alias("last_ts"))
            .iter_rows(named=True)
        }
    cold_start_ms = int(now_ms) - (int(replay_days) + FUNDING_LOOKBACK_DAYS) * DAY_MS

    def _fetch_symbol(symbol: str) -> list[dict[str, Any]] | None:
        last_ts = last_by_symbol.get(symbol)
        fetch_start = (last_ts - HOUR_MS) if last_ts is not None else cold_start_ms
        for attempt in range(2):
            try:
                return market.get_funding_history(symbol, fetch_start, int(now_ms))
            except Exception as exc:  # noqa: BLE001 - loud, retried once, never cycle-fatal
                if attempt == 0:
                    _logger.warning("carry funding fetch failed for %s (retrying once): %s", symbol, exc)
                else:
                    _logger.error(
                        "carry funding fetch failed for %s after retry; the symbol keeps "
                        "its cached prints this sweep: %s",
                        symbol,
                        exc,
                    )
        return None

    # The venue publishes settled funding only over REST, so the hourly sweep
    # is a bounded REST burst; a small worker pool shortens it. One shared
    # client is safe (the WS bootstrap pool shares one the same way) and the
    # results fold in `symbols` order so the output is order-deterministic.
    rows_by_symbol: dict[str, list[dict[str, Any]] | None] = {}
    pool_workers = max(1, min(int(workers), 8, len(symbols) or 1))
    if pool_workers > 1:
        with ThreadPoolExecutor(max_workers=pool_workers) as pool:
            for symbol, rows in zip(symbols, pool.map(_fetch_symbol, symbols)):
                rows_by_symbol[symbol] = rows
    else:
        for symbol in symbols:
            rows_by_symbol[symbol] = _fetch_symbol(symbol)

    fresh_rows: list[dict[str, Any]] = []
    failed_symbols: list[str] = []
    for symbol in symbols:
        rows = rows_by_symbol[symbol]
        if rows is None:
            failed_symbols.append(symbol)
            continue
        last_ts = last_by_symbol.get(symbol)
        floor_ts = last_ts if last_ts is not None else cold_start_ms - 1
        for row in rows:
            try:
                funding_ts = int(row["fundingRateTimestamp"])
                funding_rate = float(row["fundingRate"])
            except (KeyError, TypeError, ValueError):
                _logger.warning("carry funding row for %s is malformed: %r", symbol, row)
                continue
            if not math.isfinite(funding_rate):
                _logger.warning("carry funding row for %s has a non-finite rate", symbol)
                continue
            if funding_ts > floor_ts:
                fresh_rows.append(
                    {
                        "symbol": symbol,
                        "funding_ts_ms": funding_ts,
                        "funding_rate": funding_rate,
                    }
                )
    fresh = (
        _normalized_funding_events(pl.DataFrame(fresh_rows, infer_schema_length=None))
        if fresh_rows
        else _empty_funding_events()
    )
    if not fresh.is_empty():
        write_dataset(fresh, root, CARRY_FUNDING_DATASET, partition_by=("symbol",))
    if len(failed_symbols) < len(symbols) or not symbols:
        state.funding_swept_hour_ts = current_hour_ts
        stats["funding_swept"] = True
    stats["funding_rows_appended"] = fresh.height
    stats["funding_fetch_failures"] = len(failed_symbols)
    stats["funding_failed_symbols"] = ",".join(sorted(failed_symbols))
    combined = (
        pl.concat([cached, fresh], how="vertical").unique(subset=["symbol", "funding_ts_ms"], keep="last")
        if not fresh.is_empty()
        else cached
    )
    return combined, stats


def _candidate_filtered_universe(
    top_symbols: list[str],
    *,
    candidate_universe_file: str,
    realm: VenueRealm,
    standing_symbols: set[str],
) -> tuple[list[str], int, dict[str, str]]:
    """Intersect the turnover universe with the frozen candidate epoch.

    Standing symbols are added back AFTER the intersection: a held name must
    never lose market data (its exit still needs the replay), even when it has
    dropped out of the frozen candidate population.
    """

    skipped = 0
    kept = list(top_symbols)
    evidence = {
        "candidate_universe_artifact_sha256": "",
        "candidate_universe_file_sha256": "",
    }
    if candidate_universe_file:
        frozen = load_candidate_universe(candidate_universe_file, realm=realm)
        evidence = {
            "candidate_universe_artifact_sha256": frozen.artifact_sha256,
            "candidate_universe_file_sha256": frozen.file_sha256,
        }
        # CARRY's profile is checked here but does not narrow membership: the
        # sleeve trades the whole frozen strategy instrument set. Binding to
        # the profile subset would change the strategy's tradable population.
        require_profile_binding(
            frozen,
            profile="carry",
            current_inputs=carry_profile_universe_inputs(),
        )
        allowed = set(frozen.strategy_instruments)
        kept = [symbol for symbol in top_symbols if symbol in allowed]
        skipped = len(top_symbols) - len(kept)
    return sorted(set(kept) | set(standing_symbols)), skipped, evidence


def _build_carry_demo_market_data(
    *,
    root: Path,
    config: ResearchConfig,
    demo: CarryDemoCycleConfig,
    market: Any,
    now_ms: int,
    standing_symbols: set[str],
    state: CarryCycleState,
    kline_store: Any | None = None,
    ticker_cache: Any | None = None,
    state_cache_stale_seconds: float = 120.0,
) -> tuple[pl.DataFrame, pl.DataFrame, dict[str, Any], dict[str, float]]:
    """Carry data path: WS kline store and ticker cache first, REST fallback.

    Settled funding history has no stream on the venue, so the hourly funding
    sweep stays REST by necessity.
    """

    try:
        ticker_rows, ticker_source = _resolve_ticker_snapshot(
            market,
            ticker_cache=ticker_cache,
            state_cache_stale_seconds=state_cache_stale_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - degrade to standing symbols, as the REST-only path did
        _logger.warning("carry ticker snapshot failed; universe degrades to standing symbols: %s", exc)
        ticker_rows, ticker_source = [], "unavailable"
    top_symbols = rank_top_turnover_symbols(ticker_rows, top_n=CARRY_FETCH_UNIVERSE_TOP_N)
    fetch_symbols, candidate_skipped, candidate_evidence = _candidate_filtered_universe(
        top_symbols,
        candidate_universe_file=demo.candidate_universe_file,
        realm=candidate_universe_realm(demo.execution_environment),
        standing_symbols=standing_symbols,
    )
    if not fetch_symbols:
        raise CarrySleeveError("carry fetch universe is empty (ticker fetch failed and nothing standing)")
    try:
        launch_times = _launch_time_ms_by_symbol(_demo_instruments(market, cache_root=root, now_ms=now_ms))
    except Exception as exc:  # noqa: BLE001 - listing ages only avoid head refetches
        _logger.warning("carry instruments fetch failed; head-completeness checks degrade: %s", exc)
        launch_times = {}
    start_ms, window_end_open_ms = _kline_window(now_ms, lookback_days=demo.replay_days)
    # The shared reader's window is INCLUSIVE over bar OPENS and its end must
    # be the newest CLOSED bar's open — the same convention LONG passes. A +1h
    # here shifts the whole window one bar forward, which makes the WS store's
    # coverage probe unfulfillable (the store bootstraps, flushes, and never
    # serves a single cycle, at kline_store_rows=0) and asks REST for the
    # in-progress bar. Close-keyed (see _carry_venue_view), the newest closed
    # bar still IS the current day's 00:00 decision bar during the 00:xx hour.
    klines, kline_stats = _download_recent_1h_klines(
        fetch_symbols,
        start_ms=start_ms,
        end_ms=window_end_open_ms,
        launch_time_ms_by_symbol=launch_times,
        config=config,
        workers=demo.workers,
        market_client=market,
        cache_root=root,
        kline_store=kline_store,
    )
    funding, funding_stats = _refresh_carry_funding_cache(
        root,
        market,
        fetch_symbols,
        now_ms=now_ms,
        replay_days=demo.replay_days,
        state=state,
        workers=demo.workers,
    )
    kline_source = (
        "ws_store"
        if int(kline_stats.get("store_rows", 0)) > 0 and int(kline_stats.get("fetched_rows", 0)) == 0
        else "rest"
    )
    stats: dict[str, Any] = {
        "data_source": kline_source,
        "ticker_source": ticker_source,
        "universe_fetched": len(fetch_symbols),
        "candidate_skipped_symbols": candidate_skipped,
        **candidate_evidence,
        "kline_cache_rows": int(kline_stats.get("cache_rows", 0)),
        "kline_fetched_rows": int(kline_stats.get("fetched_rows", 0)),
        "kline_output_rows": int(kline_stats.get("output_rows", 0)),
        "kline_fetch_symbols": int(kline_stats.get("fetch_symbols", 0)),
        "kline_store_rows": int(kline_stats.get("store_rows", 0)),
        "funding_cache_rows": funding.height,
        "funding_max_ts_ms": (coerce_int(funding.get_column("funding_ts_ms").max()) if not funding.is_empty() else 0),
        **funding_stats,
    }
    return klines, funding, stats, carry_mark_prices(ticker_rows)


def _last_successful_decision_ts_ms(root: Path, *, cycles_dataset: str) -> int | None:
    """Newest decision day this root ever decided without error.

    Advisory read for the ``decision_stale`` alarm after a restart; any read
    problem degrades to "unknown" rather than failing the cycle.
    """

    try:
        frame = read_dataset_columns(
            root,
            cycles_dataset,
            columns=["decision_ts_ms", "decision_error"],
        )
    except Exception as exc:  # noqa: BLE001 - staleness telemetry must not break the cycle
        _logger.warning("carry cycles read-back failed: %s", exc)
        return None
    if frame.is_empty() or "decision_ts_ms" not in frame.columns:
        return None
    if "decision_error" in frame.columns:
        frame = frame.filter(
            pl.col("decision_error").is_null()
            | (pl.col("decision_error").cast(pl.String, strict=False).fill_null("") == "")
        )
    if frame.is_empty():
        return None
    newest = frame.get_column("decision_ts_ms").max()
    return coerce_int(newest) if newest is not None else None


def _carry_reducer_now_ms(
    *,
    cycle_started_ms: int,
    injected_now: bool,
    presettlement: tuple[CarryContractPresettlementObservation, ...],
) -> int:
    """Capture the production decision clock after every typed input read."""

    return carry_reducer_clock_ms(
        cycle_started_ms=cycle_started_ms,
        after_inputs_ms=(cycle_started_ms if injected_now else _utc_now_ms()),
        presettlement=presettlement,
    )


def run_carry_demo_cycle(
    data_root: str | Path,
    *,
    effective_config: CarryEffectiveConfig,
    market_client: Any | None = None,
    now_ms: int | None = None,
    kline_store: Any | None = None,
    ticker_cache: Any | None = None,
    state_cache_stale_seconds: float = 120.0,
    cycle_state: CarryCycleState | None = None,
    cycle_kind: str = "timer",
    freeze_ahead_decision_ts_ms: int | None = None,
) -> PublishedTargetCyclePayload:
    """Plan one CARRY cycle and publish an immutable Rust target book.

    Every cycle: rebuild the venue view, replay the registered rule to today's
    desired book, read the Rust engine heartbeat, and publish the absolute
    position request. Failure policy is HOLD-STEADY: a data-build or decision
    failure leaves the last book untouched and never flattens, while
    ``decision_error``/``decision_stale`` make the outage loud.

    ``kline_store`` serves the cycle's close-keyed 1h bars from the daemon's
    WS plane (identical bar content to the REST window contract, pushed
    instead of pulled); ``ticker_cache`` serves the turnover-ranked fetch
    universe. REST remains the fallback for either when the stream is cold or
    stale, and the sole source for settled funding history, which no stream
    carries.

    ``cycle_kind`` is the daemon's wake reason: a ``market_boundary`` wake with
    an already-frozen decision skips the data build entirely and goes straight
    to plan-and-publish, which is what turns the daily boundary from a
    multi-second pass into tens of milliseconds. ``freeze_ahead_decision_ts_ms``
    asks a pre-deadline cycle to compute and freeze the upcoming day's book
    from its own build (:func:`_freeze_decision_ahead`).
    """

    demo = effective_config.cycle
    _validate_carry_demo_config(demo)
    environment = execution_environment(demo.execution_environment).value
    supplied_root = Path(data_root).expanduser().resolve()
    root = effective_config.data_root
    if supplied_root != root:
        raise ValueError("CARRY cycle data root disagrees with its effective configuration")
    market_projection = ResearchConfig(
        exchange=effective_config.exchange,
        data_root=root,
    )
    engine_book_path = effective_config.target_book_path
    root.mkdir(parents=True, exist_ok=True)
    cycle_now_ms = int(now_ms if now_ms is not None else _utc_now_ms())
    decision_ts_ms = carry_decision_ts_ms(cycle_now_ms)
    cycle_id = f"carry-target-{CARRY_STRATEGY_ID}-{cycle_now_ms}"
    cycles_dataset = effective_config.cycles_dataset
    state = cycle_state if cycle_state is not None else CarryCycleState()

    with exclusive_file_lock(root / ".locks" / "carry_demo_cycle.lock", stale_seconds=900):
        state.bind_sizing_anchors(effective_config.sizing_anchor_path)
        engine_reading: EngineAccountReading | None = None
        try:
            engine_reading = require_recent_engine_account(
                environment,
                max_age_ns=TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
                now_ns=cycle_now_ms * 1_000_000,
                path=effective_config.engine_heartbeat_path,
                expected_account_user_id=effective_config.expected_account_user_id,
            )
            equity_usdt = float(engine_reading.equity_usdt)
            engine_account_health_error = ""
        except (OSError, RuntimeError, ValueError) as exc:
            equity_usdt = 0.0
            engine_account_health_error = str(exc)
            _logger.warning("CARRY engine account reading unavailable; book held: %s", exc)
        standing_rows = engine_reading.holdings_for_strategy(ENGINE_CARRY_SLEEVE) if engine_reading is not None else {}
        standing_symbols = set(standing_rows)

        decision: CarryDecision | None = None
        decision_error: str | None = None
        decision_frozen = False
        strategy_profile = effective_config.profile
        rule = effective_config.rule
        trail_by_symbol: dict[str, float] = {}
        build_stats: dict[str, Any] = {}
        universe_eligible = 0
        freeze_ahead_frozen = False
        drop_exit_frozen = False
        built_klines: pl.DataFrame | None = None
        built_funding: pl.DataFrame | None = None
        current_mark_prices: dict[str, float] = {}
        market: Any | None = market_client
        whale_events: pl.DataFrame | None = None
        # A deadline wake exists to publish the frozen day the instant it
        # becomes actionable; rebuilding caches first spends seconds on data
        # the frozen decision cannot read. Timer cycles keep the build (it IS
        # the cache maintenance: WS-store flush and the hourly funding sweep),
        # and an unfrozen deadline falls through to the full path below.
        # Engine-change wakes react to fills and refusals with the same frozen
        # decision, so they skip
        # the build too UNLESS this cycle owes maintenance: the hourly
        # funding sweep is due, or the daemon asked it to freeze the next
        # day ahead of the boundary. Without that carve-out, a stream of
        # engine updates would starve both.
        skip_build = cycle_kind == "market_boundary" or (
            cycle_kind == "engine_change"
            and freeze_ahead_decision_ts_ms is None
            and state.funding_swept_hour_ts == cycle_now_ms - cycle_now_ms % HOUR_MS
        )
        prewarmed = state.frozen_decision(decision_ts_ms) if skip_build else None
        data_build_skipped = prewarmed is not None
        if prewarmed is not None:
            decision, trail_by_symbol, universe_eligible, frozen_input_evidence = prewarmed
            decision_frozen = True
            build_stats = {
                "data_source": "build_skipped",
                "ticker_source": "skipped",
                **frozen_input_evidence,
            }
        else:
            try:
                if market is None:
                    market = BybitMarketData(
                        category=effective_config.exchange.category,
                        testnet=effective_config.exchange.testnet,
                    )
                klines, funding, build_stats, current_mark_prices = _build_carry_demo_market_data(
                    root=root,
                    config=market_projection,
                    demo=demo,
                    market=market,
                    now_ms=cycle_now_ms,
                    standing_symbols=standing_symbols,
                    state=state,
                    kline_store=kline_store,
                    ticker_cache=ticker_cache,
                    state_cache_stale_seconds=state_cache_stale_seconds,
                )
                built_klines, built_funding = klines, funding
                if rule.whale_cut is not None:
                    # The whale halving reads Binance EODs; refresh the tiny
                    # cache for exactly the symbols this build fetched. Never
                    # raises — a dead feed fails open per the registered rule.
                    whale_symbols = sorted(set(klines.get_column("symbol").to_list())) if not klines.is_empty() else []
                    whale_events, whale_stats = _refresh_carry_whale_cache(
                        root, whale_symbols, now_ms=cycle_now_ms, state=state
                    )
                    build_stats.update(whale_stats)
                # Asked before the panel is built: a frozen decision cannot read
                # the panel, and nothing else does.
                # ``_build_carry_demo_market_data`` stays above so the hourly
                # funding sweep and the kline caches are still maintained.
                frozen = state.frozen_decision(decision_ts_ms)
                if frozen is not None:
                    (
                        decision,
                        trail_by_symbol,
                        universe_eligible,
                        frozen_input_evidence,
                    ) = frozen
                    build_stats.update(frozen_input_evidence)
                    decision_frozen = True
                else:
                    window_start_ms = decision_ts_ms - demo.replay_days * DAY_MS
                    view = _carry_venue_view(
                        klines,
                        funding,
                        window_start_ms=window_start_ms,
                        max_bar_ts_ms=decision_ts_ms,
                        whale_events=whale_events,
                    )
                    if not view.is_empty():
                        # A cold-started cache begins mid-day, which the engine's
                        # daily-grid phase guard rightly refuses, so trim to the
                        # first 00:00 UTC key. A no-op once the cache spans the
                        # window.
                        first_ts = int(view.get_column("bar_ts_ms").min())  # type: ignore[arg-type]
                        if first_ts % DAY_MS != 0:
                            aligned_start = ((first_ts // DAY_MS) + 1) * DAY_MS
                            view = view.filter(pl.col("bar_ts_ms") >= aligned_start)
                    universe_eligible = int(view.get_column("symbol").n_unique()) if not view.is_empty() else 0
                    _validate_carry_view_health(
                        view,
                        decision_ts_ms=decision_ts_ms,
                        standing_symbols=standing_symbols,
                    )
                    trail_by_symbol = _trailing_settled_funding(funding, decision_ts_ms=decision_ts_ms)
                    decision = decide_book(view, rule, decision_ts_ms)
                    state.freeze_decision(
                        decision_ts_ms=decision_ts_ms,
                        decision=decision,
                        trail_by_symbol=trail_by_symbol,
                        universe_eligible=universe_eligible,
                        input_evidence={
                            "candidate_universe_artifact_sha256": str(
                                build_stats.get("candidate_universe_artifact_sha256", "")
                            ),
                            "candidate_universe_file_sha256": str(
                                build_stats.get("candidate_universe_file_sha256", "")
                            ),
                        },
                    )
            except Exception as exc:  # noqa: BLE001 - hold-steady: a data hiccup must never flatten
                decision_error = f"{type(exc).__name__}: {exc}"[:500]
                _logger.exception("carry decision build failed; holding the standing book")

        if freeze_ahead_decision_ts_ms is not None and built_klines is not None and built_funding is not None:
            freeze_ahead_frozen = _freeze_decision_ahead(
                state=state,
                rule=rule,
                klines=built_klines,
                funding=built_funding,
                build_stats=build_stats,
                ahead_ts_ms=int(freeze_ahead_decision_ts_ms),
                current_decision_ts_ms=decision_ts_ms,
                replay_days=demo.replay_days,
                standing_symbols=standing_symbols,
                whale_events=whale_events,
            )
        if built_klines is not None and built_funding is not None:
            # The drop exit, part of the strategy's own exit clock: the
            # upcoming day's decision reads only rows already public minutes
            # after midnight, so freeze it at the first clean post-midnight
            # build instead of inside the pre-deadline window. The zeroed
            # names' exits then publish ~00:02 while entries still wait for
            # the 00:20 clock. Same function, same gates, same refusal
            # semantics as the deadline freeze: a repair-pending build pins
            # nothing and the day falls back to the settled-print clock.
            drop_day_ts = (cycle_now_ms // DAY_MS) * DAY_MS
            if drop_day_ts > decision_ts_ms and state.frozen_decision(drop_day_ts) is None:
                drop_exit_frozen = _freeze_decision_ahead(
                    state=state,
                    rule=rule,
                    klines=built_klines,
                    funding=built_funding,
                    build_stats=build_stats,
                    ahead_ts_ms=drop_day_ts,
                    current_decision_ts_ms=decision_ts_ms,
                    replay_days=demo.replay_days,
                    standing_symbols=standing_symbols,
                    whale_events=whale_events,
                )
        sizing_anchor_requests: tuple[CarrySizingAnchorRequest, ...] = ()
        if (
            freeze_ahead_decision_ts_ms is not None
            and state.frozen_decision(int(freeze_ahead_decision_ts_ms)) is not None
            and not engine_account_health_error
            and equity_usdt > 0.0
        ):
            # Anchor tomorrow to the fresh engine account mark used to freeze
            # it, so the boundary pass cannot introduce P&L feedback.
            sizing_anchor_requests = (
                CarrySizingAnchorRequest(
                    decision_ts_ms=int(freeze_ahead_decision_ts_ms),
                    equity_usdt=float(equity_usdt),
                ),
            )

        if decision is not None:
            state.last_successful_decision_ts_ms = max(
                decision.decision_ts_ms, state.last_successful_decision_ts_ms or 0
            )
            decision_stale = False
        else:
            last_ok = state.last_successful_decision_ts_ms
            if last_ok is None:
                last_ok = _last_successful_decision_ts_ms(root, cycles_dataset=cycles_dataset)
                if last_ok is not None:
                    state.last_successful_decision_ts_ms = last_ok
            decision_stale = last_ok is None or (cycle_now_ms - last_ok) > DECISION_STALE_MS
            if decision_stale:
                _logger.error(
                    "carry decision is STALE: newest successful decision %s, now %s",
                    last_ok,
                    cycle_now_ms,
                )

        # Refresh the account immediately before any pre-settlement handoff.
        # A full market-data build can take long enough that the cycle-start
        # quantity is no longer the position CARRY is actually abandoning.
        try:
            engine_reading = require_recent_engine_account(
                environment,
                max_age_ns=TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
                now_ns=time.time_ns(),
                path=effective_config.engine_heartbeat_path,
                expected_account_user_id=effective_config.expected_account_user_id,
            )
            equity_usdt = float(engine_reading.equity_usdt)
            engine_account_health_error = ""
            standing_rows = engine_reading.holdings_for_strategy(ENGINE_CARRY_SLEEVE)
            standing_symbols = set(standing_rows)
        except (OSError, RuntimeError, ValueError) as exc:
            engine_reading = None
            equity_usdt = 0.0
            engine_account_health_error = str(exc)
            standing_rows = {}
            standing_symbols = set()
            _logger.warning(
                "CARRY commit-time engine account reading unavailable; additions and resizes blocked: %s",
                exc,
            )

        missing_mark_symbols = standing_symbols - set(current_mark_prices)
        if missing_mark_symbols and market is not None:
            try:
                ticker_rows, _ticker_source = _resolve_ticker_snapshot(
                    market,
                    ticker_cache=ticker_cache,
                    state_cache_stale_seconds=state_cache_stale_seconds,
                )
                current_mark_prices.update(
                    carry_mark_prices(ticker_rows, symbols=missing_mark_symbols)
                )
            except Exception as exc:  # noqa: BLE001 - Rust still classifies from its own market
                _logger.warning(
                    "CARRY holding marks unavailable; resize receipt is unclassified: %s",
                    exc,
                )

        presettlement_observations: tuple[CarryContractPresettlementObservation, ...] = ()
        presettle_error = ""
        if decision is not None and demo.early_exit_enabled and strategy_profile.presettle_exit and decision.weights:
            tickers: dict[str, CarryPresettlementTicker] = {}
            # Every settlement sits on an hour boundary; fetch only when one
            # is close enough for a fire to be possible.
            presettle_fetch_ms = cycle_now_ms if now_ms is not None else _utc_now_ms()
            to_boundary_ms = HOUR_MS - (presettle_fetch_ms % HOUR_MS)
            if to_boundary_ms <= _PRESETTLE_WINDOW_MS + _PRESETTLE_FETCH_SLACK_MS:
                tickers, presettle_error = _fetch_presettle_tickers(sorted(decision.weights))
            # The event owns the time at which the complete venue sample is
            # available, not the cycle-start time before account and market
            # reads. A slow fetch that crosses settlement must not create an
            # event for a print that has already paid.
            presettle_observed_ms = cycle_now_ms if now_ms is not None else _utc_now_ms()
            presettle_inputs = build_carry_presettlement_inputs(
                tickers=tickers,
                observed_ts_ms=presettle_observed_ms,
                carry_holdings=standing_rows,
            )
            presettlement_observations = tuple(
                carry_presettlement_observation(
                    symbol=row.ticker.symbol,
                    observed_ts_ms=row.observed_ts_ms,
                    settlement_ts_ms=row.ticker.settlement_ts_ms,
                    running_rate=row.ticker.running_rate,
                    mark_px=row.ticker.mark_px,
                    carry_side=row.carry_side,
                    carry_qty=row.carry_qty,
                    carry_avg_entry_px=row.carry_avg_entry_px,
                )
                for row in presettle_inputs
            )
            if presettle_error:
                _logger.warning(
                    "pre-settle ticker read failed; settled-print clock stands: %s",
                    presettle_error,
                )

        durable_events = in_scope_presettlement_events(
            load_durable_presettlement_events(effective_config.presettlement_event_path),
            environment=environment,
            source_profile=strategy_profile.profile_name,
            source_config_id=rule.config_id,
        )
        upcoming_frozen = (
            state.frozen_decision(decision.decision_ts_ms + DAY_MS)
            if decision is not None
            else None
        )
        previous_book: ParsedTargetBook | None = None
        if decision is not None and engine_account_health_error:
            try:
                previous_book = read_target_book(effective_config.target_book_path)
            except (OSError, RuntimeError, ValueError):
                previous_book = None
        reducer_now_ms = _carry_reducer_now_ms(
            cycle_started_ms=cycle_now_ms,
            injected_now=now_ms is not None,
            presettlement=presettlement_observations,
        )
        reducer_output = decide_carry(
            CarryDecisionInput(
                now_ms=reducer_now_ms,
                decision=decision,
                upcoming_decision=upcoming_frozen[0] if upcoming_frozen is not None else None,
                holdings=carry_holdings(
                    standing_rows,
                    mark_px_by_symbol=current_mark_prices,
                ),
                trail_by_symbol=tuple(sorted(trail_by_symbol.items())),
                entry_blockers=tuple(
                    sorted(
                        (
                            engine_reading.entry_blockers_for_strategy(ENGINE_CARRY_SLEEVE)
                            if engine_reading is not None
                            else {}
                        ).items()
                    )
                ),
                account_health_error=engine_account_health_error,
                equity_usdt=equity_usdt,
                sizing_anchor_requests=sizing_anchor_requests,
                settled_funding=(
                    settled_funding_observations(
                        built_funding,
                        decision_ts_ms=decision.decision_ts_ms,
                        now_ms=reducer_now_ms,
                    )
                    if decision is not None
                    else ()
                ),
                presettlement=presettlement_observations,
                durable_presettlement_fires=tuple(
                    durable_presettlement_fire(event) for event in durable_events
                ),
                previous_book=previous_book,
            ),
            state.reducer_prior(exit_state_path=effective_config.early_exit_state_path),
            carry_strategy_config(
                profile_name=strategy_profile.profile_name,
                compatibility_source=demo.strategy_profile,
                rule=rule,
                early_exit_enabled=demo.early_exit_enabled,
                presettlement_exit_enabled=strategy_profile.presettle_exit,
                notional_multiplier=demo.notional_multiplier,
                entry_leverage=demo.entry_leverage,
                stop_loss_fraction=demo.declared_stop_loss_fraction,
                max_new_entries_per_cycle=demo.max_new_entries_per_cycle,
                capital_reference_usdt=demo.capital_reference_usdt,
            ),
        )
        commit = commit_carry_output(
            reducer_output,
            state=state,
            decision_ts_ms=(decision.decision_ts_ms if decision is not None else decision_ts_ms),
            exit_state_path=effective_config.early_exit_state_path,
            presettlement_event_path=effective_config.presettlement_event_path,
            target_book_path=effective_config.target_book_path,
            environment=environment,
            source_profile=strategy_profile.profile_name,
            source_config_id=rule.config_id,
        )
        early_exit_fires = list(reducer_output.settled_exit_fires)
        presettle_fire_details = list(commit.publication_events)
        presettle_fires = [event.symbol for event in presettle_fire_details]
        dropped_now = list(reducer_output.drop_exit_fires)
        drop_exit_masked = len(dropped_now)
        drop_exit_fires: list[str] = []
        if dropped_now:
            if frozenset(dropped_now) != state.drop_exits_logged:
                _logger.info(
                    "drop exit fired: %s (the upcoming decision zeroes them; selling ahead of the 00:20 clock)",
                    ",".join(dropped_now),
                )
                drop_exit_fires = dropped_now
            state.drop_exits_logged = frozenset(dropped_now)
        if early_exit_fires:
            _logger.info(
                "early exit fired: %s (settled print at/above %.1f bp)",
                ",".join(early_exit_fires),
                -rule.exit_bp,
            )
        if presettle_fires:
            _logger.info(
                "pre-settle exit fired: %s (running rate at/above %.1f bp before the print pays)",
                ",".join(presettle_fires),
                -rule.exit_bp,
            )
        summary = reducer_output.summary
        plan = CarryTargetPlan(
            desired_book_size=summary.desired_book_size,
            desired_gross_weight=summary.desired_gross_weight,
            planned_exits=summary.planned_exits,
            planned_entries=summary.planned_entries,
            planned_resizes=summary.planned_resizes,
            resize_mark_missing_skips=summary.resize_mark_missing_skips,
            entry_cap_deferrals=summary.entry_cap_deferrals,
            entry_validity_expired_skips=summary.entry_validity_expired_skips,
            entry_dust_skips=summary.entry_dust_skips,
            engine_blocked_entries=summary.engine_blocked_entries,
            entry_blocked_reason=summary.entry_blocked_reason,
            book_written=commit.target_book is not None,
            target_book_object_path=(str(commit.target_book.object_path) if commit.target_book else ""),
        )
        payload: dict[str, Any] = {
            "cycle_id": cycle_id,
            "ts_ms": cycle_now_ms,
            "sleeve": "carry",
            "mode": f"{environment}_rust_target_book",
            "environment": environment,
            "strategy_id": CARRY_STRATEGY_ID,
            "strategy_profile": strategy_profile.profile_name,
            "effective_config_provenance": json.dumps(
                effective_config.provenance_by_field(),
                sort_keys=True,
                separators=(",", ":"),
            ),
            "operational_profile_sha256": demo.operational_profile_sha256,
            "replay_days": demo.replay_days,
            "notional_multiplier": demo.notional_multiplier,
            "entry_leverage": demo.entry_leverage,
            "declared_stop_loss_fraction": demo.declared_stop_loss_fraction,
            "max_new_entries_per_cycle": demo.max_new_entries_per_cycle,
            "decision_ts_ms": decision_ts_ms,
            "decision_error": decision_error,
            "decision_stale": decision_stale,
            "decision_frozen": decision_frozen,
            # Deadline-latency provenance: whether this cycle
            # skipped the data build (deadline wake on a frozen day), whether
            # the decision it served was frozen ahead of the deadline, and
            # whether this cycle itself froze the upcoming day.
            "data_build_skipped": data_build_skipped,
            "decision_frozen_ahead": bool(decision_frozen and state.frozen_ahead_bar_ts_ms == decision_ts_ms),
            "freeze_ahead_frozen": freeze_ahead_frozen,
            "decision_universe_size": decision.universe_size if decision is not None else 0,
            "decision_replay_days": decision.replay_days if decision is not None else 0,
            "desired_book_size": plan.desired_book_size,
            "desired_gross_weight": plan.desired_gross_weight,
            "universe_fetched": int(build_stats.get("universe_fetched", 0)),
            "universe_eligible": universe_eligible,
            "candidate_skipped_symbols": int(build_stats.get("candidate_skipped_symbols", 0)),
            "candidate_universe_artifact_sha256": str(build_stats.get("candidate_universe_artifact_sha256", "")),
            "candidate_universe_file_sha256": str(build_stats.get("candidate_universe_file_sha256", "")),
            # Whale-feed receipt: how many Binance EOD symbol-days were
            # fetched or missing and how many known values fed the view.
            # Absent keys mean the selected rule has no whale leg.
            "whale_pairs_fetched": build_stats.get("whale_pairs_fetched"),
            "whale_pairs_missing": build_stats.get("whale_pairs_missing"),
            "whale_event_rows": build_stats.get("whale_event_rows"),
            "whale_error": build_stats.get("whale_error"),
            # Early-exit receipt: names fired THIS cycle, and the standing
            # mask for the current decision day.
            "early_exit_enabled": demo.early_exit_enabled,
            "early_exit_fired": early_exit_fires,
            "early_exit_masked": len(state.early_exits or {}),
            "presettle_exit_enabled": bool(demo.early_exit_enabled and strategy_profile.presettle_exit),
            "presettle_fired": presettle_fires,
            "presettlement_event_ids": [event.event_id for event in presettle_fire_details],
            "presettlement_event_tape": str(effective_config.presettlement_event_path),
            "presettle_error": presettle_error,
            # Drop-exit receipt: names the upcoming decision zeroed and this
            # cycle announced, plus whether this cycle froze that upcoming
            # book early. Part of the exit clock; no dial.
            "drop_exit_fired": drop_exit_fires,
            "drop_exit_masked": drop_exit_masked,
            "drop_exit_froze_ahead": drop_exit_frozen,
            "open_positions": len(standing_symbols),
            "standing_symbols": len(standing_symbols),
            "planned_exits": plan.planned_exits,
            "planned_entries": plan.planned_entries,
            "planned_resizes": plan.planned_resizes,
            "resize_mark_missing_skips": plan.resize_mark_missing_skips,
            "entry_cap_deferrals": plan.entry_cap_deferrals,
            "entry_validity_expired_skips": plan.entry_validity_expired_skips,
            "entry_dust_skips": plan.entry_dust_skips,
            "engine_blocked_entries": plan.engine_blocked_entries,
            "entry_blocked_reason": plan.entry_blocked_reason,
            "exit_book_removals": plan.planned_exits,
            "entry_book_additions": plan.planned_entries,
            "book_resizes": plan.planned_resizes,
            "book_written": plan.book_written,
            "target_book_path": str(engine_book_path),
            # Null, not 0.0, when engine health is unavailable: a literal zero
            # reads as a -100% equity spike in every cycles-derived curve.
            "equity_usdt": equity_usdt if not engine_account_health_error else None,
            # The mark above is descriptive; this is what the day's targets
            # were sized against and the only one that explains a notional.
            "sizing_equity_usdt": state.sizing_equity_usdt,
            "sizing_equity_decision_ts_ms": state.sizing_equity_decision_ts_ms,
            "engine_account_health_error": engine_account_health_error,
            "entry_risk_health_ok": not engine_account_health_error and equity_usdt > 0.0,
            "kline_cache_rows": int(build_stats.get("kline_cache_rows", 0)),
            "kline_fetched_rows": int(build_stats.get("kline_fetched_rows", 0)),
            "kline_output_rows": int(build_stats.get("kline_output_rows", 0)),
            "kline_fetch_symbols": int(build_stats.get("kline_fetch_symbols", 0)),
            "kline_store_rows": int(build_stats.get("kline_store_rows", 0)),
            "funding_swept": bool(build_stats.get("funding_swept", False)),
            "funding_rows_appended": int(build_stats.get("funding_rows_appended", 0)),
            "funding_fetch_failures": int(build_stats.get("funding_fetch_failures", 0)),
            "funding_failed_symbols": str(build_stats.get("funding_failed_symbols", "")),
            "funding_cache_rows": int(build_stats.get("funding_cache_rows", 0)),
            "funding_max_ts_ms": int(build_stats.get("funding_max_ts_ms", 0)),
        }
        # storage day-buckets registered cycle ledgers regardless of what we pass
        # here. Naming the day partition anyway means an unregistered dataset
        # still gets a bounded part instead of one monolith.
        write_dataset(
            pl.DataFrame([payload], infer_schema_length=None),
            root,
            cycles_dataset,
            partition_by=("date",),
        )
        # For the daemon only, added after the dataset write above so the
        # persisted cycle schema does not change: the next instant a new
        # daily decision exists, where the daemon cuts its timer wait short.
        payload["next_time_deadline_ts_ms"] = next_carry_decision_deadline_ts_ms(cycle_now_ms)
    return PublishedTargetCyclePayload(
        payload,
        target_book_path=engine_book_path,
        target_book_object_path=plan.target_book_object_path or None,
    )


def format_carry_demo_cycle_summary(payload: dict[str, Any]) -> str:
    """Render one concise carry target-producer line for stdout/journald."""

    decision_ts = payload.get("decision_ts_ms")
    decision_day = (
        datetime.fromtimestamp(int(decision_ts) / 1000, tz=timezone.utc).date().isoformat()
        if isinstance(decision_ts, int) and decision_ts > 0
        else "?"
    )
    equity = payload.get("equity_usdt")
    equity_text = f"${float(equity):,.2f}" if isinstance(equity, (int, float)) else "unavailable"
    gross = payload.get("desired_gross_weight")
    gross_text = f"{float(gross):.3f}" if isinstance(gross, (int, float)) else "?"
    # Only rendered when non-zero: entries skipped as too small to place.
    # Without this the line reads suppressed=0 err=none while the whole
    # book silently fails to enter.
    dust = int(payload.get("entry_dust_skips", 0) or 0)
    dust_text = f" dust={dust}" if dust else ""
    # Only rendered when engaged: the deadline pass that skipped the build,
    # and the pre-deadline pass that froze the next day, are the receipts of
    # the fast boundary path.
    fast_path_text = " build_skipped=True" if payload.get("data_build_skipped") else ""
    if payload.get("freeze_ahead_frozen"):
        fast_path_text += " froze_ahead=True"
    # Only rendered when engaged: the early freeze and the names it let sell
    # before the 00:20 clock are the drop exit's whole receipt.
    if payload.get("drop_exit_froze_ahead"):
        fast_path_text += " drop_froze=True"
    drops = payload.get("drop_exit_fired") or []
    if drops:
        fast_path_text += f" drop_exits={','.join(drops)}"
    return (
        "carry target producer "
        f"id={payload.get('cycle_id', '')} mode={payload.get('mode')} "
        f"decision_day={decision_day} stale={payload.get('decision_stale')} "
        f"frozen={payload.get('decision_frozen')}{fast_path_text} "
        f"book={payload.get('desired_book_size')} gross={gross_text} "
        f"standing={payload.get('standing_symbols')} open={payload.get('open_positions')} "
        f"book_delta exit/entry/resize={payload.get('exit_book_removals')}/"
        f"{payload.get('entry_book_additions')}/{payload.get('book_resizes')} "
        f"written={payload.get('book_written')}{dust_text} equity={equity_text} "
        f"err={payload.get('decision_error') or 'none'}"
    )
