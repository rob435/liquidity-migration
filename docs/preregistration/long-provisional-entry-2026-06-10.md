# Pre-registration: PE1 — provisional trigger-hour entry for the long FC sleeve

**Date:** 2026-06-10 (registered BEFORE the decisive numbers are computed).
**Label:** `exploratory` Stage-0 (vectorized estimate from the LR-scout machinery; an
engine build follows only on a GO). **Parent:** LR program receipt
(`long-regularity-program-2026-06-10.md`) — all LR cells + the scout GO bar FAILED;
this is a NEW mechanism with its own receipt, not a rescue of a failed cell.

## Motivation (from today's pre-registered scout, not mining)

The LR-scout found, cross-venue: hourly-detected FC events that the daily close
CONFIRMS are worth +8.3/+10.8/+11.0% (bybit) and +5.9/+8.0/+6.1% (binance) mean net
at 24/48/72h FROM THE TRIGGER HOUR; events the close does NOT confirm are negative
everywhere (−0.8..−2.9%). The deployed engine enters confirmed events roughly a DAY
after the trigger hour (signal close + 1h + sniper retrace). Hypothesis: a
PROVISIONAL entry at the trigger hour — cut at the daily close if confirmation
fails, keep (engine lifecycle) if it confirms — captures continuation the day-late
entry gives away, net of the cut losses on unconfirmed entries.

## Stage-0 estimate (fixed a-priori; one scout extension, no engine change)

Per hourly event from the LR-scout event set (same triggers, dedup, cooldown):
- `entry_trig` = trigger-hour close (12 bps RT charged on every provisional entry).
- If the trigger DAY's daily bar confirms (the daily-replica trigger fires for that
  symbol/day): hold to +72h from trigger; compare against the ENGINE PROXY for the
  same event = entry at the first hourly close of the NEXT UTC day + 1h, exit at the
  same +72h-from-trigger timestamp, 12 bps RT. (Proxy bias acknowledged: the engine's
  sniper retrace improves its real entry by up to ~1%; the margin bar below exists
  partly to absorb this.)
- If the day does NOT confirm: cut at the trigger day's last hourly close
  (23:00 UTC), 12 bps RT — the provisional loss.
- Venue book EV: mean over all events of the provisional-policy net, vs mean over
  CONFIRMED events only of the engine-proxy net scaled by the confirmation rate
  (the engine only ever trades the confirmed class).

## GO bar (engine-grade build proposed only if ALL hold)

1. Provisional-policy EV ≥ 1.25× engine-proxy EV on BOTH venues (the 1.25 margin
   absorbs the sniper-retrace proxy bias + intraday execution realism).
2. Cut-loss distribution bounded: worst-decile cut loss ≥ −10% (no hidden tail worse
   than the ATR-stop class).
3. Confirmation-classification timing is honest: the cut decision uses ONLY the
   trigger-day's completed daily bar (23:00/24:00 close), never later data.

FAIL → record; the long sleeve's execution stands (sniper retrace remains the
validated entry); the LR program closes with all menus exhausted. No further
execution-timing variants on this window.

## Artifacts

Extension columns in `C:/Users/user/SHARED_DATA/long_hourly_scout_2026-06-10/`
(`<venue>_events_pe.parquet` + `report_pe.json`), script
`scripts/long_hourly_fc_scout.py` (`--pe` mode).

## Verdict (filled in after the run) — FAIL on bar 2; recorded as a STRONG LEAD

- Bar 1 (EV dominance) PASSES both venues: provisional-policy EV +3.24%/event vs
  engine-proxy +0.65% (bybit); +1.03% vs −0.51% (binance). On confirmed events the
  trigger-hour entry nets +11.04%/+6.13% mean vs +1.73%/−1.38% for the next-day
  entry — most of FC's 72h continuation happens in the first hours after the
  trigger; the day-late entry gives it away. Cross-venue sign agreement.
- Bar 2 FAILS on binance: unconfirmed-cut p10 −12.85% (bar ≥ −10%); bybit −9.57%
  passes. The scout carries no intraday stop, so the tail is the unstopped one.
- Honesty note on magnitude: the engine PROXY badly understates the real engine
  (the actual binance engine returned +22.8% on this window; the proxy EV is
  negative) because it omits the sniper retrace and ATR TP/stop lifecycle. The
  EV RATIO is therefore not citable; the dominance DIRECTION (cross-venue, large)
  is the finding.

**Disposition (per the pre-registered FAIL clause):** no engine build proposed from
this receipt; the sniper-retrace entry stands; no further execution-timing variants
on this window. RECORDED LEAD for any future program: an engine-grade provisional
trigger-hour entry with ATR stops ACTIVE FROM ENTRY (which caps exactly the bar-2
tail) + cut-at-unconfirmed-close, evaluated against the REAL engine lifecycle on
both venues under a fresh receipt. That is the one evidence-backed path to "more
frequent + earlier" found tonight; everything else densifying the long sleeve is a
confirmed null (LR receipt).
