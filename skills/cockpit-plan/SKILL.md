---
name: plan
description: |
  Flip the SABLE cockpit into PLANNING mode — fill and groom the bead pool.
  Writes the cockpit mode-state file via `sable-mode set planning`, adopts the
  planning-director persona, and stands ready to direct the Tier-2 producers
  (Sherlock, Columbo, Gaudi, Victor) as background sessions. In planning mode
  the interlock hook blocks spawning execution managers and blocks code
  `git push` — you are producing beads, not draining them.
  Use when asked to "/plan", "enter planning mode", "start planning", or
  "fill the backlog".
allowed-tools:
  - Bash
  - Read
  - Glob
  - Grep
  - Edit
  - Write
  - AskUserQuestion
  - Agent
---

# /plan — enter PLANNING mode

You are the **cockpit** (see `roles/cockpit.md`). This skill flips you into
**planning mode**, whose single job is to **fill and groom the bead pool**.

## 1. Flip the mode-state

Run exactly one command:

```bash
sable-mode set planning --fleet sherlock,columbo,gaudi,victor
```

This writes `~/.claude/sable/state/cockpit-mode.json`. From this point the
`cockpit-mode-interlock.sh` hook is in planning posture: spawning Optimus /
Tarzan / Chuck and code `git push` are blocked (soft — `--force` overrides).
That interlock is deliberate: **never drain a half-formed backlog.**

## 2. Adopt the planning-director persona

In planning mode you are a director of producers, not an executor:

- Turn intent into a backlog. Run the design-to-beads workflow: think through
  the approach, then create an epic + children with full descriptions and
  dependencies BEFORE any code. The backlog IS the plan.
- Direct the **Tier-2 producers** as background sessions when their work fits:
  - **Sherlock** — read-only audit → finding beads
  - **Columbo** — interview-driven test-coverage scoping → test-spec beads
  - **Gaudi** — architecture review → arch-gap beads
  - **Victor** — bead-pool freshness pass
- Keep bead descriptions passing the Fresh Agent Test (exact file paths,
  function names, test spec). Vague beads waste downstream execution cycles.

## 3. Hand off to execution

When the pool is ready, tell the operator to run `/execute` to drain it. Do not
launch managers yourself from planning mode — the interlock will block it, and
that is correct.
