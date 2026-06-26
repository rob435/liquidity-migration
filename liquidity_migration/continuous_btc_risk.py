"""Causal BTC-risk entry-size overlay for the continuous demo book.

``CTRL_BTC_RISK_70_90_35`` sizes entries to 35% after warm-up when the causal
BTC-risk score is in ``[0.70, 0.90)``. The evidence improved MAR/drawdown on
both venues while cutting Binance total return, so keep the caveat local to the
decision log instead of spreading policy boilerplate through runtime code.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import polars as pl

from ._common import MS_PER_DAY
from .continuous_events import _btc_trend_returns

CTRL_BTC_RISK_70_90_35_ID = "CTRL_BTC_RISK_70_90_35"
BTC_RISK_MIN_PRIOR = 50
BTC_RISK_COMPONENTS = ("btc_trend_30d", "btc_return_7d", "btc_vol_30d", "btc_trend_delta_7d")
BTC_RISK_DIRECTIONS = {
    "btc_trend_30d": "low",
    "btc_return_7d": "low",
    "btc_vol_30d": "high",
    "btc_trend_delta_7d": "low",
}

STATE_SCHEMA = {
    "decision_key": pl.String,
    "symbol": pl.String,
    "signal_ts_ms": pl.Int64,
    "btc_trend_30d": pl.Float64,
    "btc_return_7d": pl.Float64,
    "btc_vol_30d": pl.Float64,
    "btc_trend_delta_7d": pl.Float64,
    "btc_risk_score": pl.Float64,
    "stack_mult": pl.Float64,
    "score_warmup": pl.Boolean,
}


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def percentile_from_prior(prior: list[float], value: float) -> float:
    if not prior:
        return 0.5
    return sum(1 for item in prior if item <= value) / len(prior)


def mean_present(values: Iterable[float | None], *, default: float = 0.5) -> float:
    present = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return sum(present) / len(present) if present else default


def _daily_closes_by_day(klines: pl.DataFrame) -> tuple[list[int], dict[int, float]]:
    daily_close: dict[int, float] = {}
    if klines.is_empty():
        return [], {}
    for row in klines.select(["ts_ms", "close"]).sort("ts_ms").iter_rows(named=True):
        day = (int(row["ts_ms"]) // MS_PER_DAY) * MS_PER_DAY
        daily_close[day] = float(row["close"])
    return sorted(daily_close), daily_close


def _std(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def _log_returns(days: list[int], daily_close: dict[int, float], *, start_idx: int, end_idx: int) -> list[float]:
    returns: list[float] = []
    for j in range(start_idx, end_idx):
        prev = daily_close[days[j - 1]]
        cur = daily_close[days[j]]
        if prev and cur:
            returns.append(math.log(cur / prev))
    return returns


def btc_context_by_day(klines: pl.DataFrame) -> dict[int, dict[str, float | None]]:
    """Build V0 BTC-risk context keyed by signal day, using prior BTC closes only."""

    trend30 = _btc_trend_returns(klines)
    days, daily_close = _daily_closes_by_day(klines)
    if not days:
        return {}
    by_day: dict[int, dict[str, float | None]] = {}
    for idx, day in enumerate(days):
        ret7 = None
        vol30 = None
        trend_delta7 = None
        if idx >= 8:
            prev = daily_close[days[idx - 1]]
            old = daily_close[days[idx - 8]]
            ret7 = (prev / old) - 1.0 if old else None
        if idx >= 31:
            vol30 = _std(_log_returns(days, daily_close, start_idx=idx - 30, end_idx=idx))
        prior_week_day = day - 7 * MS_PER_DAY
        if day in trend30 and prior_week_day in trend30:
            trend_delta7 = float(trend30[day]) - float(trend30[prior_week_day])
        by_day[day] = {
            "btc_trend_30d": _finite(trend30.get(day)),
            "btc_return_7d": _finite(ret7),
            "btc_vol_30d": _finite(vol30),
            "btc_trend_delta_7d": _finite(trend_delta7),
        }
    return by_day


class ExpandingBtcRiskState:
    """Causal expanding percentile state for the V0 BTC-risk score."""

    def __init__(self, *, min_prior: int = BTC_RISK_MIN_PRIOR) -> None:
        self.min_prior = int(min_prior)
        self.raw_history: dict[str, list[float]] = {name: [] for name in BTC_RISK_COMPONENTS}
        self.decision_count = 0
        self.seen_keys: set[str] = set()

    def score(self, *, decision_key: str, raw_values: dict[str, float | None]) -> dict[str, Any]:
        if decision_key in self.seen_keys:
            raise ValueError(f"duplicate BTC-risk decision key: {decision_key}")
        prior_count = self.decision_count
        percentiles: dict[str, float | None] = {}
        parts: list[float | None] = []
        for name in BTC_RISK_COMPONENTS:
            value = raw_values.get(name)
            pct = None if value is None else percentile_from_prior(self.raw_history.setdefault(name, []), float(value))
            percentiles[f"{name}_pct"] = pct
            direction = BTC_RISK_DIRECTIONS[name]
            parts.append(None if pct is None else (1.0 - pct if direction == "low" else pct))
        risk_score = mean_present(parts)
        for name in BTC_RISK_COMPONENTS:
            value = raw_values.get(name)
            if value is not None:
                self.raw_history.setdefault(name, []).append(float(value))
        self.seen_keys.add(decision_key)
        self.decision_count += 1
        return {
            "prior_decision_count": prior_count,
            "score_warmup": prior_count < self.min_prior,
            **percentiles,
            "btc_risk_score": risk_score,
            "score_component_count": sum(1 for value in parts if value is not None),
            "score_missing_component_count": sum(1 for value in parts if value is None),
        }


class BtcRiskLiveSizer:
    """Persistent live state for ``CTRL_BTC_RISK_70_90_35``."""

    def __init__(
        self,
        state_path: str | Path,
        *,
        low: float = 0.70,
        high: float = 0.90,
        tail_mult: float = 0.35,
        min_prior: int = BTC_RISK_MIN_PRIOR,
    ) -> None:
        self.state_path = Path(state_path)
        self.low = float(low)
        self.high = float(high)
        self.tail_mult = float(tail_mult)
        self.state = ExpandingBtcRiskState(min_prior=min_prior)
        self._rows: list[dict[str, Any]] = []
        self._dirty = False
        self.load()

    @property
    def rows(self) -> int:
        return len(self._rows)

    def load(self) -> None:
        if not self.state_path.exists():
            return
        df = pl.read_parquet(self.state_path)
        if df.is_empty():
            return
        rows = sorted(df.to_dicts(), key=lambda row: (int(row["signal_ts_ms"]), str(row["symbol"])))
        self._rows = []
        self.state = ExpandingBtcRiskState(min_prior=self.state.min_prior)
        for row in rows:
            raw_values = {name: _finite(row.get(name)) for name in BTC_RISK_COMPONENTS}
            self.state.score(decision_key=str(row["decision_key"]), raw_values=raw_values)
            self._rows.append(dict(row))

    def score_decisions(
        self,
        decisions: list[dict[str, Any]],
        *,
        btc_context: dict[int, dict[str, float | None]],
    ) -> tuple[dict[tuple[str, int], dict[str, Any]], dict[str, Any]]:
        """Score unique final candidate decisions and return lookup + cycle stats."""

        lookup: dict[tuple[str, int], dict[str, Any]] = {}
        unique: dict[tuple[str, int], dict[str, Any]] = {}
        for row in decisions:
            symbol = str(row.get("symbol") or "")
            signal_ts = int(row.get("signal_ts_ms") or row.get("entry_signal_ts_ms") or 0)
            if symbol and signal_ts > 0:
                unique.setdefault((symbol, signal_ts), row)
        scored = 0
        tail_selected = 0
        warmup = 0
        duplicates = 0
        for symbol, signal_ts in sorted(unique):
            decision_key = f"{symbol}|{signal_ts}"
            if decision_key in self.state.seen_keys:
                existing = next((row for row in self._rows if row.get("decision_key") == decision_key), None)
                if existing is not None:
                    lookup[(symbol, signal_ts)] = dict(existing)
                duplicates += 1
                continue
            day = (signal_ts // MS_PER_DAY) * MS_PER_DAY
            raw_values = {name: _finite((btc_context.get(day) or {}).get(name)) for name in BTC_RISK_COMPONENTS}
            score = self.state.score(decision_key=decision_key, raw_values=raw_values)
            risk_score = float(score["btc_risk_score"])
            is_warmup = bool(score["score_warmup"])
            selected = (not is_warmup) and self.low <= risk_score < self.high
            mult = self.tail_mult if selected else 1.0
            out = {
                "decision_key": decision_key,
                "symbol": symbol,
                "signal_ts_ms": signal_ts,
                **raw_values,
                **score,
                "stack_mult": mult,
                "tail_selected": selected,
            }
            self._rows.append(out)
            lookup[(symbol, signal_ts)] = out
            self._dirty = True
            scored += 1
            tail_selected += int(selected)
            warmup += int(is_warmup)
        return lookup, {
            "scored": scored,
            "duplicates": duplicates,
            "tail_selected": tail_selected,
            "warmup": warmup,
            "state_rows": len(self._rows),
        }

    def save(self) -> None:
        if not self._dirty:
            return
        rows = [
            {name: row.get(name) for name in STATE_SCHEMA}
            for row in sorted(self._rows, key=lambda item: (int(item["signal_ts_ms"]), str(item["symbol"])))
        ]
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(rows, schema=STATE_SCHEMA).write_parquet(self.state_path)
        self._dirty = False
