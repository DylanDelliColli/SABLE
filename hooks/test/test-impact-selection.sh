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
#   3. (SABLE-y4nom.7.1) a changed path ABSENT from the tree is the DELETED
#      class and selects the FULL ALLOW set with a deletion-naming notice;
#      an EXISTING path with no classification is an ERROR naming its
#      remediation — the old anonymous unmapped->FULL fallback is retired
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
#   7. (SABLE-m4exv, outcome set per SABLE-y4nom.7.1) the observability
#      line: --select emits a "::notice::" mode/reason line on stderr —
#      SCOPED with a suite/path count, or FULL naming its deliberate cause
#      (declared-broad or deleted path(s)) — and "::error::" lines with a
#      nonzero exit for existing unclassified paths
#   8. (SABLE-ak7og) the REAL production declarations select
#      test-shell-run-set-strict.sh for a shell-run-set.sh change, preserving
#      its local-only --check-beads exclusion-freshness gate under
#      proportional pre-push selection
#
# Selector calls run in isolated inherited bash subshells; the setup checks,
# REAL git repo + REAL `git diff --name-only` process boundary, and production
# declaration controls remain fresh processes — no mocks, no bd/dolt. Fixture:
# a throwaway git repo carrying REAL, unmodified copies of
# .github/ci/shell-run-set.sh and .github/ci/impact-manifest.sh, with
# ALLOW/EXCLUDE/COVERS/LIB_FANOUT replaced wholesale for a small,
# fully-classified fixture universe (same substitution technique as
# test-impact-manifest.sh and test-tier-ssot-consumers.sh).
#
# Run with:
#   bash hooks/test/test-impact-selection.sh

set -uo pipefail

SOURCE_REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PROD_RUNSET="$SOURCE_REPO/.github/ci/shell-run-set.sh"
PROD_MANIFEST="$SOURCE_REPO/.github/ci/impact-manifest.sh"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/sable-test-impact-selection.XXXXXX")"
cleanup_impact_selection_fixture() { rm -rf "$TMPROOT"; }
restore_impact_selection_shell_state() {
  # Both sourced declaration files currently set exactly these options and no
  # traps. Reassert the harness contract so a future library-side change cannot
  # enable errexit or replace the cleanup trap in this long-running suite.
  set +e
  set -uo pipefail
  trap cleanup_impact_selection_fixture EXIT
}
trap cleanup_impact_selection_fixture EXIT

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
# SABLE-y4nom.7.1: absence-first DELETED semantics mean every probe path a
# case treats as PRESENT must actually exist. The class-table fixtures and
# the pytest-companion probe files are materialized (and committed) here so
# the setup --check walk sees a fully classified, fully present universe.
mkdir -p "$REPO_DIR/zero-area" "$REPO_DIR/broad-area"
echo zero  > "$REPO_DIR/zero-area/zero-doc.md"
echo broad > "$REPO_DIR/broad-area/authority.json"
echo pyowned > "$REPO_DIR/bin/pyowned_lib.py"
ln -s missing-target "$REPO_DIR/zero-area/broken-link.md"
printf '# probe test for the companion mapping\n' > "$REPO_DIR/bin/test_gamma.py"
printf '# probe test (integration form)\n'        > "$REPO_DIR/bin/test_gamma_integration.py"
printf '# production companion with no shell coverage\n' > "$REPO_DIR/bin/unmapped_thing.py"
printf '# probe test whose companion has no shell coverage\n' > "$REPO_DIR/bin/test_unmapped_thing.py"

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
# SABLE-y4nom.7.1: the fixture universe must be FULLY classified — the
# generalized completeness walk covers every tracked path, so the fixture's
# own infra copies get explicit ZERO rows, and the new-class cases get one
# fixture row per class.
s = replace_block(s, "declare -A ZERO_IMPACT=(", [
    '  [zero-area/zero-doc.md]="fixture zero-impact doc, declared reason"',
    '  [zero-area/broken-link.md]="fixture tracked broken symlink, the AGENTS.md carve-out"',
    '  [.github/ci/shell-run-set.sh]="fixture infra copy"',
    '  [.github/ci/impact-manifest.sh]="fixture infra copy"',
])
s = replace_block(s, "declare -A DECLARED_BROAD=(", [
    '  [broad-area/authority.json]="fixture central authority, declared reason"',
])
s = replace_block(s, "PY_OWNED=(", [
    "  bin/pyowned_lib.py",
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

# Load the fixture declarations and selector functions once. This must stay at
# top level: shell-run-set.sh's `declare -A` arrays would be local and disappear
# if the file were sourced from inside a loader function. impact-manifest.sh
# intentionally sets REPO to the fixture; production paths were captured above
# under SOURCE_REPO before this source.
# shellcheck source=/dev/null
. "$REPO_DIR/.github/ci/impact-manifest.sh"
restore_impact_selection_shell_state

# select_for: STDOUT ONLY — the suite list a real consumer (e.g.
# bin/sable_gate_promote_lib.py's _selected_suites) parses. The
# "::notice::" mode/reason line remains on stderr. The explicit subshell keeps
# selector-local option, trap, variable, and cwd changes out of the harness.
select_for() (
  sable_select_impacted "$@" 2>/dev/null | sort
)

# Cases asserting both output channels call the selector once and retain both
# streams. Fixed files are safe because this suite is serial and TMPROOT is
# unique; the selector itself still runs in an isolated inherited subshell.
capture_select_for() {
  local stdout_file="$TMPROOT/select.stdout"
  local stderr_file="$TMPROOT/select.stderr"
  CAPTURED_SELECT_RC=0
  ( sable_select_impacted "$@" >"$stdout_file" 2>"$stderr_file" ) \
    || CAPTURED_SELECT_RC=$?
  CAPTURED_SELECT_STDOUT="$(<"$stdout_file")"
  CAPTURED_SELECT_SORTED="$(sort <"$stdout_file")"
  CAPTURED_SELECT_STDERR="$(<"$stderr_file")"
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
capture_select_for hooks/multi-manager/hook-beta.sh
MAPPED_SEL="$CAPTURED_SELECT_SORTED"
MAPPED_STDOUT="$CAPTURED_SELECT_STDOUT"
SCOPED_NOTICE="$CAPTURED_SELECT_STDERR"
MAPPED_RC="$CAPTURED_SELECT_RC"
if [ "$MAPPED_RC" -eq 0 ] && [ "$MAPPED_SEL" = "test-fixture-beta.sh" ]; then
  pass "mapped covered-file change (hooks/multi-manager/hook-beta.sh) selects exactly test-fixture-beta.sh"
else
  fail "mapped covered-file change (hooks/multi-manager/hook-beta.sh) selects exactly test-fixture-beta.sh" "rc=$MAPPED_RC got: $MAPPED_SEL"
fi

# A suite with no COVERS entry defaults to covering its own file.
STANDALONE_SEL=$(select_for hooks/test/test-fixture-standalone.sh)
if [ "$STANDALONE_SEL" = "test-fixture-standalone.sh" ]; then
  pass "a suite with no COVERS entry defaults to covering itself — changing its own file selects exactly it"
else
  fail "a suite with no COVERS entry defaults to covering itself — changing its own file selects exactly it" "got: $STANDALONE_SEL"
fi

# ---------------------------------------------------------------------------
# 3. REBUILT under SABLE-y4nom.7.1 (codex round-1 B5: the old labels claimed
#    UNKNOWN->FULL and passed only because the probe path is ABSENT here —
#    they were accidentally exercising the DELETED class). Now they say what
#    they test: a changed path ABSENT from the tree is the DELETED class and
#    selects the FULL ALLOW set with a deletion-naming notice. The
#    EXISTING-unknown ERROR outcome is exercised in the migration section
#    below (R1), where the file is actually created.
# ---------------------------------------------------------------------------
FULL_EXPECTED=$(printf 'test-fixture-alpha.sh\ntest-fixture-beta.sh\ntest-fixture-gamma.sh\ntest-fixture-standalone.sh' | sort)
capture_select_for unmapped-area/something.py
UNMAPPED_SEL="$CAPTURED_SELECT_SORTED"
UNMAPPED_NOTICE="$CAPTURED_SELECT_STDERR"
FULL_NOTICE="$CAPTURED_SELECT_STDERR"
UNMAPPED_RC="$CAPTURED_SELECT_RC"
if [ "$UNMAPPED_RC" -eq 0 ] && [ "$UNMAPPED_SEL" = "$FULL_EXPECTED" ] && printf '%s' "$UNMAPPED_NOTICE" | grep -qi 'delet'; then
  pass "y4nom.7.1: an ABSENT changed path (unmapped-area/something.py, not yet created) is the DELETED class -> FULL set with deletion notice"
else
  fail "y4nom.7.1: an ABSENT changed path (unmapped-area/something.py, not yet created) is the DELETED class -> FULL set with deletion notice" "rc=$UNMAPPED_RC got: $UNMAPPED_SEL notice: $UNMAPPED_NOTICE"
fi

# The full-set deletion selection must be BY CONSTRUCTION identical in size
# to ALLOW, not a number that can silently drift apart from it.
ALLOW_SIZE="${#ALLOW[@]}"
UNMAPPED_COUNT=$(printf '%s\n' "$UNMAPPED_SEL" | grep -c .)
if [ "$UNMAPPED_COUNT" -eq "$ALLOW_SIZE" ]; then
  pass "the full-set deletion selection count ($UNMAPPED_COUNT) equals \${#ALLOW[@]} ($ALLOW_SIZE) — cannot silently drift apart"
else
  fail "the full-set deletion selection count ($UNMAPPED_COUNT) equals \${#ALLOW[@]} ($ALLOW_SIZE) — cannot silently drift apart" "fallback=$UNMAPPED_COUNT allow=$ALLOW_SIZE"
fi

# A changeset mixing one mapped path with one ABSENT path still yields the
# full set — the deletion dominates a partial match.
MIXED_SEL=$(select_for hooks/multi-manager/hook-alpha.sh unmapped-area/something.py)
if [ "$MIXED_SEL" = "$FULL_EXPECTED" ]; then
  pass "y4nom.7.1: a changeset mixing one mapped path with one ABSENT path still selects the FULL set (deletion dominates)"
else
  fail "y4nom.7.1: a changeset mixing one mapped path with one ABSENT path still selects the FULL set (deletion dominates)" "got: $MIXED_SEL"
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
# This is the suite's one deliberate persistent declaration rewrite. Reload at
# top level so the cached ALLOW array and reverse indexes include the orphan;
# ordinary tracked-file creation/deletion does not require a declaration load.
# shellcheck source=/dev/null
. "$REPO_DIR/.github/ci/impact-manifest.sh"
restore_impact_selection_shell_state
ORPHAN_FIXED_RC=0
bash "$REPO_DIR/.github/ci/impact-manifest.sh" --check-test-coverage >/dev/null 2>&1 || ORPHAN_FIXED_RC=$?
if [ "$ORPHAN_FIXED_RC" -eq 0 ]; then
  pass "SABLE-m4exv: adding the orphan suite to ALLOW (self-mapping is automatic) clears --check-test-coverage"
else
  fail "SABLE-m4exv: adding the orphan suite to ALLOW (self-mapping is automatic) clears --check-test-coverage" "rc=$ORPHAN_FIXED_RC"
fi

# ---------------------------------------------------------------------------
# 7. (SABLE-m4exv, outcomes per SABLE-y4nom.7.1) Observability: --select
#    emits a "::notice::" mode/reason line on stderr — SCOPED naming the
#    suite/path counts, FULL naming its deliberate cause (declared-broad or
#    deleted path(s)) — and "::error::" lines for existing unclassified
#    paths. Before m4exv, --select emitted no mode line at all, so one FULL
#    answer was byte-for-byte identical to any other.
# ---------------------------------------------------------------------------
case "$SCOPED_NOTICE" in
  *"::notice::impact-manifest: SCOPED"*)
    pass "SABLE-m4exv: a scoped selection emits an '::notice:: ... SCOPED' line on stderr"
    ;;
  *)
    fail "SABLE-m4exv: a scoped selection emits an '::notice:: ... SCOPED' line on stderr" "got: $SCOPED_NOTICE"
    ;;
esac

# (y4nom.7.1-revised: the probe path is ABSENT here — created only later in
# the migration section — so the notice this case pins is the DELETED cause,
# named per path.)
case "$FULL_NOTICE" in
  *"::notice::impact-manifest: FULL -- deleted path(s):"*"unmapped-area/something.py"*)
    pass "SABLE-m4exv/y4nom.7.1: a full-set DELETED selection emits an '::notice:: ... FULL -- deleted path(s): ...' line naming the path"
    ;;
  *)
    fail "SABLE-m4exv/y4nom.7.1: a full-set DELETED selection emits an '::notice:: ... FULL -- deleted path(s): ...' line naming the path" "got: $FULL_NOTICE"
    ;;
esac

# stdout must stay a clean suite list with NO "::"-prefixed lines mixed in —
# this is the property bin/sable_gate_promote_lib.py's _selected_suites
# parsing depends on (it filters "::"-prefixed lines, but stdout should
# never have needed that filter for THIS reason in the first place).
STDOUT_ONLY="$MAPPED_STDOUT"
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

# ---------------------------------------------------------------------------
# 8. (SABLE-ak7og) Production-declaration regression: this case deliberately
#    invokes the REAL manifest rather than the fixture declaration above.
#    test-shell-run-set-strict.sh case (f) is the local-only executor for
#    shell-run-set.sh --check-beads. A proportional check for changes to the
#    ALLOW/EXCLUDE declarations must therefore select that suite.
# ---------------------------------------------------------------------------
PROD_RUNSET_SEL=$(bash "$PROD_MANIFEST" --select .github/ci/shell-run-set.sh 2>/dev/null | sort)
if printf '%s\n' "$PROD_RUNSET_SEL" | grep -qx 'test-shell-run-set-strict.sh' \
   && printf '%s\n' "$PROD_RUNSET_SEL" | grep -qx 'test-impact-selection.sh'; then
  pass "SABLE-ak7og: a shell-run-set.sh change selects the --check-beads executor and this regression guard"
else
  fail "SABLE-ak7og: a shell-run-set.sh change selects the --check-beads executor and this regression guard" \
       "selected: ${PROD_RUNSET_SEL:-<none>}"
fi

# ---------------------------------------------------------------------------
# 9. (SABLE-slip0.7) The golden install manifest. Like case 8, these run
#    against the REAL production declarations — the defect was in what the real
#    manifest selects for two real paths, and a fixture universe cannot speak
#    to that.
#
#    THE DEFECT: hooks/test/fixtures/install-golden-manifest.txt matched no
#    selection rule, so the SABLE-slip0.1 diff (a change to
#    session-role-anchor.sh plus the matching line-30 hash refresh) escalated
#    the shell plan to the FULL 95-suite ALLOW set purely because the fixture
#    path was unrecognized. That full plan is red at base for unrelated
#    reasons, so the pre-push gate denied a two-line, obviously-correct change
#    and the branch landed with a knowingly stale golden instead.
#
#    Both halves are asserted: the fixture path is now mapped (9a/9b), and —
#    the half that actually prevents a recurrence — every artifact the golden
#    PINS selects the golden suite too (9d), so changing a hook and forgetting
#    the manifest can no longer pass a proportional run.
# ---------------------------------------------------------------------------
GOLDEN_REL="hooks/test/fixtures/install-golden-manifest.txt"
GOLDEN_SUITE="test-install-golden-manifest.sh"
ANCHOR_REL="hooks/multi-manager/session-role-anchor.sh"

GOLDEN_SEL=$(bash "$PROD_MANIFEST" --select "$GOLDEN_REL" 2>/dev/null | sort)
GOLDEN_NOTICE=$(bash "$PROD_MANIFEST" --select "$GOLDEN_REL" 2>&1 1>/dev/null)
if [ "$GOLDEN_SEL" = "$GOLDEN_SUITE" ] && printf '%s' "$GOLDEN_NOTICE" | grep -q 'SCOPED'; then
  pass "SABLE-slip0.7 (9a): the golden fixture path selects exactly $GOLDEN_SUITE — bounded and named, not the full set"
else
  fail "SABLE-slip0.7 (9a): the golden fixture path selects exactly $GOLDEN_SUITE — bounded and named, not the full set" \
       "selected: ${GOLDEN_SEL:-<none>} notice: $GOLDEN_NOTICE"
fi

# 9b: the exact SABLE-slip0.1 changeset — source edit + matching pin refresh.
PAIR_SEL=$(bash "$PROD_MANIFEST" --select "$ANCHOR_REL" "$GOLDEN_REL" 2>/dev/null | sort)
PAIR_NOTICE=$(bash "$PROD_MANIFEST" --select "$ANCHOR_REL" "$GOLDEN_REL" 2>&1 1>/dev/null)
PAIR_N=$(printf '%s\n' "$PAIR_SEL" | grep -c .)
PROD_ALLOW_SIZE=$(bash -c "source '$PROD_RUNSET' 2>/dev/null; echo \${#ALLOW[@]}")
if printf '%s' "$PAIR_NOTICE" | grep -q 'SCOPED' \
   && printf '%s\n' "$PAIR_SEL" | grep -qx "$GOLDEN_SUITE" \
   && [ "$PAIR_N" -lt "$PROD_ALLOW_SIZE" ]; then
  pass "SABLE-slip0.7 (9b): the slip0.1 pair (source + its pin refresh) is SCOPED to $PAIR_N of $PROD_ALLOW_SIZE suites and includes the golden suite"
else
  fail "SABLE-slip0.7 (9b): the slip0.1 pair (source + its pin refresh) is SCOPED to a bounded set including the golden suite" \
       "n=$PAIR_N allow=$PROD_ALLOW_SIZE notice=$PAIR_NOTICE selected: $PAIR_SEL"
fi

# 9c/9d/9e: the NEGATIVE CONTROLS. A fixture carrying real copies of both CI
# scripts, the real golden resolver lib and the real golden fixture — so the
# rule is live — with one ingredient removed per control. Without these, 9a/9b
# would pass just as well against a selector that had been made to return the
# right answer for the wrong reason.
GFIX="$TMPROOT/golden-fixture"
mkdir -p "$GFIX/.github/ci" "$GFIX/hooks/test/fixtures" "$GFIX/hooks/multi-manager"
cp "$PROD_RUNSET"                          "$GFIX/.github/ci/shell-run-set.sh"
cp "$PROD_MANIFEST"                        "$GFIX/.github/ci/impact-manifest.sh"
cp "$SOURCE_REPO/hooks/test/lib-golden-manifest.sh" "$GFIX/hooks/test/lib-golden-manifest.sh"
cp "$SOURCE_REPO/$GOLDEN_REL"                     "$GFIX/hooks/test/fixtures/"
# SABLE-y4nom.7.1: the anchor probe path must EXIST in this fixture —
# absence-first DELETED semantics would otherwise route it to the full set
# and make 9c-setup/9e vacuous.
cp "$SOURCE_REPO/$ANCHOR_REL"                     "$GFIX/$ANCHOR_REL"

# Baseline: with every ingredient present the fixture reproduces the real
# answers, so a difference below is attributable to the removal and not to the
# fixture being wired up wrong.
GFIX_BASE_SEL=$(bash "$GFIX/.github/ci/impact-manifest.sh" --select "$GOLDEN_REL" 2>/dev/null | sort)
GFIX_BASE_ANCHOR=$(bash "$GFIX/.github/ci/impact-manifest.sh" --select "$ANCHOR_REL" 2>/dev/null | sort)
if [ "$GFIX_BASE_SEL" = "$GOLDEN_SUITE" ] && printf '%s\n' "$GFIX_BASE_ANCHOR" | grep -qx "$GOLDEN_SUITE"; then
  pass "SABLE-slip0.7 (9c setup): the control fixture reproduces the real selection for both paths before anything is removed"
else
  fail "SABLE-slip0.7 (9c setup): the control fixture reproduces the real selection for both paths before anything is removed" \
       "fixture=$GFIX_BASE_SEL anchor=$GFIX_BASE_ANCHOR"
fi

# 9c: remove ONLY the COVERS entry that maps the fixture path -> the same path
# must fall back to the FULL ALLOW set, exactly as it did before this fix. This
# is the pre-fix behavior, reproduced on demand.
python3 - "$GFIX/.github/ci/shell-run-set.sh" <<'PYEOF'
import re, sys
p = sys.argv[1]
s = open(p).read()
s2, n = re.subn(r'\n *\[test-install-golden-manifest\.sh\]="[^"]*"', '', s, count=1)
assert n == 1, "COVERS entry for the golden suite not found in the fixture copy"
open(p, "w").write(s2)
PYEOF
# REVISED under SABLE-y4nom.7.1: the negative control's claim — removal of
# the mapping is LOUD, never silently scoped — is preserved and strengthened.
# An existing-but-unclassified path is now an ERROR naming itself and the
# remediation menu (nonzero exit, EMPTY stdout), not a FULL escalation.
NOMAP_SEL=$(bash "$GFIX/.github/ci/impact-manifest.sh" --select "$GOLDEN_REL" 2>/dev/null | sort)
NOMAP_NOTICE=$(bash "$GFIX/.github/ci/impact-manifest.sh" --select "$GOLDEN_REL" 2>&1 1>/dev/null); NOMAP_RC=$?
if [ "$NOMAP_RC" -ne 0 ] && [ -z "$NOMAP_SEL" ] \
   && printf '%s' "$NOMAP_NOTICE" | grep -q "UNKNOWN -- unclassified path: $GOLDEN_REL"; then
  pass "SABLE-slip0.7 (9c, y4nom.7.1-revised): NEGATIVE CONTROL — with the COVERS mapping removed, the golden fixture path is a loud UNKNOWN error naming itself (never silently scoped)"
else
  fail "SABLE-slip0.7 (9c, y4nom.7.1-revised): NEGATIVE CONTROL — with the COVERS mapping removed, the golden fixture path is a loud UNKNOWN error naming itself" \
       "rc=$NOMAP_RC sel=[$NOMAP_SEL] notice=$NOMAP_NOTICE"
fi

# 9d: the pin rule proper. A change to session-role-anchor.sh ALONE — no
# fixture edit at all — must select the golden suite. This is the property that
# makes a stale pin unlandable: SABLE-slip0.1 pushed exactly this diff, and
# nothing in the gate had anything to say about the manifest it left behind.
ANCHOR_SEL=$(bash "$PROD_MANIFEST" --select "$ANCHOR_REL" 2>/dev/null | sort)
if printf '%s\n' "$ANCHOR_SEL" | grep -qx "$GOLDEN_SUITE"; then
  pass "SABLE-slip0.7 (9d): a pinned artifact changing ALONE still selects the golden suite — a stale pin cannot ride along unverified"
else
  fail "SABLE-slip0.7 (9d): a pinned artifact changing ALONE still selects the golden suite" "selected: $ANCHOR_SEL"
fi

# 9e: NEGATIVE CONTROL for 9d — the pin rule is derived from the golden, not
# from a static list. Remove the golden from the fixture and the same path stops
# selecting the golden suite (while still selecting its ordinary COVERS
# suites), proving 9d's selection came from reading the manifest.
rm -f "$GFIX/hooks/test/fixtures/install-golden-manifest.txt"
NOGOLD_SEL=$(bash "$GFIX/.github/ci/impact-manifest.sh" --select "$ANCHOR_REL" 2>/dev/null | sort)
if ! printf '%s\n' "$NOGOLD_SEL" | grep -qx "$GOLDEN_SUITE" \
   && printf '%s\n' "$NOGOLD_SEL" | grep -qx 'test-session-role-anchor.sh'; then
  pass "SABLE-slip0.7 (9e): NEGATIVE CONTROL — with the golden removed, the pin rule goes inert (the artifact keeps its ordinary COVERS suites but no longer selects the golden suite)"
else
  fail "SABLE-slip0.7 (9e): NEGATIVE CONTROL — with the golden removed, the pin rule goes inert" "selected: $NOGOLD_SEL"
fi

# ===========================================================================
# SABLE-y4nom.7.1 — atomic classification migration (expectations bound to
# ledger v4.2, measured 2026-08-01 post-slip0.7: MAP 61 / PY 20 / ZERO 22 /
# DECLARED_BROAD 8 over the 111-path inventory; golden-derived selection is
# a DISTINCT production rule and is never duplicated into COVERS). UNKNOWN
# becomes an ERROR naming its remediation (never FULL, never zero);
# ZERO_IMPACT / DECLARED_BROAD / PY_OWNED become declared classes; a changed
# path ABSENT from the validated tree is the DELETED class (deliberate BROAD
# with a deletion-naming reason); the completeness walk covers EVERY tracked
# path.
# ===========================================================================

# The classification tables were replaced with fixture entries at
# set_manifest time (setup), and the class fixture files were materialized
# there too, so the fixture universe — including its own infra copies — is
# fully classified and fully present before the setup --check assertion.
# The full-set expectation is recomputed HERE because earlier cases mutate
# the fixture ALLOW (the m4exv orphan-suite case commits a fifth suite).
FULL_NOW=$(printf '%s\n' "${ALLOW[@]}" | sort)
echo x > "$REPO_DIR/unmapped-area/something.py"

# R1: an EXISTING unclassified path is an ERROR naming path + remediation —
# exit nonzero, EMPTY stdout (no suites to certify), never the full set.
capture_select_for unmapped-area/something.py
R1_OUT="$CAPTURED_SELECT_STDERR"; R1_RC="$CAPTURED_SELECT_RC"
R1_STDOUT="$CAPTURED_SELECT_STDOUT"
if [ "$R1_RC" -ne 0 ] && [ -z "$R1_STDOUT" ] \
   && printf '%s' "$R1_OUT" | grep -q 'unmapped-area/something.py' \
   && printf '%s' "$R1_OUT" | grep -qi 'ZERO_IMPACT\|zero-impact'; then
  pass "y4nom.7.1 R1: existing unclassified path errors (nonzero, empty stdout, names path + remediation)"
else
  fail "y4nom.7.1 R1: existing unclassified path errors (nonzero, empty stdout, names path + remediation)" "rc=$R1_RC stdout=[$R1_STDOUT] err=[${R1_OUT:0:200}]"
fi

# ZERO_IMPACT class: matched, zero suites, SCOPED notice — and never FULL.
capture_select_for zero-area/zero-doc.md
ZI_SEL="$CAPTURED_SELECT_SORTED"
ZI_NOTICE="$CAPTURED_SELECT_STDERR"
if [ -z "$ZI_SEL" ] && printf '%s' "$ZI_NOTICE" | grep -q 'SCOPED'; then
  pass "y4nom.7.1 ZERO_IMPACT: declared zero-impact path is matched with zero suites (SCOPED, not FULL)"
else
  fail "y4nom.7.1 ZERO_IMPACT: declared zero-impact path is matched with zero suites (SCOPED, not FULL)" "sel=[$ZI_SEL] notice=[$ZI_NOTICE]"
fi

# DECLARED_BROAD class: full ALLOW set, notice names the declared-broad
# path — FULL stays expressible without being the unknown default (G3).
capture_select_for broad-area/authority.json
DB_SEL="$CAPTURED_SELECT_SORTED"
DB_NOTICE="$CAPTURED_SELECT_STDERR"
if [ "$DB_SEL" = "$FULL_NOW" ] && printf '%s' "$DB_NOTICE" | grep -qi 'declared-broad\|declared broad'; then
  pass "y4nom.7.1 DECLARED_BROAD: declared-broad path selects the full ALLOW set with a naming notice (G3: FULL expressible)"
else
  fail "y4nom.7.1 DECLARED_BROAD: declared-broad path selects the full ALLOW set with a naming notice (G3: FULL expressible)" "sel=[$DB_SEL] notice=[$DB_NOTICE]"
fi

# PY_OWNED class: matched, zero shell suites (python lane owns validation).
capture_select_for bin/pyowned_lib.py
PYO_SEL="$CAPTURED_SELECT_SORTED"
PYO_NOTICE="$CAPTURED_SELECT_STDERR"
if [ -z "$PYO_SEL" ] && printf '%s' "$PYO_NOTICE" | grep -q 'SCOPED'; then
  pass "y4nom.7.1 PY_OWNED: python-owned exact entry is matched with zero shell suites"
else
  fail "y4nom.7.1 PY_OWNED: python-owned exact entry is matched with zero shell suites" "sel=[$PYO_SEL] notice=[$PYO_NOTICE]"
fi

# R7 moved to the END of this section (codex round-1 B1: the original probe
# was a fabricated never-classified absent string, which missed both real
# seams — a stale classification row surviving its file's deletion, and a
# rename). The real git delete + rename controls mutate the fixture repo, so
# they run after every case that needs the pre-mutation tree.

# R7 discrimination half: existing-unclassified is an ERROR (R1 above);
# the deleted outcomes are asserted in the real controls at section end.

# R2: completeness gate covers EVERY tracked path — a planted, committed,
# EXTENSIONLESS production script with no classification fails --check
# naming it (suffix-based definitions of code miss this repo's bin/sable-*).
printf '#!/usr/bin/env python3\n' > "$REPO_DIR/bin/fixture-prod-script"
chmod +x "$REPO_DIR/bin/fixture-prod-script"
( cd "$REPO_DIR" && git add bin/fixture-prod-script && git commit -q -m plant )
R2_OUT=$(bash "$REPO_DIR/.github/ci/impact-manifest.sh" --check 2>&1); R2_RC=$?
if [ "$R2_RC" -ne 0 ] && printf '%s' "$R2_OUT" | grep -q 'bin/fixture-prod-script'; then
  pass "y4nom.7.1 R2: planted extensionless unclassified production script fails --check by name"
else
  fail "y4nom.7.1 R2: planted extensionless unclassified production script fails --check by name" "rc=$R2_RC out=[${R2_OUT:0:200}]"
fi
( cd "$REPO_DIR" && git rm -q -f bin/fixture-prod-script && git commit -q -m unplant )

# R2-negative: with the plant removed (and fixture extras classified), the
# generalized walk is green — the gate discriminates rather than flagging
# everything.
R2N_OUT=$(bash "$REPO_DIR/.github/ci/impact-manifest.sh" --check 2>&1); R2N_RC=$?
if [ "$R2N_RC" -eq 0 ]; then
  pass "y4nom.7.1 R2-negative: fully classified fixture tree passes the generalized completeness walk"
else
  fail "y4nom.7.1 R2-negative: fully classified fixture tree passes the generalized completeness walk" "out=[${R2N_OUT:0:300}]"
fi

# R4: dual-class is an error — a path in BOTH ZERO_IMPACT and COVERS fails
# --check.
python3 - "$REPO_DIR" <<'PYEOF'
import sys
p = f"{sys.argv[1]}/.github/ci/shell-run-set.sh"
s = open(p).read()
s = s.replace('[zero-area/zero-doc.md]="fixture zero-impact doc, declared reason"',
              '[zero-area/zero-doc.md]="fixture zero-impact doc, declared reason"\n  [hooks/multi-manager/hook-alpha.sh]="dual-class plant"')
open(p, "w").write(s)
PYEOF
R4_OUT=$(bash "$REPO_DIR/.github/ci/impact-manifest.sh" --check 2>&1); R4_RC=$?
if [ "$R4_RC" -ne 0 ] && printf '%s' "$R4_OUT" | grep -q 'hook-alpha.sh'; then
  pass "y4nom.7.1 R4: a path in BOTH ZERO_IMPACT and COVERS fails --check by name"
else
  fail "y4nom.7.1 R4: a path in BOTH ZERO_IMPACT and COVERS fails --check by name" "rc=$R4_RC out=[${R4_OUT:0:200}]"
fi
python3 - "$REPO_DIR" <<'PYEOF'
import sys
p = f"{sys.argv[1]}/.github/ci/shell-run-set.sh"
s = open(p).read()
s = s.replace('\n  [hooks/multi-manager/hook-alpha.sh]="dual-class plant"', '')
open(p, "w").write(s)
PYEOF
# The plant existed only for the fresh-process --check above and is now removed
# byte-for-byte before another cached selector runs. The resident declarations
# never observed it, so re-sourcing here would add work without changing state.

# R3: the four reproduced escalation paths (2026-08-01) against the REAL
# production declarations — each yields its v4.2-ledger classified plan, not
# the 96-suite FULL set.
R3A_SEL=$(bash "$PROD_MANIFEST" --select .claude/sable/state/active-contracts.md 2>/dev/null | sort)
R3A_EXPECTED=$(printf 'test-sable-contract.sh\ntest-session-role-anchor.sh' | sort)
if [ "$R3A_SEL" = "$R3A_EXPECTED" ]; then
  pass "y4nom.7.1 R3: active-contracts.md selects exactly its two audited consumers (not FULL)"
else
  fail "y4nom.7.1 R3: active-contracts.md selects exactly its two audited consumers (not FULL)" "got: [$R3A_SEL]"
fi
R3B_SEL=$(bash "$PROD_MANIFEST" --select sable-potential-improvements.md 2>/dev/null | sort)
R3B_NOTICE=$(bash "$PROD_MANIFEST" --select sable-potential-improvements.md 2>&1 1>/dev/null)
if [ -z "$R3B_SEL" ] && printf '%s' "$R3B_NOTICE" | grep -q 'SCOPED'; then
  pass "y4nom.7.1 R3: sable-potential-improvements.md is declared zero-impact (zero suites, SCOPED)"
else
  fail "y4nom.7.1 R3: sable-potential-improvements.md is declared zero-impact (zero suites, SCOPED)" "sel=[$R3B_SEL] notice=[$R3B_NOTICE]"
fi
# v4.2: close-hold-guard.sh is GOLDEN-PINNED, so its exact set is the iwqox
# suite + the install-surface rule consumer + the golden verifier (via the
# DISTINCT slip0.7 golden-derived rule — never duplicated into COVERS).
R3C_SEL=$(bash "$PROD_MANIFEST" --select hooks/multi-manager/close-hold-guard.sh 2>/dev/null | sort)
R3C_EXPECTED=$(printf 'test-close-hold-guard.sh\ntest-install-golden-manifest.sh\ntest-orchestration-install.sh' | sort)
if [ "$R3C_SEL" = "$R3C_EXPECTED" ]; then
  pass "y4nom.7.1 R3: close-hold-guard.sh selects iwqox suite + install-surface rule + golden verifier (v4.2 exact)"
else
  fail "y4nom.7.1 R3: close-hold-guard.sh selects iwqox suite + install-surface rule + golden verifier (v4.2 exact)" "got: [$R3C_SEL]"
fi
R3D_SEL=$(bash "$PROD_MANIFEST" --select bin/sable_dossier_lib.py 2>/dev/null | sort)
R3D_NOTICE=$(bash "$PROD_MANIFEST" --select bin/sable_dossier_lib.py 2>&1 1>/dev/null)
if [ -z "$R3D_SEL" ] && printf '%s' "$R3D_NOTICE" | grep -q 'SCOPED'; then
  pass "y4nom.7.1 R3: sable_dossier_lib.py is python-owned, unpinned (zero shell suites, SCOPED)"
else
  fail "y4nom.7.1 R3: sable_dossier_lib.py is python-owned, unpinned (zero shell suites, SCOPED)" "sel=[$R3D_SEL] notice=[$R3D_NOTICE]"
fi

# R3E (v4.2): a GOLDEN-PINNED skill file's exact set — dedicated consumer +
# install-surface rule + golden verifier, all three, nothing more.
R3E_SEL=$(bash "$PROD_MANIFEST" --select skills/columbo/SKILL.md 2>/dev/null | sort)
R3E_EXPECTED=$(printf 'test-columbo-quick-mode.sh\ntest-install-golden-manifest.sh\ntest-orchestration-install.sh' | sort)
if [ "$R3E_SEL" = "$R3E_EXPECTED" ]; then
  pass "y4nom.7.1 R3E: golden-pinned skill selects dedicated + install-rule + golden verifier exactly (v4.2)"
else
  fail "y4nom.7.1 R3E: golden-pinned skill selects dedicated + install-rule + golden verifier exactly (v4.2)" "got: [$R3E_SEL]"
fi

# R6/F1: the dash-conversion matcher repair against the REAL declarations —
# bin/columbo-cost-prefilter.py has test_columbo_cost_prefilter.py and must
# classify python-owned (zero shell suites), not unmapped.
R6_SEL=$(bash "$PROD_MANIFEST" --select bin/columbo-cost-prefilter.py 2>/dev/null | sort)
R6_NOTICE=$(bash "$PROD_MANIFEST" --select bin/columbo-cost-prefilter.py 2>&1 1>/dev/null)
if [ -z "$R6_SEL" ] && printf '%s' "$R6_NOTICE" | grep -q 'SCOPED'; then
  pass "y4nom.7.1 R6: dash-named python tool with a dash-converted test classifies python-owned (matcher repair)"
else
  fail "y4nom.7.1 R6: dash-named python tool with a dash-converted test classifies python-owned (matcher repair)" "sel=[$R6_SEL] notice=[$R6_NOTICE]"
fi

# Install-surface rule, EXISTING-file evidence (codex B1 supplement: the
# old probe used an absent hypothetical path, which the corrected DELETED
# precedence now rightly routes to the deletion class). skills/gaudi/
# LANGUAGE.md exists, is an immediate skill file with SKILL.md present, and
# must select the installer suite via the rule (plus its golden pin via the
# distinct slip0.7 rule) — SCOPED, never unknown.
RULE_SEL=$(bash "$PROD_MANIFEST" --select skills/gaudi/LANGUAGE.md 2>/dev/null | sort)
RULE_RC_NOTICE=$(bash "$PROD_MANIFEST" --select skills/gaudi/LANGUAGE.md 2>&1 1>/dev/null); RULE_RC=$?
if printf '%s\n' "$RULE_SEL" | grep -qx 'test-orchestration-install.sh' \
   && printf '%s' "$RULE_RC_NOTICE" | grep -q 'SCOPED'; then
  pass "y4nom.7.1 install-surface rule: an existing immediate skill file classifies to the installer suite by rule (SCOPED)"
else
  fail "y4nom.7.1 install-surface rule: an existing immediate skill file classifies to the installer suite by rule (SCOPED)" "rc=$RULE_RC sel=[$RULE_SEL] notice=[$RULE_RC_NOTICE]"
fi

# B2 boundary matrix — direct function probes against the REAL manifest
# (read-only; --select cannot be used for absent hypotheticals now that
# DELETED precedence is correct). The rule must mirror the installer's
# ACTUAL read surface: immediate mm-hooks only, immediate skill files only
# when SKILL.md exists, plus every live BASE_HOOK_FILES member.
B2_PROBES_OK=1
B2_DETAIL=""
if ( . "$PROD_MANIFEST"; _install_surface_rule_matches skills/no-skill-dir/nested/file.md ) 2>/dev/null; then
  B2_PROBES_OK=0; B2_DETAIL="$B2_DETAIL nested-no-skill-matched;"
fi
if ( . "$PROD_MANIFEST"; _install_surface_rule_matches skills/columbo/nested/deep.md ) 2>/dev/null; then
  B2_PROBES_OK=0; B2_DETAIL="$B2_DETAIL nested-under-skill-matched;"
fi
if ( . "$PROD_MANIFEST"; _install_surface_rule_matches skills/no-skill-md-dir/file.md ) 2>/dev/null; then
  B2_PROBES_OK=0; B2_DETAIL="$B2_DETAIL no-skill-md-matched;"
fi
if ( . "$PROD_MANIFEST"; _install_surface_rule_matches hooks/multi-manager/nested/not-installed.sh ) 2>/dev/null; then
  B2_PROBES_OK=0; B2_DETAIL="$B2_DETAIL nested-mm-matched;"
fi
if [ "$B2_PROBES_OK" -eq 1 ]; then
  pass "y4nom.7.1 B2: rule boundary negatives — nested skills, no-SKILL.md dirs, and nested mm paths never match"
else
  fail "y4nom.7.1 B2: rule boundary negatives — nested skills, no-SKILL.md dirs, and nested mm paths never match" "$B2_DETAIL"
fi
B2_BASE_OK=1
B2_BASE_DETAIL=""
B2_MEMBERS=$( . "$PROD_MANIFEST"; _installer_base_hooks )
if [ -z "$B2_MEMBERS" ]; then
  B2_BASE_OK=0; B2_BASE_DETAIL="BASE_HOOK_FILES parse returned empty"
else
  for _m in $B2_MEMBERS; do
    if ! ( . "$PROD_MANIFEST"; _install_surface_rule_matches "hooks/$_m" ) 2>/dev/null; then
      B2_BASE_OK=0; B2_BASE_DETAIL="$B2_BASE_DETAIL hooks/$_m-missed;"
    fi
  done
fi
if [ "$B2_BASE_OK" -eq 1 ]; then
  pass "y4nom.7.1 B2: every live BASE_HOOK_FILES member matches the rule (parsed from the real installer)"
else
  fail "y4nom.7.1 B2: every live BASE_HOOK_FILES member matches the rule (parsed from the real installer)" "$B2_BASE_DETAIL"
fi

# B7: the two authority files' EXACT scoped sets (containment-only was the
# iwqox anti-pattern the contract rejects). impact-manifest.sh is consumed
# by exactly the two impact suites; shell-run-set.sh by its audited five.
B7_MANIFEST_SEL=$(bash "$PROD_MANIFEST" --select .github/ci/impact-manifest.sh 2>/dev/null | sort)
B7_MANIFEST_WANT=$(printf 'test-impact-manifest.sh\ntest-impact-selection.sh' | sort)
if [ "$B7_MANIFEST_SEL" = "$B7_MANIFEST_WANT" ]; then
  pass "y4nom.7.1 B7: impact-manifest.sh selects EXACTLY its two self-host suites"
else
  fail "y4nom.7.1 B7: impact-manifest.sh selects EXACTLY its two self-host suites" "got=[$B7_MANIFEST_SEL]"
fi
B7_RUNSET_SEL=$(bash "$PROD_MANIFEST" --select .github/ci/shell-run-set.sh 2>/dev/null | sort)
B7_RUNSET_WANT=$(printf 'test-ci-bd-coverage-gap.sh\ntest-impact-manifest.sh\ntest-impact-selection.sh\ntest-sealed-verification.sh\ntest-shell-run-set-strict.sh' | sort)
if [ "$B7_RUNSET_SEL" = "$B7_RUNSET_WANT" ]; then
  pass "y4nom.7.1 B7: shell-run-set.sh selects EXACTLY its audited five consumers"
else
  fail "y4nom.7.1 B7: shell-run-set.sh selects EXACTLY its audited five consumers" "got=[$B7_RUNSET_SEL]"
fi

# B4 combined per-lane plan matrix (codex round-1: exact pins, and they
# live HERE because shell literals never enter the Python literal-dependency
# graph — a bin/test_*.py naming a production path becomes that path's own
# consumer and can never honestly pin "Python: none"). One representative
# per class through the REAL `sable-dev-check --path P --dry-run`, pinning
# BOTH lanes so zero-shell is provably not zero-validation and vice versa.
DEVCHECK_BIN="$SOURCE_REPO/bin/sable-dev-check"
b4_plan() { ( cd "$SOURCE_REPO" && "$DEVCHECK_BIN" --path "$1" --dry-run 2>&1 ); }
# Exact-set extractors: the dry-run indents each PLANNED identity on its own
# line.  Anchor that render boundary instead of grepping any filename from the
# merged stdout/stderr stream.  A loud stale-profile diagnostic legitimately
# names catalog additions (for example ``bin/test_new.py``); counting that
# warning as a planned test made all six exact-set controls false-red while
# their actual plans remained correct (SABLE-y4nom.7.5 overlap preflight).
b4_python_set() { printf '%s\n' "$1" | sed -nE 's/^    (bin\/test_[A-Za-z0-9_]+\.py)$/\1/p' | sort -u; }
b4_shell_set()  { printf '%s\n' "$1" | sed -nE 's/^    (hooks\/test\/test-[A-Za-z0-9-]+\.sh)$/\1/p' | sort -u; }
B4_DIAGNOSTIC_PLANT=$(printf '%s\n' \
  'sable-dev-check: profile mismatch — catalog python_tests added: bin/test_noise.py' \
  '  Python: selected — 1 test file(s)' \
  '    bin/test_real.py' \
  '  Shell: scoped — 1 suite(s)' \
  '    hooks/test/test-real.sh')
if [ "$(b4_python_set "$B4_DIAGNOSTIC_PLANT")" = "bin/test_real.py" ] \
   && [ "$(b4_shell_set "$B4_DIAGNOSTIC_PLANT")" = "hooks/test/test-real.sh" ]; then
  pass "y4nom.7.5: exact plan extraction ignores a profile diagnostic that names a test file"
else
  fail "y4nom.7.5: exact plan extraction ignores a profile diagnostic that names a test file" \
    "py=[$(b4_python_set "$B4_DIAGNOSTIC_PLANT")] sh=[$(b4_shell_set "$B4_DIAGNOSTIC_PLANT")]"
fi
B4_ALLOW_SET=$(bash -c "source '$PROD_RUNSET' 2>/dev/null; printf 'hooks/test/%s\n' \"\${ALLOW[@]}\"" | sort -u)

B4_MAP=$(b4_plan hooks/multi-manager/close-hold-guard.sh); B4_MAP_RC=$?
B4_MAP_WANT=$(printf 'hooks/test/test-close-hold-guard.sh\nhooks/test/test-install-golden-manifest.sh\nhooks/test/test-orchestration-install.sh' | sort)
if [ "$B4_MAP_RC" -eq 0 ] && printf '%s' "$B4_MAP" | grep -q 'Python: none' \
   && [ -z "$(b4_python_set "$B4_MAP")" ] \
   && printf '%s' "$B4_MAP" | grep -q 'Shell: scoped' \
   && [ "$(b4_shell_set "$B4_MAP")" = "$B4_MAP_WANT" ]; then
  pass "y4nom.7.1 B4 MAP: close-hold-guard plans Python EXACTLY none + shell EXACTLY its three suites"
else
  fail "y4nom.7.1 B4 MAP: close-hold-guard plans Python EXACTLY none + shell EXACTLY its three suites" "py=[$(b4_python_set "$B4_MAP")] sh=[$(b4_shell_set "$B4_MAP")]"
fi

B4_ZERO=$(b4_plan sable-potential-improvements.md); B4_ZERO_RC=$?
if [ "$B4_ZERO_RC" -eq 0 ] && printf '%s' "$B4_ZERO" | grep -q 'Python: none' \
   && [ -z "$(b4_python_set "$B4_ZERO")" ] \
   && printf '%s' "$B4_ZERO" | grep -q 'Shell: scoped — 0 suite' \
   && [ -z "$(b4_shell_set "$B4_ZERO")" ]; then
  pass "y4nom.7.1 B4 ZERO: the research log plans EXACTLY zero python tests + zero shell suites (declared, not escalated)"
else
  fail "y4nom.7.1 B4 ZERO: the research log plans EXACTLY zero python tests + zero shell suites (declared, not escalated)" "py=[$(b4_python_set "$B4_ZERO")] sh=[$(b4_shell_set "$B4_ZERO")]"
fi

# ZERO with a live python owner: zero-SHELL provably is not zero-VALIDATION.
B4_ZERO_PY=$(b4_plan .github/ci/test-requirements.txt); B4_ZERO_PY_RC=$?
if [ "$B4_ZERO_PY_RC" -eq 0 ] && printf '%s' "$B4_ZERO_PY" | grep -q 'Python: selected' \
   && [ "$(b4_python_set "$B4_ZERO_PY")" = "bin/test_clean_room_dep_parity.py" ] \
   && printf '%s' "$B4_ZERO_PY" | grep -q 'Shell: scoped — 0 suite' \
   && [ -z "$(b4_shell_set "$B4_ZERO_PY")" ]; then
  pass "y4nom.7.1 B4 ZERO+PY-owner: test-requirements.txt plans EXACTLY its python parity owner + zero shell"
else
  fail "y4nom.7.1 B4 ZERO+PY-owner: test-requirements.txt plans EXACTLY its python parity owner + zero shell" "py=[$(b4_python_set "$B4_ZERO_PY")] sh=[$(b4_shell_set "$B4_ZERO_PY")]"
fi

B4_PY=$(b4_plan bin/sable_dossier_lib.py); B4_PY_RC=$?
if [ "$B4_PY_RC" -eq 0 ] && printf '%s' "$B4_PY" | grep -q 'Python: selected' \
   && [ "$(b4_python_set "$B4_PY")" = "bin/test_sable_dossier.py" ] \
   && printf '%s' "$B4_PY" | grep -q 'Shell: scoped — 0 suite' \
   && [ -z "$(b4_shell_set "$B4_PY")" ]; then
  pass "y4nom.7.1 B4 PY: dossier lib plans EXACTLY bin/test_sable_dossier.py + zero shell suites"
else
  fail "y4nom.7.1 B4 PY: dossier lib plans EXACTLY bin/test_sable_dossier.py + zero shell suites" "py=[$(b4_python_set "$B4_PY")] sh=[$(b4_shell_set "$B4_PY")]"
fi

B4_BROAD=$(b4_plan .claude/settings.json); B4_BROAD_RC=$?
B4_BROAD_PY_WANT=$(printf '%s\n' \
  bin/test_coverage_floor_integration.py \
  bin/test_identifier_decay_integration.py \
  bin/test_merge_report.py \
  bin/test_merge_report_integration.py \
  bin/test_sable_batch_coordinator_lib.py \
  bin/test_sable_gate_promote_integration.py \
  bin/test_sable_gate_promote_lib_integration.py \
  bin/test_sable_onboard.py \
  bin/test_sable_orchestration_install.py \
  bin/test_sable_orchestration_install_integration.py \
  bin/test_sable_telemetry.py \
  bin/test_sable_telemetry_integration.py \
  bin/test_snapshot_classifier.py | sort)
if [ "$B4_BROAD_RC" -eq 0 ] && printf '%s' "$B4_BROAD" | grep -q 'Python: selected' \
   && printf '%s' "$B4_BROAD" | grep -q 'Shell: full' \
   && printf '%s' "$B4_BROAD" | grep -qi 'declared-broad' \
   && [ "$(b4_python_set "$B4_BROAD")" = "$B4_BROAD_PY_WANT" ] \
   && [ "$(b4_shell_set "$B4_BROAD")" = "$B4_ALLOW_SET" ]; then
  pass "y4nom.7.1 B4 BROAD: settings.json plans EXACTLY its 13 python consumers + shell EXACTLY == ALLOW (declared-broad)"
else
  fail "y4nom.7.1 B4 BROAD: settings.json plans EXACTLY its 13 python consumers + shell EXACTLY == ALLOW (declared-broad)" "py=[$(b4_python_set "$B4_BROAD" | tr '\n' ' ')] shN=$(b4_shell_set "$B4_BROAD" | grep -c .)"
fi

B4_DEL=$(b4_plan hooks/multi-manager/removed-by-commit.sh); B4_DEL_RC=$?
if [ "$B4_DEL_RC" -eq 0 ] && printf '%s' "$B4_DEL" | grep -q 'Python: none' \
   && [ -z "$(b4_python_set "$B4_DEL")" ] \
   && printf '%s' "$B4_DEL" | grep -q 'Shell: full' \
   && printf '%s' "$B4_DEL" | grep -qi 'delet' \
   && [ "$(b4_shell_set "$B4_DEL")" = "$B4_ALLOW_SET" ]; then
  pass "y4nom.7.1 B4 DELETED: an absent rule-matchable path plans Python EXACTLY none + shell EXACTLY == ALLOW with the deletion reason"
else
  fail "y4nom.7.1 B4 DELETED: an absent rule-matchable path plans Python EXACTLY none + shell EXACTLY == ALLOW with the deletion reason" "py=[$(b4_python_set "$B4_DEL")] shN=$(b4_shell_set "$B4_DEL" | grep -c .)"
fi

# Codex round-1 live-audit fix: the installer BINARY itself is executed by
# THREE suites (role-card-install binds it via COVERS; orchestration-install
# :15 and project-clone-portability :18 execute the REAL installer) — under
# the old FULL fallback the under-mapping was masked; with FULL gone it
# would have narrowed real coverage. Exact-set pin.
B2_INST_SEL=$(bash "$PROD_MANIFEST" --select bin/sable-orchestration-install 2>/dev/null | sort)
B2_INST_WANT=$(printf 'test-impact-selection.sh\ntest-orchestration-install.sh\ntest-project-clone-portability.sh\ntest-role-card-install.sh' | sort)
if [ "$B2_INST_SEL" = "$B2_INST_WANT" ]; then
  pass "y4nom.7.1: the installer binary selects EXACTLY its four consumers (three executors + the suite pinning _installer_base_hooks, which impact-manifest now reads)"
else
  fail "y4nom.7.1: the installer binary selects EXACTLY its four consumers (three executors + the suite pinning _installer_base_hooks, which impact-manifest now reads)" "got=[$B2_INST_SEL]"
fi

# B2 dot-glob negatives (installer globs run with dotglob OFF; case '*'
# would match leading dots — the rule must not).
B2_DOT_OK=1; B2_DOT_WHY=""
for _dot in "skills/.hidden/SKILL.md" "skills/columbo/.hidden.md" "hooks/multi-manager/.hidden.sh"; do
  if ( . "$PROD_MANIFEST"; _install_surface_rule_matches "$_dot" ) 2>/dev/null; then
    B2_DOT_OK=0; B2_DOT_WHY="$B2_DOT_WHY $_dot-matched;"
  fi
done
if [ "$B2_DOT_OK" -eq 1 ]; then
  pass "y4nom.7.1 B2: hidden-dir/hidden-file paths never match the install rule (dotglob-off semantics)"
else
  fail "y4nom.7.1 B2: hidden-dir/hidden-file paths never match the install rule (dotglob-off semantics)" "$B2_DOT_WHY"
fi

# B2 parser-failure negative: a future BASE_HOOK_FILES formatting change
# that keeps the installer functional but breaks the sed parse must make
# --check nonzero with the named error (the guard exists in code; this
# proves it fires). Sourced override against the real declarations — the
# reverse index makes the extra walk cheap.
B2_PARSE_OUT=$( ( . "$PROD_MANIFEST"; _installer_base_hooks() { :; }; sable_test_coverage_check ) 2>&1 ); B2_PARSE_RC=$?
if [ "$B2_PARSE_RC" -ne 0 ] && printf '%s' "$B2_PARSE_OUT" | grep -q 'BASE_HOOK_FILES line did not parse'; then
  pass "y4nom.7.1 B2: a BASE_HOOK_FILES parse failure fails --check loudly by name (guard proven, not assumed)"
else
  fail "y4nom.7.1 B2: a BASE_HOOK_FILES parse failure fails --check loudly by name (guard proven, not assumed)" "rc=$B2_PARSE_RC out=[$(printf '%s' "$B2_PARSE_OUT" | grep -i base_hook | head -1)]"
fi

# B1 broken-symlink carve-out ([ -e ] || [ -L ]): a tracked BROKEN symlink
# with a ZERO row is PRESENT (the AGENTS.md class) — SCOPED-zero, never
# DELETED->FULL. Locks the contract carve-out instead of relying on the
# comment.
capture_select_for zero-area/broken-link.md
BSL_SEL="$CAPTURED_SELECT_SORTED"
BSL_NOTICE="$CAPTURED_SELECT_STDERR"
if [ -z "$BSL_SEL" ] && printf '%s' "$BSL_NOTICE" | grep -q 'SCOPED'; then
  pass "y4nom.7.1 B1: a tracked BROKEN symlink with a ZERO row stays PRESENT (SCOPED-zero), never DELETED->FULL"
else
  fail "y4nom.7.1 B1: a tracked BROKEN symlink with a ZERO row stays PRESENT (SCOPED-zero), never DELETED->FULL" "sel=[$BSL_SEL] notice=[$BSL_NOTICE]"
fi

# B3/B2-addendum planted-conflict matrix — ONE walk over the REAL
# declarations with five simultaneous plants (each an independent error; a
# second full 436-path walk per plant would multiply suite cost for no
# discrimination gain). Also asserts the golden-pin load COUNT stays small
# (the preload fix): count, never wall-clock (SABLE-af7u4).
B3_TRACE="$TMPROOT/golden-load-trace"
: > "$B3_TRACE"
B3_OUT=$( ( . "$PROD_MANIFEST"; \
  ZERO_IMPACT[skills/gaudi/LANGUAGE.md]="plant: dynamic-rule conflict (golden+install)"; \
  ZERO_IMPACT[hooks/test/test-sable-cli.sh]="plant: ALLOW suite own-file conflict"; \
  ZERO_IMPACT[gone/away-stale-row.md]="plant: stale row for absent path"; \
  DECLARED_BROAD[bin/sable]="plant: broad vs COVERS conflict"; \
  PY_OWNED+=("skills/columbo/columbo-prefilter.py"); \
  SABLE_GOLDEN_PIN_TRACE="$B3_TRACE" sable_test_coverage_check ) 2>&1 ); B3_RC=$?
B3_OK=1; B3_WHY=""
[ "$B3_RC" -eq 0 ] && { B3_OK=0; B3_WHY="rc=0;"; }
printf '%s' "$B3_OUT" | grep -q 'skills/gaudi/LANGUAGE.md is ZERO_IMPACT but also classified by' || { B3_OK=0; B3_WHY="$B3_WHY no-dynamic-conflict;"; }
printf '%s' "$B3_OUT" | grep -q 'GOLDEN-PIN\|INSTALL-RULE' || { B3_OK=0; B3_WHY="$B3_WHY dynamic-sources-unnamed;"; }
printf '%s' "$B3_OUT" | grep -q 'hooks/test/test-sable-cli.sh is ZERO_IMPACT but also classified by' || { B3_OK=0; B3_WHY="$B3_WHY no-self-map-conflict;"; }
printf '%s' "$B3_OUT" | grep -q 'ZERO_IMPACT row for absent path gone/away-stale-row.md' || { B3_OK=0; B3_WHY="$B3_WHY no-stale-row-error;"; }
printf '%s' "$B3_OUT" | grep -q 'bin/sable is DECLARED_BROAD but also classified by' || { B3_OK=0; B3_WHY="$B3_WHY no-broad-conflict;"; }
printf '%s' "$B3_OUT" | grep -q 'skills/columbo/columbo-prefilter.py is outside bin/' || { B3_OK=0; B3_WHY="$B3_WHY no-py-eligibility-error;"; }
if [ "$B3_OK" -eq 1 ]; then
  pass "y4nom.7.1 B3: planted-conflict matrix — dynamic-rule, suite-own-file, stale-row, broad-vs-covers, and PY-outside-bin all error in one walk"
else
  fail "y4nom.7.1 B3: planted-conflict matrix — dynamic-rule, suite-own-file, stale-row, broad-vs-covers, and PY-outside-bin all error in one walk" "$B3_WHY out=[$(printf '%s' "$B3_OUT" | grep error | head -3)]"
fi
B3_LOADS=$(grep -c . "$B3_TRACE")
if [ "$B3_LOADS" -ge 1 ] && [ "$B3_LOADS" -le 3 ]; then
  pass "y4nom.7.1 perf: golden pins loaded $B3_LOADS time(s) across the full walk (preloaded; count-not-time per SABLE-af7u4)"
else
  fail "y4nom.7.1 perf: golden pins loaded $B3_LOADS time(s) across the full walk (expected 1-3 — preload regressed)" "loads=$B3_LOADS"
fi

# No-fork structural control (codex round-1 perf blocker): per-path
# classification must come through the COVERED_BY reverse index, never a
# per-suite _covered_files sweep (~97 forks per path, ~42k across the walk).
# Count, never time: a single-path --select must invoke _covered_files ZERO
# times (fanout --check legitimately still uses it per suite).
PERF_TRACE="$TMPROOT/covered-trace"
: > "$PERF_TRACE"
SABLE_COVERED_TRACE="$PERF_TRACE" bash "$PROD_MANIFEST" --select hooks/multi-manager/close-hold-guard.sh >/dev/null 2>&1
PERF_CALLS=$(grep -c . "$PERF_TRACE")
if [ "$PERF_CALLS" -eq 0 ]; then
  pass "y4nom.7.1 perf: a single-path selection makes ZERO _covered_files calls (reverse index, no per-suite fork sweep)"
else
  fail "y4nom.7.1 perf: a single-path selection makes ZERO _covered_files calls (reverse index, no per-suite fork sweep)" "calls=$PERF_CALLS"
fi

# ---------------------------------------------------------------------------
# R7 REAL deletion + rename controls (codex round-1 B1). These mutate the
# fixture repo and therefore run LAST. Both exercise the stale-row bypass:
# the classification row survives while the file goes away, and absence must
# dominate the stale row.
# ---------------------------------------------------------------------------
( cd "$REPO_DIR" && git rm -q -f bin/gamma.py && git commit -q -m "real delete" )
capture_select_for bin/gamma.py
R7_DEL_SEL="$CAPTURED_SELECT_SORTED"
R7_DEL_NOTICE="$CAPTURED_SELECT_STDERR"
if [ "$R7_DEL_SEL" = "$FULL_NOW" ] && printf '%s' "$R7_DEL_NOTICE" | grep -qi 'delet'; then
  pass "y4nom.7.1 R7: REAL git rm of a COVERS-classified file — stale row cannot narrow the deletion; FULL set + deletion notice"
else
  fail "y4nom.7.1 R7: REAL git rm of a COVERS-classified file — stale row cannot narrow the deletion; FULL set + deletion notice" "sel=[$R7_DEL_SEL] notice=[$R7_DEL_NOTICE]"
fi

( cd "$REPO_DIR" && git mv hooks/multi-manager/hook-alpha.sh hooks/multi-manager/hook-alpha-renamed.sh && git commit -q -m "real rename" )
capture_select_for hooks/multi-manager/hook-alpha.sh
R7_REN_SEL="$CAPTURED_SELECT_SORTED"
R7_REN_NOTICE="$CAPTURED_SELECT_STDERR"
if [ "$R7_REN_SEL" = "$FULL_NOW" ] && printf '%s' "$R7_REN_NOTICE" | grep -qi 'delet'; then
  pass "y4nom.7.1 R7: REAL git mv — the vanished rename SOURCE (still COVERS/LIB_FANOUT-rowed) is the DELETED class"
else
  fail "y4nom.7.1 R7: REAL git mv — the vanished rename SOURCE (still COVERS/LIB_FANOUT-rowed) is the DELETED class" "sel=[$R7_REN_SEL] notice=[$R7_REN_NOTICE]"
fi
R7_DST_OUT=$(bash "$REPO_DIR/.github/ci/impact-manifest.sh" --select hooks/multi-manager/hook-alpha-renamed.sh 2>"$TMPROOT/r7dst.err"); R7_DST_RC=$?
if [ "$R7_DST_RC" -ne 0 ] && grep -q 'UNKNOWN -- unclassified path' "$TMPROOT/r7dst.err"; then
  pass "y4nom.7.1 R7: the rename DESTINATION is present-unclassified and errors — a rename demands reclassification, not inheritance"
else
  fail "y4nom.7.1 R7: the rename DESTINATION is present-unclassified and errors — a rename demands reclassification, not inheritance" "rc=$R7_DST_RC out=[$R7_DST_OUT] err=[$(head -c150 "$TMPROOT/r7dst.err")]"
fi

# B1's measured bypass, now closed against the REAL manifest: an absent
# mm-family path that the install rule WOULD match must be DELETED->FULL,
# never SCOPED-to-the-installer-suite.
B1_SEL=$(bash "$PROD_MANIFEST" --select hooks/multi-manager/removed-by-commit.sh 2>/dev/null | wc -l)
B1_NOTICE=$(bash "$PROD_MANIFEST" --select hooks/multi-manager/removed-by-commit.sh 2>&1 1>/dev/null)
PROD_ALLOW_N=$(bash -c "source '$PROD_RUNSET' 2>/dev/null; echo \${#ALLOW[@]}")
if [ "$B1_SEL" -eq "$PROD_ALLOW_N" ] && printf '%s' "$B1_NOTICE" | grep -qi 'delet'; then
  pass "y4nom.7.1 B1: an absent rule-matchable mm path is DELETED->FULL ($B1_SEL suites), never SCOPED to the installer"
else
  fail "y4nom.7.1 B1: an absent rule-matchable mm path is DELETED->FULL, never SCOPED to the installer" "n=$B1_SEL allow=$PROD_ALLOW_N notice=[$B1_NOTICE]"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
