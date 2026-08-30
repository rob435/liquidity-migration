"""Lifecycle loop for the independent Exodus target producer."""

from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path
from types import FrameType
from typing import Any, Callable

from liquidity_migration.strategy.exodus_producer import (
    ExodusEffectiveConfig,
    format_exodus_cycle_summary,
    run_exodus_cycle,
)
from liquidity_migration.strategy.target_book_evidence import PublishedTargetCyclePayload
from liquidity_migration.core.env_flags import validate_systemd_invocation_id
from liquidity_migration.strategy.strategy_cycle_health import (
    StrategyCycleHealth,
    write_strategy_cycle_health,
)


_logger = logging.getLogger(__name__)


def exodus_wait_seconds(
    payload: dict[str, Any],
    *,
    interval_seconds: float,
    now_ms: int | None = None,
) -> float:
    """Bound the polling wait by the next owned cover clock."""

    wait = max(float(interval_seconds), 0.0)
    raw_deadline = payload.get("next_cover_ts_ms")
    if isinstance(raw_deadline, bool) or not isinstance(raw_deadline, int):
        return wait
    current_ms = int(now_ms if now_ms is not None else time.time_ns() // 1_000_000)
    return min(wait, max(0.0, (raw_deadline - current_ms) / 1_000.0))


class ExodusProducerDaemon:
    """Run isolated Exodus cycles until systemd asks the process to stop."""

    def __init__(
        self,
        data_root: str | Path,
        *,
        config: ExodusEffectiveConfig,
        interval_seconds: float = 60.0,
        cycle_runner: Callable[..., PublishedTargetCyclePayload] = run_exodus_cycle,
    ) -> None:
        if interval_seconds < 0.0:
            raise ValueError("Exodus interval_seconds cannot be negative")
        self.data_root = Path(data_root).expanduser()
        self.config = config
        self.interval_seconds = float(interval_seconds)
        self.cycle_runner = cycle_runner
        self._stop = threading.Event()
        raw_invocation_id = config.invocation_id or None
        self._invocation_id = (
            validate_systemd_invocation_id(
                raw_invocation_id,
                label="Exodus producer INVOCATION_ID",
            )
            if raw_invocation_id is not None
            else None
        )

    def install_signal_handlers(self) -> None:
        def stop(_signal: int, _frame: FrameType | None) -> None:
            self._stop.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

    def run(self) -> dict[str, Any]:
        cycles_run = 0
        cycle_errors = 0
        while not self._stop.is_set():
            wait_seconds = self.interval_seconds
            try:
                payload = self.cycle_runner(self.data_root, config=self.config)
                if self._invocation_id is not None:
                    write_strategy_cycle_health(
                        self.data_root,
                        StrategyCycleHealth(
                            sleeve="exodus",
                            environment=self.config.environment,
                            cycle_id=str(payload["cycle_id"]),
                            cycle_ts_ms=int(payload["ts_ms"]),
                            completed_ts_ns=time.time_ns(),
                            invocation_id=self._invocation_id,
                            ws_kline_store_rows=None,
                        ),
                    )
                cycles_run += 1
                print(format_exodus_cycle_summary(payload), flush=True)
                wait_seconds = exodus_wait_seconds(
                    payload,
                    interval_seconds=self.interval_seconds,
                )
            except Exception:  # noqa: BLE001 - a daemon retries the durable transition
                cycle_errors += 1
                _logger.exception("Exodus cycle failed; the last target book remains active")
            self._stop.wait(wait_seconds)
        return {"cycles_run": cycles_run, "cycle_errors": cycle_errors}
