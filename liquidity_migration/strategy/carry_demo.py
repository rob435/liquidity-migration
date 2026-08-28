"""CARRY sleeve decision engine: the crowd-fee collector.

Computes the daily target book for the deployed carry sleeve by replaying
the registered rule (``resolve_carry_strategy_profile``; v7 by default) over
a rolling window of Bybit hourly data. The strategy logic is NOT reimplemented here: the
engine calls the exact registered-scorer functions
(:func:`liquidity_migration.rules.carry_hold.carry_hold_weights` and friends)
on the live frame (:func:`~liquidity_migration.rules.carry_hold.prepare_decision`),
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

The registered config file is loaded only so the deployed parameters are
byte-identical to the registered ones. Version selection is the
``CARRY_STRATEGY_PROFILE`` env dial → ``--strategy-profile`` (v3 → v4 promoted
2026-08-03: the toxic band's high edge moves to 0% and a crowding-persistence
size multiplier zeroes names whose recent settlements were rarely deep; v4 →
v6 promoted 2026-08-19: the flow and whale size halvings from v5 plus the
bent depth ladder, all in the shared registered scorer, so a version is a
config file plus a profile name — never a code edit. Exception again for
v7, promoted later the same day: it trades v6's registered membership file
unchanged and its first deploy carried the pre-settlement exit read below,
an execution-clock change, not a rule change).

v7's pre-settlement exit: the venue locks the upcoming crowd-fee rate just
under a minute before it pays, so inside the final minutes the public
ticker's running rate is the settled print, visible early. When a held
name's next settlement is at most 15 minutes away and that running rate is
at or above the registered −3 bp exit line, the name is sold immediately —
before the payment and the crowd's exit — instead of one minute after the
print sweeps in. The settled-print path remains as the fallback, so a
failed or missed read degrades v7 to exactly the v6 exit clock.

v5/v6's whale halving reads a SECOND venue: Binance's top-trader position
long/short ratio, the one non-Bybit input in the book. The producer keeps a
tiny per-symbol-day cache of end-of-day ratio values (the same series the
research panel attaches as ``bn_tt_ls``) and refreshes it from Binance's
public data endpoint — no key, no account, no orders. Every failure of that
feed fails OPEN per the registered rule's 48h freshness clause: a name with
no fresh ratio keeps full size, and a dead feed degrades v6 toward v6-minus-
whale rather than blocking a decision.
"""

from __future__ import annotations

import dataclasses
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
from liquidity_migration.core.artifact_snapshot import read_stable_file
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.core.durable_file import durable_atomic_replace
from liquidity_migration.rules.engine_targets import (
    EngineTarget,
    PublishedTargetBook,
    publish_target_book,
    read_target_book,
    render_target_book,
    write_target_book,
)
from liquidity_migration.strategy.account_candidate_universe import (
    carry_profile_universe_inputs,
    load_candidate_universe,
    require_profile_binding,
)
from liquidity_migration.marketdata.binance import BinanceDataError, BinanceUSDMData
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
from liquidity_migration.policy.execution_environment import (
    ExecutionEnvironment,
    candidate_universe_realm,
    execution_environment,
)
from liquidity_migration.rules.carry_hold import (
    CarryHoldConfig,
    carry_hold_weights,
    daily_grid,
    prepare_decision,
    top_n_universe,
)
from liquidity_migration.rules.exodus_short import (
    ExodusShortConfig,
    ExodusShortRecord,
    next_cover_deadline_ts_ms,
    records_from_payload,
    records_to_payload,
    render_exodus_book,
    split_due_covers,
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

#: Replay window. Hard floor: ~32d for the 30d vol filter's warm-up + 7d
#: maturity; 19d longest-ever spell. 90d keeps every input saturated with
#: wide margin while staying a trivial recompute (~1M rows).
REPLAY_DAYS = 90
MIN_REPLAY_DAYS = 45

#: Minimum universe symbols on the decision bar. Below this the data build is
#: broken and the engine fails closed, holding the previous targets, rather than
#: flattening a healthy book on a data hole. The real universe is 100 names.
MIN_DECISION_SYMBOLS = 50

_CONFIGS_DIR = Path(__file__).resolve().parents[2] / "configs"
#: The DEFAULT deployed rule file — what envelope proofs and research charts
#: read when no profile is named. The running producer resolves its own file
#: through ``resolve_carry_strategy_profile``.
CARRY_CONFIG_PATH = _CONFIGS_DIR / "lane2_carry_hold_v7.json"

#: Registered CARRY deployments, selectable per unit exactly like LONG's
#: (``CARRY_STRATEGY_PROFILE`` env → ``--strategy-profile``). Switching
#: versions is an env change plus a registered config file — never a code
#: edit.
CARRY_STRATEGY_PROFILE_CHOICES: tuple[str, ...] = ("v3", "v4", "v6", "v7")
DEFAULT_CARRY_STRATEGY_PROFILE = "v7"


@dataclasses.dataclass(frozen=True, slots=True)
class CarryStrategyProfile:
    """One registered CARRY deployment: journaled profile name + rule file.

    ``presettle_exit`` is an EXECUTION-CLOCK switch, not a rule change: v7
    reads the rule file unchanged (its forward grading continues unbroken)
    and only moves the early-exit sell from the settled print to the venue's
    pre-settlement running rate. The rule file is now ``lane2_carry_hold_v7``
    (renamed from v6 on 2026-08-26); both the settled-print and pre-settle
    clocks read it, differing only in the clock switch.
    """

    profile_name: str
    config_path: Path
    presettle_exit: bool = False


_CARRY_STRATEGY_PROFILES: dict[str, CarryStrategyProfile] = {
    "v3": CarryStrategyProfile("carry_hold_v3_live_v1", _CONFIGS_DIR / "lane2_carry_hold_v3.json"),
    "v4": CarryStrategyProfile("carry_hold_v4_live_v1", _CONFIGS_DIR / "lane2_carry_hold_v4.json"),
    "v6": CarryStrategyProfile("carry_hold_v6_live_v1", _CONFIGS_DIR / "lane2_carry_hold_v7.json"),
    "v7": CarryStrategyProfile(
        "carry_hold_v7_live_v1",
        _CONFIGS_DIR / "lane2_carry_hold_v7.json",
        presettle_exit=True,
    ),
}


def resolve_carry_strategy_profile(name: str) -> CarryStrategyProfile:
    """Resolve a registered CARRY profile; unknown names fail startup."""
    try:
        return _CARRY_STRATEGY_PROFILES[name]
    except KeyError:
        raise ValueError(
            f"unknown CARRY strategy profile {name!r}; "
            f"supported: {', '.join(CARRY_STRATEGY_PROFILE_CHOICES)}"
        ) from None


class CarrySleeveError(RuntimeError):
    """Raised when the carry decision cannot be produced safely."""


def load_carry_config(path: Path | None = None) -> CarryHoldConfig:
    """The registered rule parameters, byte-identical to the Lane-2 file."""
    return CarryHoldConfig.from_json(str(path or CARRY_CONFIG_PATH))


#: Per-process registered-rule memo for the cycle path; see ``_registered_rule``.
_REGISTERED_RULE_CACHE: dict[str, CarryHoldConfig] = {}


def _registered_rule(config_path: Path) -> CarryHoldConfig:
    """The cycle path's registered rule, parsed once per process per file.

    Never invalidated on purpose: registered rule files are immutable once
    committed, and changing the deployed rule already requires a producer
    restart operationally (the profile dial is read at startup), so a disk
    re-parse every 60-second cycle bought nothing. ``CarryHoldConfig`` is a
    frozen dataclass, so sharing one instance across cycles is safe.
    """

    key = str(config_path)
    rule = _REGISTERED_RULE_CACHE.get(key)
    if rule is None:
        rule = load_carry_config(config_path)
        _REGISTERED_RULE_CACHE[key] = rule
    return rule


@dataclasses.dataclass(frozen=True)
class CarryDecision:
    """One daily decision: the target weight book at ``decision_ts_ms``."""

    decision_ts_ms: int
    weights: dict[str, float]
    universe_size: int
    replay_days: int
    gross: float


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
ENGINE_EXODUS_SLEEVE = "exodus"
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

#: How long before the decision deadline a cycle may compute and freeze the
#: upcoming day's book. The window sits entirely inside the 20-minute kline
#: lag, so every input row for the new decision bar is already public and
#: cached when it opens; one 60-second grid cycle always lands in
#: 90 seconds, which is what lets the deadline wake publish instead of compute.
FREEZE_AHEAD_WINDOW_MS = 90 * 1000
#: Entry-signal validity. A new name not admitted within six hours belongs to
#: a stale decision and must wait for the next daily book.
SIGNAL_VALIDITY_MS = 6 * HOUR_MS
#: Producer-side guard band before ``signal_valid_until_ms``. The engine's own
#: stale-entry cutoff is stricter; this prevents adding a name
#: to a producer book that is already too old to act on.
ENTRY_PUBLISH_GUARD_MS = 15 * 60 * 1000
#: Where to write the decided book for the Rust execution engine to follow.
#: Set on the fleet's units: the engine owns the account and this book is how
#: a carry decision reaches it. It is mandatory for every cycle.
ENGINE_TARGET_BOOK_PATH_ENV = "CARRY_ENGINE_TARGET_BOOK_PATH"
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
#: Entries below this notional could quantize to zero venue quantity and come
#: back as a terminal (permanently suppressing) rejection; skip them instead.
#: The venue's own floor is 5 USDT per order and the kernel enforces the exact
#: per-symbol rule (min qty, min notional, step rounding), so this is only a
#: coarse pre-filter with headroom over 5 — not a second safety margin. At
#: 10.0 it silently blanked a small account: the funded book missed both its
#: entries at 0.1 x 99.94 = 9.99 USDT, six cents under.
ENTRY_MIN_NOTIONAL_USDT = 6.0
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
    against.

    Losing this object (restart, ``--once``) costs one extra funding sweep, one
    cycles-dataset read, and one re-anchor of the sizing equity to the current
    mark. The re-anchor can move the day's targets by
    however much equity moved since the decision; the resize dead-band absorbs
    that unless the move is large.
    """

    __slots__ = (
        "frozen_ahead_bar_ts_ms",
        "frozen_decisions",
        "funding_swept_hour_ts",
        "last_successful_decision_ts_ms",
        "sizing_equity_by_decision",
        "sizing_equity_usdt",
        "sizing_equity_decision_ts_ms",
        "sizing_anchor_path",
        "early_exits",
        "drop_exits_logged",
        "exodus_shorts",
        "whale_last_attempt_ms",
        "whale_store",
    )

    def __init__(self) -> None:
        # Keyed by decision bar; holds the two newest bars because the
        # freeze-ahead path pins TOMORROW's book while cycles before the
        # boundary still serve TODAY's. A single slot made those two freezes
        # evict each other, recomputing both once a minute.
        self.frozen_decisions: dict[int, tuple[CarryDecision, dict[str, float], int]] = {}
        self.frozen_ahead_bar_ts_ms: int | None = None
        self.funding_swept_hour_ts: int | None = None
        self.last_successful_decision_ts_ms: int | None = None
        # Sizing anchors keyed by decision bar, two-day retention for the same
        # reason as ``frozen_decisions``: the freeze-ahead pass anchors
        # TOMORROW's equity while cycles before the boundary still size
        # TODAY's book, and a single slot made each side clobber the other.
        self.sizing_equity_by_decision: dict[int, float] = {}
        self.sizing_equity_usdt: float | None = None
        self.sizing_equity_decision_ts_ms: int | None = None
        self.sizing_anchor_path: Path | None = None
        # Whale-ratio cache (v5/v6 rules only): the in-memory copy of the
        # on-disk per-symbol-day store, and the last refresh attempt so a
        # Binance outage retries on a cooldown instead of every 60s cycle.
        self.whale_store: pl.DataFrame | None = None
        self.whale_last_attempt_ms: int | None = None
        # Early-exit mask: symbol -> the decision bar it fired under. None
        # until first use, then mirrors the on-disk state file.
        self.early_exits: dict[str, int] | None = None
        # Drop-exit logging guard: the names the upcoming decision
        # zeroed and this process already announced. The mask itself is
        # re-derived every cycle from the two frozen books, so losing this
        # only repeats a log line.
        self.drop_exits_logged: frozenset[str] = frozenset()
        # Open exodus shorts. None until first use, then mirrors the on-disk
        # state file; losing it re-loads from disk, and a lost FILE covers
        # every open short (absence from the book is the exit).
        self.exodus_shorts: list[ExodusShortRecord] | None = None

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

        return self.frozen_decisions.get(int(decision_ts_ms))

    def freeze_decision(
        self,
        *,
        decision_ts_ms: int,
        decision: CarryDecision,
        trail_by_symbol: dict[str, float],
        universe_eligible: int,
        frozen_ahead: bool = False,
    ) -> None:
        """Pin this bar's book. Older bars age out two freezes later."""

        self.frozen_decisions[int(decision_ts_ms)] = (
            decision,
            dict(trail_by_symbol),
            int(universe_eligible),
        )
        while len(self.frozen_decisions) > 2:
            del self.frozen_decisions[min(self.frozen_decisions)]
        if frozen_ahead:
            self.frozen_ahead_bar_ts_ms = int(decision_ts_ms)

    def sizing_equity(self, *, decision_ts_ms: int, equity_usdt: float) -> float:
        """Equity as of when this decision was first sized, not the live mark.

        CARRY decides once a day and holds. Sizing off the live mark every cycle
        makes the day's targets a function of the book's own unrealized P&L, and
        that feedback has a direction: equity rises because the longs rose, so
        the target rises and the sleeve buys after the move, and sells after a
        fall. Anchoring to the decision keeps intraday targets constant, so only
        a new decision moves the book. The disaster stop and native protection
        remain the capital-preservation path.

        The first usable call for a decision bar sets that bar's anchor, and
        it happens ~90 seconds BEFORE the boundary by design: the day's equity is the
        freeze-time mark, not the boundary-time mark, and the resize dead-band
        absorbs the drift between the two. Anchors keep two-day retention so
        pre-boundary cycles sizing TODAY cannot evict TOMORROW's freeze-time
        anchor (or the reverse). Losing the state object re-anchors to the
        current mark, as before.

        An unusable equity read (``<= 0``) passes through unanchored: callers
        already refuse to size on it, and anchoring it would outlive the failure.
        """

        if equity_usdt <= 0.0:
            return equity_usdt
        key = int(decision_ts_ms)
        anchored = self.sizing_equity_by_decision.get(key)
        if anchored is None or anchored <= 0.0:
            anchored = float(equity_usdt)
            next_anchors = dict(self.sizing_equity_by_decision)
            next_anchors[key] = anchored
            while len(next_anchors) > 2:
                del next_anchors[min(next_anchors)]
            self._persist_sizing_anchors(next_anchors)
            self.sizing_equity_by_decision = next_anchors
        self.sizing_equity_decision_ts_ms = key
        self.sizing_equity_usdt = float(anchored)
        return float(anchored)

    def bind_sizing_anchors(self, root: Path) -> None:
        """Load the durable per-decision sizing anchors once per daemon."""

        path = root / ".cache" / "carry_sizing_anchors.json"
        if self.sizing_anchor_path is not None:
            if self.sizing_anchor_path != path:
                raise RuntimeError("CarryCycleState cannot span two data roots")
            return
        self.sizing_anchor_path = path
        try:
            path.lstat()
        except FileNotFoundError:
            return
        snapshot = read_stable_file(
            path,
            label="CARRY sizing anchors",
            reject_empty=True,
            require_single_link=True,
            max_bytes=16 * 1024,
        )
        try:
            payload = json.loads(snapshot.data)
        except json.JSONDecodeError as exc:
            raise ValueError(f"CARRY sizing anchors are not JSON: {exc}") from exc
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "anchors"}:
            raise ValueError("CARRY sizing anchors have invalid fields")
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != 1
            or not isinstance(payload["anchors"], dict)
        ):
            raise ValueError("CARRY sizing anchors have an unsupported schema")
        loaded: dict[int, float] = {}
        for raw_key, raw_value in payload["anchors"].items():
            if (
                not isinstance(raw_key, str)
                or not raw_key.isascii()
                or not raw_key.isdigit()
                or raw_key.startswith("0")
                or isinstance(raw_value, bool)
                or not isinstance(raw_value, (int, float))
            ):
                raise ValueError("CARRY sizing anchors contain an invalid value")
            key = int(raw_key)
            value = float(raw_value)
            if key <= 0 or not math.isfinite(value) or value <= 0.0:
                raise ValueError("CARRY sizing anchors contain an invalid value")
            loaded[key] = value
        if len(loaded) > 2:
            raise ValueError("CARRY sizing anchors retain more than two decisions")
        self.sizing_equity_by_decision = loaded

    def _persist_sizing_anchors(self, anchors: Mapping[int, float]) -> None:
        path = self.sizing_anchor_path
        if path is None:
            return
        durable_atomic_replace(
            path,
            canonical_json(
                {
                    "schema_version": 1,
                    "anchors": {str(key): value for key, value in sorted(anchors.items())},
                }
            )
            + b"\n",
            label="CARRY sizing anchors",
        )


@dataclasses.dataclass(frozen=True, slots=True)
class CarryDemoCycleConfig:
    """CARRY demo/mainnet target-producer configuration.

    Sizing fields (``notional_multiplier``, ``entry_leverage``,
    ``declared_stop_loss_fraction``, ``max_new_entries_per_cycle``) are injected
    from the operational profile's ``carry`` block by the CLI; rule parameters
    stay in the registered config the engine loads. The ``ws_klines_*`` block
    configures carry's WS kline plane, on by default: the daemon installs a
    carry-scoped kline stream manager (``carry_demo_daemon``) that serves the
    cycle's close-keyed bars, with REST as the fallback. Settled funding stays
    REST-only because the venue publishes no funding stream.
    """

    # --- environment / wiring ---
    execution_environment: str = ""
    candidate_universe_file: str = ""
    #: Registered deployment version (``resolve_carry_strategy_profile``).
    strategy_profile: str = DEFAULT_CARRY_STRATEGY_PROFILE
    #: Sell an exiting name at the settled print that ends it instead of the
    #: next midnight (``CARRY_EARLY_EXIT`` on the units). Off by default so
    #: ad-hoc runs replay the registered clock.
    early_exit_enabled: bool = False
    # --- sizing (operational profile carry block) ---
    notional_multiplier: float = 1.0
    entry_leverage: float = 2.0
    declared_stop_loss_fraction: float = 0.35
    max_new_entries_per_cycle: int = 10
    #: Ceiling on the equity this producer may size against, from the profile's
    #: ``capital_reference_usdt``. The engine's pre-trade caps are absolute USDT
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
    """Validate target routing and sizing before any shared resource opens."""

    execution_environment(config.execution_environment)
    resolve_carry_strategy_profile(config.strategy_profile)
    if not os.environ.get(ENGINE_TARGET_BOOK_PATH_ENV, "").strip():
        raise ValueError(f"{ENGINE_TARGET_BOOK_PATH_ENV} must name the Rust engine target book")
    if bool(getattr(config, "telegram", False)):
        raise ValueError("strategy producers do not own Telegram controls")
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
# Whale-ratio feed (v5/v6 rules only): Binance top-trader position long/short
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


def _whale_missing_pairs(
    store: pl.DataFrame, symbols: list[str], now_ms: int
) -> list[tuple[str, int]]:
    newest_end = (int(now_ms) // DAY_MS) * DAY_MS
    wanted_ends = [newest_end - k * DAY_MS for k in range(WHALE_FEED_DAYS)]
    have: set[tuple[str, int]] = set()
    if store.height:
        have = set(
            zip(store["symbol"].to_list(), store["day_end_ms"].to_list(), strict=True)
        )
    return [(s, e) for s in symbols for e in wanted_ends if (s, e) not in have]


def _fetch_whale_pair(
    symbol: str, day_end_ms: int, client_factory: Any
) -> tuple[str, int, float | None] | None:
    """One (symbol, day) EOD read: the last 5-minute ratio print of the day,
    the same value ``refresh_binance_metrics.py`` collapses to ``tt_ls_eod``.

    ``None`` = transient failure, nothing recorded, retried on a later pass.
    A tuple with a null value = the venue definitively has nothing here.
    """
    client = client_factory()
    try:
        rows = client.get_top_trader_ls_position_ratio(
            symbol, "5m", int(day_end_ms) - 6 * HOUR_MS, int(day_end_ms)
        )
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
            futures = [
                pool.submit(_fetch_whale_pair, sym, end, factory) for sym, end in missing
            ]
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

_EARLY_EXIT_STATE_NAME = "carry_early_exits.json"


def _early_exit_state_path(root: Path) -> Path:
    return root / _EARLY_EXIT_STATE_NAME


def _load_early_exits(root: Path) -> dict[str, int]:
    path = _early_exit_state_path(root)
    try:
        path.lstat()
    except FileNotFoundError:
        return {}
    snapshot = read_stable_file(
        path,
        label="CARRY early-exit state",
        reject_empty=True,
        require_single_link=True,
        max_bytes=1024 * 1024,
    )
    raw = json.loads(snapshot.data)
    if not isinstance(raw, dict) or set(raw) != {"fired"} or not isinstance(raw["fired"], dict):
        raise ValueError("CARRY early-exit state has invalid fields")
    fired: dict[str, int] = {}
    for symbol, ts in raw["fired"].items():
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol != symbol.upper()
            or not symbol.isalnum()
            or isinstance(ts, bool)
            or not isinstance(ts, int)
            or ts <= 0
        ):
            raise ValueError("CARRY early-exit state contains an invalid row")
        fired[symbol] = ts
    return fired


def _save_early_exits(root: Path, fired: dict[str, int]) -> None:
    path = _early_exit_state_path(root)
    durable_atomic_replace(
        path,
        canonical_json({"fired": dict(sorted(fired.items()))}) + b"\n",
        label="CARRY early-exit state",
    )


def _apply_early_exits(
    *,
    decision: CarryDecision,
    rule: CarryHoldConfig,
    funding: pl.DataFrame | None,
    state: CarryCycleState,
    root: Path,
    now_ms: int,
) -> tuple[CarryDecision, list[str]]:
    """Mask early-exited names out of the desired book; detect new fires.

    Detection is the registered exit test verbatim: the latest settled print
    for a desired name, newer than the decision bar, at or above
    ``-exit_bp`` (``not (fv < -exit_)`` in the state machine). Held names
    always carry a below-threshold print at the decision bar, so any firing
    print is by construction a post-decision settlement. ``funding`` is this
    cycle's swept cache; ``None`` (build-skipping wakes) masks only.
    """

    if state.early_exits is None:
        state.early_exits = _load_early_exits(root)
    # Masks from older decision bars expire with their day.
    fired = {
        s: ts for s, ts in state.early_exits.items() if ts == decision.decision_ts_ms
    }
    new_fires: list[str] = []
    if funding is not None and not funding.is_empty() and decision.weights:
        exit_thr = -(rule.exit_bp / 1e4)
        latest = (
            funding.filter(
                pl.col("symbol").is_in(sorted(decision.weights))
                & (pl.col("funding_ts_ms") > decision.decision_ts_ms)
                & (pl.col("funding_ts_ms") <= int(now_ms))
            )
            .sort("funding_ts_ms")
            .group_by("symbol")
            .agg(pl.col("funding_rate").last().alias("rate"))
        )
        for row in latest.iter_rows(named=True):
            sym = str(row["symbol"])
            rate = row["rate"]
            if sym in fired or rate is None:
                continue
            if not (float(rate) < exit_thr):
                fired[sym] = decision.decision_ts_ms
                new_fires.append(sym)
    if fired != state.early_exits:
        _save_early_exits(root, fired)
        state.early_exits = fired
    if not fired:
        return decision, new_fires
    masked = {s: w for s, w in decision.weights.items() if s not in fired}
    return (
        dataclasses.replace(
            decision, weights=masked, gross=sum(masked.values())
        ),
        new_fires,
    )


# --- the v7 pre-settlement exit read ---------------------------------------
# Bybit locks the upcoming crowd-fee rate just under a minute before it pays
# (tardis tick evidence, 2026-08-19), so inside the last minutes the ticker's
# running rate IS the print, visible early. v7 fires the same registered exit
# test on that read up to 15 minutes ahead and sells before the post-payment
# dump instead of one minute into it. Window and margin (15 min, none) are the
# measured optimum; the settled-print path stays as the fallback, so a missed
# or failed read costs nothing against the v6 clock.

_PRESETTLE_WINDOW_MS = 15 * 60_000
#: Fetch gate slack: every Bybit settlement sits on an hour boundary, so the
#: batch read only runs when one is at most window+slack away.
_PRESETTLE_FETCH_SLACK_MS = 90_000


def _presettle_ticker_factory() -> BybitMarketData:
    # Public mainnet tickers; demo trading runs on mainnet market data.
    return BybitMarketData(category="linear", retries=2, retry_sleep_seconds=0.25)


def _fetch_presettle_tickers(
    symbols: list[str],
    client_factory: Any = None,
) -> tuple[dict[str, tuple[float, int, float | None]], str]:
    """One batch ticker read: symbol -> (rate, next pay time ms, mark price).

    Never raises; a failed read returns an empty map plus the error text and
    the cycle falls back to the settled-print clock.
    """

    try:
        rows = (client_factory or _presettle_ticker_factory)().get_tickers()
    except Exception as exc:  # noqa: BLE001 - fail open to the settled-print clock
        return {}, str(exc)[:200]
    want = set(symbols)
    out: dict[str, tuple[float, int, float | None]] = {}
    for row in rows:
        sym = str(row.get("symbol", ""))
        if sym not in want:
            continue
        try:
            rate = float(row["fundingRate"])
            next_pay_ms = int(row["nextFundingTime"])
        except (KeyError, TypeError, ValueError):
            continue
        mark_px: float | None
        try:
            parsed_mark_px = float(row["markPrice"])
            mark_px = (
                parsed_mark_px
                if math.isfinite(parsed_mark_px) and parsed_mark_px > 0.0
                else None
            )
        except (KeyError, TypeError, ValueError):
            mark_px = None
        out[sym] = (rate, next_pay_ms, mark_px)
    return out, ""


@dataclasses.dataclass(frozen=True)
class PresettleFire:
    """One fire and its contemporaneous mark, captured before masking."""

    symbol: str
    settlement_ts_ms: int
    mark_px: float | None


def _apply_presettle_exits(
    *,
    decision: CarryDecision,
    rule: CarryHoldConfig,
    state: CarryCycleState,
    root: Path,
    now_ms: int,
    tickers: Mapping[str, tuple[float, int, float | None]],
) -> tuple[CarryDecision, list[str], list[PresettleFire]]:
    """Fire the registered exit test on the pre-settlement running rate.

    Runs after :func:`_apply_early_exits` (which loads and day-filters the
    shared mask). A name fires when its next settlement is inside the window
    and the running rate is at or above ``-exit_bp`` — the identical boundary
    the settled-print path uses, read minutes before the print exists.
    """

    fired = dict(state.early_exits or {})
    new_fires: list[str] = []
    fire_details: list[PresettleFire] = []
    exit_thr = -(rule.exit_bp / 1e4)
    for sym in sorted(decision.weights):
        if sym in fired:
            continue
        info = tickers.get(sym)
        if info is None:
            continue
        rate, next_pay_ms, mark_px = info
        lead_ms = int(next_pay_ms) - int(now_ms)
        if not (0 < lead_ms <= _PRESETTLE_WINDOW_MS):
            continue
        if not (float(rate) < exit_thr):
            fired[sym] = decision.decision_ts_ms
            new_fires.append(sym)
            fire_details.append(
                PresettleFire(
                    symbol=sym,
                    settlement_ts_ms=int(next_pay_ms),
                    mark_px=mark_px,
                )
            )
    if new_fires:
        state.early_exits = fired
        try:
            _save_early_exits(root, fired)
        except Exception:  # noqa: BLE001 - a lost mask re-buys once at worst
            _logger.warning("early-exit state not persisted; mask is memory-only")
    if not fired:
        return decision, new_fires, fire_details
    masked = {s: w for s, w in decision.weights.items() if s not in fired}
    return (
        dataclasses.replace(decision, weights=masked, gross=sum(masked.values())),
        new_fires,
        fire_details,
    )


def _apply_drop_exits(
    *,
    decision: CarryDecision,
    state: CarryCycleState,
) -> tuple[CarryDecision, list[str], int]:
    """Mask the names the UPCOMING frozen decision zeroes out of this book.

    Leg B of the two-leg exit clock: run pre-flip against the served old-day
    decision, it lets those exits publish at the first post-midnight cycle
    instead of the 00:20 clock, ahead of the measured post-settlement drift.
    A name still desired at any weight is a resize, not a drop, and waits
    for the flip. The exodus sleeve does NOT take these over: its registered
    trigger is the fee-recovery fire, never a membership drop. Idempotent
    across cycles — both books are frozen, so the drop set cannot drift.
    """

    upcoming = state.frozen_decision(decision.decision_ts_ms + DAY_MS)
    if upcoming is None:
        return decision, [], 0
    upcoming_weights = upcoming[0].weights
    dropped = sorted(s for s in decision.weights if s not in upcoming_weights)
    if not dropped:
        return decision, [], 0
    masked = {
        s: w for s, w in decision.weights.items() if s in upcoming_weights
    }
    return (
        dataclasses.replace(decision, weights=masked, gross=sum(masked.values())),
        dropped,
        len(dropped),
    )


# --- the exodus short (lane2_exodus_short_v1) -------------------------------
# A standalone sleeve at the engine (its own [[strategy]] block, book file,
# and fill attribution) produced from inside this process, because its whole
# trigger is the fire above: when carry abandons a dying name, take the same
# position over as a short and cover 60 minutes after the settlement. The
# rules module owns the decision surface; this section owns wiring only.

_EXODUS_STATE_NAME = "exodus_shorts.json"
#: Book file the engine's exodus follower reads. Absent = this unit does not
#: publish the sleeve; same convention as CARRY_ENGINE_TARGET_BOOK_PATH.
EXODUS_TARGET_BOOK_PATH_ENV = "EXODUS_ENGINE_TARGET_BOOK_PATH"
#: Registered exodus config dial. Absent or empty = the sleeve is OFF: no new
#: entries, and any state drains to a flat book. Same env->registry shape as
#: CARRY_STRATEGY_PROFILE, read here because the sleeve lives in this process.
EXODUS_PROFILE_ENV = "EXODUS_SHORT_PROFILE"
_EXODUS_PROFILES: dict[str, Path] = {
    "v1": _CONFIGS_DIR / "lane2_exodus_short_v1.json",
}
_EXODUS_BOOK_SOURCE = "exodus_short"
_exodus_rule_cache: dict[str, ExodusShortConfig] = {}


def _registered_exodus_rule(profile_name: str) -> ExodusShortConfig:
    # Parsed once per process, like the carry rule: registered files are
    # immutable once committed.
    cached = _exodus_rule_cache.get(profile_name)
    if cached is None:
        cached = ExodusShortConfig.from_json(_EXODUS_PROFILES[profile_name])
        _exodus_rule_cache[profile_name] = cached
    return cached


def _exodus_state_path(root: Path) -> Path:
    return root / _EXODUS_STATE_NAME


def _load_exodus_shorts(root: Path) -> list[ExodusShortRecord]:
    path = _exodus_state_path(root)
    try:
        path.lstat()
    except FileNotFoundError:
        return []
    snapshot = read_stable_file(
        path,
        label="exodus-short state",
        reject_empty=True,
        require_single_link=True,
        max_bytes=1024 * 1024,
    )
    return records_from_payload(json.loads(snapshot.data))


def _save_exodus_shorts(root: Path, records: list[ExodusShortRecord]) -> None:
    path = _exodus_state_path(root)
    durable_atomic_replace(
        path,
        canonical_json(records_to_payload(records)) + b"\n",
        label="exodus-short state",
    )


def _run_exodus_short(
    *,
    state: CarryCycleState,
    root: Path,
    fires: list[PresettleFire],
    carry_holdings: Mapping[str, tuple[str, float, float]] | None,
    entry_leverage: float,
    now_ms: int,
    exodus_held_symbols: frozenset[str] | None = None,
    exodus_working_entry_symbols: frozenset[str] | None = None,
) -> dict[str, Any]:
    """One exodus pass per carry cycle: open on this cycle's fires, cover on
    the clock, publish the book. Runs on EVERY cycle — covers must drain even
    when the carry decision is unavailable, so nothing here depends on it.

    ``carry_holdings`` is the fresh engine account reading taken after the
    fire. A fire without a positive carry-attributed quantity and a valid
    contemporaneous mark is blocked rather than inventing a target.

    The two Exodus symbol readings come from the engine. Unknown holdings or
    unfinished entries retain due state and keep publishing the named cover.
    """

    profile_name = os.environ.get(EXODUS_PROFILE_ENV, "").strip()
    book_path_text = os.environ.get(EXODUS_TARGET_BOOK_PATH_ENV, "").strip()
    if not profile_name and not book_path_text:
        return {}
    receipt: dict[str, Any] = {
        "exodus_enabled": bool(profile_name),
        "exodus_opened": [],
        "exodus_covered": [],
        "exodus_entry_blocked": [],
        "exodus_open_names": 0,
        "exodus_error": "",
    }
    try:
        if profile_name and profile_name not in _EXODUS_PROFILES:
            receipt["exodus_error"] = f"unknown exodus profile {profile_name!r}"
            _logger.error("unknown %s=%r; exodus sleeve inert", EXODUS_PROFILE_ENV, profile_name)
            return receipt
        if state.exodus_shorts is None:
            state.exodus_shorts = _load_exodus_shorts(root)
        records = list(state.exodus_shorts)
        if not profile_name:
            active: list[ExodusShortRecord] = []
            covered = records
            cfg = None
        else:
            cfg = _registered_exodus_rule(profile_name)
            active, covered = split_due_covers(records, now_ms=now_ms, cfg=cfg)
            open_symbols = {r.symbol for r in records}
            for fire in fires:
                if fire.symbol in open_symbols:
                    continue
                holding = carry_holdings.get(fire.symbol) if carry_holdings is not None else None
                if holding is None:
                    receipt["exodus_entry_blocked"].append(fire.symbol)
                    continue
                side, qty, _entry_px = holding
                if (
                    str(side).lower() != "long"
                    or not math.isfinite(float(qty))
                    or float(qty) <= 0.0
                    or fire.mark_px is None
                    or not math.isfinite(float(fire.mark_px))
                    or float(fire.mark_px) <= 0.0
                ):
                    receipt["exodus_entry_blocked"].append(fire.symbol)
                    continue
                target_qty = abs(float(qty))
                notional = target_qty * float(fire.mark_px)
                active.append(
                    ExodusShortRecord(
                        symbol=fire.symbol,
                        notional_usdt=notional,
                        settlement_ts_ms=fire.settlement_ts_ms,
                        fired_ts_ms=now_ms,
                        target_qty=target_qty,
                    )
                )
                open_symbols.add(fire.symbol)
                receipt["exodus_opened"].append(fire.symbol)
        receipt["exodus_covered"] = sorted(r.symbol for r in covered)
        receipt["exodus_open_names"] = len(active)

        # New exposure reaches durable state before the engine can see it.
        # Cover state moves in the opposite order: its explicit zero target is
        # published first, then a flat engine reading permits deletion.
        opened = [r for r in active if r not in records]
        durable_records = records
        if opened:
            durable_records = sorted(records + opened, key=lambda record: record.symbol)
            _save_exodus_shorts(root, durable_records)
            state.exodus_shorts = durable_records
        if receipt["exodus_opened"]:
            _logger.info(
                "exodus short OPENED: %s (cover %d min after settlement)",
                ",".join(receipt["exodus_opened"]),
                cfg.cover_minutes_after_settlement if cfg else 0,
            )
        if receipt["exodus_covered"]:
            _logger.info("exodus short covering: %s", ",".join(receipt["exodus_covered"]))
        if book_path_text:
            render_cfg = cfg or _registered_exodus_rule("v1")
            text = render_exodus_book(
                active,
                cfg=render_cfg,
                now_ms=now_ms,
                source=_EXODUS_BOOK_SOURCE,
                entry_leverage=entry_leverage,
                cover_records=covered,
            )
            write_target_book(Path(book_path_text), text)

            pending_covers = [
                record
                for record in covered
                if exodus_held_symbols is None
                or exodus_working_entry_symbols is None
                or record.symbol in exodus_held_symbols
                or record.symbol in exodus_working_entry_symbols
            ]
            final_records = sorted(
                active + pending_covers, key=lambda record: record.symbol
            )
            if final_records != durable_records:
                _save_exodus_shorts(root, final_records)
            state.exodus_shorts = final_records
        if cfg is not None and active:
            deadline = next_cover_deadline_ts_ms(active, cfg)
            if deadline is not None:
                receipt["exodus_next_cover_ts_ms"] = deadline
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never stop the carry sleeve
        receipt["exodus_error"] = f"{type(exc).__name__}: {exc}"[:300]
        _logger.exception("exodus short pass failed; carry cycle continues")
    return receipt


def _attach_whale_columns(
    view: pl.DataFrame, whale_events: pl.DataFrame | None
) -> pl.DataFrame:
    """Attach ``bn_tt_ls`` / ``bn_tt_ls_age_h`` exactly the way the research
    panel does — backward as-of of day-end EOD events per symbol, age in
    float hours — so the registered rule computes the whale change from the
    same shape live. ``None`` (a rule with no whale leg) leaves the frame
    untouched: the v1..v4 view stays bit-identical to before the feed existed.
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
        .with_columns(
            ((pl.col("bar_ts_ms") - pl.col("_tt_ls_ts_ms")) / HOUR_MS).alias(
                "bn_tt_ls_age_h"
            )
        )
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
    ).with_columns(
        ((pl.col("bar_ts_ms") - pl.col("funding_ts_ms")) / HOUR_MS).alias(
            "by_funding_age_h"
        )
    )
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
        universe_eligible = (
            int(view.get_column("symbol").n_unique()) if not view.is_empty() else 0
        )
        _validate_carry_view_health(
            view, decision_ts_ms=ahead_ts_ms, standing_symbols=standing_symbols
        )
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
    entry_cap_deferrals: int
    entry_validity_expired_skips: int
    entry_dust_skips: int
    engine_blocked_entries: int
    entry_blocked_reason: str
    book_written: bool
    target_book_object_path: str


def _empty_carry_plan(*, entry_blocked_reason: str = "") -> CarryTargetPlan:
    return CarryTargetPlan(
        desired_book_size=0,
        desired_gross_weight=0.0,
        planned_exits=0,
        planned_entries=0,
        planned_resizes=0,
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
    desired: Mapping[str, float],
    decision_ts_ms: int,
    sizing_equity_usdt: float,
    notional_multiplier: float,
    stop_loss_fraction: float,
    entry_leverage: float,
    strategy_profile: str,
) -> PublishedTargetBook:
    """Durably publish one decided absolute book to the Rust engine."""
    path_text = os.environ.get(ENGINE_TARGET_BOOK_PATH_ENV, "").strip()
    if not path_text:
        raise ValueError(f"{ENGINE_TARGET_BOOK_PATH_ENV} must name the Rust target book")
    if not sizing_equity_usdt > 0.0:
        raise ValueError("cannot write a target book without positive sizing equity")
    targets = [
        EngineTarget(
            symbol=symbol,
            notional_usdt=float(weight) * sizing_equity_usdt * notional_multiplier,
            stop_loss_fraction=stop_loss_fraction,
            leverage=entry_leverage,
        )
        for symbol, weight in sorted(desired.items())
    ]
    return publish_target_book(
        Path(path_text),
        render_target_book(
            source=strategy_profile,
            decision_ts_ms=decision_ts_ms,
            valid_until_ms=decision_ts_ms + SIGNAL_VALIDITY_MS,
            targets=targets,
        ),
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
    cycle_now_ms: int,
    cycle_state: CarryCycleState | None = None,
) -> CarryTargetPlan:
    """Build and publish the paced absolute book for one decision."""

    if decision is None:
        return _empty_carry_plan(entry_blocked_reason="decision_unavailable")

    decision_ts_ms = decision.decision_ts_ms
    desired = decision.weights
    entry_health_ok = not engine_account_health_error and equity_usdt > 0.0
    entry_blocked_reason = "" if entry_health_ok else "engine_account_health_unavailable"
    if not entry_health_ok:
        path_text = os.environ.get(ENGINE_TARGET_BOOK_PATH_ENV, "").strip()
        if not path_text:
            raise ValueError(f"{ENGINE_TARGET_BOOK_PATH_ENV} must name the Rust target book")
        try:
            previous = read_target_book(path_text)
        except (OSError, RuntimeError, ValueError):
            return CarryTargetPlan(
                desired_book_size=len(desired),
                desired_gross_weight=float(decision.gross),
                planned_exits=0,
                planned_entries=0,
                planned_resizes=0,
                entry_cap_deferrals=0,
                entry_validity_expired_skips=0,
                entry_dust_skips=0,
                engine_blocked_entries=0,
                entry_blocked_reason=entry_blocked_reason,
                book_written=False,
                target_book_object_path="",
            )
        if previous.source != str(demo.strategy_profile):
            raise ValueError(
                f"active target book source {previous.source!r} does not match "
                f"CARRY profile {demo.strategy_profile!r}"
            )
        retained = [target for target in previous.targets if target.symbol in desired]
        planned_exits = len(previous.targets) - len(retained)
        if planned_exits <= 0:
            return CarryTargetPlan(
                desired_book_size=len(desired),
                desired_gross_weight=float(decision.gross),
                planned_exits=0,
                planned_entries=0,
                planned_resizes=0,
                entry_cap_deferrals=0,
                entry_validity_expired_skips=0,
                entry_dust_skips=0,
                engine_blocked_entries=0,
                entry_blocked_reason=entry_blocked_reason,
                book_written=False,
                target_book_object_path="",
            )
        # Expired on publication: the follower may remove exposure and cancel
        # old entries, but it cannot open or resize any retained name.
        publication = publish_target_book(
            Path(path_text),
            render_target_book(
                source=str(demo.strategy_profile),
                decision_ts_ms=max(1, cycle_now_ms - 1),
                valid_until_ms=max(2, cycle_now_ms),
                targets=list(retained),
            ),
        )
        return CarryTargetPlan(
            desired_book_size=len(desired),
            desired_gross_weight=float(decision.gross),
            planned_exits=planned_exits,
            planned_entries=0,
            planned_resizes=0,
            entry_cap_deferrals=0,
            entry_validity_expired_skips=0,
            entry_dust_skips=0,
            engine_blocked_entries=0,
            entry_blocked_reason=entry_blocked_reason,
            book_written=True,
            target_book_object_path=str(publication.object_path),
        )
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

    standing_notional = {
        symbol: (-1.0 if side.lower() == "short" else 1.0) * abs(qty) * entry_px
        for symbol, (side, qty, entry_px) in standing_rows.items()
        if qty != 0.0 and entry_px > 0.0
    }
    standing_symbols = set(standing_notional)
    book_desired = {
        symbol: float(weight)
        for symbol, weight in desired.items()
        if symbol in standing_symbols
    }
    entry_cap_deferrals = 0
    entry_validity_expired_skips = 0
    entry_dust_skips = 0
    entry_symbols = sorted(
        (symbol for symbol in desired if symbol not in standing_symbols),
        key=lambda symbol: (trail_by_symbol.get(symbol, 0.0), symbol),
    )
    blockers = entry_blockers or {}
    engine_blocked_entries = sum(1 for symbol in entry_symbols if symbol in blockers)
    entry_symbols = [symbol for symbol in entry_symbols if symbol not in blockers]
    if cycle_now_ms >= decision_ts_ms + SIGNAL_VALIDITY_MS - ENTRY_PUBLISH_GUARD_MS:
        entry_validity_expired_skips = len(entry_symbols)
        entry_symbols = []
    planned_entries = 0
    for symbol in entry_symbols:
        target_notional = (
            float(desired[symbol]) * sizing_equity_usdt * demo.notional_multiplier
        )
        if abs(target_notional) < ENTRY_MIN_NOTIONAL_USDT:
            entry_dust_skips += 1
            continue
        if planned_entries >= demo.max_new_entries_per_cycle:
            entry_cap_deferrals += 1
            continue
        book_desired[symbol] = float(desired[symbol])
        planned_entries += 1

    planned_resizes = 0
    for symbol in sorted(set(book_desired) & standing_symbols):
        target_notional = (
            book_desired[symbol] * sizing_equity_usdt * demo.notional_multiplier
        )
        standing = standing_notional[symbol]
        threshold = max(
            RESIZE_MIN_NOTIONAL_USDT,
            RESIZE_MIN_FRACTION_OF_STANDING * abs(standing),
        )
        if abs(target_notional - standing) > threshold:
            planned_resizes += 1

    published_target_book = _write_engine_target_book(
        desired=book_desired,
        decision_ts_ms=decision_ts_ms,
        sizing_equity_usdt=sizing_equity_usdt,
        notional_multiplier=float(demo.notional_multiplier),
        stop_loss_fraction=float(demo.declared_stop_loss_fraction),
        entry_leverage=float(demo.entry_leverage),
        strategy_profile=str(demo.strategy_profile),
    )

    return CarryTargetPlan(
        desired_book_size=len(desired),
        desired_gross_weight=float(decision.gross),
        planned_exits=len(standing_symbols - set(book_desired)),
        planned_entries=planned_entries,
        planned_resizes=planned_resizes,
        entry_cap_deferrals=entry_cap_deferrals,
        entry_validity_expired_skips=entry_validity_expired_skips,
        entry_dust_skips=entry_dust_skips,
        engine_blocked_entries=engine_blocked_entries,
        entry_blocked_reason=entry_blocked_reason,
        book_written=True,
        target_book_object_path=str(published_target_book.object_path),
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
        frozen = load_candidate_universe(candidate_universe_file, realm=realm)
        # CARRY's own profile is registered and checked here, but it does NOT
        # narrow what CARRY trades: the sleeve trades the whole frozen
        # instrument set. Binding to the carry profile instead would cut the
        # tradable population from every listed perpetual (510 on demo, 512 on
        # mainnet as of the 2026-08-13 freeze) to the carry top-150 — a
        # strategy change, not a rename.
        require_profile_binding(
            frozen,
            profile="carry",
            current_inputs=carry_profile_universe_inputs(),
        )
        allowed = set(frozen.strategy_instruments)
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
        "kline_cache_rows": int(kline_stats.get("cache_rows", 0)),
        "kline_fetched_rows": int(kline_stats.get("fetched_rows", 0)),
        "kline_output_rows": int(kline_stats.get("output_rows", 0)),
        "kline_fetch_symbols": int(kline_stats.get("fetch_symbols", 0)),
        "kline_store_rows": int(kline_stats.get("store_rows", 0)),
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

    demo = demo_config or CarryDemoCycleConfig()
    _validate_carry_demo_config(demo)
    environment = execution_environment(demo.execution_environment).value
    root = Path(data_root).expanduser()
    engine_book_path = Path(os.environ[ENGINE_TARGET_BOOK_PATH_ENV]).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    cycle_now_ms = int(now_ms if now_ms is not None else _utc_now_ms())
    decision_ts_ms = carry_decision_ts_ms(cycle_now_ms)
    cycle_id = f"carry-target-{CARRY_STRATEGY_ID}-{cycle_now_ms}"
    cycles_dataset = carry_cycles_dataset(demo)
    state = cycle_state if cycle_state is not None else CarryCycleState()

    with exclusive_file_lock(root / ".locks" / "carry_demo_cycle.lock", stale_seconds=900):
        state.bind_sizing_anchors(root)
        engine_reading: EngineAccountReading | None = None
        try:
            engine_reading = require_recent_engine_account(
                environment,
                max_age_ns=TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
                now_ns=cycle_now_ms * 1_000_000,
            )
            equity_usdt = float(engine_reading.equity_usdt)
            engine_account_health_error = ""
        except (OSError, RuntimeError, ValueError) as exc:
            equity_usdt = 0.0
            engine_account_health_error = str(exc)
            _logger.warning("CARRY engine account reading unavailable; book held: %s", exc)
        standing_rows = (
            engine_reading.holdings_for_strategy(ENGINE_CARRY_SLEEVE)
            if engine_reading is not None
            else {}
        )
        standing_symbols = set(standing_rows)

        decision: CarryDecision | None = None
        decision_error: str | None = None
        decision_frozen = False
        strategy_profile = resolve_carry_strategy_profile(demo.strategy_profile)
        rule = _registered_rule(strategy_profile.config_path)
        trail_by_symbol: dict[str, float] = {}
        build_stats: dict[str, Any] = {}
        universe_eligible = 0
        freeze_ahead_frozen = False
        drop_exit_frozen = False
        built_klines: pl.DataFrame | None = None
        built_funding: pl.DataFrame | None = None
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
            decision, trail_by_symbol, universe_eligible = prewarmed
            decision_frozen = True
            build_stats = {"data_source": "build_skipped", "ticker_source": "skipped"}
        else:
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
                built_klines, built_funding = klines, funding
                if rule.whale_cut is not None:
                    # The whale halving reads Binance EODs; refresh the tiny
                    # cache for exactly the symbols this build fetched. Never
                    # raises — a dead feed fails open per the registered rule.
                    whale_symbols = (
                        sorted(set(klines.get_column("symbol").to_list()))
                        if not klines.is_empty()
                        else []
                    )
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
                    decision, trail_by_symbol, universe_eligible = frozen
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
                    universe_eligible = (
                        int(view.get_column("symbol").n_unique()) if not view.is_empty() else 0
                    )
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
            # nothing and the day degrades to the old clock.
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
        if (
            freeze_ahead_decision_ts_ms is not None
            and state.frozen_decision(int(freeze_ahead_decision_ts_ms)) is not None
            and not engine_account_health_error
            and equity_usdt > 0.0
        ):
            # Anchor tomorrow to the fresh engine account mark used to freeze
            # it, so the boundary pass cannot introduce P&L feedback.
            state.sizing_equity(
                decision_ts_ms=int(freeze_ahead_decision_ts_ms),
                equity_usdt=float(equity_usdt),
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

        early_exit_fires: list[str] = []
        if decision is not None and demo.early_exit_enabled:
            # Mask AFTER freezing: the frozen tuple keeps the registered
            # decision; the mask is re-applied every cycle from its own state.
            decision, early_exit_fires = _apply_early_exits(
                decision=decision,
                rule=rule,
                funding=built_funding,
                state=state,
                root=root,
                now_ms=cycle_now_ms,
            )
            if early_exit_fires:
                _logger.info(
                    "early exit fired: %s (settled print at/above %.1f bp)",
                    ",".join(early_exit_fires),
                    -rule.exit_bp,
                )

        presettle_fires: list[str] = []
        presettle_fire_details: list[PresettleFire] = []
        presettle_error = ""
        if (
            decision is not None
            and demo.early_exit_enabled
            and strategy_profile.presettle_exit
            and decision.weights
        ):
            # Every settlement sits on an hour boundary; fetch only when one
            # is close enough for a fire to be possible.
            to_boundary_ms = HOUR_MS - (cycle_now_ms % HOUR_MS)
            if to_boundary_ms <= _PRESETTLE_WINDOW_MS + _PRESETTLE_FETCH_SLACK_MS:
                tickers, presettle_error = _fetch_presettle_tickers(
                    sorted(decision.weights)
                )
                if tickers:
                    decision, presettle_fires, presettle_fire_details = (
                        _apply_presettle_exits(
                            decision=decision,
                            rule=rule,
                            state=state,
                            root=root,
                            now_ms=cycle_now_ms,
                            tickers=tickers,
                        )
                    )
                if presettle_fires:
                    _logger.info(
                        "pre-settle exit fired: %s (running rate at/above %.1f bp "
                        "before the print pays)",
                        ",".join(presettle_fires),
                        -rule.exit_bp,
                    )
                if presettle_error:
                    _logger.warning(
                        "pre-settle ticker read failed; settled-print clock "
                        "stands: %s",
                        presettle_error,
                    )

        # The drop exit: mask the
        # names the UPCOMING frozen decision zeroes out of the served
        # (old-day) book, so their removals publish this cycle — ~00:02,
        # before the post-settlement drift the 00:20 clock sells into.
        drop_exit_fires: list[str] = []
        drop_exit_masked = 0
        if decision is not None and decision.decision_ts_ms % DAY_MS == 0:
            decision, dropped_now, drop_exit_masked = _apply_drop_exits(
                decision=decision, state=state
            )
            if dropped_now:
                if frozenset(dropped_now) != state.drop_exits_logged:
                    _logger.info(
                        "drop exit fired: %s (the upcoming decision zeroes "
                        "them; selling ahead of the 00:20 clock)",
                        ",".join(dropped_now),
                    )
                    drop_exit_fires = dropped_now
                state.drop_exits_logged = frozenset(dropped_now)

        # The exodus pass runs on EVERY cycle: covers must drain even when
        # the carry decision is unavailable. Entries additionally need the
        # exact carry-attributed holding and contemporaneous mark.
        try:
            engine_reading = require_recent_engine_account(
                environment,
                max_age_ns=TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
                now_ns=time.time_ns(),
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
        exodus_receipt = _run_exodus_short(
            state=state,
            root=root,
            fires=presettle_fire_details,
            carry_holdings=(standing_rows if engine_reading is not None else None),
            entry_leverage=float(demo.entry_leverage),
            now_ms=cycle_now_ms,
            exodus_held_symbols=(
                frozenset(
                    engine_reading.holdings_for_strategy(ENGINE_EXODUS_SLEEVE)
                )
                if engine_reading is not None
                and ENGINE_EXODUS_SLEEVE in engine_reading.strategies
                else None
            ),
            exodus_working_entry_symbols=(
                engine_reading.working_entries_for_strategy(ENGINE_EXODUS_SLEEVE)
                if engine_reading is not None
                and ENGINE_EXODUS_SLEEVE in engine_reading.strategies
                else None
            ),
        )

        plan = _carry_target_plan(
            decision=decision,
            standing_rows=standing_rows,
            trail_by_symbol=trail_by_symbol,
            demo=demo,
            equity_usdt=equity_usdt,
            engine_account_health_error=engine_account_health_error,
            entry_blockers=(
                engine_reading.entry_blockers_for_strategy(ENGINE_CARRY_SLEEVE)
                if engine_reading is not None
                else {}
            ),
            cycle_now_ms=cycle_now_ms,
            cycle_state=state,
        )
        payload: dict[str, Any] = {
            "cycle_id": cycle_id,
            "ts_ms": cycle_now_ms,
            "sleeve": "carry",
            "mode": f"{environment}_rust_target_book",
            "environment": environment,
            "strategy_id": CARRY_STRATEGY_ID,
            "strategy_profile": strategy_profile.profile_name,
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
            "decision_frozen_ahead": bool(
                decision_frozen and state.frozen_ahead_bar_ts_ms == decision_ts_ms
            ),
            "freeze_ahead_frozen": freeze_ahead_frozen,
            "decision_universe_size": decision.universe_size if decision is not None else 0,
            "decision_replay_days": decision.replay_days if decision is not None else 0,
            "desired_book_size": plan.desired_book_size,
            "desired_gross_weight": plan.desired_gross_weight,
            "universe_fetched": int(build_stats.get("universe_fetched", 0)),
            "universe_eligible": universe_eligible,
            "candidate_skipped_symbols": int(build_stats.get("candidate_skipped_symbols", 0)),
            # Whale-feed receipt (v6+): how many Binance EOD symbol-days were
            # fetched/missing this cycle and how many known values fed the
            # view. Absent keys mean the rule has no whale leg (v3/v4).
            "whale_pairs_fetched": build_stats.get("whale_pairs_fetched"),
            "whale_pairs_missing": build_stats.get("whale_pairs_missing"),
            "whale_event_rows": build_stats.get("whale_event_rows"),
            "whale_error": build_stats.get("whale_error"),
            # Early-exit receipt: names fired THIS cycle, and the standing
            # mask for the current decision day.
            "early_exit_enabled": demo.early_exit_enabled,
            "early_exit_fired": early_exit_fires,
            "early_exit_masked": len(state.early_exits or {}),
            "presettle_exit_enabled": bool(
                demo.early_exit_enabled and strategy_profile.presettle_exit
            ),
            "presettle_fired": presettle_fires,
            "presettle_error": presettle_error,
            # Drop-exit receipt: names the upcoming decision zeroed and this
            # cycle announced, plus whether this cycle froze that upcoming
            # book early. Part of the exit clock; no dial.
            "drop_exit_fired": drop_exit_fires,
            "drop_exit_masked": drop_exit_masked,
            "drop_exit_froze_ahead": drop_exit_frozen,
            # Exodus-short receipt (lane2_exodus_short_v1): what the sleeve
            # did this cycle. Absent keys mean the unit does not publish it.
            "exodus_enabled": exodus_receipt.get("exodus_enabled"),
            "exodus_opened": exodus_receipt.get("exodus_opened"),
            "exodus_covered": exodus_receipt.get("exodus_covered"),
            "exodus_entry_blocked": exodus_receipt.get("exodus_entry_blocked"),
            "exodus_open_names": exodus_receipt.get("exodus_open_names"),
            "exodus_error": exodus_receipt.get("exodus_error"),
            "open_positions": len(standing_symbols),
            "standing_symbols": len(standing_symbols),
            "planned_exits": plan.planned_exits,
            "planned_entries": plan.planned_entries,
            "planned_resizes": plan.planned_resizes,
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
        # An exodus cover due sooner wins the slot; the 60s idle floor is the
        # correctness backstop either way, this is the accelerator.
        next_deadline_ts_ms = next_carry_decision_deadline_ts_ms(cycle_now_ms)
        exodus_cover_ts_ms = exodus_receipt.get("exodus_next_cover_ts_ms")
        if type(exodus_cover_ts_ms) is int and exodus_cover_ts_ms > 0:
            next_deadline_ts_ms = min(next_deadline_ts_ms, exodus_cover_ts_ms)
        payload["next_time_deadline_ts_ms"] = next_deadline_ts_ms
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
