#!/usr/bin/env bash
# test-no-cockpit-naming.sh — guards the de-cockpit rename (SABLE-d50.2).
# Asserts the post-rename state: cockpit is gone from user-facing names and the
# live mode-machinery source, replaced by the sable-/Orchestration namespace.
# Excludes the dying Zellij surface (SABLE-ppy deletes those) and the design docs
# (ENTRY-POINTS-DESIGN.md intentionally records the old->new mapping).
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
cd "$REPO"
fails=0
pass() { printf '  ok  %s\n' "$1"; }
fail() { printf '  FAIL %s — %s\n' "$1" "${2:-}"; fails=$((fails+1)); }
exists() { [ -e "$1" ] && pass "exists: $1" || fail "exists: $1" "missing"; }
absent() { [ ! -e "$1" ] && pass "absent: $1" || fail "absent: $1" "still present"; }
has()  { grep -q -- "$2" "$1" 2>/dev/null && pass "$1 has '$2'" || fail "$1 has '$2'" "not found"; }
lacks(){ ! grep -q -- "$2" "$1" 2>/dev/null && pass "$1 lacks '$2'" || fail "$1 lacks '$2'" "still present"; }

# Skills renamed + frontmatter
exists skills/sable-plan/SKILL.md
exists skills/sable-execute/SKILL.md
absent skills/cockpit-plan
absent skills/cockpit-execute
has skills/sable-plan/SKILL.md "name: sable-plan"
has skills/sable-execute/SKILL.md "name: sable-execute"

# Interlock hook renamed
exists hooks/multi-manager/mode-interlock.sh
absent hooks/multi-manager/cockpit-mode-interlock.sh

# Installer renamed
exists bin/sable-orchestration-install
absent bin/sable-cockpit-install

# Install flag + env
has install.sh "--orchestration"
has install.sh "SABLE_ORCHESTRATION"
lacks install.sh "--cockpit"
lacks install.sh "SABLE_MULTI_MANAGER"
lacks install.sh "SABLE_COCKPIT"

# Settings snippets point at the renamed hook
has templates/multi-manager/settings-snippet.json "mode-interlock.sh"
lacks templates/multi-manager/settings-snippet.json "cockpit-mode-interlock.sh"
has templates/multi-manager/settings-snippet-teams.json "mode-interlock.sh"
lacks templates/multi-manager/settings-snippet-teams.json "cockpit-mode-interlock.sh"

# Live mode machinery — no SABLE_COCKPIT* tokens, no cockpit-mode.json
for f in bin/sable-mode hooks/multi-manager/mode-interlock.sh hooks/multi-manager/lib-identity.sh; do
  lacks "$f" "SABLE_COCKPIT"
  lacks "$f" "cockpit-mode.json"
done
# Mode-state override env var unified to SABLE_MODE_STATE across all three
# components; the retired SABLE_MODE_FILE name is gone (SABLE-d50.4)
has bin/sable-mode "SABLE_MODE_STATE"
has hooks/multi-manager/lib-identity.sh "SABLE_MODE_STATE"
lacks hooks/multi-manager/lib-identity.sh "SABLE_MODE_FILE"
has hooks/multi-manager/mode-interlock.sh "SABLE_ORCHESTRATION_FORCE"

# COCKPIT-DESIGN.md folded away
absent COCKPIT-DESIGN.md

if [ "$fails" -eq 0 ]; then printf 'PASS test-no-cockpit-naming\n'; else printf 'FAIL test-no-cockpit-naming (%d)\n' "$fails"; exit 1; fi
