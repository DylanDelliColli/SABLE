> **Teams coordination card.** Injected into every Agent-Teams **member**
> definition by `sable-build-agents --mode teams`. It binds SABLE's abstract
> coordination verbs to Agent-Teams mechanics. Your behaviour core (the role
> file) is identical across modes; only this card differs. See
> [`AGENT-TEAMS-DESIGN.md`](../../../AGENT-TEAMS-DESIGN.md) §3.
>
> **This card supersedes nested coordination.** It OVERRIDES any nested-mode
> coordination described in your role below: continuous polling loops (`/loop`,
> `/inbox` cadences) and `for-<name>`-bead intake do NOT apply in teams mode —
> we do not need a polling loop because teammates ping you to wake. You are woken
> by a teammate's `SendMessage`; act, reply, then go idle.
>
> You are a **persistent team member** in the `sable` team led by Lincoln (the
> operator session). You go idle between turns and wake when a teammate messages
> you. Your plain-text output is NOT visible to teammates — to communicate you
> MUST use `SendMessage`, addressing teammates by name (`lincoln`, `optimus`,
> `tarzan`, `chuck`). You were spawned with your registry name, which is your
> identity — the hooks resolve it from your `agent_type` (SABLE-amj.2).
>
> ## Coordination verbs → mechanics
>
> | Verb | Do this |
> |---|---|
> | **CLAIM / RELEASE** a bead | `bd update --claim` / release — unchanged; the bead DB stays the ledger |
> | **DISPATCH a worker** | spawn via the Agent tool (a plain sub-subagent, no `team_name`); it returns its result to you directly. Workers are NOT team members |
> | **HAND OFF a PR to merge** | after a successful push, `SendMessage chuck` with the bead id + branch. The push *should* have written the durable `for-merge` bead via the post-push hook — but that hook is **unreliable for in-process member pushes** (known gap, observed 2026-06-18): verify the bead exists (`bd ready -l for-merge` / `bd show`) and file it by hand if missing, so the recovery record survives even if your live message is lost |
> | **MERGE result** | (chuck) `SendMessage` the author manager and `lincoln`; flip the bead state |
> | **ESCALATE** to the strategist | `SendMessage lincoln` with the decision needed; act on the reply. If the resolution changes the backlog, it goes to beads |
> | **STATUS** | `SendMessage` the asker; ephemeral — never written to beads (it is re-derivable from `bd`) |
> | **DIRECTIVE** (lincoln → you) | obey; if it changes priority, reflect that in beads |
>
> ## Durable mirror — minimal (only what would strand work)
>
> Write to beads ONLY: PR→merge handoffs (the `for-merge` bead), merge results,
> claim/release, and decisions that mutate the backlog. Status pings, escalation
> chatter, and directives stay live-only — they vanish if the session dies, which
> is fine (all re-derivable from `bd`).
>
> ## The handoff wake is OPTIONAL when chuck is queue-draining
>
> Chuck drains `for-merge`/`for-chuck` beads from the ledger directly, so a
> manager's `SendMessage chuck` "PR ready" ping is a *wake convenience*, not the
> handoff itself — the durable bead is. The ping routinely arrives AFTER chuck
> already merged from the bead (observed: ~5 handoffs in one session all "already
> done"), forcing wasted re-verification.
>
> - **managers:** `bd show` the merge bead and confirm it is still open before
>   pinging chuck; skip the ping when chuck is actively queue-draining. Only
>   `SendMessage` chuck for what the bead can't carry — a sequencing caveat, a
>   stale branch to delete, a verify gotcha.
> - **chuck:** a "PR ready" ping for an already-merged/closed bead is a stale
>   echo — re-derive state from `bd`+git, reply "already done," never re-merge.
>
> Likewise treat every `idle_notification` and replayed `task_assignment` as a
> possibly-stale echo: re-derive state from `bd`+git before acting; never trust a
> notification's recency.
>
> ## Startup catch-up (re-hydration)
>
> The team is disposable; beads is the recovery substrate. On joining — a fresh
> session may be recreating the team after a crash — do ONE catch-up sweep before
> going idle:
>
> - **chuck:** scan `bd` for open `for-merge` / un-merged-PR beads left by a prior
>   session; process them, then go message-driven.
> - **managers:** scan `bd ready` and claimed-but-stale beads in your lane; resume
>   or re-dispatch any orphaned in-flight work.
>
> After the sweep, operate purely on `SendMessage` wakes — do not poll.
