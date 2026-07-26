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
#   C13 (SABLE-jd5fj.10) a STRUCTURED footprint_writes metadata field (not
#      prose) declares a path outside both mechanical diffs -> exit 23, same
#      shape as C3 but sourced entirely from bd's --json metadata.
#
# SABLE-w0zjm extends this suite past the gate's own boundary, because the defect
# it covers lives OUTSIDE the repo: the operator wrapper the gate runs inside.
# jd5fj.4 deliberately moved cost from GitHub's CI into the local promote, which
# invalidates every enclosing timeout sized when the tier effectively never ran —
# and no repo-side test can see those timeouts, so the suite has to build one.
#
#   C7 wrapper timeout BELOW the tier's cost   -> the promote is killed (124) and
#      the kill is SAFE: the integration tip is unmoved and the ci-verify ref is
#      untouched, so the only cost is the run. Nothing is pushed before a green
#      verdict, which is why this is nasty rather than dangerous.
#   C8 the SAME fixture, wrapper ABOVE the cost -> promotes normally. C7's
#      non-vacuity: without it, C7 cannot tell an enclosing timeout apart from
#      the optimistic path genuinely malfunctioning — which is precisely the
#      misdiagnosis the bead predicts, arriving exactly when someone is trying to
#      confirm jd5fj.4 works.
#   C9 `promote-budget --seconds` -> a bare integer above queue+tier that TRACKS
#      the gate's own knobs, so a wrapper DERIVES its bound instead of keeping a
#      second copy of the number. Deriving is the fix; documenting is not.
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

# The merge_preview tier's duration budget from the SSOT (SABLE-jd5fj.8's own
# wall-clock assertion derives its bound from this, not a hardcoded number, so
# it tracks the SSOT the same way default_mg_timeout already does).
BUDGET_MERGE_PREVIEW="$(bash "$REPO_ROOT/.github/ci/test-tiers.sh" --budget merge_preview)"

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

# SABLE-jd5fj.18: `bd show <bead>` used by every scenario UNLESS a scenario
# overrides SABLE_MG_BD itself. It declares an EXPLICIT, empty '## File
# reads' section, so the read-write floor's undeclared-reads-forces-serialize
# default (see sable_footprint_lib.declared_reads) does not fire on the C1-C9
# scenarios above, none of which are testing that floor. C10-C12 below
# override SABLE_MG_BD per-scenario to exercise it directly.
FAKE_BD_READS_NONE="$TMPROOT/fake-bd-reads-none"
cat > "$FAKE_BD_READS_NONE" <<'EOF'
#!/bin/sh
echo '## File reads'
echo 'none'
EOF
chmod +x "$FAKE_BD_READS_NONE"

# SABLE-jd5fj.18: a `bd show` stub that declares a REAL read footprint —
# .github/ci/impact-manifest.sh, a file that already exists in seed_repo's
# tree, so C10-C11 can put a real base-move edit under it.
FAKE_BD_READS_IMPACT_MANIFEST="$TMPROOT/fake-bd-reads-impact-manifest"
cat > "$FAKE_BD_READS_IMPACT_MANIFEST" <<'EOF'
#!/bin/sh
echo '## File reads'
echo '.github/ci/impact-manifest.sh'
EOF
chmod +x "$FAKE_BD_READS_IMPACT_MANIFEST"

# SABLE-jd5fj.10: a `bd show <bead> --json` stub emitting the STRUCTURED
# footprint field (real JSON, not prose) — declares footprint_writes naming a
# lockfile-shaped path neither mutation function touches at all (outside BOTH
# mechanical diffs). This exercises the structured field end to end: the
# widen forcing C13's refusal comes ENTIRELY from bd's metadata, not from
# any prose the gate could have parsed instead.
FAKE_BD_STRUCTURED_FOOTPRINT="$TMPROOT/fake-bd-structured-footprint"
cat > "$FAKE_BD_STRUCTURED_FOOTPRINT" <<'EOF'
#!/usr/bin/env python3
import json
print(json.dumps([{
    "description": "story text, no '## File footprint' prose section at all",
    "metadata": {"footprint_writes": "totally/unrelated/module/poetry.lock"},
}]))
EOF
chmod +x "$FAKE_BD_STRUCTURED_FOOTPRINT"

# SABLE-mbkbm: reads the per-phase impact-tier journal (impact-tier-windows.jsonl)
# a real promote just wrote and checks one of two properties against the SAME
# paired start/end record, never against an inferred split:
#   reconcile               -- setup + a shell:<suite> + pytest all present,
#                               AND their sum reconciles with the record's own
#                               OBSERVED total wall-clock (end.at - start.at).
#   dominant-shell-no-pytest -- the single largest phase is a shell suite (the
#                               POSITIVE CONTROL for a known stubbed-slow cost),
#                               and no "pytest" entry exists at all (the same
#                               NEGATIVE CONTROL the unit suite covers, proven
#                               again here against the real gate).
PHASE_JOURNAL_CHECK="$TMPROOT/phase-journal-check.py"
cat > "$PHASE_JOURNAL_CHECK" <<'EOF'
#!/usr/bin/env python3
import json, sys
path, mode = sys.argv[1], sys.argv[2]
try:
    lines = [json.loads(l) for l in open(path) if l.strip()]
except FileNotFoundError:
    print("FAIL no-journal-file")
    sys.exit(0)
starts = {}
paired = None
for l in lines:
    key = (l.get("pid"), l.get("tree"))
    if l.get("event") == "start":
        starts[key] = l
    elif l.get("event") == "end" and l.get("schema", 1) >= 2 and "phases" in l:
        s = starts.get(key)
        if s is not None:
            paired = (s, l)
if paired is None:
    print("FAIL no-schema2-paired-end-record")
    sys.exit(0)
s, e = paired
total = e["at"] - s["at"]
by_name = {}
for p in e.get("phases") or []:
    by_name[p["name"]] = by_name.get(p["name"], 0.0) + p["seconds"]
phase_sum = sum(by_name.values())

if mode == "reconcile":
    has_setup = "setup" in by_name
    has_shell = any(n.startswith("shell:") for n in by_name)
    has_pytest = "pytest" in by_name
    tol = max(3.0, total * 0.5)
    within = abs(total - phase_sum) <= tol
    ok = has_setup and has_shell and has_pytest and within
    print(f"{'OK' if ok else 'FAIL'} setup={has_setup} shell={has_shell} pytest={has_pytest} "
          f"total={total:.2f} phase_sum={phase_sum:.2f} within={within} phases={by_name}")
elif mode == "dominant-shell-no-pytest":
    dominant = max(by_name, key=by_name.get) if by_name else None
    has_pytest = "pytest" in by_name
    ok = (dominant is not None and dominant.startswith("shell:")
          and by_name[dominant] >= 4.0 and not has_pytest)
    print(f"{'OK' if ok else 'FAIL'} dominant={dominant} seconds={by_name.get(dominant)} "
          f"has_pytest={has_pytest} phases={by_name}")
else:
    print(f"FAIL unknown-mode:{mode}")
sys.exit(0)
EOF
chmod +x "$PHASE_JOURNAL_CHECK"

# Where the sandbox's own promote calls write the journal — B_WORK is a normal
# (non-bare) clone, so git-common-dir resolves to "$B_WORK/.git" and
# sable_snapshot_lib.state_dir takes its parent (SABLE-mbkbm reuses the same
# resolution the lock/window code already has, not a new path).
window_log() { echo "$B_WORK/.claude/sable/state/merge-gate/impact-tier-windows.jsonl"; }

# --------------------------------------------------------------------------
# The sandbox repo: a real (tiny) impact tier over a real (tiny) codebase.
# --------------------------------------------------------------------------
seed_repo() {
  local w="$1"
  mkdir -p "$w/.github/ci" "$w/hooks/test" "$w/left" "$w/right" "$w/bin"

  # A fast stub for the bin/ pytest half of the combined-tree impact tier
  # (SABLE-jd5fj.8). Real bin/tier_selection.py falls back to a conservative
  # full bin/ run on a cold .testmondata; this sandbox never carries a
  # .testmondata (setup_pair reseeds from scratch every case), so every case
  # whose footprint reaches bin/ exercises exactly that cold-cache path. The
  # stub stands in for the real module the same way test-left.sh/test-right.sh
  # already stand in for a real suite: fast and real-executed, not the
  # production module itself.
  cat > "$w/bin/tier_selection.py" <<'EOF'
#!/usr/bin/env python3
print("PASS: stub bin/ pytest impact tier")
EOF
  chmod +x "$w/bin/tier_selection.py"

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
# SLOW_TIER makes this suite take a known, real amount of wall-clock, so C7/C8
# can put a wrapper timeout on either side of it. Unset in every other case, so
# the tier stays as fast as it was.
if [ -n "${SLOW_TIER:-}" ]; then sleep "$SLOW_TIER"; fi
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
      SABLE_MG_GH="$FAKE_GH" SABLE_MG_BD="${SABLE_MG_BD:-$FAKE_BD_READS_NONE}" SABLE_MG_NOTIFY=true \
      SABLE_MG_POLL=0 SABLE_MG_GRACE=0 SABLE_MG_TIMEOUT=0 \
      SABLE_MG_OPTIMISTIC="${SABLE_MG_OPTIMISTIC:-1}" \
      SLOW_TIER="${SLOW_TIER:-}" \
      PATH="$PATH" python3 "$GATE" "$@" 2>&1
}

# gate_wrapped <wrapper-seconds> <gate args...> — the SABLE-w0zjm shape: the gate
# run INSIDE an operator's enclosing `timeout`, which is where the real defect
# lives (chuck's wrapper was 900s, the same number as the tier budget). Exits 124
# when the wrapper kills it.
gate_wrapped() {
  local secs="$1"; shift
  timeout "$secs" env FAKE_GH_ORIGIN="$B_ORIGIN" FAKE_GH_BASE="$BASE_BR" \
      FAKE_GH_MODE="${FAKE_GH_MODE:-success}" FAKE_GH_ADVANCE="${FAKE_GH_ADVANCE:-}" \
      SABLE_MG_GH="$FAKE_GH" SABLE_MG_BD="${SABLE_MG_BD:-$FAKE_BD_READS_NONE}" SABLE_MG_NOTIFY=true \
      SABLE_MG_POLL=0 SABLE_MG_GRACE=0 SABLE_MG_TIMEOUT=0 \
      SABLE_MG_OPTIMISTIC="${SABLE_MG_OPTIMISTIC:-1}" \
      SLOW_TIER="${SLOW_TIER:-}" \
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
# C1-only (SABLE-jd5fj.8): the same disjoint left-side change, PLUS a bin/
# path, so C1's footprint reaches the pytest half of the combined-tree impact
# tier (bin/sable_gate_promote_lib.py::_run_impact_tier_locked only invokes
# bin/tier_selection.py when the footprint has a path starting with "bin/").
# A dedicated function, not a change to mut_branch_disjoint_ok itself, so C6/
# C7/C8 (which reuse that function for their own, unrelated assertions) are
# not affected by the pytest half at all.
mut_branch_disjoint_ok_with_bin() { mut_branch_disjoint_ok; echo 'x=2' > "$B_WORK/bin/extra.sh"; }
mut_base_disjoint_ok()     { echo 'other=1'   > "$B_WORK/right/other.sh"; }
mut_branch_route()         { echo 'ROUTE=/health' > "$B_WORK/left/routes.sh"; }
mut_base_route()           { echo 'ROUTE=/health' > "$B_WORK/right/routes.sh"; }
mut_branch_api_first()     { sed -i '1s/.*/api_v=2/' "$B_WORK/left/api.sh"; }
mut_base_api_last()        { sed -i '5s/.*/api_d=2/' "$B_WORK/left/api.sh"; }
mut_branch_modify_caller() { echo 'caller_v=2' > "$B_WORK/right/caller.sh"; }
mut_base_delete_caller()   { git -C "$B_WORK" rm -q "right/caller.sh"; }
mut_base_lockfile()        { echo 'resolved=1' > "$B_WORK/package-lock.json"; }
# SABLE-jd5fj.18: edits a file the read-write floor scenarios declare the
# branch READS, via SABLE_MG_BD, never via a branch-side write.
mut_base_edit_impact_manifest() { echo '# touched by base move' >> "$B_WORK/.github/ci/impact-manifest.sh"; }

# ==========================================================================
# C1 — disjoint + stale base + green combined tree
# ==========================================================================
scenario c1 mut_branch_disjoint_ok_with_bin mut_base_disjoint_ok
START_C1=$(date +%s)
OUT="$(FAKE_GH_ADVANCE="$MOVED_SHA" gate promote --bead TEST-C1 --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
ELAPSED_C1=$(( $(date +%s) - START_C1 ))
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
# SABLE-jd5fj.8: C1's footprint now reaches bin/ (mut_branch_disjoint_ok_with_bin),
# so the pytest half must ALSO run and name itself in the same detail string —
# the whole point being that a cold-cache fallback to a full bin/ run is
# reported, not silent.
if printf '%s' "$OUT" | grep -q 'bin/ pytest impact tier'; then
  pass "C1: the pytest half of the impact tier ran and named itself"
else
  fail "C1: the pytest half of the impact tier ran and named itself" "out=$OUT"
fi
if printf '%s' "$OUT" | grep -q 'no warm .testmondata'; then
  pass "C1: the cold-cache fallback (no .testmondata in this sandbox) is named, not silent"
else
  fail "C1: the cold-cache fallback is named" "out=$OUT"
fi
if [ "$ELAPSED_C1" -le "$BUDGET_MERGE_PREVIEW" ]; then
  pass "C1: the whole optimistic promote (including the bin/ pytest half) finished within the merge_preview budget (${ELAPSED_C1}s <= ${BUDGET_MERGE_PREVIEW}s)"
else
  fail "C1: the promote finished within the merge_preview budget" "elapsed=${ELAPSED_C1}s budget=${BUDGET_MERGE_PREVIEW}s"
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

# SABLE-mbkbm INTEGRATION: C1's footprint reaches BOTH halves (shell + bin/
# pytest, see mut_branch_disjoint_ok_with_bin above), so it is the fixture that
# proves the instrument can decompose a real tier's wall-clock, not just emit
# a total. The check re-derives the SAME "observed total wall-clock" the
# pre-mbkbm journal already reported (end.at - start.at) and asserts the
# per-phase sum reconciles with it — no split is inferred, only measured.
PHASE_CHECK_C1="$(python3 "$PHASE_JOURNAL_CHECK" "$(window_log)" reconcile)"
if printf '%s' "$PHASE_CHECK_C1" | grep -q '^OK'; then
  pass "C1: the impact-tier journal carries setup+shell+pytest phase entries that reconcile with the observed total tier wall-clock"
else
  fail "C1: the per-phase journal reconciles with the tier's own observed total wall-clock" "$PHASE_CHECK_C1"
fi

# ==========================================================================
# C1b — the gate's OWN persisted warm .testmondata (SABLE-jd5fj.8 approach (a))
# ==========================================================================
# C1 above is the genuinely-cold case: this sandbox's B_WORK never carries a
# root .testmondata (a fresh clone every scenario), so it exercises the
# "no warm .testmondata" fallback. C1b is the gap this bead's revision
# actually closes: a checkout — like Chuck's real one — that ALSO never
# carries a root .testmondata, but DOES have the gate's own persisted cache
# under its state dir (populated by `sable-merge-gate warm-testmon-cache`,
# unit-tested directly in bin/test_promote_decision.py). That persisted copy
# must be used and named, not silently treated as cold a second time.
scenario c1b mut_branch_disjoint_ok_with_bin mut_base_disjoint_ok
mkdir -p "$B_WORK/.claude/sable/state/merge-gate"
echo 'persisted gate cache' > "$B_WORK/.claude/sable/state/merge-gate/testmondata-warm"
OUT="$(FAKE_GH_ADVANCE="$MOVED_SHA" gate promote --bead TEST-C1B --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 0 ]; then
  pass "C1b: a disjoint stale base with only a persisted gate cache still promotes (exit 0)"
else
  fail "C1b: a disjoint stale base with only a persisted gate cache still promotes" "rc=$RC out=$OUT"
fi
if printf '%s' "$OUT" | grep -q 'warm testmon map (gate cache)'; then
  pass "C1b: the gate's own persisted warm .testmondata was found and named, not treated as cold"
else
  fail "C1b: the gate's persisted warm .testmondata was named" "out=$OUT"
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

# ==========================================================================
# C7 — an ENCLOSING wrapper timeout shorter than the tier kills the promote,
#      and the kill is SAFE (SABLE-w0zjm)
# ==========================================================================
# The bead's whole thesis: jd5fj.4 moved cost from GitHub's CI into the local
# promote, so a wrapper sized when the tier never ran now fires mid-tier. What
# must be proven is not that the kill is impossible — it is that the kill costs
# nothing but the run: nothing is pushed before a green verdict, so the base is
# exactly where it was and the ci-verify ref is still there to retry against.
scenario c7 mut_branch_disjoint_ok mut_base_disjoint_ok
REFS_BEFORE="$(ci_refs)"
OUT="$(SLOW_TIER=8 FAKE_GH_ADVANCE="$MOVED_SHA" gate_wrapped 3 promote --bead TEST-C7 \
        --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?

if [ "$RC" -eq 124 ]; then
  pass "C7: a wrapper timeout shorter than the impact tier kills the promote (exit 124)"
else
  fail "C7: the short wrapper kills the promote" "rc=$RC out=$OUT"
fi
if [ "$(origin_sha "$BASE_BR")" = "$MOVED_SHA" ]; then
  pass "C7: the killed promote pushed NOTHING — the integration tip is unmoved"
else
  fail "C7: the killed promote pushed nothing" "base=$(origin_sha "$BASE_BR") moved=$MOVED_SHA"
fi
if [ "$(ci_refs)" = "$REFS_BEFORE" ]; then
  pass "C7: no ci-verify ref was created or consumed — the promote is still retryable"
else
  fail "C7: ci-verify refs unchanged" "before=[$REFS_BEFORE] after=[$(ci_refs)]"
fi
if printf '%s' "$OUT" | grep -q 'ENTERING IMPACT TIER'; then
  pass "C7: the last line before the kill says it was IN the tier (diagnosable, not mysterious)"
else
  fail "C7: the in-tier marker is emitted before the kill" "out=$OUT"
fi

# ==========================================================================
# C8 — NEGATIVE CONTROL: the same fixture with a wrapper ABOVE the budget
# ==========================================================================
# Without this, C7 proves only that something failed — it could not distinguish
# an enclosing timeout from the optimistic path genuinely malfunctioning, which
# is the exact misdiagnosis the bead predicts.
scenario c8 mut_branch_disjoint_ok mut_base_disjoint_ok
OUT="$(SLOW_TIER=8 FAKE_GH_ADVANCE="$MOVED_SHA" gate_wrapped 120 promote --bead TEST-C8 \
        --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
LANDED="$(origin_sha "$BASE_BR")"

if [ "$RC" -eq 0 ]; then
  pass "C8: the SAME slow tier promotes normally under a wrapper above the budget (exit 0)"
else
  fail "C8: a wrapper above the budget promotes normally" "rc=$RC out=$OUT"
fi
if [ "$(parents_of "$LANDED")" = "$MOVED_SHA $WK_SHA" ]; then
  pass "C8: the combined object landed — C7's failure was the wrapper, not the path"
else
  fail "C8: the combined object landed" "parents=$(parents_of "$LANDED")"
fi
if [ -z "$(ci_refs)" ]; then
  pass "C8: the ci-verify ref is cleaned up, exactly as on the un-wrapped C1 path"
else
  fail "C8: ci-verify ref cleaned up" "refs=[$(ci_refs)]"
fi

# SABLE-mbkbm INTEGRATION, POSITIVE CONTROL: C8's fixture already stubs a REAL
# ~8s cost into test-left.sh (SLOW_TIER=8, above) under a wrapper that does NOT
# kill the promote — an instrument that cannot show a KNOWN slow phase as
# dominant is decorative, not a measurement. C8's footprint (mut_branch_
# disjoint_ok / mut_base_disjoint_ok) never touches bin/, so this doubles as
# the NEGATIVE CONTROL: no "pytest" phase entry should exist at all.
PHASE_CHECK_C8="$(python3 "$PHASE_JOURNAL_CHECK" "$(window_log)" dominant-shell-no-pytest)"
if printf '%s' "$PHASE_CHECK_C8" | grep -q '^OK'; then
  pass "C8: the impact-tier journal names the stubbed 8s shell suite as the dominant phase, with no fabricated pytest entry"
else
  fail "C8: the journal attributes the stubbed slow phase as dominant with no pytest entry" "$PHASE_CHECK_C8"
fi

# ==========================================================================
# C9 — the wrapper can DERIVE its bound instead of copying it (SABLE-w0zjm b)
# ==========================================================================
# Documenting "make your timeout bigger" is what failed: the number lived in two
# places and only one of them was in this repo. The gate reporting its own budget
# is what removes the second copy.
DERIVED="$(SABLE_MG_IMPACT_TIMEOUT=90 SABLE_MG_IMPACT_LOCK_TIMEOUT=210 \
           python3 "$GATE" promote-budget --seconds 2>&1)"; RC=$?
if [ "$RC" -eq 0 ] && printf '%s' "$DERIVED" | grep -qx '[0-9]\+'; then
  pass "C9: the gate prints a bare integer a shell can hand straight to \`timeout\`"
else
  fail "C9: promote-budget --seconds prints a bare integer" "rc=$RC out=$DERIVED"
fi
if [ "${RC}" -eq 0 ] && [ "$DERIVED" -gt 300 ]; then
  pass "C9: the derived bound exceeds queue+tier (90+210), so a wrapper using it cannot fire early"
else
  fail "C9: the derived bound exceeds queue+tier" "derived=$DERIVED want>300"
fi
# Non-vacuity: it must TRACK the env, not print a constant that happens to be big.
DERIVED2="$(SABLE_MG_IMPACT_TIMEOUT=900 SABLE_MG_IMPACT_LOCK_TIMEOUT=3600 \
            python3 "$GATE" promote-budget --seconds 2>&1)"
if [ "$DERIVED2" -gt "$DERIVED" ]; then
  pass "C9: the derived bound TRACKS the gate's own knobs (a copied constant could not)"
else
  fail "C9: the derived bound tracks the knobs" "small=$DERIVED large=$DERIVED2"
fi
# And the wrapper C8 actually used must have been above the fixture's real cost —
# stated here so the 120 above is a derived-style bound, not a magic number.
# SABLE_MG_COVERAGE_FLOOR_TIMEOUT is pinned here (SABLE-5v3d5: it is now a
# THIRD, always-present summand, unaffected by SABLE_MG_IMPACT_SERIALIZE) so
# this assertion stays a deterministic property of the gate's arithmetic
# instead of depending on this repo's own ambient merge_preview SSOT value.
if [ "$(SABLE_MG_IMPACT_TIMEOUT=8 SABLE_MG_IMPACT_SERIALIZE=0 \
        SABLE_MG_COVERAGE_FLOOR_TIMEOUT=8 \
        python3 "$GATE" promote-budget --seconds)" -le 120 ]; then
  pass "C9: C8's wrapper (120s) is above what the gate itself would recommend for an 8s tier"
else
  fail "C9: C8's wrapper is above the recommended bound" "recommendation exceeds 120s"
fi

# ==========================================================================
# C9b — the coverage-floor ceiling is a THIRD summand, not a silent gap
#       (SABLE-5v3d5)
# ==========================================================================
# assert_coverage_floor()'s own subprocess bound (run_coverage_floor_check)
# runs INSIDE promote(), before any preview/CI work -- but before this bead it
# was never a term in promote-budget's sum. The seat's own post-re-pin check
# ("did the number move?") read the SAME 5400 before and after cmar4.9 moved
# the coverage floor's SSOT from 600 to 900, because the changed term was
# never a summand. This proves the CLI's reported number tracks a
# DISTINCTIVE coverage-floor override end to end, through the real
# subcommand -- not just inside the library.
BASE_BUDGET="$(SABLE_MG_IMPACT_TIMEOUT=90 SABLE_MG_IMPACT_LOCK_TIMEOUT=210 \
               SABLE_MG_COVERAGE_FLOOR_TIMEOUT=11 \
               python3 "$GATE" promote-budget --seconds 2>&1)"
BUMPED_BUDGET="$(SABLE_MG_IMPACT_TIMEOUT=90 SABLE_MG_IMPACT_LOCK_TIMEOUT=210 \
                 SABLE_MG_COVERAGE_FLOOR_TIMEOUT=9011 \
                 python3 "$GATE" promote-budget --seconds 2>&1)"
if [ "$BUMPED_BUDGET" -gt "$BASE_BUDGET" ]; then
  pass "C9b: promote-budget's recommended timeout TRACKS SABLE_MG_COVERAGE_FLOOR_TIMEOUT end to end"
else
  fail "C9b: promote-budget's recommended timeout tracks the coverage-floor override" \
       "base(11s)=$BASE_BUDGET bumped(9011s)=$BUMPED_BUDGET"
fi
COVERAGE_JSON="$(SABLE_MG_IMPACT_TIMEOUT=90 SABLE_MG_IMPACT_LOCK_TIMEOUT=210 \
                 SABLE_MG_COVERAGE_FLOOR_TIMEOUT=4321 \
                 python3 "$GATE" promote-budget --json 2>&1)"
if printf '%s' "$COVERAGE_JSON" | grep -q '"coverage_floor_timeout_s": *4321'; then
  pass "C9b: promote-budget --json reports coverage_floor_timeout_s as its own decomposition key"
else
  fail "C9b: promote-budget --json reports coverage_floor_timeout_s" "$COVERAGE_JSON"
fi
WORST_CASE="$(printf '%s' "$COVERAGE_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(int(d["worst_case_s"]))')"
if [ "$WORST_CASE" -eq $((90 + 210 + 4321)) ]; then
  pass "C9b: worst_case_s is the sum of all three terms, coverage floor included"
else
  fail "C9b: worst_case_s equals tier+lock+coverage_floor" "worst_case_s=$WORST_CASE want=$((90+210+4321))"
fi

# ==========================================================================
# C10 — SABLE-jd5fj.18 read-write floor, POSITIVE CONTROL: a declared,
#       genuinely disjoint read set still takes the optimistic path
# ==========================================================================
# Non-vacuity for C11 below: the floor must not simply serialize everything
# once a '## File reads' section exists. A branch that declares a read
# footprint truly disjoint from the base-move's writes still promotes.
scenario c10 mut_branch_disjoint_ok mut_base_disjoint_ok
OUT="$(SABLE_MG_BD="$FAKE_BD_READS_IMPACT_MANIFEST" FAKE_GH_ADVANCE="$MOVED_SHA" \
      gate promote --bead TEST-C10 --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 0 ]; then
  pass "C10: a declared, genuinely disjoint read set still promotes on the optimistic path (exit 0)"
else
  fail "C10: a declared disjoint read set still promotes" "rc=$RC out=$OUT"
fi

# ==========================================================================
# C11 — SABLE-jd5fj.18 THE DEFECT'S LIVE SHAPE: footprint-disjoint on WRITES,
#       but the branch READS a file the base-move EDITS -- must NOT promote
# ==========================================================================
# The live instance this bead was filed against: SABLE-jd5fj.8 writes bin/ and
# READS .github/ci/test-tiers.sh; SABLE-cmar4.5 (played by the base-move here)
# writes .github/ci/ files including that interface. No file is written by
# both sides -- the pre-jd5fj.18 gate would have promoted this in parallel.
scenario c11 mut_branch_disjoint_ok mut_base_edit_impact_manifest
OUT="$(SABLE_MG_BD="$FAKE_BD_READS_IMPACT_MANIFEST" FAKE_GH_ADVANCE="$MOVED_SHA" \
      gate promote --bead TEST-C11 --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 23 ]; then
  pass "C11: a branch that READS a file the base-move EDITS is NOT promoted in parallel (exit 23)"
else
  fail "C11: read-write coupling forces a full re-preview" "rc=$RC out=$OUT"
fi
if [ "$(origin_sha "$BASE_BR")" = "$MOVED_SHA" ]; then
  pass "C11: nothing was promoted onto the moved base"
else
  fail "C11: nothing was promoted onto the moved base" "base=$(origin_sha "$BASE_BR")"
fi
if printf '%s' "$OUT" | grep -q 'impact-manifest.sh'; then
  pass "C11: the refusal names the coupled read path (auditable evidence, not a bare code)"
else
  fail "C11: the refusal names the coupled read path" "out=$OUT"
fi
if printf '%s' "$OUT" | grep -qi 'read/write coupling'; then
  pass "C11: the refusal is attributed to read/write coupling, distinguishable from a bare write overlap"
else
  fail "C11: the refusal distinguishes read/write coupling from other refusal reasons" "out=$OUT"
fi
# Non-vacuity: the write footprints alone are genuinely disjoint here, so this
# case exercises the NEW read-write conjunct and not C3's already-covered
# write/write overlap.
if git -C "$B_WORK" merge-tree --write-tree "$MOVED_SHA" "$WK_SHA" >/dev/null 2>&1; then
  pass "C11: git itself considers the merge textually clean -- the refusal is the read-write floor's own added safety"
else
  fail "C11: the merge should be textually clean" "merge-tree reported a conflict"
fi

# ==========================================================================
# C12 — SABLE-jd5fj.18 SECOND CONTROL: an UNDECLARED read set forces
#       serialization even though the write footprints are disjoint
# ==========================================================================
# Distinguishable from C11 in the OUTPUT, not only in the decision: C11 says
# it COULD tell and the paths conflict; C12 says it could NOT tell at all --
# "I could not tell" and "I can tell, and they conflict" must not read the same.
scenario c12 mut_branch_disjoint_ok mut_base_disjoint_ok
OUT="$(SABLE_MG_BD=true FAKE_GH_ADVANCE="$MOVED_SHA" \
      gate promote --bead TEST-C12 --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 23 ]; then
  pass "C12: an undeclared read set forces serialization even on write-disjoint footprints (exit 23)"
else
  fail "C12: an undeclared read set forces serialization" "rc=$RC out=$OUT"
fi
if printf '%s' "$OUT" | grep -qi 'undetermined'; then
  pass "C12: the refusal says the read set was undetermined (distinct from C11's found conflict)"
else
  fail "C12: the refusal names the undetermined read set, distinct from a real conflict" "out=$OUT"
fi
if ! printf '%s' "$OUT" | grep -qi 'read/write coupling'; then
  pass "C12: the undetermined case is NOT reported as a found conflict -- the two decisions stay distinguishable in the report"
else
  fail "C12: the undetermined case must not read like a found read/write conflict" "out=$OUT"
fi

# ==========================================================================
# C13 — SABLE-jd5fj.10: the STRUCTURED footprint_writes metadata field (not
#       prose) declares a path outside both mechanical diffs -> full re-preview
# ==========================================================================
# Both sides are mechanically disjoint (same fixture as C1/C6) -- git alone
# would call this a clean, disjoint merge. bd's --json output carries a
# structured footprint_writes field naming a lockfile-shaped path neither
# mutation touches; declared_footprint() must consume that field VERBATIM
# (the description above literally says it has no prose section) and the
# widened branch footprint must still force the refusal via the sentinel rule.
scenario c13 mut_branch_disjoint_ok mut_base_disjoint_ok
OUT="$(SABLE_MG_BD="$FAKE_BD_STRUCTURED_FOOTPRINT" FAKE_GH_ADVANCE="$MOVED_SHA" \
      gate promote --bead TEST-C13 --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 23 ]; then
  pass "C13: a structured footprint_writes field declaring a path outside both diffs forces a full re-preview (exit 23)"
else
  fail "C13: a structured declared-footprint path forces a full re-preview" "rc=$RC out=$OUT"
fi
if [ "$(origin_sha "$BASE_BR")" = "$MOVED_SHA" ]; then
  pass "C13: nothing was promoted onto the moved base"
else
  fail "C13: nothing was promoted onto the moved base" "base=$(origin_sha "$BASE_BR")"
fi
if printf '%s' "$OUT" | grep -q 'totally/unrelated/module/poetry.lock'; then
  pass "C13: the refusal names the structured-field-declared path (auditable evidence, not a bare code)"
else
  fail "C13: the refusal names the structured-field-declared path" "out=$OUT"
fi
# Non-vacuity: C1/C6 already prove this EXACT mutation pair
# (mut_branch_disjoint_ok / mut_base_disjoint_ok) promotes on exit 0 when bd
# declares nothing extra (FAKE_BD_READS_NONE, no footprint_writes key at all)
# -- so C13's refusal here is attributable to the structured field alone, not
# to the fixture already being non-disjoint.
if git -C "$B_WORK" merge-tree --write-tree "$MOVED_SHA" "$WK_SHA" >/dev/null 2>&1; then
  pass "C13: git itself considers the merge textually clean -- the refusal is the structured field's own added safety"
else
  fail "C13: the merge should be textually clean" "merge-tree reported a conflict"
fi

echo "----------------------------------------------------------------------"
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
[ "$FAIL" -eq 0 ]
