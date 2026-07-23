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
#   C1  bead A declares metadata.landing_pair=B; B is neither landed nor named
#       on this promote call -> REFUSED (exit 28), naming B. The integration
#       branch tip does not move.
#   C2  the SAME promote, with --with-pair B naming the counterpart, succeeds
#       (exit 0) — C1's non-vacuity: the refusal was the missing
#       acknowledgement, not a broken gate.
#   C3  bead B, once bead A has genuinely LANDED (its notes carry this
#       module's own "promoted byte-identical to" marker), promotes on its
#       own — no --with-pair needed, because the counterpart already landed.
#   C4  a bead with NO landing_pair metadata at all promotes independently,
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

# ---------------------------------------------------------------------------
# A real, throwaway bd DB (SABLE-jd5fj.15's isolation recipe: a FRESH DB, not
# a copy of the real one — this suite never touches the operator's real pool).
# ---------------------------------------------------------------------------
BEADS_ROOT="$TMPROOT/beads"
mkdir -p "$BEADS_ROOT"
INIT_OUT="$(cd "$BEADS_ROOT" && env BD_NON_INTERACTIVE=1 bd init --prefix=lpg 2>&1)"
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

bdu "$BID_A" --set-metadata "landing_pair=$BID_B"
bdu "$BID_B" --set-metadata "landing_pair=$BID_A"
# BID_C carries no landing_pair metadata at all — the negative control.

# ---------------------------------------------------------------------------
# C1 — a solo promote of the paired bead A is refused, naming B
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

if [ "$(base_tip)" = "$TIP_BEFORE" ]; then
  pass "C1 the integration branch tip did not move under a refused promote"
else
  fail "C1 refused promote moved the base" "$TIP_BEFORE -> $(base_tip)"
fi

# ---------------------------------------------------------------------------
# C2 — the SAME promote, with --with-pair naming the counterpart, succeeds
# ---------------------------------------------------------------------------
OUT2="$(gate promote --bead "$BID_A" --branch wk-a --base "$BASE_BR" \
        --repo "$G_WORK" --remote origin --manager chuck \
        --with-pair "$BID_B")"; RC2=$?

if [ "$RC2" -eq 0 ] && [ "$(base_tip)" != "$TIP_BEFORE" ]; then
  pass "C2 --with-pair satisfies the check and the promote succeeds"
else
  fail "C2 --with-pair promote succeeds" "rc=$RC2 tip=$(base_tip) (was $TIP_BEFORE)
$OUT2"
fi

# ---------------------------------------------------------------------------
# C3 — bead B promotes on its own once A has genuinely LANDED (no --with-pair)
# ---------------------------------------------------------------------------
TIP_AFTER_A="$(base_tip)"
OUT3="$(gate promote --bead "$BID_B" --branch wk-b --base "$BASE_BR" \
        --repo "$G_WORK" --remote origin --manager chuck)"; RC3=$?

if [ "$RC3" -eq 0 ] && [ "$(base_tip)" != "$TIP_AFTER_A" ]; then
  pass "C3 bead B promotes solo once A has landed — no --with-pair needed"
else
  fail "C3 solo promote of B succeeds once A landed" "rc=$RC3 tip=$(base_tip) (was $TIP_AFTER_A)
$OUT3"
fi

# ---------------------------------------------------------------------------
# C4 — an unpaired bead (no landing_pair metadata) promotes independently
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
  pass "C4 an unpaired bead promotes independently, untouched by the check"
else
  fail "C4 unpaired bead promotes independently" "rc=$RC4 tip=$(base_tip) (was $TIP_BEFORE_C)
$OUT4"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
