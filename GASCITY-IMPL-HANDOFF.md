# Gas City Implementation — Spike Handoff (2026-06-24)

> **PHASE 1 STATUS (updated 2026-06-24):** Build EPIC `SABLE-vj4x` is decomposed
> into an 8-bead backlog (`bd children SABLE-vj4x` for live state). Done + pushed:
> `.1` pack scaffold (canonical at `SABLE/pack/sable/`; durable checkout at
> `~/dev-environment/gascity-packs/` with `sable` a **symlink sibling** of `gascity`;
> gc resolves the `../gascity` import lexically through the symlink), `.2`
> test-evidence gate (unit+integration mandate, now mechanical), `.3` scope-creep
> gate (builds SABLE-bijh), `.4` `sable-build`+`sable-review` formulas (reuse `gc.*`
> base roles; two gate steps wired `review → sable-test-evidence → sable-scope-check
> → finalize`), `.5` stub `sable-planning` + office-hours agent. 31 pack tests pass
> (`python3 -m pytest pack/sable/tests/`). `.6` end-to-end is **structurally
> validated** — `sable-build` resolves fully (29 steps) in a real `gc-dev` city with
> the gates correctly wired — with only the **live multi-agent walk-away drain**
> remaining (see `.6` notes for the exact setup: roles import bound as `gc`, a rig,
> worker idle-drain tuning, autonomous-mode sling). Next: finish `.6` (watched live
> drain), then Phase 2 `.7` (the planning redesign), Phase 3 `.8` (full contract +
> register). Sections below are the original spike handoff (historical context).

**Branch:** `gascity-impl` (cut from `agent-teams`). This is the entry point for continuing the
Gas City build. Read this, then `bd show` the open beads listed below. The auto-memory
`active-work-gas-city-build` carries the same state in condensed form.

## Direction (why we're here)

SABLE is the user's **personal** best-possible agentic-coding workflow — not a product. Decision:
adopt **Gas City** (`gastownhall/gascity`, a Go orchestration SDK on upstream `steveyegge/beads`)
as the swappable execution **engine**; SABLE becomes a portable **opinion layer** (a Gas City
pack: agents + formulas + check-scripts + artifact schemas). The value is the opinion layer
(planning methodology + gates), **not** the engine — execution is commodity. Keep opinions
portable; even Gas City is rented. Never frame as competition/moats. North-star: the human lives
in PLANNING and walks away from headless EXECUTION (90%+ unattended). The Fresh-Agent-Test-grade
spec is the precondition for that hands-off swarm.

## Spike result: walk-away execution is PROVEN on this box ✅

End-to-end proof: a fresh dolt-provider city, with a from-source `gc` and a persistent focused
worker, autonomously created `hello.py`, verified it, and **closed its own bead** — entirely
outside the operator session. `gc rig add` (the original blocker) succeeded.

### The recipe — three levers required to make gc walk-away execution work here
1. **gc's compiled-in beads version MUST equal the system `bd`.** Brew ships them mismatched
   (gc 1.3.2 links `steveyegge/beads@v1.0.4`; system `bd` is 1.0.5) → the `version_compat` gate
   disables the native store and `gc rig add` hard-fails. FIX: build gc from source against
   beads 1.0.5 (below). The `file` beads provider is NOT a viable workaround — agents discover
   work via the `bd` CLI, which can't see the file store.
2. **`min_active_sessions = 1`** on the worker agent — otherwise the pool reconciler retires the
   worker at ~38s (idle drain) before it picks up the nudged task.
3. **A focused worker prompt** — otherwise the agent rabbit-holes (the default mayor/coordinator
   prompt burned 6.4k tokens inspecting the city instead of doing a one-line task).

### KEY FINDING (validates the whole thesis)
SABLE's **TDD gate** (from `~/.claude/settings.json`) FIRED on the gc worker's `bd close`; the
worker correctly used the `[no-test]` escape. **SABLE's mechanical gates compose with Gas City
workers for free** — workers are vanilla `claude` sessions that inherit `~/.claude` hooks
alongside gc's injected `.gc/settings.json`. So opinion-layer-on-Gas-City is de-risked: the
engine runs the swarm AND the gates port onto it with zero wiring.

## Environment / artifacts (durable)

- **Built gc (linked beads 1.0.5):** `/home/ddc/go/bin/gc-dev` (preserved; use by full path).
  Verify: `strings /home/ddc/go/bin/gc-dev | grep -o 'steveyegge/beads@v[0-9.]*'` → `v1.0.5`.
- Toolchain installed via brew: **Go 1.26.4**, **icu4c@78**. Go module cache (`~/go/pkg/mod`) is
  warm, so a rebuild is fast.
- `bd` 1.0.5, `dolt` 2.1.9, systemd user scope running, tmux/jq/flock present.
- `claude` CLI runs on the **subscription** (`claudeAiOauth`), NOT an API key. gc workers draw on
  the same subscription — fine, that's already how SABLE swarms; only the mechanism changes.
- **Clones** (in the prior session's scratchpad — MAY NOT PERSIST; re-clone if gone):
  `gastownhall/gascity` and `gastownhall/gascity-packs` (study the `gascity/` pack dir). The
  go.mod edit applied: `github.com/steveyegge/beads v1.0.4` → `v1.0.5`.

## Rebuild recipe (only if the binary is lost)
```
brew install go icu4c            # already installed
git clone https://github.com/gastownhall/gascity
# edit go.mod: steveyegge/beads v1.0.4 -> v1.0.5
env -C gascity \
  CGO_CPPFLAGS="-I/home/linuxbrew/.linuxbrew/opt/icu4c/include" \
  CGO_LDFLAGS="-L/home/linuxbrew/.linuxbrew/opt/icu4c/lib -Wl,-rpath,/home/linuxbrew/.linuxbrew/opt/icu4c/lib" \
  GOFLAGS=-mod=mod make build     # -> bin/gc
```

## How to run a city (reference, all with the gc-dev binary by full path)
1. `gc-dev init <city> --default-provider claude --skip-provider-readiness --no-start`
   (default = dolt provider; omit any `[beads] provider="file"`).
2. `gc-dev agent add --name worker` (run with `env -C <city>`), then write
   `agents/worker/agent.toml`:
   ```toml
   provider = "claude"
   min_active_sessions = 1
   sleep_after_idle = "off"
   default_sling_formula = "mol-do-work"
   ```
   and a focused `agents/worker/prompt.template.md` (do exactly the bead, no exploration).
3. `gc-dev start <city>` → `gc-dev rig add <git-repo> --city <city>` → 
   `gc-dev sling worker "task" --city <city>`. Watch via the per-city tmux socket
   (`tmux -L <cityname> capture-pane -t worker-... -p`) and `gc-dev bd show <id> --city <city>`.
- NOTE: gc refuses to overwrite the brew-installed systemd unit, so it runs the supervisor
  directly from whatever binary you invoke. For stable/persistent use, `make install` to a stable
  path + a proper systemd unit (follow-up, tracked in SABLE-8xad).

## Beads
- **CLOSED this session:** SABLE-s0fq (engine spike), SABLE-oc5w (build gc from source),
  SABLE-jrq7 (worker idle-drain).
- **OPEN — next phase:**
  - `SABLE-xk2s` — port Lincoln's opinion layer to the mayor: KEEP the strategist persona +
    status/arbitration/what-next shapes + no-code/oversee boundary; STRIP the dispatch mechanism
    (mode machinery, Agent-tool spawning, Chuck relay, worktrees — the engine owns those); ADD
    "the orchestrator dispatches; you converse/judge/arbitrate, never spawn/route." Source:
    `templates/multi-manager/roles/lincoln.md`. Operator-approved.
  - `SABLE-fh86` — strengthen planning-phase pushback/elicitation (THE value/gap). Engine-agnostic.
  - `SABLE-vj4x` — the build EPIC.
  - `SABLE-8xad` — beads version mismatch: worked around via from-source build, reported upstream
    (gastownhall/gascity#3632, commented as DylanDelliColli). Remaining: `make install` to a
    stable path; await the upstream formula pin.

## NEXT PHASE: build the opinion-layer pack
Author SABLE as a derived Gas City pack:
1. Mayor prompt = **Lincoln-minus-dispatch** (`SABLE-xk2s`).
2. **Override `planning-base`** (`gascity-packs/gascity/formulas/planning-base.formula.toml` —
   `internal = true`, explicitly designed to be overridden; already has `interaction_mode` /
   `review_mode` dials) with SABLE's adversarial, opinion-eliciting planning (`SABLE-fh86`) — the
   highest-leverage work. `build-basic-review.formula.toml` already implements acceptance /
   test-evidence / simplicity review lanes (SABLE's gate philosophy) — reuse/extend it.
3. Wire SABLE's gates as pack doctor-checks / formula check-scripts for portability (they also
   compose for free via `~/.claude`).
Validate against the benchmark: **"as effective as 3 vanilla terminals"** — favor the simplest
thing that works; treat custom orchestration code as a liability (memory
`simplicity-over-custom-orchestration`).
