"""Owner control buttons in the Telegram main chat.

A small unprivileged daemon long-polls the notification bot for button presses.
Host mutations cross one fixed sudo boundary into a root-owned, release-digest-
bound helper with an exact action allow-list:

``pause``
    Stop opening or closing anything new. The helper writes the demo sleeve
    narrowing atomically and disables the corresponding producer units. The
    account owner, its protections, and the watchdog keep running; standing
    positions stay open.

``resume``
    Demo only: restore the exact pre-pause sleeve override and start whichever
    producers resolve on. Funded resume is deliberately rollout-only and is
    never exposed by this bot.

There is no ``close`` button. ``scripts/ops.sh flatten --execute`` takes an
account to zero on the engine's own path, and it is an operator command rather
than a button on purpose: it stops the producers, and a
button that quietly stops a sleeve is the kind of thing somebody presses to see
what it does. Pause still stops new decisions.

The mainnet pause row appears only while the mainnet owner unit is active, i.e.
after the owner's own arming act; this module never arms or resumes anything.

Authorization: only updates from the configured chat are honored. Button
presses additionally require the presser to be the chat itself (a private
chat) or a member of ``TELEGRAM_CONTROL_USER_IDS``; in a group chat with no
allow-list every press is refused. Anything queued while the daemon was down
is dropped at startup so a stale press can never fire late.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "ControlPanel",
    "ControlsConfig",
    "TelegramApi",
    "VpsFleet",
    "callback_authorized",
    "main",
    "message_authorized",
    "sleeve_pause_rewrite",
    "sleeve_strip_rewrite",
]

#: Sleeve toggle keys this module manages in the host override, and the demo
#: producer unit each one publishes through.
SLEEVE_UNITS: dict[str, str] = {
    "LONG_SLEEVE": "liquidity-migration-bybit-long-demo.service",
    "CARRY_SLEEVE": "liquidity-migration-bybit-carry-demo.service",
}
MAINNET_OWNER_UNIT = "liquidity-migration-engine-mainnet.service"
MAINNET_PRODUCER_UNITS = (
    "liquidity-migration-bybit-carry-mainnet.service",
    "liquidity-migration-bybit-long-mainnet.service",
)
DEMO_OWNER_UNIT = "liquidity-migration-engine.service"
CONTROL_HELPER = "/opt/liquidity-migration-engine/bin/telegram-control-helper"
CONTROL_COMMANDS: dict[str, tuple[str, ...]] = {
    action: ("/usr/bin/sudo", "-n", CONTROL_HELPER, action)
    for action in ("pause-demo", "resume-demo", "pause-mainnet", "status-demo")
}
CONTROLS_STATE_DIR = Path("/var/lib/liquidity-migration-telegram-controls")

_PAUSE_MARKER = "# paused by telegram-controls; resume restores the saved original"
_ENVIRONMENTS = ("demo", "mainnet")


class ControlApiError(RuntimeError):
    """The Telegram API refused a call."""


@dataclass(frozen=True, slots=True)
class ControlsConfig:
    token: str
    chat_id: str
    #: Empty set means: only the private-chat owner (from.id == chat_id) may
    #: press buttons. A group chat therefore refuses every press until the
    #: owner lists user ids in TELEGRAM_CONTROL_USER_IDS.
    control_user_ids: frozenset[int]
    offset_path: Path
    poll_timeout_seconds: int = 50
    api_timeout_seconds: float = 20.0


def load_config_from_environment() -> ControlsConfig | None:
    """Build the config from the unit's environment; None when the bot is unconfigured."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    raw_ids = os.environ.get("TELEGRAM_CONTROL_USER_IDS", "")
    user_ids: set[int] = set()
    for piece in raw_ids.replace(";", ",").split(","):
        piece = piece.strip()
        if piece:
            try:
                user_ids.add(int(piece))
            except ValueError:
                logger.warning("ignoring non-numeric TELEGRAM_CONTROL_USER_IDS entry: %r", piece)
    return ControlsConfig(
        token=token,
        chat_id=chat_id,
        control_user_ids=frozenset(user_ids),
        offset_path=CONTROLS_STATE_DIR / "offset.json",
    )


# Pure pieces: sleeve-file rewrites and authorization


def _strip_managed_lines(text: str) -> list[str]:
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == _PAUSE_MARKER:
            continue
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key in SLEEVE_UNITS:
            continue
        kept.append(line)
    return kept


def sleeve_pause_rewrite(text: str | None) -> str:
    """Host override content with every managed sleeve set off, all else kept."""
    kept = _strip_managed_lines(text or "")
    lines = [*kept, _PAUSE_MARKER, *(f"{key}=off" for key in SLEEVE_UNITS)]
    return "\n".join(lines).strip("\n") + "\n"


def sleeve_strip_rewrite(text: str | None) -> str | None:
    """Fallback resume content: managed keys removed, or None to delete the file.

    Used only when the verbatim pre-pause copy is missing; repo defaults then
    decide what runs.
    """
    if text is None:
        return None
    kept = _strip_managed_lines(text)
    if not any(line.strip() for line in kept):
        return None
    return "\n".join(kept).strip("\n") + "\n"


def message_authorized(message: Mapping[str, Any], config: ControlsConfig) -> bool:
    chat = message.get("chat") or {}
    return str(chat.get("id", "")) == config.chat_id


def callback_authorized(callback: Mapping[str, Any], config: ControlsConfig) -> bool:
    """A press must come from the owner's chat AND an allowed presser."""
    message = callback.get("message") or {}
    if not message_authorized(message, config):
        return False
    from_id = (callback.get("from") or {}).get("id")
    if from_id is None:
        return False
    if config.control_user_ids:
        return int(from_id) in config.control_user_ids
    return str(from_id) == config.chat_id


# Telegram transport


class TelegramApi:
    """Minimal JSON client for the handful of bot methods the panel needs."""

    def __init__(
        self,
        token: str,
        *,
        timeout_seconds: float = 20.0,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self._token = token
        self._timeout = timeout_seconds
        self._urlopen = urlopen

    def call(self, method: str, payload: Mapping[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(dict(payload or {})).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._urlopen(request, timeout=timeout if timeout is not None else self._timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            raise ControlApiError(f"telegram {method} refused: {str(body)[:200]}")
        return body.get("result")

    def get_updates(self, *, offset: int | None, timeout_seconds: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout_seconds,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self.call("getUpdates", payload, timeout=timeout_seconds + 15.0)
        return list(result or [])

    def send_message(self, chat_id: str, text: str, *, keyboard: list[list[dict[str, str]]] | None = None) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("sendMessage", payload)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        try:
            self.call("answerCallbackQuery", payload)
        except (ControlApiError, OSError, urllib.error.URLError):
            # A press that already expired on Telegram's side must not kill the loop.
            logger.warning("answerCallbackQuery failed", exc_info=True)


# Host actions (systemctl, sleeve resolve)


class VpsFleet:
    """Read unit state directly; route every mutation through the fixed helper."""

    def __init__(self, config: ControlsConfig) -> None:
        self._config = config

    def _run(self, argv: list[str], *, timeout: float = 90.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)

    def _control(self, action: str) -> str:
        command = CONTROL_COMMANDS.get(action)
        if command is None:
            raise ValueError(f"unsupported control action: {action}")
        proc = self._run(list(command))
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise RuntimeError(f"privileged control helper refused {action}: {detail or proc.returncode}")
        return proc.stdout.strip()

    def _demo_status(self) -> tuple[bool, dict[str, str]]:
        values: dict[str, str] = {}
        for line in self._control("status-demo").splitlines():
            if "=" not in line:
                raise RuntimeError("privileged control helper returned malformed status")
            key, value = line.split("=", 1)
            if key in values or key not in {"paused", *SLEEVE_UNITS}:
                raise RuntimeError("privileged control helper returned an unexpected status field")
            values[key] = value
        if set(values) != {"paused", *SLEEVE_UNITS}:
            raise RuntimeError("privileged control helper returned incomplete status")
        if values["paused"] not in {"true", "false"}:
            raise RuntimeError("privileged control helper returned an invalid pause state")
        toggles = {key: values[key] for key in SLEEVE_UNITS}
        if any(value not in {"on", "off"} for value in toggles.values()):
            raise RuntimeError("privileged control helper returned an invalid sleeve state")
        return values["paused"] == "true", toggles

    def unit_active(self, unit: str) -> str:
        proc = self._run(["systemctl", "is-active", unit], timeout=15.0)
        return (proc.stdout or proc.stderr).strip() or "unknown"

    def mainnet_present(self) -> bool:
        return self.unit_active(MAINNET_OWNER_UNIT) == "active"

    def paused(self, environment: str) -> bool:
        if environment == "demo":
            return self._demo_status()[0]
        if environment == "mainnet":
            return all(self.unit_active(unit) != "active" for unit in MAINNET_PRODUCER_UNITS)
        raise ValueError(f"unsupported environment: {environment}")

    def resolved_sleeves(self) -> dict[str, str]:
        return self._demo_status()[1]

    def pause(self, environment: str) -> str:
        action = {"demo": "pause-demo", "mainnet": "pause-mainnet"}.get(environment)
        if action is None:
            raise ValueError(f"unsupported environment: {environment}")
        self._control(action)
        if environment == "mainnet":
            return (
                "⏸ Real-money trading is paused: both mainnet producers are stopped.\n"
                "Open positions stay open and protected by the account owner."
            )
        return (
            "⏸ Demo trading is paused: producers are stopped and the sleeves are marked off, "
            "so the watchdog will not page about them.\n"
            "Open positions stay open and protected by the account owner. "
            "The pause survives reboots and deploys until you press Resume."
        )

    def resume(self, environment: str) -> str:
        if environment == "mainnet":
            return (
                "🚫 Real-money resume is rollout-only. Use the reviewed funded rollout "
                "path so flatness, generation, and activation receipts are reverified."
            )
        if environment != "demo":
            raise ValueError(f"unsupported environment: {environment}")
        self._control("resume-demo")
        toggles = self.resolved_sleeves()
        names = ", ".join(
            unit.removeprefix("liquidity-migration-bybit-").removesuffix(".service")
            for key, unit in SLEEVE_UNITS.items()
            if toggles[key] == "on"
        )
        return f"▶️ Demo trading resumed: {names or 'no sleeve resolves on'}."

    def status_text(self) -> str:
        now = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        paused, toggles = self._demo_status()
        lines = [f"📊 Fleet status · {now}"]
        lines.append(f"demo owner: {self.unit_active(DEMO_OWNER_UNIT)}")
        for key, unit in SLEEVE_UNITS.items():
            sleeve = key.removesuffix("_SLEEVE").lower()
            lines.append(f"demo {sleeve}: unit {self.unit_active(unit)}, sleeve {toggles[key]}")
        lines.append("demo trading: PAUSED by controls" if paused else "demo trading: on")
        if self.mainnet_present():
            producer_states = ", ".join(
                f"{unit.removeprefix('liquidity-migration-bybit-').removesuffix('.service')}={self.unit_active(unit)}"
                for unit in MAINNET_PRODUCER_UNITS
            )
            lines.append(f"real money: owner active; {producer_states}")
        else:
            lines.append("real money: not armed")
        return "\n".join(lines)



# The panel


class ControlPanel:
    """Routes authorized updates to fleet actions and renders the buttons."""

    def __init__(
        self,
        config: ControlsConfig,
        api: TelegramApi,
        fleet: VpsFleet,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._api = api
        self._fleet = fleet
        self._monotonic = monotonic

    def keyboard(self) -> list[list[dict[str, str]]]:
        rows = [
            [{"text": "📊 Status", "callback_data": "status"}],
            [
                {"text": "⏸ Pause demo trading", "callback_data": "pause:demo"},
                {"text": "▶️ Resume demo", "callback_data": "resume:demo"},
            ],
        ]
        if self._fleet.mainnet_present():
            rows.append(
                [
                    {"text": "⏸ Pause real money", "callback_data": "pause:mainnet"},
                ]
            )
        return rows

    def send_panel(self) -> None:
        self._api.send_message(
            self._config.chat_id,
            "🎛 Trading controls\nPause stops new decisions; positions stay open. "
            "There is no close button — use scripts/ops.sh flatten --execute.",
            keyboard=self.keyboard(),
        )

    def handle_update(self, update: Mapping[str, Any]) -> None:
        message = update.get("message")
        if isinstance(message, Mapping):
            self._handle_message(message)
            return
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            self._handle_callback(callback)

    def _handle_message(self, message: Mapping[str, Any]) -> None:
        if not message_authorized(message, self._config):
            logger.info("ignoring message from foreign chat %s", (message.get("chat") or {}).get("id"))
            return
        text = str(message.get("text") or "").strip().lower()
        command = text.split("@", 1)[0]
        if command in ("/controls", "/start", "/panel"):
            self.send_panel()
        elif command == "/status":
            self._api.send_message(self._config.chat_id, self._fleet.status_text())

    def _handle_callback(self, callback: Mapping[str, Any]) -> None:
        callback_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        if not callback_authorized(callback, self._config):
            logger.warning("refused button press %r from user %s", data, (callback.get("from") or {}).get("id"))
            self._api.answer_callback(callback_id, "Not authorized.")
            return
        action, _, argument = data.partition(":")
        if action == "status":
            self._api.answer_callback(callback_id)
            self._api.send_message(self._config.chat_id, self._fleet.status_text())
        elif action == "pause" and argument in _ENVIRONMENTS:
            self._api.answer_callback(callback_id, "Pausing…")
            self._api.send_message(self._config.chat_id, self._safe_action(self._fleet.pause, argument))
        elif action == "resume" and argument in _ENVIRONMENTS:
            self._api.answer_callback(callback_id, "Resuming…")
            self._api.send_message(self._config.chat_id, self._safe_action(self._fleet.resume, argument))
        else:
            self._api.answer_callback(callback_id, "Unknown button.")


    def _safe_action(self, action: Callable[[str], str], environment: str) -> str:
        try:
            return action(environment)
        except Exception as exc:  # noqa: BLE001 — the loop must survive and report
            logger.exception("control action failed")
            return f"🚨 That action failed: {type(exc).__name__}: {str(exc)[:300]}"


# Offset persistence and the poll loop


def _load_offset(path: Path) -> int | None:
    try:
        return int(json.loads(path.read_text(encoding="utf-8"))["offset"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _save_offset(path: Path, offset: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps({"offset": offset}), encoding="utf-8")
    os.replace(tmp, path)


def drain_backlog(api: TelegramApi, offset: int | None) -> int | None:
    """Advance past everything queued while the daemon was down, executing nothing."""
    dropped = 0
    while True:
        updates = api.get_updates(offset=offset, timeout_seconds=0)
        if not updates:
            break
        offset = int(updates[-1]["update_id"]) + 1
        dropped += len(updates)
    if dropped:
        logger.info("dropped %d update(s) queued while the daemon was down", dropped)
    return offset


def serve_forever(config: ControlsConfig, api: TelegramApi, panel: ControlPanel, *, max_batches: int | None = None) -> None:
    try:
        api.call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "controls", "description": "Show the trading control buttons"},
                    {"command": "status", "description": "Fleet status"},
                ]
            },
        )
    except (ControlApiError, OSError, urllib.error.URLError):
        logger.warning("setMyCommands failed; /controls still works", exc_info=True)

    offset = drain_backlog(api, _load_offset(config.offset_path))
    if offset is not None:
        _save_offset(config.offset_path, offset)
    batches = 0
    while max_batches is None or batches < max_batches:
        batches += 1
        try:
            updates = api.get_updates(offset=offset, timeout_seconds=config.poll_timeout_seconds)
        except (ControlApiError, OSError, urllib.error.URLError) as exc:
            logger.warning("getUpdates failed (%s); retrying", type(exc).__name__)
            time.sleep(5.0)
            continue
        for update in updates:
            offset = int(update["update_id"]) + 1
            # Persist before acting so a crash mid-action cannot replay the press.
            _save_offset(config.offset_path, offset)
            try:
                panel.handle_update(update)
            except Exception:  # noqa: BLE001 — one bad update must not kill the panel
                logger.exception("failed to handle update")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="serve a single poll batch and exit (for smoke tests)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config = load_config_from_environment()
    if config is None:
        # Stay alive so the deploy's unit verification reads "active" and the
        # gap is visible in this journal rather than as a crash loop.
        logger.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are not set; controls are idle")
        if args.once:
            return 0
        while True:
            time.sleep(3600.0)
    api = TelegramApi(config.token, timeout_seconds=config.api_timeout_seconds)
    panel = ControlPanel(config, api, VpsFleet(config))
    serve_forever(config, api, panel, max_batches=1 if args.once else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
