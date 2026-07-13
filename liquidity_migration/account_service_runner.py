"""Demo-only process runner for the single-owner account execution service."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Mapping, Protocol

from .account_execution_stream import BybitAccountExecutionConsumer
from .account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    InstrumentRules,
)
from .account_reconcile import (
    AccountReconciliationReport,
    BybitAccountFundingReconciler,
    BybitAccountReconciler,
)
from .account_route import ensure_account_route
from .account_notifications import AccountNotificationEngine
from .account_owner_health import (
    AccountOwnerHealth,
    AccountOwnerHealthStatus,
    fold_convergence_health,
    format_convergence_health,
    write_account_owner_health,
)
from .account_owner_lease import AccountOwnerLease
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
    resolve_private_credentials,
    validate_demo_order_permission,
)
from .deterministic_serialization import canonical_json
from .execution_adapters import BybitDemoExecutionAdapter
from .market_capture import (
    BybitRawPublicMarketStream,
    MarketCaptureConfig,
    SequenceAwareMarketRecorder,
    recorder_callback,
    symbols_from_file,
)
from .protection_engine import AccountProtectionEngine
from .venue_protection import AccountHealthChain, BybitNativeProtectionManager

_logger = logging.getLogger(__name__)


class _PositionTruthChecker(Protocol):
    def require_recent_symbols_consistent(
        self,
        symbols: Sequence[str],
        *,
        max_age_ns: int,
    ) -> None: ...


def notification_position_truth(
    *,
    reconciler: _PositionTruthChecker,
    kernel: AccountExecutionKernel,
    report: AccountReconciliationReport | None,
    max_age_ns: int,
) -> tuple[bool, str]:
    """Evaluate quantity truth without conflating unrelated owner health.

    Native-protection health can be blocked while the venue and local position
    quantities still agree. Telegram must report those as two distinct facts.
    """

    if report is None:
        return False, "account reconciliation has not completed"
    state = kernel.state()
    symbols = sorted(set(report.venue_positions) | set(state.positions))
    try:
        reconciler.require_recent_symbols_consistent(
            symbols,
            max_age_ns=max_age_ns,
        )
    except Exception as exc:  # noqa: BLE001 - concise operator-visible cause
        return False, str(exc)[:240]
    return True, ""


def require_order_submit_permission(client: Any) -> None:
    """Fail owner startup unless the configured key can mutate demo orders."""

    allowed, reason = api_key_allows_order_submit(client.get_api_key_information())
    if not allowed:
        raise RuntimeError(f"Bybit demo API key cannot submit orders: {reason}")


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
        last_batch_id=last_batch_id[:500],
        detail=detail[:1000],
    )
    write_account_owner_health(account_root, health)
    return health


def _load_json(path: str | Path) -> Mapping[str, Any]:
    payload = json.loads(Path(path).expanduser().read_text())
    if not isinstance(payload, Mapping):
        raise ValueError(f"configuration must be a JSON object: {path}")
    return payload


def load_demo_rules(
    path: str | Path,
    *,
    now_ns: int | None = None,
    max_age_seconds: float | None = None,
) -> dict[str, InstrumentRules]:
    payload = _load_json(path)
    if int(payload.get("schema_version") or 0) != 2 or payload.get("environment") != "demo":
        raise ValueError("demo rules file requires schema_version=2 and environment='demo'")
    observed_hash = str(payload.get("artifact_sha256") or "")
    expected_hash = hashlib.sha256(
        canonical_json({**payload, "artifact_sha256": ""})
    ).hexdigest()
    if observed_hash != expected_hash:
        raise ValueError("demo rules file artifact_sha256 is missing or invalid")
    verified_ts_ns = int(payload.get("verified_ts_ns") or 0)
    if verified_ts_ns <= 0:
        raise ValueError("demo rules file requires verified_ts_ns")
    if max_age_seconds is not None:
        current_ns = time.time_ns() if now_ns is None else now_ns
        if max_age_seconds <= 0.0:
            raise ValueError("max demo rule age must be positive")
        age_ns = current_ns - verified_ts_ns
        if age_ns < 0 or age_ns > max_age_seconds * 1_000_000_000:
            raise ValueError("demo rules receipt is stale or future-dated")
    rows = payload.get("rules")
    if not isinstance(rows, Mapping):
        raise ValueError("demo rules file requires a 'rules' object")
    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("demo rules file requires per-symbol probe evidence")
    output: dict[str, InstrumentRules] = {}
    for symbol, raw in rows.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"demo rule {symbol} must be an object")
        normalized_symbol = str(symbol).upper()
        receipt = evidence.get(symbol) or evidence.get(normalized_symbol)
        if not isinstance(receipt, Mapping):
            raise ValueError(f"demo rule {normalized_symbol} lacks probe evidence")
        accepted_notional = float(receipt.get("lowest_accepted_notional_usdt") or 0.0)
        attempts = receipt.get("attempts")
        if (
            accepted_notional <= 0.0
            or not isinstance(attempts, list)
            or not any(isinstance(item, Mapping) and bool(item.get("accepted")) for item in attempts)
        ):
            raise ValueError(f"demo rule {normalized_symbol} has invalid acceptance evidence")
        rule = InstrumentRules(
            symbol=normalized_symbol,
            qty_step=float(raw["qty_step"]),
            min_qty=float(raw["min_qty"]),
            min_notional=float(raw["min_notional"]),
            tick_size=float(raw["tick_size"]),
            max_order_qty=float(raw.get("max_order_qty") or 0.0),
            max_leverage=float(raw.get("max_leverage") or 0.0),
            source=str(raw.get("source") or ""),
            environment=str(raw.get("environment") or ""),
            observed_ts_ns=int(raw.get("observed_ts_ns") or 0),
        )
        if abs(rule.min_notional - accepted_notional) > max(1e-12, accepted_notional * 1e-12):
            raise ValueError(f"demo rule {normalized_symbol} minimum does not match acceptance evidence")
        if rule.observed_ts_ns != verified_ts_ns:
            raise ValueError(f"demo rule {normalized_symbol} timestamp does not match receipt")
        output[normalized_symbol] = rule
    VerifiedBybitDemoRulesProvider(output)  # validate every row before returning
    return output


def load_risk_policy(path: str | Path) -> AccountRiskPolicy:
    payload = _load_json(path)
    return AccountRiskPolicy(
        max_component_gross_notional_usdt=float(payload["max_component_gross_notional_usdt"]),
        max_account_gross_notional_usdt=float(payload["max_account_gross_notional_usdt"]),
        max_symbol_notional_usdt=float(payload["max_symbol_notional_usdt"]),
        max_initial_margin_usdt=float(payload["max_initial_margin_usdt"]),
        max_leverage=float(payload["max_leverage"]),
        quantity_tolerance=float(payload.get("quantity_tolerance") or 1e-12),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the demo-only account execution owner")
    parser.add_argument("--account-root", required=True)
    parser.add_argument("--inbox-root", required=True)
    parser.add_argument("--capture-root", required=True)
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
    parser.add_argument("--owner-lock", default="")
    parser.add_argument("--reconcile-seconds", type=float, default=2.0)
    parser.add_argument("--symbol-refresh-seconds", type=float, default=5.0)
    parser.add_argument("--idle-seconds", type=float, default=0.1)
    parser.add_argument("--confirm-demo-orders", action="store_true")
    parser.add_argument("--telegram", action="store_true")
    parser.add_argument("--notification-state", default="")
    parser.add_argument("--notification-poll-seconds", type=float, default=1.0)
    parser.add_argument("--health-interval-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.health_interval_seconds <= 0.0:
        parser.error("--health-interval-seconds must be positive")

    route = ensure_account_route(
        account_id=args.account_id,
        environment="demo",
        account_root=args.account_root,
        inbox_root=args.inbox_root,
    )

    owner_lease = AccountOwnerLease(
        args.owner_lock or str(route.account_path / "account_execution_owner.lock")
    )
    owner_lease.acquire()

    validate_demo_order_permission(confirm_demo_orders=args.confirm_demo_orders)
    api_key, api_secret, demo = resolve_private_credentials()
    if not demo:
        raise RuntimeError("account service runner refuses REAL_MONEY/mainnet")
    if not api_key or not api_secret:
        raise RuntimeError("BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET are required")
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

    private_client = BybitPrivateClient(
        category="linear",
        testnet=False,
        demo=True,
        api_key=api_key,
        api_secret=api_secret,
        account_execution_owner=True,
    )
    require_order_submit_permission(private_client)
    kernel = AccountExecutionKernel(route.account_path, account_id=route.account_id)
    native_protection = BybitNativeProtectionManager(
        kernel=kernel,
        client=private_client,
        instrument_rules=rules,
        fallback_stop_fraction=args.disaster_stop_fraction,
    )
    # Prove the venue has no unowned regular or conditional order before any
    # stream starts or any strategy request can be claimed. An empty/new journal
    # therefore requires a completely empty venue order book.
    require_bybit_demo_order_ownership(
        client=private_client,
        kernel=kernel,
        native_order_verifier=native_protection.is_verified_native_order,
    )
    private_stream = BybitPrivateWebSocketStream(
        category="linear",
        testnet=False,
        demo=True,
        api_key=api_key,
        api_secret=api_secret,
    )
    recorder = SequenceAwareMarketRecorder(
        args.capture_root,
        config=MarketCaptureConfig(depth=50),
    )
    public_stream = BybitRawPublicMarketStream(
        testnet=False,
        depth=50,
        on_message=recorder_callback(recorder),
    )
    public_stream.start(symbols)
    execution_consumer = BybitAccountExecutionConsumer(
        kernel=kernel,
        private_stream=private_stream,
        native_protection_manager=native_protection,
    )
    execution_consumer.start()
    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=private_client,
        instrument_rules=rules,
        native_protection_manager=native_protection,
    )
    # Bootstrap venue truth before the service can claim any request. Existing
    # venue exposure with an empty kernel is a hard mismatch, never auto-adopted.
    startup_reconciliation = reconciler.reconcile_once()
    startup_reconciliation.require_healthy()
    funding_reconciler = BybitAccountFundingReconciler(
        kernel=kernel,
        client=private_client,
    )
    startup_funding_reconciliation = funding_reconciler.reconcile_once()
    startup_funding_reconciliation.require_healthy()
    native_protection.sync_symbols(
        [symbol for symbol, position in kernel.state().positions.items() if position.signed_qty != 0.0]
    )
    health_chain = AccountHealthChain(
        (reconciler, funding_reconciler, native_protection)
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
        required_rules_environment="demo",
        health_provider=health_chain,
        position_truth_provider=reconciler,
        max_health_age_ns=max(int(args.reconcile_seconds * 2 * 1_000_000_000), 1),
    )
    inbox = AccountIntentInbox(route)
    protection_engine = AccountProtectionEngine(
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
        )
        if args.telegram
        else None
    )
    recovered = inbox.recover_processing()
    if recovered:
        _logger.warning("recovered %d account intent(s) left processing by a prior crash", recovered)

    last_reconcile = time.monotonic()
    last_symbol_refresh = time.monotonic()
    last_notification_poll = 0.0
    last_health_write = float("-inf")
    last_health_signature: tuple[str, str, bool] | None = None
    last_batch_id = ""
    loop_sequence = 0
    requested_symbols_ready = True
    symbol_health_detail = ""
    latest_reconcile_report = reconciler.last_report
    last_capital_snapshot = snapshot_provider.current(batch_id="owner-health/bootstrap")
    try:
        while True:
            now = time.monotonic()
            loop_sequence += 1
            if now - last_reconcile >= max(args.reconcile_seconds, 0.1):
                report = reconciler.reconcile_once()
                latest_reconcile_report = report
                if not report.healthy:
                    _logger.error("account reconcile blocked new intents: %s", "; ".join(report.mismatches))
                funding_report = funding_reconciler.reconcile_once()
                if not funding_report.healthy:
                    _logger.error("account funding reconcile blocked new intents")
                last_reconcile = now
            if now - last_symbol_refresh >= max(args.symbol_refresh_seconds, 0.25):
                desired = symbols_from_file(symbols_path)
                desired.update(inbox.requested_symbols())
                current_state = kernel._state_ref()
                desired.update(
                    str(target.get("symbol") or "").upper()
                    for target in current_state.component_targets.values()
                    if target.get("symbol") and float(target.get("signed_qty") or 0.0) != 0.0
                )
                desired.update(
                    symbol
                    for symbol, position in current_state.positions.items()
                    if position.signed_qty != 0.0
                )
                desired.update(current_state.working_symbols(tolerance=policy.quantity_tolerance))
                desired.update(item.symbol for item in service.convergence_report().items)
                if desired:
                    missing_rules = sorted(desired - set(rules))
                    if missing_rules:
                        _logger.error("not subscribing symbols without verified demo rules: %s", missing_rules)
                        requested_symbols_ready = False
                        symbol_health_detail = "targets lack demo-verified rules: " + ", ".join(missing_rules)
                    else:
                        public_stream.update_symbols(desired)
                        requested_symbols_ready = True
                        symbol_health_detail = ""
                last_symbol_refresh = now
            protection_markets = {}
            for symbol in {
                str(target.get("symbol") or "").upper()
                for target in kernel.state().component_targets.values()
                if target.get("symbol") and float(target.get("signed_qty") or 0.0) != 0.0
            }:
                book = recorder.current_book(symbol)
                if book is not None:
                    protection_markets[symbol] = book.market_ref(
                        input_key=f"protection:{symbol}:{book.sequence}",
                        source="bybit_raw_l2",
                    )
            if protection_markets:
                protection_engine.evaluate(protection_markets)
            reconcile_healthy = bool(latest_reconcile_report is not None and latest_reconcile_report.healthy)
            health_status = (
                AccountOwnerHealthStatus.HEALTHY
                if requested_symbols_ready and reconcile_healthy
                else AccountOwnerHealthStatus.BLOCKED
            )
            health_detail = symbol_health_detail
            if not reconcile_healthy and latest_reconcile_report is not None:
                health_detail = "account reconciliation mismatch: " + "; ".join(latest_reconcile_report.mismatches)
            receipt = None
            try:
                receipt = service.run_once(inbox) if requested_symbols_ready else None
                if receipt is not None:
                    last_batch_id = receipt.batch_id
                    native_protection.sync_symbols(
                        [symbol for symbol, position in kernel.state().positions.items() if position.signed_qty != 0.0]
                    )
                    _logger.info(
                        "account request complete batch=%s accepted=%s commands=%d state=%s",
                        receipt.batch_id,
                        receipt.accepted,
                        len(receipt.command_ids),
                        receipt.final_state_hash[:12],
                    )
            except Exception as exc:  # noqa: BLE001 - request was released for retry
                _logger.exception("account request failed and was returned to pending")
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
                publish_demo_owner_health(
                    kernel=kernel,
                    account_root=route.account_path,
                    account_id=route.account_id,
                    risk_snapshot=last_capital_snapshot,
                    status=health_status,
                    observed_ts_ns=time.time_ns(),
                    loop_sequence=loop_sequence,
                    requested_symbols_ready=requested_symbols_ready,
                    last_batch_id=last_batch_id,
                    detail=health_detail,
                )
                last_health_write = health_now
                last_health_signature = health_signature
            if notifier is not None and now - last_notification_poll >= max(args.notification_poll_seconds, 0.25):
                midpoint_by_symbol: dict[str, float] = {}
                unavailable_midpoint_symbols: list[str] = []
                notification_wall_ns = time.time_ns()
                for symbol, position in kernel.state().positions.items():
                    if position.signed_qty == 0.0:
                        continue
                    book = recorder.current_book(symbol)
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
                reconcile_health_max_age_ns = max(
                    int(args.reconcile_seconds * 2 * 1_000_000_000),
                    1,
                )
                try:
                    health_chain.require_recent_healthy(
                        max_age_ns=reconcile_health_max_age_ns
                    )
                except Exception as exc:  # noqa: BLE001 - rendered hourly, not spammed per cycle
                    notification_health_errors.append(str(exc)[:240])
                # Position truth is narrower than general owner health. A
                # missing native stop should block health, but must not make a
                # matching venue/local quantity report claim that the two
                # disagree. Derive this flag from reconciliation alone.
                position_truth_healthy, position_truth_error = notification_position_truth(
                    reconciler=reconciler,
                    kernel=kernel,
                    report=latest_reconcile_report,
                    max_age_ns=reconcile_health_max_age_ns,
                )
                if position_truth_error:
                    notification_health_errors.append(position_truth_error)
                try:
                    convergence_report = service.convergence_report()
                    convergence_detail = format_convergence_health(convergence_report)
                except Exception as exc:  # noqa: BLE001 - rendered hourly, not spammed per cycle
                    notification_health_errors.append(
                        f"convergence health failed: {type(exc).__name__}: {exc}"[:240]
                    )
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
                        latest_reconcile_report.venue_positions
                        if latest_reconcile_report is not None
                        else {}
                    ),
                    position_truth_healthy=position_truth_healthy,
                )
                if not notification.message:
                    notifier.commit(notification)
                else:
                    try:
                        from .telegram import send_telegram_message

                        sent = send_telegram_message(notification.message, enabled=True)
                    except Exception:  # noqa: BLE001 - do not advance dedupe state on failure
                        _logger.exception("account Telegram delivery failed")
                    else:
                        if sent:
                            notifier.commit(notification)
                        else:
                            _logger.error("account Telegram delivery returned false")
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
