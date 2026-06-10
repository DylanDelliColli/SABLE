---
name: execute
description: |
  Flip the SABLE cockpit into EXECUTION mode — drain the bead pool. Writes the
  cockpit mode-state file via `sable-mode set execution`, spawns Optimus and
  Tarzan as named manager subagents, executes their dispatch requests as
  invisible background workers (Dispatching-for: attribution), and reminds the
  operator to open the Chuck terminal. In execution mode the interlock hook
  blocks spawning planning-only producers — you are draining the pool, not
  filling it.
  Use when asked to "/execute", "enter execution mode", "start executing", or
  "drain the backlog".
allowed-tools:
  - Bash
  - Read
  - Glob
  - Grep
  - AskUserQuestion
  - Agent
---

# /execute — enter EXECUTION mode

You are **Lincoln** in the cockpit seat (see `roles/lincoln.md`). This skill
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

If either fails, the pool is half-formed: return to `/plan`, or drain the
`open-question` beads, before proceeding. This is a **discipline gate, not a hard
lock** — nothing stops you, but skipping it means execution surfaces questions
the human should have answered during planning, which is exactly what staged
planning exists to prevent.

## 1. Flip the mode-state

Run exactly one command:

```bash
sable-mode set execution --fleet optimus,tarzan
```

This writes `~/.claude/sable/state/cockpit-mode.json`. From this point the
`cockpit-mode-interlock.sh` hook is in execution posture: spawning planning-only
producers (sherlock / victor / columbo) is blocked on both the Agent and Bash
legs (soft — `SABLE_COCKPIT_FORCE=1` / `--force` overrides). Mode flips are
mid-conversation; no restart.

## 2. Run the option-A dispatch topology

Managers plan, you dispatch (SABLE-uz9.4):

- **Remind the operator to open the Chuck terminal**
  (`CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager claude`) — the merge
  queue lives there; your pushes file `for-chuck` beads across the bead-DB
  bridge automatically.
- Spawn **optimus** and **tarzan** as named subagents — the operator-visible,
  selectable agents. Each reviews its lane (Optimus: beads with a parent epic;
  Tarzan: standalone/orphan beads), bundles work, and returns **structured
  dispatch requests** to you.
- **Execute each dispatch request as a background worker**: `run_in_background`,
  one `bd worktree create` worktree per worker, the canonical
  `templates/worker-dispatch.md` template, and the attribution line
  `Dispatching-for: <manager>` as the FIRST line of every dispatch prompt
  (the pre-dispatch hooks key lane accounting on it). Workers stay invisible
  to the operator.
- **Workers do not push — you push** after the owning manager reviews the
  worker result; the pre-push gate fires on your session identity.
- **Oversee**: give the operator scoped status, broker `for-lincoln`
  arbitration asks between managers, relay urgent coord beads to idle managers
  on their next spawn. You see every manager's inbox (`cross_inbox_read`) —
  synthesize, don't enumerate.
- You do not write application code and you do not claim beads yourself; the
  managers plan the draining and the workers do it. You direct, dispatch, and
  unblock.

## 3. Hand back to planning

When the pool runs dry or needs regrooming, tell the operator to run `/plan`.
Do not spawn producers yourself from execution mode — the interlock will block
it, and that is correct.
