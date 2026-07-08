# SABLE Planning Modes — Design (2026-06-26)

**Status:** high-level model **locked** this session (the three-mode shape, free
entry, the Discovery→Full composition, and Discovery's strategic-triage
character). Discovery's *practical* design is **in progress** — see the OPEN
section at the bottom. Full and Quick are settled **as-is**; this doc does not
change them beyond one ingestion seam on Full. No code yet.

This sits upstream of `TMUX-AGENTS-DESIGN.md`: that doc made the **execution**
layer a reliable consumer; this one is about the **quality of what we feed it**.

---

## Why (the bottleneck moved)

With execution now reliable (warm-pane tmux topology), nearly every remaining
unit of "did SABLE produce good work" is decided **upstream of execution** — by
the quality of the backlog. Execution is a faithful consumer now; garbage in is
no longer masked by a human babysitting the run.

The office-hours note of 2026-06-24 already named the strategic shape: SABLE is
**"a planning/enforcement half (the durable moat) plus an execution-coordination
half that is largely rentable."** We spent the prior effort making the rentable
half reliable. The durable value now lives in planning.

A second observation from the feedback corpus: **planning failures don't announce
themselves.** They surface downstream as merge conflicts, mis-scoped workers, and
rework — and get logged as *execution* problems. So planning quality is
under-measured precisely because its defects wear execution's clothes.

---

## The core insight: two activities, three modes

The planning modes are **not** three points on a single "ceremony" dial. They
split into two fundamentally different activities:

- **Discovery** answers **"what should exist?"** Its output is *intent* —
  understanding, direction, charters. It **does not feed execution. It feeds the
  next planning run.**
- **Full and Quick** both answer **"how, precisely?"** Both output an *executable
  backlog* (beads). They differ only in **rigor proportional to risk** — five
  gates vs. one pass.

So: **one discovery mode + two specification modes.** This is the distinction
that determines each mode's artifact and who consumes it.

---

## The three modes at a glance

| | **1. Discovery** | **2. Full** | **3. Quick** |
|---|---|---|---|
| **Trigger** | "What should we build / what is this for?" — fuzzy, ≥1 candidates, no defined deliverable | A known thing to build, with unknowns / wide blast radius | A small, well-specified ask |
| **Core question** | What should exist, and why? | How do we de-risk + decompose it? | What are the 1–3 beads? |
| **Lens** | **Business / strategic** | Engineering | Engineering |
| **Deliverable** | Decision record + a charter per survivor | Tested, Fresh-Agent-clean backlog | 1–3 tested beads |
| **Feeds** | **The next planning run** | Execution | Execution |
| **Engine** | office-hours, at portfolio altitude + triage | The 5 gated substages | One lightweight pass |
| **Done when** | Survivors charted + no-gos recorded | `swarm validate` + no open-questions | Single consolidated gate approved |
| **Status** | **NEW — design in progress** | as-is | as-is |

---

## Free entry + the composition pipeline

**Free entry.** There is no mandatory ladder. The user enters at whichever mode
fits the situation — Discovery is not a required first step.

**But the modes compose when chained:**

```
Discovery ──> decision record + survivor charter(s)
                     │
                     ▼  (charter = Full's FRAMING input, pre-satisfied)
                  Full ──> epic(s) ──> beads ──> execution
                     ▲
        Quick ───────┘  (side-door: small, well-specified asks straight to beads)
```

The key composition fact: **office-hours' design doc is a strict superset of what
Full's FRAMING substage produces** (user stories, non-goals, success metric,
narrowest wedge). So a Full run launched from a Discovery charter **starts at
RESEARCH** — its FRAMING is already satisfied, carried in as the charter artifact.
This is the only change to Full: it must be able to *ingest* a charter instead of
generating framing cold.

---

## Discovery — locked character (the "what", not yet the "how")

Discovery is a **strategic triage with a business lens**, not a single-idea
brainstorm.

- **Input:** a *set* of candidates under consideration (e.g. 2–3 new features /
  modules for a nascent-but-built product), or an open "what should we do next."
- **Process:** office-hours-grade interrogation **per candidate** (the six
  forcing questions, anti-sycophancy, premise challenge, alternatives,
  landscape) **plus cross-candidate triage** — the part vanilla office-hours
  doesn't do, because it assumes one idea.
- **Output:** **a decision record + a charter per survivor.** Kill one of three,
  keep two → two thought-through charters, each its own forward stream.

Three structural commitments:

1. **No-gos are first-class output, not a byproduct.** "We considered X and
   killed it *because…*" is what stops the same dead idea being relitigated in
   three months. A Discovery session that kills *all* candidates still produced a
   valuable artifact. **The deliverable is the deliberation, not just the
   survivors** — which is why Discovery "always produces something to come back
   to" and never just ends.

2. **One survivor ≠ one epic.** A kept feature may be large enough to become
   several epics. Discovery must **not** pre-draw epic boundaries — that is
   engineering-lens decomposition, which belongs to Full. Discovery hands off a
   charter ("this is worth building, here's why"); Full decides it is 1 epic or 3.

3. **The lens guardrail is the point.** Discovery answers *what / why / for-whom
   / is-it-worth-it* — demand, wedge, differentiation, fit. It explicitly
   **defers *how*** (architecture, test strategy, decomposition) to Full. This
   must be enforced: office-hours' doc template has "Approaches Considered /
   Dependencies" sections that tempt toward solutioning; in Discovery those stay
   *product* approaches, not technical ones.

---

## Discovery — practical design (locked)

### The arc (four beats)

```
(0. Diverge — only if open)   "what should we do next?" → surface candidate features
 1. Interrogate                office-hours' six forcing Qs, applied to the candidates
 2. Triage                     cross-candidate verdict: go / no-go / reshape + why
 3. Fan out                    decision record  +  one charter per survivor
```

Beat 0 fires only on open-ended entry ("what's next for this product"); with a
named 2–3 candidate set in hand, start at beat 1.

### Run shape — comparative by default, escalate on demand

One Discovery session holds all candidates in the **same context** and triages
them **against each other** — comparison is the whole point of the mode. A
heavy/ambiguous candidate can be **escalated to its own deep office-hours dive**,
then folded back into the comparative triage. Comparative-by-default avoids the
3× interview cost; escalation preserves depth where it's earned.

### The two artifacts

**A. Decision record** (one per session) — the durable "come back to it" record:
candidate set; per candidate a **verdict (go / no-go / reshape) + rationale**;
links to survivor charters; **no-go rationales kept verbatim** (the
relitigation-killer).

**B. Charter** (one per survivor) — *this is Full's FRAMING input.* office-hours'
startup-mode design doc with engineering sections stripped:
- *keep:* Problem Statement · Demand Evidence · Status Quo · Target User &
  **Narrowest Wedge** · Why-now · **Product** approaches · Recommended product
  shape · **Success metric** · **Non-goals** · Open questions
- *defer to Full:* technical dependencies, architecture, test strategy
- *header:* back-pointer to the decision record + the epic-intention shell id

### Engine, mode, handoff, invocation

- **Engine:** reuse **office-hours** wholesale (its Phases 1–5 already produce
  ~90% of charter B); the SABLE Discovery skill is a thin wrapper that adds the
  triage beat, emits the two artifacts, and enforces the business-lens guardrail
  via the trimmed template. No new interview is built. *Caveat:* office-hours is a
  **gstack** skill — reuse couples SABLE-Discovery to gstack (a conscious
  dependency, since SABLE is the portable opinion layer).
- **Mode/interlock:** the *lightest* planning sub-mode — authors no
  implementation beads, so **no backlog gate**; only the standard planning blocks
  (no manager spawns, no code push). Charter files + epic-intention shells allowed.
- **Handoff:** charter on disk (`.claude/sable/charters/<slug>.md`) **+** a bare
  epic-intention shell per survivor pointing at it, so a later Full run finds its
  home and starts at RESEARCH. **Charters are committed (durable)** — they are the
  come-back-to record that feeds future planning and must survive a fresh clone;
  this is the opposite of `.claude/sable/state/` (ephemeral, gitignored). The two
  coexist under `.claude/sable/` because the gitignore entry is the specific
  `state/` path, not the parent.
- **Invocation:** Discovery gets its own door (e.g. `/sable-discover`) rather than
  asking the self-sizer to guess it — entry is free across all three modes.

---

## The planning dossier (Full-tier gate deliverable, 2026-07-08)

**Why.** A substage gate is only as good as what the human reviews at it. The
old signoff was a scrollback of per-finding prompts plus a text summary buried
in epic notes — origin `sable-note` 2026-07-08: the TEST-STRATEGY gate should
present a visual story×test matrix, and every gate should offer a consolidated,
reviewable deliverable rather than a text-only approval.

**Convention.** Each Full-tier substage producer drops a JSON deliverable into
the per-repo planning state dir
`<repo>/.claude/sable/state/planning/<epic-id>/` (git-common-dir resolution,
same as the mode-state file; gitignored):

| File | Producer | Gate |
|---|---|---|
| `framing.json` | Lincoln (or charter ingest) | FRAMING |
| `research.json` | sherlock | RESEARCH |
| `architecture.json` | gaudi | ARCHITECTURE |
| `test-strategy.json` | columbo — the story×test matrix | TEST-STRATEGY |
| `decomposition.json` | Lincoln + victor + `bd swarm validate` | DECOMPOSITION |

`bin/sable-dossier <epic-id> --highlight <substage>` assembles whatever exists
into one self-contained HTML page (missing substages render as "not yet
produced"; malformed JSON degrades to a per-section error box). The canonical
schemas live in the `bin/sable_dossier_lib.py` docstring — producers reference
them there rather than duplicating them in role files.

**Publishing.** At every gate, Lincoln publishes the dossier with the Artifact
tool using the same file path for the whole run — one stable URL that grows
section-by-section, the pending section marked *awaiting signoff* — then asks
for approval and only then advances the substage. The framing stories' ids
(`S1..Sn`) are the traceability spine: columbo traces every test case back to
one, and beads that trace to none surface as `unmapped_beads`.

Quick tier is out of scope for now (SABLE-lykc.7 tracks syncing the standalone
columbo skill variant).

## OPEN — remaining build-design seams

Discovery's design is locked; these are the cross-mode build details to settle
during implementation (likely their own beads):

- **Full's charter-ingestion seam** — how a Full run detects a charter +
  epic-intention shell, reads it as FRAMING, and starts at RESEARCH instead of
  generating framing cold.
- **Free-entry plumbing** — how all three modes are entered now that the existing
  `/sable-plan` self-sizes only quick↔full; where Discovery's door lives and how
  the modes share (or don't) the mode-state file.

Full and Quick: confirmed **as-is**, except Full gains the charter-ingestion seam.
