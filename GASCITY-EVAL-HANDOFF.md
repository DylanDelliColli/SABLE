# Gas City Evaluation — Findings & Next-Session Handoff (2026-06-25)

**Read this first.** It captures a long evaluation of building SABLE on Gas City
(`gc`), the performance problems we hit, the workarounds we found, and the
**open architectural decision** for the next session: *keep Gas City as the
engine, or build a custom wrapper that keeps gc's best ideas without its
execution-time overhead.* The operator is **leaning toward the custom wrapper.**

Branch: `gascity-impl`. Prior context doc: `GASCITY-IMPL-HANDOFF.md` (the Phase-1
build handoff; still accurate for the build, superseded by this for strategy).

---

## TL;DR

- We built **Phase 1 of a `sable` Gas City pack** (the "batch lane") — it works:
  resolves end-to-end, runs walk-away, gates compose. Shipped + pushed.
- We then **characterized gc's execution performance** and found a **structural
  ~22–27 min per-item floor** that is **engine-level, not our pack's fault**
  (a mature reference pack, gstack, is equally slow).
- The floor is **~90% tax** (cold-session-per-step + max-effort thinking +
  schema-artifact ceremony + reconcile overhead), **~10% actual coding.** The
  same trivial task is **~1–2 min in the warm 3-terminal/agent-teams flow** →
  gc is **~15–25× slower per item** on identical work.
- **Parallelism** (not warmth) is gc's throughput lever, and it only pays off
  for **large, independent, unattended batches**. gc's own docs confirm that's
  what it's *for*; small/interactive work is explicitly "the simplest case," not
  the design center.
- **Decision to make next session:** is the per-item floor acceptable (use gc as
  the batch engine, route small/interactive work to the warm lane), **or** do we
  build a **custom execution wrapper** that keeps gc's good ideas (durable bead
  substrate, gates-compose, formula graph, per-item model ladder, parallel
  unattended drain) but replaces the cold-session-per-step + heavy-ceremony
  execution layer with something leaner (warm persistent workers, light
  artifacts) to kill the floor. **Operator leans custom.**

---

## What was BUILT (Phase 1 — committed + pushed on `gascity-impl`)

The `sable` derived pack, canonical source in this repo at **`pack/sable/`**:
- `pack.toml` (imports gc base from `../gascity`), `README.md`, `REQUIREMENTS.md`
  (GC-METH-012 compatibility ledger), byte-identical `gc-role-worker` fragment.
- **Gate check-scripts** (`pack/sable/assets/scripts/checks/`):
  - `test-evidence.sh` — mechanically requires BOTH unit + integration proof in
    the implementation-summary (the Prime Directive, made mechanical). 6 tests.
  - `scope-creep-diff.sh` — worker diff ⊆ declared scope. **Builds SABLE-bijh**
    (was unbuilt). 6 tests.
- **Formulas** (`pack/sable/formulas/`): `sable-build` (extends `build-base`,
  inserts the two gate steps between review and finalize), `sable-review`,
  `sable-planning` (Phase-1 **stub**: office-hours framing only).
- **office-hours agent** (copied from gstack) — the framing lane.
- **31 pack tests pass**: `python3 -m pytest pack/sable/tests/`.

**Pack home / how it loads:** canonical in this repo (`pack/sable/`); a durable
`gascity-packs` checkout at **`~/dev-environment/gascity-packs`** exposes `sable`
as a **symlink sibling** of `gascity` (`gascity-packs/sable -> SABLE/pack/sable`).
gc resolves the `../gascity` import **lexically through the symlink** (verified
via `gc lint`). This works for lint + `gc formula show` + running. (Note: `gc
import add` of a path inside a git worktree would canonicalize — avoid; we author
the city import as a plain path.)

**Beads:** EPIC **`SABLE-vj4x`** with children `.1`–`.8`. `.1`–`.5` CLOSED
(scaffold, both gates, formulas, planning stub). `.6` (e2e validation)
**in_progress** — structurally validated (resolves in a real city, gates wired)
but the live walk-away was deliberately stopped; see below. `.7` (Phase-2
planning redesign) and `.8` (Phase-3 full contract + register) OPEN.

---

## THE FINDINGS (why we're reconsidering gc)

### 1. The per-item execution floor is ~22–27 min and ENGINE-LEVEL
Controlled test: the **same** trivial item, same `implement` formula + orchestration,
only the worker swapped:
- base `gc.implementation-worker`: **27m 34s**
- `gstack.implementer` (a mature reference pack): **21m 58s**
- warm agent-teams baseline (direct): **~1–2 min**

gstack ≈ equally slow → the floor lives in the **shared base machinery**, not in
SABLE's pack. No pack-config change closes the ~15–25× gap to warm.

### 2. Composition of the floor (from the 27.5-min single-item run)
~5 min orchestration (convoy validate → drain → prepare-worktree → spawn) +
~10 min worker orientation+coding (**code committed at ~14.7 min**) + ~12.5 min
**ceremony AFTER the code existed** (schema-conformant `implementation-summary.v1`
artifact + close/TDD-gate protocol). **The actual coding is ~2–4 min; ~24 min is tax.**

### 3. Why it's slow (mechanism)
`wall-clock = (turn count) × (per-turn latency)`, both inflated:
- **Cold session per step** — every step is a fresh `claude` session that
  re-orients (reads the schema, the repo conventions, prior summaries) before
  acting. This is the **durability tax** (disposable workers = crash-survivable).
- **Max-effort thinking on every turn** — inherited from the operator's
  `~/.claude/settings.json` `effortLevel: xhigh` (+ `CLAUDE_EFFORT=xhigh`), so
  workers run **planning-grade reasoning on trivial work**. Not gc forcing it —
  inherited, because gc workers are vanilla `claude` sessions.
- **Rich artifact ceremony** — schema-conformant documents (`requirements.v1`,
  `implementation-summary.v1` with coverage matrices, trace) are slow to produce.
- **Many sequential turns** — claim protocol → worktree validate → orient →
  TDD → commit → summary → close/gate.

### 4. Parallelism is the throughput lever — NOT warmth
- **Cold separate/parallel drain**, 3 independent items: **all done ~37 min**
  (vs ~75 min serial). Parallelism amortizes the floor across the batch.
- **Warm same-session drain**: **STALLED at 1/3** — the single-lane warm session
  was retired by **idle-drain** (default `min_active_sessions=0`), orphaning the
  rest. Warmth is **fragile** without `min_active_sessions≥1` + `sleep_after_idle="off"`,
  and even then it's serial (slower than parallel for independent work).
- **Pools scale FROM ZERO slowly** — observed worker ramp 1@6m / 2@11m / 4@16m.
  Instant N-wide parallelism needs a **pre-warmed pool** (`min_active_sessions`).

### 5. Intended gc model (from `gascity/docs/tutorials/`, all read)
- Workers are **on-demand / disposable BY DESIGN** (the durability mechanism).
- "Always-on" warmth is the **mayor/coordinator** (= SABLE's planning lane), not
  workers.
- gc's stated purpose: **big, fanned-out, UNATTENDED jobs** ("the reason Gas City
  exists is what happens when the job is bigger… without babysitting a session").
  Orders (tutorial 07) = autonomous cron/event triggers. Small interactive tasks
  are "the simplest possible job," not the design center.
- **We had been testing gc in its worst config** (cold, serial, small, watched).

### 6. Per-item model selection (the SABLE "model ladder") — supported, with a catch
- gc supports **per-bead `opt_model` + `opt_effort`** metadata ("per-dispatch
  provider options… validated against the provider's options schema at spawn" —
  `formula-spec-v2.md`; consumed by `session_reconciler.go`). This is the vehicle
  for SABLE's model ladder.
- **CATCH (unresolved):** per-bead `opt_model=haiku` on a **convoy item did NOT
  propagate** through `implement`'s drain to the spawned worker — we could not
  verify Haiku (no `--model` flag, no model env, no session-metadata model; the
  worker apparently ran the inherited default Opus). So the ladder needs
  **agent-level pinning** (`option_defaults = { model = "haiku" }`, the documented
  reliable mechanism) OR the `opt_model` must land on the actual spawn-step bead.
- **`effort` is NOT a per-agent `option_defaults` key** — it's a claude global
  setting (`effortLevel`) / env (`CLAUDE_EFFORT`). Model is per-agent settable;
  per-agent effort is not (cleanly).
- **Design principle (operator):** complexity→model is a **coordinator (mayor)**
  decision made automatically at decomposition/bead-creation, NOT a human
  planning input. The human plans *what/how*; the coordinator stamps the model.

### 7. Other friction filed as beads
- **`SABLE-tfkv`** — the SABLE `~/.claude` tdd-gate fires on gc workers (the
  "gates compose for free" thesis CONFIRMED live), but the tdd-evidence detector's
  session keying misses gc-managed sessions, false-blocking `bd close` and forcing
  a manual-evidence workaround.
- **`SABLE-8s8z`** — the master finding bead: the throughput analysis, re-framed
  per the operator's correction (the original slow run was `interaction_mode=autonomous`
  for the WHOLE run incl. planning, which over-elaborated; human-in-loop planning
  is the right-sizer). READ THIS BEAD — it has the full evolution.

---

## STRATEGIC CONCLUSION (where we landed)

**Two-surface architecture** (validated by all the data):
- **Warm lane** = the operator's existing 3-terminal / agent-teams flow. Fast,
  interactive, for small / urgent / serial work. The daily driver.
- **gc batch lane** = large, independent, unattended, parallel batches.
- **SABLE = planning + gates**, surface-agnostic (gates already compose with both;
  proven). Planning is the shared front door; the planning **tier** (QUICK/FULL)
  ≈ the routing decision (QUICK/hotfix → warm; FULL/deep → big backlog → gc batch).
- A thin **serializer** at the plan→execution seam turns the approved backlog into
  a gc convoy (`create_beads_from_tasks.py` already does this) + a traceability
  link back. The model ladder is stamped here by the coordinator.

**BUT — the live question (operator leans this way):** the gc execution layer's
~25-min floor may make it not worth it even for batches (the floor is *fixed per
item*; only acceptable when items are large + parallel + unattended). So consider
**building a custom wrapper** that keeps gc's GOOD ideas and drops the costly layer:

| Keep (gc's good ideas) | Replace (the costly execution layer) |
|---|---|
| Durable bead substrate (state survives crashes) | Cold `claude` session **per step** → use **warm persistent workers** |
| Gates compose via `~/.claude` hooks (proven) | Heavy schema-conformant artifact ceremony → **light artifacts** |
| Formula/DAG decomposition + dependency gating | Slow pool reconcile / scale-from-zero → **pre-warmed / direct** |
| Per-item model ladder (`opt_model`) | Inherited xhigh+Opus on trivial work → **coordinator-assigned per item** |
| Parallel unattended drain; planning/execution split | The ~25-min orchestration+ceremony tax → a lean custom loop |

The wrapper would aim for: **warm-lane speed for the human-in-the-loop planning +
a lean, parallel, unattended execution drain** that doesn't pay the cold-start +
ceremony tax per item.

---

## OPEN QUESTION FOR NEXT SESSION

**Gas City as engine, or custom wrapper?** Discuss concretely:
1. Which gc ideas are load-bearing to SABLE's value (durability, gates, ladder,
   planning/exec split, parallel unattended drain) vs incidental.
2. What a lean custom execution layer looks like (warm worker pool that's *fed*
   work and reuses context; minimal artifacts; the gates as the quality bar).
3. Whether the warm 3-terminal flow + a thin orchestration shim already covers
   90% of the need, making gc unnecessary except for rare huge batches.
4. The simplicity thesis (operator's north star): the vanilla 3-terminal version
   was the "most effective ever"; every custom-orchestration layer since regressed
   it. A custom wrapper must NOT become another bug-farm — keep it minimal.

---

## STATE / HOW TO RESUME

- **Repo:** branch `gascity-impl`, clean + pushed. Pack at `pack/sable/`.
- **Beads (pushed):** `SABLE-vj4x` EPIC (`.1`–`.5` closed, `.6` in_progress,
  `.7`/`.8` open); findings `SABLE-8s8z` (master) + `SABLE-tfkv`. Run
  `bd show SABLE-8s8z` and `bd children SABLE-vj4x` first.
- **gc tooling:** `gc-dev` binary at `~/go/bin/gc-dev` (linked beads 1.0.5; brew
  `gc` is mismatched 1.0.4 — use `gc-dev` by full path). gascity **engine** clone
  at `/tmp/claude-1000/.../47836d78-.../scratchpad/gascity` (**NON-DURABLE
  scratchpad — may vanish; re-clone if gone**). gascity-**packs** durable at
  `~/dev-environment/gascity-packs`.
- **Test cities** (scratchpad, suspended): `sable-e2e-city`, `sable-fast-city`,
  `gstack-test-city`, plus older `spike-city*`. **Accumulated dolt instances are
  causing `bd init` contention** — a fresh `gc init` failed with "bd init failed."
  **If reviving gc work, clean up stale test cities first** (free dolt) before a
  new city will init.
- **Key gc docs read** (in the engine clone): `docs/tutorials/01-07`,
  `docs/reference/specs/formula-spec-v2.md`, `docs/guides/configuring-an-agent.md`.
- **Pending experiment we did NOT finish:** a clean Haiku@high single-item timing
  baseline (blocked by the dolt contention above + effort-not-being-a-per-agent
  knob). Expected ~10–18 min if run (model cuts worker-think, but orchestration +
  ceremony are model-independent). Not essential — confirmatory only.

**Recommended first moves next session:** (1) read `SABLE-8s8z` + this doc; (2)
decide gc-vs-custom-wrapper with the operator; (3) if custom: design the lean
execution layer keeping the "Keep" column above; if gc: clean the test cities,
then resume `.6` with a pre-warmed pool + agent-pinned model ladder.
