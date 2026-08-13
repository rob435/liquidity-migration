"""The research→engine plug seam renders exact, parseable TOML."""

import tomllib

import pytest

from liquidity_migration.research.engine_config import (
    EngineStrategyBlock,
    render_engine_strategies,
)


def _touch_block() -> EngineStrategyBlock:
    return EngineStrategyBlock(
        name="touch_sniper",
        capital_usdt=100.0,
        params={
            "symbol": "BTCUSDT",
            "side": "buy",
            "trigger_px": 60000.0,
            "qty": 0.001,
            "stop_px": 59000.0,
            "ttl_s": 3600,
        },
    )


def test_rendered_block_parses_back_to_the_same_values() -> None:
    text = render_engine_strategies([_touch_block()])
    parsed = tomllib.loads(text)
    assert len(parsed["strategy"]) == 1
    block = parsed["strategy"][0]
    assert block["name"] == "touch_sniper"
    assert block["capital_usdt"] == 100.0
    assert block["params"] == {
        "symbol": "BTCUSDT",
        "side": "buy",
        "trigger_px": 60000.0,
        "qty": 0.001,
        "stop_px": 59000.0,
        "ttl_s": 3600,
    }


def test_rendering_is_deterministic_and_sorted() -> None:
    a = render_engine_strategies([_touch_block()])
    b = render_engine_strategies([_touch_block()])
    assert a == b
    keys = [line.split(" = ")[0] for line in a.splitlines() if " = " in line and "name" not in line]
    assert keys[1:] == sorted(keys[1:])


def test_two_blocks_render_as_two_toml_entries() -> None:
    text = render_engine_strategies([_touch_block(), _touch_block()])
    assert len(tomllib.loads(text)["strategy"]) == 2


@pytest.mark.parametrize(
    ("block", "match"),
    [
        (EngineStrategyBlock(name="", capital_usdt=1.0), "not a plain identifier"),
        (EngineStrategyBlock(name="touch sniper", capital_usdt=1.0), "not a plain identifier"),
        (EngineStrategyBlock(name="t", capital_usdt=0.0), "positive finite"),
        (EngineStrategyBlock(name="t", capital_usdt=float("nan")), "positive finite"),
        (EngineStrategyBlock(name="t", capital_usdt=1.0, params={"bad key": 1}), "not a plain identifier"),
        (EngineStrategyBlock(name="t", capital_usdt=1.0, params={"px": float("inf")}), "not a finite number"),
        (EngineStrategyBlock(name="t", capital_usdt=1.0, params={"s": 'a"b'}), "would mangle"),
    ],
)
def test_bad_blocks_are_refused_with_a_named_reason(block: EngineStrategyBlock, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        render_engine_strategies([block])


def test_empty_render_is_refused() -> None:
    with pytest.raises(ValueError, match="no strategy blocks"):
        render_engine_strategies([])


def test_bools_do_not_render_as_integers() -> None:
    text = render_engine_strategies(
        [EngineStrategyBlock(name="t", capital_usdt=1.0, params={"enabled": True})]
    )
    assert "enabled = true" in text
