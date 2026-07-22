# tmux-native SABLE — Design (2026-06-25)

**Status: CANONICAL — the only execution topology (2026-07-06).** Built under
EPIC `SABLE-bldh` on branch `tmux-agents`; made the sole topology under epic
`SABLE-qa4d` on branch `tmux-only` (the nested-subagent and agent-teams
topologies, their installers, selection machinery, and poll-based inbox hooks
were removed). The original design text follows.

This replaces the in-process, subagent-heavy manager methodology (v2 one-window /
v3 nested / teams) with a **warm-pane substrate**: every role is a real,
persistent `claude` session in its own tmux pane, with a stable env-var identity.
It is the **vanilla 3-terminal flow generalized to N panes** — the configuration
that ran reliably for ~3 months — with the fragile in-process coordination layer
removed entirely.

---

## Why (the diagnosis this reverses)

The bug-farm was **not** "custom infra." Vanilla SABLE (Optimus / Tarzan / Chuck
as three plain `claude` terminals) was the most effective version and ran
everything for months. The regression came specifically from collapsing those
real sessions into an **in-process subagent/agent-team manager methodology**,
which introduced: identity bleed, inbox-keying misses, the `git -C <worktree>`
CWD bug (SABLE-041), non-persistent managers, teams post-push-notify failures.

History note: `MULTI-MANAGER-PATTERN.md:934` records that **tmux send-keys was
considered and dropped** on 2026-06-18 "superseded by `agent_id`-aware inbox
injection hook" — and that hook is exactly what broke. This design reverses that
decision, after actually testing it (see Spike evidence below).

The companion finding (`GASCITY-EVAL-HANDOFF.md`): Gas City's batch engine has a
~22–27 min **engine-level per-item floor** (cold session per step + heavy
ceremony). Warm panes have no such floor — workers stay resident. tmux-native
keeps gc's good ideas (durable bead substrate, gates-compose, model ladder,
planning/exec split, parallel drain) without the cold-start tax.

---

## Topology

One tmux session, **one warm `claude` pane per role**, each launched via
`sable-launch <role>` (sets `CLAUDE_AGENT_NAME` — the vanilla identity mechanism).

```
┌─ lincoln (lead/cockpit) ───┬─ optimus (epic mgr) ──────┐
│ operator talks here;       │ plans/bundles epic beads; │
│ messages mgrs via sable-msg│ spawns worker panes       │
├────────────────────────────┼───────────────────────────┤
│ tarzan (one-off mgr)       │ chuck (merge queue)       │
│ spawns worker panes        │ merges worktree branches  │
├────────────────────────────┴───────────────────────────┤
│ worker panes (ephemeral, per-bead) — each cwd = its own │
│ worktree; claude --model <ladder>; TDD → gates → push   │
└──────────────────────────────────────────────────────────┘
```

- **Lincoln** — the main pane the operator addresses. Plans (planning mode),
  oversees (execution mode). Talks to managers via `sable-msg`. Unchanged: mode
  machinery (`sable-mode`, interlock), planning substages.
- **Optimus / Tarzan** — manager panes (resident, warm). Plan + bundle beads,
  **spawn a worker per bead** via `sable-spawn-worker`, watch bead status for
  results. They do **not** push worker code (workers self-push).
- **Workers** — ephemeral panes, **spawned per-bead by a manager** with the full
  dispatch instruction set (the familiar subagent-spawn contract, unchanged in
  content). Each runs in its own worktree (= pane CWD). Does TDD, passes gates,
  **pushes its own worktree branch**, closes its bead, signals done.
- **Chuck** — merge-queue pane. Merges pushed worktree branches **as today**
  (mechanical conflicts fixed in place; semantic conflicts → for-author beads).

**Coordination substrate = the bead pool + tmux.** The bead pool carries work
state and worker results; tmux manages processes and carries the low-volume
lead↔manager conversation. Gates compose via real `~/.claude` hooks on real
sessions — unchanged.

---

## Messaging (`sable-msg`) — lead ↔ manager only

Messaging is **scoped to Lincoln ↔ Optimus/Tarzan** — low-volume, human-paced,
conversational direction ("drop the auth epic, API is urgent now"). The
high-volume worker path is deliberately message-free (workers are spawned with
their instructions, then report via the bead pool), which is what keeps the
coordination bug class out.

`sable-msg <to-role> "<text>" [--from <role>] [--interrupt]`:
- Resolves `<to-role>` → tmux pane via the **role→pane registry**.
- `send-keys -l` the body (literal; handles quoting), then `Enter` to submit —
  **the message is the turn**. No inbox, no injection hook.
- `--interrupt` sends `Escape` first, so the message lands *now* instead of
  queueing behind the recipient's current turn (the bead inbox could never do
  this).

### Sender-framing protocol (so Lincoln is 100% sure who is speaking)

Every `sable-msg` injection prepends a fixed, unmistakable header line:

```
⟦SABLE-MSG⟧ from=optimus to=lincoln
<body…>
```

Role-file rule (Lincoln + managers): **any turn whose first line is
`⟦SABLE-MSG⟧ from=<x>` is a message from agent `<x>`; any other input is from the
operator (the human).** This removes all ambiguity between operator / Optimus /
Tarzan. (Local trust model — spoof-resistance is not a concern; the header is a
disambiguator, not a security boundary.)

The registry (role→pane map) is written by `sable-tmux` at session launch and
read by `sable-msg` and `sable-spawn-worker`. Mechanism candidate: tmux pane
titles / user-options (queryable) or a small `~/.claude/sable/tmux-panes.json`.

---

## Worker dispatch (`sable-spawn-worker`) — manager-invoked

Replaces the manager's Agent-tool spawn. Manager calls, per bead bundle:

1. `bd worktree create wk-<name>` (from repo root) — the worker's CWD.
2. Open a new tmux pane/window running `claude --model <ladder>` with CWD = the
   worktree. **The model ladder pins cleanly here** (`--model haiku|sonnet|opus`)
   — solving gc's unresolved `opt_model`-didn't-propagate catch for free.
3. `send-keys` the dispatch prompt (the canonical `worker-dispatch` template,
   gate mode) into the new pane and submit.
4. Register the worker pane.

**Governance moves into the helper.** The pre-dispatch checks that used to fire
as `PreToolUse:Agent` hooks (refresh / claim / overlap / preempt / model-check)
and the `mode-interlock` become checks the helper runs *before* spawning — a
cleaner home than a hook on an opaque tool call.

**Result channel = the bead pool.** Worker pushes its own worktree branch, closes
its bead (with gate evidence), and signals done. The manager watches bead status
(`bd show`) — not a subagent return value, not a pane scrape. `capture-pane` is a
fallback for debugging a stuck worker.

---

## What this deletes

| Broke in the in-process methodology | Gone here, because… |
|---|---|
| Identity bleed (SendMessage sender mis-derived) | each pane = its own process with its own `CLAUDE_AGENT_NAME` |
| Inbox-keying / foreign-inbox leaks | no inbox injection hook; lead↔mgr = direct send-keys turn |
| `git -C <worktree>` validates wrong tree (SABLE-041) | worker pushes from its own CWD (= its worktree) |
| tdd-evidence session-keying misses (gc SABLE-tfkv) | real sessions → real session IDs |
| gc cold-session-per-step ~25-min floor | panes are warm and persistent |
| model ladder didn't propagate (gc opt_model catch) | `claude --model <x>` at pane launch |

---

## Spike evidence (verified this session)

The dropped tmux path was never actually tested. It was, this session:

1. **send-keys mechanics (bash REPL):** message injected + `Enter` submits; a
   second message sent *during* a 3s busy command **queued and ran the instant
   the target was free** — nothing dropped.
2. **Decisive test (real `claude` TUI in a tmux pane):** two messages sent, the
   second fired ~1.5s in **while Claude was still mid-turn** (turn took ~2s) —
   both answered, **in order**. Claude Code's raw-mode TUI queues type-ahead.

Conclusion: the Lincoln→busy-manager path works with nothing dropped, and
`--interrupt` (Escape-first) covers the "land now" case. Feasibility: confirmed.

---

## What stays unchanged (substrate-agnostic)

- The mode machinery: `sable-mode`, the planning substage state machine, the
  `mode-interlock` (re-homed from Agent-leg to the spawn helper / Bash leg).
- The gates: `tdd-gate` / `tdd-evidence`, `bead-description-gate`, scope-creep,
  test-evidence — they fire on real `~/.claude` hooks, which warm panes are.
- Chuck's merge-queue role and the `for-chuck` handoff.
- The bead pool as "the plan"; the Fresh Agent Test; unit+integration mandate.
- The model ladder *policy* (Sonnet default; down to Haiku / up to Opus) — only
  the *enforcement point* moves (into `sable-spawn-worker`). Note what "pins
  cleanly here" (§ above) does and does not mean: the helper PINS the model on
  the pane, it does not GRADE the bead. Applying the ladder stays a manager
  judgment expressed as `--model` or a `model:` label; absent both, the helper
  uses a flat default and announces it as such (SABLE-mn1da), and stamps what
  actually launched onto the bead afterwards (SABLE-qw9jv).

---

## Open questions / risks (carry into execution)

1. **Registry mechanism** — tmux pane titles/user-options vs a JSON file. Pick in
   the registry bead; both consumers (sable-msg, sable-spawn-worker) depend on it.
2. **Manager→Lincoln reply UX** — a reply injects a turn into the pane the
   operator is typing in. Acceptable (operator sees it inline) but confirm it
   isn't disruptive; the sender header makes provenance clear regardless.
3. **Worker pane reaping** — close on bead-close vs keep for post-mortem; cap on
   concurrent worker panes (pre-warm vs spawn-on-demand).
4. **Long mid-turn queue durability** — verified through seconds; a worker pane
   isn't messaged so this only affects lead↔manager. If a manager is in a
   multi-minute turn, `--interrupt` is the lever.
5. **Stall/liveness detection** — reuse `tripwire-watcher.py` via `capture-pane`
   to flag idle/hung worker panes.
6. **Multi-line / special-char messages** — `send-keys -l` + newline escaping in
   the helper; keep lead↔manager messages single-paragraph by convention.

---

## Operator runbook — live walk-away acceptance (SABLE-bldh.7)

The plumbing is automated-tested end-to-end (`hooks/test/test-tmux-e2e.sh`:
sable-tmux → sable-msg → sable-spawn-worker → sable-worker-status --reap, all
with stand-in panes). The **live** walk-away — real `claude` workers doing TDD,
self-pushing, Chuck merging — is the operator's acceptance run. To perform it:

1. **Plan a tiny backlog** in planning mode (`/sable-plan`): 2-3 trivial,
   independent beads with full Fresh-Agent-Test descriptions + unit/integration
   test specs, under a throwaway epic.
2. **Flip to execution** (`/sable-execute`) and bring up the session:
   ```bash
   sable-tmux            # lincoln + optimus + tarzan + chuck panes
   tmux attach -t sable
   ```
3. **From the optimus pane**, dispatch a bead:
   `sable-spawn-worker <bead-id> --scope <name>` — watch a worker window open,
   the model pin, and the worker run TDD → push its own branch → `bd close` →
   flag `@sable_status=done`.
4. **From the lincoln pane**, exercise messaging: `sable-msg optimus "status?"`
   (and `--interrupt` mid-turn). Confirm the `⟦SABLE-MSG⟧ from=lincoln` framing
   appears in optimus and that optimus replies via `sable-msg lincoln "..."`.
5. **Chuck** merges the `for-chuck` PR the worker's push filed.
6. **Reap**: `sable-worker-status --reap` clears done worker panes.

**Acceptance checks:** zero identity-bleed (every push/message carries the right
agent), zero `git -C` wrong-tree incidents (workers push from their own CWD),
gates fired on each worker's `bd close` (tdd/scope-creep/test-evidence), and
per-item wall-clock far below gc's ~22–27 min floor (warm panes; record the
number in SABLE-bldh.7).
