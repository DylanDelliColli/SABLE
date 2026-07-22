# merge-gate state (SABLE-jd5fj.5)

The per-repo state the green snapshot writes and the merge gate reads. This
directory is resolved through `git rev-parse --git-common-dir`, exactly like
`mode-state.json` one level up, so **every worktree of this repo shares one
freeze** and no two repos share any.

| file | written by | read by | meaning |
|---|---|---|---|
| `freeze.json` | `sable-snapshot run` (deterministic red), `sable-snapshot freeze` | `sable_gate_promote_lib.assert_not_frozen` | promotion is DENIED (exit 25) |
| `quarantine.json` | `sable-snapshot run` (flake), `sable-snapshot quarantine add` | `sable_snapshot_lib.classify_snapshot` | suites excluded from the freeze **trigger** — they still run, and are still recorded |
| `testmondata-warm` | `sable-merge-gate warm-testmon-cache` (SABLE-jd5fj.8) | `sable_gate_promote_lib._warm_testmondata_source` | the combined-tree impact tier's bin/ pytest half's fallback warm `.testmondata`, used when this repo's own root copy is absent — a fresh checkout's answer to CI's runner-only copy, warmed by running the same full bin/ suite locally, not automatically on every promote |

The state **files** are gitignored and this README is not. That split is
deliberate: a freeze is a fact about a moment on one machine, not about a
commit — a freeze that travelled through a merge would freeze repos that were
never broken — but the *location* is part of the contract and should exist, and
be documented, in a fresh checkout rather than springing into being after the
first run.

## Reading the state

```
sable-snapshot status          # human-readable freeze + quarantine
sable-snapshot status --json   # machine-readable, same facts
```

## Getting out of a freeze

1. **A green snapshot clears it automatically.** Fix the break (the auto-filed
   bisect bead names the deterministically-red suites), let the next scheduled
   snapshot run, and the freeze lifts itself.
2. **Or an operator lifts it on the record**: `sable-snapshot unfreeze --reason
   "..."`. The reason is required.

There is intentionally **no environment variable** that disables the freeze
check. `SABLE_MG_OPTIMISTIC=0` exists because it turns an optimization *off*
(its off-state is the safe one); a freeze bypass would turn a safety mechanism
off, and an env var is exactly the kind of bypass that leaves no name attached.

## Why quarantine is not a skip

A quarantined suite keeps running on every snapshot and its result keeps being
recorded. What quarantine removes is only the suite's ability to **trigger a
freeze**. A skipped suite is a coverage hole nothing measures; a quarantined one
still produces the evidence its flaky-fix bead needs, and a quarantined suite
that goes permanently red stays visible instead of silent.
