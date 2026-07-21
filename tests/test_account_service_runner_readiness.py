from __future__ import annotations

import ast
from pathlib import Path

import pytest

from liquidity_migration.account_reconcile import AccountReconciliationReport
from liquidity_migration.account_route import ensure_account_route
from liquidity_migration.account_service import (
    AccountIntentInbox,
    AccountTargetRequest,
    RequestedIntent,
    SleeveAdapterKind,
)
from liquidity_migration.account_market_readiness import (
    RequestedMarketReadiness,
    RequestedMarketWarmupGate,
    run_ready_request_or_converge,
)
from liquidity_migration.account_service_runner import require_startup_reconciliation_safe
from liquidity_migration.execution_adapters import BookLevel, L2BookSnapshot
from liquidity_migration.market_capture import operational_market_symbols
from liquidity_migration.strategy_runtime import SleeveTargetIntent


class _Recorder:
    def __init__(
        self,
        books: dict[str, L2BookSnapshot] | None = None,
        *,
        observed_wall_ns: int = 1_000,
    ) -> None:
        self.books = dict(books or {})
        self.observed_wall_ns = observed_wall_ns

    def current_book(self, symbol: str) -> L2BookSnapshot | None:
        return self.books.get(symbol.upper())

    def current_book_with_observed_wall_ns(
        self,
        symbol: str,
    ) -> tuple[L2BookSnapshot | None, int]:
        return self.current_book(symbol), self.observed_wall_ns


class _UpdateBetweenClockAndBookReadRecorder(_Recorder):
    """Recreate a WebSocket update overtaking a caller's earlier clock sample."""

    def current_book(self, symbol: str) -> L2BookSnapshot | None:
        self.books[symbol.upper()] = _book(symbol, local_receive_ts_ns=1_001)
        return super().current_book(symbol)

    def current_book_with_observed_wall_ns(
        self,
        symbol: str,
    ) -> tuple[L2BookSnapshot | None, int]:
        return self.current_book(symbol), 1_002


def _inbox(tmp_path: Path) -> AccountIntentInbox:
    route = ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )
    return AccountIntentInbox(route)


def _request(
    inbox: AccountIntentInbox,
    *,
    request_id: str,
    symbols: tuple[str, ...],
) -> AccountTargetRequest:
    return AccountTargetRequest(
        request_id=request_id,
        batch_id=request_id,
        created_ts_ns=1_000,
        route_id=inbox.route.route_id,
        account_id=inbox.route.account_id,
        environment=inbox.route.environment,
        intents=tuple(
            RequestedIntent(
                adapter_kind=SleeveAdapterKind.LONG,
                intent=SleeveTargetIntent(
                    decision_key=f"decision:{request_id}:{symbol}",
                    target_key=f"long/test/{symbol}",
                    strategy_id="test",
                    component_id=symbol.lower(),
                    symbol=symbol,
                    signed_notional_usdt=10.0,
                    leverage=2.0,
                    reason="readiness-test",
                ),
            )
            for symbol in symbols
        ),
    )


def _book(
    symbol: str,
    *,
    local_receive_ts_ns: int = 950,
    sequence_gap: bool = False,
) -> L2BookSnapshot:
    return L2BookSnapshot(
        symbol=symbol,
        sequence=10,
        previous_sequence=9,
        exchange_ts_ns=900,
        local_receive_ts_ns=local_receive_ts_ns,
        bids=(BookLevel(9.9, 10.0),),
        asks=(BookLevel(10.1, 10.0),),
        sequence_gap=sequence_gap,
    )


def _evaluate(
    gate: RequestedMarketWarmupGate,
    inbox: AccountIntentInbox,
    recorder: _Recorder,
    *,
    now_monotonic: float,
):
    return gate.evaluate(
        inbox=inbox,
        recorder=recorder,  # type: ignore[arg-type]
        verified_rule_symbols={"AUSDT", "BUSDT", "CUSDT"},
        now_monotonic=now_monotonic,
        max_market_age_ns=100,
    )


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0.0, 30.000001])
def test_warmup_timeout_rejects_non_finite_or_weakened_values(timeout: float) -> None:
    with pytest.raises(ValueError, match="registered 30 seconds"):
        RequestedMarketWarmupGate(timeout_seconds=timeout)


def test_queue_head_waits_for_every_exact_symbol_while_later_books_warm_in_parallel(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path)
    first = _request(
        inbox,
        request_id="arrived-first",
        symbols=("AUSDT", "CUSDT"),
    )
    later = _request(inbox, request_id="arrived-later", symbols=("BUSDT",))
    inbox.submit(first)
    inbox.submit(later)
    recorder = _Recorder({"AUSDT": _book("AUSDT"), "BUSDT": _book("BUSDT")})
    gate = RequestedMarketWarmupGate(timeout_seconds=10.0)

    waiting = _evaluate(gate, inbox, recorder, now_monotonic=1.0)

    assert waiting.request_id == first.request_id
    assert waiting.symbols == ("AUSDT", "CUSDT")
    assert waiting.ready is False
    assert "CUSDT:no_snapshot" in waiting.detail
    assert inbox.requested_symbols() == {"AUSDT", "BUSDT", "CUSDT"}
    assert inbox.peek_next() == first
    assert list((inbox.root / "completed").glob("*.json")) == []

    recorder.books["CUSDT"] = _book("CUSDT")
    ready = _evaluate(gate, inbox, recorder, now_monotonic=2.0)
    assert ready.request_id == first.request_id
    assert ready.ready is True


def test_gap_or_stale_queue_head_book_remains_warming(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    request = _request(inbox, request_id="head", symbols=("AUSDT",))
    inbox.submit(request)
    recorder = _Recorder({"AUSDT": _book("AUSDT", sequence_gap=True)})
    gate = RequestedMarketWarmupGate(timeout_seconds=10.0)

    gap = _evaluate(gate, inbox, recorder, now_monotonic=1.0)
    assert gap.ready is False
    assert "AUSDT:sequence_gap" in gap.detail

    recorder.books["AUSDT"] = _book("AUSDT", local_receive_ts_ns=899)
    stale = _evaluate(gate, inbox, recorder, now_monotonic=2.0)
    assert stale.ready is False
    assert "AUSDT:stale_book" in stale.detail


def test_book_update_after_caller_clock_sample_is_not_misclassified_as_future(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path)
    inbox.submit(_request(inbox, request_id="head", symbols=("AUSDT",)))

    readiness = _evaluate(
        RequestedMarketWarmupGate(timeout_seconds=10.0),
        inbox,
        _UpdateBetweenClockAndBookReadRecorder(),
        now_monotonic=1.0,
    )

    assert readiness.ready is True
    assert readiness.detail == ""


def test_genuine_wall_clock_regression_keeps_queue_head_blocked(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    inbox.submit(_request(inbox, request_id="head", symbols=("AUSDT",)))
    recorder = _Recorder(
        {"AUSDT": _book("AUSDT", local_receive_ts_ns=1_001)},
        observed_wall_ns=1_000,
    )

    readiness = _evaluate(
        RequestedMarketWarmupGate(timeout_seconds=10.0),
        inbox,
        recorder,
        now_monotonic=1.0,
    )

    assert readiness.ready is False
    assert "AUSDT:future_book" in readiness.detail


def test_warmup_timeout_latches_health_closed_without_consuming_request(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path)
    request = _request(inbox, request_id="head", symbols=("AUSDT",))
    inbox.submit(request)
    recorder = _Recorder()
    gate = RequestedMarketWarmupGate(timeout_seconds=5.0)

    assert _evaluate(gate, inbox, recorder, now_monotonic=10.0).timed_out is False
    timed_out = _evaluate(gate, inbox, recorder, now_monotonic=15.0)
    assert timed_out.ready is False
    assert timed_out.timed_out is True
    assert "request remains pending and owner epoch is closed" in timed_out.detail

    recorder.books["AUSDT"] = _book("AUSDT")
    still_closed = _evaluate(gate, inbox, recorder, now_monotonic=16.0)
    assert still_closed.ready is False
    assert still_closed.timed_out is True
    assert inbox.peek_next() == request
    assert len(list((inbox.root / "pending").glob("*.json"))) == 1
    assert list((inbox.root / "failed").glob("*.json")) == []
    assert list((inbox.root / "completed").glob("*.json")) == []


def test_missing_verified_rule_closes_epoch_immediately_and_latches(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    request = _request(inbox, request_id="head", symbols=("AUSDT",))
    inbox.submit(request)
    recorder = _Recorder({"AUSDT": _book("AUSDT")})
    gate = RequestedMarketWarmupGate(timeout_seconds=5.0)

    missing = gate.evaluate(
        inbox=inbox,
        recorder=recorder,  # type: ignore[arg-type]
        verified_rule_symbols=set(),
        now_monotonic=1.0,
        max_market_age_ns=100,
    )
    assert missing.ready is False
    assert missing.timed_out is True
    assert "owner epoch is closed" in missing.detail

    still_closed = gate.evaluate(
        inbox=inbox,
        recorder=recorder,  # type: ignore[arg-type]
        verified_rule_symbols={"AUSDT"},
        now_monotonic=2.0,
        max_market_age_ns=100,
    )
    assert still_closed.ready is False
    assert still_closed.timed_out is True
    assert still_closed.detail == missing.detail
    assert inbox.peek_next() == request


def test_no_pending_request_is_ready_without_inventing_a_head(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)

    readiness = _evaluate(
        RequestedMarketWarmupGate(timeout_seconds=5.0),
        inbox,
        _Recorder(),
        now_monotonic=1.0,
    )

    assert readiness.ready is True
    assert readiness.request_id == ""
    assert readiness.symbols == ()


def test_idle_owner_requires_only_one_stable_market_heartbeat() -> None:
    allowlist = {f"COIN{index:03d}USDT" for index in range(515)} | {"BTCUSDT"}

    required = operational_market_symbols(allowlist)

    assert required == {"BTCUSDT"}
    assert len(required) == 1
    assert operational_market_symbols({"ZUSDT", "AUSDT"}) == {"AUSDT"}


def test_owner_market_set_includes_every_pending_and_active_symbol() -> None:
    required = operational_market_symbols(
        {"BTCUSDT", "AUSDT", "BUSDT", "CUSDT", "DUSDT", "EUSDT", "FUSDT"},
        queued={"ausdt"},
        nonflat={"BUSDT"},
        working={"cusdt"},
        component_targets={"DUSDT"},
        convergence={"eusdt"},
        markouts={"fusdt"},
    )

    assert required == {
        "BTCUSDT",
        "AUSDT",
        "BUSDT",
        "CUSDT",
        "DUSDT",
        "EUSDT",
        "FUSDT",
    }


class _CycleService:
    def __init__(self) -> None:
        self.run_request_ids: list[str | None] = []
        self.safety_calls = 0
        self.convergence_calls = 0

    def run_safety_flat_once(self, inbox: AccountIntentInbox) -> None:
        del inbox
        self.safety_calls += 1

    def run_once(
        self,
        inbox: AccountIntentInbox,
        *,
        expected_request_id: str | None = None,
    ) -> str:
        del inbox
        self.run_request_ids.append(expected_request_id)
        return "receipt"

    def converge_once(self) -> None:
        self.convergence_calls += 1


class _SafetyCycleService(_CycleService):
    def run_safety_flat_once(self, inbox: AccountIntentInbox) -> str:
        del inbox
        self.safety_calls += 1
        return "safety-receipt"


def test_unready_head_stays_pending_without_starving_prior_convergence(
    tmp_path: Path,
) -> None:
    service = _CycleService()
    inbox = _inbox(tmp_path)
    request = _request(inbox, request_id="unready-head", symbols=("AUSDT",))
    inbox.submit(request)

    receipt = run_ready_request_or_converge(
        service=service,  # type: ignore[arg-type]
        inbox=inbox,
        readiness=RequestedMarketReadiness(
            request_id=request.request_id,
            symbols=("AUSDT",),
            ready=False,
            timed_out=True,
            detail="owner epoch is closed",
        ),
    )

    assert receipt is None
    assert service.convergence_calls == 1
    assert service.safety_calls == 1
    assert service.run_request_ids == []
    assert inbox.peek_next() == request


def test_ready_head_is_claimed_by_exact_id_without_extra_convergence(
    tmp_path: Path,
) -> None:
    service = _CycleService()
    inbox = _inbox(tmp_path)

    receipt = run_ready_request_or_converge(
        service=service,  # type: ignore[arg-type]
        inbox=inbox,
        readiness=RequestedMarketReadiness(
            request_id="ready-head",
            symbols=("AUSDT",),
            ready=True,
            timed_out=False,
            detail="",
        ),
    )

    assert receipt == "receipt"
    assert service.run_request_ids == ["ready-head"]
    assert service.convergence_calls == 0
    assert service.safety_calls == 1


def test_empty_inbox_preserves_prior_convergence_without_strict_claim(
    tmp_path: Path,
) -> None:
    service = _CycleService()
    inbox = _inbox(tmp_path)

    receipt = run_ready_request_or_converge(
        service=service,  # type: ignore[arg-type]
        inbox=inbox,
        readiness=RequestedMarketReadiness(
            request_id="",
            symbols=(),
            ready=True,
            timed_out=False,
            detail="",
        ),
    )

    assert receipt is None
    assert service.run_request_ids == []
    assert service.convergence_calls == 1
    assert service.safety_calls == 1


def test_safety_flat_runs_even_when_ordinary_queue_head_is_unready(
    tmp_path: Path,
) -> None:
    service = _SafetyCycleService()
    inbox = _inbox(tmp_path)

    receipt = run_ready_request_or_converge(
        service=service,  # type: ignore[arg-type]
        inbox=inbox,
        readiness=RequestedMarketReadiness(
            request_id="unready-entry",
            symbols=("AUSDT",),
            ready=False,
            timed_out=True,
            detail="owner epoch is closed",
        ),
    )

    assert receipt == "safety-receipt"
    assert service.run_request_ids == []
    assert service.convergence_calls == 0


def test_startup_allows_only_typed_native_breach_recovery() -> None:
    def report(
        *mismatches: str,
        native_protection_breach_only: bool = False,
    ) -> AccountReconciliationReport:
        return AccountReconciliationReport(
            snapshot_key="snapshot",
            healthy=not mismatches,
            pending_orders_checked=0,
            execution_rows_observed=0,
            order_rows_observed=0,
            venue_positions={"BUSDT": -2.0},
            reconstructed_positions={"BUSDT": -2.0},
            mismatches=mismatches,
            observed_ts_ns=1,
            native_protection_breach_only=native_protection_breach_only,
        )

    require_startup_reconciliation_safe(report())
    require_startup_reconciliation_safe(
        report(
            "native_protection:NativeProtectionReconciliationError:stop crossed",
            native_protection_breach_only=True,
        )
    )
    with pytest.raises(RuntimeError, match="NativeProtectionBreachError"):
        require_startup_reconciliation_safe(
            report(
                "native_protection:RuntimeError:provider text mentions NativeProtectionBreachError"
            )
        )
    with pytest.raises(RuntimeError, match="venue=-2"):
        require_startup_reconciliation_safe(report("BUSDT:venue=-2:reconstructed=0:tol=0.05"))
    with pytest.raises(RuntimeError, match="transport failed"):
        require_startup_reconciliation_safe(report("native_protection:RuntimeError:transport failed"))


def test_demo_and_paper_owners_share_the_strict_expected_head_gate() -> None:
    repo = Path(__file__).resolve().parents[1]
    for filename in ("account_service_runner.py", "account_paper_runner.py"):
        source = (repo / "liquidity_migration" / filename).read_text(encoding="utf-8")
        assert "RequestedMarketWarmupGate" in source
        assert "run_ready_request_or_converge(" in source
        assert "operational_market_symbols(" in source
        assert "public_stream.start(live_symbols)" in source
        assert "public_stream.update_symbols(desired)" in source


def test_demo_and_paper_validate_registered_startup_bounds_before_owner_identity() -> None:
    repo = Path(__file__).resolve().parents[1]
    for filename in ("account_service_runner.py", "account_paper_runner.py"):
        source = (repo / "liquidity_migration" / filename).read_text(encoding="utf-8")
        main = source[source.index("def main(") :]
        assert main.index("require_registered_demo_rule_max_age_hours(") < main.index("require_systemd_invocation_id()")
        assert main.index("require_registered_request_market_warmup_timeout(") < main.index(
            "require_systemd_invocation_id()"
        )


def test_demo_and_paper_owner_recorders_bind_validated_systemd_invocation() -> None:
    repo = Path(__file__).resolve().parents[1]
    for filename in ("account_service_runner.py", "account_paper_runner.py"):
        source = (repo / "liquidity_migration" / filename).read_text(encoding="utf-8")
        tree = ast.parse(source)
        recorder_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "SequenceAwareMarketRecorder"
        ]
        assert len(recorder_calls) == 1
        invocation_keywords = [
            keyword.value for keyword in recorder_calls[0].keywords if keyword.arg == "owner_invocation_id"
        ]
        assert len(invocation_keywords) == 1
        assert isinstance(invocation_keywords[0], ast.Name)
        assert invocation_keywords[0].id == "invocation_id"
        assert "invocation_id = require_systemd_invocation_id()" in source


def test_demo_owner_supervises_private_execution_stream_before_admission() -> None:
    repo = Path(__file__).resolve().parents[1]
    source = (repo / "liquidity_migration" / "account_service_runner.py").read_text(encoding="utf-8")
    loop = source[source.index("        while True:") :]
    health_chain = source[
        source.index("health_chain = AccountHealthChain(") : source.index(
            "snapshot_provider =", source.index("health_chain = AccountHealthChain(")
        )
    ]

    assert "private_stream_supervisor = PrivateExecutionStreamSupervisor(" in source
    assert "private_stream_supervisor" in health_chain
    assert loop.index("private_stream_supervisor.check(") < loop.index("run_ready_request_or_converge(")
    assert "private_stream_status is True" in loop
    assert "private_stream_supervisor.health_detail" in loop

    wrapper = (repo / "scripts" / "run_account_execution_service.sh").read_text(encoding="utf-8")
    assert 'ACCOUNT_PRIVATE_WS_RECONNECT_SECONDS="${ACCOUNT_PRIVATE_WS_RECONNECT_SECONDS:-180}"' in wrapper
    assert '--private-ws-reconnect-seconds "$ACCOUNT_PRIVATE_WS_RECONNECT_SECONDS"' in wrapper
    assert 'CONTINUOUS_CYCLE_ROOT="${CONTINUOUS_CYCLE_ROOT:-$REPO_ROOT/data/bybit-continuous-demo-event}"' in wrapper
    assert '--continuous-cycle-root "$CONTINUOUS_CYCLE_ROOT"' in wrapper


def test_protection_market_refs_skips_gapped_books_instead_of_raising() -> None:
    """A dropped L2 delta must cost one protection cycle, never the owner
    process: market_ref raises for gapped books and the runner's protection
    loop has no other handler between it and process exit."""

    from liquidity_migration.account_service_runner import protection_market_refs
    from liquidity_migration.execution_adapters import BookLevel, L2BookSnapshot

    healthy = L2BookSnapshot(
        symbol="BTCUSDT",
        sequence=100,
        previous_sequence=99,
        exchange_ts_ns=900,
        local_receive_ts_ns=1_000,
        bids=(BookLevel(9.9, 1.0),),
        asks=(BookLevel(10.1, 1.0),),
        sequence_gap=False,
        clock_offset_estimate_ns=None,
    )
    gapped = L2BookSnapshot(
        symbol="BUSDT",
        sequence=200,
        previous_sequence=150,
        exchange_ts_ns=900,
        local_receive_ts_ns=1_000,
        bids=(BookLevel(9.9, 1.0),),
        asks=(BookLevel(10.1, 1.0),),
        sequence_gap=True,
        clock_offset_estimate_ns=None,
    )

    class Recorder:
        def current_book(self, symbol: str):
            return {"BTCUSDT": healthy, "BUSDT": gapped, "TLMUSDT": None}[symbol]

    refs, skipped = protection_market_refs(Recorder(), ["BTCUSDT", "BUSDT", "TLMUSDT"])

    assert set(refs) == {"BTCUSDT"}
    assert "book_sequence_gap" in skipped["BUSDT"]
    assert skipped["TLMUSDT"] == "no_book"
