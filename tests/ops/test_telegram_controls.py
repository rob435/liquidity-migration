"""Telegram control panel: authorization, sleeve rewrites, and the confirm flow."""

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
    sleeve_pause_rewrite,
    sleeve_strip_rewrite,
)


def make_config(tmp_path: Path, **overrides: object) -> ControlsConfig:
    values: dict[str, object] = {
        "token": "tok",
        "chat_id": "777",
        "control_user_ids": frozenset(),
        "repo_dir": tmp_path,
        "offset_path": tmp_path / "offset.json",
        "host_sleeves_env": tmp_path / "sleeves.env",
        "saved_sleeves_path": tmp_path / "sleeves_before_pause.txt",
    }
    values.update(overrides)
    return ControlsConfig(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Sleeve override rewrites
# --------------------------------------------------------------------------


def test_pause_rewrite_preserves_foreign_lines_and_sets_all_off() -> None:
    # CONTINUOUS_SLEEVE is a retired toggle: pause must leave an existing line
    # alone as a foreign line and never add one of its own.
    original = "# host note\nRETIRED_TOGGLE=off\nCONTINUOUS_SLEEVE=off\nLONG_SLEEVE=on\n"
    rewritten = sleeve_pause_rewrite(original)
    assert "# host note" in rewritten
    assert "RETIRED_TOGGLE=off" in rewritten
    assert rewritten.count("CONTINUOUS_SLEEVE=") == 1
    for key in ("LONG_SLEEVE", "CARRY_SLEEVE"):
        assert rewritten.count(f"{key}=") == 1
        assert f"{key}=off" in rewritten
    assert rewritten.endswith("\n")


def test_pause_rewrite_is_idempotent_and_handles_absent_file() -> None:
    once = sleeve_pause_rewrite(None)
    assert sleeve_pause_rewrite(once) == once


def test_strip_rewrite_removes_managed_keys_and_marker() -> None:
    text = sleeve_pause_rewrite("# keep me\nRETIRED_TOGGLE=off\n")
    stripped = sleeve_strip_rewrite(text)
    assert stripped == "# keep me\nRETIRED_TOGGLE=off\n"


def test_strip_rewrite_returns_none_when_nothing_remains() -> None:
    assert sleeve_strip_rewrite(None) is None
    assert sleeve_strip_rewrite(sleeve_pause_rewrite(None)) is None


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
    def __init__(self, *, mainnet: bool = False) -> None:
        self.mainnet = mainnet
        self.calls: list[tuple[str, str]] = []

    def mainnet_present(self) -> bool:
        return self.mainnet

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


def make_panel(tmp_path: Path, *, mainnet: bool = False, now: list[float] | None = None):
    config = make_config(tmp_path)
    api = FakeApi()
    fleet = FakeFleet(mainnet=mainnet)
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
    # No close button anywhere: it published zero targets into the deleted
    # owner's inbox, and a button that cannot work is worse than no button.
    assert "close" not in flat
    assert "mainnet" not in flat


def test_panel_grows_mainnet_rows_when_owner_is_active(tmp_path: Path) -> None:
    panel, api, _, _ = make_panel(tmp_path, mainnet=True)
    panel.send_panel()
    flat = json.dumps(api.sent[-1]["keyboard"])
    assert "pause:mainnet" in flat
    assert "resume:mainnet" in flat
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
# Fleet pause/resume file semantics (systemctl faked)
# --------------------------------------------------------------------------


@pytest.fixture()
def fleet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = make_config(tmp_path)
    resolved = tmp_path / "sleeves.resolved.env"
    monkeypatch.setenv("LM_RESOLVED_SLEEVES_ENV", str(resolved))
    fleet = VpsFleet(config)
    commands: list[list[str]] = []

    def fake_run(argv: list[str], *, timeout: float = 90.0) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        if argv[0] == "bash":  # the resolve step
            resolved.write_text("LONG_SLEEVE=on\nCONTINUOUS_SLEEVE=off\nCARRY_SLEEVE=on\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(fleet, "_run", fake_run)
    return config, fleet, commands, resolved


def test_pause_saves_the_original_override_and_stops_producers(fleet_env) -> None:
    config, fleet, commands, _ = fleet_env
    config.host_sleeves_env.write_text("CONTINUOUS_SLEEVE=off\n# manual note\n", encoding="utf-8")
    note = fleet.pause("demo")
    assert "paused" in note.lower()
    assert config.saved_sleeves_path.read_text(encoding="utf-8") == "CONTINUOUS_SLEEVE=off\n# manual note\n"
    written = config.host_sleeves_env.read_text(encoding="utf-8")
    assert "LONG_SLEEVE=off" in written and "CARRY_SLEEVE=off" in written and "# manual note" in written
    stopped = [argv[3] for argv in commands if argv[:3] == ["systemctl", "disable", "--now"]]
    assert set(stopped) == set(tc.SLEEVE_UNITS.values())
    # The retired CONTINUOUS producer unit no longer exists on the host; a
    # pause that touched it would collect a spurious systemctl failure.
    assert "liquidity-migration-bybit-continuous-demo.service" not in stopped


def test_second_pause_keeps_the_first_saved_copy(fleet_env) -> None:
    config, fleet, _, _ = fleet_env
    config.host_sleeves_env.write_text("# original\n", encoding="utf-8")
    fleet.pause("demo")
    fleet.pause("demo")
    assert config.saved_sleeves_path.read_text(encoding="utf-8") == "# original\n"


def test_resume_restores_the_saved_override_verbatim(fleet_env) -> None:
    config, fleet, commands, _ = fleet_env
    config.host_sleeves_env.write_text("CONTINUOUS_SLEEVE=off\n", encoding="utf-8")
    fleet.pause("demo")
    commands.clear()
    note = fleet.resume("demo")
    assert "resumed" in note.lower()
    assert config.host_sleeves_env.read_text(encoding="utf-8") == "CONTINUOUS_SLEEVE=off\n"
    assert not config.saved_sleeves_path.exists()
    started = [argv[3] for argv in commands if argv[:3] == ["systemctl", "enable", "--now"]]
    # The resolved file turns LONG and CARRY on; CONTINUOUS stays off.
    assert set(started) == {
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-carry-demo.service",
    }


def test_resume_after_pause_of_absent_file_deletes_the_override(fleet_env) -> None:
    config, fleet, _, _ = fleet_env
    fleet.pause("demo")
    assert config.host_sleeves_env.exists()
    fleet.resume("demo")
    assert not config.host_sleeves_env.exists()


def test_mainnet_pause_and_resume_touch_only_mainnet_units(fleet_env) -> None:
    config, fleet, commands, _ = fleet_env
    fleet.pause("mainnet")
    fleet.resume("mainnet")
    touched = {argv[3] for argv in commands if argv[0] == "systemctl" and len(argv) > 3}
    assert touched == set(tc.MAINNET_PRODUCER_UNITS)
    assert not config.host_sleeves_env.exists()


# --------------------------------------------------------------------------
# Flatten outcome rendering
# --------------------------------------------------------------------------


def _outcome(status: str, *, components: int = 2, positions=None, detail: str = "") -> dict[str, object]:
    return {
        "status": status,
        "detail": detail,
        "plan": {"components": [{"symbol": f"S{i}"} for i in range(components)]},
        "residual": {"positions": positions or []},
    }


