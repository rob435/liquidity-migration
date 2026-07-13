from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

import liquidity_migration.continuous_btc_risk as btc_risk_module
from liquidity_migration.account_intent_client import AccountTargetPublisher
from liquidity_migration.account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
)
from liquidity_migration.account_service import AccountIntentInbox
from liquidity_migration.account_route import AccountRoute, ensure_account_route
from liquidity_migration.account_strategy_state import canonical_strategy_trade_rows
from liquidity_migration.continuous_btc_risk import (
    BTC_RISK_COMPONENTS,
    BTC_RISK_EVIDENCE_METADATA_KEY,
    BtcRiskLiveSizer,
)
from liquidity_migration.continuous_demo import (
    ContinuousDemoCycleConfig,
    _apply_btc_risk_sizing,
    _continuous_entry_target_intents,
    _unresolved_continuous_entry_request_count,
)


DAY_MS = 86_400_000


def _route(tmp_path: Path) -> AccountRoute:
    return ensure_account_route(
        account_id="bybit-demo-test",
        environment="demo",
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )


def _demo() -> ContinuousDemoCycleConfig:
    return ContinuousDemoCycleConfig(
        entry_btc_risk_sizing_enabled=True,
        entry_btc_risk_min_prior=0,
        sizing_mode="flat",
    )


def _btc_klines(*, descending: bool = False) -> pl.DataFrame:
    closes = [100.0 + index for index in range(40)]
    if descending:
        closes.reverse()
    return pl.DataFrame(
        {
            "symbol": ["BTCUSDT"] * 40,
            "ts_ms": [index * DAY_MS + 23 * 60 * 60 * 1000 for index in range(40)],
            "close": closes,
        }
    )


def _candidate(symbol: str, signal_ts_ms: int, *, trade_id: str | None = None) -> dict[str, object]:
    return {
        "trade_id": trade_id or f"trade-{symbol}-{signal_ts_ms}",
        "symbol": symbol,
        "signal_ts_ms": signal_ts_ms,
        "live_price": 100.0,
        "take_profit_pct": 0.12,
    }


def _score_candidate(
    root: Path,
    *,
    symbol: str = "AAAUSDT",
    signal_ts_ms: int = 11 * DAY_MS,
    descending: bool = False,
) -> tuple[ContinuousDemoCycleConfig, dict[str, object], dict[str, object]]:
    demo = _demo()
    candidate = _candidate(symbol, signal_ts_ms)
    stats = _apply_btc_risk_sizing(
        [candidate],
        config=demo,
        root=root,
        btc_klines=_btc_klines(descending=descending),
    )
    assert not stats["entry_blocked"]
    assert stats["scored"] == 1
    return demo, candidate, stats


def _propose_evidence(
    sizer: BtcRiskLiveSizer,
    *,
    symbol: str,
    signal_ts_ms: int,
    raw_values: dict[str, float | None] | None = None,
) -> dict[str, object]:
    day = (signal_ts_ms // DAY_MS) * DAY_MS
    context = {day: {name: (raw_values or {}).get(name) for name in BTC_RISK_COMPONENTS}}
    lookup, stats = sizer.score_decisions(
        [{"symbol": symbol, "signal_ts_ms": signal_ts_ms}],
        btc_context=context,
    )
    assert stats["scored"] == 1
    return lookup[(symbol, signal_ts_ms)]["decision_evidence"]


def _accepted_evidence_row(evidence: dict[str, object]) -> dict[str, object]:
    return {
        "symbol": evidence["symbol"],
        "signal_ts_ms": evidence["signal_ts_ms"],
        BTC_RISK_EVIDENCE_METADATA_KEY: evidence,
    }


def _accepted_evidence_chain(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    source = BtcRiskLiveSizer(tmp_path / "authoritative-source.parquet", min_prior=0)
    first = _propose_evidence(
        source,
        symbol="AAAUSDT",
        signal_ts_ms=10 * DAY_MS,
    )
    source.ingest_accepted_decisions([_accepted_evidence_row(first)])
    second = _propose_evidence(
        source,
        symbol="BBBUSDT",
        signal_ts_ms=11 * DAY_MS,
    )
    source.ingest_accepted_decisions([_accepted_evidence_row(second)])
    return first, second


def _entry_intent(
    demo: ContinuousDemoCycleConfig,
    candidate: dict[str, object],
    *,
    now_ms: int = 2_000_000_000_000,
):
    symbol = str(candidate["symbol"])
    intents = _continuous_entry_target_intents(
        [candidate],
        demo=demo,
        equity_usdt=10_000.0,
        order_notional_frac=0.02,
        price_by_symbol={symbol: 100.0},
        now_ms=now_ms,
        strategy_id="continuous-test",
    )
    assert len(intents) == 1
    return intents[0]


def _submit_to_kernel(
    account_root: Path,
    intent,
    *,
    accept: bool,
    batch_id: str,
):
    symbol = intent.intent.symbol
    kernel = AccountExecutionKernel(account_root, account_id="bybit-demo-test")
    limit = 1_000.0 if accept else 1.0
    result = kernel.submit_targets(
        batch_id=batch_id,
        market_inputs=[MarketInputRef(f"book:{batch_id}", symbol, 1, 2, 100.0)],
        targets=[
            DesiredTarget(
                decision_key=intent.intent.decision_key,
                target_key=intent.intent.target_key,
                sleeve="continuous",
                strategy_id=intent.intent.strategy_id,
                component_id=intent.intent.component_id,
                symbol=symbol,
                signed_qty=-1.0,
                reference_price=100.0,
                leverage=intent.intent.leverage,
                reason=intent.intent.reason,
                metadata=dict(intent.intent.metadata),
            )
        ],
        risk_snapshot=AccountRiskSnapshot(10_000.0, 9_000.0, "wallet", 3),
        risk_policy=AccountRiskPolicy(limit, limit, limit, limit, 10.0),
        instrument_rules={symbol: InstrumentRules(symbol, 0.1, 0.1, 1.0)},
    )
    assert result.accepted is accept
    return result


def test_publication_does_not_advance_but_accepted_target_advances_once(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "strategy"
    demo, candidate, _ = _score_candidate(state_root)
    state_path = state_root / "btc_risk_sizing_state.parquet"
    assert not state_path.exists()

    intent = _entry_intent(demo, candidate)
    evidence = intent.intent.metadata[BTC_RISK_EVIDENCE_METADATA_KEY]
    assert evidence["decision_key"] == f"AAAUSDT|{11 * DAY_MS}"
    assert evidence["evidence_hash"]

    route = _route(tmp_path)
    AccountTargetPublisher(route).publish(
        batch_id="published-only",
        intents=[intent],
        created_ts_ns=1,
    )
    assert not state_path.exists()

    account_root = route.account_path
    _submit_to_kernel(account_root, intent, accept=True, batch_id="accepted")
    accepted_rows = canonical_strategy_trade_rows(
        account_root,
        sleeve="continuous",
        strategy_ids=("continuous-test",),
    )
    assert accepted_rows[BTC_RISK_EVIDENCE_METADATA_KEY].to_list() == [evidence]

    first = _apply_btc_risk_sizing(
        [],
        config=demo,
        root=state_root,
        btc_klines=pl.DataFrame(),
        accepted_target_rows=accepted_rows,
    )
    assert first["accepted_ingested"] == 1
    assert pl.read_parquet(state_path)["decision_key"].to_list() == [f"AAAUSDT|{11 * DAY_MS}"]

    restarted = _apply_btc_risk_sizing(
        [],
        config=demo,
        root=state_root,
        btc_klines=pl.DataFrame(),
        accepted_target_rows=accepted_rows,
    )
    assert restarted["accepted_ingested"] == 0
    assert restarted["accepted_duplicates"] == 1
    assert pl.read_parquet(state_path).height == 1


def test_runtime_rejects_persisted_btc_state_absent_from_complete_account_projection(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "strategy"
    demo, first_candidate, _ = _score_candidate(state_root)
    first_intent = _entry_intent(demo, first_candidate)
    route = _route(tmp_path)
    _submit_to_kernel(
        route.account_path,
        first_intent,
        accept=True,
        batch_id="accepted-first",
    )
    accepted_rows = canonical_strategy_trade_rows(
        route.account_path,
        sleeve="continuous",
        strategy_ids=("continuous-test",),
    )
    synchronized = _apply_btc_risk_sizing(
        [],
        config=demo,
        root=state_root,
        btc_klines=pl.DataFrame(),
        accepted_target_rows=accepted_rows,
    )
    state_path = state_root / "btc_risk_sizing_state.parquet"
    before = state_path.read_bytes()
    next_candidate = _candidate("BBBUSDT", 12 * DAY_MS)

    stale = _apply_btc_risk_sizing(
        [next_candidate],
        config=demo,
        root=state_root,
        btc_klines=_btc_klines(),
        accepted_target_rows=pl.DataFrame(),
    )

    assert synchronized["accepted_authoritative_rows"] == 1
    assert stale["entry_blocked"]
    assert "absent from complete authoritative" in stale["blocking_reason"]
    assert state_path.read_bytes() == before
    assert BTC_RISK_EVIDENCE_METADATA_KEY not in next_candidate

    recovered = _apply_btc_risk_sizing(
        [next_candidate],
        config=demo,
        root=state_root,
        btc_klines=_btc_klines(),
        accepted_target_rows=accepted_rows,
    )

    assert not recovered["entry_blocked"]
    assert recovered["accepted_authoritative_rows"] == 1
    assert recovered["scored"] == 1
    assert BTC_RISK_EVIDENCE_METADATA_KEY in next_candidate


def test_risk_rejected_target_does_not_advance_state(tmp_path: Path) -> None:
    state_root = tmp_path / "strategy"
    demo, candidate, _ = _score_candidate(state_root)
    intent = _entry_intent(demo, candidate)
    account_root = tmp_path / "account"

    _submit_to_kernel(account_root, intent, accept=False, batch_id="rejected")
    accepted_rows = canonical_strategy_trade_rows(
        account_root,
        sleeve="continuous",
        strategy_ids=("continuous-test",),
    )
    assert accepted_rows.is_empty()

    stats = _apply_btc_risk_sizing(
        [],
        config=demo,
        root=state_root,
        btc_klines=pl.DataFrame(),
        accepted_target_rows=accepted_rows,
    )
    assert stats["accepted_ingested"] == 0
    assert not (state_root / "btc_risk_sizing_state.parquet").exists()


def test_btc_sized_entries_fail_closed_without_account_acceptance_authority(
    tmp_path: Path,
) -> None:
    candidate = _candidate("AAAUSDT", 11 * DAY_MS)
    stats = _apply_btc_risk_sizing(
        [candidate],
        config=_demo(),
        root=tmp_path,
        btc_klines=_btc_klines(),
        accepted_state_authority=False,
    )

    assert stats["entry_blocked"]
    assert stats["blocking_reason"] == "account_accepted_state_unavailable"
    assert BTC_RISK_EVIDENCE_METADATA_KEY not in candidate
    assert not (tmp_path / "btc_risk_sizing_state.parquet").exists()


@pytest.mark.parametrize("claimed", [False, True])
def test_pending_or_processing_entry_blocks_next_publication(
    tmp_path: Path,
    claimed: bool,
) -> None:
    demo, first_candidate, _ = _score_candidate(tmp_path / "first")
    intent = _entry_intent(demo, first_candidate)
    route = _route(tmp_path)
    AccountTargetPublisher(route).publish(
        batch_id="unresolved",
        intents=[intent],
        created_ts_ns=1,
    )
    if claimed:
        assert AccountIntentInbox(route).claim_next() is not None

    unresolved = _unresolved_continuous_entry_request_count(route)
    assert unresolved == 1
    next_candidate = _candidate("BBBUSDT", 12 * DAY_MS)
    state_root = tmp_path / "blocked"
    stats = _apply_btc_risk_sizing(
        [next_candidate],
        config=demo,
        root=state_root,
        btc_klines=_btc_klines(),
        unresolved_entry_requests=unresolved,
    )

    assert stats["entry_blocked"]
    assert stats["blocking_reason"] == "unresolved_account_entry_request"
    assert BTC_RISK_EVIDENCE_METADATA_KEY not in next_candidate
    assert not (state_root / "btc_risk_sizing_state.parquet").exists()


def test_target_metadata_requires_valid_matching_evidence(tmp_path: Path) -> None:
    demo = _demo()
    candidate = _candidate("AAAUSDT", 11 * DAY_MS)
    with pytest.raises(ValueError, match="decision evidence"):
        _entry_intent(demo, candidate)

    _, candidate, _ = _score_candidate(tmp_path / "scored")
    candidate[BTC_RISK_EVIDENCE_METADATA_KEY]["evidence_hash"] = "0" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        _entry_intent(demo, candidate)


def test_conflicting_duplicate_and_forked_accepted_evidence_fail_closed(
    tmp_path: Path,
) -> None:
    demo, duplicate_a, _ = _score_candidate(tmp_path / "dup-a")
    _, duplicate_b, _ = _score_candidate(tmp_path / "dup-b", descending=True)
    duplicate_rows = pl.DataFrame(
        [
            {
                "symbol": "AAAUSDT",
                "signal_ts_ms": 11 * DAY_MS,
                BTC_RISK_EVIDENCE_METADATA_KEY: duplicate_a[BTC_RISK_EVIDENCE_METADATA_KEY],
            },
            {
                "symbol": "AAAUSDT",
                "signal_ts_ms": 11 * DAY_MS,
                BTC_RISK_EVIDENCE_METADATA_KEY: duplicate_b[BTC_RISK_EVIDENCE_METADATA_KEY],
            },
        ]
    )
    duplicate_stats = _apply_btc_risk_sizing(
        [],
        config=demo,
        root=tmp_path / "dup-sink",
        btc_klines=pl.DataFrame(),
        accepted_target_rows=duplicate_rows,
    )
    assert duplicate_stats["entry_blocked"]
    assert "conflicting duplicate evidence" in duplicate_stats["blocking_reason"]
    assert not (tmp_path / "dup-sink" / "btc_risk_sizing_state.parquet").exists()

    _, fork_a, _ = _score_candidate(
        tmp_path / "fork-a",
        symbol="AAAUSDT",
        signal_ts_ms=11 * DAY_MS,
    )
    _, fork_b, _ = _score_candidate(
        tmp_path / "fork-b",
        symbol="BBBUSDT",
        signal_ts_ms=12 * DAY_MS,
    )
    fork_rows = pl.DataFrame(
        [
            {
                "symbol": fork["symbol"],
                "signal_ts_ms": fork["signal_ts_ms"],
                BTC_RISK_EVIDENCE_METADATA_KEY: fork[BTC_RISK_EVIDENCE_METADATA_KEY],
            }
            for fork in (fork_a, fork_b)
        ]
    )
    fork_stats = _apply_btc_risk_sizing(
        [],
        config=demo,
        root=tmp_path / "fork-sink",
        btc_klines=pl.DataFrame(),
        accepted_target_rows=fork_rows,
    )
    assert fork_stats["entry_blocked"]
    assert "forked predecessor" in fork_stats["blocking_reason"]
    assert not (tmp_path / "fork-sink" / "btc_risk_sizing_state.parquet").exists()


def test_receipt_chain_not_signal_time_controls_restart_order(tmp_path: Path) -> None:
    source_path = tmp_path / "source.parquet"
    source = BtcRiskLiveSizer(source_path, min_prior=0)
    future_lookup, _ = source.score_decisions(
        [{"symbol": "AAAUSDT", "signal_ts_ms": 20 * DAY_MS}],
        btc_context={20 * DAY_MS: {name: None for name in BTC_RISK_COMPONENTS}},
    )
    future_evidence = future_lookup[("AAAUSDT", 20 * DAY_MS)]["decision_evidence"]
    source.ingest_accepted_decisions([{BTC_RISK_EVIDENCE_METADATA_KEY: future_evidence}])
    past_lookup, _ = source.score_decisions(
        [{"symbol": "BBBUSDT", "signal_ts_ms": 10 * DAY_MS}],
        btc_context={10 * DAY_MS: {name: None for name in BTC_RISK_COMPONENTS}},
    )
    past_evidence = past_lookup[("BBBUSDT", 10 * DAY_MS)]["decision_evidence"]

    sink_path = tmp_path / "sink.parquet"
    sink = BtcRiskLiveSizer(sink_path, min_prior=0)
    # Deliberately reverse the rows: topology, not caller/signal order, must
    # recover the sole accepted chain.
    result = sink.ingest_accepted_decisions(
        [
            {BTC_RISK_EVIDENCE_METADATA_KEY: past_evidence},
            {BTC_RISK_EVIDENCE_METADATA_KEY: future_evidence},
        ]
    )
    assert result["ingested"] == 2
    assert pl.read_parquet(sink_path)["decision_key"].to_list() == [
        f"AAAUSDT|{20 * DAY_MS}",
        f"BBBUSDT|{10 * DAY_MS}",
    ]
    assert BtcRiskLiveSizer(sink_path, min_prior=0).rows == 2


def test_pre_evidence_state_file_replays_and_upgrades_compatibly(tmp_path: Path) -> None:
    state_path = tmp_path / "legacy.parquet"
    legacy_schema = {
        "decision_key": pl.String,
        "symbol": pl.String,
        "signal_ts_ms": pl.Int64,
        **{name: pl.Float64 for name in BTC_RISK_COMPONENTS},
        "btc_risk_score": pl.Float64,
        "stack_mult": pl.Float64,
        "score_warmup": pl.Boolean,
    }
    pl.DataFrame(
        [
            {
                "decision_key": f"AAAUSDT|{10 * DAY_MS}",
                "symbol": "AAAUSDT",
                "signal_ts_ms": 10 * DAY_MS,
                **{name: None for name in BTC_RISK_COMPONENTS},
                "btc_risk_score": 0.5,
                "stack_mult": 1.0,
                "score_warmup": False,
            }
        ],
        schema=legacy_schema,
    ).write_parquet(state_path)

    sizer = BtcRiskLiveSizer(state_path, min_prior=0)
    duplicate, stats = sizer.score_decisions(
        [{"symbol": "AAAUSDT", "signal_ts_ms": 10 * DAY_MS}],
        btc_context={},
    )
    assert stats["duplicates"] == 1
    assert duplicate[("AAAUSDT", 10 * DAY_MS)]["decision_evidence"]["evidence_hash"]

    proposed, _ = sizer.score_decisions(
        [{"symbol": "BBBUSDT", "signal_ts_ms": 11 * DAY_MS}],
        btc_context={11 * DAY_MS: {name: None for name in BTC_RISK_COMPONENTS}},
    )
    sizer.ingest_accepted_decisions(
        [
            {
                "symbol": "BBBUSDT",
                "signal_ts_ms": 11 * DAY_MS,
                BTC_RISK_EVIDENCE_METADATA_KEY: proposed[("BBBUSDT", 11 * DAY_MS)]["decision_evidence"],
            }
        ]
    )
    saved = pl.read_parquet(state_path)
    assert saved.height == 2
    assert saved["decision_evidence_json"].null_count() == 0
    assert BtcRiskLiveSizer(state_path, min_prior=0).rows == 2


def test_authoritative_empty_projection_accepts_empty_state(tmp_path: Path) -> None:
    sizer = BtcRiskLiveSizer(tmp_path / "empty.parquet", min_prior=0)

    result = sizer.reconcile_authoritative_accepted_decisions([])

    assert result == {
        "ingested": 0,
        "duplicates": 0,
        "ignored": 0,
        "authoritative_rows": 0,
        "rewritten": 0,
    }
    assert sizer.rows == 0
    assert not sizer.state_path.exists()
    assert (
        _propose_evidence(
            sizer,
            symbol="AAAUSDT",
            signal_ts_ms=10 * DAY_MS,
        )["decision_key"]
        == f"AAAUSDT|{10 * DAY_MS}"
    )


def test_authoritative_empty_reset_rejects_persisted_state_and_blocks_scoring(
    tmp_path: Path,
) -> None:
    first, _ = _accepted_evidence_chain(tmp_path)
    state_path = tmp_path / "persisted.parquet"
    sizer = BtcRiskLiveSizer(state_path, min_prior=0)
    sizer.ingest_accepted_decisions([_accepted_evidence_row(first)])
    last_good = state_path.read_bytes()

    with pytest.raises(ValueError, match="absent from complete authoritative"):
        sizer.reconcile_authoritative_accepted_decisions([])

    assert state_path.read_bytes() == last_good
    assert sizer.rows == 1
    with pytest.raises(RuntimeError, match="scoring is blocked"):
        _propose_evidence(
            sizer,
            symbol="BBBUSDT",
            signal_ts_ms=11 * DAY_MS,
        )

    recovered = sizer.reconcile_authoritative_accepted_decisions([_accepted_evidence_row(first)])
    assert recovered["authoritative_rows"] == 1
    assert (
        _propose_evidence(
            sizer,
            symbol="BBBUSDT",
            signal_ts_ms=11 * DAY_MS,
        )["decision_key"]
        == f"BBBUSDT|{11 * DAY_MS}"
    )


def test_authoritative_subset_of_persisted_chain_fails_closed(tmp_path: Path) -> None:
    first, second = _accepted_evidence_chain(tmp_path)
    sizer = BtcRiskLiveSizer(tmp_path / "persisted-full.parquet", min_prior=0)
    sizer.ingest_accepted_decisions(
        [
            _accepted_evidence_row(first),
            _accepted_evidence_row(second),
        ]
    )

    with pytest.raises(ValueError, match=f"BBBUSDT\\|{11 * DAY_MS}"):
        sizer.reconcile_authoritative_accepted_decisions([_accepted_evidence_row(first)])

    assert sizer.rows == 2


def test_authoritative_complete_replay_catches_up_persisted_prefix(
    tmp_path: Path,
) -> None:
    first, second = _accepted_evidence_chain(tmp_path)
    state_path = tmp_path / "persisted-prefix.parquet"
    sizer = BtcRiskLiveSizer(state_path, min_prior=0)
    sizer.ingest_accepted_decisions([_accepted_evidence_row(first)])

    result = sizer.reconcile_authoritative_accepted_decisions(
        [
            _accepted_evidence_row(second),
            _accepted_evidence_row(first),
            _accepted_evidence_row(first),
        ]
    )

    assert result == {
        "ingested": 1,
        "duplicates": 2,
        "ignored": 0,
        "authoritative_rows": 2,
        "rewritten": 0,
    }
    assert pl.read_parquet(state_path)["decision_key"].to_list() == [
        f"AAAUSDT|{10 * DAY_MS}",
        f"BBBUSDT|{11 * DAY_MS}",
    ]
    assert BtcRiskLiveSizer(state_path, min_prior=0).rows == 2


def test_authoritative_complete_replay_rebuilds_from_genesis(tmp_path: Path) -> None:
    first, second = _accepted_evidence_chain(tmp_path)
    state_path = tmp_path / "fresh-replay.parquet"
    sizer = BtcRiskLiveSizer(state_path, min_prior=0)

    result = sizer.reconcile_authoritative_accepted_decisions(
        [
            _accepted_evidence_row(second),
            _accepted_evidence_row(first),
        ]
    )

    assert result["ingested"] == 2
    assert result["authoritative_rows"] == 2
    assert pl.read_parquet(state_path)["decision_key"].to_list() == [
        f"AAAUSDT|{10 * DAY_MS}",
        f"BBBUSDT|{11 * DAY_MS}",
    ]


def test_authoritative_conflict_with_persisted_decision_fails_closed(
    tmp_path: Path,
) -> None:
    first, _ = _accepted_evidence_chain(tmp_path)
    state_path = tmp_path / "conflict.parquet"
    sizer = BtcRiskLiveSizer(state_path, min_prior=0)
    sizer.ingest_accepted_decisions([_accepted_evidence_row(first)])
    conflicting_source = BtcRiskLiveSizer(tmp_path / "conflicting-source.parquet", min_prior=0)
    conflicting = _propose_evidence(
        conflicting_source,
        symbol="AAAUSDT",
        signal_ts_ms=10 * DAY_MS,
        raw_values={"btc_trend_30d": 0.25},
    )

    with pytest.raises(ValueError, match="conflicts with persisted decision"):
        sizer.reconcile_authoritative_accepted_decisions([_accepted_evidence_row(conflicting)])

    assert pl.read_parquet(state_path).height == 1
    with pytest.raises(RuntimeError, match="scoring is blocked"):
        sizer.score_decisions([], btc_context={})


def test_authoritative_empty_projection_rejects_synthesized_legacy_row(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "legacy-authority.parquet"
    legacy_schema = {
        "decision_key": pl.String,
        "symbol": pl.String,
        "signal_ts_ms": pl.Int64,
        **{name: pl.Float64 for name in BTC_RISK_COMPONENTS},
        "btc_risk_score": pl.Float64,
        "stack_mult": pl.Float64,
        "score_warmup": pl.Boolean,
    }
    pl.DataFrame(
        [
            {
                "decision_key": f"AAAUSDT|{10 * DAY_MS}",
                "symbol": "AAAUSDT",
                "signal_ts_ms": 10 * DAY_MS,
                **{name: None for name in BTC_RISK_COMPONENTS},
                "btc_risk_score": 0.5,
                "stack_mult": 1.0,
                "score_warmup": False,
            }
        ],
        schema=legacy_schema,
    ).write_parquet(state_path)
    last_good = state_path.read_bytes()

    with pytest.raises(ValueError, match="absent from complete authoritative"):
        BtcRiskLiveSizer(state_path, min_prior=0).reconcile_authoritative_accepted_decisions([])

    assert state_path.read_bytes() == last_good


def test_authoritative_matching_receipt_rewrites_synthesized_legacy_row(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "legacy-rewrite.parquet"
    pl.DataFrame(
        [
            {
                "decision_key": f"AAAUSDT|{10 * DAY_MS}",
                "symbol": "AAAUSDT",
                "signal_ts_ms": 10 * DAY_MS,
                **{name: None for name in BTC_RISK_COMPONENTS},
                "btc_risk_score": 0.5,
                "stack_mult": 1.0,
                "score_warmup": False,
            }
        ],
        schema={
            "decision_key": pl.String,
            "symbol": pl.String,
            "signal_ts_ms": pl.Int64,
            **{name: pl.Float64 for name in BTC_RISK_COMPONENTS},
            "btc_risk_score": pl.Float64,
            "stack_mult": pl.Float64,
            "score_warmup": pl.Boolean,
        },
    ).write_parquet(state_path)
    sizer = BtcRiskLiveSizer(state_path, min_prior=0)
    lookup, _ = sizer.score_decisions(
        [{"symbol": "AAAUSDT", "signal_ts_ms": 10 * DAY_MS}],
        btc_context={},
    )
    evidence = lookup[("AAAUSDT", 10 * DAY_MS)]["decision_evidence"]

    result = sizer.reconcile_authoritative_accepted_decisions([_accepted_evidence_row(evidence)])

    assert result["ingested"] == 0
    assert result["rewritten"] == 1
    saved = pl.read_parquet(state_path)
    assert saved["decision_evidence_json"].null_count() == 0
    assert saved["decision_evidence_json"][0]


@pytest.mark.parametrize("failure_mode", ["file_fsync", "replace", "directory_fsync"])
def test_state_save_failure_preserves_last_good_file_and_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    state_path = tmp_path / f"save-{failure_mode}.parquet"
    sizer = BtcRiskLiveSizer(state_path, min_prior=0)
    first = _propose_evidence(
        sizer,
        symbol="AAAUSDT",
        signal_ts_ms=10 * DAY_MS,
    )
    sizer.ingest_accepted_decisions([_accepted_evidence_row(first)])
    last_good = state_path.read_bytes()
    second = _propose_evidence(
        sizer,
        symbol="BBBUSDT",
        signal_ts_ms=11 * DAY_MS,
    )

    if failure_mode == "file_fsync":

        def fail_file_fsync(_path: Path) -> None:
            raise OSError("injected file fsync failure")

        monkeypatch.setattr(btc_risk_module, "_fsync_file", fail_file_fsync)
    elif failure_mode == "replace":
        real_replace = btc_risk_module.os.replace

        def fail_new_state_replace(source: str | Path, destination: str | Path) -> None:
            if Path(source).suffix == ".tmp" and Path(destination) == state_path:
                raise OSError("injected replace failure")
            real_replace(source, destination)

        monkeypatch.setattr(btc_risk_module.os, "replace", fail_new_state_replace)
    else:
        real_fsync_directory = btc_risk_module._fsync_directory
        directory_fsync_calls = 0

        def fail_replacement_directory_fsync(path: Path) -> None:
            nonlocal directory_fsync_calls
            directory_fsync_calls += 1
            if directory_fsync_calls == 2:
                raise OSError("injected directory fsync failure")
            real_fsync_directory(path)

        monkeypatch.setattr(
            btc_risk_module,
            "_fsync_directory",
            fail_replacement_directory_fsync,
        )

    with pytest.raises(OSError, match="injected"):
        sizer.ingest_accepted_decisions([_accepted_evidence_row(second)])

    assert state_path.read_bytes() == last_good
    assert sizer.rows == 1
    assert BtcRiskLiveSizer(state_path, min_prior=0).rows == 1
    retry = _propose_evidence(
        sizer,
        symbol="BBBUSDT",
        signal_ts_ms=11 * DAY_MS,
    )
    assert retry["evidence_hash"] == second["evidence_hash"]
    assert list(tmp_path.glob(f".{state_path.name}.*")) == []


def test_state_save_uses_a_new_temporary_path_for_each_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "unique-temp.parquet"
    sizer = BtcRiskLiveSizer(state_path, min_prior=0)
    first = _propose_evidence(
        sizer,
        symbol="AAAUSDT",
        signal_ts_ms=10 * DAY_MS,
    )
    sizer.ingest_accepted_decisions([_accepted_evidence_row(first)])
    second = _propose_evidence(
        sizer,
        symbol="BBBUSDT",
        signal_ts_ms=11 * DAY_MS,
    )
    attempted_paths: list[Path] = []

    def record_and_fail(path: Path) -> None:
        attempted_paths.append(path)
        raise OSError("injected file fsync failure")

    monkeypatch.setattr(btc_risk_module, "_fsync_file", record_and_fail)
    for _ in range(2):
        with pytest.raises(OSError, match="injected"):
            sizer.ingest_accepted_decisions([_accepted_evidence_row(second)])

    assert len(attempted_paths) == 2
    assert attempted_paths[0] != attempted_paths[1]
    assert all(path.suffix == ".tmp" for path in attempted_paths)
    assert list(tmp_path.glob(f".{state_path.name}.*")) == []
