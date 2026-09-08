# Strategy & Trading Logic Specification

Mathematical models, entry/exit criteria, sizing rules, and collision invariants for the native strategy sleeves.

---

## 1. Strategy Sleeve Registry

Strategy blocks are declared in `engine.toml`. A block's ID is its position in
that realm's config and is immutable identity in that realm's WAL: every new
block appends, nothing is inserted, and each realm's tail differs.

| ID | Crate / Reducer | Sleeve | Realm | Deployed State | Core Mandate |
| :---: | :--- | :--- | :--- | :--- | :--- |
| **0** | `carry_native` | **CARRY** | demo, mainnet; mexc and hyperliquid with entries disabled | Active | Holds the daily negative-funding book until the next daily decision. Off on MEXC: its funding signal is Bybit's, and MEXC funding differs per symbol. Off on Hyperliquid: that venue funds hourly and quotes the hourly rate, so Bybit's eight-hourly rate is out by a factor of eight. |
| **1** | `long_native` | **LONG** | demo, mainnet, mexc, hyperliquid | Active | Momentum breakouts on top turnover liquid perpetuals. On MEXC and Hyperliquid the signals are Bybit-derived; a symbol that venue does not list is refused at admission, not sent. On Hyperliquid an order under the venue's 10 USD minimum notional is refused the same way. |
| **2** | `exodus_native` | **EXODUS** | demo, mainnet; mexc and hyperliquid with entries disabled | Active | Short entry on distressed CARRY pairs prior to settlement. Off with CARRY on both MEXC and Hyperliquid. |
| **3** | `quoter` | **MAKER** | mainnet | Disabled | High-frequency two-sided liquidity provision around fair mid. |
| **3** | `probe` | **PROBE** | demo | Active | Order-path benchmark, not a trading sleeve: one venue-minimum post-only `BTCUSDT` buy 0.5% under the bid every 15 min on the wall clock, pulled 2 s later, so `decide`/`durable`/`wire`/`ack`/`end_to_end` are measured on a day no sleeve trades. A fill is closed at market at once and shows only as an entry blocker; it never raises a strategy error or a Telegram message. Skips the symbol while any other sleeve holds it: a Bybit entry's `stopLoss` (`tpslMode: Full`) names the whole position's stop, and the probe's sits far from the market. |

---

## 2. Common Execution Lifecycle

Every directional sleeve executes via the same deterministic state loop:
1. **Signal Stream**: Signal worker publishes immutable observation over `stream.sock`.
2. **WAL Barrier**: Engine syncs observation to disk *before* triggering reducers.
3. **Pure Reducer**: Evaluates current checkpoint + observation $\to$ outputs target state & effects (zero I/O).
4. **Risk Admission**: Kernel validates gross exposure, quote freshness, and 24h loss ceiling.
5. **Order Dispatch**: Dispatches signed orders over the Bybit mainnet trade WebSocket, Bybit demo REST (no trade WebSocket there), MEXC REST (`/api/v1/private/order/create`, quantities in contracts), or Hyperliquid REST (`/exchange`, a signed L1 action; limit orders only, so a market intent goes out as an IOC limit, and a stop is a separate reduce-only trigger order rather than a field on the position).

---

## 3. `LONG` Sleeve Specification

* **Rule**: `configs/long_native_v12.json`
* **Reducer**: `engine/engine-strategies/src/native_long/`

### Universe & Candidate Admission
* **Eligible Universe**: Top 120 USDT perpetuals by 24h quote turnover ($\ge \$2\text{M}$ volume, $\ge 30\text{d}$ listing history). Symbol leaves below rank 160.
* **Top-50 Filter**: Sub-selected by trailing 90-day volume.
* **Admission Criteria**:
  * **Regime Gate**: BTC and ETH above their 30-day moving averages.
  * **Turnover Rank**: Volume rank $\le 10$.
  * **Price Velocity**: Price move $\ge 15\%$ and $\ge 2.5$ standard deviations.
  * **Range Close Location**: Close in top 30% of bar range ($\text{Close Location} \ge 0.70$; $\ge 0.60$ for multi-day).
  * **Volatility Boundary**: 30-day daily volatility $\le 12\%$.
  * **Capacity & Cooldown**: Maximum 10 concurrent positions; symbol must be outside 7-day cooldown.

### Order Timing & Sizing
* **Entry Execution**: Arms 1 hour after signal. Executes on a 1% price retrace or at 6-hour deadline. Late entries are cancelled. Both realms rest PostOnly for 30 s, then cancel and reconcile fills before an IOC remainder.
* **Sizing Formula**: $\text{Target Weight} = \min\left(0.30, \frac{\text{Gross Capital}}{\text{Open Slots}}\right) \times \text{Vol Target} \times \text{Weekend Mult (1.5)}$.
* **Stop Loss**: Reducer requests $3 \times \text{ATR}$, decaying to $1.5 \times \text{ATR}$ after 48 hours. Account admission caps the actual distance at 10% at 5× and preserves tighter protection.
* **Time Exit**: Unconditional exit after 3 days. No take-profit order.
* **Order Limits**: Skip entries $<\$6$ notional; minimum resize $\ge \$1$ and $\ge 5\%$ notional.

### LLM Entry Gate (Secondary Trigger)
* **Ingestion**: Reads `/var/lib/liquidity-migration/llm-driver-ledger/llm-gate-candidates.json` every minute.
* **Execution**: Candidates scoring $\ge 6$ enter immediately at market (no retrace wait).
* **Tags**: Tagged as `long-native-llm-gate` (ranks 1–10) or `long-native-llm-gate-wide` (ranks 11–30).

---

## 4. `CARRY` Sleeve Specification

* **Rule**: `configs/lane2_carry_hold_v7.json`
* **Reducer**: `engine/engine-strategies/src/native_carry/`

### Universe & Funding Selection
* **Candidate Pool**: Top 100 Bybit perpetuals by trailing 24h turnover.
* **Hysteresis Boundary**:
  * **Entry**: Last settled funding rate $\le -10\text{ bp}$ ($-0.0010$).
  * **Exit**: Settled funding rate rises above $-3\text{ bp}$ ($-0.0003$).
  * **Recovery Exit**: 2-day trailing funding recovery $> 30\text{ bp}$.
* **Toxic Asset Filter**: Excludes symbols with 3-day returns outside $[-30\%, 0\%]$ or 30-day volatility $< 5\%$.

### Sizing Multipliers
Base allocation is 10% per symbol up to 100% gross capital, modulated by four multipliers:
$$\text{Size} = \text{Base} \times M_{\text{depth}} \times M_{\text{persistence}} \times M_{\text{flow}} \times M_{\text{whale}}$$
1. **Depth Multiplier**: $M_{\text{depth}} = \text{clip}\left(\left(\frac{|\text{Funding}_{24\text{h}}|}{120\text{ bp}}\right)^{1.5}, 0.25, 1.0\right)$.
2. **Persistence Multiplier**: Deep-settlement share $\le 10\% \implies M_{\text{persistence}} = 0$.
3. **Turnover Growth Multiplier**: 3-day turnover growth $\le 40\% \implies M_{\text{flow}} = 0.5$.
4. **Whale Positioning Multiplier**: Binance top-trader long/short change $\le -26\% \implies M_{\text{whale}} = 0.5$.

### Holding & Execution

| Setting | Behavior |
| --- | --- |
| Daily book | The native v7 scorer selects names and weights once per UTC day after the configured data lag |
| Quantity | Anchor each target at the first usable decision price; retain it through price moves, partial fills and restart until the next daily decision |
| Funding exits | `early_exit_enabled=false`, `presettlement_exit_enabled=false`; no intraday settled-rate, running-rate or upcoming-book drop exit |
| Daily exits | A name omitted by the current daily decision closes; existing risk stops and explicit reductions remain active |
| Leverage and stop | 5× entry leverage, 3× notional scaling, 10% maximum stop |
| Existing checkpoint | Adopt held quantities on boot when the old target has no quantity anchor; retain already-fired exits until the next daily decision |
| EXODUS handoff | Daily holding emits no new `CarryPresettlementFire`; existing durable events and cover obligations remain available to EXODUS |

---

## 5. `EXODUS` Sleeve Specification

* **Rule**: `configs/lane2_exodus_short_v1.json`
* **Reducer**: `engine/engine-strategies/src/native_exodus/`

* **Trigger**: Consumes `CarryPresettlementFire` event emitted by `carry_native`. Has no independent universe or scoring loop.
* **Daily CARRY mode**: No new source fires; existing EXODUS positions retain their cover and protection paths.
* **Short Entry**: Sells short an exact quantity equal to the CARRY position. Entry window valid from fire time until Settlement + 5 minutes ($S+5\text{m}$).
* **Cover Exit**: Hard time cover executed unconditionally at Settlement + 60 minutes ($S+60\text{m}$).
* **Disaster Fence**: $10\%$ maximum stop-loss.

---

## 6. `MAKER` (Quoter Canary) Specification

* **Rule**: `configs/lane2_toxic_flow_quoter_v1.json`
* **Reducer**: `engine/engine-strategies/src/quoter/`
* **Status**: Deployed in strategy slot 3 on Mainnet; **disabled by default** (`quote_enabled = false`).
* **Model Inputs**: Level-50 order book, microprice, volume imbalance, volatility, queue position, fast/slow flow toxicity.
* **Canary Parameters**: Quotes `AGIUSDT` at $\$5.25$/side, $\$6$ max inventory, $6.5\text{ bp}$ half-spread, requiring $\ge 4\text{ bp}$ net edge.

---

## 7. Account Risk & Collision Rules

1. **Sleeve Attribution**: Sleeves may hold the same symbol, including opposing lots; the portfolio ledger attributes each lot while the venue holds one physical net position.
2. **Shared Capital Limits**: All sleeves draw against the shared gross exposure ceiling defined in the operational profile.
3. **Rolling-Loss Circuit Breaker**: If closed 24-hour engine PnL plus current account open losses reaches the loss ceiling, **all entry orders across all sleeves are immediately blocked**. Existing positions continue to exit normally.
4. **Own-Fills Sizing**: An allocated sleeve plans exits and resizes against its exact owned quantity plus its pending orders (`native_common::planner_facts`); the physical net does not erase an opposing sleeve lot.
   * Exposure no engine order opened — the owner's hand trades included — is nobody's: never resized, never exited, never counted as the sleeve's. A venue position on the other side of the sleeve's own fills is not its holding.
   * The compatibility path without allocated lots retains its scalar grid cleanup and venue cap; it does not define the current exact sleeve allocation.
