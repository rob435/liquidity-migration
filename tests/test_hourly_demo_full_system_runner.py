from __future__ import annotations

from scripts.run_hourly_demo_full_system_test import _compact, _demo_execution_resolved


def test_compact_cycle_payload_preserves_fast_protection_evidence() -> None:
    payload = {
        "now": "2026-05-07T07:15:02+00:00",
        "rows": {"sleeves": 1},
        "summary": {"accepted": 12},
        "sleeves": [
            {
                "sleeve": "rank_31_plus",
                "status": "ok",
                "data_root": "/tmp/run/forward_sleeves/rank_31_plus",
                "rows": {"ledger_orders": 12},
                "summary": {"accepted": 12},
                "fast_protection": {
                    "now": "2026-05-07T07:15:02+00:00",
                    "reason": "completed",
                    "symbols": ["ABCUSDT"],
                    "rows": {
                        "active_trades": 1,
                        "trigger_events": 1,
                        "submitted_or_unknown": 1,
                        "callback_count": 42,
                    },
                    "latency_ms": {"p50": 0.1, "p95": 0.3, "p99": 0.5},
                    "events": [
                        {
                            "symbol": "ABCUSDT",
                            "reason": "mfe_giveback",
                            "decision": "accepted",
                            "order_link_id": "r31p-demo-exit-abc",
                        }
                    ],
                },
            }
        ],
    }

    compacted = _compact(payload)

    fast = compacted["sleeves"][0]["fast_protection"]
    assert fast["rows"]["trigger_events"] == 1
    assert fast["rows"]["submitted_or_unknown"] == 1
    assert fast["rows"]["callback_count"] == 42
    assert fast["events"][0]["reason"] == "mfe_giveback"


def test_demo_execution_resolved_requires_demo_exits_after_paper_close() -> None:
    unresolved = {
        "summary": {
            "paper_trades": 5,
            "paper_closed_trades": 5,
            "demo_entries_filled": 5,
            "demo_exits_filled": 1,
            "demo_slices_open": 0,
        }
    }
    resolved = {
        "summary": {
            "paper_trades": 5,
            "paper_closed_trades": 5,
            "demo_entries_filled": 5,
            "demo_exits_filled": 5,
            "demo_slices_open": 0,
        }
    }

    assert _demo_execution_resolved(unresolved) is False
    assert _demo_execution_resolved(resolved) is True
