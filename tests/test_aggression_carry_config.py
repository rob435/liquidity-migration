from __future__ import annotations

from pathlib import Path

from aggression_carry.config import load_config


def test_signal_controls_load_from_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
signals:
  min_abs_score_entry: 0.75
  long_quantile: 0.30
  short_quantile: 0.10
  weights:
    carry: 0.50
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.signals.min_abs_score_entry == 0.75
    assert config.signals.long_quantile == 0.30
    assert config.signals.short_quantile == 0.10
    assert config.signals.weights["carry"] == 0.50
    assert config.signals.weights["momentum"] == 0.18
