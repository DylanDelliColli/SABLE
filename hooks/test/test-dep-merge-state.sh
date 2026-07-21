#!/usr/bin/env bash
# test-dep-merge-state.sh — INTEGRATION coverage for the close-vs-merge gap
# (SABLE-d5iku).
#
# THE DEFECT, REPRODUCED END TO END
# ---------------------------------
# `bd ready` releases a dependent the instant its blocker's STATUS becomes
# closed. A dependent sequenced behind a blocker for STRUCTURAL reasons needs
# the blocker's CODE on the branch it forks from, and those two events are
# separated by the whole merge queue. This suite stands the real defect up:
#
#   real bd  — a real blocker bead and a real dependent bead in the real bd DB,
#              wired with a real `bd dep add`. The suite ASSERTS the dependent
#              is withheld while the blocker is open and ASSERTS it appears in
#              `bd ready` the moment the blocker closes. That second assertion
#              IS the false release; if bd ever becomes merge-aware (option (b)
#              of the bead) this test says so out loud instead of going stale.
#   real git — a real bare origin, a real push of the blocker's branch, a real
#              `git merge` + push to move it onto the integration branch. NO
#              stubs anywhere in the merge path.
#
# Then it asserts bin/sable-dep-check WARNS across the false-release window and
# goes SILENT once the branch actually merges. Both directions, because a check
# that only fires has traded a false-go for a false-block.
#
# Mocking either side would defeat the point: the whole claim is about the
# relationship between two real systems (bd's status graph and git's ancestry),
# and a mock of either just replays the author's assumption.
#
# CLEAN-ROOM (SABLE-59zu): bd is deliberately absent from the ci-verify
# clean-room. The GIT half — the ancestry engine, which is the part that cannot
# be stubbed — runs there unconditionally against a real repo. The bd half
# SKIPs loudly, is never counted as a pass, and prints why. Nothing here
# skip-and-exits-0 as a whole suite.
#
# Run with:
#   bash hooks/test/test-dep-merge-state.sh

set -uo pipefail

# Resolve absolute paths BEFORE the sandbox preamble cds away (SABLE-0ssz.2).
TESTDIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$TESTDIR/../.." && pwd)"
DEP_CHECK="$REPO/bin/sable-dep-check"

# Env-neutralize real-repo git escapes for the suite duration. Every git op
# below names its own fixture repo with -C; this is defence in depth.
# shellcheck source=lib-git-sandbox.sh
source "$TESTDIR/lib-git-sandbox.sh"

PASS=0
FAIL=0
SKIP=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() {
  FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"
  echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"
}
skip() { SKIP=$((SKIP+1)); echo "SKIP: $1"; }

if [ ! -x "$DEP_CHECK" ]; then
  echo "FAIL: sable-dep-check not executable at $DEP_CHECK"
  exit 2
fi

# ---------------------------------------------------------------------------
# Real git fixture: bare origin + work clone + an UNMERGED blocker branch
# ---------------------------------------------------------------------------
FIX="$(mktemp -d)"
trap 'rm -rf "$FIX"' EXIT

ORIGIN="$FIX/origin.git"
WORK="$FIX/work"
INT_BRANCH="tmux-only"
BLOCKER_BRANCH="wk-depmerge-$$"

git init -q --bare "$ORIGIN"
git init -q "$WORK"
git -C "$WORK" config user.email "test@example.invalid"
git -C "$WORK" config user.name "SABLE Test"
# The tool must judge ancestry against the repo's OWN integration branch, so
# configure it exactly as a real SABLE checkout does.
git -C "$WORK" config sable.integrationBranch "$INT_BRANCH"
git -C "$WORK" remote add origin "$ORIGIN"

git -C "$WORK" checkout -q -b "$INT_BRANCH"
echo "base layout" > "$WORK/layout.txt"
git -C "$WORK" add layout.txt
git -C "$WORK" commit -qm "base"
git -C "$WORK" push -q origin "$INT_BRANCH"

# The blocker's work: the structural prerequisite a dependent would fork onto.
git -C "$WORK" checkout -q -b "$BLOCKER_BRANCH"
echo "snapshot unit" > "$WORK/prerequisite.txt"
git -C "$WORK" add prerequisite.txt
git -C "$WORK" commit -qm "blocker: define the snapshot unit"
git -C "$WORK" push -q origin "$BLOCKER_BRANCH"
git -C "$WORK" checkout -q "$INT_BRANCH"
git -C "$WORK" fetch -q origin

# Precondition, stated as an assertion so nothing below can pass vacuously:
# pushed and NOT merged is the exact state of the live incident.
git -C "$WORK" merge-base --is-ancestor "origin/$BLOCKER_BRANCH" "origin/$INT_BRANCH"
if [ $? -eq 1 ]; then
  pass "fixture: blocker branch is pushed to origin and NOT merged (the false-release window)"
else
  fail "fixture: blocker branch is pushed to origin and NOT merged (the false-release window)" \
       "merge-base --is-ancestor did not report unmerged"
fi

# ---------------------------------------------------------------------------
# Real bd: blocker + dependent, really wired, really closed
# ---------------------------------------------------------------------------
BLOCKER_ID=""
DEPENDENT_ID=""
bd_cleanup() {
  [ -n "$DEPENDENT_ID" ] && bd -C "$REPO" close "$DEPENDENT_ID" --sandbox \
    --reason "[no-test] test-dep-merge-state scratch" >/dev/null 2>&1
  [ -n "$BLOCKER_ID" ] && bd -C "$REPO" close "$BLOCKER_ID" --sandbox \
    --reason "[no-test] test-dep-merge-state scratch" >/dev/null 2>&1
  rm -rf "$FIX"
}

if ! command -v bd >/dev/null 2>&1; then
  skip "bd half: bd not on PATH (SABLE-59zu clean-room) — the git ancestry half above/below still ran for real"
else
  trap bd_cleanup EXIT

  # bd is invoked with -C "$REPO" throughout: the sandbox preamble moved CWD
  # away from the real checkout, and bd auto-discovers .beads/*.db from CWD.
  BLOCKER_ID=$(bd -C "$REPO" create --sandbox -q --type=task \
    --title="[int-test] d5iku blocker ($BLOCKER_BRANCH)" \
    --description="[no-test] scratch blocker for test-dep-merge-state.sh" \
    2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9.]+' | head -1)
  DEPENDENT_ID=$(bd -C "$REPO" create --sandbox -q --type=task \
    --title="[int-test] d5iku dependent ($BLOCKER_BRANCH)" \
    --description="[no-test] scratch dependent for test-dep-merge-state.sh" \
    2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9.]+' | head -1)

  if [ -z "$BLOCKER_ID" ] || [ -z "$DEPENDENT_ID" ]; then
    fail "bd fixture: created scratch blocker + dependent beads" \
         "blocker='$BLOCKER_ID' dependent='$DEPENDENT_ID'"
  else
    echo "Integration: blocker=$BLOCKER_ID dependent=$DEPENDENT_ID branch=$BLOCKER_BRANCH"

    # The STRUCTURED branch tag bin/sable-spawn-worker writes at dispatch —
    # the same key the checker reads back.
    bd -C "$REPO" update "$BLOCKER_ID" --sandbox \
      --set-metadata "branch=$BLOCKER_BRANCH" >/dev/null 2>&1
    # A real blocking edge: dependent needs blocker.
    bd -C "$REPO" dep add "$DEPENDENT_ID" "$BLOCKER_ID" >/dev/null 2>&1

    ready_has() {
      bd -C "$REPO" ready --json 2>/dev/null | python3 -c "
import json, sys
want = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(2)
sys.exit(0 if any(isinstance(b, dict) and b.get('id') == want for b in data) else 1)
" "$1"
    }

    # --- 1: while the blocker is OPEN the dependent is correctly withheld ---
    if ready_has "$DEPENDENT_ID"; then
      fail "bd: dependent is withheld from 'bd ready' while its blocker is OPEN" \
           "$DEPENDENT_ID appeared in bd ready before the blocker closed"
    else
      pass "bd: dependent is withheld from 'bd ready' while its blocker is OPEN"
    fi

    # --- 2: closing the blocker RELEASES it — the branch is still unmerged ---
    bd -C "$REPO" close "$BLOCKER_ID" --sandbox \
      --reason "[no-test] closed before merge — reproducing the d5iku window" >/dev/null 2>&1
    if ready_has "$DEPENDENT_ID"; then
      pass "bd: closing the blocker releases the dependent into 'bd ready' WHILE its branch is unmerged (the defect, reproduced)"
    else
      fail "bd: closing the blocker releases the dependent into 'bd ready' WHILE its branch is unmerged (the defect, reproduced)" \
           "$DEPENDENT_ID did not appear in bd ready after the close — if bd became merge-aware, this suite needs rewriting, not silencing"
    fi

    # --- 3: the tooling WARNS across that window ---------------------------
    OUT=$("$DEP_CHECK" --repo "$WORK" --bd-dir "$REPO" --integration-branch "$INT_BRANCH" "$DEPENDENT_ID" 2>&1)
    RC=$?
    if [ "$RC" -eq 3 ] \
       && echo "$OUT" | grep -q 'UNMERGED-BLOCKER WARNING' \
       && echo "$OUT" | grep -q "$BLOCKER_BRANCH" \
       && echo "$OUT" | grep -q "$BLOCKER_ID"; then
      pass "sable-dep-check WARNS (exit 3) naming the blocker and its unmerged branch"
    else
      fail "sable-dep-check WARNS (exit 3) naming the blocker and its unmerged branch" \
           "rc=$RC output: ${OUT:-<empty>}"
    fi

    # --- 4: --ready finds it without being told which bead to look at ------
    OUT_READY=$("$DEP_CHECK" --repo "$WORK" --bd-dir "$REPO" --integration-branch "$INT_BRANCH" --ready 2>&1)
    if echo "$OUT_READY" | grep -q "$DEPENDENT_ID"; then
      pass "sable-dep-check --ready surfaces the falsely-released bead from the ready pool"
    else
      fail "sable-dep-check --ready surfaces the falsely-released bead from the ready pool" \
           "output: ${OUT_READY:-<empty>}"
    fi

    # --- 4b: THE WIRING. The warning must reach the REAL DISPATCH PATH. ----
    #
    # This is the assertion the first cut of this bead did not have, and its
    # absence is why 44 green assertions coexisted with a guard that could not
    # fire in production. The surfacing was wired into
    # hooks/multi-manager/pre-dispatch-claim.sh, which the live settings register
    # as PreToolUse matcher `Agent` — but managers dispatch workers by calling
    # bin/sable-spawn-worker, a BASH invocation that never runs that hook. Every
    # test exercised the checker or the hook in ISOLATION, so all of them passed
    # over the one invariant that mattered: that the guard RUNS WHERE IT IS
    # NEEDED. (SABLE-z7x1o complement-coverage shape; mechanized as SABLE-tqhr3.)
    #
    # So: run the REAL program, through its REAL governance chain, against the
    # REAL bd DB and the REAL unmerged fixture branch, and assert the warning
    # comes out. Nothing here stubs the dispatch path.
    #
    # ABORTING SAFELY: the run is steered into the duplicate-dispatch refusal
    # (bead in_progress + its derived worktree present => exit 5), which sits
    # AFTER the advisory and BEFORE `bd update --claim`. That is deliberate on
    # two counts: it proves the advisory precedes the first bd WRITE, and it
    # means the suite never performs an unsandboxed bd write (which would push
    # to the shared Dolt remote) and never creates a tmux pane or a worktree.
    # bd resolves its workspace from CWD and sable-spawn-worker invokes a bare
    # `bd` — so the fixture repo must resolve the SAME DB the beads above live
    # in. BEADS_DIR is bd's own supported override; ask bd where that is rather
    # than assuming "$REPO/.beads" (in a worktree it redirects to the primary
    # checkout's DB, so the literal path is the wrong one).
    BEADS_WORKSPACE=$(bd -C "$REPO" where 2>/dev/null | head -1 | tr -d '[:space:]')

    spawn_governance_run() {
      # $1.. = extra env assignments; runs the real program from the fixture
      # repo, with the worker-pane guard and host-load guard neutralized (this
      # suite is itself frequently run from inside a worker pane) and tmux
      # pointed at a socket with no server, so capacity fails open at 0 workers.
      ( cd "$WORK" && env -u SABLE_WORKER_PANE \
          SABLE_MAX_LOAD_PER_CORE=0 \
          SABLE_TMUX_SOCKET="$FIX/no-such-tmux-socket" \
          BEADS_DIR="$BEADS_WORKSPACE" \
          "$@" \
          python3 "$REPO/bin/sable-spawn-worker" "$DEPENDENT_ID" \
            --session "sable-int-d5iku-$$" 2>&1 )
    }

    # The derived worktree path is computed by the program's OWN rule rather
    # than a re-implementation of it here, so a future rename of that rule
    # breaks the test loudly instead of silently disarming the abort.
    DERIVED_WT=$(python3 - "$REPO/bin/sable-spawn-worker" "$WORK" "$DEPENDENT_ID" <<'PY'
import importlib.util, sys
from importlib.machinery import SourceFileLoader
loader = SourceFileLoader("ssw", sys.argv[1])
spec = importlib.util.spec_from_loader("ssw", loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)
print(m.resolve_worktree_path(sys.argv[2], m.worktree_name(sys.argv[3], None)))
PY
)
    mkdir -p "$DERIVED_WT"
    bd -C "$REPO" update "$DEPENDENT_ID" --sandbox --status in_progress >/dev/null 2>&1

    SPAWN_OUT=$(spawn_governance_run)
    SPAWN_RC=$?
    if echo "$SPAWN_OUT" | grep -q 'UNMERGED-BLOCKER WARNING' \
       && echo "$SPAWN_OUT" | grep -q "$BLOCKER_BRANCH"; then
      pass "WIRING: a REAL sable-spawn-worker governance run emits the unmerged-blocker warning (the dispatch path managers actually use)"
    else
      fail "WIRING: a REAL sable-spawn-worker governance run emits the unmerged-blocker warning (the dispatch path managers actually use)" \
           "rc=$SPAWN_RC output: ${SPAWN_OUT:-<empty>}"
    fi

    # The advisory must precede the first bd WRITE, not merely exist. Exit 5 is
    # the duplicate-dispatch refusal — reaching it with the warning already
    # printed is the proof of ordering.
    if [ "$SPAWN_RC" -eq 5 ]; then
      pass "WIRING: the warning is emitted BEFORE the governance chain's first bd write (refused at duplicate-dispatch, exit 5)"
    else
      fail "WIRING: the warning is emitted BEFORE the governance chain's first bd write (refused at duplicate-dispatch, exit 5)" \
           "expected rc=5, got rc=$SPAWN_RC — if the chain changed, this test's safe-abort no longer holds and the suite may be writing to bd. output: ${SPAWN_OUT:-<empty>}"
    fi

    # Complement: the kill switch must actually kill it. An advisory nobody can
    # turn off becomes noise that gets routed around.
    #
    # NOTE both negative wiring assertions (this one and test 7) also require
    # rc=5. Without it a CRASH would pass as "silence" — which is the exact
    # complement-coverage failure this whole section exists to close, and it
    # really did pass vacuously here once before the rc was asserted.
    SPAWN_OFF=$(spawn_governance_run SABLE_DEP_MERGE_GUARD=0)
    SPAWN_OFF_RC=$?
    if [ "$SPAWN_OFF_RC" -eq 5 ] && ! echo "$SPAWN_OFF" | grep -q 'UNMERGED-BLOCKER WARNING'; then
      pass "WIRING: SABLE_DEP_MERGE_GUARD=0 silences the advisory on the real dispatch path"
    else
      fail "WIRING: SABLE_DEP_MERGE_GUARD=0 silences the advisory on the real dispatch path" \
           "rc=$SPAWN_OFF_RC (want 5 — a crash must not read as silence) output: $SPAWN_OFF"
    fi

    # --- 5: REAL merge, then the warning must STOP -------------------------
    # Nothing about the bead graph changes here — only the tree. If the check
    # still warned, it would be reading status, not merge state.
    git -C "$WORK" merge -q --no-edit "origin/$BLOCKER_BRANCH"
    git -C "$WORK" push -q origin "$INT_BRANCH"
    git -C "$WORK" fetch -q origin

    OUT_AFTER=$("$DEP_CHECK" --repo "$WORK" --bd-dir "$REPO" --integration-branch "$INT_BRANCH" "$DEPENDENT_ID" 2>&1)
    RC_AFTER=$?
    if [ "$RC_AFTER" -eq 0 ] && ! echo "$OUT_AFTER" | grep -q 'UNMERGED-BLOCKER WARNING'; then
      pass "after the REAL merge the same bead graph is clean (exit 0, no warning)"
    else
      fail "after the REAL merge the same bead graph is clean (exit 0, no warning)" \
           "rc=$RC_AFTER output: ${OUT_AFTER:-<empty>}"
    fi

    # --- 6: branch deleted post-merge stays clean --------------------------
    # This fleet prunes worker branches after they land. An absent ref must not
    # resurrect the warning, or every merged blocker would warn forever.
    git -C "$WORK" push -q origin --delete "$BLOCKER_BRANCH"
    git -C "$WORK" fetch -q --prune origin
    OUT_PRUNED=$("$DEP_CHECK" --repo "$WORK" --bd-dir "$REPO" --integration-branch "$INT_BRANCH" "$DEPENDENT_ID" 2>&1)
    RC_PRUNED=$?
    if [ "$RC_PRUNED" -eq 0 ] && ! echo "$OUT_PRUNED" | grep -q 'UNMERGED-BLOCKER WARNING'; then
      pass "blocker branch pruned after merge → still clean (no false-block)"
    else
      fail "blocker branch pruned after merge → still clean (no false-block)" \
           "rc=$RC_PRUNED output: ${OUT_PRUNED:-<empty>}"
    fi

    # --- 7: the WIRING, other direction ------------------------------------
    # Same program, same bead graph, same governance chain — only the tree has
    # changed. A dispatch path that warns unconditionally is a false-block
    # engine, and would be indistinguishable from a correct guard in test 4b.
    SPAWN_AFTER=$(spawn_governance_run)
    SPAWN_AFTER_RC=$?
    if [ "$SPAWN_AFTER_RC" -eq 5 ] && ! echo "$SPAWN_AFTER" | grep -q 'UNMERGED-BLOCKER WARNING'; then
      pass "WIRING: after the REAL merge the same real dispatch run is SILENT"
    else
      fail "WIRING: after the REAL merge the same real dispatch run is SILENT" \
           "rc=$SPAWN_AFTER_RC (want 5 — a crash must not read as silence) output: $SPAWN_AFTER"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Git-only half — runs even without bd, so the clean-room still exercises the
# ancestry engine (the part that genuinely cannot be stubbed) for real.
# ---------------------------------------------------------------------------
GITONLY="$FIX/gitonly"
git init -q "$GITONLY"
git -C "$GITONLY" config user.email "test@example.invalid"
git -C "$GITONLY" config user.name "SABLE Test"
git -C "$GITONLY" config sable.integrationBranch "$INT_BRANCH"
git -C "$GITONLY" checkout -q -b "$INT_BRANCH"
echo a > "$GITONLY/a.txt"
git -C "$GITONLY" add a.txt
git -C "$GITONLY" commit -qm base
GO_BASE=$(git -C "$GITONLY" rev-parse HEAD)
echo b > "$GITONLY/b.txt"
git -C "$GITONLY" add b.txt
git -C "$GITONLY" commit -qm work
GO_TIP=$(git -C "$GITONLY" rev-parse HEAD)
git -C "$GITONLY" update-ref refs/remotes/origin/wk-go "$GO_TIP"

git -C "$GITONLY" update-ref "refs/remotes/origin/$INT_BRANCH" "$GO_BASE"
if [ "$("$DEP_CHECK" --repo "$GITONLY" --integration-branch "$INT_BRANCH" \
        --format=json X 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["integration_branch"])')" = "$INT_BRANCH" ]; then
  pass "git-only: the configured integration branch is what ancestry is judged against"
else
  fail "git-only: the configured integration branch is what ancestry is judged against"
fi

# resolve_base_ref prefers the PUBLISHED integration ref; with only a local
# branch it must still resolve rather than degrade to "unresolvable".
git -C "$GITONLY" update-ref -d "refs/remotes/origin/$INT_BRANCH"
BASE_LOCAL=$(python3 - "$DEP_CHECK" "$GITONLY" "$INT_BRANCH" <<'PY'
import importlib.util, sys
from importlib.machinery import SourceFileLoader
loader = SourceFileLoader("sdc", sys.argv[1])
spec = importlib.util.spec_from_loader("sdc", loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)
print(m.resolve_base_ref(sys.argv[2], "origin", sys.argv[3]))
PY
)
# --- bd ABSENT: could-not-assess must be VALID and LOUD, never empty --------
# This is the ci-verify 29867402197 failure as a test. bd is deliberately absent
# from the clean-room; the tool used to raise out of subprocess.run there, emit
# EMPTY STDOUT, and take every consumer of --format json down with it — the
# assertion just above (json.load of the tool's output) is exactly the consumer
# that died. Worse than the crash: emitting nothing makes "bd unavailable, could
# not assess" indistinguishable from "assessed, nothing wrong".
#
# Reproduced by stripping bd from PATH rather than by mocking, so this runs the
# SAME code path locally (where bd exists) as in CI (where it does not).
#
# The stripped PATH is built by SYMLINKING the interpreters the tool genuinely
# needs (its own `#!/usr/bin/env python3`, plus git) into an otherwise empty
# dir, rather than by blanking PATH outright — a blank PATH cannot resolve
# python3 at all, so the script would never start and the resulting empty
# output would look like the very defect under test while proving nothing.
NOBD="$FIX/emptybin"
mkdir -p "$NOBD"
for _tool in python3 git; do
  _resolved=$(command -v "$_tool" 2>/dev/null)
  [ -n "$_resolved" ] && ln -sf "$_resolved" "$NOBD/$_tool"
done
if [ -n "$(PATH="$NOBD" command -v bd 2>/dev/null)" ]; then
  fail "bd-absent fixture: PATH really has no bd" "bd still resolvable — the assertions below would be vacuous"
fi

NOBD_JSON=$(PATH="$NOBD" "$DEP_CHECK" --repo "$GITONLY" --bd-dir "$GITONLY" \
              --integration-branch "$INT_BRANCH" --format=json SABLE-x 2>/dev/null)
NOBD_RC=$?
ASSESSED=$(printf '%s' "$NOBD_JSON" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as exc:
    print(f'INVALID-JSON: {exc}')
else:
    print(f\"assessed={d.get('assessed')} unknown={len(d.get('unknown') or [])}\")
" 2>&1)
if [ "$ASSESSED" = "assessed=False unknown=1" ] && [ "$NOBD_RC" -eq 4 ]; then
  pass "bd ABSENT: --format=json still emits VALID JSON carrying an explicit could-not-assess state (exit 4)"
else
  fail "bd ABSENT: --format=json still emits VALID JSON carrying an explicit could-not-assess state (exit 4)" \
       "rc=$NOBD_RC parsed='$ASSESSED' raw='${NOBD_JSON:-<EMPTY STDOUT — the ci-verify 29867402197 defect>}'"
fi

for fmt in text hook; do
  NOBD_OUT=$(PATH="$NOBD" "$DEP_CHECK" --repo "$GITONLY" --bd-dir "$GITONLY" \
               --integration-branch "$INT_BRANCH" --format="$fmt" SABLE-x 2>/dev/null)
  if echo "$NOBD_OUT" | grep -q 'COULD NOT ASSESS'; then
    pass "bd ABSENT: --format=$fmt says COULD NOT ASSESS out loud (never silence, which reads as clean)"
  else
    fail "bd ABSENT: --format=$fmt says COULD NOT ASSESS out loud (never silence, which reads as clean)" \
         "output: ${NOBD_OUT:-<empty — indistinguishable from a clean result>}"
  fi
done

# Complement: with bd PRESENT the healthy path is byte-for-byte silent. Without
# this, the fix above could have been "print the block unconditionally", which
# would nag every dispatch and get the guard routed around.
if command -v bd >/dev/null 2>&1; then
  HEALTHY=$("$DEP_CHECK" --repo "$GITONLY" --bd-dir "$REPO" \
              --integration-branch "$INT_BRANCH" --format=hook SABLE-d5iku 2>/dev/null)
  HEALTHY_RC=$?
  if [ -z "$HEALTHY" ] && [ "$HEALTHY_RC" -eq 0 ]; then
    pass "bd PRESENT: the healthy path stays silent (exit 0, empty hook output) — the unknown state did not become noise"
  else
    fail "bd PRESENT: the healthy path stays silent (exit 0, empty hook output) — the unknown state did not become noise" \
         "rc=$HEALTHY_RC output: $HEALTHY"
  fi
else
  skip "bd PRESENT complement: bd not on PATH (clean-room) — the bd-absent direction above still ran for real"
fi

if [ "$BASE_LOCAL" = "refs/heads/$INT_BRANCH" ]; then
  pass "git-only: an UNPUBLISHED integration branch falls back to the local ref (real repo, real refs)"
else
  fail "git-only: an UNPUBLISHED integration branch falls back to the local ref (real repo, real refs)" \
       "got '$BASE_LOCAL'"
fi

# ---------------------------------------------------------------------------
# LIVE-MATCHER REACHABILITY (SABLE-d5iku; seed of SABLE-tqhr3)
# ---------------------------------------------------------------------------
# Reads the LIVE settings registration, not a fixture. The first cut of this
# bead put the guard exclusively in hooks/multi-manager/pre-dispatch-claim.sh,
# which the live settings register as PreToolUse matcher `Agent`. Managers
# dispatch workers by calling bin/sable-spawn-worker — a BASH invocation — so
# the guard could not fire on the only path it exists to guard, while 44 tests
# passed. Repo-side tests cannot see that: the registration lives in the
# runtime, so the check has to read the runtime.
#
# THE CONTRACT: if the live matchers registering the hook leg do NOT cover the
# manager dispatch path, then the guard MUST be invoked directly from
# bin/sable-spawn-worker. Exactly one of those two has to hold, and a repo that
# satisfies neither is shipping a decorative guard.
#
# SKIPs loudly (never a silent pass) where no live settings file exists — CI
# and the clean-room have none, and the real dispatch run in test 4b above
# already covers the mechanism there.
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
if [ ! -f "$SETTINGS" ]; then
  skip "live-matcher reachability: no settings at $SETTINGS (CI/clean-room) — test 4b's real dispatch run covers the mechanism"
else
  HOOK_COVERS_DISPATCH=$(python3 - "$SETTINGS" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        settings = json.load(fh)
except Exception:
    print("unknown")
    sys.exit(0)

# Every matcher under which the dispatch-claim hook leg is registered LIVE.
matchers = []
for groups in (settings.get("hooks") or {}).values():
    for group in groups or []:
        for hook in group.get("hooks") or []:
            if "pre-dispatch-claim.sh" in (hook.get("command") or ""):
                matchers.append(group.get("matcher") or "")

if not matchers:
    print("absent")          # hook leg not registered at all
elif any("Bash" in m or m in ("*", "") for m in matchers):
    print("yes")             # a Bash-covering matcher reaches sable-spawn-worker
else:
    print("no")              # Agent-only: the manager dispatch path is NOT covered
PY
)
  # The direct leg: a real call in the governance chain, not the comment that
  # was the only mention of the hook in this file before the fix.
  if grep -qE '^\s*dep_warning = dep_merge_advisory\(' "$REPO/bin/sable-spawn-worker"; then
    DIRECT_LEG=yes
  else
    DIRECT_LEG=no
  fi

  echo "Live reachability: settings=$SETTINGS hook-covers-dispatch=$HOOK_COVERS_DISPATCH direct-leg-in-sable-spawn-worker=$DIRECT_LEG"

  if [ "$DIRECT_LEG" = "yes" ] || [ "$HOOK_COVERS_DISPATCH" = "yes" ]; then
    pass "live-matcher reachability: the unmerged-blocker guard is reachable from the manager dispatch path (sable-spawn-worker)"
  else
    fail "live-matcher reachability: the unmerged-blocker guard is reachable from the manager dispatch path (sable-spawn-worker)" \
         "the LIVE settings register the hook leg under matcher(s) that do not cover a Bash invocation (hook-covers-dispatch=$HOOK_COVERS_DISPATCH), and bin/sable-spawn-worker does not call the checker itself — so the guard cannot fire on the path managers actually use, no matter how green the rest of this suite is"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL | Skipped: $SKIP"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
  printf "Failed tests:%b\n" "$FAIL_NAMES"
  exit 1
fi
exit 0
