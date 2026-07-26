#!/usr/bin/env bash
# test-role-card-install.sh — locks the copy-install leg of "merged != present
# != executing" for the SABLE role cards (SABLE-2c2wb).
#
# SABLE-4snb4 added the sable-contained containment mandate to
# templates/multi-manager/roles/optimus.md and tarzan.md; chuck.md was left
# out because the block was hand-pasted onto the two cards its author was
# thinking about, not derived from which roles the install pipeline actually
# ships. Nothing before this test exercised the REAL install path at all —
# a change to the template could silently fail to reach the installed copy
# and nothing would catch it.
#
# bin/sable-orchestration-install (--user scope) copy-installs exactly FOUR
# role cards to $CLAUDE_USER_DIR/sable/roles/: lincoln, optimus, tarzan,
# chuck — the warm tmux-pane execution roles (session-role-anchor injects
# their identity from these files). The other four cards (columbo, rudy,
# sherlock, victor) are session-scoped Agent-tool "producers": they are never
# copy-installed here at all — bin/sable-build-agents wraps them into
# templates/agents/<name>.md instead (verified by
# hooks/test/test-agent-definitions.sh, a separate distribution path). A test
# asserting byte-equality for all eight roles against $CLAUDE_USER_DIR would
# fail by construction for those four — this test locks the REAL install
# surface, not an assumed one.
#
# Runs the REAL install.sh against a temp HOME (CLAUDE_USER_DIR), no mocked
# copy step, and diffs every installed pane-role card against its template
# per-named-role (not a global file count, so a single missing/wrong card is
# attributable rather than lost in an aggregate pass).
#
# Run with: bash hooks/test/test-role-card-install.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALLER="$REPO/bin/sable-orchestration-install"
ROLES_DIR="$REPO/templates/multi-manager/roles"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

if [ ! -x "$INSTALLER" ]; then
  echo "FAIL: installer not executable at $INSTALLER"
  exit 2
fi

TMPHOME="$(mktemp -d)"
trap 'rm -rf "$TMPHOME"' EXIT

CLAUDE_USER_DIR="$TMPHOME/.claude" bash "$INSTALLER" --user >/dev/null 2>&1

INSTALLED_ROLES="$TMPHOME/.claude/sable/roles"

# --- the four warm tmux-pane execution roles: MUST be installed, byte-identical ---
for role in lincoln optimus tarzan chuck; do
  template="$ROLES_DIR/$role.md"
  installed="$INSTALLED_ROLES/$role.md"
  if [ ! -f "$installed" ]; then
    fail "$role.md copy-installed to \$CLAUDE_USER_DIR/sable/roles/" "missing: $installed"
    continue
  fi
  if diff -q "$template" "$installed" >/dev/null 2>&1; then
    pass "$role.md installed byte-identical to its template"
  else
    fail "$role.md installed byte-identical to its template" "$(diff "$template" "$installed" | head -5)"
  fi
done

# --- the four session-scoped Agent-tool producers: never copy-installed here ---
for role in columbo rudy sherlock victor; do
  installed="$INSTALLED_ROLES/$role.md"
  if [ -e "$installed" ]; then
    fail "$role.md correctly absent from the pane-role install (producer, not a pane)" "unexpectedly present: $installed"
  else
    pass "$role.md correctly absent from the pane-role install (producer, not a pane)"
  fi
done

echo
echo "== Results: $PASS passed, $FAIL failed =="
if [ "$FAIL" -gt 0 ]; then
  echo -e "Failed:$FAIL_NAMES"
  exit 1
fi
exit 0
