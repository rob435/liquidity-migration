# Research Findings

Updated 2026-05-27 — research-evidence reset.

## Status

The per-venue full-PIT data roots (`~/SHARED_DATA/bybit_full_pit/`,
`~/SHARED_DATA/binance_full_pit/`) and every backtest report under them were
**deleted 2026-05-27**. Any claim in earlier drafts of this doc that cited
specific returns, drawdowns, Sharpe figures, sweep results, or tribunal
verdicts was sourced from those reports and is now unverifiable — those
numbers have been removed rather than left as misleading citations.

What survives:

- **The code** — the `volume-events` engine, `long_native` sleeve, and
  `strategy-tribunal` review path are all in `liquidity_migration/` and
  unchanged. Re-running them on a rebuilt data root will reproduce the same
  shape of results.
- **The methodology** — `docs/backtesting_errors_we_never_repeat.md` is the
  standard every run must clear before its numbers can be cited.
- **The live VPS demo** — `liquidity-migration-bybit-demo.service` +
  `liquidity-migration-bybit-long-demo.service` continue to run on Bybit
  demo. The on-VPS ledgers (`/opt/liquidity-migration/data/{bybit-demo-event,
  bybit-long-demo-event}`) are independent of the deleted local research
  roots and are unaffected.

## What is honestly known right now

About the **short** sleeve (`liquidity_migration` event short, `promoted`
profile):

- The signal idea — cross-sectional reversion against names that just had a
  liquidity-migration event — is theory-grounded: price-insensitive momentum
  flow exhausts in the weakest-liquidity names.
- Historically the strategy passed tribunal review with `WATCH` (not `FAIL`)
  on the 5-position research config, and the live 3-position concentrated
  config produced bigger absolute returns at a tighter drawdown gate.
- Historically pre-2023 OOS tests failed across variants — but the
  supporting artifacts for that claim were never local to this repo and have
  been removed from this doc rather than cited without evidence.

About the **long** sleeve (`long_native_v11a`, FOMO-chase only):

- Has only the FOMO-chase pattern firing in production (other patterns coded
  but `enabled=False`).
- Has never been through `strategy-tribunal` on either venue — no negative
  controls, no block-bootstrap, no formal splits.
- Best evidentiary label is **`exploratory`** until that work happens.

## What needs to happen before any new claim is made

1. **Rebuild data roots** — `bash scripts/build_full_pit_roots.sh` (per
   `docs/data_roots.md`, ~17–31 hours unattended). Re-run individual stages
   as needed.
2. **Re-run baseline backtests** — `volume-events` short canonical +
   `long_native` v11a on each venue.
3. **Run `strategy-tribunal`** on both sleeves on both venues. The long
   sleeve has never been tribunal-reviewed; that is the single highest-value
   piece of evidence to add.
4. **Re-record verified numbers here** — each cited figure must point to a
   reproducible report under a tracked data root. Numbers without that
   provenance do not belong in this doc.

## Methodology

See `docs/backtesting_errors_we_never_repeat.md`. No real-money deployment
claim is made beyond what the evidence supports — the VPS forward test is the
forward evidence, and the strategy is not real-money-validated.
