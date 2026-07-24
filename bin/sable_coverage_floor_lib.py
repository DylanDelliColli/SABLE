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

FIXED DEFECT (SABLE-owix4): detect_pruning's function-removal leg used to
ask "did a specific def NAME disappear from the diff", which made a test
RENAME invisible to it BY CONSTRUCTION — the same is true of any refactor
that moves a test between files, splits one test into two, or parametrizes
several into one, all of which INCREASE coverage. It now asks the property
the floor actually wants — "did the SIZE of the test-function set go down"
(PruningSignal.net_test_function_delta) — so a rename, move, or split net to
zero-or-positive and pass silently; only a genuine reduction in the number
of test functions is reported as pruning. See detect_pruning's own
docstring for the full reasoning, including why a parametrize-merge is
deliberately left pruning-shaped rather than special-cased.
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
    added_test_functions: list = field(default_factory=list)
    newly_skipped_markers: int = 0
    deleted_test_files: list = field(default_factory=list)

    @property
    def net_test_function_delta(self) -> int:
        """len(added) - len(removed), i.e. did the SIZE of the test-function
        set grow or shrink (SABLE-owix4). This is the property the floor
        actually cares about — not which individual names moved. A rename
        removes one name and adds a different one: delta 0. A move to
        another file: delta 0. A split into two: delta +1. All three land
        at zero-or-positive here and are NOT pruning, regardless of the
        fact that a specific name vanished from the diff. Only a genuine
        reduction in the number of test functions goes negative."""
        return len(self.added_test_functions) - len(self.removed_test_functions)

    @property
    def is_pruning(self) -> bool:
        return bool(self.net_test_function_delta < 0 or self.newly_skipped_markers
                    or self.deleted_test_files)

    @property
    def reasons(self) -> list:
        out = []
        if self.net_test_function_delta < 0:
            removed_str = ", ".join(self.removed_test_functions) or "none"
            added_str = ", ".join(self.added_test_functions) or "none"
            out.append(
                f"net decrease of {-self.net_test_function_delta} test "
                f"function(s) (removed: {removed_str}; added: {added_str})")
        if self.newly_skipped_markers:
            out.append(f"{self.newly_skipped_markers} newly-added skip marker(s)")
        if self.deleted_test_files:
            out.append("deleted test file(s): " + ", ".join(self.deleted_test_files))
        return out


def detect_pruning(diff_text: str) -> PruningSignal:
    """Scan a unified diff (as produced by `git diff <base>...<branch>`) for
    signals correlated with a coverage regression.

    THE PROPERTY detect_pruning's function-count leg ANSWERS (SABLE-owix4):
    did the NUMBER of test functions go down between base and tip — not did
    a specific def NAME disappear. A unified diff only ever shows lines that
    CHANGED, so every `def test_*` line present in base but absent in tip
    appears as a `-` line somewhere in the diff, and every one present in
    tip but absent in base appears as a `+` line, regardless of which file
    it ended up in or what it was renamed to. Comparing the SIZE of those
    two sets (PruningSignal.net_test_function_delta), instead of asking
    which individual names are common to both, is what makes a rename, a
    move to another file, or a split into more functions net to
    zero-or-positive and pass silently — only a genuine NET reduction in
    the number of test functions is reported as pruning.

    (Several tests merged into one @pytest.mark.parametrize function IS
    still a net reduction in def count by this measure — a static diff scan
    cannot see that the parametrize cases replay every input, so that shape
    is deliberately left flagged as pruning-shaped, which routes it to the
    real coverage-delta check rather than allowing or denying it outright;
    see evaluate_coverage_floor.)

    Skip markers and whole-file deletions are kept as direct property
    checks rather than set comparisons: a newly-added skip marker doesn't
    change any def count but does stop a test from running, and this
    module's KNOWN RESIDUAL (see module docstring) never parses shell
    test-file bodies at all, so for a deleted `.sh` suite file-existence is
    the only signal available — there is no name or count to compare."""
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

    return PruningSignal(removed_test_functions=sorted(removed_fns),
                         added_test_functions=sorted(added_fns),
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
