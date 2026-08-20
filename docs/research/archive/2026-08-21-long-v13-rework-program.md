# LONG v13 rework program — 2026-08-21

Owner-directed full rework attempt of the LONG sleeve's entries and exits
("v13"). Verdict: **no v13 — v12 stands.** 25 cells across seven mechanism
families; nothing clears the t ≥ 2.5 promotion bar, most lose outright, and
the one real discovery is a decomposition, not a config: the timing edge of
entering a pump before its daily confirmation is +16 bp/trade (t 3.76) on the
pumps that go on to confirm, and it is destroyed by the pumps that do not.

## Method

- Window 2021-01-01 → 2026-08-19 (exclusive), full PIT universe, archive
  manifest filtering, real funding, 3× cost multiplier (45 bp round trip),
  every cell through the historical account kernel — the same accounting that
  registered v12.
- Baseline: the registered module runner reproduces v12's recorded numbers
  (+52.5%, 294 trades, worst dip −3.32% exit-booked). The lab pipeline with
  every hook off reproduces that baseline **trade-for-trade** (exact key
  match, zero numeric drift on entry/exit price, net return, weight) before
  any variant was run.
- Grading: paired daily difference of mark-to-market book returns vs v12
  (n 1942 days), plus total, Sharpe, worst dip, MAR, win rate, best-20
  concentration, per-year splits, and exit mixes. Lane-1 throughout.
- Lab: scratchpad `v13_lab/` (session bbd0a2c8); results in `results.jsonl`,
  per-cell trades CSVs alongside.

## Exit cells — all fail

| cell | mechanism | bp/day vs v12 | t |
| --- | --- | ---: | ---: |
| e1_decay_live ×1.0/1.5/2.0 | decayed stop priced off latest ATR instead of signal-day ATR | −0.01 / −0.04 / −0.10 | −0.06 / −0.77 / −1.08 |
| e2_tp_live ×3/4/5 | take-profit refreshed daily off latest ATR | −0.09 / −0.08 / −0.15 | −0.80 / −1.23 / −1.30 |
| e2_tp_off | no take-profit, graded on total and MAR too | +0.15 (best-20 share 61%→84%) | 0.66 |
| e3_ext entry/atr × +2d/+4d | extend the hold once for trades above water at the clock | −0.27 to −0.50 | −0.92 to −1.28 |
| e4_regime either/both | exit at a daily close when the BTC/ETH entry gate is off mid-hold | −0.01 / +0.01 | −0.09 / 0.38 |
| e5_volfade 30/50 | exit when the name's volume rank fades | −0.02 / +0.00 (1–9 exits fire in 5.6y) | −0.65 / 1.00 |

The stale-anchor critique (everything priced off signal-day ATR) is measured
and does not matter. Extending winners past day 3 loses — the give-back eats
it; the 3-day clock is right. Removing the take-profit adds nothing and
concentrates the sleeve's P&L. Information exits (regime, volume) change
almost nothing because a 3-day hold rarely crosses a regime flip or a volume
fade.

## Price-volume alignment factor — harmful in both uses

`(-1 * sum(rank(correlation(rank(high), rank(volume), 3)), 3))` (owner
suggestion), tested as an entry veto on the most price-volume-aligned
candidates and as a mid-hold exit on alignment spikes:

| cell | bp/day vs v12 | t |
| --- | ---: | ---: |
| pv1_veto worst-10% / worst-25% | −0.52 / −0.66 | −2.37 / −1.98 |
| pv2_exit worst-10% / worst-25% | −0.54 / **−0.93** | −1.86 / **−2.74** |

On these names at this horizon crowding **continues**: the aligned/crowded
state the factor sells is precisely the state this sleeve is paid to hold.
Near-significant in the wrong direction — do not reuse this factor on the
LONG book in mean-reversion form.

## Entry cells — the decomposition that matters

The same filter family evaluated hourly on a rolling 24h window (universe,
regime, ATR, sigma read from the last completed daily row; rolling return,
range location, turnover rank from the trailing 24 hourly bars; one event per
symbol-day, first triggering hour; 970 events vs the daily system's 662
signals):

| cell | trades | total | Sharpe | dip | bp/day vs v12 | t |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| n1_immediate (enter at trigger close) | 488 | +40.5% | 0.92 | −6.2% | −0.37 | −0.55 |
| n1_retrace (1% retrace within 6h, else skip) | 368 | +14.9% | 0.53 | −7.7% | **−1.44** | **−2.58** |
| n1_retrace_ft (retrace else 6h fallthrough) | 486 | +34.2% | 0.83 | −6.9% | −0.61 | −1.00 |

The retrace variant is the worst cell in the program — waiting for a dip
after an intraday trigger fills precisely on the pumps that stall
(negative in four of six calendar years).

Decomposed on the 163 symbol-days both systems traded: the intraday entry is
a median 12h earlier at a median 2.05% better price and earns **+16.0
bp/trade more (t 3.76)** on the same pumps. But the 325 extra pumps that
trigger intraday and then fail the daily confirmation average −4.5 bp at a
42% win rate, and they swamp the timing gain. **The daily confirmation is
information, not latency.**

Confirmation is partially predictable at trigger time — depth ≥ 1.5× the
2.5σ bar confirms 66% vs 33% below 1.2×; triggers after 12:00 UTC confirm
52–55% vs 33% before 06:00; rolling range location predicts nothing (40%
flat). Two pre-declared hybrid cells (early entry only for gated events, the
daily path otherwise):

| cell | early entries | bp/day vs v12 | t | note |
| --- | ---: | ---: | ---: | --- |
| h1_deep_late (depth ≥ 1.5 and hour ≥ 12) | 64 | +0.10 | 0.44 | gain is all 2023; 2025–26 both worse than v12 |
| h2_deep (depth ≥ 1.5) | 104 | −0.02 | −0.08 | 2026 nearly halved |

Also: n2_no_fallthrough (retrace fills only) −0.88 bp/day, t −1.97 — the
deadline fallthrough provides 294 of v12's 514 entries and removing it loses.
The unglamorous hour-6 chase entry is load-bearing.

## What survives

The per-trade timing edge is real and the discriminator it needs (will this
pump confirm?) is not in the price/turnover panel — same conclusion as the
idio-movers study from the other direction. That is the forward-only LLM
driver ledger (`scripts/research/llm_driver_ledger.py`): nominate live
movers, judge the driver before the outcome exists, grade later. An LLM
judged on historical pumps knows how they ended; no in-sample number for it
will ever be honest.

## Lab notes (for the next person who builds on the harness)

- A reused kernel root resumes the previous run's event tape and the event
  clock then refuses the fresh run's earlier timestamps — every cell run must
  start from a clean kernel directory.
- Same-timestamp kernel batches are legal; what is not legal is two
  components of one symbol entering in one netted batch while the driver
  tracks one position per symbol — dedup event entries by symbol per batch.
- The venue nets per symbol: an hourly event stream can produce two triggers
  of one symbol on consecutive day-keys that resolve to the same entry hour.
