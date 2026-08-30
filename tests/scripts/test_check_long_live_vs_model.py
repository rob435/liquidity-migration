"""Offline tests for the LONG live-vs-model parity checker.

Every input is a tiny synthetic fixture in tmp_path: a model ledger CSV, the
producer's transitions JSONL, the engine's closed-trade JSONL, cycle payloads,
and venue closed-pnl rows. No network, no repo data roots.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "research" / "check_long_live_vs_model.py"
SPEC = importlib.util.spec_from_file_location("check_long_live_vs_model", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
parity = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = parity
SPEC.loader.exec_module(parity)

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
# 2026-08-23T00:00:00Z, a real daily-bar end stamp.
T0 = 1_787_443_200_000


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


MODEL_COLUMNS = [
    "trade_id",
    "symbol",
    "entry_signal_ts_ms",
    "entry_ts_ms",
    "exit_ts_ms",
    "entry_price",
    "exit_price",
    "exit_reason",
    "notional_weight",
    "gross_trade_return",
    "cost_return",
    "funding_return",
    "net_return",
    "pattern",
]


def _model_trade(
    symbol: str = "ENAUSDT",
    signal_ts_ms: int = T0,
    entry_ts_ms: int | None = None,
    exit_ts_ms: int | None = None,
    entry_price: float = 0.156,
    exit_price: float = 0.1562,
    exit_reason: str = "time_stop",
    notional_weight: float = 0.5,
    funding_return: float = -0.001,
) -> dict:
    entry_ts_ms = entry_ts_ms if entry_ts_ms is not None else signal_ts_ms + 2 * HOUR_MS
    exit_ts_ms = exit_ts_ms if exit_ts_ms is not None else entry_ts_ms + 3 * DAY_MS
    gross = exit_price / entry_price - 1.0
    cost = -notional_weight * 0.0021
    return {
        "trade_id": f"native-x-{symbol}-l-{symbol}",
        "symbol": symbol,
        "entry_signal_ts_ms": signal_ts_ms,
        "entry_ts_ms": entry_ts_ms,
        "exit_ts_ms": exit_ts_ms,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "notional_weight": notional_weight,
        "gross_trade_return": gross,
        "cost_return": cost,
        "funding_return": funding_return,
        "net_return": notional_weight * gross + cost + funding_return,
        "pattern": "fomo_chase",
    }


def _write_model_csv(path: Path, trades: list[dict]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MODEL_COLUMNS)
        writer.writeheader()
        writer.writerows(trades)
    return path


def _load_model(
    tmp_path: Path,
    trades: list[dict],
    start_ms: int = T0 - 30 * DAY_MS,
    end_ms: int = T0 + 30 * DAY_MS,
    margin_ms: int = 0,
):
    csv_path = _write_model_csv(tmp_path / "long_native_trades.csv", trades)
    return parity.load_model_trades(csv_path, start_ms=start_ms, end_ms=end_ms, margin_ms=margin_ms)


def _live(symbol: str = "ENAUSDT", signal_ts_ms: int | None = T0, **kwargs) -> object:
    defaults = dict(
        trade_id=f"long-{symbol}-{signal_ts_ms}" if signal_ts_ms else None,
        pattern="fomo_chase",
        entry_ts_ms=(signal_ts_ms + 2 * HOUR_MS) if signal_ts_ms else None,
        entry_ts_source="transitions",
        entry_px=0.157,
        entry_px_source="engine_journal",
        exit_ts_ms=(signal_ts_ms + 3 * DAY_MS) if signal_ts_ms else None,
        exit_px=0.141,
        exit_px_source="engine_journal",
        exit_reason="time_stop",
        exit_reason_source="planned_exit",
        net_bps=-950.0,
        net_bps_source="engine_journal",
    )
    defaults.update(kwargs)
    return parity.LiveTrade(symbol=symbol, signal_ts_ms=signal_ts_ms, **defaults)


def test_signal_key_pairing_uses_symbol_and_signal_ts(tmp_path: Path) -> None:
    """The join key is (symbol, signal timestamp), not symbol alone."""
    model = _load_model(tmp_path, [_model_trade(symbol="ENAUSDT", signal_ts_ms=T0)])
    right = _live(symbol="ENAUSDT", signal_ts_ms=T0)
    # Same symbol, signal 10 days away, and entry far outside any fallback window.
    decoy_same_symbol = _live(symbol="ENAUSDT", signal_ts_ms=T0 + 10 * DAY_MS)
    # Same signal timestamp, different symbol.
    decoy_same_ts = _live(symbol="AAVEUSDT", signal_ts_ms=T0)

    cohorts = parity.pair_trades(model, [decoy_same_symbol, right, decoy_same_ts], window_ms=24 * HOUR_MS)

    assert len(cohorts.pairs) == 1
    _, live, how = cohorts.pairs[0]
    assert how == "signal_key"
    assert live is right
    assert decoy_same_symbol in cohorts.live_only
    assert decoy_same_ts in cohorts.live_only


def test_proximity_fallbacks_pair_only_within_window(tmp_path: Path) -> None:
    model = _load_model(
        tmp_path,
        [
            _model_trade(symbol="ENAUSDT", signal_ts_ms=T0, entry_ts_ms=T0 + 2 * HOUR_MS),
            _model_trade(symbol="SOLUSDT", signal_ts_ms=T0, entry_ts_ms=T0 + 2 * HOUR_MS),
            _model_trade(symbol="OPUSDT", signal_ts_ms=T0, entry_ts_ms=T0 + 2 * HOUR_MS),
        ],
    )
    # Adjacent daily signals (exactly one day apart) pair on signal closeness.
    next_day = _live(symbol="ENAUSDT", signal_ts_ms=T0 + DAY_MS, entry_ts_ms=T0 + DAY_MS + 2 * HOUR_MS)
    # No recoverable signal, entries 3h apart -> entry-time fallback pairs.
    no_signal_near = _live(symbol="OPUSDT", signal_ts_ms=None, trade_id=None, entry_ts_ms=T0 + 5 * HOUR_MS)
    # No recoverable signal, entries 30h apart -> outside the window, unpaired.
    no_signal_far = _live(symbol="SOLUSDT", signal_ts_ms=None, trade_id=None, entry_ts_ms=T0 + 32 * HOUR_MS)

    cohorts = parity.pair_trades(model, [next_day, no_signal_near, no_signal_far], window_ms=24 * HOUR_MS)

    assert sorted((m["symbol"], how) for m, _, how in cohorts.pairs) == [
        ("ENAUSDT", "signal_proximity"),
        ("OPUSDT", "entry_proximity"),
    ]
    assert [id(t) for t in cohorts.live_only] == [id(no_signal_far)]
    assert [m["symbol"] for m in cohorts.model_only] == ["SOLUSDT"]


def test_margin_model_trade_pairs_at_the_boundary_but_is_never_a_miss(tmp_path: Path) -> None:
    # Two model trades signal one day BEFORE the window start: one pairs with a
    # live trade at the boundary, the other must vanish rather than count as a
    # model-only miss.
    start = T0
    model = _load_model(
        tmp_path,
        [
            _model_trade(symbol="ENAUSDT", signal_ts_ms=T0 - DAY_MS, exit_reason="take_profit"),
            _model_trade(symbol="ZZZUSDT", signal_ts_ms=T0 - DAY_MS),
        ],
        start_ms=start,
        end_ms=T0 + 7 * DAY_MS,
        margin_ms=DAY_MS,
    )
    assert [m["in_window"] for m in model] == [False, False]
    live = _live(symbol="ENAUSDT", signal_ts_ms=T0, exit_reason="time_stop")
    cohorts = parity.pair_trades(model, [live], window_ms=24 * HOUR_MS)

    assert [(m["symbol"], how) for m, _, how in cohorts.pairs] == [("ENAUSDT", "signal_proximity")]
    assert cohorts.model_only == []
    rows = parity.build_rows(cohorts)
    assert rows[0]["structural"] == parity.STRUCTURAL_NO_LIVE_TP
    assert "before the window start" in rows[0]["notes"]
    # A proximity pair took a different trigger: its gap is selection, not
    # execution slippage, so the slippage stats stay empty.
    summary = parity.summarize(rows)
    assert summary["entry_slip_n"] == 0
    assert summary["structural_bps"] == rows[0]["gap_bps"]


def test_slippage_signs_positive_means_live_paid_more(tmp_path: Path) -> None:
    model = _load_model(
        tmp_path,
        [_model_trade(symbol="ENAUSDT", signal_ts_ms=T0, entry_price=0.1560, exit_price=0.1500)],
    )
    live = _live(symbol="ENAUSDT", signal_ts_ms=T0, entry_px=0.1570, exit_px=0.1480)
    cohorts = parity.pair_trades(model, [live], window_ms=24 * HOUR_MS)
    row = parity.build_rows(cohorts)[0]

    # Live bought higher: positive entry slippage, worth (0.1570-0.1560)/0.1560.
    assert abs(row["entry_slippage_bps"] - (0.1570 - 0.1560) / 0.1560 * 1e4) < 1e-9
    # Live sold lower: positive exit slippage, worth (0.1500-0.1480)/0.1500.
    assert abs(row["exit_slippage_bps"] - (0.1500 - 0.1480) / 0.1500 * 1e4) < 1e-9


def test_model_take_profit_against_live_time_stop_is_structural(tmp_path: Path) -> None:
    model = _load_model(tmp_path, [_model_trade(symbol="ENAUSDT", signal_ts_ms=T0, exit_reason="take_profit")])
    live = _live(symbol="ENAUSDT", signal_ts_ms=T0, exit_reason="time_stop")
    cohorts = parity.pair_trades(model, [live], window_ms=24 * HOUR_MS)
    rows = parity.build_rows(cohorts)

    assert rows[0]["structural"] == parity.STRUCTURAL_NO_LIVE_TP
    summary = parity.summarize(rows)
    # A structural pair never counts as exit slippage.
    assert summary["exit_slip_n"] == 0
    assert summary["structural_bps"] == rows[0]["gap_bps"]


def test_decomposition_excludes_slippage_from_pairs_without_net_evidence() -> None:
    gradeable = parity._blank_row()
    gradeable.update(
        cohort="paired",
        pair_method="signal_key",
        entry_slippage_bps=3.0,
        exit_slippage_bps=4.0,
        gap_bps=-20.0,
    )
    no_net = parity._blank_row()
    no_net.update(
        cohort="paired",
        pair_method="signal_key",
        entry_slippage_bps=300.0,
        exit_slippage_bps=400.0,
        gap_bps=None,
    )

    summary = parity.summarize([gradeable, no_net])

    assert summary["n_pairs"] == 2
    assert summary["n_paired_net"] == 1
    assert summary["entry_slip_n"] == 2
    assert summary["exit_slip_n"] == 2
    assert summary["entry_component_bps"] == -3.0
    assert summary["exit_component_bps"] == -4.0
    assert summary["residual_bps"] == -13.0


def test_gate_entries_never_pool_with_kernel_trades(tmp_path: Path) -> None:
    model = _load_model(tmp_path, [_model_trade(symbol="HNTUSDT", signal_ts_ms=T0)])
    gate = _live(symbol="HNTUSDT", signal_ts_ms=T0, pattern="llm_gate")
    gate_wide = _live(symbol="HNTUSDT", signal_ts_ms=T0 + HOUR_MS, pattern="llm_gate_wide")
    cohorts = parity.pair_trades(model, [gate, gate_wide], window_ms=24 * HOUR_MS)

    assert cohorts.pairs == []
    assert [id(t) for t in cohorts.gate] == [id(gate), id(gate_wide)]
    assert [m["symbol"] for m in cohorts.model_only] == ["HNTUSDT"]
    rows = parity.build_rows(cohorts)
    gate_rows = [r for r in rows if r["cohort"] == "live_only_gate"]
    assert len(gate_rows) == 2
    assert all(r["structural"] == parity.STRUCTURAL_GATE_ONLY for r in gate_rows)


def test_model_only_rows_carry_the_missed_trade(tmp_path: Path) -> None:
    model = _load_model(
        tmp_path,
        [_model_trade(symbol="OPUSDT", signal_ts_ms=T0, entry_price=2.0, exit_price=2.1, exit_reason="take_profit")],
    )
    cohorts = parity.pair_trades(model, [], window_ms=24 * HOUR_MS)
    row = parity.build_rows(cohorts)[0]

    assert row["cohort"] == "model_only"
    assert row["model_entry_px"] == 2.0
    assert row["model_exit_px"] == 2.1
    assert row["model_exit_reason"] == "take_profit"
    assert row["model_price_net_bps"] is not None


def test_model_entry_during_prior_live_position_is_state_divergence(tmp_path: Path) -> None:
    model = _load_model(
        tmp_path,
        [_model_trade(symbol="PUMPFUNUSDT", signal_ts_ms=T0, entry_ts_ms=T0 + 2 * HOUR_MS)],
    )
    held = _live(
        symbol="PUMPFUNUSDT",
        signal_ts_ms=T0 - DAY_MS,
        entry_ts_ms=T0 - DAY_MS + HOUR_MS,
        exit_ts_ms=T0 + 2 * HOUR_MS + 90_000,
    )

    cohorts = parity.pair_trades(model, [], window_ms=24 * HOUR_MS, context_trades=[held])
    rows = parity.build_rows(cohorts)

    assert cohorts.model_only == []
    assert cohorts.model_while_live_held == [(model[0], held)]
    assert rows[0]["cohort"] == "model_while_live_held"
    assert rows[0]["structural"] == parity.STRUCTURAL_LIVE_ALREADY_HELD
    assert "state/path divergence" in rows[0]["notes"]
    summary = parity.summarize(rows)
    assert summary["n_model_while_live_held"] == 1
    assert summary["n_model_only"] == 0


def test_model_return_normalization_is_per_unit_notional(tmp_path: Path) -> None:
    trade = _model_trade(entry_price=100.0, exit_price=103.0, notional_weight=0.5, funding_return=-0.002)
    rows = _load_model(tmp_path, [trade])
    row = rows[0]
    # Price-path net per unit of the trade's own notional: gross plus the modeled
    # round-trip cost, funding kept out.
    expected_price = (trade["gross_trade_return"] + trade["cost_return"] / 0.5) * 1e4
    assert abs(row["model_price_net_bps"] - expected_price) < 1e-9
    assert abs(row["model_funding_bps"] - (-0.002 / 0.5) * 1e4) < 1e-9


def test_model_window_filters_on_signal_timestamp(tmp_path: Path) -> None:
    inside = _model_trade(symbol="A1USDT", signal_ts_ms=T0)
    before = _model_trade(symbol="A2USDT", signal_ts_ms=T0 - DAY_MS)
    at_end = _model_trade(symbol="A3USDT", signal_ts_ms=T0 + 7 * DAY_MS)
    rows = _load_model(tmp_path, [inside, before, at_end], start_ms=T0, end_ms=T0 + 7 * DAY_MS)
    assert [r["symbol"] for r in rows] == ["A1USDT"]


def test_missing_or_thin_transitions_tolerated(tmp_path: Path) -> None:
    assert parity.load_transitions(tmp_path / "does-not-exist.jsonl") == []
    thin = _write_jsonl(
        tmp_path / "transitions.jsonl",
        [
            {
                "ts_ms": T0 + HOUR_MS,
                "event": "enter",
                "trade_id": f"long-ENAUSDT-{T0}",
                "symbol": "ENAUSDT",
                "pattern": "fomo_chase",
                "entry_reason": "sniper_retrace",
                "signal_ts_ms": T0,
                "notional_usdt": 450.0,
            }
        ],
    )
    trades = parity.build_live_trades(parity.load_transitions(thin), [], None, [])
    assert len(trades) == 1
    assert trades[0].signal_ts_ms == T0
    # A transition records a target-book request, not an engine fill.
    assert trades[0].request_ts_ms == T0 + HOUR_MS
    assert trades[0].request_ts_source == "transitions"
    assert trades[0].entry_ts_ms is None


def test_malformed_jsonl_is_not_silently_dropped(tmp_path: Path) -> None:
    broken = tmp_path / "transitions.jsonl"
    broken.write_text('{"event":"enter"}\nnot-json\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"transitions\.jsonl:2: malformed JSON"):
        parity.load_transitions(broken)


def test_engine_round_trip_null_tolerated(tmp_path: Path) -> None:
    engine = _write_jsonl(
        tmp_path / "trades.jsonl",
        [
            {
                "sleeve": "long",
                "symbol": "PUMPFUNUSDT",
                "side": "long",
                "qty": 6000.0,
                "exit_px": 0.005,
                "closed_ms": T0 + DAY_MS,
                "fills": 2,
                "round_trip": None,
            },
            {"sleeve": "carry", "symbol": "X", "side": "long", "qty": 1.0, "exit_px": 1.0, "closed_ms": T0},
        ],
    )
    rows = parity.load_engine_long_trades(engine)
    assert [r["symbol"] for r in rows] == ["PUMPFUNUSDT"]
    trades = parity.build_live_trades([], rows, None, [])
    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_px == 0.005
    assert trade.entry_px is None
    assert trade.net_bps is None
    assert trade.signal_ts_ms is None


def test_cycle_evidence_recovers_entry_intent_and_infers_entry_time(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    candidate = {
        "trade_id": f"long-AAVEUSDT-{T0}",
        "symbol": "AAVEUSDT",
        "pattern": "fomo_chase",
        "signal_ts_ms": T0,
        "signal_close": 143.89,
        "live_price": 142.95,
        "entry_reason": "sniper_retrace",
        "entry_ready_ts_ms": T0 + 2 * HOUR_MS,
        "stop_loss_pct": 0.1644,
    }
    (reports / "long_native_cycle_a.json").write_text(
        json.dumps(
            {
                "cycle": {"ts_ms": T0 + 2 * HOUR_MS, "entry_book_additions": 1},
                "strategy_config": {"fc_max_hold_days": 3},
                "candidates": [candidate],
                "planned_exits": [],
            }
        ),
        encoding="utf-8",
    )
    deadline = T0 + 4 * HOUR_MS + 3 * DAY_MS
    (reports / "long_native_cycle_b.json").write_text(
        json.dumps(
            {
                "cycle": {"ts_ms": deadline},
                "strategy_config": {"fc_max_hold_days": 3},
                "candidates": [],
                "planned_exits": [
                    {
                        "trade_id": f"long-ENAUSDT-{T0}",
                        "symbol": "ENAUSDT",
                        "exit_reason": "time_stop",
                        "max_hold_deadline_ts_ms": deadline,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    cycles = parity.load_cycle_evidence(reports)
    trades = parity.build_live_trades([], [], cycles, [])
    by_symbol = {t.symbol: t for t in trades}

    aave = by_symbol["AAVEUSDT"]
    assert aave.request_ts_ms == T0 + 2 * HOUR_MS
    assert aave.request_ts_source == "cycle_candidate"
    assert aave.entry_ts_ms is None
    assert aave.entry_px is None

    ena = by_symbol["ENAUSDT"]
    assert ena.exit_reason == "time_stop"
    assert ena.hold_deadline_ts_ms == deadline
    assert ena.exit_request_ts_ms == deadline
    # The deadline starts when the producer first observes a fill, so this is
    # an observation stamp rather than an invented exact fill timestamp.
    assert ena.entry_ts_ms == deadline - 3 * DAY_MS
    assert ena.entry_ts_source == "producer_fill_observed_ts"
    assert cycles.cycle_count == 2
    assert cycles.first_cycle_ts_ms == T0 + 2 * HOUR_MS
    assert cycles.last_cycle_ts_ms == deadline


def test_uncorroborated_candidate_is_not_a_live_trade(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "long_native_cycle_a.json").write_text(
        json.dumps(
            {
                "cycle": {"ts_ms": T0, "entry_book_additions": 0},
                "strategy_config": {"fc_max_hold_days": 3},
                "candidates": [
                    {
                        "trade_id": f"long-XRPUSDT-{T0}",
                        "symbol": "XRPUSDT",
                        "pattern": "fomo_chase",
                        "signal_ts_ms": T0,
                        "live_price": 1.0,
                        "entry_ready_ts_ms": T0,
                    }
                ],
                "planned_exits": [],
            }
        ),
        encoding="utf-8",
    )
    cycles = parity.load_cycle_evidence(reports)
    trades = parity.build_live_trades([], [], cycles, [])
    assert trades == []
    assert cycles.uncorroborated_candidate_ids == {f"long-XRPUSDT-{T0}"}


def test_cycle_entry_count_does_not_mark_every_candidate_entered(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    candidates = [
        {
            "trade_id": f"long-{symbol}-{T0}",
            "symbol": symbol,
            "pattern": "fomo_chase",
            "signal_ts_ms": T0,
        }
        for symbol in ("XRPUSDT", "SOLUSDT")
    ]
    (reports / "long_native_cycle_ambiguous.json").write_text(
        json.dumps(
            {
                "cycle": {"ts_ms": T0, "entry_book_additions": 1},
                "candidates": candidates,
                "planned_exits": [],
            }
        ),
        encoding="utf-8",
    )

    cycles = parity.load_cycle_evidence(reports)

    assert parity.build_live_trades([], [], cycles, []) == []
    assert cycles.uncorroborated_candidate_ids == {candidate["trade_id"] for candidate in candidates}


def test_venue_backstop_uses_terminal_price_but_withholds_unlinked_net(tmp_path: Path) -> None:
    venue = [
        # A small close inside the producer trade's lifetime.
        {
            "_kind": "closed_pnl",
            "symbol": "ENAUSDT",
            "side": "Sell",
            "updatedTime": str(T0 + DAY_MS),
            "avgEntryPrice": "0.15606833",
            "avgExitPrice": "0.16498",
            "closedPnl": "0.62",
            "cumEntryValue": "11.08",
            "cumExitValue": str(0.16498 * 71),
            "openFee": "0.006",
            "closeFee": "0.0064",
            "closedSize": "71",
            "qty": "71",
        },
        # The largest close is NOT terminal; choosing it was the real ENA bug.
        {
            "_kind": "closed_pnl",
            "symbol": "ENAUSDT",
            "side": "Sell",
            "updatedTime": str(T0 + 2 * DAY_MS),
            "avgEntryPrice": "0.15617",
            "avgExitPrice": "0.14108",
            "closedPnl": "-24.03",
            "cumEntryValue": "244.25",
            "cumExitValue": str(0.14108 * 1564),
            "openFee": "0.1343",
            "closeFee": "0.1214",
            "closedSize": "1564",
            "qty": "1564",
        },
        # Nearest to the producer's time-stop request, but its quantity does
        # not match the planned exit and the venue row has no sleeve/trade id.
        {
            "_kind": "closed_pnl",
            "symbol": "ENAUSDT",
            "side": "Sell",
            "updatedTime": str(T0 + 3 * DAY_MS + 2_000),
            "avgEntryPrice": "0.14171",
            "avgExitPrice": "0.14266",
            "closedPnl": "1.254",
            "cumEntryValue": "224.89",
            "cumExitValue": str(0.14266 * 1587),
            "openFee": "0.1237",
            "closeFee": "0.1245",
            "closedSize": "1587",
            "qty": "1587",
        },
        # Outside the trade window: ignored.
        {
            "_kind": "closed_pnl",
            "symbol": "ENAUSDT",
            "side": "Sell",
            "updatedTime": str(T0 + 20 * DAY_MS),
            "avgEntryPrice": "0.2",
            "avgExitPrice": "0.21",
            "closedPnl": "1.0",
            "cumEntryValue": "50.0",
            "cumExitValue": "52.5",
            "openFee": "0.02",
            "closeFee": "0.02",
            "closedSize": "250",
            "qty": "250",
        },
    ]
    transitions = [
        {
            "ts_ms": T0 + HOUR_MS,
            "event": "enter",
            "trade_id": f"long-ENAUSDT-{T0}",
            "symbol": "ENAUSDT",
            "pattern": "fomo_chase",
            "entry_reason": "sniper_retrace",
            "signal_ts_ms": T0,
            "notional_usdt": 450.0,
        },
        {
            "ts_ms": T0 + 3 * DAY_MS,
            "event": "leave",
            "trade_id": f"long-ENAUSDT-{T0}",
            "symbol": "ENAUSDT",
            "pattern": "fomo_chase",
            "entry_reason": "sniper_retrace",
            "signal_ts_ms": T0,
            "notional_usdt": 450.0,
        },
    ]
    trade_id = f"long-ENAUSDT-{T0}"
    planned_qty = 1437.4397783728
    cycles = parity.CycleEvidence(
        candidates={},
        planned_exits={
            trade_id: {
                "trade_id": trade_id,
                "symbol": "ENAUSDT",
                "qty": str(planned_qty),
                "exit_reason": "time_stop",
                "exit_request_ts_ms": T0 + 3 * DAY_MS,
                "max_hold_deadline_ts_ms": T0 + 3 * DAY_MS,
            }
        },
        uncorroborated_candidate_ids=set(),
    )
    trades = parity.build_live_trades(transitions, [], cycles, venue)
    trade = trades[0]
    terminal = venue[2]
    entry_value = float(terminal["cumEntryValue"])
    price_fee_pnl = (
        float(terminal["cumExitValue"]) - entry_value - float(terminal["openFee"]) - float(terminal["closeFee"])
    )
    assert trade.entry_px == pytest.approx(float(terminal["avgEntryPrice"]))
    assert trade.entry_px_source == "venue_history_terminal_ambiguous"
    assert trade.exit_px == pytest.approx(float(terminal["avgExitPrice"]))
    assert trade.exit_ts_ms == T0 + 3 * DAY_MS + 2_000
    assert trade.venue_terminal_gap_ms == 2_000
    assert trade.net_bps is None
    assert trade.net_bps_source is None
    assert trade.venue_rows == 3
    assert trade.planned_exit_qty == pytest.approx(planned_qty)
    assert trade.venue_terminal_qty == 1587
    assert trade.venue_qty_gap == pytest.approx(1587 - planned_qty)
    assert trade.venue_terminal_closed_pnl_usdt == pytest.approx(float(terminal["closedPnl"]))
    assert trade.venue_terminal_price_fee_pnl_usdt == pytest.approx(price_fee_pnl)
    assert trade.venue_terminal_price_fee_net_bps == pytest.approx(price_fee_pnl / entry_value * 1e4)
    assert trade.venue_closed_pnl_residual_usdt == pytest.approx(float(terminal["closedPnl"]) - price_fee_pnl)
    assert "venue-only net withheld" in "; ".join(trade.notes)

    model = _load_model(tmp_path, [_model_trade(symbol="ENAUSDT", signal_ts_ms=T0)])
    paired = parity.build_rows(parity.pair_trades(model, [trade], window_ms=24 * HOUR_MS))[0]
    assert paired["entry_slippage_bps"] is None
    assert paired["exit_slippage_bps"] is None
    assert paired["gap_bps"] is None


def test_venue_position_reconstruction_explains_closed_pnl_residual() -> None:
    terminal_ts = T0 + 4 * HOUR_MS
    terminal = {
        "symbol": "AAVEUSDT",
        "side": "Sell",
        "orderId": "close-order",
        "updatedTime": str(terminal_ts),
        "closedSize": "1.5",
    }
    transactions = [
        {
            "_kind": "txn",
            "symbol": "AAVEUSDT",
            "type": "TRADE",
            "side": "Buy",
            "orderId": "open-order",
            "transactionTime": str(T0),
            "qty": "1",
            "size": "1",
        },
        {
            "_kind": "txn",
            "symbol": "AAVEUSDT",
            "type": "SETTLEMENT",
            "side": "Buy",
            "orderId": "settlement-1",
            "transactionTime": str(T0 + HOUR_MS),
            "qty": "1",
            "size": "1",
            "currency": "USDT",
            "funding": "-0.1",
        },
        {
            "_kind": "txn",
            "symbol": "AAVEUSDT",
            "type": "TRADE",
            "side": "Buy",
            "orderId": "add-order",
            "transactionTime": str(T0 + 2 * HOUR_MS),
            "qty": "0.5",
            "size": "1.5",
        },
        {
            "_kind": "txn",
            "symbol": "AAVEUSDT",
            "type": "SETTLEMENT",
            "side": "Buy",
            "orderId": "settlement-2",
            "transactionTime": str(T0 + 3 * HOUR_MS),
            "qty": "1.5",
            "size": "1.5",
            "currency": "USDT",
            "funding": "0.02",
        },
        {
            "_kind": "txn",
            "symbol": "AAVEUSDT",
            "type": "TRADE",
            "side": "Sell",
            "orderId": "close-order",
            "transactionTime": str(terminal_ts),
            "qty": "1.5",
            "size": "0",
        },
    ]

    evidence = parity._position_settlements(terminal, transactions)
    assert evidence.status == "exact_one_way_position"
    assert evidence.reason is None
    assert evidence.open_ts_ms == T0
    assert evidence.close_ts_ms == terminal_ts
    assert evidence.open_order_id == "open-order"
    assert evidence.close_order_id == "close-order"
    assert evidence.open_qty == 1.0
    assert evidence.close_qty == 1.5
    assert evidence.trade_rows == 3
    assert evidence.resizes == 1
    assert len(evidence.settlement_rows) == 2
    assert evidence.settlement_usdt == pytest.approx(-0.08)


def test_venue_position_reconstruction_refuses_nonterminal_close() -> None:
    terminal = {
        "symbol": "ENAUSDT",
        "side": "Sell",
        "orderId": "partial-close",
        "updatedTime": str(T0 + HOUR_MS),
        "closedSize": "2",
    }
    transactions = [
        {
            "_kind": "txn",
            "symbol": "ENAUSDT",
            "type": "TRADE",
            "side": "Sell",
            "orderId": "partial-close",
            "transactionTime": str(T0 + HOUR_MS),
            "qty": "2",
            "size": "3",
        }
    ]

    evidence = parity._position_settlements(terminal, transactions)
    assert evidence.status == "incomplete"
    assert evidence.reason == "terminal order does not leave the venue position flat"


def test_duplicate_venue_transactions_are_deduped_and_conflicts_fail() -> None:
    row = {"_kind": "txn", "id": "txn-1", "symbol": "AAVEUSDT", "funding": "-0.1"}
    unique, duplicates = parity._dedupe_venue_transactions([row, dict(row)])
    assert unique == [row]
    assert duplicates == 1

    conflicting = {**row, "funding": "-0.2"}
    with pytest.raises(ValueError, match="conflicting rows"):
        parity._dedupe_venue_transactions([row, conflicting])


def test_engine_round_trip_links_exact_venue_position_to_long_sleeve() -> None:
    trade = parity.LiveTrade(
        symbol="AAVEUSDT",
        entry_ts_ms=T0,
        exit_ts_ms=T0 + DAY_MS,
        entry_px=141.25,
        exit_px=128.43,
        venue_position_reconstruction="exact_one_way_position",
        venue_position_open_ts_ms=T0,
        venue_position_close_ts_ms=T0 + DAY_MS,
        venue_position_close_qty=1.55,
        venue_position_trade_rows=5,
        venue_terminal_entry_value_usdt=218.9375,
        venue_terminal_price_fee_pnl_usdt=-20.1,
        engine_attached=True,
        engine_side="long",
        engine_qty=1.55,
        engine_fills=5,
        engine_entry_notional_usdt=218.9375,
        engine_price_fee_pnl_usdt=-20.1,
    )
    terminal = {"avgEntryPrice": "141.25", "avgExitPrice": "128.43"}
    assert parity._engine_position_link(trade, terminal) == "exact_long_sleeve"

    trade.engine_fills = 4
    assert parity._engine_position_link(trade, terminal) == "unlinked: engine and venue position facts differ"


def test_live_window_filter_keeps_signal_inside_and_reports_outside(tmp_path: Path) -> None:
    inside = _live(symbol="ENAUSDT", signal_ts_ms=T0)
    outside = _live(symbol="HNTUSDT", signal_ts_ms=T0 + 8 * DAY_MS, pattern="llm_gate")
    no_signal_exit_inside = _live(
        symbol="PUMPFUNUSDT",
        signal_ts_ms=None,
        entry_ts_ms=None,
        entry_ts_source=None,
        exit_ts_ms=T0 + DAY_MS,
    )
    kept, dropped = parity.window_filter_live(
        [inside, outside, no_signal_exit_inside], start_ms=T0, end_ms=T0 + 7 * DAY_MS
    )
    assert [id(t) for t in kept] == [id(inside), id(no_signal_exit_inside)]
    assert [id(t) for t in dropped] == [id(outside)]


def test_main_end_to_end_writes_csv_report_and_summary(tmp_path: Path, capsys) -> None:
    model_csv = _write_model_csv(
        tmp_path / "model_trades.csv",
        [
            _model_trade(symbol="ENAUSDT", signal_ts_ms=T0, entry_price=0.1560, exit_price=0.1562),
            _model_trade(
                symbol="OPUSDT", signal_ts_ms=T0 + DAY_MS, entry_price=2.0, exit_price=2.2, exit_reason="take_profit"
            ),
        ],
    )
    transitions = _write_jsonl(
        tmp_path / "transitions.jsonl",
        [
            {
                "ts_ms": T0 + 2 * HOUR_MS,
                "event": "enter",
                "trade_id": f"long-ENAUSDT-{T0}",
                "symbol": "ENAUSDT",
                "pattern": "fomo_chase",
                "entry_reason": "sniper_retrace",
                "signal_ts_ms": T0,
                "notional_usdt": 450.0,
            }
        ],
    )
    engine = _write_jsonl(
        tmp_path / "trades.jsonl",
        [
            {
                "sleeve": "long",
                "symbol": "ENAUSDT",
                "side": "long",
                "qty": 1564.0,
                "exit_px": 0.14108,
                "closed_ms": T0 + 3 * DAY_MS,
                "fills": 2,
                "round_trip": {
                    "entry_px": 0.15617,
                    "entry_notional_usdt": 244.25,
                    "gross_usdt": -23.6,
                    "fees_usdt": 0.43,
                    "net_usdt": -24.03,
                    "net_bps": -983.9,
                    "opened_ms": T0 + 2 * HOUR_MS,
                    "held_ms": 3 * DAY_MS - 2 * HOUR_MS,
                },
            }
        ],
    )
    out = tmp_path / "out"
    parity.main(
        [
            "--start",
            "2026-08-23",
            "--end",
            "2026-08-30",
            "--model-trades",
            str(model_csv),
            "--transitions",
            str(transitions),
            "--trades",
            str(engine),
            "--data-root",
            str(tmp_path / "data"),
            "--out",
            str(out),
        ]
    )
    pairs_csv = out / "long_live_vs_model_pairs.csv"
    report_md = out / "long_live_vs_model_report.md"
    provenance_json = out / "long_live_vs_model_provenance.json"
    assert pairs_csv.exists() and report_md.exists() and provenance_json.exists()

    with pairs_csv.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    by_cohort = {r["cohort"] for r in rows}
    assert by_cohort == {"paired", "model_only"}
    paired = next(r for r in rows if r["cohort"] == "paired")
    assert paired["symbol"] == "ENAUSDT"
    assert paired["live_entry_px"] == "0.15617"

    printed = capsys.readouterr().out
    assert "pairs: 1" in printed
    assert "verdict:" in printed
    text = report_md.read_text(encoding="utf-8")
    assert "cold start" in text.lower()
    assert "funding" in text.lower()

    provenance = json.loads(provenance_json.read_text(encoding="utf-8"))
    identities = provenance["input_identities"]
    assert identities["model_ledger"]["sha256"]
    assert identities["transitions"]["sha256"]
    assert identities["engine_journal"]["sha256"]
    assert identities["data_root"]["content_hash_complete"] is False
    assert identities["data_root"]["read_by_checker"] is False
    assert identities["archive_manifest_report"]["exists"] is False
