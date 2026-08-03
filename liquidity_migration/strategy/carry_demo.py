"""CARRY sleeve decision engine: the crowd-fee collector.

Computes the daily target book for the deployed carry sleeve by replaying
the registered ``lane2_carry_hold_v3`` state machine over a rolling window
of Bybit hourly data. The strategy logic is NOT reimplemented here: the
engine calls the exact registered-scorer functions
(:func:`liquidity_migration.research.backtest.financed_longs.carry_hold_weights` and friends)
on the live frame (:func:`~liquidity_migration.research.backtest.financed_longs.prepare_decision`),
so deployed decisions and the Lane-2 forward scorer can only diverge where
the registered frame caveat says they must: the decision bar itself, which
the research frame drops because it requires a forward 24h return no live
decision can see.

Stateless: the replay recomputes hysteresis state from scratch each cycle over
``REPLAY_DAYS`` of history. The longest state spell in the 2021-2026 record is
19 days, so a 90-day window carries ~4.7x margin, and a spell that outlived the
window is re-captured on any bar where its funding print re-crosses the entry
threshold (entry implies hold). There is no state file, so recovery from
downtime of any length is a plain restart.

``configs/lane2_carry_hold_v3.json`` is loaded only so the deployed parameters
are byte-identical to the registered ones.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from liquidity_migration.core._common import coerce_int
from liquidity_migration.strategy.account_candidate_universe import load_candidate_universe
from liquidity_migration.account.account_intent_client import (
    ENTRY_ATTEMPT_METADATA_KEY,
    ExitFirstPublication,
    publish_exit_first_target_requests,
)
from liquidity_migration.account.account_route import require_account_route
from liquidity_migration.account.account_service import RequestedIntent, SleeveAdapterKind
from liquidity_migration.strategy.account_strategy_state import target_reservation_rows
from liquidity_migration.marketdata.bybit_market_data import BybitMarketData
from liquidity_migration.core.config import ResearchConfig
from liquidity_migration.strategy.event_demo_data import (
    _demo_instruments,
    _download_recent_1h_klines,
    _kline_window,
    _launch_time_ms_by_symbol,
    _resolve_ticker_snapshot,
    _utc_now_ms,
    rank_top_turnover_symbols,
)
from liquidity_migration.account.execution_environment import (
    ExecutionEnvironment,
    account_id_for_environment,
    candidate_universe_realm,
    execution_environment,
)
from liquidity_migration.research.backtest.financed_longs import (
    CarryHoldConfig,
    carry_hold_weights,
    daily_grid,
    prepare_decision,
    top_n_universe,
)
from liquidity_migration.data.storage import (
    exclusive_file_lock,
    read_dataset,
    read_dataset_columns,
    write_dataset,
)
from liquidity_migration.strategy.strategy_planning import (
    account_owner_equity_or_error,
    new_planning_journal_cursor,
    sleeve_planning_snapshot,
    suppress_target_intents,
)
from liquidity_migration.strategy.strategy_target_replay import PublishedTargetCyclePayload
from liquidity_migration.account.strategy_targets import component_target_intent
from liquidity_migration.core.venue_realm import VenueRealm

DAY_MS = 86_400_000
HOUR_MS = 3_600_000

#: Replay window. Hard floor: ~32d for the 30d vol filter's warm-up + 7d
#: maturity; 19d longest-ever spell. 90d keeps every input saturated with
#: wide margin while staying a trivial recompute (~1M rows).
REPLAY_DAYS = 90
MIN_REPLAY_DAYS = 45

#: Minimum universe symbols on the decision bar. Below this the data build is
#: broken and the engine fails closed, holding the previous targets, rather than
#: flattening a healthy book on a data hole. The real universe is 100 names.
MIN_DECISION_SYMBOLS = 50

CARRY_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "lane2_carry_hold_v3.json"


class CarrySleeveError(RuntimeError):
    """Raised when the carry decision cannot be produced safely."""


def load_carry_config(path: Path | None = None) -> CarryHoldConfig:
    """The registered v3 parameters, byte-identical to the Lane-2 file."""
    return CarryHoldConfig.from_json(str(path or CARRY_CONFIG_PATH))


@dataclasses.dataclass(frozen=True)
class CarryDecision:
    """One daily decision: the target weight book at ``decision_ts_ms``."""

    decision_ts_ms: int
    weights: dict[str, float]
    universe_size: int
    replay_days: int
    gross: float

    @property
    def is_flat(self) -> bool:
        return not self.weights


def decide_book(
    view: pl.DataFrame,
    cfg: CarryHoldConfig,
    decision_ts_ms: int,
    *,
    min_decision_symbols: int = MIN_DECISION_SYMBOLS,
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
            f"window ends {(decision_ts_ms - last_ts) // HOUR_MS}h before the "
            "decision bar; data build is stale"
        )
    replay_days = (decision_ts_ms - first_ts) // DAY_MS
    if replay_days < MIN_REPLAY_DAYS:
        raise CarrySleeveError(
            f"replay window {replay_days}d is below the {MIN_REPLAY_DAYS}d floor"
        )

    grid = daily_grid(prepare_decision(view.filter(pl.col("bar_ts_ms") <= decision_ts_ms)))
    universe = top_n_universe(grid, cfg.universe_top_n)
    at_bar = universe.filter(pl.col("bar_ts_ms") == decision_ts_ms)
    if at_bar.height < min_decision_symbols:
        raise CarrySleeveError(
            f"decision bar carries {at_bar.height} universe symbols "
            f"(< {min_decision_symbols}); refusing to decide on a broken build"
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
# Cycle layer: the deployed CARRY target producer.
#
# The engine above answers "what should the book be at 00:00 UTC today"; the
# layer below turns that answer into immutable account-target requests. It is
# a diff machine, not an order machine: every cycle it recomputes the desired
# book, reads the account owner's accepted reservations, and publishes only
# the difference (exit-first). No diff means no publication, which is what
# makes a 60-second cadence safe for a daily strategy.
# ---------------------------------------------------------------------------

_logger = logging.getLogger(__name__)

CARRY_STRATEGY_ID = "carry_hold_v3"
#: One stable component per symbol. Unlike the continuous sleeve (one
#: component per signal), carry manages a persistent per-symbol target that is
#: revised in place, so the component key never needs a fresh identity.
CARRY_COMPONENT_ID = "carry_hold"
CARRY_PROFILE_NAME = "carry_hold_v3_live_v1"
CARRY_CYCLES_DATASET = "carry_hold_demo_cycles"
CARRY_MAINNET_CYCLES_DATASET = "carry_hold_mainnet_cycles"
CARRY_FUNDING_DATASET = "carry_funding_events"

#: Fetch-universe breadth. The registered rule ranks its own top-100 by adv24
#: inside the replay, so the fetch set only needs to be a superset; 150 by
#: current 24h turnover comfortably covers the adv24 top-100 through rank churn.
CARRY_FETCH_UNIVERSE_TOP_N = 150

#: The daily decision becomes computable once the last kline of the prior UTC
#: day (23:00-00:00) is reliably served by REST — the same 20-minute margin the
#: rmom refresh timer uses for exactly the same bar.
DECISION_KLINE_LAG_MS = 20 * 60 * 1000
#: Entry-signal validity. The decision is a daily state, but an entry request
#: that has not been accepted within 6h belongs to a stale book; the account
#: service expires it rather than executing it late.
SIGNAL_VALIDITY_MS = 6 * HOUR_MS
#: Producer-side guard band before ``signal_valid_until_ms``. Expiry is a
#: TERMINAL attempt and, with carry's stable per-symbol component ids, terminal
#: attempts suppress that symbol's entries permanently, so never hand the
#: service an entry it can only expire.
ENTRY_PUBLISH_GUARD_MS = 15 * 60 * 1000
#: A sleeve whose newest successful decision is older than this is loudly
#: stale: today's decision still failing past 06:00 the next day.
DECISION_STALE_MS = 30 * HOUR_MS
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
RESIZE_MIN_NOTIONAL_USDT = 1.0
RESIZE_MIN_FRACTION_OF_STANDING = 0.05
#: Entries below this notional would round to zero venue quantity and come
#: back as a terminal (permanently suppressing) rejection; skip them instead.
ENTRY_MIN_NOTIONAL_USDT = 10.0
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


class CarryCycleState:
    """Mutable, daemon-owned cross-cycle memory (never decision authority).

    The cycle function itself is stateless — everything decision-relevant is
    recomputed from disk and REST each cycle. This object carries four
    operational hints between cycles: when the funding cache was last swept
    (settled prints only change on hour boundaries, so re-sweeping every 60s
    would be ~200k pointless REST calls/day), the newest successful decision
    (so the ``decision_stale`` alarm does not need to re-read the cycles
    dataset on every failing cycle), the equity this decision was first sized
    against, and the account-journal read position (so a cycle verifies the
    segments written since the last one instead of the whole account history).

    Losing this object (restart, ``--once``) costs one extra funding sweep, one
    cycles-dataset read, one cold journal read, and one re-anchor of the sizing
    equity to the current mark. The re-anchor can move the day's targets by
    however much equity moved since the decision; the resize dead-band absorbs
    that unless the move is large.
    """

    __slots__ = (
        "frozen_decision_bar_ts_ms",
        "frozen_decision_payload",
        "funding_swept_hour_ts",
        "journal_cursor",
        "last_successful_decision_ts_ms",
        "sizing_equity_usdt",
        "sizing_equity_decision_ts_ms",
    )

    def __init__(self) -> None:
        self.frozen_decision_bar_ts_ms: int | None = None
        self.frozen_decision_payload: tuple[CarryDecision, dict[str, float], int] | None = None
        self.funding_swept_hour_ts: int | None = None
        self.journal_cursor = new_planning_journal_cursor()
        self.last_successful_decision_ts_ms: int | None = None
        self.sizing_equity_usdt: float | None = None
        self.sizing_equity_decision_ts_ms: int | None = None

    def frozen_decision(
        self, decision_ts_ms: int
    ) -> tuple[CarryDecision, dict[str, float], int] | None:
        """This bar's already-computed book, if there is one.

        The registered rule decides ONCE per 00:00 UTC bar and holds for the
        day. Recomputing every 60s makes the book a function of whatever the
        caches held at that moment, and the same bar can then produce different
        symbol sets minutes apart. Later prints belong to tomorrow's bar. A
        failed decision is never frozen, so a data hiccup still retries.
        """

        if (
            self.frozen_decision_bar_ts_ms == int(decision_ts_ms)
            and self.frozen_decision_payload is not None
        ):
            return self.frozen_decision_payload
        return None

    def freeze_decision(
        self,
        *,
        decision_ts_ms: int,
        decision: CarryDecision,
        trail_by_symbol: dict[str, float],
        universe_eligible: int,
    ) -> None:
        """Pin this bar's book. Only a NEW bar replaces it."""

        self.frozen_decision_bar_ts_ms = int(decision_ts_ms)
        self.frozen_decision_payload = (decision, dict(trail_by_symbol), int(universe_eligible))

    def sizing_equity(self, *, decision_ts_ms: int, equity_usdt: float) -> float:
        """Equity as of when this decision was first sized, not the live mark.

        CARRY decides once a day and holds. Sizing off the live mark every cycle
        makes the day's targets a function of the book's own unrealized P&L, and
        that feedback has a direction: equity rises because the longs rose, so
        the target rises and the sleeve buys after the move, and sells after a
        fall. Anchoring to the decision keeps intraday targets constant, so only
        a new decision moves the book. The disaster stop and native protection
        remain the capital-preservation path.

        An unusable equity read (``<= 0``) passes through unanchored: callers
        already refuse to size on it, and anchoring it would outlive the failure.
        """

        if equity_usdt <= 0.0:
            return equity_usdt
        if (
            self.sizing_equity_decision_ts_ms != int(decision_ts_ms)
            or self.sizing_equity_usdt is None
            or self.sizing_equity_usdt <= 0.0
        ):
            self.sizing_equity_decision_ts_ms = int(decision_ts_ms)
            self.sizing_equity_usdt = float(equity_usdt)
        return float(self.sizing_equity_usdt)


@dataclasses.dataclass(frozen=True, slots=True)
class CarryDemoCycleConfig:
    """CARRY demo/mainnet target-producer configuration.

    Sizing fields (``notional_multiplier``, ``entry_leverage``,
    ``declared_stop_loss_fraction``, ``max_new_entries_per_cycle``) are injected
    from the operational profile's ``carry`` block by the CLI; rule parameters
    stay in the registered config the engine loads. The ``ws_klines_*`` block
    exists only because the shared base daemon reads those fields -- carry runs
    pure REST and never starts a kline manager.
    """

    # --- environment / wiring ---
    execution_environment: str = ""
    account_intent_inbox_root: str | None = None
    account_execution_root: str | None = None
    candidate_universe_file: str = ""
    # --- sizing (operational profile carry block) ---
    notional_multiplier: float = 1.0
    entry_leverage: float = 2.0
    declared_stop_loss_fraction: float = 0.35
    max_new_entries_per_cycle: int = 10
    #: Ceiling on the equity this producer may size against, from the profile's
    #: ``capital_reference_usdt``. The owner's pre-trade caps are absolute USDT
    #: numbers calibrated against that reference while sizing reads live equity,
    #: so without a clamp the two drift apart and the load-time envelope proof
    #: in ``operational_profile`` stops holding. 0.0 disables the clamp.
    capital_reference_usdt: float = 0.0
    operational_profile_sha256: str = ""
    # --- data build ---
    replay_days: int = REPLAY_DAYS
    workers: int = 4
    # --- WS kline plane (streams primary, REST as tail fallback) ---
    # The store must span the cycle's whole kline window (``replay_days`` plus
    # the download margin) or the shared reader never takes its fast path and
    # every cycle falls back to the on-disk cache scan.
    ws_klines_enabled: bool = True
    ws_klines_bootstrap_workers: int = 16
    ws_klines_lookback_days: int = REPLAY_DAYS + 2
    ws_klines_universe_refresh_seconds: float = 3600.0
    ws_klines_topics_per_connection: int = 180
    ws_klines_stale_warning_seconds: float = 60.0
    ws_klines_stale_reconnect_seconds: float = 180.0


def _validate_carry_demo_config(config: CarryDemoCycleConfig) -> None:
    """Validate target routing and sizing before any shared resource opens.

    Producers are target-only: every environment requires its canonical
    account-owner route, and sleeve-side Telegram stays retired.
    """

    execution_environment(config.execution_environment)
    has_account_inbox = bool(str(config.account_intent_inbox_root or "").strip())
    has_account_execution_root = bool(str(config.account_execution_root or "").strip())
    if has_account_inbox != has_account_execution_root:
        raise ValueError(
            "account_intent_inbox_root and account_execution_root must be configured together"
        )
    if not has_account_inbox:
        raise ValueError(
            "operational target mode requires account_intent_inbox_root and "
            "account_execution_root; direct sleeve order authority is retired"
        )
    if bool(getattr(config, "telegram", False)):
        raise ValueError(
            "account target route delegates Telegram exclusively to the account owner"
        )
    if not math.isfinite(config.notional_multiplier) or config.notional_multiplier <= 0.0:
        raise ValueError("notional_multiplier must be positive")
    if not math.isfinite(config.entry_leverage) or config.entry_leverage <= 0.0:
        raise ValueError("entry_leverage must be positive")
    if not 0.0 < config.declared_stop_loss_fraction < 1.0:
        raise ValueError("declared_stop_loss_fraction must be a fraction in (0, 1)")
    if config.max_new_entries_per_cycle < 1:
        raise ValueError("max_new_entries_per_cycle must be >= 1")
    if config.replay_days < MIN_REPLAY_DAYS:
        raise ValueError(f"replay_days must be >= {MIN_REPLAY_DAYS} (engine floor)")
    if config.workers < 1:
        raise ValueError("workers must be >= 1")
    if config.ws_klines_enabled and config.ws_klines_lookback_days < config.replay_days + 1:
        # A store narrower than the cycle window means the reader's fast path
        # can never engage and every cycle silently pays the slow disk scan.
        raise ValueError(
            "ws_klines_lookback_days must cover replay_days + 1 when the WS kline plane is on"
        )


def carry_cycles_dataset(config: CarryDemoCycleConfig) -> str:
    """Cycle-heartbeat dataset for this planner's environment.

    Named per environment: a cycle written into the wrong dataset would later
    be read as the wrong environment's evidence.
    """

    return {
        ExecutionEnvironment.MAINNET: CARRY_MAINNET_CYCLES_DATASET,
    }.get(execution_environment(config.execution_environment), CARRY_CYCLES_DATASET)


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
        .filter(
            pl.col("bar_ts_ms").is_between(int(window_start_ms), int(max_bar_ts_ms))
        )
        .sort(["bar_ts_ms", "symbol"])
    )
    if keyed.is_empty():
        return _empty_venue_view()
    if funding.is_empty():
        view = keyed.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("by_funding"),
            pl.lit(None, dtype=pl.Float64).alias("by_funding_age_h"),
        )
        return view.sort(["symbol", "bar_ts_ms"])
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
    ).with_columns(
        ((pl.col("bar_ts_ms") - pl.col("funding_ts_ms")) / HOUR_MS).alias(
            "by_funding_age_h"
        )
    )
    return view.drop("funding_ts_ms").sort(["symbol", "bar_ts_ms"])


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
    covered = int(at_bar.get_column("by_funding").is_not_null().sum())
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
        )
    )
    if stale_standing.height:
        names = ",".join(sorted(stale_standing.get_column("symbol").to_list()))
        raise CarrySleeveError(
            f"standing symbols with live klines but stale funding prints: {names}; "
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
        (pl.col("funding_ts_ms") > int(decision_ts_ms) - DAY_MS)
        & (pl.col("funding_ts_ms") <= int(decision_ts_ms))
    )
    if window.is_empty():
        return {}
    sums = window.group_by("symbol").agg(pl.col("funding_rate").sum().alias("trail"))
    return {str(row["symbol"]): float(row["trail"]) for row in sums.iter_rows(named=True)}


def _carry_standing_rows(reservations: pl.DataFrame) -> dict[str, tuple[float, float]]:
    """Accepted (signed notional, signed quantity) per open/pending reservation.

    Reservations are accepted desired targets (``open`` or ``target_pending``),
    not fills: the diff compares desire against desire, or a target accepted but
    unfilled is re-proposed every cycle. Notional is reconstructed as
    ``signed_qty x target_reference_price``, the pair the kernel stamped.

    Quantity comes along because zero is meaningful: a reservation for an
    already-zero target is a completed exit desire with nothing left to reduce,
    and re-exiting it loops forever.
    """

    if reservations.is_empty():
        return {}
    required = {"symbol", "signed_qty", "target_reference_price"}
    missing = sorted(required - set(reservations.columns))
    if missing:
        raise RuntimeError(f"carry reservations lack expected columns: {missing}")
    frame = reservations.select(
        pl.col("symbol").cast(pl.String),
        pl.col("signed_qty").cast(pl.Float64, strict=False).fill_null(0.0).alias("signed_qty"),
        (
            pl.col("signed_qty").cast(pl.Float64, strict=False).fill_null(0.0)
            * pl.col("target_reference_price").cast(pl.Float64, strict=False).fill_null(0.0)
        ).alias("standing_notional_usdt"),
    )
    sums = frame.group_by("symbol").agg(
        pl.col("standing_notional_usdt").sum(), pl.col("signed_qty").sum()
    )
    return {
        str(row["symbol"]): (
            float(row["standing_notional_usdt"]),
            float(row["signed_qty"]),
        )
        for row in sums.iter_rows(named=True)
    }


def _carry_component_intent(
    *,
    action: str,
    cycle_now_ms: int,
    symbol: str,
    signed_notional_usdt: float,
    leverage: float,
    reason: str,
    metadata: dict[str, Any],
) -> RequestedIntent:
    """Build one carry component target with a symbol-qualified decision key.

    The shared grammar in :func:`component_target_intent` derives the decision
    key from ``(sleeve, strategy, ts, action, component)`` — which is unique
    for sleeves that mint one component id per signal, but carry deliberately
    keeps ONE stable component id per symbol. Without qualification, two
    same-cycle exits would share a decision key: the account kernel rejects a
    reused decision key whose content changed, and the daemon's capture tape
    rejects a repeated key outright. The target key (which embeds the symbol)
    and all entry-attempt metadata are built and validated by the shared
    helper; only the decision key is suffixed here.
    """

    base = component_target_intent(
        adapter_kind=SleeveAdapterKind.CARRY,
        action=action,
        decision_ts_ms=cycle_now_ms,
        strategy_id=CARRY_STRATEGY_ID,
        component_id=CARRY_COMPONENT_ID,
        symbol=symbol,
        signed_notional_usdt=signed_notional_usdt,
        leverage=leverage,
        reason=reason,
        metadata=metadata,
    )
    qualified = dataclasses.replace(
        base.intent,
        decision_key=f"{base.intent.decision_key}/{base.intent.symbol}",
    )
    return RequestedIntent(adapter_kind=base.adapter_kind, intent=qualified)


@dataclasses.dataclass(frozen=True, slots=True)
class CarryTargetPlan:
    """Diffed intents plus the exact per-reason skip counts for the journal."""

    exit_intents: list[RequestedIntent]
    entry_intents: list[RequestedIntent]
    resize_intents: list[RequestedIntent]
    desired_book_size: int
    desired_gross_weight: float
    planned_exits: int
    planned_entries: int
    planned_resizes: int
    entry_cap_deferrals: int
    entry_validity_expired_skips: int
    entry_dust_skips: int
    entry_blocked_reason: str
    stranded_zero_quantity_reservations: int = 0


def _empty_carry_plan(*, entry_blocked_reason: str = "") -> CarryTargetPlan:
    return CarryTargetPlan(
        exit_intents=[],
        entry_intents=[],
        resize_intents=[],
        desired_book_size=0,
        desired_gross_weight=0.0,
        planned_exits=0,
        planned_entries=0,
        planned_resizes=0,
        entry_cap_deferrals=0,
        entry_validity_expired_skips=0,
        entry_dust_skips=0,
        entry_blocked_reason=entry_blocked_reason,
        stranded_zero_quantity_reservations=0,
    )


def _carry_target_plan(
    *,
    decision: CarryDecision | None,
    rule: CarryHoldConfig,
    standing_rows: dict[str, tuple[float, float]],
    trail_by_symbol: dict[str, float],
    demo: CarryDemoCycleConfig,
    equity_usdt: float,
    account_owner_health_error: str,
    cycle_now_ms: int,
    cycle_state: CarryCycleState | None = None,
) -> CarryTargetPlan:
    """Diff the desired book against the standing book into target intents.

    Exit-first by design: exits are zero targets needing no equity, so they
    publish even when the owner-health read failed, while entries and resizes
    are blocked without it. When the decision itself is unavailable NOTHING is
    planned -- a data hiccup holds the standing book, never flattens it.

    Sizing uses :meth:`CarryCycleState.sizing_equity` rather than the live mark
    in ``equity_usdt``. Callers without cross-cycle state size off the live
    value, which for one cycle is the same thing.
    """

    if decision is None:
        return _empty_carry_plan(entry_blocked_reason="decision_unavailable")

    decision_ts_ms = decision.decision_ts_ms
    desired = decision.weights
    entry_health_ok = not account_owner_health_error and equity_usdt > 0.0
    entry_blocked_reason = "" if entry_health_ok else "account_owner_health_unavailable"
    sizing_equity_usdt = (
        cycle_state.sizing_equity(decision_ts_ms=decision_ts_ms, equity_usdt=equity_usdt)
        if cycle_state is not None
        else equity_usdt
    )
    # Clamp to the profile's capital reference, after the decision anchor so a
    # profitable day cannot ratchet the book up. Never applied upward: a smaller
    # account still sizes off its own equity.
    if demo.capital_reference_usdt > 0.0:
        sizing_equity_usdt = min(sizing_equity_usdt, float(demo.capital_reference_usdt))

    # A reservation whose accepted quantity is already ZERO is a completed exit
    # desire, not exposure: re-publishing a zero target converges nothing and
    # just re-creates the pending reservation next cycle, forever.
    #
    # Such a reservation is INERT on every path. Excluding it from exits alone
    # would let a still-desired name escape through the resize branch, where a
    # zero standing notional clears any dead-band and republishes the whole
    # target every cycle. It still counts for ADMISSION, so the name is not
    # re-entered underneath its own unconverged target. Resolving it needs an
    # operator (``scripts/ops.sh wedged-command``), hence the count.
    stranded_symbols = sorted(
        symbol for symbol, (_notional, qty) in standing_rows.items() if qty == 0.0
    )
    live_standing = {
        symbol: notional
        for symbol, (notional, qty) in standing_rows.items()
        if qty != 0.0
    }
    exit_symbols = sorted(set(live_standing) - set(desired))
    exit_intents = [
        _carry_component_intent(
            action="exit",
            cycle_now_ms=cycle_now_ms,
            symbol=symbol,
            signed_notional_usdt=0.0,
            leverage=demo.entry_leverage,
            reason="carry exit: funding normalized or velocity exit",
            metadata={
                "source": "carry_target_adapter",
                "owner_sleeve": SleeveAdapterKind.CARRY.value,
                "prior_trade_id": CARRY_COMPONENT_ID,
                "decision_day_ts_ms": decision_ts_ms,
            },
        )
        for symbol in exit_symbols
    ]

    entry_intents: list[RequestedIntent] = []
    resize_intents: list[RequestedIntent] = []
    planned_entries = 0
    planned_resizes = 0
    entry_cap_deferrals = 0
    entry_validity_expired_skips = 0
    entry_dust_skips = 0
    if entry_health_ok:
        signal_valid_until_ms = decision_ts_ms + SIGNAL_VALIDITY_MS
        entry_symbols = sorted(
            (symbol for symbol in desired if symbol not in standing_rows),
            # Deepest trailing crowd payment first: the trail is negative for
            # the prints this strategy collects, so ascending is deepest-first.
            # The symbol tiebreak keeps the order deterministic.
            key=lambda symbol: (trail_by_symbol.get(symbol, 0.0), symbol),
        )
        if cycle_now_ms >= signal_valid_until_ms - ENTRY_PUBLISH_GUARD_MS:
            # Too late to open NEW risk: the service would expire these on
            # arrival, and an expiry permanently suppresses the symbol.
            # Tomorrow's decision re-desires them.
            entry_validity_expired_skips = len(entry_symbols)
            entry_symbols = []
        for symbol in entry_symbols:
            if planned_entries >= demo.max_new_entries_per_cycle:
                entry_cap_deferrals += 1
                continue
            weight = float(desired[symbol])
            target_notional = weight * sizing_equity_usdt * demo.notional_multiplier
            if target_notional < ENTRY_MIN_NOTIONAL_USDT:
                entry_dust_skips += 1
                continue
            metadata: dict[str, Any] = {
                "source": "carry_target_adapter",
                "signal_ts_ms": decision_ts_ms,
                "signal_valid_until_ms": signal_valid_until_ms,
                "stop_loss_pct": demo.declared_stop_loss_fraction,
                "target_weight": weight,
                "raw_target_notional_usdt": target_notional,
                "quantity_authority": "account_kernel_demo_rules",
            }
            trail = trail_by_symbol.get(symbol)
            if trail is not None:
                metadata["trail_fund_24h"] = float(trail)
            entry_intents.append(
                _carry_component_intent(
                    action="entry",
                    cycle_now_ms=cycle_now_ms,
                    symbol=symbol,
                    signed_notional_usdt=target_notional,
                    leverage=demo.entry_leverage,
                    reason=(
                        f"carry entry: settled print < -{rule.enter_bp:g}bp, filters pass"
                    ),
                    metadata=metadata,
                )
            )
            planned_entries += 1
        for symbol in sorted(set(desired) & set(live_standing)):
            weight = float(desired[symbol])
            target_notional = weight * sizing_equity_usdt * demo.notional_multiplier
            standing = float(live_standing[symbol])
            delta = target_notional - standing
            threshold = max(
                RESIZE_MIN_NOTIONAL_USDT,
                RESIZE_MIN_FRACTION_OF_STANDING * abs(standing),
            )
            if abs(delta) <= threshold:
                continue
            resize_intents.append(
                _carry_component_intent(
                    action="resize",
                    cycle_now_ms=cycle_now_ms,
                    symbol=symbol,
                    signed_notional_usdt=target_notional,
                    leverage=demo.entry_leverage,
                    reason="carry resize: depth rescale",
                    metadata={
                        "source": "carry_target_adapter",
                        # Protection stays declared across revisions: the
                        # kernel derives the venue stop from the outermost
                        # component-declared fraction.
                        "stop_loss_pct": demo.declared_stop_loss_fraction,
                        "decision_day_ts_ms": decision_ts_ms,
                        "target_weight": weight,
                        "raw_target_notional_usdt": target_notional,
                        "prior_standing_notional_usdt": standing,
                        "quantity_authority": "account_kernel_demo_rules",
                    },
                )
            )
            planned_resizes += 1

    return CarryTargetPlan(
        exit_intents=exit_intents,
        entry_intents=entry_intents,
        resize_intents=resize_intents,
        desired_book_size=len(desired),
        desired_gross_weight=float(decision.gross),
        planned_exits=len(exit_intents),
        planned_entries=planned_entries,
        planned_resizes=planned_resizes,
        entry_cap_deferrals=entry_cap_deferrals,
        entry_validity_expired_skips=entry_validity_expired_skips,
        entry_dust_skips=entry_dust_skips,
        entry_blocked_reason=entry_blocked_reason,
        stranded_zero_quantity_reservations=len(stranded_symbols),
    )


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
    return frame.select(
        pl.col("symbol").cast(pl.String),
        pl.col("funding_ts_ms").cast(pl.Int64),
        pl.col("funding_rate").cast(pl.Float64),
    ).unique(subset=["symbol", "funding_ts_ms"], keep="last")


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
                    _logger.warning(
                        "carry funding fetch failed for %s (retrying once): %s", symbol, exc
                    )
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
        pl.concat([cached, fresh], how="vertical").unique(
            subset=["symbol", "funding_ts_ms"], keep="last"
        )
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
) -> tuple[list[str], int]:
    """Intersect the turnover universe with the frozen candidate epoch.

    Standing symbols are added back AFTER the intersection: a held name must
    never lose market data (its exit still needs the replay), even when it has
    dropped out of the frozen candidate population.
    """

    skipped = 0
    kept = list(top_symbols)
    if candidate_universe_file:
        allowed = set(
            load_candidate_universe(candidate_universe_file, realm=realm).symbols
        )
        kept = [symbol for symbol in top_symbols if symbol in allowed]
        skipped = len(top_symbols) - len(kept)
    return sorted(set(kept) | set(standing_symbols)), skipped


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
) -> tuple[pl.DataFrame, pl.DataFrame, dict[str, Any]]:
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
    fetch_symbols, candidate_skipped = _candidate_filtered_universe(
        top_symbols,
        candidate_universe_file=demo.candidate_universe_file,
        realm=candidate_universe_realm(demo.execution_environment),
        standing_symbols=standing_symbols,
    )
    if not fetch_symbols:
        raise CarrySleeveError(
            "carry fetch universe is empty (ticker fetch failed and nothing standing)"
        )
    try:
        launch_times = _launch_time_ms_by_symbol(
            _demo_instruments(market, cache_root=root, now_ms=now_ms)
        )
    except Exception as exc:  # noqa: BLE001 - listing ages only avoid head refetches
        _logger.warning("carry instruments fetch failed; head-completeness checks degrade: %s", exc)
        launch_times = {}
    start_ms, window_end_open_ms = _kline_window(now_ms, lookback_days=demo.replay_days)
    # The REST kline pager treats its end bound as EXCLUSIVE (bars are fetched
    # with opens in [start, end)), so requesting through the CURRENT hour
    # boundary yields opens through floor(now)-1h — the newest CLOSED bar —
    # and never the in-progress bar. Close-keyed (see _carry_venue_view), that
    # newest bar IS the current day's 00:00 decision bar during the 00:xx hour.
    download_end_ms = window_end_open_ms + HOUR_MS
    klines, kline_stats = _download_recent_1h_klines(
        fetch_symbols,
        start_ms=start_ms,
        end_ms=download_end_ms,
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
        "kline_cache_rows": int(kline_stats.get("cache_rows", 0)),
        "kline_fetched_rows": int(kline_stats.get("fetched_rows", 0)),
        "kline_output_rows": int(kline_stats.get("output_rows", 0)),
        "kline_fetch_symbols": int(kline_stats.get("fetch_symbols", 0)),
        "funding_cache_rows": funding.height,
        "funding_max_ts_ms": (
            coerce_int(funding.get_column("funding_ts_ms").max()) if not funding.is_empty() else 0
        ),
        **funding_stats,
    }
    return klines, funding, stats


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


def run_carry_demo_cycle(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    demo_config: CarryDemoCycleConfig | None = None,
    market_client: Any | None = None,
    now_ms: int | None = None,
    kline_store: Any | None = None,
    ticker_cache: Any | None = None,
    state_cache_stale_seconds: float = 120.0,
    cycle_state: CarryCycleState | None = None,
) -> PublishedTargetCyclePayload:
    """Plan one CARRY cycle and publish immutable account targets.

    Every cycle: rebuild the venue view, replay the registered rule to today's
    desired book, read the account owner's accepted reservations, and publish
    only the exit-first difference. Failure policy is HOLD-STEADY: a data-build
    or decision failure publishes nothing and never flattens, while
    ``decision_error``/``decision_stale`` make the outage loud.

    ``kline_store`` serves the cycle's close-keyed 1h bars from the daemon's
    WS plane (identical bar content to the REST window contract, pushed
    instead of pulled); ``ticker_cache`` serves the turnover-ranked fetch
    universe. REST remains the fallback for either when the stream is cold or
    stale, and the sole source for settled funding history, which no stream
    carries.
    """

    demo = demo_config or CarryDemoCycleConfig()
    _validate_carry_demo_config(demo)
    environment = execution_environment(demo.execution_environment).value
    root = Path(data_root).expanduser()
    account_route = require_account_route(
        account_id=account_id_for_environment(environment),
        environment=environment,
        account_root=Path(str(demo.account_execution_root)).expanduser(),
        inbox_root=Path(str(demo.account_intent_inbox_root)).expanduser(),
    )
    root.mkdir(parents=True, exist_ok=True)
    cycle_now_ms = int(now_ms if now_ms is not None else _utc_now_ms())
    decision_ts_ms = carry_decision_ts_ms(cycle_now_ms)
    cycle_id = f"carry-target-{CARRY_STRATEGY_ID}-{cycle_now_ms}"
    cycles_dataset = carry_cycles_dataset(demo)
    state = cycle_state if cycle_state is not None else CarryCycleState()

    with exclusive_file_lock(root / ".locks" / "carry_demo_cycle.lock", stale_seconds=900):
        planning = sleeve_planning_snapshot(
            account_route,
            sleeve=SleeveAdapterKind.CARRY,
            strategy_ids=(CARRY_STRATEGY_ID,),
            journal_cursor=state.journal_cursor,
        )
        reservations = target_reservation_rows(planning.canonical_trades)
        standing_rows = _carry_standing_rows(reservations)
        standing_symbols = set(standing_rows)
        open_trades = (
            planning.canonical_trades.filter(pl.col("status") == "open")
            if not planning.canonical_trades.is_empty()
            and "status" in planning.canonical_trades.columns
            else pl.DataFrame()
        )
        equity_usdt, account_owner_health_error = account_owner_equity_or_error(
            account_route,
            environment=environment,
        )

        decision: CarryDecision | None = None
        decision_error: str | None = None
        decision_frozen = False
        rule = load_carry_config()
        trail_by_symbol: dict[str, float] = {}
        build_stats: dict[str, Any] = {}
        universe_eligible = 0
        try:
            market = market_client or BybitMarketData(
                category=config.exchange.category,
                testnet=config.exchange.testnet,
            )
            klines, funding, build_stats = _build_carry_demo_market_data(
                root=root,
                config=config,
                demo=demo,
                market=market,
                now_ms=cycle_now_ms,
                standing_symbols=standing_symbols,
                state=state,
                kline_store=kline_store,
                ticker_cache=ticker_cache,
                state_cache_stale_seconds=state_cache_stale_seconds,
            )
            window_start_ms = decision_ts_ms - demo.replay_days * DAY_MS
            view = _carry_venue_view(
                klines,
                funding,
                window_start_ms=window_start_ms,
                max_bar_ts_ms=decision_ts_ms,
            )
            if not view.is_empty():
                # A cold-started cache begins mid-day, which the engine's
                # daily-grid phase guard rightly refuses, so trim to the first
                # 00:00 UTC key. A no-op once the cache spans the window.
                first_ts = int(view.get_column("bar_ts_ms").min())  # type: ignore[arg-type]
                if first_ts % DAY_MS != 0:
                    aligned_start = ((first_ts // DAY_MS) + 1) * DAY_MS
                    view = view.filter(pl.col("bar_ts_ms") >= aligned_start)
            universe_eligible = int(view.get_column("symbol").n_unique()) if not view.is_empty() else 0
            frozen = state.frozen_decision(decision_ts_ms)
            if frozen is not None:
                decision, trail_by_symbol, universe_eligible = frozen
                decision_frozen = True
            else:
                _validate_carry_view_health(
                    view,
                    decision_ts_ms=decision_ts_ms,
                    standing_symbols=standing_symbols,
                )
                trail_by_symbol = _trailing_settled_funding(
                    funding, decision_ts_ms=decision_ts_ms
                )
                decision = decide_book(view, rule, decision_ts_ms)
                state.freeze_decision(
                    decision_ts_ms=decision_ts_ms,
                    decision=decision,
                    trail_by_symbol=trail_by_symbol,
                    universe_eligible=universe_eligible,
                )
        except Exception as exc:  # noqa: BLE001 - hold-steady: a data hiccup must never flatten
            decision_error = f"{type(exc).__name__}: {exc}"[:500]
            _logger.exception("carry decision build failed; holding the standing book")

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

        plan = _carry_target_plan(
            decision=decision,
            rule=rule,
            standing_rows=standing_rows,
            trail_by_symbol=trail_by_symbol,
            demo=demo,
            equity_usdt=equity_usdt,
            account_owner_health_error=account_owner_health_error,
            cycle_now_ms=cycle_now_ms,
            cycle_state=state,
        )
        suppression = suppress_target_intents(
            exit_intents=plan.exit_intents,
            entry_intents=[*plan.entry_intents, *plan.resize_intents],
            unresolved_target_keys=planning.unresolved_targets.target_keys,
            terminal_entry_attempts=planning.terminal_entry_attempts,
        )
        kept_exits = suppression.exit_intents
        kept_entries = [
            item
            for item in suppression.entry_intents
            if ENTRY_ATTEMPT_METADATA_KEY in item.intent.metadata
        ]
        kept_resizes = [
            item
            for item in suppression.entry_intents
            if ENTRY_ATTEMPT_METADATA_KEY not in item.intent.metadata
        ]
        target_keys = [
            item.intent.target_key for item in (*kept_exits, *kept_entries, *kept_resizes)
        ]
        if len(set(target_keys)) != len(target_keys):
            raise RuntimeError("carry planner proposed duplicate component target keys")
        publication: ExitFirstPublication = publish_exit_first_target_requests(
            planning.publisher,
            batch_prefix=f"carry/{cycle_id}",
            exit_intents=kept_exits,
            # Entries before resizes: under stop-at-first-error, that gives the
            # per-cycle entry budget publication priority.
            entry_intents=[*kept_entries, *kept_resizes],
            created_ts_ns=cycle_now_ms * 1_000_000,
            independent_entry_requests=True,
        )
        published_exit_targets = len(publication.exit_requests)
        # Independent entry-channel requests publish in caller order and stop at
        # the first error, so the published list is a prefix of
        # [entries..., resizes...] and the split is the prefix length.
        published_entry_channel = len(publication.entry_requests)
        published_entry_targets = min(published_entry_channel, len(kept_entries))
        published_resize_targets = published_entry_channel - published_entry_targets

        account_target_requests = {
            "exit_request_ids": list(publication.exit_request_ids),
            "exit_requests": [
                {
                    "request_id": item.request.request_id,
                    "batch_id": item.request.batch_id,
                    "target_key": item.request.intents[0].intent.target_key,
                }
                for item in publication.exit_requests
            ],
            "entry_request_ids": list(publication.entry_request_ids),
            "entry_requests": [
                {
                    "request_id": item.request.request_id,
                    "batch_id": item.request.batch_id,
                    "target_key": item.request.intents[0].intent.target_key,
                }
                for item in publication.entry_requests
            ],
            "publication_errors": [
                {
                    "stage": error.stage,
                    "target_key": error.target_key,
                    "error_type": error.error_type,
                    "message": error.message,
                }
                for error in publication.errors
            ],
        }
        payload: dict[str, Any] = {
            "cycle_id": cycle_id,
            "ts_ms": cycle_now_ms,
            "sleeve": "carry",
            "mode": f"{environment}_target",
            "environment": environment,
            "strategy_id": CARRY_STRATEGY_ID,
            "strategy_profile": CARRY_PROFILE_NAME,
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
            "decision_universe_size": decision.universe_size if decision is not None else 0,
            "decision_replay_days": decision.replay_days if decision is not None else 0,
            "desired_book_size": plan.desired_book_size,
            "desired_gross_weight": plan.desired_gross_weight,
            "universe_fetched": int(build_stats.get("universe_fetched", 0)),
            "universe_eligible": universe_eligible,
            "candidate_skipped_symbols": int(build_stats.get("candidate_skipped_symbols", 0)),
            "open_positions": open_trades.height,
            "target_reservations": reservations.height,
            "standing_symbols": len(standing_symbols),
            "planned_exits": plan.planned_exits,
            "planned_entries": plan.planned_entries,
            "planned_resizes": plan.planned_resizes,
            "entry_cap_deferrals": plan.entry_cap_deferrals,
            "entry_validity_expired_skips": plan.entry_validity_expired_skips,
            "entry_dust_skips": plan.entry_dust_skips,
            "entry_blocked_reason": plan.entry_blocked_reason,
            "stranded_zero_quantity_reservations": plan.stranded_zero_quantity_reservations,
            "exit_targets_queued": published_exit_targets,
            "entry_targets_queued": published_entry_targets,
            "resize_targets_queued": published_resize_targets,
            "target_intents_queued": published_exit_targets + published_entry_channel,
            "unresolved_exit_target_suppressions": suppression.unresolved_exit_suppressions,
            "unresolved_entry_target_suppressions": suppression.unresolved_entry_suppressions,
            "terminal_entry_attempt_suppressions": suppression.terminal_entry_attempt_suppressions,
            "simultaneous_exit_entry_suppressions": suppression.simultaneous_exit_suppressions,
            # Null, not 0.0, when owner health is unavailable: a literal zero
            # reads as a -100% equity spike in every cycles-derived curve.
            "equity_usdt": equity_usdt if not account_owner_health_error else None,
            # The mark above is descriptive; this is what the day's targets
            # were sized against and the only one that explains a notional.
            "sizing_equity_usdt": state.sizing_equity_usdt,
            "sizing_equity_decision_ts_ms": state.sizing_equity_decision_ts_ms,
            "account_owner_health_error": account_owner_health_error,
            "wallet_error": account_owner_health_error,
            "entry_risk_health_ok": not account_owner_health_error and equity_usdt > 0.0,
            "kline_cache_rows": int(build_stats.get("kline_cache_rows", 0)),
            "kline_fetched_rows": int(build_stats.get("kline_fetched_rows", 0)),
            "kline_output_rows": int(build_stats.get("kline_output_rows", 0)),
            "kline_fetch_symbols": int(build_stats.get("kline_fetch_symbols", 0)),
            "kline_store_rows": 0,
            "funding_swept": bool(build_stats.get("funding_swept", False)),
            "funding_rows_appended": int(build_stats.get("funding_rows_appended", 0)),
            "funding_fetch_failures": int(build_stats.get("funding_fetch_failures", 0)),
            "funding_failed_symbols": str(build_stats.get("funding_failed_symbols", "")),
            "funding_cache_rows": int(build_stats.get("funding_cache_rows", 0)),
            "funding_max_ts_ms": int(build_stats.get("funding_max_ts_ms", 0)),
            "account_target_route": True,
            "account_target_exit_request_ids": list(publication.exit_request_ids),
            "account_target_entry_request_ids": list(publication.entry_request_ids),
            "account_target_publication_error_count": len(publication.errors),
            "account_target_requests_json": json.dumps(
                account_target_requests,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        write_dataset(
            pl.DataFrame([payload], infer_schema_length=None),
            root,
            cycles_dataset,
            partition_by=(),
        )
    return PublishedTargetCyclePayload(
        payload,
        publication=publication,
        route=planning.publisher.route,
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
    suppressed = sum(
        int(payload.get(field, 0) or 0)
        for field in (
            "unresolved_exit_target_suppressions",
            "unresolved_entry_target_suppressions",
            "terminal_entry_attempt_suppressions",
            "simultaneous_exit_entry_suppressions",
        )
    )
    gross = payload.get("desired_gross_weight")
    gross_text = f"{float(gross):.3f}" if isinstance(gross, (int, float)) else "?"
    stranded = int(payload.get("stranded_zero_quantity_reservations", 0) or 0)
    # Only rendered when non-zero: a stranded reservation needs an operator.
    stranded_text = f" stranded={stranded}" if stranded else ""
    return (
        "carry target producer "
        f"id={payload.get('cycle_id', '')} mode={payload.get('mode')} "
        f"decision_day={decision_day} stale={payload.get('decision_stale')} "
        f"frozen={payload.get('decision_frozen')} "
        f"book={payload.get('desired_book_size')} gross={gross_text} "
        f"standing={payload.get('standing_symbols')} open={payload.get('open_positions')} "
        f"pub exit/entry/resize={payload.get('exit_targets_queued')}/"
        f"{payload.get('entry_targets_queued')}/{payload.get('resize_targets_queued')} "
        f"suppressed={suppressed}{stranded_text} equity={equity_text} "
        f"err={payload.get('decision_error') or 'none'}"
    )
