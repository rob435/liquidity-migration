from __future__ import annotations

import math

from liquidity_migration.research.lab.evidence import (
    COST_HEADING,
    ERA_HEADING,
    GRID_HEADING,
    SECTIONS,
    evidence_note,
    render_table,
)


def _note(**overrides) -> str:
    kwargs = dict(
        title="State exits on the v12 ledger",
        claim="Leaving when the ETH regime turns off adds +0.018 book units over 5.7 years.",
        decision="whether LONG gets a state-driven exit",
        shaped_by="the 307-trade v12 ledger, 2021-01 to 2026-08 (all of it)",
        graded_on="the same data; nothing forward",
        scope="Bybit USDT perpetuals, LONG v12 ledger, 2021-01 to 2026-08, at the live weights",
        effect="+0.018 on +0.528, t 1.95 on 20 trades, 1% of placebo draws score as well.",
        grid=[
            dict(cell="eth_regime_off", trades=20, delta=0.0183, t=1.95, placebo_share=0.01),
            dict(cell="btc_regime_off", trades=25, delta=-0.0185, t=-0.95, placebo_share=0.615),
            dict(cell="funding_ge_10bp", trades=13, delta=0.0186, t=1.53, placebo_share=0.005, note=None),
        ],
        eras=[dict(year=2024, delta=0.005), dict(year=2025, delta=0.0115), dict(year=2026, delta=0.0032)],
        cost_model="45 bp round trip, settlement-exact funding",
        artifacts="~/SHARED_DATA/bybit_full_pit/reports/exit_program_2026-09-02/long/state_exits/",
        boundary="Lane-1 on seen data; 20 trades; three trades carry 73% of the gain.",
    )
    kwargs.update(overrides)
    return evidence_note(**kwargs)


def test_every_section_and_table_heading_is_present() -> None:
    text = _note()
    assert text.startswith("# State exits on the v12 ledger\n")
    for heading in SECTIONS:
        assert f"## {heading}\n" in text
    for heading in (GRID_HEADING, ERA_HEADING, COST_HEADING):
        assert f"### {heading}\n" in text
    assert text.index(SECTIONS[0]) < text.index(SECTIONS[1]) < text.index(SECTIONS[5])


def test_every_cell_era_and_cost_line_is_printed() -> None:
    text = _note()
    for cell in ("eth_regime_off", "btc_regime_off", "funding_ge_10bp"):
        assert cell in text
    assert "| 2024 | 0.005 |" in text
    assert "| 2026 | 0.0032 |" in text
    assert "Cost model: 45 bp round trip, settlement-exact funding" in text
    assert "Decision: whether LONG gets a state-driven exit" in text
    assert "Shaped by: the 307-trade v12 ledger" in text
    assert "Graded on: the same data; nothing forward" in text
    assert "| cell | trades | delta | t | placebo_share | note |" in text
    assert "| funding_ge_10bp | 13 | 0.0186 | 1.53 | 0.005 | — |" in text
    assert "| eth_regime_off | 20 | 0.0183 | 1.95 | 0.01 | — |" in text


def test_costs_table_sits_under_its_heading_when_given() -> None:
    text = _note(costs=[dict(arm="eth_regime_off", gross=0.0201, cost=0.0018, net=0.0183)])
    cost_block = text.split(f"### {COST_HEADING}")[1].split("## ")[0]
    assert "| arm | gross | cost | net |" in cost_block
    assert "| eth_regime_off | 0.0201 | 0.0018 | 0.0183 |" in cost_block
    assert render_table([]) == "(no rows)"
    assert "(no rows)" in _note(grid=[])


def test_table_cells_format_none_nan_bool_and_pipes() -> None:
    table = render_table([dict(a=None, b=math.nan, c=True, d="x|y", e=3), dict(a=1.23456789)])
    lines = table.splitlines()
    assert lines[0] == "| a | b | c | d | e |"
    assert lines[2] == "| — | — | yes | x\\|y | 3 |"
    assert lines[3] == "| 1.235 | — | — | — | — |"
