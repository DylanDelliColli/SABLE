---
description: Read queued SABLE messages, then show addressed coordination beads
---

# /inbox — SABLE Coordination Inbox

Read the file-backed payload queue FIRST. This ordering is load-bearing:
Codex workers cannot read `bd`, while every SABLE pane can read the host-global
queue. A bead-store failure must never hide an already-delivered file payload.

Run:

```bash
sable-inbox read --json
```

The command resolves managers/cockpit through `$SABLE_AGENT_NAME` (with the
legacy `$CLAUDE_AGENT_NAME` fallback) and workers through `$SABLE_BEAD`. If it
reports `CANNOT-ASSESS`, surface that result and do not call the queue empty.

Each returned object carries `id`, `sender`, `body`, and `enqueued_at`
atomically. `enqueued_at` is ordering evidence across the two delivery
channels: if you already acted on a direct `--interrupt` turn from the same
sender, compare it with that turn's `composed=` timestamp and surface any older
queued instruction instead of blindly applying it afterward. Read and handle
the body, then acknowledge that exact observed id only after its content is
safely present in this turn:

```bash
sable-inbox ack <message-id>
```

`read` is non-destructive. Never acknowledge an id before reading its body, and
never infer an id/body pairing from a second enumeration.

After the file queue, query addressed beads when `bd` is available and this
pane has a manager/cockpit identity:

```bash
bd ready -l "for-${SABLE_AGENT_NAME:-${CLAUDE_AGENT_NAME:-}}" --json --limit 0
```

Workers may be structurally unable to run that second command. Report the file
messages normally and label the bead leg unavailable; do not turn that into a
failure of the file queue.

For each addressed bead returned, show:
- Bead ID
- Priority (`P0` is urgent and is mechanically blocking new dispatches via the preempt hook)
- Title
- One-line summary of why it landed in your inbox (extract from description if obvious)

Format:

```
INBOX (OPTIMUS) — 3 items
  [P0] bd-147 — Rebase epic-foo: trivial conflict in foo.ts:42 (from chuck)
  [P2] bd-203 — Heads-up: Tarzan's bd-201 will touch shared utils
  [P3] bd-205 — While you're in there: small TODO in src/cache.ts
```

If both readable sources are empty, say so explicitly: "Inbox empty."

After listing, continue the normal operating cycle. The host-side inbox timer
will keep issuing count-only wakes while file messages remain unacknowledged.
