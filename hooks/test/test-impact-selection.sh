#!/usr/bin/env bash
# test-impact-selection.sh — integration tests for the shell impact
# selection rule (SABLE-cmar4.2, story S1 of SABLE-cmar4): given a set of
# changed paths (git diff --name-only style), which suites should run.
#
#   1. a changed path under a mapped shared lib selects EVERY suite fanned
#      out to that lib (LIB_FANOUT)
#   2. a changed path under a mapped suite's own covered file selects
#      EXACTLY that suite
#   3. a changed path matching NEITHER selects the FULL ALLOW set
#      (conservative default — the impact tier's under-selection backstop)
#
# Real bash processes throughout, REAL git repo + REAL `git diff --name-only`
# output feeding sable_select_impacted — no mocks, no bd/dolt. Fixture: a
# throwaway git repo carrying REAL, unmodified copies of
# .github/ci/shell-run-set.sh and .github/ci/impact-manifest.sh, with
# ALLOW/COVERS/LIB_FANOUT replaced wholesale for a small, fully-classified
# fixture universe (same substitution technique as test-impact-manifest.sh
# and test-tier-ssot-consumers.sh).
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

mkdir -p "$REPO_DIR/.github/ci" "$REPO_DIR/hooks/test" "$REPO_DIR/hooks/multi-manager" "$REPO_DIR/unmapped-area"
cp "$PROD_RUNSET"    "$REPO_DIR/.github/ci/shell-run-set.sh"
cp "$PROD_MANIFEST"  "$REPO_DIR/.github/ci/impact-manifest.sh"

# Fixture universe: two suites, each covering its own hook, each hook
# sourcing its own lib. A third suite has no COVERS entry (defaults to
# covering itself) and sources no lib at all.
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
])
s = replace_block(s, "declare -A EXCLUDE=(", [])
s = replace_block(s, "declare -A COVERS=(", [
    '  [test-fixture-alpha.sh]="hooks/multi-manager/hook-alpha.sh"',
    '  [test-fixture-beta.sh]="hooks/multi-manager/hook-beta.sh"',
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

# Confirm the fixture's own fan-out is complete before relying on it for
# selection assertions — a completeness gap here would make the selection
# results meaningless.
FANOUT_CHECK_OUT=$(bash "$REPO_DIR/.github/ci/impact-manifest.sh" --check 2>&1); FANOUT_CHECK_RC=$?
if [ "$FANOUT_CHECK_RC" -eq 0 ]; then
  pass "fixture setup: the fixture's own COVERS/LIB_FANOUT is complete (--check exits 0) before selection is exercised"
else
  fail "fixture setup: the fixture's own COVERS/LIB_FANOUT is complete (--check exits 0) before selection is exercised" "$FANOUT_CHECK_OUT"
fi

select_for() {
  bash "$REPO_DIR/.github/ci/impact-manifest.sh" --select "$@" 2>&1 | sort
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
#    UNMAPPED -> the FULL ALLOW set runs (conservative default).
# ---------------------------------------------------------------------------
FULL_EXPECTED=$(printf 'test-fixture-alpha.sh\ntest-fixture-beta.sh\ntest-fixture-standalone.sh' | sort)
UNMAPPED_SEL=$(select_for unmapped-area/something.py)
if [ "$UNMAPPED_SEL" = "$FULL_EXPECTED" ]; then
  pass "unmapped path (unmapped-area/something.py) selects the FULL ALLOW set (conservative default)"
else
  fail "unmapped path (unmapped-area/something.py) selects the FULL ALLOW set (conservative default)" "got: $UNMAPPED_SEL"
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

# Exercise via real `git diff --name-only` output (not hand-typed paths):
# amend hook-alpha.sh in the fixture repo and feed the actual diff output on
# stdin.
cd "$REPO_DIR" || { echo "FATAL: cd to fixture repo failed"; exit 2; }
echo "# touched" >> hooks/multi-manager/hook-alpha.sh
git add -A
git commit -q -m "touch hook-alpha"
DIFF_SEL=$(git diff --name-only HEAD~1 HEAD | bash "$REPO_DIR/.github/ci/impact-manifest.sh" --select | sort)
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
