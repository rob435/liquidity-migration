"""`scripts/runtime/engine_status.py`: one heartbeat as health, exposure, blockers."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "runtime" / "engine_status.py"
DEV = ROOT / "scripts" / "dev.sh"
NOW_MS = 1_757_500_004_000

FULL: dict[str, Any] = {
    "wall_ts_ms": 1_757_500_000_000,
    "may_open": False,
    "private_stream_ready": False,
    "private_stream_unready_ms": 4213,
    "rolling_loss_tripped": True,
    "rolling_loss_net_usdt": -41.5,
    "rolling_loss_limit_usdt": 40,
    "account_equity_usdt": 1023.75,
    "account_available_usdt": 0,
    "account_observed_wall_ts_ms": 1_757_499_998_000,
    "uptime_s": 7200,
    "engine_commit": "abc1234",
    "strategy_errors": [{"strategy": "carry", "error": "instrument catalog stale"}],
    "strategy_entries_enabled": [
        {"strategy": "carry", "entries_enabled": True},
        {"strategy": "long", "entries_enabled": False},
    ],
    "positions": [
        {"symbol": "NEARUSDT", "side": "Buy", "qty": 120, "entry_px": 2.1, "strategy": "carry"},
        {"symbol": "ZECUSDT", "side": "Sell", "qty": 3.5, "entry_px": 40.2, "strategy": None},
    ],
    "working_entries": [{"strategy": "long", "symbol": "BTCUSDT"}],
    "pending_flatten_requests": [{"strategy": "long", "request_id": "r-1"}],
    "entry_blockers": [
        {"strategy": "carry", "symbol": "NEARUSDT", "reason": "rolling_loss_tripped"},
        {"strategy": "long", "symbol": "BTCUSDT", "reason": "rolling_loss_tripped"},
        {"strategy": "long", "symbol": "ETHUSDT", "reason": "quote_stale"},
    ],
}


def _run(path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path), "--now-ms", str(NOW_MS), *extra],
        text=True,
        capture_output=True,
        check=False,
    )


def _heartbeat(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_all_three_sections_render_the_heartbeat_the_engine_wrote(tmp_path: Path) -> None:
    result = _run(_heartbeat(tmp_path, FULL))
    assert result.returncode == 0, result.stderr
    out = result.stdout

    assert out.index("HEALTH") < out.index("EXPOSURE") < out.index("BLOCKERS")

    # HEALTH: the age is derived from --now-ms, not from the clock.
    assert "heartbeat age s" in out and "4.0" in out
    assert "may_open" in out and "no" in out
    assert "ready=no unready_ms=4213" in out
    assert "tripped=yes net=-41.5 limit=40 USDT" in out
    # A millisecond stamp stays a whole number; %g would print 1.7575e+12.
    assert "observed_wall_ts_ms=1757499998000" in out
    assert "1.7575e+12" not in out
    # Zero is a reading and prints as zero, never as unknown.
    assert "available=0 " in out
    assert "carry=yes long=no" in out
    assert "carry: instrument catalog stale" in out
    assert "long: r-1" in out
    assert "7200" in out and "abc1234" in out

    # EXPOSURE
    assert "NEARUSDT" in out and "qty=120" in out and "strategy=carry" in out
    assert "ZECUSDT" in out and "qty=3.5" in out
    assert "BTCUSDT long" in out

    # BLOCKERS: grouped by reason, each group counted.
    assert "rolling_loss_tripped (2)" in out
    assert "carry/NEARUSDT long/BTCUSDT" in out
    assert "quote_stale (1)" in out
    assert "long/ETHUSDT" in out
    # Every group label keeps a separator even when it overruns its column.
    assert "(2)carry" not in out


def test_absent_fields_print_unknown_and_never_zero(tmp_path: Path) -> None:
    result = _run(_heartbeat(tmp_path, {"may_open": True}))
    assert result.returncode == 0, result.stderr
    out = result.stdout

    for label in (
        "heartbeat age s",
        "private stream",
        "rolling loss",
        "account",
        "entries enabled",
        "strategy errors",
        "pending flatten",
        "uptime s",
        "engine commit",
        "positions",
        "working entries",
        "entry blockers",
    ):
        line = next(row for row in out.splitlines() if row.strip().startswith(label))
        assert "unknown" in line, line
    assert "may_open" in out
    # No absent reading is ever spelled as a zero.
    for line in out.splitlines():
        if "unknown" in line:
            assert " 0" not in line.replace("unknown", ""), line


def test_an_empty_list_is_a_reading_not_an_unknown(tmp_path: Path) -> None:
    payload = {
        "wall_ts_ms": NOW_MS,
        "positions": [],
        "working_entries": [],
        "entry_blockers": [],
        "strategy_errors": [],
    }
    out = _run(_heartbeat(tmp_path, payload)).stdout
    assert "flat" in out
    assert "working entries" in out
    assert "entry blockers" in out
    assert "none" in out


def test_a_missing_or_unparseable_heartbeat_is_one_line_and_exit_one(tmp_path: Path) -> None:
    missing = _run(tmp_path / "absent.json")
    assert missing.returncode == 1
    assert missing.stderr.splitlines() == ["heartbeat unreadable: No such file or directory"]
    assert missing.stdout == ""

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    result = _run(broken)
    assert result.returncode == 1
    assert len(result.stderr.splitlines()) == 1
    assert result.stderr.startswith("heartbeat unreadable:")

    not_object = _heartbeat(tmp_path, [1, 2, 3])
    result = _run(not_object)
    assert result.returncode == 1
    assert result.stderr.strip() == "heartbeat unreadable: not a JSON object"


def test_the_script_is_type_checked_and_documented() -> None:
    assert "scripts/runtime/engine_status.py" in DEV.read_text(encoding="utf-8")
    readme = (ROOT / "scripts" / "README.md").read_text(encoding="utf-8")
    assert "runtime/engine_status.py" in readme


def test_verify_reads_every_realms_engine_heartbeat_read_only() -> None:
    body = (ROOT / "scripts" / "vps" / "deploy_remote.sh").read_text(encoding="utf-8")
    assert "report_engine_status" in body
    assert 'python3 "$REPO_DIR/scripts/runtime/engine_status.py" "$heartbeat" || true' in body
    # The heartbeat path comes from the manifest, not from a second path table.
    assert 'heartbeat="$(lm_output_artifact_for_unit "$unit" 2>/dev/null || true)"' in body
