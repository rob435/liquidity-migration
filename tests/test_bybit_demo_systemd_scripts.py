from __future__ import annotations

from pathlib import Path


def test_systemd_installer_has_single_demo_engine_and_signal_timer() -> None:
    repo = Path(__file__).resolve().parents[1]
    installer = (repo / "scripts" / "install_bybit_demo_systemd.sh").read_text(encoding="utf-8")

    assert "model050426-bybit-demo" in installer
    assert "run_bybit_demo_engine.sh" in installer
    assert "Type=simple" in installer
    assert "Restart=always" in installer
    assert "RestartSec=60" in installer
    assert 'rm -f "/etc/systemd/system/$legacy_unit"' in installer
    assert "$SERVICE_NAME-signal" in installer
    assert "systemctl enable \"$SERVICE_NAME.service\"" in installer
    assert "model050426-hourly-functional.timer" in installer
    assert "model050426-profit-protector.service" in installer
    assert "disable --now \"$legacy_unit\"" in installer
    assert "Refusing to install enabled demo runtime" in installer
    assert "remove_env_regex 'HOURLY_FUNCTIONAL_.*'" in installer
    assert "remove_env_regex 'PROFIT_PROTECTOR_.*'" in installer


def test_demo_engine_locks_canonical_forward_mode_and_fast_protection() -> None:
    repo = Path(__file__).resolve().parents[1]
    installer = (repo / "scripts" / "install_bybit_demo_systemd.sh").read_text(encoding="utf-8")
    runner = (repo / "scripts" / "run_bybit_demo_engine.sh").read_text(encoding="utf-8")

    assert "DEMO_FORWARD_MODE" not in installer
    assert "DEMO_FORWARD_MODE" not in runner
    assert "DEMO_SIZING_MODE" not in runner
    assert "DEMO_MAX_ORDER_NOTIONAL" in runner
    assert "DEMO_MAX_TOTAL_NEW_NOTIONAL" in runner
    assert "DEMO_USE_WALLET_BALANCE" in runner
    assert "--max-order-notional-pct-equity" in runner
    assert "0.10" in runner
    assert "--demo-sizing-mode" not in runner
    assert "--forward-mode open-from-scan" in " ".join(runner.split())
    assert "--require-first-slice" in runner
    assert "--fast-protection-seconds" in runner
    assert "FAST_PROTECTION_SECONDS=55" in runner
    assert "sleep_to_next_minute" in runner


def test_signal_runner_opens_first_twap_slice_after_scan() -> None:
    repo = Path(__file__).resolve().parents[1]
    runner = (repo / "scripts" / "run_forward_signal_with_audit.sh").read_text(encoding="utf-8")
    compact = " ".join(runner.split())

    assert "forward-run-sleeves" in runner
    assert "--forward-mode scan" in compact
    assert "bybit-demo-cycle" in runner
    assert "--forward-mode open-from-scan" in compact
    assert "--require-first-slice" in runner
    assert "--submit-orders" in runner
    assert "--use-wallet-balance" in runner
    assert "DEMO_MAX_ORDER_NOTIONAL_PCT_EQUITY" in runner
