from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.account_kernel import (
    AccountRiskPolicy,
    InstrumentRules,
)
from liquidity_migration.account_route import ensure_account_route
from liquidity_migration.active_runtime_comparator import (
    ActiveRuntimeComparator,
    ComparatorRunConfig,
    _long_price_required_symbols,
)
from liquidity_migration.continuous_demo import (
    ContinuousDemoCycleConfig,
    apply_continuous_demo_profile,
)
from liquidity_migration.execution_adapters import (
    ExecutionTwinConfig,
    LatencyProfile,
)
from liquidity_migration.historical_account_replay import HistoricalAccountSession
from liquidity_migration.long_native import long_v11a_profile
from liquidity_migration.long_native_event_demo import LongNativeDemoCycleConfig
from liquidity_migration.venue_lifecycle import (
    DELISTING_PROXY_EXACTNESS,
    DELISTING_PROXY_METHOD,
    VenueDelistingSettlement,
)


class _FixedPrices:
    def price(self, symbol: str, boundary_ts_ms: int) -> float:
        if symbol == "CANTOUSDT":
            raise RuntimeError("optional frozen price is absent")
        assert symbol in {"AUSDT", "BUSDT", "DUSDT"}
        assert boundary_ts_ms > 0
        return 10.0

    def prices(
        self,
        symbols: Sequence[str] | set[str],
        boundary_ts_ms: int,
    ) -> dict[str, float]:
        return {
            str(symbol).upper(): self.price(str(symbol).upper(), boundary_ts_ms)
            for symbol in symbols
        }


class _Trace:
    def __init__(self) -> None:
        self.cycles: list[dict[str, Any]] = []
        self.gates: list[pl.DataFrame] = []
        self.long_rows: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.intents: list[dict[str, Any]] = []
        self.lifecycle: list[dict[str, Any]] = []

    def cycle(self, row: Mapping[str, Any]) -> None:
        self.cycles.append(dict(row))

    def continuous_gates(self, frame: pl.DataFrame) -> None:
        self.gates.append(frame.clone())

    def long_funnel(self, row: Mapping[str, Any]) -> None:
        self.long_rows.append(dict(row))

    def source_decision(self, row: Mapping[str, Any]) -> None:
        self.decisions.append(dict(row))

    def request(self, row: Mapping[str, Any]) -> None:
        self.requests.append(dict(row))

    def request_intent(self, row: Mapping[str, Any]) -> None:
        self.intents.append(dict(row))

    def venue_lifecycle(self, row: Mapping[str, Any]) -> None:
        self.lifecycle.append(dict(row))


def _btc_rows(signal_day: int) -> pl.DataFrame:
    rows = []
    for ordinal in range(46):
        rows.append(
            {
                "symbol": "BTCUSDT",
                "ts_ms": signal_day - (45 - ordinal) * MS_PER_DAY,
                "close": 100.0 + ordinal,
            }
        )
    return pl.from_dicts(rows)


@pytest.mark.parametrize(
    ("long_symbol", "expect_account_rejection"),
    (("AUSDT", False), ("BUSDT", True)),
)
def test_shared_comparator_preserves_requests_btc_chain_and_boundary_flat(
    tmp_path: Path,
    monkeypatch,
    long_symbol: str,
    expect_account_rejection: bool,
) -> None:
    import liquidity_migration.account_route as route_module
    import liquidity_migration.continuous_btc_risk as btc_module
    from liquidity_migration.artifact_snapshot import StableFileSnapshot

    monkeypatch.setattr(btc_module, "_fsync_directory", lambda _path: None)
    monkeypatch.setattr(btc_module, "_fsync_file", lambda _path: None)
    def portable_rename(source, destination, *, label: str) -> None:
        del label
        destination_path = Path(destination)
        if destination_path.exists():
            raise FileExistsError(destination_path)
        Path(source).replace(destination_path)

    monkeypatch.setattr(route_module, "rename_noreplace", portable_rename)
    monkeypatch.setattr(route_module, "_fsync_directory", lambda _path: None)
    def portable_read(path, **_kwargs) -> StableFileSnapshot:
        resolved = Path(path).resolve(strict=True)
        return StableFileSnapshot(
            path=resolved,
            data=resolved.read_bytes(),
            metadata=resolved.stat(),
        )

    monkeypatch.setattr(route_module, "read_stable_file", portable_read)
    def portable_create(path: Path, data: bytes) -> None:
        if path.exists():
            raise FileExistsError(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    monkeypatch.setattr(route_module, "_atomic_create", portable_create)
    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    route = ensure_account_route(
        account_id="active-comparator-test",
        environment="demo",
        account_root=account_root,
        inbox_root=inbox_root,
    )
    rules = {
        symbol: InstrumentRules(
            symbol=symbol,
            qty_step=1e-12,
            min_qty=1e-12,
            min_notional=0.0,
            tick_size=1e-12,
            max_order_qty=1e15,
            max_leverage=10.0,
            source="test-historical",
            environment="historical_synthetic",
            observed_ts_ns=1,
        )
        for symbol in ("AUSDT", "BUSDT")
    }
    execution = ExecutionTwinConfig(
        fee_bps=0.0,
        latency=LatencyProfile(0, 0, 0),
        max_decision_age_ns=1_000_000,
    )
    session = HistoricalAccountSession(
        account_root,
        account_id=route.account_id,
        risk_policy=AccountRiskPolicy(
            10_000_000.0,
            100_000_000.0,
            10_000_000.0,
            100_000_000.0,
            10.0,
        ),
        instrument_rules=rules,
        execution_config=execution,
        id_seed="active-comparator-test",
        unsafe_single_process_inplace_research=True,
    )
    long_demo = LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_execution_root=str(account_root),
        account_intent_inbox_root=str(inbox_root),
    )
    continuous_demo = apply_continuous_demo_profile(
        ContinuousDemoCycleConfig(
            execution_environment="demo",
            account_execution_root=str(account_root),
            account_intent_inbox_root=str(inbox_root),
            btc_trend_gate="uptrend",
            max_active=25,
            max_new_entries_per_cycle=5,
            entry_leverage=10.0,
            notional_multiplier=10.0,
            per_position_notional_pct_equity=2.0,
        )
    )
    boundary = 1_709_251_200_000  # 2024-03-01T00:00:00Z
    signal_ts = boundary - 2 * MS_PER_HOUR
    signal_day = (signal_ts // MS_PER_DAY) * MS_PER_DAY
    trace = _Trace()
    comparator = ActiveRuntimeComparator(
        route=route,
        session=session,
        instrument_rules=rules,
        execution_config=execution,
        price_port=_FixedPrices(),
        long_demo=long_demo,
        long_strategy=long_v11a_profile(),
        continuous_demo=continuous_demo,
        btc_klines=_btc_rows(signal_day),
        first_archive_day_by_symbol={"BUSDT": signal_day - 300 * MS_PER_DAY},
        btc_state_root=tmp_path / "btc-risk",
        run_config=ComparatorRunConfig(
            equity_usdt=1_000_000.0,
            long_source_start_ms=boundary - MS_PER_DAY,
            continuous_source_start_ms=boundary - MS_PER_DAY,
            source_end_ms=boundary,
        ),
        trace_sink=trace,
    )
    entry_state = pl.DataFrame(
        {
            "symbol": ["BUSDT"],
            "ts_ms": [signal_ts],
            "decile": [9.0],
            "composite": [1.0],
            "turnover_quote": [2_000_000.0],
            "rv_168h": [0.01],
            "ret1": [0.06],
            "max_ret168": [0.06],
            "prior6_ret1_max": [0.06],
            "giveback_from_prior6_high": [0.0],
            "turnover_spike_168h": [5.0],
        }
    )
    long_features = pl.DataFrame(
        {
            "symbol": [long_symbol],
            "ts_ms": [boundary - MS_PER_HOUR],
            "close": [10.2],
            "log_return": [0.20],
            "pump_3d_log": [0.20],
            "pump_7d_log": [0.20],
            "sigma_daily_30d": [0.05],
            "in_universe": [True],
            "regime_on": [True],
            "eth_regime_on": [True],
            "today_volume_rank": [1],
            "close_location": [0.80],
            "close_loc_3d": [0.80],
            "close_loc_7d": [0.80],
            "atr_14d_pct": [0.05],
            "realized_vol": [0.50],
            "btc_rv_30": [0.60],
            "symbol_age_days": [300],
            "turnover_median_90d": [2_000_000.0],
        }
    )
    ineligible_pump = {
        **long_features.to_dicts()[0],
        "symbol": "CANTOUSDT",
        "in_universe": False,
        "regime_on": False,
        "eth_regime_on": False,
        "today_volume_rank": 234,
        "symbol_age_days": 82,
        "turnover_median_90d": None,
    }
    optional_priced_pump = {
        **ineligible_pump,
        "symbol": "DUSDT",
    }
    mixed_long_features = pl.from_dicts(
        [
            *long_features.to_dicts(),
            ineligible_pump,
            optional_priced_pump,
        ],
        infer_schema_length=None,
    )
    assert _long_price_required_symbols(
        features=mixed_long_features,
        all_trades=pl.DataFrame(),
        now_ms=boundary,
        strategy=long_v11a_profile(),
    ) == {long_symbol}
    assert comparator._long_candidate_prices(
        mixed_long_features,
        boundary_ts_ms=boundary,
    ) == {long_symbol: 10.0, "DUSDT": 10.0}

    cycle = comparator.process_hour(
        boundary,
        long_recent_features=long_features,
        continuous_entry_state=entry_state,
    )
    assert cycle["long_entry_candidates"] == 1
    assert cycle["long_entry_requests"] == 1
    assert cycle["continuous_entry_candidates"] == 3
    assert cycle["continuous_entry_requests"] == 3
    assert len({row["content_hash"] for row in trace.requests}) == 4
    continuous_intents = [
        row for row in trace.intents if row["adapter_kind"] == "continuous"
    ]
    assert all(row["btc_risk_evidence_hash"] for row in continuous_intents)
    assert trace.gates[0]["targeted_p3"].item()
    assert trace.gates[0]["targeted_p4p3"].item()
    assert trace.gates[0]["targeted_p4p5"].item()

    continuous_requests = [
        row for row in trace.requests if row["stage"] == "continuous_entry"
    ]
    accepted_continuous = sum(row["accepted"] is True for row in continuous_requests)
    assert any(row["accepted"] is False for row in continuous_requests) is (
        expect_account_rejection
    )
    boundary_target_count = 1 + accepted_continuous
    assert comparator.boundary_flatten(boundary) == boundary_target_count
    summary = comparator.final_structural_summary()
    assert summary["final_flat"] is True
    assert summary["long_lifecycle_rows"] == 1
    assert summary["continuous_lifecycle_rows"] == accepted_continuous
    assert summary["btc_risk_authoritative_rows"] == int(
        accepted_continuous > 0
    )
    assert summary["monetary_outcomes_inspected"] is False
    assert sum(row["boundary_only"] is True for row in trace.decisions) == (
        boundary_target_count
    )


def test_venue_lifecycle_settles_open_position_and_blocks_future_entries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import liquidity_migration.account_route as route_module
    import liquidity_migration.continuous_btc_risk as btc_module
    from liquidity_migration.artifact_snapshot import StableFileSnapshot

    monkeypatch.setattr(btc_module, "_fsync_directory", lambda _path: None)
    monkeypatch.setattr(btc_module, "_fsync_file", lambda _path: None)

    def portable_rename(source, destination, *, label: str) -> None:
        del label
        destination_path = Path(destination)
        if destination_path.exists():
            raise FileExistsError(destination_path)
        Path(source).replace(destination_path)

    def portable_read(path, **_kwargs) -> StableFileSnapshot:
        resolved = Path(path).resolve(strict=True)
        return StableFileSnapshot(
            path=resolved,
            data=resolved.read_bytes(),
            metadata=resolved.stat(),
        )

    def portable_create(path: Path, data: bytes) -> None:
        if path.exists():
            raise FileExistsError(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    monkeypatch.setattr(route_module, "rename_noreplace", portable_rename)
    monkeypatch.setattr(route_module, "_fsync_directory", lambda _path: None)
    monkeypatch.setattr(route_module, "read_stable_file", portable_read)
    monkeypatch.setattr(route_module, "_atomic_create", portable_create)

    account_root = tmp_path / "account"
    inbox_root = tmp_path / "inbox"
    route = ensure_account_route(
        account_id="active-comparator-lifecycle-test",
        environment="demo",
        account_root=account_root,
        inbox_root=inbox_root,
    )
    rules = {
        symbol: InstrumentRules(
            symbol=symbol,
            qty_step=1e-12,
            min_qty=1e-12,
            min_notional=0.0,
            tick_size=1e-12,
            max_order_qty=1e15,
            max_leverage=10.0,
            source="test-historical",
            environment="historical_synthetic",
            observed_ts_ns=1,
        )
        for symbol in ("AUSDT", "BUSDT")
    }
    execution = ExecutionTwinConfig(
        fee_bps=0.0,
        latency=LatencyProfile(0, 0, 0),
        max_decision_age_ns=1_000_000,
    )
    session = HistoricalAccountSession(
        account_root,
        account_id=route.account_id,
        risk_policy=AccountRiskPolicy(
            10_000_000.0,
            100_000_000.0,
            10_000_000.0,
            100_000_000.0,
            10.0,
        ),
        instrument_rules=rules,
        execution_config=execution,
        id_seed="active-comparator-lifecycle-test",
        unsafe_single_process_inplace_research=True,
    )
    long_demo = LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_execution_root=str(account_root),
        account_intent_inbox_root=str(inbox_root),
    )
    continuous_demo = apply_continuous_demo_profile(
        ContinuousDemoCycleConfig(
            execution_environment="demo",
            account_execution_root=str(account_root),
            account_intent_inbox_root=str(inbox_root),
            btc_trend_gate="uptrend",
            max_active=25,
            max_new_entries_per_cycle=5,
            entry_leverage=10.0,
            notional_multiplier=10.0,
            per_position_notional_pct_equity=2.0,
        )
    )
    first_boundary = 1_709_251_200_000
    lifecycle_boundary = first_boundary + MS_PER_HOUR
    signal_ts = lifecycle_boundary - 2 * MS_PER_HOUR
    signal_day = (signal_ts // MS_PER_DAY) * MS_PER_DAY

    def lifecycle_event(symbol: str) -> VenueDelistingSettlement:
        return VenueDelistingSettlement(
            symbol=symbol,
            effective_ts_ms=lifecycle_boundary,
            dispatch_ts_ms=lifecycle_boundary,
            proxy_price=10.0,
            proxy_price_decimal="10.0",
            announcement_published_ts_ms=lifecycle_boundary - MS_PER_DAY,
            announcement_url=(
                f"https://announcements.bybit.com/en/article/{symbol.lower()}"
            ),
            announcement_uid=f"{symbol.lower()}-uid",
            announcement_sha256="a" * 64,
            index_api_sha256="b" * 64,
            index_api_canonical_sha256="c" * 64,
            proxy_method=DELISTING_PROXY_METHOD,
            proxy_exactness=DELISTING_PROXY_EXACTNESS,
            settlement_fee_usdt=0.0,
            source_scope="official_bybit_announcement_and_index_price_api",
        )

    trace = _Trace()
    comparator = ActiveRuntimeComparator(
        route=route,
        session=session,
        instrument_rules=rules,
        execution_config=execution,
        price_port=_FixedPrices(),
        long_demo=long_demo,
        long_strategy=long_v11a_profile(),
        continuous_demo=continuous_demo,
        btc_klines=_btc_rows(signal_day),
        first_archive_day_by_symbol={
            "BUSDT": signal_day - 300 * MS_PER_DAY
        },
        btc_state_root=tmp_path / "btc-risk",
        run_config=ComparatorRunConfig(
            equity_usdt=1_000_000.0,
            long_source_start_ms=first_boundary - MS_PER_DAY,
            continuous_source_start_ms=first_boundary - MS_PER_DAY,
            source_end_ms=lifecycle_boundary,
        ),
        venue_lifecycle_events=(
            lifecycle_event("AUSDT"),
            lifecycle_event("BUSDT"),
        ),
        trace_sink=trace,
    )

    def long_features(symbol: str, ts_ms: int) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "symbol": [symbol],
                "ts_ms": [ts_ms],
                "close": [10.2],
                "log_return": [0.20],
                "pump_3d_log": [0.20],
                "pump_7d_log": [0.20],
                "sigma_daily_30d": [0.05],
                "in_universe": [True],
                "regime_on": [True],
                "eth_regime_on": [True],
                "today_volume_rank": [1],
                "close_location": [0.80],
                "close_loc_3d": [0.80],
                "close_loc_7d": [0.80],
                "atr_14d_pct": [0.05],
                "realized_vol": [0.50],
                "btc_rv_30": [0.60],
                "symbol_age_days": [300],
                "turnover_median_90d": [2_000_000.0],
            }
        )

    first_cycle = comparator.process_hour(
        first_boundary,
        long_recent_features=long_features(
            "AUSDT",
            first_boundary - MS_PER_HOUR,
        ),
    )
    assert first_cycle["long_entry_requests"] == 1

    continuous_state = pl.DataFrame(
        {
            "symbol": ["BUSDT"],
            "ts_ms": [signal_ts],
            "decile": [9.0],
            "composite": [1.0],
            "turnover_quote": [2_000_000.0],
            "rv_168h": [0.01],
            "ret1": [0.06],
            "max_ret168": [0.06],
            "prior6_ret1_max": [0.06],
            "giveback_from_prior6_high": [0.0],
            "turnover_spike_168h": [5.0],
        }
    )
    lifecycle_cycle = comparator.process_hour(
        lifecycle_boundary,
        long_recent_features=long_features(
            "BUSDT",
            lifecycle_boundary - MS_PER_HOUR,
        ),
        continuous_entry_state=continuous_state,
    )

    assert lifecycle_cycle["venue_lifecycle_events"] == 2
    assert lifecycle_cycle["venue_lifecycle_settlement_fills"] == 1
    assert lifecycle_cycle["long_venue_blocked_entries"] == 1
    assert lifecycle_cycle["continuous_venue_blocked_entries"] == 3
    assert lifecycle_cycle["long_entry_requests"] == 0
    assert lifecycle_cycle["continuous_entry_requests"] == 0
    assert trace.gates[0]["targeted_p3"].item()
    assert trace.gates[0]["targeted_p4p3"].item()
    assert trace.gates[0]["targeted_p4p5"].item()
    assert comparator.boundary_flatten(lifecycle_boundary) == 0

    summary = comparator.final_structural_summary()
    assert summary["venue_lifecycle_registered_events"] == 2
    assert summary["venue_lifecycle_observed_events"] == 2
    assert summary["venue_lifecycle_settlement_fills"] == 1
    assert summary["venue_lifecycle_blocked_entries"] == 4
    assert summary["venue_lifecycle_inactive_symbols"] == 2
    assert summary["btc_risk_authoritative_rows"] == 0
    assert summary["final_flat"] is True
    assert [row["action"] for row in trace.lifecycle].count(
        "settlement_fill"
    ) == 1
    assert [row["action"] for row in trace.lifecycle].count(
        "flat_observed"
    ) == 1
    assert [row["action"] for row in trace.lifecycle].count(
        "entry_blocked"
    ) == 4
    blocked = [
        row for row in trace.lifecycle if row["action"] == "entry_blocked"
    ]
    assert all(row["trace_type"] == "venue_lifecycle_block" for row in blocked)
    assert all(
        row["reason"] == "venue_delisted_before_target_publication"
        for row in blocked
    )
