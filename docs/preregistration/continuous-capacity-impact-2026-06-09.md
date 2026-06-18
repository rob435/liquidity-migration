# R4/R5 receipt: impact-calibration status + capacity statement (continuous winner+hedge)

**Date:** 2026-06-09. **Program:** live-readiness R4 (impact calibration) + R5 (capacity).
**Current use after 2026-06-18 scrub:** historical capacity and fill-calibration
debt only. The continuous object reset after the 2026-06-18 component-set freeze, so this receipt
must not be cited as current forward-clock evidence or as a real-money deployment
approval.

## R4 — impact calibration vs observed fills: BLOCKED ON OPERATOR (documented)

The modeled cost stack (12 bps fixed round-trip + impact term scaling `scale^0.5`) has
passed 2x/3x stress everywhere, but calibration against OBSERVED fills needs the daily
demo order/fill ledgers, which exist only on the VPS. This box has no `rsync` and no
local demo-ledger copies (only thin paper roots). Unblock = run the documented pull on
a capable box: `bash scripts/reconcile.sh` (pull step rsyncs from `root@116.202.15.128`),
or hand over the `event_demo_*` order/fill datasets. Until then the impact model stays
"stress-tested, uncalibrated" — explicitly carried as a Tier-3 risk item.

**Addendum 2026-06-10:** the 2026-06-09 VPS rebuild invalidated any pre-rebuild
fill history for calibration. R4 is now blocked on fresh observed fills from the
rebuilt continuous demo sleeve; the valid fill clock starts on 2026-06-09.

## R5 — capacity statement (bar PRE-STATED before measurement)

Bar: capacity = largest deployment equity E such that the p95 ensemble trade-entry
participation ≤ 5% of entry-hour turnover, at WORST-CASE applied scale 4 (max4 anchor),
per venue; hourly turnover proxied by daily/24 (measured conservative: entry hours run
~1.4–1.5x the daily average, n=80 sampled exact hours). Trade notional = per-trade
`notional_weight` x component weight x scale x E, over all 3,055 (bybit) / 2,617
(binance) ensemble entries 2023-04..2026-05; turnover joined PIT per entry date.

| venue | p95 participation per $1 | capacity (p95 ≤ 5%) | with ~1.4x hour factor | cap/10 capacity haircut |
|---|---|---|---|---|
| bybit | 1.18e-07 | ~$0.43M | ~$0.6M | **~$43–60k** |
| binance | 2.94e-08 | ~$1.70M | ~$2.4M | **~$170–240k** |

At $1M bybit equity, 19% of entries would exceed 5% of hourly turnover (p95 11.8%) —
not executable as modeled. Cross-check: consistent with the earlier liquid-fade
estimate (~$1–3M cap) under a stricter all-trades bar.

**Honest conclusion: the continuous winner+hedge is a SMALL-BOOK strategy at current
breadth.** The old cap/10 illustration is a capacity haircut, not a deployment
approval and not a Tier-3 pass. Capacity binds on the small-name tail (bybit
especially). Documented lever (NOT run; would need pre-registered validation as a
strategy change): turnover-aware sizing caps (participation-capped weights) would
raise capacity at some MAR cost.

## R3 collector — seeded and verified on real data (same day)

Historical note: `~/SHARED_DATA/continuous_forward_state_2026-06-09/` was seeded
from the then-current component ledgers and verified idempotent on real data. That
clock is no longer current after later config-hash resets. STATE.md is the current
clock source.
