"""Run the paper integration owner with an explicitly uncalibrated execution twin."""

from __future__ import annotations

import argparse
import logging
import math
import os
import time
from pathlib import Path
from typing import Mapping

from .env_flags import explicitly_false_or_unset
from .account_execution_config import (
    load_demo_rules,
    load_risk_policy,
    require_registered_demo_rule_max_age_hours,
)
from .account_notifications import AccountNotificationEngine, deliver_notification_batch
from .logging_setup import ensure_default_log_handler
from .account_contracts import AccountRiskSnapshot, OrderCommand
from .account_kernel import AccountExecutionKernel
from .passive_execution import (
    PassivePaperExecutionAdapter,
    recover_orphaned_working_orders,
)
from .account_market_readiness import (
    RequestedMarketWarmupGate,
    require_registered_request_market_warmup_timeout,
    run_ready_request_or_converge,
)
from .account_owner_health import (
    AccountOwnerHealth,
    AccountOwnerHealthStatus,
    fold_convergence_health,
    require_systemd_invocation_id,
    write_account_owner_health,
)
from .account_owner_lease import AccountOwnerLease
from .account_route import derive_account_route, ensure_account_route
from .account_service import (
    DEFAULT_MAX_MARKET_AGE_NS,
    AccountExecutionService,
    AccountIntentInbox,
)
from .account_service_bybit import (
    CapturedBybitMarketProvider,
    VerifiedBybitDemoRulesProvider,
)
from .continuous_cycle_status import ContinuousCycleStatusReader
from .deterministic_runtime import Clock, SystemClock
from .execution_adapters import (
    INTEGRATION_ONLY_EXECUTION_MODEL_SCOPE,
    ExecutionTwinConfig,
    LatencyProfile,
    MarketOrderExecutionTwin,
)
from .market_capture import (
    BybitRawPublicMarketStream,
    MarketCaptureConfig,
    SequenceAwareMarketRecorder,
    operational_market_symbols,
    recorder_callback,
    symbols_from_file,
)
from .protection_engine import AccountProtectionEngine

_logger = logging.getLogger(__name__)

PAPER_EXECUTION_BOOK_DEPTH = 50
PAPER_INTEGRATION_EXECUTION_TWIN_CONFIG = ExecutionTwinConfig(
    fee_bps=5.5,
    latency=LatencyProfile(
        decision_to_socket_ns=0,
        order_entry_ns=0,
        order_response_ns=0,
        fill_spacing_ns=0,
        submit_to_first_fill_ns=0,
        fill_response_ns=0,
    ),
    max_decision_age_ns=250_000_000,
    allow_partial_fills=True,
    fill_partition_policy="book_level",
    immutable_replay_book=True,
    residual_adverse_slippage_bps=2.0,
    visible_book_depth=PAPER_EXECUTION_BOOK_DEPTH,
    model_scope=INTEGRATION_ONLY_EXECUTION_MODEL_SCOPE,
)

_PRIVATE_EXCHANGE_ENVIRONMENT_KEYS = (
    "BYBIT_DEMO_API_KEY",
    "BYBIT_DEMO_API_SECRET",
    "BYBIT_REAL_API_KEY",
    "BYBIT_REAL_API_SECRET",
)


def require_paper_runtime_isolation(
    environment: Mapping[str, str] = os.environ,
) -> None:
    present = [key for key in _PRIVATE_EXCHANGE_ENVIRONMENT_KEYS if environment.get(key)]
    if present:
        raise RuntimeError(
            "paper owner received private exchange credentials: " + ", ".join(present)
        )
    if not explicitly_false_or_unset(environment.get("REAL_MONEY")):
        raise RuntimeError("paper owner requires REAL_MONEY=false or unset")


def _integration_only_health_detail(detail: str) -> str:
    prefix = f"execution_model_scope={INTEGRATION_ONLY_EXECUTION_MODEL_SCOPE}"
    return "; ".join(part for part in (prefix, detail) if part)[:1000]


class FixedCapitalSnapshotProvider:
    """Fresh paper margin snapshots with an explicit fixed capital base."""

    def __init__(self, equity_usdt: float, *, clock: Clock | None = None) -> None:
        if not math.isfinite(equity_usdt) or equity_usdt <= 0.0:
            raise ValueError("paper equity must be finite and positive")
        self.equity_usdt = float(equity_usdt)
        self.clock = clock or SystemClock()

    def current(self, *, batch_id: str) -> AccountRiskSnapshot:
        observed = self.clock.wall_time_ns()
        return AccountRiskSnapshot(
            equity_usdt=self.equity_usdt,
            available_margin_usdt=self.equity_usdt,
            snapshot_key=f"paper-fixed:{self.equity_usdt:g}:{batch_id}",
            snapshot_ts_ns=observed,
        )


def publish_paper_owner_health(
    *,
    kernel: AccountExecutionKernel,
    account_root: str | Path,
    account_id: str,
    equity_usdt: float,
    status: AccountOwnerHealthStatus | str,
    observed_ts_ns: int,
    loop_sequence: int,
    requested_symbols_ready: bool,
    invocation_id: str,
    last_batch_id: str = "",
    detail: str = "",
) -> AccountOwnerHealth:
    """Publish paper-loop health bound to the current canonical account state."""

    state = kernel._state_ref()
    health = AccountOwnerHealth(
        owner="account_execution",
        environment="paper",
        account_id=account_id,
        status=status,
        observed_ts_ns=observed_ts_ns,
        loop_sequence=loop_sequence,
        journal_sequence=state.events_applied,
        journal_state_hash=state.rolling_state_hash,
        equity_usdt=equity_usdt,
        available_margin_usdt=equity_usdt,
        requested_symbols_ready=requested_symbols_ready,
        invocation_id=invocation_id,
        last_batch_id=last_batch_id[:500],
        detail=_integration_only_health_detail(detail),
    )
    write_account_owner_health(account_root, health)
    return health


def main(argv: list[str] | None = None) -> int:
    ensure_default_log_handler()
    parser = argparse.ArgumentParser(description=__doc__)
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
    parser.add_argument("--account-id", default="bybit-paper-unified")
    parser.add_argument("--equity-usdt", type=float, required=True)
    parser.add_argument("--max-demo-rule-age-hours", type=float, default=168.0)
    parser.add_argument("--symbol-refresh-seconds", type=float, default=5.0)
    parser.add_argument(
        "--request-market-warmup-timeout-seconds",
        type=float,
        default=30.0,
        help="Latch owner health blocked if the durable queue head lacks healthy fresh L2.",
    )
    parser.add_argument("--health-interval-seconds", type=float, default=5.0)
    parser.add_argument("--idle-seconds", type=float, default=0.1)
    parser.add_argument("--telegram", action="store_true")
    parser.add_argument("--notification-state", default="")
    parser.add_argument("--notification-poll-seconds", type=float, default=1.0)
    parser.add_argument(
        "--continuous-cycle-root",
        default="",
        help="Read-only paper CONTINUOUS cycle status root shown in paper notifications.",
    )
    parser.add_argument(
        "--continuous-cycle-max-age-minutes",
        type=float,
        default=15.0,
        help="Mark CONTINUOUS notification telemetry stale beyond this age.",
    )
    args = parser.parse_args(argv)
    if args.health_interval_seconds <= 0.0:
        parser.error("--health-interval-seconds must be positive")
    try:
        args.max_demo_rule_age_hours = require_registered_demo_rule_max_age_hours(
            args.max_demo_rule_age_hours
        )
        args.request_market_warmup_timeout_seconds = (
            require_registered_request_market_warmup_timeout(
                args.request_market_warmup_timeout_seconds
            )
        )
    except ValueError as exc:
        parser.error(str(exc))
    require_paper_runtime_isolation()
    invocation_id = require_systemd_invocation_id()

    requested_route = derive_account_route(
        account_id=args.account_id,
        environment="paper",
        account_root=args.account_root,
        inbox_root=args.inbox_root,
    )

    lease = AccountOwnerLease(
        requested_route.account_path / "account_execution_owner.lock",
        allow_private_parent_mount_boundary=True,
    )
    lease.acquire()
    try:
        route = ensure_account_route(
            account_id=requested_route.account_id,
            environment=requested_route.environment,
            account_root=requested_route.account_root,
            inbox_root=requested_route.inbox_root,
        )
    except BaseException:
        lease.close()
        raise
    rules = load_demo_rules(
        args.demo_rules_file,
        max_age_seconds=args.max_demo_rule_age_hours * 3600.0,
    )
    policy = load_risk_policy(args.risk_policy_file)
    symbols_path = Path(args.symbols_file).expanduser()
    symbols = symbols_from_file(symbols_path)
    if not symbols:
        raise RuntimeError("paper symbols file is empty")
    missing = sorted(symbols - set(rules))
    if missing:
        raise RuntimeError(f"paper symbols lack demo-verified rules: {', '.join(missing)}")
    live_symbols = operational_market_symbols(symbols)

    recorder = SequenceAwareMarketRecorder(
        args.capture_root,
        config=MarketCaptureConfig(
            depth=PAPER_EXECUTION_BOOK_DEPTH,
            persist_raw_market=args.persist_raw_market,
        ),
        owner_invocation_id=invocation_id,
    )
    public_stream = BybitRawPublicMarketStream(
        testnet=False,
        depth=PAPER_EXECUTION_BOOK_DEPTH,
        include_public_trades=args.persist_raw_market,
        on_message=recorder_callback(recorder),
    )
    recorder.set_required_symbols(live_symbols)
    public_stream.start(live_symbols)
    kernel = AccountExecutionKernel(route.account_path, account_id=route.account_id)
    runtime_clock = SystemClock()
    market_provider = CapturedBybitMarketProvider(recorder)
    twin = MarketOrderExecutionTwin(
        books={},
        instrument_rules=rules,
        config=PAPER_INTEGRATION_EXECUTION_TWIN_CONFIG,
        name=f"paper_{INTEGRATION_ONLY_EXECUTION_MODEL_SCOPE}",
        id_seed=f"{route.account_id}:paper-execution",
    )

    component_identity_cache: dict[tuple[str, str], tuple[str, str]] = {}

    def _component_for_command(command: OrderCommand) -> tuple[str, str]:
        """Resolve the registered A/B identity (trade id, sleeve) for a command.

        Read from the REDUCED state, not the raw journal. ``journal.events()``
        returns a full copy and the scan was O(journal) per order submission,
        inside the 250 ms decision window, growing for the epoch's whole life
        (2026-07-27 audit L13). ``target_proposals`` is the reducer's own index
        of exactly these rows, and the answer is immutable per (batch, symbol),
        so it is memoized.
        """

        key = (command.batch_id, command.symbol.upper())
        cached = component_identity_cache.get(key)
        if cached is not None:
            return cached
        identity = ("", "")
        for target_key, payload in kernel._state_ref().target_proposals.items():
            if not target_key.startswith(f"{command.batch_id}:"):
                continue
            if str(payload.get("symbol") or "").upper() != key[1]:
                continue
            identity = (
                str(payload.get("component_id") or ""),
                str(payload.get("sleeve") or ""),
            )
            break
        component_identity_cache[key] = identity
        return identity

    execution_adapter = PassivePaperExecutionAdapter(
        market_provider=market_provider,
        twin=twin,
        component_resolver=_component_for_command,
        clock=runtime_clock,
        reduce_only_max_decision_age_ns=DEFAULT_MAX_MARKET_AGE_NS,
    )
    service = AccountExecutionService(
        route=route,
        kernel=kernel,
        market_provider=market_provider,
        snapshot_provider=FixedCapitalSnapshotProvider(args.equity_usdt, clock=runtime_clock),
        rules_provider=VerifiedBybitDemoRulesProvider(rules),
        risk_policy=policy,
        execution_adapter=execution_adapter,
        required_rules_environment="demo",
        clock=runtime_clock,
        max_market_age_ns=DEFAULT_MAX_MARKET_AGE_NS,
    )
    # Passive pending state is in-memory: after a restart, terminal-cancel any
    # working order left by a prior process so nothing stays phantom-working;
    # the convergence loop re-plans the remainder deterministically.
    restart_recovery = recover_orphaned_working_orders(
        kernel._state_ref(),
        now_ns=runtime_clock.wall_time_ns(),
        tolerance=policy.quantity_tolerance,
    )
    if restart_recovery:
        service.runtime.driver.ingest(restart_recovery)
        _logger.warning(
            "cancelled %d orphaned paper working order(s) after restart",
            len(restart_recovery),
        )
    inbox = AccountIntentInbox(route)
    market_warmup_gate = RequestedMarketWarmupGate(
        timeout_seconds=args.request_market_warmup_timeout_seconds,
    )
    protection = AccountProtectionEngine(
        kernel=kernel,
        inbox=inbox,
        instrument_rules=rules,
    )
    notifier = (
        AccountNotificationEngine(
            kernel=kernel,
            state_path=(
                args.notification_state or str(route.account_path / "account_notifications.json")
            ),
            heading="Bybit paper",
            channel_label="🧪 PAPER · integration-only twin",
        )
        if args.telegram
        else None
    )
    continuous_status_reader = (
        ContinuousCycleStatusReader(
            args.continuous_cycle_root,
            environment="paper",
            max_age_minutes=args.continuous_cycle_max_age_minutes,
        )
        if notifier is not None and str(args.continuous_cycle_root).strip()
        else None
    )
    recovered = inbox.recover_processing()
    if recovered:
        _logger.warning("recovered %d paper intent(s) after restart", recovered)
    last_symbol_refresh = float("-inf")
    last_health_write = float("-inf")
    last_health_signature: tuple[str, str, bool] | None = None
    last_notification_poll = 0.0
    last_batch_id = ""
    loop_sequence = 0
    requested_symbols_ready = True
    symbol_health_detail = ""
    try:
        while True:
            now = time.monotonic()
            loop_sequence += 1
            if now - last_symbol_refresh >= max(args.symbol_refresh_seconds, 0.25):
                current_state = kernel._state_ref()
                component_target_symbols = {
                    str(target.get("symbol") or "").upper()
                    for target in current_state.component_targets.values()
                    if target.get("symbol") and float(target.get("signed_qty") or 0.0) != 0.0
                }
                nonflat_symbols = {
                    symbol
                    for symbol, position in current_state.positions.items()
                    if position.signed_qty != 0.0
                }
                desired = operational_market_symbols(
                    symbols,
                    queued=inbox.requested_symbols(),
                    nonflat=nonflat_symbols,
                    working=current_state.working_symbols(
                        tolerance=policy.quantity_tolerance
                    ),
                    component_targets=component_target_symbols,
                    convergence=(
                        item.symbol for item in service.convergence_report().items
                    ),
                )
                missing = sorted(desired - set(rules))
                if missing:
                    _logger.error("paper targets lack verified rules: %s", missing)
                # Capture every pending symbol in parallel. Only the durable
                # queue head can become executable after exact-book readiness.
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
            protection_markets = {}
            for symbol, position in kernel._state_ref().positions.items():
                if position.signed_qty == 0.0:
                    continue
                book = recorder.current_book(symbol)
                if book is not None:
                    protection_markets[symbol] = book.market_ref(
                        input_key=f"paper-protection:{symbol}:{book.sequence}",
                        source="bybit_raw_l2",
                    )
            if protection_markets:
                protection.evaluate(protection_markets)
            if execution_adapter.pending_count():
                passive_observations = execution_adapter.poll()
                if passive_observations:
                    service.runtime.driver.ingest(passive_observations)
            health_status = (
                AccountOwnerHealthStatus.HEALTHY if requested_symbols_ready else AccountOwnerHealthStatus.BLOCKED
            )
            health_detail = symbol_health_detail
            retry_delay = 0.0
            receipt = None
            try:
                receipt = run_ready_request_or_converge(
                    service=service,
                    inbox=inbox,
                    readiness=market_readiness,
                )
            except Exception as exc:  # noqa: BLE001 - durable request returns to pending
                _logger.exception("paper account request failed and was returned to pending")
                health_status = AccountOwnerHealthStatus.BLOCKED
                health_detail = f"{type(exc).__name__}: {exc}"
                retry_delay = 0.5
            else:
                if receipt is not None:
                    last_batch_id = receipt.batch_id
                    _logger.info(
                        "paper account batch=%s accepted=%s commands=%d state=%s",
                        receipt.batch_id,
                        receipt.accepted,
                        len(receipt.command_ids),
                        receipt.final_state_hash[:12],
                    )
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
                health_detail = "; ".join(
                    part for part in (health_detail, convergence_detail) if part
                )[:1000]
            health_now = time.monotonic()
            health_signature = (
                health_status.value,
                health_detail[:1000],
                requested_symbols_ready,
            )
            if (
                receipt is not None
                or health_signature != last_health_signature
                or health_now - last_health_write >= args.health_interval_seconds
            ):
                publish_paper_owner_health(
                    kernel=kernel,
                    account_root=route.account_path,
                    account_id=route.account_id,
                    equity_usdt=args.equity_usdt,
                    status=health_status,
                    observed_ts_ns=runtime_clock.wall_time_ns(),
                    loop_sequence=loop_sequence,
                    requested_symbols_ready=requested_symbols_ready,
                    invocation_id=invocation_id,
                    last_batch_id=last_batch_id,
                    detail=health_detail,
                )
                last_health_write = health_now
                last_health_signature = health_signature
            if notifier is not None and now - last_notification_poll >= max(
                args.notification_poll_seconds, 0.25
            ):
                notification_now_ns = runtime_clock.wall_time_ns()
                midpoint_by_symbol: dict[str, float] = {}
                unavailable_midpoint_symbols: list[str] = []
                for symbol, position in kernel._state_ref().positions.items():
                    if position.signed_qty == 0.0:
                        continue
                    book, notification_wall_ns = recorder.current_book_with_observed_wall_ns(
                        symbol
                    )
                    book_age_ns = (
                        notification_wall_ns - book.local_receive_ts_ns
                        if book is not None
                        else -1
                    )
                    if (
                        book is None
                        or book.sequence_gap
                        or book_age_ns < 0
                        or book_age_ns > service.max_market_age_ns
                    ):
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
                        "fresh L2 midpoint unavailable: "
                        + ", ".join(sorted(unavailable_midpoint_symbols))
                    )
                if health_status is not AccountOwnerHealthStatus.HEALTHY:
                    notification_health_errors.append(
                        (health_detail or "paper owner blocked").strip()[:240]
                    )
                if notification_health_errors:
                    notification_health = "BLOCKED · " + " · ".join(notification_health_errors)
                elif health_detail.strip():
                    notification_health = "healthy · " + health_detail.strip()[:240]
                else:
                    notification_health = "healthy"
                # The paper twin has no external venue: the local journal is
                # position truth by construction, so the healthy defaults of
                # prepare() are the honest report rather than an assumption.
                notification = notifier.prepare(
                    midpoint_by_symbol=midpoint_by_symbol,
                    health=notification_health,
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
                    context="paper account",
                    logger=_logger,
                )
                last_notification_poll = now
            if retry_delay:
                time.sleep(retry_delay)
            time.sleep(max(args.idle_seconds, 0.01))
    except KeyboardInterrupt:
        return 0
    finally:
        public_stream.close()
        recorder.close()
        lease.close()


if __name__ == "__main__":
    raise SystemExit(main())
