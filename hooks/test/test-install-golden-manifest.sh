#!/usr/bin/env bash
# test-install-golden-manifest.sh — the clean-room-runnable half of the golden
# install baseline (SABLE-slip0.7).
#
# WHY THIS SUITE EXISTS. fixtures/install-golden-manifest.txt pins a sha256 per
# installed artifact, and its ONLY consumer was test-install.sh:181 — which is
# permanently EXCLUDE'd from the ci-verify run-set (it needs a real ~/.claude
# SABLE install; the clean room ships none, SABLE-59zu). So the golden had no
# executor anywhere in the gate: an artifact could change while its pinned hash
# stayed behind and NOTHING went red. That is not hypothetical — at the base of
# this branch three entries (hooks/tdd-gate.sh, agents/rudy.md,
# skills/sable-execute/SKILL.md) had already rotted that way, unnoticed, across
# several commits. The same silence is what stranded SABLE-slip0.1: it changed
# session-role-anchor.sh, could not land the matching line-30 refresh, and no
# check could speak to whether the pair was consistent.
#
# WHAT IT CHECKS, AND WHY IT CAN RUN WHERE test-install.sh CANNOT. test-install.sh
# asserts the golden by RUNNING install.sh into a scratch HOME and hashing the
# result — real install, real ~/.claude layout, hence the permanent exclusion.
# This suite asserts the strictly weaker but still load-bearing half: for every
# artifact install.sh copies VERBATIM, the pinned hash must equal the sha256 of
# the repo file it is copied from. That relation is pure sha256 over tracked
# files — no bd, no ~/.claude, no install run, no tmux — so it runs in the clean
# room, in ALLOW, on every branch. It does NOT replace test-install.sh: the
# destination layout, the transformed/generated artifacts, and the install
# mechanics remain that suite's job, and its exclusion stays explicit.
#
# FAIL-CLOSED ON UNKNOWN ENTRIES. An installed path that maps to neither a
# verbatim source nor an explicitly-listed GOLDEN_DERIVED entry is an ERROR, not
# a skip. A checker that silently narrows to zero checked entries would read
# exactly like a green one — the same "instrument narrower than the phenomenon"
# class as SABLE-7v3z/SABLE-lcevs — so the checked count is asserted too.
#
# Run with:
#   bash hooks/test/test-install-golden-manifest.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
. "$REPO/hooks/test/lib-golden-manifest.sh"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/sable-test-golden-manifest.XXXXXX")"
trap 'rm -rf "$TMPROOT"' EXIT

GOLDEN="$REPO/$GOLDEN_MANIFEST_REL"
ANCHOR_PATH="./hooks/multi-manager/session-role-anchor.sh"

# ---------------------------------------------------------------------------
# 1. THE ASSERTION: every verbatim-copied artifact's pinned hash matches the
#    repo source it is copied from, at THIS commit.
# ---------------------------------------------------------------------------
golden_check "$GOLDEN" "$REPO"; RC_REAL=$?
# Captured now: every later case re-runs golden_check against a deliberately
# corrupted copy, which overwrites these counters.
REAL_CHECKED="$GOLDEN_CHECKED"
if [ "$RC_REAL" -eq 0 ]; then
  pass "golden manifest agrees with the repo sources it pins ($GOLDEN_CHECKED verbatim entr(y/ies), $GOLDEN_DERIVED_SEEN generated/transformed)"
else
  fail "golden manifest agrees with the repo sources it pins" \
       "$(printf '%s\n' "${GOLDEN_ERRORS[@]}")"
fi

# A checker that resolved nothing would pass case 1 vacuously. The golden pins
# every installed artifact, so the verbatim majority is dozens of entries — a
# collapse to a handful means the resolver stopped recognizing a whole class.
if [ "$REAL_CHECKED" -ge 40 ]; then
  pass "the check is not vacuous — $REAL_CHECKED entr(y/ies) were actually hashed and compared"
else
  fail "the check is not vacuous — expected >=40 verbatim entries hashed" "GOLDEN_CHECKED=$REAL_CHECKED"
fi

# ---------------------------------------------------------------------------
# 2. PLANT-AND-FAIL, BOTH POLARITIES (SABLE-slip0.7 AC2). The bead's required
#    property is that the check "can fail when line 30 does not equal the
#    sha256 of hooks/multi-manager/session-role-anchor.sh and passes when it
#    does". Plant a wrong hash for exactly that entry in a COPY of the fixture
#    (the real one is never written), assert the checker BITES and names the
#    path, then restore the exact hash and assert it PASSES again.
# ---------------------------------------------------------------------------
PLANTED="$TMPROOT/planted-manifest.txt"
WRONG_HASH="0000000000000000000000000000000000000000000000000000000000000000"
REAL_HASH="$(sha256sum "$REPO/hooks/multi-manager/session-role-anchor.sh" | cut -d' ' -f1)"

awk -v path="$ANCHOR_PATH" -v wrong="$WRONG_HASH" \
  '{ if (index($0, "  " path) > 0) { print wrong "  " path } else { print } }' \
  "$GOLDEN" > "$PLANTED"

# The plant must actually have landed — an awk that matched nothing would make
# the negative control below assert against an unmodified file and "pass".
if grep -q "^$WRONG_HASH  $ANCHOR_PATH\$" "$PLANTED" \
   && [ "$(grep -c . "$PLANTED")" = "$(grep -c . "$GOLDEN")" ]; then
  pass "plant control: the wrong hash was written to the copy for $ANCHOR_PATH (line count unchanged)"
else
  fail "plant control: the wrong hash was written to the copy for $ANCHOR_PATH (line count unchanged)"
fi

golden_check "$PLANTED" "$REPO"; RC_PLANTED=$?
PLANTED_ERRS="$(printf '%s\n' "${GOLDEN_ERRORS[@]:-}")"
if [ "$RC_PLANTED" -ne 0 ] && printf '%s' "$PLANTED_ERRS" | grep -q 'session-role-anchor.sh'; then
  pass "NEGATIVE POLARITY: a golden entry whose hash != sha256(source) FAILS the check, naming the path"
else
  fail "NEGATIVE POLARITY: a golden entry whose hash != sha256(source) FAILS the check, naming the path" \
       "rc=$RC_PLANTED errors=$PLANTED_ERRS"
fi

# The error must name BOTH hashes — a bare "mismatch" leaves the reader unable
# to tell a stale pin from a corrupted source without re-deriving by hand.
if printf '%s' "$PLANTED_ERRS" | grep -q "$REAL_HASH" \
   && printf '%s' "$PLANTED_ERRS" | grep -q "$WRONG_HASH"; then
  pass "the mismatch error names both the pinned hash and the actual source hash"
else
  fail "the mismatch error names both the pinned hash and the actual source hash" "errors=$PLANTED_ERRS"
fi

# The mismatch must be attributed to ONE entry — a checker that reds the whole
# file on any single drift cannot tell the author which pin to refresh.
if [ "${#GOLDEN_ERRORS[@]}" -eq 1 ]; then
  pass "exactly one entry is reported for a one-entry plant (errors are per-pin, not whole-file)"
else
  fail "exactly one entry is reported for a one-entry plant (errors are per-pin, not whole-file)" \
       "count=${#GOLDEN_ERRORS[@]} errors=$PLANTED_ERRS"
fi

RESTORED="$TMPROOT/restored-manifest.txt"
awk -v path="$ANCHOR_PATH" -v right="$REAL_HASH" \
  '{ if (index($0, "  " path) > 0) { print right "  " path } else { print } }' \
  "$PLANTED" > "$RESTORED"
golden_check "$RESTORED" "$REPO"; RC_RESTORED=$?
if [ "$RC_RESTORED" -eq 0 ] && cmp -s "$RESTORED" "$GOLDEN"; then
  pass "POSITIVE POLARITY: restoring the exact sha256 of session-role-anchor.sh PASSES the check again"
else
  fail "POSITIVE POLARITY: restoring the exact sha256 of session-role-anchor.sh PASSES the check again" \
       "rc=$RC_RESTORED (restored file identical to golden: $(cmp -s "$RESTORED" "$GOLDEN" && echo yes || echo no))"
fi

# ---------------------------------------------------------------------------
# 3. FAIL-CLOSED ON AN UNRECOGNIZED INSTALLED PATH. A new artifact class that
#    the resolver does not understand must ERROR, not be silently skipped —
#    silent skipping is how a pin stops being checked without anyone noticing.
# ---------------------------------------------------------------------------
UNKNOWN="$TMPROOT/unknown-entry-manifest.txt"
cp -f "$GOLDEN" "$UNKNOWN"
printf '%s  ./some-new-artifact-class/thing.conf\n' "$WRONG_HASH" >> "$UNKNOWN"
golden_check "$UNKNOWN" "$REPO"; RC_UNKNOWN=$?
UNKNOWN_ERRS="$(printf '%s\n' "${GOLDEN_ERRORS[@]:-}")"
if [ "$RC_UNKNOWN" -ne 0 ] && printf '%s' "$UNKNOWN_ERRS" | grep -q 'some-new-artifact-class/thing.conf'; then
  pass "an installed path matching neither a verbatim source nor GOLDEN_DERIVED FAILS the check, naming it"
else
  fail "an installed path matching neither a verbatim source nor GOLDEN_DERIVED FAILS the check, naming it" \
       "rc=$RC_UNKNOWN errors=$UNKNOWN_ERRS"
fi

# ---------------------------------------------------------------------------
# 4. THE EXEMPTION LIST CANNOT ROT. GOLDEN_DERIVED names the artifacts
#    install.sh generates or transforms rather than copies — the entries this
#    suite deliberately does NOT hash. An entry that no longer appears in the
#    golden at all is a stale exemption: it would keep excusing a path that no
#    longer exists, and would silently excuse it again if the path came back
#    under different semantics.
# ---------------------------------------------------------------------------
DROPPED="$TMPROOT/dropped-derived-manifest.txt"
grep -v '  ./\.sable-install-provenance$' "$GOLDEN" > "$DROPPED"
if [ "$(grep -c . "$DROPPED")" -eq "$(( $(grep -c . "$GOLDEN") - 1 ))" ]; then
  pass "drop control: exactly one GOLDEN_DERIVED entry was removed from the copy"
else
  fail "drop control: exactly one GOLDEN_DERIVED entry was removed from the copy"
fi
golden_check "$DROPPED" "$REPO"; RC_DROPPED=$?
DROPPED_ERRS="$(printf '%s\n' "${GOLDEN_ERRORS[@]:-}")"
if [ "$RC_DROPPED" -ne 0 ] && printf '%s' "$DROPPED_ERRS" | grep -q 'sable-install-provenance'; then
  pass "a GOLDEN_DERIVED entry that no longer appears in the golden FAILS the check (exemptions cannot rot)"
else
  fail "a GOLDEN_DERIVED entry that no longer appears in the golden FAILS the check (exemptions cannot rot)" \
       "rc=$RC_DROPPED errors=$DROPPED_ERRS"
fi

# ---------------------------------------------------------------------------
# 5. A MISSING SOURCE FILE IS AN ERROR, NOT A PASS. If an artifact is deleted
#    from the repo but its pin stays behind, sha256 of a nonexistent file must
#    not degrade into "no mismatch found".
# ---------------------------------------------------------------------------
GONE="$TMPROOT/gone-source-manifest.txt"
cp -f "$GOLDEN" "$GONE"
printf '%s  ./hooks/multi-manager/deleted-hook.sh\n' "$WRONG_HASH" >> "$GONE"
golden_check "$GONE" "$REPO"; RC_GONE=$?
GONE_ERRS="$(printf '%s\n' "${GOLDEN_ERRORS[@]:-}")"
if [ "$RC_GONE" -ne 0 ] && printf '%s' "$GONE_ERRS" | grep -q 'deleted-hook.sh'; then
  pass "a pin whose repo source no longer exists FAILS the check, naming it"
else
  fail "a pin whose repo source no longer exists FAILS the check, naming it" "rc=$RC_GONE errors=$GONE_ERRS"
fi

# ---------------------------------------------------------------------------
# 6. A MISSING GOLDEN IS AN ERROR. The fixture disappearing must not read as
#    "nothing to check, all clear".
# ---------------------------------------------------------------------------
golden_check "$TMPROOT/no-such-manifest.txt" "$REPO"; RC_ABSENT=$?
if [ "$RC_ABSENT" -ne 0 ]; then
  pass "an absent golden manifest FAILS the check (absence is not vacuous green)"
else
  fail "an absent golden manifest FAILS the check (absence is not vacuous green)" "rc=$RC_ABSENT"
fi

# ---------------------------------------------------------------------------
# 7. THE PIN SET AND THE SELECTION RULE AGREE. .github/ci/impact-manifest.sh
#    selects this suite for any repo source the golden pins (that is what makes
#    a hook change alone unable to land a stale manifest). Both sides read
#    golden_pinned_sources from this same lib; assert the set is non-empty and
#    contains the artifact SABLE-slip0.1 actually changed, so the rule cannot
#    quietly resolve to nothing.
# ---------------------------------------------------------------------------
PINNED="$(golden_pinned_sources "$GOLDEN")"
PINNED_N="$(printf '%s\n' "$PINNED" | grep -c .)"
if [ "$PINNED_N" -eq "$REAL_CHECKED" ] && [ "$PINNED_N" -ge 40 ]; then
  pass "golden_pinned_sources resolves the same $PINNED_N source(s) the hash check compares"
else
  fail "golden_pinned_sources resolves the same source set the hash check compares" \
       "pinned=$PINNED_N checked=$REAL_CHECKED"
fi
if printf '%s\n' "$PINNED" | grep -qx 'hooks/multi-manager/session-role-anchor.sh'; then
  pass "the pinned-source set includes hooks/multi-manager/session-role-anchor.sh (the SABLE-slip0.1 artifact)"
else
  fail "the pinned-source set includes hooks/multi-manager/session-role-anchor.sh (the SABLE-slip0.1 artifact)"
fi
# Every resolved source must be a real tracked file — a resolver that emitted
# plausible-but-wrong paths would select this suite for paths nobody edits and
# never select it for the ones they do.
MISSING_SRC=""
while IFS= read -r src; do
  [ -n "$src" ] || continue
  [ -f "$REPO/$src" ] || MISSING_SRC="$MISSING_SRC $src"
done <<< "$PINNED"
if [ -z "$MISSING_SRC" ]; then
  pass "every resolved pinned source is a real file in the repo"
else
  fail "every resolved pinned source is a real file in the repo" "missing:$MISSING_SRC"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
