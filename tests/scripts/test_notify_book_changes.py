"""The trade notifier: what pages the phone, what it says, and what stays quiet."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "notify_book_changes",
    Path(__file__).resolve().parents[2] / "scripts" / "runtime" / "notify_book_changes.py",
)
notify = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
# Registered before it runs: `dataclass` resolves annotations through
# `sys.modules[cls.__module__]`, and a module loaded straight off a path is
# not in there unless it is put there.
sys.modules[_SPEC.name] = notify
_SPEC.loader.exec_module(notify)


def _book(tmp: Path, rows: list[tuple[str, float]]) -> str:
    p = tmp / "book.json"
    p.write_text(json.dumps({"targets": [{"symbol": s, "notional_usdt": n} for s, n in rows]}))
    return str(p)


def _trade(**over) -> dict:
    trade = {
        "sleeve": "carry",
        "symbol": "ONGUSDT",
        "side": "long",
        "qty": 7347.0,
        "exit_px": 0.07072479,
        "closed_ms": 1_787_000_000_000,
        "fills": 2,
        "maker_share": 1.0,
        "arrival_shortfall_bps": 1.06,
        "round_trip": {
            "entry_px": 0.06845589,
            "entry_notional_usdt": 502.9,
            "gross_usdt": 16.83,
            "fees_usdt": 0.55,
            "net_usdt": 16.28,
            "net_bps": 323.7,
            "opened_ms": 1_786_968_882_000,
            "held_ms": 31_118_000,
        },
    }
    trade.update(over)
    return trade


class TestReadPositiveTargets:
    def test_zero_rows_are_not_positions(self, tmp_path: Path) -> None:
        path = _book(tmp_path, [("AAAUSDT", 40.0), ("BBBUSDT", 0.0)])
        assert notify.read_positive_targets(path) == {"AAAUSDT": 40.0}

    def test_an_unreadable_book_is_none_not_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "torn.json"
        p.write_text("{ torn")
        assert notify.read_positive_targets(str(p)) is None

    def test_a_missing_book_is_none(self, tmp_path: Path) -> None:
        assert notify.read_positive_targets(str(tmp_path / "absent.json")) is None


class TestBookDiff:
    def test_a_new_symbol_is_an_entry_with_its_size(self) -> None:
        assert notify.entry_messages("LONG", "", {}, {"ADAUSDT": 35.69}) == [
            "LONG enters ADAUSDT · $35.69"
        ]

    def test_a_sizing_figure_drops_its_cents_past_a_hundred(self) -> None:
        assert notify.entry_messages("CARRY", "DEMO ", {}, {"ONGUSDT": 478.10}) == [
            "DEMO CARRY enters ONGUSDT · $478"
        ]

    def test_a_resize_stays_off_the_phone(self) -> None:
        assert notify.entry_messages("CARRY", "", {"EDENUSDT": 120.0}, {"EDENUSDT": 90.0}) == []
        assert notify.book_exit_messages("CARRY", "", {"EDENUSDT": 120.0}, {"EDENUSDT": 90.0}) == []

    def test_exodus_speaks_shorts_and_covers(self) -> None:
        assert notify.entry_messages("EXODUS", "", {}, {"ONGUSDT": 85.0}) == [
            "EXODUS shorts ONGUSDT · $85.00"
        ]
        assert notify.book_exit_messages("EXODUS", "", {"ONGUSDT": 85.0}, {}) == [
            "EXODUS covers ONGUSDT"
        ]

    def test_the_real_money_account_leads_with_rm(self) -> None:
        assert notify.entry_messages("CARRY", "RM ", {}, {"AGIUSDT": 1.0}) == [
            "RM CARRY enters AGIUSDT · $1.00"
        ]

    def test_entries_come_out_sorted(self) -> None:
        assert notify.entry_messages("LONG", "", {}, {"BUSDT": 2.0, "AUSDT": 1.0}) == [
            "LONG enters AUSDT · $1.00",
            "LONG enters BUSDT · $2.00",
        ]


class TestReadNewTrades:
    def test_a_missing_file_is_none_so_the_books_still_report_exits(self, tmp_path: Path) -> None:
        assert notify.read_new_trades(str(tmp_path / "absent.jsonl"), 0) is None

    def test_only_what_is_past_the_offset_comes_back(self, tmp_path: Path) -> None:
        path = tmp_path / "trades.jsonl"
        first = json.dumps(_trade()) + "\n"
        path.write_text(first)
        trades, offset = notify.read_new_trades(str(path), 0)
        assert [t["symbol"] for t in trades] == ["ONGUSDT"]
        assert offset == len(first)

        with path.open("a") as fh:
            fh.write(json.dumps(_trade(symbol="MOVEUSDT")) + "\n")
        trades, offset = notify.read_new_trades(str(path), offset)
        assert [t["symbol"] for t in trades] == ["MOVEUSDT"]
        assert offset == path.stat().st_size

    def test_a_half_written_line_waits_for_the_rest_of_itself(self, tmp_path: Path) -> None:
        path = tmp_path / "trades.jsonl"
        whole = json.dumps(_trade()) + "\n"
        path.write_text(whole + '{"sleeve": "carry", "sym')
        trades, offset = notify.read_new_trades(str(path), 0)
        assert len(trades) == 1
        assert offset == len(whole), "the torn tail is read again next time"

    def test_a_file_that_shrank_re_baselines_rather_than_replaying(self, tmp_path: Path) -> None:
        path = tmp_path / "trades.jsonl"
        path.write_text(json.dumps(_trade()) + "\n")
        trades, offset = notify.read_new_trades(str(path), 10_000)
        assert trades == []
        assert offset == path.stat().st_size


class TestExitMessage:
    def test_the_verdict_and_the_money_are_the_first_line_in_bold(self) -> None:
        lines = notify.exit_message(_trade(), "").splitlines()
        assert lines[0] == "🟢 CARRY <b>+$16.28</b> · ONGUSDT"
        assert lines[1] == "long 8h 38m · 0.06846 → 0.07072 · +324 bp"
        assert lines[2] == "$503 · fee $0.55 · maker 100% · slip +1.1 bp"

    def test_a_price_is_four_significant_figures_not_eight_decimals(self) -> None:
        body = notify.exit_message(_trade(), "")
        assert "0.06845589" not in body, "eight decimals is texture, not information"
        assert "0.06846" in body

    def test_a_loser_is_marked_as_one(self) -> None:
        trade = _trade()
        trade["round_trip"] = dict(trade["round_trip"], net_usdt=-2.26, net_bps=-45.0)
        lines = notify.exit_message(trade, "").splitlines()
        assert lines[0] == "🔴 CARRY <b>-$2.26</b> · ONGUSDT"

    def test_the_funded_account_is_tagged(self) -> None:
        assert notify.exit_message(_trade(), "RM ").startswith("🟢 RM CARRY ")

    def test_a_close_the_log_cannot_price_says_so_rather_than_claiming_zero(self) -> None:
        body = notify.exit_message(_trade(round_trip=None), "")
        assert "$0.00" not in body
        assert body.splitlines()[0] == "CARRY closed ONGUSDT · long · out 0.07072"
        assert "what it made is unknown" in body

    def test_a_symbol_is_escaped_because_telegram_html_rejects_a_stray_bracket(self) -> None:
        body = notify.exit_message(_trade(symbol="A<B&C"), "")
        assert "A&lt;B&amp;C" in body
        assert "A<B" not in body


class TestDailySummary:
    def _rows(self) -> list[dict]:
        def row(sleeve, symbol, net):
            trade = _trade(sleeve=sleeve, symbol=symbol)
            trade["round_trip"] = dict(trade["round_trip"], net_usdt=net)
            return trade

        return [
            row("carry", "ONGUSDT", 16.28),
            row("carry", "MOVEUSDT", 25.90),
            row("exodus", "COTIUSDT", -2.26),
            _trade(sleeve="long", symbol="SOLUSDT", round_trip=None),
        ]

    def test_the_dot_is_the_days_colour_and_the_money_is_bold(self) -> None:
        body = notify.daily_summary(self._rows(), "2026-08-23")
        assert body.startswith("🟢 <b>Sun 23 Aug</b> · 3 trips · 2 won · <b>+$39.92</b>")

    def test_a_losing_day_opens_red(self) -> None:
        rows = self._rows()
        rows[0]["round_trip"] = dict(rows[0]["round_trip"], net_usdt=-50.0)
        body = notify.daily_summary(rows, "2026-08-23")
        assert body.startswith("🔴")

    def test_the_sleeve_table_is_monospace_with_win_loss_records(self) -> None:
        body = notify.daily_summary(self._rows(), "2026-08-23")
        assert "<pre>" in body and "</pre>" in body
        table = body.split("<pre>")[1].split("</pre>")[0]
        assert "CARRY" in table and "2–0" in table
        assert "EXODUS" in table and "0–1" in table

    def test_it_names_the_best_and_the_worst(self) -> None:
        body = notify.daily_summary(self._rows(), "2026-08-23")
        assert "best +$25.90 · CARRY MOVEUSDT" in body
        assert "worst -$2.26 · EXODUS COTIUSDT" in body

    def test_it_counts_what_it_could_not_price(self) -> None:
        body = notify.daily_summary(self._rows(), "2026-08-23")
        assert "1 trip unpriced" in body

    def test_it_says_funding_is_missing_in_one_quiet_line(self) -> None:
        body = notify.daily_summary(self._rows(), "2026-08-23")
        assert "<i>after fees — funding settles to the wallet separately</i>" in body

    def test_a_day_with_nothing_priced_sends_nothing(self) -> None:
        assert notify.daily_summary([_trade(round_trip=None)], "2026-08-23") is None
        assert notify.daily_summary([], "2026-08-23") is None

    def test_rows_are_per_account_so_real_money_never_melts_into_demo(self) -> None:
        rows = self._rows()[:2]
        rows[0]["account_tag"] = "DEMO "
        rows[1] = dict(rows[1], account_tag="RM ")
        body = notify.daily_summary(rows, "2026-08-23")
        table = body.split("<pre>")[1].split("</pre>")[0]
        assert "DEMO CARRY" in table and "RM CARRY" in table

    def test_the_white_dot_is_retired(self) -> None:
        assert "⚪" not in notify.exit_message(_trade(), "DEMO ")
        assert "⚪" not in notify.exit_message(_trade(round_trip=None), "DEMO ")
        assert all("⚪" not in m for m in notify.entry_messages("LONG", "DEMO ", {}, {"AUSDT": 1.0}))
        assert all("⚪" not in m for m in notify.book_exit_messages("LONG", "DEMO ", {"AUSDT": 1.0}, {}))
        assert "⚪" not in notify.daily_summary(self._rows(), "2026-08-23")

    def test_one_trip_reads_as_won_or_lost_not_one_won(self) -> None:
        body = notify.daily_summary([self._rows()[0]], "2026-08-23")
        assert "1 trip · won" in body
        assert "best" not in body, "one trip is its own best and worst"


class TestBatching:
    def test_a_quiet_run_is_one_message(self) -> None:
        assert notify.batched(["a", "b"]) == ["a\n\nb"]

    def test_a_flood_is_split_under_the_telegram_limit(self) -> None:
        long = "x" * 2_000
        parts = notify.batched([long, long, long])
        assert len(parts) == 3
        assert all(len(p) <= notify.MAX_MESSAGE_CHARS for p in parts)

    def test_nothing_in_nothing_out(self) -> None:
        assert notify.batched([]) == []


class TestFormatting:
    def test_a_price_keeps_four_significant_figures(self) -> None:
        assert notify.price(0.06845589) == "0.06846"
        assert notify.price(801.99) == "802"
        assert notify.price(77066.0) == "77,066"
        assert notify.price(0.00409889) == "0.004099"
        assert notify.price(1.5068) == "1.507"

    def test_a_held_time_reads_in_the_unit_that_matters(self) -> None:
        assert notify.held(31_118_000) == "8h 38m"
        assert notify.held(112_000_000) == "1d 7h"
        assert notify.held(600_000) == "10m"

    def test_money_always_carries_its_sign(self) -> None:
        assert notify.money(16.28) == "+$16.28"
        assert notify.money(-2.26) == "-$2.26"
        assert notify.money(0.0) == "+$0.00"
        assert notify.money(1103.9) == "+$1,103.90"

    def test_a_day_reads_like_a_person_wrote_it(self) -> None:
        assert notify.human_day("2026-08-23") == "Sun 23 Aug"


class TestOneWholeRun:
    """`main()` end to end: what a run sends, and what it remembers."""

    def _fleet(self, tmp_path: Path, monkeypatch, *, funded: bool = False):
        sent: list[str] = []
        monkeypatch.setattr(
            notify, "send_telegram_message", lambda body, **_: sent.append(body) or True
        )
        monkeypatch.setenv("BOOK_NOTIFY_STATE", str(tmp_path / "state.json"))
        monkeypatch.setenv("TELEGRAM_ENABLED", "1")
        # A day nothing closed in, so only the test that wants a summary gets
        # one however long after today these fixtures are read.
        monkeypatch.setattr(notify, "yesterday_utc", lambda _now: "1970-01-01")
        accounts = [
            notify.Account(
                name="demo",
                tag="DEMO ",
                books={
                    "CARRY": str(tmp_path / "carry.json"),
                    "EXODUS": str(tmp_path / "exodus.json"),
                },
                trades=str(tmp_path / "trades.jsonl"),
            )
        ]
        if funded:
            accounts.append(
                notify.Account(
                    name="funded",
                    tag="RM ",
                    books={"CARRY": str(tmp_path / "carry-mainnet.json")},
                    trades=str(tmp_path / "trades-mainnet.jsonl"),
                )
            )
        monkeypatch.setattr(notify, "ACCOUNTS", tuple(accounts))
        return sent

    def _write_book(self, path: Path, rows: dict[str, float]) -> None:
        path.write_text(
            json.dumps(
                {"targets": [{"symbol": s, "notional_usdt": n} for s, n in rows.items()]}
            )
        )

    def test_the_first_run_baselines_without_saying_anything(self, tmp_path, monkeypatch) -> None:
        sent = self._fleet(tmp_path, monkeypatch)
        self._write_book(tmp_path / "carry.json", {"ONGUSDT": 478.10})
        self._write_book(tmp_path / "exodus.json", {})
        notify.main()
        assert sent == []

    def test_a_new_book_symbol_pages_as_an_entry(self, tmp_path, monkeypatch) -> None:
        sent = self._fleet(tmp_path, monkeypatch)
        self._write_book(tmp_path / "carry.json", {})
        self._write_book(tmp_path / "exodus.json", {})
        notify.main()
        self._write_book(tmp_path / "carry.json", {"ONGUSDT": 478.10})
        notify.main()
        assert sent == ["DEMO CARRY enters ONGUSDT · $478"]

    def test_with_no_engine_file_an_exit_still_pages_off_the_book(
        self, tmp_path, monkeypatch
    ) -> None:
        sent = self._fleet(tmp_path, monkeypatch)
        self._write_book(tmp_path / "carry.json", {"ONGUSDT": 478.10})
        self._write_book(tmp_path / "exodus.json", {})
        notify.main()
        self._write_book(tmp_path / "carry.json", {})
        notify.main()
        assert sent == ["DEMO CARRY exits ONGUSDT"]

    def test_with_an_engine_file_the_exit_comes_with_its_money_and_only_once(
        self, tmp_path, monkeypatch
    ) -> None:
        sent = self._fleet(tmp_path, monkeypatch)
        trades = tmp_path / "trades.jsonl"
        trades.write_text("")
        self._write_book(tmp_path / "carry.json", {"ONGUSDT": 478.10})
        self._write_book(tmp_path / "exodus.json", {})
        notify.main()

        self._write_book(tmp_path / "carry.json", {})
        with trades.open("a") as fh:
            fh.write(json.dumps(_trade()) + "\n")
        notify.main()

        assert len(sent) == 1, sent
        assert sent[0].startswith("🟢 DEMO CARRY <b>+$16.28</b> · ONGUSDT")
        assert "exits ONGUSDT" not in sent[0], "the book must not say it too"

    def test_a_trade_already_in_the_file_is_history_not_news(
        self, tmp_path, monkeypatch
    ) -> None:
        sent = self._fleet(tmp_path, monkeypatch)
        (tmp_path / "trades.jsonl").write_text(json.dumps(_trade()) + "\n")
        self._write_book(tmp_path / "carry.json", {})
        self._write_book(tmp_path / "exodus.json", {})
        notify.main()
        assert sent == []

    def test_a_trade_is_never_sent_twice(self, tmp_path, monkeypatch) -> None:
        sent = self._fleet(tmp_path, monkeypatch)
        trades = tmp_path / "trades.jsonl"
        trades.write_text("")
        self._write_book(tmp_path / "carry.json", {})
        self._write_book(tmp_path / "exodus.json", {})
        notify.main()
        with trades.open("a") as fh:
            fh.write(json.dumps(_trade()) + "\n")
        notify.main()
        notify.main()
        assert len(sent) == 1, sent

    def test_both_accounts_report_and_the_funded_one_is_tagged(
        self, tmp_path, monkeypatch
    ) -> None:
        sent = self._fleet(tmp_path, monkeypatch, funded=True)
        for name in ("trades.jsonl", "trades-mainnet.jsonl"):
            (tmp_path / name).write_text("")
        self._write_book(tmp_path / "carry.json", {})
        self._write_book(tmp_path / "exodus.json", {})
        self._write_book(tmp_path / "carry-mainnet.json", {})
        notify.main()

        with (tmp_path / "trades.jsonl").open("a") as fh:
            fh.write(json.dumps(_trade()) + "\n")
        with (tmp_path / "trades-mainnet.jsonl").open("a") as fh:
            fh.write(json.dumps(_trade(symbol="AGIUSDT")) + "\n")
        notify.main()

        assert len(sent) == 1, "one run, one buzz"
        assert "🟢 DEMO CARRY <b>+$16.28</b> · ONGUSDT" in sent[0]
        assert "🟢 RM CARRY <b>+$16.28</b> · AGIUSDT" in sent[0]

    def test_an_unreadable_book_is_not_a_mass_exit(self, tmp_path, monkeypatch) -> None:
        sent = self._fleet(tmp_path, monkeypatch)
        self._write_book(tmp_path / "carry.json", {"ONGUSDT": 478.10, "AGIUSDT": 478.10})
        self._write_book(tmp_path / "exodus.json", {})
        notify.main()
        (tmp_path / "carry.json").write_text("{ torn")
        notify.main()
        assert sent == []

        # And the positions are still remembered, so the recovery is quiet too.
        self._write_book(tmp_path / "carry.json", {"ONGUSDT": 478.10, "AGIUSDT": 478.10})
        notify.main()
        assert sent == []

    def test_the_daily_summary_goes_out_once(self, tmp_path, monkeypatch) -> None:
        sent = self._fleet(tmp_path, monkeypatch)
        monkeypatch.setattr(notify, "yesterday_utc", lambda _now: "2026-08-23")
        # Stamped inside 2026-08-23 UTC.
        (tmp_path / "trades.jsonl").write_text(
            json.dumps(_trade(closed_ms=1_787_500_000_000)) + "\n"
        )
        self._write_book(tmp_path / "carry.json", {})
        self._write_book(tmp_path / "exodus.json", {})
        notify.main()
        assert len(sent) == 1 and sent[0].startswith("🟢 <b>Sun 23 Aug</b>")
        notify.main()
        assert len(sent) == 1, "the same day is not summarised twice"
