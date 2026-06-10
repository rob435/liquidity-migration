"""Tests for the live BTC-beta hedge manager (WP3 live wiring, 2026-06-10)."""

from __future__ import annotations

from pathlib import Path

from liquidity_migration.continuous_hedge_manager import (
    HEDGE_SYMBOL,
    ContinuousHedgeConfig,
    compute_hedge_decision,
    extend_with_live_days,
    hedge_order_link_id,
    load_warmstart,
)


def _anti_correlated(n: int = 120) -> tuple[list[float], list[float | None]]:
    """Book unit returns anti-correlated with BTC (short-alt book vs BTC long).

    beta ~ -0.2 keeps the full-gross hedge ratio (0.2) under the 0.30 equity cap so
    the proportionality property is testable without the clip interfering."""
    btc: list[float | None] = [0.01 if i % 2 == 0 else -0.01 for i in range(n)]
    unit = [-0.2 * float(b) + 0.0005 for b in btc]  # beta ~ -0.2
    return unit, btc


def test_warmstart_round_trip_and_extend() -> None:
    unit, btc = _anti_correlated(10)
    u2, b2 = extend_with_live_days(
        unit, btc,
        live_unit_by_day={"2026-06-10": 0.002, "2026-06-11": -0.001},
        live_btc_by_day={"2026-06-10": -0.005},  # 06-11 BTC missing -> None
    )
    assert len(u2) == 12 and len(b2) == 12
    assert u2[-2:] == [0.002, -0.001]
    assert b2[-2] == -0.005 and b2[-1] is None  # missing BTC excluded from beta


def test_hedge_is_long_only_and_sized_to_gross() -> None:
    unit, btc = _anti_correlated(120)
    cfg = ContinuousHedgeConfig(min_resize_notional_usdt=5.0)
    # full-gross book -> hedge ratio = clip(-beta,0,2)*1.0
    full = compute_hedge_decision(
        cfg, unit_returns=unit, btc_returns=btc, live_gross_short_frac=0.5,
        btc_price=100_000.0, current_hedge_qty=0.0, equity_usdt=10_000.0,
    )
    assert full.hedge_ratio_equity_frac > 0.0  # anti-correlated book -> long BTC hedge
    assert full.plan is not None and full.plan.side == "Buy" and not full.plan.reduce_only
    # half-deployed book -> half the hedge
    half = compute_hedge_decision(
        cfg, unit_returns=unit, btc_returns=btc, live_gross_short_frac=0.25,
        btc_price=100_000.0, current_hedge_qty=0.0, equity_usdt=10_000.0,
    )
    assert abs(half.hedge_ratio_equity_frac - 0.5 * full.hedge_ratio_equity_frac) < 1e-9


def test_positive_beta_book_takes_no_hedge() -> None:
    btc: list[float | None] = [0.01 if i % 2 == 0 else -0.01 for i in range(120)]
    unit = [0.4 * float(b) for b in btc]  # POSITIVE beta -> long-only clip = 0
    d = compute_hedge_decision(
        ContinuousHedgeConfig(), unit_returns=unit, btc_returns=btc,
        live_gross_short_frac=0.5, btc_price=100_000.0, current_hedge_qty=0.0, equity_usdt=10_000.0,
    )
    assert d.hedge_ratio_equity_frac == 0.0
    assert d.plan is None  # nothing to buy from flat


def test_hard_equity_cap() -> None:
    btc: list[float | None] = [0.01 if i % 2 == 0 else -0.01 for i in range(120)]
    unit = [-3.0 * float(b) for b in btc]  # huge negative beta -> ratio would blow past cap
    cfg = ContinuousHedgeConfig(max_hedge_equity_frac=0.30)
    d = compute_hedge_decision(
        cfg, unit_returns=unit, btc_returns=btc, live_gross_short_frac=0.5,
        btc_price=100_000.0, current_hedge_qty=0.0, equity_usdt=10_000.0,
    )
    assert d.hedge_ratio_equity_frac == 0.30  # capped


def test_reduce_when_overhedged() -> None:
    unit, btc = _anti_correlated(120)
    d = compute_hedge_decision(
        ContinuousHedgeConfig(min_resize_notional_usdt=5.0),
        unit_returns=unit, btc_returns=btc, live_gross_short_frac=0.5,
        btc_price=100_000.0, current_hedge_qty=1.0, equity_usdt=10_000.0,  # 100k held >> target
    )
    assert d.plan is not None and d.plan.side == "Sell" and d.plan.reduce_only


def test_min_obs_no_hedge_before_warmup() -> None:
    unit, btc = _anti_correlated(40)  # < beta_min_obs (60)
    d = compute_hedge_decision(
        ContinuousHedgeConfig(), unit_returns=unit, btc_returns=btc,
        live_gross_short_frac=0.5, btc_price=100_000.0, current_hedge_qty=0.0, equity_usdt=10_000.0,
    )
    assert d.hedge_ratio_equity_frac == 0.0  # beta=0 until min_obs satisfied


def test_n_obs_counts_beta_window_only() -> None:
    btc: list[float | None] = [0.01 if i % 2 == 0 else -0.01 for i in range(150)]
    for i in range(60, 91):
        btc[i] = None
    unit = [-0.2 * float(b) if b is not None else 0.0 for b in btc]

    d = compute_hedge_decision(
        ContinuousHedgeConfig(), unit_returns=unit, btc_returns=btc,
        live_gross_short_frac=0.5, btc_price=100_000.0, current_hedge_qty=0.0, equity_usdt=10_000.0,
    )

    assert d.n_obs == 59
    assert d.diagnostics["beta_window_observations"] == 59
    assert d.hedge_ratio_equity_frac == 0.0


def test_shipped_warmstart_artifacts_exist_and_load() -> None:
    repo = Path(__file__).resolve().parent.parent
    for venue in ("bybit", "binance"):
        p = repo / "deploy" / "hedge_warmstart" / f"{venue}_warmstart.csv"
        assert p.exists(), f"missing warm-start artifact: {p}"
        unit, btc = load_warmstart(p)
        assert len(unit) >= 90, f"{venue} warm-start shorter than the 90d beta window"
        assert len(unit) == len(btc)
        assert sum(1 for b in btc if b is not None) >= 60  # enough for beta_min_obs


def test_link_id_namespace() -> None:
    lid = hedge_order_link_id(1_700_000_000_000)
    assert lid.startswith("lm-en-ca-")
    assert f"-{HEDGE_SYMBOL[:3]}-" in lid


def test_hedge_row_is_tracked_but_never_force_exited() -> None:
    """SAFETY CONTRACT: a hedge trade row (stop/tp/planned_exit all 0) yields NO exit
    from the risk service's plan_risk_exits at any price — so the risk daemon tracks
    the BTC long (adoption skips a known symbol) but never force-closes it."""
    import polars as pl

    from liquidity_migration.continuous_hedge_manager import build_hedge_trade_row
    from liquidity_migration.event_demo_planning import plan_risk_exits

    row = build_hedge_trade_row(
        ContinuousHedgeConfig(), qty=0.01, entry_price=100_000.0, now_ms=1_700_000_000_000,
        order_link_id="lm-en-ca-btc-x", order_id="oid1",
    )
    assert row["stop_price"] == 0.0 and row["take_profit_price"] == 0.0 and row["planned_exit_ts_ms"] == 0
    assert row["side"] == "long" and row["sleeve"] == "continuous_addon" and row["status"] == "open"
    trades = pl.DataFrame([row])
    # Even at a price far above AND far below entry, and far in the future, no exit fires.
    for px in (50_000.0, 100_000.0, 500_000.0):
        exits = plan_risk_exits(
            trades,
            position_by_symbol={"BTCUSDT": {"symbol": "BTCUSDT", "side": "Buy", "size": "0.01"}},
            price_by_symbol={"BTCUSDT": px},
            now_ms=9_999_999_999_999,  # far future -> would trip a real max_hold
        )
        assert exits == [], f"hedge row force-exited at price {px}: {exits}"
