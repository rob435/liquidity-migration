"""Process runner for the single-owner account execution service.

One owner per venue realm. ``--realm`` selects it, defaults to demo, and
never reaches mainnet by omission; the realm is then carried through the
durable route identity, the credential pair, the mutation lease, both
transports, and the instrument-rule environment, so a single flag cannot
leave any one of them pointing at the other account.
"""

from __future__ import annotations

import argparse
import logging
import math
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
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
from .artifact_snapshot import read_stable_file
from .execution_environment import (
    ExecutionEnvironment,
    account_id_for_environment,
    execution_environment,
)
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
    resolve_private_credentials,
    validate_private_order_permission,
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
from .account_loss_guard import (
    LOSS_GUARD_OK,
    LOSS_GUARD_TRIPPED,
    AccountLossGuard,
)
from .protection_engine import AccountProtectionEngine
from .post_fill_markouts import PostFillMarkoutObserver
from .venue_protection import AccountHealthChain, BybitNativeProtectionManager
from .venue_instrument_rules import load_venue_rules_bytes
from .venue_realm import REALM_CREDENTIAL_VARIABLES, VenueRealm, venue_realm

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


def run_periodic_reconciliation(
    *,
    reconciler: BybitAccountReconciler,
    funding_reconciler: BybitAccountFundingReconciler,
) -> tuple[AccountReconciliationReport | None, AccountFundingReconciliationReport | None]:
    """One tolerated refresh attempt for the owner loop.

    A lone REST timeout on a read is retryable without giving up
    position-truth guarantees: the health chain's recency requirement fails
    closed on its own if refreshes keep failing, while a process death here
    would take down execution, protection, and health publishing at once.
    Startup reconciliation stays strict and does not use this wrapper.
    """

    try:
        return _run_reconciliation_cycle(
            reconciler=reconciler,
            funding_reconciler=funding_reconciler,
        )
    except Exception:  # noqa: BLE001 - transient venue read failure must not kill the owner
        _logger.exception("periodic account reconciliation failed; retrying next interval")
        return None, None


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


@dataclass(frozen=True, slots=True)
class StartupDegradation:
    """One startup check that failed while real exposure was already open."""

    stage: str
    detail: str

    def describe(self) -> str:
        return f"startup degraded at {self.stage}: {self.detail}"


def account_has_open_exposure(
    *,
    kernel: Any,
    report: AccountReconciliationReport | None,
) -> bool:
    """Whether anything is open that a stopped owner would leave unmanaged.

    Deliberately generous: a journal position, a venue position, or a working
    order all count, and an unreadable venue is treated as exposure rather than
    as proof of flatness.
    """

    state = kernel._state_ref()
    if any(position.signed_qty != 0.0 for position in state.positions.values()):
        return True
    if state.working_order_ids:
        return True
    if report is None:
        return False
    return any(abs(float(qty)) > 1e-12 for qty in report.venue_positions.values())


def degrade_or_raise(
    *,
    stage: str,
    error: BaseException,
    kernel: Any,
    report: AccountReconciliationReport | None,
) -> StartupDegradation:
    """Stay alive when exiting would strand a live book; otherwise fail loudly.

    B16. ``Restart=always`` plus a strict startup check is a 2-second crash
    loop, and during it *nothing* runs: no reconciliation, no protection sync,
    no health publication, and — the part that actually costs money — no exit
    path. The position sits at the venue behind only its native stop with no
    process able to close it.

    Exiting is still the right answer when there is nothing open: a flat
    account with a broken startup check should fail loudly so the deploy
    notices, not limp along publishing BLOCKED forever.

    Degrading is not adoption and not permission. The owner enters its normal
    loop with the failure latched into health, which is what already refuses
    every exposure-increasing batch; risk-reducing batches and the safety-flat
    path stay available by design, which is the whole point of staying up.
    """

    if not account_has_open_exposure(kernel=kernel, report=report):
        raise error
    degradation = StartupDegradation(
        stage=stage,
        detail=f"{type(error).__name__}: {error}"[:900],
    )
    _logger.critical(
        "%s; staying alive in degraded mode to keep reconciliation, protection "
        "and the exit path running over open exposure",
        degradation.describe(),
    )
    return degradation


#: Freshness bound for the book a software stop/take-profit is decided against.
#: Deliberately looser than the order-placement bound: placing an order against
#: a stale mark mis-prices a fill, while evaluating a stop against one merely
#: delays a decision that the venue-native stop still backstops. It has to sit
#: well above ordinary reconnect jitter and far below the multi-minute scale of
#: a real outage. Tune it on observed gap durations rather than by taste.
PROTECTION_MAX_BOOK_AGE_NS = 15 * 1_000_000_000


def protection_market_refs(
    recorder: Any,
    symbols: Iterable[str],
    *,
    max_book_age_ns: int = PROTECTION_MAX_BOOK_AGE_NS,
) -> tuple[dict[str, MarketInputRef], dict[str, str]]:
    """Build component-protection market refs, skipping unusable or stale books.

    ``L2BookSnapshot.market_ref`` fails closed (ValueError) for gapped,
    crossed, empty, or otherwise invalid books. The protection loop must
    skip that symbol for one cycle — the venue-native disaster stop stays
    armed independently — instead of letting the exception kill the owner
    process, which would stop execution, reconciliation, health publishing,
    and every protection at once.

    Age is checked here because **a frozen book passes every structural check**.
    A dropped WebSocket delivers no deltas at all, so ``BookReconstruction``
    stays healthy and ``sequence_gap`` stays false while the real price walks
    away from the last snapshot. The public stream times out on ping/pong
    roughly every five minutes in normal operation (observed 2026-07-30), so
    without a bound the stop and take-profit engine repeatedly decides against a
    mark minutes old — silently failing to fire while price runs through the
    level, with no error, no health change, and no alert. Every other consumer
    in the owner loop already applies a freshness bound; this one did not.

    Age is measured as ``observed_wall_ns - book.local_receive_ts_ns`` under the
    recorder's lock, so a negative value is a real clock regression rather than
    a read/update race, and is treated as unusable.
    """

    refs: dict[str, MarketInputRef] = {}
    skipped: dict[str, str] = {}
    bound = max(int(max_book_age_ns), 0)
    for symbol in symbols:
        book, observed_wall_ns = recorder.current_book_with_observed_wall_ns(symbol)
        if book is None:
            skipped[symbol] = "no_book"
            continue
        age_ns = int(observed_wall_ns) - int(book.local_receive_ts_ns)
        if age_ns < 0:
            skipped[symbol] = "future_book"
            continue
        if age_ns > bound:
            skipped[symbol] = f"stale_book:{age_ns / 1_000_000_000:.1f}s"
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


#: How many reconciliation passes a fresh venue/local disagreement is given to
#: resolve itself before Telegram calls it a fault, and the floor that bound
#: never drops below.
POSITION_TRUTH_SETTLE_RECONCILE_PASSES = 15
POSITION_TRUTH_SETTLE_FLOOR_NS = 30 * 1_000_000_000


class PositionTruthSettling:
    """Hold a fresh venue/local disagreement briefly before calling it a fault.

    The venue's REST position view lags a fill. The kernel journals the fill
    the moment it reconstructs it, while Bybit keeps returning the pre-fill
    quantity for a few seconds after. ``require_recent_symbols_consistent``
    compares the CURRENT kernel position against the LAST venue snapshot, so
    that gap reads as a contradiction even though nothing is wrong — and it
    reads that way after every single fill, by construction.

    The reduction admission gate must keep treating it as a hard stop: it is
    about to send a reduce-only order against evidence it does not have. A
    Telegram lifecycle message must not. Sharing one predicate between the two
    made the account owner announce every reduction twice — once as
    ``⚠️ Local journal reduction … awaiting venue reconciliation`` and once,
    seconds later, as its retraction. On 2026-07-30 that was 108 alarms and 86
    retractions before 13:00 UTC.

    The window is wall time since the disagreement began, reset only by
    agreement, so a disagreement whose *details* keep changing (a book being
    resized name by name) cannot hold the clock open indefinitely. Anything
    that survives the window is reported in full, with its original cause. A
    clock that runs backwards fails closed and reports immediately.

    Measured basis: every one of the 2026-07-30 alarms was retracted within 14
    seconds, median 7. The 30-second floor is a bit over twice the worst
    observed case; the pass-count bound scales it if reconciliation is ever
    slowed down.
    """

    __slots__ = ("settle_ns", "_since_ns")

    def __init__(self, *, settle_ns: int) -> None:
        self.settle_ns = max(int(settle_ns), 0)
        self._since_ns: int | None = None

    def evaluate(
        self,
        healthy: bool,
        detail: str,
        status: str,
        *,
        now_ns: int,
    ) -> tuple[bool, str, str]:
        if healthy:
            self._since_ns = None
            return True, "", "healthy"
        if self._since_ns is None:
            self._since_ns = int(now_ns)
        elapsed_ns = int(now_ns) - self._since_ns
        if 0 <= elapsed_ns < self.settle_ns:
            return True, "", "settling"
        return False, detail, status


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
    environment: str = ExecutionEnvironment.DEMO.value,
    last_batch_id: str = "",
    detail: str = "",
) -> AccountOwnerHealth:
    """Publish owner health bound to canonical state and wallet capital.

    ``environment`` labels which owner this is. It defaults to demo — the same
    direction every other fallback in the realm plumbing takes — but a mainnet
    owner must pass its own, or its health would be published, read, and
    alerted on as if it were the demo fleet's.
    """

    state = kernel._state_ref()
    health = AccountOwnerHealth(
        owner="account_execution",
        environment=execution_environment(environment).value,
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
    parser = argparse.ArgumentParser(description="Run one account execution owner")
    parser.add_argument(
        "--realm",
        choices=tuple(realm.value for realm in VenueRealm),
        default=VenueRealm.DEMO.value,
        help=(
            "Venue realm this owner authenticates against. Omitting it selects "
            "demo and never mainnet; selecting mainnet additionally requires "
            "REAL_MONEY to be explicitly armed by the owner."
        ),
    )
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

    realm = venue_realm(args.realm)
    # The realm is baked into the durable on-disk route identity and
    # ensure_account_route refuses to rewrite an existing one, so the mainnet
    # owner necessarily gets its own journal root. There is no adoption or
    # migration path from the demo journal, by construction rather than policy.
    requested_route = derive_account_route(
        account_id=args.account_id,
        environment=realm.value,
        account_root=args.account_root,
        inbox_root=args.inbox_root,
    )
    if requested_route.account_id != account_id_for_environment(realm.value):
        parser.error(
            f"--account-id {args.account_id!r} does not belong to realm {realm.value!r}"
        )

    validate_private_order_permission(
        confirm_orders=args.confirm_demo_orders,
        realm=realm,
    )
    api_key, api_secret = resolve_private_credentials(realm=realm)
    if not api_key or not api_secret:
        key_variable, secret_variable = REALM_CREDENTIAL_VARIABLES[realm]
        raise RuntimeError(f"{key_variable} and {secret_variable} are required")
    credential_client = BybitPrivateClient(
        category="linear",
        testnet=False,
        demo=realm is VenueRealm.DEMO,
        realm=realm,
        api_key=api_key,
        api_secret=api_secret,
    )
    api_key_info = require_order_submit_permission(credential_client)
    demo_identity = DemoAccountIdentity.from_api_key_info(
        api_key=api_key,
        api_key_info=api_key_info,
        environment=realm.value,
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
    if realm is VenueRealm.DEMO:
        rules = load_demo_rules(
            args.demo_rules_file,
            max_age_seconds=args.max_demo_rule_age_hours * 3600.0,
        )
    else:
        # B17. The demo receipt is produced by an order-placing probe. Off demo
        # the rules come from the read-only instruments-info endpoint instead,
        # and the loader refuses a receipt bound to any other realm.
        rules = load_venue_rules_bytes(
            read_stable_file(
                Path(args.demo_rules_file).expanduser(),
                label="venue instrument rules",
                require_single_link=False,
            ).data,
            realm=realm,
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
        demo=realm is VenueRealm.DEMO,
        realm=realm,
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
    startup_degradations: list[StartupDegradation] = []
    try:
        require_bybit_demo_order_ownership(
            client=private_client,
            kernel=kernel,
            native_order_verifier=native_protection.is_verified_native_order,
        )
    except Exception as exc:  # noqa: BLE001 - classified by degrade_or_raise
        startup_degradations.append(
            degrade_or_raise(
                stage="venue order ownership",
                error=exc,
                kernel=kernel,
                report=None,
            )
        )

    def build_private_stream() -> BybitPrivateWebSocketStream:
        return BybitPrivateWebSocketStream(
            category="linear",
            testnet=False,
            demo=realm is VenueRealm.DEMO,
            realm=realm,
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
    try:
        require_startup_reconciliation_safe(bootstrap_reconciliation)
    except Exception as exc:  # noqa: BLE001 - classified by degrade_or_raise
        startup_degradations.append(
            degrade_or_raise(
                stage="bootstrap reconciliation",
                error=exc,
                kernel=kernel,
                report=bootstrap_reconciliation,
            )
        )
    funding_reconciler = BybitAccountFundingReconciler(
        kernel=kernel,
        client=private_client,
    )
    startup_reconciliation, startup_funding_reconciliation = _run_reconciliation_cycle(
        reconciler=reconciler,
        funding_reconciler=funding_reconciler,
    )
    for stage, check in (
        ("startup reconciliation", lambda: require_startup_reconciliation_safe(startup_reconciliation)),
        ("startup funding reconciliation", startup_funding_reconciliation.require_healthy),
        (
            "startup native protection sync",
            lambda: (
                None
                if native_protection.breaches()
                else native_protection.sync_symbols(
                    [
                        symbol
                        for symbol, position in kernel._state_ref().positions.items()
                        if position.signed_qty != 0.0
                    ]
                )
            ),
        ),
    ):
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - classified by degrade_or_raise
            startup_degradations.append(
                degrade_or_raise(
                    stage=stage,
                    error=exc,
                    kernel=kernel,
                    report=startup_reconciliation,
                )
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
        rules_provider=VerifiedBybitDemoRulesProvider(rules, environment=realm.value),
        risk_policy=policy,
        execution_adapter=BybitDemoExecutionAdapter(
            private_client,
            # B5: read position truth back at the create boundary instead of
            # trusting Bybit's atomic-arming promise until the next reconcile.
            entry_stop_verifier=native_protection.verify_entry_attached_stop,
        ),
        native_protection_policy=native_protection_policy,
        required_rules_environment=realm.value,
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
            environment=realm.value,
            max_age_minutes=args.continuous_cycle_max_age_minutes,
        )
        if notifier is not None and str(args.continuous_cycle_root).strip()
        else None
    )
    recovered = inbox.recover_processing()
    if recovered:
        _logger.warning("recovered %d account intent(s) left processing by a prior crash", recovered)

    last_reconcile = time.monotonic()
    position_truth_settling = PositionTruthSettling(
        settle_ns=max(
            int(args.reconcile_seconds * POSITION_TRUTH_SETTLE_RECONCILE_PASSES * 1_000_000_000),
            POSITION_TRUTH_SETTLE_FLOOR_NS,
        )
    )
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
    # Account-level daily loss halt. The ceiling rides on the risk policy, so it
    # is bound into the operational profile and hashed into the authority
    # receipt: it cannot be raised without invalidating the deploy authority.
    # Absent/zero leaves the machinery running and observable but never tripping,
    # which is how it is exercised on demo long before it guards anything real.
    loss_guard = AccountLossGuard(
        max_daily_loss_usdt=(
            policy.max_daily_loss_usdt if policy.max_daily_loss_usdt > 0.0 else None
        )
    )
    loss_guard_flat_published = False
    try:
        while True:
            now = time.monotonic()
            loop_sequence += 1
            markout_observer.drain()
            if now - last_reconcile >= max(args.reconcile_seconds, 0.1):
                report, funding_report = run_periodic_reconciliation(
                    reconciler=reconciler,
                    funding_reconciler=funding_reconciler,
                )
                if report is not None:
                    latest_reconcile_report = report
                    if not report.healthy:
                        _logger.error(
                            "account reconcile blocked new intents: %s",
                            "; ".join(report.mismatches),
                        )
                    if funding_report is not None and not funding_report.healthy:
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
            protection_evaluation_error = ""
            if protection_skipped:
                skipped_detail = "; ".join(
                    f"{symbol}={reason}" for symbol, reason in sorted(protection_skipped.items())
                )
                _logger.warning(
                    "component protection skipped unusable books this cycle: %s",
                    skipped_detail,
                )
                # A skipped symbol means its software stop and take-profit did
                # not run. That is exactly the state in which no new exposure
                # should be opened, so it has to reach health rather than only
                # journald — the owner previously published HEALTHY while a
                # symbol's component protection was inoperative, cycle after
                # cycle. The venue-native stop is unaffected either way.
                protection_evaluation_error = _append_health_error(
                    protection_evaluation_error,
                    f"component protection did not evaluate: {skipped_detail}",
                )
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
            if startup_degradations:
                # B16. A startup check failed over open exposure and the owner
                # stayed up rather than crash-looping. One fully healthy pass
                # supersedes it — the account is now understood — and until then
                # the failure rides on health, which is what refuses new
                # exposure while leaving exits and the safety flat available.
                if health_status is AccountOwnerHealthStatus.HEALTHY:
                    _logger.warning(
                        "startup degradation cleared by a healthy pass: %s",
                        "; ".join(item.describe() for item in startup_degradations),
                    )
                    startup_degradations.clear()
                else:
                    health_status = AccountOwnerHealthStatus.BLOCKED
                    health_details.extend(item.describe() for item in startup_degradations)
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
                loss_state, loss_detail = loss_guard.evaluate(
                    equity_usdt=last_capital_snapshot.equity_usdt,
                    equity_ts_ns=last_capital_snapshot.snapshot_ts_ns,
                    now_ns=time.time_ns(),
                )
                if loss_state != LOSS_GUARD_OK:
                    health_status = AccountOwnerHealthStatus.BLOCKED
                    health_detail = _append_health_error(health_detail, loss_detail)[:1000]
                    health_signature = (
                        health_status.value,
                        health_detail,
                        requested_symbols_ready,
                    )
                if loss_state == LOSS_GUARD_TRIPPED and not loss_guard_flat_published:
                    # Publish once. run_safety_flat_once is the same durable
                    # all-flat path a native-protection breach uses: reductions
                    # bypass every notional cap and the health chain, so a
                    # BLOCKED owner can still close. Republishing every cycle
                    # would queue redundant flats behind the first.
                    _logger.critical("account loss ceiling reached: %s", loss_detail)
                    try:
                        service.run_safety_flat_once(inbox)
                        loss_guard_flat_published = True
                    except Exception as exc:  # noqa: BLE001 - never kill the owner
                        _logger.exception("loss-ceiling safety flat failed")
                        health_detail = _append_health_error(
                            health_detail,
                            f"loss-ceiling safety flat failed: {type(exc).__name__}: {exc}",
                        )[:1000]
                published_health = publish_demo_owner_health(
                    kernel=kernel,
                    account_root=route.account_path,
                    account_id=route.account_id,
                    environment=realm.value,
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
                # Reporting only. The reduction admission gate calls the
                # reconciler directly and is never softened by this.
                (
                    position_truth_healthy,
                    position_truth_error,
                    position_truth_status,
                ) = position_truth_settling.evaluate(
                    position_truth_healthy,
                    position_truth_error,
                    position_truth_status,
                    now_ns=notification_now_ns,
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
                        # No configured cycle root means the sleeve is not
                        # running (CONTINUOUS retired 2026-07-29). Render no
                        # line at all rather than a permanent "unavailable" or
                        # ever-growing "STALE" fault for a sleeve nobody runs.
                        else ""
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
