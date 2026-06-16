# SABLE Agent-Teams Topology — Design (parallel mode)

> **Status: design, `agent-teams` branch.** A second coordination topology for
> SABLE built on Claude Code's experimental **Agent Teams** feature
> (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`: `TeamCreate`, `SendMessage`,
> persistent named members). Offered as an **opt-in parallel mode** alongside the
> existing nested-subagents topology — not a replacement. Extends
> [`COCKPIT-DESIGN.md`](COCKPIT-DESIGN.md) and
> [`MULTI-MANAGER-PATTERN.md`](MULTI-MANAGER-PATTERN.md). The execution contract
> is the beads, not this file; this doc explains *why* the topology is shaped the
> way it is so a fresh agent can act on the epic.

## Why this exists

SABLE's current ("nested-subagents") topology has agents that **never talk
directly**. The bead DB is the entire coordination substrate: managers dispatch
fire-and-forget background workers, and everything coordinates asynchronously
through labelled beads (`for-chuck`, `for-lincoln`) plus a dozen polling and
injection hooks. It is durable and auditable, but indirect — latency-bound
polling, amnesiac workers, and a large amount of hook machinery that exists
*specifically to simulate inter-agent communication that did not exist natively*
when SABLE was designed.

Agent Teams provides that missing primitive: persistent named members in one
team that **message each other directly** (`SendMessage`), go idle, and wake when
addressed, with the operator able to sit in the team and steer. This design
re-founds SABLE's coordination layer on that primitive — as a parallel mode, so
existing adopters are unaffected.

## Decisions of record

The design was settled through a brainstorming dialogue. The seven load-bearing
decisions:

1. **Core driver: live coordination.** Replace bead-DB-as-IPC + polling/injection
   hooks with real-time `SendMessage` between the managers and Chuck.
2. **Beads / Teams boundary: beads = durable ledger + log; Teams = live
   fast-path.** Beads stays the source of truth for the backlog *and* records the
   coordination events that must survive a session restart. `SendMessage` is the
   real-time channel; anything load-bearing for recovery is still written to
   beads.
3. **Topology: one team, managers + Chuck persistent, workers ephemeral, Chuck
   folds in.** Lincoln is the team lead (your session); Optimus, Tarzan, and
   Chuck are persistent message-driven members; workers stay fire-and-forget
   `Agent` subagents dispatched by the managers. Chuck stops polling and becomes a
   member who wakes on a handoff message — collapsing the two-window model to one.
4. **Mirror scope: minimal — only what would strand work.** PR→merge handoffs,
   merge results, claim/release, and backlog-mutating decisions are mirrored to
   beads. Status pings, arbitration chatter, and directives stay live-only and
   vanish on crash (harmless, re-derivable).
5. **Poll hooks: clean cutover.** In teams mode the three poll-based messaging
   hooks (`inbox-injection`, `inbox-injection-precompact`, `read-guard`) do not
   run; teams relies on wake-on-message + a one-shot startup catch-up. The
   no-missed-event assumption is validated in the spike.
6. **Member construction: build artifact + inline-spawn.** A second build target
   emits committed, test-enforced teams member definitions; `/execute` reads them
   and spawns members with inline prompts (no agent-name registration, so no
   collision with the nested defs).
7. **Rollout: parallel mode, spike-gated.** Teams is opt-in
   (`SABLE_TEAMS=1`); the existing topology stays the default. The first
   implementation bead is a validation spike that gates all build work.

## 1. The factoring (why parallel mode is cheap)

SABLE already separates **behaviour** from **invocation/transport**:
`templates/multi-manager/roles/<name>.md` is the single source of truth for each
agent's behaviour, and `bin/sable-build-agents` wraps each role file in
agent-definition frontmatter + an invocation preamble to generate
`templates/agents/<name>.md`. `hooks/test/test-agent-definitions.sh` enforces
*generated == committed*. Three layers, only the small middle one diverges per
mode:

```
┌─ BEHAVIOUR CORE  (shared, single source of truth) ────────────────┐
│  templates/multi-manager/roles/<name>.md                          │
│  Who the agent is, mandate, domain logic, decision rules,         │
│  verdicts. ~90% of every agent file. Edit once → both modes get   │
│  it on rebuild.                                                   │
└───────────────────────────────────────────────────────────────────┘
                 │  bin/sable-build-agents  (build step)
        ┌────────┴─────────┐
        ▼                  ▼
┌─ NESTED wrapper ──┐  ┌─ TEAMS wrapper ───┐   ← the ONLY per-mode divergence:
│ spawned via Agent │  │ persistent team   │     a coordination card per mode,
│ tool; hand off PR │  │ member named X;   │     shared across all agents.
│ via for-chuck     │  │ hand off PR via   │     Small. Intentionally different,
│ bead; inbox poll  │  │ SendMessage chuck │     not drift.
└───────────────────┘  └───────────────────┘
        │                  │
        ▼                  ▼
┌─ ORCHESTRATION (mode-specific, small) ────────────────────────────┐
│  Launch + who-spawns-whom + hook wiring.                          │
│  nested: Lincoln spawns resident subagents + Chuck terminal.      │
│  teams:  TeamCreate + spawn members; Chuck is a member.           │
│  Lives in the /execute skill + install, NOT in agent behaviour.   │
└───────────────────────────────────────────────────────────────────┘
```

A tweak to named-agent behaviour edits one role file → rebuild → both modes
updated, with the generated==committed test as the drift guard. **What does not
centralize** (all small, well-bounded, *intentional* divergence): the two
coordination cards; the launch/orchestration path in `/execute`; the hook split
(below); and identity binding (which actually *unifies*, below).

## 2. Topology & lifecycle

You talk to **Lincoln**, and Lincoln is the team lead (the orchestrator session,
as Opus is in the `code-council-teams` skill). On entering execution Lincoln runs
`TeamCreate("sable")` and spawns the persistent members:

```
┌─ One window = Lincoln (team lead) ───────────────────────────────┐
│  Persistent members (spawned once, then idle-until-messaged):    │
│    ● optimus — epic lane    ● tarzan — orphan lane               │
│    ● chuck   — merge queue  (a MEMBER now, no longer a terminal) │
│                                                                   │
│  Ephemeral, dispatched by a manager via the Agent tool:          │
│    ○ worker-N — writes code in a git worktree, stops before push │
│                 (UNCHANGED from nested — the proven part)        │
└───────────────────────────────────────────────────────────────────┘
```

**Idle/wake replaces polling.** Members go idle and wake on `SendMessage`.
Chuck's `/loop 3m /inbox` poll disappears: Optimus pushes, `SendMessage chuck`
"PR ready," Chuck wakes, merges, replies, idles. `for-lincoln` arbitration becomes
a direct message.

**The team is disposable; beads is the recovery point** (decision 2 paying off):

| Event | What survives | How you resume |
|---|---|---|
| Session ends / context limit | committed work in git+beads; claimed-but-unfinished beads stay claimed/open | re-run `/execute` → recreate team → managers re-scan `bd ready` + stale claims → resume |
| Crash mid-dispatch | durable-log bead ("dispatched worker for SABLE-x") | managers detect orphaned in-flight beads on restart, re-dispatch |
| Worker dies mid-edit | its bead is still open; worktree discardable | re-claimed next wave |

The team holds no irreplaceable state — kill it anytime, beads rebuilds it.

## 3. Coordination protocol (SendMessage verbs + durable mirror)

Abstract verbs live in the behaviour core (identical in both modes); the per-mode
coordination card binds them to mechanics.

| Verb | From → To | Teams (live) | Nested (today) | Durable mirror? |
|---|---|---|---|---|
| **CLAIM / RELEASE** | manager → ledger | `bd` (unchanged) | `bd` | always — it *is* ledger state |
| **DISPATCH worker** | manager → worker | Agent tool | Agent tool | optional log bead |
| **REVIEW verdict** | worker → manager | Agent return | Agent return | no (acted on at once) |
| **HANDOFF (PR→merge)** | manager → chuck | `SendMessage chuck` | `for-chuck` bead | **yes** |
| **MERGE result** | chuck → mgr/lincoln | `SendMessage` | bead | **yes** (state transition) |
| **ESCALATE** | manager → lincoln | `SendMessage lincoln` | `for-lincoln` bead | only the *resolution* |
| **STATUS** | lincoln ↔ managers | `SendMessage` | read beads | no (re-derivable) |
| **DIRECTIVE** | lincoln → member | `SendMessage` | bead/verbal | only if it changes priority |

Two tiers, governed by "mirror only what would strand work":

- **Tier A — live-only (ephemeral):** STATUS, ESCALATE conversation, DIRECTIVE,
  overlap chatter. Lost on crash; all re-derivable from beads.
- **Tier B — live + durable mirror:** PR→merge handoff, merge result,
  claim/release, backlog-mutating decisions. Written to beads so a recreated team
  resumes exactly.

**Durability is already automated.** Liveness = `SendMessage` (the per-mode card
says whom to message). Durability = the existing hooks keep firing:
`post-push-merge-notify.sh` still writes the `for-merge` bead on every push — now
the *durable half*, with the manager's `SendMessage chuck` as the *live half*.
`claim/release` is already beads-native. The continuous *poll* of
`inbox-injection` is replaced by wake-on-message **plus a one-shot beads catch-up**
in each member's startup card (a re-hydrated Chuck sweeps up `for-merge` beads
left by a dead session, then goes message-driven).

Trace:
```
optimus: worker pushed wk-SABLE-x → [hook] post-push writes for-merge bead (durable)
                                  → SendMessage chuck "PR wk-SABLE-x ready" (live)
chuck (idle→wake): merges → SendMessage optimus "merged, branch deleted"
                          → bead state flips to merged (durable)
  (if conflict): SendMessage optimus "rebase needed" → optimus re-dispatches
```

## 4. Hook suite split

The line: **guards stay hooks (transport-agnostic); only the poll-based
*messaging* hooks are replaced by `SendMessage`.** Of 15 `hooks/multi-manager/`
hooks, 12 are shared, 3 are nested-only, and teams adds **zero** new hooks (its
transport is the `SendMessage` tool + the startup-card catch-up).

| Hook | Role | Teams mode |
|---|---|---|
| `pre-push-rebase-test` | 3-phase gate (rebase→static→tests) | **shared, unchanged** |
| `cockpit-mode-interlock` | planning/execution gate | **shared** |
| `tree-claim` | one operator per checkout | **shared** (one team lead per checkout) |
| `edit-write-claim-reconciler` | worker WIP file claims | **shared** |
| `pre-dispatch-{claim,refresh,overlap,preempt,model-check}` | worker dispatch governance | **shared** |
| `session-role-anchor` | name → role-file injection | **shared, *more* central** |
| `lib-identity` | identity-resolution library | **shared** (+ teams-native-name branch) |
| `post-push-merge-notify` | writes `for-merge` bead on push | **shared** — the durability mirror |
| `inbox-injection` | continuous poll of `for-X` beads | **nested-only** → wake-on-message |
| `inbox-injection-precompact` | re-inject inbox on compaction | **nested-only** → team runtime handles |
| `read-guard` | block cross-inbox bead queries | **nested-leaning** → likely moot (validate in spike) |

The dispatch-governance and gate logic — the parts most likely to be tweaked over
time — are 100% shared, so changes benefit both modes automatically.

## 5. Identity binding

Teams *simplifies* identity. Members have real names natively, so the dual
ledger/env-var scheme collapses toward name-based resolution by the existing
`session-role-anchor` + `lib-identity` machinery.

| Who | Nested identity | Teams identity |
|---|---|---|
| **Lincoln** (team lead = your session) | env-var `CLAUDE_AGENT_NAME=lincoln` | **same** — env-var, unchanged |
| **optimus / tarzan** | ledger `agent_type` via `agent_id` | **native team-member name** from hook input |
| **chuck** | env-var (separate terminal) | **native team-member name** (now a member) |

**Build consequence:** `sable-build-agents` today excludes `chuck` (env-var
terminal), `lincoln` (lead), and `gaudi` (inline skill). In teams mode **chuck
flips to a built member definition** (his behaviour already lives in
`roles/chuck.md`); **lincoln stays excluded in both modes** (he is the lead,
never a spawned member).

## 6. Mode selection, build targets & install

**Mode selection — an env var**, matching SABLE's existing `SABLE_MULTI_MANAGER` /
`SABLE_COCKPIT` convention:

```
CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1   # Claude Code's flag (prerequisite)
SABLE_TEAMS=1                            # SABLE: use the teams topology
```

`/execute` reads `SABLE_TEAMS` and branches: set → `TeamCreate` + spawn members;
unset → today's resident-subagent path. This is **orthogonal** to
planning/execution mode (which stays in `cockpit-mode.json` + the interlock). If
`SABLE_TEAMS=1` but the experimental flag is missing, `/execute` errors with the
one-line fix.

**Build — one script, two targets, one source:**

```
roles/<name>.md ──┬── --mode nested ──→ templates/agents/<name>.md
 (single source)  │                     {optimus,tarzan,sherlock,victor,rudy,columbo}
                  │                     + nested preamble + nested coordination card
                  └── --mode teams  ──→ templates/agents-teams/<name>.md
                                        {…same six… + chuck}
                                        + teams preamble + teams coordination card
```

Both targets are test-enforced (`test-agent-definitions.sh`: generated==committed
for **both** modes) — the committed-artifact + diff-test is the anti-drift guard.

**Consumption:** nested spawns via Claude Code named-agent discovery
(`~/.claude/agents/`). Teams cannot register a second agent type named `optimus`,
so `/execute` **reads** the committed `templates/agents-teams/<name>.md` files and
spawns members with inline prompts.

**Install:** the cockpit install ships the teams artifacts (the teams coordination
card, `agents-teams/` member defs, and the role files needed for inline-spawn —
note today only `lincoln.md` is installed to `~/.claude/sable/roles/`; teams needs
the others present). Warn if `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` is unset.

## 7. Validation spike & testing strategy

Four assumptions are unproven on the experimental feature, so the **first
implementation bead is a spike that gates all build work** — the same way SABLE
de-risked nested-subagents (the original spike found nested-spawning unavailable).

**Spike — one scenario, all four unknowns:**
```
TeamCreate("sable-spike"); spawn optimus + chuck.
Lincoln → SendMessage optimus: "claim spike bead, dispatch a worker for a
          trivial edit, review, push."
  ► optimus spawns worker (Agent) in a worktree           [#1 member-spawns-worker]
  ► worker edits + stops-before-push; optimus pushes      [#1 pre-push hook fires
                                                            in a MEMBER's context?]
  ► post-push hook writes for-merge bead + optimus
    SendMessage chuck "PR ready"                          [durable mirror + live]
  ► chuck (idle) WAKES, merges, replies                   [#3 wake catches handoff]
  ► inspect hook-input JSON for optimus/chuck             [#4 member name present?]
Leave team idle, push a 2nd handoff → watch stability     [#2 long-lived team]
```
Output: a findings bead. If hooks do not fire in a member's context, the design
adapts (e.g. the push happens in the lead's context). Everything after depends on
this.

**Testing strategy (unit + integration both, per the Prime Directive):**

| Layer | Unit | Integration (real composition) |
|---|---|---|
| `sable-build-agents --mode teams` | composes fixture role+card → expected artifact | extend `test-agent-definitions.sh`: generated==committed for both targets |
| `lib-identity` teams branch | member-name hook-input JSON → resolves role | — |
| Shared guard hooks under teams identity | — | feed team-member-shaped hook-input to pre-push gate, pre-dispatch-*, tree-claim, session-role-anchor against a real `bd` test workspace; assert fire/no-op |
| Durable mirror | — | simulate push → assert `post-push-merge-notify` writes `for-merge` bead (real `bd`), under teams identity |
| `/execute` mode select | env-matrix (`SABLE_TEAMS` × flag) → right path / correct error | smoke: `/execute` in throwaway workspace spawns a team without error |

**Honest limitation (stated in each transport-dependent bead):** the
`SendMessage` transport and idle→wake **cannot** be deterministically
auto-integration-tested — they require the live experimental feature. We
unit+integration test everything around the transport; the transport itself is
validated by the spike + a documented manual scenario.

## Epic shape

```
EPIC: SABLE teams topology (parallel mode)
  └─ spike: validate Agent Teams assumptions            [gates all below]
       ├─ lib-identity teams-native-name branch
       ├─ teams coordination card (the shared SendMessage verb→mechanic map)
       ├─ sable-build-agents --mode teams  (needs the card)
       ├─ shared guard hooks teams-aware + omit poll hooks  (needs lib-identity)
       ├─ /execute teams branch + SABLE_TEAMS selection  (needs build + card + identity)
       ├─ durable mirror under teams + startup catch-up  (needs hooks + /execute)
       └─ install + docs ship the teams tier             (needs /execute)
```

## Open risks

- **Experimental-feature drift.** The whole mode depends on a flag-gated feature
  that can change. Parallel mode contains the blast radius (default topology
  unaffected); the spike validates assumptions before the build sinks cost.
- **Missed-event gap.** Clean cutover (decision 5) assumes wake-on-message never
  drops a handoff. If the spike disproves this, fall back to a low-frequency
  backstop poll in teams mode.
- **Externally-triggered events.** Any merge gated on an async external signal
  (e.g. remote CI) has no natural `SendMessage` trigger; the startup catch-up +
  the durable `for-merge` bead cover re-hydration, but a live external trigger
  (webhook/cron messaging Chuck) may be needed later.
