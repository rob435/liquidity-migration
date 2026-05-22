# System Status

Updated 2026-05-22.

The liquidity-migration short strategy is in **committed paper forward testing**
on the Bybit demo account. The canonical configuration is the `promoted` profile
with `liquidity_migration_close_location_min = 0.30`.

## Funding-model correction (2026-05-22)

The perpetual-funding model over-charged funding by up to 8x: `_funding_lookup`
billed every funding row in a hold window, and 147 of 313 research-root symbols
carry intra-interval snapshot rows (e.g. hourly rows of an 8h rate). Fixed in
`008d34a` — funding is now charged once per settlement interval. The canonical
figures in this document are funding-corrected; the `strategy-tribunal` review
and 81-scenario sweep under "Research status" **predate the fix** (over-charged
funding understated return and overstated drawdown) and must be re-run before
their verdict stands.

## Canonical setting — close_location_min = 0.30

As of 2026-05-21, **`0.30` is the canonical close-location setting** for research
and for the forward test (previously 0.45). It was chosen from an exploratory
closing-bar sweep on the full-PIT IS root (2023-2026):

- 0.30 vs 0.45: more trades (510 vs 448) and higher total return (2850% vs
  2212%), at the cost of deeper drawdown (-14.2% vs -11.6%) and marginally lower
  walk-forward split Sharpe (3.59 vs 3.71).

This is a trade-count / return vs drawdown choice, not a strict improvement, and
it is **not yet tribunal-validated** — the prior tribunal WATCH verdict and
81-scenario sweep below were run on the 0.45 configuration. Validating 0.30
through `strategy-tribunal` is the open research work; the demo paper forward
test of `promoted` + close-0.30 now runs on the VPS (see Deployment status).

## Research status (prior baseline — close 0.45)

- A full point-in-time costed backtest, an 81-scenario parameter sweep, and an
  adversarial `strategy-tribunal` review returned a **WATCH** verdict with no
  FAIL findings: 3/3 pre-registered windows positive, six negative controls
  pass, 81/81 sweep scenarios promotable. This evidence is for close 0.45.
- The book carries almost no directional exposure (beta -0.03 to BTC, -0.07 to
  the equal-weight universe); it needs neither a regime gate nor a hedge.
- Caveat: true-OOS validation on the dedicated pre-2023 Bybit and Binance roots
  showed the edge does not clearly generalise before 2023 (walk-forward split
  Sharpe ~0). The edge is IS-era / regime-conditional — see
  `~/SHARED_DATA/bybit_fullpit_1h/reports/signed_flow_research_verdict.md`.

## Deployment status

- The Bybit demo (paper) forward test runs the canonical `promoted` profile at
  `close_location_min = 0.30` on the Singapore VPS. The champion/challenger
  guard authorises `promoted` as the single order-submitting demo stack;
  `demo_relaxed` and the other candidates are shadow/dry-run only. This is a
  demo-only paper forward test — not Model-Court validated, not a real-money
  promotion.
- The demo cycle fetches the top 220 symbols by 24h turnover (≥ $2M) so the
  `promoted` strategy can trade its rank 31–150 selection band;
  `event-demo-cycle` refuses a forward universe narrower than rank 150 for
  `promoted`.
- No real-money trading; the private client remains demo-only by design
  (`demo=False` is refused).
