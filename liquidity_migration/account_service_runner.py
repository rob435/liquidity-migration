"""Demo-only process runner for the single-owner account execution service."""

from __future__ import annotations

import argparse
import logging
import math
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Mapping, Protocol

from .account_execution_stream import (
    BybitAccountExecutionConsumer,
    PrivateExecutionStreamSupervisor,
)
from .account_execution_config import (
    load_demo_rules,
    load_risk_policy,
    require_registered_demo_rule_max_age_hours,
)
from .account_contracts import (
    AccountRiskSnapshot,
    MarketInputRef,
    NativeDisasterProtectionPolicy,
)
from .account_kernel import AccountExecutionKernel
from .account_market_readiness import (
    RequestedMarketWarmupGate,
    require_registered_request_market_warmup_timeout,
    run_ready_request_or_converge,
)
from .account_reconcile import (
    AccountFundingReconciliationReport,
    AccountPositionTruthMismatchError,
    AccountReconciliationReport,
    AccountReconciliationStaleError,
    BybitAccountFundingReconciler,
    BybitAccountReconciler,
)
from .account_route import derive_account_route, ensure_account_route
from .account_notifications import AccountNotificationEngine, deliver_notification_batch
from .logging_setup import ensure_default_log_handler
from .continuous_cycle_status import ContinuousCycleStatusReader
from .account_owner_health import (
    AccountOwnerHealth,
    AccountOwnerHealthStatus,
    fold_convergence_health,
    format_convergence_health,
    require_systemd_invocation_id,
    write_account_owner_health,
)
from .account_owner_lease import DemoAccountIdentity, DemoAccountMutationLease
from .account_service import AccountExecutionService, AccountIntentInbox
from .account_service_bybit import (
    BybitDemoAccountSnapshotProvider,
    CapturedBybitMarketProvider,
    VerifiedBybitDemoRulesProvider,
    require_bybit_demo_order_ownership,
)
from .bybit import (
    BybitPrivateClient,
    BybitPrivateWebSocketStream,
    api_key_allows_order_submit,
    resolve_demo_credentials,
    validate_demo_order_permission,
)
from .bybit_execution_adapter import BybitDemoExecutionAdapter
from .market_capture import (
    BybitRawPublicMarketStream,
    MarketCaptureConfig,
    SequenceAwareMarketRecorder,
    operational_market_symbols,
    recorder_callback,
    symbols_from_file,
)
from .protection_engine import AccountProtectionEngine
from .post_fill_markouts import PostFillMarkoutObserver
from .venue_protection import AccountHealthChain, BybitNativeProtectionManager

_logger = logging.getLogger(__name__)


class _PositionTruthChecker(Protocol):
    def require_recent_symbols_consistent(
        self,
        symbols: Sequence[str],
        *,
        max_age_ns: int,
    ) -> None: ...


def _run_reconciliation_cycle(
    *,
    reconciler: BybitAccountReconciler,
    funding_reconciler: BybitAccountFundingReconciler,
) -> tuple[AccountReconciliationReport, AccountFundingReconciliationReport]:
    """Refresh position truth after slower funding/journal recovery.

    Reduction admission consumes the account reconciler's direct position
    timestamp. Running it last prevents unrelated accounting work from aging a
    just-completed venue snapshot before the owner can inspect an intent.
    """

    funding_report = funding_reconciler.reconcile_once()
    position_report = reconciler.reconcile_once()
    return position_report, funding_report


def require_startup_reconciliation_safe(
    report: AccountReconciliationReport,
) -> None:
    """Allow only the native-breach condition the owner can safely reduce.

    Position drift, unknown orders, malformed venue facts, and ordinary native
    sync failures still abort startup. A definite crossed-stop breach is
    different: refusing to start would disable the only owner authorized to
    publish and execute the strict reduce-only recovery request.
    """

    if report.healthy:
        return
    recoverable = report.native_protection_breach_only
    if not recoverable:
        report.require_healthy()


def protection_market_refs(
    recorder: Any,
    symbols: Iterable[str],
) -> tuple[dict[str, MarketInputRef], dict[str, str]]:
    """Build component-protection market refs, skipping unusable books.

    ``L2BookSnapshot.market_ref`` fails closed (ValueError) for gapped,
    crossed, empty, or otherwise invalid books. The protection loop must
    skip that symbol for one cycle — the venue-native disaster stop stays
    armed independently — instead of letting the exception kill the owner
    process, which would stop execution, reconciliation, health publishing,
    and every protection at once.
    """

    refs: dict[str, MarketInputRef] = {}
    skipped: dict[str, str] = {}
    for symbol in symbols:
        book = recorder.current_book(symbol)
        if book is None:
            skipped[symbol] = "no_book"
            continue
        try:
            refs[symbol] = book.market_ref(
                input_key=f"protection:{symbol}:{book.sequence}",
                source="bybit_raw_l2",
            )
        except ValueError as exc:
            skipped[symbol] = str(exc)[:120]
    return refs, skipped


def notification_position_truth(
    *,
    reconciler: _PositionTruthChecker,
    kernel: AccountExecutionKernel,
    report: AccountReconciliationReport | None,
    max_age_ns: int,
) -> tuple[bool, str, str]:
    """Evaluate quantity truth without conflating unrelated owner health.

    Native-protection health can be blocked while the venue and local position
    quantities still agree. Telegram must report those as two distinct facts.
    """

    if report is None:
        return False, "account reconciliation has not completed", "unavailable"
    state = kernel._state_ref()
    symbols = sorted(set(report.venue_positions) | set(state.positions))
    try:
        reconciler.require_recent_symbols_consistent(
            symbols,
            max_age_ns=max_age_ns,
        )
    except AccountReconciliationStaleError as exc:
        return False, str(exc)[:240], "stale"
    except AccountPositionTruthMismatchError as exc:
        return False, str(exc)[:240], "mismatch"
    except Exception as exc:  # noqa: BLE001 - concise operator-visible cause
        return False, str(exc)[:240], "unavailable"
    return True, "", "healthy"


def append_unique_notification_health_error(
    errors: list[str],
    detail: str,
) -> None:
    """Append one health cause without repeating a changing age counter."""

    rendered = str(detail).strip()
    if not rendered:
        return
    identity = rendered.partition(": age_ns=")[0]
    if any(existing.partition(": age_ns=")[0] == identity for existing in errors):
        return
    errors.append(rendered)


def _append_health_error(existing: str, added: str, *, limit: int = 1000) -> str:
    return "; ".join(part for part in (existing.strip(), added.strip()) if part)[:limit]


def require_order_submit_permission(client: Any) -> Mapping[str, Any]:
    """Fail owner startup unless the configured key can mutate demo orders."""

    api_key_info = client.get_api_key_information()
    allowed, reason = api_key_allows_order_submit(api_key_info)
    if not allowed:
        raise RuntimeError(f"Bybit demo API key cannot submit orders: {reason}")
    return api_key_info


def publish_demo_owner_health(
    *,
    kernel: AccountExecutionKernel,
    account_root: str | Path,
    account_id: str,
    risk_snapshot: AccountRiskSnapshot,
    status: AccountOwnerHealthStatus | str,
    observed_ts_ns: int,
    loop_sequence: int,
    requested_symbols_ready: bool,
    invocation_id: str,
    last_batch_id: str = "",
    detail: str = "",
) -> AccountOwnerHealth:
    """Publish demo-owner health bound to canonical state and wallet capital."""

    state = kernel._state_ref()
    health = AccountOwnerHealth(
        owner="account_execution",
        environment="demo",
        account_id=account_id,
        status=status,
        observed_ts_ns=observed_ts_ns,
        loop_sequence=loop_sequence,
        journal_sequence=state.events_applied,
        journal_state_hash=state.rolling_state_hash,
        equity_usdt=risk_snapshot.equity_usdt,
        available_margin_usdt=risk_snapshot.available_margin_usdt,
        requested_symbols_ready=requested_symbols_ready,
        invocation_id=invocation_id,
        last_batch_id=last_batch_id[:500],
        detail=detail[:1000],
    )
    write_account_owner_health(account_root, health)
    return health


def owner_health_publish_decision(
    *,
    receipt_completed: bool,
    health_signature: tuple[str, str, bool],
    last_health_signature: tuple[str, str, bool] | None,
    journal_signature: tuple[int, str],
    last_health_journal_signature: tuple[int, str] | None,
    now_monotonic: float,
    last_capital_refresh_monotonic: float,
    health_interval_seconds: float,
) -> tuple[bool, bool]:
    """Decide whether to publish health and whether wallet capital is due.

    Reconciliation and private execution can advance the immutable journal more
    often than the ordinary health interval. Exact-head consumers must not have
    to win a timing race against journal traffic, so any new head
    republishes health. Journal-only refreshes reuse the last wallet snapshot;
    only a completed request, status change, or elapsed interval spends another
    wallet REST call.
    """

    if health_interval_seconds <= 0.0:
        raise ValueError("health_interval_seconds must be positive")
    refresh_capital = (
        receipt_completed
        or health_signature != last_health_signature
        or now_monotonic - last_capital_refresh_monotonic >= health_interval_seconds
    )
    publish = refresh_capital or journal_signature != last_health_journal_signature
    return publish, refresh_capital


def main(argv: list[str] | None = None) -> int:
    ensure_default_log_handler()
    parser = argparse.ArgumentParser(description="Run the demo-only account execution owner")
    parser.add_argument("--account-root", required=True)
    parser.add_argument("--inbox-root", required=True)
    parser.add_argument("--capture-root", required=True)
    parser.add_argument(
        "--persist-raw-market",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Persist every raw L2/trade frame for research replay. Live L2 "
            "reconstruction and exact decision-book persistence remain enabled "
            "when this is disabled."
        ),
    )
    parser.add_argument("--symbols-file", required=True)
    parser.add_argument("--demo-rules-file", required=True)
    parser.add_argument("--risk-policy-file", required=True)
    parser.add_argument(
        "--max-demo-rule-age-hours",
        type=float,
        default=168.0,
        help="Fail startup when empirical demo order-rule receipts are older than this.",
    )
    parser.add_argument(
        "--disaster-stop-fraction",
        required=True,
        type=float,
        help="Explicit full-position native stop distance used when components have no stop.",
    )
    parser.add_argument("--account-id", default="bybit-demo-unified")
    parser.add_argument("--reconcile-seconds", type=float, default=2.0)
    parser.add_argument("--symbol-refresh-seconds", type=float, default=5.0)
    parser.add_argument(
        "--request-market-warmup-timeout-seconds",
        type=float,
        default=30.0,
        help="Latch owner health blocked if the durable queue head lacks healthy fresh L2.",
    )
    parser.add_argument("--idle-seconds", type=float, default=0.1)
    parser.add_argument("--confirm-demo-orders", action="store_true")
    parser.add_argument("--telegram", action="store_true")
    parser.add_argument("--notification-state", default="")
    parser.add_argument("--notification-poll-seconds", type=float, default=1.0)
    parser.add_argument(
        "--continuous-cycle-root",
        default="",
        help="Read-only CONTINUOUS cycle status root shown in account notifications.",
    )
    parser.add_argument(
        "--continuous-cycle-max-age-minutes",
        type=float,
        default=15.0,
        help="Mark CONTINUOUS notification telemetry stale beyond this age.",
    )
    parser.add_argument("--health-interval-seconds", type=float, default=5.0)
    parser.add_argument(
        "--private-ws-reconnect-seconds",
        type=float,
        default=180.0,
        help=(
            "Continuously-disconnected bound and reconnect-attempt cooldown for the private execution/order websocket."
        ),
    )
    args = parser.parse_args(argv)
    if args.health_interval_seconds <= 0.0:
        parser.error("--health-interval-seconds must be positive")
    if not math.isfinite(args.continuous_cycle_max_age_minutes) or args.continuous_cycle_max_age_minutes <= 0.0:
        parser.error("--continuous-cycle-max-age-minutes must be positive and finite")
    if not math.isfinite(args.private_ws_reconnect_seconds) or args.private_ws_reconnect_seconds <= 0.0:
        parser.error("--private-ws-reconnect-seconds must be positive and finite")
    try:
        args.max_demo_rule_age_hours = require_registered_demo_rule_max_age_hours(args.max_demo_rule_age_hours)
        args.request_market_warmup_timeout_seconds = require_registered_request_market_warmup_timeout(
            args.request_market_warmup_timeout_seconds
        )
    except ValueError as exc:
        parser.error(str(exc))
    invocation_id = require_systemd_invocation_id()

    requested_route = derive_account_route(
        account_id=args.account_id,
        environment="demo",
        account_root=args.account_root,
        inbox_root=args.inbox_root,
    )

    validate_demo_order_permission(confirm_demo_orders=args.confirm_demo_orders)
    api_key, api_secret = resolve_demo_credentials()
    if not api_key or not api_secret:
        raise RuntimeError("BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET are required")
    credential_client = BybitPrivateClient(
        category="linear",
        testnet=False,
        demo=True,
        api_key=api_key,
        api_secret=api_secret,
    )
    api_key_info = require_order_submit_permission(credential_client)
    demo_identity = DemoAccountIdentity.from_api_key_info(
        api_key=api_key,
        api_key_info=api_key_info,
    )
    owner_lease = DemoAccountMutationLease(demo_identity)
    owner_lease.acquire()
    try:
        route = ensure_account_route(
            account_id=requested_route.account_id,
            environment=requested_route.environment,
            account_root=requested_route.account_root,
            inbox_root=requested_route.inbox_root,
        )
    except BaseException:
        owner_lease.close()
        raise
    rules = load_demo_rules(
        args.demo_rules_file,
        max_age_seconds=args.max_demo_rule_age_hours * 3600.0,
    )
    policy = load_risk_policy(args.risk_policy_file)
    symbols_path = Path(args.symbols_file).expanduser()
    symbols = symbols_from_file(symbols_path)
    if not symbols:
        raise RuntimeError("symbols file is empty; list candidate/held symbols before starting capture")
    missing_rules = sorted(symbols - set(rules))
    if missing_rules:
        raise RuntimeError(f"symbols lack verified demo rules: {', '.join(missing_rules)}")
    live_symbols = operational_market_symbols(symbols)

    private_client = BybitPrivateClient(
        category="linear",
        testnet=False,
        demo=True,
        api_key=api_key,
        api_secret=api_secret,
        mutation_lease=owner_lease,
    )
    kernel = AccountExecutionKernel(route.account_path, account_id=route.account_id)
    native_protection_policy = NativeDisasterProtectionPolicy(
        fallback_stop_fraction=args.disaster_stop_fraction,
    )
    native_protection = BybitNativeProtectionManager(
        kernel=kernel,
        client=private_client,
        instrument_rules=rules,
        fallback_stop_fraction=native_protection_policy.fallback_stop_fraction,
    )
    # Prove the venue has no unowned regular or conditional order before any
    # stream starts or any strategy request can be claimed. An empty/new journal
    # therefore requires a completely empty venue order book.
    require_bybit_demo_order_ownership(
        client=private_client,
        kernel=kernel,
        native_order_verifier=native_protection.is_verified_native_order,
    )

    def build_private_stream() -> BybitPrivateWebSocketStream:
        return BybitPrivateWebSocketStream(
            category="linear",
            testnet=False,
            demo=True,
            api_key=api_key,
            api_secret=api_secret,
        )

    private_stream = build_private_stream()
    recorder = SequenceAwareMarketRecorder(
        args.capture_root,
        config=MarketCaptureConfig(
            depth=50,
            persist_raw_market=args.persist_raw_market,
        ),
        owner_invocation_id=invocation_id,
    )
    public_stream = BybitRawPublicMarketStream(
        testnet=False,
        depth=50,
        include_public_trades=args.persist_raw_market,
        on_message=recorder_callback(recorder),
    )
    recorder.set_required_symbols(live_symbols)
    public_stream.start(live_symbols)
    markout_observer = PostFillMarkoutObserver(
        kernel=kernel,
        recorder=recorder,
    )
    execution_consumer = BybitAccountExecutionConsumer(
        kernel=kernel,
        private_stream=private_stream,
        native_protection_manager=native_protection,
        fill_observer=markout_observer.notify,
    )
    execution_consumer.start()
    private_stream_supervisor = PrivateExecutionStreamSupervisor(
        consumer=execution_consumer,
        stream_factory=build_private_stream,
        reconnect_after_seconds=args.private_ws_reconnect_seconds,
        reconnect_cooldown_seconds=args.private_ws_reconnect_seconds,
    )
    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=private_client,
        instrument_rules=rules,
        native_protection_manager=native_protection,
        fill_observer=markout_observer.notify,
    )
    # Bootstrap venue truth before the service can claim any request. Existing
    # venue exposure with an empty kernel is a hard mismatch, never auto-adopted.
    bootstrap_reconciliation = reconciler.reconcile_once()
    require_startup_reconciliation_safe(bootstrap_reconciliation)
    funding_reconciler = BybitAccountFundingReconciler(
        kernel=kernel,
        client=private_client,
    )
    startup_reconciliation, startup_funding_reconciliation = _run_reconciliation_cycle(
        reconciler=reconciler,
        funding_reconciler=funding_reconciler,
    )
    require_startup_reconciliation_safe(startup_reconciliation)
    startup_funding_reconciliation.require_healthy()
    if not native_protection.breaches():
        native_protection.sync_symbols(
            [symbol for symbol, position in kernel._state_ref().positions.items() if position.signed_qty != 0.0]
        )
    health_chain = AccountHealthChain(
        (
            private_stream_supervisor,
            reconciler,
            funding_reconciler,
            native_protection,
        )
    )
    snapshot_provider = BybitDemoAccountSnapshotProvider(private_client)
    service = AccountExecutionService(
        route=route,
        kernel=kernel,
        market_provider=CapturedBybitMarketProvider(recorder),
        snapshot_provider=snapshot_provider,
        rules_provider=VerifiedBybitDemoRulesProvider(rules),
        risk_policy=policy,
        execution_adapter=BybitDemoExecutionAdapter(private_client),
        native_protection_policy=native_protection_policy,
        required_rules_environment="demo",
        health_provider=health_chain,
        position_truth_provider=reconciler,
        max_health_age_ns=max(int(args.reconcile_seconds * 2 * 1_000_000_000), 1),
    )
    inbox = AccountIntentInbox(route)
    market_warmup_gate = RequestedMarketWarmupGate(
        timeout_seconds=args.request_market_warmup_timeout_seconds,
    )
    protection_engine = AccountProtectionEngine(
        kernel=kernel,
        inbox=inbox,
        instrument_rules=rules,
    )
    notifier = (
        AccountNotificationEngine(
            kernel=kernel,
            state_path=(args.notification_state or str(route.account_path / "account_notifications.json")),
        )
        if args.telegram
        else None
    )
    continuous_status_reader = (
        ContinuousCycleStatusReader(
            args.continuous_cycle_root,
            environment="demo",
            max_age_minutes=args.continuous_cycle_max_age_minutes,
        )
        if notifier is not None and str(args.continuous_cycle_root).strip()
        else None
    )
    recovered = inbox.recover_processing()
    if recovered:
        _logger.warning("recovered %d account intent(s) left processing by a prior crash", recovered)

    last_reconcile = time.monotonic()
    last_symbol_refresh = float("-inf")
    last_notification_poll = 0.0
    last_capital_refresh = float("-inf")
    last_health_signature: tuple[str, str, bool] | None = None
    last_health_journal_signature: tuple[int, str] | None = None
    last_batch_id = ""
    loop_sequence = 0
    requested_symbols_ready = True
    symbol_health_detail = ""
    last_request_failure_signature = ""
    latest_reconcile_report = reconciler.last_report
    last_capital_snapshot = snapshot_provider.current(batch_id="owner-health/bootstrap")
    try:
        while True:
            now = time.monotonic()
            loop_sequence += 1
            markout_observer.drain()
            if now - last_reconcile >= max(args.reconcile_seconds, 0.1):
                report, funding_report = _run_reconciliation_cycle(
                    reconciler=reconciler,
                    funding_reconciler=funding_reconciler,
                )
                latest_reconcile_report = report
                if not report.healthy:
                    _logger.error("account reconcile blocked new intents: %s", "; ".join(report.mismatches))
                if not funding_report.healthy:
                    _logger.error("account funding reconcile blocked new intents")
                last_reconcile = time.monotonic()
            private_stream_status = private_stream_supervisor.check(now_monotonic=now)
            if now - last_symbol_refresh >= max(args.symbol_refresh_seconds, 0.25):
                current_state = kernel._state_ref()
                component_target_symbols = {
                    str(target.get("symbol") or "").upper()
                    for target in current_state.component_targets.values()
                    if target.get("symbol") and float(target.get("signed_qty") or 0.0) != 0.0
                }
                nonflat_symbols = {
                    symbol for symbol, position in current_state.positions.items() if position.signed_qty != 0.0
                }
                desired = operational_market_symbols(
                    symbols,
                    queued=inbox.requested_symbols(),
                    nonflat=nonflat_symbols,
                    working=current_state.working_symbols(tolerance=policy.quantity_tolerance),
                    component_targets=component_target_symbols,
                    convergence=(item.symbol for item in service.convergence_report().items),
                    markouts=recorder.pending_post_fill_symbols(),
                )
                missing_rules = sorted(desired - set(rules))
                if missing_rules:
                    _logger.error(
                        "requested symbols lack verified demo rules: %s",
                        missing_rules,
                    )
                # Warm every queued symbol in parallel while the strict
                # queue-head gate below preserves the registered 30s timeout.
                recorder.set_required_symbols(desired)
                public_stream.update_symbols(desired)
                last_symbol_refresh = now
            market_readiness = market_warmup_gate.evaluate(
                inbox=inbox,
                recorder=recorder,
                verified_rule_symbols=set(rules),
                now_monotonic=now,
                max_market_age_ns=service.max_market_age_ns,
            )
            requested_symbols_ready = market_readiness.ready
            symbol_health_detail = market_readiness.detail
            protection_markets, protection_skipped = protection_market_refs(
                recorder,
                {
                    str(target.get("symbol") or "").upper()
                    for target in kernel._state_ref().component_targets.values()
                    if target.get("symbol") and float(target.get("signed_qty") or 0.0) != 0.0
                },
            )
            if protection_skipped:
                _logger.warning(
                    "component protection skipped unusable books this cycle: %s",
                    "; ".join(f"{symbol}={reason}" for symbol, reason in sorted(protection_skipped.items())),
                )
            protection_evaluation_error = ""
            try:
                protection_engine.evaluate_native_breaches(native_protection.breaches())
            except Exception as exc:  # noqa: BLE001 - safety publication blocks health
                _logger.exception("native-breach software-flat publication failed")
                protection_evaluation_error = _append_health_error(
                    protection_evaluation_error,
                    f"native-breach software-flat publication failed: {type(exc).__name__}: {exc}",
                )
            if protection_markets:
                try:
                    protection_engine.evaluate(protection_markets)
                except Exception as exc:  # noqa: BLE001 - protection failure blocks health, never the owner
                    _logger.exception("component protection evaluation failed")
                    protection_evaluation_error = _append_health_error(
                        protection_evaluation_error,
                        f"component protection evaluation failed: {type(exc).__name__}: {exc}",
                    )
            reconcile_healthy = bool(latest_reconcile_report is not None and latest_reconcile_report.healthy)
            health_status = (
                AccountOwnerHealthStatus.HEALTHY
                if (
                    requested_symbols_ready
                    and reconcile_healthy
                    and private_stream_status is True
                    and not protection_evaluation_error
                )
                else AccountOwnerHealthStatus.BLOCKED
            )
            health_details = [
                symbol_health_detail if not requested_symbols_ready else "",
                protection_evaluation_error,
            ]
            if not reconcile_healthy and latest_reconcile_report is not None:
                health_details.append(
                    "account reconciliation mismatch: " + "; ".join(latest_reconcile_report.mismatches)
                )
            if private_stream_status is not True:
                health_details.append(private_stream_supervisor.health_detail)
            health_detail = "; ".join(detail for detail in health_details if detail)[:1000]
            receipt = None
            try:
                receipt = run_ready_request_or_converge(
                    service=service,
                    inbox=inbox,
                    readiness=market_readiness,
                )
                if receipt is not None:
                    last_batch_id = receipt.batch_id
                    last_request_failure_signature = ""
                    try:
                        native_protection.sync_symbols(
                            [
                                symbol
                                for symbol, position in kernel._state_ref().positions.items()
                                if position.signed_qty != 0.0
                            ]
                        )
                    except Exception as exc:  # noqa: BLE001 - receipt is already durable
                        # Never misreport a completed request as returned to
                        # pending merely because post-request native protection
                        # is still converging (notably while a breach-flat fill
                        # is in flight). Reconciliation owns the next proof.
                        _logger.error(
                            "account request completed but native protection remains unhealthy: %s: %s",
                            type(exc).__name__,
                            exc,
                        )
                        health_status = AccountOwnerHealthStatus.BLOCKED
                        health_detail = (f"post-request native protection unhealthy: {type(exc).__name__}: {exc}")[
                            :1000
                        ]
                    _logger.info(
                        "account request complete batch=%s accepted=%s commands=%d state=%s",
                        receipt.batch_id,
                        receipt.accepted,
                        len(receipt.command_ids),
                        receipt.final_state_hash[:12],
                    )
            except Exception as exc:  # noqa: BLE001 - request was released for retry
                # A persistently blocked request retries every few seconds; one
                # full traceback per distinct cause keeps the journal usable
                # while each blocked pass still leaves a one-line record.
                failure_signature = f"{type(exc).__name__}: {exc}"[:500]
                if failure_signature != last_request_failure_signature:
                    last_request_failure_signature = failure_signature
                    _logger.exception("account request failed and was returned to pending")
                else:
                    _logger.error(
                        "account request failed again (traceback suppressed, unchanged cause): %s",
                        failure_signature,
                    )
                health_status = AccountOwnerHealthStatus.BLOCKED
                health_detail = f"{type(exc).__name__}: {exc}"[:1000]
                time.sleep(max(min(args.reconcile_seconds, 5.0), 0.5))
            try:
                convergence_report = service.convergence_report()
            except Exception as exc:  # noqa: BLE001 - corrupt convergence state blocks health
                health_status = AccountOwnerHealthStatus.BLOCKED
                convergence_detail = f"convergence health failed: {type(exc).__name__}: {exc}"
            else:
                health_status, health_detail = fold_convergence_health(
                    convergence_report,
                    status=health_status,
                    detail=health_detail,
                )
                convergence_detail = ""
            if convergence_detail:
                health_detail = "; ".join(part for part in (health_detail, convergence_detail) if part)[:1000]
            health_now = time.monotonic()
            health_signature = (
                health_status.value,
                health_detail[:1000],
                requested_symbols_ready,
            )
            health_state = kernel._state_ref()
            health_journal_signature = (
                health_state.events_applied,
                health_state.rolling_state_hash,
            )
            publish_health, refresh_capital = owner_health_publish_decision(
                receipt_completed=receipt is not None,
                health_signature=health_signature,
                last_health_signature=last_health_signature,
                journal_signature=health_journal_signature,
                last_health_journal_signature=last_health_journal_signature,
                now_monotonic=health_now,
                last_capital_refresh_monotonic=last_capital_refresh,
                health_interval_seconds=args.health_interval_seconds,
            )
            if publish_health:
                if refresh_capital:
                    try:
                        last_capital_snapshot = snapshot_provider.current(batch_id=f"owner-health/{loop_sequence}")
                    except Exception as exc:  # noqa: BLE001 - preserve last capital, mark blocked
                        health_status = AccountOwnerHealthStatus.BLOCKED
                        health_detail = f"wallet snapshot failed: {type(exc).__name__}: {exc}"[:1000]
                        health_signature = (
                            health_status.value,
                            health_detail,
                            requested_symbols_ready,
                        )
                published_health = publish_demo_owner_health(
                    kernel=kernel,
                    account_root=route.account_path,
                    account_id=route.account_id,
                    risk_snapshot=last_capital_snapshot,
                    status=health_status,
                    observed_ts_ns=time.time_ns(),
                    loop_sequence=loop_sequence,
                    requested_symbols_ready=requested_symbols_ready,
                    invocation_id=invocation_id,
                    last_batch_id=last_batch_id,
                    detail=health_detail,
                )
                if refresh_capital:
                    last_capital_refresh = health_now
                last_health_signature = health_signature
                last_health_journal_signature = (
                    published_health.journal_sequence,
                    published_health.journal_state_hash,
                )
            if notifier is not None and now - last_notification_poll >= max(args.notification_poll_seconds, 0.25):
                notification_now_ns = time.time_ns()
                midpoint_by_symbol: dict[str, float] = {}
                unavailable_midpoint_symbols: list[str] = []
                for symbol, position in kernel._state_ref().positions.items():
                    if position.signed_qty == 0.0:
                        continue
                    book, notification_wall_ns = recorder.current_book_with_observed_wall_ns(symbol)
                    book_age_ns = notification_wall_ns - book.local_receive_ts_ns if book is not None else -1
                    if book is None or book.sequence_gap or book_age_ns < 0 or book_age_ns > service.max_market_age_ns:
                        unavailable_midpoint_symbols.append(symbol)
                        continue
                    try:
                        midpoint_by_symbol[symbol] = book.market_ref(
                            input_key=f"notification:{symbol}:{book.sequence}",
                            source="bybit_raw_l2",
                        ).reference_price
                    except ValueError:
                        unavailable_midpoint_symbols.append(symbol)
                notification_health_errors: list[str] = []
                if unavailable_midpoint_symbols:
                    notification_health_errors.append(
                        "fresh L2 midpoint unavailable: " + ", ".join(sorted(unavailable_midpoint_symbols))
                    )
                reconcile_health_max_age_ns = max(
                    int(args.reconcile_seconds * 2 * 1_000_000_000),
                    1,
                )
                try:
                    health_chain.require_recent_healthy(max_age_ns=reconcile_health_max_age_ns)
                except Exception as exc:  # noqa: BLE001 - rendered hourly, not spammed per cycle
                    notification_health_errors.append(str(exc)[:240])
                # Position truth is narrower than general owner health. A
                # missing native stop should block health, but must not make a
                # matching venue/local quantity report claim that the two
                # disagree. Derive this flag from reconciliation alone.
                (
                    position_truth_healthy,
                    position_truth_error,
                    position_truth_status,
                ) = notification_position_truth(
                    reconciler=reconciler,
                    kernel=kernel,
                    report=latest_reconcile_report,
                    max_age_ns=reconcile_health_max_age_ns,
                )
                if position_truth_error:
                    append_unique_notification_health_error(
                        notification_health_errors,
                        position_truth_error,
                    )
                try:
                    convergence_report = service.convergence_report()
                    convergence_detail = format_convergence_health(convergence_report)
                except Exception as exc:  # noqa: BLE001 - rendered hourly, not spammed per cycle
                    notification_health_errors.append(f"convergence health failed: {type(exc).__name__}: {exc}"[:240])
                else:
                    if not convergence_report.healthy:
                        notification_health_errors.append(convergence_detail[:240])
                if notification_health_errors:
                    health = "BLOCKED · " + " · ".join(notification_health_errors)
                elif convergence_detail:
                    health = "healthy · " + convergence_detail
                else:
                    health = "healthy"
                notification = notifier.prepare(
                    midpoint_by_symbol=midpoint_by_symbol,
                    health=health,
                    venue_positions=(
                        latest_reconcile_report.venue_positions if latest_reconcile_report is not None else {}
                    ),
                    position_truth_healthy=position_truth_healthy,
                    position_truth_status=position_truth_status,
                    continuous_status=(
                        continuous_status_reader.render(now_ns=notification_now_ns)
                        if continuous_status_reader is not None
                        else "CONTINUOUS BTC gate: unavailable · cycle root not configured"
                    ),
                    now_ns=notification_now_ns,
                )
                deliver_notification_batch(
                    notifier,
                    notification,
                    context="account",
                    logger=_logger,
                )
                last_notification_poll = now
            time.sleep(max(args.idle_seconds, 0.01))
    except KeyboardInterrupt:
        return 0
    finally:
        execution_consumer.close()
        public_stream.close()
        recorder.close()
        owner_lease.close()


if __name__ == "__main__":
    raise SystemExit(main())
