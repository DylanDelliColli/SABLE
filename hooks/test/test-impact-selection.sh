#!/usr/bin/env bash
# test-impact-selection.sh — integration tests for the shell impact
# selection rule (SABLE-cmar4.2, story S1 of SABLE-cmar4; extended by
# SABLE-m4exv for the test-file-self-mapping fix): given a set of changed
# paths (git diff --name-only style), which suites should run.
#
#   1. a changed path under a mapped shared lib selects EVERY suite fanned
#      out to that lib (LIB_FANOUT)
#   2. a changed path under a mapped suite's own covered file selects
#      EXACTLY that suite
#   3. a changed path matching NEITHER selects the FULL ALLOW set
#      (conservative default — the impact tier's under-selection backstop)
#   4. (SABLE-m4exv) a suite's OWN test file always self-selects it, whether
#      or not the suite also has a COVERS entry — before this fix, a suite
#      WITH a COVERS entry lost its self-mapping entirely, which is why
#      editing e.g. hooks/test/test-optimistic-promotion.sh (5 COVERS
#      files) escalated to the FULL ALLOW set instead of selecting itself
#   5. (SABLE-m4exv) a bin/test_X.py pytest file maps to its production
#      companion bin/X.py's coverage when one exists, and otherwise is still
#      a MATCHED (not unmapped) path selecting zero additional suites —
#      pytest test files are scoped by the separate testmon-based pytest
#      tier, not by this shell manifest
#   6. (SABLE-m4exv) a suite's own file that is EXCLUDE-listed (not in
#      ALLOW) is still a matched, known path — it must not escalate to the
#      full set just because nothing runs for it
#   7. (SABLE-m4exv) the observability line: --select emits a "::notice::"
#      mode/reason line on stderr, SCOPED with a suite/path count or FULL
#      naming the unmapped path(s) — this is what makes the fallback
#      diagnosable instead of a bare, unexplained count
#
# Real bash processes throughout, REAL git repo + REAL `git diff --name-only`
# output feeding sable_select_impacted — no mocks, no bd/dolt. Fixture: a
# throwaway git repo carrying REAL, unmodified copies of
# .github/ci/shell-run-set.sh and .github/ci/impact-manifest.sh, with
# ALLOW/EXCLUDE/COVERS/LIB_FANOUT replaced wholesale for a small,
# fully-classified fixture universe (same substitution technique as
# test-impact-manifest.sh and test-tier-ssot-consumers.sh).
#
# Run with:
#   bash hooks/test/test-impact-selection.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PROD_RUNSET="$REPO/.github/ci/shell-run-set.sh"
PROD_MANIFEST="$REPO/.github/ci/impact-manifest.sh"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/sable-test-impact-selection.XXXXXX")"
trap 'rm -rf "$TMPROOT"' EXIT

REPO_DIR="$TMPROOT/fixture-repo"
BARE_DIR="$TMPROOT/fixture-bare.git"

git init -q --bare "$BARE_DIR"
git clone -q "$BARE_DIR" "$REPO_DIR"

mkdir -p "$REPO_DIR/.github/ci" "$REPO_DIR/hooks/test" "$REPO_DIR/hooks/multi-manager" "$REPO_DIR/unmapped-area" "$REPO_DIR/bin"
cp "$PROD_RUNSET"    "$REPO_DIR/.github/ci/shell-run-set.sh"
cp "$PROD_MANIFEST"  "$REPO_DIR/.github/ci/impact-manifest.sh"

# Fixture universe: two suites, each covering its own hook, each hook
# sourcing its own lib. A third suite has no COVERS entry (defaults to
# covering itself) and sources no lib at all. A fourth suite (gamma) has a
# COVERS entry pointing at a plain python file (no lib), used to exercise
# the pytest-companion mapping. A fifth suite (excluded) is deliberately
# EXCLUDE-listed rather than in ALLOW, to prove an excluded suite's own file
# is still a known/matched path.
cat > "$REPO_DIR/hooks/multi-manager/lib-alpha.sh" <<'EOF'
#!/usr/bin/env bash
true
EOF
cat > "$REPO_DIR/hooks/multi-manager/hook-alpha.sh" <<'EOF'
#!/usr/bin/env bash
. "$(dirname "${BASH_SOURCE[0]}")/lib-alpha.sh"
true
EOF
cat > "$REPO_DIR/hooks/multi-manager/lib-beta.sh" <<'EOF'
#!/usr/bin/env bash
true
EOF
cat > "$REPO_DIR/hooks/multi-manager/hook-beta.sh" <<'EOF'
#!/usr/bin/env bash
. "$(dirname "${BASH_SOURCE[0]}")/lib-beta.sh"
true
EOF
cat > "$REPO_DIR/hooks/test/test-fixture-alpha.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat > "$REPO_DIR/hooks/test/test-fixture-beta.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat > "$REPO_DIR/hooks/test/test-fixture-standalone.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat > "$REPO_DIR/hooks/test/test-fixture-gamma.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat > "$REPO_DIR/bin/gamma.py" <<'EOF'
# fixture production file for the pytest-companion mapping cases
EOF
cat > "$REPO_DIR/hooks/test/test-fixture-excluded.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

set_manifest() {
  python3 - "$REPO_DIR" <<'PYEOF'
import re, sys
fixture = sys.argv[1]
p = f"{fixture}/.github/ci/shell-run-set.sh"
s = open(p).read()

def replace_block(s, header, body_lines):
    pattern = re.escape(header) + r'.*?\n\)\n'
    body = "\n".join(body_lines)
    replacement = (f"{header}\n{body}\n)\n").replace('\\', '\\\\')
    new_s, n = re.subn(pattern, replacement, s, count=1, flags=re.DOTALL)
    assert n == 1, header
    return new_s

s = replace_block(s, "ALLOW=(", [
    "  test-fixture-alpha.sh",
    "  test-fixture-beta.sh",
    "  test-fixture-standalone.sh",
    "  test-fixture-gamma.sh",
])
s = replace_block(s, "declare -A EXCLUDE=(", [
    '  [test-fixture-excluded.sh]="fixture-only exclusion, no real gate reason needed [permanent: SABLE-m4exv]"',
])
s = replace_block(s, "declare -A COVERS=(", [
    '  [test-fixture-alpha.sh]="hooks/multi-manager/hook-alpha.sh"',
    '  [test-fixture-beta.sh]="hooks/multi-manager/hook-beta.sh"',
    '  [test-fixture-gamma.sh]="bin/gamma.py"',
])
s = replace_block(s, "declare -A LIB_FANOUT=(", [
    '  [hooks/multi-manager/lib-alpha.sh]="test-fixture-alpha.sh"',
    '  [hooks/multi-manager/lib-beta.sh]="test-fixture-beta.sh"',
])
open(p, "w").write(s)
PYEOF
}
set_manifest

cd "$REPO_DIR" || { echo "FATAL: cd to fixture repo failed"; exit 2; }
git config user.email "test@test"
git config user.name "Test"
git add -A
git commit -q -m "init fixture"
git push -q "$BARE_DIR" HEAD:refs/heads/main 2>/dev/null
cd - >/dev/null

# Confirm the fixture's own fan-out + test-coverage completeness is clean
# before relying on it for selection assertions — a completeness gap here
# would make the selection results meaningless. test-fixture-excluded.sh is
# deliberately NOT in ALLOW, so it must not trip the test-coverage check
# either (case 6 below) — this assertion is itself a live check of that.
FANOUT_CHECK_OUT=$(bash "$REPO_DIR/.github/ci/impact-manifest.sh" --check 2>&1); FANOUT_CHECK_RC=$?
if [ "$FANOUT_CHECK_RC" -eq 0 ]; then
  pass "fixture setup: the fixture's own COVERS/LIB_FANOUT/test-coverage is complete (--check exits 0) before selection is exercised"
else
  fail "fixture setup: the fixture's own COVERS/LIB_FANOUT/test-coverage is complete (--check exits 0) before selection is exercised" "$FANOUT_CHECK_OUT"
fi

# select_for: STDOUT ONLY — the suite list a real consumer (e.g.
# bin/sable_gate_promote_lib.py's _selected_suites) parses. The
# "::notice::" mode/reason line goes to stderr and is asserted separately
# via select_notice_for, below — merging them here would make every
# exact-match assertion below fragile against the observability line.
select_for() {
  bash "$REPO_DIR/.github/ci/impact-manifest.sh" --select "$@" 2>/dev/null | sort
}

# select_notice_for: just the "::notice::" line(s) on stderr.
select_notice_for() {
  bash "$REPO_DIR/.github/ci/impact-manifest.sh" --select "$@" 2>&1 1>/dev/null
}

# ---------------------------------------------------------------------------
# 1. A changed path under a mapped shared lib selects EVERY suite fanned out
#    to that lib.
# ---------------------------------------------------------------------------
LIB_SEL=$(select_for hooks/multi-manager/lib-alpha.sh)
if [ "$LIB_SEL" = "test-fixture-alpha.sh" ]; then
  pass "lib change (hooks/multi-manager/lib-alpha.sh) selects exactly the suite fanned out to it (test-fixture-alpha.sh)"
else
  fail "lib change (hooks/multi-manager/lib-alpha.sh) selects exactly the suite fanned out to it (test-fixture-alpha.sh)" "got: $LIB_SEL"
fi

# ---------------------------------------------------------------------------
# 2. A changed path under a mapped suite's own covered file selects EXACTLY
#    that suite.
# ---------------------------------------------------------------------------
MAPPED_SEL=$(select_for hooks/multi-manager/hook-beta.sh)
if [ "$MAPPED_SEL" = "test-fixture-beta.sh" ]; then
  pass "mapped covered-file change (hooks/multi-manager/hook-beta.sh) selects exactly test-fixture-beta.sh"
else
  fail "mapped covered-file change (hooks/multi-manager/hook-beta.sh) selects exactly test-fixture-beta.sh" "got: $MAPPED_SEL"
fi

# A suite with no COVERS entry defaults to covering its own file.
STANDALONE_SEL=$(select_for hooks/test/test-fixture-standalone.sh)
if [ "$STANDALONE_SEL" = "test-fixture-standalone.sh" ]; then
  pass "a suite with no COVERS entry defaults to covering itself — changing its own file selects exactly it"
else
  fail "a suite with no COVERS entry defaults to covering itself — changing its own file selects exactly it" "got: $STANDALONE_SEL"
fi

# ---------------------------------------------------------------------------
# 3. A changed path matching NEITHER a lib nor any suite's covered file is
#    UNMAPPED -> the FULL ALLOW set runs (conservative default). LOAD-BEARING
#    NEGATIVE CONTROL (SABLE-m4exv): this must still hold after the fix — a
#    genuinely unmapped path must keep escalating, or the fix has traded a
#    cost problem for a correctness problem.
# ---------------------------------------------------------------------------
FULL_EXPECTED=$(printf 'test-fixture-alpha.sh\ntest-fixture-beta.sh\ntest-fixture-gamma.sh\ntest-fixture-standalone.sh' | sort)
UNMAPPED_SEL=$(select_for unmapped-area/something.py)
if [ "$UNMAPPED_SEL" = "$FULL_EXPECTED" ]; then
  pass "unmapped path (unmapped-area/something.py) selects the FULL ALLOW set (conservative default)"
else
  fail "unmapped path (unmapped-area/something.py) selects the FULL ALLOW set (conservative default)" "got: $UNMAPPED_SEL"
fi

# The full-set fallback must be BY CONSTRUCTION identical in size to ALLOW,
# not a number that can silently drift apart from it (this is how the real
# manifest's "88"/"89" was validated in SABLE-m4exv's own investigation).
ALLOW_SIZE=$(bash -c "source '$REPO_DIR/.github/ci/shell-run-set.sh' 2>/dev/null; echo \${#ALLOW[@]}")
UNMAPPED_COUNT=$(printf '%s\n' "$UNMAPPED_SEL" | grep -c .)
if [ "$UNMAPPED_COUNT" -eq "$ALLOW_SIZE" ]; then
  pass "the full-set fallback count ($UNMAPPED_COUNT) equals \${#ALLOW[@]} ($ALLOW_SIZE) — cannot silently drift apart"
else
  fail "the full-set fallback count ($UNMAPPED_COUNT) equals \${#ALLOW[@]} ($ALLOW_SIZE) — cannot silently drift apart" "fallback=$UNMAPPED_COUNT allow=$ALLOW_SIZE"
fi

# A real `git diff --name-only`-shaped input, mixing one mapped path with one
# unmapped path in the SAME changeset, still yields the full set (the
# conservative default dominates a partial match, per the selection rule).
MIXED_SEL=$(select_for hooks/multi-manager/hook-alpha.sh unmapped-area/something.py)
if [ "$MIXED_SEL" = "$FULL_EXPECTED" ]; then
  pass "a changeset mixing one mapped path with one unmapped path still selects the FULL ALLOW set (unmapped dominates)"
else
  fail "a changeset mixing one mapped path with one unmapped path still selects the FULL ALLOW set (unmapped dominates)" "got: $MIXED_SEL"
fi

# ---------------------------------------------------------------------------
# 4. (SABLE-m4exv) A suite's OWN test file always self-selects it, even when
#    the suite ALSO has a COVERS entry pointing elsewhere. Before the fix,
#    _covered_files only defaulted to self when COVERS was UNSET, so a
#    mapped suite editing its own file fell through to UNMAPPED -> full set.
#    test-fixture-alpha.sh has a COVERS entry (hook-alpha.sh) — this is
#    exactly that case.
# ---------------------------------------------------------------------------
SELF_WITH_COVERS_SEL=$(select_for hooks/test/test-fixture-alpha.sh)
if [ "$SELF_WITH_COVERS_SEL" = "test-fixture-alpha.sh" ]; then
  pass "SABLE-m4exv: a suite WITH a COVERS entry still self-selects when its own test file changes (test-fixture-alpha.sh)"
else
  fail "SABLE-m4exv: a suite WITH a COVERS entry still self-selects when its own test file changes (test-fixture-alpha.sh)" "got: $SELF_WITH_COVERS_SEL"
fi

# ---------------------------------------------------------------------------
# 5. (SABLE-m4exv) bin/test_X.py maps to its production companion's
#    coverage, for both the plain and _integration.py naming forms. A
#    companion with NO coverage of its own (bin/test_unmapped_thing.py ->
#    bin/unmapped_thing.py, which nothing covers) is still MATCHED — it
#    selects ZERO suites, which is a DIFFERENT, DISTINGUISHABLE outcome from
#    the full-set UNMAPPED fallback (empty output vs every fixture suite).
# ---------------------------------------------------------------------------
PY_SEL=$(select_for bin/test_gamma.py)
if [ "$PY_SEL" = "test-fixture-gamma.sh" ]; then
  pass "SABLE-m4exv: bin/test_gamma.py maps to bin/gamma.py's coverage (test-fixture-gamma.sh)"
else
  fail "SABLE-m4exv: bin/test_gamma.py maps to bin/gamma.py's coverage (test-fixture-gamma.sh)" "got: $PY_SEL"
fi

PY_INTEGRATION_SEL=$(select_for bin/test_gamma_integration.py)
if [ "$PY_INTEGRATION_SEL" = "test-fixture-gamma.sh" ]; then
  pass "SABLE-m4exv: bin/test_gamma_integration.py ALSO maps to bin/gamma.py's coverage (the _integration.py naming form)"
else
  fail "SABLE-m4exv: bin/test_gamma_integration.py ALSO maps to bin/gamma.py's coverage (the _integration.py naming form)" "got: $PY_INTEGRATION_SEL"
fi

PY_NOCOMPANION_SEL=$(select_for bin/test_unmapped_thing.py)
if [ -z "$PY_NOCOMPANION_SEL" ]; then
  pass "SABLE-m4exv: bin/test_unmapped_thing.py (companion has no shell coverage) selects ZERO suites — matched, not escalated to full"
else
  fail "SABLE-m4exv: bin/test_unmapped_thing.py (companion has no shell coverage) selects ZERO suites — matched, not escalated to full" "got: $PY_NOCOMPANION_SEL"
fi

# ---------------------------------------------------------------------------
# 6. (SABLE-m4exv) A suite's own file that is EXCLUDE-listed (deliberately
#    NOT in ALLOW) is still a matched, known path: it selects zero suites
#    (nothing runs for an excluded suite) but must NOT escalate to the full
#    ALLOW set just because it was edited.
# ---------------------------------------------------------------------------
EXCLUDED_SEL=$(select_for hooks/test/test-fixture-excluded.sh)
if [ -z "$EXCLUDED_SEL" ]; then
  pass "SABLE-m4exv: an EXCLUDE-listed suite's own file is matched (zero suites), not escalated to the full ALLOW set"
else
  fail "SABLE-m4exv: an EXCLUDE-listed suite's own file is matched (zero suites), not escalated to the full ALLOW set" "got: $EXCLUDED_SEL"
fi

# ---------------------------------------------------------------------------
# 6b. (SABLE-m4exv) COMPLETENESS: sable_test_coverage_check must FIRE on a
#     deliberately introduced, genuinely unmapped test file — a suite
#     classified in NEITHER ALLOW NOR EXCLUDE. Asserting only that the check
#     passes today would pass even if the check were broken outright (a
#     vacuous "return 0"); this proves it actually detects the gap it exists
#     to catch. The orphan file is added and committed AFTER the fixture-
#     setup assertion above so it does not perturb that baseline.
# ---------------------------------------------------------------------------
cd "$REPO_DIR" || { echo "FATAL: cd to fixture repo failed"; exit 2; }
cat > hooks/test/test-fixture-orphan.sh <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
git add -A
git commit -q -m "add deliberately-unmapped orphan test file"
cd - >/dev/null

ORPHAN_OUT=$(bash "$REPO_DIR/.github/ci/impact-manifest.sh" --check-test-coverage 2>&1); ORPHAN_RC=$?
if [ "$ORPHAN_RC" -ne 0 ] && printf '%s' "$ORPHAN_OUT" | grep -q 'test-fixture-orphan.sh'; then
  pass "SABLE-m4exv: --check-test-coverage FIRES on a deliberately unmapped new test file, naming it"
else
  fail "SABLE-m4exv: --check-test-coverage FIRES on a deliberately unmapped new test file, naming it" "rc=$ORPHAN_RC out=$ORPHAN_OUT"
fi

# Adding it to ALLOW (self-mapping is now automatic) clears the error —
# proves the check is a real, fixable gate and not a permanent trap.
set_manifest_with_orphan() {
  python3 - "$REPO_DIR" <<'PYEOF'
import re, sys
fixture = sys.argv[1]
p = f"{fixture}/.github/ci/shell-run-set.sh"
s = open(p).read()
pattern = re.escape("ALLOW=(") + r'.*?\n\)\n'
body = "\n".join([
    "  test-fixture-alpha.sh",
    "  test-fixture-beta.sh",
    "  test-fixture-standalone.sh",
    "  test-fixture-gamma.sh",
    "  test-fixture-orphan.sh",
])
replacement = f"ALLOW=(\n{body}\n)\n"
new_s, n = re.subn(pattern, replacement, s, count=1, flags=re.DOTALL)
assert n == 1
open(p, "w").write(new_s)
PYEOF
}
set_manifest_with_orphan
cd "$REPO_DIR" || { echo "FATAL: cd to fixture repo failed"; exit 2; }
git add -A
git commit -q -m "add orphan suite to ALLOW"
cd - >/dev/null
ORPHAN_FIXED_RC=0
bash "$REPO_DIR/.github/ci/impact-manifest.sh" --check-test-coverage >/dev/null 2>&1 || ORPHAN_FIXED_RC=$?
if [ "$ORPHAN_FIXED_RC" -eq 0 ]; then
  pass "SABLE-m4exv: adding the orphan suite to ALLOW (self-mapping is automatic) clears --check-test-coverage"
else
  fail "SABLE-m4exv: adding the orphan suite to ALLOW (self-mapping is automatic) clears --check-test-coverage" "rc=$ORPHAN_FIXED_RC"
fi

# ---------------------------------------------------------------------------
# 7. (SABLE-m4exv) Observability: --select emits a "::notice::" mode/reason
#    line on stderr — SCOPED naming the suite/path counts, FULL naming the
#    unmapped path(s) by name. Before this fix, --select emitted no mode
#    line at all, so a correct-and-conservative FULL answer was byte-for-
#    byte identical to a genuinely-broken-selector FULL answer.
# ---------------------------------------------------------------------------
SCOPED_NOTICE=$(select_notice_for hooks/multi-manager/hook-beta.sh)
case "$SCOPED_NOTICE" in
  *"::notice::impact-manifest: SCOPED"*)
    pass "SABLE-m4exv: a scoped selection emits an '::notice:: ... SCOPED' line on stderr"
    ;;
  *)
    fail "SABLE-m4exv: a scoped selection emits an '::notice:: ... SCOPED' line on stderr" "got: $SCOPED_NOTICE"
    ;;
esac

FULL_NOTICE=$(select_notice_for unmapped-area/something.py)
case "$FULL_NOTICE" in
  *"::notice::impact-manifest: FULL"*"unmapped-area/something.py"*)
    pass "SABLE-m4exv: an unmapped selection emits an '::notice:: ... FULL -- unmapped path(s): ...' line naming the path"
    ;;
  *)
    fail "SABLE-m4exv: an unmapped selection emits an '::notice:: ... FULL -- unmapped path(s): ...' line naming the path" "got: $FULL_NOTICE"
    ;;
esac

# stdout must stay a clean suite list with NO "::"-prefixed lines mixed in —
# this is the property bin/sable_gate_promote_lib.py's _selected_suites
# parsing depends on (it filters "::"-prefixed lines, but stdout should
# never have needed that filter for THIS reason in the first place).
STDOUT_ONLY=$(bash "$REPO_DIR/.github/ci/impact-manifest.sh" --select hooks/multi-manager/hook-beta.sh 2>/dev/null)
if ! printf '%s' "$STDOUT_ONLY" | grep -q '^::'; then
  pass "SABLE-m4exv: the observability line goes to stderr only — stdout carries no '::'-prefixed line"
else
  fail "SABLE-m4exv: the observability line goes to stderr only — stdout carries no '::'-prefixed line" "got: $STDOUT_ONLY"
fi

# ---------------------------------------------------------------------------
# Exercise via real `git diff --name-only` output (not hand-typed paths):
# amend hook-alpha.sh in the fixture repo and feed the actual diff output on
# stdin.
# ---------------------------------------------------------------------------
cd "$REPO_DIR" || { echo "FATAL: cd to fixture repo failed"; exit 2; }
echo "# touched" >> hooks/multi-manager/hook-alpha.sh
git add -A
git commit -q -m "touch hook-alpha"
DIFF_SEL=$(git diff --name-only HEAD~1 HEAD | bash "$REPO_DIR/.github/ci/impact-manifest.sh" --select 2>/dev/null | sort)
cd - >/dev/null

if [ "$DIFF_SEL" = "test-fixture-alpha.sh" ]; then
  pass "real 'git diff --name-only' output piped on stdin selects exactly test-fixture-alpha.sh for a hook-alpha.sh-only commit"
else
  fail "real 'git diff --name-only' output piped on stdin selects exactly test-fixture-alpha.sh for a hook-alpha.sh-only commit" "got: $DIFF_SEL"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
