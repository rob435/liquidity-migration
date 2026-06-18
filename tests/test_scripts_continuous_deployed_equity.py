"""Pin the deployed-equity stats() Sharpe convention (audit-iter3 scripts-equity)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import polars as pl

REPO = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "continuous_deployed_equity", REPO / "scripts" / "continuous_deployed_equity.py"
    )
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["continuous_deployed_equity"] = m
    spec.loader.exec_module(m)
    return m


MOD = _load()
_DAY = 86_400_000


def test_stats_sharpe_none_on_flat_series() -> None:
    # A zero-variance daily series must yield sharpe=None, not a div-by-~0 inf/nan.
    df = pl.DataFrame({"ts_ms": [i * _DAY for i in range(5)], "basket_return": [0.0] * 5})
    assert MOD.stats(df)["sharpe_daily_ann"] is None


def test_stats_sharpe_uses_sample_std() -> None:
    # A varying series produces a finite Sharpe (sample std, ddof=1).
    rets = [0.01, -0.02, 0.03, -0.01, 0.02]
    df = pl.DataFrame({"ts_ms": [i * _DAY for i in range(5)], "basket_return": rets})
    s = MOD.stats(df)["sharpe_daily_ann"]
    assert s is not None and isinstance(s, float)


def test_deployed_equity_literals_match_frozen_config() -> None:
    """audit-iter5: the equity tool's hardcoded weights/rule/hedge are separate copies
    of the deployed object — pin them to the canonical FROZEN_FORWARD_CONFIG so a future
    retune can't silently leave the 'deployed continuous' curve drawing stale params."""
    from liquidity_migration.continuous_forward_replay import (
        FROZEN_FORWARD_CONFIG,
        frozen_hedge_rule,
        frozen_rebalance_rule,
    )
    from liquidity_migration.continuous_rebalance import ContinuousHedgeRule

    assert MOD.WINNER_WEIGHTS == FROZEN_FORWARD_CONFIG["weights"]
    assert MOD.winner_rule() == frozen_rebalance_rule()
    assert ContinuousHedgeRule(90, 60, 2.0, 5.0) == frozen_hedge_rule()
