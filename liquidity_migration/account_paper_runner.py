"""Run the shared account kernel with the deterministic execution twin for paper."""

from __future__ import annotations

import argparse
import logging
import math
import time
from pathlib import Path

from .account_kernel import AccountExecutionKernel, AccountRiskSnapshot
from .account_owner_health import (
    AccountOwnerHealth,
    AccountOwnerHealthStatus,
    fold_convergence_health,
    write_account_owner_health,
)
from .account_owner_lease import AccountOwnerLease
from .account_route import ensure_account_route
from .account_service import AccountExecutionService, AccountIntentInbox
from .account_service_bybit import (
    CapturedBybitMarketProvider,
    CapturedPaperExecutionAdapter,
    VerifiedBybitDemoRulesProvider,
)
from .account_service_runner import load_demo_rules, load_risk_policy
from .deterministic_runtime import Clock, SystemClock
from .execution_adapters import MarketOrderExecutionTwin
from .execution_twin_calibration import (
    execution_twin_config_from_calibration,
    load_calibration_receipt,
)
from .market_capture import (
    BybitRawPublicMarketStream,
    MarketCaptureConfig,
    SequenceAwareMarketRecorder,
    recorder_callback,
    symbols_from_file,
)
from .protection_engine import AccountProtectionEngine

_logger = logging.getLogger(__name__)


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
        last_batch_id=last_batch_id[:500],
        detail=detail[:1000],
    )
    write_account_owner_health(account_root, health)
    return health


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-root", required=True)
    parser.add_argument("--inbox-root", required=True)
    parser.add_argument("--capture-root", required=True)
    parser.add_argument("--symbols-file", required=True)
    parser.add_argument("--demo-rules-file", required=True)
    parser.add_argument("--risk-policy-file", required=True)
    parser.add_argument("--account-id", default="bybit-paper-unified")
    parser.add_argument("--owner-lock", default="")
    parser.add_argument("--equity-usdt", type=float, required=True)
    parser.add_argument("--calibration-file", required=True)
    parser.add_argument(
        "--latency-quantile",
        choices=("p50", "p75", "p95", "p99"),
        default="p50",
    )
    parser.add_argument(
        "--slippage-quantile",
        choices=("p50", "p75", "p95", "p99"),
        default="p50",
    )
    parser.add_argument("--max-decision-age-ms", type=float, default=250.0)
    parser.add_argument("--max-demo-rule-age-hours", type=float, default=168.0)
    parser.add_argument("--symbol-refresh-seconds", type=float, default=5.0)
    parser.add_argument("--health-interval-seconds", type=float, default=5.0)
    parser.add_argument("--idle-seconds", type=float, default=0.1)
    args = parser.parse_args(argv)
    if args.health_interval_seconds <= 0.0:
        parser.error("--health-interval-seconds must be positive")

    route = ensure_account_route(
        account_id=args.account_id,
        environment="paper",
        account_root=args.account_root,
        inbox_root=args.inbox_root,
    )

    lease = AccountOwnerLease(
        args.owner_lock or str(route.account_path / "account_execution_owner.lock")
    )
    lease.acquire()
    rules = load_demo_rules(
        args.demo_rules_file,
        max_age_seconds=args.max_demo_rule_age_hours * 3600.0,
    )
    policy = load_risk_policy(args.risk_policy_file)
    twin_config = execution_twin_config_from_calibration(
        load_calibration_receipt(args.calibration_file),
        max_decision_age_ns=int(args.max_decision_age_ms * 1_000_000),
        latency_quantile=args.latency_quantile,
        slippage_quantile=args.slippage_quantile,
        require_gate=True,
    )
    symbols_path = Path(args.symbols_file).expanduser()
    symbols = symbols_from_file(symbols_path)
    if not symbols:
        raise RuntimeError("paper symbols file is empty")
    missing = sorted(symbols - set(rules))
    if missing:
        raise RuntimeError(f"paper symbols lack demo-verified rules: {', '.join(missing)}")

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
    kernel = AccountExecutionKernel(route.account_path, account_id=route.account_id)
    runtime_clock = SystemClock()
    market_provider = CapturedBybitMarketProvider(recorder)
    twin = MarketOrderExecutionTwin(
        books={},
        instrument_rules=rules,
        config=twin_config,
        name="paper",
        id_seed=f"{route.account_id}:paper-execution",
    )
    service = AccountExecutionService(
        route=route,
        kernel=kernel,
        market_provider=market_provider,
        snapshot_provider=FixedCapitalSnapshotProvider(args.equity_usdt, clock=runtime_clock),
        rules_provider=VerifiedBybitDemoRulesProvider(rules),
        risk_policy=policy,
        execution_adapter=CapturedPaperExecutionAdapter(
            market_provider=market_provider,
            twin=twin,
        ),
        required_rules_environment="demo",
        clock=runtime_clock,
    )
    inbox = AccountIntentInbox(route)
    protection = AccountProtectionEngine(
        kernel=kernel,
        inbox=inbox,
        instrument_rules=rules,
    )
    recovered = inbox.recover_processing()
    if recovered:
        _logger.warning("recovered %d paper intent(s) after restart", recovered)
    last_symbol_refresh = time.monotonic()
    last_health_write = float("-inf")
    last_health_signature: tuple[str, str, bool] | None = None
    last_batch_id = ""
    loop_sequence = 0
    requested_symbols_ready = True
    symbol_health_detail = ""
    try:
        while True:
            now = time.monotonic()
            loop_sequence += 1
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
                missing = sorted(desired - set(rules))
                if missing:
                    _logger.error("paper targets lack verified rules: %s", missing)
                    requested_symbols_ready = False
                    symbol_health_detail = "targets lack demo-verified rules: " + ", ".join(missing)
                else:
                    public_stream.update_symbols(desired)
                    requested_symbols_ready = True
                    symbol_health_detail = ""
                last_symbol_refresh = now
            protection_markets = {}
            for symbol, position in kernel.state().positions.items():
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
            health_status = (
                AccountOwnerHealthStatus.HEALTHY if requested_symbols_ready else AccountOwnerHealthStatus.BLOCKED
            )
            health_detail = symbol_health_detail
            retry_delay = 0.0
            receipt = None
            try:
                receipt = service.run_once(inbox) if requested_symbols_ready else None
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
                    last_batch_id=last_batch_id,
                    detail=health_detail,
                )
                last_health_write = health_now
                last_health_signature = health_signature
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
