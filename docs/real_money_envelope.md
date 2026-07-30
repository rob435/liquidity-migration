# Real-Money Envelope

Blocker list and controls for putting real capital behind CARRY. Written
2026-07-30 from a five-way subsystem audit; the code path was built the same
day and this file updated with it.

**Config:** CARRY only (LONG deferred behind B3), existing main account,
**ceiling anchored to account equity** — initial margin ≤ equity, gross ≤ 2×
equity, daily loss 10% of equity. No hard money amount binds anything: the
`2500.0` in `configs/operational.mainnet.json` is the declared reference the
load-time envelope proof runs against, and every cap is a ratio of it.

**Built, not armed.** The path exists and is tested; `REAL_MONEY` is unset,
no credentials have been written, and nothing is deployed. §6 is the owner's
remaining work.

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
| B4 | Envelope anchored to observed equity; caps re-proved at each rebase | `equity_anchored_envelope.py`, `operational_profile.py` |
| B5 | Post-create stop assertion; repair on the spot, else latch and flatten | `venue_protection.py`, `bybit_execution_adapter.py` |
| B6 | Protection re-verification is per-symbol, not account-gated | `account_reconcile.py`, `venue_protection.py` |
| B7 | A void `max_leverage` is refused at rule-freeze time | `venue_instrument_rules.py` |
| B8 | Endpoint asserted post-construction, now for **both** realms | `bybit.py` |
| B9/B10/B12 | `VenueRealm`, explicit resolver, third `ExecutionEnvironment` member | `venue_realm.py`, `execution_environment.py` |
| B11 | Universe and accounting receipts carry the realm they were read from | `account_candidate_universe.py`, `account_venue_accounting.py` |
| B14 | 15s book freshness bound; skips reach health | `account_service_runner.py` |
| B15a | Wedged-command detection | `wedged_command_watch.py` |
| B15b | Exits unblocked for a wedged symbol; operator-authorized terminalization | `account_kernel.py`, `account_service.py`, `wedged_command_resolution.py` |
| B16 | Degraded-but-alive startup over open exposure | `account_service_runner.py` |
| B17 | Rules from the read-only endpoint; the order probe refuses off demo | `venue_instrument_rules.py`, `demo_rule_probe.py`, `deploy_vps_live.sh` |

**B4 detail:** producers size off live venue equity while the six caps were
calibrated against a fixed `capital_reference_usdt`. Fund below the reference and
every cap sits above anything reachable; grow above it and the load-time envelope
proof silently stops being true. The profile was always a set of *ratios* with
the reference as its scale, so `capital_reference.mode = "account_equity"` lets
the reference track observed equity and rescales every absolute cap with it —
re-running the proof at each new reference rather than arguing from linearity,
because `max_leverage` and `quantity_tolerance` are exactly what such an argument
would miss. Contraction is immediate, expansion is dead-banded at 5%, unknown
equity moves nothing, and a floor keeps a near-zero balance from producing a
degenerate envelope. The producer's own clamp is disabled in this mode: the
owner's equity-anchored caps are what bind the book.

**B5 detail:** atomic arming is a Bybit behaviour, not a system guarantee. The
adapter now reads position truth back at the create boundary. A dropped stop is
installed on the spot at the exact price the command already carried; when it
cannot be installed the symbol is latched, new exposure stops immediately, and
the next reconciliation converts the latch into the existing breach →
software-flat path. Verification never raises into the ACK — losing the
acknowledgement of a live order is worse than holding an unverified one.

**B15b detail:** three independent parts, none of which resends anything. A
symbol whose every working order is wedged admits reduce-only commands sized
against the *reconstructed position alone* (counting the wedged quantity is what
produced the 110017 reject loops); `converge_once` steps over an unresendable
wedge instead of starving every other symbol; and `ops.sh wedged-command`
terminalizes one on venue evidence. A live order at the venue refuses outright,
an unreadable venue refuses, unreconstructed fills refuse, the venue's own
terminal status resolves on its own evidence, and total absence resolves only
under an explicit operator authorization naming who checked and why.

**B16 detail:** `Restart=always` plus a strict startup check was a 2-second crash
loop during which nothing ran — no reconciliation, no protection, no exits — with
the position at the venue behind only its native stop. Five startup checks now
degrade instead of exiting, latching the failure into published health (which
already refuses exposure-increasing batches) while exits and the safety flat stay
available. A **flat** account with a broken startup check still exits loudly.

**B17 detail:** the demo probe places live PostOnly orders up to 200 USDT per
symbol and the rollout triggers it past half the receipt's lifetime. Off demo,
rules come from `get_instruments_info` — read-only, no lease, no exposure — and
the artifact records `venue_declared` rather than `probe_verified`, because that
is a genuinely weaker evidence standard. Undersized entries are rejected by the
venue at submit rather than pre-empted locally: a rejected order, not a surprise
position.

**B14 behaviour change:** owner health now degrades during feed outages instead
of publishing `HEALTHY` while protection is inoperative. Blocks new entries for
the duration. The 15s bound is a guess — tune on observed gap durations.

### Open

**B3 — no per-sleeve capital partition.** `max_component_gross_notional_usdt` is
account-wide despite the name (`account_kernel.py:2049`). One sleeve can consume
the whole envelope. Blocks running CARRY and LONG together — which is why the
CARRY producer is the only one whose `--execution-environment` accepts
`mainnet`, and why the real-money authority profile authorizes only the mainnet
owner and the mainnet CARRY unit. LONG cannot be pointed at real capital by a
flag, which is a stronger gate than this note.

### Hardening

- Stale-protection alarm shares wording with three gates that mean protection is
  actually absent (`venue_protection.py:1638`).
- 4s proof bound (`reconcile_seconds × 2`) will trip more on mainnet latency.
- Release-to-pending retries forever, no ceiling.
- `account_gross` computed from target quantities, not venue position value.
- Bybit margin tiers not modelled.

---

## 4. Readiness

Fail-closed for *new* exposure, and no longer fail-open on managing existing
exposure: B5, B6, B15b and B16 are closed, so a stranded position, a suspended
stop proof, a frozen symbol, and an absent owner each have a named answer. B3
remains, which is why only CARRY may address the mainnet owner.

The venue-native stop is unaffected by any local failure, bounding the tail at
`declared_stop_loss_fraction` (0.35) per position. That makes a bounded Tier-1
stake survivable; it does not make a sized allocation safe.

**Still unproven, and unprovable before capital:** every fill in the record is
simulated. Bybit demo prices are real, its fills are not. B5's post-create
assertion has never run against a mainnet key, and neither has anything else
here — the code path is tested, the *venue behaviour* it asserts is not. That is
what Tier 1 buys.

A dedicated subaccount would put the ceiling at the venue rather than in
software — declined, so the equity-anchored caps in B4 are what hold size.

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

Every step below is the owner's. Nothing in this repository sets `REAL_MONEY`,
writes a credential, or activates a mainnet unit.

1. Confirm the account has no manual position or open order.
   `require_bybit_demo_order_ownership` refuses to start otherwise — and under
   B16 it now degrades rather than crash-loops if it fails over open exposure,
   which is a worse thing to discover than a clean refusal.
2. API key: contract trading only, **withdrawal disabled**, IP-allowlisted to
   the VPS.
3. Write `BYBIT_REAL_API_KEY` / `BYBIT_REAL_API_SECRET` and `REAL_MONEY=true`
   to `/etc/liquidity-migration/bybit-mainnet.env` by hand, root-owned `0600`.
   These are deliberately *different variables* from the demo pair, so a stale
   demo key cannot authenticate a mainnet run.
4. Install `configs/operational.mainnet.json`. Nothing to recalibrate: every cap
   is a ratio of observed equity.
5. Freeze mainnet instrument rules read-only:
   `scripts/freeze_venue_instrument_rules.py --realm mainnet`. Do **not** run
   the demo order probe; it refuses off demo by name.
6. Freeze the candidate universe from the mainnet endpoint (`--realm mainnet`),
   so it is not labelled — or later loaded as — demo evidence.
7. Issue the real-money authority receipt:
   `--profile real-money`, the distinct acknowledgement constant
   `REAL_MONEY_OWNER_ACKNOWLEDGEMENT`, `--capital-ceiling-mode
   account_equity_multiple --capital-ceiling-value 1.0`, and an explicit
   `--authority-seconds` no greater than 30 days. All three are mandatory;
   there is no unbounded ceiling and no indefinite authority.
8. Staged install, then activate at Tier 1.

The deploy-time order probe is already refused for a non-demo realm
(`DEPLOY_VENUE_REALM`), so step 8 cannot spend money as a side effect of
shipping code.
