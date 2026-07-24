#!/usr/bin/env python3
"""Coverage floor on pruning passes (SABLE-cmar4.5, story S3).

PURE unit coverage for sable_coverage_floor_lib: pruning detection from a
unified diff (removed test function, newly-added skip marker, deleted test
file), named-override parsing (mirrors hooks/multi-manager/pre-dispatch-
model-check.sh's "Model override: <reason>" line), and the allow/deny
decision table. No subprocess, no git, no filesystem — exactly the "PURE,
so it is checkable by enumeration" style sable_gate_promote_lib.
decide_promotion already uses for the adjacent base-moved decision.

Real diff-cover, a real checked-out worktree, and the merge gate's actual
deny path (bin/sable_gate_promote_lib.assert_coverage_floor) are exercised
against a REAL temp repo in hooks/test/test-coverage-floor-gate.sh — this
file only covers the pure logic feeding that path.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import sable_coverage_floor_lib as cf  # noqa: E402


# --------------------------------------------------------------------------
# detect_pruning — removed test function
# --------------------------------------------------------------------------

def test_removed_test_function_is_detected():
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
index 1111111..2222222 100644
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -10,7 +10,3 @@ def test_helper():
     pass


-def test_edge_case_that_covers_the_branch():
-    assert foo(-1) == "negative"
-
"""
    signal = cf.detect_pruning(diff)
    assert signal.is_pruning
    assert signal.removed_test_functions == ["test_edge_case_that_covers_the_branch"]
    assert signal.newly_skipped_markers == 0
    assert signal.deleted_test_files == []


def test_a_moved_test_function_nets_to_zero():
    """Removed in one hunk, re-added elsewhere in the SAME diff (a rename/move)
    is not pruning — only a NET reduction in the function-COUNT counts
    (SABLE-owix4). removed_test_functions/added_test_functions stay raw
    (informational — they still name what moved), the netting happens via
    net_test_function_delta, not via name subtraction."""
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -1,3 +1,0 @@
-def test_moved(): pass
diff --git a/bin/test_bar.py b/bin/test_bar.py
--- a/bin/test_bar.py
+++ b/bin/test_bar.py
@@ -0,0 +1,1 @@
+def test_moved(): pass
"""
    signal = cf.detect_pruning(diff)
    assert not signal.is_pruning
    assert signal.net_test_function_delta == 0
    assert signal.removed_test_functions == ["test_moved"]
    assert signal.added_test_functions == ["test_moved"]


def test_renamed_and_strengthened_test_is_not_pruning():
    """The concrete SABLE-owix4 / SABLE-be4lo.7 instance: a test is renamed
    to reflect a STRONGER invariant (two unordered writers pinned ->
    three writers pinned in source order). One def name vanishes and a
    differently-named, stronger successor appears elsewhere in the same
    file. THE PROPERTY THE FLOOR WANTS is "did coverage decrease" — it did
    not, it went up — so this must be allowed silently, with no coverage
    -delta check required at all, exactly like a plain move/rename."""
    diff = """\
diff --git a/bin/test_sable_gate_promote_lib.py b/bin/test_sable_gate_promote_lib.py
--- a/bin/test_sable_gate_promote_lib.py
+++ b/bin/test_sable_gate_promote_lib.py
@@ -50,10 +50,12 @@
-def test_the_module_has_exactly_two_writers_to_the_integration_branch():
-    writers = {"optimus", "tarzan"}
-    assert set(get_writers()) == writers
+def test_the_module_has_exactly_three_writers_to_the_integration_branch():
+    writers = ["optimus", "tarzan", "chuck"]
+    assert get_writers_in_source_order() == writers
+    assert "lincoln" not in get_writers_in_source_order()
"""
    signal = cf.detect_pruning(diff)
    assert not signal.is_pruning
    decision = cf.evaluate_coverage_floor(signal, None, None)
    assert decision.action == cf.ACTION_ALLOW
    assert "not a pruning diff" in decision.reason


def test_genuine_deletion_with_no_replacement_is_pruning_and_denies():
    """NEGATIVE CONTROL (SABLE-owix4): a test removed with nothing added to
    replace it anywhere in the diff is a real net reduction in the
    function-count and must still be flagged pruning and denied absent a
    passing coverage check or an override — proving the fix does not
    regress into "never deny anything"."""
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -10,6 +10,0 @@
-def test_edge_case_that_covers_the_branch():
-    assert foo(-1) == "negative"
-
"""
    signal = cf.detect_pruning(diff)
    assert signal.is_pruning
    assert signal.net_test_function_delta == -1
    denied_no_check = cf.evaluate_coverage_floor(signal, None, None)
    assert denied_no_check.action == cf.ACTION_DENY
    denied_failed_check = cf.evaluate_coverage_floor(signal, False, None)
    assert denied_failed_check.action == cf.ACTION_DENY


def test_split_test_into_two_is_not_pruning():
    """Recommended additional shape (SABLE-owix4): one test split into two
    is a net INCREASE in function count and must pass, same net-zero-or
    -positive logic as a plain rename."""
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -1,4 +1,8 @@
-def test_positive_and_negative():
-    assert foo(1) == 1
-    assert foo(-1) == -1
+def test_positive():
+    assert foo(1) == 1
+
+def test_negative():
+    assert foo(-1) == -1
"""
    signal = cf.detect_pruning(diff)
    assert not signal.is_pruning
    assert signal.net_test_function_delta == 1
    decision = cf.evaluate_coverage_floor(signal, None, None)
    assert decision.action == cf.ACTION_ALLOW


def test_parametrized_merge_is_pruning_shaped_but_passes_with_coverage_check():
    """Recommended additional shape (SABLE-owix4): several tests merged into
    one @pytest.mark.parametrize function IS a net DECREASE in def count
    (3 -> 1) by this measure — a static diff scan cannot see that the
    parametrize cases replay every input, so this shape is deliberately
    left pruning-shaped rather than special-cased (that would be exactly
    the "another spelling" trap this bead exists to avoid). It must not be
    allowed outright, and it must not be permanently denied either: routed
    to the real coverage-delta check, a passing result (patch coverage
    held, as it does when the cases are preserved) allows it."""
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -1,9 +1,5 @@
-def test_foo_zero():
-    assert foo(0) == 0
-def test_foo_one():
-    assert foo(1) == 1
-def test_foo_negative():
-    assert foo(-1) == -1
+@pytest.mark.parametrize("x,expected", [(0, 0), (1, 1), (-1, -1)])
+def test_foo(x, expected):
+    assert foo(x) == expected
"""
    signal = cf.detect_pruning(diff)
    assert signal.is_pruning
    assert signal.net_test_function_delta == -2
    denied = cf.evaluate_coverage_floor(signal, None, None)
    assert denied.action == cf.ACTION_DENY
    allowed = cf.evaluate_coverage_floor(signal, True, None)
    assert allowed.action == cf.ACTION_ALLOW


# --------------------------------------------------------------------------
# detect_pruning — newly-added skip marker
# --------------------------------------------------------------------------

def test_newly_added_pytest_mark_skip_is_detected():
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -5,6 +5,7 @@
 import pytest

+@pytest.mark.skip(reason="flaky")
 def test_edge_case():
     assert foo(-1) == "negative"
"""
    signal = cf.detect_pruning(diff)
    assert signal.is_pruning
    assert signal.newly_skipped_markers == 1
    assert signal.removed_test_functions == []


def test_newly_added_unittest_skip_is_detected():
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -5,6 +5,7 @@
 class T(unittest.TestCase):
+    @unittest.skip("wip")
     def test_edge_case(self):
         self.assertEqual(foo(-1), "negative")
"""
    signal = cf.detect_pruning(diff)
    assert signal.is_pruning
    assert signal.newly_skipped_markers == 1


def test_two_added_skip_markers_are_both_counted():
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -1,4 +1,6 @@
+@pytest.mark.skip
 def test_a(): pass
+@pytest.mark.skip
 def test_b(): pass
"""
    signal = cf.detect_pruning(diff)
    assert signal.newly_skipped_markers == 2


# --------------------------------------------------------------------------
# detect_pruning — deleted test file
# --------------------------------------------------------------------------

def test_deleted_python_test_file_is_detected():
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
deleted file mode 100644
index 1111111..0000000
--- a/bin/test_foo.py
+++ /dev/null
@@ -1,5 +0,0 @@
-def test_edge_case():
-    assert foo(-1) == "negative"
"""
    signal = cf.detect_pruning(diff)
    assert signal.is_pruning
    assert signal.deleted_test_files == ["bin/test_foo.py"]


def test_deleted_shell_test_file_is_detected():
    diff = """\
diff --git a/hooks/test/test-something.sh b/hooks/test/test-something.sh
deleted file mode 100755
index 1111111..0000000
--- a/hooks/test/test-something.sh
+++ /dev/null
@@ -1,3 +0,0 @@
-#!/usr/bin/env bash
-echo hi
"""
    signal = cf.detect_pruning(diff)
    assert signal.deleted_test_files == ["hooks/test/test-something.sh"]


def test_deleted_non_test_file_is_not_pruning():
    diff = """\
diff --git a/bin/foo.py b/bin/foo.py
deleted file mode 100644
index 1111111..0000000
--- a/bin/foo.py
+++ /dev/null
@@ -1,3 +0,0 @@
-def foo(x):
-    return x
"""
    signal = cf.detect_pruning(diff)
    assert not signal.is_pruning
    assert signal.deleted_test_files == []


# --------------------------------------------------------------------------
# detect_pruning — negative case
# --------------------------------------------------------------------------

def test_non_pruning_diff_is_not_flagged():
    diff = """\
diff --git a/bin/foo.py b/bin/foo.py
--- a/bin/foo.py
+++ b/bin/foo.py
@@ -1,3 +1,4 @@
 def foo(x):
+    x = abs(x)
     return x
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -1,3 +1,6 @@
 def test_foo():
     assert foo(1) == 1
+
+def test_foo_negative():
+    assert foo(-1) == 1
"""
    signal = cf.detect_pruning(diff)
    assert not signal.is_pruning
    assert signal.reasons == []


def test_empty_diff_is_not_pruning():
    signal = cf.detect_pruning("")
    assert not signal.is_pruning


# --------------------------------------------------------------------------
# parse_named_override — requires a non-empty reason
# --------------------------------------------------------------------------

def test_override_line_embedded_in_a_text_blob_is_parsed():
    text = (
        "Some bead notes here.\n"
        "Coverage override: removed a flaky test, replacement covers the same lines\n"
        "More notes.\n"
    )
    assert cf.parse_named_override(text) == (
        "removed a flaky test, replacement covers the same lines")


def test_override_is_case_insensitive():
    text = "coverage OVERRIDE: reason goes here"
    assert cf.parse_named_override(text) == "reason goes here"


def test_bare_reason_with_no_tag_is_accepted():
    """sable-merge-gate promote --coverage-override "<reason>" hands the CLI
    value straight through — it does not repeat the tag."""
    assert cf.parse_named_override("dropped test, coverage moved to test_bar.py") == (
        "dropped test, coverage moved to test_bar.py")


def test_empty_string_is_not_an_override():
    assert cf.parse_named_override("") is None


def test_whitespace_only_is_not_an_override():
    assert cf.parse_named_override("   \n\t  ") is None


def test_none_is_not_an_override():
    assert cf.parse_named_override(None) is None


def test_tag_with_no_reason_falls_back_to_bare_text_and_is_not_none():
    """'Coverage override:' with nothing after it never matches the tag regex
    (which requires \\S after the colon) — but the WHOLE blob is still
    non-empty text, so the bare-text fallback would return it verbatim. This
    pins that a blob whose ONLY content is a bare, reason-less tag line is
    exactly the shape that must be rejected — regressed by requiring the tag
    regex to demand a reason, and separately confirming the fallback path
    does not silently launder an empty reason back into a truthy override."""
    assert cf.parse_named_override("Coverage override:") is None
    assert cf.parse_named_override("Coverage override:   ") is None


# --------------------------------------------------------------------------
# evaluate_coverage_floor — the decision table
# --------------------------------------------------------------------------

def _signal(**kw):
    return cf.PruningSignal(**kw)


def test_non_pruning_diff_always_allows_regardless_of_check_result():
    clean = _signal()
    for passed in (True, False, None):
        decision = cf.evaluate_coverage_floor(clean, passed, None)
        assert decision.action == cf.ACTION_ALLOW


def test_pruning_with_passing_check_allows():
    signal = _signal(removed_test_functions=["test_x"])
    decision = cf.evaluate_coverage_floor(signal, True, None)
    assert decision.action == cf.ACTION_ALLOW
    assert "passed" in decision.reason


def test_pruning_with_failing_check_denies():
    signal = _signal(removed_test_functions=["test_x"])
    decision = cf.evaluate_coverage_floor(signal, False, None)
    assert decision.action == cf.ACTION_DENY
    assert "FAILED" in decision.reason


def test_pruning_with_no_carried_check_denies():
    """None (not carried / could not run) is NOT treated as 'benefit of the
    doubt' — it denies exactly like a proven failure. Fail-closed, mirroring
    assert_not_frozen's unreadable-freeze-file contract."""
    signal = _signal(deleted_test_files=["bin/test_x.py"])
    decision = cf.evaluate_coverage_floor(signal, None, None)
    assert decision.action == cf.ACTION_DENY
    assert "no coverage-delta check" in decision.reason


def test_pruning_with_named_override_allows_even_when_check_failed():
    """The override is a human bypass, checked ahead of the coverage result —
    same contract as promote()'s own --override: 'consults no run at all'."""
    signal = _signal(newly_skipped_markers=1)
    decision = cf.evaluate_coverage_floor(signal, False, "flaky on CI, tracked in SABLE-xyz")
    assert decision.action == cf.ACTION_ALLOW
    assert "flaky on CI, tracked in SABLE-xyz" in decision.reason


def test_pruning_with_named_override_allows_when_check_never_ran():
    signal = _signal(newly_skipped_markers=1)
    decision = cf.evaluate_coverage_floor(signal, None, "reason")
    assert decision.action == cf.ACTION_ALLOW


def test_decision_reason_names_every_pruning_signal():
    signal = _signal(removed_test_functions=["test_a"], newly_skipped_markers=2,
                     deleted_test_files=["bin/test_b.py"])
    decision = cf.evaluate_coverage_floor(signal, None, None)
    assert "test_a" in decision.reason
    assert "2 newly-added skip marker" in decision.reason
    assert "bin/test_b.py" in decision.reason
