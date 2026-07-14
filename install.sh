#!/usr/bin/env bash
# SABLE installer — copies hooks, prepares settings.json snippet, prepends Prime Directives to CLAUDE.md.
# Cross-platform: Linux, macOS (bash 3.2+), Windows via Git Bash or WSL.
# Idempotent: safe to re-run. Backs up before editing CLAUDE.md.

set -eu

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
CLAUDE_DIR="${HOME}/.claude"
HOOKS_SRC="${REPO_DIR}/hooks"
HOOKS_DST="${CLAUDE_DIR}/hooks"
TEMPLATE_DIR="${REPO_DIR}/templates"
SETTINGS_FILE="${CLAUDE_DIR}/settings.json"
GLOBAL_CLAUDE_MD="${CLAUDE_DIR}/CLAUDE.md"
PRIME_TEMPLATE="${TEMPLATE_DIR}/global-CLAUDE-prime.md"
MM_HOOKS_SRC="${HOOKS_SRC}/multi-manager"
MM_HOOKS_DST="${HOOKS_DST}/multi-manager"
SABLE_DST="${CLAUDE_DIR}/sable"
AGENTS_YAML_SRC="${TEMPLATE_DIR}/multi-manager/agents.yaml"
SKILLS_SRC="${REPO_DIR}/skills"
SKILLS_DST="${CLAUDE_DIR}/skills"

# --- CLI flags (SABLE-106, front door SABLE-ppy; tmux-only SABLE-qa4d;
# single-path install SABLE-ssws.1 — there are no tiers and no topologies) ---
DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run)       DRY_RUN=1 ;;
        --subagent|--nested|--teams)
            echo "install.sh: '$arg' was retired — SABLE runs on the tmux warm-pane layout only (see TMUX-AGENTS-DESIGN.md)" >&2
            exit 1 ;;
        --orchestration|--foundation)
            echo "install.sh: '$arg' was retired — there is one install: the full workflow including the orchestration layer (see QUICKSTART.md)" >&2
            exit 1 ;;
        -h|--help)
            echo "Usage: install.sh [--dry-run]"
            echo "  Installs the complete SABLE workflow: beads discipline + hooks,"
            echo "  producer agent defs, and the tmux warm-pane orchestration layer."
            echo "  --dry-run              report what would be done; write nothing"
            exit 0 ;;
    esac
done

# make_dir <path> — mkdir -p, dry-run aware.
make_dir() {
    if [ "$DRY_RUN" = "1" ]; then printf '  would mkdir: %s\n' "$1"; else mkdir -p "$1"; fi
}
# copy_file <src> <dst> [label] — cp + best-effort chmod, dry-run aware.
copy_file() {
    local src="$1" dst="$2" label="${3:-$(basename "$2")}"
    if [ "$DRY_RUN" = "1" ]; then printf '  would copy: %s\n' "${dst}"; return 0; fi
    cp "${src}" "${dst}"
    chmod +x "${dst}" 2>/dev/null || true
    green "  ${label}"
}

# OS detection (informational only — script works the same way on all three)
case "$(uname -s 2>/dev/null || echo Unknown)" in
    Linux*)            OS_NAME="Linux" ;;
    Darwin*)           OS_NAME="macOS" ;;
    MINGW*|MSYS*|CYGWIN*) OS_NAME="Windows (Git Bash / MSYS)" ;;
    *)                 OS_NAME="Unknown" ;;
esac

bold()   { printf '\033[1m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
red()    { printf '\033[31m%s\033[0m\n' "$*"; }

bold "SABLE installer"
printf 'OS:         %s\n' "${OS_NAME}"
printf 'Repo:       %s\n' "${REPO_DIR}"
printf 'Target dir: %s\n\n' "${CLAUDE_DIR}"
[ "$DRY_RUN" = "1" ] && { yellow "DRY RUN — no files will be written."; echo; }

if [ "${OS_NAME}" = "Unknown" ]; then
    yellow "Could not detect OS via uname. Continuing — most likely fine if you're on a POSIX shell."
    echo
fi

print_dep_hint() {
    # $1 = dependency name (bd | dolt), $2 = canonical install URL
    local dep="$1"
    local url="$2"
    red "  ${dep} is not on PATH."
    yellow "  Canonical install instructions: ${url}"
    case "${OS_NAME}" in
        macOS)
            yellow "  macOS:   typically \`brew install\` or a release binary — check the URL above for the current command."
            ;;
        Linux*)
            yellow "  Linux:   typically a release binary, \`curl | bash\` installer, or distro package — check the URL above."
            ;;
        "Windows (Git Bash / MSYS)")
            yellow "  Windows: download the .exe from releases, or use Scoop/Chocolatey if a package exists."
            yellow "           Confirm the install dir (or .exe) is on PATH after install."
            ;;
        *)
            yellow "  Check the URL above for your platform's install command."
            ;;
    esac
    yellow "  After installing, re-run: bash install.sh"
}

# 1. Verify bd is installed (check both bd and bd.exe for Windows)
bold "Step 1/8: Verify bd is installed"
if command -v bd >/dev/null 2>&1; then
    BD_CMD="bd"
elif command -v bd.exe >/dev/null 2>&1; then
    BD_CMD="bd.exe"
else
    print_dep_hint "bd (beads)" "https://github.com/steveyegge/beads#installation"
    echo
    yellow "  Note: bd uses Dolt as its storage backend. \`bd dolt push\` will fail without dolt installed."
    yellow "  Dolt install: https://docs.dolthub.com/introduction/installation"
    exit 1
fi
green "  $(${BD_CMD} version 2>/dev/null | head -1 || echo "${BD_CMD} (version check failed but binary found)")"

# Dolt check — non-fatal warning since not every workflow uses bd dolt push
if ! command -v dolt >/dev/null 2>&1 && ! command -v dolt.exe >/dev/null 2>&1; then
    yellow "  Note: dolt not found on PATH. \`bd dolt push\` (used in session-close protocol) will fail."
    yellow "  Install: https://docs.dolthub.com/introduction/installation (not required to finish this install)"
fi

# tmux check — non-fatal warning, but the execution layer is unusable without it:
# sable-launch / sable-tmux stand up the warm-pane session on tmux.
if ! command -v tmux >/dev/null 2>&1; then
    yellow "  Note: tmux not found on PATH. The warm-pane session (sable-launch / sable-tmux) needs it."
    case "${OS_NAME}" in
        macOS)   yellow "  Install: brew install tmux" ;;
        Linux*)  yellow "  Install: your distro package manager (e.g. apt install tmux)" ;;
        "Windows (Git Bash / MSYS)") yellow "  tmux is not available on native Windows — use WSL and re-run this installer there." ;;
        *)       yellow "  Install tmux via your platform's package manager." ;;
    esac
fi
echo

# CC version floor (warn, don't fail) — named-agent (producer) dispatch during
# planning needs Claude Code >= 2.1.172. The install completes on any version.
CC_VER="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
if [ -n "${CC_VER}" ] && ! python3 -c "import sys;v=tuple(int(x) for x in '${CC_VER}'.split('.'));sys.exit(0 if v>=(2,1,172) else 1)" 2>/dev/null; then
    yellow "  Note: Claude Code ${CC_VER} is below 2.1.172 — named-agent (producer) dispatch needs >= 2.1.172. The install completes fine; upgrade before running planning producers."
    echo
fi

# 2. Verify Claude config dir exists
bold "Step 2/8: Verify ~/.claude exists"
if [ ! -d "${CLAUDE_DIR}" ]; then
    yellow "  ~/.claude not found. Creating it."
    mkdir -p "${CLAUDE_DIR}"
fi
green "  OK"
echo

# 3. Link the SABLE CLI tools (sable-*) onto PATH (SABLE-cmql). Runs for ALL tiers:
# Foundation needs sable-note; Orchestration needs sable-launch / sable-mode / etc.
# Symlinks (default) so a linked tool resolves back to the repo (sable-note finds the
# repo feedback dir) and never goes stale.
bold "Step 3/8: Link SABLE CLI tools onto PATH"
if [ "$DRY_RUN" = "1" ]; then
    yellow "  would delegate: sable-bin-install (symlink bin/sable-* into ~/.local/bin)"
elif [ -x "${REPO_DIR}/bin/sable-bin-install" ]; then
    bash "${REPO_DIR}/bin/sable-bin-install"
    green "  SABLE CLI tools linked (sable-launch, sable-note, sable-mode, ...)"
else
    yellow "  bin/sable-bin-install not found — add ${REPO_DIR}/bin to PATH manually (see PERSONAL-TOOLING.md)"
fi
echo

# 4. Copy hooks (chmod is a no-op on Windows but doesn't error)
bold "Step 4/8: Copy hooks to ${HOOKS_DST}"
make_dir "${HOOKS_DST}"
for hook in "${HOOKS_SRC}"/*.sh; do
    name="$(basename "${hook}")"
    copy_file "${hook}" "${HOOKS_DST}/${name}" "${name}"
done
echo

if [ "${OS_NAME}" = "Windows (Git Bash / MSYS)" ]; then
    yellow "  Note: hook scripts are bash. They run under Git Bash on Windows."
    yellow "  Native PowerShell agents won't execute them — use install.ps1 + WSL/Git Bash if needed."
    echo
fi

# 5. Copy agent definitions to ~/.claude/agents/ (idempotent; preserves non-SABLE agent files)
# Retired manager defs (optimus/tarzan/chuck.md) left behind by an older install
# are NOT cleaned here — this step only ever adds. bin/sable-orchestration-install
# (delegated below at step 6) is the single owner of that cleanup for the scope's
# agents/ dir, so a plain re-run of this script still retires them (SABLE-gsqj).
bold "Step 5/8: Copy agent definitions to ${CLAUDE_DIR}/agents/"
AGENTS_SRC="${TEMPLATE_DIR}/agents"
AGENTS_DST="${CLAUDE_DIR}/agents"
if [ -d "${AGENTS_SRC}" ]; then
    make_dir "${AGENTS_DST}"
    # Producers only — managers (optimus/tarzan/chuck) are tmux panes whose
    # identity comes from role files, not agent definitions (SABLE-qa4d.5).
    SABLE_AGENT_NAMES="columbo rudy sherlock victor"
    for name in ${SABLE_AGENT_NAMES}; do
        src="${AGENTS_SRC}/${name}.md"
        dst="${AGENTS_DST}/${name}.md"
        [ -f "${src}" ] && copy_file "${src}" "${dst}" "${name}.md"
    done
    [ "$DRY_RUN" = "1" ] || green "  Agent definitions installed (non-SABLE agent files preserved)"
else
    yellow "  templates/agents/ not found — skipping agent definitions install"
fi
echo

# 6. Install the orchestration (multi-manager) layer by DELEGATING to the
# complete-layer installer (SABLE-ppy). Always runs — there is one install
# (SABLE-ssws.1). The delegate installs all hooks + registry + skills + the four
# pane roles and merges the settings snippet.
bold "Step 6/8: Orchestration (multi-manager) layer"
if [ "$DRY_RUN" = "1" ]; then
    yellow "  would delegate: sable-orchestration-install --user"
elif [ -x "${REPO_DIR}/bin/sable-orchestration-install" ]; then
    green "  Delegating to sable-orchestration-install (--user)..."
    bash "${REPO_DIR}/bin/sable-orchestration-install" --user
else
    yellow "  bin/sable-orchestration-install not found — skipping the orchestration layer"
fi
echo

# 7. Prepend Prime Directives to CLAUDE.md (with backup)
bold "Step 7/8: Add Prime Directives to ${GLOBAL_CLAUDE_MD}"
if [ ! -f "${PRIME_TEMPLATE}" ]; then
    red "  Missing template: ${PRIME_TEMPLATE}"
    exit 1
fi

if [ "$DRY_RUN" = "1" ]; then
    printf '  would prepend Prime Directives to %s (if not already present)\n' "${GLOBAL_CLAUDE_MD}"
elif [ -f "${GLOBAL_CLAUDE_MD}" ] && grep -q "Prime Directive" "${GLOBAL_CLAUDE_MD}"; then
    yellow "  Prime Directive already present — skipping CLAUDE.md edit"
else
    if [ -f "${GLOBAL_CLAUDE_MD}" ]; then
        BACKUP="${GLOBAL_CLAUDE_MD}.bak.$(date +%Y%m%d%H%M%S)"
        cp "${GLOBAL_CLAUDE_MD}" "${BACKUP}"
        yellow "  Backed up existing CLAUDE.md to ${BACKUP}"
        cat "${PRIME_TEMPLATE}" "${BACKUP}" > "${GLOBAL_CLAUDE_MD}"
    else
        cp "${PRIME_TEMPLATE}" "${GLOBAL_CLAUDE_MD}"
    fi
    green "  Prime Directives prepended"
fi
echo

# 8. Print settings.json snippet (do NOT auto-edit — settings is too important to clobber)
bold "Step 8/8: Settings.json hook block"
echo "Add the following block to your ${SETTINGS_FILE} under the top-level 'hooks' key."
echo "If you already have a 'hooks' key, merge carefully (don't overwrite existing entries)."
echo
cat <<EOF
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {"type": "command", "command": "bash ${HOOKS_DST}/tdd-evidence.sh", "timeout": 3000},
          {"type": "command", "command": "bash ${HOOKS_DST}/tdd-gate.sh", "timeout": 5000},
          {"type": "command", "command": "bash ${HOOKS_DST}/bead-description-gate.sh", "timeout": 3000}
        ]
      },
      {
        "matcher": "Edit|Write",
        "hooks": [
          {"type": "command", "command": "bash ${HOOKS_DST}/tdd-remind.sh", "timeout": 3000}
        ]
      },
      {
        "matcher": "Agent",
        "hooks": [
          {"type": "command", "command": "bash ${HOOKS_DST}/agent-tdd-enforce.sh", "timeout": 3000}
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {"type": "command", "command": "bash ${HOOKS_DST}/bead-quality.sh", "timeout": 5000}
        ]
      }
    ],
    "SessionStart": [
      {"matcher": "", "hooks": [{"type": "command", "command": "bd prime"}]},
      {"matcher": "", "hooks": [{"type": "command", "command": "sable-doctor --quiet 2>&1 || true"}]}
    ],
    "PreCompact": [
      {"matcher": "", "hooks": [{"type": "command", "command": "bd prime"}]}
    ]
  }
}
EOF
echo
echo "The SessionStart sable-doctor entry above warns (non-fatal) at session start"
echo "when your installed ~/.claude drifts from this repo — see SABLE-1i6m / bin/sable-doctor."
echo

bold "Orchestration hooks"
echo "The orchestration settings snippet was merged into the scope's settings file"
echo "automatically by sable-orchestration-install (backed up; existing entries kept)."
echo

bold "Install complete."
echo
echo "Next steps:"
echo "  1. Paste the hook block(s) above into ${SETTINGS_FILE} (merge with existing config)."
echo "  2. In your project: bd init && bd hooks install"
echo "  3. RESTART Claude Code so the agent defs, /sable-plan /sable-execute /gaudi /columbo, and hooks register."
echo "  4. Start your session:  sable-launch   (Lincoln only, wraps sable-tmux; managers spawn on demand)"
echo "  5. Open a fresh agent session and use the bootstrap prompt from QUICKSTART.md"
echo "  6. Verify: see 'Verify the install' section of QUICKSTART.md — or type: sable --help"
