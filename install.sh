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
bold "Step 1/5: Verify bd is installed"
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
bold "Step 2/5: Verify ~/.claude exists"
if [ ! -d "${CLAUDE_DIR}" ]; then
    yellow "  ~/.claude not found. Creating it."
    mkdir -p "${CLAUDE_DIR}"
fi
green "  OK"
echo

# 3. Copy hooks (chmod is a no-op on Windows but doesn't error)
bold "Step 3/5: Copy hooks to ${HOOKS_DST}"
mkdir -p "${HOOKS_DST}"
for hook in "${HOOKS_SRC}"/*.sh; do
    name="$(basename "${hook}")"
    cp "${hook}" "${HOOKS_DST}/${name}"
    chmod +x "${HOOKS_DST}/${name}" 2>/dev/null || true
    green "  ${name}"
done
echo

if [ "${OS_NAME}" = "Windows (Git Bash / MSYS)" ]; then
    yellow "  Note: hook scripts are bash. They run under Git Bash on Windows."
    yellow "  Native PowerShell agents won't execute them — use install.ps1 + WSL/Git Bash if needed."
    echo
fi

# 4. Prepend Prime Directives to CLAUDE.md (with backup)
bold "Step 4/5: Add Prime Directives to ${GLOBAL_CLAUDE_MD}"
if [ ! -f "${PRIME_TEMPLATE}" ]; then
    red "  Missing template: ${PRIME_TEMPLATE}"
    exit 1
fi

if [ -f "${GLOBAL_CLAUDE_MD}" ] && grep -q "Prime Directive" "${GLOBAL_CLAUDE_MD}"; then
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

# 5. Print settings.json snippet (do NOT auto-edit — settings is too important to clobber)
bold "Step 5/5: Settings.json hook block"
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

bold "Install complete."
echo
echo "Next steps:"
echo "  1. Paste the hook block above into ${SETTINGS_FILE} (merge with existing config)."
echo "  2. In your project: bd init && bd hooks install"
echo "  3. Open a fresh agent session and use the bootstrap prompt from QUICKSTART.md"
echo "  4. Verify: see 'Verify the install' section of QUICKSTART.md"
