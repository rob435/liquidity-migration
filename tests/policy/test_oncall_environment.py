from __future__ import annotations

from pathlib import Path

import pytest

from liquidity_migration.policy.oncall_environment import (
    prepare_environments,
    validate_notifications,
    validate_oncall,
)


def _private(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_notification_environment_is_transport_only() -> None:
    values = validate_notifications(
        {
            "TELEGRAM_BOT_TOKEN": "123:token",
            "TELEGRAM_ALERT_CHAT_ID": "-100123",
            "TELEGRAM_CONTROL_USER_IDS": "12,34",
        }
    )
    assert values["TELEGRAM_ALERT_CHAT_ID"] == "-100123"
    with pytest.raises(ValueError, match="unsupported keys"):
        validate_notifications(
            {
                "TELEGRAM_BOT_TOKEN": "123:token",
                "TELEGRAM_CHAT_ID": "12",
                "BYBIT_DEMO_API_KEY": "must-not-cross",
            }
        )


def test_oncall_environment_pins_the_routine_endpoint_and_requires_deadman() -> None:
    values = validate_oncall(
        {
            "INCIDENT_ROUTINE_FIRE_URL": (
                "https://api.anthropic.com/v1/claude_code/routines/trig_1/fire"
            ),
            "INCIDENT_ROUTINE_FIRE_TOKEN": "sk-ant-test",
            "ONCALL_DEADMAN_URL": "https://hc-ping.com/check-id",
        }
    )
    assert values["ONCALL_DEADMAN_URL"].startswith("https://")
    with pytest.raises(ValueError, match="Claude Code routine"):
        validate_oncall(
            {
                "INCIDENT_ROUTINE_FIRE_URL": "https://attacker.invalid/fire",
                "INCIDENT_ROUTINE_FIRE_TOKEN": "sk-ant-test",
                "ONCALL_DEADMAN_URL": "https://hc-ping.com/check-id",
            }
        )


def test_prepare_migrates_legacy_files_once_without_copying_venue_keys(
    tmp_path: Path,
) -> None:
    legacy_telegram = _private(
        tmp_path / "bybit-demo.env",
        "BYBIT_DEMO_API_KEY=venue-secret\n"
        "TELEGRAM_BOT_TOKEN=123:token\n"
        "TELEGRAM_CHAT_ID=42\n"
        "TELEGRAM_ALERT_CHAT_ID=-10042\n",
    )
    legacy_liveness = _private(
        tmp_path / "liveness.env",
        "LIVENESS_HEARTBEAT_URL=https://hc-ping.com/check-id\n"
        "INCIDENT_ROUTINE_FIRE_URL="
        "https://api.anthropic.com/v1/claude_code/routines/trig_1/fire\n"
        "INCIDENT_ROUTINE_FIRE_TOKEN=sk-ant-test\n",
    )
    notifications = tmp_path / "notifications.env"
    oncall = tmp_path / "oncall.env"

    prepared_notifications, prepared_oncall = prepare_environments(
        notifications_path=notifications,
        oncall_path=oncall,
        legacy_telegram_path=legacy_telegram,
        legacy_liveness_path=legacy_liveness,
    )

    assert "BYBIT_DEMO_API_KEY" not in notifications.read_text(encoding="utf-8")
    assert prepared_notifications["TELEGRAM_BOT_TOKEN"] == "123:token"
    assert prepared_oncall["ONCALL_DEADMAN_URL"].endswith("check-id")
    assert notifications.stat().st_mode & 0o777 == 0o600
    assert oncall.stat().st_mode & 0o777 == 0o600

    legacy_telegram.write_text("broken=now\n", encoding="utf-8")
    legacy_liveness.write_text("broken=now\n", encoding="utf-8")
    again = prepare_environments(
        notifications_path=notifications,
        oncall_path=oncall,
        legacy_telegram_path=legacy_telegram,
        legacy_liveness_path=legacy_liveness,
    )
    assert again == (prepared_notifications, prepared_oncall)


def test_templates_name_every_runtime_key() -> None:
    root = Path(__file__).resolve().parents[2]
    notifications = (root / "deploy" / "notifications.env.template").read_text()
    oncall = (root / "deploy" / "oncall.env.template").read_text()
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_ALERT_CHAT_ID",
        "TELEGRAM_CONTROL_USER_IDS",
    ):
        assert f"{key}=" in notifications
    for key in (
        "INCIDENT_ROUTINE_FIRE_URL",
        "INCIDENT_ROUTINE_FIRE_TOKEN",
        "ONCALL_DEADMAN_URL",
    ):
        assert f"{key}=" in oncall
