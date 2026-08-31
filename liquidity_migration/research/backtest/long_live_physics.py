"""Minute-bound replay of the native Rust LONG strategy contract.

Daily signals come from the point-in-time hourly panel and every strategy
decision goes through one persistent Rust process. This module owns only
historical event ordering and execution/accounting physics.

Bybit mark-price OHLC drives entry eligibility, the exchange-native stop trigger,
and funding position value. One-minute trade OHLC drives fills, resizes, and
modeled account marks. Neither stream can reproduce a ticker feed, queue
position, order book, quantity-step history, or the path inside a minute. The
report therefore calls the result a minute execution bound. It is not exact
live/tick parity.
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import itertools
import json
import math
import os
import platform
import subprocess
import sys
from bisect import bisect_left
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import numpy as np
import polars as pl

from liquidity_migration.core._common import (
    MS_PER_DAY,
    MS_PER_MINUTE,
    date_ms,
    exact_duration_ms,
)
from liquidity_migration.core.symbol_codec import encode_symbol_partition
from liquidity_migration.rules.long_config import (
    ConfigLayer,
    StrategyConfig,
    resolve_strategy_config,
)
from liquidity_migration.rules.long_models import (
    DecisionAction,
    DecisionInput,
    DecisionOutput,
    PriorState,
)
from liquidity_migration.rules.long_native import (
    LongNativeConfig,
    resolve_long_strategy_profile,
)
from liquidity_migration.rules.rust_strategy_contract import (
    RustLongDecisionReducer,
    RustStrategyContract,
)


MINUTE_BAR_MS = exact_duration_ms(minutes=1)
DEFAULT_TAKER_FEE_BPS = 5.5
DEFAULT_MEASURED_CROSSING_COST_BPS = 7.78
DEFAULT_SLIPPAGE_BPS = DEFAULT_MEASURED_CROSSING_COST_BPS - DEFAULT_TAKER_FEE_BPS
DEFAULT_VENUE_MIN_NOTIONAL_USDT = 5.0
MAX_MARK_HIGH_SOURCE_REPAIR_BPS = 1.0
CAPITAL_REFERENCE_CLOSE_REL_TOL = 1e-12
CAPITAL_REFERENCE_CLOSE_ABS_TOL = 1e-9
LIVE_PHYSICS_SCHEMA_VERSION = 1
SOURCE_SNAPSHOT_SCHEMA_VERSION = 1
SOURCE_SNAPSHOT_NAME = "long_live_physics_source_snapshot.json"
_SOURCE_SNAPSHOT_ROOTS = (
    "scripts/research/run_long_live_physics.py",
    "liquidity_migration/research/backtest/long_live_physics.py",
    "liquidity_migration/rules/rust_strategy_contract.py",
    "engine/engine-strategies/src/bin/strategy_contract.rs",
    "engine/engine-strategies/src/native_long/plan.rs",
)
_SOURCE_SNAPSHOT_SUPPORT = ("pyproject.toml", "requirements.lock")
_GIT_LOCAL_ENV_VARS = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_WORK_TREE",
)


@dataclass(frozen=True, slots=True)
class EvidenceProvenance:
    """Which data shaped the rule and which data, if any, grades this run."""

    lane: str = "lane_1_exploratory"
    shaped_data: str = (
        "The LONG rule and this execution rebuild were shaped using prior Bybit "
        "history, including the historical surface replayed here."
    )
    graded_data: str = "none; this is a seen-data rebuild"
    claim: str = (
        "Rebuild native LONG under the deployed decision policy and minute-bound "
        "Bybit execution physics before interpreting historical performance."
    )
    non_conclusions: tuple[str, ...] = (
        "This does not establish tick, L1, queue, or fill parity.",
        "This does not establish out-of-sample performance.",
        "This does not authorize demo or real-money trading.",
    )

    def validate(self) -> None:
        if self.lane not in {"lane_1_exploratory", "lane_2_forward"}:
            raise ValueError("evidence lane must be lane_1_exploratory or lane_2_forward")
        if not self.shaped_data.strip() or not self.graded_data.strip():
            raise ValueError("shaped_data and graded_data must be explicit")
        if self.lane == "lane_1_exploratory" and self.graded_data.strip().lower() == "unseen":
            raise ValueError("a Lane-1 run cannot label its grading data unseen")


@dataclass(frozen=True, slots=True)
class LivePhysicsAssumptions:
    """Execution assumptions that sit outside the pure strategy contract."""

    initial_equity_usdt: float = 1_000.0
    taker_fee_bps: float = DEFAULT_TAKER_FEE_BPS
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    venue_min_notional_usdt: float = DEFAULT_VENUE_MIN_NOTIONAL_USDT
    evidence: EvidenceProvenance = EvidenceProvenance()

    def validate(self) -> None:
        numeric = {
            "initial_equity_usdt": self.initial_equity_usdt,
            "taker_fee_bps": self.taker_fee_bps,
            "slippage_bps": self.slippage_bps,
            "venue_min_notional_usdt": self.venue_min_notional_usdt,
        }
        for label, raw in numeric.items():
            value = float(raw)
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{label} must be finite and non-negative")
        if self.initial_equity_usdt <= 0.0:
            raise ValueError("initial_equity_usdt must be positive")
        self.evidence.validate()


@dataclass(frozen=True, slots=True)
class MinuteTapeHighRepair:
    """One raw mark-price candle whose source high was expanded."""

    ts_ms: int
    symbol: str
    raw_open: float
    raw_high: float
    raw_low: float
    raw_close: float
    repaired_high: float
    gap_bps: float


@dataclass(frozen=True, slots=True)
class MinuteTapeReceipt:
    """Identity and completeness receipt for the selected one-minute tape."""

    dataset: str
    requested_intervals: int
    requested_symbol_days: int
    selected_files: int
    selected_file_sha256: str
    rows: int
    missing_symbol_days: int
    missing_minutes: int
    missing_symbol_day_sample: tuple[str, ...] = ()
    missing_minute_sample: tuple[str, ...] = ()
    source_high_repair_count: int = 0
    source_high_repair_max_gap_bps: float = 0.0
    source_high_repair_raw_sample: tuple[MinuteTapeHighRepair, ...] = ()

    @property
    def complete(self) -> bool:
        return self.missing_symbol_days == 0 and self.missing_minutes == 0


@dataclass(frozen=True, slots=True)
class FundingTapeReceipt:
    """Download-marker proof for every candidate funding window."""

    required_intervals: int
    covered_intervals: int
    selected_markers: int
    selected_marker_sha256: str
    missing_intervals: int
    missing_interval_sample: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.missing_intervals == 0


@dataclass(frozen=True, slots=True)
class LivePhysicsCapitalReference:
    """Typed operational reference and the account caps scaled from it."""

    configured_seed_usdt: float
    tracks_equity: bool
    equity_fraction: float
    floor_usdt: float
    expand_dead_band_fraction: float
    account_gross_cap_multiple_reference: float
    account_margin_cap_multiple_reference: float
    source: str
    source_sha256: str

    def validate(self) -> None:
        positive = {
            "configured_seed_usdt": self.configured_seed_usdt,
            "equity_fraction": self.equity_fraction,
            "account_gross_cap_multiple_reference": self.account_gross_cap_multiple_reference,
            "account_margin_cap_multiple_reference": self.account_margin_cap_multiple_reference,
        }
        for label, raw in positive.items():
            value = float(raw)
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"capital reference {label} must be positive and finite")
        if self.equity_fraction > 1.0:
            raise ValueError("capital reference equity_fraction cannot exceed 1")
        if not math.isfinite(self.floor_usdt) or self.floor_usdt < 0.0:
            raise ValueError("capital reference floor_usdt must be finite and non-negative")
        if self.tracks_equity and self.floor_usdt <= 0.0:
            raise ValueError("tracking capital reference floor_usdt must be positive")
        if self.floor_usdt > self.configured_seed_usdt:
            raise ValueError("capital reference floor_usdt cannot exceed its configured seed")
        if (
            not math.isfinite(self.expand_dead_band_fraction)
            or self.expand_dead_band_fraction < 0.0
            or self.expand_dead_band_fraction >= 1.0
        ):
            raise ValueError("capital reference expand dead band must be in [0, 1)")
        if not self.source.strip():
            raise ValueError("capital reference provenance source is required")
        if len(self.source_sha256) != 64 or any(char not in "0123456789abcdef" for char in self.source_sha256):
            raise ValueError("capital reference provenance SHA-256 must be lowercase hex")


@dataclass(slots=True)
class _CapitalReferenceState:
    config: LivePhysicsCapitalReference
    current_usdt: float = field(init=False)
    minimum_usdt: float = field(init=False)
    maximum_usdt: float = field(init=False)
    observations: int = 0
    updates: int = 0
    expansions: int = 0
    contractions: int = 0

    def __post_init__(self) -> None:
        self.config.validate()
        self.current_usdt = float(self.config.configured_seed_usdt)
        self.minimum_usdt = self.current_usdt
        self.maximum_usdt = self.current_usdt

    def observe_equity(self, equity_usdt: float) -> None:
        self.observations += 1
        previous = self.current_usdt
        current = capital_reference_after_equity(
            previous,
            equity_usdt,
            config=self.config,
        )
        if current == previous:
            return
        self.current_usdt = current
        self.minimum_usdt = min(self.minimum_usdt, current)
        self.maximum_usdt = max(self.maximum_usdt, current)
        self.updates += 1
        if current < previous:
            self.contractions += 1
        else:
            self.expansions += 1


@dataclass(frozen=True, slots=True)
class LivePhysicsResult:
    trades: pl.DataFrame
    mutations: pl.DataFrame
    funding_events: pl.DataFrame
    daily_equity: pl.DataFrame
    decisions: pl.DataFrame
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ResolvedLivePhysicsConfiguration:
    """One strategy plus the account-cap ratios from the same profile read."""

    strategy: StrategyConfig
    capital_reference: LivePhysicsCapitalReference
    operational_profile_source: str
    operational_profile_sha256: str


@dataclass(frozen=True, slots=True)
class _Candidate:
    symbol: str
    signal_ts_ms: int
    signal_close: float
    feature_row: Mapping[str, Any]
    first_check_ts_ms: int
    deadline_ts_ms: int
    stale_ts_ms: int
    max_hold_duration_ms: int

    @property
    def priority(self) -> tuple[int, float, float, str]:
        return (
            -self.signal_ts_ms,
            -_finite(self.feature_row.get("log_return"), 0.0),
            _finite(self.feature_row.get("today_volume_rank"), 1e9),
            self.symbol,
        )


@dataclass(slots=True)
class _Position:
    symbol: str
    signal_ts_ms: int
    entry_ts_ms: int
    entry_reason: str
    entry_equity_usdt: float
    target_fraction_of_equity: float
    target_notional_usdt: float
    entry_leverage: float
    stop_loss_fraction: float
    stop_decay_after_ms: int
    decayed_stop_loss_fraction: float
    max_hold_deadline_ts_ms: int
    entry_reference_price: float
    entry_fill_price: float
    quantity: float = 0.0
    average_reference_price: float = 0.0
    average_fill_price: float = 0.0
    gross_realized_usdt: float = 0.0
    fee_usdt: float = 0.0
    slippage_usdt: float = 0.0
    funding_usdt: float = 0.0
    funding_event_count: int = 0
    mutation_count: int = 0
    resize_count: int = 0
    mutation_notional_usdt: float = 0.0
    minimum_mark: float = math.inf
    maximum_mark: float = -math.inf


@dataclass(slots=True)
class _Ledger:
    initial_equity_usdt: float
    gross_realized_usdt: float = 0.0
    fee_usdt: float = 0.0
    slippage_usdt: float = 0.0
    funding_usdt: float = 0.0
    mutations: list[dict[str, Any]] = field(default_factory=list)
    funding_rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def cash_equity_usdt(self) -> float:
        return (
            self.initial_equity_usdt + self.gross_realized_usdt - self.fee_usdt - self.slippage_usdt + self.funding_usdt
        )

    def marked_equity(
        self,
        positions: Mapping[str, _Position],
        marks: Mapping[str, float],
    ) -> float:
        unrealized = 0.0
        for symbol, position in positions.items():
            mark = _finite(marks.get(symbol), 0.0)
            if mark > 0.0:
                unrealized += position.quantity * (mark - position.average_reference_price)
        return self.cash_equity_usdt + unrealized


def _local_module_source(repo: Path, module: str) -> Path | None:
    relative = Path(*module.split("."))
    module_file = repo / relative.with_suffix(".py")
    if module_file.is_file():
        return module_file
    package_file = repo / relative / "__init__.py"
    return package_file if package_file.is_file() else None


def _source_imports(repo: Path, path: Path) -> set[Path]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    output: set[Path] = set()
    for module in modules:
        if module != "liquidity_migration" and not module.startswith("liquidity_migration."):
            continue
        source = _local_module_source(repo, module)
        if source is not None:
            output.add(source)
    return output


def _source_snapshot(repo: Path) -> tuple[bytes, dict[str, object]]:
    """Freeze the local Python and Rust closure behind the LONG replay."""

    selected: set[Path] = set()
    pending: list[Path] = []

    def add_source(path: Path) -> None:
        if path in selected:
            return
        selected.add(path)
        if path.suffix != ".py":
            return
        pending.append(path)
        relative = path.relative_to(repo)
        package_parts = relative.parts[:-1]
        if package_parts and package_parts[0] == "liquidity_migration":
            for depth in range(1, len(package_parts) + 1):
                initializer = repo.joinpath(*package_parts[:depth], "__init__.py")
                if initializer.is_file():
                    add_source(initializer)

    for relative in (*_SOURCE_SNAPSHOT_ROOTS, *_SOURCE_SNAPSHOT_SUPPORT):
        path = repo / relative
        if not path.is_file():
            raise RuntimeError(f"LONG source snapshot input is missing: {relative}")
        add_source(path)

    while pending:
        path = pending.pop()
        for imported in _source_imports(repo, path):
            add_source(imported)

    files: list[dict[str, object]] = []
    for path in sorted(selected, key=lambda item: item.relative_to(repo).as_posix()):
        data = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(repo).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "content_utf8": data.decode("utf-8"),
            }
        )
    payload: dict[str, object] = {
        "schema_version": SOURCE_SNAPSHOT_SCHEMA_VERSION,
        "kind": "long_live_physics_source_snapshot",
        "roots": list(_SOURCE_SNAPSHOT_ROOTS),
        "files": files,
        "runtime": {
            "python": sys.version,
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "numpy": np.__version__,
            "polars": pl.__version__,
        },
    }
    encoded = (json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    identity = {
        "source_snapshot_file": SOURCE_SNAPSHOT_NAME,
        "source_snapshot_sha256": hashlib.sha256(encoded).hexdigest(),
        "source_snapshot_files": len(files),
    }
    return encoded, identity


def resolve_live_physics_configuration(
    *,
    profile_name: str = "v12",
    operational_profile_path: str | Path = "configs/operational.mainnet.json",
    taker_fee_bps: float = DEFAULT_TAKER_FEE_BPS,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> ResolvedLivePhysicsConfiguration:
    """Read one typed profile once and retain sizing, throttle, and cap sources."""

    from liquidity_migration.core.operational_profile import load_operational_profile

    rule = resolve_long_strategy_profile(profile_name)
    operational = load_operational_profile(operational_profile_path)
    settings = operational.long
    strategy = resolve_strategy_config(
        profile_name,
        rule=rule,
        rule_source=f"registered_profile:{profile_name}",
        layers=(
            ConfigLayer(
                source="operational_profile",
                detail=(f"{operational.source_path or operational_profile_path}#{operational.source_sha256}"),
                values={
                    "notional_multiplier": settings.notional_multiplier,
                    "entry_leverage": settings.entry_leverage,
                    "order_notional_pct_equity": settings.order_notional_pct_equity,
                    "max_new_entries_per_cycle": settings.max_new_entries_per_cycle,
                },
            ),
            ConfigLayer(
                source="live_crossing_execution",
                detail=(f"{float(taker_fee_bps):g} bp fee plus {float(slippage_bps):g} bp crossing loss per side"),
                values={"round_trip_cost_bps": 2.0 * (float(taker_fee_bps) + float(slippage_bps))},
            ),
        ),
    )
    capital = operational.capital_reference_usdt
    capital_settings = operational.capital_reference
    source = str(operational.source_path or operational_profile_path)
    capital_reference = LivePhysicsCapitalReference(
        configured_seed_usdt=capital,
        tracks_equity=capital_settings.tracks_equity,
        equity_fraction=capital_settings.equity_fraction,
        floor_usdt=(
            capital_settings.floor_usdt
            if capital_settings.tracks_equity or capital_settings.floor_usdt > 0.0
            else capital
        ),
        expand_dead_band_fraction=capital_settings.expand_dead_band_fraction,
        account_gross_cap_multiple_reference=(operational.account_risk.max_account_gross_notional_usdt / capital),
        account_margin_cap_multiple_reference=(operational.account_risk.max_initial_margin_usdt / capital),
        source=source,
        source_sha256=operational.source_sha256,
    )
    capital_reference.validate()
    return ResolvedLivePhysicsConfiguration(
        strategy=strategy,
        capital_reference=capital_reference,
        operational_profile_source=source,
        operational_profile_sha256=operational.source_sha256,
    )


def deadband_threshold_usdt(
    standing_notional_usdt: float,
    *,
    config: StrategyConfig,
    venue_min_notional_usdt: float,
) -> float:
    """Return the Rust fleet planner's notional resize threshold."""

    standing = abs(float(standing_notional_usdt))
    venue_min = float(venue_min_notional_usdt)
    if not math.isfinite(standing) or not math.isfinite(venue_min) or venue_min < 0.0:
        raise ValueError("dead-band inputs must be finite and non-negative")
    return max(
        config.resize_floor_usdt,
        config.resize_floor_fraction * standing,
        venue_min,
    )


def capital_reference_after_equity(
    current_reference_usdt: float,
    equity_usdt: float,
    *,
    config: LivePhysicsCapitalReference,
) -> float:
    """Fold one account-equity reading into the Rust-equivalent reference."""

    config.validate()
    current = float(current_reference_usdt)
    if not math.isfinite(current) or current <= 0.0:
        raise ValueError("current capital reference must be positive and finite")
    equity = float(equity_usdt)
    if not config.tracks_equity or not math.isfinite(equity) or equity <= 0.0:
        return current
    target = max(equity * config.equity_fraction, config.floor_usdt)
    if math.isclose(
        target,
        current,
        rel_tol=CAPITAL_REFERENCE_CLOSE_REL_TOL,
        abs_tol=CAPITAL_REFERENCE_CLOSE_ABS_TOL,
    ):
        return current
    if target < current:
        return target
    if target <= current * (1.0 + config.expand_dead_band_fraction):
        return current
    return target


def extract_signal_candidates(
    features: pl.DataFrame,
    *,
    config: StrategyConfig,
    decision_reducer: RustLongDecisionReducer | None = None,
) -> list[_Candidate]:
    """Ask the native reducer which closed daily rows are LONG signals."""

    if decision_reducer is None:
        with RustStrategyContract() as contract:
            return extract_signal_candidates(
                features,
                config=config,
                decision_reducer=RustLongDecisionReducer(contract, config),
            )

    if features.is_empty():
        return []
    required = {"ts_ms", "symbol", "close"}
    missing = required - set(features.columns)
    if missing:
        raise ValueError(f"LONG features are missing columns: {sorted(missing)}")
    candidates: list[_Candidate] = []
    seen: set[tuple[str, int]] = set()
    for row in features.sort(["ts_ms", "symbol"]).to_dicts():
        symbol = str(row.get("symbol") or "").upper()
        signal_ts_ms = int(row.get("ts_ms") or 0)
        signal_close = _finite(row.get("close"), 0.0)
        if not symbol or signal_ts_ms <= 0 or signal_close <= 0.0:
            continue
        first_check = signal_ts_ms + exact_duration_ms(hours=max(1, config.rule.entry_delay_hours))
        deadline = signal_ts_ms + exact_duration_ms(hours=config.rule.fc_sniper_deadline_hours)
        probe = decision_reducer.decide(
            DecisionInput(
                decision_ts_ms=max(first_check, deadline),
                symbol=symbol,
                signal_ts_ms=signal_ts_ms,
                signal_close=signal_close,
                market_price=signal_close,
                equity_usdt=1.0,
                feature_row=row,
            ),
            PriorState(),
        )
        if probe.action is not DecisionAction.ENTER:
            continue
        key = (symbol, signal_ts_ms)
        if key in seen:
            raise ValueError(f"duplicate LONG feature identity: {symbol} {signal_ts_ms}")
        seen.add(key)
        candidates.append(
            _Candidate(
                symbol=symbol,
                signal_ts_ms=signal_ts_ms,
                signal_close=signal_close,
                feature_row=row,
                first_check_ts_ms=first_check,
                deadline_ts_ms=deadline,
                stale_ts_ms=signal_ts_ms + config.signal_freshness_ms,
                max_hold_duration_ms=probe.max_hold_duration_ms,
            )
        )
    return candidates


def candidate_execution_intervals(
    candidates: Sequence[_Candidate],
) -> dict[str, list[tuple[int, int]]]:
    """Return merged minute windows that can affect any candidate or fill."""

    raw: dict[str, list[tuple[int, int]]] = {}
    for candidate in candidates:
        # A capacity-blocked deadline candidate can remain eligible until the
        # signal turns stale, then needs its complete fill-anchored hold path.
        end = candidate.stale_ts_ms + candidate.max_hold_duration_ms + MINUTE_BAR_MS
        raw.setdefault(candidate.symbol, []).append((candidate.first_check_ts_ms, end))
    output: dict[str, list[tuple[int, int]]] = {}
    for symbol, intervals in raw.items():
        merged: list[tuple[int, int]] = []
        for start, end in sorted(intervals):
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        output[symbol] = merged
    return output


def load_candidate_minute_tape(
    data_root: str | Path,
    candidates: Sequence[_Candidate],
    *,
    dataset: str = "klines_1m",
) -> tuple[pl.DataFrame, MinuteTapeReceipt]:
    """Load and hash one reachable trade- or mark-price minute dataset."""

    root = Path(data_root).expanduser()
    if dataset not in {"klines_1m", "mark_price_1m"}:
        raise ValueError("candidate minute dataset must be 'klines_1m' or 'mark_price_1m'")
    dataset_root = root / dataset
    intervals = candidate_execution_intervals(candidates)
    requested_pairs: set[tuple[str, str]] = set()
    for symbol, spans in intervals.items():
        for start, end in spans:
            day = _utc_date(start)
            final_day = _utc_date(max(start, end - 1))
            while day <= final_day:
                requested_pairs.add((symbol, day.isoformat()))
                day += dt.timedelta(days=1)

    selected_files: list[Path] = []
    missing_pairs: list[str] = []
    for symbol, date_text in sorted(requested_pairs):
        directory = dataset_root / f"date={date_text}" / f"symbol={encode_symbol_partition(symbol)}"
        files = sorted(directory.glob("*.parquet")) if directory.is_dir() else []
        if not files:
            missing_pairs.append(f"{date_text}/{symbol}")
        selected_files.extend(files)

    digest = hashlib.sha256()
    for path in selected_files:
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)

    if selected_files:
        minute_bars = (
            pl.scan_parquet([str(path) for path in selected_files])
            .select(["ts_ms", "symbol", "open", "high", "low", "close"])
            .collect()
        )
    else:
        minute_bars = _empty_minute_bars()
    minute_bars, high_repairs = _canonical_minute_bars(
        minute_bars,
        dataset=dataset,
    )

    available: dict[str, set[int]] = {}
    for part in minute_bars.partition_by("symbol", maintain_order=False):
        symbol = str(part["symbol"][0])
        available[symbol] = set(int(value) for value in part["ts_ms"].to_list())
    missing_minute_count = 0
    missing_minute_sample: list[str] = []
    for symbol, spans in sorted(intervals.items()):
        observed = available.get(symbol, set())
        for start, end in spans:
            expected = start - (start % MINUTE_BAR_MS)
            if expected < start:
                expected += MINUTE_BAR_MS
            while expected < end:
                if expected not in observed:
                    missing_minute_count += 1
                    if len(missing_minute_sample) < 20:
                        missing_minute_sample.append(f"{_iso_ts(expected)}/{symbol}")
                expected += MINUTE_BAR_MS

    receipt = MinuteTapeReceipt(
        dataset=dataset,
        requested_intervals=sum(len(value) for value in intervals.values()),
        requested_symbol_days=len(requested_pairs),
        selected_files=len(selected_files),
        selected_file_sha256=digest.hexdigest(),
        rows=minute_bars.height,
        missing_symbol_days=len(missing_pairs),
        missing_minutes=missing_minute_count,
        missing_symbol_day_sample=tuple(missing_pairs[:20]),
        missing_minute_sample=tuple(missing_minute_sample),
        source_high_repair_count=len(high_repairs),
        source_high_repair_max_gap_bps=max(
            (repair.gap_bps for repair in high_repairs),
            default=0.0,
        ),
        source_high_repair_raw_sample=high_repairs[:20],
    )
    return minute_bars, receipt


def funding_frame_for_candidates(
    funding_lookup: Mapping[str, Mapping[str, Any]] | None,
    candidates: Sequence[_Candidate],
) -> tuple[pl.DataFrame, dict[str, tuple[tuple[int, int], ...]], str]:
    """Select exact settlement rows for candidate execution windows."""

    if funding_lookup is None:
        return _empty_funding(), {}, hashlib.sha256(b"").hexdigest()
    intervals = candidate_execution_intervals(candidates)
    rows: list[dict[str, Any]] = []
    coverage: dict[str, tuple[tuple[int, int], ...]] = {}
    for symbol, spans in sorted(intervals.items()):
        series = funding_lookup.get(symbol)
        if series is None:
            continue
        timestamps = [int(value) for value in series["events_ts"]]
        rates = [float(value) for value in series["events_rate"]]
        coverage[symbol] = (
            (
                int(series["start_ts_ms"]),
                int(series["end_ts_ms"]),
            ),
        )
        selected: set[int] = set()
        for start, end in spans:
            lo = bisect_left(timestamps, start)
            hi = bisect_left(timestamps, end)
            selected.update(range(lo, hi))
        rows.extend(
            {
                "ts_ms": timestamps[index],
                "symbol": symbol,
                "funding_rate": rates[index],
            }
            for index in sorted(selected)
        )
    funding = pl.DataFrame(rows, infer_schema_length=None).sort(["ts_ms", "symbol"]) if rows else _empty_funding()
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return funding, coverage, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_funding_download_coverage(
    data_root: str | Path,
    candidates: Sequence[_Candidate],
) -> tuple[dict[str, tuple[tuple[int, int], ...]], FundingTapeReceipt]:
    """Prove candidate windows from completed funding-download markers.

    First and last settlement rows do not prove that the endpoint's interior
    pages were captured. The downloader writes a non-empty marker only after a
    requested range was fetched and stored, so this receipt uses those ranges
    as the funding completeness boundary.
    """

    root = Path(data_root).expanduser()
    marker_root = root / "_download_markers" / "funding"
    intervals = candidate_execution_intervals(candidates)
    coverage: dict[str, list[tuple[int, int]]] = {}
    selected: set[Path] = set()
    missing: list[str] = []
    covered = 0
    for symbol, spans in sorted(intervals.items()):
        prefix = f"{symbol}_"
        parsed: list[tuple[int, int, Path]] = []
        for marker in sorted(marker_root.glob(f"{prefix}*.done")):
            if not marker.is_file() or marker.stat().st_size <= 0:
                continue
            middle = marker.name[len(prefix) : -len(".done")]
            parts = middle.split("_")
            if len(parts) != 2:
                continue
            try:
                marker_start, marker_end = (int(value) for value in parts)
            except ValueError:
                continue
            if marker_start < marker_end:
                parsed.append((marker_start, marker_end, marker))
        for start, end in spans:
            choices = [row for row in parsed if row[0] <= start and row[1] >= end]
            if not choices:
                missing.append(f"{symbol}:{_iso_ts(start)}..{_iso_ts(end)}")
                continue
            chosen = min(
                choices,
                key=lambda row: (row[1] - row[0], row[0], row[1], row[2].name),
            )
            selected.add(chosen[2])
            coverage.setdefault(symbol, []).append((start, end))
            covered += 1

    digest = hashlib.sha256()
    for marker in sorted(selected):
        digest.update(marker.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(marker.read_bytes())
    frozen = {symbol: tuple(sorted(spans)) for symbol, spans in sorted(coverage.items())}
    required = sum(len(spans) for spans in intervals.values())
    return frozen, FundingTapeReceipt(
        required_intervals=required,
        covered_intervals=covered,
        selected_markers=len(selected),
        selected_marker_sha256=digest.hexdigest(),
        missing_intervals=len(missing),
        missing_interval_sample=tuple(missing[:20]),
    )


def simulate_long_live_physics(
    *,
    features: pl.DataFrame,
    minute_bars: pl.DataFrame,
    mark_price_bars: pl.DataFrame,
    funding: pl.DataFrame | None,
    config: StrategyConfig,
    capital_reference: LivePhysicsCapitalReference,
    assumptions: LivePhysicsAssumptions | None = None,
    funding_coverage: Mapping[str, tuple[tuple[int, int], ...]] | None = None,
    signal_start_ms: int | None = None,
    signal_end_ms: int | None = None,
    decision_reducer: RustLongDecisionReducer | None = None,
    signal_candidates: Sequence[_Candidate] | None = None,
) -> LivePhysicsResult:
    """Replay native LONG decisions and a crossing target follower.

    Event order at a minute boundary is funding, mark-price gap/time exits,
    capital-reference observation, trade-open dead-band resizing, then entries.
    Inside the minute, an established stop reads the mark-price low before a
    second capital-reference observation, trade-close resizing, and mark-low
    retrace decisions. A low that crosses a retrace or stop proves the barrier
    was touched but not when; that event is stamped at minute end. Every entry
    decision reads the mark-price stream and every crossing fill reads the
    trade-price stream.
    """

    if decision_reducer is None:
        with RustStrategyContract() as contract:
            return simulate_long_live_physics(
                features=features,
                minute_bars=minute_bars,
                mark_price_bars=mark_price_bars,
                funding=funding,
                config=config,
                capital_reference=capital_reference,
                assumptions=assumptions,
                funding_coverage=funding_coverage,
                signal_start_ms=signal_start_ms,
                signal_end_ms=signal_end_ms,
                decision_reducer=RustLongDecisionReducer(contract, config),
                signal_candidates=signal_candidates,
            )

    physics = assumptions or LivePhysicsAssumptions()
    physics.validate()
    capital_reference.validate()
    candidates = list(signal_candidates) if signal_candidates is not None else extract_signal_candidates(
        features,
        config=config,
        decision_reducer=decision_reducer,
    )
    minute, _trade_high_repairs = _canonical_minute_bars(
        minute_bars,
        dataset="klines_1m",
    )
    mark_minute, _mark_high_repairs = _canonical_minute_bars(
        mark_price_bars,
        dataset="mark_price_1m",
    )
    funding_supplied = _canonical_funding(funding)
    funding_frame, funding_without_mark_price_bar = _funding_with_exact_mark_price_bars(
        funding_supplied,
        mark_minute,
    )
    last_bar_by_symbol: dict[str, tuple[int, float]] = {}
    for row in minute.iter_rows(named=True):
        last_bar_by_symbol[str(row["symbol"])] = (
            int(row["ts_ms"]),
            float(row["close"]),
        )
    candidates_by_symbol: dict[str, list[_Candidate]] = {}
    for candidate in candidates:
        candidates_by_symbol.setdefault(candidate.symbol, []).append(candidate)
    for values in candidates_by_symbol.values():
        values.sort(key=lambda item: item.priority)

    ledger = _Ledger(initial_equity_usdt=physics.initial_equity_usdt)
    capital_reference_state = _CapitalReferenceState(capital_reference)
    positions: dict[str, _Position] = {}
    marks: dict[str, float] = {}
    cooldown_until: dict[str, int] = {}
    attempted_signal: dict[str, int] = {}
    admitted_signal: set[tuple[str, int]] = set()
    entry_status: dict[tuple[str, int], str] = {}
    trade_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    daily_marks: dict[str, float] = {}
    stats: dict[str, int] = {
        "decision_calls": 0,
        "entries": 0,
        "skipped_entry_floor": 0,
        "exits_stop": 0,
        "exits_decayed_stop": 0,
        "exits_time": 0,
        "exits_data_end": 0,
        "resizes": 0,
        "risk_entry_blocks": 0,
        "risk_resize_blocks": 0,
        "stop_decision_minutes_without_exact_mark_bar": 0,
        "funding_settlements_without_exact_mark_price_bar": funding_without_mark_price_bar,
    }

    bar_groups = _time_groups(minute)
    mark_bar_groups = _time_groups(mark_minute)
    funding_groups = _time_groups(funding_frame)
    next_bars = next(bar_groups, None)
    next_mark_bars = next(mark_bar_groups, None)
    next_funding = next(funding_groups, None)
    final_event_ts_ms = 0
    while next_bars is not None or next_funding is not None:
        bar_ts = next_bars[0] if next_bars is not None else None
        funding_ts = next_funding[0] if next_funding is not None else None
        if bar_ts is None:
            assert funding_ts is not None
            now_ms = int(funding_ts)
        elif funding_ts is None:
            now_ms = int(bar_ts)
        else:
            now_ms = min(int(bar_ts), int(funding_ts))
        rows = next_bars[1] if next_bars is not None and bar_ts == now_ms else []
        funding_rows = next_funding[1] if next_funding is not None and funding_ts == now_ms else []
        if next_bars is not None and bar_ts == now_ms:
            next_bars = next(bar_groups, None)
        if next_funding is not None and funding_ts == now_ms:
            next_funding = next(funding_groups, None)
        while next_mark_bars is not None and next_mark_bars[0] < now_ms:
            next_mark_bars = next(mark_bar_groups, None)
        mark_rows = next_mark_bars[1] if next_mark_bars is not None and next_mark_bars[0] == now_ms else []
        if next_mark_bars is not None and next_mark_bars[0] == now_ms:
            next_mark_bars = next(mark_bar_groups, None)
        final_event_ts_ms = max(final_event_ts_ms, now_ms)

        bars_by_symbol = {str(row["symbol"]): row for row in rows}
        mark_bars_by_symbol = {str(row["symbol"]): row for row in mark_rows}
        for symbol, row in bars_by_symbol.items():
            marks[symbol] = float(row["open"])
            if symbol in positions:
                _observe_mark(positions[symbol], marks[symbol])

        _apply_funding(
            funding_rows,
            now_ms=now_ms,
            positions=positions,
            mark_bars_by_symbol=mark_bars_by_symbol,
            ledger=ledger,
        )
        _process_open_exits(
            now_ms=now_ms,
            bars_by_symbol=bars_by_symbol,
            mark_bars_by_symbol=mark_bars_by_symbol,
            positions=positions,
            marks=marks,
            ledger=ledger,
            config=config,
            physics=physics,
            cooldown_until=cooldown_until,
            trade_rows=trade_rows,
            decision_rows=decision_rows,
            stats=stats,
            funding_coverage=funding_coverage,
            decision_reducer=decision_reducer,
        )
        open_cycle_equity_usdt = max(ledger.marked_equity(positions, marks), 0.0)
        capital_reference_state.observe_equity(open_cycle_equity_usdt)
        _apply_deadbands(
            now_ms=now_ms,
            price_field="open",
            bars_by_symbol=bars_by_symbol,
            positions=positions,
            marks=marks,
            ledger=ledger,
            config=config,
            physics=physics,
            account_equity_usdt=open_cycle_equity_usdt,
            capital_reference=capital_reference,
            current_reference_usdt=capital_reference_state.current_usdt,
            stats=stats,
        )
        entries_added = _process_entries(
            now_ms=now_ms,
            phase="minute_open",
            price_field="open",
            observed_low=False,
            decision_bars_by_symbol=mark_bars_by_symbol,
            trade_bars_by_symbol=bars_by_symbol,
            candidates_by_symbol=candidates_by_symbol,
            positions=positions,
            marks=marks,
            ledger=ledger,
            config=config,
            physics=physics,
            cycle_equity_usdt=open_cycle_equity_usdt,
            capital_reference=capital_reference,
            current_reference_usdt=capital_reference_state.current_usdt,
            cooldown_until=cooldown_until,
            attempted_signal=attempted_signal,
            admitted_signal=admitted_signal,
            entry_status=entry_status,
            decision_rows=decision_rows,
            stats=stats,
            max_entries=config.max_new_entries_per_cycle,
            decision_reducer=decision_reducer,
        )

        end_ms = now_ms + MINUTE_BAR_MS
        _process_intraminute_stops(
            end_ms=end_ms,
            bars_by_symbol=bars_by_symbol,
            mark_bars_by_symbol=mark_bars_by_symbol,
            positions=positions,
            marks=marks,
            ledger=ledger,
            config=config,
            physics=physics,
            cooldown_until=cooldown_until,
            trade_rows=trade_rows,
            decision_rows=decision_rows,
            stats=stats,
            funding_coverage=funding_coverage,
            decision_reducer=decision_reducer,
        )
        for symbol, row in bars_by_symbol.items():
            marks[symbol] = float(row["close"])
            if symbol in positions:
                _observe_mark(positions[symbol], marks[symbol])
        close_cycle_equity_usdt = max(ledger.marked_equity(positions, marks), 0.0)
        capital_reference_state.observe_equity(close_cycle_equity_usdt)
        _apply_deadbands(
            now_ms=end_ms,
            price_field="close",
            bars_by_symbol=bars_by_symbol,
            positions=positions,
            marks=marks,
            ledger=ledger,
            config=config,
            physics=physics,
            account_equity_usdt=close_cycle_equity_usdt,
            capital_reference=capital_reference,
            current_reference_usdt=capital_reference_state.current_usdt,
            stats=stats,
        )
        _process_entries(
            now_ms=end_ms,
            phase="minute_touch",
            price_field="close",
            observed_low=True,
            decision_bars_by_symbol=mark_bars_by_symbol,
            trade_bars_by_symbol=bars_by_symbol,
            candidates_by_symbol=candidates_by_symbol,
            positions=positions,
            marks=marks,
            ledger=ledger,
            config=config,
            physics=physics,
            cycle_equity_usdt=close_cycle_equity_usdt,
            capital_reference=capital_reference,
            current_reference_usdt=capital_reference_state.current_usdt,
            cooldown_until=cooldown_until,
            attempted_signal=attempted_signal,
            admitted_signal=admitted_signal,
            entry_status=entry_status,
            decision_rows=decision_rows,
            stats=stats,
            max_entries=max(config.max_new_entries_per_cycle - entries_added, 0),
            decision_reducer=decision_reducer,
        )
        if end_ms % MS_PER_DAY == 0:
            daily_marks[_utc_date(end_ms - 1).isoformat()] = ledger.marked_equity(positions, marks)
        final_event_ts_ms = max(final_event_ts_ms, end_ms)

    for symbol in sorted(tuple(positions)):
        final_bar = last_bar_by_symbol.get(symbol)
        if final_bar is None:
            continue
        last_bar_start_ms, price = final_bar
        _close_position(
            symbol=symbol,
            exit_ts_ms=last_bar_start_ms + MINUTE_BAR_MS,
            reference_price=price,
            reason="data_end",
            positions=positions,
            ledger=ledger,
            config=config,
            physics=physics,
            cooldown_until=cooldown_until,
            trade_rows=trade_rows,
            stats=stats,
            funding_coverage=funding_coverage,
        )

    for candidate in candidates:
        key = (candidate.symbol, candidate.signal_ts_ms)
        if key not in entry_status:
            entry_status[key] = "not_entered"
    daily_equity = _build_daily_equity(
        daily_marks,
        initial_equity_usdt=physics.initial_equity_usdt,
        final_equity_usdt=ledger.cash_equity_usdt,
        signal_start_ms=(
            signal_start_ms
            if signal_start_ms is not None
            else min((item.signal_ts_ms for item in candidates), default=final_event_ts_ms)
        ),
        execution_end_ms=max(final_event_ts_ms, signal_end_ms or 0),
    )
    trades = _frame_or_empty(trade_rows)
    mutations = _frame_or_empty(ledger.mutations)
    funding_output = _frame_or_empty(ledger.funding_rows)
    decisions = _frame_or_empty(decision_rows)
    summary = _summarize(
        trades=trades,
        mutations=mutations,
        daily_equity=daily_equity,
        ledger=ledger,
        candidates=candidates,
        entry_status=entry_status,
        stats=stats,
    )
    capital_reference_metadata = {
        "mode": "account_equity" if capital_reference.tracks_equity else "fixed",
        "configured_seed_usdt": capital_reference.configured_seed_usdt,
        "initial_reference_usdt": capital_reference.configured_seed_usdt,
        "current_reference_usdt": capital_reference_state.current_usdt,
        "final_reference_usdt": capital_reference_state.current_usdt,
        "minimum_reference_usdt": capital_reference_state.minimum_usdt,
        "maximum_reference_usdt": capital_reference_state.maximum_usdt,
        "equity_fraction": capital_reference.equity_fraction,
        "floor_usdt": capital_reference.floor_usdt,
        "expand_dead_band_fraction": capital_reference.expand_dead_band_fraction,
        "close_enough_relative_tolerance": CAPITAL_REFERENCE_CLOSE_REL_TOL,
        "close_enough_absolute_tolerance_usdt": CAPITAL_REFERENCE_CLOSE_ABS_TOL,
        "account_gross_cap_multiple_reference": (capital_reference.account_gross_cap_multiple_reference),
        "account_margin_cap_multiple_reference": (capital_reference.account_margin_cap_multiple_reference),
        "current_account_gross_cap_usdt": (
            capital_reference_state.current_usdt * capital_reference.account_gross_cap_multiple_reference
        ),
        "current_account_margin_cap_usdt": (
            capital_reference_state.current_usdt * capital_reference.account_margin_cap_multiple_reference
        ),
        "observations": capital_reference_state.observations,
        "updates": capital_reference_state.updates,
        "expansions": capital_reference_state.expansions,
        "contractions": capital_reference_state.contractions,
        "provenance": {
            "source": capital_reference.source,
            "source_sha256": capital_reference.source_sha256,
        },
    }
    metadata = {
        "schema_version": LIVE_PHYSICS_SCHEMA_VERSION,
        "run_label": "minute_execution_bound_lane_1",
        "execution_evidence": {
            "strategy_contract_shared_across_environments": True,
            "no_take_profit": True,
            "entry_timing": (
                "mark-price minute open for deadline/gap; first qualifying mark-price "
                "one-minute low for retrace touch, stamped at minute end"
            ),
            "intraminute_order": (
                "a retrace-low entry is stamped after that bar, so no post-touch "
                "stop or resize path is invented inside the same minute"
            ),
            "hold_clock": (
                "first simulated fill; live starts the same clock when the native "
                "engine first reconciles the filled holding"
            ),
            "fill_observation_latency": (
                "the native engine's event and account-reconciliation timing is not "
                "reconstructed from minute bars"
            ),
            "target_notional": (
                "fixed from the native reducer's same-cycle account-equity snapshot"
            ),
            "deadband": "max($1, 5% standing notional, supplied venue minimum)",
            "deadband_scope": (
                "Rust non-exact target resize only: resize when the absolute "
                "target-minus-standing gap is strictly greater than the threshold"
            ),
            "entry_eligibility": (
                "mark-price open/low/close drives the shared decision; the $6 fleet "
                "entry floor is applied separately; historical quantity steps, minimum "
                "quantities, and instrument-rule changes are not reconstructed"
            ),
            "deadband_observation": "one-minute open and close; not every live tick",
            "entry_throttle": (
                "one shared max-new-entry budget across minute-open and minute-touch "
                "observations; additional live decisions inside a minute cannot be reconstructed"
            ),
            "entry_execution": "immediate taker crossing; rest_entries=false policy",
            "entry_fill": (
                "trade-price minute open or close at the modeled decision boundary; "
                "the mark-price trigger never becomes the fill price"
            ),
            "entry_observation_limit": (
                "a mark-price one-minute low can contain a touch between native live "
                "observations that the engine input would miss"
            ),
            "order_latency": "not reconstructed beyond the one-minute observation bound",
            "stop_trigger": ("Bybit mark-price minute open/low/close; never the trade-price low"),
            "stop_fill": (
                "trade-price minute open for a mark-price gap; otherwise the stop "
                "level clamped to that minute's observed trade-price range"
            ),
            "funding": (
                "exact settlement timestamps and rates; mark-price one-minute open is the position-value proxy"
            ),
            "funding_mark_requirement": (
                "a settlement is charged only when that symbol has a loaded mark-price "
                "bar at the exact settlement minute"
            ),
            "data_end_liquidation": (
                "an unresolved open position is closed at its symbol's final loaded "
                "minute end using that minute's close, never at a later funding timestamp"
            ),
            "historical_market_resolution": "1m_trade_ohlc_plus_1m_mark_price_ohlc",
            "tick_or_l1_parity": False,
            "quantity_step_history": False,
            "venue_rejection_or_impact_model": False,
            "account_capacity": (
                "LONG-only gross and initial-margin caps scale from the mutable capital "
                "reference, not raw equity; other sleeves remain absent"
            ),
            "capital_reference_observation": (
                "configured seed, then one modeled marked-equity observation before each "
                "minute-open and minute-close risk wave; contraction is immediate and "
                "expansion must strictly clear the profile dead band"
            ),
        },
        "effective_strategy_config": config.as_json_dict(),
        "capital_reference": capital_reference_metadata,
        "execution_assumptions": {
            **asdict(physics),
            "per_mutation_crossing_cost_bps": (physics.taker_fee_bps + physics.slippage_bps),
        },
        "equity_scale": {
            "label": (
                "normalized_1000_usdt"
                if math.isclose(
                    physics.initial_equity_usdt,
                    1_000.0,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                else "declared_input_not_venue_verified"
            ),
            "venue_balance_verified": False,
            "material_to_result": "yes; $1 resize, $5 venue, and $6 entry floors are absolute",
        },
        "evidence": asdict(physics.evidence),
        "decision_contract": {
            "authority": "rust_long_native",
            "transport": "one_persistent_jsonl_process",
            "publishes_orders": False,
        },
        "rows": {
            "features": features.height,
            "native_signal_candidates": len(candidates),
            "minute_bars": minute.height,
            "trade_price_minute_bars": minute.height,
            "mark_price_minute_bars": mark_minute.height,
            "funding_settlements_supplied": funding_supplied.height,
            "funding_settlements_with_exact_mark_price_bar": funding_frame.height,
            "funding_settlements_without_exact_mark_price_bar": (funding_without_mark_price_bar),
            "trades": trades.height,
            "mutations": mutations.height,
            "funding_settlements_charged": funding_output.height,
        },
        "entry_status_counts": dict(sorted(_counts(entry_status.values()).items())),
        "summary": summary,
        "calendar_years": _era_rows(daily_equity, mode="year"),
        "era_halves": _era_rows(daily_equity, mode="halves"),
        "accounting_reconciliation": {
            "gross_realized_usdt": ledger.gross_realized_usdt,
            "fees_usdt": -ledger.fee_usdt,
            "slippage_usdt": -ledger.slippage_usdt,
            "funding_usdt": ledger.funding_usdt,
            "net_usdt": ledger.cash_equity_usdt - ledger.initial_equity_usdt,
            "reconciles": math.isclose(
                ledger.cash_equity_usdt - ledger.initial_equity_usdt,
                ledger.gross_realized_usdt - ledger.fee_usdt - ledger.slippage_usdt + ledger.funding_usdt,
                rel_tol=0.0,
                abs_tol=1e-9,
            ),
        },
    }
    return LivePhysicsResult(
        trades=trades,
        mutations=mutations,
        funding_events=funding_output,
        daily_equity=daily_equity,
        decisions=decisions,
        metadata=metadata,
    )


def run_long_live_physics_research(
    data_root: str | Path,
    *,
    profile_name: str = "v12",
    operational_profile_path: str | Path = "configs/operational.mainnet.json",
    start: str,
    end: str,
    report_dir: str | Path | None = None,
    assumptions: LivePhysicsAssumptions | None = None,
    command: Sequence[str] = (),
) -> dict[str, Any]:
    """Build PIT signals, load candidate minute paths, simulate, and report."""

    if not start or not end or date_ms(start) >= date_ms(end):
        raise ValueError("start and end-exclusive end must define a positive window")
    physics = assumptions or LivePhysicsAssumptions()
    physics.validate()
    resolved = resolve_live_physics_configuration(
        profile_name=profile_name,
        operational_profile_path=operational_profile_path,
        taker_fee_bps=physics.taker_fee_bps,
        slippage_bps=physics.slippage_bps,
    )
    effective = resolved.strategy
    rule: LongNativeConfig = replace(
        resolve_long_strategy_profile(profile_name),
        start_date=start,
        end_date=end,
    )
    # Import here to keep the pure simulator independent of the broad research
    # input loader. That loader applies the canonical archive-manifest PIT filter.
    from liquidity_migration.research.backtest.long_native import (
        build_long_research_inputs,
    )

    root = Path(data_root).expanduser()
    inputs = build_long_research_inputs(root, config=rule)
    features = inputs["features"]
    with RustStrategyContract() as contract:
        decision_reducer = RustLongDecisionReducer(contract, effective)
        candidates = extract_signal_candidates(
            features,
            config=effective,
            decision_reducer=decision_reducer,
        )
        minute, minute_receipt = load_candidate_minute_tape(
            root,
            candidates,
            dataset="klines_1m",
        )
        mark_minute, mark_minute_receipt = load_candidate_minute_tape(
            root,
            candidates,
            dataset="mark_price_1m",
        )
        funding, _row_span_coverage, funding_sha256 = funding_frame_for_candidates(
            inputs["funding_lookup"],
            candidates,
        )
        funding_coverage, funding_receipt = load_funding_download_coverage(
            root,
            candidates,
        )
        result = simulate_long_live_physics(
            features=features,
            minute_bars=minute,
            mark_price_bars=mark_minute,
            funding=funding,
            config=effective,
            capital_reference=resolved.capital_reference,
            assumptions=physics,
            funding_coverage=funding_coverage,
            signal_start_ms=date_ms(start),
            signal_end_ms=date_ms(end),
            decision_reducer=decision_reducer,
            signal_candidates=candidates,
        )
    output_dir = Path(report_dir).expanduser() if report_dir is not None else root / "reports" / "long_live_physics"
    output_dir.mkdir(parents=True, exist_ok=True)
    source_snapshot_bytes, source_snapshot_identity = _source_snapshot(Path(__file__).resolve().parents[3])
    config_payload = json.dumps(
        {
            "strategy": effective.as_json_dict(),
            "capital_reference": asdict(resolved.capital_reference),
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    feature_payload = json.dumps(
        _json_safe([dict(item.feature_row) for item in candidates]),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )
    metadata = {
        **result.metadata,
        "scope": {
            "venue": "Bybit linear perpetuals",
            "population": "archive-manifest point-in-time members",
            "signal_start": start,
            "signal_end_exclusive": end,
            "initial_equity_usdt": physics.initial_equity_usdt,
        },
        "configuration_resolution": {
            "operational_profile": resolved.operational_profile_source,
            "operational_profile_sha256": resolved.operational_profile_sha256,
            "capital_reference": asdict(resolved.capital_reference),
        },
        "pit_manifest": {
            "rows": inputs["archive_manifest"].height,
            "full_pit_universe_pass": inputs["full_pit_universe_pass"],
            "coverage_scope": inputs["pit_coverage_scope"],
            "required_date_symbols": len(inputs["pit_required_date_symbols"]),
            "covered_date_symbols": len(inputs["pit_covered_date_symbols"]),
            "full_root_full_pit_universe_pass": inputs["full_root_pit_universe_pass"],
            "full_root_required_date_symbols": len(inputs["pit_full_root_required_date_symbols"]),
            "full_root_covered_date_symbols": len(inputs["pit_full_root_covered_date_symbols"]),
            "filter": inputs["pit_filter_receipt"],
        },
        "minute_tape": {**asdict(minute_receipt), "complete": minute_receipt.complete},
        "mark_price_minute_tape": {
            **asdict(mark_minute_receipt),
            "complete": mark_minute_receipt.complete,
        },
        "funding_tape": {
            **asdict(funding_receipt),
            "complete": funding_receipt.complete,
        },
        "taint_reasons": _taint_reasons(
            full_pit_universe_pass=bool(inputs["full_pit_universe_pass"]),
            minute_receipt=minute_receipt,
            mark_minute_receipt=mark_minute_receipt,
            funding_receipt=funding_receipt,
            trades=result.trades,
            funding_settlements_without_exact_mark_price_bar=int(
                result.metadata["rows"]["funding_settlements_without_exact_mark_price_bar"]
            ),
        ),
        "identities": {
            "data_root": str(root.resolve()),
            "selected_minute_partitions_sha256": minute_receipt.selected_file_sha256,
            "selected_mark_price_minute_partitions_sha256": (mark_minute_receipt.selected_file_sha256),
            "selected_funding_rows_sha256": funding_sha256,
            "selected_funding_markers_sha256": funding_receipt.selected_marker_sha256,
            "native_signal_features_sha256": hashlib.sha256(feature_payload.encode("utf-8")).hexdigest(),
            "effective_config_sha256": hashlib.sha256(config_payload.encode("utf-8")).hexdigest(),
            **source_snapshot_identity,
            **_git_identity(),
        },
        "command": list(command),
    }
    metadata["tainted"] = bool(metadata["taint_reasons"])
    if metadata["tainted"]:
        metadata["run_label"] = "minute_execution_diagnostic_tainted"
    _write_result(
        output_dir,
        result,
        metadata,
        source_snapshot_bytes=source_snapshot_bytes,
    )
    return {**metadata, "report_dir": str(output_dir)}


def format_long_live_physics_report(metadata: Mapping[str, Any]) -> str:
    summary = dict(metadata.get("summary") or {})
    scope = dict(metadata.get("scope") or {})
    tape = dict(metadata.get("minute_tape") or {})
    mark_tape = dict(metadata.get("mark_price_minute_tape") or {})
    funding_tape = dict(metadata.get("funding_tape") or {})
    rows = dict(metadata.get("rows") or {})
    evidence = dict(metadata.get("evidence") or {})
    equity_scale = dict(metadata.get("equity_scale") or {})
    capital_reference = dict(metadata.get("capital_reference") or {})
    mark_high_repair_sample = list(mark_tape.get("source_high_repair_raw_sample") or [])
    mark_high_repair_sample_text = json.dumps(
        _json_safe(mark_high_repair_sample),
        sort_keys=True,
        separators=(",", ":"),
    )
    years = list(metadata.get("calendar_years") or [])
    halves = list(metadata.get("era_halves") or [])
    tainted = metadata.get("tainted")
    taint_reasons = list(metadata.get("taint_reasons") or [])
    pit = dict(metadata.get("pit_manifest") or {})
    pit_scope = dict(pit.get("coverage_scope") or {})
    trust = (
        "TAINTED DIAGNOSTIC — do not use the performance numbers"
        if tainted is True
        else (
            "complete trade-and-mark minute execution bound; not tick/L1 parity or unseen evidence"
            if tainted is False
            else "not graded; the caller supplied no tape-integrity verdict"
        )
    )
    lines = [
        "# Native LONG — live-policy minute replay",
        "",
        "## Result",
        "",
        f"- Trust: **{trust}**",
        f"- Taint reasons: {', '.join(str(item) for item in taint_reasons) or 'none'}",
        f"- Causal-input point-in-time universe coverage: `{pit.get('full_pit_universe_pass', 'unknown')}`",
        f"- Full-root point-in-time universe coverage: `{pit.get('full_root_full_pit_universe_pass', 'unknown')}` (informational)",
        "- Causal PIT input window: "
        f"{pit_scope.get('input_start', '')} to "
        f"{pit_scope.get('input_end_exclusive', '')} (end exclusive; "
        f"{pit_scope.get('feature_lookback_days', '')}-day maximum lookback)",
        f"- Label: `{metadata.get('run_label', '')}`",
        f"- Venue/population: {scope.get('venue', '')}; {scope.get('population', '')}",
        f"- Signal window: {scope.get('signal_start', '')} to {scope.get('signal_end_exclusive', '')} (end exclusive)",
        f"- Equity scale: `{equity_scale.get('label', '')}`; venue balance verified=`{equity_scale.get('venue_balance_verified', False)}`",
        "- Capital reference: "
        f"mode=`{capital_reference.get('mode', '')}`; configured seed="
        f"{_money(capital_reference.get('configured_seed_usdt'))}; current/final="
        f"{_money(capital_reference.get('current_reference_usdt'))}/"
        f"{_money(capital_reference.get('final_reference_usdt'))}; updates="
        f"{capital_reference.get('updates', 0)}",
        f"- Trades: {summary.get('trades', 0)}",
        f"- Net return: {_pct(summary.get('total_return'))}",
        f"- Gross price P&L: {_money(summary.get('gross_price_pnl_usdt'))}",
        f"- Fees / slippage / funding: {_money(summary.get('fees_usdt'))} / {_money(summary.get('slippage_usdt'))} / {_money(summary.get('funding_usdt'))}",
        f"- Daily smoothness score (Sharpe): {summary.get('daily_sharpe')}",
        f"- Worst dip (max drawdown): {_pct(summary.get('max_drawdown'))}",
        f"- Dead-band resizes: {summary.get('resizes', 0)}",
        "",
        "## Execution boundary",
        "",
        "The replay calls the native Rust decision reducer. Entry eligibility uses one-minute "
        "Bybit mark-price opens, lows, and closes, matching the live mark-price-first snapshot. "
        "Entry fills, resizing, and account marks use the separate traded-price stream. Exchange-native "
        "stop decisions also use the mark-price minute open and low, matching the live MarkPrice "
        "trigger. A low proves a touch, but not its exact time or fill; a retrace fill uses the "
        "trade-price close and is stamped at minute end, with no later path invented inside that bar. "
        "A mark-price touch between native live observations could be missed even though the "
        "minute tape records it. "
        "The mark-price loader may expand a source high that misses open or close by at most 1 bp; "
        "it records the raw row and never changes decision-used open, low, or close. "
        "Dead-band checks happen at trade-price minute open and close, not every tick. Funding uses "
        "the exact settlement timestamp and rate only when the symbol has a mark-price bar at that "
        "exact minute; the mark-price minute open is the settlement position-value proxy. The "
        "capital reference starts at the typed profile seed, observes the same modeled equity snapshot "
        "used by each sizing wave before risk admission, contracts immediately, and expands only "
        "strictly beyond the profile dead band. Live can refresh venue equity and admit risk between "
        "these minute-open and minute-close observations, which this replay does not reconstruct. "
        "This is not tick, "
        "L1, queue, impact, quantity-step, or venue-rejection parity. The replay starts "
        "hold and stop clocks at its simulated fill. Live starts those clocks when the "
        "native engine first reconciles the venue fill; minute bars do not reconstruct "
        "that observation delay.",
        "",
        f"Trade-price minute tape complete: {bool(tape.get('complete', False))}; missing symbol-days={tape.get('missing_symbol_days', 0)}, missing minutes={tape.get('missing_minutes', 0)}.",
        f"Mark-price minute tape complete: {bool(mark_tape.get('complete', False))}; missing symbol-days={mark_tape.get('missing_symbol_days', 0)}, missing minutes={mark_tape.get('missing_minutes', 0)}.",
        "Mark-price source high repairs: "
        f"count={int(mark_tape.get('source_high_repair_count') or 0)}, "
        f"max gap={_finite(mark_tape.get('source_high_repair_max_gap_bps'), 0.0):.6f} bp, "
        f"raw sample={mark_high_repair_sample_text}.",
        f"Funding download coverage complete: {bool(funding_tape.get('complete', False))}; covered intervals={funding_tape.get('covered_intervals', 0)}/{funding_tape.get('required_intervals', 0)}, missing intervals={funding_tape.get('missing_intervals', 0)}.",
        f"Funding rows without an exact mark-price minute bar: {rows.get('funding_settlements_without_exact_mark_price_bar', 0)}.",
        "",
        "## Evidence",
        "",
        f"- Lane: {evidence.get('lane', '')}",
        f"- Data that shaped the rule: {evidence.get('shaped_data', '')}",
        f"- Data that graded it: {evidence.get('graded_data', '')}",
        "- Non-conclusions: " + "; ".join(evidence.get("non_conclusions") or []),
        "",
        "## Calendar years",
        "",
        "| Era | Start equity | End equity | Return | Days |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    lines.extend(_render_era(row) for row in years)
    lines.extend(
        [
            "",
            "## Era halves",
            "",
            "| Era | Start equity | End equity | Return | Days |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    lines.extend(_render_era(row) for row in halves)
    lines.extend(
        [
            "",
            "## Identities",
            "",
            f"- Git commit: `{(metadata.get('identities') or {}).get('git_commit', '')}`",
            f"- Dirty tree: `{(metadata.get('identities') or {}).get('git_dirty', '')}`",
            f"- Exact source snapshot: `{(metadata.get('identities') or {}).get('source_snapshot_file', '')}` ({(metadata.get('identities') or {}).get('source_snapshot_files', 0)} files)",
            f"- Exact source snapshot SHA-256: `{(metadata.get('identities') or {}).get('source_snapshot_sha256', '')}`",
            f"- Effective config SHA-256: `{(metadata.get('identities') or {}).get('effective_config_sha256', '')}`",
            f"- Capital-reference profile: `{(capital_reference.get('provenance') or {}).get('source', '')}`",
            f"- Capital-reference profile SHA-256: `{(capital_reference.get('provenance') or {}).get('source_sha256', '')}`",
            f"- Selected trade-price minute partitions SHA-256: `{(metadata.get('identities') or {}).get('selected_minute_partitions_sha256', '')}`",
            f"- Selected mark-price minute partitions SHA-256: `{(metadata.get('identities') or {}).get('selected_mark_price_minute_partitions_sha256', '')}`",
            f"- Selected funding rows SHA-256: `{(metadata.get('identities') or {}).get('selected_funding_rows_sha256', '')}`",
            f"- Selected funding markers SHA-256: `{(metadata.get('identities') or {}).get('selected_funding_markers_sha256', '')}`",
            "",
        ]
    )
    return "\n".join(lines)


def _process_entries(
    *,
    now_ms: int,
    phase: str,
    price_field: str,
    observed_low: bool,
    decision_bars_by_symbol: Mapping[str, Mapping[str, Any]],
    trade_bars_by_symbol: Mapping[str, Mapping[str, Any]],
    candidates_by_symbol: Mapping[str, Sequence[_Candidate]],
    positions: dict[str, _Position],
    marks: dict[str, float],
    ledger: _Ledger,
    config: StrategyConfig,
    physics: LivePhysicsAssumptions,
    cycle_equity_usdt: float,
    capital_reference: LivePhysicsCapitalReference,
    current_reference_usdt: float,
    cooldown_until: Mapping[str, int],
    attempted_signal: dict[str, int],
    admitted_signal: set[tuple[str, int]],
    entry_status: dict[tuple[str, int], str],
    decision_rows: list[dict[str, Any]],
    stats: dict[str, int],
    max_entries: int,
    decision_reducer: RustLongDecisionReducer,
) -> int:
    eligible: list[tuple[_Candidate, Mapping[str, Any]]] = []
    for symbol, decision_bar in decision_bars_by_symbol.items():
        if symbol in positions or symbol not in trade_bars_by_symbol:
            continue
        for candidate in candidates_by_symbol.get(symbol, ()):
            key = (candidate.symbol, candidate.signal_ts_ms)
            if key in admitted_signal or now_ms < candidate.first_check_ts_ms or now_ms >= candidate.stale_ts_ms:
                continue
            eligible.append((candidate, decision_bar))
    eligible.sort(key=lambda item: item[0].priority)
    entries_added = 0
    for candidate, decision_bar in eligible:
        if candidate.symbol in positions:
            continue
        key = (candidate.symbol, candidate.signal_ts_ms)
        decision_price = _finite(decision_bar.get(price_field), 0.0)
        decision_low = _finite(decision_bar.get("low"), 0.0) if observed_low else None
        trade_bar = trade_bars_by_symbol[candidate.symbol]
        trade_price = _finite(trade_bar.get(price_field), 0.0)
        if decision_price <= 0.0 or trade_price <= 0.0:
            continue
        output = decision_reducer.decide(
            DecisionInput(
                decision_ts_ms=now_ms,
                symbol=candidate.symbol,
                signal_ts_ms=candidate.signal_ts_ms,
                signal_close=candidate.signal_close,
                market_price=decision_price,
                observed_low=decision_low,
                equity_usdt=cycle_equity_usdt,
                feature_row=candidate.feature_row,
            ),
            PriorState(
                cooldown_until_ms=int(cooldown_until.get(candidate.symbol, 0)),
                attempted_signal_ts_ms=int(attempted_signal.get(candidate.symbol, 0)),
                active_positions=len(positions),
            ),
        )
        stats["decision_calls"] += 1
        if output.action is not DecisionAction.ENTER:
            continue
        if entries_added >= max(max_entries, 0):
            entry_status.setdefault(key, "throttled_then_retried")
            continue
        if output.target_notional_usdt < config.entry_floor_usdt:
            attempted_signal[candidate.symbol] = candidate.signal_ts_ms
            admitted_signal.add(key)
            entry_status[key] = "entry_below_engine_floor"
            stats["skipped_entry_floor"] += 1
            continue
        if not _risk_admits_target(
            positions=positions,
            marks=marks,
            symbol=candidate.symbol,
            target_notional_usdt=output.target_notional_usdt,
            target_leverage=output.entry_leverage,
            account_equity_usdt=cycle_equity_usdt,
            capital_reference=capital_reference,
            current_reference_usdt=current_reference_usdt,
        ):
            attempted_signal[candidate.symbol] = candidate.signal_ts_ms
            admitted_signal.add(key)
            entry_status[key] = "risk_refused"
            stats["risk_entry_blocks"] += 1
            continue
        reference_price = trade_price
        _open_position(
            output=output,
            candidate=candidate,
            now_ms=now_ms,
            reference_price=reference_price,
            equity_usdt=cycle_equity_usdt,
            positions=positions,
            ledger=ledger,
            physics=physics,
            phase=phase,
        )
        marks[candidate.symbol] = trade_price
        attempted_signal[candidate.symbol] = candidate.signal_ts_ms
        admitted_signal.add(key)
        entry_status[key] = "entered"
        entries_added += 1
        stats["entries"] += 1
        decision_rows.append(
            {
                **output.as_json_dict(),
                "phase": phase,
                "entry_signal_stream": "mark_price",
                "entry_fill_stream": "trade_price",
                "reference_fill_price": reference_price,
            }
        )
    return entries_added


def _open_position(
    *,
    output: DecisionOutput,
    candidate: _Candidate,
    now_ms: int,
    reference_price: float,
    equity_usdt: float,
    positions: dict[str, _Position],
    ledger: _Ledger,
    physics: LivePhysicsAssumptions,
    phase: str,
) -> None:
    fill_price = reference_price * (1.0 + physics.slippage_bps / 10_000.0)
    position = _Position(
        symbol=candidate.symbol,
        signal_ts_ms=candidate.signal_ts_ms,
        entry_ts_ms=now_ms,
        entry_reason=output.entry_reason,
        entry_equity_usdt=equity_usdt,
        target_fraction_of_equity=output.target_fraction_of_equity,
        target_notional_usdt=output.target_notional_usdt,
        entry_leverage=output.entry_leverage,
        stop_loss_fraction=output.stop_loss_fraction,
        stop_decay_after_ms=output.stop_decay_after_ms,
        decayed_stop_loss_fraction=output.decayed_stop_loss_fraction,
        max_hold_deadline_ts_ms=now_ms + output.max_hold_duration_ms,
        entry_reference_price=reference_price,
        entry_fill_price=fill_price,
    )
    positions[candidate.symbol] = position
    _mutate_position(
        position,
        ts_ms=now_ms,
        quantity_delta=output.target_notional_usdt / reference_price,
        reference_price=reference_price,
        reason=f"entry:{phase}:{output.entry_reason}",
        ledger=ledger,
        physics=physics,
    )
    _observe_mark(position, reference_price)


def _process_open_exits(
    *,
    now_ms: int,
    bars_by_symbol: Mapping[str, Mapping[str, Any]],
    mark_bars_by_symbol: Mapping[str, Mapping[str, Any]],
    positions: dict[str, _Position],
    marks: Mapping[str, float],
    ledger: _Ledger,
    config: StrategyConfig,
    physics: LivePhysicsAssumptions,
    cooldown_until: dict[str, int],
    trade_rows: list[dict[str, Any]],
    decision_rows: list[dict[str, Any]],
    stats: dict[str, int],
    funding_coverage: Mapping[str, tuple[tuple[int, int], ...]] | None,
    decision_reducer: RustLongDecisionReducer,
) -> None:
    for symbol in sorted(tuple(positions)):
        bar = bars_by_symbol.get(symbol)
        if bar is None:
            continue
        position = positions[symbol]
        trade_open = float(bar["open"])
        mark_bar = mark_bars_by_symbol.get(symbol)
        mark_open = _finite(mark_bar.get("open"), 0.0) if mark_bar is not None else 0.0
        output = decision_reducer.decide(
            DecisionInput(
                decision_ts_ms=now_ms,
                symbol=symbol,
                signal_ts_ms=position.signal_ts_ms,
                market_price=mark_open,
            ),
            _prior(position),
        )
        stats["decision_calls"] += 1
        if output.action is DecisionAction.EXIT:
            _close_position(
                symbol=symbol,
                exit_ts_ms=now_ms,
                reference_price=trade_open,
                reason=output.reason,
                positions=positions,
                ledger=ledger,
                config=config,
                physics=physics,
                cooldown_until=cooldown_until,
                trade_rows=trade_rows,
                stats=stats,
                funding_coverage=funding_coverage,
            )
            decision_rows.append(
                {
                    **output.as_json_dict(),
                    "phase": "minute_open",
                    **(
                        {"stop_trigger_stream": "mark_price"}
                        if output.reason in {"stop_loss", "decayed_stop_loss"}
                        else {}
                    ),
                }
            )


def _process_intraminute_stops(
    *,
    end_ms: int,
    bars_by_symbol: Mapping[str, Mapping[str, Any]],
    mark_bars_by_symbol: Mapping[str, Mapping[str, Any]],
    positions: dict[str, _Position],
    marks: Mapping[str, float],
    ledger: _Ledger,
    config: StrategyConfig,
    physics: LivePhysicsAssumptions,
    cooldown_until: dict[str, int],
    trade_rows: list[dict[str, Any]],
    decision_rows: list[dict[str, Any]],
    stats: dict[str, int],
    funding_coverage: Mapping[str, tuple[tuple[int, int], ...]] | None,
    decision_reducer: RustLongDecisionReducer,
) -> None:
    del marks
    bar_start_ms = end_ms - MINUTE_BAR_MS
    for symbol in sorted(tuple(positions)):
        trade_bar = bars_by_symbol.get(symbol)
        if trade_bar is None:
            continue
        mark_bar = mark_bars_by_symbol.get(symbol)
        if mark_bar is None:
            stats["stop_decision_minutes_without_exact_mark_bar"] += 1
            continue
        position = positions[symbol]
        mark_low = float(mark_bar["low"])
        mark_close = float(mark_bar["close"])
        output = decision_reducer.decide(
            DecisionInput(
                # The low is known only when the minute closes. Evaluate it
                # against the stop that was active for that whole minute;
                # otherwise a stop that arms exactly at ``end_ms`` reaches
                # backward into prices observed before it existed.
                decision_ts_ms=bar_start_ms,
                symbol=symbol,
                signal_ts_ms=position.signal_ts_ms,
                market_price=mark_close,
                observed_low=mark_low,
            ),
            _prior(position),
        )
        stats["decision_calls"] += 1
        if output.action is DecisionAction.EXIT and output.reason in {"stop_loss", "decayed_stop_loss"}:
            stop_price = position.average_fill_price * (1.0 - output.stop_loss_fraction)
            if float(mark_bar["open"]) <= stop_price:
                reference_price = float(trade_bar["open"])
            else:
                reference_price = min(
                    max(stop_price, float(trade_bar["low"])),
                    float(trade_bar["high"]),
                )
            _close_position(
                symbol=symbol,
                exit_ts_ms=end_ms,
                reference_price=reference_price,
                reason=output.reason,
                positions=positions,
                ledger=ledger,
                config=config,
                physics=physics,
                cooldown_until=cooldown_until,
                trade_rows=trade_rows,
                stats=stats,
                funding_coverage=funding_coverage,
            )
            decision_rows.append(
                {
                    **output.as_json_dict(),
                    "phase": "minute_low",
                    "observation_end_ts_ms": end_ms,
                    "stop_trigger_stream": "mark_price",
                }
            )


def _apply_deadbands(
    *,
    now_ms: int,
    price_field: str,
    bars_by_symbol: Mapping[str, Mapping[str, Any]],
    positions: Mapping[str, _Position],
    marks: Mapping[str, float],
    ledger: _Ledger,
    config: StrategyConfig,
    physics: LivePhysicsAssumptions,
    account_equity_usdt: float,
    capital_reference: LivePhysicsCapitalReference,
    current_reference_usdt: float,
    stats: dict[str, int],
) -> None:
    for symbol in sorted(positions):
        bar = bars_by_symbol.get(symbol)
        if bar is None:
            continue
        price = float(bar[price_field])
        position = positions[symbol]
        standing = position.quantity * price
        threshold = deadband_threshold_usdt(
            standing,
            config=config,
            venue_min_notional_usdt=physics.venue_min_notional_usdt,
        )
        gap = position.target_notional_usdt - standing
        if abs(gap) <= threshold:
            continue
        target_qty = position.target_notional_usdt / price
        delta = target_qty - position.quantity
        if abs(delta) <= 1e-15:
            continue
        if delta > 0.0 and not _risk_admits_target(
            positions=positions,
            marks=marks,
            symbol=symbol,
            target_notional_usdt=position.target_notional_usdt,
            target_leverage=position.entry_leverage,
            account_equity_usdt=account_equity_usdt,
            capital_reference=capital_reference,
            current_reference_usdt=current_reference_usdt,
        ):
            stats["risk_resize_blocks"] += 1
            continue
        _mutate_position(
            position,
            ts_ms=now_ms,
            quantity_delta=delta,
            reference_price=price,
            reason=f"deadband_resize:{price_field}",
            ledger=ledger,
            physics=physics,
        )
        position.resize_count += 1
        stats["resizes"] += 1


def _apply_funding(
    rows: Sequence[Mapping[str, Any]],
    *,
    now_ms: int,
    positions: Mapping[str, _Position],
    mark_bars_by_symbol: Mapping[str, Mapping[str, Any]],
    ledger: _Ledger,
) -> None:
    for row in sorted(rows, key=lambda item: str(item["symbol"])):
        symbol = str(row["symbol"])
        position = positions.get(symbol)
        if position is None:
            continue
        mark_bar = mark_bars_by_symbol.get(symbol)
        mark_price = _finite(mark_bar.get("open"), 0.0) if mark_bar is not None else 0.0
        if mark_price <= 0.0:
            raise RuntimeError(f"funding settlement has no contemporaneous mark-price minute open: {symbol} {now_ms}")
        rate = float(row["funding_rate"])
        payment = -position.quantity * mark_price * rate
        position.funding_usdt += payment
        position.funding_event_count += 1
        ledger.funding_usdt += payment
        receipt = {
            "ts_ms": now_ms,
            "symbol": symbol,
            "funding_rate": rate,
            "quantity": position.quantity,
            "price_proxy": mark_price,
            "price_stream": "mark_price_1m_open",
            "funding_usdt": payment,
        }
        ledger.funding_rows.append(receipt)


def _risk_admits_target(
    *,
    positions: Mapping[str, _Position],
    marks: Mapping[str, float],
    symbol: str,
    target_notional_usdt: float,
    target_leverage: float,
    account_equity_usdt: float,
    capital_reference: LivePhysicsCapitalReference,
    current_reference_usdt: float,
) -> bool:
    """Apply the profile's reference-scaled caps to a LONG-only book."""

    if (
        not math.isfinite(account_equity_usdt)
        or account_equity_usdt <= 0.0
        or not math.isfinite(current_reference_usdt)
        or current_reference_usdt <= 0.0
        or target_leverage <= 0.0
    ):
        return False
    gross = 0.0
    margin = 0.0
    for held_symbol, position in positions.items():
        if held_symbol == symbol:
            notional = abs(float(target_notional_usdt))
            leverage = float(target_leverage)
        else:
            mark = _finite(marks.get(held_symbol), 0.0)
            notional = abs(position.quantity * mark) if mark > 0.0 else abs(position.target_notional_usdt)
            leverage = position.entry_leverage
        gross += notional
        margin += notional / leverage
    if symbol not in positions:
        gross += abs(float(target_notional_usdt))
        margin += abs(float(target_notional_usdt)) / float(target_leverage)
    tolerance = max(
        CAPITAL_REFERENCE_CLOSE_ABS_TOL,
        current_reference_usdt * CAPITAL_REFERENCE_CLOSE_REL_TOL,
    )
    return (
        gross <= current_reference_usdt * capital_reference.account_gross_cap_multiple_reference + tolerance
        and margin <= current_reference_usdt * capital_reference.account_margin_cap_multiple_reference + tolerance
    )


def _mutate_position(
    position: _Position,
    *,
    ts_ms: int,
    quantity_delta: float,
    reference_price: float,
    reason: str,
    ledger: _Ledger,
    physics: LivePhysicsAssumptions,
) -> None:
    if not math.isfinite(quantity_delta) or not math.isfinite(reference_price) or reference_price <= 0.0:
        raise ValueError("position mutation must have finite quantity and positive price")
    old_qty = position.quantity
    new_qty = old_qty + quantity_delta
    if new_qty < -1e-10:
        raise RuntimeError("LONG target follower cannot cross through flat")
    if abs(new_qty) < 1e-12:
        new_qty = 0.0
        quantity_delta = -old_qty
    slippage_rate = physics.slippage_bps / 10_000.0
    fill_price = reference_price * (1.0 + slippage_rate if quantity_delta > 0.0 else 1.0 - slippage_rate)
    mutation_notional = abs(quantity_delta) * reference_price
    fee = mutation_notional * physics.taker_fee_bps / 10_000.0
    slippage = mutation_notional * slippage_rate
    gross_realized = 0.0
    if quantity_delta > 0.0:
        position.average_reference_price = (
            old_qty * position.average_reference_price + quantity_delta * reference_price
        ) / new_qty
        position.average_fill_price = (old_qty * position.average_fill_price + quantity_delta * fill_price) / new_qty
    elif quantity_delta < 0.0:
        close_qty = -quantity_delta
        gross_realized = close_qty * (reference_price - position.average_reference_price)
        if new_qty == 0.0:
            position.average_reference_price = 0.0
            position.average_fill_price = 0.0
    position.quantity = new_qty
    position.gross_realized_usdt += gross_realized
    position.fee_usdt += fee
    position.slippage_usdt += slippage
    position.mutation_count += 1
    position.mutation_notional_usdt += mutation_notional
    ledger.gross_realized_usdt += gross_realized
    ledger.fee_usdt += fee
    ledger.slippage_usdt += slippage
    ledger.mutations.append(
        {
            "ts_ms": ts_ms,
            "symbol": position.symbol,
            "reason": reason,
            "quantity_before": old_qty,
            "quantity_delta": quantity_delta,
            "quantity_after": new_qty,
            "reference_price": reference_price,
            "fill_price": fill_price,
            "target_notional_usdt": position.target_notional_usdt,
            "standing_notional_before_usdt": old_qty * reference_price,
            "standing_notional_after_usdt": new_qty * reference_price,
            "gross_realized_usdt": gross_realized,
            "fee_usdt": fee,
            "slippage_usdt": slippage,
        }
    )


def _close_position(
    *,
    symbol: str,
    exit_ts_ms: int,
    reference_price: float,
    reason: str,
    positions: dict[str, _Position],
    ledger: _Ledger,
    config: StrategyConfig,
    physics: LivePhysicsAssumptions,
    cooldown_until: dict[str, int],
    trade_rows: list[dict[str, Any]],
    stats: dict[str, int],
    funding_coverage: Mapping[str, tuple[tuple[int, int], ...]] | None,
) -> None:
    position = positions[symbol]
    _mutate_position(
        position,
        ts_ms=exit_ts_ms,
        quantity_delta=-position.quantity,
        reference_price=reference_price,
        reason=f"exit:{reason}",
        ledger=ledger,
        physics=physics,
    )
    net = position.gross_realized_usdt - position.fee_usdt - position.slippage_usdt + position.funding_usdt
    coverage = (funding_coverage or {}).get(symbol, ())
    if not coverage:
        funding_mode = "unverified"
    elif any(position.entry_ts_ms >= start_ms and exit_ts_ms <= end_ms for start_ms, end_ms in coverage):
        funding_mode = "modeled"
    else:
        funding_mode = "partial"
    trade_rows.append(
        {
            "trade_id": f"native-{position.signal_ts_ms}-{symbol}",
            "symbol": symbol,
            "side": "long",
            "signal_ts_ms": position.signal_ts_ms,
            "entry_ts_ms": position.entry_ts_ms,
            "exit_ts_ms": exit_ts_ms,
            "entry_reason": position.entry_reason,
            "exit_reason": reason,
            "entry_reference_price": position.entry_reference_price,
            "entry_fill_price": position.entry_fill_price,
            "exit_reference_price": reference_price,
            "entry_equity_usdt": position.entry_equity_usdt,
            "target_fraction_of_equity": position.target_fraction_of_equity,
            "target_notional_usdt": position.target_notional_usdt,
            "entry_leverage": position.entry_leverage,
            "stop_loss_fraction": position.stop_loss_fraction,
            "decayed_stop_loss_fraction": position.decayed_stop_loss_fraction,
            "max_hold_deadline_ts_ms": position.max_hold_deadline_ts_ms,
            "hold_minutes": (exit_ts_ms - position.entry_ts_ms) / MS_PER_MINUTE,
            "gross_price_pnl_usdt": position.gross_realized_usdt,
            "fee_usdt": position.fee_usdt,
            "slippage_usdt": position.slippage_usdt,
            "funding_usdt": position.funding_usdt,
            "net_pnl_usdt": net,
            "net_return_on_entry_equity": (
                net / position.entry_equity_usdt if position.entry_equity_usdt > 0.0 else None
            ),
            "funding_mode": funding_mode,
            "funding_event_count": position.funding_event_count,
            "mutation_count": position.mutation_count,
            "resize_count": position.resize_count,
            "mutation_notional_usdt": position.mutation_notional_usdt,
            "minimum_mark": (position.minimum_mark if math.isfinite(position.minimum_mark) else None),
            "maximum_mark": (position.maximum_mark if math.isfinite(position.maximum_mark) else None),
        }
    )
    cooldown_until[symbol] = exit_ts_ms + exact_duration_ms(days=config.rule.cooldown_days)
    del positions[symbol]
    if reason == "time_stop":
        stats["exits_time"] += 1
    elif reason == "decayed_stop_loss":
        stats["exits_decayed_stop"] += 1
    elif reason == "data_end":
        stats["exits_data_end"] += 1
    else:
        stats["exits_stop"] += 1


def _prior(position: _Position) -> PriorState:
    return PriorState(
        requested=True,
        filled=True,
        entry_ts_ms=position.entry_ts_ms,
        entry_price=position.average_fill_price,
        target_notional_usdt=position.target_notional_usdt,
        stop_loss_fraction=position.stop_loss_fraction,
        stop_decay_after_ms=position.stop_decay_after_ms,
        decayed_stop_loss_fraction=position.decayed_stop_loss_fraction,
        max_hold_deadline_ts_ms=position.max_hold_deadline_ts_ms,
    )


def _canonical_minute_bars(
    value: pl.DataFrame,
    *,
    dataset: str,
) -> tuple[pl.DataFrame, tuple[MinuteTapeHighRepair, ...]]:
    if dataset not in {"klines_1m", "mark_price_1m"}:
        raise ValueError("one-minute dataset must be 'klines_1m' or 'mark_price_1m'")
    if value.is_empty():
        return _empty_minute_bars(), ()
    required = {"ts_ms", "symbol", "open", "high", "low", "close"}
    missing = required - set(value.columns)
    if missing:
        raise ValueError(f"one-minute bars are missing columns: {sorted(missing)}")
    selected = value.select(sorted(required)).with_columns(
        pl.col("ts_ms").cast(pl.Int64),
        pl.col("symbol").cast(pl.String),
        *[pl.col(name).cast(pl.Float64) for name in ("open", "high", "low", "close")],
    )
    conflicts = (
        selected.group_by(["symbol", "ts_ms"])
        .agg(*[pl.col(name).n_unique().alias(name) for name in ("open", "high", "low", "close")])
        .filter(pl.any_horizontal([pl.col(name) > 1 for name in ("open", "high", "low", "close")]))
    )
    if not conflicts.is_empty():
        raise ValueError(f"one-minute tape has conflicting duplicate bars: {conflicts.head(3).to_dicts()}")
    selected = selected.unique(["symbol", "ts_ms"], keep="first")
    invalid_values = selected.filter(
        (pl.col("ts_ms") <= 0)
        | (pl.col("ts_ms") % MINUTE_BAR_MS != 0)
        | pl.any_horizontal(
            [
                pl.col(name).is_null() | ~pl.col(name).is_finite() | (pl.col(name) <= 0.0)
                for name in ("open", "high", "low", "close")
            ]
        )
    )
    if not invalid_values.is_empty():
        raise ValueError(f"one-minute tape has invalid values: {invalid_values.head(3).to_dicts()}")

    invalid_low = selected.filter(pl.col("low") > pl.min_horizontal("open", "close", "high"))
    if not invalid_low.is_empty():
        raise ValueError(f"one-minute tape has invalid low ordering: {invalid_low.head(3).to_dicts()}")

    endpoint_high = pl.max_horizontal("open", "close")
    high_defects = selected.filter(pl.col("high") < endpoint_high).sort(["ts_ms", "symbol"])
    if high_defects.is_empty():
        return selected.sort(["ts_ms", "symbol"]), ()
    if dataset != "mark_price_1m":
        raise ValueError(f"one-minute tape has invalid high ordering: {high_defects.head(3).to_dicts()}")

    repairs: list[MinuteTapeHighRepair] = []
    excessive: list[dict[str, Any]] = []
    for row in high_defects.iter_rows(named=True):
        raw_open = float(row["open"])
        raw_high = float(row["high"])
        raw_low = float(row["low"])
        raw_close = float(row["close"])
        repaired_high = max(raw_open, raw_close)
        gap_bps = (repaired_high - raw_high) / repaired_high * 10_000.0
        if gap_bps > MAX_MARK_HIGH_SOURCE_REPAIR_BPS:
            excessive.append({**row, "source_high_gap_bps": gap_bps})
            continue
        repairs.append(
            MinuteTapeHighRepair(
                ts_ms=int(row["ts_ms"]),
                symbol=str(row["symbol"]),
                raw_open=raw_open,
                raw_high=raw_high,
                raw_low=raw_low,
                raw_close=raw_close,
                repaired_high=repaired_high,
                gap_bps=gap_bps,
            )
        )
    if excessive:
        raise ValueError(f"mark-price minute tape high defect exceeds the 1 bp source-repair bound: {excessive[:3]}")

    selected = selected.with_columns(
        pl.when(pl.col("high") < endpoint_high).then(endpoint_high).otherwise(pl.col("high")).alias("high")
    )
    invalid_envelope = selected.filter(
        (pl.col("low") > pl.min_horizontal("open", "close", "high"))
        | (pl.col("high") < pl.max_horizontal("open", "close", "low"))
    )
    if not invalid_envelope.is_empty():
        raise ValueError(f"one-minute tape has invalid OHLC rows: {invalid_envelope.head(3).to_dicts()}")
    return selected.sort(["ts_ms", "symbol"]), tuple(repairs)


def _canonical_funding(value: pl.DataFrame | None) -> pl.DataFrame:
    if value is None or value.is_empty():
        return _empty_funding()
    required = {"ts_ms", "symbol", "funding_rate"}
    missing = required - set(value.columns)
    if missing:
        raise ValueError(f"funding rows are missing columns: {sorted(missing)}")
    selected = value.select(sorted(required)).with_columns(
        pl.col("ts_ms").cast(pl.Int64),
        pl.col("symbol").cast(pl.String),
        pl.col("funding_rate").cast(pl.Float64),
    )
    conflicts = (
        selected.group_by(["symbol", "ts_ms"])
        .agg(pl.col("funding_rate").n_unique().alias("rates"))
        .filter(pl.col("rates") > 1)
    )
    if not conflicts.is_empty():
        raise ValueError(f"funding tape has conflicting duplicate settlements: {conflicts.head(3).to_dicts()}")
    selected = selected.unique(["symbol", "ts_ms"], keep="first")
    invalid = selected.filter(
        (pl.col("ts_ms") <= 0) | pl.col("funding_rate").is_null() | ~pl.col("funding_rate").is_finite()
    )
    if not invalid.is_empty():
        raise ValueError(f"funding tape has invalid rows: {invalid.head(3).to_dicts()}")
    return selected.sort(["ts_ms", "symbol"])


def _funding_with_exact_mark_price_bars(
    funding: pl.DataFrame,
    mark_price_minute: pl.DataFrame,
) -> tuple[pl.DataFrame, int]:
    """Keep settlements with a mark-price bar at that exact minute."""

    if funding.is_empty():
        return funding, 0
    if mark_price_minute.is_empty():
        return _empty_funding(), funding.height
    minute_keys = mark_price_minute.select(["ts_ms", "symbol"]).unique()
    supported = funding.join(minute_keys, on=["ts_ms", "symbol"], how="semi").sort(["ts_ms", "symbol"])
    return supported, funding.height - supported.height


def _time_groups(frame: pl.DataFrame) -> Iterator[tuple[int, list[dict[str, Any]]]]:
    if frame.is_empty():
        return
    rows = frame.iter_rows(named=True)
    for raw_ts, group in itertools.groupby(rows, key=lambda row: int(row["ts_ms"])):
        yield raw_ts, list(group)


def _build_daily_equity(
    marks: Mapping[str, float],
    *,
    initial_equity_usdt: float,
    final_equity_usdt: float,
    signal_start_ms: int,
    execution_end_ms: int,
) -> pl.DataFrame:
    if execution_end_ms <= 0:
        return pl.DataFrame(
            {
                "date": pl.Series([], dtype=pl.String),
                "equity_usdt": pl.Series([], dtype=pl.Float64),
                "daily_return": pl.Series([], dtype=pl.Float64),
                "drawdown": pl.Series([], dtype=pl.Float64),
            }
        )
    start = _utc_date(signal_start_ms)
    end = _utc_date(max(signal_start_ms, execution_end_ms - 1))
    rows: list[dict[str, Any]] = []
    previous = initial_equity_usdt
    peak = initial_equity_usdt
    day = start
    while day <= end:
        date_text = day.isoformat()
        equity = float(marks.get(date_text, previous))
        if day == end:
            equity = final_equity_usdt
        daily_return = equity / previous - 1.0 if previous > 0.0 else 0.0
        peak = max(peak, equity)
        rows.append(
            {
                "date": date_text,
                "equity_usdt": equity,
                "daily_return": daily_return,
                "drawdown": equity / peak - 1.0 if peak > 0.0 else 0.0,
            }
        )
        previous = equity
        day += dt.timedelta(days=1)
    return pl.DataFrame(rows, infer_schema_length=None)


def _summarize(
    *,
    trades: pl.DataFrame,
    mutations: pl.DataFrame,
    daily_equity: pl.DataFrame,
    ledger: _Ledger,
    candidates: Sequence[_Candidate],
    entry_status: Mapping[tuple[str, int], str],
    stats: Mapping[str, int],
) -> dict[str, Any]:
    final = ledger.cash_equity_usdt
    daily_returns = (
        np.asarray(daily_equity["daily_return"].to_list(), dtype=float)
        if not daily_equity.is_empty()
        else np.asarray([], dtype=float)
    )
    std = float(daily_returns.std()) if daily_returns.size else 0.0
    sharpe = float(daily_returns.mean() / std * math.sqrt(365.25)) if std > 0.0 else None
    max_drawdown = _finite(daily_equity["drawdown"].min(), 0.0) if not daily_equity.is_empty() else 0.0
    net_values = trades["net_pnl_usdt"].to_list() if not trades.is_empty() else []
    winners = sum(float(value) > 0.0 for value in net_values)
    funding_modes = _counts(str(value) for value in trades["funding_mode"].to_list()) if not trades.is_empty() else {}
    return {
        "signal_candidates": len(candidates),
        "entered_candidates": sum(value == "entered" for value in entry_status.values()),
        "trades": trades.height,
        "win_rate": winners / len(net_values) if net_values else None,
        "initial_equity_usdt": ledger.initial_equity_usdt,
        "final_equity_usdt": final,
        "total_return": final / ledger.initial_equity_usdt - 1.0,
        "gross_price_pnl_usdt": ledger.gross_realized_usdt,
        "fees_usdt": -ledger.fee_usdt,
        "slippage_usdt": -ledger.slippage_usdt,
        "funding_usdt": ledger.funding_usdt,
        "net_pnl_usdt": final - ledger.initial_equity_usdt,
        "daily_sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "resizes": int(stats.get("resizes", 0)),
        "mutations": mutations.height,
        "mutation_notional_usdt": (
            float((mutations["quantity_delta"].abs() * mutations["reference_price"]).sum())
            if not mutations.is_empty()
            else 0.0
        ),
        "funding_mode_counts": funding_modes,
        **dict(stats),
    }


def _era_rows(daily: pl.DataFrame, *, mode: str) -> list[dict[str, Any]]:
    if daily.is_empty():
        return []
    rows = daily.to_dicts()
    if mode == "year":
        labels = [str(row["date"])[:4] for row in rows]
    elif mode == "halves":
        midpoint = len(rows) // 2
        labels = ["first_half" if index < midpoint else "second_half" for index in range(len(rows))]
    else:
        raise ValueError(f"unknown era mode: {mode}")
    output: list[dict[str, Any]] = []
    for label in dict.fromkeys(labels):
        selected = [row for row, row_label in zip(rows, labels, strict=True) if row_label == label]
        start_equity = float(selected[0]["equity_usdt"]) / (1.0 + float(selected[0]["daily_return"]))
        end_equity = float(selected[-1]["equity_usdt"])
        returns = np.asarray([float(row["daily_return"]) for row in selected], dtype=float)
        std = float(returns.std())
        output.append(
            {
                "era": label,
                "days": len(selected),
                "start_equity_usdt": start_equity,
                "end_equity_usdt": end_equity,
                "return": end_equity / start_equity - 1.0 if start_equity > 0.0 else None,
                "daily_sharpe": (float(returns.mean() / std * math.sqrt(365.25)) if std > 0.0 else None),
            }
        )
    return output


def _write_result(
    output_dir: Path,
    result: LivePhysicsResult,
    metadata: Mapping[str, Any],
    *,
    source_snapshot_bytes: bytes,
) -> None:
    frames = {
        "long_live_physics_trades.csv": result.trades,
        "long_live_physics_mutations.csv": result.mutations,
        "long_live_physics_funding.csv": result.funding_events,
        "long_live_physics_daily_equity.csv": result.daily_equity,
        "long_live_physics_decisions.csv": result.decisions,
    }
    for name, frame in frames.items():
        # Report directories may be reused. Empty outputs must truncate a stale
        # artifact from an earlier run.
        frame.write_csv(output_dir / name)
    (output_dir / SOURCE_SNAPSHOT_NAME).write_bytes(source_snapshot_bytes)
    (output_dir / "long_live_physics_report.json").write_text(
        json.dumps(
            _json_safe(metadata),
            indent=2,
            sort_keys=True,
            default=str,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "long_live_physics_report.md").write_text(format_long_live_physics_report(metadata), encoding="utf-8")


def _taint_reasons(
    *,
    full_pit_universe_pass: bool,
    minute_receipt: MinuteTapeReceipt,
    mark_minute_receipt: MinuteTapeReceipt,
    funding_receipt: FundingTapeReceipt,
    trades: pl.DataFrame,
    funding_settlements_without_exact_mark_price_bar: int,
) -> list[str]:
    output: list[str] = []
    if not full_pit_universe_pass:
        output.append("pit_universe_coverage_failed")
    if minute_receipt.missing_symbol_days:
        output.append("candidate_minute_partitions_missing")
    if minute_receipt.missing_minutes:
        output.append("candidate_minute_rows_missing")
    if mark_minute_receipt.missing_symbol_days:
        output.append("candidate_mark_price_minute_partitions_missing")
    if mark_minute_receipt.missing_minutes:
        output.append("candidate_mark_price_minute_rows_missing")
    if funding_receipt.missing_intervals:
        output.append("funding_download_coverage_missing")
    if funding_settlements_without_exact_mark_price_bar:
        output.append("funding_settlement_mark_price_bar_missing")
    if not trades.is_empty():
        modes = set(str(value) for value in trades["funding_mode"].to_list())
        if modes - {"modeled"}:
            output.append("funding_not_fully_modeled")
        if "data_end" in set(str(value) for value in trades["exit_reason"].to_list()):
            output.append("open_trade_force_closed_at_data_end")
    return output


def _git_identity() -> dict[str, Any]:
    repo = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    for name in _GIT_LOCAL_ENV_VARS:
        env.pop(name, None)
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        return {"git_commit": commit, "git_dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return {"git_commit": "unavailable", "git_dirty": None}


def _empty_minute_bars() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_ms": pl.Series([], dtype=pl.Int64),
            "symbol": pl.Series([], dtype=pl.String),
            "open": pl.Series([], dtype=pl.Float64),
            "high": pl.Series([], dtype=pl.Float64),
            "low": pl.Series([], dtype=pl.Float64),
            "close": pl.Series([], dtype=pl.Float64),
        }
    )


def _empty_funding() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_ms": pl.Series([], dtype=pl.Int64),
            "symbol": pl.Series([], dtype=pl.String),
            "funding_rate": pl.Series([], dtype=pl.Float64),
        }
    )


def _frame_or_empty(rows: Sequence[Mapping[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


def _observe_mark(position: _Position, mark: float) -> None:
    if math.isfinite(mark) and mark > 0.0:
        position.minimum_mark = min(position.minimum_mark, mark)
        position.maximum_mark = max(position.maximum_mark, mark)


def _finite(value: Any, default: float) -> float:
    try:
        output = float(value)
    except (TypeError, ValueError):
        return default
    return output if math.isfinite(output) else default


def _utc_date(ts_ms: int) -> dt.date:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).date()


def _iso_ts(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).isoformat()


def _counts(values: Iterable[str]) -> dict[str, int]:
    output: dict[str, int] = {}
    for value in values:
        output[value] = output.get(value, 0) + 1
    return output


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return [_json_safe(item) for item in sorted(value, key=str)]
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Path):
        return str(value)
    return value


def _pct(value: Any) -> str:
    number = _finite(value, float("nan"))
    return "n/a" if not math.isfinite(number) else f"{number:.2%}"


def _money(value: Any) -> str:
    number = _finite(value, float("nan"))
    return "n/a" if not math.isfinite(number) else f"{number:+,.2f} USDT"


def _render_era(row: Mapping[str, Any]) -> str:
    return (
        f"| {row.get('era', '')} | {float(row.get('start_equity_usdt') or 0.0):,.2f} | "
        f"{float(row.get('end_equity_usdt') or 0.0):,.2f} | {_pct(row.get('return'))} | "
        f"{int(row.get('days') or 0)} |"
    )
