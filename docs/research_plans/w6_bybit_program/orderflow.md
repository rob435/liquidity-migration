# W6 — orderflow squeeze-proxy track

Operator-directed 2026-06-15. The orderflow track of the W6 bybit-first program (see
`docs/research_plans/w6_bybit_program/PLAN.md`). The W5 program closed the price/return
lever space; this is the orderflow frontier W5 deliberately did not touch. Thesis: the continuous book
SHORTS pumped names, so a pump on a CROWDED long (OI buildup, extreme funding,
liquidation cluster, thinning book) is a squeeze that fades harder — the squeeze
context is the signal, not raw taker-flow composition (P10's null).

Status legend: ✅ done · 🔬 evidence in hand · 🧱 data-gated · 📝 design frozen.

## 1. Squeeze-proxy sizing (OI/funding) — testable NOW 🔬

The exploratory screen (`scripts/w6_squeeze_proxy_screen.py`) found real
within-symbol IC over composite, symbol-hash control degenerate both venues:
- `oi_chg_24h`: **bybit +0.0665, p=0.002, all thirds +** (binance OI-gated).
- `funding_level`: **binance +0.056, p=0.013**; bybit +0.025 same sign.

Next = the binding engine intervention, pre-registered at
`docs/preregistration/2026-06-15-w6-squeeze-proxy-sizing.md`: a mean-1,
gross-neutral squeeze-proxy sizing tilt via the existing `size_mult_lookup` hook, with
the multi-seed random/shuffle controls + cost stress + thirds + bybit-robust bar.
Admissibility ≠ harvestable (Stage 7b path-shape was admissible, Stage 5 did not
harvest) — the controls decide. Runs on the data box; no live change until it passes.

## 2. Liquidation/depth squeeze design — frozen, data-gated 📝🧱

The collectors already exist and write forward-only tapes (raw history is unbuyable):
- `liquidity_migration/liquidation_collector.py` — Bybit `allLiquidation` + Binance
  `!forceOrder@arr` WS → `data/liquidations/{venue}/{YYYY-MM-DD}.jsonl`
  (venue, symbol, RAW per-venue side, price, qty, ts_ms). Unit:
  `deploy/systemd/liquidity-migration-liquidation-collector.service`.
- `liquidity_migration/depth_collector.py` — hourly Bybit REST band snapshots
  (cumulative quote notional within ±{0.2,1,2,3,4,5}% of mid; NULL beyond span) →
  `data/depth/bybit/<YYYY-MM-DD>.jsonl`. Live on the VPS since 2026-06-13 (581 symbols).
  Unit: `deploy/systemd/liquidity-migration-depth-collector.service`.

**Frozen design (run when tapes mature — depth ~weeks more, liquidation ~2026-07-10):**
add two causal pre-entry squeeze features to the screen, then (if admissible) the same
sizing/hedge intervention as §1:
- `liq_cluster_6h` = same-side (long-liquidation) notional in the 6h before the signal
  bar, per-symbol-normalized. A liquidation cascade into the pump = squeeze in progress.
- `book_thinning` = drop in ±1–2% bid-band cumulative notional over 6–24h pre-entry
  (depth_collector bands), per-symbol z. A thinning book = fragile, fades harder.
Same statistic (within-symbol partial rank-IC over composite + symbol-hash control),
same both-venue + control + cost bar. Binance depth has no equivalent collector (bybit
REST only) and binance liquidation is host-gated (§3) — so these start bybit-primary,
binance via the forward tapes once §3 lands. NOT run until the tapes clear a minimum
observed-window + coverage floor (no thin-tape false positives).

## 3. Binance liquidation host — infra gap, OPERATOR action 🧱

The collector code handles binance already; the blocker is purely network/region:
`wss://fstream.binance.com` is geo-blocked from the current Hetzner host, so the
binance leg idles with zero rows (STATE.md). To start the binance forward liquidation
tape:
1. Stand up a small VPS in a Binance-permitted region (NOT US / not the blocked
   region), clone the repo + `.venv`, demo creds only (the collector needs none — it's
   public WS, no order path).
2. Enable just the liquidation collector there:
   `systemctl enable --now liquidity-migration-liquidation-collector` (writes
   `data/liquidations/binance/<date>.jsonl`).
3. Sync the JSONL back into the forward liquidation tape under SHARED_DATA on a daily
   cadence (rsync/scp; append-only, idempotent by day file).
Alternative: route only the binance WS egress through a permitted-region proxy on the
existing host. Either way this is operator/infra — it cannot be done from the dev box
(region-blocked) and provisions a host, so it is flagged, not auto-executed. Every day
it is unfixed is a day of missing binance liquidation history that can never be backfilled.

## 4. P11 taker-flow panel completion — idle-time data build 🧱

Raw taker-flow composition was a null (P10), but a richer feature set on a complete
panel is admissible as a fresh stage. Current coverage:
- bybit `taker_flow_5m` (taker_buy_quote/sell_quote/n_buy/n_sell) — partial.
- binance `binance_usdm_taker_flow_1h` (taker_imbalance/buy_sell_ratio/signed_volume) —
  recent-window (REST history is recent-only per Binance docs).
Completion = backfill the full-universe taker-flow history where the venues allow
(`scripts/backfill_binance_metrics_vision.py` for binance vision-archived metrics;
bybit 5m taker aggregation), on a permitted host, so a re-test can use imbalance
persistence / divergence features beyond `flow_support_6h`. Idle-time, lowest priority
of the four — only worth it if §1/§2 motivate a taker-flow re-test.

## Sequencing
1. §1 squeeze-proxy sizing sweep (now; data in hand) — the live evidence path.
2. §3 binance liquidation host (operator; starts the clock on binance liq history).
3. §2 liquidation/depth screen (when tapes mature; design frozen).
4. §4 P11 completion (idle-time; conditional on §1/§2).
Tier-3 real-money gate UNCHANGED throughout; everything is research-stage forward-watch.
