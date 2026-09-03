#!/usr/bin/env python3
"""One line per minute saying what each engine is worth.

``heartbeat.json`` is rewritten every five seconds and nothing keeps the old
one, so the fleet has no history of its own equity: ``trades.jsonl`` holds
realized round trips and says nothing between them, and a drawdown that never
closed a trade leaves no trace at all. This appends the heartbeat's numbers to
a monthly JSONL file, once a minute, forever. The file is the record; anything
that draws a curve reads it.

A realm with no heartbeat is recorded as ``state=absent`` rather than skipped.
The gap is the fact worth keeping: an engine that was down for two hours has
120 lines saying so, and a curve that simply stops cannot be told from a
recorder that stopped.

Realms and their heartbeat paths come from ``deploy/fleet_manifest.tsv``, so
this cannot drift from the fleet the deploy installs.

With ``METRICS_PUSH_URL``/``_USER``/``_TOKEN`` set, the same sample is also
pushed as InfluxDB line protocol -- one HTTP POST, no daemon, no agent -- to
whatever accepts it (Grafana Cloud's Influx endpoint is what
``docs/observability.md`` sets up). The push is best-effort and never blocks
the local append: the remote is a view, the file is the truth.

Exits 0 unless the state directory itself cannot be written.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = _REPO_ROOT / "deploy" / "fleet_manifest.tsv"
_DEFAULT_STATE_DIR = Path("/var/lib/liquidity-migration/equity")
_PUSH_TIMEOUT_S = 10.0

# One append is one line, well under PIPE_BUF, and this is the only writer.
_MAX_LINE_BYTES = 4096


@dataclass(frozen=True)
class Source:
    """A realm and the artifact the manifest says its engine publishes."""

    realm: str
    kind: str
    path: Path


def read_sources(manifest: Path = _MANIFEST) -> list[Source]:
    """Engine heartbeats and recorder status files, in manifest order."""
    sources: list[Source] = []
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("|")
        if len(fields) != 16:
            raise ValueError(f"fleet manifest row has {len(fields)} fields: {line!r}")
        unit, realm, artifact = fields[0], fields[2], fields[9]
        if artifact == "-":
            continue
        if unit.startswith("liquidity-migration-engine"):
            sources.append(Source(realm=realm, kind="engine", path=Path(artifact)))
        elif unit.startswith("liquidity-migration-forward-capture"):
            name = unit[len("liquidity-migration-forward-capture") :].removesuffix(".service")
            sources.append(Source(realm=name.lstrip("-") or "bybit", kind="recorder", path=Path(artifact)))
    if not sources:
        raise ValueError(f"fleet manifest names no artifacts: {manifest}")
    return sources


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _count(value: Any) -> int:
    return len(value) if isinstance(value, (list, dict)) else 0


def engine_sample(realm: str, path: Path, now_ms: int) -> dict[str, Any]:
    """The heartbeat's numbers, or a line saying why there are none."""
    sample: dict[str, Any] = {"ts_ms": now_ms, "realm": realm, "kind": "engine"}
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {**sample, "state": "absent"}
    except OSError as error:
        return {**sample, "state": "unreadable", "error": str(error)}
    try:
        beat = json.loads(raw)
    except ValueError as error:
        return {**sample, "state": "unparsable", "error": str(error)}
    if not isinstance(beat, dict):
        return {**sample, "state": "unparsable", "error": "heartbeat is not an object"}

    written_ms = _number(beat.get("wall_ts_ms"))
    observed_ms = _number(beat.get("account_observed_wall_ts_ms"))
    raw_positions = beat.get("positions")
    positions: list[Any] = raw_positions if isinstance(raw_positions, list) else []
    notional = 0.0
    sleeves: dict[str, int] = {}
    for position in positions:
        if not isinstance(position, dict):
            continue
        qty = _number(position.get("qty")) or 0.0
        entry = _number(position.get("entry_px")) or 0.0
        notional += abs(qty * entry)
        sleeve = position.get("strategy")
        key = sleeve if isinstance(sleeve, str) and sleeve else "unattributed"
        sleeves[key] = sleeves.get(key, 0) + 1

    sample.update(
        {
            "state": "live",
            "venue": beat.get("venue"),
            "mode": beat.get("mode"),
            "engine_commit": beat.get("engine_commit"),
            "account_user_id": beat.get("account_user_id"),
            "heartbeat_age_ms": None if written_ms is None else round(now_ms - written_ms),
            "account_age_ms": None if observed_ms is None else round(now_ms - observed_ms),
            "equity_usdt": _number(beat.get("account_equity_usdt")),
            "available_usdt": _number(beat.get("account_available_usdt")),
            "position_count": len(positions),
            "position_entry_notional_usdt": round(notional, 8),
            "sleeve_positions": sleeves,
            "may_open": _number(beat.get("may_open")),
            "rolling_loss_net_usdt": _number(beat.get("rolling_loss_net_usdt")),
            "rolling_loss_limit_usdt": _number(beat.get("rolling_loss_limit_usdt")),
            "rolling_loss_tripped": _number(beat.get("rolling_loss_tripped")),
            "entry_blockers": _count(beat.get("entry_blockers")),
            "strategy_errors": _count(beat.get("strategy_errors")),
            "uptime_s": _number(beat.get("uptime_s")),
            "market_events": _number(beat.get("market_events")),
            "orders_sent": _number(beat.get("orders_sent")),
            "fills": _number(beat.get("fills")),
            "stream_resets": _number(beat.get("stream_resets")),
            "venue_clock_offset_ms": _number(beat.get("venue_clock_offset_ms")),
            "fills_maker_share": _number(beat.get("fills_maker_share")),
            "fill_all_in_arrival_bps": _number(beat.get("fill_all_in_arrival_bps")),
            "end_to_end_p50_ns": _number(beat.get("end_to_end_p50_ns")),
            "end_to_end_p99_ns": _number(beat.get("end_to_end_p99_ns")),
        }
    )
    return sample


def recorder_sample(name: str, path: Path, now_ms: int) -> dict[str, Any]:
    """What the tape recorder took in, and how close it is to its allowance."""
    sample: dict[str, Any] = {"ts_ms": now_ms, "realm": name, "kind": "recorder"}
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        state = "absent" if isinstance(error, FileNotFoundError) else "unreadable"
        return {**sample, "state": state, "error": str(error)}
    if not isinstance(status, dict):
        return {**sample, "state": "unreadable", "error": "status is not an object"}
    raw_budget = status.get("budget")
    budget: dict[str, Any] = raw_budget if isinstance(raw_budget, dict) else {}
    recorded_ns = _number(status.get("recorded_at_ns"))
    receive_ns = _number(status.get("last_receive_ns"))
    sample.update(
        {
            "state": "live",
            "venue": status.get("venue"),
            "status_age_ms": None if recorded_ns is None else round(now_ms - recorded_ns / 1e6),
            "receive_age_ms": None if not receive_ns else round(now_ms - receive_ns / 1e6),
            "projected_month_gb": _number(budget.get("projected_month_gb")),
            "monthly_gb": _number(budget.get("monthly_gb")),
            "budget_over": _number(budget.get("over")),
            "shed_feeds": _count(budget.get("shed")),
            "received_frames": _number(status.get("received_frames")),
            "written_rows": _number(status.get("written_rows")),
            "queued_frames": _number(status.get("queued_frames")),
            "dropped_frames": _number(status.get("dropped_frames")),
            "disk_dropped_frames": _number(status.get("disk_dropped_frames")),
            "snapshot_failures": _number(status.get("snapshot_failures")),
            "free_disk_bytes": _number(status.get("free_disk_bytes")),
        }
    )
    return sample


def sample_path(state_dir: Path, sample: dict[str, Any]) -> Path:
    month = time.strftime("%Y-%m", time.gmtime(sample["ts_ms"] / 1000))
    return state_dir / f"{sample['kind']}-{sample['realm']}-{month}.jsonl"


def append(state_dir: Path, sample: dict[str, Any]) -> Path:
    """Append one line. O_APPEND under the line cap is one atomic write."""
    path = sample_path(state_dir, sample)
    line = (json.dumps(sample, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(line) > _MAX_LINE_BYTES:
        raise ValueError(f"sample is {len(line)} bytes, over the {_MAX_LINE_BYTES} byte append cap")
    with path.open("ab") as handle:
        handle.write(line)
    return path


def _escape_tag(value: str) -> str:
    for char, replacement in ((",", r"\,"), ("=", r"\="), (" ", r"\ ")):
        value = value.replace(char, replacement)
    return value


def line_protocol(sample: dict[str, Any]) -> str:
    """One InfluxDB line: `lm_<kind>,tags fields timestamp_ns`.

    Only finite numbers become fields; strings become tags. Every sample
    carries `up`, so a realm with no heartbeat pushes `up=0` rather than
    nothing: a remote that receives nothing cannot tell a dead engine from a
    dead recorder.
    """
    tags = {"realm": str(sample["realm"]), "state": str(sample.get("state", "unknown"))}
    for key in ("venue", "mode"):
        value = sample.get(key)
        if isinstance(value, str) and value:
            tags[key] = value
    fields: dict[str, float] = {"up": 1.0 if sample.get("state") == "live" else 0.0}
    for key, value in sorted(sample.items()):
        if key in {"ts_ms", "realm", "kind", "state", "error"}:
            continue
        if key == "sleeve_positions" and isinstance(value, dict):
            for sleeve, count in sorted(value.items()):
                fields[f"sleeve_{sleeve}_positions"] = float(count)
            continue
        number = _number(value)
        if number is not None and number == number and abs(number) != float("inf"):
            fields[key] = number
    tag_text = ",".join(f"{key}={_escape_tag(value)}" for key, value in sorted(tags.items()))
    field_text = ",".join(f"{key}={value!r}" for key, value in sorted(fields.items()))
    return f"lm_{sample['kind']},{tag_text} {field_text} {int(sample['ts_ms']) * 1_000_000}"


def push(body: str, url: str, user: str, token: str) -> None:
    request = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Authorization": "Basic "
            + base64.b64encode(f"{user}:{token}".encode()).decode("ascii"),
        },
    )
    with urllib.request.urlopen(request, timeout=_PUSH_TIMEOUT_S) as response:  # noqa: S310
        if response.status >= 300:
            raise urllib.error.HTTPError(url, response.status, "rejected", response.headers, None)


def _cell(value: Any, spec: str = ".2f") -> str:
    """A missing number is a dash, never a crash: absent samples have no keys."""
    number = _number(value)
    return "-" if number is None else format(number, spec)


def render_curve(state_dir: Path, realm: str, samples: int) -> str:
    """The curve, on the host, from the file. No remote and no library."""
    paths = sorted(state_dir.glob(f"engine-{realm}-*.jsonl"))
    if not paths:
        return f"no equity samples yet for {realm} in {state_dir}"
    rows: list[dict[str, Any]] = []
    for path in reversed(paths):
        for raw in reversed(path.read_text(encoding="utf-8").splitlines()):
            if not raw.strip():
                continue
            try:
                rows.append(json.loads(raw))
            except ValueError:
                continue
            if len(rows) >= samples:
                break
        if len(rows) >= samples:
            break
    rows.reverse()
    if not rows:
        return f"no equity samples yet for {realm} in {state_dir}"

    values = [row.get("equity_usdt") for row in rows]
    known = [value for value in values if isinstance(value, (int, float))]
    lines = [
        f"realm={realm} samples={len(rows)} "
        f"first={time.strftime('%Y-%m-%d %H:%M', time.gmtime(rows[0]['ts_ms'] / 1000))}Z "
        f"last={time.strftime('%Y-%m-%d %H:%M', time.gmtime(rows[-1]['ts_ms'] / 1000))}Z"
    ]
    if known:
        low, high = min(known), max(known)
        blocks = " ▁▂▃▄▅▆▇█"
        span = high - low
        spark = "".join(
            blocks[0]
            if not isinstance(value, (int, float))
            else blocks[1 + int((value - low) / span * 7)] if span > 0 else blocks[4]
            for value in values
        )
        lines.append(f"equity {low:.2f} .. {high:.2f} USDT  net {known[-1] - known[0]:+.2f}")
        lines.append(spark)
    gaps = sum(1 for row in rows if row.get("state") != "live")
    if gaps:
        lines.append(f"{gaps} of {len(rows)} samples had no live heartbeat")
    lines.append("")
    lines.append(f"{'time':<17}{'equity':>11}{'avail':>11}{'pos':>5}{'open?':>6}  state")
    for row in rows[-20:]:
        stamp = time.strftime("%m-%d %H:%M", time.gmtime(row["ts_ms"] / 1000))
        lines.append(
            f"{stamp:<17}"
            f"{_cell(row.get('equity_usdt')):>11}"
            f"{_cell(row.get('available_usdt')):>11}"
            f"{_cell(row.get('position_count'), '.0f'):>5}"
            f"{'yes' if row.get('may_open') else 'no':>6}  {row.get('state')}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, default=_DEFAULT_STATE_DIR)
    parser.add_argument("--manifest", type=Path, default=_MANIFEST)
    parser.add_argument(
        "--show",
        metavar="REALM",
        help="print the recorded curve for one realm instead of sampling",
    )
    parser.add_argument("--samples", type=int, default=240, help="samples to read for --show")
    args = parser.parse_args(argv)

    if args.show:
        print(render_curve(args.state_dir, args.show, max(1, args.samples)))
        return 0

    args.state_dir.mkdir(parents=True, exist_ok=True)
    now_ms = time.time_ns() // 1_000_000
    lines: list[str] = []
    for source in read_sources(args.manifest):
        if source.kind == "engine":
            sample = engine_sample(source.realm, source.path, now_ms)
        else:
            sample = recorder_sample(source.realm, source.path, now_ms)
        append(args.state_dir, sample)
        lines.append(line_protocol(sample))

    url = os.environ.get("METRICS_PUSH_URL", "").strip()
    user = os.environ.get("METRICS_PUSH_USER", "").strip()
    token = os.environ.get("METRICS_PUSH_TOKEN", "").strip()
    if not (url and user and token):
        print(f"recorded {len(lines)} samples; no metrics sink configured")
        return 0
    try:
        push("\n".join(lines) + "\n", url, user, token)
    except Exception as error:  # noqa: BLE001 - a view must never break the record
        print(f"WARNING: metrics push failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 0
    print(f"recorded and pushed {len(lines)} samples")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
