from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from .config import CostConfig, PortfolioConfig, SignalConfig
from .research import attach_forward_returns
from .storage import read_dataset, write_dataset


@dataclass(frozen=True, slots=True)
class PortfolioScenarioResult:
    scenario: str
    total_return: float
    mean_period_return: float
    volatility: float
    sharpe_like: float
    max_drawdown: float
    average_gross: float
    average_net: float
    turnover: float
    gross_alpha_pnl: float
    long_pnl: float
    short_pnl: float
    funding_pnl: float
    fee_pnl: float
    slippage_pnl: float
    total_cost_pnl: float
    selected_missing_forward_returns: int
    fees_share_gross_alpha: float
    max_symbol_pnl_share: float


@dataclass(frozen=True, slots=True)
class ScenarioRun:
    summary: PortfolioScenarioResult
    periods: list[dict[str, Any]]
    positions: list[dict[str, Any]]
    symbol_attribution: list[dict[str, Any]]
    monthly_attribution: list[dict[str, Any]]


def run_portfolio_backtest(
    data_root: str | Path,
    *,
    report_dir: str | Path | None = None,
    portfolio_config: PortfolioConfig | None = None,
    signal_config: SignalConfig | None = None,
    cost_config: CostConfig | None = None,
) -> dict[str, Any]:
    features = read_dataset(data_root, "features_1h")
    klines = read_dataset(data_root, "klines_1h")
    funding = read_dataset(data_root, "funding")
    if features.is_empty():
        raise RuntimeError("features_1h is empty; run build-features first")

    with_returns = attach_forward_returns(features, klines, horizons_h=(4,))
    with_returns = attach_latest_funding(with_returns, funding)
    scenario_runs = [
        run_detailed_cost_scenario(
            with_returns,
            scenario=name,
            cost_multiplier=multiplier,
            portfolio_config=portfolio_config,
            signal_config=signal_config,
            cost_config=cost_config,
            all_taker=all_taker,
        )
        for name, multiplier, all_taker in (
            ("base", 1.0, False),
            ("2x_costs", 2.0, False),
            ("3x_costs", 3.0, False),
            ("all_taker", 1.0, True),
        )
    ]

    summaries = [asdict(run.summary) for run in scenario_runs]
    periods = [row for run in scenario_runs for row in run.periods]
    positions = [row for run in scenario_runs for row in run.positions]
    symbols = [row for run in scenario_runs for row in run.symbol_attribution]
    months = [row for run in scenario_runs for row in run.monthly_attribution]
    robustness = compute_robustness(summaries, periods, positions, symbols)
    payload = {
        "scenarios": summaries,
        "periods": periods,
        "positions": positions,
        "symbol_attribution": symbols,
        "monthly_attribution": months,
        "robustness": robustness,
        "acceptance_gates": compute_acceptance_gates(summaries, robustness),
        "rows": with_returns.height,
    }

    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "portfolio_backtest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "portfolio_backtest.md").write_text(format_portfolio_report(payload), encoding="utf-8")
    write_dataset(pl.DataFrame(summaries), data_root, "portfolio_backtest", partition_by=())
    if periods:
        write_dataset(pl.DataFrame(periods), data_root, "portfolio_periods", partition_by=("scenario",))
    if positions:
        write_dataset(pl.DataFrame(positions), data_root, "portfolio_positions", partition_by=("scenario", "symbol"))
    if symbols:
        write_dataset(pl.DataFrame(symbols), data_root, "portfolio_symbol_attribution", partition_by=("scenario",))
    if months:
        write_dataset(pl.DataFrame(months), data_root, "portfolio_monthly_attribution", partition_by=("scenario",))
    return payload


def run_cost_scenario(
    df: pl.DataFrame,
    *,
    scenario: str,
    cost_multiplier: float,
    portfolio_config: PortfolioConfig | None = None,
    signal_config: SignalConfig | None = None,
    cost_config: CostConfig | None = None,
    all_taker: bool = False,
) -> PortfolioScenarioResult:
    return run_detailed_cost_scenario(
        df,
        scenario=scenario,
        cost_multiplier=cost_multiplier,
        portfolio_config=portfolio_config,
        signal_config=signal_config,
        cost_config=cost_config,
        all_taker=all_taker,
    ).summary


def run_detailed_cost_scenario(
    df: pl.DataFrame,
    *,
    scenario: str,
    cost_multiplier: float,
    portfolio_config: PortfolioConfig | None = None,
    signal_config: SignalConfig | None = None,
    cost_config: CostConfig | None = None,
    all_taker: bool = False,
) -> ScenarioRun:
    portfolio = portfolio_config or PortfolioConfig()
    signals = signal_config or SignalConfig()
    costs = cost_config or CostConfig()
    cost_rates = _one_way_cost_rates(costs, cost_multiplier=cost_multiplier, all_taker=all_taker)

    prev_weights: dict[str, float] = {}
    period_returns: list[float] = []
    gross_values: list[float] = []
    net_values: list[float] = []
    periods: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    symbol_totals: dict[str, dict[str, float]] = {}
    selected_missing_forward_returns = 0
    turnover_total = 0.0
    equity = 1.0
    peak_equity = 1.0

    for part in df.sort(["ts_ms", "symbol"]).partition_by("ts_ms", maintain_order=True):
        ts_ms = int(part["ts_ms"][0])
        if datetime_hour(ts_ms) % portfolio.rebalance_interval_h != 0:
            continue
        rows = [row for row in part.to_dicts() if _finite(row.get("composite_score"))]
        if len(rows) < 4:
            continue

        weights = _target_weights(rows, portfolio=portfolio, signals=signals)
        row_by_symbol = {str(row["symbol"]): row for row in rows}
        ledger_symbols = sorted(set(prev_weights) | set(weights))
        turnover_by_symbol = {
            symbol: abs(weights.get(symbol, 0.0) - prev_weights.get(symbol, 0.0))
            for symbol in ledger_symbols
        }
        turnover = sum(turnover_by_symbol.values())
        gross = sum(abs(value) for value in weights.values())
        net = sum(weights.values())
        gross_values.append(gross)
        net_values.append(net)
        turnover_total += turnover

        period_long_price = 0.0
        period_short_price = 0.0
        period_funding = 0.0
        period_fee = 0.0
        period_slippage = 0.0
        period_missing = 0
        period_position_rows: list[dict[str, Any]] = []

        for symbol in ledger_symbols:
            prev_weight = prev_weights.get(symbol, 0.0)
            target_weight = weights.get(symbol, 0.0)
            trade_weight = target_weight - prev_weight
            row = row_by_symbol.get(symbol)
            log_return = row.get("forward_return_4h") if row is not None else None
            if target_weight != 0 and _finite(log_return):
                price_return = math.exp(float(log_return)) - 1.0
            elif target_weight != 0:
                selected_missing_forward_returns += 1
                period_missing += 1
                price_return = 0.0
            else:
                price_return = 0.0

            price_pnl = target_weight * price_return
            funding_rate = row.get("funding_rate_8h_equiv") if row is not None else None
            funding_pnl = (
                -target_weight * float(funding_rate) * (portfolio.rebalance_interval_h / 8.0)
                if target_weight != 0 and _finite(funding_rate)
                else 0.0
            )
            fee_pnl = -turnover_by_symbol[symbol] * cost_rates["fee"]
            slippage_pnl = -turnover_by_symbol[symbol] * (cost_rates["slippage"] + cost_rates["adverse_selection"])
            total_pnl = price_pnl + funding_pnl + fee_pnl + slippage_pnl

            if target_weight > 0:
                period_long_price += price_pnl
            elif target_weight < 0:
                period_short_price += price_pnl
            period_funding += funding_pnl
            period_fee += fee_pnl
            period_slippage += slippage_pnl

            side = "long" if target_weight > 0 else "short" if target_weight < 0 else "flat"
            position_row = {
                "scenario": scenario,
                "ts_ms": ts_ms,
                "month": _month(ts_ms),
                "symbol": symbol,
                "prev_weight": prev_weight,
                "target_weight": target_weight,
                "trade_weight": trade_weight,
                "side": side,
                "gross": gross,
                "net": net,
                "price_return": price_return,
                "price_pnl": price_pnl,
                "funding_pnl": funding_pnl,
                "fee_pnl": fee_pnl,
                "slippage_pnl": slippage_pnl,
                "total_pnl": total_pnl,
                "missing_forward_return": target_weight != 0 and not _finite(log_return),
            }
            period_position_rows.append(position_row)

            totals = symbol_totals.setdefault(
                symbol,
                {
                    "price_pnl": 0.0,
                    "long_price_pnl": 0.0,
                    "short_price_pnl": 0.0,
                    "funding_pnl": 0.0,
                    "fee_pnl": 0.0,
                    "slippage_pnl": 0.0,
                    "net_pnl": 0.0,
                    "periods": 0.0,
                    "max_abs_weight": 0.0,
                },
            )
            totals["price_pnl"] += price_pnl
            totals["long_price_pnl"] += price_pnl if target_weight > 0 else 0.0
            totals["short_price_pnl"] += price_pnl if target_weight < 0 else 0.0
            totals["funding_pnl"] += funding_pnl
            totals["fee_pnl"] += fee_pnl
            totals["slippage_pnl"] += slippage_pnl
            totals["net_pnl"] += total_pnl
            totals["periods"] += 1.0
            totals["max_abs_weight"] = max(totals["max_abs_weight"], abs(target_weight))

        period_return = period_long_price + period_short_price + period_funding + period_fee + period_slippage
        equity *= 1.0 + period_return
        peak_equity = max(peak_equity, equity)
        drawdown = equity / peak_equity - 1.0
        for row in period_position_rows:
            row["equity"] = equity
            row["drawdown"] = drawdown
        positions.extend(period_position_rows)
        periods.append(
            {
                "scenario": scenario,
                "ts_ms": ts_ms,
                "month": _month(ts_ms),
                "period_return": period_return,
                "equity": equity,
                "drawdown": drawdown,
                "gross": gross,
                "net": net,
                "turnover": turnover,
                "long_pnl": period_long_price,
                "short_pnl": period_short_price,
                "gross_alpha_pnl": period_long_price + period_short_price + period_funding,
                "funding_pnl": period_funding,
                "fee_pnl": period_fee,
                "slippage_pnl": period_slippage,
                "missing_forward_returns": period_missing,
            }
        )
        period_returns.append(period_return)
        prev_weights = weights

    returns = np.asarray(period_returns, dtype=float)
    total_return = float(equity - 1.0)
    vol = float(np.std(returns, ddof=1)) if returns.size > 1 else 0.0
    mean_period = float(np.mean(returns)) if returns.size else 0.0
    sharpe_like = float(mean_period / vol * math.sqrt(365 * 6)) if vol > 1e-12 else 0.0
    max_dd = min((float(row["drawdown"]) for row in periods), default=0.0)
    symbol_attribution = _symbol_attribution(scenario, symbol_totals, total_return)
    monthly_attribution = _monthly_attribution(scenario, periods)
    long_price = sum(float(row["long_pnl"]) for row in periods)
    short_price = sum(float(row["short_pnl"]) for row in periods)
    funding_pnl = sum(float(row["funding_pnl"]) for row in periods)
    fee_pnl = sum(float(row["fee_pnl"]) for row in periods)
    slippage_pnl = sum(float(row["slippage_pnl"]) for row in periods)
    gross_alpha = long_price + short_price + funding_pnl
    fees_share = abs(fee_pnl) / gross_alpha if gross_alpha > 1e-12 else float("nan")
    summary = PortfolioScenarioResult(
        scenario=scenario,
        total_return=total_return,
        mean_period_return=mean_period,
        volatility=vol,
        sharpe_like=sharpe_like,
        max_drawdown=max_dd,
        average_gross=float(np.mean(gross_values)) if gross_values else 0.0,
        average_net=float(np.mean(net_values)) if net_values else 0.0,
        turnover=turnover_total,
        gross_alpha_pnl=gross_alpha,
        long_pnl=long_price,
        short_pnl=short_price,
        funding_pnl=funding_pnl,
        fee_pnl=fee_pnl,
        slippage_pnl=slippage_pnl,
        total_cost_pnl=fee_pnl + slippage_pnl,
        selected_missing_forward_returns=selected_missing_forward_returns,
        fees_share_gross_alpha=fees_share,
        max_symbol_pnl_share=_max_symbol_share(symbol_attribution, total_return),
    )
    return ScenarioRun(
        summary=summary,
        periods=periods,
        positions=positions,
        symbol_attribution=symbol_attribution,
        monthly_attribution=monthly_attribution,
    )


def _one_way_cost_rates(
    costs: CostConfig,
    *,
    cost_multiplier: float,
    all_taker: bool,
) -> dict[str, float]:
    if all_taker:
        fee_bps = costs.taker_fee_bps
        slippage_bps = costs.taker_slippage_bps_liquid
        adverse_bps = 0.0
    else:
        maker_prob = min(max(costs.maker_fill_probability, 0.0), 1.0)
        fee_bps = maker_prob * costs.maker_fee_bps + (1.0 - maker_prob) * costs.taker_fee_bps
        slippage_bps = (1.0 - maker_prob) * costs.taker_slippage_bps_liquid
        adverse_bps = maker_prob * costs.maker_adverse_selection_bps
    return {
        "fee": cost_multiplier * fee_bps / 10_000.0,
        "slippage": cost_multiplier * slippage_bps / 10_000.0,
        "adverse_selection": cost_multiplier * adverse_bps / 10_000.0,
    }


def format_portfolio_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Aggression-Carry Portfolio Backtest",
        "",
        f"Input rows: {payload['rows']}",
        "",
        "## Scenario Summary",
        "",
        "| Scenario | Total return | Sharpe-like | Max DD | Avg gross | Avg net | Gross alpha | Long price | Short price | Funding | Fees | Slippage | Fee/Gross | Max Symbol | Missing returns |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in payload["scenarios"]:
        lines.append(
            f"| {item['scenario']} | {item['total_return']:.4f} | {item['sharpe_like']:.2f} | "
            f"{item['max_drawdown']:.4f} | {item['average_gross']:.3f} | {item['average_net']:.3f} | "
            f"{item['gross_alpha_pnl']:.4f} | {item['long_pnl']:.4f} | {item['short_pnl']:.4f} | "
            f"{item['funding_pnl']:.4f} | {item['fee_pnl']:.4f} | {item['slippage_pnl']:.4f} | "
            f"{item['fees_share_gross_alpha']:.2%} | {item['max_symbol_pnl_share']:.2%} | "
            f"{item['selected_missing_forward_returns']} |"
        )
    lines.extend(
        [
            "",
            "## Robustness",
            "",
            "| Check | Value |",
            "|---|---:|",
        ]
    )
    for key, value in payload.get("robustness", {}).items():
        if isinstance(value, float):
            lines.append(f"| {key} | {value:.6f} |")
        else:
            lines.append(f"| {key} | {value} |")
    lines.extend(
        [
            "",
            "## Acceptance Gates",
            "",
            "| Gate | Passed | Value |",
            "|---|---:|---:|",
        ]
    )
    for item in payload.get("acceptance_gates", []):
        value = item.get("value")
        formatted = f"{value:.6f}" if isinstance(value, float) else str(value)
        lines.append(f"| {item['gate']} | {item['passed']} | {formatted} |")
    lines.append("")
    return "\n".join(lines)


def compute_robustness(
    summaries: list[dict[str, Any]],
    periods: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    symbol_attribution: list[dict[str, Any]],
) -> dict[str, Any]:
    base = _find_scenario(summaries, "base")
    two_x = _find_scenario(summaries, "2x_costs")
    base_periods = [row for row in periods if row["scenario"] == "base"]
    base_positions = [row for row in positions if row["scenario"] == "base"]
    base_symbols = [row for row in symbol_attribution if row["scenario"] == "base"]
    excluded_symbols = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    month_returns = _period_returns_by_month(base_periods)
    best_month = max(month_returns, key=month_returns.get) if month_returns else None
    return {
        "base_total_return": float(base.get("total_return", 0.0)),
        "two_x_total_return": float(two_x.get("total_return", 0.0)),
        "long_price_pnl": float(base.get("long_pnl", 0.0)),
        "short_price_pnl": float(base.get("short_pnl", 0.0)),
        "fees_share_gross_alpha": float(base.get("fees_share_gross_alpha", float("nan"))),
        "max_symbol_total_pnl_share": max(
            (float(row.get("total_pnl_share", 0.0)) for row in base_symbols),
            default=float("nan"),
        ),
        "max_symbol_abs_pnl_share": max(
            (float(row.get("abs_pnl_share", 0.0)) for row in base_symbols),
            default=float("nan"),
        ),
        "return_excluding_btc_eth_sol": _compound_periods_from_positions(
            [row for row in base_positions if row["symbol"] not in excluded_symbols]
        ),
        "best_month": best_month,
        "return_excluding_best_month": _compound_returns(
            [float(row["period_return"]) for row in base_periods if row["month"] != best_month]
        ),
    }


def compute_acceptance_gates(
    summaries: list[dict[str, Any]],
    robustness: dict[str, Any],
) -> list[dict[str, Any]]:
    base = _find_scenario(summaries, "base")
    two_x = _find_scenario(summaries, "2x_costs")
    return [
        {
            "gate": "profitable_under_2x_costs",
            "passed": bool(float(two_x.get("total_return", 0.0)) > 0),
            "value": float(two_x.get("total_return", 0.0)),
        },
        {
            "gate": "long_book_contributes",
            "passed": bool(float(base.get("long_pnl", 0.0)) > 0),
            "value": float(base.get("long_pnl", 0.0)),
        },
        {
            "gate": "short_book_contributes",
            "passed": bool(float(base.get("short_pnl", 0.0)) > 0),
            "value": float(base.get("short_pnl", 0.0)),
        },
        {
            "gate": "fees_below_40pct_gross_alpha",
            "passed": bool(float(base.get("fees_share_gross_alpha", float("inf"))) < 0.40),
            "value": float(base.get("fees_share_gross_alpha", float("nan"))),
        },
        {
            "gate": "no_symbol_over_20pct_total_pnl",
            "passed": bool(float(robustness.get("max_symbol_total_pnl_share", 1.0)) <= 0.20),
            "value": float(robustness.get("max_symbol_total_pnl_share", float("nan"))),
        },
        {
            "gate": "survives_excluding_btc_eth_sol",
            "passed": bool(float(robustness.get("return_excluding_btc_eth_sol", 0.0)) > 0),
            "value": float(robustness.get("return_excluding_btc_eth_sol", 0.0)),
        },
        {
            "gate": "survives_excluding_best_month",
            "passed": bool(float(robustness.get("return_excluding_best_month", 0.0)) > 0),
            "value": float(robustness.get("return_excluding_best_month", 0.0)),
        },
    ]


def _target_weights(rows: list[dict[str, Any]], *, portfolio: PortfolioConfig, signals: SignalConfig) -> dict[str, float]:
    sorted_rows = sorted(rows, key=lambda row: float(row["composite_score"]))
    long_bucket = max(1, int(math.ceil(len(sorted_rows) * signals.long_quantile)))
    short_bucket = max(1, int(math.ceil(len(sorted_rows) * signals.short_quantile)))
    shorts = [row for row in sorted_rows[:short_bucket] if abs(float(row["composite_score"])) >= signals.min_abs_score_entry]
    longs = [row for row in sorted_rows[-long_bucket:] if abs(float(row["composite_score"])) >= signals.min_abs_score_entry]
    weights: dict[str, float] = {}
    side_gross = portfolio.base_gross_exposure / 2.0
    if longs:
        denom = sum(abs(float(row["composite_score"])) for row in longs)
        for row in longs:
            raw = side_gross * abs(float(row["composite_score"])) / max(denom, 1e-12)
            weights[str(row["symbol"])] = min(raw, portfolio.max_single_position_weight)
    if shorts:
        denom = sum(abs(float(row["composite_score"])) for row in shorts)
        for row in shorts:
            raw = side_gross * abs(float(row["composite_score"])) / max(denom, 1e-12)
            weights[str(row["symbol"])] = -min(raw, portfolio.max_single_position_weight)
    net = sum(weights.values())
    if abs(net) > portfolio.max_net_exposure_abs and weights:
        excess = abs(net) - portfolio.max_net_exposure_abs
        if net > 0:
            long_total = sum(weight for weight in weights.values() if weight > 0)
            factor = max(0.0, (long_total - excess) / long_total) if long_total > 0 else 0.0
            weights = {symbol: (weight * factor if weight > 0 else weight) for symbol, weight in weights.items()}
        else:
            short_total = -sum(weight for weight in weights.values() if weight < 0)
            factor = max(0.0, (short_total - excess) / short_total) if short_total > 0 else 0.0
            weights = {symbol: (weight * factor if weight < 0 else weight) for symbol, weight in weights.items()}
    return weights


def attach_latest_funding(features: pl.DataFrame, funding: pl.DataFrame) -> pl.DataFrame:
    if features.is_empty():
        return features
    if funding.is_empty():
        return features.with_columns(pl.lit(None, dtype=pl.Float64).alias("funding_rate_8h_equiv"))
    funding_rows: dict[str, list[dict[str, Any]]] = {}
    for key, part in funding.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = key[0] if isinstance(key, tuple) else key
        funding_rows[str(symbol)] = part.to_dicts()
    output = []
    for key, part in features.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        rows = funding_rows.get(symbol, [])
        cursor = 0
        latest = None
        for row in part.to_dicts():
            ts_ms = int(row["ts_ms"])
            while cursor < len(rows) and int(rows[cursor]["ts_ms"]) <= ts_ms:
                latest = rows[cursor].get("funding_rate_8h_equiv", rows[cursor].get("funding_rate"))
                cursor += 1
            item = dict(row)
            item["funding_rate_8h_equiv"] = float(latest) if latest is not None else None
            output.append(item)
    return pl.DataFrame(output).sort(["ts_ms", "symbol"])


def _symbol_attribution(
    scenario: str,
    symbol_totals: dict[str, dict[str, float]],
    total_return: float,
) -> list[dict[str, Any]]:
    total_abs = sum(abs(values["net_pnl"]) for values in symbol_totals.values())
    rows = []
    for symbol, values in sorted(symbol_totals.items()):
        rows.append(
            {
                "scenario": scenario,
                "symbol": symbol,
                **values,
                "abs_pnl_share": abs(values["net_pnl"]) / total_abs if total_abs > 1e-12 else float("nan"),
                "total_pnl_share": abs(values["net_pnl"]) / abs(total_return) if abs(total_return) > 1e-12 else float("nan"),
            }
        )
    return rows


def _monthly_attribution(scenario: str, periods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in periods:
        grouped.setdefault(str(row["month"]), []).append(row)
    output = []
    for month, rows in sorted(grouped.items()):
        output.append(
            {
                "scenario": scenario,
                "month": month,
                "return": _compound_returns([float(row["period_return"]) for row in rows]),
                "long_pnl": sum(float(row["long_pnl"]) for row in rows),
                "short_pnl": sum(float(row["short_pnl"]) for row in rows),
                "gross_alpha_pnl": sum(float(row["gross_alpha_pnl"]) for row in rows),
                "funding_pnl": sum(float(row["funding_pnl"]) for row in rows),
                "fee_pnl": sum(float(row["fee_pnl"]) for row in rows),
                "slippage_pnl": sum(float(row["slippage_pnl"]) for row in rows),
                "turnover": sum(float(row["turnover"]) for row in rows),
            }
        )
    return output


def _max_symbol_share(symbol_attribution: list[dict[str, Any]], total_return: float) -> float:
    if abs(total_return) <= 1e-12:
        return float("nan")
    return max((float(row["total_pnl_share"]) for row in symbol_attribution), default=0.0)


def _find_scenario(summaries: list[dict[str, Any]], scenario: str) -> dict[str, Any]:
    return next((item for item in summaries if item["scenario"] == scenario), {})


def _period_returns_by_month(periods: list[dict[str, Any]]) -> dict[str, float]:
    grouped: dict[str, list[float]] = {}
    for row in periods:
        grouped.setdefault(str(row["month"]), []).append(float(row["period_return"]))
    return {month: _compound_returns(values) for month, values in grouped.items()}


def _compound_returns(returns: list[float]) -> float:
    if not returns:
        return 0.0
    return float(np.prod(1.0 + np.asarray(returns, dtype=float)) - 1.0)


def _compound_periods_from_positions(positions: list[dict[str, Any]]) -> float:
    grouped: dict[int, float] = {}
    for row in positions:
        grouped[int(row["ts_ms"])] = grouped.get(int(row["ts_ms"]), 0.0) + float(row["total_pnl"])
    return _compound_returns([grouped[ts_ms] for ts_ms in sorted(grouped)])


def _month(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m")


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def datetime_hour(ts_ms: int) -> int:
    return int((ts_ms // (60 * 60 * 1000)) % 24)
