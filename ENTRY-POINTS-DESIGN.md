# SABLE Entry Points — Design (onboarding + small-ask planning)

> **Status: design — 2026-06-16.** Reference artifact for the two main
> interaction points into SABLE: **install** (how you get in) and **`/sable-plan`**
> (how you start a unit of work). Extends [`MULTI-MANAGER-PATTERN.md`](MULTI-MANAGER-PATTERN.md).
> The execution contract is the beads, not this file; this doc exists so a fresh
> agent understands *why* the entry points are shaped this way.
>
> **Reconciliation with v3 (`SABLE-d50`):** the install work here **supersedes**
> the inline-and-delete plan in `SABLE-ppy`/`SABLE-iw0` with a front-door +
> delegation model, and adds a de-cockpit rename those beads predate. Quick-tier
> planning is **net-new** — v3's NON-GOALS explicitly exclude planning-flow
> changes. See §4 for the precise supersession.

## Motivation

Two friction points were eroding adoption of SABLE's own discipline:

1. **Planning doesn't scale down.** `/plan` is a five-gate, human-in-the-loop
   pipeline (FRAMING → RESEARCH → ARCHITECTURE → TEST-STRATEGY → DECOMPOSITION)
   built for large epics. For a small ask — "resize these 3 login cards from L to
   XL" — the interview ceremony costs ten minutes to produce one bead, so users
   skip planning entirely. The cost was never the *bead* or the *tests*; it was
   the **interview**.
2. **The install path is unfinished and inconsistent.** Two installers
   (`install.sh --cockpit` and `bin/sable-cockpit-install`) with overlapping,
   half-duplicated logic; the Teams topology was never an install-time choice
   (just two runtime env vars); and "cockpit" is dead Zellij-era naming that no
   longer describes a one-window Lincoln chat.

## 1. De-cockpit rename

"cockpit" was the operator's Zellij control *pane*. With the one-window topology
there is no pane to pilot, so the term is removed entirely. The naming rule is
two-fold:

- **Remove "cockpit" from every name.**
- **Add the `sable-` prefix only to user-facing names** (skills, invokable
  binaries, flags, env vars). Internal names just lose "cockpit" — no prefix.

This completes an already-established namespace (`sable-review`, `sable-mode`,
`sable-note`, `sable-build-agents`, …); "cockpit" and the architectural
"multi-manager" token were the inconsistencies. "multi-manager" survives only as
*prose describing the architecture*, never as a name in a command/flag/path.

| Now | → | Note |
|---|---|---|
| `skills/cockpit-plan` → `/plan` | `skills/sable-plan` → `/sable-plan` | user-facing; also disambiguates from generic plan/execute skills |
| `skills/cockpit-execute` → `/execute` | `skills/sable-execute` → `/sable-execute` | user-facing |
| `--cockpit` flag | `--orchestration` | user-facing |
| `SABLE_COCKPIT` + `SABLE_MULTI_MANAGER` | one var `SABLE_ORCHESTRATION` (on/off) | user-facing |
| `cockpit-mode-interlock.sh` | `mode-interlock.sh` | internal — no `sable-` |
| `bin/sable-cockpit-install` | `bin/sable-orchestration-install` | user-facing; kept as a delegate (see §2) |
| `bin/sable-cockpit`, `bin/sable-status`, `sable.kdl` | **deleted** | dead Zellij surface (also `test_sable_status.py`, `test-cockpit-layout.sh`, `test-sable-status.sh`) |
| `COCKPIT-DESIGN.md` | folded into `MULTI-MANAGER-PATTERN.md` | — |
| `multi-manager/` dirs, other hook files, internal test names | unchanged (de-cockpit only) | internal |

The rename **sequences first** — it touches the same files the v3 execution-topology
beads edit in place, so it must land before them to avoid stale paths and
collisions.

## 2. Install pathing — one front door, delegation

`install.sh` is **the** front door. Interactive when run on a TTY; flag-driven for
CI and idempotent re-runs (`--orchestration --teams`, `--orchestration --subagent`,
`--scope user|project`).

It walks a small decision tree, with **newcomer-friendly copy** (no Lincoln, no
command names, no beads jargon — those are introduced *after* install):

- **Tier — Foundation or Orchestration?**
  - *Foundation* — "A disciplined workflow for AI-assisted coding. Every task
    becomes a tracked issue, code changes require tests, and quality checks run
    automatically. It works inside your normal coding sessions — nothing new to
    launch or learn."
  - *Orchestration* — "Everything in Foundation, plus a hands-off multi-agent
    mode. A lead session breaks work into a plan and delegates it to specialized
    agents that write, review, and merge code with minimal supervision — you steer
    strategy, they handle execution. Best for larger efforts you'd rather delegate
    than drive step by step."
- **If Orchestration — Subagent or Teams?**
  - *Subagent (default)* — managers run as background subagents in the lead
    session. Stable, no experimental flags.
  - *Teams (experimental)* — managers are persistent Agent-Teams members
    coordinating via live messaging; more parallelism. Requires
    `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`.
- **Scope** — User (`~/.claude`) / Project (`./.claude`).

Whichever topology is chosen, the installer configures it **end-to-end** — the
right agent defs (`agents-teams/` only for Teams), the right settings snippet
(Teams omits the inbox/precompact/read-guard poll hooks), the env vars set — and
**prints the one thing it can't safely auto-write**: the `settings.json`
experimental-flag line for Teams, with an "add this + restart" note. That collapses
today's four unguided manual steps into one choice.

**Structure: delegation, not inline.** `install.sh` owns the choice UX and the
Foundation tier; for the Orchestration layer it **delegates to
`sable-orchestration-install`** (the renamed `sable-cockpit-install`), passing the
resolved topology + scope. This keeps that script's project-scope install, its
uninstall path, and its 35 tested assertions intact, and leaves it callable
directly for advanced use. The front door is the documented path; the delegate is
the engine.

The Orchestration layer remains an **optional tier** (consistent with the shipped
`SABLE-106` `--cockpit`/`SABLE_MULTI_MANAGER` tier split) — Foundation adopters get
SABLE's discipline without the managed workflow.

## 3. Quick-tier planning — self-sizing `/sable-plan`

`/sable-plan` self-sizes. Lincoln reads the ask, **proposes** a tier ("this looks
quick — quick or full?"), the human confirms. Lincoln recommends; the human is
never locked in.

- **Full tier:** unchanged — the existing five-gate pipeline.
- **Quick tier:** Lincoln leads one lightweight pass and the specialists keep their
  lanes; what's dropped is the *interview ceremony*, not the deliverables.

| Substage | Owner | Quick-tier behavior |
|---|---|---|
| FRAMING | **Lincoln** | One strategic line — this is his lane |
| RESEARCH | sherlock | **Skipped** when no unknowns (Lincoln's triage call) |
| ARCHITECTURE | gaudi | **Skipped** when no new contracts |
| TEST-STRATEGY | **columbo** (quick mode) | Non-interview; **extends existing test files** by default; emits the unit+integration delta |
| DECOMPOSITION | **Lincoln** | Authors the bead(s), folds in columbo's test spec |

**Gates: full = 5 sign-offs; quick = 1** — a single consolidated review (frame +
test spec + the beads), approve once → Tarzan. A pure docs/config ask takes the
existing `[no-test]` path so columbo isn't invoked when there's no code change.

**Safety rail:** if a quick plan hits a real unknown or architecture fork
mid-flight, Lincoln surfaces "this needs real framing — bump to full?" — quick→full
is an explicit one-way upgrade, never a silent downgrade of rigor.

What this requires building:
- **columbo gains a quick mode** — non-interview, one-shot, extend-existing-tests
  bias. Centralized in the role file, so both topologies (subagent spawn / Teams
  SendMessage) inherit it. Runs inline as the `/columbo` skill in quick mode (like
  gaudi already runs inline), not a background spawn — that's where the
  spawn-latency saving comes from.
- **`/sable-plan` + `mode-interlock.sh` become tier-aware** — quick telescopes the
  substage machine to the single gate; the interlock recognizes the quick lane.

Because Lincoln authors the bead with columbo's spec folded in, the quick-tier bead
is **complete at hand-off** (file paths + unit+integration test spec) and passes the
`bead-description-gate` and `tdd-gate` exactly as a full-tier bead would.

## 4. Reconciliation with v3 (`SABLE-d50`)

The v3 epic predates both the Teams tier and this session. Precise supersession:

- **`SABLE-ppy` / `SABLE-iw0` (install consolidation)** — **superseded.** v3 planned
  to *inline* the cockpit payload into `install.sh` and *deprecate-then-delete*
  `sable-cockpit-install`, on the premise that the Lincoln layer "IS the topology,
  not optional." This session reverses that: **front door + delegation**, the layer
  stays an **optional tier**, the second installer is **kept and renamed** (not
  deleted). The Zellij deletion, doc sweep, and CC version-floor warning from
  `SABLE-ppy` survive unchanged. (Rationale: `SABLE-106` already shipped the layer
  as an optional tier, so ppy's non-optional premise was already stale; and the
  Teams tier makes a configurable installer more valuable, not less.)
- **De-cockpit rename** — **new**, sequenced first; the v3 in-place edit beads
  (`SABLE-9s8` cockpit-execute, `SABLE-4k7` cockpit-mode-interlock, `SABLE-86n`
  docs, and the `test-cockpit-*.sh` references) must use the post-rename paths.
- **Quick-tier planning** — **new epic.** v3 NON-GOALS exclude planning-flow and
  Tier-2 producer changes, so this does not touch the v3 tree.
- **npm packaging** — **deferred** (traction-gated). GitHub clone + `install.sh` is
  the near-term distribution path; npm becomes a thin `npx sable` wrapper over the
  same channel-agnostic front door if/when external adoption warrants it.

## Open risks

- **Quick-tier misclassification.** Lincoln proposes the tier; a too-big ask waved
  through as quick is caught by the single confirmation gate and the quick→full
  escalation rail. Acceptable.
- **columbo quick-mode boundary.** "Extend existing tests" is a *soft* default — if
  nothing covers the area, columbo still authors a new test. The role file must say
  so explicitly or quick mode silently under-tests greenfield code.
- **Rename ↔ v3 sequencing.** If the rename and the v3 in-place edits race, paths go
  stale. The rename is a hard predecessor to the v3 file-edit beads.
- **Teams experimental-flag step.** The one manual `settings.json` edit remains
  (we won't auto-clobber `settings.json`); the installer must print it
  unmistakably or Teams installs silently no-op.
