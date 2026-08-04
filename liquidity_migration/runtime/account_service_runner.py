"""Process runner for the single-owner account execution service.

One owner per venue realm. ``--realm`` selects it and defaults to demo; the
realm is then carried through the route identity, the credential pair, the
mutation lease, both transports, and the instrument-rule environment, so one
flag cannot leave any of them pointing at the other account.
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

from liquidity_migration.venue.account_execution_stream import (
    BybitAccountExecutionConsumer,
    PrivateExecutionStreamSupervisor,
)
from liquidity_migration.policy.account_execution_config import (
    load_demo_rules,
    load_risk_policy,
    require_registered_demo_rule_max_age_hours,
)
from liquidity_migration.account.account_contracts import (
    AccountRiskSnapshot,
    MarketInputRef,
    NativeDisasterProtectionPolicy,
)
from liquidity_migration.account.account_kernel import AccountExecutionKernel
from liquidity_migration.account.account_market_readiness import (
    RequestedMarketWarmupGate,
    require_registered_request_market_warmup_timeout,
    run_ready_request_or_converge,
)
from liquidity_migration.venue.account_reconcile import (
    AccountFundingReconciliationReport,
    AccountPositionTruthMismatchError,
    AccountReconciliationReport,
    AccountReconciliationStaleError,
    BybitAccountFundingReconciler,
    BybitAccountReconciler,
)
from liquidity_migration.account.account_route import derive_account_route, ensure_account_route
from liquidity_migration.core.artifact_snapshot import read_stable_file
from liquidity_migration.account.execution_environment import (
    ExecutionEnvironment,
    account_id_for_environment,
    execution_environment,
)
from liquidity_migration.ops.account_notifications import AccountNotificationEngine, deliver_notification_batch
from liquidity_migration.core.logging_setup import ensure_default_log_handler
from liquidity_migration.strategy.continuous_cycle_status import ContinuousCycleStatusReader
from liquidity_migration.account.account_owner_health import (
    AccountOwnerHealth,
    AccountOwnerHealthStatus,
    fold_convergence_health,
    format_convergence_health,
    require_systemd_invocation_id,
    write_account_owner_health,
)
from liquidity_migration.account.account_owner_lease import DemoAccountIdentity, DemoAccountMutationLease
from liquidity_migration.account.account_service import (
    AccountExecutionService,
    AccountIntentInbox,
    StaleEntryRequestExpired,
)
from liquidity_migration.venue.account_service_bybit import (
    BybitAccountSnapshotProvider,
    CapturedBybitMarketProvider,
    VerifiedBybitDemoRulesProvider,
    require_bybit_order_ownership,
)
from liquidity_migration.venue.bybit import (
    BybitPrivateClient,
    BybitPrivateWebSocketStream,
    api_key_allows_order_submit,
    resolve_private_credentials,
)
from liquidity_migration.core.deterministic_runtime import SystemClock
from liquidity_migration.venue.bybit_execution_adapter import BybitDemoExecutionAdapter
from liquidity_migration.venue.entry_quote_manager import EntryQuoteConfig, EntryQuoteManager
from liquidity_migration.account.market_capture import (
    BybitRawPublicMarketStream,
    MarketCaptureConfig,
    SequenceAwareMarketRecorder,
    operational_market_symbols,
    recorder_callback,
    symbols_from_file,
)
from liquidity_migration.policy.equity_anchored_envelope import EquityAnchoredEnvelope
from liquidity_migration.policy.operational_profile import load_operational_profile
from liquidity_migration.policy.account_loss_guard import (
    LOSS_GUARD_OK,
    LOSS_GUARD_TRIPPED,
    AccountLossGuard,
)
from liquidity_migration.venue.protection_engine import AccountProtectionEngine
from liquidity_migration.account.post_fill_markouts import PostFillMarkoutObserver
from liquidity_migration.venue.venue_protection import AccountHealthChain, BybitNativeProtectionManager
from liquidity_migration.venue.venue_instrument_rules import load_venue_rules_bytes
from liquidity_migration.core.venue_realm import REALM_CREDENTIAL_VARIABLES, VenueRealm, venue_realm

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

    Reduction admission consumes the position timestamp, so running it last
    keeps accounting work from aging a just-taken venue snapshot.
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

    A single REST read timeout is retryable: the health chain's recency
    requirement fails closed on its own if refreshes keep failing, whereas
    dying here would take down execution, protection, and health publishing
    together. Startup reconciliation stays strict and skips this wrapper.
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
    sync failures still abort startup. A definite crossed-stop breach does not:
    refusing to start would disable the only owner able to execute the strict
    reduce-only recovery request.
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

    Generous: a journal position, a venue position, or a working order all
    count, and an unreadable venue counts as exposure.
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

    ``Restart=always`` plus a strict startup check is a 2-second crash loop
    during which nothing runs -- no reconciliation, no protection sync, no
    health publication, and no exit path -- while the position sits at the
    venue behind only its native stop.

    With nothing open, exiting is right: a flat account with a broken startup
    check should fail loudly rather than publish BLOCKED forever.

    Degrading is not adoption. The owner enters its normal loop with the
    failure latched into health, which already refuses every
    exposure-increasing batch, while reductions and the safety flat stay
    available.
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
#: Looser than the order-placement bound: a stale mark mis-prices a fill, but
#: only delays a stop decision the venue-native stop still backstops. Sits above
#: ordinary reconnect jitter and well below the scale of a real outage.
PROTECTION_MAX_BOOK_AGE_NS = 15 * 1_000_000_000


def protection_market_refs(
    recorder: Any,
    symbols: Iterable[str],
    *,
    max_book_age_ns: int = PROTECTION_MAX_BOOK_AGE_NS,
) -> tuple[dict[str, MarketInputRef], dict[str, str]]:
    """Build component-protection market refs, skipping unusable or stale books.

    ``L2BookSnapshot.market_ref`` raises for gapped, crossed, empty, or invalid
    books; skip the symbol for one cycle (the venue-native stop stays armed)
    rather than letting the exception kill the owner process.

    Age matters because a frozen book passes every structural check: a dropped
    WebSocket delivers no deltas, so reconstruction stays healthy and
    ``sequence_gap`` stays false while price walks away. Without the bound the
    stop/take-profit engine decides against a minutes-old mark with no error,
    no health change, and no alert.

    Age is ``observed_wall_ns - book.local_receive_ts_ns`` under the recorder's
    lock, so a negative value is a clock regression, not a read race, and
    counts as unusable.
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

    Native-protection health can be blocked while venue and local quantities
    agree; those are two distinct facts.
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


#: Reconciliation passes a fresh venue/local disagreement gets to resolve
#: itself before it is reported as a fault, and the floor for that bound.
POSITION_TRUTH_SETTLE_RECONCILE_PASSES = 15
POSITION_TRUTH_SETTLE_FLOOR_NS = 30 * 1_000_000_000


class PositionTruthSettling:
    """Hold a fresh venue/local disagreement briefly before calling it a fault.

    The venue's REST position view lags a fill: the kernel journals it on
    reconstruction while Bybit returns the pre-fill quantity for a few seconds.
    ``require_recent_symbols_consistent`` compares the CURRENT kernel position
    against the LAST venue snapshot, so that gap reads as a contradiction after
    every fill even though nothing is wrong.

    The reduction admission gate still treats it as a hard stop -- it is about
    to trade against evidence it does not have -- but a lifecycle message must
    not, or every reduction is announced and then retracted seconds later.

    The window is wall time since the disagreement began, reset only by
    agreement, so a disagreement whose details keep changing cannot hold the
    clock open. Anything surviving the window is reported in full with its
    original cause, and a backwards clock reports immediately.

    Observed retractions ran under 14s, median 7; the 30-second floor is about
    twice the worst case, and the pass-count bound scales it if reconciliation
    slows down.
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


def _live_gross_notional_usdt(kernel: Any, recorder: Any) -> float:
    """Gross notional of the reconstructed book at the freshest observed marks.

    From *positions*, not target quantities: targets cannot see exposure the
    book acquired outside the kernel's own commands.
    """

    total = 0.0
    for symbol, position in kernel._state_ref().positions.items():
        if position.signed_qty == 0.0:
            continue
        price = 0.0
        book, _observed_ns = recorder.current_book_with_observed_wall_ns(symbol)
        if book is not None:
            try:
                price = float(
                    book.market_ref(
                        input_key=f"ceiling:{symbol}:{book.sequence}",
                        source="bybit_raw_l2",
                    ).reference_price
                )
            except ValueError:
                price = 0.0
        if price <= 0.0:
            # No usable mark: fall back to the position's own entry basis
            # rather than silently valuing an open position at zero.
            price = abs(float(position.average_price or 0.0))
        total = math.fsum((total, abs(position.signed_qty) * price))
    return total


def _load_equity_anchored_envelope(path: str) -> EquityAnchoredEnvelope | None:
    """Build the envelope when the risk policy is a full operational profile.

    The flat policy shape has no ratios to anchor and keeps its fixed caps.
    """

    try:
        profile = load_operational_profile(path)
    except (ValueError, OSError, RuntimeError):
        return None
    if not profile.capital_reference.tracks_equity:
        return None
    return EquityAnchoredEnvelope(profile)


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
    venue_facts_at_ns: int,
    venue_facts_healthy: bool,
    invocation_id: str,
    environment: str = ExecutionEnvironment.DEMO.value,
    last_batch_id: str = "",
    detail: str = "",
) -> AccountOwnerHealth:
    """Publish owner health bound to canonical state and wallet capital.

    ``environment`` labels which owner this is and defaults to demo, so a
    mainnet owner must pass its own or be alerted on as part of the demo fleet.
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
        venue_facts_at_ns=venue_facts_at_ns,
        venue_facts_healthy=venue_facts_healthy,
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

    Reconciliation and private execution advance the journal faster than the
    health interval, so any new head republishes health rather than making
    exact-head consumers race journal traffic. Journal-only refreshes reuse the
    last wallet snapshot; only a completed request, status change, or elapsed
    interval spends another wallet REST call.
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
        help=(
            "Fail startup when empirical demo order-rule receipts are older than this. "
            "Mainnet caps this at the registered 168 hours; demo takes any positive value."
        ),
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
        help=(
            "Report owner health blocked while the durable queue head lacks healthy "
            "fresh L2 for longer than this. Mainnet caps it at the registered 30 seconds."
        ),
    )
    parser.add_argument(
        "--max-unsubmitted-exposure-age-seconds",
        type=float,
        default=120.0,
        help=(
            "Refuse a never-attempted exposure-increasing command older than this. "
            "Command age is anchored to the shared batch journal instant, so the "
            "budget must cover whole-batch venue latency, not one round trip."
        ),
    )
    parser.add_argument(
        "--entry-quote-window-seconds",
        type=float,
        default=120.0,
        help=(
            "Rest exposure-increasing entries as a limit order at the touch for "
            "up to this long before crossing the spread (0 disables and every "
            "entry is a market order again). The 120s default is the arm the "
            "2026-08-03 overnight quote lab measured at a 70%% passive fill rate."
        ),
    )
    parser.add_argument(
        "--entry-clip-touch-fraction",
        type=float,
        default=1.0,
        help=(
            "Rest at most this fraction of the quantity already displayed at "
            "the touch per quote window (0 disables clipping). A larger entry "
            "arrives as a sequence of touch-sized windows via convergence "
            "re-planning; measured 2026-08-04, the thin half of the universe "
            "displays only 23-181 USDT at the touch."
        ),
    )
    parser.add_argument(
        "--entry-clip-min-notional-usdt",
        type=float,
        default=100.0,
        help=(
            "Floor for one window's resting clip, so a near-empty touch "
            "cannot stretch one entry into hundreds of windows."
        ),
    )
    parser.add_argument("--idle-seconds", type=float, default=0.1)
    parser.add_argument(
        "--confirm-demo-orders",
        action="store_true",
        help=(
            "deprecated no-op: order submission is gated by the named realm and "
            "its credentials. Still accepted so a deployed wrapper keeps starting."
        ),
    )
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
    if (
        not math.isfinite(args.max_unsubmitted_exposure_age_seconds)
        or args.max_unsubmitted_exposure_age_seconds <= 0.0
    ):
        parser.error("--max-unsubmitted-exposure-age-seconds must be positive and finite")
    if not math.isfinite(args.entry_quote_window_seconds) or args.entry_quote_window_seconds < 0.0:
        parser.error("--entry-quote-window-seconds must be zero or a positive finite number")
    if not math.isfinite(args.entry_clip_touch_fraction) or args.entry_clip_touch_fraction < 0.0:
        parser.error("--entry-clip-touch-fraction must be zero or a positive finite number")
    if not math.isfinite(args.entry_clip_min_notional_usdt) or args.entry_clip_min_notional_usdt < 0.0:
        parser.error("--entry-clip-min-notional-usdt must be zero or a positive finite number")
    if not math.isfinite(args.continuous_cycle_max_age_minutes) or args.continuous_cycle_max_age_minutes <= 0.0:
        parser.error("--continuous-cycle-max-age-minutes must be positive and finite")
    if not math.isfinite(args.private_ws_reconnect_seconds) or args.private_ws_reconnect_seconds <= 0.0:
        parser.error("--private-ws-reconnect-seconds must be positive and finite")
    realm = venue_realm(args.realm)
    # Mainnet holds every registered startup bound. Demo only needs values that
    # are finite and positive, so a stale rule receipt or a slow reconnecting
    # link is an operator flag away from starting rather than a code deploy.
    mainnet = realm is VenueRealm.MAINNET
    try:
        args.max_demo_rule_age_hours = require_registered_demo_rule_max_age_hours(
            args.max_demo_rule_age_hours,
            enforce_registered_ceiling=mainnet,
        )
        args.request_market_warmup_timeout_seconds = require_registered_request_market_warmup_timeout(
            args.request_market_warmup_timeout_seconds,
            enforce_registered_ceiling=mainnet,
        )
    except ValueError as exc:
        parser.error(str(exc))
    invocation_id = require_systemd_invocation_id(require_systemd=mainnet)

    # The realm is baked into the on-disk route identity and
    # ensure_account_route refuses to rewrite an existing one, so each realm
    # gets its own journal root with no migration path between them.
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
        # The demo receipt comes from an order-placing probe. Off demo, rules
        # come from read-only instruments-info, and the loader refuses a
        # receipt bound to any other realm.
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
    # In ``account_equity`` mode the six absolute caps are a fraction of the
    # observed wallet. The envelope owns the rebase discipline; the kernel's
    # policy is re-read from it after every capital refresh.
    envelope = _load_equity_anchored_envelope(args.risk_policy_file)
    if envelope is not None:
        policy = envelope.policy()
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
    # No unowned regular or conditional order may exist before a stream starts
    # or a request is claimed, so a new journal requires an empty venue book.
    startup_degradations: list[StartupDegradation] = []
    try:
        require_bybit_order_ownership(
            client=private_client,
            kernel=kernel,
            native_order_verifier=native_protection.is_verified_native_order,
        )
    except Exception as exc:  # noqa: BLE001 - classified by degrade_or_raise
        if mainnet:
            startup_degradations.append(
                degrade_or_raise(
                    stage="venue order ownership",
                    error=exc,
                    kernel=kernel,
                    report=None,
                )
            )
        else:
            # An owner that refuses to start cannot cancel the stray order it
            # refused over. The 2s reconciler runs the same check every cycle
            # and marks health, which blocks entries and still permits exits.
            _logger.warning(
                "unowned venue orders at startup on realm %s; starting anyway so "
                "reconciliation can adopt or cancel them: %s: %s",
                realm.value,
                type(exc).__name__,
                str(exc)[:900],
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
    # Bootstrap venue truth before any request is claimed. Venue exposure with
    # an empty kernel is a hard mismatch, never auto-adopted.
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
    snapshot_provider = BybitAccountSnapshotProvider(private_client)

    def _recorder_touch(symbol: str) -> tuple[float, float, float, float] | None:
        """The owner's own reconstructed book: touch with displayed sizes."""

        book = recorder.current_book(symbol, depth=1)
        if book is None or book.sequence_gap or not book.bids or not book.asks:
            return None
        best_bid, best_ask = book.bids[0], book.asks[0]
        if best_bid.price <= 0.0 or best_ask.price <= best_bid.price:
            return None
        return best_bid.price, best_ask.price, best_bid.qty, best_ask.qty

    entry_quotes = (
        EntryQuoteManager(
            private_client,
            config=EntryQuoteConfig(
                window_seconds=args.entry_quote_window_seconds,
                clip_touch_fraction=args.entry_clip_touch_fraction,
                min_clip_notional_usdt=args.entry_clip_min_notional_usdt,
            ),
            instrument_rules=rules,
            kernel=kernel,
            clock=SystemClock(),
            entry_stop_verifier=native_protection.verify_entry_attached_stop,
            touch_source=_recorder_touch,
        )
        if args.entry_quote_window_seconds > 0.0
        else None
    )
    service = AccountExecutionService(
        route=route,
        kernel=kernel,
        market_provider=CapturedBybitMarketProvider(recorder),
        snapshot_provider=snapshot_provider,
        rules_provider=VerifiedBybitDemoRulesProvider(rules, environment=realm.value),
        risk_policy=policy,
        execution_adapter=BybitDemoExecutionAdapter(
            private_client,
            # Read position truth back at the create boundary instead of
            # trusting atomic arming until the next reconcile.
            entry_stop_verifier=native_protection.verify_entry_attached_stop,
            max_unsubmitted_exposure_age_ns=int(
                args.max_unsubmitted_exposure_age_seconds * 1_000_000_000
            ),
            entry_quotes=entry_quotes,
        ),
        resting_entry_quotes=(
            entry_quotes.symbol_has_active_quote if entry_quotes is not None else None
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
            heading=f"Bybit {realm.value}",
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
    # Account-level daily loss halt; the ceiling rides on the risk policy.
    # Absent or zero leaves the machinery running and observable but never
    # tripping.
    loss_guard = AccountLossGuard(
        max_daily_loss_usdt=(
            policy.max_daily_loss_usdt if policy.max_daily_loss_usdt > 0.0 else None
        )
    )
    envelope_rebase_detail = ""
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
            if entry_quotes is not None:
                # Advance resting entry quotes on the loop cadence: reprice
                # toward a moved touch, cross at the window deadline, verify
                # the attached stop on fill. Never raises.
                entry_quotes.advance()
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
                # Warm all queued symbols in parallel; the queue-head gate
                # below still holds the registered 30s timeout.
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
                # not run, which is exactly when no new exposure should open, so
                # it must reach health and not only journald. The venue-native
                # stop is unaffected either way.
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
            # From the reconciler, not from this loop's clock: last_report is
            # only replaced by a pass that actually reached the venue, so a
            # loop whose REST reads keep failing keeps publishing health while
            # this timestamp ages out. Startup guarantees a report exists
            # before the loop is entered (both startup passes raise on
            # failure).
            venue_facts_report = reconciler.last_report
            assert venue_facts_report is not None
            reconcile_healthy = bool(venue_facts_report.healthy)
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
                # A startup check failed over open exposure and the owner
                # stayed up. One fully healthy pass supersedes it; until then
                # the failure rides on health, refusing new exposure while
                # exits and the safety flat stay available.
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
                        # Do not report a completed request as returned to
                        # pending just because native protection is still
                        # converging; reconciliation owns the next proof.
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
                # A blocked request retries every few seconds: one traceback
                # per distinct cause, one line per pass.
                failure_signature = f"{type(exc).__name__}: {exc}"[:500]
                if failure_signature != last_request_failure_signature:
                    last_request_failure_signature = failure_signature
                    if isinstance(exc, StaleEntryRequestExpired):
                        _logger.exception(
                            "account request retired to failed/ (every entry signal expired)"
                        )
                    else:
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
                if envelope is not None:
                    rebase = envelope.observe_equity(last_capital_snapshot.equity_usdt)
                    if rebase is not None:
                        service.risk_policy = envelope.policy()
                        policy = service.risk_policy
                        # The ceiling scales with the envelope, but the guard
                        # keeps its own day anchor: rebasing must not refresh a
                        # partly spent loss budget.
                        loss_guard.max_daily_loss_usdt = (
                            policy.max_daily_loss_usdt
                            if policy.max_daily_loss_usdt > 0.0
                            else None
                        )
                        envelope_rebase_detail = ""
                    if envelope.last_error:
                        envelope_rebase_detail = envelope.last_error
                if envelope_rebase_detail:
                    health_status = AccountOwnerHealthStatus.BLOCKED
                    health_detail = _append_health_error(
                        health_detail, envelope_rebase_detail
                    )[:1000]
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
                    # Publish once: republishing would queue redundant flats.
                    # run_safety_flat_once is the same all-flat path a native
                    # breach uses, so a BLOCKED owner can still close.
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
                    venue_facts_at_ns=venue_facts_report.observed_ts_ns,
                    venue_facts_healthy=reconcile_healthy,
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
                # Narrower than owner health: a missing native stop blocks
                # health but must not make matching quantities report a
                # disagreement. From reconciliation alone.
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
                # Reporting only; the admission gate calls the reconciler.
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
                        # running, so render no line rather than a permanent
                        # "unavailable" or ever-growing "STALE" fault.
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
