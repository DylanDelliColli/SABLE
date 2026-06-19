---
name: sable-execute
description: |
  Flip SABLE into EXECUTION mode — drain the bead pool. Writes the
  mode-state file via `sable-mode set execution`, spawns Optimus and
  Tarzan as resident manager subagents that dispatch their OWN workers and push
  their own approved work, and reminds the operator to open the Chuck terminal.
  In execution mode the interlock hook blocks spawning planning-only producers
  — you are draining the pool, not filling it.
  Use when asked to "/sable-execute", "enter execution mode", "start executing", or
  "drain the backlog".
allowed-tools:
  - Bash
  - Read
  - Glob
  - Grep
  - AskUserQuestion
  - Agent
  - TeamCreate
  - TeamDelete
  - SendMessage
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

## 1. Flip the mode-state

Run exactly one command:

```bash
sable-mode set execution --fleet optimus,tarzan
```

This writes `~/.claude/sable/state/mode-state.json`. From this point the
`mode-interlock.sh` hook is in execution posture: spawning planning-only
producers (sherlock / victor / columbo) is blocked on both the Agent and Bash
legs (soft — `SABLE_ORCHESTRATION_FORCE=1` / `--force` overrides). Mode flips are
mid-conversation; no restart.

## 2. Choose the dispatch topology

Run the preflight to pick the topology from the environment — this is orthogonal
to the execution MODE you just set; it selects *how* agents are wired:

```bash
sable-teams-preflight
```

- prints `nested` → the resident-subagent topology (§2a, default).
- prints `teams` → the Agent-Teams topology (§2b; needs `SABLE_TEAMS=1` and
  `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`).
- exits non-zero → `SABLE_TEAMS=1` but the experimental flag is missing; relay
  the one-line fix it prints to the operator and stop.

**If preflight prints `teams`, you MUST do a runtime tool-availability probe before
proceeding to §2b.** The preflight can only check env flags and defs on disk — it
cannot see which tools the current CC session has loaded. Use ToolSearch (query
`"select:TeamCreate,TeamDelete,TeamList"`) or check your allowed-tools list to
confirm `TeamCreate` and `TeamDelete` are actually available in this session.
If they are absent or deferred (e.g. "MCP server disconnected: TeamCreate,
TeamDelete"), fall back to the nested topology (§2a) and notify the operator:
"Teams tools not yet loaded — using nested topology; add
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` to settings.json and restart to enable
teams."  Do NOT proceed to §2b without confirming Team* tools are live.

### 2a. Nested topology (default)

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

### 2b. Teams topology (`SABLE_TEAMS=1`)

The Agent-Teams topology collapses the second terminal: Chuck folds into the team
and coordination is live over `SendMessage` instead of polling. You (Lincoln) are
the team lead.

**Verify your lead identity BEFORE spawning members.** Run
`echo "${CLAUDE_AGENT_NAME:-UNSET}"`. It MUST print `lincoln`. If it prints
`UNSET` (or anything else), your outbound `SendMessage` sender is mis-derived from
whichever sub-agent's message/notification triggered the current turn — producing
self-addressed messages (`optimus→optimus`) that can silently DROP operational
directives (observed live 2026-06-18, losing an active "rebase before push"
warning). The env var is read at session launch and cannot be set mid-session:
STOP and tell the operator to relaunch the lead with
`CLAUDE_AGENT_NAME=lincoln claude` before proceeding. See
[`AGENT-TEAMS-DESIGN.md`](../../../AGENT-TEAMS-DESIGN.md) §5 + the live-dogfooding
amendments.

- **Create the team:** `TeamCreate` a team named `sable`.
- **Spawn the members** — optimus, tarzan, and chuck — as persistent team members
  via the Agent tool with `team_name: sable` and `name:` = the registry name (so
  each member's hook-input `agent_type` carries its role and `lib-identity`
  resolves it, SABLE-amj.2). Use the built teams definition at
  `~/.claude/agents-teams/<name>.md` as the member's **inline prompt**; do NOT pass
  `agentType` (these are inline-spawned to avoid colliding with the nested
  named-agent defs — SABLE-amj.4 / design decision 6). There is **no separate Chuck
  terminal** in teams mode.
- **Coordination is live** (the teams card, SABLE-amj.3): a manager pushes its
  approved worktree and `SendMessage chuck` "PR ready: <bead>, <branch>"; chuck
  wakes, merges, replies; `for-lincoln` arbitration is a direct message to you.
  The durable `for-merge` bead still lands via `post-push-merge-notify` (the
  recovery record) — beads stays the ledger, SendMessage is the fast path.
- **Workers are unchanged:** managers dispatch their own workers via the Agent
  tool as plain sub-subagents (no `team_name`) and push their own approved work.
- **The team is disposable; beads is the recovery substrate.** If the session
  ends, re-run `/sable-execute` to recreate the team — members catch up from beads on
  join (the startup sweep in the teams card), then go idle and wake on
  `SendMessage`. `TeamDelete` when draining is done.
- **Oversee** as in §2a, but broker `for-lincoln` arbitration live over
  `SendMessage`.

## 3. Hand back to planning

When the pool runs dry or needs regrooming, tell the operator to run `/sable-plan`.
Do not spawn producers yourself from execution mode — the interlock will block
it, and that is correct.
