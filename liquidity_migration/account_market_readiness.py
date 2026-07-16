"""Strict queue-head market-data readiness shared by demo and paper owners."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .account_service import (
    AccountExecutionService,
    AccountIntentInbox,
    AccountServiceReceipt,
)
from .market_capture import SequenceAwareMarketRecorder


REGISTERED_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS = 30.0


def require_registered_request_market_warmup_timeout(value: object) -> float:
    try:
        seconds = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("request market warmup timeout must be numeric") from exc
    if (
        not math.isfinite(seconds)
        or seconds <= 0.0
        or seconds > REGISTERED_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "request market warmup timeout cannot exceed the registered 30 seconds"
        )
    return seconds


@dataclass(frozen=True, slots=True)
class RequestedMarketReadiness:
    request_id: str
    symbols: tuple[str, ...]
    ready: bool
    timed_out: bool
    detail: str


@dataclass(slots=True)
class RequestedMarketWarmupGate:
    """Gate the durable queue head on an exact healthy captured L2 book.

    The timeout is deliberately latched for the current request. A late
    snapshot cannot silently reopen the owner epoch after the readiness SLA was
    missed; the request remains pending and owner health stays blocked.
    """

    timeout_seconds: float
    _request_id: str = ""
    _started_monotonic: float = 0.0
    _timed_out: bool = False
    _waiting: bool = False
    _terminal_detail: str = ""

    def __post_init__(self) -> None:
        self.timeout_seconds = require_registered_request_market_warmup_timeout(
            self.timeout_seconds
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
            self._timed_out = False
            self._waiting = False
            self._terminal_detail = ""
            return RequestedMarketReadiness("", (), True, False, "")

        if request.request_id != self._request_id:
            self._request_id = request.request_id
            self._started_monotonic = now_monotonic
            self._timed_out = False
            self._waiting = False
            self._terminal_detail = ""
        symbols = tuple(sorted({item.intent.symbol.upper() for item in request.intents}))
        if self._timed_out:
            return RequestedMarketReadiness(
                request.request_id,
                symbols,
                False,
                True,
                self._terminal_detail,
            )
        missing_rules = sorted(set(symbols) - verified_rule_symbols)
        if missing_rules:
            self._timed_out = True
            self._terminal_detail = (
                "queue head lacks demo-verified rules; owner epoch is closed and "
                "the request remains pending: " + ", ".join(missing_rules)
            )
            return RequestedMarketReadiness(
                request.request_id,
                symbols,
                False,
                True,
                self._terminal_detail,
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

        elapsed = max(now_monotonic - self._started_monotonic, 0.0)
        if elapsed >= self.timeout_seconds and (issues or self._waiting):
            self._timed_out = True
            self._terminal_detail = (
                f"queue-head market warmup timed out after {self.timeout_seconds:g}s; "
                "request remains pending and owner epoch is closed: "
                + ", ".join(symbols)
            )
        if self._timed_out:
            return RequestedMarketReadiness(
                request.request_id,
                symbols,
                False,
                True,
                self._terminal_detail,
            )
        if issues:
            self._waiting = True
            return RequestedMarketReadiness(
                request.request_id,
                symbols,
                False,
                False,
                "waiting for queue-head market data: " + ", ".join(issues),
            )
        self._waiting = False
        return RequestedMarketReadiness(
            request.request_id,
            symbols,
            True,
            False,
            "",
        )


def run_ready_request_or_converge(
    *,
    service: AccountExecutionService,
    inbox: AccountIntentInbox,
    readiness: RequestedMarketReadiness,
) -> AccountServiceReceipt | None:
    """Claim only a proved-ready head while preserving prior convergence work.

    Market warmup governs the newly queued request, not deterministic retries
    already committed to the account journal.  No queue head, or a
    missing/stale book on an unrelated head, must therefore leave claims alone
    without starving an earlier convergence plan (especially a risk-reducing
    one).
    """

    if readiness.request_id and readiness.ready:
        return service.run_once(
            inbox,
            expected_request_id=readiness.request_id,
        )
    service.converge_once()
    return None
