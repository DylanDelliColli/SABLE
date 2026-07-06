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
lacks_re(){ ! grep -Eq -- "$2" "$1" 2>/dev/null && pass "$1 lacks /$2/" || fail "$1 lacks /$2/" "still present"; }

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

# Settings snippet points at the renamed hook (the teams snippet was deleted
# with the teams topology, SABLE-qa4d.4)
has templates/multi-manager/settings-snippet.json "mode-interlock.sh"
lacks templates/multi-manager/settings-snippet.json "cockpit-mode-interlock.sh"

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

# --- SABLE-d50.3: residual cockpit-prose scrub + slash-command mapping ---
# Fully-scrubbed files: the bare word 'cockpit' is gone. (It legitimately
# survives as the registry type/env-name identifier in agents.yaml + the hook
# code, and as the mode-machinery name in lincoln.md / the topology docs — those
# are owned by other beads and intentionally NOT asserted here.)
for f in skills/sable-plan/SKILL.md \
         skills/sable-execute/SKILL.md \
         templates/multi-manager/roles/sherlock.md \
         templates/agents/sherlock.md \
         templates/agents-teams/sherlock.md \
         SABLE.md \
         hooks/multi-manager/session-role-anchor.sh; do
  lacks "$f" "cockpit"
done
# Mixed files: prose scrubbed but legit identifiers (type list / env-name match)
# retained — assert the specific scrubbed phrases are gone, not the whole word.
lacks hooks/multi-manager/mode-interlock.sh "cockpit seat"
lacks hooks/multi-manager/mode-interlock.sh "cockpit main"
lacks hooks/multi-manager/mode-interlock.sh "from the cockpit"
lacks hooks/multi-manager/lib-identity.sh 'cockpit" default'

# Slash-command mapping: the skills resolve as /sable-plan and /sable-execute
# (their frontmatter names — there is no /plan or /execute command alias). The
# word-boundary regex catches the bare stale form while sparing /sable-plan and
# the unrelated /plan-ceo-review skill (a '-' after 'plan' is excluded).
for f in skills/sable-execute/SKILL.md \
         QUICKSTART.md \
         MULTI-MANAGER-PATTERN.md \
         PERSONAL-TOOLING.md \
         templates/multi-manager/agents.yaml \
         templates/multi-manager/roles/lincoln.md \
         hooks/multi-manager/mode-interlock.sh; do
  lacks_re "$f" "/plan([^A-Za-z-]|$)"
  lacks_re "$f" "/execute([^A-Za-z-]|$)"
done
has skills/sable-execute/SKILL.md "/sable-execute"

# --- SABLE-d50.5: ppy/86n-owned doc residuals the de-cockpit sweep missed ---
# roles/cockpit.md was renamed to roles/lincoln.md (a dead path before this);
# the v1 Zellij surface (sable-cockpit / sable-status / sable.kdl) is DELETED,
# not merely "deprecated, not deleted". In PERSONAL-TOOLING 'cockpit' survives
# only as the CLAUDE_AGENT_NAME env identity, so assert the prose phrases —
# QUICKSTART and MULTI-MANAGER-PATTERN scrub fully.
lacks PERSONAL-TOOLING.md "roles/cockpit.md"
lacks PERSONAL-TOOLING.md "Cockpit"
lacks PERSONAL-TOOLING.md "the cockpit"
lacks PERSONAL-TOOLING.md "sable-cockpit"
lacks PERSONAL-TOOLING.md "sable-status"
lacks QUICKSTART.md "cockpit"
lacks MULTI-MANAGER-PATTERN.md "cockpit"
lacks MULTI-MANAGER-PATTERN.md "sable-status"

if [ "$fails" -eq 0 ]; then printf 'PASS test-no-cockpit-naming\n'; else printf 'FAIL test-no-cockpit-naming (%d)\n' "$fails"; exit 1; fi
