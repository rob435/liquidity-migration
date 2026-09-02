"""Per-trade exit overlays on a recorded ledger, scored against a matched random-exit placebo.

Each recorded trade is replayed on its own bars from the fill: the recorded stop
geometry stays, and the exit rule under test supplies a different hard clock
(the bar end at which the trade leaves at the close if the stop has not fired).
The variant's net is priced the way the ledger prices trades: weight times
(gross return, less the round-trip cost, plus funding). The delta against the
recorded exit is summed, split by exit year, and given a paired t over the
trades the rule changed: a different exit bar, or the same bar at a different
price (a stop turned into a close).

The placebo deals the same number of exits at random. With ``candidates`` it
picks that many trades among those with a candidate exit and gives each a
random candidate (the state-exit program's placebo); without, it deals the
rule's own hard-clock horizons to random trades (the horizon program's). The
share of draws scoring at least the real delta is what a cell has to beat.

Trades are long unless the ledger has a ``side`` column. Required columns:
``symbol, entry_ts_ms, exit_ts_ms``; ``entry_price`` and ``exit_price`` are
taken from the bars when absent; ``net_return`` lets the harness report how
far its own pricing of the recorded exits sits from the ledger; ``stop_price``
enables the stop replay; ``planned_exit_ts_ms`` is the recorded hard clock;
``notional_weight`` defaults to 1; ``entry_signal_ts_ms`` anchors the daily
decision stamps and defaults to the fill.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import polars as pl

from liquidity_migration.core._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.data.trade_lifecycle import _funding_lookup, _perp_funding_return
from liquidity_migration.research.lab.backtest import years_of
from liquidity_migration.research.lab.plateau import Arm

Trade = Mapping[str, Any]
#: symbol, side, entry_ts_ms, exit_ts_ms -> signed funding return to the position
FundingReturn = Callable[[str, str, int, int], float]
#: The alternative hard-exit clock for one trade, or None to keep the recorded exit.
ExitRule = Callable[[Trade, "Bars"], int | None]
#: The hard-exit clocks the placebo may deal one trade.
Candidates = Callable[[Trade, "Bars"], Sequence[int]]


@dataclass(frozen=True)
class Bars:
    """One symbol's bars, indexed by bar end."""

    ends: list[int]
    close: np.ndarray
    low: np.ndarray

    def close_at(self, end_ts: int) -> float | None:
        i = bisect_left(self.ends, end_ts)
        return float(self.close[i]) if i < len(self.ends) and self.ends[i] == end_ts else None


def bars_by_symbol(prices: pl.DataFrame, *, bar_ms: int = MS_PER_HOUR) -> dict[str, Bars]:
    """Split a ``ts_ms, symbol, close[, low]`` frame into per-symbol bars; ``ts_ms`` is the bar open."""
    out: dict[str, Bars] = {}
    for key, part in prices.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = key[0] if isinstance(key, tuple) else key
        low = part["low"] if "low" in part.columns else part["close"]
        out[str(symbol)] = Bars(
            ends=[int(v) for v in (part["ts_ms"] + bar_ms).to_list()],
            close=part["close"].to_numpy().astype(float),
            low=low.to_numpy().astype(float),
        )
    return out


@dataclass(frozen=True)
class StopGeometry:
    """The recorded stop: the trade's ``stop_price``, its distance scaled by ``decay_factor`` once ``decay_after_ms`` have passed since the fill."""

    decay_after_ms: int = 48 * MS_PER_HOUR
    decay_factor: float = 0.5


def simulate_exit(
    trade: Trade, bars: Bars, hard_exit_ts: int, geometry: StopGeometry | None = None
) -> tuple[int, float, str]:
    """Walk the bars after the fill: stop on the low, else leave at the close of the bar ending at or after the hard clock.

    Returns (exit bar end, exit price, reason) with reason one of ``time_stop``,
    ``stop_loss``, ``decayed_stop_loss``, ``data_end``.
    """
    entry_ts, entry_px = int(trade["entry_ts_ms"]), float(trade["entry_price"])
    stop_frac: float | None = None
    decay_at = entry_ts
    decay_factor = 1.0
    if geometry is not None and trade.get("stop_price") is not None:
        stop_frac = 1.0 - float(trade["stop_price"]) / entry_px
        decay_at = entry_ts + geometry.decay_after_ms
        decay_factor = geometry.decay_factor
    i = bisect_right(bars.ends, entry_ts)
    while i < len(bars.ends):
        end = int(bars.ends[i])
        if end >= hard_exit_ts:
            return end, float(bars.close[i]), "time_stop"
        if stop_frac is not None:
            decayed = end >= decay_at
            stop = stop_frac * decay_factor if decayed else stop_frac
            if min(float(bars.close[i]), float(bars.low[i])) <= entry_px * (1.0 - stop):
                return end, entry_px * (1.0 - stop), "decayed_stop_loss" if decayed else "stop_loss"
        i += 1
    return int(bars.ends[-1]), float(bars.close[-1]), "data_end"


@dataclass(frozen=True)
class Pricing:
    """weight x (gross return - round-trip cost + funding), the ledger's own accounting."""

    round_trip_cost_bps: float
    funding_return: FundingReturn | None = None

    def net(self, trade: Trade, exit_ts: int, exit_px: float) -> float:
        side = str(trade.get("side") or "long")
        gross = exit_px / float(trade["entry_price"]) - 1.0
        if side == "short":
            gross = -gross
        fund = 0.0
        if self.funding_return is not None:
            fund = self.funding_return(str(trade["symbol"]), side, int(trade["entry_ts_ms"]), int(exit_ts))
        weight = trade.get("notional_weight")
        return float(1.0 if weight is None else weight) * (gross - self.round_trip_cost_bps / 1e4 + fund)


def settlement_funding(funding: pl.DataFrame) -> FundingReturn:
    """Settlement-exact funding from a ``ts_ms, symbol, funding_rate`` frame: a long pays the rates in (entry, exit]."""
    lookup = _funding_lookup(funding)

    def signed_return(symbol: str, side: str, entry_ts_ms: int, exit_ts_ms: int) -> float:
        value, _, _ = _perp_funding_return(
            lookup, symbol=symbol, side=side, entry_ts_ms=entry_ts_ms, exit_ts_ms=exit_ts_ms
        )
        return float(value)

    return signed_return


def alive_daily_stamps(trade: Trade, *, max_days: int = 4, bar_ms: int = MS_PER_HOUR) -> list[int]:
    """Daily decision stamps (signal close + k days, k = 1..max_days) at which the trade is alive and an exit at the next bar lands before the recorded exit."""
    signal = trade.get("entry_signal_ts_ms")
    sig = int(trade["entry_ts_ms"] if signal is None else signal)
    entry, exit_ts = int(trade["entry_ts_ms"]), int(trade["exit_ts_ms"])
    out: list[int] = []
    for k in range(1, max_days + 1):
        s = sig + k * MS_PER_DAY
        if s <= entry:
            continue
        if s + bar_ms >= exit_ts:
            break
        out.append(s)
    return out


def state_exit_rule(
    state: Callable[[Trade, int], bool], *, max_days: int = 4, bar_ms: int = MS_PER_HOUR
) -> ExitRule:
    """Leave at the bar after the first alive daily stamp where ``state(trade, stamp)`` holds."""

    def rule(trade: Trade, bars: Bars) -> int | None:
        hit = next((s for s in alive_daily_stamps(trade, max_days=max_days, bar_ms=bar_ms) if state(trade, s)), None)
        return None if hit is None else hit + bar_ms

    return rule


def alive_exit_candidates(*, max_days: int = 4, bar_ms: int = MS_PER_HOUR) -> Candidates:
    """The placebo's menu for a trade: the bar after each alive daily stamp."""

    def candidates(trade: Trade, bars: Bars) -> list[int]:
        return [s + bar_ms for s in alive_daily_stamps(trade, max_days=max_days, bar_ms=bar_ms)]

    return candidates


def hard_clock(hours: float) -> ExitRule:
    """Leave ``hours`` after the fill."""

    def rule(trade: Trade, bars: Bars) -> int:
        return int(trade["entry_ts_ms"]) + int(hours * MS_PER_HOUR)

    return rule


@dataclass
class OverlayResult:
    name: str
    per_trade: pl.DataFrame
    total_delta: float
    n_changed: int
    mean_delta_changed_bp: float
    t_changed: float
    by_year: dict[int, float]
    n_worse_years: int
    placebo_deltas: np.ndarray
    ledger_max_abs_diff: float

    @property
    def placebo_mean(self) -> float:
        return float(self.placebo_deltas.mean()) if len(self.placebo_deltas) else math.nan

    @property
    def placebo_p90(self) -> float:
        return float(np.quantile(self.placebo_deltas, 0.9)) if len(self.placebo_deltas) else math.nan

    @property
    def share_placebo_beating_real(self) -> float:
        if not len(self.placebo_deltas):
            return math.nan
        return float((self.placebo_deltas >= self.total_delta).mean())

    @property
    def arm(self) -> Arm:
        return Arm(delta=self.total_delta, placebo_share=self.share_placebo_beating_real)

    def summary(self) -> dict[str, Any]:
        return dict(
            variant=self.name, n_changed=self.n_changed, total_delta=round(self.total_delta, 4),
            mean_delta_changed_bp=round(self.mean_delta_changed_bp, 2), t_changed=round(self.t_changed, 2),
            by_year={y: round(v, 4) for y, v in self.by_year.items()}, n_worse_years=self.n_worse_years,
            placebo_mean=round(self.placebo_mean, 4), placebo_p90=round(self.placebo_p90, 4),
            share_placebo_beating_real=round(self.share_placebo_beating_real, 3),
            ledger_max_abs_diff=self.ledger_max_abs_diff,
        )


def _paired_t(d: np.ndarray) -> float:
    if len(d) <= 2:
        return math.nan
    sd = float(d.std(ddof=1))
    return float(d.mean() / (sd / math.sqrt(len(d)))) if sd > 0 else math.nan


def _filled_rows(trades: pl.DataFrame, bars: Mapping[str, Bars]) -> list[dict[str, Any]]:
    rows = trades.sort(["entry_ts_ms", "symbol"]).to_dicts()
    for t in rows:
        b = bars[str(t["symbol"])]
        if t.get("entry_price") is None:
            t["entry_price"] = b.close_at(int(t["entry_ts_ms"]))
        if t.get("exit_price") is None:
            t["exit_price"] = b.close_at(int(t["exit_ts_ms"]))
        if t["entry_price"] is None or t["exit_price"] is None:
            raise ValueError(f"{t['symbol']}: no bar closes at the recorded entry or exit stamp")
    return rows


def check_reproduction(
    trades: pl.DataFrame,
    bars: Mapping[str, Bars],
    *,
    pricing: Pricing,
    geometry: StopGeometry | None = None,
) -> tuple[int, float]:
    """Replay every trade to its recorded hard clock: (exits that differ from the ledger, max |priced net - net_return|)."""
    rows = _filled_rows(trades, bars)
    mismatches = 0
    worst = 0.0
    for t in rows:
        planned = t.get("planned_exit_ts_ms")
        hard = int(t["exit_ts_ms"] if planned is None else planned)
        ets, epx, reason = simulate_exit(t, bars[str(t["symbol"])], hard, geometry)
        recorded_reason = t.get("exit_reason")
        if (
            ets != int(t["exit_ts_ms"])
            or abs(epx - float(t["exit_price"])) > 1e-9
            or (recorded_reason is not None and reason != recorded_reason)
        ):
            mismatches += 1
        if t.get("net_return") is not None:
            worst = max(worst, abs(pricing.net(t, ets, epx) - float(t["net_return"])))
    return mismatches, worst


def evaluate_overlay(
    trades: pl.DataFrame,
    bars: Mapping[str, Bars],
    rule: ExitRule,
    *,
    pricing: Pricing,
    geometry: StopGeometry | None = None,
    candidates: Candidates | None = None,
    draws: int = 200,
    seed: int = 20260902,
    name: str = "overlay",
) -> OverlayResult:
    rows = _filled_rows(trades, bars)
    n = len(rows)
    entry = np.array([int(t["entry_ts_ms"]) for t in rows], dtype="int64")
    base_exit = np.array([int(t["exit_ts_ms"]) for t in rows], dtype="int64")
    base_net = np.array([pricing.net(t, int(t["exit_ts_ms"]), float(t["exit_price"])) for t in rows])
    ledger_diff = 0.0
    if n and "net_return" in trades.columns:
        ledger = np.array([float(t["net_return"]) for t in rows])
        ledger_diff = float(np.max(np.abs(base_net - ledger)))

    variant_exit = base_exit.copy()
    variant_net = base_net.copy()
    hard = np.full(n, -1, dtype="int64")
    reasons = ["recorded"] * n
    for k, t in enumerate(rows):
        b = bars[str(t["symbol"])]
        clock = rule(t, b)
        if clock is None:
            continue
        ets, epx, reason = simulate_exit(t, b, int(clock), geometry)
        variant_exit[k], variant_net[k], hard[k], reasons[k] = ets, pricing.net(t, ets, epx), int(clock), reason
    delta = variant_net - base_net
    changed = (variant_exit != base_exit) | (delta != 0)
    n_changed = int(changed.sum())
    years = years_of(base_exit) if n else np.array([], dtype=int)
    by_year = {int(y): float(delta[years == y].sum()) for y in sorted(set(years.tolist()))}

    rng = np.random.default_rng(seed)
    placebo = np.zeros(max(draws, 0))
    if n_changed and draws > 0:
        if candidates is None:
            horizons = hard[changed] - entry[changed]
            for d in range(draws):
                pick = rng.choice(n, size=n_changed, replace=False)
                dealt = rng.permutation(horizons)
                total = 0.0
                for k, h in zip(pick.tolist(), dealt.tolist()):
                    t = rows[k]
                    ets, epx, _ = simulate_exit(t, bars[str(t["symbol"])], int(entry[k]) + int(h), geometry)
                    total += pricing.net(t, ets, epx) - base_net[k]
                placebo[d] = total
        else:
            menus = [list(candidates(t, bars[str(t["symbol"])])) for t in rows]
            eligible = np.array([k for k, menu in enumerate(menus) if menu], dtype="int64")
            size = min(n_changed, len(eligible))
            for d in range(draws):
                pick = rng.choice(eligible, size=size, replace=False) if size else np.array([], dtype="int64")
                total = 0.0
                for k in pick.tolist():
                    t = rows[k]
                    menu = menus[k]
                    clock = menu[int(rng.integers(len(menu)))]
                    ets, epx, _ = simulate_exit(t, bars[str(t["symbol"])], int(clock), geometry)
                    total += pricing.net(t, ets, epx) - base_net[k]
                placebo[d] = total

    per_trade = pl.DataFrame(
        dict(
            symbol=[str(t["symbol"]) for t in rows],
            entry_ts_ms=entry.tolist(),
            exit_ts_ms=base_exit.tolist(),
            variant_exit_ts_ms=variant_exit.tolist(),
            hard_exit_ts_ms=[None if h < 0 else int(h) for h in hard.tolist()],
            exit_reason=reasons,
            base_net=base_net.tolist(),
            variant_net=variant_net.tolist(),
            delta=delta.tolist(),
            changed=changed.tolist(),
        ),
        schema_overrides={"hard_exit_ts_ms": pl.Int64},
    )
    return OverlayResult(
        name=name,
        per_trade=per_trade,
        total_delta=float(delta.sum()),
        n_changed=n_changed,
        mean_delta_changed_bp=float(delta[changed].mean() * 1e4) if n_changed else 0.0,
        t_changed=_paired_t(delta[changed]),
        by_year=by_year,
        n_worse_years=sum(v < 0 for v in by_year.values()),
        placebo_deltas=placebo,
        ledger_max_abs_diff=ledger_diff,
    )
