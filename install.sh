#!/usr/bin/env bash
# SABLE installer — copies hooks, prepares settings.json snippet, prepends Prime Directives to CLAUDE.md.
# Cross-platform: Linux, macOS (bash 3.2+), Windows via Git Bash or WSL.
# Idempotent: safe to re-run. Backs up before editing CLAUDE.md.

set -eu

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# Source paths — always the repo checkout, scope-independent. Install
# DESTINATIONS (CLAUDE_DIR and everything under it) are derived EXACTLY ONCE by
# the scope block below (D5, SABLE-59t6.3) — NOT here — so no step forks on scope.
HOOKS_SRC="${REPO_DIR}/hooks"
TEMPLATE_DIR="${REPO_DIR}/templates"
PRIME_TEMPLATE="${TEMPLATE_DIR}/global-CLAUDE-prime.md"

# --- CLI flags (SABLE-106, front door SABLE-ppy; tmux-only SABLE-qa4d;
# single-path install SABLE-ssws.1 — there are no tiers and no topologies) ---
DRY_RUN=0
FROM_HERE=0
PROJECT_MODE=0
FORCE=0
PROJECT_PATH_ARG=""

# --- THE settings.json CONTRACT (SABLE-gxsji) ---
# ONE string, printed to the operator by step 6 AND asserted by
# bin/test_sable_install.py against the code that enforces it, so the promise
# and the behaviour cannot drift apart again. They already did once: step 8
# promised "do NOT auto-edit — settings is too important to clobber" while
# step 6's delegate merged rows into that same file on every ordinary run. The
# row it merged on 2026-07-23 was a PreToolUse hook — an interceptor that runs
# BEFORE EVERY TOOL CALL OF EVERY AGENT — and nothing in the install output
# named it. It was noticed only because that particular hook was too broken to
# stay quiet; a working one would still be running, unnoticed and unconsented.
# So the default is now print-only, and consent is an explicit flag.
SETTINGS_CONTRACT="install.sh does NOT write settings.json — the step-6 delegate runs inside a guard that restores the file afterwards. Pass --merge-settings to consent to the write."
MERGE_SETTINGS=0

for arg in "$@"; do
    case "$arg" in
        --dry-run)       DRY_RUN=1 ;;
        --from-here)     FROM_HERE=1 ;;
        --project)       PROJECT_MODE=1 ;;
        --project=*)     PROJECT_MODE=1; PROJECT_PATH_ARG="${arg#--project=}" ;;
        --force)         FORCE=1 ;;
        --merge-settings) MERGE_SETTINGS=1 ;;
        --subagent|--nested|--teams)
            echo "install.sh: '$arg' was retired — SABLE runs on the tmux warm-pane layout only (see TMUX-AGENTS-DESIGN.md)" >&2
            exit 1 ;;
        --orchestration|--foundation)
            echo "install.sh: '$arg' was retired — there is one install: the full workflow including the orchestration layer (see QUICKSTART.md)" >&2
            exit 1 ;;
        -h|--help)
            echo "Usage: install.sh [--dry-run] [--from-here] [--project[=<path>]] [--force] [--merge-settings]"
            echo "  Installs the complete SABLE workflow: beads discipline + hooks,"
            echo "  producer agent defs, and the tmux warm-pane orchestration layer."
            echo "  --dry-run              report what would be done; write nothing"
            echo "  --from-here            install from this checkout even if it's a linked"
            echo "                         git worktree (default: refuse — see SABLE-s6qk)"
            echo "  --project[=<path>]     install the SABLE layer INTO a project's own .claude"
            echo "                         (+ its CLAUDE.md) instead of ~/.claude. Path defaults"
            echo "                         to the current repo root (via git-common-dir); refuses"
            echo "                         outside a git repo. The CLI tools still link globally"
            echo "                         into ~/.local/bin (hybrid contract, SABLE-59t6)."
            echo "  --force                proceed with --project even when ~/.claude already"
            echo "                         carries SABLE hooks (accepts hooks firing twice)."
            echo "  --merge-settings       CONSENT to this install writing the scope's"
            echo "                         settings.json. Without it the install is print-only:"
            echo "                         the step-6 delegate's merge is reverted, the exact"
            echo "                         changes are printed, and the would-be result is"
            echo "                         parked as a .proposed file for you to apply."
            echo "                         ${SETTINGS_CONTRACT}"
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

# --- BEGIN settings-guard (SABLE-gxsji) ---
# Everything between these two sentinels is self-contained and is EXTRACTED AND
# SOURCED VERBATIM by bin/test_sable_install.py so the mutation reporter can be
# unit-tested without running a whole install. The sentinels are part of the
# contract: the test fails if they go missing, so this block cannot be quietly
# dissolved back into the script body.
#
# WHY THIS EXISTS. install.sh's step 6 delegates to bin/sable-orchestration-install,
# which merges hook rows into the scope's settings.json. install.sh promised
# in source that it did not touch that file. On 2026-07-23 an ordinary install
# merged a PreToolUse row — an interceptor that runs before EVERY tool call of
# EVERY agent in the fleet — with nobody having decided to turn it on and
# nothing in the install output naming it. It was discovered only because that
# hook could not load its own library and therefore announced itself on every
# command. A working payload would have been silent and would still be running.
#
# So: the delegate is run INSIDE this guard. By default the guard restores the
# file and prints what would have changed (the promise becomes real);
# --merge-settings keeps the write and prints what changed. Either way the
# install SAYS SO and NAMES what was added, removed, or modified — a silent
# REMOVAL is strictly harder to detect than a silent addition, because an
# addition at least has a payload that might misbehave and an absence has
# nothing that could ever fire.

SETTINGS_GUARD_TMP=""
SETTINGS_PRE_SNAPSHOT=""   # copy of the file as it was before the delegate ran
SETTINGS_PRE_EXISTED=0
SETTINGS_BACKUP_PATH=""    # timestamped rollback copy; named in the output
SETTINGS_PROPOSED_PATH=""  # print-only: where the delegate's would-be result was parked

settings_guard_tmpdir() {
    if [ -z "${SETTINGS_GUARD_TMP}" ]; then
        SETTINGS_GUARD_TMP="$(mktemp -d)"
    fi
    printf '%s\n' "${SETTINGS_GUARD_TMP}"
}

# settings_rotating_path <file> <infix> — a TIMESTAMPED, collision-free name.
#
# The delegate's own backup is `<file>.bak`, SINGLE-SLOT, same name every run,
# so each install DESTROYS the previous one. That is not a footnote: the
# pre-window backup that proved the 2026-07-23 wiring did not pre-exist was
# already gone by 2026-07-26, overwritten by a later ordinary install. The
# evidence needed to measure how often this happens is destroyed by the same
# operation whose rate we are trying to measure. The installer already knows
# how to do this properly — sable-orchestration-install snapshots changed
# artifacts into a timestamped .install-bak-<ts>/ dir — so this reuses that
# idea for the single most security-relevant file it touches.
#
# date(1) resolves to one second and two installs fit inside one second, so a
# same-second collision is broken with a counter: a rotation that can still
# clobber its predecessor is not a rotation.
settings_rotating_path() {
    local file="$1" infix="$2" ts base candidate n
    ts="$(date +%Y%m%d%H%M%S)"
    base="${file}.${infix}.${ts}"
    candidate="${base}"
    n=1
    while [ -e "${candidate}" ]; do
        candidate="${base}.${n}"
        n=$((n + 1))
    done
    printf '%s\n' "${candidate}"
}

# settings_report_mutations <pre> <post> [label] — print one indented line per
# SEMANTIC difference between two settings files. Either path argument may name
# a file that does not exist, which means "absent". [label] is the name the
# CREATED/DELETED lines use for the file being reported on (the caller passes
# the real settings path, since <pre>/<post> are throwaway snapshot copies whose
# temp paths would mean nothing to a reader). Prints NOTHING and returns 1 when
# the two are semantically identical.
#
# THE COMPARISON IS SEMANTIC, NOT BYTE-FOR-BYTE, AND THAT IS LOAD-BEARING. The
# delegate re-serializes the entire file with json.dumps(indent=2) on every
# run, so a byte comparison would report a mutation every single time — the
# "this install changed nothing" outcome would be unreachable and the report
# would be the always-fires shape that a check must never have. A reporter that
# cannot return "unchanged" is indistinguishable from one that is broken.
settings_report_mutations() {
    SETTINGS_PRE_PATH="${1:-}" SETTINGS_POST_PATH="${2:-}" SETTINGS_LABEL="${3:-}" python3 - <<'PY'
import json, os, sys


def load(path):
    """None means the file is absent; {} means present but unreadable as JSON."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


pre_path = os.environ.get('SETTINGS_PRE_PATH', '')
post_path = os.environ.get('SETTINGS_POST_PATH', '')
label = os.environ.get('SETTINGS_LABEL', '') or post_path or pre_path
pre, post = load(pre_path), load(post_path)


def rows(data):
    """(event, matcher, command) -> the hook row, flattened across blocks."""
    out = {}
    hooks = (data or {}).get('hooks')
    if not isinstance(hooks, dict):
        return out
    for event, blocks in hooks.items():
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            matcher = block.get('matcher', '')
            for hook in block.get('hooks') or []:
                if isinstance(hook, dict):
                    out[(event, matcher, hook.get('command', ''))] = hook
    return out


lines = []
if pre is None and post is not None:
    lines.append('CREATED   %s (did not exist before)' % label)
elif pre is not None and post is None:
    lines.append('DELETED   %s (existed before, gone now)' % label)

before, after = rows(pre), rows(post)
for key in sorted(set(before) | set(after)):
    event, matcher, command = key
    where = '%s[%s]' % (event, matcher or '*')
    if key not in before:
        lines.append('ADDED     hook %s %s' % (where, command))
    elif key not in after:
        # The strictly more dangerous half. Nothing fires to announce an
        # absence, so a removal that is not reported here is unreportable
        # anywhere else.
        lines.append('REMOVED   hook %s %s' % (where, command))
    elif before[key] != after[key]:
        lines.append('MODIFIED  hook %s %s (%s -> %s)' % (
            where, command,
            json.dumps(before[key], sort_keys=True),
            json.dumps(after[key], sort_keys=True)))


def others(data):
    return {k: v for k, v in (data or {}).items() if k != 'hooks'}


oa, ob = others(pre), others(post)
for key in sorted(set(oa) | set(ob)):
    if key not in oa:
        lines.append('ADDED     key %s' % key)
    elif key not in ob:
        lines.append('REMOVED   key %s' % key)
    elif oa[key] != ob[key]:
        lines.append('MODIFIED  key %s' % key)

# Backstop: a 'hooks' value that is not a dict (hand-edited, or a schema this
# reporter does not model) diffs to nothing above. Report it rather than let an
# unmodelled shape read as "unchanged" — an unreadable diff is not a clean one.
if not lines and (pre or {}).get('hooks') != (post or {}).get('hooks'):
    lines.append('MODIFIED  key hooks')

for line in lines:
    print('    ' + line)
sys.exit(0 if lines else 1)
PY
}

# settings_guard_capture — snapshot the scope's settings file BEFORE the
# delegate runs, and take the timestamped rollback copy.
settings_guard_capture() {
    local tmp
    tmp="$(settings_guard_tmpdir)"
    SETTINGS_PRE_SNAPSHOT="${tmp}/settings.pre.json"
    rm -f "${SETTINGS_PRE_SNAPSHOT}"
    if [ -f "${SETTINGS_FILE}" ]; then
        SETTINGS_PRE_EXISTED=1
        cp "${SETTINGS_FILE}" "${SETTINGS_PRE_SNAPSHOT}"
        SETTINGS_BACKUP_PATH="$(settings_rotating_path "${SETTINGS_FILE}" "bak")"
        cp "${SETTINGS_FILE}" "${SETTINGS_BACKUP_PATH}"
    else
        SETTINGS_PRE_EXISTED=0
        SETTINGS_BACKUP_PATH=""
    fi
}

# settings_guard_settle — report, then either keep the delegate's write
# (--merge-settings) or restore the file (default, print-only).
settings_guard_settle() {
    local tmp post report changed=0
    tmp="$(settings_guard_tmpdir)"
    post="${tmp}/settings.post.json"
    report="${tmp}/settings.report.txt"
    rm -f "${post}"
    [ -f "${SETTINGS_FILE}" ] && cp "${SETTINGS_FILE}" "${post}"

    if settings_report_mutations "${SETTINGS_PRE_SNAPSHOT}" "${post}" "${SETTINGS_FILE}" > "${report}"; then
        changed=1
    fi

    echo
    bold "  settings.json (${SETTINGS_FILE})"
    if [ "${MERGE_SETTINGS}" = "1" ]; then
        if [ "${changed}" = "1" ]; then
            yellow "  --merge-settings given — this install CHANGED your settings.json:"
            cat "${report}"
            [ -n "${SETTINGS_BACKUP_PATH}" ] && green "  Roll back with: cp ${SETTINGS_BACKUP_PATH} ${SETTINGS_FILE}"
        else
            green "  unchanged — this install added, removed and modified nothing."
        fi
        return 0
    fi

    # Print-only (the default). Restore whenever the BYTES differ, not only
    # when the semantics do: a re-serialization with no semantic change is
    # still a write to a file this installer promises not to write.
    if [ "${changed}" = "1" ]; then
        SETTINGS_PROPOSED_PATH="$(settings_rotating_path "${SETTINGS_FILE}" "proposed")"
        red   "  This install's step-6 delegate WOULD have changed settings.json:"
        cat "${report}"
        yellow "  NOT APPLIED. ${SETTINGS_CONTRACT}"
        yellow "  Ignore the delegate's own 'settings snippet merged' line above — this guard"
        yellow "  reverted that write."
        [ -f "${SETTINGS_FILE}" ] && cp "${SETTINGS_FILE}" "${SETTINGS_PROPOSED_PATH}"
        yellow "  The would-be result is parked, unapplied, at:"
        yellow "    ${SETTINGS_PROPOSED_PATH}"
        yellow "  Apply it by re-running with --merge-settings, or by merging that file yourself."
    else
        green "  unchanged — this install added, removed and modified nothing."
    fi

    if [ "${SETTINGS_PRE_EXISTED}" = "1" ]; then
        if ! cmp -s "${SETTINGS_PRE_SNAPSHOT}" "${SETTINGS_FILE}"; then
            cp "${SETTINGS_PRE_SNAPSHOT}" "${SETTINGS_FILE}"
        fi
    elif [ -f "${SETTINGS_FILE}" ]; then
        rm -f "${SETTINGS_FILE}"
    fi
    [ -n "${SETTINGS_BACKUP_PATH}" ] && green "  Pre-install rollback copy: ${SETTINGS_BACKUP_PATH}"
    return 0
}
# --- END settings-guard (SABLE-gxsji) ---

# --- Canonical-checkout guard (SABLE-s6qk) ---
# Running the installer from a linked git worktree re-derives every hook copy
# source and every ~/.local/bin/sable-* symlink target from that (ephemeral)
# path. Incident market-brief-package-1y6d (2026-07-09): a worker ran
# install.sh from its per-bead worktree, which silently hot-swapped the LIVE
# ~/.claude hook copies with an unmerged branch's code and re-pointed all 19
# sable-* symlinks at the worktree — one routine `git worktree prune` away
# from dangling the whole fleet toolchain. Refuse by default from any
# checkout that isn't the main one; --from-here overrides for deliberate use.

# canonical_checkout_root <dir> — prints the MAIN worktree root (the parent of
# the shared git common-dir; every linked worktree of a project resolves to
# the same one) so the refusal can name the real target without hardcoding a
# path. Fails silently if <dir> isn't a git work tree at all.
canonical_checkout_root() {
    local base="$1" common root
    common="$(git -C "${base}" rev-parse --git-common-dir 2>/dev/null)" || return 1
    [ -n "${common}" ] || return 1
    case "${common}" in
        /*) ;;
        *)  common="${base}/${common}" ;;
    esac
    root="$(cd "$(dirname "${common}")" 2>/dev/null && pwd)" || return 1
    printf '%s\n' "${root}"
}

# is_linked_worktree <dir> — true if <dir> is a LINKED git worktree (not the
# main checkout): its .git is a plain file (the git 2.5+ linked-worktree
# marker), or its git-dir disagrees with the shared git-common-dir. False
# (proceeds) if <dir> isn't a git work tree at all, or git isn't on PATH —
# nothing to guard against. A plain fresh clone (.git is a directory, git-dir
# == git-common-dir) IS canonical by this same test, so a first-time
# bootstrap on a new machine never needs --from-here — canonical-ness is
# derived, never hardcoded to a path.
is_linked_worktree() {
    local dir="$1" git_dir common_dir
    [ -f "${dir}/.git" ] && return 0
    [ -d "${dir}/.git" ] || return 1
    command -v git >/dev/null 2>&1 || return 1
    git_dir="$(git -C "${dir}" rev-parse --git-dir 2>/dev/null)" || return 1
    common_dir="$(git -C "${dir}" rev-parse --git-common-dir 2>/dev/null)" || return 1
    [ "${git_dir}" != "${common_dir}" ]
}

if [ "${FROM_HERE}" != "1" ] && is_linked_worktree "${REPO_DIR}"; then
    red   "install.sh: refusing to run from a linked git worktree:"
    red   "  ${REPO_DIR}"
    echo
    if CANONICAL="$(canonical_checkout_root "${REPO_DIR}")"; then
        yellow "  Run install.sh from the canonical checkout instead:"
        yellow "    ${CANONICAL}/install.sh"
    fi
    yellow "  Installing from a worktree re-derives every ~/.claude hook copy and every"
    yellow "  ~/.local/bin/sable-* symlink from THIS path. The next 'git worktree prune'"
    yellow "  (or the branch merging/deleting) dangles the live toolchain."
    yellow "  If this really is the checkout you mean to install from, re-run with:"
    yellow "    install.sh --from-here"
    exit 1
fi

# --- Install scope: derive every destination EXACTLY ONCE (D5, SABLE-59t6.3) ---
# Default scope is global (~/.claude). --project retargets the whole .claude
# layer AND the Prime-Directive CLAUDE.md into a project's own tree; the
# ~/.local/bin CLI symlinks (Step 3) stay global under BOTH scopes (hybrid
# contract, S2). Every step below consumes these derived variables and NONE of
# them re-tests the raw --project flag — that is the point of deriving here.

# global_settings_has_sable_hooks — true when ~/.claude/settings.json already
# registers SABLE hooks (orchestration multi-manager hooks or the beads/tdd
# gates). A project-scope install layered on top would double-register them.
global_settings_has_sable_hooks() {
    local gs="${HOME}/.claude/settings.json"
    [ -f "${gs}" ] || return 1
    grep -Eq 'multi-manager/|tdd-gate\.sh|tdd-evidence\.sh|tdd-remind\.sh|bead-description-gate\.sh|bead-quality\.sh|agent-tdd-enforce\.sh|sable-doctor' "${gs}"
}

if [ "${PROJECT_MODE}" = "1" ]; then
    # Target project root: --project=<path> if given, else the current dir.
    # Resolve via git-common-dir (canonical_checkout_root) so a call from a
    # linked worktree still targets the project's MAIN checkout; refuse if the
    # seed is not inside a git repo at all.
    _proj_seed="${PROJECT_PATH_ARG:-$PWD}"
    if ! PROJECT_ROOT="$(canonical_checkout_root "${_proj_seed}")"; then
        red   "install.sh: --project requires a git repository."
        red   "  Not inside a git work tree: ${_proj_seed}"
        yellow "  cd into the project you want SABLE installed in, or pass"
        yellow "  --project=<path> pointing inside a git repo."
        exit 1
    fi
    SCOPE="project"
    ORCH_SCOPE_FLAG="--project"
    CLAUDE_DIR="${PROJECT_ROOT}/.claude"
    PRIME_TARGET="${PROJECT_ROOT}/CLAUDE.md"
    # Portable hook root for the Step 8 paste block: a committed project
    # settings.json must reference ${CLAUDE_PROJECT_DIR}, never this machine's
    # absolute path (matches sable-orchestration-install --project, SABLE-59t6.2).
    HOOK_CMD_ROOT='${CLAUDE_PROJECT_DIR}/.claude/hooks'
    # Double-fire guard: with global SABLE hooks already registered, adding a
    # project registration fires every hook TWICE. Refuse unless --force.
    if [ "${FORCE}" != "1" ] && global_settings_has_sable_hooks; then
        red   "install.sh: refusing --project — ~/.claude/settings.json already carries SABLE hook registrations."
        red   "  Installing the project scope too would register the same hooks a second time;"
        red   "  every SABLE hook would then fire TWICE per event."
        echo
        yellow "  Remedy — pick ONE:"
        yellow "    • Remove one scope's wiring: uninstall the global hooks"
        yellow "      (sable-orchestration-install --user --uninstall, and drop the beads/tdd"
        yellow "      hook rows from ~/.claude/settings.json), then re-run --project; OR"
        yellow "    • Re-run with --force to install the project scope anyway, accepting double-fire."
        exit 1
    fi
else
    SCOPE="user"
    ORCH_SCOPE_FLAG="--user"
    PROJECT_ROOT=""
    CLAUDE_DIR="${HOME}/.claude"
    PRIME_TARGET="${CLAUDE_DIR}/CLAUDE.md"
    HOOK_CMD_ROOT="${CLAUDE_DIR}/hooks"
fi

# Destinations — all hang off the single derived CLAUDE_DIR above.
HOOKS_DST="${CLAUDE_DIR}/hooks"
SETTINGS_FILE="${CLAUDE_DIR}/settings.json"

bold "SABLE installer"
printf 'OS:         %s\n' "${OS_NAME}"
printf 'Repo:       %s\n' "${REPO_DIR}"
printf 'Scope:      %s\n' "${SCOPE}"
printf 'Target dir: %s\n' "${CLAUDE_DIR}"
if [ -n "${PROJECT_ROOT}" ]; then
    printf 'Project:    %s\n' "${PROJECT_ROOT}"
    printf 'CLI tools:  %s/.local/bin (global — hybrid contract)\n' "${HOME}"
fi
printf '\n'
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
    yellow "  Note: bd uses Dolt as its storage backend. \`sable-dolt-push\` will fail without dolt installed."
    yellow "  Dolt install: https://docs.dolthub.com/introduction/installation"
    exit 1
fi
green "  $(${BD_CMD} version 2>/dev/null | head -1 || echo "${BD_CMD} (version check failed but binary found)")"

# Dolt check — non-fatal warning since not every workflow uses sable-dolt-push
if ! command -v dolt >/dev/null 2>&1 && ! command -v dolt.exe >/dev/null 2>&1; then
    yellow "  Note: dolt not found on PATH. \`sable-dolt-push\` (used in session-close protocol) will fail."
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

# 2. Verify the scope's Claude config dir exists (dry-run aware — must not write)
bold "Step 2/8: Verify ${CLAUDE_DIR} exists"
if [ ! -d "${CLAUDE_DIR}" ]; then
    yellow "  ${CLAUDE_DIR} not found."
    make_dir "${CLAUDE_DIR}"
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
# pane roles, and it WANTS to merge the settings snippet into the scope's
# settings.json.
#
# THE DELEGATE'S SETTINGS WRITE IS FENCED (SABLE-gxsji). It runs between
# settings_guard_capture and settings_guard_settle. By default the guard
# restores the file and prints exactly what the merge would have added,
# removed or modified; --merge-settings keeps the write and prints the same
# report. Neither path is silent, which is the whole point: the 2026-07-23
# incident was a PreToolUse interceptor merged fleet-wide by an ordinary
# install with nothing in the output naming it.
#
# The revert is a REVERT, not a prevention — the delegate does write the file
# and the guard puts it back a moment later, so a session starting inside that
# window reads the merged file. Removing the window means giving the delegate
# its own print-only mode, which was outside this bead's declared file
# footprint. Tracked as SABLE-93txr, which also covers the delegate's DIRECT
# invocation (still unguarded) and its single-slot .bak.
bold "Step 6/8: Orchestration (multi-manager) layer"
if [ "$DRY_RUN" = "1" ]; then
    yellow "  would delegate: sable-orchestration-install ${ORCH_SCOPE_FLAG}"
    yellow "  would NOT change ${SETTINGS_FILE} — ${SETTINGS_CONTRACT}"
elif [ -x "${REPO_DIR}/bin/sable-orchestration-install" ]; then
    settings_guard_capture
    green "  Delegating to sable-orchestration-install (${ORCH_SCOPE_FLAG})..."
    SABLE_PROJECT_DIR="${PROJECT_ROOT}" bash "${REPO_DIR}/bin/sable-orchestration-install" "${ORCH_SCOPE_FLAG}"
    settings_guard_settle
else
    yellow "  bin/sable-orchestration-install not found — skipping the orchestration layer"
fi
echo

# 7. Prepend Prime Directives to the scope's CLAUDE.md (with backup). Under
# --project this is <project>/CLAUDE.md, NOT the global ~/.claude/CLAUDE.md.
bold "Step 7/8: Add Prime Directives to ${PRIME_TARGET}"
if [ ! -f "${PRIME_TEMPLATE}" ]; then
    red "  Missing template: ${PRIME_TEMPLATE}"
    exit 1
fi

if [ "$DRY_RUN" = "1" ]; then
    printf '  would prepend Prime Directives to %s (if not already present)\n' "${PRIME_TARGET}"
elif [ -f "${PRIME_TARGET}" ] && grep -q "Prime Directive" "${PRIME_TARGET}"; then
    yellow "  Prime Directive already present — skipping CLAUDE.md edit"
else
    if [ -f "${PRIME_TARGET}" ]; then
        BACKUP="${PRIME_TARGET}.bak.$(date +%Y%m%d%H%M%S)"
        cp "${PRIME_TARGET}" "${BACKUP}"
        yellow "  Backed up existing CLAUDE.md to ${BACKUP}"
        cat "${PRIME_TEMPLATE}" "${BACKUP}" > "${PRIME_TARGET}"
    else
        cp "${PRIME_TEMPLATE}" "${PRIME_TARGET}"
    fi
    green "  Prime Directives prepended"
fi
echo

# 8. Print the BASE-tier settings.json snippet for manual pasting.
#
# HISTORY OF THIS COMMENT, kept because it is the shape of the defect. It once
# read "do NOT auto-edit — settings is too important to clobber" as if that
# covered the whole file, while step 6's delegate merged the orchestration rows
# into that same file on every run (SABLE-nn54x narrowed the wording to the
# base-tier block and said so). SABLE-gxsji then took the other half: rather
# than documenting the auto-merge, the auto-merge now requires consent. So the
# promise is once again whole-file, and this time it is enforced in code by the
# settings-guard around step 6 rather than asserted in prose.
bold "Step 8/8: Settings.json hook block (base tier — paste this yourself)"
echo "Add the following block to your ${SETTINGS_FILE} under the top-level 'hooks' key."
echo "If you already have a 'hooks' key, merge carefully (don't overwrite existing entries)."
echo "NOTE: this block is the BASE tier only. The orchestration (multi-manager) rows are"
echo "printed by step 6 above and are NOT applied unless you re-run with --merge-settings."
echo
cat <<EOF
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {"type": "command", "command": "bash ${HOOK_CMD_ROOT}/tdd-evidence.sh", "timeout": 3000},
          {"type": "command", "command": "bash ${HOOK_CMD_ROOT}/tdd-gate.sh", "timeout": 5000},
          {"type": "command", "command": "bash ${HOOK_CMD_ROOT}/bead-description-gate.sh", "timeout": 3000}
        ]
      },
      {
        "matcher": "Edit|Write",
        "hooks": [
          {"type": "command", "command": "bash ${HOOK_CMD_ROOT}/tdd-remind.sh", "timeout": 3000}
        ]
      },
      {
        "matcher": "Agent",
        "hooks": [
          {"type": "command", "command": "bash ${HOOK_CMD_ROOT}/agent-tdd-enforce.sh", "timeout": 3000}
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {"type": "command", "command": "bash ${HOOK_CMD_ROOT}/bead-quality.sh", "timeout": 5000}
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

# 9. Record install provenance (SABLE-78kxu) — without this, "is X deployed?"
# is unanswerable: sable-doctor's manifest check only asks "do installed files
# MATCH the tree?", so a file that doesn't exist in the tree yet is invisible
# to it and a "clean" report is fully compatible with a not-yet-merged guard
# being entirely absent. Best-effort: a repo dir that isn't a git checkout
# (unlikely for this installer) skips silently rather than failing the install.
bold "Recording install provenance"
if [ "$DRY_RUN" = "1" ]; then
    yellow "  would write: ${CLAUDE_DIR}/.sable-install-provenance"
elif command -v git >/dev/null 2>&1 && PROV_SHA="$(git -C "${REPO_DIR}" rev-parse HEAD 2>/dev/null)"; then
    PROV_BRANCH="$(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")"
    # "dirty" is a reproducibility claim: can the recorded SHA reconstruct the
    # INSTALLED SET? install.sh installs TRACKED content, so only tracked
    # modifications break that (-uno excludes untracked entries). Untracked
    # presence is a different fact — it does not affect reproducibility from
    # the recorded SHA — so it gets its own field rather than being folded
    # into "dirty" (SABLE-dt92b: an untracked-only tree was stamping DIRTY /
    # "not reproducible", which is false).
    if [ -n "$(git -C "${REPO_DIR}" status --porcelain -uno 2>/dev/null)" ]; then
        PROV_DIRTY="true"
    else
        PROV_DIRTY="false"
    fi
    if git -C "${REPO_DIR}" status --porcelain 2>/dev/null | grep -q '^??'; then
        PROV_UNTRACKED="true"
    else
        PROV_UNTRACKED="false"
    fi
    PROV_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    {
        printf 'commit=%s\n' "${PROV_SHA}"
        printf 'branch=%s\n' "${PROV_BRANCH}"
        printf 'dirty=%s\n' "${PROV_DIRTY}"
        printf 'untracked=%s\n' "${PROV_UNTRACKED}"
        printf 'timestamp=%s\n' "${PROV_TIMESTAMP}"
    } > "${CLAUDE_DIR}/.sable-install-provenance"
    PROV_NOTE=""
    if [ "${PROV_DIRTY}" = "true" ]; then
        PROV_NOTE="DIRTY tree — not reproducible"
    fi
    if [ "${PROV_UNTRACKED}" = "true" ]; then
        if [ -n "${PROV_NOTE}" ]; then
            PROV_NOTE="${PROV_NOTE}; untracked files present"
        else
            PROV_NOTE="untracked files present"
        fi
    fi
    if [ -n "${PROV_NOTE}" ]; then
        green "  installed from ${PROV_SHA} (${PROV_BRANCH}) — ${PROV_NOTE}"
    else
        green "  installed from ${PROV_SHA} (${PROV_BRANCH})"
    fi
else
    yellow "  Could not determine repo commit (not a git checkout?) — skipping provenance stamp."
fi
echo

bold "Orchestration hooks"
echo "${SETTINGS_CONTRACT}"
echo "Step 6 above printed the exact add/remove/modify set the orchestration snippet"
echo "wants, and (unless you passed --merge-settings) parked the would-be result as a"
echo ".proposed file next to your settings.json instead of applying it."
echo "sable-orchestration-install also STAGES (never activates) the reconciliation"
echo "floor's host timer artifacts (systemd --user unit + cron fallback line) under"
echo "${CLAUDE_DIR}/sable/reconcile-timer/. Activate with the ONE self-verifying"
echo "command it prints above — 'sable-reconcile-timer --install-schedule' — and"
echo "re-check any time with 'sable-reconcile-timer --check-schedule --repo <repo>',"
echo "which fails loudly when nothing is scheduled OR when what is scheduled sweeps"
echo "a different repo (SABLE-jfg6.5 / D3 TIMER LEG; SABLE-5xz68)."
echo

bold "Install complete."
echo
echo "Next steps:"
echo "  1. Paste the BASE-tier hook block above into ${SETTINGS_FILE} (merge with existing"
echo "     config), and apply the orchestration rows step 6 reported — either by merging its"
echo "     .proposed file yourself or by re-running: bash install.sh --merge-settings"
echo "  2. In your project: bd init && bd hooks install"
echo "  3. RESTART Claude Code so the agent defs, /sable-plan /sable-execute /gaudi /columbo, and hooks register."
echo "  4. Start your session:  sable-launch   (Lincoln only, wraps sable-tmux; managers spawn on demand)"
echo "  5. Open a fresh agent session and use the bootstrap prompt from QUICKSTART.md"
echo "  6. Verify: see 'Verify the install' section of QUICKSTART.md — or type: sable --help"
