# Test-cost audit

Audit date: 2026-07-27–28
Tracking bead: `SABLE-ssu6v`

## Reproduce the reports

Install the pinned CI environment before comparing results:

```bash
python -m pip install -r .github/ci/test-requirements.txt
```

The Python reporter measures the normal pytest invocation. It does not start a
second collection:

```bash
python -m pytest bin/ -q -p no:cacheprovider \
  --sable-test-cost-report=/tmp/sable-python-cost.json
```

The shell reporter similarly runs the authoritative `ALLOW` set once:

```bash
bash .github/ci/shell-run-set.sh \
  --profile /tmp/sable-shell-cost.tsv
```

The static load-boundary check catches a test that starts another test runner
without an explicit, reasoned declaration:

```bash
python bin/columbo-cost-prefilter.py --check-load-declarations
```

Reports use monotonic elapsed time. Compare runs on the same host with no
other SABLE test run in flight. tmux-dependent tests need a host where tmux
socket creation is permitted; a filesystem-only sandbox is not a valid timing
environment for those modules.

## What was accidental

| Cost source | Before | After | Disposition |
| --- | ---: | ---: | --- |
| Empty-install `sable-doctor` classification | 7.22s | 0.84s | One batch classifier call replaces one process per bin |
| `sable-orchestration-install` hook dependency scan | 2.73s | 1.06s | One dependency-closure scan replaces one Python process per hook |
| Doctor integration module | 208s | 43.66s in the final profile | Share immutable install/provenance fixtures; copy only for mutating cases |
| Doctor provenance cases | ~27s | 9.86s | Share the clean repository template |
| Ordinary footprint unit module | 24.06s | 1.85s | Move its two real-bd cases to the existing integration module |
| Reconciliation integration module | 648.30s | 222.96s after `SABLE-9sxao` | Cache one record set per classification, then deep-copy one immutable real-git/real-bd fixture template per test |
| CI bd coverage wrapper | ~115s timeout-class | 1.47s | Stop rerunning two authoritative real-bd suites already in `ALLOW` |
| Post-push isolation wrapper | >45s | 0.28s | One real sentinel plus structural assertions replaces repeated full-suite calls |
| Library identity isolation wrapper | 25.3s | 12.14s | One sabotage case replaces four concurrent copies |
| Gate-promotion test module | 104.48s ordinary | 66.56s declared integration | Correctly tier the existing real-repo/worktree/CLI contract |
| Seat-sighting hook real-bd test | 8.16–12.87s in an ordinary module | 8.44s declared integration | Move the unchanged Dolt/hook boundary test to the existing integration module (`SABLE-kdn3y`) |

The reconciliation production cache is deliberately narrow: cached records
live only inside one branch-classification context. No result survives to
another branch or reconciliation cadence.

`SABLE-9sxao` subsequently removed the remaining fixture churn. The module
builds one real bare repository, clone, embedded Dolt store, and small pool of
generic open/closed work beads per pytest session. Every test receives a deep
copy, with its git remote, beads remote, hooks path, mutable refs, Dolt files,
and in-memory bead pool rebound to that copy. The reconciler still executes as
a subprocess and still queries real git and real `bd` for every verdict. A
fixture guard proves the copies share no mutable files or pool objects.

The same-host module run retained all 24 reconciliation verdict tests, added
one fixture-isolation guard, and fell from the bead's 384.70-second acceptance
baseline to 222.96 seconds (42.0% lower), including the one-time template
build.

The audit also found two correctness defects that timing-only work would have
missed:

- Spawn integration tests inherited the checkout's live mode and failed when
  it was honestly in planning mode. They now use an isolated execution-mode
  state file.
- A fallback-bead test reconstructed a timestamped message with a stale helper
  signature. It now derives the stable prefix through the production
  `message_identity` and `fallback_bead_title` seams, with its negative control
  retained.

## What remains expensive on purpose

The original audit's final green Python profile measured all 2,944 collected tests in
1,599.25 aggregate test-seconds. The largest modules were:

| Module | Seconds | Classification |
| --- | ---: | --- |
| `test_sable_reconcile_handoffs_integration.py` | 391.89 originally; 222.96 after `SABLE-9sxao` | Real bd/git branch-state authority |
| `test_sable_spawn_worker_integration.py` | 214.04 | Real tmux, bd, dispatch, and lifecycle authority |
| `test_sable_screen_integration.py` | 169.54 | Real terminal delivery and synchronization |
| `test_activation_screen_integration.py` | 89.49 | Real activation/runtime screen contract |
| `test_sable_docker_preflight_integration.py` | 76.78 | External CLI/preflight behavior |
| `test_sable_telemetry_integration.py` | 70.58 | Real telemetry lifecycle |
| `test_sable_gate_promote_lib_integration.py` | 66.56 | Real repos, worktrees, CLIs, and bd |
| `test_sable_worker_status_integration.py` | 62.87 | Real tmux worker lifecycle |
| `test_sable_msg_integration.py` | 57.20 | Real tmux delivery/fallback |

The final green shell profile measured 94 authoritative suites in 1,045.86
aggregate suite-seconds. Its leading costs were post-push notification
(74.47s), real-bd overlap dispatch (68.08s), tree-claim's real-bash oracle
(64.29s), mode interlock (56.98s), pre-dispatch claim (51.47s), close-hold
authority (48.11s), and optimistic promotion (46.09s).

Those tests were not deleted or replaced with mocks. The next safe reductions
are local: persistent real-bd fixture stores, fewer process launches against
the same immutable state, and deterministic tmux readiness signals in place
of polling latency. Each module must retain at least one real end-to-end path
for every authority boundary it owns.

### Shell budget follow-up (`SABLE-rm6kl`)

The full `ALLOW` membership remained unchanged at 94 suites. Two consecutive
unrestricted profiles on the audit host were fully green at 883.44 and 897.52
aggregate suite-seconds, below the 900-second `merge_preview` budget. No suite
moved to `EXCLUDE`; real bd/git/tmux boundaries and planted negative controls
remain in the authoritative run.

The largest before/after changes (after is the mean of the two acceptance
profiles) were:

| Suite | Before | After | What changed |
| --- | ---: | ---: | --- |
| Post-push notification | 74.47s | 64.80s | One isolated real-bd store, no accidental preview worker launch, built-in parsing/intersection |
| Overlap dispatch E2E | 68.08s | 38.20s | Keep the real deny/serialize/persistence boundary; leave duplicate decision permutations in the exhaustive unit suite |
| Tree-claim oracle | 64.29s | 44.83s | One input parse and built-in JSON decision emission |
| Mode interlock | 56.98s | 42.71s | One input/state snapshot, one cached leading-command classification, built-in decision emission |
| Pre-dispatch claim | 51.47s | 31.50s | Reuse one isolated real store and bead across state transitions |
| Close-hold authority | 48.11s | 29.50s | Isolated real store with the two load-bearing real dispositions; exhaustive state permutations stay in unit/plant coverage |
| Optimistic promotion | 46.09s | 28.69s | Respect the explicit bd test seam instead of cold-initializing an unrelated host store |
| Impact-tier serialization | 39.27s | 14.34s | Use the bd seam in transport-only cases; retain the concurrent real-bd isolation case |
| Tier-red capture | 25.82s | 1.48s | Do not initialize real bd for injected tier-output fixtures |

This follow-up exposed the recurring architectural cost pattern: a test often
crossed the expensive real boundary for every state-machine permutation, even
after a unit suite had already made those decisions load-bearing. The retained
shape is one real boundary proof per authority, exhaustive fast decision tests,
and an explicit plant where a false green would otherwise be plausible.

The 897.52-second sample leaves only 2.48 seconds of budget headroom. The
contract is satisfied twice, but future work should treat post-push
notification and high-variance concurrent suites as the next optimization
targets rather than weakening the budget or dropping coverage.

### Readiness and fixture follow-up (`SABLE-epi9c`)

The five modules named by the follow-up were profiled together on the same
unrestricted host. The initial premise was only partly correct: spawn-worker,
worker-status, and message integration paid guessed tmux sleeps, while screen
and activation-screen paid repeated cold Git/Dolt setup without using tmux at
all. The fix follows the boundary that actually owns each cost rather than
forcing one synchronization abstraction across unrelated modules.

| Module | Before | After | Reduction |
| --- | ---: | ---: | ---: |
| `test_sable_spawn_worker_integration.py` | 345.75s | 172.78s | 50.0% |
| `test_sable_screen_integration.py` | 241.20s | 118.66s | 50.8% |
| `test_activation_screen_integration.py` | 108.33s | 43.09s | 60.2% |
| `test_sable_worker_status_integration.py` | 72.04s | 46.93s | 34.9% |
| `test_sable_msg_integration.py` | 66.62s | 36.69s | 44.9% |

The final combined run passed all 128 tests in 419.03 wall seconds (418.15
aggregate test-seconds), down from 834.28 wall seconds (833.94 aggregate) for
the 126-test baseline: 49.8% lower, against a required 30%.

The retained contract shape is:

- Screen and activation-screen build their real Git/Dolt/install baseline once
  per session, then deep-copy and rebind each test world. Isolation tests prove
  Git remotes, Beads files, ordinary files, and absolute activation symlinks do
  not share mutable state.
- Spawn-worker, worker-status, and message integration wait on real session,
  pane, content, prompt, marker, and file transitions. The stuck-dialog timeout
  remains a real bounded negative control; it was not converted into an
  immediate assertion.
- The two three-bead bundle-success traversals were one authority exercised
  twice. The retained real-store test now uses repeated `--bundle A --bundle B`
  arguments while still proving self-overlap release, all three prompt members,
  status, assignee, and a real worker pane. The separate real foreign-overlap
  refusal remains.
- The message controlled experiment now uses explicit busy and idle TUI
  stand-ins with readiness markers. A bare Bash prompt was not an honest model
  for the Claude composer's Escape behavior and produced a real flake when the
  old startup sleep was removed.

No production path changed in this follow-up. The speedup came from removing
test harness latency and duplicate traversal while preserving the real
authority boundaries and increasing the suite by two net tests.

## Guardrails and architecture decision

`bin/conftest.py` fails the session when an ordinary test exceeds 10 seconds
or an ordinary module exceeds 45 seconds. Files named `*_integration.py` are
explicitly heavy; an exceptional ordinary test can instead carry:

```text
# sable-test-load: measured-slow -- <specific reason>
```

`columbo-cost-prefilter.py --check-load-declarations` statically rejects
undeclared nested pytest and shell-suite runners. A reasoned boundary uses:

```text
# sable-test-load: nested-runner -- <specific reason>
```

Both authoritative workflows run that static check.

## Final sealed verdict

The exact source state documented here produced:

- Python: 2,928 passed, 16 expected skips, zero failures in 1,603.91s;
  the JSON contains exactly 2,944 test records across 108 modules and zero
  ordinary-load violations.
- Shell: all 94 authoritative `ALLOW` suites green in one profiled pass;
  the TSV contains exactly 94 passing rows.

The architectural rule is intentionally small: keep one normal runner per
sealed verdict, measure it in-band, declare expensive boundaries, and
consolidate setup at the production seam that owns it. This avoids another
web of per-suite hooks while still failing closed when accidental cost enters
the ordinary developer path.

## Merge-preview critical-path follow-up (`SABLE-pfkwc`)

The earlier audit reduced individual test costs but left ci-verify's complete
Python, static, and shell verdicts serialized inside one Actions job. A
faithful bd/Dolt-absent clean-room baseline on current HEAD measured:

| Lane | Wall time |
| --- | ---: |
| Full Python (2,817 passed, 196 skipped) | 304.90s |
| Shell `ALLOW` (94/94 green) | 400.64s |
| Sequential critical path | 705.54s |

Splitting these into multiple Actions jobs would consume multiple slots per
rolling preview and reduce fleet concurrency at the account's 20-job ceiling.
The implemented design keeps one job and overlaps three fail-closed lanes on
its dedicated runner: complete Python, the existing serial shell execution,
and static fixture/classification/load/impact authority. It does not add
within-suite parallelism locally or in CI.

The parent owns each lane's exact PID, records `wait`'s exit status, and replays
logs by stable lane name. A missing status is red. A planted regression suite
proves all three lanes really overlap and that one failed lane fails the
combined verdict.

Within-shell concurrency was measured and rejected. Two shell workers looked
attractive in isolation (282.70s versus 400.64s serial) and produced several
green combined plants near 400s. A later repeat took 508.51s and failed the
real tmux superseding-message integration when corrected delivery exhausted
all eight verified attempts under load. That one-in-several contention flake
invalidated the faster topology. The final design therefore uses only the
coarse independent lanes and retains serial execution inside the shell lane.

The local development host was not used for final latency acceptance once an
unrelated worker's long-running pytest was observed alongside the SABLE run:
that shared-host sample was green but took 715.72s and is local-swarm evidence,
not a dedicated-runner comparison.

Two actual GitHub-hosted executions accepted the implementation at exact source
SHA `3c3b2c5328147b989579e70a86d487ade8c0ffd9`:

| GitHub execution | Job wall time | Sealed verdict | Result |
| --- | ---: | ---: | --- |
| [Representative serialized baseline](https://github.com/DylanDelliColli/SABLE/actions/runs/30236161394) | 11m06s | 10m47s from Python start through shell completion | Green |
| [Acceptance 1](https://github.com/DylanDelliColli/SABLE/actions/runs/30375931009) | 4m14s | 3m45s | Green: 2,809 passed, 208 skipped; shell 95/95; static authority |
| [Acceptance 2](https://github.com/DylanDelliColli/SABLE/actions/runs/30376402148) | 4m11s | 3m45s | Green: 2,809 passed, 208 skipped; shell 95/95; static authority |

The end-to-end job reduction is 61.9% and 62.3%, respectively. The two
acceptance jobs differ by three seconds (1.2%), use one Actions slot each, and
retain the complete Python discovery, serial shell allowlist, and static
fail-closed checks. The baseline is a representative prior source state rather
than a synthetic same-SHA rewrite; upstream changes increased shell membership
from 94 to 95 and changed Python disposition before acceptance, so the table
reports the observed test counts instead of implying identical membership.

The clean-room work also exposed a correctness defect in
`test-doctor-snapshot-staleness.sh`: its fixture always piped the checkout's
working-tree diff into `git apply`, which fails when the checkout is clean.
The fixture now treats an empty tracked delta as a valid no-op and carries a
regression assertion for a clean checkout.

The generalized method and standalone-auditor design learnings are recorded in
[TEST-SUITE-AUDIT-PLAYBOOK.md](TEST-SUITE-AUDIT-PLAYBOOK.md).

The CI measurements use an otherwise isolated runner. They do not establish
local behavior when 15+ workers each launch a scoped test process on one
development host. SABLE deliberately leaves local shell execution serial and
forbids workers from invoking the sealed full suite. Fifteen workers are already
a parallel test scheduler, so enabling inner fan-out per worker would multiply
contention. The broader-than-scoped one-per-host rule is currently prose rather
than admission control. A representative swarm plant and the decision about a
heavyweight-only host token budget were completed in `SABLE-x2r7g`. The plant
found an accidental shared bd/Dolt lookup inside a tmux integration fixture,
not a need for broad admission. The isolated fix and repeated 15-worker
evidence are recorded in
[TEST-CONTENTION-AUDIT.md](TEST-CONTENTION-AUDIT.md).

## Local xdist broad-seat audit (`SABLE-y4nom.7.4`)

This experiment is deliberately separate from `sable-test-contention`. That
tool forbids the sealed full suite and keeps doing so. The xdist ladder holds
VE.2's existing git-common-dir publisher lock for one repo-wide broad-seat
lease, uses only fixed widths (`serial`, `-n 2`, and `-n 4`) with
`--dist=loadscope`, and changes no normal validation command.

The pre-ladder audit examined the shared-resource families called out by
`SABLE-poykv`, plus the resources added since that measurement:

| Surface | Modules inspected | Parallel disposition |
| --- | --- | --- |
| Pytest session artifacts | `conftest.py`, `test_conftest.py` | Controller-owned after the planted xdist fix described below. |
| Merge-gate lock/window state | `test_promote_decision.py`, `test_sable_gate_promote_lib_integration.py` | Real temp repos resolve per-repo state; synthetic-repo cases explicitly point lock/log paths at `tmp_path`. |
| Snapshot/batch/telemetry state | `test_snapshot_classifier.py`, `test_sable_batch_coordinator_lib.py`, `test_sable_telemetry.py`, `test_sable_telemetry_integration.py` | Mutation uses `tmp_path` override seams; shipped repo state and the live beads store are read-only probes. |
| Mode and manager state | `test_conftest_hermetic_env.py`, `test_sable_spawn_manager_integration.py`, `test_sable_spawn_worker_integration.py` | Autouse mode state is per-test. Tmux sockets and worker branch/worktree names are UUID-scoped. Spawn-worker deliberately mutates real repo refs and scratch beads, so repeated identity/tail evidence remains mandatory. |
| Tmux integration | message/session/view/relink/pane/worker-status/stall/recycle integration modules | Every real server uses a unique `tmux -L` socket and teardown; fixed socket names appear only in unit strings/stubs. |
| Git/coverage roots | `test_coverage_floor_integration.py`, `test_diff_cover_scope_integration.py`, `test_sable_recover_integration.py` | Real source root is read or locally cloned; mutable repos/worktrees are under pytest temp roots. |
| Dolt/beads | `test_sable_dolt_push_integration.py`, `test_footprint_lib_integration.py`, identifier-decay and telemetry integration | Dolt config/remotes and ordinary bead mutation are per-test stores. Live-store operations are read-only except spawn-worker's uniquely named scratch beads. |
| Install/doctor surfaces | activation-debt, orchestration-install, bin-install, doctor, onboard, and inline-body-guard integration modules | HOME, install prefixes, and destination trees are all redirected under `tmp_path`; source checkout files are read-only. |
| Docker | `test_sable_docker_preflight_integration.py` | Container names are UUID-scoped, but the daemon and host cgroups are shared. Treat load/tail movement as a measured stop condition. |

The reporter audit found a real precondition failure before any broad ladder
run. A two-worker `loadscope` canary intentionally assigned two fully skipped
modules to separate workers. Pytest's controller reported all 41 skips and the
cost report held all 41 rows across both modules, but the persisted skip-set
baseline held only the 11 rows from one worker. Each worker had run
`pytest_sessionfinish` and written its partial set with a later start timestamp;
the complete controller then correctly refused to overwrite that apparently
newer result. A worker-shaped unit test first reproduced the partial cost write.
`conftest.py` now returns from artifact publication on xdist workers, leaving
the controller—the process that receives every remote test and collection
report—as the sole writer. The repeated real canary produced 41/41 cost rows
and 41/41 persisted skips across both modules. Serial behavior does not take
that branch.

`pytest-xdist` remains a local experiment dependency. It is not added to the
clean-room requirements because this bead neither runs the experiment in CI
nor enables xdist in an authoritative workflow. The dedicated sampler records
collection and pass/fail/skip/xfail identity, per-test and per-module tails,
wall time, timeout rate, child CPU time, and sampled host load. Its result table
and GO/NO-GO decision are appended here after the bounded repeated ladder.
