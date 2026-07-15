# Repository cleanup handoff

This is a copy-paste prompt for a deliberately aggressive but evidence-driven
cleanup. Do not run it concurrently with the account-execution evidence window
or in its dirty worktree. Large deletion is desirable only when the surviving
runtime, research history, and evidence readers are demonstrably intact.

## Audit snapshot

On 2026-07-14 the workspace occupied about 1.3 GB, but the tracked repository
was only about 8.7 MB across 380 files. The account cutover already deleted 47
tracked legacy files: three systemd units, 15 package modules, seven scripts,
and 22 tests. A second comparably large dead tracked runtime was not identified.

Most visible bulk was ignored local state:

- `.mypy_cache/`: about 193 MB;
- `graphify-out/cache/`: about 178 MB;
- project `__pycache__/` directories outside `.venv`: about 53 MB;
- `.coverage`, `.pytest_cache/`, `.ruff_cache/`, and
  `liquidity_migration.egg-info/`: small disposable products;
- `.venv/`: about 502 MB and recreatable, but useful;
- `data/` and `backtest-runs/`: at least 241 MB and potentially
  evidence-bearing, not cache merely because they are ignored.

There were only two local branches, `main` and
`codex/account-execution-cutover`, plus one stash containing unique historical
documentation edits. There was no branch pile to delete. The cutover worktree
had 94 modified and 58 untracked paths, including the owner's untracked
`STRATEGY_OVERHAUL_MASTER_PLAN_HANDOFF.md`. Blanket ignored-file cleaning could
therefore destroy data, credentials, research artifacts, or user work.

After the cutover is captured and Graphify work is no longer dirty, an explicit
allowlist may remove the caches above without running tests. Do not remove
`graphify-out/graph.json` merely because it is generated while its report or
architecture update is still in flight. Deleting `.venv/` is optional and
requires accepting the rebuild cost. None of these disk cleanups requires or
justifies deleting tracked product history.

The account cutover already removed a substantial direct-execution surface.
Names that look old may still be migration readers, compatibility projections,
artifact verifiers, incident evidence, preregistrations, deploy recovery paths,
or inputs to the separate big-PC alpha program. Age and aesthetics are not
proof of deadness.

## Copy-paste cleanup prompt

```text
Perform a genuinely aggressive, net-deletion repository cleanup in
/Users/jhbvdnsbkvnsd/Desktop/liquidity-migration, but delete only what can be
proved unreachable, superseded, generated, duplicated, or safely archived.
Optimize for a smaller comprehensible repository, not for a dramatic diff.

Authority and prerequisite:
- CLEANUP_AUTHORITY=ANALYZE_AND_IMPLEMENT_TRACKED_REPO_CHANGES.
- EXTERNAL_ARTIFACT_DELETION=NO. DEPLOYMENT=NO. MAINNET=NO. FORCE_PUSH=NO.
- Do not begin implementation while the account-execution cutover has a dirty
  worktree, an active forward-evidence window, or an unverified deployment. If
  that prerequisite is not met, complete only the read-only inventory and stop.
- When implementation is allowed, create a separate codex/repo-cleanup branch
  and worktree from the verified post-cutover main commit. Never reuse the
  evidence worktree and never use cleanup to make the cutover easier to merge.

Read AGENTS.md, STATE.md, docs/governance.md, docs/operations.md,
docs/account_execution_cutover.md, docs/research_summary.md,
docs/preregistration/INDEX.md, graphify-out/GRAPH_REPORT.md, and the repo-map
skill. Verify graph claims against current source, tests, entry points, deploy
files, workflows, artifacts, and runtime config.

Phase 1 — inventory before deletion:
1. Record the exact clean base commit, branches/worktrees and stashes, tracked/
   untracked/ignored paths, package entry points, CLI commands, systemd units,
   workflow paths, shell launchers, import graph, data/evidence readers, and top
   tracked and untracked disk consumers. Never use git clean -fdx, broad rm -rf,
   find -delete, reset --hard, or checkout-discard operations.
2. Create docs/repository_cleanup_inventory.md. Give every candidate one of:
   KEEP, DELETE, ARCHIVE_OUTSIDE_REPO, MIGRATE_THEN_DELETE, or UNKNOWN. Record
   owner, callers, runtime/deploy reachability, persisted-format responsibility,
   evidence value, deletion proof, required migration, and validation.
3. Search more than Python imports. Check pyproject/package data, dynamic
   imports, CLI parser/dispatch, scripts/ops.sh, shell/systemd ExecStart paths,
   GitHub workflows, recovery scripts, docs/runbooks, test fixtures,
   serialization tags, journals/receipts, Graphify references, and external
   big-PC handoffs. An rg miss alone is not deletion proof.

Hard retention rules:
4. Preserve active preregistrations, governance, incident reports, research
   receipts, failed/negative evidence, account journals, reset archives and
   hashes, deployment/recovery contracts, current artifact verifiers, and any
   file required to read retained historical formats. Git history protects only
   tracked bytes; it does not recover untracked or host-only evidence.
5. Do not delete secrets, credentials, VPS state, data roots, evidence roots,
   worktrees, branches, or stashes as part of source cleanup. Report them
   separately with an explicit retention/archive proposal. Never print secret
   contents.
6. Preserve STRATEGY_OVERHAUL_MASTER_PLAN_HANDOFF.md and its referenced big-PC
   contracts unless their owner explicitly retires them after the separate run.
   Keep the .codex and .claude project-skill trees synchronized; their apparent
   duplication is intentional.

Phase 2 — aggressive, reviewable deletion:
7. Delete in dependency order: generated/cache noise and ignore-rule gaps;
   unreachable compatibility exports; dead modules and their tests; retired CLI
   commands/scripts/services/workflows; duplicate configuration and adapters;
   then stale documentation references. Prefer deleting a whole dead path over
   leaving forwarding shims. Do not rewrite a deleted subsystem under a new
   name merely to preserve familiarity.
8. Keep commits single-purpose and net-negative by category. Before each commit,
   show the deletion manifest and proof. If a candidate is UNKNOWN, keep it or
   first add the missing observability/migration; uncertainty is not permission.
9. For persisted formats, first prove retained artifacts can be read by the new
   canonical reader. If a migration is necessary, make it one-way only after a
   verified backup/hash and retain a small fixture plus schema documentation.
   Never mutate historical receipts to make old readers removable.

Efficient validation:
10. Cache-only cleanup requires no tests. During a tracked deletion slice, run
    git diff --check, Ruff for affected surviving code, import/package checks,
    relevant CLI --help routes, systemd/workflow reference checks, and only the
    tests mapped to that slice. Use pytest collection or package-integrity tests
    to catch missing modules early.
11. Batch related deletions. Do not run the full suite after every file. Run the
    appropriate subsystem cluster after each category and one repository-wide
    Ruff, full pytest, scoped mypy, packaging/import-integrity, shell syntax,
    systemd reference, and documentation-link pass after the final cleanup bytes
    are frozen. If that gate fails, debug narrowly and rerun the full gate once
    on the repaired frozen candidate.
12. Compare before/after tracked file count, Python/shell line count, CLI command
    count, systemd/workflow surface, dependency count, test count, and disk
    footprint. A reduced test count is acceptable only when it exactly follows
    removed behavior; coverage of surviving invariants must not fall silently.

Likely later candidates, not preapproved deletions:
13. Reassess requirements.txt only after the big-PC overhaul no longer discovers
    requirements*.txt. Consider moving test-only fault injection support under
    tests rather than calling it obsolete. Reassess compatibility journal/
    projection/bootstrap paths only after the fresh epoch is deployed and old
    sealed roots remain readable through a dedicated offline path. Dated
    experiment dispatchers may be deleted only after their experiment closes
    and the compact falsifier/outcome record remains.

Documentation and graph:
14. Update STATE.md only if operational facts changed; cleanup alone usually
    does not. Update operations/data/PIT/promoted-profile docs for removed
    surfaces, preserve historical documents as dated history, synchronize
    project-skill mirrors, and regenerate Graphify only after the final
    architecture settles and unrelated graph work is safe.
15. Produce a final deletion ledger: removed paths, proof, migrations, retained
    UNKNOWN items, test commands/results, before/after metrics, and rollback
    commit. Do not push main, deploy, or delete the cleanup branch under this
    prompt. Hand back the clean candidate for review.

Definition of done:
- the repository has a materially smaller tracked/runtime surface;
- every deletion is traceable to reachability, supersession, generation, or a
  verified archive/migration decision;
- current demo/paper execution, research artifact readers, CLI/deploy/recovery
  entry points, and retained evidence remain valid;
- the final complete validation passes on one exact clean commit;
- no external data, evidence, secret, main branch, stash, or deployment state
  changed.
```
