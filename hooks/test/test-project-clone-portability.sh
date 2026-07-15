#!/usr/bin/env bash
# test-project-clone-portability.sh — proves the committed project settings.json
# from sable-orchestration-install (SABLE-59t6.2) travels across a clone at a
# DIFFERENT absolute path: install into repoA, copy repoA/.claude verbatim to
# repoB (a different absolute path), and assert every ${CLAUDE_PROJECT_DIR}-rooted
# hook command still resolves to an existing executable under repoB/.claude.
#
# KNOWN LIMIT (accepted at test-strategy gate): this proves the wiring travels
# (paths resolve, JSON parses), not that Claude Code actually fires the hooks
# at runtime — that would require a live session.
#
# Run with:
#   bash hooks/test/test-project-clone-portability.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALLER="$REPO/bin/sable-orchestration-install"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

if [ ! -x "$INSTALLER" ]; then echo "FAIL: installer not executable at $INSTALLER"; exit 2; fi

valid_json(){ python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$1" 2>/dev/null; }

# repoA and repoB deliberately live under different mktemp roots/depths so their
# absolute paths differ in both length and content — a naive substring-replace
# bug (rewriting to repoA's absolute path instead of the placeholder) would
# produce a settings.json that resolves under repoA but 404s under repoB.
TMP_A="$(mktemp -d)"
TMP_B="$(mktemp -d)"
REPO_A="$TMP_A/nested/repoA"
REPO_B="$TMP_B/deeper/nested/path/repoB"
mkdir -p "$REPO_A" "$REPO_B"

SABLE_PROJECT_DIR="$REPO_A" bash "$INSTALLER" --project >/dev/null 2>&1

SET_A="$REPO_A/.claude/settings.json"
if [ -e "$SET_A" ]; then pass "repoA: project install wrote committed settings.json"; else fail "repoA: project install wrote committed settings.json"; fi
if valid_json "$SET_A"; then pass "repoA: settings.json is valid JSON"; else fail "repoA: settings.json is valid JSON"; fi

# Simulate a git clone to a new absolute path: copy .claude verbatim, no rewriting.
cp -R "$REPO_A/.claude" "$REPO_B/"
SET_B="$REPO_B/.claude/settings.json"

if valid_json "$SET_B"; then pass "repoB (post-clone): settings.json still parses"; else fail "repoB (post-clone): settings.json still parses"; fi

# Every hook command must resolve, under repoB's OWN absolute path, to an
# existing executable file — never repoA's path, never any other absolute path.
RESOLVE_OUT="$(REPO_B_DIR="$REPO_B" python3 - "$SET_B" <<'PY'
import json, os, sys

path = sys.argv[1]
repo_b = os.environ['REPO_B_DIR']
placeholder = '${CLAUDE_PROJECT_DIR}'

with open(path) as f:
    data = json.load(f)

commands = []
for blocks in data.get('hooks', {}).values():
    if not isinstance(blocks, list):
        continue
    for b in blocks:
        if not isinstance(b, dict):
            continue
        for h in b.get('hooks', []):
            if isinstance(h, dict) and h.get('command'):
                commands.append(h['command'])

if not commands:
    print("NO_COMMANDS")
    sys.exit(0)

bad_prefix = []
absolute_leak = []
unresolved = []
not_executable = []

for cmd in commands:
    # command is e.g. "bash ${CLAUDE_PROJECT_DIR}/.claude/hooks/multi-manager/x.sh"
    parts = cmd.split()
    pathish = parts[-1] if parts else cmd
    if not pathish.startswith(placeholder + '/.claude/hooks/'):
        bad_prefix.append(cmd)
        continue
    if repo_b in cmd:
        # the placeholder form must never already contain a baked-in absolute
        # path (that would defeat the point of the placeholder)
        absolute_leak.append(cmd)
    resolved = pathish.replace(placeholder, repo_b, 1)
    if not os.path.isfile(resolved):
        unresolved.append(resolved)
    elif not os.access(resolved, os.X_OK):
        not_executable.append(resolved)

print("COUNT: %d" % len(commands))
print("BAD_PREFIX: " + (" | ".join(bad_prefix) if bad_prefix else "-"))
print("ABSOLUTE_LEAK: " + (" | ".join(absolute_leak) if absolute_leak else "-"))
print("UNRESOLVED: " + (" | ".join(unresolved) if unresolved else "-"))
print("NOT_EXECUTABLE: " + (" | ".join(not_executable) if not_executable else "-"))
PY
)"

field() { printf '%s\n' "$RESOLVE_OUT" | grep "^$1: " | sed "s/^$1: //"; }

if [ "$(printf '%s\n' "$RESOLVE_OUT" | grep '^COUNT: ')" != "COUNT: 0" ]; then pass "repoB: settings.json has hook commands to check ($(field COUNT))"; else fail "repoB: settings.json has hook commands to check" "no hooks found"; fi
if [ "$(field BAD_PREFIX)" = "-" ]; then pass "repoB: every hook command is \${CLAUDE_PROJECT_DIR}/.claude/hooks/-rooted"; else fail "repoB: every hook command is \${CLAUDE_PROJECT_DIR}/.claude/hooks/-rooted" "$(field BAD_PREFIX)"; fi
if [ "$(field ABSOLUTE_LEAK)" = "-" ]; then pass "repoB: no absolute machine path baked into any hook command"; else fail "repoB: no absolute machine path baked into any hook command" "$(field ABSOLUTE_LEAK)"; fi
if [ "$(field UNRESOLVED)" = "-" ]; then pass "repoB: placeholder-substituted hook paths resolve to existing files under repoB"; else fail "repoB: placeholder-substituted hook paths resolve to existing files under repoB" "$(field UNRESOLVED)"; fi
if [ "$(field NOT_EXECUTABLE)" = "-" ]; then pass "repoB: resolved hook scripts are executable"; else fail "repoB: resolved hook scripts are executable" "$(field NOT_EXECUTABLE)"; fi

# Sanity: repoA's own absolute path must not appear anywhere in the settings
# file at all (proves we never fell back to the old absolute-BASE rewrite).
if grep -q "$REPO_A" "$SET_B"; then
    fail "repoB: repoA's absolute path does not appear anywhere in the cloned settings.json" "found $REPO_A in $SET_B"
else
    pass "repoB: repoA's absolute path does not appear anywhere in the cloned settings.json"
fi

rm -rf "$TMP_A" "$TMP_B"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
