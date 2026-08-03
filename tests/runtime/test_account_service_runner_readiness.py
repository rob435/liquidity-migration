from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from liquidity_migration.venue.account_reconcile import AccountReconciliationReport
from liquidity_migration.account.account_route import ensure_account_route
from liquidity_migration.account.account_service import (
    AccountIntentInbox,
    AccountTargetRequest,
    RequestedIntent,
    SleeveAdapterKind,
)
from liquidity_migration.account.account_market_readiness import (
    RequestedMarketReadiness,
    RequestedMarketWarmupGate,
    require_registered_request_market_warmup_timeout,
    run_ready_request_or_converge,
)
from liquidity_migration.account.account_owner_health import (
    require_systemd_invocation_id,
    validate_systemd_invocation_id,
)
from liquidity_migration.policy.account_execution_config import (
    require_registered_demo_rule_max_age_hours,
)
from liquidity_migration.runtime.account_service_runner import (
    account_has_open_exposure,
    degrade_or_raise,
    require_startup_reconciliation_safe,
)
from liquidity_migration.account.execution_adapters import BookLevel, L2BookSnapshot
from liquidity_migration.account.market_capture import operational_market_symbols
from liquidity_migration.account.strategy_runtime import SleeveTargetIntent


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


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0.0, -1.0])
def test_warmup_timeout_rejects_non_finite_or_non_positive_values(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        RequestedMarketWarmupGate(timeout_seconds=timeout)


def test_registered_warmup_ceiling_binds_mainnet_and_not_demo() -> None:
    """The 30s ceiling is a mainnet startup bound, not an invariant of the gate.

    A demo owner on a slow or reconnecting link must be able to widen its
    warmup budget with a flag rather than a code deploy.
    """

    assert require_registered_request_market_warmup_timeout(30.0) == 30.0
    with pytest.raises(ValueError, match="registered 30 seconds"):
        require_registered_request_market_warmup_timeout(30.000001)
    assert (
        require_registered_request_market_warmup_timeout(
            120.0,
            enforce_registered_ceiling=False,
        )
        == 120.0
    )
    assert RequestedMarketWarmupGate(timeout_seconds=120.0).timeout_seconds == 120.0


def test_registered_demo_rule_age_ceiling_binds_mainnet_and_not_demo() -> None:
    """Same split for the instrument-rule receipt age.

    With a stale receipt and a probe that will not run, a hard ceiling leaves
    no way to start a demo owner at all.
    """

    assert require_registered_demo_rule_max_age_hours(168.0) == 168.0
    with pytest.raises(ValueError, match="registered 168 hours"):
        require_registered_demo_rule_max_age_hours(168.1)
    assert (
        require_registered_demo_rule_max_age_hours(
            720.0,
            enforce_registered_ceiling=False,
        )
        == 720.0
    )
    for rejected in (float("nan"), float("inf"), 0.0, -1.0):
        with pytest.raises(ValueError, match="finite and positive"):
            require_registered_demo_rule_max_age_hours(
                rejected,
                enforce_registered_ceiling=False,
            )


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


def test_overdue_warmup_reports_unservable_head_and_recovers_on_a_healthy_book(
    tmp_path: Path,
) -> None:
    """A book gap must cost cycles, not the owner epoch.

    The head stays pending and health stays blocked for as long as its book is
    unusable, and the detail says how long -- but the same head becomes
    servable the moment a healthy book arrives, with no process restart.
    """

    inbox = _inbox(tmp_path)
    request = _request(inbox, request_id="head", symbols=("AUSDT",))
    inbox.submit(request)
    recorder = _Recorder()
    gate = RequestedMarketWarmupGate(timeout_seconds=5.0)

    assert _evaluate(gate, inbox, recorder, now_monotonic=10.0).timed_out is False
    overdue = _evaluate(gate, inbox, recorder, now_monotonic=15.0)
    assert overdue.ready is False
    assert overdue.timed_out is True
    assert "AUSDT:no_snapshot" in overdue.detail
    assert "unservable for 5.0s" in overdue.detail
    assert "remains pending" in overdue.detail

    recorder.books["AUSDT"] = _book("AUSDT")
    recovered = _evaluate(gate, inbox, recorder, now_monotonic=16.0)
    assert recovered.ready is True
    assert recovered.timed_out is False
    assert recovered.detail == ""
    assert inbox.peek_next() == request
    assert len(list((inbox.root / "pending").glob("*.json"))) == 1
    assert list((inbox.root / "failed").glob("*.json")) == []
    assert list((inbox.root / "completed").glob("*.json")) == []


def test_missing_verified_rule_blocks_the_head_until_the_rule_is_verified(
    tmp_path: Path,
) -> None:
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
    assert "queue head lacks venue-verified rules: AUSDT" in missing.detail

    verified = gate.evaluate(
        inbox=inbox,
        recorder=recorder,  # type: ignore[arg-type]
        verified_rule_symbols={"AUSDT"},
        now_monotonic=2.0,
        max_market_age_ns=100,
    )
    assert verified.ready is True
    assert verified.detail == ""
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


class _ExposureKernel:
    """Minimal kernel stand-in for the startup exposure classification."""

    def __init__(self, *, positions: dict[str, float], working: set[str] | None = None) -> None:
        self._state = SimpleNamespace(
            positions={
                symbol: SimpleNamespace(signed_qty=qty) for symbol, qty in positions.items()
            },
            working_order_ids=working or set(),
        )

    def _state_ref(self) -> SimpleNamespace:
        return self._state


def _report(*, venue_positions: dict[str, float]) -> AccountReconciliationReport:
    return AccountReconciliationReport(
        snapshot_key="snapshot",
        healthy=False,
        pending_orders_checked=0,
        execution_rows_observed=0,
        order_rows_observed=0,
        venue_positions=venue_positions,
        reconstructed_positions={},
        mismatches=("BUSDT:venue=-2:reconstructed=0:tol=0.05",),
        observed_ts_ns=1,
    )


def test_startup_failure_over_a_flat_account_still_aborts_loudly() -> None:
    """Degrading a startup failure must not soften one that strands nothing."""

    error = RuntimeError("account reconciliation unhealthy: BUSDT:venue=-2")
    with pytest.raises(RuntimeError, match="venue=-2"):
        degrade_or_raise(
            stage="startup reconciliation",
            error=error,
            kernel=_ExposureKernel(positions={"BUSDT": 0.0}),
            report=_report(venue_positions={}),
        )


@pytest.mark.parametrize(
    ("kernel", "report"),
    [
        (_ExposureKernel(positions={"BUSDT": -2.0}), _report(venue_positions={})),
        (_ExposureKernel(positions={}, working={"cmd-1"}), _report(venue_positions={})),
        (_ExposureKernel(positions={}), _report(venue_positions={"BUSDT": -2.0})),
    ],
    ids=["journal-position", "working-order", "venue-position"],
)
def test_startup_failure_over_open_exposure_degrades_instead_of_crash_looping(
    kernel: _ExposureKernel,
    report: AccountReconciliationReport,
) -> None:
    """Exiting on a startup failure over open exposure is a 2s crash loop with the book
    unmanaged, so the runner degrades instead.
    """

    degradation = degrade_or_raise(
        stage="startup reconciliation",
        error=RuntimeError("account reconciliation unhealthy: BUSDT:venue=-2"),
        kernel=kernel,
        report=report,
    )

    assert degradation.stage == "startup reconciliation"
    assert "venue=-2" in degradation.detail
    assert degradation.describe().startswith("startup degraded at startup reconciliation:")


def test_open_exposure_detection_treats_an_unread_venue_as_flat_only_when_the_journal_is() -> None:
    assert account_has_open_exposure(
        kernel=_ExposureKernel(positions={"BUSDT": 1e-9}), report=None
    )
    assert not account_has_open_exposure(
        kernel=_ExposureKernel(positions={"BUSDT": 0.0}), report=None
    )


def test_runner_degrades_every_exposure_bearing_startup_check() -> None:
    """Each startup check that can strand a live book routes through the degrade path.

    The venue-order-ownership check is the one exception, and only off mainnet:
    an owner that refuses to start cannot cancel the stray order it refused
    over, and the 2s reconciler runs the same check every cycle and marks
    health. Its degrade stage is still here for the mainnet branch.
    """

    source = (
        Path(__file__).resolve().parents[2]
        / "liquidity_migration" / "runtime" / "account_service_runner.py"
    ).read_text(encoding="utf-8")
    for stage in (
        '"venue order ownership"',
        '"bootstrap reconciliation"',
        '"startup reconciliation"',
        '"startup funding reconciliation"',
        '"startup native protection sync"',
    ):
        assert stage in source, stage
    # The bare raising forms must not survive at function level (they are only
    # reachable now from inside a try that hands the error to degrade_or_raise).
    # Each pair is load-bearing together: the "not in" alone passes vacuously the
    # day one of these is renamed, which is exactly what happened to the ownership
    # check, so pin the guarded form too.
    for guarded in (
        "require_startup_reconciliation_safe(",
        "require_bybit_order_ownership(",
    ):
        assert f"\n    {guarded}" not in source, guarded
        assert f"\n        {guarded}" in source, guarded
    assert "\n    startup_funding_reconciliation.require_healthy()" not in source
    # ...and the latch is folded into published health rather than only logged.
    assert "if startup_degradations:" in source
    assert "startup_degradations.clear()" in source


def test_demo_owner_uses_the_strict_expected_head_gate() -> None:
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "liquidity_migration" / "runtime" / "account_service_runner.py").read_text(
        encoding="utf-8"
    )
    assert "RequestedMarketWarmupGate" in source
    assert "run_ready_request_or_converge(" in source
    assert "operational_market_symbols(" in source
    assert "public_stream.start(live_symbols)" in source
    assert "public_stream.update_symbols(desired)" in source


def _owner_argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--account-root", str(tmp_path / "account"),
        "--inbox-root", str(tmp_path / "inbox"),
        "--capture-root", str(tmp_path / "capture"),
        "--symbols-file", str(tmp_path / "symbols.json"),
        "--demo-rules-file", str(tmp_path / "rules.json"),
        "--risk-policy-file", str(tmp_path / "risk.json"),
        "--disaster-stop-fraction", "0.25",
        *extra,
    ]


@pytest.mark.parametrize(
    ("flag", "value", "message"),
    (
        ("--max-demo-rule-age-hours", "999", "registered 168 hours"),
        ("--request-market-warmup-timeout-seconds", "120", "registered 30 seconds"),
    ),
)
def test_mainnet_holds_registered_startup_bounds_before_touching_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    value: str,
    message: str,
) -> None:
    """A weakened bound fails argument parsing, not something later.

    Reaching credentials or the mutation lease first would mean a bad bound is
    only caught after the owner has started binding real account state.
    """

    import liquidity_migration.runtime.account_service_runner as runner

    monkeypatch.setenv("REAL_MONEY", "true")
    monkeypatch.setenv("BYBIT_REAL_API_KEY", "k")
    monkeypatch.setenv("BYBIT_REAL_API_SECRET", "s")

    with pytest.raises(SystemExit):
        runner.main(_owner_argv(tmp_path, "--realm", "mainnet", flag, value))

    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flag", "value"),
    (
        ("--max-demo-rule-age-hours", "999"),
        ("--request-market-warmup-timeout-seconds", "120"),
    ),
)
def test_demo_accepts_a_widened_startup_bound_and_runs_on_to_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    value: str,
) -> None:
    """Demo takes any positive finite bound, so a stale receipt or a slow link
    is an operator flag away from starting rather than a code deploy."""

    import liquidity_migration.runtime.account_service_runner as runner

    monkeypatch.setenv("REAL_MONEY", "false")
    for key in ("BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET"):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(RuntimeError, match="BYBIT_DEMO_API_KEY"):
        runner.main(_owner_argv(tmp_path, "--realm", "demo", flag, value))


def test_owner_generation_id_is_required_only_where_systemd_supervises() -> None:
    """Mainnet must be the owner systemd supervises; demo may be run by hand.

    The synthesized id keeps systemd's exact shape because health and capture
    projections validate it, and a malformed value stays a fault either way.
    """

    with pytest.raises(RuntimeError, match="INVOCATION_ID is required"):
        require_systemd_invocation_id({})

    synthesized = require_systemd_invocation_id({}, require_systemd=False)
    assert validate_systemd_invocation_id(synthesized) == synthesized
    assert synthesized != require_systemd_invocation_id({}, require_systemd=False)

    supervised = {"INVOCATION_ID": "ab" * 16}
    assert require_systemd_invocation_id(supervised) == "ab" * 16
    assert require_systemd_invocation_id(supervised, require_systemd=False) == "ab" * 16
    with pytest.raises(RuntimeError, match="hexadecimal"):
        require_systemd_invocation_id({"INVOCATION_ID": "nope"}, require_systemd=False)


def test_demo_owner_recorder_binds_validated_systemd_invocation() -> None:
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "liquidity_migration" / "runtime" / "account_service_runner.py").read_text(
        encoding="utf-8"
    )
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
    assert "invocation_id = require_systemd_invocation_id(" in source


def test_demo_owner_supervises_private_execution_stream_before_admission() -> None:
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "liquidity_migration" / "runtime" / "account_service_runner.py").read_text(encoding="utf-8")
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

    wrapper = (repo / "scripts" / "runtime" / "run_account_execution_service.sh").read_text(encoding="utf-8")
    assert 'ACCOUNT_PRIVATE_WS_RECONNECT_SECONDS="${ACCOUNT_PRIVATE_WS_RECONNECT_SECONDS:-180}"' in wrapper
    assert '--private-ws-reconnect-seconds "$ACCOUNT_PRIVATE_WS_RECONNECT_SECONDS"' in wrapper
    # CONTINUOUS is retired: the cycle root is unset by default and the flag is
    # only passed when an operator configures a running sleeve again.
    assert 'CONTINUOUS_CYCLE_ROOT="${CONTINUOUS_CYCLE_ROOT:-}"' in wrapper
    assert '--continuous-cycle-root "$CONTINUOUS_CYCLE_ROOT"' in wrapper
    assert '"${continuous_cycle_args[@]}"' in wrapper


def test_protection_market_refs_skips_gapped_books_instead_of_raising() -> None:
    """A dropped L2 delta must cost one protection cycle, never the owner process:
    ``market_ref`` raises for gapped books and the runner's protection loop has no
    other handler between it and process exit.
    """

    from liquidity_migration.runtime.account_service_runner import protection_market_refs
    from liquidity_migration.account.execution_adapters import BookLevel, L2BookSnapshot

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
        def current_book_with_observed_wall_ns(self, symbol: str):
            book = {"BTCUSDT": healthy, "BUSDT": gapped, "TLMUSDT": None}[symbol]
            return book, 1_000

    refs, skipped = protection_market_refs(Recorder(), ["BTCUSDT", "BUSDT", "TLMUSDT"])

    assert set(refs) == {"BTCUSDT"}
    assert "book_sequence_gap" in skipped["BUSDT"]
    assert skipped["TLMUSDT"] == "no_book"


def _protection_book(symbol: str, local_receive_ts_ns: int):
    from liquidity_migration.account.execution_adapters import BookLevel, L2BookSnapshot

    return L2BookSnapshot(
        symbol=symbol,
        sequence=100,
        previous_sequence=99,
        exchange_ts_ns=local_receive_ts_ns - 100,
        local_receive_ts_ns=local_receive_ts_ns,
        bids=(BookLevel(9.9, 1.0),),
        asks=(BookLevel(10.1, 1.0),),
        sequence_gap=False,
        clock_offset_estimate_ns=None,
    )


def test_protection_market_refs_rejects_a_frozen_book() -> None:
    """A dropped WebSocket delivers no deltas at all, so reconstruction stays healthy
    and ``sequence_gap`` stays false while the real price walks away. A frozen book
    passes every structural check, so it must be rejected on staleness.
    """

    from liquidity_migration.runtime.account_service_runner import (
        PROTECTION_MAX_BOOK_AGE_NS,
        protection_market_refs,
    )

    fresh = _protection_book("BTCUSDT", 1_000_000_000_000)
    frozen = _protection_book("BUSDT", 1_000_000_000_000)
    observed = 1_000_000_000_000 + PROTECTION_MAX_BOOK_AGE_NS + 1

    class Recorder:
        def current_book_with_observed_wall_ns(self, symbol: str):
            if symbol == "BTCUSDT":
                return fresh, fresh.local_receive_ts_ns + 1_000_000
            return frozen, observed

    refs, skipped = protection_market_refs(Recorder(), ["BTCUSDT", "BUSDT"])

    assert set(refs) == {"BTCUSDT"}
    assert skipped["BUSDT"].startswith("stale_book:")


def test_protection_market_refs_accepts_a_book_inside_the_bound() -> None:
    from liquidity_migration.runtime.account_service_runner import (
        PROTECTION_MAX_BOOK_AGE_NS,
        protection_market_refs,
    )

    book = _protection_book("BTCUSDT", 5_000_000_000_000)

    class Recorder:
        def current_book_with_observed_wall_ns(self, symbol: str):
            return book, book.local_receive_ts_ns + PROTECTION_MAX_BOOK_AGE_NS

    refs, skipped = protection_market_refs(Recorder(), ["BTCUSDT"])
    assert set(refs) == {"BTCUSDT"}
    assert skipped == {}


def test_protection_market_refs_treats_a_backwards_clock_as_unusable() -> None:
    """Age is measured under the recorder's lock, so a negative age is a real
    wall-clock regression rather than a read/update race, and fails closed.
    """

    from liquidity_migration.runtime.account_service_runner import protection_market_refs

    book = _protection_book("BTCUSDT", 5_000_000_000_000)

    class Recorder:
        def current_book_with_observed_wall_ns(self, symbol: str):
            return book, book.local_receive_ts_ns - 1

    refs, skipped = protection_market_refs(Recorder(), ["BTCUSDT"])
    assert refs == {}
    assert skipped["BTCUSDT"] == "future_book"


def test_periodic_reconciliation_survives_transient_venue_read_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from liquidity_migration.runtime.account_service_runner import run_periodic_reconciliation

    class TimeoutReconciler:
        def reconcile_once(self):
            raise RuntimeError("Bybit get_positions failed: Server Timeout (ErrCode: 10000)")

    class HealthyFundingReconciler:
        # The cycle runs funding first, then positions; the position timeout
        # must discard this cycle's funding result too.
        def reconcile_once(self):
            return object()

    with caplog.at_level("ERROR", logger="liquidity_migration.runtime.account_service_runner"):
        report, funding_report = run_periodic_reconciliation(
            reconciler=TimeoutReconciler(),  # type: ignore[arg-type]
            funding_reconciler=HealthyFundingReconciler(),  # type: ignore[arg-type]
        )

    assert report is None
    assert funding_report is None
    assert any(
        "periodic account reconciliation failed" in record.getMessage()
        for record in caplog.records
    )
