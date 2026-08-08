"""Bounded bridge from canonical demo fills to market markout schedules.

The private execution consumer only enqueues an execution id. The account-owner
loop later resolves that id from canonical kernel state and performs capture
I/O, so diagnostics cannot delay fill accounting or venue reconciliation.
"""

from __future__ import annotations

import logging
import queue
from collections import OrderedDict
from dataclasses import dataclass

from liquidity_migration.account.account_kernel import AccountExecutionKernel
from liquidity_migration.account.market_capture import (
    DEFAULT_POST_FILL_MARKOUT_HORIZONS_NS,
    MAX_PENDING_POST_FILL_MARKOUTS,
    SequenceAwareMarketRecorder,
)


_logger = logging.getLogger(__name__)
MAX_PENDING_FILL_REGISTRATIONS = max(
    MAX_PENDING_POST_FILL_MARKOUTS // len(DEFAULT_POST_FILL_MARKOUT_HORIZONS_NS),
    1,
)
MAX_FILL_REGISTRATIONS_PER_DRAIN = 128


@dataclass(frozen=True, slots=True)
class MarkoutDrainReport:
    dequeued: int
    registered: int
    capacity_rejected: int
    duplicates: int
    missing_executions: int
    failed: int


class PostFillMarkoutObserver:
    """Keep markout capture off the private execution mutation path."""

    def __init__(
        self,
        *,
        kernel: AccountExecutionKernel,
        recorder: SequenceAwareMarketRecorder,
    ) -> None:
        self.kernel = kernel
        self.recorder = recorder
        self._queue: queue.Queue[str] = queue.Queue(
            maxsize=MAX_PENDING_FILL_REGISTRATIONS
        )
        self._registered: OrderedDict[str, None] = OrderedDict()
        self.dropped_registrations = 0

    def notify(self, execution_id: str) -> bool:
        """Queue one committed execution without blocking the caller."""

        execution_id = str(execution_id).strip()
        if not execution_id:
            return False
        try:
            self._queue.put_nowait(execution_id)
        except queue.Full:
            self.dropped_registrations += 1
            _logger.error(
                "post-fill markout registration queue is full; execution=%s",
                execution_id,
            )
            return False
        return True

    def drain(
        self,
        *,
        max_items: int = MAX_FILL_REGISTRATIONS_PER_DRAIN,
    ) -> MarkoutDrainReport:
        """Resolve queued ids from canonical state and register bounded marks."""

        dequeued = registered = capacity_rejected = duplicates = missing = failed = 0
        for _index in range(max(int(max_items), 0)):
            try:
                execution_id = self._queue.get_nowait()
            except queue.Empty:
                break
            dequeued += 1
            if execution_id in self._registered:
                self._registered.move_to_end(execution_id)
                duplicates += 1
                continue
            state = self.kernel._state_ref()
            execution = state.executions.get(execution_id)
            if execution is None:
                missing += 1
                _logger.error(
                    "post-fill markout registration lacks canonical execution=%s",
                    execution_id,
                )
                continue
            command_id = str(execution.get("command_id") or "")
            order = state.orders.get(command_id)
            if order is None:
                missing += 1
                _logger.error(
                    "post-fill markout registration lacks command=%s execution=%s",
                    command_id,
                    execution_id,
                )
                continue
            try:
                schedule = self.recorder.register_post_fill_markouts(
                    execution_id=execution_id,
                    command_id=command_id,
                    symbol=order.symbol,
                    signed_qty=float(execution.get("signed_qty") or 0.0),
                    fill_price=float(execution.get("price") or 0.0),
                    fill_exchange_ts_ns=int(
                        execution.get("exchange_ts_ns") or 0
                    ),
                    fill_local_receive_ts_ns=int(
                        execution.get("local_receive_ts_ns") or 0
                    ),
                )
            except Exception:  # noqa: BLE001 - diagnostics never block account ownership
                failed += 1
                _logger.exception(
                    "post-fill markout registration failed execution=%s",
                    execution_id,
                )
                continue
            if str(schedule.get("schedule_status") or "") == "rejected_capacity":
                capacity_rejected += 1
                _logger.warning(
                    "post-fill markout schedule rejected at capacity; execution=%s",
                    execution_id,
                )
            self._registered[execution_id] = None
            self._registered.move_to_end(execution_id)
            while len(self._registered) > MAX_PENDING_FILL_REGISTRATIONS:
                self._registered.popitem(last=False)
            registered += 1
        return MarkoutDrainReport(
            dequeued=dequeued,
            registered=registered,
            capacity_rejected=capacity_rejected,
            duplicates=duplicates,
            missing_executions=missing,
            failed=failed,
        )
