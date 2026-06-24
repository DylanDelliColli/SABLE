# SABLE

> **Swarm Architecture with Bead-Led Execution** — a methodology for rigorous, test-driven software development by autonomous AI agent swarms.

AI agents are remarkable software engineers and remarkably undisciplined ones. SABLE is the discipline: a small set of mechanical gates and one organizing idea that turn a capable-but-sloppy agent into one that ships tested, verified, pushed work — every time, without you watching.

**New here?** Start with **[QUICKSTART.md](QUICKSTART.md)** — ~10 minutes to a running setup. This page is the pitch: *why* SABLE exists and what it gives you. The full methodology lives in **[SABLE.md](SABLE.md)**.

---

## The problem

The usual way to make an agent behave is to write more instructions. Another section in the system prompt. Another bullet in the dispatch. It works — briefly. As instructions grow, compliance drops: a 50-line file gets followed, a 500-line file gets skimmed in exactly the places that matter.

And there's a deeper issue. Agents are **amnesiac**. Every session starts from zero — no memory of yesterday's brilliant architecture, no "how we do things here" beyond what's written down. The agent that built your feature is gone. Today's agent is a stranger with commit access.

Left alone, strangers with commit access:

- skip tests, because the code "looks right"
- close tasks they never verified
- leave work unpushed at the end of a session
- solve the wrong problem because the spec was vague
- forget the bug they noticed in passing — it's gone next session

## The insight

**Mechanical enforcement beats documentation.** The behaviors that matter most shouldn't be *encouraged* — they should be *impossible to skip*. Tests must run before work can close. Task descriptions must be complete before an agent can act on them. Sessions must push before they end. Not guidelines — **gates**.

And the second half: **planning rigor buys execution speed.** When every unit of work is granular, self-contained, and documented well enough that a total stranger could execute it, agents run in parallel with almost no coordination overhead. You invest two minutes writing a precise task so the agent doesn't burn ten re-discovering the problem.

## How it works — three reinforcing pillars

| Pillar | What it is | Why it matters |
|--------|-----------|----------------|
| **Beads** | A granular, file-based issue tracker (`bd`) that *is* the plan. Each bead is a contract: what, where, why, and how to verify. | The backlog replaces the planning doc nobody reads. Dependencies encode order; descriptions encode intent. An interrupted session resumes with one command. |
| **Enforced TDD** | Hooks log every test run and **block closing a task without test evidence**. A `[no-test]` escape hatch exists for docs — used deliberately, never by accident. | Every closed task carries proof someone verified it. The amnesiac who inherits the code can trust it works. |
| **Swarm execution** | An orchestrator dispatches parallel workers, each owning a few related beads, with overlap resilience and mandatory verification. | Many fast workers on well-specified beads beat one agent doing everything in sequence. Recovery from a failed worker is trivial — just re-dispatch. |

The throughline: **hooks enforce, docs inform.** If a rule is non-negotiable, it's a harness-level gate the agent literally cannot bypass — not another line of prose competing for a finite attention budget.

## What it feels like

The single most important quality bar in SABLE is the **Fresh Agent Test**:

> Could a fresh agent, with only this task and the codebase, act on it without re-exploring the source?

A task that **fails** it:

```
Fix the cache logic
```

The agent spends five minutes reading files just to find out what's wrong, where, and why.

A task that **passes** it:

```
Fix _build_cache_key in orchestrator.py:142 — uses string concatenation,
causes key collisions when location strings contain slashes. Replace with
hashlib.md5(). Called by all three collectors.

Reproduce:  pytest tests/orchestration/test_cache.py -v
Done when:  test_cache_key_with_slashes passes, no other regressions.
```

The agent starts coding immediately. The difference is 30 seconds of your effort — and it's the difference between a swarm that works and one that thrashes. Multiply across every task in a backlog and you have a system where parallel agents are an asset, not a liability.

## The adoption ramp

You install everything on day one; the *practice* ramps as you get fluent.

| Stage | Practice | Climb when |
|-------|----------|-----------|
| **Foundation** | Beads + integration tests + hooks. One agent, sequential work. | Day 1 — stay here until your descriptions pass the Fresh Agent Test reflexively. |
| **Hierarchy** | Epics, child beads, dependencies, preflight checks. | You hit multi-step features that need ordering. |
| **Swarm** | Parallel agents via `bd swarm` and worktrees. | Spec-writing is automatic and your budget supports concurrency. |

Beyond the ramp, SABLE scales into a **multi-manager topology** — a single operator window hosting named managers that each command their own worker swarm, with mechanical conflict prevention and addressed inter-agent messaging. That's power-user territory; see [`MULTI-MANAGER-PATTERN.md`](MULTI-MANAGER-PATTERN.md) when you get there.

## Is this for you?

**Yes, if** you run a coding agent (Claude Code, Codex CLI, anything that loads a global instructions file) on real work, you're tired of re-explaining context every session, and you want output you can trust without reviewing every line.

**Not yet, if** you're doing one-off scripting or exploratory prototyping where the overhead of tracking and testing every change isn't worth it. SABLE is opinionated on purpose — the rules are non-negotiable, and that's the point.

## Get started

- **[QUICKSTART.md](QUICKSTART.md)** — prerequisites, `bash install.sh`, the bootstrap prompt, and a verification smoke test. ~10 minutes.
- **[SABLE.md](SABLE.md)** — the full methodology, rationale, hook catalog, and worked examples. Read once top-to-bottom; reference by section after.
- **[MULTI-MANAGER-PATTERN.md](MULTI-MANAGER-PATTERN.md)** — the advanced multi-agent coordination pattern.

---

*The human plans deeply. The agents execute quickly and correctly. The hooks make sure the checks that enable both are never skipped.*
