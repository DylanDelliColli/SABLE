# SABLE Cockpit — Planning / Execution mode UI

> **Status: design — `personal-tooling` branch.** Reference artifact for the
> SABLE-cockpit epic. The execution contract is the beads, not this file; this
> doc exists so a fresh agent can understand *why* the beads are shaped the way
> they are. Extends [`MULTI-MANAGER-PATTERN.md`](MULTI-MANAGER-PATTERN.md).

## Problem

The multi-manager pattern is powerful but operationally heavy: the operator
juggles up to four-plus terminals (Optimus / Tarzan / Chuck / Lincoln, plus
session-scoped producers), mentally tracks which agents are filling the bead
pool versus draining it, and re-derives cross-agent state by hand. The agents
and their coordination machinery are good; the **surface** is the friction.

## Core insight

The pattern already has an implicit Planning/Execution split — it's latent in
the tier structure:

- **Tier-2 producers** (Sherlock, Columbo, Gaudi, Victor) *fill and groom the
  bead pool*. The continuous-mode hooks deliberately no-op for them.
- **Tier-1 + integrator** (Optimus, Tarzan, Chuck) plus the **Tier-3
  strategist** (Lincoln) *drain the pool*. The whole coordination surface —
  inbox injection, claim/overlap/preempt, the pre-push gate — fires only for
  `CLAUDE_AGENT_ROLE=manager`.

The cockpit promotes that latent split to a first-class surface. **The mode is a
property of one cockpit session; the bead pool is the hinge both modes pivot
on.** Planning fills it, Execution drains it. Because the bead DB is durable,
you can plan one day and execute the next — the UI is the beads-as-state
principle made visible.

## What changes vs. what is reused

**Reused unchanged:** `agents.yaml` registry, every role file, all twelve
coordination hooks, the bead DB, identity injection, inbox + read-guard,
`sable-note`, every existing skill. The *engine does not change* — only the
surface does. Producers and managers run exactly as they do today.

**New (small surface):**

| Piece | What it is |
|-------|------------|
| `cockpit` role | Lincoln evolved: the single named session you talk to. Launcher + overseer (Execution) + planning-director (Planning). |
| `/plan`, `/execute` skills | Flip the cockpit's mode: swap injected persona context, write the mode-state file, set which fleet is launchable. |
| mode-state file | `~/.claude/sable/state/cockpit-mode.json` → `{mode, since, fleet[]}`. Single source of truth read by the interlock hook and the dashboard. |
| mode interlock (1 hook) | `PreToolUse:Bash` guard. Planning: blocks spawning execution managers and blocks `git push` of code (don't drain a half-formed backlog). Execution: blocks spawning planning-only producers. Soft — `--force` override. The *mechanical guarantee* the two modes buy. |
| `sable-status` binary | Read-only dashboard — the one real build. Polls bead DB + `claude agents --json` + mode-state; renders per-mode rows. |
| `sable.kdl` layout | Git-syncable Zellij layout opening `cockpit ∣ sable-status`. Runs inside Windows Terminal; no emulator swap. |

## How the swarm runs underneath (the hybrid)

The cockpit spawns managers and producers as **pinned background sessions**
(`claude agents`, with `CLAUDE_AGENT_NAME`/`CLAUDE_AGENT_ROLE` set at spawn so
OS-level identity, hooks, and parallelism are all intact). You primarily talk to
the cockpit; you can **attach** to any agent to watch or intervene. The
dashboard renders all of them from `claude agents --json` + bead state. One
surface to the operator, full multi-agent fidelity underneath.

Critically: **managers always run with their hooks live** — they only exist
during execution anyway. The mode does *not* change the managers; it changes
what the cockpit is allowed to launch and which persona it wears. This keeps the
interlock clean and avoids a global-state race between a planning cockpit and a
draining manager.

## The two modes

### Planning Mode — fill the pool
- Cockpit wears the planning persona: brainstorm → backlog, runs the
  design-to-beads workflow, grooms deps.
- Launches Tier-2 producers as background sessions (kept as-is — no rewrite to
  workflows in v1).
- Interlock blocks execution-manager spawn + code `git push`.
- Dashboard emphasizes: pool growing, findings per producer, dep graph forming.

### Execution Mode — drain the pool
- Cockpit wears the overseer/strategist persona (Lincoln's original job):
  launches Optimus / Tarzan / Chuck, gives status, brokers `for-lincoln`
  arbitration.
- All execution hooks live (unchanged).
- Interlock blocks spawning planning-only producers from the cockpit.
- Dashboard emphasizes: managers + workers, burn-down, merge queue, overlaps,
  push-gate status, inbox.

### Dashboard sketch

```
PLANNING                                    EXECUTION
┌ sable-status ──────────────┐    ┌ sable-status ──────────────┐
│ MODE: ▣ PLANNING           │    │ MODE: ▶ EXECUTION          │
│ pool  ▁▂▃▄▅ 14 ready ↑     │    │ pool  ▅▄▃▂▁ 14→9 burn ↓    │
│ deps  3 blocked            │    │ optimus ▶ bd-205 · 2 wkrs  │
│ producers                  │    │ tarzan  ▶ bd-198 push      │
│   sherlock  6 findings     │    │ chuck   ⧗ merge queue (2)  │
│   columbo   4 specs        │    │ overlap bd-205⇄147 foo.ts  │
│   gaudi     2 arch-gaps    │    │ push-gate optimus: tests ✓ │
│   victor    pool fresh 7m  │    │ inbox   for-lincoln (1)    │
└────────────────────────────┘    └────────────────────────────┘
```

Same binary, same poll loop; the mode banner selects which rows to emphasize.

## Host: Zellij

No attachment to Windows Terminal + cross-machine sync via `git pull` → Zellij
runs *inside* Windows Terminal (no emulator swap), its layout is a **KDL file
that travels in the repo**, and it has a native status bar and friendly keys.
`zellij --layout sable.kdl` is the one launch command. (WezTerm is the
alternative if replacing the emulator is ever desired; avoid `wt split-pane` —
the layout is not a syncable artifact.) The dashboard binary is host-agnostic;
only the ~20-line layout file changes per host.

## Build order

1. `cockpit` role + identity registration + `/plan` `/execute` skills +
   mode-state file. *The modes exist and the persona flips; useful alone.*
2. Mode interlock hook. *The mechanical guarantee.*
3. `sable-status` dashboard binary. *The visible payoff.*
4. `sable.kdl` Zellij layout + launch wiring.
5. UI/UX iteration pass on the Zellij/dashboard surface (explicitly expected —
   the layout and dashboard ergonomics will need real dogfooding).
6. Docs: extend `PERSONAL-TOOLING.md` + `MULTI-MANAGER-PATTERN.md` with cockpit
   install/usage.

## Out of scope (YAGNI)

Web/Electron dashboard, hosting the chat PTY ourselves, multi-repo control,
clickable bead graph, rewriting producers as dynamic workflows. All re-addable
later; none needed for a single power operator. The producer-as-workflow upgrade
is a deliberate future option, not v1.
