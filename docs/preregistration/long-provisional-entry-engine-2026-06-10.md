# Active Receipt: LONG PE2 Provisional Entry

**Status:** not adopted; future-OOS re-judgment armed.

## Mechanism

`fc_provisional_entry=True` enters at the trigger hour with ATR stops active from
entry and cuts at an unconfirmed daily close. No other v11a+div+volup125
parameters change.

## In-Window Verdict

PE2 failed its registered cross-venue adoption bar, mainly by missing the Binance
ret/DD threshold by about 1%. The flag remains implemented, tested, and default
off. It is not part of the active LONG profile.

## Only Revival Path

Re-judge PE2 only when both full-PIT roots extend at least 60 days past
2026-05-28 and both venues have enough provisional-entry trades.

Forward/OOS pass requires the same spirit as the original bar: both venues must
improve without a Binance rescue, active-day fraction must not degrade, and the
result must survive the normal cost/stress and Tier ladder. If it fails that
fresh bar, remove the dormant flag.

Historical PE1/PE2 tables are in git history.
