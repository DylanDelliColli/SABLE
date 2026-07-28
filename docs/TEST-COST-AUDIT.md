# Test-cost audit

Audit date: 2026-07-27
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
| Reconciliation integration module | 648.30s | 391.89s | Read a branch's real-bd record set once per classification |
| CI bd coverage wrapper | ~115s timeout-class | 1.47s | Stop rerunning two authoritative real-bd suites already in `ALLOW` |
| Post-push isolation wrapper | >45s | 0.28s | One real sentinel plus structural assertions replaces repeated full-suite calls |
| Library identity isolation wrapper | 25.3s | 12.14s | One sabotage case replaces four concurrent copies |
| Gate-promotion test module | 104.48s ordinary | 66.56s declared integration | Correctly tier the existing real-repo/worktree/CLI contract |

The reconciliation change is deliberately narrow: cached records live only
inside one branch-classification context. No result survives to another
branch or reconciliation cadence.

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

The final green Python profile measured all 2,944 collected tests in
1,599.25 aggregate test-seconds. The largest modules were:

| Module | Seconds | Classification |
| --- | ---: | --- |
| `test_sable_reconcile_handoffs_integration.py` | 391.89 | Real bd/git branch-state authority |
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
