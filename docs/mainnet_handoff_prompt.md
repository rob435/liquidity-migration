# Handoff prompt — build the mainnet execution path

Paste the block below into a fresh agent session started in this repository.
Written 2026-07-30. Context and blocker list: `docs/real_money_envelope.md`.

Two notes before you use it.

**Expect the agent to stop short of arming.** Most capable agents will build the
code path and then decline to enter mainnet API credentials or flip the live
switch, the same way this session did. That is not an obstacle to route around —
those are the two steps that should have a human on them regardless, and the
prompt is written so the engineering completes without them. Budget for doing
steps 8 and 9 yourself.

**Do not paste credentials into any agent session.** Not into chat, not into a
tool call, not "just to test it". Write them to the VPS by hand.

---

## The prompt

```
Read docs/real_money_envelope.md first — it is the design and blocker list for
this work, written 2026-07-30 after a five-way subsystem audit. Then read
AGENTS.md and STATE.md.

Owner parameters, already decided:
  sleeve   : CARRY only (LONG is deferred behind B3, no per-sleeve partition)
  account  : existing Bybit mainnet main account
  ceiling  : $2,500 USDT, absolute
  daily loss limit: $250 (10% of ceiling)

Build the mainnet execution path. Do NOT enable it, do NOT enter credentials,
do NOT activate — leave those to the owner. Deliver the code path plus tests,
committed, with the full local gate green (scripts/dev.sh check).

Scope, in dependency order:

1. Close the four remaining safety blockers before any mainnet work. They are
   specified with file:line anchors in docs/real_money_envelope.md §3:
     B16 — startup mismatch enters a degraded-but-alive mode that keeps the
           reconcile loop, protection sync, health publication and exit path
           running, instead of exiting into a 2s crash loop with real positions
           unmanaged (account_service_runner.py:584, Restart=always).
     B5  — assert post-create that the venue actually applied the attached
           stop, and flatten immediately if it did not. Atomic arming is a
           Bybit API behaviour, never exercised against a mainnet key.
     B15-resolution — an operator-authorized journal transition that terminalizes
           a wedged `commanded` order. wedged_command_watch.py already detects
           and ranks them; what is missing is the exit from the state. Do NOT
           relax the no-blind-resend rule: a command that lost its answer may
           correspond to a live venue order. This one needs owner review of the
           design before you build it — ask.
     B6  — run reconcile_venue_positions per symbol whose venue row is legible
           rather than gating the whole sweep on account-wide agreement
           (account_reconcile.py:255).

2. Credential resolution. bybit.py:resolve_demo_credentials has no mainnet
   branch by design. Add an explicit realm-selecting resolver. Requirements:
   the realm must be an explicit argument, never a default; mainnet must never
   be reachable by omission; and REAL_MONEY must remain the arming switch with
   reject_ambiguous_flag semantics intact.

3. Realm plumbing. ExecutionEnvironment (execution_environment.py:13) is a
   two-member enum; ~12 call sites assume two values (account_route.py:616,
   account_owner_health.py:183/350, account_service.py:135, and others). Add the
   third member and fix every site. Keep DEMO_REST_ENDPOINT-style post-
   construction endpoint assertion from bybit.py — extend it to assert whichever
   realm was explicitly selected, never to skip the check.

4. A separate account journal. The realm is baked into the durable on-disk
   identity and ensure_account_route refuses to rewrite it, so the mainnet owner
   needs its own root. Do not attempt to adopt or migrate the demo journal.

5. Instrument rules from the read-only get_instruments_info endpoint rather than
   from order probing, and hard-refuse the order-placing probe outside demo
   (B17). The probe places live orders up to 200 USDT and is triggered by
   deploy_vps_live.sh:1052 when rules pass half-life — on a real account that is
   CI spending money as a side effect of shipping code.

6. Re-freeze the candidate universe from the mainnet endpoint.
   account_candidate_universe.py:632 pins it to api-demo.bybit.com.
   account_venue_accounting.py:693 hardcodes "environment": "demo" into the
   reconciliation receipt — fix that too or mainnet evidence will be mislabelled
   as demo and loaded as such by three_way_reconciliation.py:648.

7. A real-money authority profile: distinct acknowledgement constant, a
   MANDATORY capital ceiling recorded in the receipt, and a MANDATORY expiry.
   Real-money authority must not be indefinite. Cross-check the live book
   against the receipt ceiling, not only against the policy file.

Configuration for this deployment:
  capital_reference_usdt = 2500      (NOT the demo 250000 — carry_demo.py clamps
                                      sizing to min(anchored_equity, this), so
                                      it is what holds the producer to a
                                      fraction of a larger balance)
  max_daily_loss_usdt    = 250       (already enforced; account_loss_guard.py
                                      trips to run_safety_flat_once)
  recalibrate all six caps in account_risk against 2500, not 250000

Constraints:
  - Never enable REAL_MONEY, never read or write credential values, never
    activate against a live account. Those are the owner's steps.
  - Unknown safety-critical state fails closed.
  - Do not weaken any existing guard to make the mainnet path easier. If a guard
    is in the way, add a realm-explicit branch; do not delete the assertion.
  - The five demo-enforcement layers exist deliberately. Every one of them needs
    a realm-explicit equivalent, not a removal.
  - Full local gate green before each commit. Preserve the uncommitted idio
    work in the tree.

Report at the end: what is built, what is NOT built, and the exact owner steps
remaining to arm it.
```

---

## Steps 8 and 9 — yours

8. **Create the API key.** Contract trading scope only. **Withdrawal permission
   disabled** — that is the one permission that turns a software defect into an
   unrecoverable loss. IP-allowlist it to the VPS.
9. **Write credentials to the VPS by hand**, root-owned, `0600`. Then set
   `REAL_MONEY` and activate.

Before step 9, confirm the account carries no manual or open orders —
`require_bybit_demo_order_ownership` (`account_service_bybit.py:280`) refuses to
start otherwise, and on a main account you also trade by hand, it will never
start.

## The thing worth re-reading before you fund it

`docs/real_money_envelope.md` §2: 91% of lifetime closed P&L is a churn artefact
from a bug fixed the same day; the price-return record underneath is +$298 over
eight weeks; funding is real and large but exists because these are squeezed
small-cap alts, which is a regime, not a constant; and **every fill in the entire
track record is simulated**. Tier 1 exists to buy real fills. Treat the $2,500 as
tuition, not as a position.
