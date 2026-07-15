#!/usr/bin/env bash
# test-lib-registry-path.sh — unit tests for sable_registry_path in
# hooks/multi-manager/lib-registry-path.sh (SABLE-59t6.1)
#
# Contract under test:
#   sable_registry_path [base_dir] prints the agents.yaml registry path.
#   Resolution order:
#     1. $SABLE_AGENTS_YAML if set and non-empty (unified override).
#     2. base_dir (default PWD) inside a git work tree → the MAIN worktree root
#        (parent of the shared git common-dir, so every linked worktree of a
#        project resolves to ONE shared file) + /.claude/sable/agents.yaml,
#        but ONLY when that file EXISTS (project ships its own registry).
#     3. otherwise → $HOME/.claude/sable/agents.yaml (legacy global path; also
#        the byte-identical dormant fallback when no registry exists anywhere).
#
# Structure mirrors test-lib-mode-path.sh (SABLE-5hck.1) 5-case shape, including
# the linked-worktree git-worktree-add fixture reused verbatim.
#
# Run with:
#   bash hooks/test/test-lib-registry-path.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LIB="$REPO_ROOT/hooks/multi-manager/lib-registry-path.sh"

PASS=0
FAIL=0
FAIL_NAMES=""

pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() {
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  $1"
  echo "FAIL: $1"
  [ -n "${2:-}" ] && echo "  $2"
}
assert_eq() {
  # name expected actual
  if [ "$2" = "$3" ]; then pass "$1"; else fail "$1" "expected '$2', got '$3'"; fi
}

if [ ! -f "$LIB" ]; then
  echo "FAIL: lib not found at $LIB (resolver not implemented yet)"
  exit 2
fi
# shellcheck source=../multi-manager/lib-registry-path.sh
. "$LIB"

# Canonicalize a directory the same way the resolver does, so expected paths
# match regardless of /tmp symlink quirks (e.g. macOS /tmp -> /private/tmp).
canon() { ( cd "$1" && pwd ); }

# A throwaway git repo with one commit (worktree add needs a HEAD).
make_repo() {
  local d; d="$(mktemp -d)"
  git -C "$d" init -q
  git -C "$d" -c user.email=t@t -c user.name=t commit --allow-empty -m init -q
  printf '%s\n' "$d"
}

# Write a project registry (<repo>/.claude/sable/agents.yaml) into a repo.
seed_project_registry() {
  local repo="$1"
  mkdir -p "$repo/.claude/sable"
  cat > "$repo/.claude/sable/agents.yaml" <<'YAML'
agents:
  optimus:
    type: epic_manager
YAML
}

# Run the resolver with SABLE_AGENTS_YAML unset and HOME pinned, so the expected
# HOME-fallback path is deterministic.
resolve_no_override() {
  # $1 = base_dir  $2 = fake HOME
  ( unset SABLE_AGENTS_YAML; HOME="$2" sable_registry_path "$1" )
}

# ---------- 1. SABLE_AGENTS_YAML override wins (even with a project registry) ----------
RE1="$(make_repo)"; seed_project_registry "$RE1"
OVERRIDE="$(mktemp -u)"
GOT="$(SABLE_AGENTS_YAML="$OVERRIDE" sable_registry_path "$RE1")"
assert_eq "SABLE_AGENTS_YAML override wins over project + git resolution" "$OVERRIDE" "$GOT"

# ---------- 2. in-repo project registry resolves the repo's own agents.yaml ----------
FAKEHOME="$(mktemp -d)"
RE1_C="$(canon "$RE1")"
GOT="$(resolve_no_override "$RE1" "$FAKEHOME")"
assert_eq "git repo with a project registry resolves the repo's agents.yaml" \
  "$RE1_C/.claude/sable/agents.yaml" "$GOT"

# ---------- 3. linked worktree resolves the MAIN checkout's registry ----------
# Registry lives only in the MAIN checkout; a linked worktree (its own dir has no
# registry) must still resolve to the main repo's file via the shared common-dir.
WT="$(mktemp -u)"
git -C "$RE1" worktree add -q "$WT" -b wt-registry-path-test
GOT="$(resolve_no_override "$WT" "$FAKEHOME")"
assert_eq "linked worktree resolves the MAIN checkout's project registry" \
  "$RE1_C/.claude/sable/agents.yaml" "$GOT"

# ---------- 4. non-git base dir → HOME fallback (legacy global path) ----------
ND="$(mktemp -d)"
GOT="$(resolve_no_override "$ND" "$FAKEHOME")"
assert_eq "non-git base dir falls back to HOME global path" \
  "$FAKEHOME/.claude/sable/agents.yaml" "$GOT"

# ---------- 5. git repo WITHOUT a project registry → HOME (dormant fallback) ----------
# The deliberate departure from lib-mode-path: a repo that ships NO agents.yaml
# stays on today's global-registry behavior, byte-identical to the old default.
RE5="$(make_repo)"
GOT="$(resolve_no_override "$RE5" "$FAKEHOME")"
assert_eq "git repo absent a project registry falls back to HOME (dormant, byte-identical)" \
  "$FAKEHOME/.claude/sable/agents.yaml" "$GOT"

# ---------- bonus: no base-dir arg uses PWD ----------
GOT="$( unset SABLE_AGENTS_YAML; cd "$RE1" && HOME="$FAKEHOME" sable_registry_path )"
assert_eq "no-arg resolves from PWD (project registry present)" \
  "$RE1_C/.claude/sable/agents.yaml" "$GOT"

# ---------- cleanup ----------
git -C "$RE1" worktree remove --force "$WT" 2>/dev/null || rm -rf "$WT"
rm -rf "$RE1" "$RE5" "$ND" "$FAKEHOME"

# ---------- Summary ----------
echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
  echo -e "Failed tests:$FAIL_NAMES"
  exit 1
fi
exit 0
