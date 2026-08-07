---
name: sable-shift-change
description: Write and validate a durable shift-change handoff when an agent or session is saturated, being recycled, compacted, or transferring responsibility to a fresh agent. Use for a shift report, session handoff, context offload, fresh-agent document, manager recycle, or any handoff where direction changed and the code, documentation, tracker, or runtime may disagree.
---

# SABLE Shift Change

A shift report is a verified map to durable state, not a transcript and not a substitute for durable state. Write the smallest report from which a fresh agent can act safely without conversational repair.

Bound every claim to the evidence supporting it. Never expand a true local observation into an unqualified statement about the repository, fleet, or current state.

## Choose the durable home

Follow the repository's instructions for tracker use, report location, commit, and push. Prefer this order of authority unless the repository defines another:

1. Executable state and authoritative records.
2. Landed decisions and code at exact revisions.
3. The issue tracker and version-control history.
4. The shift report as an index and interpretation layer.
5. A live message as a wake-up pointer only.

Do not put essential state only in a pane, chat message, uncommitted file, or agent memory. When the repository provides a recovery command, make it the incoming agent's first command. In SABLE, run `sable-recover --repo "$PWD"`; treat the report as a hint over the recovered lane state, not as its replacement.

## Build the report from evidence

Freeze new design work while authoring the handoff. Then reconstruct state from the repository, tracker, version control, and runtime rather than from memory.

Execute each load-bearing probe and paste or summarize its actual result. Use bounded probes with positive controls where an absence matters. A command written in prose is not evidence that the command ran.

Match the probe's object and logic to the claim. Containment in `HEAD` does not prove containment in a remote branch, and observing one leg of an `A or B` release condition does not prove the combined condition.

Classify information before writing:

- **Embed:** the objective and success condition; decisions and reversals; the authority map; exact next action; hazards; holds; unknowns; and ownership. These are the cargo a fresh agent cannot infer reliably.
- **Link precisely:** commits, issue IDs, files, sections, test artifacts, and decision records. Give each link a one-line semantic claim. An identifier without what it proves forces rediscovery.
- **Recompute on intake:** branch containment, upstream equality, dirty files, live agents or panes, leases, locks, queue state, and CI status. Record the authoring-time observation, time, scope, and probe, but require the incoming agent to rerun it.

Distinguish these states explicitly:

- **Landed:** committed and, where required, pushed. Name the object and what it proves.
- **In flight:** claimed or running, with owner and observable status.
- **Uncommitted:** present only in a working tree or external scratch area. Name the exact location and owner.
- **Planned:** agreed but not yet implemented. Do not describe it in present tense.
- **Parked or held:** intentionally inactive, with the release condition and decision owner.
- **Report prose:** interpretation in this document, not implementation evidence.

## Write sections in this order

### 1. Identity and snapshot boundary

State the repository, branch, outgoing role, intended incoming role, report path, and observation time. If useful, name a **pre-report base SHA**. Never call that SHA the current HEAD: committing the report changes HEAD and falsifies the sentence.

Resolve the report's own commit only after committing it, for example with a path-scoped history query. Verify push or branch containment separately when the repository requires it.

### 2. Read-first authority map

Name what is authoritative for each relevant subject. If code and documentation intentionally disagree, use the bounded authority-inversion form below. Do not write a repository-wide rule such as “trust docs, ignore source.”

### 3. Objective and success condition

State the deliverable, why it exists, what completion means, and what is expressly outside the current task. Preserve product intent, not just the next edit.

### 4. Direction changes and settled decisions

List changes chronologically. For each one, state:

- the prior direction;
- the replacement direction;
- the reason or evidence;
- the exact durable decision object; and
- what it supersedes.

Mark decisions the incoming agent should apply rather than re-litigate. Mark unresolved product choices separately.

### 5. Durable work state

Use the landed, in-flight, uncommitted, planned, and parked categories. Name owners and exact objects. Record pre-existing dirty files so the incoming agent does not mistake them for abandoned work or overwrite them.

### 6. Ownership and boundaries

State who owns each active lane, which areas are off limits, what coordination is pending, and which live messages still need delivery. A report sent to a stale pane is not a handoff; the durable pointer must reach the role that will actually restart the lane.

### 7. Hazards, holds, and negative instructions

Explain what must not happen, why, the precise scope, and the release condition. Avoid permanent-sounding warnings with no owner or exit criterion.

### 8. Incoming boot sequence

Give three to five exact read or probe commands in order, followed by one concrete first act. Prefer commands that independently establish state over commands that merely print the report's assertions.

### 9. Verification ledger and known defects

For every load-bearing or mutable claim, record:

| Claim | Evidence or probe | Observed result and time | Incoming action |
|---|---|---|---|
| What is claimed, with scope | Exact authoritative object or executable probe | Actual bounded result | Trust as durable, rerun, or escalate on mismatch |

Keep discovered report defects visible in a correction section. State the original claim, the correction, and the lesson. Do not silently polish a failed handoff into apparent perfection; the failure is evidence for the next author.

### 10. Closeout pointer

After the report is durable, record or send the smallest reliable pointer to it, then end the outgoing session. Follow the repository's normal issue close, commit, sync, push, and handoff rules. Do not leave the saturated agent running as a shadow source of truth.

## Handle intentionally misleading source

When a redirected design leaves old implementation in place until replacement, create an explicit and temporary authority inversion:

```markdown
### Authority inversion — ACTIVE

- Subject: <the exact behavior or design question>
- Authoritative: <files/records and exact revisions, with what each proves>
- Knowingly stale: <bounded paths or modules>
- Still valid there: <facts the stale source continues to prove>
- No longer proved there: <facts the reader must not infer>
- Why retained: <for example, deletion-by-replacement>
- Starts at: <decision object or revision>
- Ends when: <observable condition, issue closure, or replacement revision>
- Verify now: <executable probes and expected bounded results>
- Outside this boundary: <normal authority rules; disagreement is a defect>
```

Constrain the inversion by both **subject** and **path**. Name unaffected code explicitly when confusion is likely. Any surprising disagreement outside the boundary is a defect to investigate, not permission to dismiss source generally.

The report must make clear whether the replacement is landed, planned, or merely described by the report itself. Where a probe and prose disagree, the probe wins and the report needs correction.

## Split verification ownership

The outgoing agent owns truthful construction:

- Re-measure volatile inherited facts immediately before recording them.
- Verify every load-bearing claim it can verify; do not defer accuracy to the incoming agent.
- State probe bounds and demonstrate visibility with a positive control when absence matters.
- Label the pre-report base honestly, then resolve and verify the report object after commit.
- Record dirty and external state that the branch cannot prove.
- Append visible corrections when a claim fails after landing.

The incoming agent owns independent intake verification:

- Read repository instructions and run the repository's recovery command first when one exists.
- Rerun every mutable or load-bearing probe before consequential work.
- Compare results with the report instead of merely confirming that the report exists.
- On mismatch, stop only the affected action, preserve the discrepancy, and correct or supersede the report durably.
- Apply settled decisions from their named authoritative objects; investigate contradictions outside any bounded inversion.

This is intentionally asymmetric: the incoming check does not excuse weak outgoing evidence, and outgoing confidence does not replace a fresh check after time has passed.

## Reject handoffs that fail the fresh-agent test

A bad handoff looks plausible:

```markdown
We changed direction. Docs are current; code is not. Scribe and Ledger are gone.
dafbed3 and 5ad9f4d landed. Tests pass. Continue yx3.
main equals origin/main at b714451.
```

It fails because:

- “docs” and “code” have no subject or path boundary;
- the commit and issue identifiers have no semantic claim or exact next action;
- “tests pass” names neither suite, revision, result, nor time;
- it omits in-flight, uncommitted, parked, ownership, and dirty state;
- it gives no end condition for distrusting source; and
- committing the report makes its mutable HEAD assertion stale by construction.

This last defect can recur inside its own repair: replacing one current-HEAD sentence while leaving “verified at HEAD=...” in a probe preamble repeats the same false claim. Eliminate the mutable assertion; do not relocate it.

The pattern is **unbounded confidence**. “Most named docs agreed at one revision,” “upstream matched when probed,” or “these paths are intentionally stale” can each be true. Broadening any of them into “the docs are correct,” “the branch is current,” or “documentation wins over source” makes the report false beyond its evidence.

Before accepting the handoff, require the incoming agent to answer from the report and durable objects, without conversational help:

1. What is the objective and success condition?
2. Which exact objects are authoritative for the next decision?
3. What is landed, in flight, uncommitted, planned, and parked?
4. What must not happen, over what scope, and until what observable event?
5. What is the first command and first consequential action?
6. Which claims must be recomputed, and what happens if they differ?

If the incoming agent cannot answer, repair the durable artifact instead of explaining it in chat. If it can answer but a probe disagrees, the handoff has successfully exposed a real state change or defect; preserve that evidence and update the record.

## Keep the machinery bounded

Do not turn a shift report into a second tracker, a session transcript, or a retrospective essay. Omit logs and history that do not change the incoming agent's first decisions. Link to durable detail rather than copying it. Add a field or check only when it prevents a concrete fresh-agent failure.
