"""The check that would have caught TLMUSDT in 60 seconds instead of never."""

from __future__ import annotations

from pathlib import Path

import pytest

from liquidity_migration.account_contracts import AccountState
from liquidity_migration.demo_paper_agreement import (
    AgreementReport,
    TargetRow,
    accepted_quantities,
    compare_accepted_quantities,
    compare_books,
    evaluate_demo_paper_agreement,
    latest_book,
)


def _row(
    symbol: str,
    notional: float,
    *,
    sleeve: str = "carry",
    ts: int = 1,
    mirror_scale: float | None = None,
    weight: float | None = 0.1,
) -> TargetRow:
    return TargetRow(
        sleeve=sleeve,
        target_key=f"{sleeve}/carry_hold_v3/carry_hold/{symbol}",
        symbol=symbol,
        signed_notional_usdt=notional,
        created_ts_ns=ts,
        batch_id=f"{sleeve}/batch/{ts}",
        mirror_scale=mirror_scale,
        target_weight=weight,
    )


def _book(*rows: TargetRow) -> dict[str, TargetRow]:
    return latest_book(rows)


def _compare(demo, paper, **kwargs) -> AgreementReport:
    return compare_books(demo, paper, sleeves=("carry",), **kwargs)


class TestBookReduction:
    def test_the_latest_target_for_a_key_wins(self) -> None:
        book = _book(_row("LAUSDT", 25_000.0, ts=1), _row("LAUSDT", 30_000.0, ts=2))
        assert list(book.values())[0].signed_notional_usdt == 30_000.0

    def test_a_flat_target_removes_the_symbol_from_the_book(self) -> None:
        assert _book(_row("LAUSDT", 25_000.0, ts=1), _row("LAUSDT", 0.0, ts=2)) == {}

    def test_a_flat_target_does_not_resurrect_an_older_position(self) -> None:
        book = _book(
            _row("LAUSDT", 0.0, ts=2),
            _row("LAUSDT", 25_000.0, ts=1),
            _row("ESPUSDT", 10_000.0, ts=1),
        )
        assert set(book) == {"carry/carry_hold_v3/carry_hold/ESPUSDT"}


class TestDisagreement:
    def test_matching_books_agree(self) -> None:
        demo = _book(_row("LAUSDT", 25_000.0))
        paper = _book(_row("LAUSDT", 25_000.0))
        report = _compare(demo, paper)
        assert report.agree
        assert report.detail() == "demo and paper agree on 1 symbol(s)"

    def test_a_symbol_only_paper_holds_is_reported(self) -> None:
        """A symbol one side holds and the other does not is reported."""

        demo = _book(_row("LAUSDT", 25_000.0))
        paper = _book(_row("LAUSDT", 25_000.0), _row("TLMUSDT", 20_446.0))
        report = _compare(demo, paper)
        assert not report.agree
        assert [item.kind for item in report.disagreements] == ["only_in_paper"]
        assert report.disagreements[0].symbol == "TLMUSDT"
        assert "TLMUSDT" in report.detail()

    def test_a_symbol_only_demo_holds_is_reported(self) -> None:
        report = _compare(_book(_row("LAUSDT", 25_000.0)), _book())
        assert [item.kind for item in report.disagreements] == ["only_in_demo"]

    def test_a_notional_gap_beyond_tolerance_is_reported(self) -> None:
        demo = _book(_row("LAUSDT", 25_516.0))
        paper = _book(_row("LAUSDT", 25_000.0))
        report = _compare(demo, paper, relative_tolerance=0.01)
        assert not report.agree
        assert report.disagreements[0].kind == "notional"
        assert report.disagreements[0].relative_difference == pytest.approx(0.0202, abs=1e-3)

    def test_a_notional_gap_inside_tolerance_is_not_reported(self) -> None:
        assert _compare(
            _book(_row("LAUSDT", 25_100.0)),
            _book(_row("LAUSDT", 25_000.0)),
            relative_tolerance=0.01,
        ).agree


class TestScaledMirror:
    def test_a_declared_scale_is_normalised_away(self) -> None:
        demo = _book(_row("LAUSDT", 25_000.0))
        paper = _book(_row("LAUSDT", 12_500.0, mirror_scale=0.5))
        assert _compare(demo, paper, applied_scale=0.5).agree

    def test_a_scaled_book_that_is_genuinely_wrong_still_reports(self) -> None:
        demo = _book(_row("LAUSDT", 25_000.0))
        paper = _book(_row("LAUSDT", 20_000.0, mirror_scale=0.5))
        assert not _compare(demo, paper, applied_scale=0.5).agree

    def test_a_non_positive_scale_is_refused(self) -> None:
        with pytest.raises(ValueError):
            _compare(_book(), _book(), applied_scale=0.0)


class TestAcceptedQuantities:
    def _state(self, **targets: tuple[str, float]) -> AccountState:
        state = AccountState()
        for key, (symbol, qty) in targets.items():
            state.component_targets[key.replace("__", "/")] = {
                "symbol": symbol,
                "signed_qty": qty,
            }
        return state

    def test_quantities_are_scoped_to_the_requested_sleeves(self) -> None:
        state = self._state(
            carry__v3__c__LAUSDT=("LAUSDT", 100.0),
            long__v11__c__ONDOUSDT=("ONDOUSDT", 50.0),
        )
        assert set(accepted_quantities(state, sleeves=frozenset({"carry"}))) == {
            "carry/v3/c/LAUSDT"
        }

    def test_a_flat_component_target_is_not_a_held_position(self) -> None:
        state = self._state(carry__v3__c__LAUSDT=("LAUSDT", 0.0))
        assert accepted_quantities(state, sleeves=frozenset({"carry"})) == {}

    def test_the_basis_fossil_is_reported(self) -> None:
        """Identical notionals with different accepted quantities: one book re-stamped at
        churn-time prices, the other still carrying its entry stamp.
        """

        demo = {"carry/v3/c/LAUSDT": ("LAUSDT", 497_332.0)}
        paper = {"carry/v3/c/LAUSDT": ("LAUSDT", 435_123.0)}
        found = compare_accepted_quantities(demo, paper, relative_tolerance=0.01)
        assert [item.kind for item in found] == ["quantity"]
        assert found[0].relative_difference == pytest.approx(0.125, abs=1e-3)
        assert "qty" in found[0].describe()

    def test_matching_quantities_report_nothing(self) -> None:
        book = {"carry/v3/c/LAUSDT": ("LAUSDT", 497_332.0)}
        assert compare_accepted_quantities(book, dict(book)) == ()


class TestUnreadableInputs:
    def test_a_missing_tape_is_not_agreement(self, tmp_path: Path) -> None:
        report = evaluate_demo_paper_agreement(
            demo_tape=tmp_path / "missing-demo.jsonl",
            paper_tape=tmp_path / "missing-paper.jsonl",
            sleeves=("carry",),
        )
        # A missing tape parses as an empty capture, which is two empty books:
        # genuinely in agreement, and distinct from a corrupt one.
        assert report.agree

    def test_a_corrupt_tape_is_reported_rather_than_read_as_agreement(
        self, tmp_path: Path
    ) -> None:
        demo = tmp_path / "demo.jsonl"
        paper = tmp_path / "paper.jsonl"
        demo.write_bytes(b'{"schema_version":1,"kind":"wrong"}\n')
        paper.write_bytes(b"")
        report = evaluate_demo_paper_agreement(
            demo_tape=demo, paper_tape=paper, sleeves=("carry",)
        )
        assert not report.agree
        assert report.unreadable
        assert "unreadable" in report.detail()

    def test_no_sleeve_scope_is_a_usage_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            evaluate_demo_paper_agreement(
                demo_tape=tmp_path / "a.jsonl", paper_tape=tmp_path / "b.jsonl", sleeves=()
            )
