# Personal tooling (branch: `personal-tooling`)

This branch carries Dylan's experimental SABLE tooling across machines. **It is not meant for merge into `main`** until the tools have been shaken out.

Contents:
- `bin/sable-note` — shell script for frictionless capture of SABLE methodology observations
- `skills/sable-review/SKILL.md` — Claude Code skill for triaging accumulated feedback
- `skills/audit-deep-dive/SKILL.md` — Claude Code skill for converting AUDIT: beads into epic+children
- `skills/columbo/SKILL.md` + `skills/columbo/columbo-prefilter.py` — Claude Code skill that delivers the Columbo test-coverage planning workflow without requiring the multi-manager registry, role files, agent identity, or coordination hooks. Use this on machines where you want Columbo's interview + skeleton-test output but not the full multi-manager pattern (typical for work computers where you bounce between many repos). Invokable as `/columbo` once installed at `~/.claude/skills/columbo/`.
- `MULTI-MANAGER-PATTERN.md` — experimental coordination pattern for power-user multi-agent swarms. Eight-agent roster: continuous execution managers (Optimus / Tarzan / Chuck), session-scoped planning agents (Sherlock / Victor / Rudy / Columbo), and execution-session strategist (Lincoln). Companion `hooks/multi-manager/`, `templates/multi-manager/`, `bin/columbo-prefilter.py` (Columbo's audit-mode triage tool), and `bin/sable-agents` reminder helper.

## Columbo: skill vs. multi-manager pattern

Columbo's interview workflow (taxonomy, decision rubric, 5-phase flow, skeleton-test convention, one-more-thing rule) ships in two forms:

- **Skill (`skills/columbo/`)** — portable, single-file, no dependencies beyond `bd`. Right for work computers, repos where you don't run a manager swarm, or any setup where you want the workflow without the agent-coordination plumbing. Invokable as `/columbo` from any cwd.
- **Multi-manager agent (`templates/multi-manager/roles/columbo.md`)** — full implementation with identity injection, `for-columbo` inbox, bead-template gate enforcement, runs as a peer to Sherlock / Victor / Rudy. Right for personal projects with the full SABLE stack installed.

Both produce the same outputs: `columbo-test-spec` / `columbo-test-gap` beads + `*.skel.test.<ext>` skeleton files. Pick the one that matches your setup; the workflow content is the same.

## Install on a new machine

After `git fetch` + `git checkout personal-tooling`:

### 1. `sable-note` on PATH

```bash
chmod +x bin/sable-note
echo 'export PATH="$PATH:'"$(pwd)"'/bin"' >> ~/.zshrc   # or ~/.bashrc
source ~/.zshrc
sable-note --help
```

### 2. `/sable-review` skill available to Claude Code

```bash
mkdir -p ~/.claude/skills/sable-review
cp skills/sable-review/SKILL.md ~/.claude/skills/sable-review/SKILL.md
```

Confirm by starting a fresh Claude Code session and checking the skill list — `sable-review` should appear.

### 2b. `/columbo` skill available to Claude Code (work-machine variant)

```bash
mkdir -p ~/.claude/skills/columbo
cp skills/columbo/SKILL.md           ~/.claude/skills/columbo/SKILL.md
cp skills/columbo/columbo-prefilter.py ~/.claude/skills/columbo/columbo-prefilter.py
```

Confirm by starting a fresh Claude Code session and checking the skill list — `columbo` should appear. Skip on machines where you've installed the full multi-manager pattern (the `columbo()` shell function + `~/.claude/sable/roles/columbo.md` registry entry give you the same workflow with the additional agent-coordination plumbing).

## Syncing changes between machines

When you edit the skill or script on one machine:

```bash
git add bin/sable-note skills/sable-review/SKILL.md
git commit -m "tweak: <what changed>"
git push
```

On the other machine:

```bash
git pull
cp skills/sable-review/SKILL.md ~/.claude/skills/sable-review/SKILL.md
```

(The `sable-note` script runs from wherever it's checked out, so `git pull` alone is enough for script changes.)

## Captured feedback (`feedback/*.md`)

`feedback/*.md` is in `.gitignore` — your raw notes stay local to each machine. If you want the notes synced too, remove that line from `.gitignore` on this branch.

### 3. Multi-Manager Coordination Pattern (advanced)

See [`MULTI-MANAGER-PATTERN.md`](MULTI-MANAGER-PATTERN.md) for the full design and setup. **Do not install on a machine whose repos aren't ready for the discipline this pattern requires** — bead description quality must be reliably high, and the operator must be fluent at the SABLE Swarm stage. Read the prerequisites in the doc before adopting.

Files:
- `MULTI-MANAGER-PATTERN.md` — design doc and setup instructions
- `hooks/multi-manager/*.sh` — twelve coordination hooks (session-role-anchor, read-guard, inbox-injection, inbox-injection-precompact, pre-dispatch refresh/claim/overlap/preempt/model-check, edit-write-claim-reconciler, pre-push-rebase-test, post-push-merge-notify) + `upgrade-notes.md`
- `templates/multi-manager/agents.yaml` — agent registry: Optimus / Tarzan / Chuck (managers), Sherlock / Victor / Rudy / Columbo (planning agents), Lincoln (strategist)
- `templates/multi-manager/roles/*.md` — role prompts injected at SessionStart (one per agent)
- `templates/multi-manager/commands/inbox.md` — `/inbox` slash command
- `templates/multi-manager/settings-snippet.json` — JSON to merge into `~/.claude/settings.json`
- `bin/sable-agents` — quick-reference helper that reads the registry and prints a scannable summary
- `bin/columbo-prefilter.py` — static-analysis test-shallowness ranker. Runs before Columbo's interview in audit mode to triage which test files are worth talking about. Six heuristics across TS + Python: happy-path-only, single-case-wonder, mock-saturation, missing-categories, stale-fixture, assertion-density.
- `templates/sherlock-bead.md` — required template for `sherlock-finding` beads (mechanically enforced by `bead-description-gate.sh`)
- `templates/columbo-bead.md` — required template for `columbo-test-spec` (forward) and `columbo-test-gap` (audit) beads (mechanically enforced by `bead-description-gate.sh`)

## Promotion to `main`

Once the tooling is proven, cherry-pick the relevant commits onto `main` and add:
- Install instructions in `QUICKSTART.md` (the sable-note / sable-review section we drafted and reverted — in git history if needed)
- PATH setup line in `install.sh` and `install.ps1`
- Reference in `templates/global-CLAUDE-prime.md` and the global CLAUDE.md
- For Multi-Manager Pattern specifically: requires the promotion criteria documented in `MULTI-MANAGER-PATTERN.md` (4+ weeks of operation, demonstrated conflict reduction, peer review)

Until then, this branch is the canonical home.
