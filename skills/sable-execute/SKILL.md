---
name: sable-execute
description: |
  Flip SABLE into EXECUTION mode — drain the bead pool. Writes the
  mode-state file via `sable-mode set execution`, then stands up the manager
  fleet on demand: `sable-spawn-manager --all` opens optimus, tarzan, and
  chuck as warm claude panes in their own hidden windows and kicks their
  operating loops (the session itself is Lincoln-only until now —
  sable-launch is mode-neutral). Managers spawn a worker pane per bead;
  workers self-push their worktree branches; Chuck merges. In execution mode
  the interlock hook blocks spawning planning-only producers — you are
  draining the pool, not filling it.
  Use when asked to "/sable-execute", "enter execution mode", "start executing", or
  "drain the backlog".
allowed-tools:
  - Bash
  - Read
  - Glob
  - Grep
  - AskUserQuestion
---

# /sable-execute — enter EXECUTION mode

You are **Lincoln**, the orchestrator main session (see `roles/lincoln.md`). This skill
flips you into **execution mode**, whose single job is to **drain the bead
pool**.

## 0. Verify handoff readiness (soft gate)

Before flipping to execution, confirm the backlog is actually drainable —
otherwise you send managers into under-scoped work. Two checks:

- **Planning reached the end.** `sable-mode substage get` returns
  `decomposition` (or a prior planning session already handed off). If it returns
  an earlier substage, the staged flow isn't finished.
- **No open questions remain.** `bd ready -l open-question` is empty — every
  ambiguity the human needed to resolve has been resolved.

If either fails, the pool is half-formed: return to `/sable-plan`, or drain the
`open-question` beads, before proceeding. This is a **discipline gate, not a hard
lock** — nothing stops you, but skipping it means execution surfaces questions
the human should have answered during planning, which is exactly what staged
planning exists to prevent.

## 0.5 Docker Supabase preflight (hard gate, SABLE-n5rb)

Before any lane touches Docker Supabase, run:

```bash
sable-docker-preflight
```

This detects the dockerd/containerd ghost-container desync class that
corrupted a shared pgdata volume after the 2026-07-07 WSL hard reboot
(forensics + recovery runbook: market-brief-package-9scm) — containerd can
revive pre-crash containers as tasks dockerd no longer tracks while dockerd
separately starts its own visible generation, and both postgres processes end
up writing the SAME volume. This recurs on every hard reboot/freeze, so it
must be caught here, before `sable-spawn-manager --all` lets any worker near
the db.

- **Exit 0 — clean.** Proceed to step 1.
- **Nonzero because docker/Supabase isn't part of this project** (stderr/JSON
  errors say `docker not found` or `no supabase_db_* container found`) — not
  applicable here, proceed to step 1.
- **Any other nonzero — HARD STOP.** Do not flip the mode-state, do not spawn
  managers. Paste the diagnosis to the operator (which of phantom
  containers / ghost cgroup tasks / dual postmaster fired, and the specific
  IDs/timestamps reported) and point at the runbook the tool prints
  (market-brief-package-9scm). Wait for the operator to run recovery before
  retrying this step.

## 1. Flip the mode-state

Run exactly one command:

```bash
sable-mode set execution --fleet optimus,tarzan,chuck
```

This writes the **per-repo** mode-state file — `<repo>/.claude/sable/state/mode-state.json`
when inside a git repo (resolved from the git common-dir, so all of the repo's
worktrees share one mode), or `~/.claude/sable/state/mode-state.json` outside a
git repo. Because the mode lives in the repo, you can run a separate SABLE
session in **another** repo at the same time — e.g. plan project B while project
A executes — without the two clobbering each other's mode. From this point the
`mode-interlock.sh` hook is in execution posture: spawning planning-only
producers (sherlock / victor / columbo) is blocked on both the Agent and Bash
legs (soft — `SABLE_ORCHESTRATION_FORCE=1` / `--force` overrides). Mode flips are
mid-conversation; no restart.

## 1.5 Seed the active-contracts surface (SABLE-9ozz)

The mode flip alone is invisible to a manager pane that **restarts** mid-drain
(`/clear`, crash, session limit): the pane re-boots on its STATIC role card and
loses every conversation-state convention this fleet is running under — the
merge-gate sole-path contract, any interim worker cap, the manual-relay rule
while a hook is dark. That was the 2026-07-13 gah9 bypass: a restarted chuck
merged with bare `git merge --no-ff` because his static identity still described
the old manual flow. Persist the live contracts to disk so `session-role-anchor.sh`
surfaces them into every fresh boot's identity:

```bash
sable-contract set  "Merges go ONLY through sable-merge-gate. NO bare git merge/push on any integration branch."
sable-contract add  "Workers self-push their worktree branch; the post-push hook files for-chuck; Chuck merges via the gate."
# add any interim fleet rule live this shift, e.g.:
# sable-contract add "Interim worker cap: 2 per manager until SABLE-p8rf lands."
```

`sable-contract` writes `<repo>/.claude/sable/state/active-contracts.md`, colocated
with the mode-state (same per-repo resolution). Update it the moment a protocol
flips — a contract change that lives only in this conversation dies with the next
restart. Clear a rule with `sable-contract clear` / re-`set` when it no longer
applies.

## 2. Bring up the warm-pane session

Execution runs on the **tmux warm-pane topology** — the only execution topology
(see `TMUX-AGENTS-DESIGN.md`): every role is a real, persistent `claude` session
in its own tmux pane with an env-var identity (`CLAUDE_AGENT_NAME`), registered
in the role→pane registry (`@sable_role` pane option) that `sable-msg` and the
worker-spawn tooling resolve against.

Determine which of two states you are in:

- **You are the lincoln pane** of a running sable session (check:
  `CLAUDE_AGENT_NAME` is `lincoln` and `$TMUX` is set;
  `tmux display-message -p '#{@sable_role}'` prints `lincoln`). This is the
  normal case — `sable-launch` creates a Lincoln-only session. **Stand up the
  fleet now**: run `sable-spawn-manager --all` — each manager (optimus,
  tarzan, chuck) opens as a warm `claude` session in its OWN detached window
  (your window is not disturbed), launched with a bypass permission posture
  and kicked into its operating loop. Idempotent: already-running managers are
  skipped. The interlock allows this only in execution mode — which you just
  set in step 1.
- **No sable session exists yet** (`tmux has-session -t sable` fails). Tell the
  operator to run `sable-launch` from a plain terminal (it wraps `sable-tmux`,
  creates the Lincoln-only session, and attaches — `tmux attach -t sable`),
  continue this conversation in the **lincoln pane**, then stand up the fleet
  as above.

How the drain works (all of it happens in the panes, not in your context):

- **Managers (optimus, tarzan)** drain their lanes from `bd ready`: verify each
  ready bead, claim it, and **dispatch their own workers** — one ephemeral
  worker pane per bead via the worker-spawn helper (worktree = pane CWD, model
  pinned from the bead's `model:` label, pre-dispatch governance runs inside
  the helper). Managers review results through the bead pool; they do **not**
  push worker code.
- **Workers** do TDD in their own worktree, pass the gates, **self-push** their
  own worktree branch from their pane CWD, close their bead with gate evidence,
  and flag `@sable_status=done`.
- **Chuck** is the merge-queue **pane**. A manager's push notifies him
  message-first (`sable-msg chuck`, sent by the post-push hook), with a durable
  `for-chuck` bead as the fallback when his pane is unreachable; he merges,
  replies, and idles. There is no second terminal to open.
- **Reap** finished worker panes with `sable-worker-status --reap`.
- **Peek** at any pane or hidden worker window with `sable-view` (status
  table), `sable-view <role>` (focus), or `sable-view <role> --tail` (read
  without switching).

## 3. Talk to the managers (sable-msg)

Lead↔manager conversation is low-volume, direct, and message-first:

```bash
sable-msg optimus "status?"                      # queued behind the current turn
sable-msg tarzan  "drop the auth epic, API is urgent now" --interrupt
```

`--interrupt` sends Escape first so the message lands mid-turn instead of
queueing. Every injected turn opens with the fixed header
`⟦SABLE-MSG⟧ from=<sender>` — the framing rule that lets every pane distinguish
agent traffic from the operator. Replies from managers arrive in your pane the
same way; treat any turn without that header as the human.

## 4. Oversee

- Give the operator scoped status: managers report over `sable-msg`; work state
  and worker results live in the bead pool (`bd show`, `bd list
  --status=in_progress`) — synthesize, don't enumerate.
- Broker arbitration: when a manager messages you with a conflict or a
  priority question, decide (or relay to the operator) and reply via
  `sable-msg`.
- You do **not** write application code, claim beads, dispatch workers, or
  push. The managers plan, dispatch, review; the workers build and self-push;
  Chuck merges. You keep the session coherent.

## 5. Hand back to planning

When the pool runs dry or needs regrooming, tell the operator to run `/sable-plan`.
Do not spawn producers yourself from execution mode — the interlock will block
it, and that is correct.
