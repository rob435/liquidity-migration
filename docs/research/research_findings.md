# Research findings

The durable record of what this repository's strategy research establishes, negative results included.
Evidence grading and promotion: [docs/research/governance.md](governance.md). Evidence rules:
[AGENTS.md](../../AGENTS.md). Failure taxonomy: [docs/research/backtesting_errors_we_never_repeat.md](backtesting_errors_we_never_repeat.md).
Data tiers, roots, and PIT membership: [docs/data.md](../data.md).

## 2026-08-26 — Cross-venue funding gap: Lane-1 exploration

The "trade the venue where funding is most negative (Bybit as reference)"
idea was probed on seen data only — it grades nothing. The registered v7
carry-hold book's held name-days (n=1,957, both-venue reads fresh) were
split by `funding_diff_bp` (= bybit minus binance settled funding, bp/day).
Script: `scripts/research/demo_funding_gap_diagnostic.py`; artifact:
`reports/demo_funding_gap_diagnostic.json` (gitignored).

**Result.** The gap is predictive of the book's own per-name-day net, in the
direction the idea expects — but it is *the Bybit book's* edge, not a
separately capturable cross-venue alpha:

- baseline (all held): net +16.5 bp/name-day, t +3.76
- Bybit much deeper (gap < −40 bp/day): net +51.5, t +1.95
- venues agree (−10..+10): net +6.8, t +1.93
- Bybit negative **and** Binance negative: net +17.1, t +3.77 (n=1861, 95% of
  the book) — vs Bybit negative and Binance non-negative: net +4.9, t +0.30

**Interpretation.** The deep-gap cohort is the book's best subset, and the
edge concentrates where the venues *diverge* — that is consistent with
"route to the venue that pays most." But the caveat that killed
`lane2_funding_spread_v1` still stands: Binance funding alone is near-noise
(t 0.4 / Sharpe 0.18 on its own), so the gap's predictive content is
**Bybit-depth carrying the load, not a second venue you could trade into**.
The deep-gap era split is thin and one-sided: 2022 is sharply negative
(−96.6 bp/name-day, n=14) and 2024-26 carry it — so this is a regime
feature of the structural funding inversion, not a stable property.

**Non-conclusion.** This is Lane-1 selection evidence, not a graded result.
It does not establish a separately capturable cross-venue edge, and it does
not reverse the `funding_spread_v1` deletion reasoning (that config deleted
2026-08-19; cross-venue replication on corrected accounting measured t 0.4).
The one live question it leaves: whether "*both* venues negative" (a
convergence signal) predicts anything *in addition* to Bybit being negative
— the n=1861 vs n=96 split above suggests it mostly does not.


