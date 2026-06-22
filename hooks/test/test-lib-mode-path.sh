#!/usr/bin/env bash
# test-lib-mode-path.sh — unit tests for sable_mode_state_path in
# hooks/multi-manager/lib-mode-path.sh (SABLE-5hck.1)
#
# Contract under test:
#   sable_mode_state_path [base_dir] prints the mode-state file path.
#   Resolution order:
#     1. $SABLE_MODE_STATE if set and non-empty (test override + escape hatch).
#     2. base_dir (default PWD) inside a git work tree → the MAIN worktree root
#        (parent of the shared git common-dir, so every linked worktree of a
#        project resolves to ONE shared file) + /.claude/sable/state/mode-state.json
#     3. otherwise → $HOME/.claude/sable/state/mode-state.json (legacy global path).
#
# Run with:
#   bash hooks/test/test-lib-mode-path.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LIB="$REPO_ROOT/hooks/multi-manager/lib-mode-path.sh"

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
# shellcheck source=../multi-manager/lib-mode-path.sh
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

# ---------- 1. SABLE_MODE_STATE override wins (even inside a git repo) ----------
OVERRIDE="$(mktemp -u)"
GOT="$(SABLE_MODE_STATE="$OVERRIDE" sable_mode_state_path "$REPO_ROOT")"
assert_eq "SABLE_MODE_STATE override wins over git resolution" "$OVERRIDE" "$GOT"

# ---------- 2. plain git repo → in-repo path ----------
RA="$(make_repo)"; RA_C="$(canon "$RA")"
GOT="$(sable_mode_state_path "$RA")"
assert_eq "git repo resolves to in-repo state path" \
  "$RA_C/.claude/sable/state/mode-state.json" "$GOT"

# ---------- 3. linked worktree resolves to the MAIN repo's path ----------
WT="$(mktemp -u)"
git -C "$RA" worktree add -q "$WT" -b wt-mode-path-test
WT_C="$(canon "$WT")"
GOT="$(sable_mode_state_path "$WT")"
assert_eq "linked worktree shares the main checkout's mode path" \
  "$RA_C/.claude/sable/state/mode-state.json" "$GOT"

# ---------- 4. non-git base dir → HOME fallback (legacy global path) ----------
ND="$(mktemp -d)"
FAKEHOME="$(mktemp -d)"
GOT="$(HOME="$FAKEHOME" sable_mode_state_path "$ND")"
assert_eq "non-git base dir falls back to HOME global path" \
  "$FAKEHOME/.claude/sable/state/mode-state.json" "$GOT"

# ---------- 5. no base-dir arg uses PWD ----------
GOT="$(cd "$RA" && sable_mode_state_path)"
assert_eq "no-arg resolves from PWD (git repo)" \
  "$RA_C/.claude/sable/state/mode-state.json" "$GOT"

# ---------- cleanup ----------
git -C "$RA" worktree remove --force "$WT" 2>/dev/null || rm -rf "$WT"
rm -rf "$RA" "$ND" "$FAKEHOME"

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
