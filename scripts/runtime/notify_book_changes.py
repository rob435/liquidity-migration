"""Trade updates for the phone: what filled, and what it made.

The engine heartbeat says which attributed positions actually exist. A new
LONG, CARRY, or Exodus row is an entry only after the account owner can see the
fill. The engine's closed-trade file says what happened when a position ended:
prices, fees, time held, and money. Only engine account evidence is authoritative.

The look is deliberate and small. Every message is one monospace block, so
columns line up and the whole thing can be copied in a tap. Two dots — 🟢
made money, 🔴 lost it — on the messages that carry a verdict, and bare text
on the ones that do not; the verdict and the money lead, because the phone's
notification preview shows one line and that line is the whole point. Prices
carry four significant figures: past that they are texture, and the percent
figure already says what moved. Returns read as percent of the position,
never basis points. Builders write plain text; `as_block` escapes it.

Every message names its account: RM is the funded account (real money), DEMO
is the demo.

**Net here is after the venue's fees and nothing else.** The crowd fee
(funding) is settled into the wallet on the venue's own clock and the engine
is never told about it, so no number here carries it.

Sleeves that only exercise the machinery are named in `HIDDEN_SLEEVES`: they
are printed to the log and kept out of every message and every total.

Messages go to the main line (the owner's DM with the bot); the group is the
alerting line and gets nothing from here.

First run on an empty state baselines silently, and so does a trade file seen
for the first time. An unreadable, stale, or unattributed account snapshot
keeps its previous state instead of inventing a position change.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from liquidity_migration.ops.telegram import as_block, send_telegram_message

STATE_PATH = "/var/lib/liquidity-migration/trade-notify/state.json"
HEARTBEAT_MAX_AGE_MS = 5 * 60_000
DIRECTIONAL_SLEEVES = frozenset({"carry", "long", "exodus"})

#: Sleeves that exist to exercise the machinery rather than to make money.
#: Their trades reach the log and nothing else — not a message, not a total.
HIDDEN_SLEEVES = frozenset({"maker_canary"})

#: Telegram refuses a message past 4096 characters; a batch is split under it.
MAX_MESSAGE_CHARS = 3_500


@dataclass(frozen=True)
class Account:
    name: str
    #: Prefixed to the sleeve in every message from this account. Empty for
    #: the one whose messages need no explaining.
    tag: str
    realm: str
    heartbeat: str
    #: Where the engine appends one JSON line per closed position. If it is
    #: absent there is no closed-trade update; no other file substitutes.
    trades: str


@dataclass(frozen=True)
class TradeRead:
    trades: list[dict]
    next_offset: int
    malformed_offset: int | None = None


ACCOUNTS = (
    Account(
        name="demo",
        tag="DEMO ",
        realm="demo",
        heartbeat="/var/lib/liquidity-migration-engine/heartbeat.json",
        trades="/var/lib/liquidity-migration-engine/trades.jsonl",
    ),
    Account(
        name="funded",
        tag="RM ",
        realm="mainnet",
        heartbeat="/var/lib/liquidity-migration-engine-mainnet/heartbeat.json",
        trades="/var/lib/liquidity-migration-engine-mainnet/trades.jsonl",
    ),
)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionRead:
    positions: dict[str, dict[str, float]]
    ambiguous_symbols: frozenset[str]


def read_attributed_positions(
    path: str,
    *,
    realm: str,
    now_ms: int | None = None,
) -> PositionRead | None:
    """Directional sleeve -> symbol -> filled notional, or None when unsafe."""

    if now_ms is None:
        now_ms = int(time.time() * 1000)
    try:
        with open(path) as fh:
            payload = json.load(fh)
        if (
            not isinstance(payload, dict)
            or payload.get("mode") != "live"
            or payload.get("realm") != realm
            or type(payload.get("wall_ts_ms")) is not int
            or not 0 <= now_ms - payload["wall_ts_ms"] <= HEARTBEAT_MAX_AGE_MS
            or not isinstance(payload.get("positions"), list)
        ):
            return None
        positions = {sleeve.upper(): {} for sleeve in DIRECTIONAL_SLEEVES}
        ambiguous: set[str] = set()
        seen: set[str] = set()
        for row in payload["positions"]:
            if not isinstance(row, dict):
                return None
            symbol = row.get("symbol")
            side = row.get("side")
            strategy = row.get("strategy")
            qty = row.get("qty")
            entry_px = row.get("entry_px")
            if (
                not isinstance(symbol, str)
                or not symbol
                or symbol in seen
                or side not in {"long", "short"}
                or type(qty) not in {int, float}
                or type(entry_px) not in {int, float}
                or not math.isfinite(float(qty))
                or not math.isfinite(float(entry_px))
                or float(qty) <= 0.0
                or float(entry_px) <= 0.0
            ):
                return None
            seen.add(symbol)
            if strategy is None:
                ambiguous.add(symbol)
                continue
            if not isinstance(strategy, str):
                return None
            sleeve = strategy.lower()
            if sleeve in DIRECTIONAL_SLEEVES:
                positions[sleeve.upper()][symbol] = float(qty) * float(entry_px)
        return PositionRead(positions, frozenset(ambiguous))
    except (OSError, ValueError, TypeError):
        return None


def read_new_trades(path: str, offset: int) -> TradeRead | None:
    """Whole JSON lines added since `offset`, and where to read from next.

    None when the file is not there at all. A file shorter than the offset
    was replaced under us: it re-baselines rather than replaying, because
    losing a few messages beats sending hundreds at once.
    """

    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size < offset:
        return TradeRead(trades=[], next_offset=size)
    trades: list[dict] = []
    with open(path, "rb") as fh:
        fh.seek(offset)
        body = fh.read()
    consumed = 0
    malformed_offset: int | None = None
    for raw_line in body.splitlines(keepends=True):
        # A line still being written has no newline yet; the rest waits for the
        # next run rather than being parsed in half.
        if not raw_line.endswith(b"\n"):
            break
        line = raw_line.strip()
        if not line:
            consumed += len(raw_line)
            continue
        try:
            trade = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed_offset = offset + consumed
            break
        if not isinstance(trade, dict):
            malformed_offset = offset + consumed
            break
        trades.append(trade)
        consumed += len(raw_line)
    return TradeRead(
        trades=trades,
        next_offset=offset + consumed,
        malformed_offset=malformed_offset,
    )


# --------------------------------------------------------------------------
# Formatting. Everything below returns Telegram HTML.
# --------------------------------------------------------------------------


def hidden(trade: dict) -> bool:
    return str(trade.get("sleeve", "")).lower() in HIDDEN_SLEEVES


def money(usdt: float) -> str:
    """Signed, always: the sign is the whole message."""

    if abs(usdt) < 0.01 and usdt != 0.0:
        return f"{'+' if usdt > 0 else '-'}${abs(usdt):.4f}"
    return f"{'+' if usdt >= 0 else '-'}${abs(usdt):,.2f}"


def notional(usdt: float) -> str:
    """An entry's size. Cents on a sizing figure are noise past $100."""

    if usdt >= 100:
        return f"${usdt:,.0f}"
    return f"${usdt:,.2f}"


def price(value: float) -> str:
    """Four significant figures. 0.06845589 → 0.06846: on a phone the rest is
    texture, and the percent figure already carries the move."""

    if not math.isfinite(value) or value == 0.0:
        return str(value)
    if abs(value) >= 1_000:
        return f"{value:,.0f}"
    return f"{value:.4g}"


def quantity(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:,.0f}"
    return price(value)


def held(ms: int) -> str:
    seconds = max(int(ms), 0) // 1000
    days = seconds // 86_400
    hours = (seconds % 86_400) // 3_600
    minutes = (seconds % 3_600) // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def percent(bps: float) -> str:
    """A basis-point figure as percent of the position, which is the unit
    the owner reads. Two decimals for a return; two significant figures for
    slip, which lives near a hundredth of a percent and would read as a
    measured zero at return precision."""

    pct = bps / 100.0
    if abs(pct) >= 0.01 or pct == 0.0:
        return f"{pct:+.2f}%"
    return f"{pct:+.2g}%"


def human_day(day: str) -> str:
    """2026-08-23 → Sun 23 Aug."""

    d = datetime.strptime(day, "%Y-%m-%d")
    return f"{d:%a} {d.day} {d:%b}"


def exit_message(trade: dict, tag: str) -> str:
    """One closed position. The verdict is the first line, and the money is
    bold — the phone's notification preview shows nothing else."""

    sleeve = str(trade.get("sleeve", "?")).upper()
    symbol = trade.get("symbol", "?")
    side = trade.get("side", "?")
    round_trip = trade.get("round_trip")

    if not round_trip:
        # The fills that opened it are in a log segment the engine no longer
        # replays. The close is still news; the money is not knowable.
        return (
            f"{tag}{sleeve} closed {symbol} · {side}"
            f" · out {price(float(trade.get('exit_px', 0.0)))} · unpriced"
        )

    net = float(round_trip["net_usdt"])
    stats = [f"{notional(float(round_trip['entry_notional_usdt']))}"]
    stats.append(f"fee ${float(round_trip['fees_usdt']):.2f}")
    share = trade.get("maker_share")
    if share is not None:
        stats.append(f"maker {float(share) * 100:.0f}%")
    slip = trade.get("arrival_shortfall_bps")
    if slip is not None:
        # The engine's convention is positive-when-adverse; a signed number
        # on the phone would sit next to a net where positive means made
        # money. The verb carries the direction instead, no sign to misread.
        cost = float(slip)
        if cost == 0.0:
            stats.append("slip 0.00%")
        else:
            verb = "paid" if cost > 0 else "saved"
            stats.append(f"slip {verb} {percent(abs(cost)).lstrip('+')}")
    return "\n".join(
        [
            f"{'🟢' if net >= 0 else '🔴'} {tag}{sleeve} {money(net)} · {symbol}",
            f"{side} {held(int(round_trip['held_ms']))}"
            f" · {price(float(round_trip['entry_px']))}"
            f" → {price(float(trade.get('exit_px', 0.0)))}"
            f" · {percent(float(round_trip['net_bps']))}",
            " · ".join(stats),
        ]
    )


def entry_messages(
    sleeve: str, tag: str, before: dict[str, float], now: dict[str, float]
) -> list[str]:
    """Attributed filled positions present now and absent before."""

    verb = "shorts" if sleeve == "EXODUS" else "enters"
    return [
        f"{tag}{sleeve} {verb} {symbol} · {notional(now[symbol])}"
        for symbol in sorted(set(now) - set(before))
    ]


def daily_summary(trades: list[dict], day: str) -> str | None:
    """What the closed positions made yesterday. The dot is the day's colour."""

    trades = [t for t in trades if not hidden(t)]
    priced = [t for t in trades if t.get("round_trip")]
    if not priced:
        return None
    nets = [float(t["round_trip"]["net_usdt"]) for t in priced]
    total = sum(nets)
    won = sum(1 for net in nets if net > 0)
    if len(nets) == 1:
        score = "won" if won else "lost"
    elif won == len(nets):
        score = "all won"
    elif won == 0:
        score = "none won"
    else:
        score = f"{won} won"
    trips = "1 trip" if len(nets) == 1 else f"{len(nets)} trips"
    lines = [
        f"{'🟢' if total >= 0 else '🔴'} {human_day(day)}"
        f" · {trips} · {score} · {money(total)}",
        "",
    ]

    by_sleeve: dict[str, list[float]] = {}
    for trade in priced:
        # The account is part of the row's name: real money and demo run the
        # same sleeves, and one row adding both would put play money and the
        # owner's own in a single figure.
        label = str(trade.get("account_tag", "")) + str(trade["sleeve"]).upper()
        by_sleeve.setdefault(label, []).append(float(trade["round_trip"]["net_usdt"]))
    name_w = max(len(name) for name in by_sleeve)
    sums = {name: sum(rows) for name, rows in by_sleeve.items()}
    money_w = max(len(money(v)) for v in sums.values())
    rows = []
    for name in sorted(by_sleeve):
        wins = sum(1 for net in by_sleeve[name] if net > 0)
        record = f"{wins}–{len(by_sleeve[name]) - wins}"
        rows.append(f"{name:<{name_w}}  {record:>5}  {money(sums[name]):>{money_w}}")
    lines += rows

    if len(priced) >= 2:
        lines.append("")
        best = max(priced, key=lambda t: float(t["round_trip"]["net_usdt"]))
        worst = min(priced, key=lambda t: float(t["round_trip"]["net_usdt"]))
        lines.append(
            f"best  {money(float(best['round_trip']['net_usdt']))}"
            f" · {str(best.get('account_tag', '')) + str(best['sleeve']).upper()}"
            f" {best['symbol']}"
        )
        lines.append(
            f"worst {money(float(worst['round_trip']['net_usdt']))}"
            f" · {str(worst.get('account_tag', '')) + str(worst['sleeve']).upper()}"
            f" {worst['symbol']}"
        )

    unpriced = len(trades) - len(priced)
    if unpriced:
        word = "trip" if unpriced == 1 else "trips"
        lines.append(f"{unpriced} {word} unpriced")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


def batched(messages: list[str]) -> list[str]:
    """One message per run where they fit, so a busy minute is one buzz."""

    out: list[str] = []
    current = ""
    for message in messages:
        candidate = f"{current}\n\n{message}" if current else message
        if len(candidate) > MAX_MESSAGE_CHARS and current:
            out.append(current)
            current = message
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def send(messages: list[str], *, enabled: bool) -> bool:
    for body in batched(messages):
        try:
            sent = send_telegram_message(
                as_block(body), enabled=enabled, channel="main", parse_mode="HTML"
            )
        except Exception as exc:
            print(f"unsent ({exc.__class__.__name__}): {body.splitlines()[0]}")
            return False
        head = body.splitlines()[0]
        lines = body.count("\n\n") + 1
        print(f"{'sent' if sent else 'unsent'} {lines} update(s), first: {head}")
        if not sent:
            return False
    return True


def yesterday_utc(now_s: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now_s - 86_400))


def write_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as handle:
        handle.write(json.dumps(state, indent=1, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def main() -> int:
    state_path = Path(os.environ.get("BOOK_NOTIFY_STATE", STATE_PATH))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(state_path.read_text())
    except Exception:
        state = {}
    positions_before = dict(state.get("positions") or {})
    offsets = dict(state.get("trade_offsets") or {})

    enabled = os.environ.get("TELEGRAM_ENABLED", "").strip() == "1"
    messages: list[str] = []
    positions_now: dict[str, dict[str, float]] = {}
    malformed_trades: list[tuple[str, int]] = []

    for account in ACCOUNTS:
        read = read_new_trades(account.trades, int(offsets.get(account.trades, 0)))
        if read is not None:
            offset = read.next_offset
            if account.trades not in offsets:
                # Everything already in the file happened before this reader
                # existed. Baseline through its valid complete lines rather
                # than announcing a history. A malformed line remains the
                # boundary until it is repaired.
                print(f"baselined {account.trades} at {offset} bytes")
            else:
                for trade in read.trades:
                    if hidden(trade):
                        print(
                            f"not shown ({trade.get('sleeve')}):"
                            f" {account.tag}{trade.get('symbol')}"
                        )
                        continue
                    messages.append(exit_message(trade, account.tag))
            offsets[account.trades] = offset
            if read.malformed_offset is not None:
                malformed_trades.append((account.trades, read.malformed_offset))
                print(
                    f"malformed trade blocks {account.trades}"
                    f" at byte {read.malformed_offset}"
                )

        snapshot = read_attributed_positions(account.heartbeat, realm=account.realm)
        if snapshot is None:
            for sleeve in sorted(name.upper() for name in DIRECTIONAL_SLEEVES):
                key = f"{account.name}/{sleeve}"
                if key in positions_before:
                    positions_now[key] = positions_before[key]
            continue
        for sleeve in sorted(name.upper() for name in DIRECTIONAL_SLEEVES):
            key = f"{account.name}/{sleeve}"
            now = dict(snapshot.positions[sleeve])
            before = positions_before.get(key)
            if isinstance(before, dict):
                for symbol in snapshot.ambiguous_symbols:
                    if symbol in before and symbol not in now:
                        now[symbol] = before[symbol]
                messages += entry_messages(sleeve, account.tag, before, now)
            positions_now[key] = now

    # Once a day, on the first run after midnight UTC, over the day that just
    # ended. Stamped by the day it covers, so a run that could not send tries
    # again rather than skipping it.
    day = yesterday_utc(time.time())
    if state.get("summarised_day") != day:
        summary = daily_summary(trades_of_day(day), day)
        if summary is not None:
            messages.append(summary)
        state["summarised_day"] = day

    if not send(messages, enabled=enabled):
        print("notification state retained for retry")
        return 1
    if not messages:
        print("nothing to say")

    state.pop("books", None)
    state["positions"] = positions_now
    state["trade_offsets"] = offsets
    write_state(state_path, state)
    return 1 if malformed_trades else 0


def trades_of_day(day: str) -> list[dict]:
    """Every closed position stamped inside one UTC day, both accounts."""

    midnight = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = int(midnight.timestamp()) * 1000
    end = start + 86_400_000
    out = []
    for account in ACCOUNTS:
        try:
            with open(account.trades) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        trade = json.loads(line)
                    except Exception:
                        continue
                    closed_ms = int(trade.get("closed_ms", 0))
                    if start <= closed_ms < end:
                        # Which account it was is known only here, by which
                        # file the line came out of; the line does not say.
                        trade["account_tag"] = account.tag
                        out.append(trade)
                    elif closed_ms >= end + 60_000:
                        break
        except OSError:
            continue
    return out


if __name__ == "__main__":
    raise SystemExit(main())
