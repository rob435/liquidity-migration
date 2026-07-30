# Real-Money Envelope

Blocker list and controls for putting real capital behind CARRY. Written
2026-07-30 from a five-way subsystem audit. No mainnet code path exists yet.

**Config:** CARRY only (LONG deferred behind B3), existing main account,
$2,500 ceiling, $250 daily loss limit.

---

## 1. Measured evidence, 2026-07-30

Read from the venue, not from backtests.

**Closed P&L**, 266 rows, 2026-06-05 → 07-30, 21 days with closes:

```
total                    +$3,200.21
  booked 07-30           +$2,902.11   (91%, 140 closes)
  before 07-30             +$298.10   (~8 weeks)
```

The 07-30 figure is the mark-to-market sizing defect fixed in `b13cbfac3`, not
strategy P&L: 133 of that day's closes were `carry resize: depth rescale`,
trimming a winning ESPUSDT position and moving unrealised gain into the realised
column. Read `+$298 on a $250,000 reference over eight weeks` as the price-return
record.

**Funding** — the actual thesis, and real (verified against public rate history):

```
lifetime SETTLEMENT      +$3,048.83
  before CARRY v3          -$148.88   (account was paying)
  07-29                  +$1,630.36
  07-30 (to 14:00)       +$1,567.35
```

| Symbol | Funding to a long | Worst 4h |
| --- | --- | --- |
| `LAUSDT` | 6.355%/day | −1.9639% |
| `ESPUSDT` | 1.495%/day | −0.7135% |
| `VANRYUSDT` | 1.233%/day | −1.0007% |

Rates are that high because these are squeezed small-cap alts — the payoff is
negative-skew and regime-dependent. `LAUSDT` earned ~9.5% funding since entry
while its price fell 7.6%.

**Fees:** $85.68 lifetime, $70.78 of it in the 1.5 days since CARRY v3 — mostly
the sizing defect. Verify the post-fix run rate before any ramp.

**Execution capacity**, calm-book snapshot:

| Symbol | Held | Spread | Full-position exit | Held / 24h turnover |
| --- | --- | --- | --- | --- |
| `VANRYUSDT` | $9,409 | 2.5bp | 12.9bp | 0.233% |
| `LAUSDT` | $25,677 | 1.9bp | 27.7bp | 0.192% |
| `ESPUSDT` | $25,537 | 1.4bp | 23.4bp | 0.058% |

All fill inside 200 levels. `MEASURED_ROUND_TRIP_BP = 15.56` is about right,
mildly optimistic for `LAUSDT`. Not a stress estimate — depth into a 35%
gap-down is a fraction of this, and slippage scales superlinearly with size.

**Forward record:** CARRY (`lane2_carry_hold_v3`) live since 2026-07-29, ~1.5
days, contaminated by the sizing defect for most of it. The 2026-07-28 funding
double-count correction put `carry_hold`'s benchmark Sharpe at 1.21 (t 2.31),
which does not beat CONTINUOUS. **Every fill in the record is simulated** —
Bybit demo prices are real, fills are not.

---

## 2. What already works

- `AccountExecutionKernel._evaluate_batch` — real pre-trade gate, six absolute
  limits inside the journal transaction, atomic rejection, not bypassable.
- Risk policy SHA-256 bound into the authority receipt; limits can't change
  without invalidating authority.
- Stops armed in the same `place_order` call as entry
  (`bybit_execution_adapter.py:146`) — no naked window in the happy path.
- Arming failure fails closed pre-exposure (`bybit_execution_adapter.py:118`,
  `account_kernel.py:2154`).
- Risk-reducing batches bypass every cap by design — exits always possible.
- Order authority in one process; producers are credential-free.
- Five independent demo-enforcement layers.

---

## 3. Blockers

### Closed

| | What | Where |
| --- | --- | --- |
| B2/B13 | Account-level daily loss halt, wired to `run_safety_flat_once` | `account_loss_guard.py` |
| B4 | Sizing clamped to `min(anchored_equity, capital_reference_usdt)` | `carry_demo.py` |
| B14 | 15s book freshness bound; skips reach health | `account_service_runner.py` |
| B8 | Endpoint asserted post-construction | `bybit.py` |
| B15a | Wedged-command detection | `wedged_command_watch.py` |
| B17 | Downgraded — probe already gated by `BybitPrivateClient.__post_init__` | — |

**B4 detail:** producers size off live venue equity; the six caps are calibrated
against a fixed `capital_reference_usdt`. Unclamped, fund below the reference and
every cap sits above anything reachable; grow above it and the load-time envelope
proof (`operational_profile.py:290-424`) silently stops being true. Setting
`capital_reference_usdt = 2500` is what holds the producer to $2,500 on a larger
balance.

**B14 behaviour change:** owner health now degrades during feed outages instead
of publishing `HEALTHY` while protection is inoperative. Blocks new entries for
the duration. The 15s bound is a guess — tune on observed gap durations.

### Open

**B3 — no per-sleeve capital partition.** `max_component_gross_notional_usdt` is
account-wide despite the name (`account_kernel.py:2049`). One sleeve can consume
the whole envelope. Blocks running CARRY and LONG together.

**B5 — stop atomicity unverified against mainnet.** A venue behaviour, not a
system guarantee. If mainnet accepts the market order but drops the attached
TP/SL, the position opens naked. Assert post-create; flatten if absent.

**B6 — protection re-verification suspends on any mismatch.**
`reconcile_venue_positions` runs only `if not mismatches`
(`account_reconcile.py:255`), so verification stops exactly when the account is
least understood. Compounding: scale-in trusts a *journal* record, not a venue
read (`account_kernel.py:1371`). Run per-symbol for every legible venue row.

**B15b — no exit from a wedged command.** `if symbol in working_symbols:
continue` (`account_kernel.py:2106`) suppresses all command generation for a
symbol, including exits. Wedges permanently via `BybitSubmissionUncertain`
(`execution_adapters.py:739`) or `StaleUnsubmittedExposureCommand`
(`execution_adapters.py:763`, reachable from OOM at `MemoryMax=512M`, a deploy,
or a reboot). `converge_once` returns on the first commanded plan
(`account_service.py:2060`), so one wedged early-alphabet symbol starves
convergence for all others. Needs an operator-authorized journal transition —
**do not relax no-blind-resend**; the command may correspond to a live order.

**B16 — startup strictness plus `Restart=always` is a crash loop.**
`require_startup_reconciliation_safe` (`account_service_runner.py:584`) aborts on
any mismatch; during the loop nothing runs — no reconciliation, no protection, no
exits. Needs a degraded-but-alive mode.

**B7** — `rules.max_leverage <= 0` silently voids the venue leverage cap
(`account_kernel.py:2006`). **B9** — credential realm never verified, only
presence (`operational_runtime_authority.py:1012`). **B10** —
`ExecutionEnvironment` is two-valued, ~12 assuming call sites. **B11** —
universe pinned to `api-demo` (`account_candidate_universe.py:632`);
`account_venue_accounting.py:693` hardcodes `"environment": "demo"` into the
reconciliation receipt. **B12** — `BybitPrivateClient.demo` defaults `True`,
`BybitMarketData.demo` defaults `False`.

### Hardening

- Stale-protection alarm shares wording with three gates that mean protection is
  actually absent (`venue_protection.py:1638`).
- 4s proof bound (`reconcile_seconds × 2`) will trip more on mainnet latency.
- Release-to-pending retries forever, no ceiling.
- `account_gross` computed from target quantities, not venue position value.
- Bybit margin tiers not modelled.

---

## 4. Readiness

Fail-closed for *new* exposure. Fail-open on managing existing exposure: B15b
can strand a position, B14's underlying data path and B16 can leave the owner
blind or absent, and B3/B5/B6 remain.

The venue-native stop is unaffected by any local failure, bounding the tail at
`declared_stop_loss_fraction` (0.35) per position. That makes a bounded Tier-1
stake survivable; it does not make a sized allocation safe.

Close B5, B6, B15b, B16 before capital. A dedicated subaccount would put the
ceiling at the venue rather than in software — declined, so the clamp in B4 is
the only thing holding size.

---

## 5. Ramp

| Tier | Capital | Gate |
| --- | --- | --- |
| 1 | venue minimums on 3 names | 5 days: no unexplained mismatches, no protection-stale events, no resize churn, realised cost within 1.5× the 15.56bp model |
| 2 | 10% of ceiling | 10 more days clean + one funding-regime turn survived |
| 3 | 50% | 20 more days clean + one ≥5% drawdown handled unattended |
| 4 | 100% | 30 more days clean |

Tier 1 buys real fills, not returns. Any breach drops to 0, not one tier down.

---

## 6. Arming (owner-executed)

1. Confirm the account has no manual or open orders —
   `require_bybit_demo_order_ownership` (`account_service_bybit.py:280`) refuses
   to start otherwise.
2. API key: contract trading only, **withdrawal disabled**, IP-allowlisted to
   the VPS.
3. Write credentials to the VPS by hand, root-owned `0600`.
4. `capital_reference_usdt = 2500`; recalibrate all six caps against it.
5. `max_daily_loss_usdt = 250`.
6. Issue a real-money authority receipt with ceiling and expiry.
7. Staged install, then activate at Tier 1.

Step 7 only after B17's deploy-time rule probe is confirmed inert for the realm —
it places live orders up to 200 USDT when rules pass half-life
(`deploy_vps_live.sh:1052`).
