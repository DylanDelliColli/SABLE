#!/usr/bin/env bash
# test-snapshot-freeze.sh — green snapshot + freeze/quarantine, in real
# composition (SABLE-jd5fj.5).
#
# WHAT IS UNDER TEST
# ------------------
# The fleet-wide safety mechanism: a scheduled full-suite run whose ONE
# automatic re-run of the failed suites is a CLASSIFIER (deterministic vs
# flaky), the per-repo freeze/quarantine state it writes, and the MECHANICAL
# refusal that state produces in the only function that writes to the
# integration branch.
#
# THE FIXTURE IS THE ARGUMENT. Nothing about the classification is simulated:
# the sandbox repo carries a real tier SSOT (the cmar4.1 --list/--budget
# interface) and real suites that really pass and really fail — including a
# GENUINELY FLAKY one, a script that fails its first invocation and passes its
# second by reading a counter file it increments itself. So "red -> green means
# flaky" is measured through the real runner, real subprocesses, real exit
# codes, not injected as a dict. Same for the state: real JSON files at the real
# git-common-dir-resolved path, and a real `sable-merge-gate promote` against a
# real origin + clone that really refuses.
#
#   C1  deterministic red (fails twice)  -> FREEZE written, exit 25
#   C2  the freeze names the suites and is readable by `status`
#   C3  a real FLAKE (red then green)    -> NO freeze, suite QUARANTINED, exit 26
#   C4  a quarantined suite that fails twice STILL RUNS and is still recorded,
#       and does NOT freeze — quarantine is an exclusion from the trigger, not
#       a skip. (The "still runs" half is measured from the suite's own side
#       effect, not from the runner's report.)
#   C5  promote is DENIED while frozen: exit 25, and the integration branch tip
#       does not move.
#   C6  the SAME promote succeeds once unfrozen — C5's non-vacuity. Without
#       this, C5 would pass just as well against a promote that is broken.
#   C7  a CORRUPT freeze file denies the promote (fail-closed).
#   C8  a green snapshot CLEARS an existing freeze.
#   C9  exactly one bisect bead across repeated freezing snapshots, and exactly
#       one flaky-fix bead per flaky suite (real sandboxed bd).
#   C10 the shipped state dir is real and its README is tracked while the state
#       files are ignored — both directions of the .gitignore exception.
#   C11 the suite is hermetic against an ambient SABLE_MERGE_GATE_STATE — the
#       leak that made every "per-repo" state dir below collapse into one.
#
# ACTIVATION NOTE (SABLE-jd5fj.5): bin/ is a PINNED SNAPSHOT DIRECTORY, so
# merging this changes nothing at runtime until an operator-brokered pin
# refresh. Everything asserted here runs against the WORKING TREE — it is
# evidence about the code, and deliberately not a claim about the live fleet.
# That distinction is pointed for this bead in particular: this IS a gate, and
# a gate whose only evidence was a pre-refresh behavioural observation would be
# attesting itself with exactly the class of non-evidence it exists to prevent.
#
# Run with:
#   bash hooks/test/test-snapshot-freeze.sh
#
# Clean-room safe (SABLE-59zu): C1-C8 and C10 need only bash + git + python3.
# C9 needs a real `bd` and SELF-SKIPS (loudly) when there isn't one — it drives
# a throwaway HOME with its own `bd init --non-interactive` database, never the
# real bead pool.

set -uo pipefail

# ENV HERMETICITY, before anything else. Every knob this suite exercises is
# also an override a developer (or a wrapping snapshot run) may have exported —
# and the state-dir override is the sharpest: with it set, every "per-repo"
# state dir below collapses into ONE shared directory and the freeze written by
# case N is read by case N+1. That is not hypothetical; it is how this suite
# first failed, run from inside a sandboxed snapshot invocation. A suite whose
# result depends on the ambient environment is a suite that reports on the
# developer's shell, not on the code.
unset SABLE_MERGE_GATE_STATE SABLE_SNAPSHOT_RUNNER SABLE_SNAPSHOT_TIMEOUT \
      SABLE_SNAPSHOT_BRANCH SABLE_SNAPSHOT_RUN_URL SABLE_MG_REPO SABLE_MG_BASE \
      SABLE_MG_REMOTE SABLE_MG_OPTIMISTIC

# Resolve absolute paths BEFORE the sandbox preamble cds away (SABLE-0ssz.2).
TESTDIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$TESTDIR/../.." && pwd)"
SNAPSHOT="$REPO_ROOT/bin/sable-snapshot"
GATE="$REPO_ROOT/bin/sable-merge-gate"

# shellcheck source=lib-git-sandbox.sh
source "$TESTDIR/lib-git-sandbox.sh"

PASS=0
FAIL=0
SKIP=0
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
skip() { SKIP=$((SKIP+1)); echo "SKIP: $1"; }

[ -f "$SNAPSHOT" ] || { echo "FATAL: missing $SNAPSHOT"; exit 2; }
[ -f "$GATE" ]     || { echo "FATAL: missing $GATE"; exit 2; }

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"; sable_test_git_sandbox_cleanup' EXIT

BASE_BR="trunk"

# ---------------------------------------------------------------------------
# The sandbox repo: a real tier SSOT over real suites.
# ---------------------------------------------------------------------------
# seed_snapshot_repo <dir> — a git repo carrying:
#   test-good.sh     always passes
#   test-broken.sh   always fails            -> deterministic red
#   test-flaky.sh    fails once, then passes -> a REAL red-then-green flake
# Each suite appends to hooks/test/<name>.runs, so "did it actually run" is
# observable from the suite's own side effect rather than from the runner's
# self-report (C4 depends on that distinction).
seed_snapshot_repo() {
  local w="$1"
  mkdir -p "$w/.github/ci" "$w/hooks/test"

  cat > "$w/.github/ci/test-tiers.sh" <<'EOF'
#!/usr/bin/env bash
# Minimal REAL tier SSOT: the same --list/--budget interface the production
# .github/ci/test-tiers.sh exposes (SABLE-cmar4.1), so the runner under test
# reads suite membership from an SSOT exactly as it does in production.
set -uo pipefail
case "${1:-}" in
  --list)
    case "${2:-}" in
      full_snapshot) printf 'test-good.sh\ntest-broken.sh\ntest-flaky.sh\n' ;;
      pre_push)      printf 'test-good.sh\n' ;;
      *) echo "unknown tier '${2:-}'" >&2; exit 1 ;;
    esac ;;
  --budget)
    case "${2:-}" in
      full_snapshot|pre_push) echo 60 ;;
      *) echo "unknown tier '${2:-}'" >&2; exit 1 ;;
    esac ;;
  *) echo "usage: $0 --list <tier> | --budget <tier>" >&2; exit 2 ;;
esac
EOF
  chmod +x "$w/.github/ci/test-tiers.sh"

  cat > "$w/hooks/test/test-good.sh" <<'EOF'
#!/usr/bin/env bash
echo run >> "$(dirname "$0")/test-good.sh.runs"
echo "PASS: good"
EOF
  cat > "$w/hooks/test/test-broken.sh" <<'EOF'
#!/usr/bin/env bash
echo run >> "$(dirname "$0")/test-broken.sh.runs"
echo "FAIL: broken — a real, reproducing failure"
exit 1
EOF
  # The genuine flake: fails the FIRST time it is ever invoked, passes after.
  # Nondeterminism-by-construction, so the classifier's red->green path runs
  # against a real second process and a real second exit code.
  cat > "$w/hooks/test/test-flaky.sh" <<'EOF'
#!/usr/bin/env bash
runs="$(dirname "$0")/test-flaky.sh.runs"
echo run >> "$runs"
if [ "$(grep -c . "$runs")" -le 1 ]; then
  echo "FAIL: flaky — lost the race this time"
  exit 1
fi
echo "PASS: flaky — won the race this time"
EOF
  chmod +x "$w/hooks/test/test-good.sh" "$w/hooks/test/test-broken.sh" \
           "$w/hooks/test/test-flaky.sh"
}

# fresh_repo <name> — a real git repo (so state resolves via git-common-dir the
# way production does) seeded with the tier above.
fresh_repo() {
  local w="$TMPROOT/$1"
  mkdir -p "$w"
  git init -q -b "$BASE_BR" "$w"
  git -C "$w" config user.email "t@sable.invalid"
  git -C "$w" config user.name "SABLE Test"
  seed_snapshot_repo "$w"
  git -C "$w" add -A
  git -C "$w" commit -q -m init
  echo "$w"
}

# snap <repo> [args...] — run the snapshot runner. SABLE_SNAPSHOT_BD=true keeps
# bead filing inert for the state-focused cases (C9 supplies a real bd).
snap() {
  local w="$1"; shift
  env SABLE_SNAPSHOT_BD="${SABLE_SNAPSHOT_BD:-true}" \
      HOME="${BD_HOME:-$HOME}" \
      python3 "$SNAPSHOT" "$@" --repo "$w" 2>&1
}

state_dir_of() { echo "$1/.claude/sable/state/merge-gate"; }
runs_of() { grep -c . "$1/hooks/test/$2.runs" 2>/dev/null || echo 0; }

# ---------------------------------------------------------------------------
# C1/C2 — deterministic red freezes
# ---------------------------------------------------------------------------
W="$(fresh_repo det)"
# Quarantine the flaky suite up front so this case isolates the deterministic
# one (the mixed case is C4's business).
snap "$W" quarantine add test-flaky.sh >/dev/null
OUT="$(snap "$W" run)"; RC=$?
SD="$(state_dir_of "$W")"

if [ "$RC" -eq 25 ]; then
  pass "C1 deterministic red (failed twice) exits 25 — FROZEN"
else
  fail "C1 deterministic red exits 25" "rc=$RC
$OUT"
fi

if [ -f "$SD/freeze.json" ]; then
  pass "C1 freeze flag persisted at the git-common-dir-resolved state dir"
else
  fail "C1 freeze flag persisted" "no $SD/freeze.json
$OUT"
fi

if grep -q 'test-broken.sh' "$SD/freeze.json" 2>/dev/null; then
  pass "C2 the freeze record names the deterministically-red suite"
else
  fail "C2 freeze record names the suite" "$(cat "$SD/freeze.json" 2>&1)"
fi

STATUS="$(snap "$W" status)"
if echo "$STATUS" | grep -q "FROZEN"; then
  pass "C2 'sable-snapshot status' reports the freeze to an operator"
else
  fail "C2 status reports the freeze" "$STATUS"
fi

# The re-run really happened, and ONLY on the failed suite: test-good.sh ran
# once, test-broken.sh twice. This is what makes it a classifier rather than a
# blanket retry.
if [ "$(runs_of "$W" test-good.sh)" = "1" ] && [ "$(runs_of "$W" test-broken.sh)" = "2" ]; then
  pass "C1 the automatic re-run covered ONLY the failed suite (good=1, broken=2)"
else
  fail "C1 re-run is scoped to the failures" \
       "good=$(runs_of "$W" test-good.sh) broken=$(runs_of "$W" test-broken.sh)"
fi

# ---------------------------------------------------------------------------
# C3 — a real flake quarantines and does NOT freeze
# ---------------------------------------------------------------------------
W2="$(fresh_repo flake)"
# Remove the deterministic breaker so the ONLY red in this repo is the flake.
rm -f "$W2/hooks/test/test-broken.sh"
cat > "$W2/hooks/test/test-broken.sh" <<'EOF'
#!/usr/bin/env bash
echo run >> "$(dirname "$0")/test-broken.sh.runs"
echo "PASS: no longer broken"
EOF
chmod +x "$W2/hooks/test/test-broken.sh"

OUT2="$(snap "$W2" run)"; RC2=$?
SD2="$(state_dir_of "$W2")"

if [ "$RC2" -eq 26 ]; then
  pass "C3 a real red-then-green flake exits 26 — classified, not frozen"
else
  fail "C3 flake exits 26" "rc=$RC2
$OUT2"
fi

if [ ! -f "$SD2/freeze.json" ]; then
  pass "C3 a flake does NOT freeze the fleet (no freeze flag written)"
else
  fail "C3 flake must not freeze" "$(cat "$SD2/freeze.json")"
fi

if grep -q 'test-flaky.sh' "$SD2/quarantine.json" 2>/dev/null; then
  pass "C3 the flaky suite is quarantined"
else
  fail "C3 flaky suite quarantined" "$(cat "$SD2/quarantine.json" 2>&1)"
fi

# ---------------------------------------------------------------------------
# C4 — quarantine is an exclusion from the trigger, NOT a skip
# ---------------------------------------------------------------------------
W3="$(fresh_repo quarantined)"
# Make the flaky suite permanently red, then quarantine it. It now fails twice
# — the exact shape that freezes when NOT quarantined (C1).
cat > "$W3/hooks/test/test-flaky.sh" <<'EOF'
#!/usr/bin/env bash
echo run >> "$(dirname "$0")/test-flaky.sh.runs"
echo "FAIL: permanently red while quarantined"
exit 1
EOF
chmod +x "$W3/hooks/test/test-flaky.sh"
cat > "$W3/hooks/test/test-broken.sh" <<'EOF'
#!/usr/bin/env bash
echo run >> "$(dirname "$0")/test-broken.sh.runs"
echo "PASS: fine"
EOF
chmod +x "$W3/hooks/test/test-broken.sh"
snap "$W3" quarantine add test-flaky.sh >/dev/null

OUT3="$(snap "$W3" run)"; RC3=$?
SD3="$(state_dir_of "$W3")"

if [ ! -f "$SD3/freeze.json" ]; then
  pass "C4 a quarantined suite failing TWICE does not freeze the fleet"
else
  fail "C4 quarantined suite must not freeze" "$(cat "$SD3/freeze.json")"
fi

# The non-skip half, measured from the suite's OWN side effect: it ran on the
# first pass AND on the re-run. A quarantine that skipped would show 0.
if [ "$(runs_of "$W3" test-flaky.sh)" -ge 2 ]; then
  pass "C4 the quarantined suite STILL RAN (its own side effect proves it, runs=$(runs_of "$W3" test-flaky.sh))"
else
  fail "C4 quarantined suite still runs" "runs=$(runs_of "$W3" test-flaky.sh)"
fi

if echo "$OUT3" | grep -q 'test-flaky.sh'; then
  pass "C4 the quarantined suite's red is still RECORDED in the verdict"
else
  fail "C4 quarantined red is recorded" "$OUT3"
fi

if [ "$RC3" -eq 0 ]; then
  pass "C4 a run whose only red is quarantined exits 0"
else
  fail "C4 quarantined-only red exits 0" "rc=$RC3
$OUT3"
fi

# ---------------------------------------------------------------------------
# C5/C6/C7 — the MECHANICAL promote refusal, against a real origin + clone
# ---------------------------------------------------------------------------
G_ORIGIN="$TMPROOT/gate-origin.git"
G_WORK="$TMPROOT/gate-work"
git init -q --bare -b "$BASE_BR" "$G_ORIGIN"
git clone -q "$G_ORIGIN" "$G_WORK" 2>/dev/null
git -C "$G_WORK" config user.email "t@sable.invalid"
git -C "$G_WORK" config user.name "SABLE Test"
echo base > "$G_WORK/f.txt"
git -C "$G_WORK" add -A
git -C "$G_WORK" commit -q -m init
git -C "$G_WORK" push -q origin "$BASE_BR"
git -C "$G_WORK" checkout -q -b wk-1
echo worker >> "$G_WORK/f.txt"
git -C "$G_WORK" commit -qam "worker change"
git -C "$G_WORK" push -q origin wk-1

# A fake gh reporting the ci-verify ref's real tip as a successful run — the
# same seam every other merge-gate suite uses.
FAKE_GH="$TMPROOT/fake-gh"
cat > "$FAKE_GH" <<'EOF'
#!/usr/bin/env python3
import json, os, subprocess, sys
od = os.environ["FAKE_GH_ORIGIN"]
a = sys.argv[1:]
ref = a[a.index("--branch") + 1]
sha = subprocess.run(["git", "--git-dir=" + od, "rev-parse", "refs/heads/" + ref],
                     text=True, capture_output=True).stdout.strip()
print(json.dumps([{"databaseId": 1, "headSha": sha, "status": "completed",
                   "conclusion": "success", "url": "http://fake/run/1"}]))
EOF
chmod +x "$FAKE_GH"

gate() {
  env FAKE_GH_ORIGIN="$G_ORIGIN" SABLE_MG_GH="$FAKE_GH" \
      SABLE_MG_BD=true SABLE_MG_NOTIFY=true \
      SABLE_MG_POLL=0 SABLE_MG_GRACE=0 SABLE_MG_TIMEOUT=0 \
      python3 "$GATE" "$@" 2>&1
}
base_tip() { git --git-dir="$G_ORIGIN" rev-parse "refs/heads/$BASE_BR"; }

# Freeze the gate repo through the real CLI (real state file, real path).
snap "$G_WORK" freeze --reason "rehearsal freeze" >/dev/null
TIP_BEFORE="$(base_tip)"
POUT="$(gate promote --bead SABLE-test --branch wk-1 --base "$BASE_BR" \
        --repo "$G_WORK" --remote origin --manager chuck)"; PRC=$?

if [ "$PRC" -eq 25 ]; then
  pass "C5 promote is DENIED with exit 25 while the fleet is frozen"
else
  fail "C5 frozen promote exits 25" "rc=$PRC
$POUT"
fi

if [ "$(base_tip)" = "$TIP_BEFORE" ]; then
  pass "C5 the integration branch tip did not move under a frozen promote"
else
  fail "C5 frozen promote moved the base" "$TIP_BEFORE -> $(base_tip)"
fi

# C7 fail-closed, BEFORE the unfreeze: a freeze file that cannot be parsed must
# deny, not sail through.
printf '{corrupt' > "$(state_dir_of "$G_WORK")/freeze.json"
CRC_OUT="$(gate promote --bead SABLE-test --branch wk-1 --base "$BASE_BR" \
           --repo "$G_WORK" --remote origin --manager chuck)"; CRC=$?
if [ "$CRC" -eq 25 ]; then
  pass "C7 an UNREADABLE freeze file denies the promote (fail-closed)"
else
  fail "C7 corrupt freeze state fails closed" "rc=$CRC
$CRC_OUT"
fi

# C6 non-vacuity: the SAME promote, once unfrozen, actually promotes.
snap "$G_WORK" unfreeze --reason "rehearsal over" >/dev/null
GOUT="$(gate promote --bead SABLE-test --branch wk-1 --base "$BASE_BR" \
        --repo "$G_WORK" --remote origin --manager chuck)"; GRC=$?
if [ "$GRC" -eq 0 ] && [ "$(base_tip)" != "$TIP_BEFORE" ]; then
  pass "C6 the SAME promote succeeds once unfrozen — C5's refusal was the freeze, not a broken gate"
else
  fail "C6 unfrozen promote succeeds" "rc=$GRC tip=$(base_tip) (was $TIP_BEFORE)
$GOUT"
fi

# ---------------------------------------------------------------------------
# C8 — a green snapshot clears an existing freeze
# ---------------------------------------------------------------------------
W4="$(fresh_repo unfreeze)"
cat > "$W4/hooks/test/test-broken.sh" <<'EOF'
#!/usr/bin/env bash
echo run >> "$(dirname "$0")/test-broken.sh.runs"
echo "PASS: fixed"
EOF
cat > "$W4/hooks/test/test-flaky.sh" <<'EOF'
#!/usr/bin/env bash
echo run >> "$(dirname "$0")/test-flaky.sh.runs"
echo "PASS: steady"
EOF
chmod +x "$W4/hooks/test/test-broken.sh" "$W4/hooks/test/test-flaky.sh"
snap "$W4" freeze --reason "stale freeze from an earlier break" >/dev/null
[ -f "$(state_dir_of "$W4")/freeze.json" ] || fail "C8 setup: freeze not written"

OUT4="$(snap "$W4" run)"; RC4=$?
if [ "$RC4" -eq 0 ] && [ ! -f "$(state_dir_of "$W4")/freeze.json" ]; then
  pass "C8 a green snapshot CLEARS the freeze — promotion opens again automatically"
else
  fail "C8 green snapshot clears the freeze" "rc=$RC4 freeze still: $(ls "$(state_dir_of "$W4")" 2>&1)
$OUT4"
fi

# ---------------------------------------------------------------------------
# C9 — exactly one bead, with a REAL sandboxed bd
# ---------------------------------------------------------------------------
if ! command -v bd >/dev/null 2>&1; then
  skip "C9 idempotent bead filing — no bd on PATH (SABLE-59zu clean-room has none by design)"
else
  BD_HOME="$TMPROOT/bd-home"
  mkdir -p "$BD_HOME"
  W5="$(fresh_repo beads)"

  # bd runs with the SANDBOX repo as its CWD and a THROWAWAY HOME, so it can
  # never see or write the real bead pool. The cd is guarded (SABLE-0ssz.2: an
  # unguarded cd that fails leaves the command running in the real worktree).
  bd_in_sandbox() {
    ( cd "$W5" || exit 97
      env HOME="$BD_HOME" BD_NON_INTERACTIVE=1 CI=true bd "$@" )
  }
  # `bd init` on the embedded-Dolt backend can leave a PARTIAL database on a
  # first-run race (rc 0, no config.yaml) — gate on the artifact, retry the
  # half-init, same as bin/test_sable_reconcile_handoffs_integration.py.
  BD_INIT_OK=0
  for _ in 1 2 3 4; do
    rm -rf "$W5/.beads"
    bd_in_sandbox init --non-interactive >/dev/null 2>&1
    if [ -f "$W5/.beads/config.yaml" ]; then BD_INIT_OK=1; break; fi
  done

  if [ "$BD_INIT_OK" -ne 1 ]; then
    skip "C9 idempotent bead filing — bd init never produced a clean sandbox DB"
  else
    bd_count() {
      bd_in_sandbox list --status=open "--label=$1" --json 2>/dev/null \
        | python3 -c 'import json,sys
try: print(len(json.load(sys.stdin)))
except Exception: print(0)'
    }
    export BD_HOME
    SABLE_SNAPSHOT_BD="bd" snap "$W5" run >/dev/null
    # Run it AGAIN: the same break, the same flake, a second snapshot. Nothing
    # carries over between the two runs except the OPEN BEAD POOL — which is
    # the whole point: the dedup is a query, not a remembered flag.
    rm -f "$W5/hooks/test/"*.runs
    cat > "$W5/hooks/test/test-flaky.sh" <<'EOF'
#!/usr/bin/env bash
runs="$(dirname "$0")/test-flaky.sh.runs"
echo run >> "$runs"
if [ "$(grep -c . "$runs")" -le 1 ]; then
  echo "FAIL: flaky again"; exit 1
fi
echo "PASS: flaky recovered again"
EOF
    chmod +x "$W5/hooks/test/test-flaky.sh"
    SABLE_SNAPSHOT_BD="bd" snap "$W5" run >/dev/null

    N_BISECT="$(bd_count snapshot-freeze)"
    N_FLAKE="$(bd_count snapshot-flake)"

    if [ "${N_BISECT:-0}" = "1" ]; then
      pass "C9 two freezing snapshots filed EXACTLY ONE bisect bead (idempotent by key, not by memory)"
    else
      fail "C9 exactly one bisect bead" "count=${N_BISECT:-unknown}"
    fi
    if [ "${N_FLAKE:-0}" = "1" ]; then
      pass "C9 two flaking snapshots filed EXACTLY ONE flaky-fix bead"
    else
      fail "C9 exactly one flaky-fix bead" "count=${N_FLAKE:-unknown}"
    fi
    unset BD_HOME
  fi
fi

# ---------------------------------------------------------------------------
# C10 — the shipped state dir: README tracked, state files ignored
# ---------------------------------------------------------------------------
REAL_SD=".claude/sable/state/merge-gate"
if [ -d "$REPO_ROOT/$REAL_SD" ] && [ -f "$REPO_ROOT/$REAL_SD/README.md" ]; then
  pass "C10 the state dir exists in the checkout and documents the contract"
else
  fail "C10 state dir is shipped and documented" "missing $REPO_ROOT/$REAL_SD/README.md"
fi

# Both directions of the .gitignore exception, asked of real git.
ignored() { git -C "$REPO_ROOT" check-ignore -q "$1"; }
if ! ignored "$REAL_SD/README.md"; then
  pass "C10 the README is TRACKABLE — the location is part of the contract"
else
  fail "C10 README must not be ignored" "git check-ignore matched it"
fi
if ignored "$REAL_SD/freeze.json" && ignored "$REAL_SD/quarantine.json"; then
  pass "C10 the state FILES are ignored — a freeze never travels through a merge"
else
  fail "C10 state files must be ignored" "freeze.json/quarantine.json are trackable"
fi
# The re-ignore lines are load-bearing: un-excluding .claude/sable/ must not
# have un-excluded mode-state.json or the installed cockpit artifacts.
if ignored ".claude/sable/state/mode-state.json" && ignored ".claude/sable/anything"; then
  pass "C10 the negation did not leak — mode-state and cockpit artifacts stay ignored"
else
  fail "C10 gitignore negation leaked" "something under .claude/sable/ became trackable"
fi

# ---------------------------------------------------------------------------
# C11 — the suite's own hermeticity, asserted rather than assumed
# ---------------------------------------------------------------------------
# Two repos must resolve to two DIFFERENT state dirs. Under an ambient
# SABLE_MERGE_GATE_STATE they resolve to the same one, every case above shares a
# freeze, and the suite reports on the developer's shell instead of the code.
if [ "$(state_dir_of "$W")" != "$(state_dir_of "$W2")" ] \
   && [ -z "${SABLE_MERGE_GATE_STATE:-}" ] \
   && [ "$(env SABLE_MERGE_GATE_STATE=/nonexistent python3 -c \
            'import sys; sys.path.insert(0, sys.argv[1]); import sable_snapshot_lib as s; print(s.state_dir("."))' \
            "$REPO_ROOT/bin")" = "/nonexistent" ]; then
  pass "C11 state dirs are per-repo here, and the override seam still works when set deliberately"
else
  fail "C11 suite hermeticity" "ambient SABLE_MERGE_GATE_STATE=${SABLE_MERGE_GATE_STATE:-<unset>}"
fi

echo
echo "test-snapshot-freeze.sh: $PASS passed, $FAIL failed, $SKIP skipped"
[ "$FAIL" -eq 0 ] || exit 1
