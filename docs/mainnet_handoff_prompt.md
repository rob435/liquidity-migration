# Mainnet handoff prompt

Paste into a fresh session started in this repo. Context: `docs/real_money_envelope.md`.

Don't paste credentials into any agent session — write them to the VPS by hand.

```
Read docs/real_money_envelope.md, AGENTS.md, and STATE.md first.

Owner parameters, decided:
  sleeve   : CARRY only (LONG deferred behind B3)
  account  : existing Bybit mainnet main account
  ceiling  : $2,500 USDT absolute
  daily loss: $250

Build the mainnet execution path. Deliver code plus tests, committed, with
scripts/dev.sh check green. Leave credential entry and activation to the owner.

In dependency order:

1. Close the open safety blockers first (anchors in real_money_envelope.md §3):
     B16 — degraded-but-alive startup mode that keeps reconcile, protection sync,
           health publication and the exit path running instead of exiting into a
           2s crash loop (account_service_runner.py:584, Restart=always).
     B5  — assert post-create that the venue applied the attached stop; flatten
           if it did not. Atomic arming is a Bybit behaviour, never exercised
           against a mainnet key.
     B6  — run reconcile_venue_positions per legible symbol rather than gating
           the sweep on account-wide agreement (account_reconcile.py:255).
     B15b— operator-authorized journal transition terminalizing a wedged
           `commanded` order. wedged_command_watch.py already detects them; the
           exit from the state is missing. Do NOT relax no-blind-resend — the
           command may correspond to a live venue order. Get the design reviewed
           before building it.

2. Credential resolution: add an explicit realm-selecting resolver. Realm must
   be an explicit argument, never a default; mainnet unreachable by omission;
   REAL_MONEY stays the arming switch with reject_ambiguous_flag intact.

3. Realm plumbing: add the third ExecutionEnvironment member
   (execution_environment.py:13) and fix ~12 assuming call sites
   (account_route.py:616, account_owner_health.py:183/350,
   account_service.py:135, others). Extend the post-construction endpoint
   assertion in bybit.py to assert whichever realm was selected — never skip it.

4. Separate account journal. The realm is baked into the durable on-disk
   identity and ensure_account_route refuses to rewrite it. Do not migrate the
   demo journal.

5. Instrument rules from read-only get_instruments_info instead of order
   probing, and hard-refuse the order-placing probe outside demo (B17 —
   deploy_vps_live.sh:1052 places live orders up to 200 USDT when rules pass
   half-life).

6. Re-freeze the candidate universe from mainnet
   (account_candidate_universe.py:632). Fix account_venue_accounting.py:693,
   which hardcodes "environment": "demo" into the reconciliation receipt.

7. Real-money authority profile: distinct acknowledgement, mandatory capital
   ceiling in the receipt, mandatory expiry. Cross-check the live book against
   the receipt ceiling, not just the policy file.

Config:
  capital_reference_usdt = 2500    (NOT demo's 250000 — carry_demo.py clamps
                                    sizing to min(anchored_equity, this))
  max_daily_loss_usdt    = 250     (enforced; account_loss_guard.py trips to
                                    run_safety_flat_once)
  recalibrate all six account_risk caps against 2500

Constraints:
  - Unknown safety-critical state fails closed.
  - Don't weaken existing guards to ease the mainnet path. If one is in the way,
    add a realm-explicit branch; don't delete the assertion.
  - The five demo-enforcement layers each need a realm-explicit equivalent, not
    a removal.
  - Full local gate green per commit. Preserve uncommitted idio work in the tree.

Report at the end: what's built, what isn't, and the exact owner steps remaining.
```

## Owner steps

1. API key: contract trading only, **withdrawal disabled**, IP-allowlisted to the VPS.
2. Credentials to the VPS by hand, root-owned `0600`.
3. Confirm no manual/open orders on the account — `require_bybit_demo_order_ownership`
   (`account_service_bybit.py:280`) refuses to start otherwise.
4. Set `REAL_MONEY`, activate.
