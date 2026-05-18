from __future__ import annotations

from pathlib import Path


def test_runtime_scripts_do_not_delete_live_cycle_locks() -> None:
    repo = Path(__file__).resolve().parents[1]
    scripts = [
        repo / "scripts" / "run_bybit_demo_event_engine.sh",
        repo / "scripts" / "run_bybit_demo_ws_risk_engine.sh",
    ]

    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert "rm -f \"$DATA_ROOT/.locks/" not in text
        assert "mkdir -p \"$DATA_ROOT/.locks\"" in text


def test_event_entry_runner_default_cadence_is_rate_limit_safe() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "run_bybit_demo_event_engine.sh").read_text(encoding="utf-8")

    assert 'INTERVAL_SECONDS="${INTERVAL_SECONDS:-300}"' in text
    assert "cycle_elapsed_seconds=$(($(date +%s) - cycle_start_epoch))" in text
    assert "starting next cycle immediately" in text


def test_systemd_entry_runner_uses_vps_cadence() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "deploy" / "systemd" / "model050426-bybit-demo.service").read_text(
        encoding="utf-8"
    )

    assert "Environment=INTERVAL_SECONDS=60" in text
    assert "Environment=STRATEGY_PROFILE=demo_relaxed" in text
    assert "Environment=UNIVERSE_RANK_END=300" in text
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in text


def test_event_entry_runner_only_submits_active_champion_profile() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "run_bybit_demo_event_engine.sh").read_text(encoding="utf-8")

    assert 'SUBMIT_ORDERS:-0}" == "1"' in text
    assert '$STRATEGY_PROFILE" != "demo_relaxed"' in text
    assert "champion/challenger stack" in text


def test_live_runners_do_not_write_repo_bytecode() -> None:
    repo = Path(__file__).resolve().parents[1]
    paths = [
        repo / "scripts" / "run_bybit_demo_event_engine.sh",
        repo / "scripts" / "run_bybit_demo_ws_risk_engine.sh",
        repo / "deploy" / "systemd" / "model050426-bybit-demo.service",
        repo / "deploy" / "systemd" / "model050426-bybit-risk.service",
    ]

    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "PYTHONDONTWRITEBYTECODE" in text
