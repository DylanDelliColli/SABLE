---
name: execute
description: |
  Flip the SABLE cockpit into EXECUTION mode — drain the bead pool. Writes the
  cockpit mode-state file via `sable-mode set execution`, spawns Optimus and
  Tarzan as resident manager subagents that dispatch their OWN workers and push
  their own approved work, and reminds the operator to open the Chuck terminal.
  In execution mode the interlock hook blocks spawning planning-only producers
  — you are draining the pool, not filling it.
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

## 2. Run the native-spawn dispatch topology

Managers dispatch and push their own lanes (SABLE-uz9.11); you spawn them and
oversee:

- **Remind the operator to open the Chuck terminal**
  (`CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager claude`) — the merge
  queue lives there; the managers' pushes file `for-chuck` beads across the
  bead-DB bridge automatically.
- Spawn **optimus** and **tarzan** ONCE per execution session as **resident**
  named subagents with `run_in_background: true` — ALWAYS background, never
  foreground (a foreground Agent call blocks the main conversation). Residents
  run a rolling poll loop for the whole session — ongoing context windows that
  accumulate lane knowledge across tasks; they are operator-visible and
  selectable. Each works its lane (Optimus: beads with a parent epic; Tarzan:
  standalone/orphan beads).
- **Managers dispatch their own workers** now (nested spawn, CC 2.1.177,
  SABLE-uz9.8/uz9.9): each manager creates a worktree (`bd worktree create`),
  spawns background workers via the Agent tool filling
  `templates/worker-dispatch.md` gate mode (the pre-dispatch governance hooks
  fire on the manager's own Agent call with its lane identity), reviews the
  stopped-before-push results, and **pushes approved work itself**
  (`git -C <worktree> push`, gated by `pre-push-rebase-test`). You do NOT
  execute dispatch requests and you do NOT push — that relay is gone.
- **Oversee**: give the operator scoped status, broker `for-lincoln`
  arbitration asks between managers, relay urgent coord beads. You see every
  manager's inbox (`cross_inbox_read`) — synthesize, don't enumerate. When a
  manager files a `shift-report` (context pressure / stand-down), respawn it
  fresh — lane state rehydrates from beads.
- You do not write application code, claim beads, dispatch workers, or push.
  The managers plan, dispatch, review, and push their lanes; you spawn them and
  keep the session coherent.

## 3. Hand back to planning

When the pool runs dry or needs regrooming, tell the operator to run `/plan`.
Do not spawn producers yourself from execution mode — the interlock will block
it, and that is correct.
