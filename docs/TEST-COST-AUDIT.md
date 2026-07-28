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
