"""Strict queue-head market-data readiness for the account owners."""

from __future__ import annotations

import math
from dataclasses import dataclass

from liquidity_migration.account.account_service import (
    AccountExecutionService,
    AccountIntentInbox,
    AccountServiceReceipt,
)
from liquidity_migration.account.market_capture import SequenceAwareMarketRecorder


REGISTERED_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS = 30.0


def require_registered_request_market_warmup_timeout(
    value: object,
    *,
    enforce_registered_ceiling: bool = True,
) -> float:
    """Validate the queue-head warmup timeout.

    Mainnet holds the registered 30-second ceiling. Demo and paper only need a
    finite positive value, so an operator can widen the warmup budget on a slow
    or reconnecting link without a code deploy.
    """

    try:
        seconds = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("request market warmup timeout must be numeric") from exc
    if not math.isfinite(seconds) or seconds <= 0.0:
        raise ValueError("request market warmup timeout must be finite and positive")
    if enforce_registered_ceiling and seconds > REGISTERED_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS:
        raise ValueError("request market warmup timeout cannot exceed the registered 30 seconds")
    return seconds


@dataclass(frozen=True, slots=True)
class RequestedMarketReadiness:
    """One evaluation of the queue head against the live books.

    ``timed_out`` says the head has been unservable for longer than the warmup
    timeout right now. It is a symptom reported for health, not a latch.

    ``carries_new_risk`` is read off the head's own targets, so a halted account
    can refuse it without first waiting for books it will never look at.
    """

    request_id: str
    symbols: tuple[str, ...]
    ready: bool
    timed_out: bool
    detail: str
    carries_new_risk: bool = False
    batch_id: str = ""


@dataclass(slots=True)
class RequestedMarketWarmupGate:
    """Gate the durable queue head on an exact healthy captured L2 book.

    Every cycle re-reads the books. While the head's book is absent, gapped,
    empty, stale, or future-dated the request stays pending and owner health
    stays blocked; the moment those books are healthy the same head becomes
    servable again. Past the warmup timeout the detail says how long the head
    has been unservable, which is loud without costing the owner epoch: a
    200ms book gap must not require a process restart to recover.
    """

    timeout_seconds: float
    _request_id: str = ""
    _started_monotonic: float = 0.0

    def __post_init__(self) -> None:
        self.timeout_seconds = require_registered_request_market_warmup_timeout(
            self.timeout_seconds,
            enforce_registered_ceiling=False,
        )

    def _unservable(
        self,
        request_id: str,
        symbols: tuple[str, ...],
        *,
        reason: str,
        elapsed: float,
        overdue: bool,
        carries_new_risk: bool,
        batch_id: str,
    ) -> RequestedMarketReadiness:
        detail = reason
        if overdue:
            detail += (
                f"; queue head has been unservable for {elapsed:.1f}s "
                f"(warmup timeout {self.timeout_seconds:g}s) and remains pending"
            )
        return RequestedMarketReadiness(
            request_id, symbols, False, overdue, detail, carries_new_risk, batch_id
        )

    def evaluate(
        self,
        *,
        inbox: AccountIntentInbox,
        recorder: SequenceAwareMarketRecorder,
        verified_rule_symbols: set[str],
        now_monotonic: float,
        max_market_age_ns: int,
    ) -> RequestedMarketReadiness:
        if max_market_age_ns < 0:
            raise ValueError("max market age cannot be negative")
        request = inbox.peek_next()
        if request is None:
            self._request_id = ""
            self._started_monotonic = 0.0
            return RequestedMarketReadiness("", (), True, False, "")

        if request.request_id != self._request_id:
            self._request_id = request.request_id
            self._started_monotonic = now_monotonic
        symbols = tuple(sorted({item.intent.symbol.upper() for item in request.intents}))
        carries_new_risk = AccountExecutionService.request_carries_new_risk(request)
        elapsed = max(now_monotonic - self._started_monotonic, 0.0)
        overdue = elapsed >= self.timeout_seconds
        missing_rules = sorted(set(symbols) - verified_rule_symbols)
        if missing_rules:
            return self._unservable(
                request.request_id,
                symbols,
                reason="queue head lacks venue-verified rules: " + ", ".join(missing_rules),
                elapsed=elapsed,
                overdue=overdue,
                carries_new_risk=carries_new_risk,
                batch_id=request.batch_id,
            )

        issues: list[str] = []
        for symbol in symbols:
            book, observed_wall_ns = recorder.current_book_with_observed_wall_ns(symbol)
            if book is None:
                issues.append(f"{symbol}:no_snapshot")
                continue
            if book.sequence_gap:
                issues.append(f"{symbol}:sequence_gap")
            if not book.bids or not book.asks:
                issues.append(f"{symbol}:empty_book")
            age_ns = observed_wall_ns - book.local_receive_ts_ns
            if age_ns < 0:
                issues.append(f"{symbol}:future_book")
            elif age_ns > max_market_age_ns:
                issues.append(f"{symbol}:stale_book")

        if issues:
            return self._unservable(
                request.request_id,
                symbols,
                reason="waiting for queue-head market data: " + ", ".join(issues),
                elapsed=elapsed,
                overdue=overdue,
                carries_new_risk=carries_new_risk,
                batch_id=request.batch_id,
            )
        return RequestedMarketReadiness(
            request.request_id,
            symbols,
            True,
            False,
            "",
            carries_new_risk,
            request.batch_id,
        )


def run_ready_request_or_converge(
    *,
    service: AccountExecutionService,
    inbox: AccountIntentInbox,
    readiness: RequestedMarketReadiness,
) -> AccountServiceReceipt | None:
    """Claim only a proved-ready head while preserving prior convergence work.

    Warmup governs the newly queued request, not retries already committed to
    the journal, so an absent or stale book on an unrelated head must not
    starve an earlier convergence plan.

    One head is claimed while still unservable: a risk-carrying request on a
    halted account. Its refusal reads no book, and leaving it at the head would
    park the account's own all-flat behind a request that can never be served.
    """

    safety_receipt = service.run_safety_flat_once(inbox)
    if safety_receipt is not None:
        return safety_receipt
    refusable_head = (
        not readiness.ready
        and readiness.carries_new_risk
        and bool(service.halted_for_new_risk(batch_id=readiness.batch_id))
    )
    if readiness.request_id and (readiness.ready or refusable_head):
        return service.run_once(
            inbox,
            expected_request_id=readiness.request_id,
        )
    service.converge_once()
    return None
