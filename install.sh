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

# --- CLI flags (SABLE-106) ---
DRY_RUN=0
ORCHESTRATION=0
[ "${SABLE_ORCHESTRATION:-}" = "1" ] && ORCHESTRATION=1
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --orchestration) ORCHESTRATION=1 ;;
        -h|--help)
            echo "Usage: install.sh [--orchestration] [--dry-run]"
            echo "  --orchestration   also install the multi-manager Orchestration tier"
            echo "              (hooks/multi-manager + agents.yaml; or SABLE_ORCHESTRATION=1)"
            echo "  --dry-run   report what would be copied; write nothing"
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
printf 'Target dir: %s\n' "${CLAUDE_DIR}"
printf 'Orchestration:    %s\n\n' "$([ "$ORCHESTRATION" = "1" ] && echo "yes (multi-manager tier)" || echo "no (Foundation tier; pass --orchestration to add)")"
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
bold "Step 1/7: Verify bd is installed"
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
echo

# 2. Verify Claude config dir exists
bold "Step 2/7: Verify ~/.claude exists"
if [ ! -d "${CLAUDE_DIR}" ]; then
    yellow "  ~/.claude not found. Creating it."
    mkdir -p "${CLAUDE_DIR}"
fi
green "  OK"
echo

# 3. Copy hooks (chmod is a no-op on Windows but doesn't error)
bold "Step 3/7: Copy hooks to ${HOOKS_DST}"
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

# 4. Copy agent definitions to ~/.claude/agents/ (idempotent; preserves non-SABLE agent files)
bold "Step 4/7: Copy agent definitions to ${CLAUDE_DIR}/agents/"
AGENTS_SRC="${TEMPLATE_DIR}/agents"
AGENTS_DST="${CLAUDE_DIR}/agents"
if [ -d "${AGENTS_SRC}" ]; then
    make_dir "${AGENTS_DST}"
    SABLE_AGENT_NAMES="columbo optimus rudy sherlock tarzan victor"
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

# 5. Install the Orchestration (multi-manager) tier — gated so Foundation adopters opt in (SABLE-106)
bold "Step 5/7: Install Orchestration (multi-manager) extensions"
if [ "$ORCHESTRATION" != "1" ]; then
    yellow "  Skipped — Foundation tier. Re-run with --orchestration (or SABLE_ORCHESTRATION=1) to"
    yellow "  install the one-window manager hooks + agents.yaml registry."
else
    if [ -d "${MM_HOOKS_SRC}" ]; then
        make_dir "${MM_HOOKS_DST}"
        for hook in "${MM_HOOKS_SRC}"/*.sh; do
            [ -f "${hook}" ] || continue
            copy_file "${hook}" "${MM_HOOKS_DST}/$(basename "${hook}")" "multi-manager/$(basename "${hook}")"
        done
    else
        yellow "  hooks/multi-manager/ not found — skipping Orchestration hooks"
    fi
    if [ -f "${AGENTS_YAML_SRC}" ]; then
        make_dir "${SABLE_DST}"
        if [ "$DRY_RUN" != "1" ] && [ -f "${SABLE_DST}/agents.yaml" ] \
           && ! cmp -s "${AGENTS_YAML_SRC}" "${SABLE_DST}/agents.yaml"; then
            cp "${SABLE_DST}/agents.yaml" "${SABLE_DST}/agents.yaml.bak.$(date +%Y%m%d%H%M%S)"
            yellow "  Existing agents.yaml differed — backed up before overwrite"
        fi
        copy_file "${AGENTS_YAML_SRC}" "${SABLE_DST}/agents.yaml" "sable/agents.yaml"
    else
        yellow "  templates/multi-manager/agents.yaml not found — skipping registry"
    fi
    # Skills (slash commands) — installed by their frontmatter 'name:' (the repo
    # dir may differ, e.g. sable-plan -> plan), with all sibling files.
    if [ -d "${SKILLS_SRC}" ]; then
        for skdir in "${SKILLS_SRC}"/*/; do
            [ -f "${skdir}SKILL.md" ] || continue
            skname="$(grep -m1 '^name:' "${skdir}SKILL.md" | sed 's/^name:[[:space:]]*//' | tr -d '[:space:]\r')"
            [ -n "${skname}" ] || skname="$(basename "${skdir}")"
            make_dir "${SKILLS_DST}/${skname}"
            for f in "${skdir}"*; do
                [ -f "${f}" ] && copy_file "${f}" "${SKILLS_DST}/${skname}/$(basename "${f}")" "skills/${skname}/$(basename "${f}")"
            done
        done
    else
        yellow "  skills/ not found — skipping skills install"
    fi
    [ "$DRY_RUN" = "1" ] || green "  Orchestration tier installed. Add the multi-manager hook block (printed below) to settings.json."
fi
echo

# 6. Prepend Prime Directives to CLAUDE.md (with backup)
bold "Step 6/7: Add Prime Directives to ${GLOBAL_CLAUDE_MD}"
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

# 6. Print settings.json snippet (do NOT auto-edit — settings is too important to clobber)
bold "Step 7/7: Settings.json hook block"
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
      {"matcher": "", "hooks": [{"type": "command", "command": "bd prime"}]}
    ],
    "PreCompact": [
      {"matcher": "", "hooks": [{"type": "command", "command": "bd prime"}]}
    ]
  }
}
EOF
echo

if [ "$ORCHESTRATION" = "1" ]; then
    bold "Orchestration (multi-manager) hook block"
    echo "ALSO merge the multi-manager hook entries into ${SETTINGS_FILE}. The canonical"
    echo "block is committed at:"
    echo "    ${TEMPLATE_DIR}/multi-manager/settings-snippet.json"
    echo "It registers the pre-dispatch governance (PreToolUse:Agent), the mode"
    echo "interlock + tree-claim + pre-push gate (PreToolUse:Bash), the claim reconciler"
    echo "(PreToolUse:Edit|Write), and the post-push notify (PostToolUse:Bash)."
    echo
fi

bold "Install complete."
echo
echo "Next steps:"
echo "  1. Paste the hook block(s) above into ${SETTINGS_FILE} (merge with existing config)."
echo "  2. In your project: bd init && bd hooks install"
echo "  3. Agent definitions are now in ${CLAUDE_DIR}/agents/ — restart Claude Code for them to take effect."
if [ "$ORCHESTRATION" = "1" ]; then
    echo "  4. Orchestration installed: ${MM_HOOKS_DST}/ + ${SABLE_DST}/agents.yaml + ${SKILLS_DST}/ skills. Verify:"
    echo "       ls ${MM_HOOKS_DST} && ls ${SKILLS_DST} && head -1 ${SABLE_DST}/agents.yaml"
    echo "     RESTART Claude Code so the /plan /execute /gaudi /columbo slash commands register."
    echo "     Chuck stays a terminal — add the env-var alias from QUICKSTART.md if you run merge-queue."
fi
echo "  5. Open a fresh agent session and use the bootstrap prompt from QUICKSTART.md"
echo "  6. Verify: see 'Verify the install' section of QUICKSTART.md"
