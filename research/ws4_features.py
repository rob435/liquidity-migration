"""WS-4 — higher-IC feature search.

For each candidate feature, the standalone per-day cross-sectional IC vs the
3d-forward short return, measured on IS-train (methodology: IC before P&L).
A feature earns consideration only with a stable, significant train IC AND a
coherent a-priori short-the-exhausted-pump rationale (written below).

DATA LIMITATION (documented deviation): the plan's marquee WS-4 features —
funding rate, open-interest surge, taker buy/sell imbalance — need the funding /
open_interest / taker datasets, which the freshly-rebuilt PIT roots do NOT
contain (the rebuild fetched klines only; the OOS roots never had them). This
script therefore covers only klines-derived candidates. Funding/OI/taker remain
an open WS-4 item contingent on acquiring those datasets.

Candidates (sign = larger value should mean MORE short-worthy):
  signal_day_last6h_return     pump still accelerating into the close
  signal_day_range_pct         blow-off intraday range = climax/exhaustion
  intraday_range_expansion_7d  range expansion vs prior 7d = volatility climax
  daily_intraday_return_1d     size of the pump day's own open->close move
  return_7d                    cumulative 7d run-up = more to mean-revert
  close_vs_prior20_high        closed at/through the 20d high = breakout extension
  event_uniqueness_score       crowding / how unusual the event is
  prior20_drawdown             pre-pump drawdown (context)
  + the 4 existing reversion_alpha components, for reference.

Usage:  python -m research.ws4_features <window>   (default is_train)
"""
from __future__ import annotations

import sys

from research._panel import load_scored
from liquidity_migration.ic_diagnostic import add_forward_short_returns, cross_sectional_ic

CANDIDATES = [
    "signal_day_last6h_return",
    "signal_day_range_pct",
    "intraday_range_expansion_7d",
    "daily_intraday_return_1d",
    "return_7d",
    "close_vs_prior20_high",
    "event_uniqueness_score",
    "prior20_drawdown",
    # existing components, for reference
    "z_rank_jump", "z_residual_return", "z_turnover_ratio", "z_close_location",
    "reversion_score",
]


def main() -> None:
    window = sys.argv[1] if len(sys.argv) > 1 else "is_train"
    scored, klines = load_scored(window)
    print(f"WS-4 feature IC — window={window}  scored rows={scored.height}")

    present = [c for c in CANDIDATES if c in scored.columns]
    missing = [c for c in CANDIDATES if c not in scored.columns]
    if missing:
        print(f"  (absent from panel: {missing})")

    panel = add_forward_short_returns(scored, klines, [3], entry_delay_hours=1)
    print(f"\n{'feature':<30}{'mean_ic':>10}{'t_stat':>9}{'hit':>8}{'n_days':>8}")
    rows = []
    for c in present:
        r = cross_sectional_ic(panel, c, "fwd_short_return_3d")
        rows.append((c, r))
    # sort by |IC| descending so the strongest signals (either sign) surface
    rows.sort(key=lambda kv: -abs(kv[1].mean_ic))
    for c, r in rows:
        print(f"  {c:<28}{r.mean_ic:>10.4f}{r.t_stat:>9.2f}{r.hit_rate:>8.3f}{r.n_days:>8}")
    print("\nnote: a useful short feature has IC > 0 (larger value -> larger fwd"
          " short return). Negative-IC features are sign-flippable candidates.")


if __name__ == "__main__":
    main()
