# Personal tooling (branch: `personal-tooling`)

This branch carries Dylan's experimental SABLE tooling across machines. **It is not meant for merge into `main`** until the tools have been shaken out.

Contents:
- `bin/sable-note` — shell script for frictionless capture of SABLE methodology observations
- `skills/sable-review/SKILL.md` — Claude Code skill for triaging accumulated feedback

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

## Promotion to `main`

Once the tooling is proven, cherry-pick the relevant commits onto `main` and add:
- Install instructions in `QUICKSTART.md` (the sable-note / sable-review section we drafted and reverted — in git history if needed)
- PATH setup line in `install.sh` and `install.ps1`
- Reference in `templates/global-CLAUDE-prime.md` and the global CLAUDE.md

Until then, this branch is the canonical home.
