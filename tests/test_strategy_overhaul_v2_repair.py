from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import polars as pl
import pytest


REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "run_strategy_overhaul_v2_repair",
        REPO / "scripts/run_strategy_overhaul_v2_repair.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_strategy_overhaul_v2_repair"] = module
    spec.loader.exec_module(module)
    return module


MOD = _load()


def test_compare_rmom_requires_exact_stable_key_set_and_values() -> None:
    start_ms = MOD._date_ms(MOD.COMPARATOR_START)
    rebuilt = pl.DataFrame(
        {
            "symbol": ["AAA", "BBB", "AAA"],
            "ts_ms": [start_ms, start_ms, start_ms + 86_400_000],
            "residual_momentum": [1.0, 2.0, 3.0],
            "is_provisional": [False, False, True],
        }
    )
    legacy = rebuilt.drop("is_provisional").head(2)

    receipt = MOD._compare_rmom_to_legacy(rebuilt, legacy)

    assert receipt["status"] == "pass"
    assert receipt["matching_keys"] == 2
    with pytest.raises(RuntimeError, match="stable-key sets differ"):
        MOD._compare_rmom_to_legacy(
            rebuilt,
            legacy.vstack(
                pl.DataFrame(
                    {
                        "symbol": ["CCC"],
                        "ts_ms": [start_ms],
                        "residual_momentum": [4.0],
                    }
                )
            ),
        )


def test_load_ledger_pins_counts_keys_and_pnl_identities() -> None:
    ledger = MOD._load_and_validate_ledger()

    assert ledger.height == sum(MOD.EXPECTED_TRADE_COUNTS.values())
    assert ledger["source_key"].n_unique() == ledger.height


def test_source_key_hash_matches_frozen_samples() -> None:
    ledger = MOD._load_and_validate_ledger()
    sample = MOD.phase3._account_sample(ledger)

    for sleeve, expected in MOD.EXPECTED_SAMPLE.items():
        part = sample.filter(pl.col("sleeve") == sleeve)
        assert MOD._source_key_sha256(part) == expected["source_keys_sha256"]
