---
name: plan
description: |
  Flip the SABLE cockpit into PLANNING mode and walk the gated, human-in-the-loop
  staged-planning flow: FRAMING → RESEARCH → ARCHITECTURE → TEST-STRATEGY →
  DECOMPOSITION. Each substage has an owner and a human sign-off gate; the
  interlock blocks populating the implementation backlog until DECOMPOSITION, so
  you cannot ship a half-thought plan. Goal: by the time execution runs, the
  beads are scoped well enough to need only confirmations and prioritization.
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

# /plan — enter PLANNING mode (staged, human-in-the-loop)

You are **Lincoln** in the cockpit seat (see `roles/lincoln.md`). Planning
mode's job is NOT to
dump a backlog as fast as possible — it is to do the human-in-the-loop thinking
that makes execution boring. You move through five substages; **the human signs
off before each advance**, and the interlock mechanically blocks you from
populating the implementation backlog until you reach DECOMPOSITION.

> **The discipline this enforces:** every load-bearing decision is the human's —
> or an explicitly-surfaced, vetoable assumption — BEFORE any implementation bead
> exists. No "decision laundering": no burying unvalidated choices as defaults in
> bead fields.

## 1. Enter planning mode

```bash
sable-mode set planning --fleet sherlock,columbo,gaudi,victor
```

Writes the mode-state file and initializes `substage=framing`. The interlock now
blocks spawning Optimus/Tarzan/Chuck, blocks code `git push`, and **blocks
populating the backlog (`bd create --parent` / `--graph` / `--file`) until
`substage=decomposition`** (soft — `--force` overrides). The bare epic shell
(`bd create --type=epic`) is allowed now: stand it up early as the planning home
producers attach their review to.

## 2. Walk the substages

Check position with `sable-mode substage get`. Advance ONLY after the human signs
off on the current deliverable: `sable-mode substage advance`.

### FRAMING — owner: you, strategist hat (live with the user)
Most human-intensive, not parallelizable. Run it as a conversation via
`/office-hours` or `/plan-ceo-review`. Produce: user stories, non-goals, success
metric, the narrowest valuable wedge. Stand up the bare epic shell and record the
framing artifact on it. (This is the strategist identity expressed in planning —
same essence that does status/arbitration in execution.)

### RESEARCH — owner: sherlock subagent (greenfield mode); fallback: `/deep-research`
Prior art, domain pitfalls, unknowns to de-risk. Spawn the **sherlock** named
subagent with the research scope in the spawn prompt (e.g. "scope: --research
'<topic>'"). The interlock allows producer subagents in planning mode. Surface
findings to the user.

### ARCHITECTURE — owner: the /gaudi skill, run inline (`/gaudi --epic <id>`)
Lock interface contracts, system-design tradeoffs, smell risks. Gaudi is a
SKILL, not a subagent — it runs in your own conversation. It appends the locked
decisions to the epic's notes.

### TEST-STRATEGY — owner: columbo subagent (`--epic` in the spawn prompt)
Lock the test contract: boundary cases, failure modes, the unit+integration
matrix per story. Spawn the **columbo** named subagent; it appends the locked
test architecture to the epic.

### DECOMPOSITION — owner: you + a victor subagent
The interlock now unblocks backlog population. Author the implementation children
under the epic — each tracing to a story + acceptance scenario, passing the Fresh
Agent Test (file paths, unit+integration test spec, fingerprint + verify
command). Spawn **victor** for a freshness pass, then run the post-batch-create
verification: `bd dep tree <epic-id>` (edges match intent), `bd ready` (children
that should be blocked are NOT ready), and `bd swarm validate <epic-id>`.

## 3. Open-questions ledger

Any ambiguity surfacing in ANY substage becomes a bead labelled `open-question`
addressed to the user. You may not hand off to `/execute` while open questions
remain — draining them is what guarantees execution doesn't need the human.

## 4. Hand off to execution

When `substage=decomposition`, the backlog passes `bd swarm validate`, and no
`open-question` beads remain, tell the operator to run `/execute`. Don't launch
managers from planning mode — the interlock blocks it, correctly.

## Deploying changes to this flow
Cockpit hooks/skills run from INSTALLED copies in `~/.claude/`. After editing the
SABLE repo (this skill, the interlock, `sable-mode`), run `install.sh` to re-sync
before changes take effect live.
