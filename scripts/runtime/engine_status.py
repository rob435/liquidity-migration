#!/usr/bin/env python3
"""Read one engine heartbeat and answer "why is it not trading".

Read-only: opens the heartbeat file, prints HEALTH, EXPOSURE and BLOCKERS, and
touches nothing else. No venue access, no new state, no daemon.

A field the heartbeat does not carry prints as ``unknown``. Zero is a reading;
``unknown`` is the absence of one, and the two are never spelled the same way.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

UNKNOWN = "unknown"
LABEL_WIDTH = 24


def _row(label: str, value: object) -> str:
    # The padded label keeps a space even when the label overruns the column.
    return f"  {label:<{LABEL_WIDTH - 1}} {value}"


def _get(heartbeat: dict[str, Any], key: str) -> Any | None:
    value = heartbeat.get(key)
    return None if value is None else value


def _number(heartbeat: dict[str, Any], key: str) -> str:
    value = _get(heartbeat, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return UNKNOWN
    return f"{value:g}"


def _millis(heartbeat: dict[str, Any], key: str) -> str:
    """Epoch milliseconds, printed whole: %g would turn them into 1.7575e+12."""
    value = _get(heartbeat, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return UNKNOWN
    return f"{int(value)}"


def _flag(heartbeat: dict[str, Any], key: str) -> str:
    value = _get(heartbeat, key)
    return UNKNOWN if not isinstance(value, bool) else ("yes" if value else "no")


def _rows(heartbeat: dict[str, Any], key: str) -> list[dict[str, Any]] | None:
    value = _get(heartbeat, key)
    if not isinstance(value, list):
        return None
    return [row for row in value if isinstance(row, dict)]


def _text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    return UNKNOWN if value is None else str(value)


def _heartbeat_age_s(heartbeat: dict[str, Any], now_ms: int) -> str:
    stamp = _get(heartbeat, "wall_ts_ms")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return UNKNOWN
    return f"{(now_ms - stamp) / 1000.0:.1f}"


def _health(heartbeat: dict[str, Any], now_ms: int) -> list[str]:
    lines = ["HEALTH", _row("heartbeat age s", _heartbeat_age_s(heartbeat, now_ms))]
    lines.append(_row("may_open", _flag(heartbeat, "may_open")))

    unready = _get(heartbeat, "private_stream_unready_ms")
    ready = _flag(heartbeat, "private_stream_ready")
    if isinstance(unready, (int, float)) and not isinstance(unready, bool):
        lines.append(_row("private stream", f"ready={ready} unready_ms={unready:g}"))
    else:
        lines.append(_row("private stream", f"ready={ready} unready_ms={UNKNOWN}"))

    lines.append(
        _row(
            "rolling loss",
            "tripped={} net={} limit={} USDT".format(
                _flag(heartbeat, "rolling_loss_tripped"),
                _number(heartbeat, "rolling_loss_net_usdt"),
                _number(heartbeat, "rolling_loss_limit_usdt"),
            ),
        )
    )
    lines.append(
        _row(
            "account",
            "equity={} available={} observed_wall_ts_ms={}".format(
                _number(heartbeat, "account_equity_usdt"),
                _number(heartbeat, "account_available_usdt"),
                _millis(heartbeat, "account_observed_wall_ts_ms"),
            ),
        )
    )

    permissions = _rows(heartbeat, "strategy_entries_enabled")
    if permissions is None:
        lines.append(_row("entries enabled", UNKNOWN))
    elif not permissions:
        lines.append(_row("entries enabled", "no strategies"))
    else:
        rendered = " ".join(
            f"{_text(row, 'strategy')}={_flag(row, 'entries_enabled')}" for row in permissions
        )
        lines.append(_row("entries enabled", rendered))

    errors = _rows(heartbeat, "strategy_errors")
    if errors is None:
        lines.append(_row("strategy errors", UNKNOWN))
    elif not errors:
        lines.append(_row("strategy errors", "none"))
    else:
        lines.append(_row("strategy errors", f"{len(errors)}"))
        for row in errors:
            lines.append(_row("", f"{_text(row, 'strategy')}: {_text(row, 'error')}"))

    pending = _rows(heartbeat, "pending_flatten_requests")
    if pending is None:
        lines.append(_row("pending flatten", UNKNOWN))
    else:
        lines.append(_row("pending flatten", f"{len(pending)}"))
        for row in pending:
            lines.append(_row("", f"{_text(row, 'strategy')}: {_text(row, 'request_id')}"))

    lines.append(_row("uptime s", _number(heartbeat, "uptime_s")))
    commit = _get(heartbeat, "engine_commit")
    lines.append(_row("engine commit", UNKNOWN if commit is None else str(commit)))
    return lines


def _exposure(heartbeat: dict[str, Any]) -> list[str]:
    lines = ["EXPOSURE"]
    positions = _rows(heartbeat, "positions")
    if positions is None:
        lines.append(_row("positions", UNKNOWN))
    elif not positions:
        lines.append(_row("positions", "flat"))
    else:
        lines.append(_row("positions", f"{len(positions)}"))
        for row in positions:
            qty = row.get("qty")
            qty_text = (
                UNKNOWN if isinstance(qty, bool) or not isinstance(qty, (int, float)) else f"{qty:g}"
            )
            lines.append(
                _row(
                    "",
                    "{:<14} {:<5} qty={:<12} strategy={}".format(
                        _text(row, "symbol"),
                        _text(row, "side"),
                        qty_text,
                        _text(row, "strategy"),
                    ),
                )
            )

    working = _rows(heartbeat, "working_entries")
    if working is None:
        lines.append(_row("working entries", UNKNOWN))
    elif not working:
        lines.append(_row("working entries", "none"))
    else:
        lines.append(_row("working entries", f"{len(working)}"))
        for row in working:
            lines.append(_row("", f"{_text(row, 'symbol')} {_text(row, 'strategy')}"))
    return lines


def _blockers(heartbeat: dict[str, Any]) -> list[str]:
    lines = ["BLOCKERS"]
    blockers = _rows(heartbeat, "entry_blockers")
    if blockers is None:
        lines.append(_row("entry blockers", UNKNOWN))
        return lines
    if not blockers:
        lines.append(_row("entry blockers", "none"))
        return lines

    lines.append(_row("entry blockers", f"{len(blockers)}"))
    grouped: dict[str, list[str]] = {}
    for row in blockers:
        grouped.setdefault(_text(row, "reason"), []).append(
            f"{_text(row, 'strategy')}/{_text(row, 'symbol')}"
        )
    for reason in sorted(grouped):
        members = grouped[reason]
        lines.append(_row(f"{reason} ({len(members)})", " ".join(sorted(members))))
    return lines


def render(heartbeat: dict[str, Any], now_ms: int) -> str:
    sections = [_health(heartbeat, now_ms), _exposure(heartbeat), _blockers(heartbeat)]
    return "\n\n".join("\n".join(section) for section in sections) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("heartbeat", type=Path, help="path to the engine's heartbeat.json")
    parser.add_argument(
        "--now-ms",
        type=int,
        default=None,
        help="wall clock in epoch milliseconds for the heartbeat age (default: now)",
    )
    arguments = parser.parse_args(argv)

    try:
        raw = arguments.heartbeat.read_text(encoding="utf-8")
    except OSError as error:
        print(f"heartbeat unreadable: {error.strerror or error}", file=sys.stderr)
        return 1
    try:
        heartbeat = json.loads(raw)
    except ValueError as error:
        print(f"heartbeat unreadable: {error}", file=sys.stderr)
        return 1
    if not isinstance(heartbeat, dict):
        print("heartbeat unreadable: not a JSON object", file=sys.stderr)
        return 1

    now_ms = arguments.now_ms if arguments.now_ms is not None else int(time.time() * 1000)
    sys.stdout.write(render(heartbeat, now_ms))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
