#!/usr/bin/env bash
# test-landing-pair-gate.sh — MUST-LAND-TOGETHER pairing refusal, in real
# composition (SABLE-rzkw7).
#
# WHAT IS UNDER TEST
# ------------------
# The near-miss that named this bead: chuck was holding one half of a
# deliberately-paired change and said he would have promoted it on the other
# lane's sign-off alone, because the pairing lived only in a bead note and a
# manager's working memory — nowhere the promote path ever reads. This suite
# proves the mechanical floor against a REAL bd store and REAL git (only the
# GitHub Actions verdict is injected, the same seam every other merge-gate
# suite uses — see test-snapshot-freeze.sh/test-optimistic-promotion.sh):
#
#   C1  the authoritative CLI declares A/B, then B's metadata is removed to
#       plant a real asymmetric store. A is refused and the tip does not move.
#   C2  B also discovers A's reverse-only declaration through the durable
#       relates-to index, is refused, and the tip still does not move.
#   C3  the authoritative declaration CLI repairs the pair; the former
#       --with-pair acknowledgement is rejected as unknown and
#       cannot move the integration branch.
#   C4  A and B form one CI-verified fold and land through ONE integration-ref
#       update; both exact branch tips are ancestors of the new tip.
#   C5  the authoritative removal CLI clears both metadata records in one
#       operation. The generic relation remains policy-inert and is not
#       destructively removed because it may have predated the pair.
#   C6  a bead with NO landing_pair metadata at all promotes independently,
#       untouched by any of this — the negative control that proves the check
#       discriminates on the declared relation, not on any file-level property.
#
# Run with:
#   bash hooks/test/test-landing-pair-gate.sh
#
# SELF-SKIPS (loudly) when bd is not on PATH (SABLE-59zu clean room) — this
# suite's whole point is the real bd metadata + notes round-trip, so a bd-free
# environment has nothing for it to prove; it drives its own throwaway bd DB,
# never the real bead pool.

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$TESTDIR/../.." && pwd)"
GATE="$REPO_ROOT/bin/sable-merge-gate"

[ -f "$GATE" ] || { echo "FATAL: missing $GATE"; exit 2; }

if ! command -v bd >/dev/null 2>&1; then
  echo "SKIP: bd not on PATH — this suite's whole point is a real bd store"
  exit 0
fi

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

BASE_BR="trunk"

# ---------------------------------------------------------------------------
# A real origin + clone, two disjoint worker branches.
# ---------------------------------------------------------------------------
G_ORIGIN="$TMPROOT/gate-origin.git"
G_WORK="$TMPROOT/gate-work"
git init -q --bare -b "$BASE_BR" "$G_ORIGIN"
git --git-dir="$G_ORIGIN" config core.logAllRefUpdates true
git clone -q "$G_ORIGIN" "$G_WORK" 2>/dev/null
git -C "$G_WORK" config user.email "t@sable.invalid"
git -C "$G_WORK" config user.name "SABLE Test"
echo base > "$G_WORK/f.txt"
git -C "$G_WORK" add -A
git -C "$G_WORK" commit -q -m init
git -C "$G_WORK" push -q origin "$BASE_BR"

git -C "$G_WORK" checkout -q -b wk-a
echo a >> "$G_WORK/a.txt"
git -C "$G_WORK" add -A
git -C "$G_WORK" commit -qam "wk-a change"
git -C "$G_WORK" push -q origin wk-a

git -C "$G_WORK" checkout -q "$BASE_BR"
git -C "$G_WORK" checkout -q -b wk-b
echo b >> "$G_WORK/b.txt"
git -C "$G_WORK" add -A
git -C "$G_WORK" commit -qam "wk-b change"
git -C "$G_WORK" push -q origin wk-b
git -C "$G_WORK" checkout -q "$BASE_BR"

# A fake gh reporting each branch's real tip as a successful run — the same
# seam test-snapshot-freeze.sh / test-optimistic-promotion.sh already use.
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
      SABLE_MG_NOTIFY=true \
      SABLE_MG_POLL=0 SABLE_MG_GRACE=0 SABLE_MG_TIMEOUT=0 \
      BEADS_DB="$BEADS_DB" \
      python3 "$GATE" "$@" 2>&1
}
base_tip() { git --git-dir="$G_ORIGIN" rev-parse "refs/heads/$BASE_BR"; }
base_reflog_count() {
  git --git-dir="$G_ORIGIN" reflog show --format=%H "refs/heads/$BASE_BR" | wc -l | tr -d ' '
}

# ---------------------------------------------------------------------------
# A real, throwaway bd DB (SABLE-jd5fj.15's isolation recipe: a FRESH DB, not
# a copy of the real one — this suite never touches the operator's real pool).
# ---------------------------------------------------------------------------
BEADS_ROOT="$TMPROOT/beads"
mkdir -p "$BEADS_ROOT"
# -u BEADS_DB (SABLE-sx1rb): never inherit an ambient BEADS_DB (e.g. the
# impact tier's own) — this suite builds and must use its OWN isolated DB.
INIT_OUT="$(cd "$BEADS_ROOT" && env -u BEADS_DB BD_NON_INTERACTIVE=1 bd init --prefix=lpg 2>&1)"
if [ ! -d "$BEADS_ROOT/.beads" ]; then
  echo "FATAL: could not initialize an isolated bd DB: $INIT_OUT"
  exit 2
fi
BEADS_DB="$BEADS_ROOT/.beads"

bdc() { env BEADS_DB="$BEADS_DB" bd create "$@"; }
bdu() { env BEADS_DB="$BEADS_DB" bd update "$@" >/dev/null; }
bead_id_of() { echo "$1" | grep -oE 'Created issue:\s*\S+' | awk '{print $NF}'; }

BID_A="$(bead_id_of "$(bdc --title="landing-pair test A" \
  --description="scratch bead for hooks/test/test-landing-pair-gate.sh [no-test]" \
  --type=task -p 2)")"
BID_B="$(bead_id_of "$(bdc --title="landing-pair test B" \
  --description="scratch bead for hooks/test/test-landing-pair-gate.sh [no-test]" \
  --type=task -p 2)")"
BID_C="$(bead_id_of "$(bdc --title="landing-pair test C (unpaired)" \
  --description="scratch bead for hooks/test/test-landing-pair-gate.sh [no-test]" \
  --type=task -p 2)")"

if [ -z "$BID_A" ] || [ -z "$BID_B" ] || [ -z "$BID_C" ]; then
  echo "FATAL: could not extract bead ids (A=$BID_A B=$BID_B C=$BID_C)"
  exit 2
fi

DECLARE_OUT="$(gate landing-pair declare "$BID_A" "$BID_B" --repo "$G_WORK")"
DECLARE_RC=$?
if [ "$DECLARE_RC" -eq 0 ]; then
  pass "C1 authoritative landing-pair declaration succeeds"
else
  fail "C1 authoritative landing-pair declaration succeeds" \
       "rc=$DECLARE_RC
$DECLARE_OUT"
fi

# Plant the failure mode from SABLE-8mfe6 in a real store: a partially removed
# declaration. The sanctioned remover never emits this state, but a killed old
# two-write workflow or manual metadata edit can. The relation intentionally
# remains, making A's declaration visible from B's single ordinary show read.
bdu "$BID_B" --unset-metadata landing_pair
# BID_C carries no landing_pair metadata at all — the negative control.

# ---------------------------------------------------------------------------
# C1 — the declaring endpoint is refused under asymmetric state
# ---------------------------------------------------------------------------
TIP_BEFORE="$(base_tip)"
OUT1="$(gate promote --bead "$BID_A" --branch wk-a --base "$BASE_BR" \
        --repo "$G_WORK" --remote origin --manager chuck)"; RC1=$?

if [ "$RC1" -eq 28 ]; then
  pass "C1 a solo promote of a MUST-LAND-TOGETHER bead is REFUSED with exit 28"
else
  fail "C1 solo promote exits 28" "rc=$RC1
$OUT1"
fi

if echo "$OUT1" | grep -q "$BID_B"; then
  pass "C1 the refusal names the counterpart bead ($BID_B)"
else
  fail "C1 refusal names the counterpart" "$OUT1"
fi

if echo "$OUT1" | grep -Eq 'landing-pair declare|landing-pair remove'; then
  pass "C1 the asymmetric refusal names the mechanical repair paths"
else
  fail "C1 asymmetric refusal names a repair path" "$OUT1"
fi

if [ "$(base_tip)" = "$TIP_BEFORE" ]; then
  pass "C1 the integration branch tip did not move under a refused promote"
else
  fail "C1 refused promote moved the base" "$TIP_BEFORE -> $(base_tip)"
fi

# ---------------------------------------------------------------------------
# C2 — the blank endpoint discovers the reverse-only declaration and refuses
# ---------------------------------------------------------------------------
OUT2="$(gate promote --bead "$BID_B" --branch wk-b --base "$BASE_BR" \
        --repo "$G_WORK" --remote origin --manager chuck)"; RC2=$?

if [ "$RC2" -eq 28 ] && echo "$OUT2" | grep -qi asymmetric; then
  pass "C2 reverse-only endpoint is mechanically refused as asymmetric"
else
  fail "C2 reverse-only endpoint is refused" "rc=$RC2
$OUT2"
fi

if [ "$(base_tip)" = "$TIP_BEFORE" ]; then
  pass "C2 the integration branch still did not move"
else
  fail "C2 reverse-only refusal moved the base" "$TIP_BEFORE -> $(base_tip)"
fi

# ---------------------------------------------------------------------------
# C3 — the one declaration interface repairs state; old acknowledgement stays gone
# ---------------------------------------------------------------------------
REPAIR_OUT="$(gate landing-pair declare "$BID_A" "$BID_B" --repo "$G_WORK")"
REPAIR_RC=$?
OUT3="$(gate promote --bead "$BID_A" --branch wk-a --base "$BASE_BR" \
        --repo "$G_WORK" --remote origin --manager chuck \
        --with-pair "$BID_B")"; RC3=$?

if [ "$REPAIR_RC" -eq 0 ]; then
  pass "C3 authoritative declaration repairs partial metadata"
else
  fail "C3 authoritative declaration repairs partial metadata" \
       "rc=$REPAIR_RC
$REPAIR_OUT"
fi

if [ "$RC3" -eq 2 ] && [ "$(base_tip)" = "$TIP_BEFORE" ]; then
  pass "C3 removed --with-pair flag is rejected and moves nothing"
else
  fail "C3 removed --with-pair is fail-closed" "rc=$RC3 tip=$(base_tip) (was $TIP_BEFORE)
$OUT3"
fi

# ---------------------------------------------------------------------------
# C4 — both paired branches land through one exact combined-object ref update
# ---------------------------------------------------------------------------
FOLD_INFO="$(
  env PYTHONPATH="$REPO_ROOT/bin" python3 - "$G_WORK" "$BASE_BR" "$BID_A" "$BID_B" <<'PY'
import sys
import sable_batch_fold_lib as fold
import sable_gate_git_lib as git_lib

repo, base, bead_a, bead_b = sys.argv[1:]
git_lib._git(repo, "fetch", "origin", base, "wk-a", "wk-b")
base_sha = git_lib.resolve_commit(repo, "origin/" + base)
members = [
    fold.FoldMember("wk-a", git_lib.resolve_commit(repo, "origin/wk-a"), bead_a),
    fold.FoldMember("wk-b", git_lib.resolve_commit(repo, "origin/wk-b"), bead_b),
]
tip, ref = fold.push_batch_ref(repo, "origin", base_sha, members)
print(tip, ref)
PY
)"
FOLD_TIP="${FOLD_INFO%% *}"
REFLOG_BEFORE="$(base_reflog_count)"
OUT4="$(gate land-batch --base "$BASE_BR" \
        --member "wk-a:$BID_A" --member "wk-b:$BID_B" \
        --repo "$G_WORK" --remote origin --manager chuck)"; RC4=$?
REFLOG_AFTER="$(base_reflog_count)"

if [ "$RC4" -eq 0 ] && [ "$(base_tip)" = "$FOLD_TIP" ]; then
  pass "C4 the exact CI-verified pair fold lands"
else
  fail "C4 exact pair fold lands" "rc=$RC4 tip=$(base_tip) expected=$FOLD_TIP
$OUT4"
fi

if [ $((REFLOG_AFTER - REFLOG_BEFORE)) -eq 1 ]; then
  pass "C4 pair landing advances the integration ref exactly once"
else
  fail "C4 pair landing uses one integration-ref update" \
       "reflog before=$REFLOG_BEFORE after=$REFLOG_AFTER"
fi

if git --git-dir="$G_ORIGIN" merge-base --is-ancestor \
     "$(git --git-dir="$G_ORIGIN" rev-parse refs/heads/wk-a)" "$FOLD_TIP" &&
   git --git-dir="$G_ORIGIN" merge-base --is-ancestor \
     "$(git --git-dir="$G_ORIGIN" rev-parse refs/heads/wk-b)" "$FOLD_TIP"; then
  pass "C4 both exact worker tips are contained in the landed fold"
else
  fail "C4 landed fold contains both exact worker tips"
fi

# ---------------------------------------------------------------------------
# C5 — removal clears both declarations and preserves generic relationship data
# ---------------------------------------------------------------------------
REMOVE_OUT="$(gate landing-pair remove "$BID_A" "$BID_B" --repo "$G_WORK")"
REMOVE_RC=$?
PAIR_STATE="$(
  env BEADS_DB="$BEADS_DB" bd show "$BID_A" "$BID_B" --json |
    python3 -c 'import json,sys
rows=json.load(sys.stdin)
bad=[r["id"] for r in rows
     if (r.get("metadata") or {}).get("landing_pair")]
print(",".join(bad))'
)"
RELATION_COUNT="$(
  env BEADS_DB="$BEADS_DB" bd show "$BID_A" "$BID_B" --json |
    python3 -c 'import json,sys
rows=json.load(sys.stdin)
members={r["id"] for r in rows}
print(sum(1 for r in rows for d in r.get("dependencies", [])
          if d.get("dependency_type") == "relates-to"
          and d.get("id") in members))'
)"
if [ "$REMOVE_RC" -eq 0 ] && [ -z "$PAIR_STATE" ] &&
   [ "$RELATION_COUNT" -eq 2 ]; then
  pass "C5 removal clears both declarations without deleting generic relation"
else
  fail "C5 authoritative removal clears reciprocal state" \
       "rc=$REMOVE_RC remaining=$PAIR_STATE relation_count=$RELATION_COUNT
$REMOVE_OUT"
fi

# ---------------------------------------------------------------------------
# C6 — an unpaired bead (no landing_pair metadata) promotes independently
# ---------------------------------------------------------------------------
git -C "$G_WORK" checkout -q "$BASE_BR"
git -C "$G_WORK" pull -q origin "$BASE_BR"
git -C "$G_WORK" checkout -q -b wk-c
echo c >> "$G_WORK/c.txt"
git -C "$G_WORK" add -A
git -C "$G_WORK" commit -qam "wk-c change"
git -C "$G_WORK" push -q origin wk-c
git -C "$G_WORK" checkout -q "$BASE_BR"

TIP_BEFORE_C="$(base_tip)"
OUT4="$(gate promote --bead "$BID_C" --branch wk-c --base "$BASE_BR" \
        --repo "$G_WORK" --remote origin --manager chuck)"; RC4=$?

if [ "$RC4" -eq 0 ] && [ "$(base_tip)" != "$TIP_BEFORE_C" ]; then
  pass "C6 an unpaired bead promotes independently, untouched by the check"
else
  fail "C6 unpaired bead promotes independently" "rc=$RC4 tip=$(base_tip) (was $TIP_BEFORE_C)
$OUT4"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
