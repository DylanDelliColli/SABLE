# SABLE Quickstart

Get a fresh project running on SABLE in under 10 minutes. For the full methodology and rationale, see [SABLE.md](SABLE.md).

---

## What you're getting

After this guide, your coding agent (Claude Code, Codex CLI, or any tool that loads `~/.claude/CLAUDE.md`) will:

- Track every unit of work as a **bead** in a queryable issue database (no more lost context between sessions)
- Refuse to close beads without test evidence (mechanically — not just by convention)
- Require both unit AND integration tests for every code change
- Enforce description quality on every bead so the next amnesiac agent can pick up where the last one left off
- Coordinate parallel agents through `bd swarm` and `bd worktree` once you climb to that stage

This is opinionated. The whole point of the framework is that the rules are non-negotiable.

### Adoption ramp — the install is the same, the practice ramps over time

The full methodology (including swarm execution — it's in the name) is the destination. New adopters reach it through three stages:

| Stage | Practice | When to climb |
|-------|----------|---------------|
| **Foundation** | Beads + integration tests + hooks. One agent at a time, sequential work. | Day 1. Stay here until your bead descriptions consistently pass the Fresh Agent Test without thinking about it. |
| **Hierarchy** | Add: epics, child beads, dependencies, `bd preflight`. | When you start hitting multi-bead features that need ordering. |
| **Swarm** | Add: parallel agents via `bd swarm` and `bd worktree`. | When spec-writing is automatic and your API budget supports it. See SABLE.md §6. |

The install in this QUICKSTART is the same regardless of stage — all hooks, all bd commands, the full toolkit. **The ramp is about how you use it, not what you install.** New to agentic coding? Start sequential. Already comfortable? Climb fast.

---

## Prerequisites

1. **A coding agent** that loads `~/.claude/CLAUDE.md` on session start. Confirmed working: Claude Code (CLI, desktop, IDE extensions), Codex CLI. Other agents work if they support a global instructions file.
2. **bd (beads) installed.** Install from https://github.com/steveyegge/beads#installation. Verify with `bd version`.
3. **Dolt (bd's storage backend).** Install from https://docs.dolthub.com/introduction/installation. Required for `bd dolt push` (session-close protocol). The installer will warn if dolt is missing but won't block on it.
4. **git ≥ 2.5** (worktrees are used by swarm execution).
5. **bash + tmux** — the hooks are bash scripts and the execution layer runs
   every agent as a persistent `claude` session in a tmux pane:
   - **Linux:** native bash; `apt install tmux` (or your package manager).
   - **macOS:** native bash 3.2+; `brew install tmux`.
   - **Windows:** **WSL2 only.** tmux does not exist on native Windows;
     `install.ps1` is a guidance shim that points you into WSL.
6. **Optional but recommended:** Docker (for spinning up local databases for integration tests).

---

## Install in three steps

### Step 1 — Get the SABLE files

```bash
git clone https://github.com/<your-fork>/SABLE.git ~/sable
cd ~/sable
```

(Or: clone wherever you keep tooling. The path doesn't matter — the install script is path-agnostic.)

### Step 2 — Run the installer

**Linux, macOS, Windows (inside WSL):**
```bash
bash install.sh
```

There is **one install** — no tiers, no topology choices. It:
1. Verifies `bd` is on PATH (warns about missing dolt / tmux with install hints)
2. Links the `sable-*` CLI tools onto your PATH (`~/.local/bin`, symlinks)
3. Copies the base hook scripts into `~/.claude/hooks/`
4. Copies the producer agent definitions into `~/.claude/agents/`
5. Installs the orchestration layer (multi-manager hooks, `agents.yaml`
   registry, the four pane role files, the SABLE skills) and auto-merges its
   settings snippet (backed up; existing entries preserved)
6. Prepends the SABLE Prime Directives to `~/.claude/CLAUDE.md` (with a timestamped backup if one already exists)
7. Prints the base-hook JSON snippet you paste into `~/.claude/settings.json` (does NOT auto-edit that block — you review and paste)

Idempotent and safe to re-run. `bash install.sh --dry-run` reports exactly what
would be copied and writes nothing. On native Windows, `pwsh ./install.ps1`
prints the WSL instructions and exits.

If you'd rather do it manually, see [Manual install](#manual-install) below.

### Step 3 — Initialize beads in your project

```bash
cd /path/to/your/project
bd init
bd hooks install   # post-commit + post-checkout for graphify-style auto-rebuilds
```

That's it. Open a fresh agent session in this project and you're on SABLE.

---

## The bootstrap prompt

The first time you open your agent in a SABLE-enabled project, paste this exact prompt to ensure the agent reads the methodology and commits to following it:

```
Read /full/path/to/SABLE.md in full before responding.

After reading, confirm in one short message that you understand and will follow:
1. The Prime Directive: ALL work flows through beads, no exceptions, even tiny tasks.
   Use `bd q "<title>"` for fast capture. Never substitute TodoWrite or memory.
2. The Test Coverage Requirement: every code change requires BOTH unit AND
   integration tests. Mocking the database in integration tests defeats the purpose.
   Smoke tests are encouraged but not gated.
3. Issue Discovery: the moment you notice any bug, smell, or unexpected
   behavior — even tangential to the current task — log a bead immediately.
4. Adoption stage: I am at the [Foundation | Hierarchy | Swarm] stage today.
   Default to single-agent sequential work unless I explicitly ask for swarm
   execution OR I've told you I'm at the Swarm stage. The full methodology
   includes parallel agents (see SABLE.md §6) — that's the destination, not
   necessarily the starting point.

Then ask me what we're working on. Apply SABLE rigorously from the first bead onward.
```

Replace `/full/path/to/SABLE.md` with the absolute path where you cloned the repo (e.g. `~/sable/SABLE.md`).

**Why a bootstrap prompt rather than just relying on `~/.claude/CLAUDE.md`?** Two reasons:
1. CLAUDE.md only contains the headlines (Prime Directives + Quick Reference). The full rationale, hook architecture, and worked examples live in SABLE.md. The bootstrap prompt makes the agent load the full methodology.
2. Explicit confirmation creates accountability. An agent that wrote out "I will follow the Prime Directive" is measurably more compliant than one that just absorbed it passively.

You only need this prompt **once per project**. After the first session, the methodology is internalized for that workspace.

---

## Verify the install

Open a new agent session in your project and run these as a smoke test:

```bash
bd ready                           # Should run cleanly, return open work or "no ready issues"
bd q "test bead — delete me"       # Should print a new bead ID
bd close <id-from-above>           # Should be BLOCKED by tdd-gate.sh asking for tests
bd update <id> --notes "[no-test] smoke test bead"
bd close <id>                      # Should now succeed (escape hatch in action)
```

If `bd close` succeeded the first time without asking for tests, the hooks aren't firing — re-run `install.sh` and verify the JSON in `~/.claude/settings.json` matches.

---

## Climbing to orchestration (advanced)

The orchestration layer is **installed by default** — climbing is about
*practice*, not installing. It is the top rung of the
[adoption ramp](#adoption-ramp--the-install-is-the-same-the-practice-ramps-over-time):
the **tmux warm-pane topology**, where every role is a real, persistent `claude`
session in its own tmux pane — you talk to *Lincoln* (the lead pane), the
execution managers (*Optimus*, *Tarzan*) dispatch one ephemeral worker pane per
bead, workers self-push their own worktree branches, and *Chuck* (the merge-queue
pane) merges them. Start using it once your bead-writing is automatic and your
usage budget supports parallel agents (see SABLE.md §6).

The install already put in place:
- `~/.claude/hooks/multi-manager/*.sh` — the governance hooks (pre-dispatch
  refresh/claim/overlap/preempt/model-check, the mode interlock, the
  pre-push gate, post-push notify, identity discrimination).
- `~/.claude/sable/agents.yaml` — the agent registry (identities, lanes, inboxes).
- `~/.claude/sable/roles/` — the four warm-pane role files (lincoln, optimus,
  tarzan, chuck) that `session-role-anchor` injects into each pane.
- `~/.claude/skills/` — the SABLE slash commands (`/sable-plan`, `/sable-execute`,
  `/gaudi`, `/columbo`, `/audit-deep-dive`, `/sable-review`), installed by their
  skill name.
- The settings snippet, **merged into `~/.claude/settings.json`
  automatically** (backed up first; existing entries preserved).
- The producer agent definitions in `~/.claude/agents/`.

**Restart Claude Code** after installing so the agent definitions, slash
commands, and hook registrations load. Lost? `sable --help` prints the whole
operator map.

**Verify orchestration:**

```bash
ls ~/.claude/hooks/multi-manager/        # governance hooks present
head -1 ~/.claude/sable/agents.yaml      # registry present
sable-mode get                           # mode-state helper resolves (planning|execution)
```

The mode is **per-repo** — `sable-mode` resolves the state file from the repo you
are in (`<repo>/.claude/sable/state/mode-state.json`, shared across that repo's
worktrees; it falls back to `~/.claude/sable/state/mode-state.json` outside a git
repo, and `SABLE_MODE_STATE` overrides). So you can run **concurrent** SABLE
sessions in different repos — e.g. plan project B while project A executes — and
their modes never collide. (`sable-mode set` also adds the state dir to the
repo's `.gitignore`, so it never shows up as an untracked file.)

In a fresh session, `/sable-plan` walks the staged planning substages and `/sable-execute`
drains the pool via the warm-pane session (below). The full topology lives in
[`TMUX-AGENTS-DESIGN.md`](TMUX-AGENTS-DESIGN.md) and
[`MULTI-MANAGER-PATTERN.md`](MULTI-MANAGER-PATTERN.md).

**Three planning modes (free entry).** `/sable-discover` is **Mode 1** — a
strategic, business-lens partner that decides *what* to build across a set of
candidate features and emits a decision record + one charter per survivor (durable
markdown, not beads). `/sable-plan` is the specification half: **Full** (the five
gated substages) and **Quick** (small, well-specified asks), both producing an
executable backlog. Enter at any mode; a Full run started from a Discovery charter
skips straight to RESEARCH. See
[`PLANNING-MODES-DESIGN.md`](PLANNING-MODES-DESIGN.md).

### The warm-pane session (the only topology)

Two commands to remember:

```bash
sable-launch      # START: your session — a tmux session holding ONLY the
                  # lincoln pane, the agent you talk to. Mode-neutral: plan
                  # or execute from here; no managers yet.
sable-view        # DEEP-DIVE: inspect the agent panes — best from a SECOND
                  # terminal (it attaches via a grouped session, so your
                  # Lincoln view is never stolen)
```

Talk to Lincoln full-screen. When execution starts (`/sable-execute`), the
managers stand up **on demand** — `sable-spawn-manager --all` opens optimus,
tarzan, and chuck in their own *hidden windows*, kicked into their operating
loops; your Lincoln window stays exactly where it is. Mid-session:

```bash
sable-view                  # status table: every role pane + worker window
sable-view optimus          # deep-dive into a manager (second terminal)
sable-view worker --tail    # read a hidden worker window without switching
sable-msg optimus "status?" # message a pane (--interrupt to land mid-turn)
sable-worker-status --reap  # clear finished worker panes
sable --help                # the full operator map, any time
```

(`sable-launch` wraps the lower-level `sable-tmux` layout tool and attaches
with `tmux attach -t "$(sable-session)"`; `sable-launch lincoln` still launches
a single identity session by hand. The session name derives from the repo —
`sable-<repo-basename>` — so **each repo gets its own concurrent fleet** and
every tool run inside a repo addresses only that repo's panes; set
`SABLE_TMUX_SESSION` to override the name explicitly.)

Managers dispatch one worker pane per bead (worktree = pane CWD, model pinned
from the bead's `model:` label); workers do TDD, pass the gates, self-push, and
close their beads; Chuck merges, notified message-first with a durable
`for-chuck` bead as fallback. No experimental flags, no second terminal. The
full design is in [`TMUX-AGENTS-DESIGN.md`](TMUX-AGENTS-DESIGN.md).

---

## Day-1 workflow

Your first task on SABLE — at any stage — starts with the Foundation pattern:

1. **Plan** — think through the change. Identify the deliverable.
2. **Create the bead**: `bd create --title="..." --description="..." --type=bug|task|feature --priority=2`. The description must pass the Fresh Agent Test (file paths, function names, what's wrong, suggested approach, AND a test spec listing both unit and integration tests).
3. **Claim and work**: `bd update <id> --claim`. Write the failing tests first (red), then the implementation (green), then run both unit and integration tests.
4. **Close**: `bd close <id>`. The TDD gate hook fires here — if no tests ran this session, the close is blocked.
5. **Session close**: `bd preflight`, then `git push`, `bd dolt push`. Work is not done until pushed.

**Hierarchy stage** — add when features need 3+ beads with ordering:
- Create an epic: `bd create --type=epic --title="..." --description="..."`
- Create child beads: `bd create --parent=<epic-id> --title="..." --description="..."`
- Add dependencies with requirement language: `bd dep add <child-B> <child-A>` means "B needs A"
- Visualize: `bd dep tree <epic-id>` or `bd children <epic-id>`

**Swarm stage** — add when spec-writing is automatic and your usage budget supports parallel agents:
- Validate the structure: `bd swarm validate <epic-id>`. Fix warnings before dispatching.
- Create one worktree per worker: `bd worktree create worker-1`, `bd worktree create worker-2`
- Dispatch one agent per worktree
- Merge their branches sequentially after they close
- See SABLE.md §6 for the full pattern, gotchas, and coordination primitives (`bd gate`, `bd merge-slot`)

The SABLE.md "Getting Started" section (§10) walks through this in more detail with worked examples.

### Optional companion skills — pre-execution planning

SABLE ships with two companion skills that gate the planning phase of a bead or epic before workers start writing code. Both are interview-driven and produce beads (never source).

- **`/columbo`** — test-coverage planning. Drag boundary cases, failure modes, and regression-from-experience cases out of your head before TDD ships happy-path-only suites. Four modes: `--feature` / `--bead` / `--audit` / `--epic`.
- **`/gaudi`** — architecture review. Audit existing modules for named code smells (Fowler catalog) or gate the architectural shape of a planned epic before swarm workers dispatch. Pedagogical voice — explains tradeoffs and smells in plain language. Two modes in v1: `--audit` / `--epic`.

Recommended pattern: run `/gaudi --epic <id>` first to lock interfaces and named tradeoffs, then `/columbo --epic <id>` to lock the test contract, then dispatch workers. The two skills are independent and can be run in either order.

---

## Manual install

If you don't want to run `install.sh`, do these by hand.

### Copy hooks

```bash
mkdir -p ~/.claude/hooks
cp hooks/*.sh ~/.claude/hooks/
chmod +x ~/.claude/hooks/*.sh
```

### Register hooks in `~/.claude/settings.json`

Merge this `hooks` block into your existing `settings.json` (preserving any other config you have):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {"type": "command", "command": "bash /home/USER/.claude/hooks/tdd-evidence.sh", "timeout": 3000},
          {"type": "command", "command": "bash /home/USER/.claude/hooks/tdd-gate.sh", "timeout": 5000},
          {"type": "command", "command": "bash /home/USER/.claude/hooks/bead-description-gate.sh", "timeout": 3000}
        ]
      },
      {
        "matcher": "Edit|Write",
        "hooks": [
          {"type": "command", "command": "bash /home/USER/.claude/hooks/tdd-remind.sh", "timeout": 3000}
        ]
      },
      {
        "matcher": "Agent",
        "hooks": [
          {"type": "command", "command": "bash /home/USER/.claude/hooks/agent-tdd-enforce.sh", "timeout": 3000}
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {"type": "command", "command": "bash /home/USER/.claude/hooks/bead-quality.sh", "timeout": 5000}
        ]
      }
    ],
    "SessionStart": [
      {"matcher": "", "hooks": [{"type": "command", "command": "bd prime"}]}
    ],
    "PreCompact": [
      {"matcher": "", "hooks": [{"type": "command", "command": "bd prime"}]}
    ]
  }
}
```

Replace `USER` with your username.

### Add Prime Directives to `~/.claude/CLAUDE.md`

Copy the contents of `templates/global-CLAUDE-prime.md` (in this repo) to the top of your `~/.claude/CLAUDE.md`. If you don't have one, create it.

---

## Could this be an npm package?

Considered and rejected for now. SABLE is bash hooks + a JSON config snippet + the `bd` CLI (which has its own installer). An npm package would add a JavaScript dependency for what's essentially a `cp` and a `cat`. The shell installer in this repo (`install.sh`) is the right shape — pipeable, no runtime dependencies, transparent.

If interest grows, a few realistic distribution paths:
- `curl -fsSL https://sable.dev/install.sh | bash` (one-line install once we have a domain)
- A Homebrew formula (`brew install sable`) — natural for the bash + bd ecosystem
- A `npx create-sable-project` scaffold for fresh projects (would handle `bd init`, hooks, and project CLAUDE.md template all at once)

For now, clone-and-run is the simplest path and avoids supply-chain ambiguity.

---

## What to read next

- **[SABLE.md](SABLE.md)** — full methodology, rationale, and worked examples. Read top-to-bottom once; reference by section thereafter.
- **`bd prime`** — generated workflow context that SABLE injects on every session start. Re-read whenever you've forgotten a command.
- **`bd <command> --help`** — the actual reference for every bd command. SABLE.md covers the high-leverage ones; --help covers the rest.
