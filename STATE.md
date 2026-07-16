# Operational State

Updated from authenticated and systemd checks at 2026-07-16 00:40 UTC. These
facts describe the installed commit named below, not the uncommitted cleanup.

## Verified pre-cutover host state

- Installed and authorized commit:
  `2d42c7b78bd4945d65eb90f6a3e33d1b2e901cf2`, profile
  `demo-operational`.
- Demo owner and demo LONG/CONTINUOUS producers were active. Paper was excluded
  by both the old receipt and the host sleeve override. Bulk collectors and raw
  account-market persistence were off.
- Authenticated Bybit demo reads showed zero non-flat positions and zero regular
  or conditional orders before the owner was recycled.
- The installed owner had rebuilt a 2.75 GiB resident set and pushed the 4 GiB
  host into 2 GiB of swap. The cause is the installed commit's per-symbol raw
  record retention, not account exposure. Recycling the proved-flat owner
  restored about 3.1 GiB available memory and reduced swap use to 42 MiB.
- The cleanup commit is not yet installed or authorized. It removes that raw
  retention, bounds live-L2 subscriptions to active work, checkpoints unchanged
  reconciliation truth, fixes the liveness timestamp race, and adds per-service
  memory ceilings.
- The tracked hedge history is an immutable model prior through 2026-07-09,
  not a live-extended tape. Activation checks its schema, provenance, causal
  boundary, and estimator sufficiency; elapsed wall time does not invalidate it.
  Its coefficients can drift and are not current calibration or performance evidence.
- Paper has not yet been started under the isolated account-owner topology.
- Mainnet, `REAL_MONEY`, and real-money credentials remain unauthorized.

Historical candidate, rule-probe, reset, and flatness receipts are evidence for
their exact old commits only. They do not authorize this changed tree.

## Intended runtime

The intended cutover mode is `operational`: demo and isolated paper account
owners, enabled demo/paper LONG and CONTINUOUS producers, continuous demo
hedge/RMOM timers, and demo-paper liveness. Its commit-owned paper model is
explicitly `integration_only_uncalibrated`; paper is neither performance
evidence nor full hedged-portfolio parity. Both profiles require raw bulk market persistence
disabled; live L2 readiness, decision books, canonical journals,
reconciliation, and protection remain mandatory.

Paper runs as a non-login user without demo/mainnet credentials, with private
state roots, byte-identical isolated candidate/rule/risk inputs, narrow
read-only access to shared public snapshots, and hard memory/swap ceilings.
Those controls bound failure; they do not prove that all six persistent workers
fit the 4 GiB host. Activation therefore requires a multi-cycle resource soak.

## Required next sequence

1. Bind the validated cleanup to one exact clean commit.
2. While the fleet is quiescent, run the stopped `install` phase for that commit.
3. Recheck environment files, roots, candidate universe, rules, risk policy,
   sleeve toggles, authenticated demo flatness, and hedge model-prior identity.
4. Issue a new create-only operational authorization for the exact installed
   commit, machine, profile, environment bytes, runtime inputs, and root identities.
5. Run `activate`, then the read-only `verify` path. Owners start before producers;
   any mismatch fails closed.

Commands and required handshakes are in `docs/operations.md`. Architecture and
claim limits are in `docs/account_execution.md`.

## Research state

No confirmatory experiment is active. The canonical cancellation and retired
evidence record is `docs/research_summary.md`; it grants no runtime authority.
