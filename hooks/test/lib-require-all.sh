#!/usr/bin/env bash
# lib-require-all.sh — per-clause reporting for conjunction controls (SABLE-muew7).
#
# THE DEFECT CLASS: a control built from a conjunction reports ONE boolean for
# N independent claims. Its red is a could-not-assess wearing a verdict's
# clothes. Concretely, hooks/test/test-ci-bd-coverage-gap.sh used to collapse
# three independent claims (rc==0, subtest count==5, no Skipped line) into one
# `if A && B && C; then pass; else fail "..."; fi` — when it went red, "this
# control failed" was the entire verdict. SABLE-1gnuj cost three agents and an
# evening because nobody could tell WHICH of the three had actually moved.
#
# THE FIX IS THE REPORT, NOT THE PREDICATE (see the bead — do not "fix" this
# by splitting every conjunction into independent controls; the three claims
# above genuinely must ALL hold, and three separate controls would triple the
# output and lose the statement that they are one property). require_all
# keeps the decision a single boolean and makes the REPORT distinguish the
# states: on a full pass it says nothing at all (a healthy run must stay
# quiet — this is the same property SABLE-wezu1's fix had to preserve), and
# on any failure it names every failing clause, not just the first.
#
# USAGE — pre-compute each clause as a shell exit code (0 = held, nonzero =
# failed; the bash-native convention, not a boolean value), then call:
#
#   [ "$RC" -eq 0 ];                                  c1=$?
#   [ "${TESTS:-0}" -eq 5 ];                           c2=$?
#   ! printf '%s' "$OUT" | grep -q 'Skipped:';         c3=$?
#
#   require_all "bd-present overlap control" \
#     "rc is 0" "$c1" \
#     "tests == 5" "$c2" \
#     "no Skipped line" "$c3"
#
#   if [ "$REQUIRE_ALL_OK" -eq 1 ]; then
#     pass "negative control: ..."
#   else
#     fail "negative control: ..." "$REQUIRE_ALL_DETAIL (rc=$RC tests=${TESTS:-<none>})"
#   fi
#
# Pre-computing each clause (rather than require_all eval-ing condition
# strings itself) keeps this a plain function call with no eval and no
# quoting hazard — the caller's own `[ ... ]` / `grep` already ran for real,
# require_all only asks each one's exit code what it decided.

# require_all NAME LABEL1 RC1 [LABEL2 RC2 ...]
# Sets (never prints — a fully-passing control must stay silent, or a
# per-clause "fix" that always narrates makes every green run unreadable):
#   REQUIRE_ALL_OK      1 if every clause's RC was 0, else 0
#   REQUIRE_ALL_DETAIL  "" when REQUIRE_ALL_OK=1; otherwise
#                       "NAME: failing clause(s): <label>[, <label>...]"
#                       naming ONLY the clauses whose RC was nonzero.
require_all() {
  local name="$1"; shift
  local label rc failing=""
  REQUIRE_ALL_OK=1
  while [ "$#" -ge 2 ]; do
    label="$1"; rc="$2"; shift 2
    if [ "$rc" -ne 0 ]; then
      REQUIRE_ALL_OK=0
      if [ -z "$failing" ]; then
        failing="$label"
      else
        failing="$failing, $label"
      fi
    fi
  done
  if [ "$REQUIRE_ALL_OK" -eq 1 ]; then
    REQUIRE_ALL_DETAIL=""
  else
    REQUIRE_ALL_DETAIL="$name: failing clause(s): $failing"
  fi
}
