# SABLE: Swarm Architecture with Bead-Led Execution

> A methodology for rigorous, test-driven software development using autonomous AI agent swarms.

---

## ⚠️ Prime Directive: All Work Flows Through Beads

**This is the single non-negotiable rule of the entire methodology. Every other section of this document exists to support it.**

If you are an agent reading this document, internalize the following before doing anything else:

- **Every unit of work — bug fix, feature, refactor, investigation, doc edit, even one-line changes — must exist as a bead before you start it.** No exceptions for "quick" tasks. No exceptions for "I'll log it after." No exceptions because the bead "feels like overkill."
- **Every bug, smell, or unexpected behavior you notice — at any time, in any context, even tangential to your current task — must immediately become a bead.** Do not ask "should I log this?" Do not finish your current thought first. Stop, log, then continue. See §3.7.
- **Never use TodoWrite, in-conversation task lists, scratch markdown plans, or your own memory as a substitute for beads.** The user has told you explicitly: this framework is the foundation of all programming work. Bypassing it — even for "small" things — corrupts the foundation.
- **If you cannot articulate which bead your current action serves, stop and create one.** This is the single best test of whether you are inside the methodology or drifting out of it.

The reasoning is in §2 (Core Principles), but the rule does not depend on you understanding the reasoning. **Follow it first; the rest of the document explains why it works.**

If you find yourself in a situation where the rule seems impractical — the bead would be trivial, the work is "obviously" too small, the user "just wants a quick fix" — the answer is still to create the bead. `bd q "<title>"` makes capture take three seconds. Your sense that "this one doesn't need a bead" is the failure mode this document was written to prevent.

---

## Table of Contents

1. [Introduction & Thesis](#1-introduction--thesis)
2. [Core Principles](#2-core-principles)
3. [The Bead System](#3-the-bead-system)
4. [Test-Driven Development](#4-test-driven-development)
5. [Hook Architecture](#5-hook-architecture)
6. [Swarm Execution](#6-swarm-execution)
7. [Documentation Strategy](#7-documentation-strategy)
8. [Session Management](#8-session-management)
9. [Anti-Patterns & Failure Modes](#9-anti-patterns--failure-modes)
10. [Getting Started](#10-getting-started)

---

## 1. Introduction & Thesis

### The Problem

Large language model agents are remarkably capable software engineers. They can read codebases, write implementations, debug failures, and ship features. But without structure, they are also remarkably undisciplined. They skip tests. They write vague commit messages. They close tasks they haven't verified. They leave work unpushed. They solve the wrong problem because they didn't read the spec carefully enough.

The traditional response is to write more instructions. Add another section to the system prompt. Another paragraph in the CLAUDE.md. Another bullet point in the dispatch prompt. This works — briefly. As documentation grows, compliance drops. A 50-line instruction file gets followed reliably. A 200-line file gets skimmed. A 500-line file gets ignored in exactly the places it matters most.

The deeper problem is that AI agents are **amnesiac**. Every agent starts from zero. It has no memory of prior conversations, no institutional knowledge, no sense of "how we do things here" beyond what's written down in the files it reads at the start of its session. The agent that brilliantly architected a feature yesterday is gone. Today's agent is a stranger who happens to have access to the same codebase.

### The Insight

The solution isn't more documentation — it's **mechanical enforcement**. The behaviors that matter most should be impossible to skip, not merely encouraged. Tests must run before work can close. Beads must contain required sections. Sessions must push before ending. These aren't guidelines — they're gates.

But enforcement alone produces rigid, fragile systems. The second insight is that **planning rigor enables execution speed**. When every unit of work is granular, well-documented, and self-contained, agents can execute in parallel without coordination overhead. When every bead passes the "Fresh Agent Test" — could a stranger act on this with only the bead and the codebase? — swarm execution becomes trivially parallelizable.

### SABLE in One Paragraph

SABLE is a methodology for AI-assisted software development that combines three reinforcing systems: **Beads** (a granular issue tracker that serves as the execution contract between human planners and agent workers), **Test-Driven Development** (enforced mechanically via hooks, not just documented), and **Swarm Execution** (parallel agent dispatch with overlap resilience and mandatory verification). The human invests deeply in planning and specification. The agents execute quickly and correctly. Hooks ensure the rigorous checks and balances that make this possible are never skipped. Swarm execution is in the name for a reason — it's where the throughput multiplier lives — but the methodology rewards a deliberate ramp into it (see Adoption Path below).

### Adoption Path

SABLE is designed so you can adopt it incrementally. The full methodology — including swarm — is the goal; new adopters reach it through a learning ramp rather than all at once.

| Stage | What you use | When to climb |
|-------|--------------|---------------|
| **Foundation** | Beads + the Prime Directive + integration tests + hooks. Single agent, sequential work. | Day 1. Stay here until your bead descriptions consistently pass the Fresh Agent Test without conscious effort. |
| **Hierarchy** | Add: epics, child beads, `bd dep tree`, `bd preflight`. | When you start hitting features that need 3+ beads with ordering. Usually after a week or two on Foundation. |
| **Swarm** | Add: parallel agent dispatch via `bd swarm` and `bd worktree`. The full methodology. | Once spec-writing is automatic, you've shipped a few hierarchical features cleanly, and your usage budget supports running multiple agents in parallel. See §6. |

**If you're new to AI-assisted development:** start at Foundation, even though the goal is Swarm. The discipline built at each stage is what makes the next stage work — bad beads in a swarm waste 5× the agent cycles of bad beads sequentially. Skipping the ramp is the most common failure mode this methodology was built to prevent.

**If you're already comfortable with agentic coding:** you can climb the ramp in days, not weeks. The stages exist as guardrails, not gates.

---

## 2. Core Principles

### 2.1 The Amnesiac Agent Model

This is the foundational mental model of SABLE. Internalize it and everything else follows.

**Every agent starts with zero memory.** There is no "I'll remember this later." There is no shared conversation context between agents. Each agent wakes up, reads the codebase and its assigned beads, does its work, and disappears. The next agent has no idea what the previous one was thinking — only what it wrote down.

This isn't a limitation to work around. It's the design constraint that makes the entire methodology work. When you truly internalize the amnesiac model:

- **Bead quality becomes non-negotiable.** A vague bead like "fix the cache logic" wastes a full agent cycle because the agent has to re-explore the codebase to understand what's wrong, where, and why. A detailed bead with file paths, function names, and a suggested approach lets the agent start coding immediately.

- **The backlog becomes the plan.** You don't need a separate planning document that agents might not read. The beads *are* the plan. Dependencies encode ordering. Descriptions encode intent. The `bd ready` command surfaces exactly what can be worked on next.

- **Session boundaries become irrelevant.** It doesn't matter if work spans one session or ten. It doesn't matter if the same agent continues or a new one picks up. The bead is the contract. The code is the artifact. The tests are the proof. Everything else is ephemeral.

- **Documentation length becomes a liability.** A 200-line instruction file isn't twice as good as 100 lines — it's worse, because an amnesiac agent has a finite attention budget. Every line competes for compliance. The instructions that matter most must be the shortest and most prominent — or better yet, enforced by hooks that can't be ignored.

### 2.2 Beads Are the Execution Contract

In SABLE, beads are not just a task tracker. They are the **primary communication channel** between the human (who plans) and the agents (who execute). A bead is a contract: "Here is exactly what needs to be done, why, and how to verify it's correct."

This is a deliberate inversion of the typical developer workflow. Most developers maintain a loose backlog and figure out the details as they code. SABLE front-loads all that thinking into the bead. The bead is detailed so the agent can be fast. The human invests 2 minutes writing a thorough description so the agent doesn't waste 10 minutes re-discovering the problem.

### 2.3 Hooks Enforce, Docs Inform

SABLE uses a strict hierarchy of enforcement:

| Layer | Mechanism | Can Be Ignored? | Use For |
|-------|-----------|-----------------|---------|
| **Hooks** | Harness-level gates (PreToolUse, PostToolUse) | No — agents literally cannot bypass them | Non-negotiables: tests before close, required bead sections |
| **Project CLAUDE.md** | Short, project-specific instructions | Sometimes — but high compliance when kept lean | Test commands, file conventions, mock patterns |
| **Global CLAUDE.md** | Cross-project instructions | Increasingly ignored as it grows | Minimal: workflow rules, pointers to hooks |

The rule is simple: if a behavior is non-negotiable, make it a hook. If it's important but contextual, put it in a short project doc. If it's general guidance, keep it to a few lines in the global doc. Never add to the global doc what a hook can enforce.

### 2.4 Test Evidence Is Non-Negotiable

Agents will skip tests if you let them. Not maliciously — they optimize for task completion, and tests feel like overhead when the implementation "looks right." But untested code from an amnesiac agent is a liability. The agent that wrote it is gone. The next agent that touches the code has no way to know if the original implementation was correct except by running tests — which don't exist.

SABLE enforces TDD mechanically. The `tdd-gate` hook blocks beads from being closed unless tests were run during the session. The `tdd-remind` hook nudges agents to write tests when they edit source files that lack them. The `tdd-evidence` hook logs every test execution so the gate can verify it happened.

This isn't about test coverage metrics. It's about **proof of work**. Every closed bead has evidence that someone verified the implementation works.

---

## 3. The Bead System

### 3.1 What Beads Are

Beads (via the `bd` CLI) are a lightweight, file-based issue tracker designed for AI-assisted workflows. Each bead is a unit of work — a bug fix, a feature, a task — with structured metadata and a description that serves as the agent's marching orders.

Key properties:

- **File-based**: Beads live in `.beads/` in the repository. No external service required.
- **CLI-native**: All operations via `bd` commands. No web UI needed. Agents interact with beads the same way humans do.
- **Dependency-aware**: Beads can block other beads. `bd ready` surfaces only unblocked work.
- **Dolt-backed**: Beads sync to a Dolt remote, providing version history and collaboration.

### 3.2 The Fresh Agent Test

This is the single most important quality gate for beads:

> **Could a fresh agent, with only this bead and the codebase, act on this task without re-exploring source files?**

If the answer is no, the description isn't good enough.

A **failing** description:
```
Fix the cache logic
```

A **passing** description:
```
Fix _build_cache_key in orchestrator.py:142 — uses string concatenation,
causes key collisions when location strings contain slashes. Replace with
hashlib.md5(). Called by all three collectors (cmhc_collector.py,
environics_collector.py, urban_collector.py).

## Steps to Reproduce
Run: python -m pytest tests/orchestration/test_cache.py -v --tb=short
Test test_cache_key_with_slashes fails with KeyError.

## Acceptance Criteria
test_cache_key_with_slashes passes. No other test regressions.
```

The difference is 30 seconds of human effort. The payoff is an agent that starts coding immediately instead of spending 5 minutes reading files to understand the problem.

### 3.3 Anatomy of a Good Bead

Every bead should include:

| Field | Purpose | Example |
|-------|---------|---------|
| **Title** | One-line summary, specific enough to act on | "Fix cache key collisions in orchestrator.py" |
| **Description** | The full contract: what, where, why, how | File paths, function names, root cause, suggested approach |
| **Type** | bug, task, or feature | `--type=bug` |
| **Priority** | 0-4 (P0=critical, P2=medium, P4=backlog) | `--priority=2` |
| **Test spec** | Which test file, what assertions | "tests/test_cache.py::test_key_collisions" |

Required sections by type:

**Bugs** must include:
- `## Steps to Reproduce` — Exact commands to trigger the bug
- `## Acceptance Criteria` — How to verify the fix

**Tasks and features** must include:
- `## Acceptance Criteria` — How to verify completion

Use separate fields for separate concerns:
```bash
bd update <id> --description "What's wrong and where"
bd update <id> --notes "Discoveries appended during work"
bd update <id> --design "Architectural approach decided on"
bd update <id> --acceptance "How to verify it's done"
```

### 3.4 Dependencies

Dependencies encode ordering constraints between beads. The syntax uses **requirement language**, not temporal language:

```bash
# "step2 needs step1" — step2 depends on step1
bd dep add step2 step1

# NEVER use temporal language — it inverts the arguments:
# "step1 comes before step2" ← This is confusing and error-prone
```

The mental model: `bd dep add A B` means "A requires B." A is blocked until B is closed.

Check blocking relationships:
```bash
bd blocked          # Show all blocked beads
bd show <id>        # See what blocks/is blocked by this bead
bd dep tree <id>    # Visualize the full dependency tree under a bead
```

`bd dep add` is for **blocking** dependencies (work order). For non-blocking links between beads that are merely *related* (same area, share context), use:

```bash
bd dep relate A B    # Bidirectional "relates to" — does NOT block
bd dep unrelate A B  # Remove the relation
```

This matters for swarm planning: blocking deps reduce parallelism (children of a sequential chain can't run concurrently). Use `relate` when you want discoverability without sacrificing throughput.

### 3.5 Hierarchy & Structure

Epics group child beads. Children inherit context (and optionally labels) from their parent. SABLE uses this hierarchy to make swarm execution legible — one epic per coherent body of work, children for individual deliverables.

```bash
# Create a child bead linked to a parent on creation
bd create --title="..." --description="..." --type=task --parent=<epic-id>

# Reparent an existing bead (use empty string to detach)
bd update <id> --parent=<new-parent-id>

# List all children of a parent (closed children included by default)
bd children <epic-id>
bd children <epic-id> --pretty   # Tree view

# Epic lifecycle
bd epic status <epic-id>          # Show completion progress
bd epic close-eligible            # Auto-close epics whose children are all done
```

Children inherit labels from their parent unless you pass `--no-inherit-labels`. This means tagging the epic correctly propagates to the swarm.

The `--waits-for-gate` flag controls when an epic considers itself "ready":
- `all-children` (default) — epic stays open until every child closes
- `any-children` — epic closes as soon as any child closes (rare; useful for "first one wins" patterns)

### 3.6 The Backlog Is the Plan

In SABLE, you don't write a separate implementation plan and then create beads. **The beads are the plan.** The design-to-beads workflow:

1. **Think through the approach.** Understand the problem, identify the solution, consider edge cases.
2. **Convert every deliverable into beads.** Each bead gets a full description that passes the Fresh Agent Test. Create an epic for the overall goal (`bd create --type=epic`) and child beads for individual deliverables (`bd create --parent=<epic-id>`).
3. **Add dependencies.** Use requirement language: "B needs A."
4. **Validate the structure.** Run `bd swarm validate <epic-id>` to see ready fronts (waves of parallel work), estimated worker-sessions, and warnings.
5. **Work from `bd ready`** (or kick off a swarm — see section 6.5).

If a session ends mid-work, the next agent runs `bd ready` and continues. No re-reading plans. No lost context. The beads are the single source of truth.

### 3.7 Issue Discovery Is Mandatory

Any bug, bad practice, incorrect behavior, pre-existing error, or code smell noticed at any time — by any agent, during any task — must be immediately logged as a bead. This is non-negotiable.

Do not ask "should I log this?" Just log it:

```bash
bd create --title="<what's wrong>" --type=bug --priority=2 \
  --description="<file, function, what's wrong, how to reproduce, acceptance criteria>"
```

The reasoning: agents are amnesiac. If it's not in a bead, it doesn't exist in the next session. The cost of a false-positive bead (turns out it wasn't a real issue) is trivial. The cost of a missed bug (nobody remembers it existed) compounds over time.

**Important**: Before creating a bead, verify the referenced file or function actually exists (grep or glob). Hallucinated beads waste full agent cycles when the next agent tries to act on them.

---

## 4. Test-Driven Development

### 4.1 Why TDD Matters More with Agents

Test-driven development is valuable in traditional software engineering. In agentic development, it's essential. Here's why:

**Agents optimize for completion, not correctness.** An agent's natural inclination is to write the implementation, verify it "looks right," close the bead, and move on. Tests feel like overhead. But without tests, there's no proof the implementation works — and the agent that wrote it is about to disappear.

**The amnesiac problem compounds.** When a human writes untested code, they at least remember what they were thinking and can debug it later. When an agent writes untested code, the context is gone. The next agent that encounters a bug has to reverse-engineer the original intent from the code alone.

**Parallel execution demands test boundaries.** In a swarm, multiple agents edit different parts of the codebase simultaneously. Tests are the contract that ensures one agent's changes don't break another's assumptions. Without tests, you're relying on agents to "be careful" — which is the same as hoping for the best.

### 4.2 The Red-Green-Refactor Cycle

SABLE follows standard TDD adapted for agentic execution:

1. **Red**: Write a failing test that describes the expected behavior. Run it. Confirm it fails for the right reason.
2. **Green**: Write the minimum implementation to make the test pass. Run the test. Confirm it passes.
3. **Refactor**: Clean up the implementation without changing behavior. Run the test again. Confirm it still passes.

The key adaptation for agents: **the test file and expected assertions should be specified in the bead description.** The dispatching human (or orchestrator agent) defines what "done" looks like in test terms, not just prose.

Example bead with test spec:
```
Fix _build_cache_key collisions in orchestrator.py:142

Test file: tests/orchestration/test_cache.py
Add test: test_cache_key_with_slashes — assert that keys containing
"/" produce distinct cache entries, not collisions.
Expected: test fails before fix (red), passes after (green).
```

### 4.3 The Two-Hook Relay

SABLE enforces TDD through a mechanical two-hook relay:

**Hook 1: tdd-evidence.sh** (silent logger)
- Trigger: PreToolUse on every Bash command
- Action: If the command matches a test pattern (`pytest`, `vitest`, `npm test`), append a timestamped entry to `/tmp/tdd-evidence-${SESSION_ID}`
- Impact: None. Silent. No blocking. Just logging.

**Hook 2: tdd-gate.sh** (hard gate)
- Trigger: PreToolUse on every Bash command
- Action: If the command is `bd close`, check if the evidence file exists and is non-empty
- If evidence found: Allow the close
- If no evidence: **Deny the close** with a message: "No tests were run this session. Run your test suite first."
- Impact: Agents literally cannot close beads without running tests

The order matters: the evidence hook must run before the gate hook in the settings.json matcher array, so that a test command and a close command in the same turn work correctly.

**Hook 3: tdd-remind.sh** (soft nudge)
- Trigger: PreToolUse on Edit/Write
- Action: If editing a source file (`.py`, `.ts`, `.tsx`) that isn't itself a test file, check if a corresponding test file exists. If not, inject a reminder: "No test file found for this source. Write a failing test first."
- Impact: Contextual nudge. Not a hard block — the agent can still edit. But the reminder appears right when it matters.

### 4.4 The Escape Hatch

Not every bead involves code. Documentation changes, configuration updates, and process improvements don't need test evidence. SABLE provides an explicit escape hatch:

```bash
bd update <id> --notes "[no-test] docs-only change, no code modified"
bd close <id>
```

The `[no-test]` marker in the bead's notes field tells the gate hook to allow the close without test evidence. This creates an audit trail — the next agent (or human) can see exactly why tests were skipped.

Rules for the escape hatch:
- Only works on **single-bead closes** (`bd close <id>`). Multi-bead closes (`bd close id1 id2 id3`) always require test evidence — this prevents one `[no-test]` bead from bypassing the gate for code beads bundled in the same close.
- The `[no-test]` marker must be added explicitly. Agents can't accidentally skip tests — they have to consciously opt out.

### 4.5 Test Coverage Required: Unit + Integration

**Unit tests alone are not sufficient. Every code change requires both unit AND integration coverage for the behavior it touches.** This is not a stylistic preference — it is a hard requirement of the methodology, on the same level as the Prime Directive.

The reasoning: agents are excellent at writing unit tests that pass while still shipping broken systems. Mocked dependencies hide the bugs that actually cause production outages. Two functions that each pass their unit tests can still fail when composed. The only test that proves a feature works is one that exercises the real composition of the moving parts.

**The required layers:**

| Layer | Required? | What it tests | Examples |
|-------|-----------|---------------|----------|
| **Unit** | **Required** | A single function/class/module in isolation. Mocks for everything external. | `test_calculate_affordability_with_zero_income()` |
| **Integration** | **Required** | Multiple components working together against real dependencies (real DB, real HTTP, real file system). No mocks for the things being integrated. | `test_briefing_generation_writes_to_supabase()`, `test_geocoding_pipeline_against_local_db()` |
| **Smoke** | **Strongly encouraged, not gated** | The smallest end-to-end "does the thing start and respond at all?" check. Not exhaustive — just proof of life. | `test_app_starts_and_health_endpoint_returns_200()`, `test_cli_invokes_without_crash()` |

**Concrete rules:**

1. **Every bead description must specify both a unit test file/assertion AND an integration test file/assertion.** If only one is specified, the bead fails the Fresh Agent Test — the next worker won't know whether integration coverage was waived deliberately or forgotten.
2. **If a change cannot be integration-tested, the bead must explicitly say why** in the description (e.g. "no integration test — pure type-level refactor with no runtime effect"). This is treated like the `[no-test]` escape hatch: an explicit, audited opt-out, not a default.
3. **Mocking the database in integration tests defeats the purpose.** Use a real local database (Docker, sqlite, ephemeral postgres). Mocks belong in unit tests; integration tests must hit real systems or they are unit tests with a misleading label.
4. **Smoke tests are encouraged for any service with a startup path** (web server, CLI, worker). They catch the entire class of bugs where "the code is correct but the service won't boot." But they are not blocked by the gate hook — missing smoke tests are a code review issue, not a close-time block.

**Updated bead-description template:**

```
Fix _build_cache_key collisions in orchestrator.py:142

Test spec:
  Unit:        tests/unit/test_cache.py::test_cache_key_with_slashes
               Assert keys with "/" produce distinct cache entries.
  Integration: tests/integration/test_orchestrator.py::test_orchestrator_caches_distinct_locations
               Run two locations whose names differ only by slash placement;
               assert both make it through the orchestrator without collision
               (real local Supabase, no mocks).
  Smoke:       (optional) make sure the orchestrator starts cleanly with the new key fn.
```

**For dispatch prompts**, the worker must be told to run BOTH layers before closing:

```
Run:
  pytest tests/unit/test_cache.py -v
  pytest tests/integration/test_orchestrator.py -v -m integration
Both must pass before bd close.
```

The `tdd-gate` hook accepts evidence from any test invocation, so it does not distinguish layers. **The discipline lives in the bead description and dispatch prompt, not in the hook.** This is intentional — you cannot mechanically detect "did the test actually exercise the real integration." You can only enforce that the bead specified what was required and the worker executed it.

---

## 5. Hook Architecture

### 5.1 Why Hooks Beat Documentation

This insight emerged from practice: as CLAUDE.md files grow beyond ~100 lines, agent compliance drops measurably. Instructions that were followed perfectly in a 50-line file get missed in a 200-line file. The cause is simple: agents have a finite attention budget, and every additional line of documentation dilutes the importance of every other line.

Hooks solve this by moving enforcement from the attention layer (will the agent read and follow this instruction?) to the harness layer (the system prevents the undesired action mechanically). The agent doesn't need to remember to run tests — the hook blocks the close if it doesn't.

The enforcement hierarchy:

```
Hooks (harness-enforced)      ← Can't be ignored. Use for non-negotiables.
  ↓
Short project docs (~60 lines) ← Mostly followed. Use for project-specific conventions.
  ↓
Global docs (~100 lines)       ← Followed when short. Use for cross-project basics.
  ↓
Long global docs (200+ lines)  ← Unreliable. Avoid growing to this size.
```

### 5.2 Hook Fundamentals

Claude Code hooks are shell scripts that run at specific lifecycle points. They receive JSON on stdin and can influence agent behavior through their output.

**Hook events:**
- `PreToolUse` — Fires before a tool executes. Can **deny** the action or inject **additional context**.
- `PostToolUse` — Fires after a tool executes. Can inject **additional context** (nudges, reminders). Cannot deny.
- `SessionStart` — Fires when a session begins. Good for environment setup.
- `PreCompact` — Fires before context compaction. Good for re-injecting critical state.

**Input format** (JSON on stdin):
```json
{
  "session_id": "abc123",
  "tool_name": "Bash",
  "tool_input": {
    "command": "bd close my-bead-id"
  }
}
```

PostToolUse also includes `tool_result` with `stdout` and `stderr`.

**Output format:**

To deny (PreToolUse only):
```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Your explanation here"
  }
}
```

To inject context:
```json
{
  "additionalContext": "Your message to the agent here"
}
```

To allow silently: exit 0 with no output.

### 5.3 The SABLE Hook Catalog

SABLE uses six hooks. Here are the complete implementations with annotations.

#### Hook 1: tdd-evidence.sh

Silently logs test executions to an evidence file keyed by session.

```bash
#!/usr/bin/env bash
# tdd-evidence.sh — Log test runs to evidence file
# Trigger: PreToolUse on Bash | Timeout: 3000ms
set -euo pipefail

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
cmd = d.get('tool_input', {}).get('command', '')
sid = d.get('session_id', '')
print(f'{sid}\n{cmd}')
" 2>/dev/null) || exit 0

SESSION_ID=$(echo "$PARSED" | sed -n '1p')
COMMAND=$(echo "$PARSED" | sed -n '2p')

[ -z "$SESSION_ID" ] && exit 0
[ -z "$COMMAND" ] && exit 0

# Match test commands — extend this list for your stack
if echo "$COMMAND" | grep -qE '(pytest|vitest|npm test|npx vitest|jest|cargo test|go test)'; then
  echo "$(date -Iseconds) $COMMAND" >> "/tmp/tdd-evidence-${SESSION_ID}"
fi

exit 0
```

#### Hook 2: tdd-gate.sh

Blocks `bd close` unless test evidence exists for this session.

```bash
#!/usr/bin/env bash
# tdd-gate.sh — Block bd close without test evidence
# Trigger: PreToolUse on Bash | Timeout: 5000ms
set -euo pipefail

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
cmd = d.get('tool_input', {}).get('command', '')
sid = d.get('session_id', '')
print(f'{sid}\n{cmd}')
" 2>/dev/null) || exit 0

SESSION_ID=$(echo "$PARSED" | sed -n '1p')
COMMAND=$(echo "$PARSED" | sed -n '2p')

[ -z "$COMMAND" ] && exit 0

# Only act on bd close commands
echo "$COMMAND" | grep -q '^bd close' || exit 0

# Extract bead IDs (strip flags like --reason="...")
BEAD_ARGS=$(echo "$COMMAND" | sed 's/^bd close //' \
  | sed 's/--[a-z]*="[^"]*"//g' \
  | sed 's/--[a-z]*=[^ ]*//g' | xargs)
ID_COUNT=$(echo "$BEAD_ARGS" | wc -w)

# Single-bead close: check [no-test] escape hatch
# For multi-bead close, skip escape hatch — evidence required
if [ "$ID_COUNT" -eq 1 ]; then
  BEAD_ID="$BEAD_ARGS"
  # Check bead notes for [no-test] marker
  # Using bd show --json to read notes field
  NOTES=$(bd show "$BEAD_ID" --json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
if isinstance(data, list) and len(data) > 0:
    print(data[0].get('notes', '') or '')
" 2>/dev/null || echo "")
  if echo "$NOTES" | grep -q '\[no-test\]'; then
    exit 0  # Escape hatch: allow without evidence
  fi
fi

# Check for test evidence
EVIDENCE_FILE="/tmp/tdd-evidence-${SESSION_ID}"
if [ -s "$EVIDENCE_FILE" ]; then
  exit 0  # Tests were run — allow close
fi

# No evidence — block the close
python3 -c "
import json
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': 'TDD gate: No tests were run this session. Run your test suite first. For non-code beads: add [no-test] to bead notes and close individually.'
    }
}))
"
```

#### Hook 3: tdd-remind.sh

Nudges agents to write tests when editing source files that lack them.

```bash
#!/usr/bin/env bash
# tdd-remind.sh — Inject reminder on untested source file edits
# Trigger: PreToolUse on Edit|Write | Timeout: 3000ms
set -euo pipefail

FILE_PATH=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d.get('tool_input', {}).get('file_path', ''))
" 2>/dev/null) || exit 0

[ -z "$FILE_PATH" ] && exit 0

# Only act on source files
echo "$FILE_PATH" | grep -qE '\.(py|ts|tsx|js|jsx)$' || exit 0

# Skip if this IS a test file
echo "$FILE_PATH" | grep -qiE '(test|spec|__tests__)' && exit 0

# Extract basename without extension for fuzzy matching
BASENAME=$(basename "$FILE_PATH" | sed 's/\.[^.]*$//')

# Find the project root (nearest directory with package.json, pyproject.toml, or .git)
DIR=$(dirname "$FILE_PATH")
PROJECT_ROOT="$DIR"
while [ "$PROJECT_ROOT" != "/" ]; do
  [ -f "$PROJECT_ROOT/package.json" ] || \
  [ -f "$PROJECT_ROOT/pyproject.toml" ] || \
  [ -d "$PROJECT_ROOT/.git" ] && break
  PROJECT_ROOT=$(dirname "$PROJECT_ROOT")
done

# Fuzzy search for any test file referencing this module
if find "$PROJECT_ROOT" -maxdepth 5 -type f \
  \( -name "*test*${BASENAME}*" -o -name "*${BASENAME}*test*" -o -name "*spec*${BASENAME}*" \) \
  2>/dev/null | grep -q .; then
  exit 0  # Test file exists — stay silent
fi

# No test found — inject reminder
python3 -c "
import json
print(json.dumps({
    'additionalContext': 'TDD: No test file found for $BASENAME. Write a failing test before implementing changes.'
}))
"
```

#### Hook 4: bead-quality.sh

Nudges agents to add required sections after creating incomplete beads.

```bash
#!/usr/bin/env bash
# bead-quality.sh — PostToolUse nudge after bd create
# Trigger: PostToolUse on Bash | Timeout: 5000ms
set -euo pipefail

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
cmd = d.get('tool_input', {}).get('command', '')
stdout = d.get('tool_result', {}).get('stdout', '')
print(f'{cmd}\n{stdout}')
" 2>/dev/null) || exit 0

COMMAND=$(echo "$PARSED" | sed -n '1p')
STDOUT=$(echo "$PARSED" | sed -n '2p')

[ -z "$COMMAND" ] && exit 0

# Only act on bd create commands
echo "$COMMAND" | grep -q '^bd create' || exit 0

# Extract bead ID from stdout
BEAD_ID=$(echo "$STDOUT" | grep -oP 'Created issue: \K[a-zA-Z0-9_-]+' || echo "")
[ -z "$BEAD_ID" ] && exit 0

# Detect type
TYPE="task"
echo "$COMMAND" | grep -q '\-\-type=bug' && TYPE="bug"

# Read bead description
DESC=$(bd show "$BEAD_ID" --json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
if isinstance(data, list) and len(data) > 0:
    print(data[0].get('description', '') or '')
" 2>/dev/null || echo "")

# Check required sections
MISSING=""
if [ "$TYPE" = "bug" ]; then
  echo "$DESC" | grep -q '## Steps to Reproduce' || MISSING="## Steps to Reproduce"
fi
echo "$DESC" | grep -qi '## Acceptance Criteria' || {
  [ -n "$MISSING" ] && MISSING="$MISSING, ## Acceptance Criteria" || MISSING="## Acceptance Criteria"
}

[ -z "$MISSING" ] && exit 0

python3 -c "
import json
print(json.dumps({
    'additionalContext': 'Bead quality: $BEAD_ID is missing required sections: $MISSING. Run: bd update $BEAD_ID --description \"...\" to add them now.'
}))
"
```

#### Hook 5: agent-tdd-enforce.sh

Injects TDD reminders into Agent dispatch prompts that lack TDD instructions. This catches the most common drift point: orchestrators dispatching implementation agents without specifying Red-Green-Refactor.

```bash
#!/usr/bin/env bash
# agent-tdd-enforce.sh — Inject TDD boilerplate into Agent dispatch prompts
# Trigger: PreToolUse on Agent | Timeout: 3000ms
set -euo pipefail

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
tool_name = d.get('tool_name', '')
prompt = d.get('tool_input', {}).get('prompt', '')
desc = d.get('tool_input', {}).get('description', '')
subtype = d.get('tool_input', {}).get('subagent_type', '')
print(f'{tool_name}\n{subtype}\n{desc}')
print('---PROMPT---')
print(prompt)
" 2>/dev/null) || exit 0

TOOL_NAME=$(echo "$PARSED" | sed -n '1p')
SUBTYPE=$(echo "$PARSED" | sed -n '2p')
DESC=$(echo "$PARSED" | sed -n '3p')
PROMPT=$(echo "$PARSED" | sed -n '5,\$p')

# Only act on Agent tool
[ "$TOOL_NAME" = "Agent" ] || exit 0

# Skip exploration/research agents
echo "$SUBTYPE" | grep -qiE '^(Explore|Plan|claude-code-guide)$' && exit 0
echo "$DESC" | grep -qiE '(explore|research|search|find|read|check|investigate)' && exit 0

# Skip if not implementation work
echo "$PROMPT" | grep -qiE '(implement|create|write|modify|add|fix|refactor|update|build)' || exit 0

# Check for TDD keywords
if echo "$PROMPT" | grep -qiE '(TDD|test.first|failing.test|RED.*GREEN|red-green|write.test|test.before)'; then
  exit 0
fi

# Check for [no-test] escape hatch
if echo "$PROMPT" | grep -qiE '\[no-test\]'; then
  exit 0
fi

# Missing TDD — inject reminder
python3 -c "
import json
print(json.dumps({
    'additionalContext': 'SABLE TDD ENFORCEMENT: This agent dispatch is for implementation work but contains no TDD instructions. Include Red-Green-Refactor: write failing test first, run to confirm RED, implement, run to confirm GREEN. For non-code tasks, add [no-test] to bead notes.'
}))
"
```

**Why this hook matters:** In practice, orchestrator agents frequently dispatch implementation workers without TDD instructions, especially for "simple" component creation tasks. The existing `tdd-gate` hook catches this at close time, but by then the agent has already written code-first. This hook catches the problem at dispatch time and reminds the orchestrator before the agent starts work.

#### Hook 6: bead-description-gate.sh

Validates bead descriptions at creation time, nudging for test specs and file paths.

```bash
#!/usr/bin/env bash
# bead-description-gate.sh — Validate bead descriptions on creation
# Trigger: PreToolUse on Bash (matching bd create) | Timeout: 3000ms
set -euo pipefail

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
cmd = d.get('tool_input', {}).get('command', '')
print(cmd)
" 2>/dev/null) || exit 0

COMMAND="$PARSED"
[ -z "$COMMAND" ] && exit 0

# Only act on bd create commands
echo "$COMMAND" | grep -q '^bd create' || exit 0

# Skip epics
echo "$COMMAND" | grep -qiE 'epic' && exit 0

# Check for description flag
if ! echo "$COMMAND" | grep -qE '\-\-description'; then
  python3 -c "
import json
print(json.dumps({
    'additionalContext': 'SABLE bead quality: No --description flag. Every bead needs a description that passes the Fresh Agent Test: file paths, function names, test file path, acceptance criteria.'
}))
"
  exit 0
fi

# Extract description content
DESC=$(echo "$COMMAND" | python3 -c "
import sys, re
cmd = sys.stdin.read()
m = re.search(r'--description[= ]\"([^\"]+)\"', cmd) or re.search(r\"--description[= ]'([^']+)'\", cmd)
print(m.group(1) if m else '')
" 2>/dev/null || echo "")

[ -z "$DESC" ] && exit 0

# Check for test spec
MISSING=""
if ! echo "$DESC" | grep -qiE '(test|\.test\.|\.spec\.|__tests__|pytest|vitest|TDD|red.green|\[no-test\])'; then
  MISSING="test spec (which test file, what assertions)"
fi

# Check for file paths
if ! echo "$DESC" | grep -qiE '(\.(ts|tsx|py|js|jsx)|frontend/|src/|lib/|components/)'; then
  [ -n "$MISSING" ] && MISSING="$MISSING, " || true
  MISSING="${MISSING}file paths (exact files to create/modify)"
fi

[ -z "$MISSING" ] && exit 0

python3 -c "
import json
print(json.dumps({
    'additionalContext': 'SABLE bead quality: Description missing: $MISSING. Good beads pass the Fresh Agent Test — agents should act immediately without re-exploring.'
}))
"
```

**Why this hook matters:** Vague beads are the second most common source of wasted agent cycles. An agent dispatched with a bead that says "fix the comparison table" will spend 5+ minutes reading files to understand the problem. A bead that says "modify ComparisonDataTable.tsx:127 — the React.Fragment needs a key prop" lets the agent start immediately.

### 5.4 Writing Your Own Hooks

When adding behaviors to SABLE, ask:

1. **Is this non-negotiable?** → Make it a PreToolUse hook that denies.
2. **Is this important but not worth blocking?** → Make it a PostToolUse or PreToolUse hook that injects context.
3. **Is this project-specific?** → Put it in the project CLAUDE.md.
4. **Is this general guidance?** → Add one line to the global CLAUDE.md (if it's really necessary).

Hook design principles:
- **Fast exit for non-matching commands.** Every hook fires on its matcher (e.g., all Bash commands). The first lines should check if this invocation is relevant and `exit 0` immediately if not.
- **Use python3 for JSON parsing.** It's universally available. Don't assume `jq` is installed.
- **Timeout generously but not excessively.** 3-5 seconds is usually plenty. A slow hook blocks every tool call that matches.
- **Fail open.** If the hook script errors, it should `exit 0` (allow) rather than blocking the agent. Use `|| exit 0` after parsing to handle unexpected input gracefully.
- **No side effects on non-matching commands.** A hook that runs on every Bash command must be invisible when it doesn't match.

---

## 6. Swarm Execution

> **Swarm is core to SABLE — it's literally in the name (Swarm Architecture with Bead-Led Execution).** This section describes the full execution model the methodology was built for.
>
> **For new adopters:** climb here from Foundation deliberately, not on day one. Swarm assumes:
> - **Sufficient API/usage budget** — parallel agents multiply token cost. A 5-agent swarm runs ~5× the cost of sequential.
> - **Strong bead-writing discipline** — bad beads in a swarm waste 5× the agent cycles. The Fresh Agent Test must be second nature.
> - **Comfort with merge orchestration** — even with `bd worktree`, you'll be merging multiple branches per session.
>
> If any of those aren't yet true for you, ship 2–3 features on the sequential pattern first. The discipline you build at Foundation is what makes swarms work — skipping the ramp is the most common failure mode. **Once you're ready, this is the level the methodology is designed for.**

### 6.1 The Orchestrator/Worker Model

SABLE uses a two-tier execution model:

**The Orchestrator** is the primary Claude instance in the user's conversation. It plans work, creates beads, dispatches agents, reviews results, and manages the session lifecycle. The orchestrator rarely writes code directly — it delegates to workers.

**Workers** are subagents dispatched by the orchestrator using Claude Code's Agent tool. Each worker receives a prompt specifying which beads to work on, claims them, implements the changes, runs tests, and closes the beads. Workers operate in isolation — they don't communicate with each other or with the orchestrator during execution.

The orchestrator's job is to make workers successful by:
1. Writing high-quality beads that pass the Fresh Agent Test
2. Bundling related beads intelligently
3. Ensuring no two workers touch the same files
4. Reviewing results and handling failures

### 6.2 Dispatching Workers

A good dispatch prompt is concise and specific:

```
You are working in /path/to/repo on the dev branch.

Beads to work on:
- <bead-id-1>: Fix cache key collisions in orchestrator.py:142
- <bead-id-2>: Add cache key unit tests in test_cache.py

Claim both beads first with bd update <id> --claim.
Work sequentially: fix the code, write/run tests, close each bead.
Run: python -m pytest tests/orchestration/test_cache.py -v to verify.
Close: bd close <bead-id-1> <bead-id-2>
```

What NOT to include in dispatch prompts:
- Workflow rules (CLAUDE.md provides these automatically)
- Bash rules (global settings handle permissions)
- TDD instructions (hooks enforce this mechanically)

The prompt should answer three questions: **Which beads? What files? What commands?**

### 6.3 Bead Bundling

Cluster 2-3 related beads per worker when they share context (same file, same directory, same feature). Benefits:

- Workers read the file once and make multiple related changes
- Fewer total agents = less overhead
- Related changes are committed together, reducing merge noise

Don't over-bundle. If beads touch different subsystems, dispatch them as separate workers — parallelism is more valuable than reducing agent count.

### 6.4 Worktree Isolation

Parallel agents editing the same working tree is the single biggest source of merge conflicts in swarm execution. The fix is `git worktree` — separate working directories sharing one git repo, so each worker operates in its own checkout.

Beads provides `bd worktree` as the right way to do this. Plain `git worktree add` works but creates two problems: (1) the new worktree has no `.beads` database, so `bd` commands fail; (2) you have to wire up branch naming and gitignore manually.

```bash
# Create a worktree at ./feature-auth on a new branch of the same name
bd worktree create feature-auth

# With a specific branch name
bd worktree create worker-1 --branch=fix/cache-keys

# Outside the repo (avoid cluttering main checkout)
bd worktree create ../agents/worker-1

# Inspect
bd worktree list           # All worktrees
bd worktree info           # Info about the current one
bd worktree remove <name>  # Remove (with safety checks)
```

`bd worktree create` automatically:
- Creates the git worktree at `./<name>` (or specified path)
- Sets up `.beads/redirect` pointing to the main repo's beads database (workers see the same issue state)
- Adds the worktree path to `.gitignore` if inside the repo root

**The pattern for swarms:** before dispatching N parallel workers, the orchestrator creates N worktrees (one per worker), and each worker's dispatch prompt specifies its worktree path. Workers can edit overlapping areas without stepping on each other; the orchestrator merges their branches sequentially after they close.

**Anti-pattern:** dispatching multiple workers into the same working tree because "they're touching different files." This breaks the moment one worker imports from a file another is editing, when shared config changes, or when test runners cache state. Use a worktree per worker; the cost is one bash call.

### 6.5 Formal Swarms with `bd swarm`

Section 6.2 covered the dispatch pattern. `bd swarm` formalizes it: an epic + its children become a registered "swarm molecule" with structure-aware tooling.

```bash
# Validate before dispatching — surfaces ready fronts and warnings
bd swarm validate <epic-id>
bd swarm validate <epic-id> --verbose   # Include detailed graph

# Create the swarm molecule (registers it for discovery)
bd swarm create <epic-id>

# Live status
bd swarm status

# List all swarms
bd swarm list
```

`bd swarm validate` is the highest-leverage piece. It outputs:
- **Ready fronts** — waves of parallel work (children with no remaining blockers)
- **Estimated worker-sessions** — how many dispatches the swarm will need
- **Maximum parallelism** — the widest front in the DAG
- **Warnings** — circular deps, stranded children, missing test files

Run this before creating worktrees. If max parallelism is 2, you don't need 5 worktrees. If validate shows warnings, fix them before dispatching — debugging a swarm mid-execution is much harder than fixing the plan.

**When to use `bd swarm` vs ad-hoc dispatch:**
- **Ad-hoc**: 2-3 beads, all clearly independent, you can hold the whole plan in your head. Just dispatch.
- **Formal swarm**: 4+ beads with non-trivial dependencies, or work that will span multiple sessions. The extra ceremony pays for itself in legibility.

### 6.6 Advanced Coordination Primitives

Two primitives exist for cross-cutting coordination but are situational. Use them when you actually need them; don't pre-emptively build workflows around them.

- **`bd gate`** — async wait conditions that block downstream work. Types: `human` (manual close), `timer`, `gh:run` (waits for a workflow), `gh:pr` (waits for a PR merge), `bead` (waits for a bead in another rig). Use when one bead's progress depends on something happening *outside* the local issue tracker.
- **`bd merge-slot`** — a mutex for the merge queue. Only one agent holds the slot at a time, preventing parallel agents from racing to resolve conflicts and creating cascading conflicts. Use when running auto-merging swarms; not needed when a human reviews and merges each PR sequentially.

`bd gate --help` and `bd merge-slot --help` have full reference. Document a project-specific use case in the project's CLAUDE.md *after* you've used the primitive once.

### 6.7 Overlap Resilience

In a swarm, things don't always go as planned:

- **A worker finds its bead already closed.** Another worker completed it (perhaps it was a dependency that got resolved). The worker should skip gracefully and continue to its next bead.

- **Two workers edit the same file.** This happens when worktree isolation isn't used or when bundling isn't perfect. Git handles most merge conflicts automatically. For the rest, the orchestrator resolves conflicts after workers complete.

- **A worker fails mid-task.** The bead stays open (in_progress status). The orchestrator can dispatch a new worker to pick it up, or investigate and re-plan.

The key insight: **beads make recovery trivial.** Because every unit of work is tracked independently, a failed worker doesn't corrupt the overall plan. You just re-dispatch.

### 6.8 Grep After Refactors

When a worker removes or renames a variable, function, import, or prop, it **must** grep for all references across the codebase before closing. Removing a declaration without updating every consumer causes runtime errors that surface later and are harder to debug.

This rule should be included in dispatch prompts for any bead that involves refactoring:

```
After renaming/removing, grep for ALL references across the codebase
before closing. Removing a declaration without updating consumers
causes runtime errors.
```

### 6.9 Model Selection for Workers

Different tasks benefit from different models:

- **Sonnet** (or equivalent fast model): Implementation tasks, test writing, bug fixes, refactoring. The majority of worker dispatches.
- **Opus** (or equivalent reasoning model): Architecture decisions, complex debugging, nuanced code review. Used by the orchestrator or for specific difficult beads.

The orchestrator runs on the most capable model available. Workers run on the fastest model that can handle the task. This optimizes for total throughput — many fast workers executing well-specified beads is faster than one powerful agent doing everything sequentially.

---

## 7. Documentation Strategy

### 7.1 The Two-Document Model

SABLE uses exactly two CLAUDE.md files per project:

**Global CLAUDE.md** (`~/.claude/CLAUDE.md`)
- Cross-project workflow rules
- Beads CLI reference (essential commands only)
- Anti-patterns table
- Session-close protocol
- ~100-120 lines maximum

**Project CLAUDE.md** (`<project-root>/CLAUDE.md`)
- Repository overview and architecture
- Test commands with exact invocations
- File naming conventions
- Key configuration (env vars, database setup)
- Modification recipes for common tasks
- ~60-100 lines maximum

### 7.2 What Goes Where

| Content | Location | Rationale |
|---------|----------|-----------|
| `bd` workflow rules | Global | Same across all projects |
| Anti-patterns table | Global | Universal lessons |
| Session-close protocol | Global | Always applies |
| Test commands (`pytest`, `vitest`) | Project | Different per project |
| File conventions (where tests live) | Project | Different per project |
| Architecture overview | Project | Unique to each repo |
| TDD enforcement | Hooks | Non-negotiable — can't be in docs |
| Bead quality checks | Hooks | Non-negotiable — can't be in docs |
| Test reminder on edit | Hooks | Non-negotiable — can't be in docs |

### 7.3 Why Less Documentation = Better Compliance

Every line in CLAUDE.md competes for attention. Adding a line doesn't just add information — it dilutes everything else. A 50-line file where every line matters will be followed more reliably than a 300-line file with the same important lines buried among context.

Practical guidelines:
- **Before adding to CLAUDE.md, ask: can a hook enforce this instead?** If yes, write the hook.
- **Remove lines that duplicate what the code shows.** If the convention is obvious from reading the codebase, documenting it is redundant.
- **Audit quarterly.** Read every line and ask: is this still true? Is this still necessary? Has a hook replaced this? Remove aggressively.

---

## 8. Session Management

### 8.1 Starting a Session

Every session begins the same way:

```bash
bd ready            # See what's available to work on
bd list --status=in_progress  # Check if anything was left mid-work
```

If `bd ready` shows nothing, the orchestrator's job is to plan the next batch of work — review the codebase, identify gaps, create beads, add dependencies.

If `bd ready` shows available beads, the orchestrator dispatches workers.

If `in_progress` beads exist, something was interrupted. Investigate: is the work partially done? Should it be re-dispatched or re-planned?

### 8.2 Cross-Session Handoff

This is where the amnesiac model pays off. Session handoff in SABLE is trivial because **all state lives in beads and code**:

- Open beads = work remaining
- Closed beads = work completed
- In-progress beads = work interrupted (investigate)
- `git log` = what was changed and when
- `git status` = what's uncommitted

There's no "session notes" to read. No "what I was thinking" document. The next agent (or human) runs `bd ready` and picks up where things left off. The beads contain everything needed to continue.

### 8.3 Session-Close Protocol

Work is not done until it's pushed. This is mandatory — no exceptions:

```bash
# 1. Close finished beads
bd close <completed-bead-ids>

# 2. Create beads for any remaining work or discovered issues
bd create --title="..." --description="..." --type=...

# 3. Commit code changes
git add <specific-files>
git commit -m "descriptive message"

# 4. Push everything
git pull --rebase
bd dolt push        # Push beads to Dolt remote
git push            # Push code to Git remote
git status          # MUST show "up to date with origin"
```

**Why this is non-negotiable:** An agent that does great work but doesn't push has accomplished nothing. The next session starts fresh. If the work isn't in the remote, it doesn't exist. Treat unpushed work as lost work.

### 8.4 Context Recovery

When context is compacted (long sessions) or a new session starts, the `bd prime` command re-injects critical workflow context. SABLE configures this as a SessionStart and PreCompact hook so it happens automatically.

---

## 9. Anti-Patterns & Failure Modes

These are battle-tested lessons from real swarm development.

### 9.1 The Anti-Patterns Table

| Anti-Pattern | Why It's Bad | Instead |
|---|---|---|
| `bd edit` | Opens `$EDITOR` (vim/nano), hangs the agent | `bd update <id> --description "..."` |
| Markdown TODO lists | Splits tracking between docs and beads | `bd create` for everything |
| Temporal dep language ("A before B") | Inverts `bd dep add` arguments | Requirement language: "B needs A" |
| Not closing blockers promptly | Freezes all downstream beads | `bd close <id>` immediately when done |
| Stopping without pushing | Strands work locally — next session can't see it | `bd dolt push` + `git push` always |
| Code without tests | Hook blocks `bd close`, wastes agent time | Write failing test first; `[no-test]` for docs-only |
| Vague bead descriptions | Agent wastes a full cycle re-exploring the codebase | Pass the Fresh Agent Test: file paths, function names, approach |
| Over-bundling beads per agent | Single failure blocks all bundled beads | 2-3 related beads max per worker |
| Skipping grep after refactors | Removed declarations leave broken consumers | Grep all references before closing refactor beads |
| Growing CLAUDE.md past 150 lines | Compliance drops as document length increases | Move enforcement to hooks, keep docs lean |
| Asking "should I log this?" | Agent amnesia means it won't be logged next session | Just create the bead immediately |
| Dispatching without test spec | Agent writes code but not tests, gets blocked by gate | Include test file path and assertions in every dispatch prompt (hook 5 enforces this) |
| Dispatching agents without TDD instructions | Agent writes code-first, TDD gate catches too late | Hook 5 (agent-tdd-enforce) injects TDD reminder at dispatch time |
| Creating beads without file paths or test specs | Agent wastes cycle re-exploring codebase | Hook 6 (bead-description-gate) nudges at creation time |

### 9.2 Recovery Patterns

**Stuck agent (can't close due to TDD gate):**
The agent got blocked because it didn't run tests. This is working as intended. The agent should run the relevant test suite and then retry the close.

**Bead with broken description:**
The bead-quality hook will nudge after creation. If the nudge was ignored (shouldn't happen, but might in edge cases), the orchestrator should `bd update` the description before dispatching a worker.

**Worker finished but bead still open:**
Check if the worker crashed or timed out. The bead is in `in_progress` status. Either re-dispatch or investigate. Use `bd show <id>` to see the current state.

**Merge conflicts after parallel workers:**
Normal in swarm execution. The orchestrator resolves conflicts, or dispatches a dedicated worker to resolve them. The beads for the conflicting work are already closed — the merge resolution is a new (small) task.

**Circular dependencies:**
`bd blocked` will surface beads that are blocked by each other. Break the cycle by removing one dependency: re-read the beads and determine which one can actually proceed independently.

---

## 10. Getting Started

### 10.1 Prerequisites

- **Claude Code** (CLI, desktop, or IDE extension) — the AI development environment
- **Beads CLI** (`bd`) — the issue tracker. Install from [github.com/steveyegge/beads](https://github.com/steveyegge/beads)
- **Git** — version control
- **Python 3** — used by hook scripts for JSON parsing
- A project with a test framework already configured (pytest, vitest, jest, etc.)

### 10.2 Step-by-Step Setup

#### Step 1: Initialize Beads

```bash
cd your-project
bd init
```

This creates the `.beads/` directory in your repository.

#### Step 2: Create Hook Scripts

Create the directory and all six hook scripts:

```bash
mkdir -p ~/.claude/hooks
```

Copy the six scripts from [Section 5.3](#53-the-sable-hook-catalog) into:
- `~/.claude/hooks/tdd-evidence.sh`
- `~/.claude/hooks/tdd-gate.sh`
- `~/.claude/hooks/tdd-remind.sh`
- `~/.claude/hooks/bead-quality.sh`
- `~/.claude/hooks/agent-tdd-enforce.sh`
- `~/.claude/hooks/bead-description-gate.sh`

Make them executable:
```bash
chmod +x ~/.claude/hooks/*.sh
```

**Customize the test patterns** in `tdd-evidence.sh` for your stack. The default pattern matches `pytest`, `vitest`, `npm test`, `jest`, `cargo test`, and `go test`. Add your framework's test command if it's not listed.

#### Step 3: Register Hooks in Settings

Add to `~/.claude/settings.json` (create if it doesn't exist):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/tdd-evidence.sh",
            "timeout": 3000
          },
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/tdd-gate.sh",
            "timeout": 5000
          },
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/bead-description-gate.sh",
            "timeout": 3000
          }
        ]
      },
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/tdd-remind.sh",
            "timeout": 3000
          }
        ]
      },
      {
        "matcher": "Agent",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/agent-tdd-enforce.sh",
            "timeout": 3000
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/bead-quality.sh",
            "timeout": 5000
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "bd prime"
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "bd prime"
          }
        ]
      }
    ]
  }
}
```

#### Step 4: Write Global CLAUDE.md

Create `~/.claude/CLAUDE.md` with your cross-project workflow rules. Keep it under 120 lines. Template:

```markdown
# Global Instructions

## ⚠️ Prime Directive

**ALL work flows through beads. No exceptions.**

Every bug fix, feature, refactor, investigation, and noticed issue must exist as a bead before you act on it. If you cannot point to the bead your current action serves, stop and create one. `bd q "<title>"` takes three seconds — there is no task too small. Never substitute TodoWrite, scratch lists, or memory for beads. This is the foundation; bypassing it corrupts every other practice.

## Beads Issue Tracker

All projects use **bd (beads)** for issue tracking.

### Quick Reference
- `bd ready` — Find available work
- `bd show <id>` — View issue details
- `bd update <id> --claim` — Claim work
- `bd close <id>` — Complete work
- `bd create --title="..." --description="..." --type=bug|task|feature --priority=2`
- `bd q "<title>"` — Quick capture (just outputs ID; use for failure-trigger logging)
- `bd defer <id>` — Real bead but not for now (drops out of `bd ready`)
- `bd children <id> --pretty` — Tree view of children under a parent/epic
- `bd swarm validate <epic-id>` — Pre-flight a swarm: see ready fronts, parallelism, warnings
- `bd worktree create <name>` — Isolated worktree with shared beads database (use before parallel dispatch)
- `bd preflight` — PR readiness check before `git push`

### Rules
- Use `bd` for ALL task tracking
- Issue discovery is mandatory — see a bug, log a bead
- **Every code change requires both unit AND integration tests** (smoke tests encouraged). Unit tests alone are insufficient — see SABLE §4.5.
- One `bd` command per Bash call (no chaining with && or ;)
- Never use `bd edit` — it opens $EDITOR and hangs agents

### Description Quality: The Fresh Agent Test
Could a fresh agent act on this bead without re-exploring source files?
A good description includes: file paths, function names, what's wrong,
suggested approach, **and a test spec listing BOTH a unit test
(file::test_name + assertions) AND an integration test (file::test_name +
real dependencies it exercises).** If integration testing is genuinely
not applicable, the bead must say so explicitly.

### Dependencies
"B needs A" → `bd dep add B A` (requirement language, not temporal)

### Dispatching Subagents
- Bundle 2-3 related beads per agent
- Include test file path and failing assertion in every prompt
- Grep all references after refactors before closing
- **Subagent capability inheritance is not automatic** — Bash, Edit, and Write permissions don't propagate from the orchestrator. If a subagent needs to run shell commands or modify files, either enable Accept Edits at the conversation level OR scope the subagent to read-only work (Read/Glob/Grep/Write to a known output path).

### Shell Command Style
Claude Code's ambiguous-syntax check fires **before** the allow list and prompts the user for any of these, even inside correctly-quoted strings:

| Trigger | Example |
|---------|---------|
| Command chaining | `cmd1 && cmd2`, `cmd1 \|\| cmd2`, `cmd1; cmd2` |
| Pipes / redirection | `cmd \| grep`, `cmd > file`, `cmd 2>&1` |
| Command substitution | `$(cat foo)`, `` `cmd` `` |
| Heredoc | `<<'EOF'` |

**To run commands without permission prompts:**
1. **Use Read / Glob / Grep / Edit / Write tools for file operations** — never bash for file reading or editing
2. **One command per Bash call** — never chain with `&&`, `||`, or `;`
3. **Resolve dynamic values once and hardcode** — instead of `$(cat .interpreter) script.py`, look up the path with Read, then call the absolute path directly
4. **Write multi-step pipelines to a file, then run as one line** — `python3 /tmp/pipeline.py` beats inlining `python3 -c "..."` with chained commands
5. **No heredocs** — use Write to create the file, then Bash to run it

The cost of one extra Bash call is small. The cost of breaking flow on every permission prompt compounds.

### Anti-Patterns
| Anti-Pattern | Why | Instead |
|---|---|---|
| `bd edit` | Hangs agent | `bd update <id> --field "..."` |
| Code without tests | Hook blocks close | Test first; `[no-test]` for docs-only |
| Code with only unit tests | Mocks hide composition bugs that ship to prod | Add integration tests against real deps; `[no-integration]` only with explicit reason in bead |
| Mocking the database in integration tests | Tests pass while real system breaks | Use a real local DB (Docker, sqlite); mocks belong in unit tests |
| Vague descriptions | Wastes agent cycle | Pass the Fresh Agent Test |
| Stopping without push | Strands work | Push always |
| `&&` / `\|\|` / `;` chains in Bash | Triggers permission prompt | One command per Bash call |
| `$(cmd)` substitution in Bash | Triggers permission prompt | Resolve once, hardcode the value |
| `python3 -c "long script"` | Hard to debug, often chained | Write to file, run with `python3 /tmp/foo.py` |
| Subagent assumes Bash works | Permissions don't propagate | Verify capability or scope to read-only |
| Manual `git worktree add` for parallel agents | Misses beads redirect; no `bd` access in worktree | `bd worktree create <name>` |
| Multiple workers in same working tree | Cascading merge conflicts | One worktree per worker |
| Skipping `bd swarm validate` before swarm dispatch | Find structural issues mid-execution | Validate first; warnings before workers |

## Session Close Protocol
1. Close finished beads
2. Create beads for remaining work (use `bd q` for quick capture; `bd defer` for "real but not now")
3. Run `bd preflight` for the PR readiness checklist
4. git commit + git push
5. bd dolt push
6. git status must show "up to date with origin"
7. If worktrees were used: `bd worktree remove <name>` for any completed ones
```

#### Step 5: Write Project CLAUDE.md

Create `<project-root>/CLAUDE.md` with project-specific details. Template:

```markdown
# Project CLAUDE.md

## Overview
[Brief description of the project]

## Test Commands
[Exact commands for running tests in this project]

## File Conventions
[Where source files live, where test files live, naming patterns]

## Architecture
[Key components, data flow, tech stack]

## Common Tasks
[Recipes for common modifications]
```

Keep it under 100 lines. Focus on what an agent needs to know to work in this specific codebase.

#### Step 6: Verify the Setup

Run these checks to confirm everything works:

```bash
# 1. Beads operational
bd ready

# 2. Create a test bead
bd create --title="Test bead — verify SABLE setup" \
  --description="Verify the full workflow works." \
  --type=task --priority=4

# 3. Try to close without running tests — should be blocked
bd close <bead-id>
# Expected: "TDD gate: No tests were run this session..."

# 4. Run your test suite
pytest  # or npm test, vitest, etc.

# 5. Now close — should succeed
bd close <bead-id>

# 6. Edit a source file without a test — should see reminder
# (Edit any .py or .ts file that doesn't have a test)

# 7. Clean up
git status  # Should be clean
```

---

## Appendix A: Glossary

| Term | Definition |
|------|-----------|
| **Bead** | A unit of work in the beads issue tracker — a bug, task, or feature with structured metadata |
| **Fresh Agent Test** | The quality bar for bead descriptions: could a fresh agent act on this without re-exploring? |
| **Orchestrator** | The primary Claude instance that plans, dispatches, and reviews |
| **Worker** | A subagent dispatched to execute specific beads |
| **Swarm** | Multiple workers executing in parallel |
| **Hook** | A shell script that runs at Claude Code lifecycle events to enforce behavior |
| **TDD Gate** | The hook that blocks `bd close` without test evidence |
| **Evidence File** | A session-scoped temp file logging test executions |
| **Escape Hatch** | The `[no-test]` marker for non-code beads |
| **Bundle** | A group of 2-3 related beads assigned to a single worker |

## Appendix B: Quick Reference Card

```
SESSION START         SESSION CLOSE          DISPATCH PATTERN
─────────────         ─────────────          ────────────────
bd ready              bd close <ids>         Agent prompt:
bd list --status=     bd create (remaining)  - Which beads
  in_progress         git add + commit       - What files
                      git pull --rebase      - What commands
                      bd dolt push           - bd close <ids>
                      git push
                      git status → clean

BEAD LIFECYCLE        HOOKS                      TDD CYCLE
──────────────        ─────                      ─────────
create → open         tdd-evidence (log)         Red: failing test
claim → in_progress   tdd-gate (block close)     Green: implement
close → closed        tdd-remind (nudge edit)    Refactor: clean up
                      bead-quality (nudge create) Gate: close bead
                      agent-tdd-enforce (nudge dispatch)
                      bead-description-gate (nudge create)
```
