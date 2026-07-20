# Discovery: Pure-OpenAI Autonomous Cloud Fleet

**Mode:** `/sable-discover` (planning, no beads authored)
**Date:** 2026-07-20
**Branch:** `cloud-deploy`
**Status:** Discovery complete through architecture shape. Next action = Phase 0 spike (see bottom).
**Beads:** none authored by request. Nothing here has been filed.

---

## 1. The decision (locked by the operator)

Build a **pure-OpenAI** autonomous agent fleet on a cloud VPS, running side projects
unattended. **Claude Code remains the local interactive daily driver** and is not
part of the cloud build.

**Rationale, in the operator's words:** *"I don't trust that Anthropic won't ban this
practice in the near future, so I don't want to build it around their tech."*

This is a **platform-risk bet, not a factual claim** — it survived a premise challenge
(see §2) and was reaffirmed. Do not relitigate it in a future session. Hedged designs
that keep Claude Code in the cloud critical path defeat the stated purpose.

Explicitly **rejected** during discovery:

| Option | Verdict | Why |
|---|---|---|
| Claude Code on VPS (Anthropic sub) | rejected | Cheapest + easiest, but retains the platform dependency the operator is buying out of |
| Proxy: Claude Code → OpenAI backend | rejected | See §4 — 502-instead-of-429 and duplicate tool calls make it unfit for autonomy |
| `codex mcp-server` hybrid (Claude orchestrates, Codex executes) | rejected | Still Claude-dependent at the orchestration layer |
| Full dual-runtime SABLE (managers + workers on both) | rejected | 3–6 weeks + permanent dual-maintenance tax on the daily driver |

---

## 2. The premise challenge (resolved, recorded to prevent re-running it)

I challenged the belief that "Anthropic has banned subscription-as-API."

**What Anthropic actually enforced** (clarified 2026-02-20, enforced 2026-04-04) targeted
**third-party clients** wearing Claude subscription OAuth — OpenClaw, OpenCode, Cline,
Roo Code. The line drawn is **client identity**, not **machine location**. Counter-evidence
that a blanket headless ban does not exist: Anthropic ships Claude Code on the web,
first-party cloud sandboxes, and a GitHub Action — all subscription-backed, all headless.

**Unverified and left unverified by choice:** current status of `claude setup-token` /
`CLAUDE_CODE_OAUTH_TOKEN`, the first-party headless-subscription mechanism.

**Operator's response:** allowed-today ≠ safe-to-build-on. The bet is about *future*
policy risk, which no amount of present-tense verification resolves. **Premise challenge
closed.** If a future session feels tempted to re-open this, the answer is already given.

---

## 3. The core architectural insight

> **This is not a port. It is a deletion.**

A large fraction of SABLE's complexity exists to work around **one Claude Code
limitation: it has no programmatic driver.** To make it act, you must *type at it*.
That single constraint is the origin of:

- `bin/sable_pane_lib.py` — ~500 lines scraping `❯` composer glyphs, the literal string
  `"esc to interrupt"`, spinner chars `⠋⠙⠹✻✽✳`, and "trust this folder" / "bypass
  permissions mode" startup dialogs
- `sable-msg`'s `tmux send-keys` transport, the l7uv self-heal, `submitted_own_turn` /
  `dispatch_landed` polling, `_already_pending()` double-type guard
- Warm-pane topology, pane readiness gates, `@sable_status` lifecycle wrapper
- `sable-worker-status`'s `_composer_box` / `has_pending_input` scraping and the
  `send-keys C-u` composer clear before reaping

**Codex has real programmatic drivers:**

- **`codex exec`** — non-interactive, JSONL event stream (`thread.started`,
  `item.completed`, `turn.completed` with usage), real exit code, `--output-schema`,
  `-o/--output-last-message`, `--sandbox {read-only|workspace-write|danger-full-access}`,
  `-a/--ask-for-approval never`, `codex exec resume --last`
- **`codex app-server`** — bidirectional JSON-RPC 2.0, same core engine as the TUI;
  schemas are generatable via `codex app-server generate-ts` / `generate-json-schema`

So the terminal-emulation layer is **thrown away, not reimplemented.** That is a refund,
not a migration cost.

---

## 4. Research findings (2026-07-20, verify freshness before relying)

Docs note: `developers.openai.com/codex/*` now 308-redirects to `learn.chatgpt.com/docs/*`.
Latest Codex CLI stable at time of research: **0.144.6 (2026-07-18)**.

### 4.1 Feature parity — better than expected

The assumed disqualifier ("Codex has no hook system") is **false as of 2026**. Codex ships:

- **10 hook events:** SessionStart, SubagentStart, PreToolUse, PermissionRequest,
  PostToolUse, PreCompact, PostCompact, UserPromptSubmit, SubagentStop, **Stop**.
  Config: `~/.codex/hooks.json`, `.codex/hooks.json`, or inline `[hooks]` in `config.toml`
- **Stop hook contract is literally the tdd-gate:** `{"decision": "block", "reason": "..."}`
- **PreToolUse deny:** `{"hookSpecificOutput": {"permissionDecision": "deny", ...}}` or
  exit code 2 + stderr. Exit-code semantics match Claude Code (0 pass / 0+JSON structured /
  2 block / other non-zero = hook failure but continue)
- **Agent Skills — the same open standard** (`SKILL.md`, `~/.agents/skills`,
  `.agents/skills`). The 8 SABLE skills (~2,665 lines) are near-free to relocate
- **Subagents** (`~/.codex/agents/*.toml`), **MCP** (client + server), **AGENTS.md**
  (hierarchy: global → git-root → intermediate → cwd, `.override.md` variants, 32 KiB cap
  via `project_doc_max_bytes`), ~40 slash commands + `$CODEX_HOME/prompts/*.md`

**Real gaps:**
- Only `command` handlers execute — `prompt` and `agent` handlers are parsed but **skipped**
- `async` parsed but **not supported**; every hook is synchronous and blocking
- `permissionDecision: 'ask'`, `continue: false`, `stopReason`, `suppressOutput` parsed
  but **non-functional**. PreToolUse acts on **`deny` only**
- **Hosted tools (e.g. WebSearch) bypass the hook path entirely** — interception is not total
- ~19 Claude Code events have no Codex equivalent (no SessionEnd, PostToolUseFailure,
  FileChanged, TaskCreated/Completed, WorktreeCreate/Remove, CwdChanged)

### 4.2 Auth — device flow is sanctioned

- **`codex login --device-auth`** is the documented headless path. Prints a code, approve
  at chatgpt.com from any browser. No port-forwarding, no file copying.
- Credential at **`~/.codex/auth.json`** (plaintext) or OS keyring; controlled by
  `cli_auth_credentials_store` = `file` | `keyring` | `auto`
- **Auto-refresh during use.** Access tokens ~10 days; refresh token regenerates.
  **Sessions idle >8 days go stale** → a weekly keep-alive job is sufficient
- The ChatGPT OAuth token is **not** an API key — it cannot call `api.openai.com`
- Business/Team workspaces **can have device-code auth disabled by an admin**
  (openai/codex#9253). Personal Plus/Pro unaffected
- `codex login --with-access-token` / `CODEX_ACCESS_TOKEN` exists for
  *"trusted scripts, schedulers, and private CI runners"* — **Business/Enterprise only**

### 4.3 Quota — the binding constraint

Per-5-hour-window messages (learn.chatgpt.com/docs/pricing):

| Plan | GPT-5.6 Sol | Terra | Luna |
|---|---|---|---|
| Plus ($20) | 15–90 | 20–110 | 50–280 |
| Pro 5x ($100) | 75–450 | 100–550 | 250–1400 |
| Pro 20x ($200) | 300–1800 | 400–2200 | 1000–5600 |
| Enterprise/Edu (flexible) | **no fixed rate limits** | | |

- The 6x spread within each cell is because consumption is driven by **reasoning time and
  token volume**, not message count (since 2026-04-09). The table is near non-predictive
  for agentic workloads
- **Local and cloud share one pool**, also shared with ChatGPT Work — daytime interactive
  use competes with the fleet
- **No numeric weekly cap is published for any plan.** Largest documentation gap; you
  cannot capacity-plan from official docs
- **No documented concurrency cap** — but community evidence converges on **3–6 concurrent
  agents** as the practical ceiling per subscription. One measured report: *"7 prompts used
  my entire 5 hour limit, in 10 minutes."*
- `agents.max_threads` default 6, with bugs where the real cap is lower (#33039, #33447)
- **Business Codex seats closed to new workspaces 2026-06-24** — existing grandfathered
- **Unconfirmed:** a 2026-07-12 X post from OpenAI's Codex product lead reportedly announced
  temporary removal of the 5-hour window for Plus/Pro/Business. **Not reflected on the
  official pricing page.** Do not architect against it.
- **Contradiction flagged:** Business measured ~220% faster drain than Plus despite
  identical published limits

### 4.4 Terms of service — recorded unsoftened

No explicit prohibition on headless subscription-driven Codex; OpenAI documents it. But:

From `learn.chatgpt.com/docs/auth/ci-cd-auth`, verbatim:
> **"The right way to authenticate automation is with an API key. Use this guide only if
> you specifically need to run the workflow as your Codex account."**
> **"Use one `auth.json` per runner or per serialized workflow stream."**
> **"Do not share the same file across concurrent jobs or multiple machines."**
> "Do not use this workflow for public or open-source repositories."

That last-but-one line is the only official statement on the parallel-fleet question and
**it is negative** — though framed as refresh-token rotation hygiene, and N processes
sharing one `CODEX_HOME` on **one** machine is materially different from N machines each
holding a credential copy.

Terms of Use (eff. 2026-01-01) prohibit sharing credentials, "automatically or
programmatically extract data or Output," and circumventing rate limits. The
extract clause read literally would catch OpenAI's own CLI, so the operative reading must
be anti-scraping — **but OpenAI has never confirmed this.** Asked point-blank in
openai/codex#8338 (2025-12-19), a maintainer answered the licensing half and **left the
ToS question unanswered.** No safe harbor.

**Enforcement reality — the material risk.** Active suspension wave, appeals denied:
- 2026-07-16: Pro account auto-banned mid-session at 4AM on the **official** CLI, no proxy;
  appeal denied
- Account deactivated overnight while Codex was running
- One Pro + three Plus accounts on one team suspended the same day

None confirmed automation-triggered — OpenAI states no reason. But the caught profile is
**unattended overnight runs**, bans are instant and automated, and one flagged identity can
take out a team. **Documented permission is not protection from an abuse classifier.**

### 4.5 Why the proxy path was rejected

`raine/claude-code-proxy` (active, 386 stars) does PKCE OAuth against `auth.openai.com`
using the Codex client ID and **works with a ChatGPT subscription, no API key**. Rejected
anyway — the failure modes are precisely the autonomous ones:

- **Quota exhaustion returns opaque `502 api_error` instead of `429 rate_limit_error`**
  (issue #65, 2026-07-19) — workers cannot distinguish "back off" from "broken"
- `count_tokens` doesn't exist; Claude Code silently falls back to estimation, so
  compaction goes approximate exactly where long unattended runs need precision
- `CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK=1` is **mandatory** — without it the
  streaming retry path **duplicates tool calls**. In a merge worker that is destructive
- Reasoning blocks dropped in transit; `parallel_tool_calls` hardcoded false on one path (#36)
- **Supply chain:** LiteLLM PyPI 1.82.7/1.82.8 shipped a credential stealer (2026-03-24).
  Clean: ≤1.82.6 or ≥1.83.0. A translation proxy sees every token of every request

Moot regardless — the proxy keeps Claude Code, which the §1 decision excludes.

### 4.6 Pricing

**Subscriptions:** ChatGPT Plus $20 · Pro 5x $100 · Pro 20x $200 · Business $25/user
($20 annual, min 2) · Enterprise unpublished. (Claude Pro $20 · Max 5x $100 · Max 20x $200
— for reference only; not part of this build.)

**Codex credits:** metered since 2026-04-02. **1 credit = exactly $0.04** (verified
arithmetically against the API rate card). Typical burn 5–40 credits/message = $0.20–$1.60.
Per-plan included allowances are **not published anywhere reachable**.

**API per-token (USD/1M), OpenAI:** gpt-5.6-sol $5.00 / $0.50 cached / $30.00 out ·
terra $2.50/$0.25/$15.00 · luna $1.00/$0.10/$6.00 · gpt-5.4-mini $0.75/$0.075/$4.50.
The dedicated `-codex` model line is **stale** (gpt-5.3-codex); Codex now runs
GPT-5.6 Sol/Terra/Luna.

> **Cost trap worth designing around:** OpenAI bills requests over **272K input tokens at
> 2x input / 1.5x output, applied to the entire request, not the overage.** Fat-context
> workers get expensive nonlinearly. Keep dispatch briefs tight and worktrees narrow.

**VPS (16 GB / ~8 vCPU class):**

| Provider | Plan | vCPU | RAM | $/mo |
|---|---|---|---|---|
| **Hetzner EU** | **CX43** | 8 | 16 GB | **$18.49** |
| Vultr (US) | vhp-8c-16gb | 8 | 16 GB | $96.00 |
| AWS Lightsail | 16 GB mem-opt | 2 burstable | 16 GB | $74.00 — **avoid** |
| DigitalOcean | CPU-Opt 16GB | 8 | 16 GB | $168.00 |
| Fly.io | performance-8x | 8 | 16 GB | ~$340.66 |

**Staleness warning:** Hetzner raised prices twice in 2026 — ~37% on Apr 1, then Jun 15
where **CPX/CCX more than doubled (+113–176%)** citing the DRAM shock. Most
"Hetzner is 4x cheaper" articles (incl. June 2026 ones) **predate this and are wrong.**
The CX line only rose ~33%, which is why CX43 is the pick. Existing servers are
grandfathered but **rescaling reprices**. Caveat: CX/CPX carry fair-use terms; if agents
are genuinely CPU-pegged rather than blocked on API calls, budget CCX23 ($101.49).

**Target running cost: ~$120/mo** = Hetzner CX43 $18.49 + ChatGPT Pro 5x $100.
Plus at $20 will not sustain even one worker.

---

## 5. Target architecture

### 5.1 Split on the mode boundary — a seam SABLE already has

> **Planning = local, Claude Code, human-in-the-loop.
> Execution = VPS, Codex, autonomous.**

The VPS **never enters planning mode**, so it needs none of:

- Lincoln's cockpit, the substage state machine, the interlock's planning legs
- The producers — sherlock, columbo, gaudi, victor
- **All six `PreToolUse`/`Agent` hooks** (`pre-dispatch-model-check`, `-claim`, `-overlap`,
  `-preempt`, `-refresh`) — the least portable cluster in the system, and it is
  planning/dispatch-side
- Planning skills (`/sable-plan`, `/sable-discover`, `/gaudi`, `/columbo`)

Scope collapses to **execution mode only** — roughly a third of SABLE's surface, and the
most portable third.

### 5.2 No LLM managers — deterministic supervisor instead

Optimus/Tarzan/Chuck are LLM agents in warm panes substantially because they must
**converse with the operator**. Autonomous means no conversation. Their actual job — poll
`bd ready`, respect lane filters, enforce worker caps, spawn, watch, reap, retry, escalate
— is **deterministic control flow currently written as prose in a role card and executed
unreliably by an Opus-pinned model in a pane you must screen-scrape to know is alive.**

On the VPS, write it as code. Supervisor daemon:
`poll beads → claim → worktree → codex exec → parse JSONL → gate → push → close → repeat`

Wins: **all quota goes to workers** (can't afford 2 of 3–6 slots on middle management);
deterministic failure (exit codes, not wedged panes — cf. the `h0jw`-class beads);
unit+integration testable; cheaper and faster to build than porting role cards.

Merge serialization: `sable-merge-gate` is **pure git/gh/bd with zero Claude coupling** —
the supervisor calls it directly. LLM judgment on merges is a nice-to-have to add back
after the loop is trustworthy. **Exactly one merge serializer**, always.

### 5.3 Inventory

| | Component |
|---|---|
| **Survives verbatim** | `bd`/beads work substrate · git worktree-per-bead, branch naming, rebase discipline · `sable-merge-gate`, `sable-dolt-push`, `sable-reconcile-handoffs`, `sable-recover`, `sable-fixture-tripwire`, `sable-docker-preflight` (all pure git/gh/bd/dolt) · the **methodology**: Fresh Agent Test, unit+integration gating, TDD-first, mandatory issue discovery |
| **Near-free port** | 8 skills (~2,665 lines) — same open Agent Skills standard · `CLAUDE.md` → `AGENTS.md` · role cards → system prompts / skill content |
| **Deleted, not ported** | `sable_pane_lib.py` TUI scraping · `sable-msg` send-keys transport · pane readiness/busy/startup-dialog handling · `sable-worker-status` composer scraping · `sable-spawn-manager` · `@sable_status` lifecycle wrapper · warm panes as topology. **The most brittle ~20% of the codebase.** |
| **Must be built** | **The supervisor daemon** (the one real new component) · Codex `hooks.json` equivalents for tdd-gate, pre-push test, tree-claim, read-guard · auth lifecycle (`codex login --device-auth` + weekly keep-alive) · **budget governor** · observability for a system nobody watches |

**tmux becomes optional** — keep it for attach-and-watch observability, but no longer
load-bearing. Strict reliability improvement.

### 5.4 Repo strategy

Keep it **in the SABLE repo**. Reuse `sable-merge-gate`, `sable-dolt-push`, and the bd
conventions **verbatim**; add a **separate executor path** rather than making the pane
tooling dual-runtime. Making the daily driver dual-runtime doubles the complexity of the
thing you depend on to get work done.

---

## 6. Effort

| Phase | Scope | Estimate |
|---|---|---|
| **0 — Spike** | One `codex exec` worker, handed a real dispatch brief, completes one bead end-to-end: worktree → implement → tests green → branch push → `bd close`. No supervisor, run by hand, **on the laptop — no VPS needed** | **2–3 days** |
| **1 — Loop** | Supervisor daemon: claim → spawn → parse JSONL → gate → merge-gate → close. Single worker, serial | **1–2 weeks** |
| **2 — Fleet** | Concurrency to quota ceiling, worker caps, retry/backoff, stuck detection, budget governor, kill switch | **1–2 weeks** |
| **3 — Walk-away** | Failure recovery, notification-when-it-needs-you, hardening against overnight failure modes | **1–2 weeks** |

**3–5 weeks of fleet-time to trustworthy unattended operation**; usable single-worker loop
inside two weeks.

---

## 7. Risks

1. **Do hooks fire in `codex exec`?** — **UNVERIFIED.** Secondary sources say yes; official
   docs do not explicitly confirm; DeepWiki source analysis showing exec shares the core
   engine makes it near-certain but is not primary. **If they don't, gates move into the
   supervisor** (it runs tests, checks the diff, decides to push) — arguably the more robust
   design anyway, since a verifying supervisor is harder to prompt around than a hook.
   **Do not pass `--ignore-user-config`**, which skips `config.toml` hooks.
2. **No fallback by design.** Single-provider risk is accepted deliberately. Mitigation is
   not a second provider — it is keeping **state independent of runtime**: work lives in
   beads and git, the box holds no unique copy of anything, account death costs throughput
   not history. Already true architecturally; do not erode it.
3. **Opaque quota + unbounded autonomous burn.** No published weekly cap. A fleet can eat a
   week's allowance overnight with nothing to show. **Budget governor is not a Phase 3
   nicety** — track credits ($0.04 each), hard-stop at a set ceiling, make the stop loud.
4. **Ban wave.** Most recent report 4 days before this writing; official CLI, no proxy,
   appeals denied; the caught profile is unattended overnight runs — i.e. exactly this
   build. Budget the account as **something that may die without explanation**.
5. **Model behavior is unknown.** Skills, gates, and prompts are tuned on months of Claude
   failure modes. GPT-5.6 Sol fails differently. Expect prompt-level rework in Phases 0–1;
   budget it rather than treating it as slippage.

---

## 8. Thesis note

The operator's stated purpose for SABLE is a **portable opinion layer over a swappable
engine**, not a Claude Code extension. **This build is the first real test of that claim.**
Everything that survives the port is genuinely the opinion layer; everything that dies was
engine-specific plumbing mistaken for architecture. Valuable as an audit regardless of
whether the VPS earns its keep.

---

## 9. NEXT ACTION — Phase 0 spike

The whole decision compresses into ~3 days, needs **no VPS, no OpenAI infrastructure, and
no commitment**. Run it locally.

It must answer three questions:

1. **Do Codex hooks fire under `codex exec`?** (gates the entire architecture — see §7.1)
2. **Can a `codex exec` worker complete one real SABLE bead end-to-end?**
   worktree → implement → tests green → branch push → `bd close`
3. **How does GPT-5.6 Sol behave against the existing test gates?**

Open at the point this conversation paused: the operator asked whether to spec the spike
concretely — **which bead to use as the guinea pig, pass/fail criteria, and the
JSONL-parsing contract.** That spec has **not** been written yet. It is the next
deliverable.

**Still no beads authored.** Nothing is filed. Resume by deciding whether to spec the
spike, then whether to file it.

---

## Source audit note

Repo-coupling findings in §3 and §5.3 come from a full read-only audit of
`/home/ddc/dev-environment/SABLE` (bin/, hooks/, templates/, install.sh) performed
2026-07-20. Key measurements: 40 `bin/` tools, 24 production hooks (4,867 LOC) + 71 hook
test scripts (15,692 LOC), 8 skills, 4 subagent definitions, 4 role cards, 21,226 total
shell LOC, largest file `bin/sable-spawn-worker` (67 KB). The runtime-swap seam already
exists: **`SABLE_TMUX_PANE_CMD`, `SABLE_WORKER_CMD`, `SABLE_WORKER_PERMISSION`** already
substitute the pane binary wholesale (the test suite uses shell stand-ins).

External findings in §4 are dated 2026-07-20 and **should be re-verified before relying on
them** — pricing, quota, and Codex feature surface are all moving fast.
