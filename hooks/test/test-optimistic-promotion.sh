#!/usr/bin/env bash
# test-optimistic-promotion.sh — optimistic disjoint promotion (SABLE-jd5fj.4)
#
# WHAT IS UNDER TEST
# ------------------
# The one relaxation in the merge-pipeline epic: when the integration branch
# moves out from under a CI-green preview, a DISJOINT base-move now earns an
# impact-scoped re-test on the REAL COMBINED TREE instead of a full re-preview.
# Everything here runs against real git (a bare origin + a working clone, real
# worktree checkouts, real suite executions); only the Actions verdict is
# injected, through the SABLE_MG_GH seam that the rest of the gate's suites
# already use.
#
# THE FIXTURE IS THE ARGUMENT. The sandbox repo carries a real, tiny impact tier
# — .github/ci/impact-manifest.sh selecting suites for changed paths, and
# hooks/test suites that genuinely pass or fail — because the property that
# matters is not "the gate called something", it is "the gate learned something
# TRUE about the merged state". So the RED case is a real semantic break of the
# exact class SABLE-djopw ruled disjointness unsound for: two file-disjoint,
# textually-clean changes that are each green alone and broken together (both
# register the same route; the tree-wide uniqueness check only fails once both
# are present). No diff of either side can show it. Only the combined tree can.
#
#   C1 disjoint + stale base + green combined tree  -> the tier RUNS on the real
#      merge, and the object that lands is the COMBINED commit (not the stale
#      CI-green preview), byte-identical to what the tier verified.
#   C2 disjoint + stale base + RED combined tree    -> exit 20, nothing promoted.
#      This is C1's non-vacuity: the tier is a real observation, not a formality.
#   C3 OVERLAPPING footprints, textually clean      -> exit 23, full re-preview.
#      git says the merge is clean; the gate still refuses. That refusal is the
#      whole safety argument.
#   C4 modify/delete pair                           -> exit 23. A deletion is a
#      change to that path; a footprint that drops D-status paths would call
#      this disjoint and promote it.
#   C5 lockfile sentinel                            -> exit 23, with no path
#      intersection anywhere to justify it.
#   C6 SABLE_MG_OPTIMISTIC=0                        -> exit 23 on C1's own
#      scenario: the kill switch restores the pre-jd5fj.4 behaviour exactly.
#
# Do NOT read this suite as evidence that the relaxation is low-risk. It shows
# the gate refuses the shapes we enumerated. SABLE-nueh3's 0/126 base rate was
# measured under the regime this bead removes, so the usable prior for what we
# did not enumerate is the rule-of-three bound (<=2.4%) — which is why the tier
# is mandatory on every optimistic path and an unanswerable tier falls back.
#
# ACTIVATION NOTE (SABLE-jd5fj.4): bin/ is a PINNED SNAPSHOT DIRECTORY, so
# merging this changes nothing at runtime until an operator-brokered pin
# refresh. Everything asserted here runs against the WORKING TREE — it is
# evidence about the code, and deliberately not a claim about the live fleet.
#
# Run with:
#   bash hooks/test/test-optimistic-promotion.sh
#
# Clean-room safe (SABLE-59zu): needs only bash + git + python3. bd / sable-msg /
# gh are stubbed; nothing here touches a real remote.

set -uo pipefail

# Resolve absolute paths BEFORE the sandbox preamble cds away (SABLE-0ssz.2).
TESTDIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$TESTDIR/../.." && pwd)"
GATE="$REPO_ROOT/bin/sable-merge-gate"

# shellcheck source=lib-git-sandbox.sh
source "$TESTDIR/lib-git-sandbox.sh"

PASS=0
FAIL=0
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -f "$GATE" ] || { echo "FATAL: missing $GATE"; exit 2; }
[ -f "$REPO_ROOT/bin/sable_footprint_lib.py" ] || { echo "FATAL: missing bin/sable_footprint_lib.py"; exit 2; }

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"; sable_test_git_sandbox_cleanup' EXIT

BASE_BR="trunk"

# A fake `gh` reporting a completed run whose headSha is the real tip of the
# ci-verify ref. FAKE_GH_ADVANCE moves origin's base at call time — the genuine
# base-moved-during-CI race every case below depends on.
FAKE_GH="$TMPROOT/fake-gh"
cat > "$FAKE_GH" <<'EOF'
#!/usr/bin/env python3
import json, os, subprocess, sys
od = os.environ["FAKE_GH_ORIGIN"]
advance = os.environ.get("FAKE_GH_ADVANCE", "")
if advance:
    subprocess.run(["git", "--git-dir=" + od, "update-ref",
                    "refs/heads/" + os.environ["FAKE_GH_BASE"], advance], check=False)
a = sys.argv[1:]
ref = a[a.index("--branch") + 1]
sha = subprocess.run(["git", "--git-dir=" + od, "rev-parse", "refs/heads/" + ref],
                     text=True, capture_output=True).stdout.strip()
print(json.dumps([{"databaseId": 1, "headSha": sha, "status": "completed",
                   "conclusion": os.environ.get("FAKE_GH_MODE", "success"),
                   "url": "http://fake/run/1"}]))
EOF
chmod +x "$FAKE_GH"

# --------------------------------------------------------------------------
# The sandbox repo: a real (tiny) impact tier over a real (tiny) codebase.
# --------------------------------------------------------------------------
seed_repo() {
  local w="$1"
  mkdir -p "$w/.github/ci" "$w/hooks/test" "$w/left" "$w/right"

  cat > "$w/.github/ci/impact-manifest.sh" <<'EOF'
#!/usr/bin/env bash
# Minimal real impact manifest: changed path -> selected suite(s). An unmapped
# path selects everything, mirroring the conservative default of the real
# .github/ci/impact-manifest.sh (SABLE-cmar4.2).
set -uo pipefail
[ "${1:-}" = "--select" ] || { echo "usage: $0 --select <path>..." >&2; exit 2; }
shift
declare -A sel=()
for p in "$@"; do
  case "$p" in
    left/*)  sel[test-left.sh]=1 ;;
    right/*) sel[test-right.sh]=1 ;;
    *)       sel[test-left.sh]=1; sel[test-right.sh]=1 ;;
  esac
done
printf '%s\n' "${!sel[@]}"
EOF
  chmod +x "$w/.github/ci/impact-manifest.sh"

  # The tree-wide consistency check. Each side registering a route is fine;
  # two registrations of the SAME route is the break — and it exists only in
  # the combined state.
  cat > "$w/hooks/test/test-left.sh" <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2
n="$(grep -rho 'ROUTE=/health' left right 2>/dev/null | grep -c .)"
if [ "$n" -gt 1 ]; then
  echo "FAIL: /health is registered $n times — duplicate route registration"
  exit 1
fi
echo "PASS: route registrations are unique ($n)"
EOF
  cat > "$w/hooks/test/test-right.sh" <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
echo "PASS: right side self-check"
EOF
  chmod +x "$w/hooks/test/test-left.sh" "$w/hooks/test/test-right.sh"

  printf 'api_v=1\napi_a=1\napi_b=1\napi_c=1\napi_d=1\n' > "$w/left/api.sh"
  printf 'caller_v=1\n' > "$w/right/caller.sh"
}

# setup_pair <name> — fresh origin+clone with the seeded tree on trunk.
setup_pair() {
  local name="$1"
  B_ORIGIN="$TMPROOT/$name-origin.git"
  B_WORK="$TMPROOT/$name-work"
  git init -q --bare -b "$BASE_BR" "$B_ORIGIN"
  git clone -q "$B_ORIGIN" "$B_WORK" 2>/dev/null
  git -C "$B_WORK" config user.email "t@sable.invalid"
  git -C "$B_WORK" config user.name "SABLE Test"
  seed_repo "$B_WORK"
  git -C "$B_WORK" add -A
  git -C "$B_WORK" commit -q -m init
  git -C "$B_WORK" push -q origin "$BASE_BR"
}

# commit_on <branch> <from> <message> — helper for building the two sides.
start_branch() { git -C "$B_WORK" checkout -q -b "$1" "${2:-$BASE_BR}"; }
commit_push() {
  git -C "$B_WORK" add -A
  git -C "$B_WORK" commit -q -m "$1"
  git -C "$B_WORK" push -q origin "$(git -C "$B_WORK" rev-parse --abbrev-ref HEAD)"
}

gate() {
  env FAKE_GH_ORIGIN="$B_ORIGIN" FAKE_GH_BASE="$BASE_BR" \
      FAKE_GH_MODE="${FAKE_GH_MODE:-success}" FAKE_GH_ADVANCE="${FAKE_GH_ADVANCE:-}" \
      SABLE_MG_GH="$FAKE_GH" SABLE_MG_BD=true SABLE_MG_NOTIFY=true \
      SABLE_MG_POLL=0 SABLE_MG_GRACE=0 SABLE_MG_TIMEOUT=0 \
      SABLE_MG_OPTIMISTIC="${SABLE_MG_OPTIMISTIC:-1}" \
      PATH="$PATH" python3 "$GATE" "$@" 2>&1
}

ci_refs() { git --git-dir="$B_ORIGIN" for-each-ref --format='%(refname:short)' refs/heads/ci-verify/; }
origin_sha() { git --git-dir="$B_ORIGIN" rev-parse "refs/heads/$1"; }
parents_of() { git -C "$B_WORK" rev-list --parents -n 1 "$1" | cut -d' ' -f2-; }

# scenario <name> <branch-mutation-fn> <basemove-mutation-fn>
# Builds: a worker branch off trunk, a kicked preview for it, and a pushed
# base-move commit that the fake gh will make trunk point at mid-promote.
# Sets: KICKED_REF, KICKED_SHA, WK_SHA, MOVED_SHA, OLD_BASE.
scenario() {
  local name="$1" branch_fn="$2" basemove_fn="$3"
  setup_pair "$name"
  OLD_BASE="$(origin_sha "$BASE_BR")"

  start_branch wk-1
  "$branch_fn"
  commit_push "worker change"
  WK_SHA="$(origin_sha wk-1)"

  gate preview --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin >/dev/null
  KICKED_REF="$(ci_refs | head -1)"
  KICKED_SHA="$(origin_sha "$KICKED_REF")"

  git -C "$B_WORK" checkout -q "$BASE_BR"
  start_branch basemove
  "$basemove_fn"
  commit_push "base moved"
  MOVED_SHA="$(origin_sha basemove)"
}

# --- the mutations each case composes -------------------------------------
mut_branch_disjoint_ok()   { echo 'feature=1' > "$B_WORK/left/feature.sh"; }
mut_base_disjoint_ok()     { echo 'other=1'   > "$B_WORK/right/other.sh"; }
mut_branch_route()         { echo 'ROUTE=/health' > "$B_WORK/left/routes.sh"; }
mut_base_route()           { echo 'ROUTE=/health' > "$B_WORK/right/routes.sh"; }
mut_branch_api_first()     { sed -i '1s/.*/api_v=2/' "$B_WORK/left/api.sh"; }
mut_base_api_last()        { sed -i '5s/.*/api_d=2/' "$B_WORK/left/api.sh"; }
mut_branch_modify_caller() { echo 'caller_v=2' > "$B_WORK/right/caller.sh"; }
mut_base_delete_caller()   { git -C "$B_WORK" rm -q "right/caller.sh"; }
mut_base_lockfile()        { echo 'resolved=1' > "$B_WORK/package-lock.json"; }

# ==========================================================================
# C1 — disjoint + stale base + green combined tree
# ==========================================================================
scenario c1 mut_branch_disjoint_ok mut_base_disjoint_ok
OUT="$(FAKE_GH_ADVANCE="$MOVED_SHA" gate promote --bead TEST-C1 --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
LANDED="$(origin_sha "$BASE_BR")"

if [ "$RC" -eq 0 ]; then
  pass "C1: a disjoint stale base promotes (exit 0)"
else
  fail "C1: a disjoint stale base promotes" "rc=$RC out=$OUT"
fi
if printf '%s' "$OUT" | grep -q 'impact tier on combined tree'; then
  pass "C1: the impact tier ran on the real combined tree"
else
  fail "C1: the impact tier ran on the combined tree" "out=$OUT"
fi
if printf '%s' "$OUT" | grep -q 'test-left.sh'; then
  pass "C1: the tier was IMPACT-SCOPED (named the selected suite), not a full run"
else
  fail "C1: the tier was impact-scoped" "out=$OUT"
fi
if [ "$LANDED" != "$KICKED_SHA" ] && [ "$LANDED" != "$OLD_BASE" ]; then
  pass "C1: the stale CI-green preview is NOT what landed"
else
  fail "C1: the stale preview must not land" "landed=$LANDED kicked=$KICKED_SHA"
fi
if [ "$(parents_of "$LANDED")" = "$MOVED_SHA $WK_SHA" ]; then
  pass "C1: what landed IS the combined object (parents = moved base + worker branch)"
else
  fail "C1: what landed is the combined object" "parents=$(parents_of "$LANDED") want=$MOVED_SHA $WK_SHA"
fi
if git -C "$B_WORK" cat-file -e "$LANDED^{commit}" 2>/dev/null \
   && [ -n "$(git -C "$B_WORK" ls-tree "$LANDED" left/feature.sh)" ] \
   && [ -n "$(git -C "$B_WORK" ls-tree "$LANDED" right/other.sh)" ]; then
  pass "C1: the promoted tree contains BOTH changes (the merge really happened)"
else
  fail "C1: the promoted tree contains both changes" "landed=$LANDED"
fi
if [ -z "$(ci_refs)" ]; then
  pass "C1: the ci-verify ref is cleaned up on the optimistic path too"
else
  fail "C1: ci-verify ref cleaned up" "refs=[$(ci_refs)]"
fi

# ==========================================================================
# C2 — disjoint + stale base + RED combined tree (C1's non-vacuity)
# ==========================================================================
scenario c2 mut_branch_route mut_base_route
OUT="$(FAKE_GH_ADVANCE="$MOVED_SHA" gate promote --bead TEST-C2 --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 20 ]; then
  pass "C2: two file-disjoint, textually-clean changes that break together eject on exit 20"
else
  fail "C2: an impact-RED combined tree ejects on exit 20" "rc=$RC out=$OUT"
fi
if [ "$(origin_sha "$BASE_BR")" = "$MOVED_SHA" ]; then
  pass "C2: nothing was promoted — the base is still just the base-move"
else
  fail "C2: nothing was promoted" "base=$(origin_sha "$BASE_BR") moved=$MOVED_SHA"
fi
if printf '%s' "$OUT" | grep -q 'duplicate route registration'; then
  pass "C2: the RED verdict came from the real suite output, not from a stub"
else
  fail "C2: the RED verdict is real" "out=$OUT"
fi

# ==========================================================================
# C3 — OVERLAPPING footprints, textually clean -> full re-preview
# ==========================================================================
scenario c3 mut_branch_api_first mut_base_api_last
OUT="$(FAKE_GH_ADVANCE="$MOVED_SHA" gate promote --bead TEST-C3 --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 23 ]; then
  pass "C3: overlapping footprints force a full re-preview (exit 23) even though git merges cleanly"
else
  fail "C3: overlapping footprints force a full re-preview" "rc=$RC out=$OUT"
fi
if [ "$(origin_sha "$BASE_BR")" = "$MOVED_SHA" ]; then
  pass "C3: nothing was promoted onto the moved base"
else
  fail "C3: nothing was promoted onto the moved base" "base=$(origin_sha "$BASE_BR")"
fi
if printf '%s' "$OUT" | grep -q 'left/api.sh'; then
  pass "C3: the refusal names the intersecting path (auditable evidence, not a bare code)"
else
  fail "C3: the refusal names the intersecting path" "out=$OUT"
fi
# Non-vacuity of C3's "git merges cleanly": the same two commits DO merge.
if git -C "$B_WORK" merge-tree --write-tree "$MOVED_SHA" "$WK_SHA" >/dev/null 2>&1; then
  pass "C3: git itself considers the overlapping merge clean — the gate's refusal is the added safety"
else
  fail "C3: the overlapping merge is textually clean" "merge-tree reported a conflict"
fi

# ==========================================================================
# C4 — modify/delete pair is non-disjoint
# ==========================================================================
scenario c4 mut_branch_modify_caller mut_base_delete_caller
OUT="$(FAKE_GH_ADVANCE="$MOVED_SHA" gate promote --bead TEST-C4 --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 23 ]; then
  pass "C4: a modify/delete pair is classed NON-disjoint (exit 23)"
else
  fail "C4: modify/delete is non-disjoint" "rc=$RC out=$OUT"
fi
if printf '%s' "$OUT" | grep -q 'right/caller.sh'; then
  pass "C4: the DELETED path is in the footprint (naming it is the proof)"
else
  fail "C4: the deleted path is in the footprint" "out=$OUT"
fi
if [ "$(origin_sha "$BASE_BR")" = "$MOVED_SHA" ]; then
  pass "C4: nothing was promoted"
else
  fail "C4: nothing was promoted" "base=$(origin_sha "$BASE_BR")"
fi

# ==========================================================================
# C5 — lockfile sentinel, with no path intersection at all
# ==========================================================================
scenario c5 mut_branch_disjoint_ok mut_base_lockfile
OUT="$(FAKE_GH_ADVANCE="$MOVED_SHA" gate promote --bead TEST-C5 --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 23 ]; then
  pass "C5: a lockfile touched by either side forces a full re-preview (exit 23)"
else
  fail "C5: lockfile sentinel forces a re-preview" "rc=$RC out=$OUT"
fi
if printf '%s' "$OUT" | grep -qi 'sentinel'; then
  pass "C5: the refusal says WHY — a sentinel, not a path intersection"
else
  fail "C5: the refusal names the sentinel rule" "out=$OUT"
fi

# ==========================================================================
# C6 — the kill switch restores the pre-jd5fj.4 behaviour on C1's scenario
# ==========================================================================
scenario c6 mut_branch_disjoint_ok mut_base_disjoint_ok
OUT="$(SABLE_MG_OPTIMISTIC=0 FAKE_GH_ADVANCE="$MOVED_SHA" gate promote --bead TEST-C6 --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 23 ] && [ "$(origin_sha "$BASE_BR")" = "$MOVED_SHA" ]; then
  pass "C6: SABLE_MG_OPTIMISTIC=0 turns C1's promotable scenario back into exit 23"
else
  fail "C6: the kill switch restores the old behaviour" "rc=$RC base=$(origin_sha "$BASE_BR") out=$OUT"
fi
if ! printf '%s' "$OUT" | grep -q 'impact tier'; then
  pass "C6: with the switch off, no impact tier runs at all"
else
  fail "C6: no impact tier runs with the switch off" "out=$OUT"
fi

echo "----------------------------------------------------------------------"
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
[ "$FAIL" -eq 0 ]
