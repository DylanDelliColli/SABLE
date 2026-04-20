# Multi-Manager Coordination Pattern

> **Status: experimental — `personal-tooling` branch only.** This pattern extends SABLE for high-throughput, multi-agent power-user workflows. Do not adopt until the prerequisites below are second nature.

A coordination pattern for running 2-3 named manager agents (each commanding their own worker swarm) in parallel against the same repository, with mechanical conflict prevention, addressed inter-agent messaging, and selective dispatch preemption.

---

## When to use this

This pattern is **not** part of the SABLE on-ramp. It assumes you already operate fluently at the Swarm stage of the standard adoption path (Foundation → Hierarchy → Swarm) and are running into the specific failure modes below:

- You are running 2+ persistent manager agents in parallel terminals (e.g. one focused on epics, another on one-off bugfixes, a third on integration)
- Your throughput is high enough that 30-minute-old branches conflict with each other (not 3-day-old)
- You spend non-trivial time relaying messages between agents (e.g. merge agent → epic manager) instead of doing strategic work
- Bead descriptions in your repo reliably name files (Fresh Agent Test passes consistently)
- Your usage budget supports running multiple Claude Code sessions concurrently

If any of these aren't true, stay on standard SABLE. Adopt this pattern after you've felt the specific pain it solves.

---

## The three-manager architecture

This pattern is described in terms of three concrete managers — adapt the names and scopes to your setup. The mechanism is the same.

| Agent | Type | Scope | Claims |
|-------|------|-------|--------|
| **Optimus** | epic_manager | Large feature epics, hardening, multi-bead sequences | Beads with a parent epic, or epics themselves |
| **Tarzan** | one_off_manager | Bugfixes, one-offs, doc updates, anything standalone | Orphan beads (no parent), regardless of priority |
| **Chuck** | integrator | Merge queue, PR review, conflict resolution | `for-chuck` beads only (does not claim from general pool) |

**Critical**: ownership is based on **work shape**, not priority. A P0 auth-breaking bug is Tarzan's territory if it's standalone. A P3 nice-to-have refactor is Optimus's territory if it's an epic. Priority signals urgency; structure signals ownership.

### Why three?

Two managers (Optimus + Tarzan) plus an integrator (Chuck) is the minimum viable shape for separation of concerns:

- Without Tarzan, Optimus gets pulled into one-liner bugs that interrupt epic work
- Without Optimus, Tarzan tries to run epics they're not structured for
- Without Chuck, the human becomes the merge-queue bottleneck and the messenger between the other two

Adding more agents (e.g., a Researcher, a QA specialist) is supported via the registry pattern — see [Adding agents](#adding-agents).

---

## Identity & immutability

Each manager launches with an immutable identity established at the OS level, not in conversation context.

### Launch aliases

```bash
# In ~/.zshrc or equivalent
alias optimus='CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager claude'
alias tarzan='CLAUDE_AGENT_NAME=tarzan CLAUDE_AGENT_ROLE=manager claude'
alias chuck='CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager claude'
```

Hooks inherit the Claude Code process's environment. An agent running `export CLAUDE_AGENT_NAME=tarzan` inside a Bash tool only affects that subshell, which is discarded after the command. The env var is effectively immutable from the agent's perspective.

### Identity injection

A `SessionStart` hook reads `$CLAUDE_AGENT_NAME`, loads the corresponding role file from `~/.claude/sable/roles/<name>.md`, and injects it as the agent's identity context. A `PreCompact` hook re-injects after compaction (identity erodes silently otherwise).

### Subagent context discrimination

Subagents dispatched via the `Agent` tool inherit the parent's environment, so `$CLAUDE_AGENT_NAME` propagates. To prevent subagents from being mistaken for managers (and triggering manager-specific hooks), every relevant hook checks for the `agent_id` field in hook input JSON:

```bash
AGENT_ID=$(jq -r '.agent_id // empty')
if [ -n "$AGENT_ID" ]; then
  exit 0  # subagent context — skip manager-specific behavior
fi
```

`agent_id` is documented to be **present in subagent contexts and absent in main-session contexts**. This is the discriminator. Without it, env-var-based identity would leak from manager sessions into their dispatched subagents.

---

## Registry

`~/.claude/sable/agents.yaml` is the single source of truth for agent properties. All hooks read it dynamically. Adding or modifying an agent requires no hook code changes.

```yaml
agents:
  optimus:
    type: epic_manager
    scope: "Large feature epics, hardening work, multi-bead sequences"
    claim_filter: "--has-parent"          # bd ready filter for general pool
    inbox_label: for-optimus
    role_prompt: roles/optimus.md
    dispatches_workers: true

  tarzan:
    type: one_off_manager
    scope: "Bugfixes, one-offs, docs — anything standalone"
    claim_filter: "--no-parent"
    inbox_label: for-tarzan
    role_prompt: roles/tarzan.md
    dispatches_workers: true

  chuck:
    type: integrator
    scope: "Merge queue, PR review, conflict resolution"
    claim_filter: "for-chuck-only"        # only acts on addressed coord beads
    inbox_label: for-chuck
    role_prompt: roles/chuck.md
    dispatches_workers: false
    fix_directly:
      - imports
      - lockfile
      - whitespace
      - non_overlapping_diffs
      - docs
    delegate_to_author:
      - overlapping_logic
      - semantic_conflicts
      - test_divergence
    post_push_autoack: true
```

---

## Communication

### Label convention

| Label | Meaning |
|-------|---------|
| `for-optimus` | Addressed to Optimus's inbox |
| `for-tarzan` | Addressed to Tarzan's inbox |
| `for-chuck` | Addressed to Chuck's inbox |
| `coord` | Umbrella label for all coordination traffic (filterable in one query) |

### When to address at creation

| Bead origin | Address at creation? | Why |
|-------------|----------------------|-----|
| Coordination (one agent → another) | **Always** | The whole point is "this is for you" |
| Discovered work with obvious fit | Optionally | Saves a routing step |
| General planning output | Usually not | Let managers claim from the pool via their `claim_filter` |

### `/inbox` slash command

Manual pull for managers who want to deliberately check their inbox without waiting for an automatic injection. Backed by `bd ready -l for-$CLAUDE_AGENT_NAME`. Lives at `~/.claude/commands/inbox.md`.

### Inbox injection (automatic push)

A `PostToolUse` hook on `Bash` queries `bd ready -l for-<self>` after every tool call in the main session. If there are unread items (compared against a session-scoped dedup file), it injects a notification via `additionalContext`. Effective latency for active managers: seconds.

The hook fast-exits when `agent_id` is present (subagent context) so workers never see manager inbox material.

A `PreCompact` hook clears the dedup file so post-compact re-injection re-orients the manager to outstanding messages.

### Read guard (no cross-inbox queries)

A `PreToolUse` hook on `Bash` denies any `bd ready -l for-<other>` or `bd list -l for-<other>` query where `<other> != $CLAUDE_AGENT_NAME`. Optimus literally cannot read Tarzan's inbox. Mechanical, not discretionary.

---

## Coordination mechanisms (rolling execution)

This pattern uses **rolling execution**: managers dispatch workers concurrently as work becomes available, with no batching. The conflict-prevention surface is mechanical:

### 1. Pre-dispatch worktree refresh

Before any worker is dispatched into a worktree, a `PreToolUse:Agent` hook runs:

```bash
git -C <worktree> fetch origin
git -C <worktree> rebase $SABLE_BASE_BRANCH
```

`$SABLE_BASE_BRANCH` defaults to `origin/main` but can be set per-repo (e.g. `export SABLE_BASE_BRANCH=origin/dev` for repos with a dev integration branch).

**Effect**: workers always start from current state. Eliminates "Optimus dispatched on a 30-min-old base" entirely.

### 2. WIP file claims

When a manager dispatches a worker for bead-X, a `PreToolUse:Agent` hook reads the bead's description, extracts file paths (which the Fresh Agent Test requires), and pre-writes them to bead-X's notes:

```
WIP-CLAIMS: src/auth/foo.ts, tests/auth/foo.test.ts
```

Claims exist *before* the worker starts editing, closing the dispatch-time race condition.

A `PreToolUse:Edit|Write` hook reconciles emergent claims — if the worker modifies a file not declared in the bead description (legitimate scope creep), the file is appended to claims automatically.

### 3. Overlap awareness (advisory, not blocking)

A `PreToolUse:Agent` hook reads claims from all in-progress beads. If the proposed dispatch would touch files claimed by another in-progress bead, the hook **annotates** rather than denies:

```
OVERLAP DETECTED:
  Proposed dispatch (bd-205): foo.ts, bar.ts
  In-progress (bd-147, tarzan): foo.ts
  Decision: dispatch will proceed; file a coord bead if intentional collaboration is needed.
```

The annotation is also pushed into the eventual PR description and the `for-chuck` notification bead, so Chuck can sequence merges intelligently. Information-rich, not enforcement-heavy.

### 4. Selective dispatch preemption

A `PreToolUse:Agent` hook checks the manager's inbox for any `priority=0` coord bead. If one exists, **the next dispatch is denied** with a message: "Resolve urgent coord bead bd-N before dispatching new work."

Existing dispatched workers are unaffected — only the *next* dispatch is blocked. This is structural, not discretionary: the manager cannot dispatch new work while urgent feedback is pending.

**Escape valve**: `bd defer <id> --reason="..."` removes the bead from the block list while keeping it visible. Use when the user is AFK and has explicitly told the agent to defer blockers.

### 5. Pre-push rebase + test

A `PreToolUse:Bash` hook matching `git push` enforces:

```bash
git fetch origin
git rebase $SABLE_BASE_BRANCH
<test command>
```

Push is denied if the rebase fails or tests fail. Catches "my branch is behind main" and regression cases *locally* before exposing them to CI.

**Run a fast subset, not the full suite.** Pre-push blocks the manager agent while tests run — if the suite takes 5 minutes, that's 5 minutes of dead time per push. The recommended pattern is to scope `SABLE_TEST_COMMAND` to a fast subset (smoke + changed unit tests), target <60 seconds, and keep full-suite validation in CI. Anything slower and operators reach for `SABLE_SKIP_PRE_PUSH=1`, defeating the purpose.

Examples:

```bash
# Vitest — changed-only tests
export SABLE_TEST_COMMAND="npm test -- --changed --run"

# Pytest — unit layer with fail-fast and last-failed prioritization
export SABLE_TEST_COMMAND="pytest tests/unit -x --lf"

# Custom script
export SABLE_TEST_COMMAND="bash ./scripts/pre-push-smoke.sh"
```

**Timeout coupling — both values must match.** The hook has two independent timeouts:

| Timeout | Location | Default | Controls |
|---------|----------|---------|----------|
| Inner (test budget) | `$SABLE_PRE_PUSH_TEST_TIMEOUT` (seconds) | 60 | How long the test command may run |
| Outer (hook budget) | `"timeout"` in settings.json (ms) | 90000 | How long Claude Code lets the entire hook run |

The outer MUST exceed the inner plus ~30s buffer for fetch/rebase. If you raise one, raise the other. If the outer is lower than the inner, Claude Code kills the hook before tests finish — they appear to "pass" because the hook exits nonzero without returning a deny decision.

Default pairing (inner 60s / outer 90000ms) is sized for a fast pre-push subset. For a 5-minute test budget: set `SABLE_PRE_PUSH_TEST_TIMEOUT=300` AND change settings.json to `"timeout": 330000`.

### 6. Post-push Chuck notification

A `PostToolUse:Bash` hook matching successful `git push` files a `for-chuck` bead:

```
PR <url> ready for review
Files modified: foo.ts, bar.ts
Overlap analysis:
  - bd-147 (tarzan, in-progress): foo.ts
  - bd-203 (optimus, in-progress): bar.ts
```

Chuck's inbox injection picks this up immediately. Chuck has overlap context to sequence merges intelligently — hold this PR if a related PR is in flight, merge if independent.

### 7. Chuck's fix-vs-delegate threshold

Chuck uses the registry's `fix_directly` and `delegate_to_author` lists to decide:

| Fix directly | Delegate to author |
|--------------|--------------------|
| Import order/grouping conflicts | Two branches changed overlapping logic |
| Lockfile conflicts (package-lock, Cargo.lock, yarn.lock) | Competing function signatures |
| Non-overlapping diff regions in same file | Different implementations of same behavior |
| Whitespace, formatting | Semantic config changes (env defaults, feature flags) |
| Pure doc conflicts | Test expectations diverging |

Mechanical conflicts (no intent to get wrong) → Chuck fixes inline. Semantic conflicts (where author intent matters) → Chuck files a `for-<author>` bead with the conflict context and a suggested resolution.

---

## Hook catalog (advanced — multi-manager)

All hooks live in `hooks/multi-manager/`. They compose with the existing SABLE hook catalog (six hooks for TDD enforcement, bead quality, agent-dispatch enforcement) without overlap.

| Hook | Trigger | Purpose | Mode |
|------|---------|---------|------|
| `session-role-anchor.sh` | SessionStart, PreCompact | Inject role identity from `~/.claude/sable/roles/<name>.md` | Inject context |
| `read-guard.sh` | PreToolUse:Bash | Deny `bd ready -l for-<foreign>` queries | Hard deny |
| `inbox-injection.sh` | PostToolUse:Bash | Inject unread for-self bead notifications (with dedup, agent_id skip) | Inject context |
| `pre-dispatch-refresh.sh` | PreToolUse:Agent | Rebase target worktree on `$SABLE_BASE_BRANCH` | Side effect (rebase) |
| `pre-dispatch-claim.sh` | PreToolUse:Agent | Read bead description, write file claims to bead notes | Side effect (bd update) |
| `pre-dispatch-overlap.sh` | PreToolUse:Agent | Annotate overlap with other in-progress beads | Inject context |
| `pre-dispatch-preempt.sh` | PreToolUse:Agent | Block dispatch if P0 coord bead in inbox | Hard deny |
| `edit-write-claim-reconciler.sh` | PreToolUse:Edit\|Write | Append modified file to bead claims | Side effect (bd update) |
| `pre-push-rebase-test.sh` | PreToolUse:Bash matching `git push` | Force rebase + tests before push | Hard deny |
| `post-push-merge-notify.sh` | PostToolUse:Bash matching `git push` | File `for-chuck` bead with overlap analysis | Side effect (bd create) |

**Bead quality hook upgrade**: when this pattern is active, `bead-description-gate.sh` (existing SABLE hook) should be upgraded from warning to blocking. Rolling execution depends on bead descriptions reliably naming files. See [`hooks/multi-manager/upgrade-notes.md`](hooks/multi-manager/upgrade-notes.md).

---

## Setup

### Prerequisites

1. SABLE Foundation + Hierarchy + Swarm already operational
2. `bd` CLI installed and `.beads/` initialized in the target repo
3. The six existing SABLE hooks installed and registered in `~/.claude/settings.json`
4. Bead descriptions reliably naming files (verify with `bd lint`)

### Step 1: Install registry and roles

```bash
mkdir -p ~/.claude/sable/roles
cp templates/multi-manager/agents.yaml ~/.claude/sable/agents.yaml
cp templates/multi-manager/roles/*.md ~/.claude/sable/roles/
```

Edit `~/.claude/sable/agents.yaml` to match your agent set (rename, add, remove as needed).

### Step 2: Install hooks

```bash
mkdir -p ~/.claude/hooks/multi-manager
cp hooks/multi-manager/*.sh ~/.claude/hooks/multi-manager/
chmod +x ~/.claude/hooks/multi-manager/*.sh
```

### Step 3: Install slash command

```bash
mkdir -p ~/.claude/commands
cp templates/multi-manager/commands/inbox.md ~/.claude/commands/inbox.md
```

### Step 4: Register hooks in `settings.json`

Add to your existing `~/.claude/settings.json` (do not replace — append to existing arrays). See [`templates/multi-manager/settings-snippet.json`](templates/multi-manager/settings-snippet.json) for the exact JSON to merge.

### Step 5: Configure base branch and test subset (per repo)

```bash
# In each repo's project CLAUDE.md or shell wrapper
export SABLE_BASE_BRANCH=origin/dev           # or origin/main
export SABLE_TEST_COMMAND="npm test -- --changed --run"   # fast subset, see §Coordination mechanism 5
export SABLE_PRE_PUSH_TEST_TIMEOUT=60         # seconds, pair with settings.json outer timeout
```

Defaults: `SABLE_BASE_BRANCH=origin/main`, `SABLE_TEST_COMMAND` auto-detected from project files (`npm test` / `pytest` / `cargo test` / `go test ./...`), `SABLE_PRE_PUSH_TEST_TIMEOUT=60`.

If you need a longer test budget, remember to raise `"timeout"` in settings.json to match — see Coordination mechanism 5 for the coupling rule.

### Step 6: Add aliases

```bash
# In ~/.zshrc
alias optimus='CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager claude'
alias tarzan='CLAUDE_AGENT_NAME=tarzan CLAUDE_AGENT_ROLE=manager claude'
alias chuck='CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager claude'
```

### Step 7: Verify

In separate terminals:

```bash
optimus    # session opens; SessionStart hook should inject Optimus role
```

Inside that session:

```
> /inbox
```

Should return Optimus's addressed beads (or "Inbox empty" if none). Try `bd ready -l for-tarzan` — should be denied by the read guard.

---

## Operating discipline

### Manager loop (rolling)

```
1. Check /inbox at the top of each cycle (auto-injected on every bash call anyway)
2. Resolve any urgent coord beads (P0)
3. Pick next bead from claim_filter pool: bd ready <claim_filter> --no-label for-*
4. Pre-dispatch hooks fire automatically (refresh, claim, overlap, preempt)
5. Dispatch worker via Agent tool
6. While worker runs: plan next dispatch, handle returned worker if any
7. When a worker returns:
   a. Review their output
   b. git push triggers pre-push hook (rebase + test)
   c. Successful push triggers post-push hook (file for-chuck)
8. Repeat
```

### Chuck loop (continuous polling)

Chuck runs on a tight loop (`/loop 3m`) or fully continuous via the `loop` skill. On each tick:

```
1. Check /inbox for new for-chuck beads
2. For each PR-ready bead:
   a. gh pr view; gh pr checks
   b. If conflict: classify (mechanical vs semantic)
      - Mechanical: rebase + push
      - Semantic: file for-<author> bead with conflict context
   c. If green and no overlapping in-flight PR: merge
   d. If overlapping in-flight PR: hold, wait for sequencing
3. Close completed for-chuck beads
```

### When stepping away (AFK)

Tell each active manager:

```
> I'm AFK for 30min. If any P0 coord beads land, defer them with bd defer <id> --reason="user AFK 30min" so dispatches don't block. I'll resolve when back.
```

Managers comply via `bd defer`. Dispatch resumes; deferred beads stay visible for resolution on return.

---

## Adding agents

To add a new manager (e.g. a Researcher):

1. Add entry to `~/.claude/sable/agents.yaml`:
   ```yaml
   researcher:
     type: investigator
     scope: "Codebase exploration, design proposals, spike investigation"
     claim_filter: "--label=research"
     inbox_label: for-researcher
     role_prompt: roles/researcher.md
     dispatches_workers: true
   ```

2. Create `~/.claude/sable/roles/researcher.md` defining the role's responsibilities, scope, exclusions.

3. Add an alias:
   ```bash
   alias researcher='CLAUDE_AGENT_NAME=researcher CLAUDE_AGENT_ROLE=manager claude'
   ```

No hook code changes required. The hooks read the registry dynamically.

---

## Failure modes & known limitations

### Bead-quality dependency

This pattern's effectiveness scales directly with bead description quality. Specifically:
- Pre-dispatch claim writing requires file paths in descriptions
- Overlap detection relies on accurate claims
- Preemption requires coord beads being well-formed (P0 + correct label)

If bead quality slips, rolling degrades faster than batch would. The bead-description-gate hook becomes load-bearing — make it a hard block, not a nudge.

### Push-time rebase failures

The pre-push hook may fail when worker changes legitimately conflict with mainline drift. This is not a system bug — it's expected. The manager resolves manually during validation, then retries push. Frequency drops as bead scoping improves.

### Worker file-claim race window

WIP claims are written at dispatch time from bead descriptions. Files modified during work that weren't declared in the description are picked up by the Edit/Write reconciler — but there's a small window between dispatch and first edit where claims may be incomplete. Tolerable; the overlap hook will detect on subsequent dispatches.

### Cross-repo coordination

This pattern operates per-repo (per `.beads/` instance). Managers running across multiple repos do not see each other's beads. Out of scope for this version.

### Subagent contamination upper bound

Subagents inherit `CLAUDE_AGENT_NAME` from their parent's environment. The `agent_id` discriminator handles inbox injection and identity-aware hooks correctly. But a subagent that runs `bd ready -l for-optimus` would still get Optimus's inbox (the read guard treats them as Optimus). Mitigation lives in dispatch prompts: explicitly forbid inbox queries in worker prompts. Not a hard block.

### Chuck's loop overhead

Continuous Chuck polling consumes usage even when idle. Cost is bounded (idle polling is cheap), but worth monitoring. Tighten to 30-60s during heavy throughput, loosen to 5min during quiet periods.

---

## What this pattern explicitly does NOT include

These were considered and dropped during design:

- **Path ownership for epics** (mechanical block on writes outside owned paths) — too elaborate for the actual conflict surface; replaced by soft awareness via PR annotation
- **`for-user` inbox** — beads aren't an effective human medium per the methodology owner's explicit feedback
- **Custom MCP mailbox server** — premature; revisit if pull latency becomes painful
- **Tmux send-keys cross-terminal doorbell** — superseded by `agent_id`-aware inbox injection hook
- **Presence/AFK file infrastructure** — `bd defer` + verbal instruction sufficient
- **Batch execution mode** — fallback design; rolling is primary

---

## Promotion criteria

This pattern remains on the `personal-tooling` branch until:

1. Operated continuously for 4+ weeks without architectural revision
2. Demonstrated measurable conflict reduction in at least one real project
3. Documented at least one failure mode that the design correctly handled (proves the gates earn their slots)
4. Peer review by at least one other SABLE adopter at the Swarm stage

When promoted to main, this doc becomes a section in `SABLE.md` under "Section 6.X — Multi-Manager Coordination" or a sibling document referenced from the Adoption Path table.

Until then: this is Dylan's experimental tooling. Use accordingly.
