# Herdr as a fleet substrate — evaluation (2026-07-29)

**Status: EVALUATED, NOT ADOPTED. Deferred (SABLE-qirwv).**
Decision deferred by the operator until the Codex sandbox blocker (SABLE-82k8m)
is cleared, on the grounds that it blocks the fleet on *either* substrate.

Everything below is from the published docs at <https://herdr.dev/docs/> only.
Nothing has been installed, run, or benchmarked. No claim here is verified
against a live Herdr. Treat every "does X" as "is documented to do X".

---

## What it is

A mouse-first terminal multiplexer with first-class AI-agent support:

- **21 agent kinds** via `herdr agent start <name> --kind <kind>`, including
  both `claude` and `codex`.
- A **newline-delimited JSON socket API** over a unix socket
  (`~/.config/herdr/herdr.sock`, overridable via `HERDR_SOCKET_PATH` /
  `HERDR_SESSION` / `--session`).
- A CLI over the same surface, plus `herdr api schema --json` for the full
  protocol.

## The correction that matters

The overview page says state comes from *"lifecycle hooks when installed;
otherwise screen manifest"*, which reads as though installing an integration
buys authoritative state. **It does not, for either provider we use.** The
integrations page is explicit:

> **Claude Code:** "The hook reports Claude Code session identity to the local
> Herdr socket on session start. Claude Code state comes from Herdr's screen
> manifest detection."
>
> **Codex:** "The Codex hook reports session identity through the same local
> socket API used by other integrations. Codex state comes from Herdr's screen
> manifest detection."

The integrations report **session identity** (which buys resume). **State stays
screen-scraped** from a bottom-buffer snapshot matched against TOML manifests.

So adoption **relocates** our scraping into upstream-maintained data. It does
not eliminate it. That is still a gain — manifests are data rather than
constants compiled into our library, they cover 15+ agents, and someone else
owns keeping them current — but it breaks the same way when a TUI shifts, and
the fix timeline moves upstream.

**Corollary: do not adopt Herdr to fix detection bugs.** See "The categorical
fix is ours" below.

## What it genuinely buys

Ranked by what we cannot cheaply replicate:

1. **Atomic prompt delivery.** `herdr agent prompt <target> <text> [--wait]
   [--until STATUS]` — documented to honour *"live bracketed-paste mode and
   submit text plus encoded Enter atomically."* Our `deliver_text` types,
   verifies, sends Enter, verifies, and retries — and still misreported
   delivery to Chuck on 2026-07-29 (SABLE-yuwrs). This is a race we would
   otherwise have to engineer our way out of.
2. **A headless control plane.** `send-keys` + `capture-pane` fundamentally
   requires something to be *rendering* for output to mean anything. A JSON
   socket does not. This is the difference between a fleet you supervise and
   one you can drive from cron, CI, or a webhook — i.e. the VPS story.
3. **Push instead of poll.** `events.subscribe` with `pane.agent_status_changed`
   (also `pane.created/updated/closed/focused/exited`, workspace and tab
   events) replaces the stall probe, reap loop, and worker-status polling.
4. **`blocked` detection for unattended runs.** *"Herdr only marks `blocked`
   when the live bottom-buffer snapshot matches known visible approval,
   question, or permission UI."* That is precisely the signal needed to page a
   human at 3am, and it bears directly on the walk-away validation goal
   (SABLE-bldh.7), currently gated on nothing being able to notice a stalled
   fleet.
5. **Not maintaining multiplexer glue.** A large fraction of
   `bin/sable_pane_lib.py` is archaeology of two vendors' TUI redraws, accreted
   one incident bead at a time. It will never stop growing while we own it, and
   each new provider multiplies it.
6. **Debuggability.** `herdr agent explain <target> --json` reports *why* a
   state was classified. The 2026-07-29 spawn failure was
   `not in composer posture` with no way to see why; diagnosing it required
   reverse-engineering SGR-2 dim styling from raw captures.

## The categorical fix is ours, and is substrate-independent

Stop **inferring** semantic state from rendered pixels; have the agents' own
lifecycle hooks **report** it. SABLE already installs a hook graph into both
providers.

That fix works on tmux today and would delete most of `sable_pane_lib.py`'s
heuristic mass — spinner regexes, elapsed-time regexes, dialog posture, overlay
evidence, session-limit banners, and the SGR-2 ghost discriminator added in
SABLE-6c391 — regardless of which multiplexer is underneath.

**Herdr's relevance here is that it already exposes the seam for it:**

```
herdr pane report-agent <pane_id> --source ID --agent LABEL \
  --state idle|working|blocked|unknown [--message TEXT] [--seq N] \
  [--agent-session-id ID] [--agent-session-path PATH]
```

This inverts the usual adoption story: we would not be a *consumer* of Herdr's
detection, we would be a *provider* of it — supplying the lifecycle authority
its own claude/codex integrations lack.

**Sequencing consequence:** do NOT build a bespoke hook-reported-state
mechanism on tmux and then port it. If the spike holds up, implement
hook-reported state directly against `report-agent`.

## What it does NOT fix

Orthogonal — Herdr launches the process; these stay ours:

| Bead | Why unaffected |
|---|---|
| **SABLE-82k8m** | Codex `workspace-write` sandbox blocks `.git` and `~/.claude` writes. Sandbox flags are ours. **This is the current fleet blocker.** |
| SABLE-1zau6 | `sable-mode handoff show` planning-gated |
| SABLE-cftx3 | A Codex SessionStart hook returns invalid session-start JSON |
| SABLE-4gxef | Non-hermetic tests reading live mode-state |
| SABLE-gzxlk | Stale `CLAUDE_AGENT_NAME` inherited into Codex panes |

## Scope of the swap (measured 2026-07-29)

- **20** non-test files invoke tmux directly.
- `bin/sable_pane_lib.py` is **1007 lines** carrying **29** predicate/regex
  definitions.
- **8** pane options in use: `@sable_role`, `@sable_bead`, `@sable_status`,
  `@sable_lane`, `@sable_class`, `@sable_provider`, `@sable_repo`,
  `@sable_deliverable`.

**Nothing in the methodology layer moves.** The receipt, lane partitioning,
merge gate, worker lifecycle, TDD/bead hooks and contracts are all
substrate-independent.

This is a **substrate swap, not a topology change**. It is not on the order of
subagents→tmux, which redefined what an agent *was* and forced the
manager/worker/seat model into existence. The two weeks of tmux operating
experience is almost entirely methodology learning, and carries over intact.

## Known hazard: the role registry has no clean Herdr home

Most likely thing to be discovered late, so settle it first.

Our `@sable_*` pane options are **load-bearing routing state**: `sable-msg`
resolves role→pane through them, `sable-worker-status` and the registry read
them, and the spawn helpers refuse on provider mismatch by comparing them.

The obvious analogue is:

```
herdr pane report-metadata <pane_id> --source ID [--token NAME=VALUE] ...
```

…but the docs describe metadata as **display-only (non-semantic)**, *"normalized
before storage"* (whitespace trimmed, control characters removed) and **capped
at 80 characters**. Storing authoritative routing state in a display-only,
silently-truncating field is a semantic downgrade, and fails quietly on an
unusual value — `@sable_deliverable` can carry a path.

Only `herdr agent rename <target> <name>` is a true equivalent, and it covers
`@sable_role` alone (names constrained to `[a-z][a-z0-9_-]{0,31}`, unique among
live agents).

**Likely answer:** keep a SABLE-owned registry keyed on `HERDR_PANE_ID`, which
the docs say the process *"keeps its launch-time"* value of across pane moves,
making it a stable key. Confirm against `herdr api schema` before committing.

## Risks to clear before adopting

1. **Hook-file collision.** `herdr integration install codex` writes
   `~/.codex/herdr-agent-state.sh`, updates `hooks.json`, and sets
   `[features] hooks = true` in `config.toml`. The claude one writes
   `~/.claude/hooks/herdr-agent-state.sh` and updates `settings.json`. **These
   are the exact files SABLE's installer owns.** The docs do not state whether
   existing user hook entries survive. Test against a redirected `CODEX_HOME` /
   `CLAUDE_CONFIG_DIR` before touching live config.
2. **Maturity unverified.** No data on release cadence, issue backlog, bus
   factor, or whether the codex manifest tracks codex-cli 0.146.0. This is a
   substrate bet, not a library bet — if it stalls, we are stranded.
3. **Registry hazard** (above).

## The spike (do this before deciding)

Bounded, an afternoon. Install Herdr, redirect `CODEX_HOME` to a scratch dir,
install the codex integration *there*, launch one Codex pane, and answer:

1. Does `agent.wait --until idle` report correctly on a pane showing a **ghost
   placeholder** in its composer? (The SABLE-6c391 case.)
2. Does `agent prompt` land atomically in a **busy** pane and report success?
   (The SABLE-yuwrs case — queueing only happens when the recipient is
   mid-turn, which is exactly when our detection failed.)
3. Can one of our existing hooks drive `pane report-agent` to supply
   authoritative state?
4. Where does the **role registry** live? (Hazard above.)

Answer those with evidence and the adoption question answers itself.

## References

- Docs: <https://herdr.dev/docs/> — Agents, Integrations, API, Socket API, CLI
  reference
- Tracking bead: **SABLE-qirwv** (deferred)
- Blocker to clear first: **SABLE-82k8m**
- Bugs motivating the evaluation: SABLE-6c391, SABLE-yuwrs, SABLE-0ro7r
  (closed invalid)
