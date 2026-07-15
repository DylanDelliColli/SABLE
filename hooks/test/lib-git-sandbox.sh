#!/usr/bin/env bash
# lib-git-sandbox.sh — sourceable test-harness preamble that env-neutralizes
# real-repo git escapes for the duration of a shell test suite (SABLE-0ssz.2).
#
# WHY THIS EXISTS
# --------------
# Three P0s on 2026-07-14 shared ONE mechanism: a test fixture ran a real git
# operation that escaped its sandbox and hit the REAL repo / shared git state.
#   - SABLE-xydb: a fixture `git push origin HEAD refs/heads/main` fired in the
#     real worktree and corrupted origin/main.
#   - SABLE-a5a5: a fixture bare `git config user.name Test` wrote the SHARED
#     .git/config (worktree common-dir), mis-authoring commits and mis-actoring
#     every bd write.
#   - SABLE-l1jf: the downstream identity symptoms.
# In every case the ROOT enabler was an UNGUARDED `cd` into a mktemp fixture:
# when the cd failed (a busy-/tmp race), CWD silently stayed in the real
# worktree and the following bare git ops ran THERE.
#
# WHAT THIS NEUTRALIZES (and what it cannot)
# ------------------------------------------
# Sourcing this file, for the suite duration:
#   1. Anchors the suite's ambient CWD to a throwaway SANDBOX git repo (never
#      the real worktree). An unguarded-cd escape therefore lands in the
#      sandbox, not the real repo — this is the direct antidote to the xydb /
#      a5a5 mechanism.
#   2. Points GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM at a throwaway config that
#      carries a valid throwaway identity, so a stray `git config --global` (or
#      a fixture that relies on ambient identity) can never touch the user's
#      real global config, and commits still work.
#   3. Rewrites the real origin URL (and the common real-remote prefixes) to the
#      sandbox bare repo via url.<sandbox>.insteadOf, so even a push that somehow
#      runs against the real remote name is redirected to the throwaway bare.
#   4. Unsets GIT_DIR / GIT_WORK_TREE, so a stray git op has no INHERITED
#      real-repo target.
#   5. Gives the sandbox repo its OWN `origin` = a throwaway bare, so a bare
#      `git push origin <ref>` executed at the ambient CWD reaches the throwaway,
#      not the real remote.
#
# HONEST LIMIT: env-level neutralization CANNOT stop an op that EXPLICITLY names
# the real repo, e.g. `git -C /path/to/real/repo branch -D x`. An explicit -C
# overrides GIT_DIR and CWD. That class (SABLE-0ssz.4) is caught by the
# bin/sable-fixture-tripwire static check, not by this preamble — the preamble
# defeats the AMBIENT/CWD escape class, the tripwire bans the EXPLICIT-path one.
#
# USAGE
# -----
#   # near the top of a suite, AFTER it has resolved its own absolute paths:
#   source "$(dirname "$0")/lib-git-sandbox.sh"
#
# Exported for the sourcing suite:
#   SABLE_TEST_SANDBOX          the throwaway sandbox root (auto-removed on EXIT)
#   SABLE_TEST_SANDBOX_ORIGIN   the throwaway bare origin inside it
#   SABLE_TEST_REAL_REPO        the real worktree root captured before anchoring
#   SABLE_TEST_ORIG_PWD         the suite's CWD at source time
#
# Opt-out knobs (export BEFORE sourcing):
#   SABLE_TEST_SANDBOX_NO_CD=1  keep the env redirects but do NOT cd into the
#                               sandbox (for suites that cannot tolerate a CWD
#                               change; weaker — the ambient-cd escape is only
#                               fully neutralized WITH the cd).

# ---------------------------------------------------------------------------

sable_test_git_sandbox_cleanup() {
  # Idempotent; scoped strictly to our own sandbox dir.
  [ -n "${SABLE_TEST_SANDBOX:-}" ] || return 0
  case "$SABLE_TEST_SANDBOX" in
    /tmp/*|"${TMPDIR:-/tmp}"/*) rm -rf "$SABLE_TEST_SANDBOX" ;;
  esac
  unset SABLE_TEST_SANDBOX SABLE_TEST_SANDBOX_ORIGIN
}

sable_test_git_sandbox_init() {
  # Re-entrancy guard: a suite sourcing this twice is a no-op the second time.
  [ -n "${SABLE_TEST_SANDBOX:-}" ] && return 0

  SABLE_TEST_ORIG_PWD="$PWD"
  SABLE_TEST_REAL_REPO="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  local real_origin
  real_origin="$(git config --get remote.origin.url 2>/dev/null || true)"

  SABLE_TEST_SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/sable-test-sandbox.XXXXXX")" || {
    echo "FATAL: lib-git-sandbox.sh could not create a sandbox dir" >&2
    return 1
  }

  local gc="$SABLE_TEST_SANDBOX/gitconfig"
  # A valid throwaway identity so fixtures that never set a local identity still
  # commit — without ever reading or writing the user's real global config.
  git config --file "$gc" user.name  "SABLE Test Sandbox"
  git config --file "$gc" user.email "sandbox@sable.test.invalid"
  git config --file "$gc" init.defaultBranch master
  # Allow pushes to a local/file bare (some git builds gate this by default).
  git config --file "$gc" protocol.file.allow always

  SABLE_TEST_SANDBOX_ORIGIN="$SABLE_TEST_SANDBOX/origin.git"
  git init -q --bare "$SABLE_TEST_SANDBOX_ORIGIN"
  git init -q "$SABLE_TEST_SANDBOX/repo"
  git -C "$SABLE_TEST_SANDBOX/repo" remote add origin "$SABLE_TEST_SANDBOX_ORIGIN"

  # Redirect any push that names the REAL remote to the throwaway bare. The
  # exact discovered origin URL is the load-bearing entry; the broad prefixes
  # are belt-and-suspenders for the common real-remote forms.
  if [ -n "$real_origin" ]; then
    git config --file "$gc" url."$SABLE_TEST_SANDBOX_ORIGIN".insteadOf "$real_origin"
  fi
  local pfx
  for pfx in "git@github.com:" "https://github.com/" "ssh://git@github.com/" \
             "git://github.com/" "http://github.com/"; do
    git config --file "$gc" --add url."$SABLE_TEST_SANDBOX_ORIGIN".insteadOf "$pfx"
  done

  export GIT_CONFIG_GLOBAL="$gc"
  export GIT_CONFIG_SYSTEM=/dev/null
  unset GIT_DIR GIT_WORK_TREE

  if [ "${SABLE_TEST_SANDBOX_NO_CD:-0}" != "1" ]; then
    cd "$SABLE_TEST_SANDBOX/repo" || {
      echo "FATAL: lib-git-sandbox.sh could not cd into the sandbox repo" >&2
      return 1
    }
  fi

  # Auto-clean the sandbox on suite exit. We deliberately do NOT chain onto an
  # existing EXIT trap: `( )` subshells INHERIT their parent's EXIT trap and
  # fire it on subshell exit, so chaining would re-run a parent's cleanup
  # PREMATURELY (and could destroy shared state). Setting our own trap instead
  # OVERRIDES the inherited one inside a subshell — the safe behavior. A suite
  # that sets its own EXIT trap AFTER sourcing should call
  # sable_test_git_sandbox_cleanup from it (the sandbox lives under $TMPDIR, so a
  # missed cleanup is harmless litter, never a correctness issue).
  trap 'sable_test_git_sandbox_cleanup' EXIT
  export SABLE_TEST_SANDBOX SABLE_TEST_SANDBOX_ORIGIN SABLE_TEST_REAL_REPO SABLE_TEST_ORIG_PWD
}

# Auto-run on source so a suite gets protection from a single `source` line.
sable_test_git_sandbox_init
