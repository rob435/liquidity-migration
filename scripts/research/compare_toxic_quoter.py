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


REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_MANIFEST = REPO_ROOT / "engine" / "Cargo.toml"
REGISTERED_CONFIG = REPO_ROOT / "configs" / "lane2_toxic_flow_quoter_v1.json"
REGISTERED_RULE = json.loads(REGISTERED_CONFIG.read_text())["rule"]
REGISTERED_FLOW = REGISTERED_RULE["flow"]

NS = 1_000_000_000
HALF_SPREAD_BP = float(REGISTERED_RULE["half_spread_bps"])
REQUOTE_BP = float(REGISTERED_RULE["requote_bps"])
SIGNAL_HALF_LIFE_NS = int(float(REGISTERED_RULE["signal_half_life_ms"]) * 1_000_000)
FLOW_FAST_HALF_LIFE_NS = int(float(REGISTERED_FLOW["fast_half_life_ms"]) * 1_000_000)
FLOW_SLOW_HALF_LIFE_NS = int(float(REGISTERED_FLOW["slow_half_life_ms"]) * 1_000_000)
FLOW_FAST_WEIGHT = float(REGISTERED_FLOW["fast_weight"])
FLOW_SLOW_WEIGHT = float(REGISTERED_FLOW["slow_weight"])
FLOW_DEPTH_BP = float(REGISTERED_FLOW["near_depth_bps"])
FLOW_VOL_DEPTH_MULTIPLIER = float(REGISTERED_FLOW["volatility_depth_multiplier"])
FLOW_MAX_SCORE = float(REGISTERED_FLOW["max_score"])
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
    queue_reprice_edge_bp: float = 0.0


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
    Arm(
        "directional_w4",
        maker_fee_bp=float(REGISTERED_RULE["maker_fee_bps"]),
        min_edge_bp=float(REGISTERED_RULE["min_edge_bps"]),
        volatility_multiplier=float(REGISTERED_RULE["volatility_multiplier"]),
        book_lean_bp=float(REGISTERED_RULE["book_lean_bps"]),
        flow_response_bp=float(REGISTERED_FLOW["response_bps"]),
        flow_max_widen_bp=float(REGISTERED_FLOW["max_widen_bps"]),
        flow_pull_score=REGISTERED_FLOW["pull_score"],
        queue_reprice_edge_bp=float(REGISTERED_RULE["queue_reprice_edge_bps"]),
    ),
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


class RustQuoterContract:
    """Persistent adapter to the compiled Rust reducer and quote planner."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [
                "cargo",
                "run",
                "--quiet",
                "--manifest-path",
                str(ENGINE_MANIFEST),
                "-p",
                "engine-strategies",
                "--bin",
                "quoter_contract",
            ],
            cwd=REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(self._init()) + "\n")
        self.process.stdin.flush()

    @staticmethod
    def _init() -> dict[str, Any]:
        return {
            "half_spread_bps": HALF_SPREAD_BP,
            "requote_bps": REQUOTE_BP,
            "signal_half_life_ms": SIGNAL_HALF_LIFE_NS / 1_000_000,
            "flow_fast_half_life_ms": FLOW_FAST_HALF_LIFE_NS / 1_000_000,
            "flow_slow_half_life_ms": FLOW_SLOW_HALF_LIFE_NS / 1_000_000,
            "flow_fast_weight": FLOW_FAST_WEIGHT,
            "flow_slow_weight": FLOW_SLOW_WEIGHT,
            "flow_depth_bps": FLOW_DEPTH_BP,
            "flow_volatility_depth_multiplier": FLOW_VOL_DEPTH_MULTIPLIER,
            "flow_max_score": FLOW_MAX_SCORE,
            "arms": [
                {
                    "name": arm.name,
                    "maker_fee_bps": arm.maker_fee_bp,
                    "min_edge_bps": arm.min_edge_bp,
                    "volatility_multiplier": arm.volatility_multiplier,
                    "toxicity_bps": arm.toxicity_bp,
                    "book_lean_bps": arm.book_lean_bp,
                    "trade_lean_bps": arm.trade_lean_bp,
                    "flow_response_bps": arm.flow_response_bp,
                    "flow_max_widen_bps": arm.flow_max_widen_bp,
                    "flow_pull_score": arm.flow_pull_score,
                    "queue_reprice_edge_bps": arm.queue_reprice_edge_bp,
                }
                for arm in ARMS
            ],
        }

    def decide(
        self,
        event: dict[str, Any],
        tick_size: float,
        working: dict[str, dict[str, float | None]],
    ) -> dict[str, dict[str, float | None]]:
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(
            json.dumps({"tick_size": tick_size, "event": event, "working": working})
            + "\n"
        )
        self.process.stdin.flush()
        response = self.process.stdout.readline()
        if not response:
            assert self.process.stderr is not None
            detail = self.process.stderr.read().strip()
            raise RuntimeError(f"Rust quoter contract stopped: {detail}")
        return json.loads(response)["prices"]

    def close(self, *, check: bool = True) -> None:
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
        code = self.process.wait()
        if check and code != 0:
            assert self.process.stderr is not None
            raise RuntimeError(
                f"Rust quoter contract exited {code}: {self.process.stderr.read().strip()}"
            )


@dataclass(slots=True)
class VirtualQuote:
    side: str
    px: float | None = None
    queue: float = 0.0
    filled: bool = False

    def apply_decision(
        self, wanted: float | None, mirror: BookMirror, symbol: str
    ) -> None:
        if wanted is None:
            self.px = None
            self.queue = 0.0
            return
        if self.px == wanted:
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
    contract = RustQuoterContract()
    failure: BaseException | None = None
    try:
        return _simulate_with_contract(
            records, symbol, tick, attempt_ns, markout_ns, contract
        )
    except BaseException as error:
        failure = error
        raise
    finally:
        contract.close(check=failure is None)


def _simulate_with_contract(
    records: Iterator[dict[str, Any]],
    symbol: str,
    tick: float,
    attempt_ns: int,
    markout_ns: int,
    contract: RustQuoterContract,
) -> Simulation:
    mirror = BookMirror()
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

        working = {
            arm.name: {
                ("bid" if side == "Buy" else "ask"): (
                    opportunity.quotes[(arm.name, side)].px
                    if opportunity is not None
                    and not opportunity.quotes[(arm.name, side)].filled
                    else None
                )
                for side in ("Buy", "Sell")
            }
            for arm in ARMS
        }
        if kind == "public_trade":
            event = {
                "kind": "trades",
                "recv_ns": ts,
                "buy_qty": qty if valid_trade and aggressor == "Buy" else 0.0,
                "sell_qty": qty if valid_trade and aggressor == "Sell" else 0.0,
                "last_px": price if valid_trade else 0.0,
            }
        else:
            event = {
                "kind": "depth",
                "recv_ns": ts,
                "bids": mirror.levels(symbol, "Buy"),
                "asks": mirror.levels(symbol, "Sell"),
            }
        decisions = contract.decide(event, tick, working)

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
                        opportunity.quotes[(arm.name, side)] = quote

        if opportunity is not None:
            for arm in ARMS:
                prices = decisions[arm.name]
                for side in ("Buy", "Sell"):
                    quote = opportunity.quotes[(arm.name, side)]
                    if quote.filled:
                        continue
                    key = "bid" if side == "Buy" else "ask"
                    quote.apply_decision(prices[key], mirror, symbol)

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
