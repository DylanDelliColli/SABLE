# SABLE Cockpit — v2 Design (one-window topology)

> **Status: v2 — `sable-v2` branch.** Reference artifact for the SABLE v2
> one-window topology. The execution contract is the beads, not this file; this
> doc exists so a fresh agent can understand *why* the topology is shaped the
> way it is. Extends [`MULTI-MANAGER-PATTERN.md`](MULTI-MANAGER-PATTERN.md).

## The v2 topology in brief

SABLE v2 reduces the operator surface to **one primary window**: a Lincoln main
session (`CLAUDE_AGENT_NAME=lincoln CLAUDE_AGENT_ROLE=manager claude`) that
spawns Optimus and Tarzan as **resident manager subagents** (one spawn per
session, bead-DB duplex protocol). Chuck remains a second terminal — a
deliberate hybrid holdout, explained below.

```
┌─ Lincoln main session (one window) ─────────────────────────────────────┐
│ You talk here. Lincoln is the strategist + overseer.                     │
│                                                                           │
│ Resident subagents (spawned once at session start, live in background):  │
│   ● Optimus  — epic_manager, claims --has-parent beads                   │
│   ● Tarzan   — one_off_manager, claims --no-parent beads                 │
│                                                                           │
│ Background workers (invisible, dispatched on demand by Optimus/Tarzan):  │
│   ○ worker-1, worker-2, ... (Explore/Agent subagents, short-lived)       │
│                                                                           │
│ Identity: ledger-based (agent_type in hook input JSON). Fallback:        │
│   env-var CLAUDE_AGENT_NAME/CLAUDE_AGENT_ROLE for terminal sessions.     │
└──────────────────────────────────────────────────────────────────────────┘

┌─ Chuck terminal (second window, env-var identity) ──────────────────────┐
│ CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager claude                 │
│ Runs the merge queue continuously. Always-on, session-shaped.            │
│ Receives for-chuck beads from post-push-merge-notify.                    │
└──────────────────────────────────────────────────────────────────────────┘
```

Lincoln pushes completed work; the hook suite (pre-push gate, post-push notify)
fires in the Lincoln session. Chuck's `for-chuck` notification beads land via
the bead DB — a cross-boundary handoff that needs no IPC.

## Chuck: the hybrid holdout

Chuck stays an env-var terminal (`CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager
claude`) rather than a subagent because **always-on merge-queue polling is
session-shaped** — subagents cannot self-schedule or block-poll. A Claude Code
subagent runs to completion and exits; Chuck's loop (`/loop 3m /inbox`) never
reaches completion. Chuck therefore lives as a persistent main session with
env-var identity, exactly as in v1.

No code changes to Chuck's role or hooks were needed for v2. The env-var
fallback from SABLE-uz9.3 keeps his suite live. Chuck's coordination hooks
(`inbox-injection.sh`, `pre-push-rebase-test.sh`, `post-push-merge-notify.sh`)
are all triggered by `CLAUDE_AGENT_ROLE=manager` — satisfied by env-var.

### Cross-boundary handoff — verified evidence

The v2 topology's cross-boundary handoff was verified during the SABLE-uz9.4
live scenario: after Lincoln (with resident Optimus/Tarzan subagents) pushed
work, the `post-push-merge-notify.sh` hook filed **SABLE-m7c** ("Review PR
from lincoln: sable-v2", labels `coord`, `for-chuck`). Chuck's inbox-injection
hook picked this up in the next poll cycle.

Evidence on record:
- **Bead ID**: SABLE-m7c
- **Title**: "Review PR from lincoln: sable-v2"
- **Labels**: `coord`, `for-chuck`
- **How produced**: `post-push-merge-notify.sh` fired during the SABLE-uz9.4
  live scenario (controlled re-fire with `origin/main` after findings
  SABLE-jpr/61n/8rv). The hook requires no knowledge of whether the pushing
  session is a main session or subagent — it just fires on a successful `git
  push` matching `CLAUDE_AGENT_ROLE=manager`. The bead DB is the bridge.
- **Implication**: the two-window topology works end-to-end. Lincoln's session
  fires the notify hook; Chuck's inbox-injection hook (polling `for-chuck`
  beads) delivers the message. No new IPC required.

### Chuck migration options (revisit later)

Chuck's env-var terminal model is correct for now. If future scenarios warrant
migrating Chuck to the one-window topology, the options are:

1. **Scheduled main-session wakeups** — Lincoln periodically invokes Chuck's
   merge-queue logic as a short-lived subagent, accepting up-to-N-minute latency
   on merge processing.
2. **Cron routine** — an external cron job spawns a Claude Code session with
   Chuck's identity at a fixed interval (e.g. every 3 minutes), runs the inbox
   check, and exits.
3. **Push-event routing through Lincoln** — the post-push hook files a
   `for-lincoln` coord bead instead of (or in addition to) `for-chuck`; Lincoln
   triages and routes to Chuck when a Chuck session is not running.

None of these are better than the current approach today. Revisit when: (a) the
operator no longer wants a second terminal, or (b) Chuck's polling frequency
creates usage pressure that outweighs the simplicity cost.

## Core insight: the bead pool as mode hinge

The v2 topology preserves the v1 planning/execution split at the semantic level:

- **Tier-2 producers** (Sherlock, Columbo, Gaudi, Victor) *fill and groom the
  bead pool*. They are session-scoped: user invokes them, they run, they exit.
- **Tier-1 managers** (Optimus, Tarzan) plus the **strategist** (Lincoln) *drain
  the pool*. All coordination hooks fire for `CLAUDE_AGENT_ROLE=manager`.

The bead pool is the hinge: planning fills it, execution drains it. The v2
change is that Optimus and Tarzan now live inside Lincoln's main session as
resident subagents, not as separate terminals.

## Planning Mode — fill the pool (staged, human-in-the-loop)

Planning is a gated substage state machine run from within the Lincoln session.
The five substages are ordered and each requires human sign-off before advance:

1. **FRAMING** — Lincoln (strategist hat, live conversation): scope, constraints,
   goal statement. Human reviews and approves the frame before proceeding.
2. **RESEARCH** — Sherlock (greenfield audit): filing finding beads, identifying
   design gaps and unknowns. Sherlock self-reviews, addresses, exits.
3. **ARCHITECTURE** — Gaudi (`--epic`): structural design, component breakdown,
   interface contracts. Output: locked architecture review attached to the epic.
4. **TEST-STRATEGY** — Columbo (`--epic`): test contract per component, skeleton
   test files, gap analysis. Output: `columbo-test-spec` beads + `*.skel.test.*`
   files.
5. **DECOMPOSITION** — Lincoln + Victor: decompose architecture into
   implementation beads, validate pool freshness, finalize the backlog.

The interlock (`cockpit-mode-interlock.sh`) blocks execution-manager spawns and
code `git push` until `substage=decomposition`, so a half-formed plan cannot
reach execution. The bare epic shell is created early as the planning home Gaudi
and Columbo attach locked reviews to.

## Execution Mode — drain the pool

In execution mode, Lincoln oversees the resident subagents:

- Optimus and Tarzan run their dispatch loops autonomously (claiming beads,
  dispatching workers, reviewing, pushing).
- Lincoln gives status, brokers `for-lincoln` arbitration, and helps the user
  think strategically.
- All execution hooks are live: inbox injection, pre-dispatch
  refresh/claim/overlap/preempt/model-check, pre-push gate, post-push notify.
- Chuck handles the merge queue in the second terminal, receiving `for-chuck`
  beads from Lincoln's post-push hook.

## Identity: ledger-based vs. env-var

V2 uses **dual identity modes**:

| Mode | When used | How hooks discriminate |
|------|-----------|------------------------|
| **Ledger-based** (`agent_type` in hook input JSON) | Resident subagents (Optimus, Tarzan) spawned inside Lincoln's session | Hooks read `agent_type` from the `agent_id`-qualified hook input to determine the agent's role |
| **Env-var** (`CLAUDE_AGENT_NAME` / `CLAUDE_AGENT_ROLE`) | Terminal sessions (Lincoln, Chuck) and pre-v2 installs | Hooks read `$CLAUDE_AGENT_NAME` and `$CLAUDE_AGENT_ROLE` from the process environment |

Subagents dispatched via the `Agent` tool carry `agent_id` in the hook input.
This is the discriminator: `agent_id` present → subagent context; absent →
main-session context. The env-var fallback remains fully functional for any
agent launched from a terminal (Chuck, Lincoln, or standalone installs).

## Supersedes: v1 Zellij/sable-status surface

The v1 topology (documented in git history) required:
- A two-pane **Zellij** layout (`templates/multi-manager/layouts/sable.kdl`)
  opening a cockpit pane (left) and a `sable-status` dashboard pane (right).
- **`bin/sable-status`** — a Textual Python dashboard polling the bead DB,
  `claude agents --json`, and the mode-state file.
- **`bin/sable-cockpit`** — a helper that resolved and launched the Zellij
  layout.
- **`~/.claude/sable/state/cockpit-mode.json`** — the mode-state file
  toggled by `/plan` and `/execute` skills.

These pieces are **deprecated** (marked with DEPRECATED headers) but not
deleted — the dashboard may return as an optional monitoring pane in a future
iteration. The v1 full text lives in git history; it is not kept inline.

**Why replaced:** the two-pane surface added operational complexity
(Zellij dependency, layout file maintenance, dashboard binary) that proved
unnecessary once Optimus and Tarzan became resident subagents. The bead DB and
Lincoln's natural strategist role already surface status on demand. The Zellij
surface added no information that `bd ready` and `/inbox` don't already provide.

## What is reused from v1 (unchanged)

- All twelve coordination hooks in `hooks/multi-manager/` — unchanged.
- The `agents.yaml` registry and all role files — unchanged.
- The bead DB as the single coordination surface — unchanged.
- Chuck's continuous merge-queue discipline — unchanged.
- All six planning agents (Sherlock, Victor, Rudy, Columbo + Lincoln + Gaudi)
  and their session-scoped invocation patterns — unchanged.
- The `/inbox` slash command — unchanged.
- The mode interlock (`cockpit-mode-interlock.sh`) — unchanged, guards the
  planning/execution boundary in the Lincoln session.
- `bin/sable-mode` — unchanged, reads/writes mode-state.

## The installer (install.sh)

`install.sh` now ships:

1. The six named agent definitions (`templates/agents/*.md`) to
   `~/.claude/agents/` (idempotent; preserves any non-SABLE agent files already
   present — only the six SABLE agents are written).
2. The hook suite (unchanged).
3. Prime Directives prepended to `~/.claude/CLAUDE.md`.
4. Settings.json snippet for hook registration (printed, not auto-applied).

The multi-manager cockpit installer (`bin/sable-cockpit-install`) installs the
cockpit-specific pieces (interlock, identity injection, `/plan`/`/execute`
skills, registry, layout) separately with project or user scope.
