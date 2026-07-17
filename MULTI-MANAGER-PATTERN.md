# Multi-Manager Coordination Pattern

> **Status: experimental — `personal-tooling` branch only.** This pattern extends SABLE for high-throughput, multi-agent power-user workflows. Do not adopt until the prerequisites below are second nature.

A coordination pattern for running a roster of named agents in parallel against the same repository — three continuous **execution managers** (each commanding their own worker swarm), four session-scoped **planning agents**, and one execution-session **strategist** — with mechanical conflict prevention, addressed inter-agent messaging, and selective dispatch preemption.

The pattern's name still says "multi-manager" because the manager trio (Optimus / Tarzan / Chuck) is the load-bearing piece — the hooks key off `CLAUDE_AGENT_ROLE=manager` and only managers operate continuously. The other agents extend the same registry and identity infrastructure to cover bead-quality production (Sherlock), bead-pool freshness (Victor), end-to-end validation (Rudy), test-coverage scoping (Columbo), and strategic conversation during execution (Lincoln) without bolting on a parallel system.

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

## The agent set

This pattern is described in terms of a concrete eight-agent roster — adapt the names and scopes to your setup. The mechanism is the same. All eight live in a single `agents.yaml` registry; what differs is lifecycle and which hooks act on them.

### Tier 1 — Continuous execution managers (run during execution sessions)

| Agent | Type | Scope | Claims |
|-------|------|-------|--------|
| **Optimus** | epic_manager | Large feature epics, hardening, multi-bead sequences | Beads with a parent epic, or epics themselves |
| **Tarzan** | one_off_manager | Bugfixes, one-offs, doc updates, anything standalone | Orphan beads (no parent), regardless of priority |
| **Chuck** | integrator | Merge queue, PR review, conflict resolution | `for-chuck` beads only (does not claim from general pool) |

**Critical**: ownership is based on **work shape**, not priority. A P0 auth-breaking bug is Tarzan's territory if it's standalone. A P3 nice-to-have refactor is Optimus's territory if it's an epic. Priority signals urgency; structure signals ownership.

These three are the original "managers." They launch with `CLAUDE_AGENT_ROLE=manager`, so all the coordination hooks (inbox injection, pre-dispatch refresh/claim/overlap/preempt/model-check, pre-push gate, post-push notify) fire for them.

### Tier 2 — Session-scoped planning agents (run during planning sessions, not continuous)

| Agent | Type | Scope | Lifecycle |
|-------|------|-------|-----------|
| **Sherlock** | auditor | Read-only repo audit producing high-quality finding beads (design rot, redundancy, verbosity, dead code, test gaps) | User invokes with scope arg (`sherlock src/auth`); writes beads, self-reviews, addresses, exits |
| **Victor** | bead_validator | Validate open beads against current HEAD; update or close stale ones using differential validation | User invokes (`victor`, `victor --epic=…`); 5-run ramp-up before auto-closing |
| **Rudy** | quality_validator | End-to-end browser validation on the integration-branch dev deploy (Vercel preview + Supabase dev only) | User invokes (`rudy`, `rudy --feature=…`); files bug beads + a `rudy-report` at session end; refuses prod / PR-preview / local-dev targets |
| **Columbo** | test_planner | Interview-driven test-coverage planning. Forward mode produces `columbo-test-spec` beads + skeleton test files (`*.skel.test.<ext>`) for new feature work; audit mode produces `columbo-test-gap` finding beads against existing modules. | User invokes (`columbo --feature "<desc>"`, `columbo --bead SABLE-xxx`, `columbo --audit src/auth`); audit mode runs `bin/columbo-prefilter` first to triage; writes beads, self-reviews, exits |

These never run continuously. They are pure producers (Sherlock, Rudy, Columbo) or pool-maintainers (Victor) — the user kicks them off, they do their pass, they exit. Sherlock, Victor, and Columbo may dispatch read-only Explore subagents. Rudy runs browser interaction itself (browser sessions need state continuity).

The continuous-mode hooks are gated on `CLAUDE_AGENT_ROLE=manager`, so they no-op for these four. Planning agents rely on a different discipline: bead-template enforcement (`templates/sherlock-bead.md` for `sherlock-finding` labels and `templates/columbo-bead.md` for `columbo-test-spec` / `columbo-test-gap` labels are mechanically required via `bead-description-gate.sh`).

#### Victor's canonical execution shape — parallel validators over source-file clusters

A freshness pass is not a single-agent read-everything sweep. The validated shape (six validators over ~24 beads caught a deleted file, a moved component, off-by-N anchors, an already-shipped feature, and systemic enum laundering — with code quotes for each):

1. **Cluster** the open beads by the source file(s) they reference.
2. **Dispatch one read-only validator per cluster** (Explore subagents), in parallel.
3. Each validator returns a **structured per-bead verdict**:
   - `anchor_status`: VALID / DRIFTED / GONE
   - `gap_status`: REPRODUCES / FIXED / PARTIAL / CANT_TELL
   - `planning_bar`: MEETS / LAUNDERED_DECISION / UNDERSPECCED
   - `recommended_action`, plus **evidence as a file:line code quote**
4. Victor reconciles verdicts into bead updates/closes (subject to the auto-close ramp-up) and the `victor-report`.

Clustering by file means each validator reads its sources once and judges every bead against them — cheaper and more accurate than per-bead dispatch, and the structured verdict prevents free-form "looks fine" reports from slipping through.

### Tier 3 — Execution-session strategist (runs as peer, not orchestrator)

| Agent | Type | Scope | Lifecycle |
|-------|------|-------|-----------|
| **Lincoln** | strategist | Status reporting, strategic conversation, cross-manager brokering during execution sessions | The lead pane of the warm-pane tmux session (optimus, tarzan, and chuck run in their own panes). Three modes: Quick strategy, Arbitration, What's next. Managers reach it over `sable-msg`; `/inbox` remains the manual pull for durable `for-lincoln` beads. |

Lincoln is the agent the user **primarily talks to** during a working session. Optimus / Tarzan / Chuck are autonomous — they don't need conversation, they need beads. Lincoln gives status, brokers `for-lincoln` arbitration asks from the other three, and helps the user think strategically without becoming an orchestrator. Lincoln has `cross_inbox_read: true` (bypasses the read guard so it can give status across all managers) and may file `for-X` coord beads (one-line, not detailed specs).

Lincoln is forbidden from invoking Sherlock, Victor, Rudy, or Columbo — those are user-driven planning sessions, not Lincoln's tools.

### Why this shape?

Two execution managers (Optimus + Tarzan) plus an integrator (Chuck) is the minimum viable shape for execution separation of concerns:

- Without Tarzan, Optimus gets pulled into one-liner bugs that interrupt epic work
- Without Optimus, Tarzan tries to run epics they're not structured for
- Without Chuck, the human becomes the merge-queue bottleneck and the messenger between the other two

The planning trio + strategist were added because:

- **Sherlock** moves audit-style finding production out of the manager loop. Optimus and Tarzan execute — they shouldn't be writing audit beads in the same session they're shipping fixes.
- **Victor** prevents the bead pool from rotting. Without it, fixed bugs stay open and workers waste cycles on already-resolved issues.
- **Rudy** validates integrated state before promotion to prod. Worker-level unit + integration tests aren't enough — the dev environment needs a separate human-style sanity pass.
- **Columbo** lifts test-coverage scoping out of the manager loop. Workers execute TDD, but TDD given only a behavioral spec ships happy-path-only suites. Columbo writes the test contract — concrete cases plus skeleton files — that makes TDD produce robust suites instead of green-by-omission ones.
- **Lincoln** removes the human-as-orchestrator pressure during execution. Without Lincoln, the user is constantly re-deriving cross-manager state to give direction; with Lincoln, the user has a strategic conversation partner that already knows what's happening.

Adding more agents is supported via the registry pattern — see [Adding agents](#adding-agents).

### Quick reminder helper

`bin/sable-agents` reads the registry and prints a scannable summary of who does what. Use it for the "I forgot what victor does" moment:

```bash
sable-agents              # all agents, multi-line detail
sable-agents --list       # one-line each
sable-agents victor       # single agent + role file path
```

---

## The warm-pane topology (tmux)

SABLE's execution surface is **one tmux session, one warm `claude` pane per
role** — the only topology (SABLE-qa4d; designed in
[`TMUX-AGENTS-DESIGN.md`](TMUX-AGENTS-DESIGN.md)). The session starts
**Lincoln-only** (`sable-launch`, wrapping the `sable-tmux` layout tool —
mode-neutral: a planning session needs no fleet). When execution begins,
`sable-spawn-manager` stands up optimus/tarzan/chuck **on demand** — each in
its own detached window (the operator's Lincoln window is never disturbed),
with its identity (`CLAUDE_AGENT_NAME` / `CLAUDE_AGENT_ROLE=manager`) set at
launch, registered in the role→pane registry, and kicked into its operating
loop. The interlock gates manager spawning to execution mode.

**Lincoln** is the pane you talk to. **Optimus and Tarzan** are resident manager
panes: each drains its lane from `bd ready`, and each **dispatches its own
worker panes** — one ephemeral pane per bead via the worker-spawn helper
(worktree = pane CWD, model pinned from the bead's `model:` label, governance
checks inside the helper). **Workers self-push** their own worktree branches
from their own CWD and close their beads with gate evidence; managers never
push worker code. **Chuck** is the merge-queue pane — a manager's push notifies
him message-first (`sable-msg chuck`, sent by the post-push hook), with the
durable `for-chuck` bead as the fallback when his pane is unreachable.
Lead↔manager conversation is `sable-msg` with the `⟦SABLE-MSG⟧ from=<sender>`
framing header; the high-volume worker path is deliberately message-free
(workers are spawned with their instructions and report via the bead pool).

| Mode | Job | Mechanics |
|------|-----|-----------|
| **Planning** | fill & groom the pool via the staged substages (FRAMING → RESEARCH → ARCHITECTURE → TEST-STRATEGY → DECOMPOSITION) | Lincoln runs the substage machine; Tier-2 producers invoked on demand; interlock blocks execution dispatches until `substage=decomposition` |
| **Execution** | drain the bead pool | `sable-tmux --autostart` brings up the warm-pane session; managers dispatch a worker pane per bead, workers self-push, Chuck merges; Lincoln directs over `sable-msg` and oversees |

> **Planning has three modes (free entry).** The Planning row above is the **Full**
> mode (the high-rigor five-substage flow). Its siblings: **Quick** (telescopes the
> gate to a single approval for small, well-specified asks) and **Discovery**
> (Mode 1, `/sable-discover`) — the strategic, business-lens partner *upstream* of
> both that decides WHAT should exist across a candidate set and emits charters
> (not beads). A Full run started from a Discovery charter begins at RESEARCH. Full
> model: [`PLANNING-MODES-DESIGN.md`](PLANNING-MODES-DESIGN.md).

Planning is staged, not a single step. The five substages are:

- **FRAMING** (Lincoln strategist hat, live conversation)
- **RESEARCH** (Sherlock greenfield — findings → beads, exits)
- **ARCHITECTURE** (Gaudi `--epic` — locked review attached to the epic)
- **TEST-STRATEGY** (Columbo `--epic` — test-spec beads + skeleton files)
- **DECOMPOSITION** (Lincoln + Victor — finalize implementation backlog)

Human signs off before each `sable-mode substage advance`. The interlock blocks
the Lincoln session from populating the implementation backlog until
`substage=decomposition`. See [`MULTI-MANAGER-PATTERN.md`](MULTI-MANAGER-PATTERN.md) and the
`/sable-plan` skill.

Mechanics:

- **`bin/sable-mode`** — reads/writes the **per-repo** mode-state file
  `<repo>/.claude/sable/state/mode-state.json` (`{mode, since, fleet, substage}`),
  resolved from the git common-dir so a repo's worktrees share one mode (falls
  back to `~/.claude/sable/state/mode-state.json` outside a git repo;
  `SABLE_MODE_STATE` overrides). The source of truth shared by the skills and the
  interlock — scoped per-repo so concurrent SABLE sessions in different repos
  keep independent modes. `sable-mode substage get|set|advance` walks the
  planning substages; `sable-mode path` prints the resolved path.
- **`/sable-plan` and `/sable-execute`** (`skills/sable-plan`, `skills/sable-execute`) —
  flip the mode and swap Lincoln's persona.
- **`hooks/multi-manager/mode-interlock.sh`** — the mechanical guarantee.
  A `PreToolUse:Bash` guard that enforces the mode boundary (soft `--force` /
  `SABLE_ORCHESTRATION_FORCE=1` override). No-ops for non-Lincoln and subagent
  contexts. Registered first in the `Bash` matcher in `settings-snippet.json`.

---

## Identity & immutability

Each manager launches with an immutable identity established at the OS level, not in conversation context.

### Launching sessions (sable-launch + sable-view)

The session door is **`sable-launch`**: a tmux session holding only the
lincoln pane (identity set at the OS level, no aliases; existing sessions
reused), attached — mode-neutral. The fleet arrives later via
**`sable-spawn-manager`** (execution mode only), each manager in its own
hidden window. **`sable-view`** is the inspection tool: a status table of
every agent pane and worker window, `sable-view <role>` to deep-dive
(from a second terminal it attaches through a grouped session so the
Lincoln client's focus is untouched), `sable-view <role> --tail` to read one
without switching. `sable --help` prints the whole operator map.

For a **single role by hand** (a solo Lincoln session, or re-launching one
pane), the role form sets the identity + role so a session never launches
unnamed (the identity-bleed root cause, SABLE-njiv). With the repo `bin/` on
your PATH (see PERSONAL-TOOLING.md):

```bash
sable-launch lincoln  # the lead — one identity claude session, no tmux
sable-launch chuck    # the merge-queue role (normally a sable-tmux pane)
```

If you prefer raw shell aliases, the equivalent forms are:

```bash
# In ~/.zshrc or equivalent
alias lincoln='CLAUDE_AGENT_NAME=lincoln CLAUDE_AGENT_ROLE=manager claude'
alias chuck='CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager claude'
```

Planning agents are also still invokable directly
if you want to run a standalone session:

```bash
# Session-scoped planning agents (continuous-mode hooks no-op for these roles)
sherlock() { CLAUDE_AGENT_NAME=sherlock CLAUDE_AGENT_ROLE=auditor          claude "$@"; }
victor()   { CLAUDE_AGENT_NAME=victor   CLAUDE_AGENT_ROLE=bead_validator   claude "$@"; }
rudy()     { CLAUDE_AGENT_NAME=rudy     CLAUDE_AGENT_ROLE=quality_validator claude "$@"; }
columbo()  { CLAUDE_AGENT_NAME=columbo  CLAUDE_AGENT_ROLE=test_planner     claude "$@"; }
```

**Manual launch:** if you are running Optimus or Tarzan
by hand outside the tmux session (debugging a single lane, or a machine without
tmux), the raw aliases still work — they are exactly what `sable-tmux` sets per
pane:

```bash
alias optimus='CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager claude'
alias tarzan='CLAUDE_AGENT_NAME=tarzan CLAUDE_AGENT_ROLE=manager claude'
```

Hooks inherit the Claude Code process's environment. An agent running `export CLAUDE_AGENT_NAME=tarzan` inside a Bash tool only affects that subshell, which is discarded after the command. The env var is effectively immutable from the agent's perspective.

**On role values.** All continuous-mode hooks (`pre-dispatch-*`, `pre-push-rebase-test`, `post-push-merge-notify`) hard-exit when `CLAUDE_AGENT_ROLE != "manager"`. Every warm pane launches with `manager` (set by `sable-tmux`); Sherlock / Victor / Rudy / Columbo launch with their own roles so the continuous hooks no-op for them. Their discipline comes from session-scoped invocation patterns and bead-template enforcement, not from runtime hooks.

### Identity injection

A `SessionStart` hook reads `$CLAUDE_AGENT_NAME`, loads the corresponding role file from `~/.claude/sable/roles/<name>.md`, and injects it as the agent's identity context. A `PreCompact` hook re-injects after compaction (identity erodes silently otherwise).

### Dual identity mode

Two identity modes coexist:

| Mode | Context | Mechanism |
|------|---------|-----------|
| **Env-var** | Warm panes (lincoln, optimus, tarzan, chuck — launched by `sable-tmux`) and standalone sessions | `CLAUDE_AGENT_NAME` / `CLAUDE_AGENT_ROLE` set at launch; hooks read them from the process environment |
| **Ledger-based** | Producer subagents (Sherlock, Victor, Columbo) spawned via the `Agent` tool during planning | `agent_type` field in hook input JSON; the agent's named definition (`~/.claude/agents/<name>.md`) sets identity at spawn time |

The ledger-based mode is what `claude --agent-id` / the `Agent` tool uses when
spawning named agents from `~/.claude/agents/`. The env-var mode is how every
execution pane is identified — real sessions with real session IDs, which is
exactly what the warm-pane topology exists to guarantee.

In practice, the hooks use the `agent_id` discriminator to tell contexts apart:

```bash
AGENT_ID=$(jq -r '.agent_id // empty')
if [ -n "$AGENT_ID" ]; then
  exit 0  # subagent context — skip manager-specific behavior
fi
```

`agent_id` is **present in subagent contexts and absent in main-session contexts**.
This is the discriminator for all manager-specific hooks (pre-dispatch
refresh/claim/overlap/preempt, pre-push gate, post-push notify).
Without it, env-var-based identity would leak from a pane into the
subagents it spawns.

### Subagent context discrimination

Subagents dispatched via the `Agent` tool inherit the parent's environment, so `$CLAUDE_AGENT_NAME` propagates. Every relevant hook checks for `agent_id` as above. The ledger-based producers additionally carry their identity in the agent definition file injected at spawn — the env-var in the environment is their parent's (Lincoln's) identity, not theirs, so hooks must rely on the input JSON for correct discrimination.

---

## Registry

`~/.claude/sable/agents.yaml` is the single source of truth for agent properties. All hooks read it dynamically. Adding or modifying an agent requires no hook code changes. The canonical file is at [`templates/multi-manager/agents.yaml`](templates/multi-manager/agents.yaml) — copy it during install. The condensed shape:

```yaml
agents:
  # Tier 1 — continuous execution managers
  optimus:
    type: epic_manager
    claim_filter: "--has-parent"
    inbox_label: for-optimus
    dispatches_workers: true
  tarzan:
    type: one_off_manager
    claim_filter: "--no-parent"
    inbox_label: for-tarzan
    dispatches_workers: true
  chuck:
    type: integrator
    claim_filter: "for-chuck-only"
    inbox_label: for-chuck
    dispatches_workers: false
    fix_directly: [imports, lockfile, whitespace, non_overlapping_diffs, docs]
    delegate_to_author: [overlapping_logic, semantic_conflicts, test_divergence, config_changes]
    post_push_autoack: true

  # Tier 2 — session-scoped planning agents
  sherlock:
    type: auditor
    claim_filter: null              # pure producer, doesn't claim
    inbox_label: null               # no inbox; scope passed at invocation
    dispatches_workers: true        # read-only Explore subagents only
    bead_template: templates/sherlock-bead.md   # required for sherlock-finding labels
    quality_bar: above_default
  victor:
    type: bead_validator
    claim_filter: null              # operates over bd list, doesn't claim
    inbox_label: for-victor         # accepts freshness-pass requests from user/Lincoln
    dispatches_workers: true        # read-only Explore subagents only
    auto_close_ramp_up: 5           # first 5 runs label only; auto-close after
    validation_marker: victor-validated-at
  rudy:
    type: quality_validator
    claim_filter: null              # operates on user-invoked scope
    inbox_label: for-rudy
    dispatches_workers: false       # browser sessions need single-process state
    test_target:
      url_env: SABLE_RUDY_BASE_URL                  # Vercel preview for integration branch
      supabase_env: SABLE_RUDY_SUPABASE_URL
      branch_env: SABLE_RUDY_INTEGRATION_BRANCH
      forbidden_targets: [production, pr_preview, local_dev_server]
  columbo:
    type: test_planner
    claim_filter: null              # pure producer, conversation-driven
    inbox_label: for-columbo        # accepts --bead requests from managers
    dispatches_workers: true        # read-only Explore subagents only
    bead_template: templates/columbo-bead.md   # required for columbo-test-spec / columbo-test-gap
    quality_bar: above_default

  # Tier 3 — execution-session strategist
  lincoln:
    type: strategist
    claim_filter: null              # pure interlocutor
    inbox_label: for-lincoln
    dispatches_workers: true        # read-only Explore subagents only
    cross_inbox_read: true          # bypass read-guard to give cross-manager status
    files_addressed_beads: true     # may file for-X coord beads (one-line, not specs)
    forbidden_invocations: [sherlock, victor, rudy, columbo]
```

**Per-bead model selection.** Beads optionally carry a `model:<haiku|sonnet|opus>` label that drives worker model choice at dispatch time. The `pre-dispatch-model-check.sh` hook validates that the dispatch's model parameter matches the label (or that the prompt includes a `Model override: <reason>` line). See SABLE.md §6.9 for the ladder rules.

---

## Communication

### Label convention

| Label | Meaning |
|-------|---------|
| `for-optimus` | Addressed to Optimus's inbox (epic-track work, coord asks from O's lane) |
| `for-tarzan` | Addressed to Tarzan's inbox (orphan work, swarm-blocker P0s) |
| `for-chuck` | Addressed to Chuck's inbox (PR-ready notifications, conflict delegations) |
| `for-victor` | Bead-pool freshness-pass request (user or Lincoln files; Sherlock sometimes self-files) |
| `for-rudy` | E2E validation request on the integration-branch dev deploy (user-only origin) |
| `for-columbo` | Test-coverage scoping request — managers file these to ask Columbo to enrich an existing feature bead with child test beads |
| `for-lincoln` | Arbitration ask from Optimus / Tarzan / Chuck, or direction from user |
| `sherlock-finding` | Audit finding from Sherlock (mechanically validated against `templates/sherlock-bead.md`) |
| `sherlock:<category>` | Sub-category on Sherlock findings (`design-rot`, `redundancy`, `verbosity`, `dead-code`, `test-gap`) |
| `columbo-test-spec` | Forward-mode Columbo output — one bead per skeleton test file or coherent case cluster (mechanically validated against `templates/columbo-bead.md`) |
| `columbo-test-gap` | Audit-mode Columbo output — one bead per shallow-test gap, with fingerprint + cited test/source files (mechanically validated against `templates/columbo-bead.md`) |
| `columbo-test-spec:<category>` / `columbo-test-gap:<category>` | Sub-category mirroring the 12-category taxonomy in `roles/columbo.md` (`behavioral`, `boundary`, `negative`, `state-machine`, `failure-modes`, `concurrency`, `integration`, `regression`, `invariants`, `security`, `performance`, `observability`) |
| `rudy-report` | Per-session Rudy summary bead filed at end of validation pass |
| `victor-report` | Per-session Victor summary bead filed at end of freshness pass |
| `victor-suspects-stale` | Victor's pre-auto-close label during the 5-run ramp-up |
| `coord` | Umbrella label for all coordination traffic (filterable in one query) |

Sherlock has no inbox — it's a pure producer invoked synchronously with a scope arg. Findings it produces are addressed to `for-optimus` (epic candidates) or `for-tarzan` (standalone fixes) at the addressing pass, not to Sherlock itself.

Columbo's `for-columbo` inbox is the exception among planning agents: managers file `for-columbo` requests to enrich an existing feature bead with child test specs (e.g. Optimus shipping a new endpoint files a one-liner to ask Columbo to scope its tests in a separate session). Forward-mode invocations from the user (`columbo --feature` / `columbo --bead`) bypass the inbox entirely.

### When to address at creation

| Bead origin | Address at creation? | Why |
|-------------|----------------------|-----|
| Coordination (one agent → another) | **Always** | The whole point is "this is for you" |
| Discovered work with obvious fit | Optionally | Saves a routing step |
| General planning output | Usually not | Let managers claim from the pool via their `claim_filter` |

### `/inbox` slash command

Manual pull for managers who want to deliberately check their durable inbox. Backed by `bd ready -l for-$CLAUDE_AGENT_NAME`. Lives at `~/.claude/commands/inbox.md`.

### Live messaging (sable-msg)

Lead↔manager conversation is direct: `sable-msg <role> "<text>"` resolves the
target pane via the role→pane registry and injects the message as a turn,
prefixed with the `⟦SABLE-MSG⟧ from=<sender>` framing header (`--interrupt`
sends Escape first so it lands mid-turn). The old poll-based inbox-injection
hooks are gone — the `for-X` labels survive as the **durable fallback channel**
(e.g. `post-push-merge-notify` files a `for-chuck` bead when Chuck's pane is
unreachable), drained by each role's operating loop via `bd ready -l for-<self>`
and the `/inbox` manual pull.

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

When a manager dispatches a worker for bead-X, a `PreToolUse:Agent` hook reads the bead's description, extracts file paths (which the Fresh Agent Test requires), and pre-writes them to bead-X's `wip_claims` **metadata field** (a dedicated column, not notes — see below):

```
wip_claims: src/auth/foo.ts, tests/auth/foo.test.ts
```

Claims exist *before* the worker starts editing, closing the dispatch-time race condition.

**SABLE-szd:** claims live in metadata, not notes, deliberately. `bd update --notes` **overwrites** the whole notes field rather than appending, so any later notes write on the same bead — a manager's review-step note, a `[no-test]` annotation, anything — used to silently wipe the `WIP-CLAIMS:` line the dispatch hook had written, breaking overlap detection for that bead for the rest of its life. Metadata is a separate column `bd update --notes` never touches, so a claim survives regardless of what else updates notes afterward.

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

**Escape valve**: `bd defer <id>` removes the bead from the block list while keeping it visible. `bd defer` takes no `--reason` flag — record WHY first with `bd update <id> --notes "deferred: <reason> (existing notes preserved)"` (remember `--notes` overwrites; fetch-and-append), then defer. Use when the user is AFK and has explicitly told the agent to defer blockers.

### 5. Pre-push three-phase gate (rebase → static → tests)

A `PreToolUse:Bash` hook matching `git push` enforces three sequential phases:

```bash
# Phase 1: REBASE (always runs, never skippable)
git fetch origin
git rebase $SABLE_BASE_BRANCH

# Phase 2: STATIC (always runs, never skippable)
$SABLE_PRE_PUSH_TYPECHECK_COMMAND   # auto-detected if unset
$SABLE_PRE_PUSH_LINT_COMMAND        # only if explicitly set

# Phase 3: TESTS (skippable via SABLE_SKIP_PRE_PUSH=1 or PHASE=skip)
<test command>
```

Push is denied if any phase fails. Catches "my branch is behind main," typecheck regressions, lint errors, and unit-test breakage *locally* before exposing to CI.

**Critical: SABLE_SKIP_PRE_PUSH=1 only skips Phase 3 (tests).** Rebase and static analysis still run. This is a deliberate weakening of the bypass to prevent typecheck regressions from sneaking through to CI under the cover of "I'm bypassing for the date timebomb." If you need to bypass everything (true emergency, e.g. CI infra outage), disable the hook in `settings.json` explicitly, or use `git push --force` which short-circuits the hook entirely.

Static phase auto-detects:
- `tsconfig.json` present → `npx --no-install tsc --noEmit`
- `pyproject.toml` with `[tool.mypy]` → `mypy .`
- `Cargo.toml` → `cargo check --all-targets`
- `go.mod` → `go vet ./...`

Override via `SABLE_PRE_PUSH_TYPECHECK_COMMAND`. Lint is opt-in only (no auto-detect across linter ecosystems) — set `SABLE_PRE_PUSH_LINT_COMMAND` if you want a lint phase.

The hook has **two operating modes for Phase 3 (tests)**:

#### Mode: `auto` (default)

SABLE rebases, then runs tests. Fast subset recommended. Subject to the timeout-coupling rule (below).

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

#### Mode: `skip` (delegate to repo's own git hooks)

For repos that already have a real `.githooks/pre-push` (or `.git/hooks/pre-push`) that runs lint/test/build, duplicating test execution in SABLE's hook is waste — and opens the timeout-coupling footgun for no gain. Set `SABLE_PRE_PUSH_TEST_PHASE=skip` in the repo's project config:

```bash
# In the project's CLAUDE.md or shell wrapper:
export SABLE_PRE_PUSH_TEST_PHASE=skip
export SABLE_BASE_BRANCH=origin/dev
```

SABLE's hook then runs `git fetch && git rebase` only (completes in seconds; the outer hook timeout becomes irrelevant). Git's native pre-push hook fires AFTER SABLE's, so tests still run on the **rebased** state — you keep the "tests pass on what would actually merge" guarantee without duplicating the test command in two places.

When to pick which:

| Use `auto` when | Use `skip` when |
|-----------------|-----------------|
| Repo has no native pre-push hook | Repo has `.githooks/pre-push` already running lint+test+build |
| You want SABLE to define the test contract | Repo's own git tooling is the source of truth |
| Single-tool-chain project | Project already has pre-push infrastructure you don't want to re-litigate |

**Emergency bypass is for emergencies, not failing tests.** `SABLE_SKIP_PRE_PUSH=1` (and the repo's own bypass, e.g. `SKIP_PREPUSH=1`) exists for known-acceptable test failures (e.g. a date timebomb tracked under a named bead). It scopes to **Phase 3 only** — rebase and static analysis still run regardless. It is NOT permission to skip when pre-push complains. If pre-push fails for any reason not on your dispatch's "Known acceptable issues" list, STOP and report — do not bypass. Workers that bypass routinely cost more in CI fix-rounds than the pre-push budget saves.

**Typecheck is now structurally enforced via Phase 2** (since SABLE-cew). Workers can't ship typecheck regressions through bypass anymore — the static phase runs unconditionally. The dispatch template's typecheck advisory is now belt-and-braces with the hook gate; both layers reinforce the same outcome.

### 6. Tree claims (one main session per checkout)

**The ba5424d incident.** Two main sessions (Lincoln + a side session) shared the same working tree. The side session ran `git add` and `git commit` while Lincoln had staged resident-manager changes. The side session swept all staged content — including Lincoln's unrelated edits — into its gitignore commit. The commit message was misleading and the diff was wrong. Both sessions had write access to the shared git index with no coordination.

**The rule.** One main session may hold the index-claim on a given checkout at a time. If you need a second concurrent session, create an isolated worktree: `bd worktree create <name>`. Extra sessions in the same checkout must wait or take an explicit override.

**Mechanism.** A `PreToolUse:Bash` hook (`hooks/multi-manager/tree-claim.sh`) fires on every index-mutating git command:

```
git add, git commit, git rm, git mv, git restore --staged, git reset
```

Global git flags (`-C`, `-c k=v`, `--no-pager`, etc.) are tolerated. All other commands pass through immediately (non-mutating commands do not touch the claim).

The claim file lives at `$(git -C <cwd> rev-parse --git-dir)/sable-tree-claim` and contains `"session_id timestamp"`. Because `rev-parse --git-dir` resolves to the per-worktree gitdir for `git worktree add` worktrees, each checkout has an independent claim file — the main checkout and any worktrees never compete.

**Claim lifecycle (TTL default: 3600s, override `SABLE_TREE_CLAIM_TTL`):**

| State | Action |
|-------|--------|
| No claim file | Write claim for this session, allow |
| Own claim | Refresh timestamp, allow |
| Foreign claim, age < TTL | Deny — name the holder, the claim age, and both escape hatches |
| Foreign claim, age ≥ TTL | Take over (overwrite), allow + `additionalContext` noting the takeover |

**Escape hatches:**
1. `SABLE_TREE_CLAIM_OVERRIDE=1` — allow the command, take over the claim, emit `additionalContext` recording the override.
2. Delete the claim file manually (`rm $(git rev-parse --git-dir)/sable-tree-claim`) and retry.

**Fail-open guarantees.** The hook never denies when:
- The cwd is not inside a git repo (`git rev-parse` fails → exit 0).
- Session identity is unknowable (no `session_id` in JSON, no `CLAUDE_SESSION_ID` env) → allow + `additionalContext`.
- The claim file is unreadable or corrupt → take over + allow + `additionalContext`.

### 7. Post-push Chuck notification

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
| `tree-claim.sh` | PreToolUse:Bash | Lockfile: one main session per checkout — deny index-mutating git commands when another session holds a fresh claim (TTL 3600s; `SABLE_TREE_CLAIM_OVERRIDE=1` or manual delete to escape) | Hard deny |
| `read-guard.sh` | PreToolUse:Bash | Deny `bd ready -l for-<foreign>` queries (Lincoln bypassed via `cross_inbox_read: true`) | Hard deny |
| `pre-dispatch-refresh.sh` | PreToolUse:Agent | Rebase target worktree on `$SABLE_BASE_BRANCH` | Side effect (rebase) |
| `pre-dispatch-claim.sh` | PreToolUse:Agent | Read bead description, write file claims to bead notes | Side effect (bd update) |
| `pre-dispatch-overlap.sh` | PreToolUse:Agent | Annotate overlap with other in-progress beads | Inject context |
| `pre-dispatch-preempt.sh` | PreToolUse:Agent | Block dispatch if P0 coord bead in inbox | Hard deny |
| `pre-dispatch-model-check.sh` | PreToolUse:Agent | Enforce model ladder — bead `model:` label must match dispatch's model param, or prompt must include `Model override: <reason>` | Hard deny |
| `edit-write-claim-reconciler.sh` | PreToolUse:Edit\|Write | Append modified file to bead claims | Side effect (bd update) |
| `pre-push-rebase-test.sh` | PreToolUse:Bash matching `git push` | Force rebase + tests before push | Hard deny |
| `post-push-merge-notify.sh` | PostToolUse:Bash matching `git push` | File `for-chuck` bead with overlap analysis (Chuck's own pushes are skipped) | Side effect (bd create) |

Every continuous-mode hook (everything except `session-role-anchor.sh` and `read-guard.sh`) hard-exits when `CLAUDE_AGENT_ROLE != "manager"`, so they no-op in Sherlock / Victor / Rudy / Columbo sessions. (The former poll-based `inbox-injection` hooks were deleted with the tmux-only cutover — live messaging is `sable-msg`; the durable `for-X` labels remain the fallback channel.)

**Bead quality hook**: `bead-description-gate.sh` (existing SABLE hook) is now mode-aware. When `CLAUDE_AGENT_NAME` is set or `CLAUDE_AGENT_ROLE=manager` (i.e. a multi-manager session), the hook hard-blocks (denies) `bd create` if the description is missing required content. Outside that context (single-agent SABLE), it nudges via `additionalContext`. Rolling execution depends on bead descriptions reliably naming files; the manager-context hard-block is the structural answer to bead-quality drift.

**Sherlock-finding label enforcement**: when `bd create` includes `--labels=sherlock-finding`, the hook additionally requires the sections from `templates/sherlock-bead.md`: `## Rationale`, an Evidence block with at least one `Fingerprint:` line, `## Proposed approach`, `## Scope estimate`, `## Risk if not addressed`. Sherlock commits to this contract in its role file; the hook makes it mechanical. See `hooks/test/test-bead-description-gate.sh` for the full behavior matrix.

**Columbo label enforcement**: the same hook also recognizes `columbo-test-spec` (forward-mode test specs) and `columbo-test-gap` (audit-mode gap findings) labels and enforces the per-template required sections from `templates/columbo-bead.md`. Spec beads must include `## Feature under test`, `## Test file`, `## Cases` with at least one `Why:` sub-line per case, `## Categories`, `## Fixtures / setup`, and `## Out of scope`. Gap beads must include `## Symptom`, `## Cited test file`, `## Cited source file`, `## Fingerprint`, `## Cases to add`, `## Categories`, and `## Risk if not addressed`. Both labels compose with sibling labels (e.g. `model:sonnet`, `for-tarzan`) without interference — see `hooks/test/test-bead-description-gate.sh` for the cross-label cases.

---

## Setup

### Prerequisites

1. SABLE Foundation + Hierarchy + Swarm already operational
2. `bd` CLI installed and `.beads/` initialized in the target repo
3. The six existing SABLE hooks installed and registered in `~/.claude/settings.json`
4. Bead descriptions reliably naming files (verify with `bd lint`)

### Step 1: Install agent definitions

```bash
# install.sh does this automatically — run it instead of copying by hand.
# Manual copy (same idempotent logic):
mkdir -p ~/.claude/agents
for name in columbo rudy sherlock victor; do
    cp templates/agents/${name}.md ~/.claude/agents/${name}.md
done
```

The named agent definitions in `~/.claude/agents/` are the identity source for
the planning **producers** only. The execution roles (lincoln, optimus, tarzan,
chuck) are warm panes identified by `CLAUDE_AGENT_NAME` + their role files —
they have no agent definitions. Non-SABLE agent files in `~/.claude/agents/`
are preserved — the installer only writes the four producers by name.

**Note:** edit the role source files in `templates/multi-manager/roles/` and
re-run `bin/sable-build-agents` (then `install.sh`) to propagate changes. Hand-
edits to `~/.claude/agents/` are overwritten on the next install.

### Step 2: Install registry and roles

```bash
mkdir -p ~/.claude/sable/roles
cp templates/multi-manager/agents.yaml ~/.claude/sable/agents.yaml
cp templates/multi-manager/roles/*.md ~/.claude/sable/roles/
```

Edit `~/.claude/sable/agents.yaml` to match your agent set (rename, add, remove as needed).

### Step 4: Install hooks

```bash
mkdir -p ~/.claude/hooks/multi-manager
cp hooks/multi-manager/*.sh ~/.claude/hooks/multi-manager/
chmod +x ~/.claude/hooks/multi-manager/*.sh
```

### Step 5: Install slash command

```bash
mkdir -p ~/.claude/commands
cp templates/multi-manager/commands/inbox.md ~/.claude/commands/inbox.md
```

### Step 6: Register hooks in `settings.json`

Add to your existing `~/.claude/settings.json` (do not replace — append to existing arrays). See [`templates/multi-manager/settings-snippet.json`](templates/multi-manager/settings-snippet.json) for the exact JSON to merge.

### Step 7: Configure base branch and test phase (per repo)

```bash
# In each repo's project CLAUDE.md or shell wrapper

# Option A — SABLE owns the test gate (auto mode):
export SABLE_BASE_BRANCH=origin/dev           # or origin/main
export SABLE_TEST_COMMAND="npm test -- --changed --run"   # fast subset; see §Coordination mechanism 5
export SABLE_PRE_PUSH_TEST_TIMEOUT=60         # seconds, pair with settings.json outer timeout

# Option B — Repo's native git hook owns the test gate (skip mode):
export SABLE_BASE_BRANCH=origin/dev
export SABLE_PRE_PUSH_TEST_PHASE=skip         # SABLE rebases only; .githooks/pre-push runs tests
```

Defaults: `SABLE_BASE_BRANCH=origin/main`, `SABLE_PRE_PUSH_TEST_PHASE=auto`, `SABLE_TEST_COMMAND` auto-detected from project files (`npm test` / `pytest` / `cargo test` / `go test ./...`), `SABLE_PRE_PUSH_TEST_TIMEOUT=60`.

If you need a longer test budget in auto mode, remember to raise `"timeout"` in settings.json to match — see Coordination mechanism 5 for the coupling rule. Or switch to skip mode if your repo already has a native pre-push hook.

### Step 8: Add aliases (optional)

**An execution session needs no aliases** — `sable-tmux --autostart` launches
every pane with its identity set. The aliases below are for launching a single
role by hand (see [Launching sessions](#launching-sessions-sable-tmux--sable-launch));
add planning agent functions when you start running planning sessions.

```bash
# In ~/.zshrc — optional manual-launch forms:
alias lincoln='CLAUDE_AGENT_NAME=lincoln CLAUDE_AGENT_ROLE=manager claude'
alias chuck='CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager claude'

# Add when you start running planning sessions:
sherlock() { CLAUDE_AGENT_NAME=sherlock CLAUDE_AGENT_ROLE=auditor          claude "$@"; }
victor()   { CLAUDE_AGENT_NAME=victor   CLAUDE_AGENT_ROLE=bead_validator   claude "$@"; }
rudy()     { CLAUDE_AGENT_NAME=rudy     CLAUDE_AGENT_ROLE=quality_validator claude "$@"; }
columbo()  { CLAUDE_AGENT_NAME=columbo  CLAUDE_AGENT_ROLE=test_planner     claude "$@"; }
```

### Step 9: Verify

In your Lincoln terminal:

```bash
lincoln    # session opens; SessionStart hook should inject Lincoln role
```

Inside that session:

```
> /inbox
```

Should return Lincoln's addressed beads (or "Inbox empty" if none). Try `bd ready -l for-tarzan` — should be denied by the read guard (Lincoln does not have cross-inbox read for Tarzan).

Confirm that the agent definitions installed correctly:

```bash
ls ~/.claude/agents/   # should list columbo.md rudy.md sherlock.md victor.md (producers only)
```

`sable-agents <name>` should print details for any registered agent — useful for spot-checking that the registry was copied to `~/.claude/sable/agents.yaml` correctly.

---

## Operating discipline

### Manager loop (rolling)

```
1. Read any ⟦SABLE-MSG⟧ direction and drain /inbox (bd ready -l for-<self>)
2. Resolve any urgent coord beads (P0)
3. Pick next bead from claim_filter pool: bd ready <claim_filter> --no-label for-*
4. Verify the bead against HEAD, claim it
5. Dispatch a worker pane via the worker-spawn helper — it creates the
   worktree, pins the model from the bead's model: label, runs the
   governance checks, and delivers the canonical worker-dispatch template
   (templates/worker-dispatch.md). All required slots; no shortcuts.
6. While workers run: plan the next dispatch; poll sable-worker-status
7. When a worker finishes (bead closed, @sable_status=done):
   a. Review the result via the bead pool + the pushed branch
   b. The worker already self-pushed; its push notified Chuck message-first
      (durable for-chuck bead as fallback)
   c. Reap done panes: sable-worker-status --reap
8. Repeat; stand down when the pool and the inbox are empty
```

**The dispatch prompt is load-bearing.** Ad-hoc prompts drift mid-session
(early dispatches say one thing, later ones say another), and workers absorb
the inconsistency. The worker-spawn helper delivers the canonical template at
[`templates/worker-dispatch.md`](templates/worker-dispatch.md) for every
dispatch. The slots aren't decorative — each one prevents a specific failure
mode documented from real sessions.

### Reaching an idle manager

A warm pane that has gone idle is still a live session: `sable-msg <role>
"<text>"` lands as its next turn the moment it is free (verified through the
type-ahead queue), and `--interrupt` lands it mid-turn. There is no polling gap
to engineer around — the durable `for-<self>` beads are drained at the top of
each loop cycle and via the `/inbox` manual pull.

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
> I'm AFK for 30min. If any P0 coord beads land, note the reason (bd update <id> --notes "deferred: user AFK 30min") and defer them (bd defer <id>) so dispatches don't block. I'll resolve when back.
```

Managers comply via `bd defer`. Dispatch resumes; deferred beads stay visible for resolution on return.

### Tarzan's emergency mode (P0 swarm-blockers)

Tarzan's default mode is "claim orphan bead → dispatch worker → review → push." For most one-offs, dispatch is right.

**Exception: when an orphan bead is actively blocking 2+ other managers' dispatches** (e.g. a date-timebomb test breaking pre-push for everyone, a CI infra outage, a corrupt lockfile in main), dispatch overhead is the wrong tradeoff. Worker spin-up + pre-push round-trip + Chuck handoff easily costs 10+ minutes of compounded blocked-manager time. Tarzan handles these directly:

1. Claim the bead (`bd update <id> --claim`)
2. Edit + test + push from Tarzan's main session — **no `Agent` dispatch**
3. Notify the user + other managers via the fix-shipped coord bead

**Trigger conditions** (any one is sufficient):
- 2+ in-progress beads have WIP-CLAIMS on files this bead would touch
- Another manager files a P0 `for-tarzan` coord bead saying "I'm blocked, can't dispatch"
- Pre-push is failing repo-wide on the manager's own attempts

**Why not dispatch:** for non-emergency one-offs, dispatch is right (parallelism, isolation, hooks fire correctly). For swarm-wide blockers, latency is the dominant variable — every minute of dispatch overhead is N minutes of multi-manager blocked time. Tarzan's session is already the right scope.

**Why not Optimus:** role purity. Optimus claims by `--has-parent` (epic children). A swarm-blocker is structurally an orphan — Tarzan's lane.

### Verify before batch-closing on user merge cues

When the user signals "Chuck merged X" or "those PRs are in," verify each PR's state via `gh pr view <num> --json state,mergedAt -q '.state + " " + (.mergedAt // "")'` before `bd close`. User shorthand ("everything," "those," "the auth ones") is approximate; bead state is permanent. Cheap to verify, expensive to unwind a bead closed against an unmerged PR.

The Chuck loop already does this implicitly (it watches PR state directly). Other managers should mirror the discipline when triggered by user cues.

---

## Adding agents

To add a new agent — manager, planning-session, or anything else:

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

2. Create `~/.claude/sable/roles/researcher.md` defining the role's responsibilities, scope, exclusions. (Use the existing role files in `templates/multi-manager/roles/` as a shape reference.)

3. Add an alias or function. The role value determines whether continuous-mode hooks fire:
   ```bash
   # If the agent runs continuously and should get inbox injection / pre-dispatch
   # / pre-push hooks, use CLAUDE_AGENT_ROLE=manager:
   alias researcher='CLAUDE_AGENT_NAME=researcher CLAUDE_AGENT_ROLE=manager claude'

   # If session-scoped (Sherlock/Victor/Rudy/Columbo pattern), pick a role string that
   # is NOT "manager" so the continuous-mode hooks no-op:
   researcher() { CLAUDE_AGENT_NAME=researcher CLAUDE_AGENT_ROLE=investigator claude "$@"; }
   ```

No hook code changes required. The hooks read the registry dynamically.

If the new agent produces beads that should be mechanically validated against a template (Sherlock's pattern), add a `bead_template:` entry to its registry record and extend `hooks/bead-description-gate.sh` to recognize a new label gate. That's the only case that touches hook code.

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

### Worktree creation dirties parent and is cwd-sensitive

Two related `bd worktree create` issues bit hard in real sessions:

1. **cwd-sensitive placement.** When the manager's cwd has drifted into a subdirectory (`frontend/`, another worktree from rebase work), new worktrees nest there instead of landing at repo root. Mitigation: always `cd "$(git rev-parse --show-toplevel)"` before `bd worktree create`. Better fix lives upstream as a `bd worktree create --at <path>` flag or root-resolution.

2. **gitignore append dirties parent.** Each `bd worktree create` appends the new worktree path to the parent's `.gitignore`. The parent now has uncommitted changes, which makes the next pre-dispatch rebase hook noisy or fail. Mitigation: commit gitignore-only changes immediately after `bd worktree create`, or set up a manager-side helper that auto-stages gitignore-only diffs before the next dispatch.

Both are tracked as upstream `bd` improvements; until landed, the mitigations above are the workaround.

### Parallel incidental fixes (rare)

Two workers occasionally fix the same bug in parallel — one as a side effect of investigating an unrelated bead, another as the deliberate target of a sibling dispatch. Result: ~10 minutes of duplicated worker compute, one PR closed as superseded. Currently undefended; the cost is low enough that the per-dispatch overhead of a registry mechanism doesn't pay for itself. Promote to a real mechanism if observed 3+ times in a quarter.

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
- **Tmux send-keys cross-terminal doorbell** — dropped 2026-06-18 in favor of the `agent_id`-aware inbox injection hook, then **reversed 2026-06-25** after the injection-hook approach proved to be the bug-farm: send-keys was spike-tested for real and became `sable-msg`, the live lead↔manager channel (see `TMUX-AGENTS-DESIGN.md`)
- **Presence/AFK file infrastructure** — `bd defer` + verbal instruction sufficient
- **Batch execution mode** — fallback design; rolling is primary

---

## Promotion criteria

This pattern remains on the `personal-tooling` branch until:

1. Operated continuously for 4+ weeks without architectural revision
2. Demonstrated measurable conflict reduction in at least one real project
3. Documented at least one failure mode that the design correctly handled (proves the gates earn their slots)
4. Survived a deliberately contentious session (Optimus + Tarzan sharing an epic with cross-cutting children, race-prone bead-claim windows, intentional same-file edits) — observed where the scope filter actually breaks
5. Peer review by at least one other SABLE adopter at the Swarm stage

When promoted to main, this doc becomes a section in `SABLE.md` under "Section 6.X — Multi-Manager Coordination" or a sibling document referenced from the Adoption Path table.

Until then: this is Dylan's experimental tooling. Use accordingly.
