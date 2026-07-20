# Strategy Research V7 — offensive thesis slate (Lane-1 draft, 2026-07-20)

Registered as a Lane-1 exploration draft under `docs/governance.md` after the
operator's 2026-07-20 redirection: the program's center of gravity moves from
defensive conditioning to **new-edge theses large enough to change the
strategy and to be verifiable at all**. The book-level risk chassis
(R1/R3/R2 in `docs/tail_risk_program.md`) remains the risk substrate; this
slate is the offense.

Honesty boundary: no thesis can be promised profitable. What is controllable
is the admission bar — only mechanisms whose plausible edge is large enough
to survive measured costs *and* to be adjudicated on a real horizon are
worth testing. Small edges are not just unprofitable here; per T-K they are
unknowable.

## Admission bar (from measured physics, frozen for this slate)

A V7 thesis advances to a Lane-2 config commit only if its Lane-1 evidence
shows, era-stable and with all grid cells reported:

- expected **net ≥ +40 bp per trade** after the frozen 45 bp round-trip
  hurdle and funding (T-K: measured per-bet vol ≈ 1,000 bps and ρ̂ ≈ 0.21
  make anything smaller unverifiable on a useful horizon), **or**
- equivalent book-level economics at deployable gross with ≥ 5 independent
  bets/day.

Anything below the bar is dropped without ceremony and recorded in the
hypothesis ledger. MAR is banned at negative net; uncertainty is computed on
listing-wave / calendar blocks (taxonomy item 29).

## Reserve safety (explicit)

The reserved one-shot surface is the **V2 label-level candidate tape** over
`[2025-01-01, 2026-07-06)` — an object, not a calendar span. V3–V5 already
rendered deployed books through 2026-07 without opening it. V7 Lane-1 work
reads kline/funding/OI/manifest series and new event surfaces; it does not
read the V2 tape. The reserve stays closed for R2.

## Theses

### T-L — Young-listing lifecycle sleeve (flagship)

**Mechanism.** The deployed universe requires listing age ≥ 240 days —
the entire young-listing population is untraded today. New perp listings
carry the largest structured flows in this market: listing-day pump,
day-2→14 bleed, extreme funding, turnover decay. This is where "liquidity
migration" literally happens, and none of the 29 prior hypothesis families
touched it (the age gate predates them all): a genuinely new family.
**Why the edge should be large.** Listing-week moves are tens of percent;
the largest characteristic contrast V2 ever measured on the *old*
population was listing age. **Data:** in hand (Bybit manifest launch
provenance + 1h klines + funding 2021→2026; Binance 2020→ as robustness).
**Lane-1 design (first cells run 2026-07-20):** event-time panel day 0→30
for every listing, left-censoring excluded, era-split thirds; naive
equal-notional arms — short d1→d7, short d2→d7, short d2→d14, long d0→d2
continuation — net of 45 bp + funding; block bootstrap by listing month.
**Advance/drop:** admission bar above; execution caveat recorded (listing-
week spreads exceed the measured deployed-flow costs; a dedicated cost read
is required before any Lane-2 commit).

### T-M — Funding-extreme carry harvest

**Mechanism.** During pump episodes crowded longs pay shorts funding that
annualizes to hundreds of percent. Short the perp for the *carry*, not the
direction, with the directional residual hedged (BTC leg via the existing
hedge machinery; same-name cross-venue perp where both list). New family:
carry capture, not price momentum. **Why large:** episode funding of
0.3–1.0%/8h dwarfs the 45 bp hurdle when persistence exceeds a day.
**Data:** full funding history both venues in hand. **Lane-1 design:**
distribution of funding episodes (rate × persistence × symbol age),
carry-capture P&L for entry thresholds ≥{0.15,0.3,0.5}%/8h with hedge-cost
model, era-split, all cells. **Advance/drop:** admission bar; the hedge
residual must be measured, not assumed.

### T-N — Cascade-riding long (anti-book made offense)

**Mechanism.** The liquidation-cascade state the R2 governor detects is,
inverted, an entry signal: ride confirmed cascades with sizing-bounded risk
(no per-trade stops — closed line). Starts from the frozen C-H1/C-H2
estimands and their multiplicity rule. **Data:** OI/LSR/taker/premium in
hand; forward liquidation prints once P0.3 records. **Sequenced after
T-L/T-M Lane-1**, shares the P2.1 feature build.

### T-O — Cross-venue listing lead-lag

**Mechanism.** Symbols trading on an incumbent venue (MEXC/Gate/HL) before
a Bybit listing give the Bybit open a price-discovery anchor; deviation
from the incumbent path at listing is tradeable drift. **Blocked on new
venue data** (acquisition project; P3 tier). Not started.

### T-P — Young-listing long continuation

The long side of T-L (day-0/1 momentum continuation with the existing
FOMO-chase machinery on the <240d population). Evaluated from the same T-L
panel; second-order.

## Provenance

Everything here is Lane-1 on seen or newly-acquired-unopened data surfaces;
each thesis's evidence card records which. Config commits (registration),
hypothesis-ledger rows, and kill criteria precede any forward grading.
No runtime, sizing, or deployment change is authorized by this draft.
