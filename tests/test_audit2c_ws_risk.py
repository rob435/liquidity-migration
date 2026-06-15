"""audit2c: ws_risk telegram-dedupe un-record runs on the CONSUMER thread.

The background sender thread must not mutate consumer-only state
(state.telegram_keys_sent) or race the dedupe file; on a failed send it hands the
key back via a queue and the consumer un-records it.
"""

from __future__ import annotations

from liquidity_migration import ws_risk
from liquidity_migration.config import ResearchConfig
from liquidity_migration.ws_risk import EventWebSocketRiskConfig, EventWebSocketRiskEngine


def test_drain_failed_telegram_keys_unrecords_on_consumer_thread(tmp_path) -> None:
    engine = EventWebSocketRiskEngine(
        tmp_path, config=ResearchConfig(), risk_config=EventWebSocketRiskConfig(telegram=True)
    )
    try:
        engine.state.telegram_keys_sent.add("k-x")
        ws_risk._write_telegram_dedupe_keys(engine.report_dir, engine.state.telegram_keys_sent)
        # Sender thread hands a failed key back; consumer drains + un-records it.
        engine._telegram_failed_keys.put("k-x")
        engine._drain_failed_telegram_keys()
        assert "k-x" not in engine.state.telegram_keys_sent
        assert "k-x" not in set(ws_risk._read_telegram_dedupe_keys(engine.report_dir))
    finally:
        engine.close()


def test_drain_failed_telegram_keys_noop_when_queue_empty(tmp_path) -> None:
    engine = EventWebSocketRiskEngine(
        tmp_path, config=ResearchConfig(), risk_config=EventWebSocketRiskConfig(telegram=True)
    )
    try:
        engine.state.telegram_keys_sent.add("keep")
        ws_risk._write_telegram_dedupe_keys(engine.report_dir, engine.state.telegram_keys_sent)
        engine._drain_failed_telegram_keys()  # nothing queued -> no change
        assert "keep" in engine.state.telegram_keys_sent
        assert "keep" in set(ws_risk._read_telegram_dedupe_keys(engine.report_dir))
    finally:
        engine.close()
