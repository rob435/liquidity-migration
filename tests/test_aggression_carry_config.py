from __future__ import annotations

from pathlib import Path

from aggression_carry.config import load_config


def test_volume_alpha_controls_load_from_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
volume_alpha:
  horizons_d: [1, 7]
  quantiles: [0.25, 0.50]
cost_model:
  maker_fee_bps: 1.0
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.volume_alpha.horizons_d == (1, 7)
    assert config.volume_alpha.quantiles == (0.25, 0.50)
    assert config.costs.maker_fee_bps == 1.0
