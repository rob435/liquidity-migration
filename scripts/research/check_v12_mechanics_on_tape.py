#!/usr/bin/env python3
"""Grade the LONG kernel's fill assumptions against the recorded market tape.

The backtest kernel (``liquidity_migration/research/backtest/long_native.py``)
prices v12's fills off hourly bars, and both fill rules are assumptions:

- Entry: a resting "sniper" limit at signal close x (1 - retrace, 1% in v12)
  inside a 6-hour window. The kernel treats the first hourly bar whose LOW
  touches that level -- or the deadline bar when none does -- as "observed",
  and fills at the NEXT bar's OPEN (``_entry_at_next_hour_open``), not at the
  limit itself.
- Stop: when a bar's LOW touches the stop level, the kernel fills exactly AT
  the stop price at that bar's end (``_scan_position_exit``), after raising
  the stop from entry x (1 - 3 x ATR) to entry x (1 - 1.5 x ATR) once the
  trade is 48 hours old. The trades CSV records only the stop as it stood at
  exit, so this tool reconstructs both legs from that final level and the
  registered v12 multipliers.

This tool replays the forward tape recorded by
``scripts/research/capture_bybit_forward.py`` and grades those assumptions
per trade:

- Entry fill honesty, two bounds reported side by side. Conservative: filled
  only when a public trade prints strictly BELOW the limit -- someone traded
  through our level, no claim about our place in the queue. Optimistic:
  filled the first moment a trade prints at or below the limit or the touch
  (depth-1 book, or the depth-50 top) reaches it. When the input carries no
  limit price, the limit is derived from the tape itself: the last trade at
  or before the signal timestamp is the signal close.
- Stop fill honesty: at the first tape moment the mark price (from tickers;
  the last trade before any mark is seen) crosses the stop level, walk the
  fresh depth-50 book with the base quantity bought by the stated entry
  notional and report the displayed average exit against the assumed fill at
  the stop price. The walk is a same-moment displayed-depth estimate, not a
  bound on realized slippage: latency, cancellations, impact, hidden size and
  replenishment are absent.

Live has no take-profit, so only entry and stop are graded.

Lane-1 triage diagnostic, in the quote lab's spirit: it flags execution
assumptions on whatever tape exists. It is never a registration tool -- it
grades no strategy, registers nothing, and its numbers promote nothing.

Usage:
  python scripts/research/check_v12_mechanics_on_tape.py --tape-root DIR \
      --model-trades long_native_trades.csv --out OUTDIR
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from liquidity_migration.research.execution.quote_lab.book import BookMirror  # noqa: E402
from liquidity_migration.rules.long_native import long_v12_profile  # noqa: E402

MS_PER_HOUR = 3_600_000
REGISTERED_MODEL_TRADE = "registered_model_trade"
TAPE_DERIVED_PROXY = "tape_derived_proxy"
ARTIFICIAL_EXERCISE = "artificial_exercise"
LIVE_STATE_OBSERVATION = "live_state_observation"
LIVE_TRANSITION_OBSERVATION = "live_transition_observation"
MODEL_EVIDENCE_KINDS = frozenset(
    {
        REGISTERED_MODEL_TRADE,
        TAPE_DERIVED_PROXY,
        ARTIFICIAL_EXERCISE,
    }
)
ALL_EVIDENCE_KINDS = (
    REGISTERED_MODEL_TRADE,
    TAPE_DERIVED_PROXY,
    ARTIFICIAL_EXERCISE,
    LIVE_STATE_OBSERVATION,
    LIVE_TRANSITION_OBSERVATION,
)
_PROFILE = long_v12_profile()
RETRACE_PCT = _PROFILE.fc_sniper_retrace_pct
DEADLINE_HOURS = max(max(1, _PROFILE.entry_delay_hours), _PROFILE.fc_sniper_deadline_hours)


@dataclass(slots=True)
class TradeSpec:
    """One trade to grade, from whichever record supplied it."""

    trade_id: str
    symbol: str
    source: str
    evidence_kind: str
    signal_ts_ms: int
    kernel_entry_ts_ms: int | None = None
    kernel_entry_price: float | None = None
    kernel_exit_ts_ms: int | None = None
    exit_reason: str = ""
    stop_level: float | None = None
    stop_decay_at_ms: int | None = None
    stop_decayed_level: float | None = None
    stop_path_source: str = ""
    limit_price: float | None = None


@dataclass(slots=True)
class TradeGrade:
    spec: TradeSpec
    covered: bool = False
    limit_price: float | None = None
    limit_source: str = ""
    signal_close_tape: float | None = None
    signal_close_age_s: float | None = None
    entry_cons_fill_ms: int | None = None
    entry_opt_fill_ms: int | None = None
    entry_note: str = ""
    tape_first_ms: int | None = None
    tape_last_ms: int | None = None
    entry_window_bracketed: bool = False
    stop_observation_bracketed: bool = False
    coverage_note: str = ""
    stop_trigger_ms: int | None = None
    stop_trigger_source: str = ""
    stop_book_ts_ms: int | None = None
    stop_book_age_ms: int | None = None
    stop_walk_avg_price: float | None = None
    stop_walk_target_qty: float | None = None
    stop_walk_filled_qty: float | None = None
    stop_walk_filled_fraction: float | None = None
    stop_note: str = ""
    _latched: bool = field(default=False, repr=False)

    def effective_stop(self, ts_ms: int) -> float | None:
        spec = self.spec
        if spec.stop_level is None:
            return None
        if spec.stop_decay_at_ms is not None and spec.stop_decayed_level is not None and ts_ms >= spec.stop_decay_at_ms:
            return max(spec.stop_level, spec.stop_decayed_level)
        return spec.stop_level

    @property
    def entry_deadline_ms(self) -> int:
        return self.spec.signal_ts_ms + DEADLINE_HOURS * MS_PER_HOUR

    @property
    def stop_window_start_ms(self) -> int:
        return self.spec.kernel_entry_ts_ms or self.spec.signal_ts_ms

    def window_end_ms(self) -> int | None:
        """Last moment this trade needs tape; None while the trade is open."""

        end = self.spec.kernel_exit_ts_ms
        if self.spec.stop_level is not None and end is None:
            return None
        return max(self.entry_deadline_ms, end) if end is not None else self.entry_deadline_ms


@dataclass(slots=True)
class Counters:
    rows_read: int = 0
    sequence_gaps: int = 0
    segments_read: int = 0
    segments_skipped: int = 0
    partial_segments: int = 0
    invalid_timestamp_rows: int = 0
    timestamp_regressions: int = 0
    segment_paths: list[str] = field(default_factory=list)
    skipped_segment_paths: list[str] = field(default_factory=list)
    partial_segment_paths: list[str] = field(default_factory=list)


def lines(path: Path) -> Iterator[dict[str, Any]]:
    """Tape rows through the zstd binary; the repo deliberately pins no python zstd."""

    if path.suffix != ".zst":
        with path.open("rb") as handle:
            for raw in handle:
                if raw.strip():
                    yield json.loads(raw)
        return
    with subprocess.Popen(["zstd", "-dcq", "--", str(path)], stdout=subprocess.PIPE) as process:
        assert process.stdout is not None
        for raw in process.stdout:
            if raw.strip():
                yield json.loads(raw)
        code = process.wait()
        if code != 0:
            raise RuntimeError(f"zstd failed for {path} with exit {code}")


def segments(root: Path, day: str, symbol: str) -> list[Path]:
    directory = root / day / symbol
    return sorted((*directory.glob("segment-*.jsonl"), *directory.glob("segment-*.jsonl.zst")))


def partial_segments(root: Path, day: str, symbol: str) -> int:
    return len(list((root / day / symbol).glob("segment-*.jsonl.partial")))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_identity(path: Path, *, with_hash: bool = True) -> dict[str, Any]:
    resolved = path.resolve()
    result: dict[str, Any] = {
        "path": str(resolved),
        "bytes": resolved.stat().st_size,
    }
    if with_hash:
        result["sha256"] = _sha256(resolved)
    return result


def _git_worktree_blob(repo: Path, source_path: str) -> str:
    path = repo / source_path
    if not path.is_file():
        raise ValueError(f"current checkout has no {source_path}")
    result = subprocess.run(
        ["git", "-C", str(repo), "hash-object", f"--path={source_path}", "--", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"cannot identify current checkout source {source_path}")
    return result.stdout.strip()


def _kernel_identity(commit: str | None, *, repo: Path | None = None) -> dict[str, Any]:
    if not commit:
        return {"commit": None, "source_blobs": {}, "pinned": False}
    repo = repo or Path(__file__).resolve().parents[2]
    resolved = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{commit}^{{commit}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if resolved.returncode != 0:
        raise ValueError(f"--model-commit does not resolve to a commit: {commit}")
    full_commit = resolved.stdout.strip()
    source_paths = (
        "liquidity_migration/rules/long_native.py",
        "liquidity_migration/research/backtest/long_native.py",
    )
    blobs: dict[str, str] = {}
    current_blobs: dict[str, str] = {}
    for source_path in source_paths:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", f"{full_commit}:{source_path}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(f"model commit {full_commit} has no {source_path}")
        blobs[source_path] = result.stdout.strip()
        current_blobs[source_path] = _git_worktree_blob(repo, source_path)
    mismatches = [source_path for source_path in source_paths if current_blobs[source_path] != blobs[source_path]]
    if mismatches:
        joined = ", ".join(mismatches)
        raise ValueError(
            f"--model-commit {full_commit} does not match the current checkout for {joined}; "
            "refusing to run current profile code under a pinned identity"
        )
    return {
        "commit": full_commit,
        "source_blobs": blobs,
        "current_source_blobs": current_blobs,
        "matches_current_checkout": True,
        "pinned": True,
    }


def available_days(root: Path, symbol: str) -> list[str]:
    found = []
    for day_dir in root.iterdir() if root.is_dir() else []:
        if day_dir.is_dir() and (day_dir / symbol).is_dir() and segments(root, day_dir.name, symbol):
            found.append(day_dir.name)
    return sorted(found)


def _day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()


def _days_between(start: str, end: str) -> set[str]:
    first = datetime.fromisoformat(start).date()
    last = datetime.fromisoformat(end).date()
    out = set()
    while first <= last:
        out.add(first.isoformat())
        first += timedelta(days=1)
    return out


def walk_book(levels: list[tuple[float, float]], target_qty: float) -> tuple[float | None, float]:
    """(average price, filled qty) from eating displayed levels in venue order."""

    filled = 0.0
    cost = 0.0
    for price, qty in levels:
        take = min(qty, target_qty - filled)
        if take <= 0.0:
            break
        filled += take
        cost += take * price
    if filled <= 0.0:
        return None, 0.0
    return cost / filled, filled


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def model_stop_path(
    *,
    entry_price: float | None,
    entry_ts_ms: int | None,
    exit_ts_ms: int | None,
    recorded_stop: float | None,
) -> tuple[float | None, int | None, float | None, str]:
    """Rebuild v12's initial and decayed stops from its final CSV stop.

    The kernel writes only the stop in force when the trade exits. Before the
    48-hour change point that is the 3x-ATR stop; at or after it, it is the
    1.5x-ATR stop. Both levels therefore remain exactly recoverable from the
    entry price and the registered multiplier ratio.
    """

    if recorded_stop is None:
        return None, None, None, "missing"
    if (
        entry_price is None
        or entry_price <= 0.0
        or entry_ts_ms is None
        or recorded_stop <= 0.0
        or recorded_stop >= entry_price
        or _PROFILE.fc_atr_stop_mult <= 0.0
        or _PROFILE.fc_stop_time_decay_hours <= 0
        or _PROFILE.fc_stop_time_decay_atr_mult <= 0.0
    ):
        return recorded_stop, None, None, "recorded_constant_unreconstructed"

    decay_at_ms = entry_ts_ms + _PROFILE.fc_stop_time_decay_hours * MS_PER_HOUR
    final_is_decayed = exit_ts_ms is not None and exit_ts_ms >= decay_at_ms
    recorded_mult = _PROFILE.fc_stop_time_decay_atr_mult if final_is_decayed else _PROFILE.fc_atr_stop_mult
    atr_fraction = (entry_price - recorded_stop) / entry_price / recorded_mult
    initial = entry_price * (1.0 - _PROFILE.fc_atr_stop_mult * atr_fraction)
    decayed = entry_price * (1.0 - _PROFILE.fc_stop_time_decay_atr_mult * atr_fraction)
    if initial <= 0.0 or decayed <= 0.0:
        return recorded_stop, None, None, "recorded_constant_unreconstructed"
    source = "model_csv_final_decayed" if final_is_decayed else "model_csv_final_initial"
    return initial, decay_at_ms, decayed, source


def load_model_trades(path: Path) -> list[TradeSpec]:
    """Rows in the kernel's ``long_native_trades.csv`` schema.

    ``stop_price`` there is the stop as it stood at exit. The registered v12
    multiplier ratio recovers its initial and 48-hour levels. An optional
    ``limit_price`` column pins the entry limit; absent, the limit is derived
    from the tape's own signal close. Every row must declare ``evidence_kind``
    as ``registered_model_trade``, ``tape_derived_proxy``, or
    ``artificial_exercise``. The tool never guesses that fact from a trade ID.
    """

    out: list[TradeSpec] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for i, row in enumerate(csv.DictReader(handle)):
            symbol = str(row.get("symbol") or "").upper()
            signal_ts = _int_or_none(row.get("entry_signal_ts_ms"))
            if not symbol or signal_ts is None:
                continue
            evidence_kind = str(row.get("evidence_kind") or "").strip()
            if evidence_kind not in MODEL_EVIDENCE_KINDS:
                allowed = ", ".join(sorted(MODEL_EVIDENCE_KINDS))
                detail = f"got {evidence_kind!r}" if evidence_kind else "field is missing or empty"
                raise ValueError(f"{path}: model row {i + 2} must declare evidence_kind ({allowed}); {detail}")
            entry_ts = _int_or_none(row.get("entry_ts_ms"))
            entry_price = _float_or_none(row.get("entry_price"))
            exit_ts = _int_or_none(row.get("exit_ts_ms"))
            initial_stop, decay_at, decayed_stop, stop_source = model_stop_path(
                entry_price=entry_price,
                entry_ts_ms=entry_ts,
                exit_ts_ms=exit_ts,
                recorded_stop=_float_or_none(row.get("stop_price")),
            )
            out.append(
                TradeSpec(
                    trade_id=str(row.get("trade_id") or f"model-{i}-{symbol}"),
                    symbol=symbol,
                    source="model_csv",
                    evidence_kind=evidence_kind,
                    signal_ts_ms=signal_ts,
                    kernel_entry_ts_ms=entry_ts,
                    kernel_entry_price=entry_price,
                    kernel_exit_ts_ms=exit_ts,
                    exit_reason=str(row.get("exit_reason") or ""),
                    stop_level=initial_stop,
                    stop_decay_at_ms=decay_at,
                    stop_decayed_level=decayed_stop,
                    stop_path_source=stop_source,
                    limit_price=_float_or_none(row.get("limit_price")),
                )
            )
    return out


def load_live_state(path: Path) -> list[TradeSpec]:
    """Held rows from the producer's book record (open trades: no exit yet)."""

    from liquidity_migration.strategy.long_book_state import read_book_state

    out: list[TradeSpec] = []
    for entry in read_book_state(path).held.values():
        entered = int(entry.entered_ts_ms) or None
        entry_price = float(entry.entry_price) if entry.entry_price > 0.0 else None
        stop_level = None
        decay_at = None
        decayed_level = None
        if entry_price is not None and entry.stop_loss_fraction > 0.0:
            stop_level = entry_price * (1.0 - float(entry.stop_loss_fraction))
            if entered and entry.stop_decay_after_ms > 0 and entry.decayed_stop_loss_pct > 0.0:
                decay_at = entered + int(entry.stop_decay_after_ms)
                decayed_level = entry_price * (1.0 - float(entry.decayed_stop_loss_pct))
        out.append(
            TradeSpec(
                trade_id=entry.trade_id,
                symbol=entry.symbol.upper(),
                source="live_state",
                evidence_kind=LIVE_STATE_OBSERVATION,
                signal_ts_ms=int(entry.signal_ts_ms) or (entered or 0),
                kernel_entry_ts_ms=entered,
                kernel_entry_price=entry_price,
                stop_level=stop_level,
                stop_decay_at_ms=decay_at,
                stop_decayed_level=decayed_level,
                stop_path_source="live_state_explicit",
            )
        )
    return [spec for spec in out if spec.signal_ts_ms > 0]


def load_transitions(path: Path) -> list[TradeSpec]:
    """Enter/leave rows from the producer's transitions log.

    These carry no prices, so only the entry side is gradable, with the limit
    derived from the tape's signal close.
    """

    enters: dict[str, dict[str, Any]] = {}
    leaves: dict[str, int] = {}
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            row = json.loads(raw)
            trade_id = str(row.get("trade_id") or "")
            if not trade_id:
                continue
            if row.get("event") == "enter":
                enters[trade_id] = row
            elif row.get("event") == "leave":
                leaves[trade_id] = int(row.get("ts_ms") or 0)
    out: list[TradeSpec] = []
    for trade_id, row in enters.items():
        signal_ts = _int_or_none(row.get("signal_ts_ms"))
        symbol = str(row.get("symbol") or "").upper()
        if not symbol or not signal_ts:
            continue
        out.append(
            TradeSpec(
                trade_id=trade_id,
                symbol=symbol,
                source="transitions",
                evidence_kind=LIVE_TRANSITION_OBSERVATION,
                signal_ts_ms=signal_ts,
                kernel_entry_ts_ms=_int_or_none(row.get("ts_ms")),
                kernel_exit_ts_ms=leaves.get(trade_id),
            )
        )
    return out


def grade_symbol(
    tape_root: Path,
    symbol: str,
    grades: list[TradeGrade],
    *,
    notional_usdt: float,
    max_book_age_ms: int,
    counters: Counters,
) -> None:
    available = available_days(tape_root, symbol)
    if not available:
        return
    wanted: set[str] = set()
    for grade in grades:
        end = grade.window_end_ms()
        end_day = _day(end) if end is not None else available[-1]
        wanted |= _days_between(_day(grade.spec.signal_ts_ms), end_day)
    days = [day for day in available if day in wanted]
    if not days:
        return
    for grade in grades:
        if grade.spec.limit_price is not None:
            grade.limit_price = grade.spec.limit_price
            grade.limit_source = "explicit"

    deep = BookMirror()
    touch = BookMirror()
    mark_price: float | None = None
    last_trade_px: float | None = None
    last_trade_ms: int | None = None
    first_row_ms: int | None = None
    last_row_ms = 0
    deep_book_ms: int | None = None
    skipped_for_symbol = 0
    regressed_for_symbol = False
    readable_days: set[str] = set()

    def check_entry(grade: TradeGrade, ts_ms: int, *, trade_px: float | None, best_ask: float | None) -> None:
        limit = grade.limit_price
        if limit is None or not (grade.spec.signal_ts_ms < ts_ms <= grade.entry_deadline_ms):
            return
        if trade_px is not None:
            if grade.entry_cons_fill_ms is None and trade_px < limit:
                grade.entry_cons_fill_ms = ts_ms
            if grade.entry_opt_fill_ms is None and trade_px <= limit:
                grade.entry_opt_fill_ms = ts_ms
        if best_ask is not None and grade.entry_opt_fill_ms is None and best_ask <= limit:
            grade.entry_opt_fill_ms = ts_ms

    def check_stop(grade: TradeGrade, ts_ms: int, price: float, source: str) -> None:
        if grade.stop_trigger_ms is not None:
            return
        level = grade.effective_stop(ts_ms)
        if level is None or not (grade.stop_window_start_ms < ts_ms):
            return
        end = grade.spec.kernel_exit_ts_ms
        if end is not None and ts_ms > end:
            return
        if price > level:
            return
        grade.stop_trigger_ms = ts_ms
        grade.stop_trigger_source = source
        if not deep.healthy(symbol):
            grade.stop_note = "book unhealthy at trigger (gap or no snapshot); walk skipped"
            return
        if deep_book_ms is None:
            grade.stop_note = "no timed depth-50 book at trigger; walk skipped"
            return
        grade.stop_book_ts_ms = deep_book_ms
        grade.stop_book_age_ms = ts_ms - deep_book_ms
        if grade.stop_book_age_ms < 0 or grade.stop_book_age_ms > max_book_age_ms:
            grade.stop_note = (
                f"depth-50 book is {grade.stop_book_age_ms}ms old at trigger (limit {max_book_age_ms}ms); walk skipped"
            )
            return
        qty_basis = grade.spec.kernel_entry_price or level
        target_qty = notional_usdt / qty_basis
        avg, filled = walk_book(deep.levels(symbol, "Buy", limit=50), target_qty)
        grade.stop_walk_target_qty = target_qty
        grade.stop_walk_filled_qty = filled
        if avg is None:
            grade.stop_note = "empty bid book at trigger; walk impossible"
            return
        grade.stop_walk_avg_price = avg
        grade.stop_walk_filled_fraction = filled / target_qty
        if grade.stop_walk_filled_fraction < 1.0:
            grade.stop_note = "displayed 50 levels thinner than the notional; walked what was shown"

    for day in days:
        partials = sorted((tape_root / day / symbol).glob("segment-*.jsonl.partial"))
        counters.partial_segments += len(partials)
        counters.partial_segment_paths.extend(str(path.resolve()) for path in partials)
        for segment in segments(tape_root, day, symbol):
            try:
                rows = list(lines(segment))
            except (RuntimeError, OSError, json.JSONDecodeError):
                counters.segments_skipped += 1
                counters.skipped_segment_paths.append(str(segment.resolve()))
                skipped_for_symbol += 1
                continue
            counters.segments_read += 1
            counters.segment_paths.append(str(segment.resolve()))
            readable_days.add(day)
            for row in rows:
                counters.rows_read += 1
                kind = row.get("kind")
                ts_ms = int(row.get("local_receive_ts_ns") or 0) // 1_000_000
                if ts_ms <= 0:
                    counters.invalid_timestamp_rows += 1
                    continue
                if ts_ms < last_row_ms:
                    counters.timestamp_regressions += 1
                    regressed_for_symbol = True
                    continue
                first_row_ms = ts_ms if first_row_ms is None else min(first_row_ms, ts_ms)
                last_row_ms = ts_ms
                for grade in grades:
                    if not grade._latched and ts_ms > grade.spec.signal_ts_ms:
                        grade._latched = True
                        if last_trade_px is not None and last_trade_ms is not None:
                            grade.signal_close_tape = last_trade_px
                            age_s = (grade.spec.signal_ts_ms - last_trade_ms) / 1000.0
                            grade.signal_close_age_s = age_s
                            if grade.limit_price is None:
                                grade.limit_price = last_trade_px * (1.0 - RETRACE_PCT)
                                grade.limit_source = "derived_tape_close"
                            if age_s > 60.0:
                                grade.entry_note = f"signal close from a trade {age_s:.0f}s before the signal"
                        elif grade.limit_price is None:
                            grade.entry_note = "no tape trade at or before the signal; entry ungraded"
                if kind in ("orderbook_snapshot", "orderbook_delta"):
                    if row.get("sequence_gap"):
                        counters.sequence_gaps += 1
                    depth = int(row.get("depth") or 0)
                    if depth == 50:
                        deep.apply(row)
                        healthy = deep.healthy(symbol)
                        deep_book_ms = ts_ms if healthy else None
                        ask = deep.best_ask(symbol) if healthy else None
                        for grade in grades:
                            check_entry(grade, ts_ms, trade_px=None, best_ask=ask)
                    elif depth == 1:
                        touch.apply(row)
                        ask = touch.best_ask(symbol) if touch.healthy(symbol) else None
                        for grade in grades:
                            check_entry(grade, ts_ms, trade_px=None, best_ask=ask)
                elif kind == "public_trade":
                    deep.apply(row)
                    price = float(row.get("price") or 0.0)
                    if price <= 0.0:
                        continue
                    last_trade_px, last_trade_ms = price, ts_ms
                    for grade in grades:
                        check_entry(grade, ts_ms, trade_px=price, best_ask=None)
                        if mark_price is None:
                            check_stop(grade, ts_ms, price, "last_trade")
                elif kind == "ticker":
                    values = row.get("values")
                    mark = values.get("mark_price") if isinstance(values, dict) else None
                    if mark is not None:
                        mark_price = float(mark)
                        for grade in grades:
                            check_stop(grade, ts_ms, mark_price, "mark_price")

    # First/last timestamps bracket an interval; they do not prove every event
    # inside it arrived. Unreadable segments and timestamp regressions refuse
    # even that narrower claim.
    for grade in grades:
        grade.tape_first_ms = first_row_ms
        grade.tape_last_ms = last_row_ms or None
        requested_end = grade.window_end_ms() or last_row_ms
        entry_days = _days_between(_day(grade.spec.signal_ts_ms), _day(grade.entry_deadline_ms))
        grade.covered = bool(
            first_row_ms is not None and last_row_ms > grade.spec.signal_ts_ms and first_row_ms <= requested_end
        )
        interval_clean = skipped_for_symbol == 0 and not regressed_for_symbol
        grade.entry_window_bracketed = bool(
            interval_clean
            and entry_days <= readable_days
            and first_row_ms is not None
            and first_row_ms <= grade.spec.signal_ts_ms
            and last_row_ms >= grade.entry_deadline_ms
        )
        stop_end = grade.stop_trigger_ms or grade.spec.kernel_exit_ts_ms
        stop_days = _days_between(_day(grade.stop_window_start_ms), _day(stop_end)) if stop_end is not None else set()
        grade.stop_observation_bracketed = bool(
            interval_clean
            and stop_days <= readable_days
            and first_row_ms is not None
            and stop_end is not None
            and first_row_ms <= grade.stop_window_start_ms
            and last_row_ms >= stop_end
        )
        coverage_issues: list[str] = []
        if first_row_ms is None:
            coverage_issues.append("no readable timestamped tape rows")
        else:
            if first_row_ms > grade.spec.signal_ts_ms:
                coverage_issues.append("tape starts after the signal")
            if last_row_ms < grade.entry_deadline_ms:
                coverage_issues.append("tape ends before the entry deadline")
            if grade.spec.kernel_exit_ts_ms is not None and last_row_ms < grade.spec.kernel_exit_ts_ms:
                coverage_issues.append("tape ends before the model exit")
        if skipped_for_symbol:
            coverage_issues.append(f"{skipped_for_symbol} segment(s) unreadable")
        if regressed_for_symbol:
            coverage_issues.append("receive timestamps regressed")
        missing_days = sorted((entry_days | stop_days) - readable_days)
        if missing_days:
            coverage_issues.append(f"no completed segment on {', '.join(missing_days)}")
        grade.coverage_note = "; ".join(coverage_issues)
        if grade.limit_price is not None and grade.entry_opt_fill_ms is None and not grade.entry_note:
            grade.entry_note = (
                "unfilled in both bounds inside the deadline window"
                if grade.entry_window_bracketed
                else "entry window is not fully bracketed; unfilled so far"
            )
        if grade.spec.stop_level is not None and grade.stop_trigger_ms is None and not grade.stop_note:
            grade.stop_note = (
                "stop never crossed on tape inside the trade's window"
                if grade.stop_observation_bracketed
                else "tape ends before the trade's exit; stop not crossed so far"
            )


CSV_COLUMNS = [
    "trade_id",
    "symbol",
    "source",
    "evidence_kind",
    "covered",
    "tape_first_ts_ms",
    "tape_last_ts_ms",
    "entry_window_bracketed",
    "stop_observation_bracketed",
    "coverage_note",
    "signal_ts_ms",
    "limit_price",
    "limit_source",
    "signal_close_tape",
    "signal_close_age_s",
    "kernel_entry_ts_ms",
    "kernel_entry_price",
    "entry_conservative_fill_ts_ms",
    "entry_conservative_gap_s",
    "entry_optimistic_fill_ts_ms",
    "entry_optimistic_gap_s",
    "entry_limit_vs_kernel_bp",
    "entry_note",
    "stop_initial_level",
    "stop_decay_ts_ms",
    "stop_decayed_level",
    "stop_path_source",
    "stop_level",
    "stop_trigger_ts_ms",
    "stop_trigger_source",
    "stop_trigger_vs_kernel_exit_s",
    "stop_walk_avg_price",
    "stop_book_ts_ms",
    "stop_book_age_ms",
    "stop_walk_target_qty",
    "stop_walk_filled_qty",
    "stop_walk_shortfall_bp",
    "stop_walk_filled_fraction",
    "stop_note",
]


def _gap_s(fill_ms: int | None, kernel_ms: int | None) -> float | None:
    if fill_ms is None or kernel_ms is None:
        return None
    return (fill_ms - kernel_ms) / 1000.0


def grade_row(grade: TradeGrade) -> dict[str, Any]:
    spec = grade.spec
    limit_vs_kernel_bp = None
    if grade.limit_price and spec.kernel_entry_price:
        limit_vs_kernel_bp = (spec.kernel_entry_price - grade.limit_price) / grade.limit_price * 1e4
    shortfall_bp = None
    stop_reference_ms = grade.stop_trigger_ms or spec.kernel_exit_ts_ms
    stop_at_reference = grade.effective_stop(stop_reference_ms) if stop_reference_ms else spec.stop_level
    if grade.stop_walk_avg_price is not None and stop_at_reference:
        shortfall_bp = (stop_at_reference - grade.stop_walk_avg_price) / stop_at_reference * 1e4
    return {
        "trade_id": spec.trade_id,
        "symbol": spec.symbol,
        "source": spec.source,
        "evidence_kind": spec.evidence_kind,
        "covered": grade.covered,
        "tape_first_ts_ms": grade.tape_first_ms,
        "tape_last_ts_ms": grade.tape_last_ms,
        "entry_window_bracketed": grade.entry_window_bracketed,
        "stop_observation_bracketed": grade.stop_observation_bracketed,
        "coverage_note": grade.coverage_note,
        "signal_ts_ms": spec.signal_ts_ms,
        "limit_price": grade.limit_price,
        "limit_source": grade.limit_source,
        "signal_close_tape": grade.signal_close_tape,
        "signal_close_age_s": grade.signal_close_age_s,
        "kernel_entry_ts_ms": spec.kernel_entry_ts_ms,
        "kernel_entry_price": spec.kernel_entry_price,
        "entry_conservative_fill_ts_ms": grade.entry_cons_fill_ms,
        "entry_conservative_gap_s": _gap_s(grade.entry_cons_fill_ms, spec.kernel_entry_ts_ms),
        "entry_optimistic_fill_ts_ms": grade.entry_opt_fill_ms,
        "entry_optimistic_gap_s": _gap_s(grade.entry_opt_fill_ms, spec.kernel_entry_ts_ms),
        "entry_limit_vs_kernel_bp": limit_vs_kernel_bp,
        "entry_note": grade.entry_note,
        "stop_initial_level": spec.stop_level,
        "stop_decay_ts_ms": spec.stop_decay_at_ms,
        "stop_decayed_level": spec.stop_decayed_level,
        "stop_path_source": spec.stop_path_source,
        "stop_level": stop_at_reference,
        "stop_trigger_ts_ms": grade.stop_trigger_ms,
        "stop_trigger_source": grade.stop_trigger_source,
        "stop_trigger_vs_kernel_exit_s": _gap_s(grade.stop_trigger_ms, spec.kernel_exit_ts_ms),
        "stop_walk_avg_price": grade.stop_walk_avg_price,
        "stop_book_ts_ms": grade.stop_book_ts_ms,
        "stop_book_age_ms": grade.stop_book_age_ms,
        "stop_walk_target_qty": grade.stop_walk_target_qty,
        "stop_walk_filled_qty": grade.stop_walk_filled_qty,
        "stop_walk_shortfall_bp": shortfall_bp,
        "stop_walk_filled_fraction": grade.stop_walk_filled_fraction,
        "stop_note": grade.stop_note,
    }


def _evidence_kind_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(row["evidence_kind"]) for row in rows)
    return {kind: counts[kind] for kind in ALL_EVIDENCE_KINDS}


def comparison(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Machine-readable population and counts for kernel-vs-tape comparisons."""

    entry_rows = [row for row in rows if bool(row["entry_window_bracketed"]) and row["limit_price"] not in (None, "")]
    stop_rows = [row for row in rows if bool(row["stop_observation_bracketed"]) and row["stop_level"] not in (None, "")]
    any_rows = [row for row in rows if row in entry_rows or row in stop_rows]
    registered_rows = [row for row in any_rows if row["evidence_kind"] == REGISTERED_MODEL_TRADE]
    return {
        "schema_version": 1,
        "population_rule": {
            "entry": "entry_window_bracketed and limit_price present",
            "stop": "stop_observation_bracketed and stop_level present",
            "overlap_only_is_not_graded": True,
        },
        "evidence_kind_counts": {
            "input": _evidence_kind_counts(rows),
            "entry_comparison": _evidence_kind_counts(entry_rows),
            "stop_comparison": _evidence_kind_counts(stop_rows),
            "any_bracketed_comparison": _evidence_kind_counts(any_rows),
        },
        "registered_model_rows_graded": len(registered_rows),
        "entry": {
            "rows": len(entry_rows),
            "conservative_fills": sum(row["entry_conservative_fill_ts_ms"] is not None for row in entry_rows),
            "optimistic_fills": sum(row["entry_optimistic_fill_ts_ms"] is not None for row in entry_rows),
        },
        "stop": {
            "rows": len(stop_rows),
            "triggers": sum(row["stop_trigger_ts_ms"] is not None for row in stop_rows),
            "full_displayed_book_walks": sum(
                row["stop_walk_shortfall_bp"] is not None
                and row["stop_walk_filled_fraction"] is not None
                and row["stop_walk_filled_fraction"] >= 1.0 - 1e-12
                for row in stop_rows
            ),
        },
    }


def footer(grades: list[TradeGrade], counters: Counters) -> str:
    covered = sum(1 for grade in grades if grade.covered)
    entry_bracketed = sum(1 for grade in grades if grade.entry_window_bracketed)
    stop_rows = [grade for grade in grades if grade.spec.stop_level is not None]
    stop_bracketed = sum(1 for grade in stop_rows if grade.stop_observation_bracketed)
    symbols = sorted({grade.spec.symbol for grade in grades})
    return "\n".join(
        [
            "--- coverage confession ---",
            f"input trade rows:                 {len(grades)}",
            f"symbols in trade input:           {len(symbols)} ({', '.join(symbols)})",
            f"rows with any tape overlap:       {covered}",
            f"entry windows bracketed by tape:  {entry_bracketed}/{len(grades)}",
            f"stop observations bracketed:      {stop_bracketed}/{len(stop_rows)}",
            f"rows read:                 {counters.rows_read}",
            f"segments read:             {counters.segments_read}",
            f"sequence gaps hit:         {counters.sequence_gaps}",
            f"invalid timestamp rows:    {counters.invalid_timestamp_rows}",
            f"timestamp regressions:     {counters.timestamp_regressions}",
            f"segments skipped:          {counters.segments_skipped}"
            f" (unreadable), {counters.partial_segments} still-partial not read",
        ]
        + (
            ["no entry window or stop observation is bracketed; this run grades nothing"]
            if entry_bracketed == 0 and stop_bracketed == 0
            else []
        )
    )


def _display_number(value: float | None, decimals: int) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{decimals}f}".rstrip("0").rstrip(".")


def _row_interpretation(row: dict[str, Any]) -> str:
    trade_id = str(row["trade_id"])
    source = str(row["evidence_kind"])
    limit = _display_number(row["limit_price"], 6)
    entry_gap = row["entry_optimistic_gap_s"]
    if entry_gap is None:
        entry = "not filled under either tape bound"
    else:
        direction = "earlier" if entry_gap < 0.0 else "later"
        entry = f"optimistic fill {abs(entry_gap) / 3600.0:.2f}h {direction} than the kernel open"
        conservative_gap = row["entry_conservative_gap_s"]
        if conservative_gap is not None:
            entry += f"; conservative fill {abs(conservative_gap) / 3600.0:.2f}h {direction}"
    kernel_gap = row["entry_limit_vs_kernel_bp"]
    gap = ""
    if kernel_gap is not None:
        gap = f"; kernel open vs limit {kernel_gap:+.2f} bp"
    stop = _display_number(row["stop_level"], 6)
    if row["stop_trigger_ts_ms"] is None:
        stop_text = f"stop {stop} not triggered"
    elif row["stop_walk_shortfall_bp"] is None:
        stop_text = f"stop {stop} triggered; no full fresh-book walk"
    else:
        avg = _display_number(row["stop_walk_avg_price"], 6)
        stop_text = (
            f"stop {stop} triggered; displayed walk averaged {avg} ({row['stop_walk_shortfall_bp']:+.3f} bp shortfall)"
        )
    return f"- `{trade_id}` — {source}, limit {limit}: {entry}{gap}; {stop_text}."


def summary_md(
    rows: list[dict[str, Any]],
    grades: list[TradeGrade],
    counters: Counters,
    notional: float,
    max_book_age_ms: int,
    model_commit: str | None,
) -> str:
    comparison_data = comparison(rows)
    graded = [r for r in rows if r["entry_window_bracketed"] and r["limit_price"] is not None]
    cons = [r for r in graded if r["entry_conservative_fill_ts_ms"] is not None]
    opt = [r for r in graded if r["entry_optimistic_fill_ts_ms"] is not None]
    cons_gaps = [r["entry_conservative_gap_s"] for r in cons if r["entry_conservative_gap_s"] is not None]
    opt_gaps = [r["entry_optimistic_gap_s"] for r in opt if r["entry_optimistic_gap_s"] is not None]
    limit_gap_groups: dict[str, list[float]] = {}
    source_fill_counts: dict[str, list[int]] = {}
    for row in graded:
        source = str(row["limit_source"] or "unknown")
        counts = source_fill_counts.setdefault(source, [0, 0, 0])
        counts[0] += 1
        counts[1] += int(row["entry_conservative_fill_ts_ms"] is not None)
        counts[2] += int(row["entry_optimistic_fill_ts_ms"] is not None)
        gap = row["entry_limit_vs_kernel_bp"]
        if gap is not None:
            limit_gap_groups.setdefault(source, []).append(gap)
    stops = [r for r in rows if r["stop_observation_bracketed"] and r["stop_level"] not in (None, "")]
    triggered = [r for r in stops if r["stop_trigger_ts_ms"] is not None]
    full_walks = [
        r
        for r in triggered
        if r["stop_walk_shortfall_bp"] is not None
        and r["stop_walk_filled_fraction"] is not None
        and r["stop_walk_filled_fraction"] >= 1.0 - 1e-12
    ]
    shortfalls = [r["stop_walk_shortfall_bp"] for r in full_walks]

    def med(values: list[float]) -> str:
        return f"{statistics.median(values):+.1f}" if values else "n/a"

    limit_gap_text = (
        ", ".join(
            f"{source} n={len(values)} median {med(values)}" for source, values in sorted(limit_gap_groups.items())
        )
        or "n/a"
    )
    fill_source_text = (
        ", ".join(
            f"{source} {counts[1]}/{counts[2]}/{counts[0]}" for source, counts in sorted(source_fill_counts.items())
        )
        or "n/a"
    )
    row_interpretations = [_row_interpretation(row) for row in rows]
    if any(row["evidence_kind"] == ARTIFICIAL_EXERCISE for row in rows):
        row_interpretations.append(
            "- Exercise rows test mechanics only. Their fill times, price gaps and book walks do not "
            "validate the registered v12 limit or stop."
        )
    kind_counts = comparison_data["evidence_kind_counts"]

    def kind_count_text(scope: str) -> str:
        return ", ".join(f"{kind}={count}" for kind, count in kind_counts[scope].items())

    lines_out = [
        "# v12 mechanics on tape",
        "",
        "Lane-1 triage diagnostic. It grades the kernel's fill assumptions against",
        "the recorded forward tape; it registers nothing and promotes nothing.",
        "Events are ordered by local receive time, the information available to the process.",
        f"Model kernel commit: {model_commit or 'UNPINNED'}.",
        f"Registered kernel: {_PROFILE.execution_strategy_id}; entry retrace {RETRACE_PCT:.2%}",
        f"within {DEADLINE_HOURS}h, stop {_PROFILE.fc_atr_stop_mult:g}x ATR then",
        f"{_PROFILE.fc_stop_time_decay_atr_mult:g}x ATR after {_PROFILE.fc_stop_time_decay_hours}h.",
        "",
        "## Evidence population",
        f"- input rows by evidence kind: {kind_count_text('input')}",
        f"- entry comparison rows by evidence kind: {kind_count_text('entry_comparison')}",
        f"- stop comparison rows by evidence kind: {kind_count_text('stop_comparison')}",
        f"- registered model rows in any bracketed comparison: {comparison_data['registered_model_rows_graded']}",
        "- tape-derived proxies and artificial exercises are diagnostics, not registered model trades",
        "",
        "## Entry fill honesty",
        f"- fully bracketed rows with an entry limit graded: {len(graded)}",
        f"- conservative fills (a trade printed strictly through the limit): {len(cons)}/{len(graded)}",
        f"- optimistic fills (touch or an at-limit print reached it): {len(opt)}/{len(graded)}",
        f"- fills by limit source, conservative/optimistic/rows: {fill_source_text}",
        f"- median fill-time gap vs the kernel's next-bar-open fill (s): conservative {med(cons_gaps)},"
        f" optimistic {med(opt_gaps)} (negative = tape fills earlier)",
        f"- kernel-entry-vs-limit gap by limit source (bp): {limit_gap_text}"
        " (positive = the kernel's assumed open fill is worse than the limit)",
        "",
        "## Stop fill honesty",
        f"- fully bracketed rows with a stop level: {len(stops)}, triggered on tape: {len(triggered)}",
        f"- full displayed-depth walks no older than {max_book_age_ms} ms: {len(full_walks)}",
    ]
    if shortfalls:
        lines_out.append(
            f"- walk shortfall vs the assumed at-stop fill, {notional:.0f} USDT entry notional (bp):"
            f" median {med(shortfalls)}, worst {max(shortfalls):+.1f}"
        )
    lines_out += [
        "- the walk uses one displayed snapshot; latency, cancellations, impact, hidden size and",
        "  replenishment are absent, so it is not a bound on realized slippage",
        "",
        "## Per-row interpretation",
        *row_interpretations,
        "",
        "## Per-trade rows",
        "`mechanics_per_trade.csv`",
        "",
        "## Coverage confession",
        "```",
        footer(grades, counters),
        "```",
        "",
    ]
    return "\n".join(lines_out)


def provenance(
    *,
    args: argparse.Namespace,
    rows: list[dict[str, Any]],
    counters: Counters,
    kernel: dict[str, Any],
    output_files: list[Path],
) -> dict[str, Any]:
    named_inputs = {
        "model_trades": args.model_trades,
        "live_state": args.live_state,
        "transitions": args.transitions,
    }
    comparison_data = comparison(rows)
    return {
        "schema_version": 2,
        "evidence_label": "lane_1_execution_mechanics_diagnostic",
        "comparison": comparison_data,
        "model_kernel": kernel,
        "kernel_contract": {
            "strategy_id": _PROFILE.execution_strategy_id,
            "entry_retrace_fraction": RETRACE_PCT,
            "entry_deadline_hours": DEADLINE_HOURS,
            "entry_fill": "next hourly open after the observed retrace or deadline bar",
            "initial_stop_atr_multiple": _PROFILE.fc_atr_stop_mult,
            "stop_decay_hours": _PROFILE.fc_stop_time_decay_hours,
            "decayed_stop_atr_multiple": _PROFILE.fc_stop_time_decay_atr_mult,
            "kernel_stop_fill": "stop level when hourly low reaches it",
        },
        "tape_contract": {
            "root": str(args.tape_root.resolve()),
            "event_time_basis": "local_receive_ts_ns",
            "completed_segments_read": [
                _file_identity(Path(path), with_hash=args.hash_tape_inputs) for path in counters.segment_paths
            ],
            "segment_hashes_included": bool(args.hash_tape_inputs),
            "unreadable_segments": counters.skipped_segment_paths,
            "partial_segments_not_read": [
                _file_identity(Path(path), with_hash=False) for path in counters.partial_segment_paths
            ],
        },
        "other_inputs": {
            name: (_file_identity(path) if path is not None else None) for name, path in named_inputs.items()
        },
        "script": _file_identity(Path(__file__)),
        "parameters": {
            "notional_usdt_at_entry": args.notional_usdt,
            "max_book_age_ms": args.max_book_age_ms,
        },
        "scope": {
            "input_rows": len(rows),
            "symbols": sorted({str(row["symbol"]) for row in rows}),
            "evidence_kind_counts": comparison_data["evidence_kind_counts"],
            "registered_model_rows_graded": comparison_data["registered_model_rows_graded"],
            "rows_with_tape_overlap": sum(bool(row["covered"]) for row in rows),
            "entry_windows_bracketed": sum(bool(row["entry_window_bracketed"]) for row in rows),
            "stop_observations_bracketed": sum(bool(row["stop_observation_bracketed"]) for row in rows),
            "tape_rows_read": counters.rows_read,
            "segments_read": counters.segments_read,
            "sequence_gaps": counters.sequence_gaps,
            "invalid_timestamp_rows": counters.invalid_timestamp_rows,
            "timestamp_regressions": counters.timestamp_regressions,
            "segments_skipped": counters.segments_skipped,
            "partial_segments_not_read": counters.partial_segments,
        },
        "outputs": [_file_identity(path) for path in output_files],
        "non_conclusions": [
            "This does not grade strategy returns or alpha.",
            "A displayed static-book walk is not realized stop slippage.",
            "A tape-derived signal close is not the kernel kline close.",
            "Rows absent from the trade input are not graded even when their symbols exist in the tape root.",
        ],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--tape-root", type=Path, required=True)
    result.add_argument("--model-trades", type=Path, help="kernel long_native_trades.csv")
    result.add_argument("--live-state", type=Path, help="producer long-demo-state.json")
    result.add_argument("--transitions", type=Path, help="producer book-transitions jsonl")
    result.add_argument("--notional-usdt", type=float, default=1000.0)
    result.add_argument("--model-commit", help="git commit whose LONG kernel produced the model input")
    result.add_argument(
        "--hash-tape-inputs",
        action="store_true",
        help="SHA-256 every completed tape segment read into the provenance output",
    )
    result.add_argument(
        "--max-book-age-ms",
        type=int,
        default=1_000,
        help="skip stop walks when the last healthy depth-50 update is older than this",
    )
    result.add_argument("--out", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not math.isfinite(args.notional_usdt) or args.notional_usdt <= 0.0:
        print("--notional-usdt must be finite and positive", file=sys.stderr)
        return 2
    if args.max_book_age_ms <= 0:
        print("--max-book-age-ms must be positive", file=sys.stderr)
        return 2
    try:
        kernel = _kernel_identity(args.model_commit)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    specs: list[TradeSpec] = []
    try:
        if args.model_trades:
            specs.extend(load_model_trades(args.model_trades))
        if args.live_state:
            specs.extend(load_live_state(args.live_state))
        if args.transitions:
            specs.extend(load_transitions(args.transitions))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not specs:
        print("no trades: pass --model-trades, --live-state, or --transitions with rows in them", file=sys.stderr)
        return 2

    counters = Counters()
    grades = [TradeGrade(spec=spec) for spec in specs]
    by_symbol: dict[str, list[TradeGrade]] = {}
    for grade in grades:
        by_symbol.setdefault(grade.spec.symbol, []).append(grade)
    for symbol in sorted(by_symbol):
        grade_symbol(
            args.tape_root,
            symbol,
            by_symbol[symbol],
            notional_usdt=args.notional_usdt,
            max_book_age_ms=args.max_book_age_ms,
            counters=counters,
        )
    for grade in grades:
        if not grade.covered:
            note = grade.coverage_note or "no tape for this symbol over the trade's times"
            grade.entry_note = grade.entry_note or note
            grade.stop_note = grade.stop_note or note

    rows = [grade_row(grade) for grade in grades]
    args.out.mkdir(parents=True, exist_ok=True)
    per_trade_path = args.out / "mechanics_per_trade.csv"
    summary_path = args.out / "mechanics_summary.md"
    comparison_path = args.out / "mechanics_comparison.json"
    with per_trade_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    summary_path.write_text(
        summary_md(
            rows,
            grades,
            counters,
            args.notional_usdt,
            args.max_book_age_ms,
            kernel["commit"],
        ),
        encoding="utf-8",
    )
    comparison_path.write_text(json.dumps(comparison(rows), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.out / "mechanics_provenance.json").write_text(
        json.dumps(
            provenance(
                args=args,
                rows=rows,
                counters=counters,
                kernel=kernel,
                output_files=[per_trade_path, summary_path, comparison_path],
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(footer(grades, counters))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
