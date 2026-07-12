# Bybit Demo Minimum-Order Probe — 2026-07-12

## Evidence card

- **Claim:** determine the smallest executable USDT-linear demo order and test
  whether `minNotionalValue` is the only binding venue constraint.
- **Mode:** bounded `forward_execution` on Bybit demo; no mainnet authority.
- **Scope:** current XRPUSDT instrument filters and market-order behavior at
  `2026-07-12T21:02:51Z`. BTCUSDT/ETHUSDT values below are contemporaneous
  public-filter diagnostics, not submitted-order probes.
- **Validity:** valid for the narrow current venue-mechanics claim. It is not
  alpha, capacity, or strategy-performance evidence.
- **Authorization:** owner requested a minimum-order test and a 10x demo
  functional stress on 2026-07-12.

## Safety boundary

Before the probe, the demo account had equity `$10,030.1017491`, zero positions,
and zero open orders. The continuous and long demo order writers, the hedge
timer/service, and the risk reconciler were stopped for the bounded mutation.
The probe asserted a globally flat account before sending anything. All
previously active services were restored afterward.

## Instrument snapshot

XRPUSDT reported:

- last price `1.0999`;
- `qtyStep=0.1`;
- `minOrderQty=0.1`;
- `minNotionalValue=5` USDT.

The smallest step-aligned quantity meeting the notional rule was therefore
`4.6 XRP`, worth `$5.05954` at the snapshot price.

## Direct demo results

| Probe | Quantity | Snapshot value | Result |
| --- | ---: | ---: | --- |
| Off-grid quantity | 4.65 XRP | $5.114535 | Rejected: `Qty invalid`, Bybit `10001` |
| Below minimum value | 4.5 XRP | $4.94955 | Rejected: minimum order value 5 USDT, Bybit `110094` |
| Calculated minimum | 4.6 XRP | $5.05954 | Filled at 10x exchange leverage |

The accepted buy (`5357ea57-bd91-4735-bdaa-391cdb898cb3`) filled 4.6 XRP at
`1.0999`; the reduce-only sell (`2ff52196-d1c8-4151-933f-998a1560c59a`)
filled the same quantity and price 526 ms later. Each taker execution charged
`0.00278275` USDT, for total probe fees of `0.00556550` USDT.

Postcondition: zero positions and zero open orders across the demo account.

## Conclusion

Approximately `$5.06` was directly executable on XRPUSDT. The proposition that
minimum notional is the only relevant constraint is contradicted: a larger
`$5.11` order was rejected solely because its quantity was off the 0.1 grid.
The effective minimum is symbol-specific and must combine `qtyStep`,
`minOrderQty`, `minNotionalValue`, and current price.

At the contemporaneous public snapshot, the same calculation yielded roughly
`$64.18` for BTCUSDT (`0.001 BTC`) and `$18.21` for ETHUSDT (`0.01 ETH`), despite
both advertising a 5 USDT minimum notional. Those two values explain why a
small beta target can remain unexecutable even after removing a fixed $25
strategy-side deadband.
