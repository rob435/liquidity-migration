# Continuous V2 Exit-Alpha 2b — Vol-Scaled TP Verdict + Exit-Alpha Conclusion

Date: 2026-06-19

Construction: `docs/preregistration/2026-06-19-continuous-v2-f2-exit-alpha-construction.md`
Prior: `...-f2-exit-alpha-sweep-verdict.md`, `...-f2-exit-tp-lifecycle-verdict.md`
Scope: both-venue DD-aware screen (no rebalance). Screen only; not a candidate; operator-gated; no
real-money claim. Run: `backtest-runs/continuous_v2_f2b_vol_tp_2026-06-19/`.

## Proxy validation (vs the flat-TP lifecycle)

The DD-aware proxy (daily equity from per-trade contributions) reproduces the lifecycle venue split:
flat_12 proxy MARΔ = **+1.11 Bybit / −0.82 Binance** (lifecycle: +1.79 / −3.66 — same sign; the proxy
understates magnitude because it omits the vol-target rebalance), and Binance max-DD rises
−0.0314 → −0.0391 (lifecycle direction confirmed). The proxy is therefore trustworthy for the
direction of the vol-scaled question.

## Vol-normalized TP result (T_i = clip(0.10·(rv168_i/median)^p, 5%, 20%))

| policy | Bybit proxy MARΔ | Binance proxy MARΔ |
| --- | ---: | ---: |
| flat_12 / flat_15 | +1.11 / +0.83 | −0.82 / −0.57 |
| vol_norm p=0.5 | +0.71 | −1.39 |
| vol_norm p=1.0 | +0.80 | −1.78 |

## Verdict — vol-scaling does NOT reconcile the split; exit-TP venue split is fundamental

Volatility-normalized TP is **dominated** by the flat raise on both venues: still Bybit-positive /
Binance-negative, and on Binance it is **worse** than the flat raise (−1.4/−1.8 vs −0.8) because
giving high-vol names a wider target amplifies the drawdown — the high-vol Binance fade names are
exactly the ones that keep running against the short. On Bybit it is positive but smaller than the
flat raise (tightening low-vol names cuts some Bybit winners). Falsified as a both-venue reconciliation.

## Exit-alpha overall conclusion (Problem Book F, this session)

Exit alpha was investigated end-to-end and creatively (20+ exit policies across both venues, with
DD-aware validation):

1. **Simple early exits** (shorter hold, time-decay, MFE-giveback, partial): closed — they cut the
   trades that ride to the +10% TP (−36% to −87%, worse than a random-exit null).
2. **Raising the TP** (flat 12%/15%): the one robust improvement, but **single-venue** — lifecycle
   MAR **+1.79 / +2.23 on Bybit** (return +24–31pp, Sharpe 3.71→4.1, bootstrap 90–95%) vs
   **−3.66 / −3.45 on Binance** (drawdown doubles). Venue split → fails the two-venue bar.
3. **Vol-scaled TP**: does not reconcile the split; worse on Binance.

**Root cause (real microstructure):** the fade reversion on Bybit overshoots 10% on the winners
(a wider TP harvests them with stable DD), whereas Binance names that have not reverted by 10% tend
to keep running against the short (especially high-vol ones), so any wider/longer exit explodes
Binance drawdown. This is a true cross-venue disagreement, a regime/microstructure warning — not a
both-venue alpha.

## Attribution — the venue split is drawdown-structure, NOT universe (ledger-checked)

Direct comparison of the TP15 vs control trade ledgers (joined per trade on
component/symbol/entry_signal_ts) rules out the "Bybit-exclusive winners" hypothesis:

- Universe overlap is 252 symbols (99 Bybit-only, 82 Binance-only). The TP15 gain is **mostly
  shared symbols** (Bybit delta +0.054 shared vs +0.022 Bybit-only), the per-symbol delta is
  **+0.62 correlated across venues**, and of 252 shared symbols only 13 gained on Bybit while
  losing on Binance (68 gained on both). Top Bybit gainers (LUMIA/EDU/UMA/UNFI/STO/SYN) are all
  also on Binance.
- Of the control 10%-TP winners, **45% ride to 15% and 55% give back on BOTH venues** (identical),
  with near-identical gave-back raw (+7.8%/+6.6%) and MAE deepening (~−4.6%→−6.4%).
- **Per-trade contribution Δ is POSITIVE on both** (+0.076 Bybit, +0.026 Binance). The venue split
  appears only at the portfolio level: the vol-target rebalance + drawdown. Raising the TP roughly
  **doubles Binance max-DD (−3.27%→−5.61%)** while leaving Bybit's flat (−5.49%→−5.21%, already loose),
  and washes the Binance return gain to flat. Binance's high MAR (8.185) was built on a *uniquely
  tight drawdown*; the wider TP trades that tight DD away (MAR-negative on Binance), whereas Bybit's
  already-loose DD absorbs it (MAR-positive). Same tickers, same trade behavior — different DD edge.
- The Binance damage is **pervasive, not a clustered/avoidable regime**: 17 of 34 months are worse
  under TP15 (negative monthly deltas −0.121 vs positive +0.120 → net ~flat return); Bybit's worst DD
  is unchanged (−5.21% vs −5.49%, same 2024-02 trough). The split is structural, not incidental — it
  cannot be rescued by excluding one bad period.

## Rebalance-off check (is the daily vol curve confounding the conclusion?)

Re-evaluated control vs TP12/TP15 on the fixed-weight component MTM with the daily vol-target
**rebalance DISABLED** (constant gross), reporting Sharpe (rebalance-light) alongside MAR:

| arm/venue | ΔMAR (no-rebal) | ΔSharpe (no-rebal) | ΔmaxDD | ΔMAR (rebalanced) |
| --- | ---: | ---: | ---: | ---: |
| TP12 Bybit | +0.73 | +0.34 | ~0 | +1.79 |
| TP12 Binance | −1.59 | −0.09 | −0.0042 | −3.66 |
| TP15 Bybit | +0.52 | +0.18 | ~0 | +2.23 |
| TP15 Binance | −1.47 | −0.13 | −0.0042 | −3.45 |

Findings: (1) the venue split **persists with the vol curve off** — Bybit improves on return, Sharpe
and MAR with flat DD; Binance buys a negligible +0.5–0.9pp return for a ~47% deeper drawdown and a
LOWER Sharpe (risk-negative even at constant gross). (2) The rebalance **amplifies** the split
(Binance −1.59→−3.66, Bybit +0.73→+1.79) but does not create it — the vol-target levers up good
risk-adjusted configs and penalizes bad ones, magnifying the per-venue Sharpe difference already in
the raw trades. (3) The rebalance is **value-adding** (control MAR 4.23→5.66 Bybit, 5.30→8.19 Binance —
Binance's tight-DD MAR edge is largely the rebalance), so it should not simply be disabled.
Methodological upgrade adopted: report **constant-gross Sharpe alongside rebalanced MAR** going
forward; checked against this session — it changes no conclusion (flow/sizing were return-negative on
Binance; the TP raise is Bybit-only on both lenses).

## Status and the only live lead

- **No both-venue exit-alpha candidate.** Frozen v2 (10% TP, 24h hold) stays.
- **Robust Bybit-only lead:** raising the Bybit component TP to ~12–15% improves Bybit MAR by ~1.8–2.2
  (robust). This is exploratory and operator-gated — a venue-specific exit policy is a separate Book G2
  decision, the cross-venue disagreement is a warning, and per-trade DD-aware lifecycle confirmation of
  any vol/venue-adaptive variant would need a per-trade-TP engine hook (frozen daily-engine change,
  not made here). Recommend: if the operator wants to pursue the Bybit-side gain, register a Bybit-only
  exploratory forward shadow at TP 12%; do not change the both-venue frozen object.
