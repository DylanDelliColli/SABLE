#!/usr/bin/env bash
# test-close-hold-guard.sh — UNIT + INTEGRATION tests for
# hooks/multi-manager/close-hold-guard.sh (SABLE-hl9fu).
#
# WHAT IS ACTUALLY UNDER TEST. The guard couples two acts that nothing coupled
# before: closing a bead, and declaring what happens to its unlanded branch. The
# interesting claim is NOT "it can deny" — any gate can deny. It is that the gate
# denies in exactly one state (unlanded AND unheld AND unhanded-off) and is
# COMPLETELY SILENT in every other, because a gate on `bd close` that is noisy
# gets bypassed within a day and a bypassed gate is worse than none. So the
# negative controls below are the deliverable, and they are asserted on the EXACT
# emitted decision value ('deny' / 'allow' / '<none>' for no output at all), never
# a substring sniff — a hook that emitted a differently-shaped blob fails here
# rather than passing on an incidental match.
#
# THE UNIT LEG USES A REAL GIT REPO, NOT A STUBBED GIT. The trap this bead is
# about is an ABSENT REF being read as "not contained": a bare `git rev-parse`
# echoes an unresolvable ref back to stdout, and every caller downstream treats
# that garbage string as a real tip (300 false findings in one hand-written
# sweep). A stubbed git can only ever confirm the stub author's own model of
# absence. So the unit leg builds a real throwaway repo (hooks/test/
# lib-git-sandbox.sh anchors it away from the real worktree) with a genuinely
# merged branch, a genuinely unmerged one, a genuinely reaped one, and a
# genuinely archive-tagged one — and runs the REAL bin/sable-contained against
# them. Only the BEAD STORE is stubbed here, because the bead fixtures are what
# the decision table varies.
#
# PLANT-AND-FAIL (SABLE-5lli.7) is at the bottom: a mutant of the hook with the
# coupling — and only the coupling — removed must flip the uncontained-close case
# from DENY to silent, and must leave every negative control byte-identical. Both
# polarities, so the deny assertion is shown load-bearing rather than assumed.
#
# INTEGRATION leg: isolated real bd store + the suite's real sandbox git refs,
# no stubs anywhere, self-skipping (loudly, with a Skipped count) when bd is
# absent — the ci-verify clean room is tmux+pytest only (SABLE-k35mw/59zu).
# DELIBERATE DEVIATION FROM THE BEAD'S SPEC, stated rather than silently taken:
# the spec says "push it to a real origin WITHOUT merging". The property under
# test is containment, which the suite's real merged/unmerged local refs answer
# exactly; publishing fixture branches or writing fixture beads into shared
# project state adds risk and cost without strengthening that predicate.
#
# Run with:
#   bash hooks/test/test-close-hold-guard.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/close-hold-guard.sh"
CONTAINED_BIN="$REPO/bin/sable-contained"

PASS=0
FAIL=0
SKIP=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
skip() { SKIP=$((SKIP+1)); echo "SKIP: $1"; }

if [ ! -f "$HOOK" ]; then
  fail "hook present at hooks/multi-manager/close-hold-guard.sh" "not found at $HOOK"
  echo "Tests: 1 | Passed: 0 | Failed: 1 | Skipped: 0"
  exit 1
fi

PYBIN="$(dirname "$(command -v python3)")"

# Anchor the suite's ambient CWD in a throwaway git repo BEFORE anything else,
# so an unguarded escape lands there rather than in the real worktree.
# shellcheck source=lib-git-sandbox.sh
source "$(dirname "$0")/lib-git-sandbox.sh"

SANDBOX_REPO="$SABLE_TEST_SANDBOX/repo"
FIXTURE_DIR="$(mktemp -d)"
cleanup_fixture() { rm -rf "$FIXTURE_DIR"; sable_test_git_sandbox_cleanup; }
trap 'cleanup_fixture' EXIT

# ===========================================================================
# REAL git topology in the sandbox. Every branch state the guard distinguishes
# exists here for real; none of them is a stub's idea of that state.
#
#   master            the integration branch
#   wk-landed         merged into master        -> CONTAINED
#   wk-unlanded       a commit master lacks     -> NOT CONTAINED
#   wk-handoff        ditto (a handoff names it)
#   wk-handoff-extra  ditto (nothing names it; the prefix-boundary control)
#   wk-retired        ref DELETED + archive/wk-retired tag -> RETIRED
#   wk-gone           never existed                        -> UNKNOWN
# ===========================================================================
sandbox_git() { git -C "$SANDBOX_REPO" "$@"; }

seed_sandbox() {
  cd "$SANDBOX_REPO" || return 1
  sandbox_git config sable.integrationBranch master
  echo seed > seed.txt
  sandbox_git add seed.txt
  sandbox_git commit -q -m "seed"

  sandbox_git checkout -q -b wk-landed
  echo landed > landed.txt
  sandbox_git add landed.txt
  sandbox_git commit -q -m "work that lands"
  sandbox_git checkout -q master
  sandbox_git merge -q --no-ff -m "merge wk-landed" wk-landed
  echo more > more.txt
  sandbox_git add more.txt
  sandbox_git commit -q -m "master moves on"

  local br
  for br in wk-unlanded wk-handoff wk-handoff-extra wk-retired; do
    sandbox_git checkout -q -b "$br" master
    echo "$br" > "$br.txt"
    sandbox_git add "$br.txt"
    sandbox_git commit -q -m "unmerged work on $br"
    sandbox_git checkout -q master
  done

  # Reap wk-retired DELIBERATELY: tag it, then delete the ref. This is the state
  # a bare `git rev-parse` turns into a garbage tip.
  sandbox_git tag "archive/wk-retired" wk-retired
  sandbox_git branch -q -D wk-retired
}

if ! seed_sandbox; then
  fail "sandbox git topology builds" "seed_sandbox failed in $SANDBOX_REPO"
  echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL | Skipped: $SKIP"
  exit 1
fi
pass "fixture precondition: real sandbox git topology built (landed / unlanded / reaped / archived)"

# ===========================================================================
# Stub ONLY the bead store. `bd show <id> --json` prints the same single-element
# list shape real bd emits; `bd list ... --label for-chuck --json` prints the
# handoff corpus. Unknown ids exit non-zero (the unreadable-bead case).
# SABLE_TEST_FORCHUCK_FAIL=1 makes the corpus query itself fail.
# ===========================================================================
STUB_DIR="$FIXTURE_DIR/bin"
mkdir -p "$STUB_DIR"
cat > "$STUB_DIR/bd" <<'STUB'
#!/usr/bin/env bash
case "${1:-}" in
  show)
    case "${2:-}" in
      SABLE-nobranch)  printf '[{"id":"SABLE-nobranch","status":"in_progress","metadata":{"model":"opus"}}]\n' ;;
      SABLE-landed)    printf '[{"id":"SABLE-landed","status":"in_progress","metadata":{"branch":"wk-landed"}}]\n' ;;
      SABLE-landed2)   printf '[{"id":"SABLE-landed2","status":"in_progress","metadata":{"branch":"wk-landed"}}]\n' ;;
      SABLE-unlanded)  printf '[{"id":"SABLE-unlanded","status":"in_progress","metadata":{"branch":"wk-unlanded"}}]\n' ;;
      SABLE-held)      printf '[{"id":"SABLE-held","status":"in_progress","metadata":{"branch":"wk-unlanded","hold":"waiting on the gxsji ruling","hold_by":"tarzan","hold_since":"2026-07-26T02:00:00Z","hold_until":"gxsji decides the installer wiring"}}]\n' ;;
      SABLE-partial)   printf '[{"id":"SABLE-partial","status":"in_progress","metadata":{"branch":"wk-unlanded","hold":"waiting on the gxsji ruling","hold_by":"tarzan"}}]\n' ;;
      SABLE-blankhold) printf '[{"id":"SABLE-blankhold","status":"in_progress","metadata":{"branch":"wk-unlanded","hold":"   ","hold_by":"tarzan","hold_since":"2026-07-26T02:00:00Z","hold_until":"never"}}]\n' ;;
      SABLE-handoff)   printf '[{"id":"SABLE-handoff","status":"in_progress","metadata":{"branch":"wk-handoff"}}]\n' ;;
      SABLE-handoffx)  printf '[{"id":"SABLE-handoffx","status":"in_progress","metadata":{"branch":"wk-handoff-extra"}}]\n' ;;
      SABLE-retired)   printf '[{"id":"SABLE-retired","status":"in_progress","metadata":{"branch":"wk-retired"}}]\n' ;;
      SABLE-reaped)    printf '[{"id":"SABLE-reaped","status":"in_progress","metadata":{"branch":"wk-gone"}}]\n' ;;
      SABLE-badjson)   printf 'this is not json\n' ;;
      *)               exit 1 ;;
    esac
    ;;
  list)
    [ "${SABLE_TEST_FORCHUCK_FAIL:-0}" = "1" ] && exit 1
    printf '[{"id":"SABLE-hoff1","title":"[AUTO-NOTIFY] Review PR from tarzan: wk-handoff"}]\n'
    ;;
  *) exit 1 ;;
esac
STUB
chmod +x "$STUB_DIR/bd"

# The REAL containment tool, reachable by name. `command -v sable-contained` is
# how the hook resolves it in production, so resolving it the same way here means
# the unit leg exercises the real predicate (SABLE-gdp05/4snb4), not a mirror.
ln -sf "$CONTAINED_BIN" "$STUB_DIR/sable-contained"

# A PATH carrying the bead stub but NO sable-contained, for the
# containment-tool-missing control.
NOCONT_DIR="$FIXTURE_DIR/bin-nocontained"
mkdir -p "$NOCONT_DIR"
cp -f "$STUB_DIR/bd" "$NOCONT_DIR/bd"
EMPTY_DIR="$FIXTURE_DIR/empty-bin"
mkdir -p "$EMPTY_DIR"

json() { # <command> -> PreToolUse hook input
  python3 -c "
import json, sys
print(json.dumps({'tool_name': 'Bash', 'tool_input': {'command': sys.argv[1]}, 'hook_event_name': 'PreToolUse'}))
" "$1"
}

# run_hook_with <hook-path> <path-value> <command> [env assignments...]
run_hook_with() {
  local hook="$1" path="$2" cmd="$3"; shift 3
  json "$cmd" | env PATH="$path" "$@" bash "$hook" 2>/dev/null
}

run_hook() { local cmd="$1"; shift; run_hook_with "$HOOK" "$STUB_DIR:$PATH" "$cmd" "$@"; }

# decision_of <hook stdout> -> the exact permissionDecision string, '<none>' for
# no output at all, or '<malformed>' if it is not the documented shape.
decision_of() {
  printf '%s' "$1" | python3 -c "
import json, sys
raw = sys.stdin.read().strip()
if not raw:
    print('<none>'); sys.exit(0)
try:
    d = json.loads(raw)
except Exception:
    print('<malformed>'); sys.exit(0)
hso = d.get('hookSpecificOutput')
if not isinstance(hso, dict) or hso.get('hookEventName') != 'PreToolUse':
    print('<malformed>'); sys.exit(0)
print(hso.get('permissionDecision') or '<malformed>')
"
}

assert_decision() { # <label> <expected> <command> [env assignments...]
  local label="$1" expected="$2" cmd="$3" out got
  shift 3
  out="$(run_hook "$cmd" "$@")"
  got="$(decision_of "$out")"
  if [ "$got" = "$expected" ]; then
    pass "$label"
  else
    fail "$label" "expected decision '$expected', got '$got' (raw: ${out:-<empty>})"
  fi
}

assert_mentions() { # <label> <needle> <command>
  local label="$1" needle="$2" cmd="$3" out
  out="$(run_hook "$cmd")"
  if printf '%s' "$out" | grep -qF -- "$needle"; then
    pass "$label"
  else
    fail "$label" "output did not mention '$needle': ${out:-<empty>}"
  fi
}

assert_absent() { # <label> <needle-that-must-not-appear> <command>
  local label="$1" needle="$2" cmd="$3" out
  out="$(run_hook "$cmd")"
  if printf '%s' "$out" | grep -qF -- "$needle"; then
    fail "$label" "output unexpectedly contained '$needle': ${out:-<empty>}"
  else
    pass "$label"
  fi
}

# ===========================================================================
# THE DEFECT ITSELF
# ===========================================================================
assert_decision "test_close_of_bead_with_uncontained_branch_is_refused_or_held — the defect: DENY" \
  deny 'bd close SABLE-unlanded'

assert_mentions "the refusal NAMES the branch (not just the bead)" \
  "wk-unlanded" 'bd close SABLE-unlanded'

assert_mentions "the refusal names the bead being closed (attributable, not a generic warning)" \
  "SABLE-unlanded" 'bd close SABLE-unlanded'

assert_mentions "the refusal says WHY — the invisible-state bead is cited" \
  "SABLE-zvx4i" 'bd close SABLE-unlanded'

assert_mentions "the refusal hands over the hold route with all four fields" \
  "hold_until=" 'bd close SABLE-unlanded'

assert_mentions "the refusal hands over the handoff route too (a worker is not wedged)" \
  "sable-reconcile-handoffs" 'bd close SABLE-unlanded'

# LOAD-BEARING: the guard must not author release prose. A hold_until nobody
# wrote reads as a considered decision and gets honoured as one.
assert_mentions "the hold template leaves hold_until an UNMISTAKABLE placeholder" \
  "<the condition that releases it>" 'bd close SABLE-unlanded'

# ===========================================================================
# THE NEGATIVE CONTROLS — the actual deliverable. A gate that fires on every
# close is unusable and gets reverted (SABLE-r5pfw erosion).
# ===========================================================================
assert_decision "test_close_of_bead_with_contained_branch_is_silent — a landed branch closes with NO output" \
  '<none>' 'bd close SABLE-landed'

assert_decision "test_close_of_bead_with_no_branch_metadata_is_silent — the common case is untouched" \
  '<none>' 'bd close SABLE-nobranch'

assert_decision "a complete four-field hold closes silently (the manager's route)" \
  '<none>' 'bd close SABLE-held'

assert_decision "an existing for-chuck handoff naming the branch closes silently (the worker's route)" \
  '<none>' 'bd close SABLE-handoff'

assert_decision "a non-bd command is untouched" \
  '<none>' 'git status'

assert_decision "bd show is not a close and is untouched" \
  '<none>' 'bd show SABLE-unlanded --json'

assert_decision "bd update is not a close and is untouched" \
  '<none>' 'bd update SABLE-unlanded --status closed'

assert_decision "multi-id close stays silent when EVERY target is clean" \
  '<none>' 'bd close SABLE-landed SABLE-landed2 SABLE-nobranch'

# ===========================================================================
# THE ABSENCE PROBE — a reaped ref is a THIRD STATE, never "not contained".
# This is the case a bare `git rev-parse` gets confidently wrong, in the
# BLOCKING direction: it would refuse a close nobody can satisfy.
# ===========================================================================
assert_decision "test_absent_branch_ref_is_UNKNOWN_not_not_contained — allow, never deny" \
  allow 'bd close SABLE-reaped'

assert_mentions "the absent ref is reported as UNKNOWN in so many words" \
  "UNKNOWN" 'bd close SABLE-reaped'

assert_mentions "the absent-ref report names the branch it could not resolve" \
  "wk-gone" 'bd close SABLE-reaped'

# ...and UNKNOWN must not be laundered into the merged verdict either. An
# absence probe that decays toward silence is the other half of the same bug.
assert_absent "an absent ref is NOT reported as merged/contained" \
  "is merged into" 'bd close SABLE-reaped'

assert_decision "a DELIBERATELY RETIRED branch (archive tag) is allowed, not denied" \
  allow 'bd close SABLE-retired'

assert_mentions "the retired branch is reported as retired, distinctly from unknown" \
  "DELIBERATELY RETIRED" 'bd close SABLE-retired'

# ===========================================================================
# HOLD COMPLETENESS. A partial hold is refused at creation time — the one moment
# the person who knows the answer is still present.
# ===========================================================================
assert_decision "a PARTIAL hold (2 of 4 fields) is DENIED, not accepted with a flag" \
  deny 'bd close SABLE-partial'

assert_mentions "the partial-hold refusal names the missing fields" \
  "hold_since" 'bd close SABLE-partial'

assert_mentions "the partial-hold refusal names hold_until too" \
  "hold_until" 'bd close SABLE-partial'

assert_decision "a whitespace-only hold reason is not a hold (presence is keyed on content)" \
  deny 'bd close SABLE-blankhold'

# ===========================================================================
# HANDOFF MATCHING — delimited, so a handoff for wk-handoff never covers
# wk-handoff-extra. A prefix match here would silently release the guard on a
# branch nobody has seen.
# ===========================================================================
assert_decision "a handoff naming wk-handoff does NOT cover wk-handoff-extra (prefix boundary)" \
  deny 'bd close SABLE-handoffx'

assert_mentions "the prefix-boundary refusal names the right branch" \
  "wk-handoff-extra" 'bd close SABLE-handoffx'

# ===========================================================================
# FAIL-OPEN, BUT NEVER SILENTLY. Every unreadable input ALLOWS and says so.
# Refusing a close because bd hiccuped is exactly the noise that gets a gate
# bypassed; allowing quietly is the SABLE-2az2x defect.
# ===========================================================================
assert_decision "an unreadable for-chuck corpus ALLOWS (never denies on a failed query)" \
  allow 'bd close SABLE-unlanded' SABLE_TEST_FORCHUCK_FAIL=1
OUT="$(run_hook_with "$HOOK" "$STUB_DIR:$PATH" 'bd close SABLE-unlanded' SABLE_TEST_FORCHUCK_FAIL=1)"
if printf '%s' "$OUT" | grep -qF 'UNKNOWN'; then
  pass "the unreadable for-chuck corpus is reported as UNKNOWN, not folded into 'no handoff'"
else
  fail "the unreadable for-chuck corpus is reported as UNKNOWN, not folded into 'no handoff'" "raw: ${OUT:-<empty>}"
fi

assert_decision "an unreadable bead ALLOWS" \
  allow 'bd close SABLE-missing'
assert_mentions "the unreadable bead is reported COULD NOT ASSESS and named" \
  "SABLE-missing" 'bd close SABLE-missing'

assert_decision "unparseable bd show output ALLOWS" \
  allow 'bd close SABLE-badjson'

assert_decision "an unparseable command line (dangling quote) ALLOWS" \
  allow 'bd close "SABLE-unlanded'
assert_mentions "the unparseable command line is reported COULD NOT ASSESS" \
  "COULD NOT ASSESS" 'bd close "SABLE-unlanded'

assert_decision "a bd close with no parseable target id ALLOWS, loudly" \
  allow 'bd close --sandbox'
assert_mentions "the unparseable target is reported COULD NOT ASSESS" \
  "COULD NOT ASSESS" 'bd close --sandbox'

# sable-contained missing must NEVER be read as 'not contained' — that would
# refuse every close on a machine that had not run sable-bin-install.
OUT="$(run_hook_with "$HOOK" "$NOCONT_DIR:$PYBIN:/usr/bin:/bin" 'bd close SABLE-unlanded')"
GOT="$(decision_of "$OUT")"
if [ "$GOT" = "allow" ] && printf '%s' "$OUT" | grep -qF 'sable-contained'; then
  pass "sable-contained absent from PATH ALLOWS and says so (never a silent deny-everything)"
else
  fail "sable-contained absent from PATH ALLOWS and says so (never a silent deny-everything)" \
       "decision='$GOT' raw='${OUT:-<empty>}'"
fi

# No bd at all: the guarded close cannot write either, so there is nothing to
# protect and nothing to say.
OUT="$(run_hook_with "$HOOK" "$EMPTY_DIR:$PYBIN:/usr/bin:/bin" 'bd close SABLE-unlanded')"
if [ -z "$OUT" ]; then
  pass "no bd on PATH exits silently (the guarded close cannot happen either)"
else
  fail "no bd on PATH exits silently (the guarded close cannot happen either)" "got: $OUT"
fi

# ===========================================================================
# COMMAND SHAPES that must not slip past, and shapes that must not over-deny.
# ===========================================================================
assert_decision "bd close behind a shell separator is caught" \
  deny 'cd /tmp && bd close SABLE-unlanded'

assert_decision "bd close behind an env prefix is caught" \
  deny 'env FOO=1 bd close SABLE-unlanded'

assert_decision "bd close with trailing flags still resolves the target" \
  deny 'bd close SABLE-unlanded --sandbox --reason "done"'

assert_decision "multi-id close DENIES when ANY target is uncontained" \
  deny 'bd close SABLE-landed SABLE-unlanded'

# A bead id sitting in a FLAG VALUE is not a close target — over-reading it
# would deny closes that never named that bead.
assert_decision "a bead id inside a --reason value is not treated as a target" \
  '<none>' 'bd close SABLE-landed --reason "supersedes SABLE-unlanded"'

# ===========================================================================
# Degenerate hook input must never crash or deny (#16047 empty-stdin
# degradation is a live upstream condition in this fleet).
# ===========================================================================
OUT="$(printf '' | env PATH="$STUB_DIR:$PATH" bash "$HOOK" 2>/dev/null)"
if [ -z "$OUT" ]; then pass "empty stdin exits silently"; else fail "empty stdin exits silently" "got: $OUT"; fi

OUT="$(printf 'not json' | env PATH="$STUB_DIR:$PATH" bash "$HOOK" 2>/dev/null)"
if [ -z "$OUT" ]; then pass "non-JSON stdin exits silently"; else fail "non-JSON stdin exits silently" "got: $OUT"; fi

# ===========================================================================
# PLANT-AND-FAIL (SABLE-5lli.7)
#
# Build a MUTANT of the hook with the coupling — and ONLY the coupling —
# removed: a `deny_with_reason() { exit 0; }` override inserted at the hook's
# own PLANT-MARKER. Everything else (parsing, containment, hold reading,
# handoff lookup, every could-not-assess path) is byte-identical.
#
# The mutant MUST flip the uncontained-close case from DENY to silent — that is
# what proves the deny assertion above is load-bearing rather than green for an
# unrelated reason. And it MUST leave the negative controls unchanged, which is
# what proves the plant bites in one direction only (a mutation that changed
# everything would prove nothing about this assertion in particular).
# ===========================================================================
MUTANT="$FIXTURE_DIR/close-hold-guard.MUTANT.sh"
python3 - "$HOOK" "$MUTANT" <<'PLANT'
import sys
src, dst = sys.argv[1], sys.argv[2]
lines = open(src).read().splitlines(keepends=True)
out, planted = [], False
for line in lines:
    out.append(line)
    if not planted and line.startswith("# PLANT-MARKER: deny-override-insertion-point"):
        out.append("deny_with_reason() { exit 0; }\n")
        planted = True
open(dst, "w").write("".join(out))
sys.exit(0 if planted else 1)
PLANT
if [ $? -ne 0 ]; then
  fail "PLANT: the hook still carries its PLANT-MARKER seam" "marker not found in $HOOK — the plant could not be built"
else
  pass "PLANT: the hook still carries its PLANT-MARKER seam"

  MUT_OUT="$(run_hook_with "$MUTANT" "$STUB_DIR:$PATH" 'bd close SABLE-unlanded')"
  MUT_GOT="$(decision_of "$MUT_OUT")"
  if [ "$MUT_GOT" = "<none>" ]; then
    pass "PLANT-AND-FAIL: with the coupling removed, the uncontained close goes SILENT — the deny assertion is load-bearing"
  else
    fail "PLANT-AND-FAIL: with the coupling removed, the uncontained close goes SILENT — the deny assertion is load-bearing" \
         "mutant decision='$MUT_GOT' raw='${MUT_OUT:-<empty>}'; if this is still 'deny' the coupling is not where the test thinks it is"
  fi

  MUT_OUT="$(run_hook_with "$MUTANT" "$STUB_DIR:$PATH" 'bd close SABLE-landed')"
  if [ "$(decision_of "$MUT_OUT")" = "<none>" ]; then
    pass "PLANT control: the landed-branch case is silent under BOTH the real hook and the mutant (the plant bites in one direction only)"
  else
    fail "PLANT control: the landed-branch case is silent under BOTH the real hook and the mutant" "mutant raw='${MUT_OUT:-<empty>}'"
  fi

  MUT_OUT="$(run_hook_with "$MUTANT" "$STUB_DIR:$PATH" 'bd close SABLE-reaped')"
  if [ "$(decision_of "$MUT_OUT")" = "allow" ]; then
    pass "PLANT control: the absent-ref report survives the mutation (it was never a denial to begin with)"
  else
    fail "PLANT control: the absent-ref report survives the mutation" "mutant raw='${MUT_OUT:-<empty>}'"
  fi
fi

# ===========================================================================
# INTEGRATION — real bd store, real git refs, the real integration branch, the
# real containment tool. No stubs. Self-skips (loudly) without bd.
#
# Assertions are by ATTRIBUTABLE IDENTITY (SABLE-jd5fj.15): the exact bead ids
# and branch names this fixture minted, never a global count.
# ===========================================================================
echo
echo "--- INTEGRATION (real bd + real git) ---"

if ! command -v bd >/dev/null 2>&1; then
  skip "INTEGRATION: real bd + real git — bd not on PATH (SABLE-k35mw/59zu clean room is tmux+pytest only)"
else
  INTEG="master"

  INTEG_REF=""
  for CAND in "$INTEG"; do
    if git -C "$SANDBOX_REPO" rev-parse --verify --quiet "$CAND" >/dev/null 2>&1; then
      INTEG_REF="$CAND"; break
    fi
  done

  if [ -z "$INTEG_REF" ]; then
    skip "INTEGRATION: integration branch '$INTEG' does not resolve in the sandbox repo — cannot ask the containment question"
  else
    SUF="hl9fu-$$"
    BR_UNCONT="wk-unlanded"
    BR_CONT="wk-landed"
    INTEG_BD_ROOT="$FIXTURE_DIR/real-bd"
    INTEG_BD_NOHOOKS="$FIXTURE_DIR/real-bd-nohooks"
    mkdir -p "$INTEG_BD_ROOT" "$INTEG_BD_NOHOOKS"
    git -C "$INTEG_BD_ROOT" init -q
    git -C "$INTEG_BD_ROOT" config core.hooksPath "$INTEG_BD_NOHOOKS"
    (cd "$INTEG_BD_ROOT" && env -u BEADS_DB BD_NON_INTERACTIVE=1 \
      bd init --prefix=hl9fu --non-interactive >/dev/null 2>&1) || true
    INTEG_BEADS_DB="$INTEG_BD_ROOT/.beads"

    int_bd() {
      env BEADS_DB="$INTEG_BEADS_DB" BD_NON_INTERACTIVE=1 bd "$@"
    }

    int_cleanup() {
      cleanup_fixture
    }
    trap 'int_cleanup' EXIT

    NEW_SHA="$(git -C "$SANDBOX_REPO" rev-parse --verify --quiet "$BR_UNCONT" 2>/dev/null)"

    if [ ! -d "$INTEG_BEADS_DB" ] || [ -z "$NEW_SHA" ] \
       || ! git -C "$SANDBOX_REPO" rev-parse --verify --quiet "$BR_CONT" >/dev/null 2>&1; then
      skip "INTEGRATION: could not initialize the isolated real bd + git fixture"
    else
      pass "real git: fixture minted a genuinely uncontained branch ($BR_UNCONT) and a genuinely landed one ($BR_CONT at $INTEG_REF)"

      make_bead() { # <suffix> -> bead id
        int_bd create --sandbox \
          --title="[int-test] SABLE-hl9fu close-hold-guard $1 $SUF" \
          --description="Scratch bead minted inside the isolated store owned by hooks/test/test-close-hold-guard.sh, exercising hooks/multi-manager/close-hold-guard.sh against real bd data. [no-test]" \
          --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1
      }

      # run the REAL hook from the REAL repo — bd and git must both resolve.
      run_int() {
        ( cd "$SANDBOX_REPO" || exit 1
          json "$1" | env BEADS_DB="$INTEG_BEADS_DB" bash "$HOOK" 2>/dev/null )
      }

      B_UNCONT="$(make_bead uncontained)"
      B_CONT="$(make_bead contained)"

      if [ -z "$B_UNCONT" ] || [ -z "$B_CONT" ]; then
        skip "INTEGRATION: could not create the real scratch beads"
      else
        echo "Integration: uncontained bead=$B_UNCONT branch=$BR_UNCONT | contained bead=$B_CONT branch=$BR_CONT"
        int_bd update "$B_UNCONT" --sandbox --set-metadata "branch=$BR_UNCONT" >/dev/null 2>&1
        int_bd update "$B_CONT" --sandbox --set-metadata "branch=$BR_CONT" >/dev/null 2>&1

        # --- the two dispositions, in one pass ---------------------------
        OUT_U="$(run_int "bd close $B_UNCONT")"
        DEC_U="$(decision_of "$OUT_U")"
        if [ "$DEC_U" = "deny" ] && printf '%s' "$OUT_U" | grep -qF "$BR_UNCONT"; then
          pass "real bd + real git: closing $B_UNCONT (unlanded $BR_UNCONT, no hold, no handoff) is DENIED and names the branch"
        else
          fail "real bd + real git: closing $B_UNCONT (unlanded $BR_UNCONT, no hold, no handoff) is DENIED and names the branch" \
               "decision='$DEC_U' raw='${OUT_U:-<empty>}'"
        fi

        OUT_C="$(run_int "bd close $B_CONT")"
        DEC_C="$(decision_of "$OUT_C")"
        if [ "$DEC_C" = "<none>" ]; then
          pass "real bd + real git: closing $B_CONT (branch AT the integration tip) is SILENT — no new friction on a landed close"
        else
          fail "real bd + real git: closing $B_CONT (branch AT the integration tip) is SILENT" \
               "decision='$DEC_C' raw='${OUT_C:-<empty>}'"
        fi
      fi
    fi
  fi
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL | Skipped: $SKIP"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
