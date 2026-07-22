#!/usr/bin/env python3
"""sable_coverage_floor_lib — coverage floor on pruning passes (SABLE-cmar4.5).

Story S3 (locked: diff-cover patch-coverage semantics — strict patch gate,
lenient project floor). A PRUNING diff — one that removes a test function,
adds a skip marker, or deletes a test file — is not inherently wrong, but it
is exactly the shape of change that can silently erase coverage without any
other signal noticing. This module gives the merge gate a mechanical answer
to "did this pruning diff carry a coverage-delta check": detect pruning from
a unified diff (PURE, no I/O — unit-tested directly), and decide whether a
pruning diff may promote given the real result of that check (also PURE,
enumerable like sable_gate_promote_lib.decide_promotion).

Actually RUNNING the check (pytest + coverage + diff-cover against a checked
-out tree) is I/O and lives in sable_gate_promote_lib.run_coverage_floor_check,
which shells out to .github/ci/diff-cover-gate.sh — the invocation itself is
kept in .github/ci/ so it can be run standalone (by hand, or by CI) the same
way the merge gate runs it.

KNOWN RESIDUAL (accepted at the TEST-STRATEGY gate, cmar4 S3.7): this floor
has teeth only on the pytest/bin/ half. A weakened hooks/test/*.sh suite BODY
is invisible to diff-cover (it is not a coverage.py/pytest tool); only a
removed MAPPING is caught, by the separate manifest-completeness check
(SABLE-cmar4.2). Do not extend this module to fake shell coverage — that is
the accepted gap, not a bug in this file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Pruning detection — PURE, operates on unified diff text only
# --------------------------------------------------------------------------

_DIFF_HEADER_RE = re.compile(r'^diff --git a/(\S+) b/(\S+)')
_DELETED_MODE_RE = re.compile(r'^deleted file mode')
_REMOVED_TEST_FN_RE = re.compile(r'^-\s*(?:async\s+)?def\s+(test_\w+)\s*\(')
_ADDED_TEST_FN_RE = re.compile(r'^\+\s*(?:async\s+)?def\s+(test_\w+)\s*\(')
_ADDED_SKIP_RE = re.compile(
    r'^\+.*(?:@pytest\.mark\.skip\b|@unittest\.skip\b|pytest\.skip\()')
# Naming conventions this repo actually uses (see CLAUDE.md: bin/test_*.py,
# hooks/test/test-*.sh) plus the common test_foo.py / foo_test.py shapes.
_TEST_FILE_RE = re.compile(
    r'(^|/)(test_[\w]+\.py|[\w]+_test\.py|test-[\w-]+\.sh)$')


@dataclass
class PruningSignal:
    removed_test_functions: list = field(default_factory=list)
    newly_skipped_markers: int = 0
    deleted_test_files: list = field(default_factory=list)

    @property
    def is_pruning(self) -> bool:
        return bool(self.removed_test_functions or self.newly_skipped_markers
                    or self.deleted_test_files)

    @property
    def reasons(self) -> list:
        out = []
        if self.removed_test_functions:
            out.append("removed test function(s): "
                       + ", ".join(self.removed_test_functions))
        if self.newly_skipped_markers:
            out.append(f"{self.newly_skipped_markers} newly-added skip marker(s)")
        if self.deleted_test_files:
            out.append("deleted test file(s): " + ", ".join(self.deleted_test_files))
        return out


def detect_pruning(diff_text: str) -> PruningSignal:
    """Scan a unified diff (as produced by `git diff <base>...<branch>`) for
    the three named pruning shapes. A function removed in one hunk and
    re-added elsewhere in the SAME diff (a move/rename) nets out and is not
    reported — only a NET removal counts."""
    removed_fns: set = set()
    added_fns: set = set()
    skip_count = 0
    deleted_files: list = []
    current_file = None
    file_deleted = False

    def _flush():
        if current_file is not None and file_deleted and _TEST_FILE_RE.search(current_file):
            deleted_files.append(current_file)

    for line in diff_text.splitlines():
        m = _DIFF_HEADER_RE.match(line)
        if m:
            _flush()
            current_file = m.group(2)
            file_deleted = False
            continue
        if _DELETED_MODE_RE.match(line):
            file_deleted = True
            continue
        m = _REMOVED_TEST_FN_RE.match(line)
        if m:
            removed_fns.add(m.group(1))
            continue
        m = _ADDED_TEST_FN_RE.match(line)
        if m:
            added_fns.add(m.group(1))
            continue
        if _ADDED_SKIP_RE.match(line):
            skip_count += 1
    _flush()

    net_removed = sorted(removed_fns - added_fns)
    return PruningSignal(removed_test_functions=net_removed,
                         newly_skipped_markers=skip_count,
                         deleted_test_files=deleted_files)


# --------------------------------------------------------------------------
# Named-reason override — mirrors hooks/multi-manager/pre-dispatch-model-
# check.sh's "Model override: <reason>" line exactly (same shape, same
# non-empty-reason requirement), just a different tag.
# --------------------------------------------------------------------------

def parse_named_override(text: str, tag: str = "Coverage override"):
    """Find a `<tag>: <reason>` line and return the trimmed reason. Two forms
    are accepted, both requiring a non-empty reason:

      1. A `<tag>: <reason>` LINE embedded anywhere in `text` (case-
         insensitive, multiline search) — mirrors pre-dispatch-model-check.sh's
         `^Model override:[[:space:]]+\\S` regex exactly, for text blobs
         (bead notes, dispatch prompts) that carry the tag inline.
      2. `text` itself IS the reason, tag-free — the shape
         `sable-merge-gate promote --coverage-override "<reason>"` produces,
         where the CLI flag's value already is the reason and does not repeat
         the tag.

    Returns None if `text` is empty/whitespace-only in both forms, i.e. an
    override is never inferred from an absent or blank reason."""
    if not text or not text.strip():
        return None
    pattern = re.compile(rf'(?im)^{re.escape(tag)}:[ \t]+(\S.*)$')
    m = pattern.search(text)
    if m:
        return m.group(1).strip()
    # No valid "tag: reason" line. If the tag appears at all — just with a
    # missing or blank reason — this is a BOGUS override attempt, not a bare
    # reason, and must not fall through to the bare-text branch below (which
    # would otherwise launder the tag text itself into a truthy "reason").
    if re.search(rf'(?im)^{re.escape(tag)}:', text):
        return None
    return text.strip()


# --------------------------------------------------------------------------
# The decision table — PURE and total, same style as
# sable_gate_promote_lib.decide_promotion so it is checkable by enumeration.
# --------------------------------------------------------------------------

ACTION_ALLOW = "allow"
ACTION_DENY = "deny"


@dataclass
class CoverageFloorDecision:
    action: str
    reason: str


def evaluate_coverage_floor(signal: PruningSignal, coverage_check_passed,
                            override_reason) -> CoverageFloorDecision:
    """coverage_check_passed is tri-valued: True (diff-cover ran and the patch
    met --fail-under), False (diff-cover ran and failed it), or None (the
    branch does not carry the check at all, or it could not be run) — None and
    False are handled IDENTICALLY here, both deny, because "we could not prove
    it's covered" and "we proved it's not" both mean the same thing to a
    promotion: don't. The tri-state exists only so callers can say which
    happened, exactly as sable_gate_promote_lib.decide_promotion's `disjoint`
    parameter does for the base-moved decision."""
    if not signal.is_pruning:
        return CoverageFloorDecision(ACTION_ALLOW,
            "not a pruning diff — no coverage-delta check required")

    if override_reason:
        return CoverageFloorDecision(ACTION_ALLOW,
            f"pruning diff ({'; '.join(signal.reasons)}), named override: {override_reason}")

    if coverage_check_passed is True:
        return CoverageFloorDecision(ACTION_ALLOW,
            f"pruning diff ({'; '.join(signal.reasons)}), diff-cover patch-coverage "
            f"check passed")

    if coverage_check_passed is False:
        return CoverageFloorDecision(ACTION_DENY,
            f"pruning diff ({'; '.join(signal.reasons)}) and the diff-cover "
            f"patch-coverage check FAILED — coverage regressed on the "
            f"removed/skipped test's lines")

    return CoverageFloorDecision(ACTION_DENY,
        f"pruning diff ({'; '.join(signal.reasons)}) but no coverage-delta check "
        f"(.github/ci/diff-cover-gate.sh, diff-cover --fail-under) was carried "
        f"on this branch — DENIED. Add the check or a "
        f"'Coverage override: <reason>' line.")
