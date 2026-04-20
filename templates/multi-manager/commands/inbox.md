---
description: Show your addressed coordination inbox (for-<self> beads)
---

# /inbox — Multi-Manager Coordination Inbox

Query the bead system for items addressed to your manager identity.

Run:

```bash
bd ready -l "for-${CLAUDE_AGENT_NAME}" --json
```

If `$CLAUDE_AGENT_NAME` is not set, this slash command is not applicable — you are not running in a multi-manager identity context. Note that to the user and exit.

For each item returned, show:
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

If inbox is empty, say so explicitly: "Inbox empty."

After listing, do not auto-act on items. Wait for the human or continue your normal cycle. The hook-driven inbox injection will continue surfacing items between bash calls automatically.
