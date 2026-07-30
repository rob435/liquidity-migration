# Real-Money Envelope

**Status: design only. Nothing in this document is armed.** The repository has
no mainnet code path; `REAL_MONEY` remains rejected at every layer. This file
specifies what must exist, and what must be true, before real capital is put
behind the CARRY and LONG sleeves — and the procedure the owner executes to arm
it. Written 2026-07-30 at owner request ("I want real money on the carry and
long sleeves").

Read this with `docs/governance.md` (Progressive Evidence Model) and the risk
envelope section of `STATE.md`.

---

## 1. What the owner asked for, and what is missing

The instruction was "real money on the carry and long sleeves ... take full
authority". Two of the three inputs a real-money boundary needs were not named:

| Input | Given | Needed |
| --- | --- | --- |
| Which sleeves | CARRY and LONG | — |
| Which account | not named | a dedicated mainnet subaccount, funded only to the ceiling |
| Max capital at risk | not named | an absolute USDT number, bound into the authority receipt |

`AGENTS.md` requires "a separate, narrow owner instruction naming the deployment
and risk boundary". "Full authority" is the opposite of a boundary, so this
document treats the capital ceiling as a **mandatory parameter with no default**.
Nothing arms until it is supplied.

---

## 2. Measured evidence as of 2026-07-30 14:00 UTC

All figures below are read from the venue (Bybit demo), not from the local
journal, and not from backtests. They are the honest forward record.

### 2.1 Lifetime closed P&L is not what it looks like

266 closed-P&L rows, 2026-06-05 → 2026-07-30, 21 distinct days with closes.

```
total closed P&L            +$3,200.21
  of which booked 07-30     +$2,902.11   (91%, across 140 closes)
  everything before 07-30     +$298.10   (over ~8 weeks)
```

The 07-30 figure is **not** profit the strategy generated. 133 of that day's
closing events were `carry resize: depth rescale` — the mark-to-market sizing
defect fixed in `b13cbfac3` and deployed the same day. Those resizes repeatedly
trimmed a winning ESPUSDT position, moving unrealised gain into the realised
column without creating any. Net of the fees it burned, it was value-destroying.

**Read the pre-07-30 line as the price-return track record: +$298 on a $250,000
capital reference over eight weeks.** Approximately flat.

### 2.2 Funding is the entire thesis, and it is real

```
lifetime SETTLEMENT (funding)          +$3,048.83
  before CARRY v3 (to 2026-07-28)        -$148.88   (the account PAID funding)
  2026-07-29                           +$1,630.36
  2026-07-30 (to 14:00 UTC)            +$1,567.35
```

Verified against public funding-rate history — this is genuine market funding,
not a demo artefact. Over the 68 hours to 2026-07-30, a long earned:

| Symbol | Funding to a long | Worst single 4h interval |
| --- | --- | --- |
| `LAUSDT` | **6.355%/day** | −1.9639% |
| `ESPUSDT` | 1.495%/day | −0.7135% |
| `VANRYUSDT` | 1.233%/day | −1.0007% |

So the sleeve works, and it works hard. But understand *why* the rate is that
high: these are violently squeezed small-cap alts where the crowd is short and
paying to stay short. **The strategy is being paid to hold falling knives.**
`LAUSDT` earned roughly 9.5% in funding since entry while its price fell 7.6%.
That is the trade. It is a negative-skew payoff, and the payoff regime that
produces 6%/day funding is not a regime that persists.

### 2.3 Costs

```
lifetime fees                    $85.68
  before CARRY v3                $14.90   (~8 weeks)
  2026-07-29 + 2026-07-30        $70.78   (1.5 days)
```

CARRY has spent 4.75× the account's entire prior fee history in 3% of the
elapsed time. Most of that was the sizing defect. Post-fix the run rate should
collapse; that is the first thing to verify before any capital ramp.

### 2.4 Execution capacity is adequate at current size

Snapshot of the live book against the held position, 2026-07-30:

| Symbol | Held | Spread | Cost to liquidate the whole position | Held / 24h turnover |
| --- | --- | --- | --- | --- |
| `VANRYUSDT` | $9,409 | 2.5bp | 12.9bp | 0.233% |
| `LAUSDT` | $25,677 | 1.9bp | 27.7bp | 0.192% |
| `ESPUSDT` | $25,537 | 1.4bp | 23.4bp | 0.058% |

Every position fills completely inside 200 book levels. The repo's
`MEASURED_ROUND_TRIP_BP = 15.56` is approximately right for routine trading,
mildly optimistic for `LAUSDT`.

**This is a calm-book snapshot and must not be read as a stress estimate.** The
execution scenario that matters is liquidating into the 35% gap-down that trips
`declared_stop_loss_fraction`, when depth is a fraction of the above. Capacity
also degrades superlinearly with size: these numbers license the *current*
notional, not a scaled one.

### 2.5 Forward-record age

| Sleeve | Live since | Forward days | Contaminated? |
| --- | --- | --- | --- |
| CARRY (`lane2_carry_hold_v3`) | 2026-07-29 | ~1.5 | yes — sizing defect for most of it |
| LONG (`v11a`) | earlier, at 0.5× multiplier | longer | owner declined full size 2026-07-28 pending ~4× envelope |

The 2026-07-28 funding double-count correction dropped `carry_hold`'s benchmark
Sharpe to 1.21 (t 2.31), which does **not** beat the CONTINUOUS benchmark. The
earlier claim that it did was a scorer defect, not a result.

### 2.6 Honest summary

The carry mechanism is real and is currently earning well. The forward record
proving it is **1.5 days long and was corrupted by a bug for most of that**. The
price-return record underneath is flat. Real capital on 1.5 days of evidence is
a decision the owner is entitled to make; it should be made knowing that is what
it is, and it argues strongly for the smallest viable starting tier.

---

## 3. Controls that must exist before real capital

From a five-way subsystem audit of the guards, the pre-trade risk machinery, the
venue-protection path, the authority system, and the failure paths (2026-07-30).

### 3.0 What already exists and should not be rebuilt

Credit where the design is genuinely sound — these are the load-bearing pieces a
real-money envelope bolts onto, not things to replace:

- **`AccountExecutionKernel._evaluate_batch` is a real pre-trade gate.** Six
  absolute USDT/leverage limits from `AccountRiskPolicy`, evaluated *inside* the
  journal transaction; a failing batch is rejected atomically and emits zero
  order commands. A strategy bug cannot reach the venue adapter around it.
- **The risk policy is cryptographically bound to the authority.** The authority
  receipt records `ACCOUNT_RISK_POLICY_FILE`'s SHA-256, so the limits cannot be
  edited without invalidating authority and blocking unit start. This is exactly
  the property a capital ceiling needs.
- **Stops are armed atomically with entry.** `stopLoss`, `slTriggerBy=MarkPrice`,
  `tpslMode=Full` ride in the *same* `place_order` call as the entry
  (`bybit_execution_adapter.py:146`), so there is no window where the position
  exists naked — in the happy path.
- **Arming failure fails closed before exposure exists.** A non-reduce-only
  command lacking durable attached protection is refused in pre-flight
  (`bybit_execution_adapter.py:118`) and the kernel independently rejects the
  target (`account_kernel.py:2154`).
- **The risk-reducing bypass is correct.** A strictly risk-reducing batch skips
  every notional/margin/leverage cap by design — you must always be able to
  exit. A stale protection proof likewise cannot block a flatten.
- **Order authority is concentrated in exactly one process class.** Only the
  Bybit account owner (`account_service_runner.py`) touches the venue, through a
  single client. Every strategy producer is credential-free and publishes
  targets into a filesystem inbox. For real money this is the single best
  structural property the system has: the blast radius of any strategy bug is a
  rejected batch, and the credential blast radius is one process.
- **Five independent demo-enforcement layers**, not one: the `REAL_MONEY`
  ambiguous-flag contract, credential resolution with no mainnet branch at all,
  per-object `demo` assertions on every venue-touching class, a canonical
  mutation lease bound to an authenticated demo `userID`, and a root-issued
  authority receipt re-verified before every unit exec.

### 3.1 Hard blockers — real money is unsafe without these

**B1. Limits are admission-time only; nothing re-checks the standing book.**
Every cap is evaluated when a new target batch is admitted and never again as
prices move. A book that passes at entry can drift far past every limit and no
control notices. *Close it:* revalue the standing book against the policy on
each reconcile pass (the loop already runs every 2.0s) and enter a no-new-risk
state on breach.

**B2. No daily-loss or drawdown halt anywhere in the runtime.** The kill
criteria (`sleeve_kill_criteria.py`) are weekly, read-only, and explicitly carry
no operational authority — "a trip is executed by the operator" — with K1 set at
a 30% drawdown and several criteria checked by hand. The only automatic loss
control is the per-position venue stop at 0.35. *Closed:* implemented this
change as `liquidity_migration/account_loss_guard.py` (see §3.3).

**B3. No per-sleeve capital partition.** Despite its name,
`max_component_gross_notional_usdt` is account-wide (`account_kernel.py:2049`).
The owner wants CARRY *and* LONG; as built, one sleeve can consume the entire
envelope and starve the other, and a bug in either is bounded only by the shared
ceiling. *Close it:* per-sleeve sub-budgets in `AccountRiskPolicy`.

**B4. Producer intent and owner ceiling differ by 5–8×.** CARRY's worst case per
name is `capital_reference × 0.1 × 1.0 = $25,000`; `max_symbol_notional_usdt` is
$125,000. Producer-side sizing is advisory — it runs in separate processes and
the owner never re-derives it. A producer bug that mis-sizes by 5× passes the
gate silently. *Close it:* tighten the owner caps to just above intended size so
the gate actually binds.

**B5. Stop atomicity is an unverified venue behaviour.** The no-naked-window
property is Bybit's API behaviour, not a system guarantee, and it has never been
exercised against a mainnet key. If mainnet accepts the market order while
rejecting or dropping the attached TP/SL — a documented class of venue behaviour
— the position opens naked and the only recovery is the ~2s reconcile. *Close
it:* assert post-create that the venue actually applied the stop, and flatten
immediately if it did not.

**B6. Protection re-verification suspends exactly when the account is least
understood.** `reconcile_once` only calls `reconcile_venue_positions` *if there
are no mismatches* (`account_reconcile.py:255`). So the moment anything drifts,
the system stops proving its stops are armed. Worse, scale-in authorisation
trusts a *journal* protection record rather than a venue read
(`account_kernel.py:1371`) — with verification suspended, the kernel will
authorise a scale-in against a stale `active` record. *Close it:* verify and
repair protection per-symbol for every legible venue row, especially when
something else has drifted.

**B7. An incomplete instrument-rules file silently voids the venue leverage
cap.** The second, independent leverage check applies only when
`rules.max_leverage > 0.0` (`account_kernel.py:2006`); a zero or absent value
downgrades it to a no-op rather than failing closed. *Close it:* require the
field to be present and positive for real-money instruments.

**B17. The deploy pipeline places live venue orders by itself.**
`deploy_vps_live.sh:1052` runs `probe_bybit_demo_rules.py --confirm-demo-probe`
automatically whenever instrument rules are past half-life during an ordinary
rollout — including one dispatched from the GitHub Actions dropdown. The probe
**places and cancels real orders** (`demo_rule_probe.py:637`/`:714`) at up to
`REGISTERED_MAX_PROBE_NOTIONAL_USDT = 200.0`, `REGISTERED_PROBE_DISTANCE_BPS =
100.0`, across the candidate universe. **Some fill.** On a real account that is
CI spending your money as a side effect of deploying, with no trading decision
behind it and no line in any strategy's record.

Compounding it: rules are pinned to `environment == "demo"` with a 168h max age
(`account_execution_config.py:63`, `candidate_rule_coverage.py:20`) and a stale
receipt is a *hard owner startup failure* — another route into the B16 crash
loop. `account_candidate_universe.py:632` pins the tradeable universe to
`api-demo.bybit.com`. So real orders would be sized, rounded, and
min-notional-checked against **demo** instrument rules for a **demo-listed**
universe: a tick/lot/min-notional divergence gives venue rejects at best and
mis-sized positions at worst. *Close it:* derive rules from the read-only
`get_instruments_info` rather than by order probing, hard-refuse the
order-placing probe outside demo, and re-freeze the universe from mainnet.

### 3.1b Silent-wrong-realm blockers

These are different in kind from the above. Nothing is missing — the code would
run, and do the wrong thing quietly. Every one of them is a consequence of five
years of "demo is the only realm" being *assumed* rather than *asserted*.

**B8. The realm is chosen by a keyword argument and never verified.** The entire
demo-vs-mainnet decision is delegated to pybit's `demo=` kwarg
(`bybit.py:123`), with no post-construction check of which endpoint the client
actually addresses. If a pybit upgrade renamed or dropped that kwarg, the client
would silently address `api.bybit.com`. Today that fails closed only by accident
— demo keys do not authenticate against mainnet. Once real credentials exist,
that accident stops protecting anything, and it protects in the wrong direction.
*Close it:* assert the resolved base URL after construction and refuse to
proceed unless it matches the intended realm.

**B9. Credential-realm is never verified, only presence.**
`_validate_environments` (`operational_runtime_authority.py:1012`) checks that
`BYBIT_DEMO_API_KEY`/`_SECRET` exist. **Mainnet keys pasted into those variable
names would satisfy that check and every downstream check except the endpoint
itself.** The whole safety architecture rests on an assumption about what is in
a file, which nothing tests. *Close it:* query the key's own metadata and assert
the realm before the first order.

**B10. `ExecutionEnvironment` is a two-valued enum with ~12 assuming call
sites.** (`execution_environment.py:13`, fanning out to `account_route.py:616`,
`account_owner_health.py:183`/`:350`, `account_service.py:135`, and more.) The
realm is also baked into the durable on-disk account identity, and
`ensure_account_route` refuses to rewrite it — so a real-money owner cannot
adopt an existing demo journal, by design. That is correct, and it means the
mainnet path needs its own journal from day one.

**B11. Provenance artifacts are pinned to demo and would need re-freezing.**
`load_candidate_universe` rejects any artifact whose environment is not `demo`
or whose endpoint is not `api-demo.bybit.com` (`account_candidate_universe.py:632`)
— so the tradeable universe itself is demo-locked. Separately,
`account_venue_accounting.py:693` **hardcodes `"environment": "demo"`** into the
reconciliation receipt, so a mainnet reconciliation would emit a receipt
claiming to be demo evidence, which `three_way_reconciliation.py:648` would then
load as such. That is a provenance corruption, not just a cosmetic bug.

**B12. Two classes in the same package default the same field oppositely.**
`BybitPrivateClient.demo` defaults `True` (`bybit.py:110`); `BybitMarketData.demo`
defaults `False` (`bybit_market_data.py:185`, so public REST reads go to
mainnet). An author copying a construction pattern between them gets the wrong
realm with no error. *Close it:* make the field required, with no default,
wherever it selects a money realm.

**Worth knowing:** decision market data *already* comes from the mainnet public
plane while fills come from Bybit's simulated demo engine. The prices and depth
the strategies see are real — which is why the §2.4 depth measurements are
meaningful — but every fill in the track record is simulated. The demo record
contains no evidence at all about real fill quality, queue position, or
rejection behaviour. That is exactly what Tier 1 of the ramp exists to buy.

### 3.1c Unattended-operation blockers

The design is genuinely fail-closed for *new exposure*: stale market data, stale
reconciliation, a dead private socket, an ambiguous order-create, or a missing
native stop all end with health `BLOCKED` and producers refusing to open. It is
fail-**open** in three places, and all three matter more with real money than
with demo, because on demo the cost of being wrong is a log line.

**B13. Nothing in the system ever flattens, cancels, or halts on its own.** The
only automated escalation is a Telegram message, and Telegram failure is logged
and ignored. Every recovery path in the runtime terminates in "a human notices".
That is a coherent choice for a demo account and an unacceptable one for
unattended real capital. *Close it:* the loss guard's `tripped` state must drive
an actual flatten, and delivery failure of the alert must itself be an alarm.

**B14. Software stops are evaluated against an unbounded-age cached book.**
`protection_market_refs()` (`account_service_runner.py:169`) feeds
`recorder.current_book(symbol)` with no age bound, and
`AccountProtectionEngine.evaluate()` (`protection_engine.py:111`) skips a symbol
only on an explicit `sequence_gap` — there is no freshness check at all. **This
is not hypothetical: the public feed times out roughly every five minutes in
current production.** During a hole, the stop silently does not fire while the
real price runs through it, and nothing reports anything wrong. Worse, when a
usable book cannot be built the symbol is dropped from protection evaluation for
that cycle and the fact is only `warning()`-logged
(`account_service_runner.py:739`) — so component protection can be inoperative
for a symbol, cycle after cycle, while the owner publishes `HEALTHY` and
producers keep opening positions elsewhere.

*Scope note, because it changes the severity:* this is the **software** stop
layer. The venue-native stop at Bybit is unaffected by a local data hole, so the
catastrophic backstop still holds. What is lost is every intermediate exit.

**B15. Several unresolvable states wedge permanently instead of escalating, and
a wedged order suppresses exits.** `if symbol in working_symbols: continue`
(`account_kernel.py:2106`) suppresses *all* command generation for a symbol
while any order for it is working. A command can become permanently stuck via
`BybitSubmissionUncertain` (`execution_adapters.py:739`) or via
`StaleUnsubmittedExposureCommand` after a SIGKILL in a millisecond-wide window
(`execution_adapters.py:763` — reachable from an ordinary OOM at
`MemoryMax=512M`, a deploy restart, or a host reboot). The result: **the owner
will never emit an exit for that symbol again** — not from a producer request,
not from convergence — while the position stays open at the venue. And
`converge_once()` returns on the first plan holding a `commanded` order
(`account_service.py:2060`), so one wedged alphabetically-early symbol starves
convergence for *every* other symbol, including the reduce-only residual closes
that finish partially-filled exits.

**B16. Two paths publish `HEALTHY` while blind.**
`run_periodic_reconciliation()` swallows every exception and retains the
previous report (`account_service_runner.py:129`), so persistently failing venue
reads with an empty inbox still publish `HEALTHY`. Separately,
`require_startup_reconciliation_safe()` aborts startup on any mismatch
(`account_service_runner.py:584`), which with `Restart=always` produces a
2-second crash loop during which nothing runs at all — no reconciliation, no
software protection, no exits — for as long as the mismatch persists.

### 3.2 Hardening — should have, survivable without

- **H1. The stale-protection alarm is the wrong shape.** "native protection
  health is stale" is a freshness assertion about the last venue-side proof (a
  4s bound from `reconcile_seconds × 2`), and it shares its wording with three
  sibling gates that mean protection is genuinely absent. Operators will learn
  to discount the string that would also appear if the reconcile loop had died.
  Give the freshness case its own wording.
- **H2. The 4s proof bound will trip far more on mainnet.** It is fed by a 2s
  loop doing 6+ serialised REST round trips; mainnet latency and rate limiting
  are worse and more variable. Each trip blocks new non-reducing execution —
  fail-safe, but a self-inflicted availability problem. Do not widen the bound
  blindly: that timestamp is the only evidence the stop is still there.
- **H3. Release-to-pending retries forever.** A permanently stale clock means an
  entry retrying every ~2s indefinitely rather than escalating. Add a ceiling.
- **H4. `account_gross` is computed from target quantities at the batch
  reference price**, not venue-reported position value, so it measures the
  journal's belief rather than the account. Cross-check against the venue.
- **H5. Bybit's margin tiers are not modelled.** Projected initial margin uses
  requested leverage, not the venue's risk-tier schedule, which raises
  maintenance margin at higher notional. At real size the projection understates
  the requirement.

### 3.3 Built so far

Three changes, all safety-increasing and all active on demo — where they get
exercised long before they guard anything real.

**Closes B4 — the caps now actually bind.** `carry_demo.py` clamps sizing to
`min(decision_anchored_equity, capital_reference_usdt)`, injected from the
profile through `cli.py`. Before this, producers sized off live venue equity
while the owner's six absolute caps were calibrated against a fixed $250,000
reference, so the two drifted apart in both directions: fund below the reference
and every cap sits far above anything reachable, leaving the pre-trade layer
decorative; grow above it — by funding *or simply by profit* — and the load-time
envelope proof in `operational_profile.py:290-424` silently stops being true.
The clamp is a ceiling and never a floor: a smaller account still sizes off its
own equity. Applied after the decision anchor, so a profitable day cannot ratchet
the book up. Three tests, including that the ceiling does not become a floor.

**Closes B14 — software stops can no longer read a frozen book.**
`protection_market_refs` now uses `current_book_with_observed_wall_ns()` and
rejects books older than `PROTECTION_MAX_BOOK_AGE_NS` (15s), and every skipped
symbol now reaches owner health instead of only journald. The defect it closes:
a dropped WebSocket delivers no deltas at all, so the reconstruction stays
healthy and `sequence_gap` stays false while the real price walks away — a
frozen book passed every structural check. Three tests, including a backwards
clock treated as unusable.

*Operational consequence, stated because it is a real behaviour change:* owner
health will now degrade during feed outages instead of publishing `HEALTHY`
while a symbol's protection is inoperative. That blocks new entries during
outages. This is intended — it is the correct response to being blind — but it
will be visible, and the 15s bound should be tuned against observed gap
durations rather than left at a guess.

**Closes B2 — the account-level daily loss halt.**
`liquidity_migration/account_loss_guard.py` + 14 tests.

Account-level rather than per-sleeve, because the failure that matters is the
whole book moving together — precisely what per-position stops cannot see, and
CARRY holds a basket selected *because* their funding is extreme, so their price
moves are far from independent.

Three states, because "I do not know" and "I know it is bad" deserve different
answers:

| State | Meaning | Action |
| --- | --- | --- |
| `ok` | fresh equity, within budget | trade normally |
| `blocked` | equity too stale to judge | no new risk; existing positions stand under their venue stops |
| `tripped` | daily ceiling breached against a fresh reading | flatten and stop; never self-clears |

Design decisions worth stating:

- **Staleness blocks but never flattens.** Flattening on missing data is a risky
  action taken blind, and the public feed drops for minutes at a time in normal
  operation (`ping/pong timed out` roughly every five minutes). A
  staleness-triggered flatten would fire constantly and destroy the book it
  exists to protect.
- **The anchor is the day's open, not its high-water mark.** A trailing variant
  would ratchet the threshold up after a profitable morning and stop the sleeve
  out on ordinary give-back.
- **The anchor is snapshotable.** Without persistence, a crash-loop grants an
  unlimited daily budget one restart at a time. There is a test that documents
  exactly this failure.
- **A trip never clears on its own** — not on recovery, not on a new UTC day.
  Only an explicit operator reset.
- **It runs on demo with the ceiling unset**, so the machinery is exercised and
  observable long before it guards anything real.

Still to wire: evaluation into the account owner's 2s reconcile loop, and the
flatten-on-trip action. Deliberately not wired in the same change that
introduces the component.

### 3.4 Readiness verdict

**The system is not currently safe to run unattended with real capital.** That
is a statement about operational maturity, not about code quality — this is a
carefully built system with better structural properties than most, and the
audit credited them in §3.0. The gap is specific and closeable.

What is genuinely solid: order authority concentrated in one credential-free-
producer architecture; a real atomic pre-trade gate; policy limits bound
cryptographically to the deploy authority; stops armed in the same call as the
entry; and a consistent fail-closed posture toward *opening new exposure*.

What makes it unsafe today, in order:

1. **It cannot stop itself.** No automatic halt, flatten, or cancel exists
   anywhere. Every escalation path ends at a Telegram message that can fail
   silently (B13).
2. **It can reach a state where it cannot exit a position.** A wedged order
   suppresses all command generation for its symbol — permanently — and one
   wedged symbol can starve convergence for every other (B15).
3. **It can be blind and report healthy.** Software stops evaluate against
   unbounded-stale books, and the feed genuinely drops every few minutes; two
   separate paths publish `HEALTHY` while unable to see the venue (B14, B16).
4. **No mainnet path has ever been exercised.** Every fill in the entire track
   record is simulated. Real partial fills, rate limits, position modes, and
   rejections are all untested (B8–B12).

The one real mitigation: the venue-native stop lives at Bybit and is unaffected
by any local failure, so the catastrophic tail is bounded at
`declared_stop_loss_fraction` per position regardless of what the software does.
That converts "unbounded loss" into "a 35% loss per name that the system may be
unable to manage actively". For a bounded Tier-1 stake that is a survivable
shape. For a meaningful allocation it is not.

**Recommendation.** Close B13–B16 and B5, then start at Tier 1 with an amount
whose total loss would be an acceptable tuition payment — not a sized position.
The purpose of Tier 1 is to buy the one thing eight weeks of demo cannot: real
fills. Everything above Tier 1 should wait on both the blocker list and a
forward record materially longer than the 1.5 contaminated days that exist now.

---

## 4. Staged capital ramp

No tier advances on time alone. Each gate is a measured condition.

**Entry precondition for Tier 1, before any capital:** B5 and B13–B16 closed and
demonstrated on demo. Those are the four ways the system can lose the ability to
act while believing it is fine, plus the one way a position can open naked on a
venue it has never spoken to. None of them are visible in a P&L curve, so no
amount of further demo *performance* substitutes for closing them.

| Tier | Capital | Advance gate |
| --- | --- | --- |
| 0 | $0 — demo only | current state |
| 1 | smallest notional clearing venue minimums on 3 names | 5 consecutive days: zero unexplained reconciliation mismatches, zero protection-stale events, zero resize churn, realised cost within 1.5× the 15.56bp model |
| 2 | 10% of ceiling | 10 further days clean, plus one full funding-regime turn (a day where funding on a held name goes positive) survived without breach |
| 3 | 50% of ceiling | 20 further days clean, plus one drawdown ≥5% handled without manual intervention |
| 4 | 100% of ceiling | 30 further days clean |

Tier 1 exists to prove the plumbing, not the alpha: real credentials, real
fills, real funding settlement, real reconciliation. Its purpose is to find the
mainnet-only bugs while the loss is bounded by lunch money.

Any breach at any tier drops to Tier 0, not to the tier below.

---

## 5. Arming procedure (owner-executed)

These steps involve mainnet credentials and real-capital enablement. **They are
executed by the owner. Claude does not perform any of them and does not handle
the credential values.**

1. Create a **dedicated Bybit mainnet subaccount** and fund it with the ceiling
   amount and no more. This is the outermost control and the only one that
   cannot fail open: the venue itself caps the loss at what was funded.
2. Create an API key on that subaccount, scoped to contract trading only, with
   withdrawal permission **disabled** and IP-allowlisted to the VPS.
3. Write the credentials to a root-owned `0600` env file on the VPS by hand.
   Never via a tool, never through argv, never pasted into a chat.
4. Set the capital ceiling and daily-loss limit in the real-money risk policy.
5. Issue a real-money authority receipt naming the ceiling and an expiry.
6. Run the staged install, then activate at Tier 1.

Step 6 is the only one that resembles the deploy path already in use.

---

## 6. Scope limit on this document

This design does **not** remove the `REAL_MONEY` guards, and no change in this
repository will remove them without a further, explicit owner instruction naming
the account and the ceiling. The guards are listed in §3 with the exact
condition each one should become conditional on. Building the envelope first and
opening the valve second is the correct order regardless of authorization: every
control specified here is safety-increasing and inert while the sleeves run on
demo.
