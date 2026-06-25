# Worker Dispatch Template

Canonical prompt structure for dispatching a worker subagent on a bead.

> **When to use:** every worker dispatch from a manager (Optimus, Tarzan, or any
> SABLE manager). The orchestrator → worker pattern in SABLE.md §6.2 is the
> minimum; this template is the production-grade form for high-throughput
> sessions where ad-hoc prompts have drifted in past sessions.
>
> **When NOT to use:** read-only exploration (Explore/Plan/research subagents),
> one-shot grep tasks. Those don't need the constraint scaffolding.

---

## The slots

Every dispatch prompt MUST fill these. Missing slots are the leading cause of
worker waste (duplicated bead filings, SKIP_PREPUSH bypasses on real failures,
fixing already-fixed bugs).

```
You are working in {WORKING_DIR} on the {BRANCH} branch.

Worktree: {WORKING_DIR}
(Absolute path. The pre-dispatch-refresh hook rebases THIS checkout on the base
branch before you start — SABLE-uz9.15. Keep this structured line intact.)

## Worker model

{haiku | sonnet | opus}

(This must match the bead's `model:` label, OR include a `Model override: <reason>`
line below if you're stepping the model up/down. The pre-dispatch-model-check
hook reads both. Default Sonnet. Apply the ladder — see "Model selection"
section below for rules.)

## Bead

{BEAD_ID}: {BEAD_TITLE}

{PASTE FULL BEAD DESCRIPTION HERE — file paths, acceptance criteria, test spec}

## Verify current state first

Before writing any code, run:

  {VALIDATION_COMMAND}

If it is already clean / passing / the bug is not reproducible, STOP and
report — the bead may be stale. Do not "find something to do" on a clean bead.

## Constraints

- Do NOT use SKIP_PREPUSH=1 / SABLE_SKIP_PRE_PUSH=1. If pre-push fails, STOP
  and report before bypassing. Bypass is for named, tracked infra failures
  only — see "Known acceptable failures" below.
- Run typecheck independently of pre-push (`{TYPECHECK_COMMAND}`) before push.
  Even if you bypass pre-push for a known failure, do not let type errors
  through to CI.
- **Re-fetch and rebase immediately before your final commit** — not just at
  start. Run `git fetch origin && git rebase origin/{BASE_BRANCH}` right before
  you commit/hand off. The pre-dispatch hook rebases at spawn, but in a
  fast-moving multi-manager drain the base advances *during* your task; a commit
  on a spawn-time base surfaces another lane's just-merged work as spurious
  deletions in `git diff origin/{BASE_BRANCH}..HEAD` (observed live: a 40-file
  phantom-deletion diff). Rebase late, not just early.
- Target the correct base branch on the PR ({BASE_BRANCH}).
- After renaming/removing any declaration, grep for ALL references across the
  codebase before closing.

## Known acceptable failures

The following pre-existing failures are tracked and may legitimately surface
in your run. Do NOT file new beads for these — they are already known:

  - {BEAD_ID_1}: {SHORT_DESCRIPTION} ({STATUS_OR_RESOLUTION_PR})
  - {BEAD_ID_2}: {SHORT_DESCRIPTION} ({STATUS_OR_RESOLUTION_PR})

If you hit one of these, note it in your report-back and proceed. If you hit
something NOT on this list, file a fresh bead via `bd q "<one-liner>"` before
fixing.

## Report back

Return:
- PR URL (or "no PR — bead closed locally" if doc-only)
- Bead IDs you closed
- Any new beads filed for sightings (with IDs)
- Any constraint you bent and why
- Test output: the EXACT copy-pasteable command you ran (e.g.
  `cd location-briefing && pytest tests/integration/test_x.py`) AND the relevant
  output lines proving the gate ran. Report the REAL command, not a reconstructed
  path — a wrong path makes the reviewer's re-run `collect 0 items` and falsely
  read as green (observed live, twice).
```

---

## Gate mode (manager-reviewed) vs self-push

SABLE has two dispatch modes. **Which one applies is set by who dispatched you
and is stated in your prompt.**

### Gate mode — DEFAULT for manager (Optimus/Tarzan) dispatch

The manager reviews your work *before* anything is pushed (the APPROVE-PUSH
gate). You do everything up to the push, then **STOP**:

1. Implement the bead(s) in the worktree named by the `Worktree:` line.
2. Run the unit AND integration tests; capture the output.
3. Rebase on the base branch (`{BASE_BRANCH}`).
4. Commit. **Do NOT push, and do NOT open a PR.**
5. Return — as your final message, not a bead — a STOP-BEFORE-PUSH report:
   - **Worktree** path and **branch** name
   - **Parked commit SHA** (`git -C <worktree> rev-parse HEAD`) — the exact
     state you are handing over for review
   - **Test output** — the EXACT copy-pasteable command(s) you ran AND output
     proving unit + integration gates ran green (report the real command, not a
     reconstructed path — a wrong path re-runs as `collect 0 items` and false-greens)
   - Bead IDs ready to close, and any constraint you bent and why

The manager reviews this, and on APPROVE pushes it itself
(`git -C <worktree> push`, gated by `pre-push-rebase-test`). On REVISE you (or a
follow-up worker) get dispatched again into the same worktree. **You never push
in gate mode.**

### Self-push — for low-stakes / Lincoln-utility dispatch

Doc-only fixes, bd hygiene, and Lincoln's own utility spawns may self-push: do
the work, rebase, push, open the PR, and report the **PR URL** per the Report
back rubric above. Use this only when no manager review gate applies.

**If your prompt is ambiguous about which mode, assume gate mode and STOP before
push** — a needless review round-trip is cheap; an unreviewed push is not.

### Warm-pane self-push — DEFAULT in the tmux-native topology

When a manager spawns you via `sable-spawn-worker` (the tmux warm-pane topology,
TMUX-AGENTS-DESIGN.md), you are a **real, warm `claude` session in your own tmux
pane**, and your shell **CWD is your worktree**. The result channel is the bead
pool: the manager watches your bead's status, not a returned message. Lifecycle:

1. Implement the bead(s). Your CWD already *is* the worktree — there is **no
   `git -C`** anywhere in your flow (the old in-process model's `git -C <tree>`
   validated the wrong tree, SABLE-041; warm panes delete that bug).
2. Run the unit AND integration tests; capture the exact command + output.
3. Rebase on the base branch, commit, and **push your own worktree branch**:
   plain `git push` from your CWD. The `pre-push-rebase-test` gate runs; on
   failure STOP and report — do not bypass. The post-push hook files the
   `for-chuck` handoff; **Chuck merges your branch** as usual. You do NOT open PRs.
4. `bd close <bead-id>` with the test evidence (the tdd-gate keys off your real
   session — warm panes satisfy it natively).
5. **Flag done for the reaper:** `tmux set-option -p @sable_status done`. Your
   spawn already set `@sable_role=worker` and `@sable_bead=<id>` on this pane;
   `sable-worker-status --reap` will clean the pane up once you are done.

You self-push your OWN branch only — never another lane's. The manager reviews
the *result* via the closed bead + the `for-chuck` PR; there is no stop-before-push
hand-back in this mode.

---

## Model selection (the ladder)

The bead's `model:` label is the primary signal. If absent, apply the ladder
to pick — and add the `model:` label to the bead via `bd update` so the next
dispatch doesn't re-litigate.

**Default: Sonnet** (claude-sonnet-4-6). All work starts here.

**Step DOWN to Haiku** only if ALL four are true:
- Mechanical work (rename, format, copy-paste pattern, typo, regex replace)
- Deterministic spec (file path + exact change, OR a clear template at N sites)
- Low-risk path (dev tooling, docs, tests, internal scripts, comments)
- No judgment calls — worker purely executes

**Step UP to Opus** if ANY of:
- Design thinking required (which approach? what trade-offs?)
- Security-sensitive path (auth, payments, RLS, PII, secrets, session boundaries)
- Cross-cutting impact (multi-subsystem, ripples through data flow)
- Spec has judgment-call gaps ("decide the right pattern", "investigate why X")
- Unclear / intermittent debugging (race conditions, flaky tests with unknown cause)

**Common mis-classifications to avoid:**

| Tempting wrong call | Why wrong | Right answer |
|---|---|---|
| "Epic child → Opus" | Many epic children are mechanical apply-the-pattern | Apply the ladder per child |
| "Single-file → Haiku" | Single-file auth/payments changes still need Opus | Risk dimension wins |
| "Bug fix → Sonnet" | Typo is Haiku; race condition is Opus | Depends on debugging complexity |
| "sherlock-finding → Haiku" | `sherlock:design-rot` often needs Opus | Per-category; only `sherlock:dead-code` is reliably Haiku |
| "12 files → Opus" | Same pattern at every site is still mechanical | Mechanical-ness wins regardless of count |

**Override syntax.** If the bead has `model:sonnet` but you've decided based on
day-of context that it should be Opus (e.g., the cited code has moved into auth
since the label was set), include a line in the dispatch prompt:

```
Model override: cited code now lives in src/auth/middleware.ts, raised to opus
```

The hook reads that line and allows. Without it, mismatch denies.

---

## Why each slot exists

Every slot above traces to a real failure mode in past sessions. Removing a
slot reintroduces that failure mode. Don't strip the template "for brevity"
without understanding what you're losing.

| Slot | Failure mode it prevents |
|------|--------------------------|
| Working dir + branch | Workers editing the wrong checkout (especially when manager has multiple worktrees open) |
| Bead description paste | Worker re-exploring the codebase when description already names the files |
| Verify current state first | Wasted dispatch on already-fixed beads ("stale bead" pattern); worker writes code for a passing test |
| Constraints — SKIP_PREPUSH | Worker bypasses pre-push on a real failure, ships typecheck regression to CI |
| Constraints — typecheck independently | Pre-push bypass hides type errors that catch in CI anyway |
| Constraints — rebase | Worker pushes on a 30-min-old base, hits avoidable conflicts |
| Known acceptable failures | Workers refile duplicate beads for issues already tracked + claimed |
| Report back rubric | Manager has to re-investigate worker output to know what shipped |

---

## Filling the slots — manager workflow

1. **Pick the bead.** `bd ready` (filtered by your `claim_filter`).
2. **Verify the bead passes the Fresh Agent Test.** If file paths or test spec
   are missing, `bd update <id> --description "..."` first. Don't dispatch
   into ambiguity.
3. **Identify VALIDATION_COMMAND.** What command would prove the bug exists
   right now? That's the worker's first step.
4. **Identify TYPECHECK_COMMAND.** Project-level — `npx tsc --noEmit`,
   `mypy`, `cargo check`, etc.
5. **Populate Known acceptable failures.** Check `bd list --status=in_progress`
   and `bd list --label=coord` for the last hour. Anything that might trip
   this worker goes in the list with its tracking bead ID.
6. **Send the dispatch.** Pre-dispatch hooks (refresh, claim, overlap,
   preempt) fire automatically. The worker starts with a fresh rebase.

---

## Anti-patterns in dispatch prompts

| Anti-pattern | Why | Instead |
|--------------|-----|---------|
| "Use SKIP_PREPUSH=1 if needed" | Soft language reads as permission | "Do NOT use SKIP_PREPUSH. If pre-push fails, STOP and report." |
| Empty Known-failures list ("none known") | Workers refile duplicates of in-flight issues | Always check `bd list --status=in_progress` first; populate even if just one entry |
| Skipping "Verify current state" on bug-fix beads | Worker writes code for a passing test | Always include the validation command — it costs 10s and saves a wasted dispatch |
| Pasting CLAUDE.md or workflow rules into the prompt | Bloats context, dilutes constraints | Workers inherit CLAUDE.md and global hooks already; trust the harness |
| Dispatching without test command | Worker writes code, can't close (TDD gate), bounces back | Always specify the exact `pytest` / `vitest` / `cargo test` invocation |

---

## Compact form for low-stakes dispatches

For doc-only changes, single-line refactors, or bd hygiene work, the full
template is overkill. Compact form keeps the load-bearing slots:

```
Working in {WORKING_DIR} on {BRANCH}.

Bead: {BEAD_ID} — {BEAD_TITLE}
{1-2 line description with file path}

Verify first: {VALIDATION_COMMAND}. If clean, STOP and report.
Do NOT bypass pre-push.
Known issues: none / {LIST}

Run: {TEST_COMMAND}
Close: bd close {BEAD_ID}
Report PR URL.
```

If the bead has any of these properties, use the FULL template, not the compact:
- Touches files claimed by another in-progress bead (overlap warning)
- Has cross-cutting test impact (could trip tripwire tests)
- Is part of an epic with sibling beads in flight
- Is a P0/P1 bug (high cost if worker bounces back)
