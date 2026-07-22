# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   sable-dolt-push  # blessed wrapper, never bare `bd dolt push`; chuck-only in a swarm
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var

## Build & Test

This repo's actual stack is Python + bash/tmux — there is no Go tooling here.
**`bd preflight`'s built-in checklist is hardcoded to Go (`go test`,
`golangci-lint`, `gofmt`, `cmd/bd/version.go`) and is not configurable per
project (SABLE-4yp75)**, so on this repo every one of those checks either
false-fails ("golangci-lint not found in PATH") or silently no-ops. Do not
treat a red `bd preflight` run as signal here; use the commands below instead.

```bash
# Full Python suite (unit + integration; bd/dolt-dependent tests self-skip
# when those tools are absent — see ci-verify.yml):
python -m pytest bin/ -q -p no:cacheprovider

# Shell test suites (classification is fail-closed; --run executes the
# allowlisted suites; see .github/ci/shell-run-set.sh header for the
# excluded-suite policy):
bash .github/ci/shell-run-set.sh --check
bash .github/ci/shell-run-set.sh --run

# Fast local pre-push subset (also runs automatically via the pre-push git
# hook through this repo's .sable testCommand=):
bash .github/ci/test-tiers.sh --run pre_push
```

The two `bd preflight` checks that ARE language-agnostic (no beads pollution,
AGENTS.md/CLAUDE.md doc-sync) are still worth a glance from its output —
just ignore the tests/lint/format/version-sync rows.

## Architecture Overview

SABLE is a methodology + tooling repo for bd-based multi-agent development:
`bin/` holds the Python/bash CLI tools (`sable-*`) and their `test_*.py` /
`hooks/test/test-*.sh` suites; `hooks/` holds the git-hook and multi-manager
enforcement scripts; `skills/` and `templates/` hold the Claude Code skill
definitions this methodology installs into consumer projects; `SABLE.md`
and `QUICKSTART.md` are the portable methodology docs shipped to other repos
(don't assume this repo's own stack when editing those — they describe
whatever stack the *downstream* project uses).

## Conventions & Patterns

- New `bin/` tools follow the `sable-<name>` (bash) or `sable_<name>_lib.py`
  (Python library) naming already in use — see `bin/sable-doctor` /
  `bin/sable-test` for the header-comment style (context + contract, not
  what the code obviously does).
- Every `bin/*.py` needs a matching `test_*.py` (pytest auto-discovers all of
  `bin/`, so an untested file is a silent gap, not a skip).
- Every `hooks/test/test-*.sh` must be classified in
  `.github/ci/shell-run-set.sh`'s `ALLOW`/`EXCLUDE` lists or `--check` fails
  the gate — see that script's header for why.
