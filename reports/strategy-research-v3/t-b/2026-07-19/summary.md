# T-B — Funding-floor entry/exit economics (exploratory, Lane 1)

**Status: EXPLORATORY.** Counterfactual post-processing of the spent V2 discovery
surface. No alpha, robustness, candidate, or promotion claim. Prototype value, if
any, accrues only through the forward rolling ledger (Lane 2).

## What ran

- Input: frozen V2 barebones CONTINUOUS ledger (16,745 short trades, entries
  2021-05-06 → 2024-12-01, SHA-256 `368a7c04…64ddd`, verified) plus a settlement
  funding panel rebuilt from the full-PIT root (879,556 settlements, 431 symbols;
  reproduces every modeled ledger trade's funding to 0.0 and the official daily
  curve to ≤ 3.6e-15).
- Entry filter: keep a trade iff `TP_distance > modeled cost + multiple × floor`,
  floor = `max(0, −(known_rate × settlements in the planned 24h hold))`.
  Rate conventions: `prev` = last settled rate ≤ entry (strictly PIT under any
  venue semantics; primary); `next` = realized next-settlement rate (PIT only if
  Bybit fixes the rate at the interval start — unverified, reported as the
  information-upper-bound variant).
- Drain exit: exit at the first settlement where realized cumulative funding cost
  plus projected remaining (`−rate_at_settlement × remaining settlements`)
  ≥ `fraction × TP_distance`.
- Full grid (all cells reported, none discarded): multiples {none, 1.0, 1.25, 1.5}
  × fractions {none, 0.02, 0.05, 0.10, 0.20} on `prev`; multiples-only on `next`.
  Era split at 2023-02-22 (calendar midpoint of ledger entries).

## Results (baseline: net −20.23%, maxDD −38.74%, early +7.39% / late −27.62%)

Floor size: zero for ~78% of trades; p90 ≈ 19–28 bps, p99 ≈ 470–530 bps,
max ≈ 28–30% (per unit notional). At TP distance 12% the filter binds on only
23–83 of 16,745 trades.

| Cell (full period) | Net | Δ vs base | Removed | Funding saved | Gross forgone |
|---|---:|---:|---:|---:|---:|
| prev ×1.0 | −19.70% | **+0.52pp** | 23 | +0.91% | +0.43% |
| prev ×1.25 | −20.10% | +0.13pp | 39 | +1.40% | +1.34% |
| prev ×1.5 | −20.41% | −0.18pp | 69 | +2.28% | +2.60% |
| next ×1.0 | −18.47% | **+1.75pp** | 35 | +1.60% | +0.09% |
| next ×1.5 | −17.11% | **+3.12pp** | 83 | +3.62% | +0.67% |

- **Era stability — the decisive arm.** `prev` ×1.0 is early-only (+0.95pp early,
  −0.42pp late): **not era-stable**. `next` improves both eras at every multiple
  (×1.5: +1.19pp early, +1.93pp late) and lowers maxDD (−36.9% vs −38.7%) and
  worst day (−3.95% vs −5.21%).
- **Salience-bias check.** As the multiple rises, forgone gross grows faster than
  funding saved under `prev` (removed trades at ×1.5 were net **winners**:
  +2.60% gross vs 2.28% funding + 0.14% cost saved). The mechanism guarantees
  only the cost side; the same crazy-funding symbols carry the largest gross wins.
- **Drain exit: refuted on this trade shape.** Every fraction, every era is worse
  (full-period −25.5% to −31.6% vs −20.2%; e.g. fraction 0.02 forfeits 18.9% gross
  to save 12.1% funding). Funding-paying shorts systematically still had large
  positive remaining gross; exiting early destroys more than it saves. Spot-verified
  by hand on the heaviest funding payer (FTTUSDT, 2022-11-10, hourly −2.5% caps).

## Read

The floor structure is real but nearly toothless at the barebones 12% TP
geometry, and the strictly-PIT version of it is not era-stable. The only
era-stable improvement uses next-settlement-rate information whose PIT status
depends on venue mechanics not verified here. The drain-exit half of the thesis
is contradicted by the data.

## Limitations

- Pure post-processing: removed/shortened trades free no capacity for unobserved
  substitutes; admission effects unmodeled.
- Costs per trade held at the ledger's modeled round-trip; no re-priced impact.
- 19 partial-funding-coverage trades retained as-is (they reproduce exactly).
- Barebones trade shape (12% TP, 24h hold) — not the deployed profile geometry.
- Discovery surface already inspected by V2; every number here is spent.

## Next action (owner decision, not a conclusion)

If anything advances, it is `next ×1.0–1.5` as a forward-ledger prototype, and
only after verifying Bybit's funding-rate timing semantics (is the next
settlement's rate fixed at interval start?). Nothing here changes any profile.

Artifacts: `tb_grid.csv` (all 72 cells), `tb_diagnostics.json`,
`tb_trade_panel.parquet` (local; hash in `manifest.json`), manifest with grids,
input hashes, and code commit.
