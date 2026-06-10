# Pre-registration: dynamic-exit FORWARD paper-shadow (2026-06-10, operator-directed wiring)

**Context:** the fade-completion dynamic exit was a pre-registered in-sample NULL
(`continuous-dynamic-exit-2026-06-10.md`): bybit ΔMAR +1.74 but 2026-carried, binance
−2.10 — a cross-venue mirage whose attractive half may or may not be a real recent
bybit-microstructure effect. In-sample evidence CANNOT decide that; forward evidence
can. The operator directed live wiring 2026-06-10; per house rules a failed-Tier-2
idea gets the SHADOW instrument, not orders.

## What is wired (zero order impact)

`liquidity_migration/continuous_dynexit_shadow.py`, called once per demo cycle
(`dynexit_shadow_enabled=True`, fail-safe wrapped): for every fresh live entry, the
shadow arms with `anchor = clip(max(runup24h, ret1), .03, .60)` at the signal bar
(causal, from the WS kline cache) and `target = entry × (1 − 0.5·anchor)`; it exits
on a bar-low touch of the target (fill AT target — the engine TP convention) or at
the real trade's exit (whichever first), logging to
`continuous_dynexit_shadow.jsonl`. The REAL book is untouched.

## A-priori forward bar (frozen now; do not loosen)

After ≥ **60 forward days** with ≥ **40 armed shadows** on the bybit demo book:

- **F1:** shadow book per-trade mean net return (shadow_ret − the same cost basis as
  the real trades) > the real book's over the SAME trades, AND
- **F2:** the shadow improvement holds in BOTH halves of the forward window
  (no single-month carry), AND
- **F3:** the bybit-2026-recency question is answered affirmatively only if F1+F2
  hold — otherwise the shadow is retired and §4-D stays closed.

PASS ⇒ a Stage-2 promotion case may be drafted (operator-gated, fresh receipt,
demo-orders only). FAIL/expiry ⇒ retire the shadow; the in-sample NULL stands as the
final word. The bar is evaluated by comparing the shadow JSONL against the real
trade ledger on matched trade_ids — no re-simulation, no window re-use.
