#!/usr/bin/env bash
# test-role-card-install.sh — locks the copy-install leg of "merged != present
# != executing" for the SABLE role cards and registry (SABLE-2c2wb).
#
# SABLE-4snb4 added the sable-contained containment mandate to
# templates/multi-manager/roles/optimus.md and tarzan.md; chuck.md was left
# out because the block was hand-pasted onto the two cards its author was
# thinking about, not derived from which roles the install pipeline actually
# ships. Nothing before this test exercised the REAL install path at all —
# a change to the template could silently fail to reach the installed copy
# and nothing would catch it.
#
# bin/sable-orchestration-install (--user scope) derives its copy-installed
# role-card set from templates/multi-manager/roles/*.md. This keeps bounded
# producer panes such as victor on the same role-anchor path and makes a newly
# shipped card installable without a second hand-maintained enumeration.
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
REGISTRY="$REPO/templates/multi-manager/agents.yaml"

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

# --- every shipped role card: MUST be installed, byte-identical ---
for template in "$ROLES_DIR"/*.md; do
  role="$(basename "$template" .md)"
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

# The installed registry selects those role cards and encodes the
# planning-producer/execution-pane boundary, so it belongs to the same
# byte-identity contract as the cards.
INSTALLED_REGISTRY="$TMPHOME/.claude/sable/agents.yaml"
if diff -q "$REGISTRY" "$INSTALLED_REGISTRY" >/dev/null 2>&1; then
  pass "agents.yaml installed byte-identical to its template"
else
  fail "agents.yaml installed byte-identical to its template" \
    "$(diff "$REGISTRY" "$INSTALLED_REGISTRY" | head -5)"
fi

echo
echo "== Results: $PASS passed, $FAIL failed =="
if [ "$FAIL" -gt 0 ]; then
  echo -e "Failed:$FAIL_NAMES"
  exit 1
fi
exit 0
