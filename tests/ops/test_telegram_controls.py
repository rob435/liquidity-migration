"""Telegram control panel: authorization, helper calls, and the confirm flow."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from liquidity_migration.ops import telegram_controls as tc
from liquidity_migration.ops.telegram_controls import (
    ControlPanel,
    ControlsConfig,
    VpsFleet,
    callback_authorized,
    drain_backlog,
    message_authorized,
)


def make_config(tmp_path: Path, **overrides: object) -> ControlsConfig:
    values: dict[str, object] = {
        "token": "tok",
        "chat_id": "777",
        "control_user_ids": frozenset(),
        "offset_path": tmp_path / "offset.json",
    }
    values.update(overrides)
    return ControlsConfig(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


def test_message_from_foreign_chat_is_denied(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    assert not message_authorized({"chat": {"id": 123}}, config)
    assert message_authorized({"chat": {"id": 777}}, config)


def test_private_chat_press_requires_from_id_to_match_chat(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    ok = {"message": {"chat": {"id": 777}}, "from": {"id": 777}}
    imposter = {"message": {"chat": {"id": 777}}, "from": {"id": 999}}
    assert callback_authorized(ok, config)
    assert not callback_authorized(imposter, config)
    assert not callback_authorized({"message": {"chat": {"id": 777}}}, config)


def test_group_chat_press_needs_the_allow_list(tmp_path: Path) -> None:
    group = make_config(tmp_path, chat_id="-100200")
    press = {"message": {"chat": {"id": -100200}}, "from": {"id": 42}}
    assert not callback_authorized(press, group)
    allowed = make_config(tmp_path, chat_id="-100200", control_user_ids=frozenset({42}))
    assert callback_authorized(press, allowed)
    assert not callback_authorized({"message": {"chat": {"id": -100200}}, "from": {"id": 43}}, allowed)


def test_runtime_state_path_is_fixed_outside_the_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    monkeypatch.setenv("LM_HOST_SLEEVES_ENV", "/tmp/caller-selected-sleeves")
    monkeypatch.setenv("LM_RESOLVED_SLEEVES_ENV", "/tmp/caller-selected-resolved")
    config = tc.load_config_from_environment()
    assert config is not None
    assert config.offset_path == tc.CONTROLS_STATE_DIR / "offset.json"


# --------------------------------------------------------------------------
# Panel routing with fakes
# --------------------------------------------------------------------------


class FakeApi:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.answered: list[tuple[str, str]] = []

    def send_message(self, chat_id: str, text: str, *, keyboard=None) -> None:
        self.sent.append({"chat_id": chat_id, "text": text, "keyboard": keyboard})

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answered.append((callback_id, text))


class FakeFleet:
    def __init__(self, *, funded: tuple[str, ...] = ()) -> None:
        self.funded = funded
        self.calls: list[tuple[str, str]] = []

    def funded_present(self) -> tuple[str, ...]:
        return self.funded

    def status_text(self) -> str:
        return "status"

    def pause(self, environment: str) -> str:
        self.calls.append(("pause", environment))
        return "paused"

    def resume(self, environment: str) -> str:
        self.calls.append(("resume", environment))
        return "resumed"

    def close_positions(self, environment: str) -> str:
        self.calls.append(("close", environment))
        return "closed"


def make_panel(tmp_path: Path, *, funded: tuple[str, ...] = (), now: list[float] | None = None):
    config = make_config(tmp_path)
    api = FakeApi()
    fleet = FakeFleet(funded=funded)
    clock = now if now is not None else [0.0]
    panel = ControlPanel(config, api, fleet, monotonic=lambda: clock[0])  # type: ignore[arg-type]
    return panel, api, fleet, clock


def _press(data: str, *, chat: int = 777, user: int = 777) -> dict[str, object]:
    return {
        "callback_query": {
            "id": "cb1",
            "data": data,
            "from": {"id": user},
            "message": {"chat": {"id": chat}},
        }
    }


def test_controls_command_sends_panel_without_mainnet_rows(tmp_path: Path) -> None:
    panel, api, _, _ = make_panel(tmp_path)
    panel.handle_update({"message": {"chat": {"id": 777}, "text": "/controls"}})
    keyboard = api.sent[-1]["keyboard"]
    flat = json.dumps(keyboard)
    assert "pause:demo" in flat
    # Flatten is an explicit operator flow, not a phone button.
    assert "close" not in flat
    assert "mainnet" not in flat


def test_panel_grows_mainnet_rows_when_owner_is_active(tmp_path: Path) -> None:
    panel, api, _, _ = make_panel(tmp_path, funded=("mainnet",))
    panel.send_panel()
    flat = json.dumps(api.sent[-1]["keyboard"])
    assert "pause:mainnet" in flat
    assert "pause:mexc" not in flat


def test_panel_grows_one_row_per_running_funded_realm(tmp_path: Path) -> None:
    panel, api, _, _ = make_panel(tmp_path, funded=("mainnet", "mexc"))
    panel.send_panel()
    flat = json.dumps(api.sent[-1]["keyboard"])
    assert "pause:mainnet" in flat
    assert "pause:mexc" in flat
    # Resume stays a shell action for every funded realm.
    assert "resume:mainnet" not in flat
    assert "resume:mexc" not in flat
    assert "resume:mainnet" not in flat
    assert "close" not in flat


def test_pause_and_resume_presses_reach_the_fleet(tmp_path: Path) -> None:
    panel, api, fleet, _ = make_panel(tmp_path)
    panel.handle_update(_press("pause:demo"))
    panel.handle_update(_press("resume:demo"))
    assert fleet.calls == [("pause", "demo"), ("resume", "demo")]
    assert [m["text"] for m in api.sent] == ["paused", "resumed"]


def test_unauthorized_press_is_answered_but_never_acted_on(tmp_path: Path) -> None:
    panel, api, fleet, _ = make_panel(tmp_path)
    panel.handle_update(_press("pause:demo", user=999))
    assert fleet.calls == []
    assert api.answered == [("cb1", "Not authorized.")]
    assert api.sent == []


# --------------------------------------------------------------------------
# Backlog drain
# --------------------------------------------------------------------------


class DrainApi:
    def __init__(self, batches: list[list[dict[str, int]]]) -> None:
        self.batches = batches
        self.requested_offsets: list[int | None] = []

    def get_updates(self, *, offset: int | None, timeout_seconds: int) -> list[dict[str, int]]:
        assert timeout_seconds == 0
        self.requested_offsets.append(offset)
        return self.batches.pop(0) if self.batches else []


def test_drain_backlog_skips_queued_presses_and_advances_offset() -> None:
    api = DrainApi([[{"update_id": 5}, {"update_id": 6}], []])
    offset = drain_backlog(api, None)  # type: ignore[arg-type]
    assert offset == 7
    assert api.requested_offsets == [None, 7]


# --------------------------------------------------------------------------
# Fleet privilege delegation (subprocesses faked)
# --------------------------------------------------------------------------


@pytest.fixture()
def fleet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = make_config(tmp_path)
    fleet = VpsFleet(config)
    commands: list[list[str]] = []

    def fake_run(argv: list[str], *, timeout: float = 90.0) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        if argv == [*tc.CONTROL_COMMANDS["status-fleet"]]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=(
                    "fleet-status-v1\n"
                    "demo-control|paused|false\n"
                    "sleeve|long|on\n"
                    "sleeve|carry|on\n"
                    "entries|demo|long|true\n"
                    "entries|demo|carry|true\n"
                    "entries|demo|exodus|true\n"
                    "entries|mainnet|long|true\n"
                    "entries|mainnet|carry|true\n"
                    "entries|mainnet|exodus|true\n"
                    "unit|demo|owner|-|liquidity-migration-engine.service|active\n"
                    "unit|demo|signal|directional|liquidity-migration-signal-worker-demo.service|active\n"
                    "unit|mainnet|owner|-|liquidity-migration-engine-mainnet.service|active\n"
                    "unit|mainnet|signal|directional|liquidity-migration-signal-worker-mainnet.service|active\n"
                    # The mexc realm is in the manifest and unarmed: unit rows,
                    # no heartbeat, therefore no entry rows.
                    "unit|mexc|owner|-|liquidity-migration-engine-mexc.service|inactive\n"
                    "unit|mexc|signal|directional|liquidity-migration-signal-worker-mexc.service|inactive\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(fleet, "_run", fake_run)
    return config, fleet, commands


def test_demo_pause_delegates_one_exact_helper_action(fleet_env) -> None:
    _config, fleet, commands = fleet_env
    note = fleet.pause("demo")
    assert "paused" in note.lower()
    assert commands == [list(tc.CONTROL_COMMANDS["pause-demo"])]
    assert all("disable" not in command for command in commands)


def test_demo_resume_uses_helper_then_reads_helper_status(fleet_env) -> None:
    _config, fleet, commands = fleet_env
    note = fleet.resume("demo")
    assert "resumed" in note.lower()
    assert commands == [
        list(tc.CONTROL_COMMANDS["resume-demo"]),
        list(tc.CONTROL_COMMANDS["status-fleet"]),
    ]


def test_helper_status_is_exactly_parsed_and_rejects_extra_fields(fleet_env, monkeypatch) -> None:
    _config, fleet, _commands = fleet_env
    status = fleet.status_text()
    assert "demo long: entries on, configured on" in status
    assert "demo carry: entries on, configured on" in status

    def malformed(argv: list[str], *, timeout: float = 90.0) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="fleet-status-v1\ndemo-control|paused|false\nsleeve|long|on\nPATH=/tmp\n",
            stderr="",
        )

    monkeypatch.setattr(fleet, "_run", malformed)
    with pytest.raises(RuntimeError, match="malformed fleet row"):
        fleet.status_text()


def test_status_renders_manifest_owners_signal_workers_and_entry_permissions(fleet_env) -> None:
    _config, fleet, commands = fleet_env
    status = fleet.status_text()
    assert "demo owner: active" in status
    assert "demo signal worker: active" in status
    assert "demo exodus: entries on" in status
    assert "real money: owner active" in status
    assert "signal active" in status
    assert "carry=on" in status
    assert "exodus=on" in status
    assert "long=on" in status
    assert commands == [list(tc.CONTROL_COMMANDS["status-fleet"])]


def test_mainnet_pause_and_resume_each_reach_exactly_their_own_action(fleet_env) -> None:
    """Pausing funded trading from a phone is only useful if it can be undone
    from the same phone; the helper, not the bot, holds the guards.
    """
    _config, fleet, commands = fleet_env
    fleet.pause("mainnet")
    message = fleet.resume("mainnet")
    assert commands == [
        list(tc.CONTROL_COMMANDS["pause-mainnet"]),
        list(tc.CONTROL_COMMANDS["resume-mainnet"]),
    ]
    assert "REAL_MONEY is not touched" in message


def test_mexc_pause_and_resume_each_reach_exactly_their_own_action(fleet_env) -> None:
    _config, fleet, commands = fleet_env
    fleet.pause("mexc")
    message = fleet.resume("mexc")
    assert commands == [
        list(tc.CONTROL_COMMANDS["pause-mexc"]),
        list(tc.CONTROL_COMMANDS["resume-mexc"]),
    ]
    assert "REAL_MONEY is not touched" in message


def test_an_unarmed_funded_realm_reports_units_without_entry_rows(fleet_env) -> None:
    """An unarmed realm publishes no heartbeat, so the helper reports no entry
    permissions for it. That is a status line, not a broken panel."""

    _config, fleet, _commands = fleet_env
    status = fleet.status_text()
    assert "mexc: owner inactive; signal inactive; not armed" in status
    assert fleet.funded_present() == ("mainnet",)


def test_control_action_allowlist_cannot_forward_paths_units_or_environment(fleet_env) -> None:
    _config, fleet, commands = fleet_env
    with pytest.raises(ValueError, match="unsupported control action"):
        fleet._control("pause-demo /etc/passwd")
    assert commands == []
    assert set(tc.CONTROL_COMMANDS) == {
        "pause-demo",
        "resume-demo",
        "pause-mainnet",
        "resume-mainnet",
        "pause-mexc",
        "resume-mexc",
        "status-fleet",
    }
    for action, command in tc.CONTROL_COMMANDS.items():
        assert command == ("/usr/bin/sudo", "-n", tc.CONTROL_HELPER, action)
