# New-data scoping: PIT-clean derivatives datasets for Bybit + Binance USDM (2026-06-10)

**Label: EXPLORATORY / data-availability reconnaissance only.** No backtest, no
parameter, no evidence claim. Purpose: acquisition map for six candidate signal
inputs — (1) liquidation events, (2) order-book depth/imbalance, (3) taker
buy/sell flow, (4) open-interest history, (5) perp basis / premium-index term
structure, (6) options skew — with the hard requirement of **point-in-time-clean
HISTORY back to ~2023-01, both venues if possible, free/cheap preferred**.

Verification method: direct S3 XML listings of the `data.binance.vision` bucket
(via `https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix=...`),
direct listings of `public.bybit.com`, vendor docs, and web search — all on
2026-06-10. Items I could not confirm are tagged **unverified**.

---

## 1. Availability matrix

| Dataset | Venue | Source | History start | Last update / status | Granularity | Cost |
|---|---|---|---|---|---|---|
| Liquidation events | Binance UM | data.binance.vision `liquidationSnapshot` | was 2023-06-25 | **REMOVED — prefix empty as of 2026-06-10 (verified)** | per published order | free (gone) |
| Liquidation events | Binance CM | data.binance.vision `liquidationSnapshot` | 2023-06-25 (BTCUSD_PERP, verified) | **frozen 2024-06-18 (verified)** | per published order | free |
| Liquidation events | Binance UM | Tardis.dev `forceOrder` capture | 2020-01-07 | current | per published order (sampled, see §3) | $350–700/mo tier |
| Liquidation events | Bybit | **no official archive** (public.bybit.com has none; no REST history endpoint found) | — | — | — | — |
| Liquidation events | Bybit | Tardis.dev `liquidation` / `allLiquidation` | 2020-11-03→2023-04-05, then **GAP**, then 2025-02-25→ | current | sampled pre-2025-02; ALL events after | $350–700/mo tier |
| Liquidation aggregates | both | Coinalyze API | multi-year daily (exact start unverified) | current | **daily only** for deep history; intraday = rolling ~1500–2000 points | free (40 req/min) |
| Liquidation aggregates | both | Coinglass API | multi-year (depth tied to plan) | current | 1h/4h/1d aggregates; raw orders last 7 days only | $29–299+/mo |
| Book depth (banded) | Binance UM | data.binance.vision `bookDepth` | 2023-01-01 (BTCUSDT, verified) | current (2026-06-08, verified) | snapshots at ±1–5% bands, ~1-min cadence (cadence unverified) | free |
| Book ticker (BBO ticks) | Binance UM | data.binance.vision `bookTicker` | 2023-05-16 (BTCUSDT, verified) | **discontinued 2024-03-30 (verified)** | tick | free |
| Order book L2 | Bybit | bybit.com/derivatives/en/history-data portal | ~2023 (third-party reported, unverified) | current | raw depth snapshots (JSON), 7-day range per request | free, no login |
| Order book L2 | both | Tardis.dev (`depth@0ms` Binance since 2019-11-17; `orderbook.50/500/200` Bybit since 2019-12-23) | 2019 | current | full incremental tick L2 | $350–700/mo tier |
| Taker buy/sell flow | Binance UM | data.binance.vision `trades`/`aggTrades` (side-flagged) | ~contract launch (2019–2020, unverified exact) | current | tick | free |
| Taker buy/sell flow | Bybit | public.bybit.com `trading/` (side-flagged tick trades) | 2020-03-25 (BTCUSDT, verified) | current | tick | free |
| Taker L/S ratio (precomputed) | Binance UM | data.binance.vision `metrics` | 2020-09-01 (BTCUSDT, verified) | current (2026-06-08, verified) | 5-min | free |
| Taker L/S ratio (precomputed) | Bybit | — none historical found; REST `account-ratio` is accounts not volume (depth unverified) | — | — | — | — |
| Open interest | Binance UM | data.binance.vision `metrics` (`sum_open_interest`) | 2020-09-01 (verified) | current | 5-min | free |
| Open interest | Binance UM | REST `/futures/data/openInterestHist` | **30 days only (verified docs)** | live | 5m–1d | free |
| Open interest | Bybit | REST `/v5/market/open-interest` | docs claim back to symbol launch (quote below; practical depth unverified) | live | 5min–1d, cursor-paginated | free |
| Open interest | both | Tardis (Binance `openInterest` snapshots ~6s since 2020-05-12; Bybit inside `instrument_info`/`tickers`) | 2020 | current | seconds-level | $ tier |
| Premium index / basis | Binance UM | data.binance.vision `premiumIndexKlines` | 2020-01 (BTCUSDT 1m monthly, verified) | current (2026-05, verified) | 1m klines | free |
| Premium index / basis | Bybit | public.bybit.com `premium_index/` | 2019-10-01 | **dead 2020-03-10 (verified); inverse only** | daily CSVs of index ticks | free |
| Premium index / basis | Bybit | REST `/v5/market/premium-index-price-kline` (USDT perps) | depth unverified | live | klines | free |
| Options skew (BTC/ETH regime input) | Deribit | Tardis.dev options (`ticker` w/ IV+greeks, `markprice.options`, `options_chain` CSV) | 2019-03-30 (verified docs) | current | tick / chain snapshots | Options plan $350–700/mo |
| Options skew | Deribit et al. | Laevitas API (25Δ RR/BF, IV history) | "5+ years" (vendor claim) | current | hourly/daily series | paid, quote-based (unverified) |
| Options skew | Deribit | Deribit public REST DVOL history | ~2021 (unverified) | current | 1h/1d | free |

---

## 2. Binance public archive (data.binance.vision) — detail

Verified via S3 listings on 2026-06-10. `futures/um/daily/` contains exactly:
`aggTrades, bookDepth, bookTicker, indexPriceKlines, klines, markPriceKlines,
metrics, premiumIndexKlines, trades` ([listing](https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=data/futures/um/daily/)).

- **`liquidationSnapshot` (UM): REMOVED.** The prefix
  `data/futures/um/daily/liquidationSnapshot/` (and `.../BTCUSDT/`) returns an
  **empty listing** (verified twice). It existed historically — a [dev-forum
  thread (Apr 2024)](https://dev.binance.vision/t/the-liquidationsnapshot-data-is-behind/19576)
  reported UM BTCUSDT updates stalled at **2024-03-31** with no staff response.
  The CM equivalent still exists: BTCUSD_PERP runs **2023-06-25 → 2024-06-18**,
  then stops ([CM listing](https://data.binance.vision/?prefix=data%2Ffutures%2Fcm%2Fdaily%2FliquidationSnapshot%2F)).
  Conclusion: **Binance no longer publishes liquidation history; the UM archive
  was deleted, the CM archive is frozen mid-2024.** Mirrors of the deleted UM
  files may exist on Kaggle/GitHub (unverified, provenance risk).
- **`metrics`**: BTCUSDT **2020-09-01 → current** (2026-06-08 verified;
  newer symbols start at listing). 5-min rows; fields include
  `sum_open_interest`, `sum_open_interest_value`, top-trader long/short
  account+position ratios, global long/short ratio, and
  `sum_taker_long_short_vol_ratio` (column set per community docs —
  **verify on first download**; the official README does not document it).
  This is the only free OI + taker-ratio history; the REST equivalents
  (`/futures/data/openInterestHist`, `takerlongshortRatio`, …) serve **only the
  latest 30 days** ([docs](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics)).
- **`bookDepth`**: BTCUSDT **2023-01-01 → current** (2026-06-08 verified). Not
  a full order book: timestamped snapshots of **cumulative depth and notional at
  percentage bands −5%…+5% from reference price** (semantics per
  [dev-forum thread](https://dev.binance.vision/t/what-is-the-meaning-of-the-percentage-in-bookdepth-data/18581));
  cadence looks ~1/min from examples (**exact cadence unverified — inspect a file**).
- **`bookTicker`**: BTCUSDT **2023-05-16 → 2024-03-30, then discontinued**
  (verified via marker listing; no keys after 2024-03-30). Note the same
  spring-2024 cutoff cluster as liquidationSnapshot — Binance quietly stopped
  several tick-level publications around 2024-Q2.
- **`trades`/`aggTrades`**: daily+monthly, side-flagged (`is_buyer_maker`),
  full depth back to ~contract launch (exact start unverified) → taker
  buy/sell flow is reconstructable at any horizon.
- **`premiumIndexKlines`**: monthly 1m files BTCUSDT **2020-01 → 2026-05**
  (verified) — clean perp-basis history. `indexPriceKlines`/`markPriceKlines`
  same family. (`fundingRate` monthly files also exist — already covered in-repo.)

## 3. The liquidation completeness caveat (both venues)

- **Binance**: since the 2021-04 stream change (date approximate, widely
  reported; **unverified exact**), the `forceOrder` WS pushes **only one
  liquidation order per symbol per 1000 ms** — official docs: "only the latest
  one liquidation order within 1000ms" ([Liquidation Order Streams](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Liquidation-Order-Streams)).
  The archived dataset was literally named `liquidationSnapshot`, consistent
  with being a capture of this sampled snapshot stream, i.e. **it inherits the
  cap** (counts and volumes are floors, biased low precisely in cascade
  seconds). I found **no official statement** confirming or denying full
  coverage in the archive ([forum thread asking about the format got no
  answer](https://dev.binance.vision/t/trying-to-understand-binance-historical-liquidation-snapshots/22392)) — treat as sampled. **Every third-party Binance
  liquidation history (Tardis, Coinalyze, Coinglass, Amberdata) collects from
  the same capped stream and inherits the same floor.**
- **Bybit**: the legacy `liquidation` topic pushed **at most one order per
  second per symbol** and is deprecated; `allLiquidation` (launched
  **2025-02**, 500 ms batches) pushes **all** liquidations
  ([deprecated stream](https://bybit-exchange.github.io/docs/v5/websocket/public/liquidation),
  [allLiquidation](https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation),
  [Decrypt on the full-disclosure change](https://decrypt.co/307194/bybit-sets-industry-benchmark-with-full-disclosure-of-liquidation-data)).
  **Bybit publishes no historical liquidation archive at all** — nothing on
  public.bybit.com, no REST history endpoint found (live-collect-only).
- **Tardis Bybit gap**: their capture is `liquidation` **2020-11-03 →
  2023-04-05**, then **nothing until** `allLiquidation` **2025-02-25**
  ([Tardis Bybit page](https://docs.tardis.dev/historical-data-details/bybit)).
  ⇒ **Event-level Bybit liquidation history for 2023-04 → 2025-02 — most of the
  target window — effectively does not exist from any source I could verify.**
  Fallback for that window: Coinalyze/Coinglass interval aggregates (collected
  from the sampled 1/sec stream → undercounted, and daily-granularity only for
  deep history on Coinalyze).

**Cross-venue validation problem, stated explicitly:** a liquidation-feature
signal can be trained on Binance (Tardis forceOrder, 2020→current, sampled) but
can only be validated on Bybit **before 2023-04** (sampled) or **after 2025-02**
(complete) — and the two Bybit regimes have *different* completeness, so even
that comparison is confounded. Any liquidation-input signal therefore needs
either (a) a liquidation **proxy** computable on both venues from free PIT data
(trades + OI deltas; see recommendation 1), or (b) acceptance of
Binance-only development with a forward-only Bybit validation window growing
from 2025-02.

## 4. Bybit official sources — detail

- **public.bybit.com** (verified listing): `trading/` (side-flagged tick trade
  CSVs per symbol, BTCUSDT from **2020-03-25**, ~1,200+ symbols, current),
  `spot/`, `spot_index/`, `kline_for_metatrader4/`, `premium_index/`
  (**inverse contracts only, 2019-10-01 → 2020-03-10, dead**). **No
  liquidations, no OI, no funding, no order book** here.
- **[bybit.com/derivatives/en/history-data](https://www.bybit.com/derivatives/en/history-data)**
  (page timed out on fetch; contents per third-party tooling/blogs —
  [example](https://medium.com/@lu.battistoni/how-to-download-and-format-free-historical-order-book-dataset-16b3a84a8e0e),
  [downloader repo](https://github.com/nssanta/Bybit-Download-OrderBook-Trades-Klines)):
  free, no-login downloads of **order book snapshots (raw JSON), trades,
  klines**, reportedly back to ~**2023**, limited to 7-day ranges per request.
  **Unverified directly — confirm coverage start per symbol before relying on it.**
- **REST**: `/v5/market/open-interest` (5min–1d, cursor pagination; docs:
  *"The upper limit time you can query is the launch time of the symbol"* —
  i.e. full history claimed; [docs](https://bybit-exchange.github.io/docs/v5/market/open-interest);
  practical depth **unverified — spot-check 2023-01 before counting on it**),
  `/v5/market/history-fund-rate` (full funding history),
  `/v5/market/premium-index-price-kline` (USDT perps; depth unverified).
  PIT caveat for all REST backfills: you get today's copy of history; no way to
  audit revisions. Low risk for OI/funding, but record the pull date.

## 5. Third-party vendors — profiles and rough cost

| Vendor | What it has (for this program) | History | Cost (rough) | Verdict |
|---|---|---|---|---|
| **[Tardis.dev](https://docs.tardis.dev/)** | Tick-level raw WS captures, CSV + API: Binance USDM since 2019-11-17 (depth@0ms, trades, bookTicker, markPrice, forceOrder since 2020-01-07, openInterest ~6s snapshots since 2020-05-12); Bybit since 2019-11/2020-05 (orderbook 200/500/50, trades, tickers/instrument_info incl. OI+funding, liquidations per §3) | to 2019; **non-Business tiers = 4-year lookback** (yearly billing) — still covers 2023-01 today, won't by 2027 | Perpetuals plan: Academic $350/mo, Solo $700/mo, Pro $900/mo, Business $2,500/mo; All-exchanges higher ([pricing](https://tardis.dev/#pricing)) | The only PIT event-level multi-venue archive; the reference source if budget allows |
| **[Coinalyze](https://api.coinalyze.net/v1/doc/)** | Free REST API: liquidation history, OI history, funding + predicted funding, L/S ratio, OHLCV; Bybit + Binance both covered | **Daily granularity kept forever; intraday (1m–12h) only a rolling ~1500–2000 points (deleted daily!)** | free, 40 req/min | Good free daily-feature source; useless for intraday squeeze timing history |
| **[Coinglass](https://docs.coinglass.com/reference/liquidation-history)** | Pair + aggregated liquidation history (interval aggregates), OI, funding, L/S, heatmaps (model-derived); raw liquidation orders **last 7 days only** | multi-year, depth/granularity gated by plan; CSV/bulk export = Enterprise | Hobbyist $29/mo, Startup $79/mo, Standard $299/mo (commercial bar), Enterprise custom | Chart-first; OK cheap cross-check, weak as a primary PIT archive |
| **[Amberdata](https://docs.amberdata.io/docs/liquidations-2)** | Institutional REST+WS: historical liquidations (orders *and* trades where reported) for Binance+Bybit, OI, vol surfaces | deep (vendor claim) | enterprise quote-based (typically $1k+/mo, unverified) | Capable but likely overkill/over-budget |
| **CryptoQuant** | Integrated liquidation/OI/funding charts incl. Bybit+Binance; CSV on paid plans | multi-year, mostly 1h/daily | consumer tiers ~$29–99/mo, API enterprise (unverified) | Analytics-first, not an event archive |
| **[CoinAPI](https://www.coinapi.io/blog/crypto-liquidation-data)** | Metrics API: exchange-native liquidation metrics, OI history, funding; flat-file S3 csv.gz | varies per metric (unverified) | per-credit / subscription, mid-priced (unverified) | Secondary option; same upstream sampling caps apply |

## 6. Options skew (regime input) — yes/no

**Yes, available.** Best PIT source: **Tardis Deribit** — all options instruments
since **2019-03-30**, `ticker` (greeks + IV), `markprice.options`, and a derived
**`options_chain`** CSV dataset from which 25Δ skew is directly computable
([docs](https://docs.tardis.dev/historical-data-details/deribit)); Options plan
$350–700/mo (Academic/Solo). Precomputed alternatives: **Laevitas** (25Δ RR/BF +
IV history, "5+ years", REST API; pricing quote-based, [docs](https://docs.laevitas.ch/options/historical));
Amberdata vol surfaces (enterprise). Free floor: Deribit's public REST DVOL
index history for BTC/ETH (coarse vol-regime proxy, not skew; depth unverified).
A daily 25Δ-skew regime series is cheap to obtain; tick-level options data is not needed.

## 7. Ranked recommendations (PIT-clean, both-venue constraint)

1. **Binance Vision `metrics` + raw tick trades both venues (taker-flow + OI
   stack) — free, do first.** `metrics` gives 5-min OI + taker-L/S +
   top-trader ratios 2020-09→current for every UM perp; Bybit OI backfilled
   via `/v5/market/open-interest` (verify depth at 2023-01). Taker buy/sell
   flow built **identically on both venues** from side-flagged tick trades
   (Vision `trades`/`aggTrades`; public.bybit.com `trading/`). This is the only
   stack that is simultaneously free, event-time PIT, ≥2023-01, and truly
   cross-venue — and trades+OI-delta features double as the **liquidation
   proxy** (forced-flow bursts ≈ aggressive volume spikes coinciding with OI
   drops), sidestepping §3 entirely.
2. **Liquidations: start free live collection NOW; buy Tardis only if the
   proxy shows promise.** Stand up a collector for Bybit `allLiquidation`
   (complete since 2025-02) + Binance `forceOrder` (sampled) — every month of
   delay is lost forward history no vendor can sell back (Bybit 2023-04→2025-02
   is unrecoverable at event level, full stop). For backfill, Tardis Perpetuals
   (Academic $350/mo if eligible) provides Binance forceOrder 2020→current and
   the two Bybit segments; validate the proxy from rec 1 against these tapes
   where they exist. Do not buy aggregator "liquidation history" as evidence —
   all of it inherits the exchange sampling caps.
3. **Depth/imbalance: Binance `bookDepth` (free, 2023-01→current, ±1–5% bands)
   as the cheap regime feature; Bybit's history-data order-book archive
   (free, ~2023→, raw snapshots) as the matching counterpart if the Binance
   side earns it.** Caveat up front: cross-venue parity here is approximate —
   Binance is pre-banded ~1-min snapshots, Bybit is raw L2 you must band
   yourself (or pay Tardis for symmetric tick L2 on both). Treat depth as a
   Binance-first investigation with a Bybit confirmation pass, not a
   simultaneous both-venue build.

Skew (rec 6) is a cheap add-on regime series, not a primary acquisition. The
Binance spring-2024 publication freezes (liquidationSnapshot, bookTicker) are a
standing warning: **mirror any Vision dataset you start depending on — Binance
deletes/freezes archives without notice.**

## 8. Verification log

Directly verified 2026-06-10 (S3/HTML listings): um/daily dataset inventory; um
liquidationSnapshot empty; cm liquidationSnapshot 2023-06-25→2024-06-18;
metrics 2020-09-01→2026-06-08; bookDepth 2023-01-01→2026-06-08; bookTicker
2023-05-16→2024-03-30 (no later keys); premiumIndexKlines 1m 2020-01→2026-05;
public.bybit.com inventory; trading/BTCUSDT from 2020-03-25; premium_index
inverse-only 2019-10-01→2020-03-10. Docs-verified: Binance forceOrder 1000 ms
snapshot semantics; REST 30-day caps; Bybit liquidation deprecation +
allLiquidation; Bybit OI pagination claim; Tardis channel dates + pricing;
Coinalyze retention. **Unverified**: liquidationSnapshot-archive sampling
inheritance (inferred, no official statement); Bybit history-data portal
coverage (page timed out); Bybit REST OI practical depth; exact Binance trades
archive start; metrics column set; Laevitas/Amberdata/CryptoQuant/CoinAPI exact
pricing and history starts; Deribit DVOL history depth; 2021-04-27 as the exact
Binance stream-cap date.
