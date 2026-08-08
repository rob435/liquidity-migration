"""Owner control buttons in the Telegram main chat.

A small always-on daemon long-polls the notification bot for button presses
and runs three owner actions per environment:

``pause``
    Stop opening or closing anything new. Writes the sleeve toggles off in the
    host override (``/etc/liquidity-migration/sleeves.env``), regenerates the
    resolved toggle file with the same deploy library the rollout uses, and
    stops the producer units. The account owner, its protections, and the
    watchdog keep running; standing positions stay open. Because the pause is
    the designed host narrowing, it survives reboots and deploys, and the
    watchdog reads it as "deliberately off" rather than an outage.

``resume``
    Restore the sleeve override file to exactly what it was before the pause
    (a manual owner narrowing is preserved), regenerate the resolved toggles,
    and start whichever producers resolve on.

``close``
    Two-tap market close: pause first so a producer cannot republish, then run
    the existing flatten path (zero targets through the account owner's own
    inbox — every close is an ordinary reduce-only command). Trading stays
    paused afterwards until the owner presses resume.

Mainnet rows appear only while the mainnet owner unit is active, i.e. after
the owner's own arming act; this module never arms anything. Pausing mainnet
stops its producer units directly (mainnet has no sleeve toggles).

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
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

_REPO_ROOT = Path(__file__).resolve().parents[2]

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
    "CONTINUOUS_SLEEVE": "liquidity-migration-bybit-continuous-demo.service",
    "CARRY_SLEEVE": "liquidity-migration-bybit-carry-demo.service",
}
MAINNET_OWNER_UNIT = "liquidity-migration-account-execution-mainnet.service"
MAINNET_PRODUCER_UNITS = (
    "liquidity-migration-bybit-carry-mainnet.service",
    "liquidity-migration-bybit-long-mainnet.service",
)
DEMO_OWNER_UNIT = "liquidity-migration-account-execution.service"

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
    repo_dir: Path
    offset_path: Path
    host_sleeves_env: Path
    #: Verbatim copy of the host override taken at pause time; resume restores
    #: it so a manual owner narrowing survives a pause/resume round trip.
    saved_sleeves_path: Path
    poll_timeout_seconds: int = 50
    api_timeout_seconds: float = 20.0
    confirm_ttl_seconds: float = 120.0
    flatten_wait_seconds: float = 240.0


def load_config_from_environment(repo_dir: Path) -> ControlsConfig | None:
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
    state_dir = repo_dir / "data" / "telegram-controls"
    return ControlsConfig(
        token=token,
        chat_id=chat_id,
        control_user_ids=frozenset(user_ids),
        repo_dir=repo_dir,
        offset_path=state_dir / "offset.json",
        host_sleeves_env=Path(os.environ.get("LM_HOST_SLEEVES_ENV") or "/etc/liquidity-migration/sleeves.env"),
        saved_sleeves_path=state_dir / "sleeves_before_pause.txt",
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


# Host actions (systemctl, sleeve resolve, flatten)


class VpsFleet:
    """The subprocess side: unit state, sleeve toggles, and the flatten path."""

    def __init__(self, config: ControlsConfig) -> None:
        self._config = config

    def _run(self, argv: list[str], *, timeout: float = 90.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)

    def unit_active(self, unit: str) -> str:
        proc = self._run(["systemctl", "is-active", unit], timeout=15.0)
        return (proc.stdout or proc.stderr).strip() or "unknown"

    def mainnet_present(self) -> bool:
        return self.unit_active(MAINNET_OWNER_UNIT) == "active"

    def paused(self, environment: str) -> bool:
        if environment == "demo":
            return self._config.saved_sleeves_path.exists()
        return all(self.unit_active(unit) != "active" for unit in MAINNET_PRODUCER_UNITS)

    def resolved_sleeves(self) -> dict[str, str]:
        resolved = Path(os.environ.get("LM_RESOLVED_SLEEVES_ENV") or "/etc/liquidity-migration/sleeves.resolved.env")
        toggles: dict[str, str] = {}
        try:
            for line in resolved.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    toggles[key.strip()] = value.strip()
        except OSError:
            pass
        return toggles

    def _resolve_sleeves(self) -> None:
        """Regenerate the resolved toggle file with the deploy's own library.

        The hedge-timer line is computed by the deploy, not by the toggle
        loader, so the previous value is carried over verbatim.
        """
        script = (
            "set -euo pipefail\n"
            f'cd "{self._config.repo_dir}"\n'
            "source deploy/lib_sleeves.sh\n"
            'prev_hedge=""\n'
            'if [ -f "$LM_RESOLVED_SLEEVES_ENV" ]; then\n'
            "  prev_hedge=\"$(sed -n 's/^CONTINUOUS_HEDGE_TIMER=//p' \"$LM_RESOLVED_SLEEVES_ENV\" | head -1)\"\n"
            "fi\n"
            "lm_load_sleeve_toggles\n"
            'if [ -n "$prev_hedge" ]; then CONTINUOUS_HEDGE_TIMER="$prev_hedge"; fi\n'
            "lm_write_resolved_sleeve_toggles\n"
        )
        proc = self._run(["bash", "-c", script], timeout=30.0)
        if proc.returncode != 0:
            raise RuntimeError(f"sleeve toggle resolve failed: {(proc.stderr or proc.stdout)[:300]}")

    def _write_host_sleeves(self, content: str | None) -> None:
        path = self._config.host_sleeves_env
        if content is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.controls.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def _stop_units(self, units: list[str]) -> list[str]:
        failures: list[str] = []
        for unit in units:
            proc = self._run(["systemctl", "disable", "--now", unit])
            if proc.returncode != 0 and "does not exist" not in (proc.stderr or ""):
                failures.append(f"{unit}: {(proc.stderr or proc.stdout).strip()[:120]}")
        return failures

    def _start_units(self, units: list[str]) -> list[str]:
        failures: list[str] = []
        for unit in units:
            proc = self._run(["systemctl", "enable", "--now", unit])
            if proc.returncode != 0:
                failures.append(f"{unit}: {(proc.stderr or proc.stdout).strip()[:120]}")
        return failures

    def pause(self, environment: str) -> str:
        if environment == "mainnet":
            failures = self._stop_units(list(MAINNET_PRODUCER_UNITS))
            if failures:
                return "🚨 Mainnet pause hit trouble: " + "; ".join(failures)
            return (
                "⏸ Real-money trading is paused: both mainnet producers are stopped.\n"
                "Open positions stay open and protected by the account owner."
            )
        saved = self._config.saved_sleeves_path
        current = self._config.host_sleeves_env.read_text(encoding="utf-8") if self._config.host_sleeves_env.exists() else None
        if not saved.exists():
            # Second pause press must not overwrite the true pre-pause copy.
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_text("<absent>" if current is None else current, encoding="utf-8")
        self._write_host_sleeves(sleeve_pause_rewrite(current))
        self._resolve_sleeves()
        failures = self._stop_units(list(SLEEVE_UNITS.values()))
        if failures:
            return "🚨 Pause hit trouble: " + "; ".join(failures)
        return (
            "⏸ Demo trading is paused: producers are stopped and the sleeves are marked off, "
            "so the watchdog will not page about them.\n"
            "Open positions stay open and protected by the account owner. "
            "The pause survives reboots and deploys until you press Resume."
        )

    def resume(self, environment: str) -> str:
        if environment == "mainnet":
            failures = self._start_units(list(MAINNET_PRODUCER_UNITS))
            if failures:
                return "🚨 Mainnet resume hit trouble: " + "; ".join(failures)
            return "▶️ Real-money trading resumed: both mainnet producers are running again."
        saved = self._config.saved_sleeves_path
        if saved.exists():
            original = saved.read_text(encoding="utf-8")
            self._write_host_sleeves(None if original == "<absent>" else original)
        else:
            current = (
                self._config.host_sleeves_env.read_text(encoding="utf-8")
                if self._config.host_sleeves_env.exists()
                else None
            )
            self._write_host_sleeves(sleeve_strip_rewrite(current))
        self._resolve_sleeves()
        toggles = self.resolved_sleeves()
        to_start = [unit for key, unit in SLEEVE_UNITS.items() if toggles.get(key, "off").lower() in ("on", "1", "true", "yes")]
        failures = self._start_units(to_start)
        saved.unlink(missing_ok=True)
        if failures:
            return "🚨 Resume hit trouble: " + "; ".join(failures)
        names = ", ".join(unit.removeprefix("liquidity-migration-bybit-").removesuffix(".service") for unit in to_start)
        return f"▶️ Demo trading resumed: {names or 'no sleeve resolves on'}."

    def close_positions(self, environment: str) -> str:
        pause_note = self.pause(environment)
        if pause_note.startswith("🚨"):
            return pause_note + "\nMarket close NOT run — producers may still be publishing."
        argv = [
            "bash",
            str(self._config.repo_dir / "scripts" / "vps" / "flatten_account.sh"),
            "--environment",
            environment,
            "--execute",
            "--operator",
            "telegram-controls",
            "--reason",
            "owner pressed market-close in Telegram",
            "--wait-seconds",
            str(self._config.flatten_wait_seconds),
        ]
        try:
            proc = self._run(argv, timeout=self._config.flatten_wait_seconds + 180.0)
        except subprocess.TimeoutExpired:
            return "🚨 Market close is taking too long to report back — check the fleet before assuming flat."
        outcome = _parse_flatten_output(proc.stdout)
        return _describe_flatten(environment, proc.returncode, outcome, proc.stderr)

    def status_text(self) -> str:
        now = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        toggles = self.resolved_sleeves()
        lines = [f"📊 Fleet status · {now}"]
        lines.append(f"demo owner: {self.unit_active(DEMO_OWNER_UNIT)}")
        for key, unit in SLEEVE_UNITS.items():
            sleeve = key.removesuffix("_SLEEVE").lower()
            lines.append(f"demo {sleeve}: unit {self.unit_active(unit)}, sleeve {toggles.get(key, '?')}")
        lines.append("demo trading: PAUSED by controls" if self.paused("demo") else "demo trading: on")
        if self.mainnet_present():
            producer_states = ", ".join(
                f"{unit.removeprefix('liquidity-migration-bybit-').removesuffix('.service')}={self.unit_active(unit)}"
                for unit in MAINNET_PRODUCER_UNITS
            )
            lines.append(f"real money: owner active; {producer_states}")
        else:
            lines.append("real money: not armed")
        return "\n".join(lines)


def _parse_flatten_output(stdout: str) -> dict[str, Any] | None:
    start = stdout.find("{")
    if start < 0:
        return None
    try:
        parsed = json.loads(stdout[start:])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _describe_flatten(environment: str, returncode: int, outcome: dict[str, Any] | None, stderr: str) -> str:
    label = "real-money" if environment == "mainnet" else "demo"
    if outcome is None:
        return f"🚨 {label} market close gave no readable report (exit {returncode}): {stderr.strip()[:300]}"
    status = str(outcome.get("status") or "")
    planned = len((outcome.get("plan") or {}).get("components") or [])
    residual = outcome.get("residual") or {}
    positions = residual.get("positions") or []
    if status == "already_flat":
        return f"✅ Nothing to close — the {label} book was already flat. Trading stays paused."
    if status == "flat":
        return f"✅ Closed {planned} {label} position(s); the book is flat. Trading stays paused until you press Resume."
    if status == "dust_limited":
        rendered = ", ".join(f"{p.get('symbol')}={p.get('signed_qty')}" for p in positions)
        return (
            f"⚠️ Closed what the venue allows; leftover crumbs below the minimum order size remain: {rendered}. "
            "Trading stays paused."
        )
    detail = str(outcome.get("detail") or "").strip()
    return f"🚨 {label} market close did not finish cleanly ({status or f'exit {returncode}'}): {detail[:300]}"


# The panel


@dataclass(slots=True)
class _PendingClose:
    environment: str
    issued_monotonic: float


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
        self._pending_close: dict[str, _PendingClose] = {}

    def keyboard(self) -> list[list[dict[str, str]]]:
        rows = [
            [{"text": "📊 Status", "callback_data": "status"}],
            [
                {"text": "⏸ Pause demo trading", "callback_data": "pause:demo"},
                {"text": "▶️ Resume demo", "callback_data": "resume:demo"},
            ],
            [{"text": "🚨 Close ALL demo positions", "callback_data": "close:demo"}],
        ]
        if self._fleet.mainnet_present():
            rows.append(
                [
                    {"text": "⏸ Pause real money", "callback_data": "pause:mainnet"},
                    {"text": "▶️ Resume real money", "callback_data": "resume:mainnet"},
                ]
            )
            rows.append([{"text": "🚨 Close ALL real-money positions", "callback_data": "close:mainnet"}])
        return rows

    def send_panel(self) -> None:
        self._api.send_message(
            self._config.chat_id,
            "🎛 Trading controls\nPause stops new decisions; positions stay open. "
            "Close pauses first, then market-closes everything.",
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
        elif action == "close" and argument in _ENVIRONMENTS:
            self._api.answer_callback(callback_id)
            self._offer_close_confirm(argument)
        elif action == "closego":
            self._confirm_close(callback_id, argument)
        elif action == "closecancel":
            self._pending_close.pop(argument, None)
            self._api.answer_callback(callback_id, "Cancelled.")
            self._api.send_message(self._config.chat_id, "Market close cancelled — nothing was done.")
        else:
            self._api.answer_callback(callback_id, "Unknown button.")

    def _offer_close_confirm(self, environment: str) -> None:
        nonce = secrets.token_hex(8)
        self._pending_close[nonce] = _PendingClose(environment=environment, issued_monotonic=self._monotonic())
        label = "REAL-MONEY" if environment == "mainnet" else "demo"
        self._api.send_message(
            self._config.chat_id,
            f"⚠️ Close every open {label} position at market and pause trading?\n"
            f"This cannot be undone. Confirm within {int(self._config.confirm_ttl_seconds)}s.",
            keyboard=[
                [{"text": f"✅ Yes, close all {label} positions", "callback_data": f"closego:{nonce}"}],
                [{"text": "❌ Cancel", "callback_data": f"closecancel:{nonce}"}],
            ],
        )

    def _confirm_close(self, callback_id: str, nonce: str) -> None:
        pending = self._pending_close.pop(nonce, None)
        if pending is None:
            self._api.answer_callback(callback_id, "That confirmation is stale — press Close again.")
            return
        if self._monotonic() - pending.issued_monotonic > self._config.confirm_ttl_seconds:
            self._api.answer_callback(callback_id, "Confirmation expired — press Close again.")
            return
        self._api.answer_callback(callback_id, "Closing…")
        label = "real-money" if pending.environment == "mainnet" else "demo"
        self._api.send_message(self._config.chat_id, f"⏳ Pausing trading and market-closing the {label} book…")
        self._api.send_message(self._config.chat_id, self._safe_action(self._fleet.close_positions, pending.environment))

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
    parser.add_argument("--repo-dir", type=Path, default=_REPO_ROOT)
    parser.add_argument("--once", action="store_true", help="serve a single poll batch and exit (for smoke tests)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config = load_config_from_environment(args.repo_dir)
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
