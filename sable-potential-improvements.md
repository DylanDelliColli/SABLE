# SABLE Potential Improvements

A living research log. Each entry is a candidate improvement distilled from an
external-repo review, with the evidence behind it and its current adoption
status. Started 2026-07-30 from a review of Dicklesworthstone's
`mcp_agent_mail` and `agentic_coding_flywheel_setup` (ACFS). Update this file
as further repos are reviewed.

**Status legend:**
- **ADOPTED** — operator ruled; doctrine or tooling already changed.
- **FILED** — a bead exists; work is scheduled or schedulable.
- **PROPOSED** — recommended; awaiting an operator call.
- **INPUT** — feeds an existing decision or epic; no standalone action.

## Sources reviewed (2026-07-30)

| Source | Version | What it is |
|---|---|---|
| [mcp_agent_mail](https://github.com/Dicklesworthstone/mcp_agent_mail) | 5e48183 (v0.3.4) | Durable agent-to-agent mail server (FastMCP, SQLite + git archive) with advisory file leases and a git-hook guard |
| [agentic_coding_flywheel_setup](https://github.com/Dicklesworthstone/agentic_coding_flywheel_setup) | 90d82d4 | VPS bootstrap + a 10-tool agent-orchestration stack, manifest-driven installer |
| [ntm](https://github.com/Dicklesworthstone/ntm) | main | Named Tmux Manager (Go). The tmux send/readiness layer ACFS installs |
| [beads_rust](https://github.com/Dicklesworthstone/beads_rust) | web review | Active Rust port of classic Go `bd`; local-first, JSONL, no dolt |
| [rtk](https://github.com/rtk-ai/rtk) | 8a24ce2 (0.42.4) | Rust CLI proxy filtering bash output before the agent reads it |
| [headroom](https://github.com/headroomlabs-ai/headroom) | v0.33.0 | Context-compression layer (proxy/library/MCP) with reversible CCR store |
| [graphify](https://github.com/Graphify-Labs/graphify) | v0.9.31 | Local tree-sitter knowledge graph; explorer ran it against a SABLE copy and measured |
| [ouroboros](https://github.com/Q00/ouroboros) | 198112a | Spec-first "Agent OS"; planning-phase mechanisms only (operator ruling) |

Full mechanism inventories with file:line references live in the 2026-07-30
cockpit session transcript. Condensed findings are on SABLE-wgylm (closed).

---

## 1. Message transport: port ntm's tmux send fixes — PROPOSED (recommend front of queue)

**Problem.** sable-msg is measurably lossy. 263 of our 1,712 closed beads
(15%) are auto-filed `SABLE-MSG undelivered` fallbacks. We have paid for this
repeatedly: dropped manager sends, a dead optimus→chuck edge, false-negative
`--interrupt` escalation.

**What ntm knows that we don't.** Its send path fixes four silent-loss
mechanisms, each documented in code comments:

1. `send-keys -l` strips newlines on tmux 3.6+. Char-by-char typing also races
   Claude Code's file/@-mention autocomplete picker: a prompt containing a
   path, an `@`, or a `name.ext` token can pop the picker mid-token, so the
   trailing Enter selects a menu entry instead of submitting. Every SABLE
   dispatch prompt contains paths and bead IDs. Fix: route through
   `load-buffer` (stdin) + `paste-buffer -p` (bracketed paste, atomic) when
   content has newlines or autocomplete-risk tokens (`session.go:1911-1941`).
2. tmux treats an argument ending in an unescaped `;` as a command terminator;
   `--` does not protect it. With 4096-byte chunking, a chunk boundary after a
   `;` drops bytes from the middle of a prompt (`session.go:1531-1558`).
3. Enter must be its own keystroke, sent after a delay (100 ms for agent
   panes), as the literal `Enter`, not `C-m` — Codex distinguishes them. Agent
   panes get double-Enter: text → 1 s → Enter → 500 ms → Enter
   (`session.go:1963-2006`).
4. Chunk at 4096 bytes with UTF-8 rune-boundary backtracking. Wrap tmux calls
   in a circuit breaker that counts only infrastructure failures
   (`client.go:48-123`).

**Why it leads.** The drain runs on this channel. Fixing it first makes every
later dispatch more reliable, and it shuts off the largest class of auto-filed
bead exhaust (§6).

## 2. Delivery semantics: durable store + tmux doorbell — PROPOSED

**The hybrid.** mcp_agent_mail's store is better than our channel; our tmux
nudge is better than its notification layer (which never wakes a busy agent —
its push layer ships disabled and unwatched). The right architecture: a
durable message record (send either raises or is stored — no "did the
keystroke land" ambiguity), with the tmux send demoted to a doorbell.

**Two mechanisms worth grafting even without full adoption:**
- **Tri-state receipts.** Delivered, read, and acknowledged as three explicit
  per-recipient states. Their inbox fetch deliberately does not mark messages
  read; reading and acking are separate calls.
- **ACK-escalation backpressure.** A recipient that stops acknowledging gets
  its inbox progressively lease-locked, so senders start failing loudly
  instead of succeeding silently. This is the single best answer either repo
  offers to our false-receipt problem. It ships disabled in theirs.

**Adoption caveats (if we take the server itself):** committed live bearer
token, stored XSS in search snippets, an env-var auth bypass, unauthenticated
write endpoints by default, no prompt-injection handling on message bodies.
Treat as a parts shop, not a substrate.

## 3. Pane readiness and send verification — PROPOSED

- **Per-provider classifiers with golden fixtures.** ntm keeps
  working/idle patterns per provider with captured pane fixtures per state, so
  a provider TUI change fails a test instead of silently corrupting dispatch.
  Its Claude classifier orders dynamic markers ("which marker is most recent")
  rather than trusting position, because Claude pins its input box to the
  bottom whether busy or idle. We learned a fragment of this fixing
  SABLE-6c391 (Codex ghost placeholder); ntm has the full library.
- **Bias to false-busy.** Their stated rule: a dispatcher must never inject
  into a working agent, so the only acceptable classification error is
  treating a maybe-idle pane as busy. Matches our dialog-stall discipline;
  worth encoding.
- **Robot-ack.** Post-send confirmation with four evidence types
  (prompt-returned / echo-detected / explicit-ack / output-started) and
  per-pane latency. This mechanizes our standing rule that a delivery receipt
  is not delivery — confirm by a positive control in the receiver's state.

## 4. Footprint enforcement: close the declared-vs-touched gap — PROPOSED

Our dispatch-time footprint check is stronger than their server (which always
grants and merely warns). Their enforcement end is stronger than ours:

- **A git pre-commit/pre-push guard** that blocks changes touching another
  agent's exclusive lease. It reads state from disk (keeps enforcing when the
  server is down), resolves `--git-dir` correctly for linked worktrees,
  expands renames into both old and new paths, and installs as a chain-runner
  that composes with existing hooks. Grafting this onto our declared
  footprints closes SABLE-84qf9's intra-wave overlap hole without touching the
  dispatcher.
- **Build-slot lease.** A coarse file-based mutex plus a subprocess wrapper
  with a background renewer. This is the right shape for making Chuck's
  merge-seat exclusivity mechanical rather than conventional.

## 5. Install and activation: manifest-declared link-vs-copy — INPUT (feed SABLE-y4nom)

ACFS resolves our entire pin/symlink activation saga structurally:

- **Symlink and copy are two differently named operations** chosen per tool in
  a declarative manifest. Eight call sites total, each visible in a diff with
  a stated reason. A guard refuses to replace an existing non-symlink.
  "Live or pinned" stops being installer trivia and becomes a reviewed,
  per-tool declaration. This dissolves the doctrine we currently maintain by
  hand (pin verification by object hash, hot-swap regimes, repin reverts).
- **A drift contract**: deliberately dumb regexes assert that non-generated
  surfaces track the source of truth, with named failure codes. Documentation
  rot becomes a build failure. Our `--check`/`--check-beads` are narrow
  versions; the generalization is one contract file.
- **Refuse to auto-fix a dirty tree**, with the incident that taught them
  named in the comment.
- **Doctor checks generated from per-tool verify entries**, where "verify"
  means wired-in (hook registered in settings), not merely present — and every
  failure emits a copy-pasteable fix command.

## 6. Backlog discipline — mixed status

- **Capture-then-curate — ADOPTED (operator ruling 2026-07-30; SABLE-jgax3, P0).**
  Routine discovery goes to `sable-note`; beads enter the pool only through
  operator-invoked `/sable-review`. Carve-out: blocking defects bead
  immediately. Scope: global. Doctrine amended in global CLAUDE.md and the
  project CLAUDE.md (Codex-visible copy); SABLE-jgax3 tracks the remaining
  sweep, sable-note per-project routing, and the prose-guard test.
  Evidence: ACFS's pool sits at 97.8% closed with all open beads from one
  planning wave, because bead creation there is a deliberate act. Ours grew
  arithmetically because every agent filed on reflex.
- **Granularity finding — no change needed.** Measured 2026-07-30: work
  quantum per bead is nearly identical (median time-to-close 2.0 h ACFS vs
  3.3 h SABLE). Our descriptions run 1.5–2.5× longer because they carry
  forensic evidence and doctrine — deliberate, keep. The divergence was pool
  semantics (work vs coordination exhaust), addressed above and by §1.
- **`close_reason` discipline — PROPOSED.** ACFS records a one-paragraph
  "what actually got done" on every close. Cheap, and it makes the closed set
  legible for analysis.
- **Dedup pass and reality-check prompts — PROPOSED.** After every large
  bead-creation batch: an explicit merge-duplicates pass. Periodically: "if
  we implement every open bead, does the gap close completely? Why not?"
  Both are named, reusable prompts in ACFS doctrine.
- **bv (beads viewer) evaluation — PROPOSED, HELD (operator).** Graph-leverage
  ranking over the bead DAG (`--robot-next`: unblocks-count, PageRank,
  reasons, claim command). Use cases: dispatch ordering under a worker cap,
  triage by centrality, decomposition-time graph QA. Must first verify it
  parses our Go-bd/dolt data (likely needs a JSONL export path). Agents must
  use `--robot-*` flags only; the bare TUI blocks a session.

## 7. Lint agent-facing prose as a CI gate — PROPOSED (seeded)

ACFS lints agent-facing docs for commands that violate a runtime policy
(`file:line` output, inline suppression marker, no runtime dependency),
because agents read the docs — prose drift is a production incident. SABLE has
a pile of prose-only rules begging for this: bare `bd close` never piped, dolt
push is Chuck-only, never `bd edit`, `--append-notes` never `--notes`, one bd
command per Bash call. SABLE-jgax3's test spec seeds the first instance (a
prose guard for the retired reflex-bead directive).

## 8. Planning methodology ideas — INPUT (planning-mode candidates)

- **Three spaces with an escalation ladder.** Plan space, bead space, code
  space. Debates belong in plan space; dependency shaping in bead space.
  Discovering missing structure while coding means step back up a level, not
  push more code through a weak graph.
- **The one-way door.** Once in bead space, never look back at the markdown
  plan — which forces full detail transfer into beads. A stricter phrasing of
  "the backlog IS the plan."
- **Quantified convergence gate for bead polishing.** Weighted score over
  output-size shrinkage, change velocity, and content similarity; finalize at
  0.75+, diminishing returns above 0.90. Could sharpen our DECOMPOSITION
  substage exit criterion.
- **The 15-minute operator sweep.** A named, short human control loop: is the
  top pick still sensible, any silence/blockers, stale reservations, one
  drifting agent to restart, and "will the open beads actually close the
  gap?"

## 9. Capacity and admission — PROPOSED (light-touch)

- **Read-only stoplight, human launches.** ACFS's five-layer admission stack
  produces a number, a stoplight, and a copyable spawn command — then stops.
  No autoscaler. Matches our operator-present culture; parts of it could
  harden our worker-cap practice.
- **`does_not` arrays.** Their swarm tools emit a machine-readable "here is
  what I will never do" list inside their own JSON output, with tests proving
  each claim. Worth copying for sable tools that agents call.
- **Offload-aware caps.** Under build-queue pressure their agent cap becomes
  available remote build slots, not local RAM — the melt risk is N agents
  compiling, not N agents thinking. Relevant if we ever offload test runs.

## 10. Substrate intel — INPUT (attach to SABLE-qirwv / engine decision)

- Go `bd` is frozen as "classic beads"; Yegge's active development moved
  toward Gastown (Gas City) — already SABLE's intended swappable engine.
- `br` (beads_rust) is a very active (~2,400 commits), Yegge-endorsed Rust
  port that freezes the classic SQLite + JSONL architecture. **No dolt
  backend**; strictly local-first with explicit manual sync. Given our
  dolt-corruption history (the origin of the Chuck-only push rule), "classic
  beads without dolt" now has a maintained implementation. Real datapoint for
  the engine decision; not a recommendation yet.

## 11. Bash-output token economy: rtk — PROPOSED (narrow pilot, hard exclusions)

rtk is careful engineering, not a naive `tail`: exit codes preserved
(including signal → 128+sig), a never-worse token guard, tee-to-disk recovery,
and regression tests asserting failures are not masked. It composes with our
hook stack: the settings merge is additive, and `bd`, `sable-*`, and
`bash `-prefixed commands are untouched by construction, so tdd-gate still
sees `bd close` verbatim.

- **Disqualified as-shipped: `rtk pytest`.** It injects `--tb=short`, drops
  `-rs` skip reasons and the entire `--sable-report-skip-set` reporter output,
  caps failures at 10, and never surfaces stderr to the agent. Also exclude
  `git add/commit/push` — the session-close protocol requires verifying the
  push, and rtk collapses it to one line.
- **Pilot scope:** read-only commands only (`git status/log/diff/show`, `gh`,
  `ls`, `grep`, `cat`, `ps`). Set `RTK_TEE_DIR` per tmux pane first — the
  shared tee store keeps 20 files and a fleet rotates recovery files away.
- **First experiment before anything:** run one `python -m pytest` under the
  hook and check whether `hooks/tdd-evidence.sh` records evidence. rtk is not
  in its wrapper-unwrap allowlist; the failure would masquerade as a TDD-gate
  bug.
- **Codex caveat:** rtk gives Codex prompt guidance only, no interception —
  near-no-op for all-Codex fleet runs.
- Two pre-existing SABLE gaps this review surfaced (captured as sable-notes
  2026-07-30): tdd-evidence's hardcoded wrapper allowlist, and the skip-set
  reporter's lack of a stable machine-readable line prefix.

## 12. Context compression: headroom — CAUTION (lossless-only pilot; shift-length hypothesis dead)

- **Manager shift length: wrong tool.** Headroom compresses the request wire
  format; it does not shrink what the client's own context accounting holds,
  so it buys no shift extension. The orthogonal lever found on the way:
  pointing Claude Code at a custom base URL makes it materialize every MCP
  tool schema into context; their `--tool-search` restores deferral.
- **Default profile disqualified** under the no-silent-signal-loss doctrine:
  the LOG/SEARCH/DIFF compressors emit their retrieval marker only below a
  ratio threshold while the router accepts any shrink, so a pytest log can
  lose lines with no recovery path — and the wrong assumption is enshrined in
  one of their passing tests.
- **`HEADROOM_LOSSLESS=1` is the only acceptable phase-1 config** — verified
  byte-exact inverse contract, explicit no-unrecoverable-loss invariant.
- **Fleet-topology bug (released, fix pending):** without a per-session
  header, parallel panes with near-identical system prompts share one
  prefix-cache tracker — measured 2.5–3x net cost increase under Claude Code.
  Per-pane session IDs are mandatory in any pilot.
- **Pilot if ever:** proxy form on Codex worker panes, lossless mode, per-pane
  session IDs, CCR TTL raised above the longest worker run, gated on their own
  retention scorer showing lost == 0 in the errors dimension.

## 13. Repo indexing and anchor freshness: graphify — PROPOSED (adopt the indexer, skip the graph)

Explorer-measured against a SABLE copy (363 code files, 89k LOC Python + 43k
bash): cold build 33–46 s, 9,973 nodes, symbol→file:line exact on 15/15
samples, and `detect_incremental()` answers "which files changed since the
graph was built" in ~3 s without touching the graph.

- **Top pick, shippable without adopting the graph at all:** teach Victor the
  3-second manifest check — intersect the changed-file set with the files
  named in open beads' fingerprints. Replaces most of a grep-driven freshness
  pass for seconds of cost.
- **Second:** `graphify explain <node-id>` symbol cards (~300 tokens: exact
  location plus callers and callees with file:line) for worker priming.
  Avoid the natural-language `query` path — measured noisy and truncated on
  SABLE.
- **Skip for planning audits:** measured god nodes are our test scaffolding,
  communities rediscover the directory layout, and INFERRED edges misbind on
  shared test-helper names.
- **Disqualifying for architecture description:** the graph contains zero
  Python↔bash edges (39 cross-file bash edges, all .sh→.sh). SABLE's
  architecture is exactly that seam. Upstream feature request, not a config
  change.
- **Install cautions:** project scope only; skip its git hooks (post-commit
  spawns detached rebuilds and deliberately skips linked worktrees, so worker
  commits refresh nothing anyway); bare `graphify install` appends to the
  global CLAUDE.md; `built_at_commit` is advisory — a stale graph answers
  with stale line numbers and no warning. Incidental: the graphify skill
  installed on this machine is 0.4.14 against 0.9.31 current.
- **Merge-train delta comparison (operator idea 2026-07-30) — PROPOSED behind
  an evidence gate.** Not per-worktree full graphs (37 s per rebuild, and the
  hook skips worktrees anyway): one spine graph per integration landing, plus
  a per-branch delta (changed symbols + edges in/out) from the 3-second
  manifest check. Pairwise delta comparison across the queue predicts the
  semantic-conflict class footprints cannot see (branch B adds a call into a
  symbol branch A removed) and gives a finer disjointness instrument for
  trains admission (SABLE-21rug) and intra-wave overlap checks (SABLE-84qf9).
  Hard guardrails: seam-touching branches (Python↔bash) can never be declared
  graph-disjoint — the graph has zero cross-language edges; EXTRACTED edges
  only for disjointness verdicts (INFERRED misbinds measured); every verdict
  binds to the spine SHA and recomputes on landing (discipline 13 / the kznzo
  composition lesson). Evidence gate before any wiring: replay Chuck's recent
  queue history pairwise and measure predicted-red true positives vs
  false-conflict serializations. Cuts against y4nom simplification — proceed
  only if replay shows real batching gains, after the coverage-floor fix and
  triage.

## 14. Planning-phase mechanisms: ouroboros — INPUT (feed /sable-plan; operator ruled planning-only)

Operator ruling 2026-07-30: the adoption surface is planning, not their
execution runner. The portable mechanisms:

- **Dual-closure gate.** A stage closes only when a structural completeness
  check AND a confidence score both agree; if the model says ready but the
  ledger has gaps, closure is refused and the next question targets the first
  gap. Maps directly onto our substage gates — a human "looks good" is one
  signal, not sufficiency. (Their README oversells this: the interactive path
  is human-gated with a force override; adopt the scoring, not their wiring.)
- **Requirements ledger with evidence provenance.** Ten required sections
  (including non_goals, verification_plan, failure_modes); every entry labeled
  by source, with model-inferred content deliberately excluded from
  evidence-backed status and queryable as assumption-only. "Human-confirmed
  vs inferred" is invisible in a bead today and is a real risk-class
  distinction for the Fresh Agent Test.
- **Deterministic spec linter + per-AC content hash.** A no-LLM vague-terms
  blocklist (easy, robust, scalable, seamless…) that `bd lint` could grow this
  week, plus a content hash per acceptance criterion (position- and
  session-independent) enabling a close-time check that the bead closed is
  the bead planned.
- **Policy worth copying verbatim:** never auto-rewrite an approved spec when
  execution cannot meet it — a named reward-hacking surface. (Their own
  evolutionary loop violates it; the caution is half the lesson.)
- **Deterministic context pack:** ≤6,000-char machine-generated repo priming
  (manifests, verify commands, tree), explicitly no LLM prose. A cheap
  deterministic half of worker priming beside the bead description.
- **EventStore: no audit value for us.** Prompts stripped by design, no
  tamper evidence, absence of a row proves nothing, and the coding CLIs never
  emit into it. Clean-room rerun is categorically stronger (they have zero
  hermetic re-verification). Two concurrency ideas inside it are worth
  remembering: CAS one-winner terminal-transition guards, and write-path
  secret hygiene as an invariant.
- **Verification benchmarks to hold tdd-gate against** (captured as a
  sable-note): orchestrator-run verify commands with a before/after workspace
  digest (closes the "verify command that fixes what it verifies" hole);
  evidence rules that refuse agent self-report as sole support; reviewer
  independence as structure with honest "unverified" labels; a single
  choke-point veto that can only flip approve→reject.
- **Where SABLE is measurably ahead:** durable dependency-aware backlog, five
  human gates vs one force-overridable, hermetic clean-room verification, an
  enforced coverage floor.

## 15. Explicitly not taking

- mcp_agent_mail's notification layer (weaker than our tmux nudge) and its
  advisory-only dispatch grant (weaker than our footprint check); its security
  posture as shipped (see §2 caveats).
- ACFS's per-provider rule sync (negative result) and its unwired onboarding
  gates.
- rtk's pytest filter and git-write filters (§11), and `rtk err` (known
  exit-code swallow, their #846).
- headroom's default lossy profile (§12), and any use where the retention
  scorer reports lost > 0 in the errors dimension.
- graphify's NL-query, community-clustering, and audit layers on SABLE (§13),
  its git hooks, and `--strict` mode (denies the agent's first raw Read).
- ouroboros's execution runner, its skills-as-load-bearing-infrastructure
  pattern, its `bypassPermissions` execution default, and its EventStore as an
  audit substrate (§14).
- Wholesale adoption of any of the six systems. All are parts shops.

## 16. Decisions logged from these reviews (2026-07-30)

1. **Capture-then-curate adopted** (carve-out: blocking defects bead
   immediately; scope: global; curation: operator-manual only). SABLE-jgax3.
   Applied during the second batch: three discoveries captured as sable-notes
   rather than beads (tdd-evidence unwrap allowlist, skip-set reporter marker,
   verify-mutation digest gap).
2. **Modular repo split: deferred, criteria set.** Not a big-bang split. A
   module earns its own repo when it has zero sibling-lib imports, a stable
   CLI/JSON contract, isolated tests, and versioned checksummed consumption.
   Sequence: triage → drag-reducers (SABLE-1dmfc, send channel) → drain →
   y4nom simplification → one-leaf extraction pilot. ACFS proves the model
   works but shows the integration machinery it costs.
3. **Bead granularity: keep ours.** Work quantum matches ACFS; our longer
   descriptions are the institutional-memory function and are deliberate.
4. **Ouroboros is planning-phase only** (operator, 2026-07-30): its interview
   scoring, ledger, linter, and gate ideas feed /sable-plan; the runner and
   EventStore are out.
5. **Headroom's shift-length use case is rejected on mechanism** — it cannot
   extend manager shifts; only its worker-output compression survives as a
   conditional, lossless-only candidate.
6. **bv evaluation and all §1–§7, §11–§13 proposals: held** pending operator
   scheduling.
