# T-L v1 — young-listing lifecycle, unconditional arms (Lane-1, 2026-07-20)

**Claim tested:** the untraded <240d Bybit listing population carries
calendar-mechanical drift (post-listing bleed / day-0 continuation) large
enough to clear the V7 admission bar (net ≥ +40 bp/trade, era-stable).

**Data:** Bybit full-PIT root, 925 listings 2021-01→2026-07 (5 left-censored,
920 eligible; anchor = first 1h bar, cross-checked against manifest
`v5_observed_launch_date`). Equal-notional arms, net of the frozen 45 bp
round trip and UTC-day funding sums (positive funding pays shorts). Block
bootstrap by listing month (63 blocks). All cells in `tl_arm_grid.csv`;
per-trade rows in `tl_trades.parquet`; day-0..30 panel with turnover and
funding paths in `event_panel.parquet`.

**Result: no unconditional arm passes.** The population is highly active —
short win rates 0.62–0.72, per-trade gross magnitudes of hundreds of bps —
but the signs are era-unstable:

| Arm | 2021H2-22 net bp | 2023-24 net bp | 2025-26 net bp | full net bp [95% CI] |
| --- | ---: | ---: | ---: | --- |
| long_d0_d2 | −284 | +103 | −64 | −46 [−275, +202] |
| short_d1_d7 | +275 | +273 | **−175** | +85 [−302, +425] |
| short_d2_d7 | +90 | +313 | **−332** | −4 [−413, +345] |
| short_d2_d14 | +11 | +26 | **−829** | −332 [−1699, +592] |
| short_d2_d30 | +558 | **−17** | +1171 [+323, +2072] | +577 [−90, +1188] |

**Reading.** The 2021–2024 listing bleed (short the day-1/2 close, cover
within a week: +270–310 bp net, consistent across both early eras) inverts
in 2025–26 — recent listings keep pumping past day 7 and the funding drag
on shorts grows (−90 to −238 bp per hold). The lone large positive late-era
cell (d2→d30) is sign-unstable mid-era and carries 28-day illiquid holds.
Day-0 chase is negative or flat everywhere.

**Conclusion (honest, per the admission bar):** calendar-time-only rules on
this population are dropped. The magnitudes confirm the population is where
the action is; the edge, if extractable, is **conditional** — candidate
conditioners for v2, all computable from the panel already built: day-1
pump size, turnover-decay rate (the actual liquidity-migration signal),
funding state at entry, and listing-wave crowding. Era stability across the
2024/2025 boundary is the primary acceptance test for v2, since that is
exactly where v1 broke.

**Limitations:** listing anchor collapses reused-ticker incarnations to the
earliest; 45 bp understates listing-week execution costs (a dedicated cost
read is mandatory before any Lane-2 commit); funding approximated as
UTC-day sums; Bybit only. Lane-1 exploratory on a seen root — not alpha,
robustness, or promotion evidence. The reserved V2 label tape was not read.
