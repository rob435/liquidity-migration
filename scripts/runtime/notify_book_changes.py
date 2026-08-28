"""Trade updates for the phone: what each sleeve asked for, and what it made.

Two sources, because they answer different questions. The **target books**
say what a sleeve decided — a new symbol with size is an entry, and that is
news the moment it is decided, before anything fills. The **engine's
closed-trade file** says what actually happened when a position ended: the
prices, the fees, the time held, and the money. An exit is worth reading only
with those numbers next to it, so exits come from the engine and entries from
the books.

The look is deliberate and small. Two dots — 🟢 made money, 🔴 lost it — on
the messages that carry a verdict, and bare text on the ones that do not; the
verdict and the money lead, bold, because the phone's notification preview
shows one line and that line is the whole point. Prices carry four
significant figures: past that they are texture, and the percent figure
already says what moved. Returns read as percent of the position, never
basis points. Messages are Telegram HTML, so every symbol and
sleeve name is escaped here.

Every message names its account: RM is the funded account (real money), DEMO
is the demo.

**Net here is after the venue's fees and nothing else.** The crowd fee
(funding) is settled into the wallet on the venue's own clock and the engine
is never told about it, so no number here carries it. The daily summary says
so once a day.

Messages go to the main line (the owner's DM with the bot); the group is the
alerting line and gets nothing from here.

First run on an empty state baselines silently, and so does a book or a trade
file seen for the first time. An unreadable book keeps its previous state — a
producer mid-write must not read as a mass exit.
"""

from __future__ import annotations

import html
import json
import math
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
    #: Prefixed to the sleeve in every message from this account. Empty for
    #: the one whose messages need no explaining.
    tag: str
    books: dict[str, str]
    #: Where the engine appends one JSON line per closed position. Absent
    #: means no engine is reporting, and exits fall back to the books.
    trades: str


ACCOUNTS = (
    Account(
        name="demo",
        tag="DEMO ",
        books={
            "CARRY": f"{TARGETS}/carry-demo.json",
            "LONG": f"{TARGETS}/long-demo.json",
            "EXODUS": f"{TARGETS}/exodus-demo.json",
        },
        trades="/var/lib/liquidity-migration-engine/trades.jsonl",
    ),
    Account(
        name="funded",
        tag="RM ",
        books={
            "CARRY": f"{TARGETS}/carry-mainnet.json",
            "LONG": f"{TARGETS}/long-mainnet.json",
            "EXODUS": f"{TARGETS}/exodus-mainnet.json",
        },
        trades="/var/lib/liquidity-migration-engine-mainnet/trades.jsonl",
    ),
)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Formatting. Everything below returns Telegram HTML.
# --------------------------------------------------------------------------


def esc(text: object) -> str:
    """Telegram HTML rejects a stray ``<`` outright — the message never
    arrives. So everything dynamic passes through here."""

    return html.escape(str(text), quote=False)


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

    sleeve = esc(str(trade.get("sleeve", "?")).upper())
    symbol = esc(trade.get("symbol", "?"))
    side = esc(trade.get("side", "?"))
    round_trip = trade.get("round_trip")

    if not round_trip:
        # The fills that opened it are in a log segment the engine no longer
        # replays. The close is still news; the money is not knowable.
        return "\n".join(
            [
                f"{tag}{sleeve} closed {symbol} · {side}"
                f" · out {price(float(trade.get('exit_px', 0.0)))}",
                "opened before this log, so what it made is unknown",
            ]
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
            f"{'🟢' if net >= 0 else '🔴'} {tag}{sleeve} <b>{money(net)}</b> · {symbol}",
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
    """What a sleeve has decided to hold that it did not before."""

    verb = "shorts" if sleeve == "EXODUS" else "enters"
    return [
        f"{tag}{esc(sleeve)} {verb} {esc(symbol)} · {notional(now[symbol])}"
        for symbol in sorted(set(now) - set(before))
    ]


def book_exit_messages(
    sleeve: str, tag: str, before: dict[str, float], now: dict[str, float]
) -> list[str]:
    """Exits with nothing to say about them, for an account whose engine is
    not writing closed trades."""

    verb = "covers" if sleeve == "EXODUS" else "exits"
    return [
        f"{tag}{esc(sleeve)} {verb} {esc(symbol)}"
        for symbol in sorted(set(before) - set(now))
    ]


def daily_summary(trades: list[dict], day: str) -> str | None:
    """What the closed positions made yesterday. The dot is the day's colour."""

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
        f"{'🟢' if total >= 0 else '🔴'} <b>{human_day(day)}</b>"
        f" · {trips} · {score} · <b>{money(total)}</b>"
    ]

    by_sleeve: dict[str, list[float]] = {}
    for trade in priced:
        # The account is part of the row's name: real money and demo run the
        # same sleeves, and one row adding both would put play money and the
        # owner's own in a single figure.
        label = esc(str(trade.get("account_tag", "")) + str(trade["sleeve"]).upper())
        by_sleeve.setdefault(label, []).append(float(trade["round_trip"]["net_usdt"]))
    name_w = max(len(name) for name in by_sleeve)
    sums = {name: sum(rows) for name, rows in by_sleeve.items()}
    money_w = max(len(money(v)) for v in sums.values())
    rows = []
    for name in sorted(by_sleeve):
        wins = sum(1 for net in by_sleeve[name] if net > 0)
        record = f"{wins}–{len(by_sleeve[name]) - wins}"
        rows.append(f"{name:<{name_w}}  {record:>5}  {money(sums[name]):>{money_w}}")
    lines.append("<pre>" + "\n".join(rows) + "</pre>")

    if len(priced) >= 2:
        best = max(priced, key=lambda t: float(t["round_trip"]["net_usdt"]))
        worst = min(priced, key=lambda t: float(t["round_trip"]["net_usdt"]))
        lines.append(
            f"best {money(float(best['round_trip']['net_usdt']))}"
            f" · {esc(str(best.get('account_tag', '')) + str(best['sleeve']).upper())}"
            f" {esc(best['symbol'])}"
        )
        lines.append(
            f"worst {money(float(worst['round_trip']['net_usdt']))}"
            f" · {esc(str(worst.get('account_tag', '')) + str(worst['sleeve']).upper())}"
            f" {esc(worst['symbol'])}"
        )

    lines.append("<i>after fees — funding settles to the wallet separately</i>")
    unpriced = len(trades) - len(priced)
    if unpriced:
        word = "trip" if unpriced == 1 else "trips"
        lines.append(f"<i>{unpriced} {word} unpriced — opened before this log</i>")
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


def send(messages: list[str], *, enabled: bool) -> None:
    for body in batched(messages):
        try:
            sent = send_telegram_message(
                body, enabled=enabled, channel="main", parse_mode="HTML"
            )
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
        new_trades = None
        read = read_new_trades(account.trades, int(offsets.get(account.trades, 0)))
        if read is not None:
            new_trades, offset = read
            if account.trades not in offsets:
                # Everything already in the file happened before this reader
                # existed. Baseline to the end of it rather than announcing a
                # history.
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

    state["books"] = books_now
    state["trade_offsets"] = offsets
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
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
                        # Which account it was is known only here, by which
                        # file the line came out of; the line does not say.
                        trade["account_tag"] = account.tag
                        out.append(trade)
        except OSError:
            continue
    return out


if __name__ == "__main__":
    main()
