"""Shared loader: build (and cache) the reversion_alpha feature panel per root.

The IS root is ~372k tiny parquet partitions; reading + enriching it costs
several minutes. Each root's klines and enriched features are therefore cached
as a single parquet on first use, so repeated WS scripts reload in seconds.

Cache invalidation is manual: delete ~/lm_build/cache/ if a root is rebuilt.
Only call load_* AFTER a root's build has completed, or a partial root will be
cached.
"""
from __future__ import annotations

import os
from pathlib import Path

import polars as pl

from liquidity_migration.reversion_alpha import ReversionConfig, compute_reversion_score
from liquidity_migration.volume_events import _enriched_event_features
from liquidity_migration.volume_features import build_volume_features
from liquidity_migration.storage import read_dataset

WINDOWS = {
    "is_train": ("~/SHARED_DATA/bybit_fullpit_1h", "2023-09-01", "2024-09-01"),
    "is_valid": ("~/SHARED_DATA/bybit_fullpit_1h", "2024-09-01", "2026-05-18"),
    "oos_bybit": ("~/SHARED_DATA/bybit_oos_pre2023", "2022-04-01", "2023-05-03"),
    "oos_binance": ("~/SHARED_DATA/binance_oos_pit", "2020-09-01", "2023-05-01"),
}

CACHE_DIR = Path(os.path.expanduser("~/lm_build/cache"))


def _root_key(root: str) -> str:
    return Path(os.path.expanduser(root)).name


def load_klines(root: str) -> pl.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{_root_key(root)}_klines.parquet"
    if cache.exists():
        return pl.read_parquet(cache)
    klines = read_dataset(root, "klines_1h")
    klines.write_parquet(cache)
    return klines


def load_enriched(root: str) -> pl.DataFrame:
    """Whole-root enriched feature panel (not window-filtered), cached."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{_root_key(root)}_enriched.parquet"
    if cache.exists():
        return pl.read_parquet(cache)
    klines = load_klines(root)
    manifest = read_dataset(root, "archive_trade_manifest")
    features = _enriched_event_features(
        build_volume_features(klines), klines, manifest,
        funding=read_dataset(root, "funding"),
        open_interest=read_dataset(root, "open_interest"),
        signed_flow_1h=pl.DataFrame(),
        mark_price_1h=read_dataset(root, "mark_price_1h"),
        index_price_1h=read_dataset(root, "index_price_1h"),
        premium_index_1h=read_dataset(root, "premium_index_1h"),
    )
    features.write_parquet(cache)
    return features


def load_features(window: str) -> tuple[pl.DataFrame, pl.DataFrame, str, str]:
    """Return (enriched_features_filtered_to_window, klines, start, end)."""
    root, start, end = WINDOWS[window]
    features = load_enriched(root)
    klines = load_klines(root)
    if start:
        features = features.filter(pl.col("date") >= start)
    if end:
        features = features.filter(pl.col("date") < end)
    return features, klines, start, end


def load_scored(window: str, config: ReversionConfig | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Return (scored_panel, klines) for a window."""
    root, start, end = WINDOWS[window]
    cfg = config or ReversionConfig(start=start, end=end)
    features, klines, _, _ = load_features(window)
    return compute_reversion_score(features, cfg), klines
