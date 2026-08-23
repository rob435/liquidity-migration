"""Trade updates for the phone: what each sleeve asked for, and what it made.

Two sources, because they answer different questions. The **target books**
say what a sleeve decided — a new symbol with size is an entry, and that is
news the moment it is decided, before anything fills. The **engine's
closed-trade file** says what actually happened when a position ended: the
prices, the fees, the time held, and the money. An exit is worth reading only
with those numbers next to it, so exits come from the engine and entries from
the books.

Both accounts are covered. The funded one is tagged; the demo one is not,
because that is the one that speaks most days.

**Net here is after the venue's fees and nothing else.** The crowd fee
(funding) is settled into the wallet on the venue's own clock and the engine
is never told about it, so no number here carries it. The daily summary says
so out loud once a day.

Messages go to the main line (the owner's DM with the bot); the group is the
alerting line and gets nothing from here.

First run on an empty state baselines silently, and so does a book or a trade
file seen for the first time. An unreadable book keeps its previous state — a
producer mid-write must not read as a mass exit.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from liquidity_migration.ops.telegram import send_telegram_message

TARGETS = "/var/lib/liquidity-migration/targets"
STATE_PATH = "/var/lib/liquidity-migration/book-notify/state.json"

#: Telegram refuses a message past 4096 characters; a batch is split under it.
MAX_MESSAGE_CHARS = 3_500


@dataclass(frozen=True)
class Account:
    name: str
    #: Prefixed to every message from this account. Empty for the one whose
    #: messages need no explaining.
    tag: str
    books: dict[str, str]
    #: Where the engine appends one JSON line per closed position. Absent
    #: means no engine is reporting, and exits fall back to the books.
    trades: str


ACCOUNTS = (
    Account(
        name="demo",
        tag="",
        books={
            "CARRY": f"{TARGETS}/carry-demo.json",
            "LONG": f"{TARGETS}/long-demo.json",
            "EXODUS": f"{TARGETS}/exodus-demo.json",
        },
        trades="/var/lib/liquidity-migration-engine/trades.jsonl",
    ),
    Account(
        name="funded",
        tag="[funded] ",
        books={
            "CARRY": f"{TARGETS}/carry-mainnet.json",
            "LONG": f"{TARGETS}/long-mainnet.json",
        },
        trades="/var/lib/liquidity-migration-engine-mainnet/trades.jsonl",
    ),
)


def read_positive_targets(path: str) -> dict[str, float] | None:
    """Symbol -> notional for rows with size, or None when unreadable."""

    try:
        with open(path) as fh:
            book = json.load(fh)
        out: dict[str, float] = {}
        for row in book.get("targets") or []:
            notional = abs(float(row.get("notional_usdt", 0.0)))
            if notional > 0.0:
                out[str(row["symbol"])] = notional
        return out
    except Exception:
        return None


def read_new_trades(path: str, offset: int) -> tuple[list[dict], int] | None:
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
        return [], size
    trades = []
    with open(path) as fh:
        fh.seek(offset)
        body = fh.read()
    # A line still being written has no newline yet; the rest waits for the
    # next run rather than being parsed in half.
    consumed = body.rfind("\n") + 1
    for line in body[:consumed].splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            trades.append(json.loads(line))
        except Exception:
            continue
    return trades, offset + consumed


def money(usdt: float) -> str:
    """Signed, and always with its sign: the sign is the whole message."""

    if abs(usdt) < 0.01 and usdt != 0.0:
        return f"{'+' if usdt > 0 else '-'}${abs(usdt):.4f}"
    return f"{'+' if usdt >= 0 else '-'}${abs(usdt):.2f}"


def price(value: float) -> str:
    """A price at the precision it has: this fleet trades both 100,000 of a
    coin worth 0.0037 and 0.05 of one worth 800."""

    magnitude = abs(value)
    if magnitude >= 1_000:
        text = f"{value:.1f}"
    elif magnitude >= 1:
        text = f"{value:.4f}"
    elif magnitude > 0:
        text = f"{value:.8f}"
    else:
        return str(value)
    return text.rstrip("0").rstrip(".") if "." in text else text


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


def exit_message(trade: dict, tag: str) -> str:
    """One closed position, in four lines or fewer."""

    sleeve = str(trade.get("sleeve", "?")).upper()
    symbol = trade.get("symbol", "?")
    side = trade.get("side", "?")
    round_trip = trade.get("round_trip")

    stats = []
    share = trade.get("maker_share")
    if share is not None:
        stats.append(f"rested {share * 100:.0f}%")
    slip = trade.get("arrival_shortfall_bps")
    if slip is not None:
        stats.append(f"slip {slip:+.1f} bp")

    if not round_trip:
        # The fills that opened it are in a log segment the engine no longer
        # replays. The close is still news; what it made is not knowable.
        lines = [
            f"⚪ {tag}{sleeve} exit {symbol} {side}",
            f"out {price(float(trade.get('exit_px', 0.0)))} · {quantity(float(trade.get('qty', 0.0)))}",
            "what it made is not in the engine's current log",
        ]
        return "\n".join(lines)

    net = float(round_trip["net_usdt"])
    fills = int(trade.get("fills", 0))
    stats.append(f"fee ${float(round_trip['fees_usdt']):.2f}")
    return "\n".join(
        [
            f"{'🟢' if net >= 0 else '🔴'} {tag}{sleeve} exit {symbol} {side}",
            f"{money(net)} after fees · {float(round_trip['net_bps']):+.0f} bp"
            f" · held {held(int(round_trip['held_ms']))}",
            f"in {price(float(round_trip['entry_px']))}"
            f" → out {price(float(trade.get('exit_px', 0.0)))}"
            f" · ${float(round_trip['entry_notional_usdt']):,.0f} · {fills} fills",
            " · ".join(stats),
        ]
    )


def entry_messages(sleeve: str, tag: str, before: dict[str, float], now: dict[str, float]) -> list[str]:
    """What a sleeve has decided to hold that it did not before."""

    out = []
    for symbol in sorted(set(now) - set(before)):
        verb = "short" if sleeve == "EXODUS" else "entry"
        out.append(f"⚡ {tag}{sleeve} {verb} {symbol} ${now[symbol]:,.2f}")
    return out


def book_exit_messages(sleeve: str, tag: str, before: dict[str, float], now: dict[str, float]) -> list[str]:
    """Exits with nothing to say about them, for an account whose engine is
    not writing closed trades."""

    verb = "covered" if sleeve == "EXODUS" else "exit"
    return [f"⚪ {tag}{sleeve} {verb} {symbol}" for symbol in sorted(set(before) - set(now))]


def daily_summary(trades: list[dict], day: str) -> str | None:
    """What the closed positions made yesterday, per sleeve."""

    priced = [t for t in trades if t.get("round_trip")]
    if not priced:
        return None
    nets = [float(t["round_trip"]["net_usdt"]) for t in priced]
    won = sum(1 for net in nets if net > 0)
    lines = [
        f"📊 {day} · {len(priced)} closed",
        f"{won} won ({100 * won / len(priced):.0f}%) · {money(sum(nets))} after fees",
        "",
    ]
    sleeves: dict[str, list[float]] = {}
    for trade in priced:
        sleeves.setdefault(str(trade["sleeve"]).upper(), []).append(
            float(trade["round_trip"]["net_usdt"])
        )
    for sleeve in sorted(sleeves):
        rows = sleeves[sleeve]
        share = 100 * sum(1 for net in rows if net > 0) / len(rows)
        lines.append(f"{sleeve} {len(rows)} · {share:.0f}% won · {money(sum(rows))}")
    best = max(priced, key=lambda t: float(t["round_trip"]["net_usdt"]))
    worst = min(priced, key=lambda t: float(t["round_trip"]["net_usdt"]))
    lines += [
        "",
        f"best {money(float(best['round_trip']['net_usdt']))}"
        f" {str(best['sleeve']).upper()} {best['symbol']}",
        f"worst {money(float(worst['round_trip']['net_usdt']))}"
        f" {str(worst['sleeve']).upper()} {worst['symbol']}",
        "",
        "the crowd fee (funding) is not in these numbers: the venue settles it"
        " into the wallet and never tells the engine.",
    ]
    unpriced = len(trades) - len(priced)
    if unpriced:
        lines.append(f"{unpriced} close(s) left out: opened before the engine's current log.")
    return "\n".join(lines)


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


def send(messages: list[str], *, enabled: bool) -> None:
    for body in batched(messages):
        try:
            sent = send_telegram_message(body, enabled=enabled, channel="main")
        except Exception as exc:
            print(f"unsent ({exc.__class__.__name__}): {body.splitlines()[0]}")
            continue
        head = body.splitlines()[0]
        lines = body.count("\n\n") + 1
        print(f"{'sent' if sent else 'unsent'} {lines} update(s), first: {head}")


def yesterday_utc(now_s: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now_s - 86_400))


def main() -> None:
    state_path = Path(os.environ.get("BOOK_NOTIFY_STATE", STATE_PATH))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(state_path.read_text())
    except Exception:
        state = {}
    books_before = dict(state.get("books") or {})
    offsets = dict(state.get("trade_offsets") or {})

    enabled = os.environ.get("TELEGRAM_ENABLED", "").strip() == "1"
    messages: list[str] = []
    books_now: dict[str, dict[str, float]] = {}

    for account in ACCOUNTS:
        first_sight = account.trades not in offsets
        read = read_new_trades(account.trades, int(offsets.get(account.trades, 0)))
        new_trades = None
        if read is None:
            # No file yet. Remembering that we looked is what makes the first
            # trade ever written news rather than history: without it, the
            # run that finds the file also decides it has always been there.
            offsets[account.trades] = 0
        else:
            new_trades, offset = read
            if first_sight:
                # A file that was already there when this reader first looked
                # holds trades from before it existed. Baseline to the end
                # rather than announcing a history.
                offset = os.path.getsize(account.trades)
                print(f"baselined {account.trades} at {offset} bytes")
            else:
                for trade in new_trades:
                    messages.append(exit_message(trade, account.tag))
            offsets[account.trades] = offset

        for sleeve, path in account.books.items():
            key = f"{account.name}/{sleeve}"
            now = read_positive_targets(path)
            if now is None:
                # Mid-write, or gone. Keeping the old state is what stops a
                # transient read looking like every position closing at once.
                if key in books_before:
                    books_now[key] = books_before[key]
                continue
            before = books_before.get(key)
            if isinstance(before, dict):
                messages += entry_messages(sleeve, account.tag, before, now)
                if new_trades is None:
                    messages += book_exit_messages(sleeve, account.tag, before, now)
            books_now[key] = now

    # Once a day, on the first run after midnight UTC, over the day that just
    # ended. Stamped by the day it covers, so a run that could not send tries
    # again rather than skipping it.
    day = yesterday_utc(time.time())
    if state.get("summarised_day") != day:
        summary = daily_summary(trades_of_day(day), day)
        if summary is not None:
            messages.append(summary)
        state["summarised_day"] = day

    send(messages, enabled=enabled)
    if not messages:
        print("nothing to say")

    # Written whole, so a key this reader no longer keeps does not linger.
    kept = {
        "books": books_now,
        "trade_offsets": offsets,
        "summarised_day": state.get("summarised_day"),
    }
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(kept, indent=1, sort_keys=True))
    tmp.replace(state_path)


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
                    if start <= int(trade.get("closed_ms", 0)) < end:
                        out.append(trade)
        except OSError:
            continue
    return out


if __name__ == "__main__":
    main()
