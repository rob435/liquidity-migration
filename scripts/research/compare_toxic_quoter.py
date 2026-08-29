#!/usr/bin/env python3
"""Paired conservative queue replay for the quoter's directional flow response.

Every arm starts from the same two-sided opportunity. Displayed queue is
charged ahead of the virtual order; only public trades advance it. The replay
includes maker fees and a 15-second mark after a fill. It does not model the
inventory created by one fill, so it grades quote selection, not a complete
market-making book.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from liquidity_migration.research.execution.quote_lab.book import BookMirror  # noqa: E402

try:
    import orjson

    def loads(raw: bytes) -> dict[str, Any]:
        return orjson.loads(raw)

except ImportError:

    def loads(raw: bytes) -> dict[str, Any]:
        return json.loads(raw)


NS = 1_000_000_000
HALF_SPREAD_BP = 6.5
REQUOTE_BP = 2.5
SIGNAL_HALF_LIFE_NS = 250_000_000
FLOW_FAST_HALF_LIFE_NS = 250_000_000
FLOW_SLOW_HALF_LIFE_NS = 3_000_000_000
FLOW_FAST_WEIGHT = 0.65
FLOW_SLOW_WEIGHT = 0.35
FLOW_DEPTH_BP = 10.0
FLOW_VOL_DEPTH_MULTIPLIER = 2.0
FLOW_MAX_SCORE = 4.0
MAX_MARK_DELAY_NS = 2 * NS


@dataclass(frozen=True, slots=True)
class Arm:
    name: str
    maker_fee_bp: float
    min_edge_bp: float = 4.0
    volatility_multiplier: float = 2.0
    toxicity_bp: float = 0.0
    book_lean_bp: float = 1.5
    trade_lean_bp: float = 0.0
    flow_response_bp: float = 0.0
    flow_max_widen_bp: float = 8.0
    flow_pull_score: float | None = None


ARMS = (
    Arm(
        "current",
        maker_fee_bp=2.0,
        toxicity_bp=4.0,
        trade_lean_bp=1.5,
    ),
    Arm("fee_corrected", maker_fee_bp=4.0),
    Arm("directional_w1", maker_fee_bp=4.0, flow_response_bp=1.0),
    Arm("directional_w2", maker_fee_bp=4.0, flow_response_bp=2.0),
    Arm("directional_w4", maker_fee_bp=4.0, flow_response_bp=4.0),
    Arm(
        "directional_w2_pull1",
        maker_fee_bp=4.0,
        flow_response_bp=2.0,
        flow_pull_score=1.0,
    ),
    Arm(
        "directional_w2_pull2",
        maker_fee_bp=4.0,
        flow_response_bp=2.0,
        flow_pull_score=2.0,
    ),
)


def decay(value: float, elapsed_ns: int, half_life_ns: int) -> float:
    return value * math.exp(-math.log(2.0) * elapsed_ns / half_life_ns)


@dataclass(slots=True)
class FlowState:
    last_ns: int = 0
    flow_last_ns: int = 0
    var_mid: float = 0.0
    microprice: float = 0.0
    variance: float = 0.0
    book_imbalance: float = 0.0
    trade_imbalance: float = 0.0
    flow_fast: float = 0.0
    flow_slow: float = 0.0
    bid_depth_usdt: float = 0.0
    ask_depth_usdt: float = 0.0

    def advance(self, now_ns: int) -> float:
        if self.last_ns == 0:
            self.last_ns = now_ns
            legacy_decay = 0.0
        else:
            now_ns = max(now_ns, self.last_ns)
            elapsed = now_ns - self.last_ns
            legacy_decay = decay(1.0, elapsed, SIGNAL_HALF_LIFE_NS)
            self.variance *= legacy_decay
            self.trade_imbalance *= legacy_decay
            self.last_ns = now_ns
        if self.flow_last_ns == 0:
            self.flow_last_ns = now_ns
        else:
            flow_now = max(now_ns, self.flow_last_ns)
            elapsed = flow_now - self.flow_last_ns
            self.flow_fast = decay(self.flow_fast, elapsed, FLOW_FAST_HALF_LIFE_NS)
            self.flow_slow = decay(self.flow_slow, elapsed, FLOW_SLOW_HALF_LIFE_NS)
            self.flow_last_ns = flow_now
        return legacy_decay

    def on_book(self, mirror: BookMirror, symbol: str, now_ns: int) -> None:
        if not mirror.healthy(symbol):
            return
        bid = mirror.best_bid(symbol)
        ask = mirror.best_ask(symbol)
        if bid is None or ask is None or bid <= 0.0 or ask < bid:
            return
        weight = self.advance(now_ns)
        mid = 0.5 * (bid + ask)
        if self.var_mid > 0.0:
            change = math.log(mid / self.var_mid)
            self.variance += max(1.0 - weight, 0.01) * change * change
        bids = mirror.levels(symbol, "Buy")
        asks = mirror.levels(symbol, "Sell")
        bid_weight = sum(qty / (index + 1) for index, (_, qty) in enumerate(bids))
        ask_weight = sum(qty / (index + 1) for index, (_, qty) in enumerate(asks))
        total_weight = bid_weight + ask_weight
        self.book_imbalance = (
            (bid_weight - ask_weight) / total_weight if total_weight > 0.0 else 0.0
        )
        top_qty = bids[0][1] + asks[0][1]
        self.microprice = (
            (ask * bids[0][1] + bid * asks[0][1]) / top_qty if top_qty > 0.0 else mid
        )
        self.var_mid = mid
        volatility_bp = math.sqrt(max(self.variance, 0.0)) * 10_000.0
        band_bp = min(100.0, FLOW_DEPTH_BP + FLOW_VOL_DEPTH_MULTIPLIER * volatility_bp)
        band = band_bp / 10_000.0
        self.bid_depth_usdt = sum(
            px * qty
            for index, (px, qty) in enumerate(bids)
            if index == 0 or px >= mid * (1.0 - band)
        )
        self.ask_depth_usdt = sum(
            px * qty
            for index, (px, qty) in enumerate(asks)
            if index == 0 or px <= mid * (1.0 + band)
        )

    def on_trade(self, price: float, qty: float, side: str, now_ns: int) -> None:
        weight = self.advance(now_ns)
        observed = 1.0 if side == "Buy" else -1.0
        self.trade_imbalance += max(1.0 - weight, 0.05) * (
            observed - self.trade_imbalance
        )
        if price <= 0.0 or qty <= 0.0:
            return
        if side == "Buy" and self.ask_depth_usdt > 0.0:
            shock = price * qty / self.ask_depth_usdt
        elif side == "Sell" and self.bid_depth_usdt > 0.0:
            shock = -price * qty / self.bid_depth_usdt
        else:
            return
        shock = max(-FLOW_MAX_SCORE, min(FLOW_MAX_SCORE, shock))
        self.flow_fast = max(-FLOW_MAX_SCORE, min(FLOW_MAX_SCORE, self.flow_fast + shock))
        self.flow_slow = max(-FLOW_MAX_SCORE, min(FLOW_MAX_SCORE, self.flow_slow + shock))

    @property
    def score(self) -> float:
        score = (
            FLOW_FAST_WEIGHT * self.flow_fast + FLOW_SLOW_WEIGHT * self.flow_slow
        ) / (FLOW_FAST_WEIGHT + FLOW_SLOW_WEIGHT)
        return max(-FLOW_MAX_SCORE, min(FLOW_MAX_SCORE, score))

    @property
    def volatility_bp(self) -> float:
        return math.sqrt(max(self.variance, 0.0)) * 10_000.0


def snap_price(price: float, tick: float, side: str) -> float:
    steps = price / tick
    snapped = math.floor(steps + 1e-10) * tick if side == "Buy" else math.ceil(steps - 1e-10) * tick
    return round(snapped, 12)


def desired_price(
    arm: Arm,
    state: FlowState,
    bid: float,
    ask: float,
    tick: float,
    side: str,
) -> float | None:
    score = state.score
    if arm.flow_pull_score is not None:
        attacked = (side == "Sell" and score >= arm.flow_pull_score) or (
            side == "Buy" and score <= -arm.flow_pull_score
        )
        if attacked:
            return None
    mid = 0.5 * (bid + ask)
    fair = state.microprice or mid
    fair += mid * (
        arm.book_lean_bp * state.book_imbalance
        + arm.trade_lean_bp * state.trade_imbalance
    ) / 10_000.0
    half_spread_bp = max(
        HALF_SPREAD_BP,
        arm.maker_fee_bp
        + arm.min_edge_bp
        + arm.volatility_multiplier * state.volatility_bp
        + arm.toxicity_bp * abs(state.trade_imbalance),
    )
    extra_bp = min(arm.flow_max_widen_bp, arm.flow_response_bp * abs(score))
    if side == "Buy" and score < 0.0:
        half_spread_bp += extra_bp
    if side == "Sell" and score > 0.0:
        half_spread_bp += extra_bp
    wanted = fair * (1.0 - half_spread_bp / 10_000.0) if side == "Buy" else fair * (
        1.0 + half_spread_bp / 10_000.0
    )
    wanted = min(wanted, ask - tick) if side == "Buy" else max(wanted, bid + tick)
    return snap_price(wanted, tick, side)


@dataclass(slots=True)
class VirtualQuote:
    side: str
    px: float | None = None
    queue: float = 0.0
    filled: bool = False

    def reprice(self, wanted: float | None, mirror: BookMirror, symbol: str, mid: float) -> None:
        if wanted is None:
            self.px = None
            self.queue = 0.0
            return
        if self.px is not None and abs(self.px - wanted) / mid * 10_000.0 <= REQUOTE_BP:
            return
        self.px = wanted
        self.queue = mirror.depth_at(symbol, self.side, wanted)

    def on_book(self, mirror: BookMirror, symbol: str) -> bool:
        if self.px is None or self.filled or not mirror.healthy(symbol):
            return False
        bid = mirror.best_bid(symbol)
        ask = mirror.best_ask(symbol)
        assert bid is not None and ask is not None
        crossed = ask <= self.px if self.side == "Buy" else bid >= self.px
        if crossed:
            self.filled = True
            return True
        self.queue = min(self.queue, mirror.depth_at(symbol, self.side, self.px))
        return False

    def on_trade(self, price: float, qty: float, aggressor: str, tick: float) -> bool:
        if self.px is None or self.filled:
            return False
        consuming = aggressor == ("Sell" if self.side == "Buy" else "Buy")
        if not consuming:
            return False
        through = price < self.px - 0.5 * tick if self.side == "Buy" else price > self.px + 0.5 * tick
        at_level = abs(price - self.px) <= 0.5 * tick
        if through:
            self.filled = True
            return True
        if at_level:
            self.queue = max(0.0, self.queue - qty)
            if self.queue <= 1e-12:
                self.filled = True
                return True
        return False


@dataclass(slots=True)
class Opportunity:
    index: int
    started_ns: int
    quotes: dict[tuple[str, str], VirtualQuote] = field(default_factory=dict)


@dataclass(slots=True)
class PendingMark:
    key: tuple[int, str, str]
    side: str
    fill_px: float
    due_ns: int
    fee_bp: float


@dataclass(slots=True)
class Simulation:
    values: dict[tuple[int, str, str], float] = field(default_factory=dict)
    filled: set[tuple[int, str, str]] = field(default_factory=set)
    pending: list[PendingMark] = field(default_factory=list)
    opportunities: int = 0
    unmarked: set[tuple[int, str, str]] = field(default_factory=set)


def finish_opportunity(opportunity: Opportunity | None, result: Simulation) -> None:
    if opportunity is None:
        return
    for (arm, side), quote in opportunity.quotes.items():
        key = (opportunity.index, side, arm)
        if not quote.filled:
            result.values[key] = 0.0


def simulate(
    records: Iterator[dict[str, Any]],
    symbol: str,
    tick: float,
    attempt_ns: int,
    markout_ns: int,
) -> Simulation:
    mirror = BookMirror()
    state = FlowState()
    result = Simulation()
    opportunity: Opportunity | None = None
    next_start_ns = 0
    index = 0

    for row in records:
        kind = str(row.get("kind") or "")
        if kind not in {"orderbook_snapshot", "orderbook_delta", "public_trade"}:
            continue
        ts = int(row.get("local_receive_ts_ns") or 0)
        if ts <= 0:
            continue
        if opportunity is not None and ts >= opportunity.started_ns + attempt_ns:
            finish_opportunity(opportunity, result)
            opportunity = None

        price = float(row.get("price") or 0.0)
        qty = float(row.get("qty") or 0.0)
        aggressor = str(row.get("side") or "")
        valid_trade = (
            kind == "public_trade"
            and price > 0.0
            and qty > 0.0
            and aggressor in {"Buy", "Sell"}
        )

        mirror.apply(row)
        # The event happened before it became visible. An order already in
        # the book can fill on it; the signal derived from this event may
        # protect only against what comes next.
        if opportunity is not None:
            for arm in ARMS:
                for side in ("Buy", "Sell"):
                    quote = opportunity.quotes[(arm.name, side)]
                    if quote.filled or quote.px is None:
                        continue
                    filled = (
                        quote.on_trade(price, qty, aggressor, tick)
                        if valid_trade
                        else quote.on_book(mirror, symbol)
                    )
                    if not filled:
                        continue
                    key = (opportunity.index, side, arm.name)
                    result.filled.add(key)
                    result.pending.append(
                        PendingMark(
                            key=key,
                            side=side,
                            fill_px=quote.px,
                            due_ns=ts + markout_ns,
                            fee_bp=arm.maker_fee_bp,
                        )
                    )

        if kind == "public_trade":
            if valid_trade:
                state.on_trade(price, qty, aggressor, ts)
        else:
            state.on_book(mirror, symbol, ts)

        if mirror.healthy(symbol):
            bid = mirror.best_bid(symbol)
            ask = mirror.best_ask(symbol)
            assert bid is not None and ask is not None
            mid = 0.5 * (bid + ask)
            if kind != "public_trade":
                for pending in list(result.pending):
                    if ts < pending.due_ns:
                        continue
                    result.pending.remove(pending)
                    if ts - pending.due_ns > MAX_MARK_DELAY_NS:
                        result.unmarked.add(pending.key)
                        continue
                    direction = 1.0 if pending.side == "Buy" else -1.0
                    pnl_bp = direction * (mid - pending.fill_px) / pending.fill_px * 10_000.0
                    result.values[pending.key] = pnl_bp - pending.fee_bp

            if opportunity is None and ts >= next_start_ns:
                index += 1
                opportunity = Opportunity(index=index, started_ns=ts)
                result.opportunities += 1
                next_start_ns = ts + attempt_ns
                for arm in ARMS:
                    for side in ("Buy", "Sell"):
                        quote = VirtualQuote(side=side)
                        quote.reprice(desired_price(arm, state, bid, ask, tick, side), mirror, symbol, mid)
                        opportunity.quotes[(arm.name, side)] = quote

            if opportunity is not None:
                for arm in ARMS:
                    for side in ("Buy", "Sell"):
                        quote = opportunity.quotes[(arm.name, side)]
                        if quote.filled:
                            continue
                        quote.reprice(desired_price(arm, state, bid, ask, tick, side), mirror, symbol, mid)

    finish_opportunity(opportunity, result)
    result.unmarked.update(pending.key for pending in result.pending)
    return result


def lines(path: Path) -> Iterator[dict[str, Any]]:
    if path.suffix != ".zst":
        with path.open("rb") as handle:
            for raw in handle:
                if raw.strip():
                    yield loads(raw)
        return
    with subprocess.Popen(["zstd", "-dcq", "--", str(path)], stdout=subprocess.PIPE) as process:
        assert process.stdout is not None
        for raw in process.stdout:
            if raw.strip():
                yield loads(raw)
        code = process.wait()
        if code != 0:
            raise RuntimeError(f"zstd failed for {path} with exit {code}")


def records(paths: list[Path], max_records: int) -> Iterator[dict[str, Any]]:
    seen = 0
    started = False
    for path in paths:
        for row in lines(path):
            if not started:
                if row.get("kind") != "orderbook_snapshot":
                    continue
                started = True
            yield row
            seen += 1
            if max_records > 0 and seen >= max_records:
                return


def segments(root: Path, date: str, symbol: str) -> list[Path]:
    directory = root / date / symbol
    return sorted((*directory.glob("segment-*.jsonl"), *directory.glob("segment-*.jsonl.zst")))


def available_symbols(root: Path, dates: list[str]) -> list[str]:
    found: set[str] = set()
    for date in dates:
        directory = root / date
        if directory.is_dir():
            found.update(child.name.upper() for child in directory.iterdir() if child.is_dir())
    return sorted(found)


def t_stat(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    spread = statistics.stdev(values)
    return None if spread == 0.0 else statistics.mean(values) / (spread / math.sqrt(len(values)))


def summarize(results: list[Simulation], arm: Arm, control: str = "fee_corrected") -> dict[str, Any]:
    values: list[float] = []
    diffs: list[float] = []
    fills = 0
    control_fills = 0
    avoided_loss = 0.0
    forgone_gain = 0.0
    pairs = 0
    opportunities = sum(result.opportunities * 2 for result in results)
    unmarked = sum(
        1
        for result in results
        for _, _, name in result.unmarked
        if name == arm.name
    )
    for result in results:
        for key, value in result.values.items():
            index, side, name = key
            if name != arm.name:
                continue
            values.append(value)
            fills += int(key in result.filled)
            control_key = (index, side, control)
            if control_key not in result.values:
                continue
            control_value = result.values[control_key]
            diffs.append(value - control_value)
            pairs += 1
            control_filled = control_key in result.filled
            control_fills += int(control_filled)
            if control_filled and key not in result.filled:
                if control_value < 0.0:
                    avoided_loss += -control_value
                else:
                    forgone_gain += control_value
    return {
        "arm": arm.name,
        "opportunities": opportunities,
        "markable_opportunities": len(values),
        "markout_coverage": len(values) / opportunities if opportunities else None,
        "fills": fills,
        "fill_rate": fills / opportunities if opportunities else None,
        "mean_net_bp_per_markable_quote": statistics.mean(values) if values else None,
        "paired_n": pairs,
        "paired_delta_bp_per_quote": statistics.mean(diffs) if diffs else None,
        "paired_t": t_stat(diffs),
        "control_fills": control_fills,
        "avoided_loss_bp": avoided_loss,
        "forgone_gain_bp": forgone_gain,
        "unmarked_fills": unmarked,
        "config": {field: getattr(arm, field) for field in arm.__dataclass_fields__ if field != "name"},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tape-root", type=Path, required=True)
    parser.add_argument("--dates", required=True)
    parser.add_argument("--ticks", type=Path, required=True)
    parser.add_argument("--symbols", default="AUTO")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attempt-seconds", type=float, default=30.0)
    parser.add_argument("--markout-seconds", type=float, default=15.0)
    parser.add_argument("--max-records-per-symbol", type=int, default=0)
    args = parser.parse_args(argv)

    dates = [date.strip() for date in args.dates.split(",") if date.strip()]
    ticks = {str(key).upper(): float(value) for key, value in json.loads(args.ticks.read_text()).items()}
    symbols = (
        available_symbols(args.tape_root, dates)
        if args.symbols.upper() == "AUTO"
        else sorted({symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()})
    )
    symbols = [symbol for symbol in symbols if symbol in ticks]
    if not symbols:
        raise SystemExit("no tape symbols have a tick size")

    all_results: list[Simulation] = []
    by_date: dict[str, list[Simulation]] = {date: [] for date in dates}
    sources: list[dict[str, Any]] = []
    for date in dates:
        for symbol in symbols:
            paths = segments(args.tape_root, date, symbol)
            if not paths:
                continue
            result = simulate(
                records(paths, args.max_records_per_symbol),
                symbol,
                ticks[symbol],
                int(args.attempt_seconds * NS),
                int(args.markout_seconds * NS),
            )
            all_results.append(result)
            by_date[date].append(result)
            sources.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "segments": [str(path) for path in paths],
                    "opportunities": result.opportunities * 2,
                    "unmarked_fills": len(result.unmarked),
                }
            )
            print(
                f"{date} {symbol:16s} opportunities={result.opportunities * 2:6d}",
                file=sys.stderr,
                flush=True,
            )

    table = [summarize(all_results, arm) for arm in ARMS]
    payload = {
        "kind": "liquidity_migration_toxic_quoter_pair_replay",
        "lane": "Lane-1 exploratory seen-tape diagnostic",
        "claim": "one-sided depth-scaled flow protection improves fee-adjusted 15-second quote selection",
        "scope": {
            "tape_root": str(args.tape_root),
            "dates": dates,
            "symbols": symbols,
            "attempt_seconds": args.attempt_seconds,
            "markout_seconds": args.markout_seconds,
            "queue_bound": "conservative displayed queue; public trades advance it",
            "inventory": "independent flat quote opportunities; no inventory path",
        },
        "feature": {
            "fast_half_life_ms": FLOW_FAST_HALF_LIFE_NS / 1_000_000,
            "slow_half_life_ms": FLOW_SLOW_HALF_LIFE_NS / 1_000_000,
            "fast_weight": FLOW_FAST_WEIGHT,
            "slow_weight": FLOW_SLOW_WEIGHT,
            "near_depth_bps": FLOW_DEPTH_BP,
            "volatility_depth_multiplier": FLOW_VOL_DEPTH_MULTIPLIER,
            "max_score": FLOW_MAX_SCORE,
        },
        "arms": table,
        "by_date": {
            date: [summarize(results_for_date, arm) for arm in ARMS]
            for date, results_for_date in by_date.items()
        },
        "sources": sources,
        "non_conclusions": [
            "Seen tape shaped and graded every arm, so no row is forward evidence.",
            "Independent quote opportunities omit inventory, stop, and competing-sleeve paths.",
            "Public L50 queue bounds do not reveal the venue's exact queue priority.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    for row in table:
        print(
            f"{row['arm']:24s} fill={100 * (row['fill_rate'] or 0):5.1f}% "
            f"net/quote={row['mean_net_bp_per_markable_quote']!s:>9s} "
            f"delta={row['paired_delta_bp_per_quote']!s:>9s} t={row['paired_t']!s:>9s}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
