#!/usr/bin/env bash
# test-event-pair.sh — the S5 locked event-pair contract, hook leg + poll leg
# (SABLE-jd5fj.1 / SABLE-jd5fj.2)
#
# WHAT IS UNDER TEST
# ------------------
# Two independent producers of the SAME `sable-merge-gate preview` kick:
#
#   HOOK leg (jd5fj.1) — post-push-merge-notify.sh fires the kick on a
#   confirmed worker push, in the background, immediately.
#
#   POLL leg (jd5fj.2) — sable-reconcile-handoffs' preview-kick pass fires the
#   SAME kick for any origin `wk-*` branch that is genuinely unmerged and past
#   the settle window, REGARDLESS of whether the hook ever fired (unwired,
#   raced, or crashed mid-flight).
#
# Both legs call the identical `sable-merge-gate preview` entrypoint, which is
# idempotent on the SHARED preview_kick_ref key (a pure function of the
# (base_sha, branch_sha) pair) — this suite proves that sharing holds with REAL
# git and the REAL gate, not a stub, in both race orders AND under a genuine
# concurrent race (PART B's adversarial case), because right now — mid pull
# freeze, hook leg dark in the field — the poll leg is the SOLE kick path and a
# key-derivation bug would stay invisible until both legs fire together.
#
# PART A — hook wiring dark (the exact production condition right now): NO
#          hook ever fires; the poll leg alone kicks the missed preview within
#          one reconcile invocation.
# PART B — both legs fire for the SAME push: hook-then-poll, poll-then-hook,
#          and a genuine unsynchronized concurrent race. Every case: EXACTLY
#          ONE ci-verify ref lands, whichever leg wins.
# PART C — REGRESSION: the pre-existing 4-part stranded-handoff predicate
#          (P1..P4, classify_branch) is unchanged by the new preview-kick pass
#          living in the SAME reconcile() loop — merged / open-work-bead /
#          handoff-already-filed / too-fresh / true-positive cases still
#          classify identically, using a real sandboxed `bd` stub (predicates
#          2/3 need bead data; the preview-kick leg never touches bd at all).
#
# Run with:
#   bash hooks/test/test-event-pair.sh
#
# Clean-room safe (SABLE-59zu): needs only bash + git + python3. bd / gh /
# sable-msg / tmux are stubbed; nothing here touches a real remote.

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$TESTDIR/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/multi-manager/post-push-merge-notify.sh"
GATE="$REPO_ROOT/bin/sable-merge-gate"
RECONCILE="$REPO_ROOT/bin/sable-reconcile-handoffs"

# shellcheck source=lib-git-sandbox.sh
source "$TESTDIR/lib-git-sandbox.sh"

PASS=0
FAIL=0
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

for f in "$HOOK" "$GATE" "$RECONCILE"; do
  [ -f "$f" ] || { echo "FATAL: missing $f"; exit 2; }
done

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"; sable_test_git_sandbox_cleanup' EXIT

STUB_DIR="$TMPROOT/stubs"
mkdir -p "$STUB_DIR"

# Configurable `bd` stub (PART C drives its responses via env vars; PARTS A/B
# never inspect bd's output — the preview-kick leg never calls bd at all).
cat > "$STUB_DIR/bd" <<'PYEOF'
#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
log = os.environ.get("BD_CALL_LOG")
if log:
    with open(log, "a") as f:
        f.write(" ".join(args) + "\n")
sub = args[0] if args else ""
if sub == "show":
    status = os.environ.get("STUB_BD_SHOW_STATUS", "")
    if status:
        print(json.dumps([{"id": "X", "status": status}])); sys.exit(0)
    sys.exit(1)
if sub == "search":
    status = os.environ.get("STUB_BD_SEARCH_STATUS", "")
    print(json.dumps([{"id": "X", "status": status}] if status else []))
    sys.exit(0)
if sub == "list":
    # Two DIFFERENT `bd list` call shapes reach this stub:
    #   --metadata-field branch=<b>  -> the work-bead resolver (status + HOLD
    #                                   metadata, SABLE-i5739 / SABLE-jejx3)
    #   --label for-chuck            -> predicate 3's suppression corpus
    # Before SABLE-jejx3 only the second existed here, so a single response
    # sufficed; conflating them now would feed the hold reader the for-chuck
    # corpus and silently make every branch read as unheld.
    if any(a.startswith("--metadata-field") or a.startswith("branch=") for a in args):
        rc = os.environ.get("STUB_BD_WORKBEAD_RC", "0")
        if rc != "0":
            print("simulated bd failure"); sys.exit(int(rc))
        print(os.environ.get("STUB_BD_WORKBEAD_JSON", "[]"))
        sys.exit(0)
    print(os.environ.get("STUB_BD_FORCHUCK_JSON", "[]"))
    sys.exit(0)
if sub == "create":
    sys.exit(0)
sys.exit(0)
PYEOF
cat > "$STUB_DIR/gh" <<'EOF'
#!/usr/bin/env bash
# SABLE-xw32f: configurable so PART E's QUEUED-AT-SEAT cases can report an
# in-flight or terminal Actions run via STUB_GH_RUN_LIST_JSON. Every OTHER
# test in this suite relies on the pre-existing default below (exit 1 —
# "gh unavailable", which ref_has_inflight_run fails open on to
# not-in-flight) and stays unaffected unless that var is set.
if [ "${1:-}" = "run" ] && [ "${2:-}" = "list" ] && [ -n "${STUB_GH_RUN_LIST_JSON:-}" ]; then
  echo "$STUB_GH_RUN_LIST_JSON"
  exit 0
fi
exit 1
EOF
cat > "$STUB_DIR/sable-msg" <<'EOF'
#!/usr/bin/env bash
exit "${SABLE_MSG_STUB_RC:-1}"
EOF
cat > "$STUB_DIR/tmux" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  [ "$a" = "list-panes" ] && { echo "chuck"; exit 0; }
  [ "$a" = "display-message" ] && { echo "${SABLE_STUB_PANE_ROLE:-}"; exit 0; }
done
exit 0
EOF
chmod +x "$STUB_DIR/bd" "$STUB_DIR/gh" "$STUB_DIR/sable-msg" "$STUB_DIR/tmux"

# Fixture self-check (chuck/tarzan root-cause, SABLE-vif5e): the `bd` stub's
# `list` subcommand MUST emit valid JSON even when STUB_BD_FORCHUCK_JSON is
# left UNSET (the C5 true-positive case below does exactly this) — bash's
# "${VAR:-}" forwarding in run_reconcile sets the var to an EMPTY STRING
# rather than leaving it unset, so the stub's own `os.environ.get(...,
# "[]")` default never applies and it prints a blank line instead. This
# fixture fed the reconciler that garbage for as long as this line existed;
# nothing caught it until sable-reconcile-handoffs' vif5e fix started
# warning on unparseable JSON instead of silently swallowing it. Guard the
# fixture's own contract here so a future regression fails loud in THIS
# suite, not by way of a product change happening to notice.
STUB_LIST_JSON="$(env -i PATH="$STUB_DIR:$PATH" HOME="$HOME" \
    "$STUB_DIR/bd" list --status open,in_progress --label for-chuck --json)"
if ! python3 -c "import json,sys; json.loads(sys.argv[1])" "$STUB_LIST_JSON" 2>/dev/null; then
  echo "FATAL: bd stub's 'list' output is not valid JSON with STUB_BD_FORCHUCK_JSON unset: '$STUB_LIST_JSON'"
  exit 2
fi

# Real sable-merge-gate on PATH — the hook resolves it via `command -v`, and
# the whole point of this suite is exercising the REAL preview_kick_ref
# derivation, not a recorder stand-in (that coverage already lives in
# test-preview-kick.sh PART A).
BIN_SHIM="$TMPROOT/bin-shim"
mkdir -p "$BIN_SHIM"
ln -s "$GATE" "$BIN_SHIM/sable-merge-gate"

# An old-enough committer date that any reasonable --age-min is already past.
OLD_DATE="2001-01-01T00:00:00 +0000"

make_post_input() {
  python3 -c "
import json, sys
cmd, cwd = sys.argv[1:3]
print(json.dumps({'tool_input': {'command': cmd}, 'cwd': cwd,
                  'tool_response': {'stdout': '', 'stderr': ''}}))
" "$1" "$2"
}

# fresh_pair <label> → ORIGIN/WORK: bare origin (tmux-only) + a work clone with
# one worker branch `wk-<label>` carrying a backdated commit (already past any
# settle window).
fresh_pair() {
  local label="$1"
  ORIGIN="$TMPROOT/$label-origin.git"
  WORK="$TMPROOT/$label-work"
  git init -q --bare -b tmux-only "$ORIGIN"
  git clone -q "$ORIGIN" "$WORK" 2>/dev/null
  git -C "$WORK" config user.email "t@sable.invalid"
  git -C "$WORK" config user.name  "SABLE Test"
  git -C "$WORK" config sable.integrationBranch tmux-only
  echo base > "$WORK/base.txt"
  git -C "$WORK" add base.txt
  git -C "$WORK" commit -q -m base
  git -C "$WORK" push -q origin tmux-only

  BRANCH="wk-$label"
  git -C "$WORK" checkout -q -b "$BRANCH" tmux-only
  echo work > "$WORK/work.txt"
  git -C "$WORK" add work.txt
  GIT_COMMITTER_DATE="$OLD_DATE" GIT_AUTHOR_DATE="$OLD_DATE" \
    git -C "$WORK" commit -q -m "worker change"
  git -C "$WORK" push -q origin "$BRANCH"
  git -C "$WORK" checkout -q tmux-only
}

ci_refs() { git --git-dir="$ORIGIN" for-each-ref --format='%(refname:short)' refs/heads/ci-verify/; }
ci_ref_count() { ci_refs | grep -c .; }

# run_hook <branch> → fires the REAL hook on a confirmed push of <branch>; the
# preview kick it starts is detached (setsid/nohup), so this returns almost
# immediately, before the kick necessarily lands.
run_hook() {
  local branch="$1"
  git -C "$WORK" checkout -q "$branch"
  local json
  json="$(make_post_input "git push" "$WORK")"
  env -i PATH="$BIN_SHIM:$STUB_DIR:$PATH" HOME="$HOME" \
      SABLE_HOOK_TRACE_LOG="$TMPROOT/$branch-hook-trace.log" \
      SABLE_PREVIEW_KICK_LOG="$TMPROOT/$branch-kick.log" \
      CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager \
      timeout 30 bash "$HOOK" <<< "$json" >"$TMPROOT/$branch-hook.out" 2>&1
  git -C "$WORK" checkout -q tmux-only
}

# run_reconcile → runs the REAL poll leg once against $WORK. Stub-control vars
# (BD_CALL_LOG, STUB_BD_*, RECONCILE_AGE_MIN), when set as a prefix on the
# CALL to this function, are read from the calling shell and explicitly
# forwarded into the `env -i` child below (which otherwise wipes them).
run_reconcile() {
  local age_min="${RECONCILE_AGE_MIN:-0}"
  env -i PATH="$STUB_DIR:$PATH" HOME="$HOME" SABLE_RC_BD="$STUB_DIR/bd" \
      BD_CALL_LOG="${BD_CALL_LOG:-}" \
      STUB_BD_SHOW_STATUS="${STUB_BD_SHOW_STATUS:-}" \
      STUB_BD_SEARCH_STATUS="${STUB_BD_SEARCH_STATUS:-}" \
      STUB_BD_FORCHUCK_JSON="${STUB_BD_FORCHUCK_JSON:-[]}" \
      STUB_BD_WORKBEAD_JSON="${STUB_BD_WORKBEAD_JSON:-[]}" \
      STUB_BD_WORKBEAD_RC="${STUB_BD_WORKBEAD_RC:-0}" \
      SABLE_HOLD_STALE_DAYS="${SABLE_HOLD_STALE_DAYS:-3}" \
      STUB_GH_RUN_LIST_JSON="${STUB_GH_RUN_LIST_JSON:-}" \
      SABLE_QUEUED_STALE_MIN="${SABLE_QUEUED_STALE_MIN:-120}" \
      python3 "$RECONCILE" --repo "$WORK" --remote origin --age-min "$age_min"
}

await_ref_count() {
  local want="$1" i
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    [ "$(ci_ref_count)" -ge "$want" ] && break
    sleep 0.2
  done
  sleep 0.4
  ci_ref_count
}

# ==========================================================================
# PART A — hook wiring dark; the poll leg alone kicks the missed preview
# ==========================================================================

fresh_pair a-dark

if [ "$(ci_ref_count)" -eq 0 ]; then
  pass "A0: no ci-verify ref exists before either leg runs"
else
  fail "A0: no ci-verify ref exists before either leg runs" "refs=[$(ci_refs)]"
fi

OUT="$(run_reconcile)"; RC=$?
N="$(ci_ref_count)"
if [ "$RC" -eq 0 ] && [ "$N" -eq 1 ]; then
  pass "A1: poll leg kicks a missed preview within ONE invocation (hook never ran)"
else
  fail "A1: poll leg kicks a missed preview within ONE invocation" "rc=$RC refs=[$(ci_refs)] out=$OUT"
fi
if printf '%s\n' "$(ci_refs)" | grep -q "^ci-verify/wk-a-dark-"; then
  pass "A1: the kicked ref names the branch (ci-verify/<branch>-<key>)"
else
  fail "A1: the kicked ref names the branch" "refs=[$(ci_refs)]"
fi

# ==========================================================================
# PART B — both legs fire for the same push: exactly one preview, every order
# ==========================================================================

# (B1) hook first, poll second (sequential, deterministic ordering)
fresh_pair b-hook-first
run_hook "$BRANCH" >/dev/null
N_HOOK="$(await_ref_count 1)"
if [ "$N_HOOK" -eq 1 ]; then
  pass "B1: hook leg kicks (precondition for the ordering test)"
else
  fail "B1: hook leg kicks (precondition)" "refs=[$(ci_refs)]"
fi
BEFORE="$(ci_refs)"
OUT="$(run_reconcile)"; RC=$?
if [ "$RC" -eq 0 ] && [ "$(ci_refs)" = "$BEFORE" ] && [ "$(ci_ref_count)" -eq 1 ]; then
  pass "B1: poll leg, arriving AFTER the hook, does not double-kick"
else
  fail "B1: poll leg does not double-kick after the hook" "before=[$BEFORE] after=[$(ci_refs)] out=$OUT"
fi

# (B2) poll first, hook second (the reverse order)
fresh_pair b-poll-first
OUT1="$(run_reconcile)"; RC1=$?
N_POLL="$(ci_ref_count)"
if [ "$RC1" -eq 0 ] && [ "$N_POLL" -eq 1 ]; then
  pass "B2: poll leg kicks (precondition for the reverse-ordering test)"
else
  fail "B2: poll leg kicks (precondition)" "refs=[$(ci_refs)] out=$OUT1"
fi
BEFORE="$(ci_refs)"
run_hook "$BRANCH" >/dev/null
N_AFTER="$(await_ref_count 1)"
if [ "$N_AFTER" -eq 1 ] && [ "$(ci_refs)" = "$BEFORE" ]; then
  pass "B2: hook leg, arriving AFTER the poll, does not double-kick"
else
  fail "B2: hook leg does not double-kick after the poll" "before=[$BEFORE] after=[$(ci_refs)]"
fi

# (B3) THE ADVERSARIAL CASE — genuinely unsynchronized: both legs fired for the
# same push with NO ordering guarantee between them (this is the shape that
# matters most right now: the hook leg is dark in the field under the pull
# freeze, so whenever it DOES fire again it will race an already-running poll
# leg with zero coordination). Exactly one ci-verify ref must land regardless
# of which leg's fetch/build/push interleaves first.
fresh_pair b-race
(
  run_hook "$BRANCH" >/dev/null
) &
HOOK_BG=$!
(
  run_reconcile >/dev/null
) &
POLL_BG=$!
wait "$HOOK_BG"
wait "$POLL_BG"
N_RACE="$(await_ref_count 1)"
if [ "$N_RACE" -eq 1 ]; then
  pass "B3 ADVERSARIAL: hook and poll firing concurrently for the same push land EXACTLY ONE preview"
else
  fail "B3 ADVERSARIAL: exactly one preview under a genuine concurrent race" "refs=[$(ci_refs)] count=$N_RACE"
fi

# ==========================================================================
# PART C — REGRESSION: the 4-part stranded predicate is unchanged by the new
# preview-kick pass sharing its reconcile() loop. Real git; a configurable bd
# stub stands in for predicates 2/3 (bead status, for-chuck corpus) — the
# preview-kick leg never touches bd, so these are pure classify_branch checks
# run alongside a kick attempt, proving the two legs don't interfere.
# ==========================================================================

# (C1) already-merged branch: not stranded, and NOT a preview-kick candidate
# either (P1 gates both legs identically).
fresh_pair c-merged
git -C "$WORK" checkout -q tmux-only
git -C "$WORK" merge -q --ff-only "$BRANCH" 2>/dev/null || git -C "$WORK" merge -q "$BRANCH" -m merge
git -C "$WORK" push -q origin tmux-only
CALLLOG="$TMPROOT/c-merged-bdcalls.log"
OUT="$(BD_CALL_LOG="$CALLLOG" STUB_BD_SEARCH_STATUS=closed run_reconcile)"; RC=$?
if [ "$RC" -eq 0 ] && [ "$(ci_ref_count)" -eq 0 ] && ! grep -q "^create " "$CALLLOG" 2>/dev/null; then
  pass "C1 REGRESSION: an already-merged branch is neither stranded nor preview-kicked"
else
  fail "C1 REGRESSION: merged branch skipped by both legs" "refs=[$(ci_refs)] out=$OUT"
fi

# (C2) unmerged + OPEN work bead (P2 fails) → NOT stranded (no for-chuck bead
# filed), but the preview-kick leg fires anyway — it does not consult P2/P3.
fresh_pair c-open-bead
CALLLOG="$TMPROOT/c-open-bdcalls.log"
OUT="$(BD_CALL_LOG="$CALLLOG" STUB_BD_SEARCH_STATUS=open run_reconcile)"; RC=$?
if [ "$RC" -eq 0 ] && ! grep -q "^create " "$CALLLOG" 2>/dev/null; then
  pass "C2 REGRESSION: an open (not-yet-done) work bead still blocks the stranded-handoff bead"
else
  fail "C2 REGRESSION: open work bead blocks stranded-handoff filing" "out=$OUT"
fi
if [ "$(ci_ref_count)" -eq 1 ]; then
  pass "C2: preview-kick STILL fires for that same branch (P2 is not part of its predicate)"
else
  fail "C2: preview-kick fires despite the open work bead" "refs=[$(ci_refs)]"
fi

# (C3) unmerged + closed work bead + a for-chuck handoff ALREADY on record
# (P3 fails) → not stranded (no duplicate bead), but STILL preview-kicked.
fresh_pair c-handoff-filed
FORCHUCK_JSON="[{\"title\": \"[AUTO-NOTIFY] Review PR from optimus: $BRANCH\", \"labels\": [\"for-chuck\"]}]"
CALLLOG="$TMPROOT/c-handoff-bdcalls.log"
OUT="$(BD_CALL_LOG="$CALLLOG" STUB_BD_SEARCH_STATUS=closed STUB_BD_FORCHUCK_JSON="$FORCHUCK_JSON" run_reconcile)"; RC=$?
if [ "$RC" -eq 0 ] && ! grep -q "^create " "$CALLLOG" 2>/dev/null; then
  pass "C3 REGRESSION: an already-filed for-chuck handoff still suppresses a duplicate bead"
else
  fail "C3 REGRESSION: existing handoff suppresses duplicate filing" "out=$OUT"
fi
if [ "$(ci_ref_count)" -eq 1 ]; then
  pass "C3: preview-kick STILL fires despite the handoff already being on record"
else
  fail "C3: preview-kick fires despite the existing handoff" "refs=[$(ci_refs)]"
fi

# (C4) too-fresh push (P4 fails on BOTH legs — they share the age check):
# neither stranded nor preview-kicked.
ORIGIN="$TMPROOT/c-fresh-origin.git"
WORK="$TMPROOT/c-fresh-work"
git init -q --bare -b tmux-only "$ORIGIN"
git clone -q "$ORIGIN" "$WORK" 2>/dev/null
git -C "$WORK" config user.email "t@sable.invalid"
git -C "$WORK" config user.name  "SABLE Test"
git -C "$WORK" config sable.integrationBranch tmux-only
echo base > "$WORK/base.txt"; git -C "$WORK" add base.txt; git -C "$WORK" commit -q -m base
git -C "$WORK" push -q origin tmux-only
git -C "$WORK" checkout -q -b wk-c-fresh tmux-only
echo work > "$WORK/work.txt"; git -C "$WORK" add work.txt
git -C "$WORK" commit -q -m "worker change"   # NO backdate -> just pushed, age ~0
git -C "$WORK" push -q origin wk-c-fresh
git -C "$WORK" checkout -q tmux-only
CALLLOG="$TMPROOT/c-fresh-bdcalls.log"
OUT="$(BD_CALL_LOG="$CALLLOG" STUB_BD_SEARCH_STATUS=closed RECONCILE_AGE_MIN=10 run_reconcile)"; RC=$?
if [ "$RC" -eq 0 ] && [ "$(ci_ref_count)" -eq 0 ] && ! grep -q "^create " "$CALLLOG" 2>/dev/null; then
  pass "C4 REGRESSION: a just-pushed (too-fresh) branch is skipped by BOTH legs"
else
  fail "C4 REGRESSION: too-fresh branch skipped by both legs" "refs=[$(ci_refs)] out=$OUT"
fi

# (C5) the true-positive stranded case, WITH the preview-kick leg active: a
# for-chuck bead is filed AND a preview is kicked for the same branch.
fresh_pair c-stranded
CALLLOG="$TMPROOT/c-stranded-bdcalls.log"
OUT="$(BD_CALL_LOG="$CALLLOG" STUB_BD_SEARCH_STATUS=closed run_reconcile)"; RC=$?
if [ "$RC" -eq 0 ] && grep -q "^create " "$CALLLOG" 2>/dev/null; then
  pass "C5 REGRESSION: the true-positive stranded case still files its for-chuck bead"
else
  fail "C5 REGRESSION: true-positive stranded case files a bead" "out=$OUT"
fi
if [ "$(ci_ref_count)" -eq 1 ]; then
  pass "C5: the stranded branch is ALSO preview-kicked (both legs fire together, no conflict)"
else
  fail "C5: stranded branch is also preview-kicked" "refs=[$(ci_refs)]"
fi

# (C6) SABLE-2az2x: the for-chuck corpus query reads back genuinely
# UNREADABLE (unparseable JSON, not merely empty) for what would otherwise be
# the C5 true-positive stranded case. classify_branch correctly assumes-named
# (skip-filing, no duplicate risk) -- but the operator-visible SUMMARY must
# say the corpus could not be assessed, not print the same "0 stranded, 0
# filed" line a genuinely healthy sweep prints. A floor that has silently
# stopped filing must never look identical to one with nothing to file.
fresh_pair c-corpus-unreadable
CALLLOG="$TMPROOT/c-corpus-unreadable-bdcalls.log"
OUT="$(BD_CALL_LOG="$CALLLOG" STUB_BD_SEARCH_STATUS=closed STUB_BD_FORCHUCK_JSON="not-json" run_reconcile)"; RC=$?
if [ "$RC" -eq 0 ] && ! grep -q "^create " "$CALLLOG" 2>/dev/null; then
  pass "C6 SABLE-2az2x: an unreadable for-chuck corpus suppresses filing (assume-named, no duplicate manufactured)"
else
  fail "C6 SABLE-2az2x: unreadable corpus must suppress filing, not crash or duplicate" "rc=$RC out=$OUT"
fi
SUMMARY="$(printf '%s\n' "$OUT" | grep '^sable-reconcile-handoffs:')"
if printf '%s' "$SUMMARY" | grep -q "0 stranded branch(es), 0 for-chuck bead(s) filed" \
   && printf '%s' "$SUMMARY" | grep -q "CORPUS UNREADABLE"; then
  pass "C6 SABLE-2az2x: the summary flags CORPUS UNREADABLE rather than reading as an ordinary clean sweep"
else
  fail "C6 SABLE-2az2x: summary must distinguish could-not-assess from nothing-stranded" "summary=[$SUMMARY]"
fi

# ==========================================================================
# PART D — SABLE-jejx3: HELD is a third outcome, not a suppression.
#
# A branch under an explicit do-not-merge hold satisfies all four stranded
# predicates identically to an accidentally-unmerged one, so the floor filed a
# handoff meaning "nobody merged this — merge it", the EXACT INVERSE of the
# standing instruction, and re-filed it every cadence once Chuck closed it.
#
# Real git, the real reconciler, the real preview-kick leg alongside it. The
# hold lives in the WORK BEAD's metadata (this suite's bd stub serves it, per
# the file's clean-room contract — the real-bd, no-mocks rehearsal of the same
# contract, including the branch-rename case, lives in
# bin/test_sable_reconcile_handoffs_integration.py::test_jejx3_*).
#
# Every case below is paired with its POSITIVE CONTROL: the same sweep without
# the hold marker DOES file, so no assertion here can pass vacuously.
# ==========================================================================

# (D1) held branch: no bead filed, but the sweep still NAMES it with all four
# fields (held / by whom / since when / until what).
fresh_pair d-held
HOLD_JSON="[{\"id\": \"SABLE-work\", \"status\": \"closed\", \"metadata\": {\"branch\": \"$BRANCH\", \"hold\": \"false-negative security regression\", \"hold_by\": \"tarzan\", \"hold_since\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"hold_until\": \"tarzan green-lights a revised tip\"}}]"
CALLLOG="$TMPROOT/d-held-bdcalls.log"
OUT="$(BD_CALL_LOG="$CALLLOG" STUB_BD_WORKBEAD_JSON="$HOLD_JSON" run_reconcile)"; RC=$?
if [ "$RC" -eq 0 ] && ! grep -q "^create " "$CALLLOG" 2>/dev/null; then
  pass "D1 SABLE-jejx3: a HELD branch files NO 'merge me' handoff (the inverted bead is never manufactured)"
else
  fail "D1 SABLE-jejx3: held branch must not file a handoff" "rc=$RC out=$OUT"
fi
if printf '%s' "$OUT" | grep -q "HELD $BRANCH" \
   && printf '%s' "$OUT" | grep -q "by=tarzan" \
   && printf '%s' "$OUT" | grep -q "until=tarzan green-lights a revised tip" \
   && printf '%s' "$OUT" | grep -q "false-negative security regression"; then
  pass "D1 SABLE-jejx3: the held branch is still NAMED every cadence, with by/since/until/reason"
else
  fail "D1 SABLE-jejx3: a held branch must never be silently invisible" "out=$OUT"
fi
SUMMARY="$(printf '%s\n' "$OUT" | grep '^sable-reconcile-handoffs:')"
if printf '%s' "$SUMMARY" | grep -q "1 held branch(es)" \
   && printf '%s' "$SUMMARY" | grep -q "$BRANCH"; then
  pass "D1 SABLE-jejx3: the SUMMARY names the held branch (three outcomes reported, not two)"
else
  fail "D1 SABLE-jejx3: summary must name held branches" "summary=[$SUMMARY]"
fi

# (D1-control) POSITIVE CONTROL — the identical branch and sweep with the hold
# marker REMOVED does file, proving the sweep was capable of filing in this
# configuration and the marker is what stopped it.
UNHELD_JSON="[{\"id\": \"SABLE-work\", \"status\": \"closed\", \"metadata\": {\"branch\": \"$BRANCH\"}}]"
CALLLOG="$TMPROOT/d-control-bdcalls.log"
OUT="$(BD_CALL_LOG="$CALLLOG" STUB_BD_WORKBEAD_JSON="$UNHELD_JSON" run_reconcile)"; RC=$?
if [ "$RC" -eq 0 ] && grep -q "^create " "$CALLLOG" 2>/dev/null; then
  pass "D1 POSITIVE CONTROL: the same branch WITHOUT the hold marker still files (marker does the work)"
else
  fail "D1 POSITIVE CONTROL: unheld branch must still file" "rc=$RC out=$OUT"
fi

# (D2) a STALE / unowned / open-ended hold escalates into the summary — a
# forgotten hold is self-silencing by construction (it suppresses the report
# that would surface it), so it must decay LOUDLY, not quietly.
fresh_pair d-stale
STALE_JSON="[{\"id\": \"SABLE-work\", \"status\": \"closed\", \"metadata\": {\"branch\": \"$BRANCH\", \"hold\": \"reason lost to a pane restart\", \"hold_since\": \"2001-01-01T00:00:00Z\"}}]"
CALLLOG="$TMPROOT/d-stale-bdcalls.log"
OUT="$(BD_CALL_LOG="$CALLLOG" STUB_BD_WORKBEAD_JSON="$STALE_JSON" run_reconcile)"; RC=$?
SUMMARY="$(printf '%s\n' "$OUT" | grep '^sable-reconcile-handoffs:')"
if [ "$RC" -eq 0 ] && ! grep -q "^create " "$CALLLOG" 2>/dev/null \
   && printf '%s' "$OUT" | grep -q "STALE(" \
   && printf '%s' "$OUT" | grep -q "UNOWNED" \
   && printf '%s' "$OUT" | grep -q "NO-RELEASE-CONDITION" \
   && printf '%s' "$SUMMARY" | grep -q "1 NEEDING REVIEW"; then
  pass "D2 SABLE-jejx3: a stale/unowned/open-ended hold is flagged NEEDING REVIEW in the summary"
else
  fail "D2 SABLE-jejx3: an aging hold must escalate, never quietly persist" "rc=$RC summary=[$SUMMARY] out=$OUT"
fi

# (D2-control) a well-formed, fresh hold adds NO review noise, so the flag
# means something when it does appear.
FRESH_HOLD_JSON="[{\"id\": \"SABLE-work\", \"status\": \"closed\", \"metadata\": {\"branch\": \"$BRANCH\", \"hold\": \"rejected tip, revision inbound\", \"hold_by\": \"tarzan\", \"hold_since\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"hold_until\": \"revised tip green\"}}]"
OUT="$(STUB_BD_WORKBEAD_JSON="$FRESH_HOLD_JSON" run_reconcile)"
SUMMARY="$(printf '%s\n' "$OUT" | grep '^sable-reconcile-handoffs:')"
if printf '%s' "$SUMMARY" | grep -q "1 held branch(es)" \
   && ! printf '%s' "$SUMMARY" | grep -q "NEEDING REVIEW"; then
  pass "D2 CONTROL: a fresh, owned, bounded hold adds no review noise"
else
  fail "D2 CONTROL: a healthy hold must not be flagged" "summary=[$SUMMARY]"
fi

# (D3) UNREADABLE hold state is its own reported outcome: the work-bead query
# fails outright, so 'held' can be neither confirmed nor ruled out. The floor
# must NOT file (an inverted handoff against a genuinely held branch is the
# unrecoverable direction) and must NOT look like a clean sweep either
# (SABLE-2az2x's lesson, one surface over).
fresh_pair d-unreadable
CALLLOG="$TMPROOT/d-unreadable-bdcalls.log"
OUT="$(BD_CALL_LOG="$CALLLOG" STUB_BD_WORKBEAD_RC=1 STUB_BD_SEARCH_STATUS="" run_reconcile)"; RC=$?
SUMMARY="$(printf '%s\n' "$OUT" | grep '^sable-reconcile-handoffs:')"
if [ "$RC" -eq 0 ] && ! grep -q "^create " "$CALLLOG" 2>/dev/null \
   && printf '%s' "$OUT" | grep -q "HOLD-STATE UNREADABLE $BRANCH" \
   && printf '%s' "$SUMMARY" | grep -q "UNREADABLE hold state"; then
  pass "D3 SABLE-jejx3: an unreadable hold state suppresses filing AND says so (never a clean-looking sweep)"
else
  fail "D3 SABLE-jejx3: unreadable hold state must be its own reported outcome" "rc=$RC summary=[$SUMMARY] out=$OUT"
fi

# (D4) the preview-kick leg is unchanged by all of the above — it reads P1/P4
# only, so a hold must not silently disable CI warm-up as an unannounced side
# effect (the same structural-independence check as i5739's AC5).
if [ "$(ci_ref_count)" -eq 1 ]; then
  pass "D4 SABLE-jejx3: the preview-kick leg still fires for a branch whose hold state is unreadable"
else
  fail "D4 SABLE-jejx3: preview-kick must stay structurally independent of hold state" "refs=[$(ci_refs)]"
fi

# ==========================================================================
# PART E — SABLE-xw32f: QUEUED AT THE SEAT is a reported, non-stranded
# outcome, not a suppression.
#
# A branch with a ci-verify preview IN FLIGHT satisfies the ordinary
# stranded predicate identically to abandoned work — under burst load, six
# such branches (already in chuck's hands, previews running) would have been
# re-filed as for-chuck handoffs as fast as chuck closed them. This suite
# kicks a REAL preview via the REAL hook (the SAME preview_kick_ref key the
# poll leg's own no-op check already uses — one source of truth, not a
# second one invented for the test), then drives the REAL reconciler with a
# controllable `gh` stub reporting the run in-flight vs. terminal.
# ==========================================================================

# (E1) a branch with a genuinely in-flight preview: no handoff is filed, and
# the sweep NAMES it queued, with a count.
fresh_pair e-queued
E1_BRANCH="$BRANCH"
run_hook "$BRANCH"
if [ "$(await_ref_count 1)" -lt 1 ]; then
  fail "E1 SABLE-xw32f precondition: the hook must have kicked a real ci-verify ref" "refs=[$(ci_refs)]"
else
  pass "E1 precondition: a real ci-verify preview ref exists for $BRANCH"
fi
CLOSED_JSON="[{\"id\": \"SABLE-work\", \"status\": \"closed\", \"metadata\": {\"branch\": \"$BRANCH\"}}]"
CALLLOG="$TMPROOT/e-queued-bdcalls.log"
OUT="$(BD_CALL_LOG="$CALLLOG" STUB_BD_WORKBEAD_JSON="$CLOSED_JSON" \
       STUB_GH_RUN_LIST_JSON='[{"status":"in_progress"}]' run_reconcile)"; RC=$?
E1_OUT="$OUT"
if [ "$RC" -eq 0 ] && ! grep -q "^create " "$CALLLOG" 2>/dev/null; then
  pass "E1 SABLE-xw32f: a branch with an in-flight preview files NO for-chuck handoff"
else
  fail "E1 SABLE-xw32f: a queued branch must not file" "rc=$RC out=$OUT"
fi
if printf '%s' "$OUT" | grep -q "$BRANCH: QUEUED("; then
  pass "E1 SABLE-xw32f: the queued branch is NAMED, not silently skipped (SABLE-2az2x's constraint)"
else
  fail "E1 SABLE-xw32f: a queued branch must be reported by name" "out=$OUT"
fi
SUMMARY="$(printf '%s\n' "$OUT" | grep '^sable-reconcile-handoffs:')"
if printf '%s' "$SUMMARY" | grep -q "1 queued-at-seat branch(es)" \
   && printf '%s' "$SUMMARY" | grep -q "$BRANCH"; then
  pass "E1 SABLE-xw32f: the SUMMARY carries a queued count naming the branch"
else
  fail "E1 SABLE-xw32f: summary must name queued branches with a count" "summary=[$SUMMARY]"
fi

# (E2) same ci-verify ref, but Actions now reports it TERMINAL (completed) —
# a stale, uncleaned-up ref must NOT read as still-queued forever; the
# branch falls through to the ordinary predicate and files.
CALLLOG="$TMPROOT/e-terminal-bdcalls.log"
OUT="$(BD_CALL_LOG="$CALLLOG" STUB_BD_WORKBEAD_JSON="$CLOSED_JSON" \
       STUB_GH_RUN_LIST_JSON='[{"status":"completed"}]' run_reconcile)"; RC=$?
if [ "$RC" -eq 0 ] && grep -q "^create " "$CALLLOG" 2>/dev/null; then
  pass "E2 SABLE-xw32f: a TERMINAL preview run does not swallow a real strand — the branch still files"
else
  fail "E2 SABLE-xw32f: a terminal preview must not suppress filing forever" "rc=$RC out=$OUT"
fi

# (E2-positive-control) a SEPARATE branch with NO preview kicked at all DOES
# file — proving the sweep is capable of filing in this exact fixture
# configuration, so E1's silence is the queued check's doing, not a broken
# harness (xw32f's own required positive control).
fresh_pair e-no-preview
CALLLOG="$TMPROOT/e-no-preview-bdcalls.log"
OUT="$(BD_CALL_LOG="$CALLLOG" STUB_BD_WORKBEAD_JSON="$CLOSED_JSON" run_reconcile)"; RC=$?
if [ "$RC" -eq 0 ] && grep -q "^create " "$CALLLOG" 2>/dev/null; then
  pass "E2 POSITIVE CONTROL: a branch with no preview at all still files (the sweep can file in this run)"
else
  fail "E2 POSITIVE CONTROL: an unqueued, genuinely stranded branch must still file" "rc=$RC out=$OUT"
fi

# (E3) the preview-kick leg (jd5fj.2) stays structurally independent of the
# QUEUED check — reading P1/P4 only, exactly as SABLE-jejx3's D4 pins for
# HELD. E1's own run (captured in E1_OUT, the SAME queued branch) already
# printed its preview-kick line BEFORE classify_branch ever ran (reconcile()
# runs the preview-kick pass unconditionally per branch, ahead of the
# stranded/queued classification) — confirm that line is there, so a queued
# verdict is never mistaken for having silently disabled CI warm-up.
if printf '%s' "$E1_OUT" | grep -q "$E1_BRANCH: preview-kick kick-ok"; then
  pass "E3 SABLE-xw32f: the preview-kick leg still fires independently of the queued predicate"
else
  fail "E3 SABLE-xw32f: preview-kick must stay structurally independent of the queued check" "E1_OUT=$E1_OUT"
fi

echo "----------------------------------------------------------------------"
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
[ "$FAIL" -eq 0 ]
