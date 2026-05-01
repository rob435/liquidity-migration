from __future__ import annotations

import argparse
import asyncio
import csv
import html
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from database import SignalDatabase


MESSAGE_RE = re.compile(
    r'<div class="message default clearfix(?: joined)?".*?'
    r'<div class="pull_right date details" title="(?P<title>[^"]+)">.*?</div>'
    r'(?:(?:.|\n)*?<div class="from_name">\s*(?P<sender>[^<]+)\s*</div>)?'
    r'(?:(?:.|\n)*?<div class="text">(?P<text>.*?)</div>)',
    re.S,
)


@dataclass(slots=True)
class ActualTradeEvent:
    timestamp: str
    ticker: str
    event: str
    side: str
    reason: str | None = None


@dataclass(slots=True)
class MatchedTradeEvent:
    ticker: str
    event: str
    side: str
    actual_timestamp: str
    backtest_timestamp: str
    delta_seconds: float
    actual_reason: str | None = None
    backtest_reason: str | None = None
    reason_match: bool | None = None


@dataclass(slots=True)
class ReconciliationSummary:
    actual_entries: int
    backtest_entries: int
    matched_entries: int
    entry_precision: float
    entry_recall: float
    avg_entry_delta_seconds: float | None
    actual_exits: int
    backtest_exits: int
    matched_exits: int
    exit_precision: float
    exit_recall: float
    avg_exit_delta_seconds: float | None
    matched_exit_reason_count: int
    mismatched_exit_reason_count: int
    actual_unique_tickers: int
    backtest_unique_tickers: int
    matched_unique_tickers: int
    ticker_precision: float
    ticker_recall: float


@dataclass(slots=True)
class TickerReconciliationSummary:
    ticker: str
    actual_entries: int
    backtest_entries: int
    matched_entries: int
    actual_exits: int
    backtest_exits: int
    matched_exits: int
    avg_entry_delta_seconds: float | None
    avg_exit_delta_seconds: float | None
    matched_exit_reason_count: int
    mismatched_exit_reason_count: int


@dataclass(slots=True)
class ReconciliationResult:
    summary: ReconciliationSummary
    tickers: list[TickerReconciliationSummary]
    matched_entries: list[MatchedTradeEvent]
    matched_exits: list[MatchedTradeEvent]
    unmatched_actual_entries: list[ActualTradeEvent]
    unmatched_backtest_entries: list[ActualTradeEvent]
    unmatched_actual_exits: list[ActualTradeEvent]
    unmatched_backtest_exits: list[ActualTradeEvent]


def _clean_html_text(raw: str) -> str:
    text = raw.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def parse_telegram_trade_events(html_text: str) -> list[ActualTradeEvent]:
    events: list[ActualTradeEvent] = []
    last_sender: str | None = None
    for match in MESSAGE_RE.finditer(html_text):
        sender = (match.group("sender") or last_sender or "").strip()
        if sender:
            last_sender = sender
        if "BybitDemo_bot" not in sender:
            continue
        text = _clean_html_text(match.group("text") or "")
        title = match.group("title")
        if not text or not title:
            continue
        timestamp = datetime.strptime(title, "%d.%m.%Y %H:%M:%S UTC+00:00").isoformat() + "+00:00"
        entry_match = re.match(r"(?P<ticker>[A-Z0-9]+USDT) entered new (?P<side>long|short) position", text)
        if entry_match:
            events.append(
                ActualTradeEvent(
                    timestamp=timestamp,
                    ticker=entry_match.group("ticker"),
                    event="entry",
                    side=entry_match.group("side").upper(),
                    reason="entry",
                )
            )
            continue
        exit_match = re.match(
            r"(?P<ticker>[A-Z0-9]+USDT) (?P<label>take-profit exit|stop-loss exit|venue-managed exit|exchange exit)",
            text,
        )
        if exit_match:
            events.append(
                ActualTradeEvent(
                    timestamp=timestamp,
                    ticker=exit_match.group("ticker"),
                    event="exit",
                    side="LONG",
                    reason=exit_match.group("label"),
                )
            )
    return events


def load_backtest_trade_events(csv_path: str) -> list[ActualTradeEvent]:
    events: list[ActualTradeEvent] = []
    with Path(csv_path).expanduser().open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ticker = str(row.get("ticker", "")).strip()
            side = str(row.get("side", "LONG")).strip().upper()
            opened_at = str(row.get("opened_at", "")).strip()
            closed_at = str(row.get("closed_at", "")).strip()
            if ticker and opened_at:
                events.append(
                    ActualTradeEvent(
                        timestamp=opened_at,
                        ticker=ticker,
                        event="entry",
                        side=side,
                        reason="entry",
                    )
                )
            if ticker and closed_at:
                events.append(
                    ActualTradeEvent(
                        timestamp=closed_at,
                        ticker=ticker,
                        event="exit",
                        side=side,
                        reason=str(row.get("exit_reason", "")).strip() or None,
                    )
                )
    return events


def previous_utc_date_label() -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()


def resolve_trade_date(trade_date: str | None) -> str:
    return trade_date or previous_utc_date_label()


def load_actual_trade_events_from_db(db_path: str, *, trade_date: str | None = None) -> list[ActualTradeEvent]:
    resolved_trade_date = resolve_trade_date(trade_date)
    database = SignalDatabase(str(Path(db_path).expanduser()))
    try:
        rows = database._conn.execute(
            """
            SELECT ticker, side, opened_at, closed_at, exit_reason
            FROM trade_analytics
            WHERE substr(opened_at, 1, 10) = ?
               OR substr(closed_at, 1, 10) = ?
            ORDER BY opened_at ASC, closed_at ASC, ticker ASC
            """,
            (resolved_trade_date, resolved_trade_date),
        ).fetchall()
    finally:
        database.close()

    events: list[ActualTradeEvent] = []
    for row in rows:
        ticker = str(row["ticker"]).strip()
        side = str(row["side"]).strip().upper()
        opened_at = str(row["opened_at"]).strip()
        closed_at = str(row["closed_at"]).strip()
        exit_reason = str(row["exit_reason"] or "").strip() or None
        if ticker and opened_at.startswith(resolved_trade_date):
            events.append(
                ActualTradeEvent(
                    timestamp=opened_at,
                    ticker=ticker,
                    event="entry",
                    side=side,
                    reason="entry",
                )
            )
        if ticker and closed_at.startswith(resolved_trade_date):
            events.append(
                ActualTradeEvent(
                    timestamp=closed_at,
                    ticker=ticker,
                    event="exit",
                    side=side,
                    reason=exit_reason,
                )
            )
    return events


def _match_events(
    actual: list[ActualTradeEvent],
    backtest: list[ActualTradeEvent],
    *,
    tolerance_minutes: int,
) -> tuple[list[MatchedTradeEvent], list[ActualTradeEvent], list[ActualTradeEvent]]:
    tolerance_seconds = tolerance_minutes * 60
    used_backtest: set[int] = set()
    matched: list[MatchedTradeEvent] = []
    unmatched_actual: list[ActualTradeEvent] = []
    for event in actual:
        event_dt = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
        best_index: int | None = None
        best_delta: float | None = None
        for index, candidate in enumerate(backtest):
            if index in used_backtest:
                continue
            if (
                candidate.ticker != event.ticker
                or candidate.event != event.event
                or candidate.side != event.side
            ):
                continue
            candidate_dt = datetime.fromisoformat(candidate.timestamp.replace("Z", "+00:00"))
            delta = abs((candidate_dt - event_dt).total_seconds())
            if delta > tolerance_seconds:
                continue
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_index = index
        if best_index is None:
            unmatched_actual.append(event)
            continue
        used_backtest.add(best_index)
        candidate = backtest[best_index]
        matched.append(
            MatchedTradeEvent(
                ticker=event.ticker,
                event=event.event,
                side=event.side,
                actual_timestamp=event.timestamp,
                backtest_timestamp=candidate.timestamp,
                delta_seconds=float(best_delta or 0.0),
                actual_reason=event.reason,
                backtest_reason=candidate.reason,
                reason_match=(
                    None
                    if event.event != "exit"
                    else event.reason == candidate.reason
                ),
            )
        )
    unmatched_backtest = [event for index, event in enumerate(backtest) if index not in used_backtest]
    return matched, unmatched_actual, unmatched_backtest


def reconcile_trade_events(
    actual_events: list[ActualTradeEvent],
    backtest_events: list[ActualTradeEvent],
    *,
    tolerance_minutes: int = 30,
) -> ReconciliationResult:
    actual_entries = [event for event in actual_events if event.event == "entry"]
    backtest_entries = [event for event in backtest_events if event.event == "entry"]
    actual_exits = [event for event in actual_events if event.event == "exit"]
    backtest_exits = [event for event in backtest_events if event.event == "exit"]
    matched_entries, unmatched_actual_entries, unmatched_backtest_entries = _match_events(
        actual_entries,
        backtest_entries,
        tolerance_minutes=tolerance_minutes,
    )
    matched_exits, unmatched_actual_exits, unmatched_backtest_exits = _match_events(
        actual_exits,
        backtest_exits,
        tolerance_minutes=tolerance_minutes,
    )
    matched_exit_reason_count = sum(1 for row in matched_exits if row.reason_match is True)
    mismatched_exit_reason_count = sum(1 for row in matched_exits if row.reason_match is False)
    actual_tickers = {event.ticker for event in actual_events}
    backtest_tickers = {event.ticker for event in backtest_events}
    matched_tickers = {row.ticker for row in matched_entries} | {row.ticker for row in matched_exits}
    ticker_rows = _summarize_tickers(
        actual_entries=actual_entries,
        backtest_entries=backtest_entries,
        actual_exits=actual_exits,
        backtest_exits=backtest_exits,
        matched_entries=matched_entries,
        matched_exits=matched_exits,
    )
    return ReconciliationResult(
        summary=ReconciliationSummary(
            actual_entries=len(actual_entries),
            backtest_entries=len(backtest_entries),
            matched_entries=len(matched_entries),
            entry_precision=(len(matched_entries) / len(backtest_entries)) if backtest_entries else 0.0,
            entry_recall=(len(matched_entries) / len(actual_entries)) if actual_entries else 0.0,
            avg_entry_delta_seconds=(
                sum(row.delta_seconds for row in matched_entries) / len(matched_entries)
                if matched_entries
                else None
            ),
            actual_exits=len(actual_exits),
            backtest_exits=len(backtest_exits),
            matched_exits=len(matched_exits),
            exit_precision=(len(matched_exits) / len(backtest_exits)) if backtest_exits else 0.0,
            exit_recall=(len(matched_exits) / len(actual_exits)) if actual_exits else 0.0,
            avg_exit_delta_seconds=(
                sum(row.delta_seconds for row in matched_exits) / len(matched_exits)
                if matched_exits
                else None
            ),
            matched_exit_reason_count=matched_exit_reason_count,
            mismatched_exit_reason_count=mismatched_exit_reason_count,
            actual_unique_tickers=len(actual_tickers),
            backtest_unique_tickers=len(backtest_tickers),
            matched_unique_tickers=len(matched_tickers),
            ticker_precision=(len(matched_tickers) / len(backtest_tickers)) if backtest_tickers else 0.0,
            ticker_recall=(len(matched_tickers) / len(actual_tickers)) if actual_tickers else 0.0,
        ),
        tickers=ticker_rows,
        matched_entries=matched_entries,
        matched_exits=matched_exits,
        unmatched_actual_entries=unmatched_actual_entries,
        unmatched_backtest_entries=unmatched_backtest_entries,
        unmatched_actual_exits=unmatched_actual_exits,
        unmatched_backtest_exits=unmatched_backtest_exits,
    )


def _summarize_tickers(
    *,
    actual_entries: list[ActualTradeEvent],
    backtest_entries: list[ActualTradeEvent],
    actual_exits: list[ActualTradeEvent],
    backtest_exits: list[ActualTradeEvent],
    matched_entries: list[MatchedTradeEvent],
    matched_exits: list[MatchedTradeEvent],
) -> list[TickerReconciliationSummary]:
    entry_actual_counts: dict[str, int] = defaultdict(int)
    entry_backtest_counts: dict[str, int] = defaultdict(int)
    exit_actual_counts: dict[str, int] = defaultdict(int)
    exit_backtest_counts: dict[str, int] = defaultdict(int)
    matched_entry_rows: dict[str, list[MatchedTradeEvent]] = defaultdict(list)
    matched_exit_rows: dict[str, list[MatchedTradeEvent]] = defaultdict(list)
    for row in actual_entries:
        entry_actual_counts[row.ticker] += 1
    for row in backtest_entries:
        entry_backtest_counts[row.ticker] += 1
    for row in actual_exits:
        exit_actual_counts[row.ticker] += 1
    for row in backtest_exits:
        exit_backtest_counts[row.ticker] += 1
    for row in matched_entries:
        matched_entry_rows[row.ticker].append(row)
    for row in matched_exits:
        matched_exit_rows[row.ticker].append(row)

    tickers = sorted(
        set(entry_actual_counts)
        | set(entry_backtest_counts)
        | set(exit_actual_counts)
        | set(exit_backtest_counts)
    )
    rows: list[TickerReconciliationSummary] = []
    for ticker in tickers:
        ticker_matched_entries = matched_entry_rows.get(ticker, [])
        ticker_matched_exits = matched_exit_rows.get(ticker, [])
        rows.append(
            TickerReconciliationSummary(
                ticker=ticker,
                actual_entries=entry_actual_counts.get(ticker, 0),
                backtest_entries=entry_backtest_counts.get(ticker, 0),
                matched_entries=len(ticker_matched_entries),
                actual_exits=exit_actual_counts.get(ticker, 0),
                backtest_exits=exit_backtest_counts.get(ticker, 0),
                matched_exits=len(ticker_matched_exits),
                avg_entry_delta_seconds=(
                    sum(row.delta_seconds for row in ticker_matched_entries) / len(ticker_matched_entries)
                    if ticker_matched_entries
                    else None
                ),
                avg_exit_delta_seconds=(
                    sum(row.delta_seconds for row in ticker_matched_exits) / len(ticker_matched_exits)
                    if ticker_matched_exits
                    else None
                ),
                matched_exit_reason_count=sum(1 for row in ticker_matched_exits if row.reason_match is True),
                mismatched_exit_reason_count=sum(1 for row in ticker_matched_exits if row.reason_match is False),
            )
        )
    return rows


def format_reconciliation(result: ReconciliationResult) -> str:
    summary = result.summary
    lines = [
        "Backtest reconciliation",
        (
            f"  entries: actual={summary.actual_entries} backtest={summary.backtest_entries} "
            f"matched={summary.matched_entries} precision={summary.entry_precision * 100:.1f}% "
            f"recall={summary.entry_recall * 100:.1f}% "
            f"avg_delta_s={summary.avg_entry_delta_seconds:.1f}"
            if summary.avg_entry_delta_seconds is not None
            else (
                f"  entries: actual={summary.actual_entries} backtest={summary.backtest_entries} "
                f"matched={summary.matched_entries} precision={summary.entry_precision * 100:.1f}% "
                f"recall={summary.entry_recall * 100:.1f}% avg_delta_s=n/a"
            )
        ),
        (
            f"  exits: actual={summary.actual_exits} backtest={summary.backtest_exits} "
            f"matched={summary.matched_exits} precision={summary.exit_precision * 100:.1f}% "
            f"recall={summary.exit_recall * 100:.1f}% "
            f"avg_delta_s={summary.avg_exit_delta_seconds:.1f} "
            f"reason_match={summary.matched_exit_reason_count} "
            f"reason_mismatch={summary.mismatched_exit_reason_count}"
            if summary.avg_exit_delta_seconds is not None
            else (
                f"  exits: actual={summary.actual_exits} backtest={summary.backtest_exits} "
                f"matched={summary.matched_exits} precision={summary.exit_precision * 100:.1f}% "
                f"recall={summary.exit_recall * 100:.1f}% avg_delta_s=n/a "
                f"reason_match={summary.matched_exit_reason_count} "
                f"reason_mismatch={summary.mismatched_exit_reason_count}"
            )
        ),
        (
            f"  tickers: actual={summary.actual_unique_tickers} backtest={summary.backtest_unique_tickers} "
            f"matched={summary.matched_unique_tickers} precision={summary.ticker_precision * 100:.1f}% "
            f"recall={summary.ticker_recall * 100:.1f}%"
        ),
    ]
    if result.tickers:
        lines.append("")
        lines.append("Ticker summary")
        ranked_tickers = sorted(
            result.tickers,
            key=lambda row: (
                abs(row.actual_entries - row.backtest_entries) + abs(row.actual_exits - row.backtest_exits),
                -(row.matched_entries + row.matched_exits),
                row.ticker,
            ),
            reverse=True,
        )
        for row in ranked_tickers[:10]:
            lines.append(
                f"  {row.ticker}: "
                f"entries actual/backtest/matched={row.actual_entries}/{row.backtest_entries}/{row.matched_entries} "
                f"exits actual/backtest/matched={row.actual_exits}/{row.backtest_exits}/{row.matched_exits}"
            )
    matched_sections = [
        ("Matched entries", result.matched_entries),
        ("Matched exits", result.matched_exits),
    ]
    for title, rows in matched_sections:
        if not rows:
            continue
        lines.append("")
        lines.append(title)
        for row in rows[:10]:
            reason_suffix = ""
            if row.event == "exit":
                reason_suffix = (
                    f" actual_reason={row.actual_reason} backtest_reason={row.backtest_reason}"
                )
            lines.append(
                f"  {row.ticker} {row.event} side={row.side} delta_s={row.delta_seconds:.1f}"
                f"{reason_suffix}"
            )
    sections = [
        ("Unmatched actual entries", result.unmatched_actual_entries),
        ("Unmatched backtest entries", result.unmatched_backtest_entries),
        ("Unmatched actual exits", result.unmatched_actual_exits),
        ("Unmatched backtest exits", result.unmatched_backtest_exits),
    ]
    for title, rows in sections:
        if not rows:
            continue
        lines.append("")
        lines.append(title)
        for row in rows[:20]:
            reason_suffix = f" reason={row.reason}" if row.reason else ""
            lines.append(f"  {row.timestamp} {row.ticker} {row.event} side={row.side}{reason_suffix}")
    return "\n".join(lines)


def export_reconciliation(result: ReconciliationResult, *, export_dir: str) -> None:
    export_path = Path(export_dir).expanduser()
    export_path.mkdir(parents=True, exist_ok=True)
    with (export_path / "reconciliation_summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(result.summary)))
        writer.writeheader()
        writer.writerow(asdict(result.summary))
    with (export_path / "ticker_reconciliation.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(result.tickers[0])) if result.tickers else [
            "ticker",
            "actual_entries",
            "backtest_entries",
            "matched_entries",
            "actual_exits",
            "backtest_exits",
            "matched_exits",
            "avg_entry_delta_seconds",
            "avg_exit_delta_seconds",
            "matched_exit_reason_count",
            "mismatched_exit_reason_count",
        ])
        writer.writeheader()
        for row in result.tickers:
            writer.writerow(asdict(row))
    matched_sections = {
        "matched_entries.csv": result.matched_entries,
        "matched_exits.csv": result.matched_exits,
    }
    for filename, rows in matched_sections.items():
        with (export_path / filename).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "ticker",
                    "event",
                    "side",
                    "actual_timestamp",
                    "backtest_timestamp",
                    "delta_seconds",
                    "actual_reason",
                    "backtest_reason",
                    "reason_match",
                ],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(asdict(row))


async def log_reconciliation_result(
    *,
    db_path: str,
    result: ReconciliationResult,
    window_date: str,
    actual_source: str,
    backtest_source: str,
    export_dir: str | None = None,
    notes: str = "",
) -> int:
    database = SignalDatabase(str(Path(db_path).expanduser()))
    try:
        await database.initialize()
        summary = result.summary
        return await database.record_reconciliation_run(
            created_at=datetime.now(timezone.utc).isoformat(),
            window_date=window_date,
            actual_source=actual_source,
            backtest_source=backtest_source,
            actual_entries=summary.actual_entries,
            backtest_entries=summary.backtest_entries,
            matched_entries=summary.matched_entries,
            entry_precision=summary.entry_precision,
            entry_recall=summary.entry_recall,
            actual_exits=summary.actual_exits,
            backtest_exits=summary.backtest_exits,
            matched_exits=summary.matched_exits,
            exit_precision=summary.exit_precision,
            exit_recall=summary.exit_recall,
            matched_exit_reason_count=summary.matched_exit_reason_count,
            mismatched_exit_reason_count=summary.mismatched_exit_reason_count,
            actual_unique_tickers=summary.actual_unique_tickers,
            backtest_unique_tickers=summary.backtest_unique_tickers,
            matched_unique_tickers=summary.matched_unique_tickers,
            ticker_precision=summary.ticker_precision,
            ticker_recall=summary.ticker_recall,
            export_dir=export_dir or "",
            notes=notes,
        )
    finally:
        database.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare actual Telegram trade events against backtest trade exports.")
    parser.add_argument("--telegram-html", default=None, help="Path to the Telegram export HTML file.")
    parser.add_argument("--actual-db", default=None, help="Path to the live SQLite DB containing trade_analytics.")
    parser.add_argument("--actual-date", default=None, help="UTC trade date in YYYY-MM-DD. Defaults to yesterday UTC for --actual-db.")
    parser.add_argument("--backtest-trades-csv", required=True, help="Path to backtest_trades.csv.")
    parser.add_argument("--tolerance-minutes", type=int, default=30, help="Timestamp match tolerance in minutes.")
    parser.add_argument("--export-dir", default=None, help="Optional export directory for reconciliation CSVs.")
    parser.add_argument("--log-db", default=None, help="Optional SQLite DB path to persist reconciliation summary rows.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if bool(args.telegram_html) == bool(args.actual_db):
        raise ValueError("Use exactly one of --telegram-html or --actual-db")
    if args.telegram_html:
        telegram_html = Path(args.telegram_html).expanduser().read_text(encoding="utf-8")
        actual = parse_telegram_trade_events(telegram_html)
        actual_source = str(Path(args.telegram_html).expanduser())
        window_date = "mixed"
    else:
        window_date = resolve_trade_date(args.actual_date)
        actual = load_actual_trade_events_from_db(args.actual_db, trade_date=window_date)
        actual_source = f"{Path(args.actual_db).expanduser()}:{window_date}"
    backtest = load_backtest_trade_events(args.backtest_trades_csv)
    result = reconcile_trade_events(actual, backtest, tolerance_minutes=args.tolerance_minutes)
    if args.export_dir:
        export_reconciliation(result, export_dir=args.export_dir)
    if args.log_db:
        asyncio.run(
            log_reconciliation_result(
                db_path=args.log_db,
                result=result,
                window_date=window_date,
                actual_source=actual_source,
                backtest_source=str(Path(args.backtest_trades_csv).expanduser()),
                export_dir=args.export_dir,
            )
        )
    print(format_reconciliation(result))


if __name__ == "__main__":
    main()
