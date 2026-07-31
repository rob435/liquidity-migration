"""Causal BTC-risk entry-size overlay for the continuous demo book.

``CTRL_BTC_RISK_70_90_35`` sizes entries to 35% after warm-up when the causal
BTC-risk score is in ``[0.70, 0.90)``.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import polars as pl

from liquidity_migration.core._common import MS_PER_DAY
from liquidity_migration.research.backtest.continuous_events import _btc_trend_returns
from liquidity_migration.core.deterministic_serialization import canonical_json

CTRL_BTC_RISK_70_90_35_ID = "CTRL_BTC_RISK_70_90_35"
BTC_RISK_MIN_PRIOR = 50
BTC_RISK_EVIDENCE_SCHEMA_VERSION = 1
BTC_RISK_EVIDENCE_METADATA_KEY = "btc_risk_decision_evidence"
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
    "decision_evidence_json": pl.String,
}

_BTC_RISK_STATE_GENESIS_HASH = hashlib.sha256(
    canonical_json({"schema_version": 1, "state": "btc_risk_genesis"})
).hexdigest()


def _hash_payload(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(payload))).hexdigest()


def _is_sha256(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _next_state_hash(
    predecessor_state_hash: str,
    *,
    decision_key: str,
    symbol: str,
    signal_ts_ms: int,
    raw_values: Mapping[str, float | None],
) -> str:
    return _hash_payload(
        {
            "schema_version": 1,
            "predecessor_state_hash": predecessor_state_hash,
            "decision": {
                "decision_key": decision_key,
                "symbol": symbol,
                "signal_ts_ms": signal_ts_ms,
                "raw_values": {name: raw_values.get(name) for name in BTC_RISK_COMPONENTS},
            },
        }
    )


def _required_int(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"BTC-risk evidence {label} must be an integer >= {minimum}")
    return value


def _required_float(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"BTC-risk evidence {label} must be numeric")
    output = float(value)
    if not math.isfinite(output):
        raise ValueError(f"BTC-risk evidence {label} must be finite")
    return output


def normalize_btc_risk_decision_evidence(
    value: object,
    *,
    expected_arm_id: str | None = None,
    expected_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and normalize one versioned, self-hashed decision receipt.

    Chain and percentile validation require a replay state and are performed by
    the incremental or authoritative reconciliation methods on
    :class:`BtcRiskLiveSizer`.
    """

    if not isinstance(value, Mapping):
        raise ValueError("BTC-risk decision evidence must be an object")
    required_top = {
        "schema_version",
        "arm_id",
        "decision_key",
        "symbol",
        "signal_ts_ms",
        "predecessor_state_hash",
        "result_state_hash",
        "policy",
        "raw_values",
        "result",
        "evidence_hash",
    }
    if set(value) != required_top:
        raise ValueError("BTC-risk decision evidence has an invalid field set")
    schema_version = _required_int(value.get("schema_version"), label="schema_version", minimum=1)
    if schema_version != BTC_RISK_EVIDENCE_SCHEMA_VERSION:
        raise ValueError(f"unsupported BTC-risk evidence schema {schema_version}")
    arm_id = str(value.get("arm_id") or "")
    decision_key = str(value.get("decision_key") or "")
    symbol = str(value.get("symbol") or "").upper()
    signal_ts_ms = _required_int(value.get("signal_ts_ms"), label="signal_ts_ms", minimum=1)
    predecessor_state_hash = str(value.get("predecessor_state_hash") or "")
    result_state_hash = str(value.get("result_state_hash") or "")
    evidence_hash = str(value.get("evidence_hash") or "")
    if not arm_id or not decision_key or not symbol:
        raise ValueError("BTC-risk evidence arm, decision key, and symbol are required")
    if decision_key != f"{symbol}|{signal_ts_ms}":
        raise ValueError("BTC-risk evidence decision key does not match symbol/signal timestamp")
    if expected_arm_id is not None and arm_id != expected_arm_id:
        raise ValueError(f"BTC-risk evidence arm mismatch: expected {expected_arm_id!r}, got {arm_id!r}")
    if not _is_sha256(predecessor_state_hash) or not _is_sha256(result_state_hash) or not _is_sha256(evidence_hash):
        raise ValueError("BTC-risk evidence contains an invalid SHA-256 hash")

    policy_raw = value.get("policy")
    if not isinstance(policy_raw, Mapping) or set(policy_raw) != {"low", "high", "tail_mult", "min_prior"}:
        raise ValueError("BTC-risk evidence policy has an invalid field set")
    policy = {
        "low": _required_float(policy_raw.get("low"), label="policy.low"),
        "high": _required_float(policy_raw.get("high"), label="policy.high"),
        "tail_mult": _required_float(policy_raw.get("tail_mult"), label="policy.tail_mult"),
        "min_prior": _required_int(policy_raw.get("min_prior"), label="policy.min_prior"),
    }
    if not 0.0 <= policy["low"] < policy["high"] <= 1.0 or policy["tail_mult"] <= 0.0:
        raise ValueError("BTC-risk evidence policy bounds/multiplier are invalid")
    if expected_policy is not None:
        normalized_expected = {
            "low": float(expected_policy["low"]),
            "high": float(expected_policy["high"]),
            "tail_mult": float(expected_policy["tail_mult"]),
            "min_prior": int(expected_policy["min_prior"]),
        }
        if policy != normalized_expected:
            raise ValueError("BTC-risk evidence policy does not match the active arm")

    raw_input = value.get("raw_values")
    if not isinstance(raw_input, Mapping) or set(raw_input) != set(BTC_RISK_COMPONENTS):
        raise ValueError("BTC-risk evidence raw values have an invalid field set")
    raw_values: dict[str, float | None] = {}
    for name in BTC_RISK_COMPONENTS:
        raw = raw_input.get(name)
        raw_values[name] = None if raw is None else _required_float(raw, label=f"raw_values.{name}")

    result_input = value.get("result")
    required_result = {
        "prior_decision_count",
        "score_warmup",
        "btc_risk_score",
        "stack_mult",
        "tail_selected",
        "score_component_count",
        "score_missing_component_count",
    }
    if not isinstance(result_input, Mapping) or set(result_input) != required_result:
        raise ValueError("BTC-risk evidence result has an invalid field set")
    score_warmup = result_input.get("score_warmup")
    tail_selected = result_input.get("tail_selected")
    if not isinstance(score_warmup, bool) or not isinstance(tail_selected, bool):
        raise ValueError("BTC-risk evidence warmup/tail flags must be booleans")
    result = {
        "prior_decision_count": _required_int(
            result_input.get("prior_decision_count"), label="result.prior_decision_count"
        ),
        "score_warmup": score_warmup,
        "btc_risk_score": _required_float(result_input.get("btc_risk_score"), label="result.btc_risk_score"),
        "stack_mult": _required_float(result_input.get("stack_mult"), label="result.stack_mult"),
        "tail_selected": tail_selected,
        "score_component_count": _required_int(
            result_input.get("score_component_count"), label="result.score_component_count"
        ),
        "score_missing_component_count": _required_int(
            result_input.get("score_missing_component_count"), label="result.score_missing_component_count"
        ),
    }
    if not 0.0 <= result["btc_risk_score"] <= 1.0 or result["stack_mult"] <= 0.0:
        raise ValueError("BTC-risk evidence score/multiplier is out of range")
    normalized = {
        "schema_version": schema_version,
        "arm_id": arm_id,
        "decision_key": decision_key,
        "symbol": symbol,
        "signal_ts_ms": signal_ts_ms,
        "predecessor_state_hash": predecessor_state_hash,
        "result_state_hash": result_state_hash,
        "policy": policy,
        "raw_values": raw_values,
        "result": result,
    }
    if _hash_payload(normalized) != evidence_hash:
        raise ValueError("BTC-risk decision evidence hash mismatch")
    return {**normalized, "evidence_hash": evidence_hash}


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def percentile_from_prior(prior: list[float], value: float) -> float:
    """Share of prior observations at or below ``value``.

    ``prior`` is maintained SORTED (see ``ExpandingBtcRiskState.score``), so the
    count is a bisect: the authoritative replay calls this once per component
    per receipt, and a linear scan makes reconciliation quadratic in chain
    length.
    """

    if not prior:
        return 0.5
    return bisect.bisect_right(prior, value) / len(prior)


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

    def clone(self) -> ExpandingBtcRiskState:
        cloned = ExpandingBtcRiskState(min_prior=self.min_prior)
        cloned.raw_history = {name: list(values) for name, values in self.raw_history.items()}
        cloned.decision_count = self.decision_count
        cloned.seen_keys = set(self.seen_keys)
        return cloned

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
                # Sorted insert: `percentile_from_prior` bisects, and history
                # order means nothing beyond the empirical distribution.
                bisect.insort(self.raw_history.setdefault(name, []), float(value))
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
        arm_id: str = CTRL_BTC_RISK_70_90_35_ID,
    ) -> None:
        self.state_path = Path(state_path)
        self.low = float(low)
        self.high = float(high)
        self.tail_mult = float(tail_mult)
        self.arm_id = str(arm_id).strip()
        if not self.arm_id:
            raise ValueError("BTC-risk arm_id is required")
        if not 0.0 <= self.low < self.high <= 1.0:
            raise ValueError("BTC-risk score bounds must satisfy 0 <= low < high <= 1")
        if not math.isfinite(self.tail_mult) or self.tail_mult <= 0.0:
            raise ValueError("BTC-risk tail multiplier must be finite and positive")
        if isinstance(min_prior, bool) or int(min_prior) < 0:
            raise ValueError("BTC-risk min_prior must be a non-negative integer")
        self.state = ExpandingBtcRiskState(min_prior=min_prior)
        self._rows: list[dict[str, Any]] = []
        self._state_hash = _BTC_RISK_STATE_GENESIS_HASH
        self._dirty = False
        self._authoritative_reconciliation_error: str | None = None
        self._epoch_orphaned_rows = 0
        self._last_reconciled_signature: str | None = None
        self._last_reconciled_orphaned_links = 0
        self.load()

    @property
    def rows(self) -> int:
        return len(self._rows)

    @property
    def policy(self) -> dict[str, float | int]:
        return {
            "low": self.low,
            "high": self.high,
            "tail_mult": self.tail_mult,
            "min_prior": self.state.min_prior,
        }

    def load(self) -> None:
        if not self.state_path.exists():
            return
        df = pl.read_parquet(self.state_path)
        if df.is_empty():
            return
        self._replace_rows(df.to_dicts())
        self._dirty = False

    def _replace_rows(self, rows: list[dict[str, Any]]) -> None:
        self._rows = []
        self.state = ExpandingBtcRiskState(min_prior=self.state.min_prior)
        self._state_hash = _BTC_RISK_STATE_GENESIS_HASH
        # Parquet row order is the accepted receipt-chain order, so receipts
        # accepted out of signal-time order still replay exactly.
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            signal_ts_ms = int(row.get("signal_ts_ms") or 0)
            decision_key = str(row.get("decision_key") or "")
            if not symbol or signal_ts_ms <= 0 or decision_key != f"{symbol}|{signal_ts_ms}":
                raise ValueError("BTC-risk state row has invalid decision identity")
            evidence_json = row.get("decision_evidence_json")
            if evidence_json is None or str(evidence_json).strip() == "":
                raise ValueError("BTC-risk state row is missing decision evidence")
            try:
                raw_evidence = json.loads(evidence_json) if isinstance(evidence_json, str) else evidence_json
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("BTC-risk state contains unreadable decision evidence") from exc
            evidence = normalize_btc_risk_decision_evidence(raw_evidence)
            if evidence["arm_id"] != self.arm_id or evidence["policy"] != self.policy:
                # A decision from another arm epoch is a prior-epoch orphan,
                # not corruption: drop and count it.
                self._epoch_orphaned_rows += 1
                continue
            raw_values = {name: _finite(row.get(name)) for name in BTC_RISK_COMPONENTS}
            score = self.state.score(decision_key=decision_key, raw_values=raw_values)
            result_state_hash = _next_state_hash(
                self._state_hash,
                decision_key=decision_key,
                symbol=symbol,
                signal_ts_ms=signal_ts_ms,
                raw_values=raw_values,
            )
            if evidence["predecessor_state_hash"] == self._state_hash:
                self._validate_evidence(
                    evidence,
                    decision_key=decision_key,
                    symbol=symbol,
                    signal_ts_ms=signal_ts_ms,
                    raw_values=raw_values,
                    score=score,
                    predecessor_state_hash=self._state_hash,
                    result_state_hash=result_state_hash,
                )
            else:
                self._validate_rebased_evidence(
                    evidence,
                    decision_key=decision_key,
                    symbol=symbol,
                    signal_ts_ms=signal_ts_ms,
                    raw_values=raw_values,
                )
            normalized_row = {
                "decision_key": decision_key,
                "symbol": symbol,
                "signal_ts_ms": signal_ts_ms,
                **raw_values,
                "btc_risk_score": evidence["result"]["btc_risk_score"],
                "stack_mult": evidence["result"]["stack_mult"],
                "score_warmup": evidence["result"]["score_warmup"],
                "decision_evidence_json": canonical_json(evidence).decode("utf-8"),
            }
            self._rows.append(normalized_row)
            self._state_hash = result_state_hash

    def _build_evidence(
        self,
        *,
        decision_key: str,
        symbol: str,
        signal_ts_ms: int,
        raw_values: Mapping[str, float | None],
        score: Mapping[str, Any],
        predecessor_state_hash: str,
        result_state_hash: str,
    ) -> dict[str, Any]:
        risk_score = float(score["btc_risk_score"])
        is_warmup = bool(score["score_warmup"])
        selected = (not is_warmup) and self.low <= risk_score < self.high
        result = {
            "prior_decision_count": int(score["prior_decision_count"]),
            "score_warmup": is_warmup,
            "btc_risk_score": risk_score,
            "stack_mult": self.tail_mult if selected else 1.0,
            "tail_selected": selected,
            "score_component_count": int(score["score_component_count"]),
            "score_missing_component_count": int(score["score_missing_component_count"]),
        }
        payload = {
            "schema_version": BTC_RISK_EVIDENCE_SCHEMA_VERSION,
            "arm_id": self.arm_id,
            "decision_key": decision_key,
            "symbol": symbol,
            "signal_ts_ms": signal_ts_ms,
            "predecessor_state_hash": predecessor_state_hash,
            "result_state_hash": result_state_hash,
            "policy": self.policy,
            "raw_values": {name: raw_values.get(name) for name in BTC_RISK_COMPONENTS},
            "result": result,
        }
        return {**payload, "evidence_hash": _hash_payload(payload)}

    @staticmethod
    def _equal_optional_float(left: object, right: float | None) -> bool:
        left_value = _finite(left)
        if left_value is None or right is None:
            return left_value is None and right is None
        return math.isclose(left_value, right, rel_tol=1e-12, abs_tol=1e-12)

    def _validate_evidence(
        self,
        evidence: Mapping[str, Any],
        *,
        decision_key: str,
        symbol: str,
        signal_ts_ms: int,
        raw_values: Mapping[str, float | None],
        score: Mapping[str, Any],
        predecessor_state_hash: str,
        result_state_hash: str,
    ) -> None:
        if (
            evidence["decision_key"] != decision_key
            or evidence["symbol"] != symbol
            or evidence["signal_ts_ms"] != signal_ts_ms
        ):
            raise ValueError("BTC-risk evidence identity conflicts with the accepted target")
        if evidence["predecessor_state_hash"] != predecessor_state_hash:
            raise ValueError("BTC-risk evidence predecessor is stale or conflicts with accepted state")
        if evidence["result_state_hash"] != result_state_hash:
            raise ValueError("BTC-risk evidence result state hash mismatch")
        for name in BTC_RISK_COMPONENTS:
            if not self._equal_optional_float(evidence["raw_values"].get(name), raw_values.get(name)):
                raise ValueError(f"BTC-risk evidence raw value {name} conflicts with accepted state")
        expected = self._build_evidence(
            decision_key=decision_key,
            symbol=symbol,
            signal_ts_ms=signal_ts_ms,
            raw_values=raw_values,
            score=score,
            predecessor_state_hash=predecessor_state_hash,
            result_state_hash=result_state_hash,
        )
        if evidence != expected:
            raise ValueError("BTC-risk evidence result conflicts with causal replay")

    def _validate_rebased_evidence(
        self,
        evidence: Mapping[str, Any],
        *,
        decision_key: str,
        symbol: str,
        signal_ts_ms: int,
        raw_values: Mapping[str, float | None],
    ) -> None:
        """Validate a receipt rebased across a never-accepted predecessor link.

        The recorded hashes and score live on a proposal branch whose
        predecessor was never accepted, so the result cannot be replayed:
        validation covers identity, raw-value agreement, recorded-link
        integrity, and the receipt's own policy arithmetic.
        """

        if (
            evidence["decision_key"] != decision_key
            or evidence["symbol"] != symbol
            or evidence["signal_ts_ms"] != signal_ts_ms
        ):
            raise ValueError("BTC-risk evidence identity conflicts with the accepted target")
        for name in BTC_RISK_COMPONENTS:
            if not self._equal_optional_float(evidence["raw_values"].get(name), raw_values.get(name)):
                raise ValueError(f"BTC-risk evidence raw value {name} conflicts with accepted state")
        if evidence["result_state_hash"] != _next_state_hash(
            evidence["predecessor_state_hash"],
            decision_key=decision_key,
            symbol=symbol,
            signal_ts_ms=signal_ts_ms,
            raw_values=evidence["raw_values"],
        ):
            raise ValueError("BTC-risk evidence result state hash mismatch")
        policy = evidence["policy"]
        result = evidence["result"]
        selected = (not result["score_warmup"]) and policy["low"] <= result["btc_risk_score"] < policy["high"]
        expected_mult = policy["tail_mult"] if selected else 1.0
        if (
            result["tail_selected"] != selected
            or not math.isclose(result["stack_mult"], expected_mult, rel_tol=1e-12, abs_tol=1e-12)
            or result["score_warmup"] != (result["prior_decision_count"] < policy["min_prior"])
        ):
            raise ValueError("BTC-risk evidence result conflicts with its recorded policy")
        present = sum(1 for name in BTC_RISK_COMPONENTS if evidence["raw_values"].get(name) is not None)
        if (
            result["score_component_count"] != present
            or result["score_missing_component_count"] != len(BTC_RISK_COMPONENTS) - present
        ):
            raise ValueError("BTC-risk evidence component counts conflict with its raw values")

    def _normalize_accepted_evidence_rows(
        self,
        rows: Iterable[Mapping[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], int, int, int]:
        evidence_by_key: dict[str, dict[str, Any]] = {}
        ignored = 0
        epoch_ignored = 0
        duplicate_rows = 0
        for row in rows:
            raw_evidence = row.get(BTC_RISK_EVIDENCE_METADATA_KEY)
            if raw_evidence is None:
                ignored += 1
                continue
            evidence = normalize_btc_risk_decision_evidence(raw_evidence)
            if evidence["arm_id"] != self.arm_id or evidence["policy"] != self.policy:
                # Accepted evidence from another arm epoch is not authority for
                # the active arm. Same-key conflicts apply within one epoch.
                epoch_ignored += 1
                continue
            row_symbol = str(row.get("symbol") or "").upper()
            row_signal_ts = int(row.get("signal_ts_ms") or 0)
            if row_symbol and row_symbol != evidence["symbol"]:
                raise ValueError("accepted BTC-risk evidence symbol conflicts with target row")
            if row_signal_ts and row_signal_ts != evidence["signal_ts_ms"]:
                raise ValueError("accepted BTC-risk evidence timestamp conflicts with target row")
            prior = evidence_by_key.get(evidence["decision_key"])
            if prior is not None:
                if prior["evidence_hash"] != evidence["evidence_hash"]:
                    raise ValueError("accepted BTC-risk decision has conflicting duplicate evidence")
                duplicate_rows += 1
                continue
            evidence_by_key[evidence["decision_key"]] = evidence
        return evidence_by_key, ignored, epoch_ignored, duplicate_rows

    def _persisted_evidence_by_key(self) -> dict[str, dict[str, Any]]:
        return {
            str(row["decision_key"]): normalize_btc_risk_decision_evidence(
                json.loads(str(row["decision_evidence_json"])),
                expected_arm_id=self.arm_id,
                expected_policy=self.policy,
            )
            for row in self._rows
        }

    @staticmethod
    def _state_row_from_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
        raw_values = dict(evidence["raw_values"])
        return {
            "decision_key": evidence["decision_key"],
            "symbol": evidence["symbol"],
            "signal_ts_ms": evidence["signal_ts_ms"],
            **raw_values,
            "btc_risk_score": evidence["result"]["btc_risk_score"],
            "stack_mult": evidence["result"]["stack_mult"],
            "score_warmup": evidence["result"]["score_warmup"],
            "decision_evidence_json": canonical_json(evidence).decode("utf-8"),
        }

    def _replay_evidence_chain(
        self,
        evidence_by_key: Mapping[str, dict[str, Any]],
        *,
        candidate_state: ExpandingBtcRiskState,
        candidate_state_hash: str,
    ) -> tuple[ExpandingBtcRiskState, str, list[dict[str, Any]], int]:
        """Replay accepted receipts as one chain, healing never-accepted links.

        Only accepted proposals reach the authority, so a receipt chained past a
        never-accepted same-cycle proposal records a predecessor no accepted
        evidence can fill. It commits rebased onto the replayed head (counted per
        severed link) and its same-cycle successors follow the recorded branch so
        acceptance order is preserved. A receipt whose recorded predecessor
        matches the replayed head commits strictly; a forked one fails closed.
        """

        replayed_rows: list[dict[str, Any]] = []
        orphaned_links = 0
        linked_state_hash = candidate_state_hash
        remaining = dict(evidence_by_key)
        # Index by predecessor once: rescanning every remaining receipt per
        # iteration makes the replay quadratic in chain length.
        by_predecessor: dict[str, list[str]] = {}
        for key, evidence in remaining.items():
            by_predecessor.setdefault(str(evidence["predecessor_state_hash"]), []).append(key)

        def successors(predecessor_state_hash: str) -> list[dict[str, Any]]:
            keys = [key for key in by_predecessor.get(predecessor_state_hash, ()) if key in remaining]
            by_predecessor[predecessor_state_hash] = keys
            return [remaining[key] for key in keys]

        while remaining:
            rebased = False
            next_receipts = successors(candidate_state_hash)
            if not next_receipts and linked_state_hash != candidate_state_hash:
                next_receipts = successors(linked_state_hash)
                rebased = True
            if len(next_receipts) > 1:
                raise ValueError("accepted BTC-risk evidence contains a forked predecessor")
            if not next_receipts:
                recorded_results = {evidence["result_state_hash"] for evidence in remaining.values()}
                heads = [
                    evidence
                    for evidence in remaining.values()
                    if evidence["predecessor_state_hash"] not in recorded_results
                ]
                if not heads:
                    raise ValueError("accepted BTC-risk evidence has a predecessor gap or stale branch")
                next_receipts = heads[:1]
                orphaned_links += 1
                rebased = True
            evidence = next_receipts[0]
            decision_key = evidence["decision_key"]
            symbol = evidence["symbol"]
            signal_ts_ms = evidence["signal_ts_ms"]
            raw_values = dict(evidence["raw_values"])
            score = candidate_state.score(decision_key=decision_key, raw_values=raw_values)
            result_state_hash = _next_state_hash(
                candidate_state_hash,
                decision_key=decision_key,
                symbol=symbol,
                signal_ts_ms=signal_ts_ms,
                raw_values=raw_values,
            )
            if rebased:
                self._validate_rebased_evidence(
                    evidence,
                    decision_key=decision_key,
                    symbol=symbol,
                    signal_ts_ms=signal_ts_ms,
                    raw_values=raw_values,
                )
            else:
                self._validate_evidence(
                    evidence,
                    decision_key=decision_key,
                    symbol=symbol,
                    signal_ts_ms=signal_ts_ms,
                    raw_values=raw_values,
                    score=score,
                    predecessor_state_hash=candidate_state_hash,
                    result_state_hash=result_state_hash,
                )
            replayed_rows.append(self._state_row_from_evidence(evidence))
            candidate_state_hash = result_state_hash
            linked_state_hash = evidence["result_state_hash"]
            del remaining[decision_key]
        return candidate_state, candidate_state_hash, replayed_rows, orphaned_links

    def _commit_replayed_state(
        self,
        *,
        candidate_state: ExpandingBtcRiskState,
        candidate_state_hash: str,
        candidate_rows: list[dict[str, Any]],
    ) -> None:
        previous = (
            self.state,
            self._state_hash,
            self._rows,
            self._dirty,
        )
        self.state = candidate_state
        self._state_hash = candidate_state_hash
        self._rows = candidate_rows
        self._dirty = True
        try:
            self.save()
        except BaseException:
            (
                self.state,
                self._state_hash,
                self._rows,
                self._dirty,
            ) = previous
            raise

    def score_decisions(
        self,
        decisions: list[dict[str, Any]],
        *,
        btc_context: dict[int, dict[str, float | None]],
    ) -> tuple[dict[tuple[str, int], dict[str, Any]], dict[str, Any]]:
        """Score unique final candidate decisions and return lookup + cycle stats."""

        if self._authoritative_reconciliation_error is not None:
            raise RuntimeError(
                "BTC-risk scoring is blocked after failed authoritative reconciliation: "
                f"{self._authoritative_reconciliation_error}"
            )

        lookup: dict[tuple[str, int], dict[str, Any]] = {}
        unique: dict[tuple[str, int], dict[str, Any]] = {}
        for row in decisions:
            symbol = str(row.get("symbol") or "").upper()
            signal_ts = int(row.get("signal_ts_ms") or row.get("entry_signal_ts_ms") or 0)
            if symbol and signal_ts > 0:
                unique.setdefault((symbol, signal_ts), row)
        scored = 0
        tail_selected = 0
        warmup = 0
        duplicates = 0
        proposal_state = self.state.clone()
        proposal_state_hash = self._state_hash
        existing_by_key = {str(row["decision_key"]): row for row in self._rows}
        for symbol, signal_ts in sorted(unique, key=lambda item: (item[1], item[0])):
            decision_key = f"{symbol}|{signal_ts}"
            if decision_key in proposal_state.seen_keys:
                existing = existing_by_key.get(decision_key)
                if existing is not None:
                    evidence = normalize_btc_risk_decision_evidence(
                        json.loads(str(existing["decision_evidence_json"])),
                        expected_arm_id=self.arm_id,
                        expected_policy=self.policy,
                    )
                    lookup[(symbol, signal_ts)] = {
                        **dict(existing),
                        "tail_selected": evidence["result"]["tail_selected"],
                        "decision_evidence": evidence,
                    }
                duplicates += 1
                continue
            day = (signal_ts // MS_PER_DAY) * MS_PER_DAY
            raw_values = {name: _finite((btc_context.get(day) or {}).get(name)) for name in BTC_RISK_COMPONENTS}
            score = proposal_state.score(decision_key=decision_key, raw_values=raw_values)
            result_state_hash = _next_state_hash(
                proposal_state_hash,
                decision_key=decision_key,
                symbol=symbol,
                signal_ts_ms=signal_ts,
                raw_values=raw_values,
            )
            evidence = self._build_evidence(
                decision_key=decision_key,
                symbol=symbol,
                signal_ts_ms=signal_ts,
                raw_values=raw_values,
                score=score,
                predecessor_state_hash=proposal_state_hash,
                result_state_hash=result_state_hash,
            )
            out = {
                "decision_key": decision_key,
                "symbol": symbol,
                "signal_ts_ms": signal_ts,
                **raw_values,
                **score,
                "stack_mult": evidence["result"]["stack_mult"],
                "tail_selected": evidence["result"]["tail_selected"],
                "decision_evidence": evidence,
            }
            lookup[(symbol, signal_ts)] = out
            proposal_state_hash = result_state_hash
            scored += 1
            tail_selected += int(evidence["result"]["tail_selected"])
            warmup += int(evidence["result"]["score_warmup"])
        return lookup, {
            "scored": scored,
            "duplicates": duplicates,
            "tail_selected": tail_selected,
            "warmup": warmup,
            "state_rows": len(self._rows),
        }

    def reconcile_authoritative_accepted_decisions(
        self,
        rows: Iterable[Mapping[str, Any]],
    ) -> dict[str, int]:
        """Reconcile against a complete canonical set of accepted receipts.

        The supplied evidence is replayed from genesis and treated as the
        entire authority for the active arm epoch. Bookkeeping drift is
        self-healed — never a reason to block entries:

        * persisted decisions missing from the authority, or recorded under
          another arm epoch (policy or arm retune), are prior-epoch orphans
          (ledger reset, migration): the state is rebased onto the replayed
          authoritative chain and the orphans are dropped and counted
          (``orphaned_dropped``);
        * accepted evidence from another arm epoch is ignored and counted
          (``epoch_ignored``);
        * accepted evidence chained past a never-accepted proposal is rebased
          onto the replayed head and counted per severed link
          (``orphaned_links``).

        A same-key same-arm evidence-hash conflict is corruption, not epoch
        drift, and still fails closed until a complete reconciliation
        succeeds.
        """

        self._authoritative_reconciliation_error = "authoritative reconciliation did not complete"
        try:
            evidence_by_key, ignored, epoch_ignored, duplicate_rows = self._normalize_accepted_evidence_rows(rows)
            # Short-circuit an unchanged authority: the replay below re-hashes
            # every receipt from genesis. The signature covers the authoritative
            # evidence AND the persisted state, so real drift still forces the
            # full replay, and the recount below catches anything left to
            # ingest, drop, or rebase.
            signature = self._authority_signature(
                self._persisted_evidence_by_key(),
                evidence_by_key,
                epoch_orphaned_rows=self._epoch_orphaned_rows,
            )
            if self._last_reconciled_signature is not None and signature == self._last_reconciled_signature:
                steady_persisted = self._persisted_evidence_by_key()
                steady_missing = set(steady_persisted) - set(evidence_by_key)
                steady_retained = len(steady_persisted) - len(steady_missing)
                if (
                    not steady_missing
                    and len(evidence_by_key) - steady_retained == 0
                    and self._epoch_orphaned_rows == 0
                ):
                    self._authoritative_reconciliation_error = None
                    return {
                        "ingested": 0,
                        "duplicates": duplicate_rows + steady_retained,
                        "ignored": ignored,
                        "epoch_ignored": epoch_ignored,
                        "orphaned_dropped": 0,
                        "orphaned_links": self._last_reconciled_orphaned_links,
                        "authoritative_rows": len(evidence_by_key),
                    }
            candidate_state, candidate_state_hash, authoritative_rows, orphaned_links = self._replay_evidence_chain(
                evidence_by_key,
                candidate_state=ExpandingBtcRiskState(min_prior=self.state.min_prior),
                candidate_state_hash=_BTC_RISK_STATE_GENESIS_HASH,
            )
            persisted_by_key = self._persisted_evidence_by_key()
            missing_from_authority = sorted(set(persisted_by_key) - set(evidence_by_key))
            for decision_key, persisted in persisted_by_key.items():
                authoritative = evidence_by_key.get(decision_key)
                if authoritative is None:
                    continue
                if persisted["evidence_hash"] != authoritative["evidence_hash"]:
                    raise ValueError(
                        f"authoritative accepted BTC-risk evidence conflicts with persisted decision {decision_key}"
                    )

            epoch_orphaned_rows = self._epoch_orphaned_rows
            retained = len(persisted_by_key) - len(missing_from_authority)
            ingested = len(evidence_by_key) - retained
            if ingested or missing_from_authority or epoch_orphaned_rows:
                self._commit_replayed_state(
                    candidate_state=candidate_state,
                    candidate_state_hash=candidate_state_hash,
                    candidate_rows=authoritative_rows,
                )
        except Exception as exc:
            self._authoritative_reconciliation_error = str(exc)
            raise
        self._authoritative_reconciliation_error = None
        self._epoch_orphaned_rows = 0
        stats = {
            "ingested": ingested,
            "duplicates": duplicate_rows + retained,
            "ignored": ignored,
            "epoch_ignored": epoch_ignored,
            "orphaned_dropped": len(missing_from_authority) + epoch_orphaned_rows,
            "orphaned_links": orphaned_links,
            "authoritative_rows": len(evidence_by_key),
        }
        self._last_reconciled_signature = self._authority_signature(
            self._persisted_evidence_by_key(), evidence_by_key, epoch_orphaned_rows=0
        )
        self._last_reconciled_orphaned_links = orphaned_links
        return stats

    def _authority_signature(
        self,
        persisted_by_key: Mapping[str, Mapping[str, Any]],
        evidence_by_key: Mapping[str, Mapping[str, Any]],
        *,
        epoch_orphaned_rows: int,
    ) -> str:
        """Cheap fingerprint of "which receipts are authoritative, over which state"."""

        material = {
            "arm_id": self.arm_id,
            "epoch_orphaned_rows": int(epoch_orphaned_rows),
            "persisted": sorted(
                f"{key}:{row['evidence_hash']}" for key, row in persisted_by_key.items()
            ),
            "authoritative": sorted(
                f"{key}:{evidence['evidence_hash']}" for key, evidence in evidence_by_key.items()
            ),
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()

    def save(self) -> None:
        if not self._dirty:
            return
        rows = [{name: row.get(name) for name in STATE_SCHEMA} for row in self._rows]
        parent = self.state_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        token = f"{os.getpid()}-{uuid.uuid4().hex}"
        temporary_path = self.state_path.with_name(f".{self.state_path.name}.{token}.tmp")
        backup_path = self.state_path.with_name(f".{self.state_path.name}.{token}.bak")
        prior_exists = self.state_path.exists()
        backup_created = False
        replaced = False
        try:
            pl.DataFrame(rows, schema=STATE_SCHEMA).write_parquet(temporary_path)
            _fsync_file(temporary_path)
            if prior_exists:
                os.link(self.state_path, backup_path)
                backup_created = True
                _fsync_directory(parent)
            os.replace(temporary_path, self.state_path)
            replaced = True
            _fsync_directory(parent)
        except BaseException:
            rollback_error: BaseException | None = None
            if replaced:
                try:
                    if backup_created:
                        os.replace(backup_path, self.state_path)
                        backup_created = False
                    else:
                        self.state_path.unlink(missing_ok=True)
                    _fsync_directory(parent)
                except BaseException as exc:
                    rollback_error = exc
            _unlink_quietly(temporary_path)
            if backup_created and (not replaced or rollback_error is None):
                _unlink_quietly(backup_path)
            if rollback_error is not None:
                raise RuntimeError(
                    "BTC-risk state save failed and rollback could not be confirmed; "
                    f"prior state backup is {backup_path}"
                ) from rollback_error
            raise
        else:
            _unlink_quietly(backup_path)
            if prior_exists:
                # The replacement is already durable; a failed fsync of the
                # redundant backup's removal must not roll it back.
                try:
                    _fsync_directory(parent)
                except OSError:
                    pass
            self._dirty = False
        finally:
            _unlink_quietly(temporary_path)
